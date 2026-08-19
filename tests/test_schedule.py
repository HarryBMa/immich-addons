"""Cron parsing, next-fire arithmetic, and the scheduler's firing policy (PLAN.md Phase 7).

The policy tests matter more than the parser tests: getting "1 January at 03:00" to parse is easy,
and deciding what to do when the hub was switched off that night is the actual design.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from immich_addons.core.config import Settings
from immich_addons.core.jobs import JobQueue, JobStatus
from immich_addons.hub.registry import AddonStore, build_catalog
from immich_addons.hub.schedule import (
    CronError,
    Scheduler,
    describe,
    next_fire,
    parse_cron,
    timezone,
)

REGISTRY = Path(__file__).resolve().parents[1] / "registry" / "index.json"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


# --- parsing ------------------------------------------------------------------------------


def test_every_field_can_be_a_wildcard() -> None:
    cron = parse_cron("* * * * *")
    assert len(cron.minute) == 60
    assert len(cron.hour) == 24
    assert cron.matches(at("2026-01-01T00:00"))


@pytest.mark.parametrize(
    ("expression", "field", "expected"),
    [
        ("0 3 1 1 *", "minute", {0}),
        ("0,30 * * * *", "minute", {0, 30}),
        ("*/15 * * * *", "minute", {0, 15, 30, 45}),
        ("0 9-17 * * *", "hour", set(range(9, 18))),
        ("0 9-17/4 * * *", "hour", {9, 13, 17}),
    ],
)
def test_terms(expression: str, field: str, expected: set[int]) -> None:
    assert set(getattr(parse_cron(expression), field)) == expected


def test_sunday_is_both_zero_and_seven() -> None:
    assert parse_cron("0 0 * * 7").weekday == parse_cron("0 0 * * 0").weekday


@pytest.mark.parametrize(
    ("expression", "complaint"),
    [
        ("0 3 1 1", "expected 5 fields"),
        ("0 3 1 1 * *", "expected 5 fields"),
        ("60 3 * * *", "minute"),
        ("0 24 * * *", "hour"),
        ("0 3 32 * *", "day-of-month"),
        ("0 3 * 13 *", "month"),
        ("0 3 * * 8", "day-of-week"),
        ("0 3 5-1 * *", "day-of-month"),
        ("banana 3 * * *", "minute"),
        ("*/0 * * * *", "step"),
    ],
)
def test_bad_expressions_name_the_field(expression: str, complaint: str) -> None:
    """The message ends up under a form field, so it has to say which part is wrong."""
    with pytest.raises(CronError, match=complaint):
        parse_cron(expression)


def test_day_and_weekday_are_ored_when_both_are_restricted() -> None:
    """Real cron's oddest rule, deliberately reproduced: ``0 0 1 * 1`` means the 1st *or* a
    Monday, not the 1st *and* a Monday."""
    cron = parse_cron("0 0 1 * 1")
    assert cron.matches(at("2026-04-01T00:00")), "the 1st, a Wednesday"
    assert cron.matches(at("2026-04-06T00:00")), "a Monday that is not the 1st"
    assert not cron.matches(at("2026-04-07T00:00")), "neither"


def test_day_and_weekday_are_anded_when_only_one_is_restricted() -> None:
    cron = parse_cron("0 0 1 * *")
    assert cron.matches(at("2026-04-01T00:00"))
    assert not cron.matches(at("2026-04-06T00:00"))


# --- next_fire ----------------------------------------------------------------------------


def test_next_fire_is_strictly_after_the_reference() -> None:
    """Otherwise a fire would re-trigger forever: last_fired == due == fire again."""
    cron = parse_cron("0 3 * * *")
    assert next_fire(cron, at("2026-04-01T03:00")) == at("2026-04-02T03:00")


def test_next_fire_finds_the_same_day_when_it_can() -> None:
    cron = parse_cron("0 3,15 * * *")
    assert next_fire(cron, at("2026-04-01T08:00")) == at("2026-04-01T15:00")


def test_next_fire_crosses_a_year() -> None:
    cron = parse_cron("0 3 1 1 *")
    assert next_fire(cron, at("2026-01-01T03:00")) == at("2027-01-01T03:00")


def test_next_fire_handles_a_leap_day() -> None:
    cron = parse_cron("0 0 29 2 *")
    assert next_fire(cron, at("2026-03-01T00:00")) == at("2028-02-29T00:00")


def test_next_fire_gives_up_on_a_date_that_never_comes() -> None:
    cron = parse_cron("0 0 30 2 *")
    assert next_fire(cron, at("2026-01-01T00:00")) is None


def test_next_fire_keeps_the_reference_timezone() -> None:
    stockholm = timezone("Europe/Stockholm")
    assert stockholm is not None
    reference = datetime(2026, 4, 1, 8, 0, tzinfo=stockholm)
    fired = next_fire(parse_cron("0 3 * * *"), reference)
    assert fired is not None
    assert fired.tzinfo is stockholm
    assert (fired.hour, fired.day) == (3, 2)


def test_an_unknown_timezone_falls_back_rather_than_raising() -> None:
    """A typo in HUB_TIMEZONE must not stop the hub from booting."""
    assert timezone("Mars/Olympus_Mons") is None
    assert timezone("") is None


def test_describe_labels_the_presets() -> None:
    assert describe("0 3 1 1 *") == "Every 1 January at 03:00"
    assert describe("7 4 * * *") == "7 4 * * *"


# --- the scheduler ------------------------------------------------------------------------


@pytest.fixture
def parts(tmp_path: Path):  # noqa: ANN201
    settings = Settings(
        immich_url="http://immich.test:2283",
        hub_password="x",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    store = AddonStore(settings)
    jobs = JobQueue(settings.jobs_db_path)
    catalog = build_catalog(REGISTRY)
    return Scheduler(catalog, store, jobs, tick_s=0.01), store, jobs


def test_only_schedulable_addons_are_offered(parts) -> None:  # noqa: ANN001
    scheduler, _, _ = parts
    ids = scheduler.scheduled_ids()
    assert "year-highlights" in ids, "declares schedule"
    assert "auto-lut" in ids, "declares poll"
    assert "zine-maker" not in ids, "manual only"


def test_saving_a_schedule_stamps_it_so_it_does_not_fire_immediately(parts) -> None:  # noqa: ANN001
    """Saving `0 3 * * *` at noon must not fire this morning's run as a missed one."""
    scheduler, store, jobs = parts
    store.set_enabled("year-highlights", True)
    scheduler.save("year-highlights", "0 3 * * *")

    assert scheduler.tick(scheduler.now() + timedelta(minutes=1)) == []
    assert jobs.list() == []


