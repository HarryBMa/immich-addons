"""Hub-side cron: run an addon on a timetable (PLAN.md Phase 7).

Two motivating cases, and they are less alike than they look:

* **year-highlights on 1 January** — fires once a year. If the hub happens to be down that night,
  quietly producing the film in March is worse than not producing it at all.
* **auto-lut polling** — fires every few minutes when Immich's webhook step is unavailable
  (``TRIGGER_MODE=poll``). Missing one tick is meaningless; the next one picks the work up.

So the scheduler is deliberately *forgetful*: it fires at most once per tick per addon, collapses
any fires missed while the process was down into a single one, and drops even that if the missed
time is older than :data:`MAX_CATCH_UP_S`. A schedule is a request to act *around* a time, never a
debt to be repaid.

No dependency: the cron subset here is the useful part of the syntax (``*``, lists, ranges, steps)
in five fields, evaluated in the hub's configured timezone rather than the container's, because
"1 January at 03:00" is a local-time idea and containers usually think in UTC.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from immich_addons.core.jobs import JobQueue, JobStatus
from immich_addons.hub.registry import AddonStore, CatalogEntry

log = logging.getLogger(__name__)

#: How long after a missed fire time it is still worth running. Beyond this the schedule is
#: reported as missed and skipped: a year film for last January is not wanted in March.
MAX_CATCH_UP_S = 6 * 3600

#: How often the thread wakes. Cron resolution is a minute, so this only bounds the lag.
TICK_S = 30.0

#: How far ahead :func:`next_fire` will search before giving up (a leap-safe four years).
HORIZON_DAYS = 4 * 366

FIELD_RANGES: tuple[tuple[int, int], ...] = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")

_TERM = re.compile(r"^(?:(\*)|(\d+)(?:-(\d+))?)(?:/(\d+))?$")

#: A few schedules worth offering in the UI rather than making people remember the field order.
PRESETS: tuple[tuple[str, str], ...] = (
    ("0 3 1 1 *", "Every 1 January at 03:00"),
    ("0 4 1 * *", "Monthly, the 1st at 04:00"),
    ("0 4 * * 1", "Weekly, Monday at 04:00"),
    ("0 3 * * *", "Daily at 03:00"),
    ("*/15 * * * *", "Every 15 minutes"),
    ("*/5 * * * *", "Every 5 minutes"),
)


class CronError(ValueError):
    """The cron expression is not something this parser accepts."""


@dataclass(frozen=True)
class Cron:
    """A parsed five-field expression: the set of allowed values per field."""

    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]
    #: Real cron ORs day-of-month with day-of-week when *both* are restricted, so
    #: ``0 0 1 * 1`` means "the 1st **or** any Monday". Remembering which fields were wildcards is
    #: the only way to reproduce that.
    day_restricted: bool
    weekday_restricted: bool
    expression: str

    def matches_date(self, when: date) -> bool:
        if when.month not in self.month:
            return False
        # Python's Monday=0 matches cron's 1..5 offset by one; cron's 0 and 7 are both Sunday.
        weekday = (when.weekday() + 1) % 7
        day_ok = when.day in self.day
        weekday_ok = weekday in self.weekday
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def matches(self, when: datetime) -> bool:
        return (
            when.minute in self.minute and when.hour in self.hour and self.matches_date(when.date())
        )


def _parse_field(text: str, index: int) -> tuple[frozenset[int], bool]:
    """One field to its value set, plus whether it was restricted (i.e. not a bare ``*``)."""
    low, high = FIELD_RANGES[index]
    values: set[int] = set()
    restricted = False

    for part in text.split(","):
        match = _TERM.match(part.strip())
        if match is None:
            raise CronError(f"{FIELD_NAMES[index]}: cannot read {part.strip()!r}")
        star, start, end, step_text = match.groups()
        step = int(step_text) if step_text else 1
        if step < 1:
            raise CronError(f"{FIELD_NAMES[index]}: step must be 1 or more")

        if star:
            first, last = low, high
            restricted = restricted or step != 1
        else:
            first = int(start)
            last = int(end) if end is not None else first
            restricted = True
        if first < low or last > high or first > last:
            raise CronError(f"{FIELD_NAMES[index]}: {part.strip()!r} is outside {low}-{high}")
        values.update(range(first, last + 1, step))

    return frozenset(values), restricted


def parse_cron(expression: str) -> Cron:
    """Parse ``minute hour day-of-month month day-of-week``.

    Accepts ``*``, ``a``, ``a-b``, ``*/n`` and ``a-b/n``, comma-separated. Weekday accepts 0-6 with
    both 0 and 7 meaning Sunday. Raises :class:`CronError` with the offending field named, because
    this string is typed into a form by a human.
    """
    fields = expression.split()
    if len(fields) != 5:
        raise CronError(
            f"expected 5 fields ({', '.join(FIELD_NAMES)}), got {len(fields)}: {expression!r}"
        )

    parsed: list[frozenset[int]] = []
    restricted: list[bool] = []
    for index, field in enumerate(fields):
        if index == 4:
            field = field.replace("7", "0")  # noqa: PLW2901 - cron allows both for Sunday
        values, is_restricted = _parse_field(field, index)
        if not values:
            raise CronError(f"{FIELD_NAMES[index]}: matches nothing")
        parsed.append(values)
        restricted.append(is_restricted)

    return Cron(
        minute=parsed[0],
        hour=parsed[1],
        day=parsed[2],
        month=parsed[3],
        weekday=parsed[4],
        day_restricted=restricted[2],
        weekday_restricted=restricted[4],
        expression=" ".join(fields),
    )


def next_fire(cron: Cron, after: datetime) -> datetime | None:
    """First minute strictly after ``after`` that the expression matches, or ``None``.

    Walks whole days first and only descends into minutes on a day that can match, so resolving
    "every 1 January" from 2 January costs about 1400 date checks rather than half a million
    minute checks.
    """
    cursor = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    horizon = cursor.date() + timedelta(days=HORIZON_DAYS)
    day = cursor.date()

    while day <= horizon:
        if cron.matches_date(day):
            first_minute = cursor if day == cursor.date() else None
            for hour in sorted(cron.hour):
                for minute in sorted(cron.minute):
                    candidate = datetime.combine(
                        day, datetime.min.time(), tzinfo=cursor.tzinfo
                    ).replace(hour=hour, minute=minute)
                    if first_minute is None or candidate >= first_minute:
                        return candidate
        day += timedelta(days=1)
    return None


def describe(expression: str) -> str:
    """A preset's label, or the expression itself. Good enough for a form hint."""
    for preset, label in PRESETS:
        if preset == expression:
            return label
    return expression


