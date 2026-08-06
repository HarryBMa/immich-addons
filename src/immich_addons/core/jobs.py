"""SQLite job queue with a single background worker thread (PLAN.md §5).

Deliberately small: one table, one worker, no broker. Every addon run — manual, webhook-triggered
or scheduled — becomes a row here, which is what makes the hub's ``/jobs`` page the single honest
record of what the hub did to the library.

Anything left ``running`` when the process died is swept to ``failed (interrupted)`` on startup,
except for addons registered as resumable (year-highlights resumes from its cache, PLAN.md §6.4).
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Keep the tail of the log only; a year-highlights run is chatty.
MAX_LOG_LINES = 400


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobCancelledError(Exception):
    """Raised inside a runner when the job has been cancelled from the UI."""


@dataclass
class Job:
    id: int
    addon: str
    params: dict[str, Any]
    status: JobStatus
    progress: float
    log: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}


@dataclass
class JobContext:
    """Handed to a runner. The only way a runner talks back to the queue."""

    job_id: int
    addon: str
    params: dict[str, Any]
    _queue: JobQueue

    def progress(self, fraction: float, message: str = "") -> None:
        """Report progress in 0..1. Also checks for cancellation, so a runner that reports
        progress regularly is cancellable for free."""
        self.check_cancelled()
        self._queue._set_progress(self.job_id, max(0.0, min(1.0, fraction)))
        if message:
            self.log(message)

    def log(self, line: str) -> None:
        self._queue._append_log(self.job_id, line)

    def artifact(self, kind: str, value: str, label: str = "") -> None:
        """Record something the run produced: a file under /data/output, an Immich album id, …"""
        self._queue._add_artifact(self.job_id, {"kind": kind, "value": value, "label": label})

    def check_cancelled(self) -> None:
        if self._queue.is_cancelled(self.job_id):
            raise JobCancelledError(f"job {self.job_id} cancelled")


Runner = Callable[[JobContext], None]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    addon         TEXT    NOT NULL,
    params_json   TEXT    NOT NULL DEFAULT '{}',
    status        TEXT    NOT NULL DEFAULT 'queued',
    progress      REAL    NOT NULL DEFAULT 0.0,
    log           TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    artifacts_json TEXT   NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class JobQueue:
    """Owns the SQLite file and, optionally, one worker thread.

    Each call opens its own short-lived connection: simpler than sharing one across threads, and
    fast enough for a queue that sees a few jobs an hour.
    """

    def __init__(self, db_path: Path, *, resumable_addons: set[str] | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.resumable_addons = resumable_addons or set()
        self._runners: dict[str, Runner] = {}
        self._pending: queue.Queue[int] = queue.Queue()
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._init_db()

    # --- storage ------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            addon=row["addon"],
            params=json.loads(row["params_json"]),
            status=JobStatus(row["status"]),
            progress=row["progress"],
            log=row["log"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            artifacts=json.loads(row["artifacts_json"]),
        )

    # --- public API ---------------------------------------------------------------------

    def register(self, addon: str, runner: Runner) -> None:
        self._runners[addon] = runner

    def enqueue(self, addon: str, params: dict[str, Any] | None = None) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (addon, params_json, status, created_at) VALUES (?, ?, ?, ?)",
                (addon, json.dumps(params or {}), JobStatus.QUEUED, _now()),
            )
            job_id = int(cur.lastrowid or 0)
        self._pending.put(job_id)
        log.info("queued job %s for addon %s", job_id, addon)
        return job_id

    def get(self, job_id: int) -> Job | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list(self, *, limit: int = 50, addon: str | None = None) -> list[Job]:
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if addon:
            sql += " WHERE addon = ?"
            params.append(addon)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def cancel(self, job_id: int) -> None:
        """Ask a job to stop. Queued jobs end immediately; running ones stop at their next
        ``progress()``/``check_cancelled()`` call, leaving any cache intact."""
        with self._lock:
            self._cancelled.add(job_id)
        job = self.get(job_id)
        if job and job.status is JobStatus.QUEUED:
            self._finish(job_id, JobStatus.CANCELLED, "cancelled before it started")

    def is_cancelled(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def sweep_interrupted(self) -> int:
        """Mark jobs the previous process left ``running`` as failed. Returns how many.

        Resumable addons keep their row queued instead, so restarting the container picks the work
        back up from the cache rather than throwing it away.
        """
        swept = 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, addon FROM jobs WHERE status = ?", (JobStatus.RUNNING,)
            ).fetchall()
            for row in rows:
                if row["addon"] in self.resumable_addons:
                    conn.execute(
                        "UPDATE jobs SET status = ? WHERE id = ?", (JobStatus.QUEUED, row["id"])
                    )
                    self._pending.put(int(row["id"]))
                else:
                    conn.execute(
                        "UPDATE jobs SET status = ?, finished_at = ?, log = log || ? WHERE id = ?",
                        (
                            JobStatus.FAILED,
                            _now(),
                            "\nfailed (interrupted): the hub restarted mid-run\n",
                            row["id"],
                        ),
                    )
                    swept += 1
        if swept:
            log.warning("swept %s interrupted job(s) to failed", swept)
        return swept

    # --- worker -------------------------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread. Idempotent."""
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._requeue_pending()
        self._worker = threading.Thread(target=self._work, name="job-worker", daemon=True)
        self._worker.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._pending.put(-1)  # wake the worker
        if self._worker:
            self._worker.join(timeout=timeout)
            self._worker = None

    def _requeue_pending(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY id", (JobStatus.QUEUED,)
            ).fetchall()
        for row in rows:
            self._pending.put(int(row["id"]))

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._pending.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id < 0:
                continue
            self.run_job(job_id)

    def run_job(self, job_id: int) -> None:
        """Run one job inline. Used by the worker, and directly by tests."""
        job = self.get(job_id)
        if job is None or job.is_terminal:
            return
        if self.is_cancelled(job_id):
            self._finish(job_id, JobStatus.CANCELLED, "cancelled before it started")
            return

        runner = self._runners.get(job.addon)
        if runner is None:
            self._finish(job_id, JobStatus.FAILED, f"no runner registered for addon {job.addon!r}")
            return

        self._mark_running(job_id)
        ctx = JobContext(job_id=job_id, addon=job.addon, params=job.params, _queue=self)
        try:
            runner(ctx)
        except JobCancelledError:
            self._finish(job_id, JobStatus.CANCELLED, "cancelled")
        except Exception as exc:  # noqa: BLE001 - a failing addon must not kill the worker
            log.exception("job %s (%s) failed", job_id, job.addon)
            self._finish(job_id, JobStatus.FAILED, f"{type(exc).__name__}: {exc}")
            self._append_log(job_id, traceback.format_exc().rstrip())
        else:
            self._set_progress(job_id, 1.0)
            self._finish(job_id, JobStatus.DONE, "done")

    # --- mutations used by JobContext ---------------------------------------------------

    def _mark_running(self, job_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
                (JobStatus.RUNNING, _now(), job_id),
            )

    def _finish(self, job_id: int, status: JobStatus, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                (status, _now(), job_id),
            )
        if message:
            self._append_log(job_id, message)

    def _set_progress(self, job_id: int, fraction: float) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (fraction, job_id))

    def _append_log(self, job_id: int, line: str) -> None:
        stamped = f"[{_now()}] {line}"
        with self._connect() as conn:
            row = conn.execute("SELECT log FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            lines = (row["log"].splitlines() + stamped.splitlines())[-MAX_LOG_LINES:]
            conn.execute("UPDATE jobs SET log = ? WHERE id = ?", ("\n".join(lines), job_id))

    def _add_artifact(self, job_id: int, artifact: dict[str, Any]) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT artifacts_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            artifacts = json.loads(row["artifacts_json"])
            artifacts.append(artifact)
            conn.execute(
                "UPDATE jobs SET artifacts_json = ? WHERE id = ?",
                (json.dumps(artifacts), job_id),
            )