def test_a_due_schedule_fires_once(parts) -> None:  # noqa: ANN001
    scheduler, store, jobs = parts
    store.set_enabled("year-highlights", True)
    scheduler.save("year-highlights", "0 3 * * *")

    tomorrow_at_three = scheduler.now().replace(hour=3, minute=0) + timedelta(days=1)
    assert len(scheduler.tick(tomorrow_at_three)) == 1

    job = jobs.list()[0]
    assert job.addon == "year-highlights"
    assert job.params["trigger"] == "schedule"

    assert scheduler.tick(tomorrow_at_three + timedelta(minutes=1)) == [], "not again this minute"


def test_a_disabled_addon_does_not_fire(parts) -> None:  # noqa: ANN001
    """The enable toggle is the master switch; a saved schedule alone is not consent to run."""
    scheduler, _, jobs = parts
    scheduler.save("year-highlights", "* * * * *")
    assert scheduler.tick(scheduler.now() + timedelta(minutes=5)) == []
    assert jobs.list() == []


def test_clearing_the_schedule_stops_it(parts) -> None:  # noqa: ANN001
    scheduler, store, _ = parts
    store.set_enabled("auto-lut", True)
    scheduler.save("auto-lut", "*/5 * * * *")
    scheduler.save("auto-lut", "")

    assert scheduler.next_run("auto-lut") is None
    assert scheduler.tick(scheduler.now() + timedelta(hours=2)) == []


def test_missed_fires_collapse_into_one(parts) -> None:  # noqa: ANN001
    """Down for three hours on a 5-minute poll: catch up once, not 36 times."""
    scheduler, store, jobs = parts
    store.set_enabled("auto-lut", True)
    scheduler.save("auto-lut", "*/5 * * * *")

    fired = scheduler.tick(scheduler.now() + timedelta(hours=3))
    assert len(fired) == 1
    assert len(jobs.list()) == 1


