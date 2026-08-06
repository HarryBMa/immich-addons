from __future__ import annotations

import time
from pathlib import Path

import pytest

from immich_addons.core.jobs import JobCancelledError, JobContext, JobQueue, JobStatus


@pytest.fixture
def jobs(tmp_path: Path) -> JobQueue:
    return JobQueue(tmp_path / "jobs.sqlite")


def test_enqueue_then_run_succeeds(jobs: JobQueue) -> None:
    seen: list[JobContext] = []

    def runner(ctx: JobContext) -> None:
        seen.append(ctx)
        ctx.progress(0.5, "halfway")
        ctx.artifact("file", "/data/output/zine_print.pdf", "print PDF")

    jobs.register("zine-maker", runner)
    job_id = jobs.enqueue("zine-maker", {"pages": 8})
    jobs.run_job(job_id)

    job = jobs.get(job_id)
    assert job is not None
    assert job.status is JobStatus.DONE
    assert job.progress == 1.0
    assert job.params == {"pages": 8}
    assert "halfway" in job.log
    assert job.artifacts == [
        {"kind": "file", "value": "/data/output/zine_print.pdf", "label": "print PDF"}
    ]
    assert seen[0].params == {"pages": 8}


def test_a_failing_runner_fails_the_job_and_keeps_the_traceback(jobs: JobQueue) -> None:
    def runner(ctx: JobContext) -> None:
        raise ValueError("lut file is missing")

    jobs.register("auto-lut", runner)
    job_id = jobs.enqueue("auto-lut")
    jobs.run_job(job_id)

    job = jobs.get(job_id)
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert "ValueError: lut file is missing" in job.log
    assert "Traceback" in job.log


def test_an_unregistered_addon_fails_cleanly(jobs: JobQueue) -> None:
    """PLAN.md Phase 2 acceptance: a stub Run must fail cleanly and show its log."""
    job_id = jobs.enqueue("not-installed")
    jobs.run_job(job_id)

    job = jobs.get(job_id)
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert "no runner registered" in job.log


def test_cancel_before_start(jobs: JobQueue) -> None:
    jobs.register("year-highlights", lambda ctx: None)
    job_id = jobs.enqueue("year-highlights")
    jobs.cancel(job_id)
    jobs.run_job(job_id)

    job = jobs.get(job_id)
    assert job is not None
    assert job.status is JobStatus.CANCELLED


def test_cancel_mid_run_stops_at_the_next_progress_call(jobs: JobQueue) -> None:
    steps: list[int] = []

    def runner(ctx: JobContext) -> None:
        for i in range(10):
            ctx.progress(i / 10, f"step {i}")
            steps.append(i)
            if i == 2:
                jobs.cancel(ctx.job_id)

    jobs.register("year-highlights", runner)
    job_id = jobs.enqueue("year-highlights")
    jobs.run_job(job_id)

    job = jobs.get(job_id)
    assert job is not None
    assert job.status is JobStatus.CANCELLED
    assert steps == [0, 1, 2], "the runner kept going after cancellation"


def test_check_cancelled_raises_inside_a_runner(jobs: JobQueue) -> None:
    jobs.register("x", lambda ctx: None)
    job_id = jobs.enqueue("x")
    ctx = JobContext(job_id=job_id, addon="x", params={}, _queue=jobs)
    jobs.cancel(job_id)
    with pytest.raises(JobCancelledError):
        ctx.check_cancelled()


def test_sweep_marks_interrupted_jobs_failed(jobs: JobQueue) -> None:
    job_id = jobs.enqueue("auto-lut")
    jobs._mark_running(job_id)

    assert jobs.sweep_interrupted() == 1

    job = jobs.get(job_id)
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert "interrupted" in job.log


def test_sweep_requeues_resumable_addons(tmp_path: Path) -> None:
    """year-highlights resumes from its cache, so a restart must not throw the work away."""
    jobs = JobQueue(tmp_path / "jobs.sqlite", resumable_addons={"year-highlights"})
    job_id = jobs.enqueue("year-highlights")
    jobs._mark_running(job_id)

    assert jobs.sweep_interrupted() == 0

    job = jobs.get(job_id)
    assert job is not None
    assert job.status is JobStatus.QUEUED


def test_worker_thread_drains_the_queue(jobs: JobQueue) -> None:
    done: list[int] = []
    jobs.register("auto-lut", lambda ctx: done.append(ctx.job_id))

    ids = [jobs.enqueue("auto-lut") for _ in range(3)]
    jobs.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(done) < 3:
            time.sleep(0.02)
    finally:
        jobs.stop()

    assert sorted(done) == sorted(ids)
    assert all(jobs.get(i).status is JobStatus.DONE for i in ids)  # type: ignore[union-attr]


def test_list_is_newest_first_and_filterable(jobs: JobQueue) -> None:
    jobs.enqueue("auto-lut")
    second = jobs.enqueue("zine-maker")

    assert jobs.list()[0].id == second
    assert [j.addon for j in jobs.list(addon="auto-lut")] == ["auto-lut"]


def test_log_is_capped(jobs: JobQueue) -> None:
    from immich_addons.core.jobs import MAX_LOG_LINES

    def runner(ctx: JobContext) -> None:
        for i in range(MAX_LOG_LINES + 50):
            ctx.log(f"line {i}")

    jobs.register("chatty", runner)
    job_id = jobs.enqueue("chatty")
    jobs.run_job(job_id)

    job = jobs.get(job_id)
    assert job is not None
    assert len(job.log.splitlines()) <= MAX_LOG_LINES
    assert "line 0" not in job.log