def timezone(name: str) -> ZoneInfo | None:
    """Resolve a timezone name, or ``None`` for "use the machine's local time".

    An unusable name warns and returns ``None`` rather than raising: a typo in ``HUB_TIMEZONE``
    should cost you a correct schedule, not the whole hub.
    """
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r; falling back to local time", name)
        return None


def local_timezone() -> tzinfo:
    """The machine's zone as a concrete ``tzinfo``, so every datetime here can stay aware."""
    return datetime.now().astimezone().tzinfo or UTC


@dataclass
class ScheduleState:
    """What the store holds for one addon's schedule."""

    cron: str = ""
    last_fired: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.cron)


class Scheduler:
    """Enqueues jobs when their schedule comes round. Owns a thread; does no work itself.

    Kept apart from :class:`~immich_addons.core.jobs.JobQueue` on purpose: the queue's job is to run
    one thing at a time and record what happened, and it should not also have opinions about
    calendars. The scheduler's only power is ``enqueue``.
    """

    def __init__(
        self,
        catalog: list[CatalogEntry],
        store: AddonStore,
        jobs: JobQueue,
        *,
        tz_name: str = "",
        tick_s: float = TICK_S,
        max_catch_up_s: float = MAX_CATCH_UP_S,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.jobs = jobs
        #: Always a concrete zone. Every datetime inside the scheduler is aware and lives here, so
        #: a caller handing in a UTC "now" (or a stored stamp from before the zone was configured)
        #: can never produce a naive/aware comparison.
        self.tz = timezone(tz_name) or local_timezone()
        self.tick_s = tick_s
        self.max_catch_up_s = max_catch_up_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- state ---------------------------------------------------------------------------

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def _here(self, when: datetime) -> datetime:
        """Move any datetime into the scheduler's zone; assume a naive one already is."""
        if when.tzinfo is None:
            return when.replace(tzinfo=self.tz)
        return when.astimezone(self.tz)

    def read(self, addon_id: str) -> ScheduleState:
        raw = self.store.read(addon_id).get("schedule", {})
        return ScheduleState(cron=raw.get("cron", ""), last_fired=raw.get("last_fired", ""))

    def save(self, addon_id: str, expression: str) -> ScheduleState:
        """Set (or clear, with an empty string) an addon's schedule.

        Stamps ``last_fired`` at save time so a schedule saved at noon does not immediately fire
        this morning's 03:00 as though it had been missed.
        """
        expression = expression.strip()
        if expression:
            parse_cron(expression)  # raises CronError, which the route turns into a form message
        state = self.store.read(addon_id)
        state["schedule"] = {"cron": expression, "last_fired": self.now().isoformat()}
        self.store.write(addon_id, state)
        return self.read(addon_id)

    def scheduled_ids(self) -> list[str]:
        """Addons that are installed and may be scheduled at all."""
        return [
            entry.id
            for entry in self.catalog
            if entry.addon is not None
            and (entry.addon.supports("schedule") or entry.addon.supports("poll"))
        ]

    def next_run(self, addon_id: str) -> datetime | None:
        state = self.read(addon_id)
        if not state.enabled:
            return None
        try:
            cron = parse_cron(state.cron)
        except CronError:
            return None
        return next_fire(cron, self._reference(state))

    def _reference(self, state: ScheduleState) -> datetime:
        """The point to measure the next fire from: the last fire, else now."""
        if state.last_fired:
            try:
                return self._here(datetime.fromisoformat(state.last_fired))
            except ValueError:
                log.warning("unreadable last_fired %r; measuring from now", state.last_fired)
        return self.now()

    # --- firing --------------------------------------------------------------------------

    def _busy(self, addon_id: str) -> bool:
        """True if this addon already has work outstanding. A slow year-highlights run must not
        collect a queue of copies of itself behind it."""
        return any(
            job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
            for job in self.jobs.list(limit=20, addon=addon_id)
        )

    def tick(self, now: datetime | None = None) -> list[int]:
        """Fire whatever is due. Returns the job ids created. Safe to call directly in tests."""
        now = self._here(now) if now else self.now()
        fired: list[int] = []

        for addon_id in self.scheduled_ids():
            if not self.store.is_enabled(addon_id):
                continue
            state = self.read(addon_id)
            if not state.enabled:
                continue
            try:
                cron = parse_cron(state.cron)
            except CronError as exc:
                log.warning("addon %s has an unusable schedule: %s", addon_id, exc)
                continue

            due = next_fire(cron, self._reference(state))
            if due is None or due > now:
                continue

            # Whatever happens next, the schedule has been considered: stamp it so a stuck addon
            # cannot make every later tick re-evaluate the same overdue fire.
            self._stamp(addon_id, now)

            late_s = (now - due).total_seconds()
            if late_s > self.max_catch_up_s:
                log.warning(
                    "addon %s missed its %s run by %.0f h; skipping rather than running it late",
                    addon_id,
                    due.isoformat(timespec="minutes"),
                    late_s / 3600,
                )
                continue
            if self._busy(addon_id):
                log.info("addon %s is still busy; skipping this scheduled run", addon_id)
                continue

            job_id = self.jobs.enqueue(addon_id, {"trigger": "schedule", "due": due.isoformat()})
            log.info("scheduled run of %s queued as job %s", addon_id, job_id)
            fired.append(job_id)

        return fired

    def _stamp(self, addon_id: str, when: datetime) -> None:
        state = self.store.read(addon_id)
        schedule = state.get("schedule", {})
        schedule["last_fired"] = when.isoformat()
        state["schedule"] = schedule
        self.store.write(addon_id, state)

    # --- thread --------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad schedule must not kill the thread
                log.exception("scheduler tick failed")
