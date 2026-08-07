"""Short-lived selection tokens: the handover from Immich's UI to the hub (PLAN.md Phase 8a).

The flow this exists for: you select photos in Immich, choose "Send to Addons", and land in a
prefilled addon form. The asset IDs have to travel from Immich's page to the hub's page, and the
obvious way — putting them in the URL — is the one thing the plan forbids.

So the browser POSTs the IDs to :func:`create` and gets back an opaque token. The token goes in
the URL *fragment* (``#sel=…``), which browsers never send to a server, and the hub's own page
exchanges it for the IDs over an authenticated request. The consequences are worth stating:

* no asset ID ever appears in a URL, an access log, a proxy log, or a ``Referer`` header;
* the token is useless after 15 minutes and useless after it has been used once by a form;
* holding a token proves nothing on its own — exchanging it requires a logged-in hub session.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Long enough to walk from Immich's tab to a config form; short enough that a token left in a
#: bookmarked URL is worthless.
TTL_S = 15 * 60

#: A hard ceiling on one handover. Selecting an entire library and posting it here should fail
#: fast rather than quietly become a 200 MB request body.
MAX_ASSET_IDS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS selections (
    token       TEXT PRIMARY KEY,
    asset_json  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS selections_expiry_idx ON selections (expires_at);
"""


class SelectionError(ValueError):
    """The payload is not a usable selection."""


@dataclass(frozen=True)
class Selection:
    token: str
    asset_ids: tuple[str, ...]
    expires_at: float

    @property
    def count(self) -> int:
        return len(self.asset_ids)


class SelectionStore:
    """Tokens in SQLite, beside the job queue.

    On disk rather than in memory so a hub restart between "Send to Addons" and the form loading
    does not lose the selection — and so nothing here depends on there being exactly one process.
    """

    def __init__(self, db_path: Path, *, ttl_s: float = TTL_S) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_s = ttl_s
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def create(self, asset_ids: list[str], *, now: float | None = None) -> Selection:
        """Store a selection and return its token. Never logs the IDs."""
        cleaned = [str(a).strip() for a in asset_ids if str(a).strip()]
        if not cleaned:
            raise SelectionError("no asset ids in the selection")
        if len(cleaned) > MAX_ASSET_IDS:
            raise SelectionError(f"too many asset ids (limit {MAX_ASSET_IDS})")

        now = time.time() if now is None else now
        token = secrets.token_urlsafe(24)
        expires_at = now + self.ttl_s
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO selections (token, asset_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token, json.dumps(cleaned), now, expires_at),
            )
        # Count only. The IDs are the thing this module exists to keep out of logs.
        log.info("stored a selection of %s asset(s)", len(cleaned))
        return Selection(token=token, asset_ids=tuple(cleaned), expires_at=expires_at)

    def resolve(self, token: str, *, now: float | None = None) -> Selection | None:
        """The selection behind a token, or ``None`` if it is unknown or expired."""
        now = time.time() if now is None else now
        self.purge(now=now)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM selections WHERE token = ? AND expires_at > ?", (token, now)
            ).fetchone()
        if row is None:
            return None
        return Selection(
            token=row["token"],
            asset_ids=tuple(json.loads(row["asset_json"])),
            expires_at=row["expires_at"],
        )

    def consume(self, token: str, *, now: float | None = None) -> Selection | None:
        """Resolve and delete in one step, so a token cannot be replayed."""
        selection = self.resolve(token, now=now)
        if selection is not None:
            self.delete(token)
        return selection

    def delete(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM selections WHERE token = ?", (token,))

    def purge(self, *, now: float | None = None) -> int:
        """Drop expired rows. Returns how many. Called on every resolve, so the table cannot grow
        without bound even if nothing ever sweeps it deliberately."""
        now = time.time() if now is None else now
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM selections WHERE expires_at <= ?", (now,))
            return cursor.rowcount or 0