def test_a_long_missed_fire_is_skipped_not_run_late(parts) -> None:  # noqa: ANN001
    """A year film for last January is not wanted in March — the run is dropped and logged."""
    scheduler, store, jobs = parts
    store.set_enabled("year-highlights", True)
    scheduler.save("year-highlights", "0 3 1 1 *")

    two_months_late = scheduler.now().replace(month=3, day=1, hour=12) + timedelta(days=365)
    assert scheduler.tick(two_months_late) == []
    assert jobs.list() == []


def test_a_skipped_fire_still_moves_the_clock_forward(parts) -> None:  # noqa: ANN001
    """The skip stamps last_fired too, so the next tick evaluates the *next* occurrence rather
    than re-deciding the same overdue one every 30 seconds."""
    scheduler, store, _ = parts
    store.set_enabled("year-highlights", True)
    scheduler.save("year-highlights", "0 3 1 1 *")

    late = scheduler.now().replace(month=3, day=1, hour=12) + timedelta(days=365)
    scheduler.tick(late)

    following = scheduler.next_run("year-highlights")
    assert following is not None
    assert following > late


def test_a_busy_addon_does_not_stack_up_runs(parts) -> None:  # noqa: ANN001
    """year-highlights can run for an hour; a 15-minute schedule must not queue four copies."""
    scheduler, store, jobs = parts
    store.set_enabled("year-highlights", True)
    scheduler.save("year-highlights", "*/15 * * * *")
    jobs.enqueue("year-highlights", {"trigger": "manual"})

    assert scheduler.tick(scheduler.now() + timedelta(hours=1)) == []
    assert len(jobs.list()) == 1


def test_an_unparseable_stored_schedule_is_survivable(parts) -> None:  # noqa: ANN001
    """Hand-edited config files exist. A bad one warns and is ignored; it does not crash the
    tick and take every other addon's schedule down with it."""
    scheduler, store, jobs = parts
    store.set_enabled("auto-lut", True)
    state = store.read("auto-lut")
    state["schedule"] = {"cron": "not a cron", "last_fired": ""}
    store.write("auto-lut", state)

    assert scheduler.tick(scheduler.now() + timedelta(days=1)) == []
    assert scheduler.next_run("auto-lut") is None


def test_a_corrupt_last_fired_falls_back_to_now(parts) -> None:  # noqa: ANN001
    scheduler, store, _ = parts
    store.set_enabled("auto-lut", True)
    scheduler.save("auto-lut", "*/5 * * * *")
    state = store.read("auto-lut")
    state["schedule"]["last_fired"] = "yesterday-ish"
    store.write("auto-lut", state)

    assert scheduler.next_run("auto-lut") is not None


def test_the_thread_starts_and_stops(parts) -> None:  # noqa: ANN001
    scheduler, _, _ = parts
    scheduler.start()
    scheduler.start()  # idempotent
    assert scheduler._thread is not None
    scheduler.stop()
    assert scheduler._thread is None


def test_next_run_reads_the_configured_timezone(tmp_path: Path) -> None:
    settings = Settings(
        immich_url="http://immich.test:2283", data_dir=tmp_path / "d", _env_file=None
    )
    scheduler = Scheduler(
        build_catalog(REGISTRY),
        AddonStore(settings),
        JobQueue(settings.jobs_db_path),
        tz_name="Europe/Stockholm",
    )
    scheduler.save("year-highlights", "0 3 1 1 *")
    when = scheduler.next_run("year-highlights")

    assert when is not None
    assert str(when.tzinfo) == "Europe/Stockholm"
    assert (when.month, when.day, when.hour) == (1, 1, 3)


def test_jobs_carry_the_due_time_for_the_log(parts) -> None:  # noqa: ANN001
    """The job log should be able to say *which* scheduled slot it is servicing."""
    scheduler, store, jobs = parts
    store.set_enabled("auto-lut", True)
    scheduler.save("auto-lut", "*/5 * * * *")
    scheduler.tick(scheduler.now() + timedelta(minutes=10))

    due = datetime.fromisoformat(jobs.list()[0].params["due"])
    assert due.minute % 5 == 0


def test_utc_reference_is_accepted(parts) -> None:  # noqa: ANN001
    """tick() may be handed an aware datetime from any zone; comparisons must not explode."""
    scheduler, store, jobs = parts
    store.set_enabled("auto-lut", True)
    scheduler.save("auto-lut", "*/5 * * * *")

    assert len(scheduler.tick(datetime.now(UTC) + timedelta(hours=1))) == 1
    assert jobs.list()[0].status is JobStatus.QUEUED
