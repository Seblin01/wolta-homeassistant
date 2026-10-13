"""DataUpdateCoordinator for the Wolta integration."""

from __future__ import annotations

import json
import logging
from bisect import bisect_right
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import aiohttp
from astral import Observer
from astral import sun as astral_sun

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.sun import get_astral_observer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    MAX_ROWS_PER_PUT,
    WoltaApiClient,
    WoltaApiError,
    WoltaAuthError,
    WoltaRateLimitError,
    WoltaReplacementConflict,
    WoltaReplacementGone,
)
from .const import (
    KEY_BATTERY_STATUS,
    BATTERY_STATUS_MEASURED,
    BATTERY_STATUS_NAMEPLATE,
    BATTERY_STATUS_NEEDS_INPUT,
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_BATTERY_KW,
    CONF_BATTERY_KWH,
    CONF_CAPACITY_ISSUE_IGNORED,
    CONF_DIRECTION_FLIPPED_AT,
    CONF_DIRECTION_ISSUE_IGNORED,
    CONF_COST_SEK,
    CONF_EFF,
    CONF_EFFICIENCY_ISSUE_IGNORED,
    CONF_EXPORT_EXTRA_ORE,
    CONF_EXPORT_EXTRA_PCT,
    CONF_EXTERNAL_CONTROL,
    CONF_FLEX_COMPENSATION,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_GRID_VAR_ORE,
    CONF_GRID_VAR_PCT,
    CONF_INVERT_BATTERY,
    CONF_INVERT_INTERVALS,
    CONF_NAMEPLATE_KW,
    CONF_POWER_ISSUE_IGNORED,
    CONF_PURCHASE_DATE,
    CONF_RESERVE_PCT,
    CONF_SOC,
    CONF_SOC_ISSUE_IGNORED,
    CONF_SOLAR,
    CONF_SURCHARGE_ORE,
    CONF_TOKEN,
    CONF_VIEW_ONLY,
    CONF_ZONE,
    DOMAIN,
    WOLTA_API_BASE,
)
from . import stats
from . import coverage
from . import intervals
from .stats import (
    aggregate_5min_to_15min,
    async_fetch_change,
    async_fetch_measurement,
    battery_state_rows,
    clean_quarters,
    known_zero_gaps,
    measured_starts,
    merge_streams,
    soc_unit_ok,
    soc_from_hourly,
    soc_quarters_from_5min,
    split_hour_to_quarters,
    sum_quarter_dicts,
)

_LOGGER = logging.getLogger(__name__)

# Statistics period per aggregation function, and how far before a window the read
# starts so the window's first period can be judged against the one before it.
_PERIOD_S: dict[Callable, int] = {aggregate_5min_to_15min: 300, split_hour_to_quarters: 3600}
_PERIOD: dict[int, timedelta] = {300: timedelta(minutes=5), 3600: timedelta(hours=1)}


def _floor(t: datetime, period_s: int) -> datetime:
    """``t`` floored to a whole statistics period (UTC)."""
    ts = int(t.timestamp())
    return datetime.fromtimestamp(ts - ts % period_s, tz=timezone.utc)


def _read_start(start: datetime, period_s: int) -> datetime:
    """Where a read for a window starting at ``start`` begins: the period holding
    ``start`` floored, minus one period. The recorder returns rows with
    ``start_ts >= start_time`` and does not floor (HA 2026.8.3 recorder/statistics.py),
    so ``start - period`` alone misses the period before the window whenever ``start``
    is not on a period boundary, and the window's first period could not be judged."""
    return _floor(start, period_s) - _PERIOD[period_s]


def _short_term_start(now: datetime) -> datetime:
    """Where the flows switch from hourly to 5-minute statistics, on a whole hour.
    Unaligned, the quarter straddling it would be judged complete from the hour while
    its value came from the 5-minute periods after the switch - a partial quarter."""
    return _floor(now - timedelta(days=_SHORT_TERM_DAYS), 3600)


@dataclass(frozen=True)
class _Source:
    """One statistics read: rows per statistic id, the aggregation that turns them into
    quarters, and the window [start, end) whose quarters this source decides.
    ``anchors``: per solar sensor, the start of its last measured period before the
    rows (WoltaCoordinator._anchor_solar), for clean_quarters' ``anchor_start``."""
    raw: dict
    agg_fn: Callable
    start: datetime
    end: datetime
    anchors: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Built:
    streams: dict[str, dict]          # merge_streams' keyword arguments
    complete: set[datetime]           # quarters every selected sensor measured cleanly
    silent: set[str]                  # selected sensors that stopped measuring (_silent_sensors)
    incomplete: int                   # quarters with a value somewhere but not complete
    blame: dict[str, int]             # sensor -> quarters it made incomplete
    asleep: set[str]                  # solar sensors not silent only because the sun was down
    # Per stream (merge_streams' names, unswapped): quarters where every sensor of the
    # stream measured cleanly. None for a stream without sensors - unbounded, it never
    # limits a quarter. Field-scoped replacements judge only the streams they replace.
    complete_by_stream: dict[str, set[datetime] | None]


@dataclass(frozen=True)
class _ReplacementType:
    """The hooks a replacement job type plugs into the runner (_run_replacement_job).

    A replacement spec is a dict {kind, window_start, window_end, fields, reason} with the
    window as ISO strings in UTC on whole hours; the runner adds the id.

    Contract: hooks mutate `job` (the dict in Store) in place and never replace it; the
    runner saves it. `rows_for` must give the same rows for the same spec, since it runs
    again on every resume of an open replacement. Hooks should not raise: the runner
    tolerates it (logged, retried next cycle, and a replacement that keeps failing is
    given up, and a job that keeps failing is parked), but a hook that raises never makes
    progress. A hook that wants the job held back sets `job["retry_at"]` (ISO) rather
    than returning None every cycle; one that waits for a reason that may never pass (no
    data_since from the server) calls _note_job_failure, so the job is parked after
    _JOB_MAX_FAILED_CYCLES cycles in a row."""

    # (job, now, profile) -> the next spec; None to wait (nothing is opened and the regular
    # upload goes on), or "done" when the job has nothing left to replace.
    next_replacement: Callable[[dict, datetime, dict | None], Awaitable[dict | str | None]]
    # (job, spec, result) after each committed window, before the job is saved. `result`
    # is the server's commit result, or {} when it had nothing to replace (409 empty).
    on_committed: Callable[[dict, dict, dict], Awaitable[None]]
    # (job) once next_replacement says "done": apply the job's target to the applied state.
    on_finished: Callable[[dict], Awaitable[None]]
    # (job, spec, now) -> the rows of a "rows" replacement, or None when they cannot be
    # built now (the job waits). Not needed by a type that only swaps.
    rows_for: Callable[[dict, dict, datetime], Awaitable[list[dict] | None]] | None = None


def desired_invert_intervals(data) -> list[intervals.Interval]:
    """The battery inversion the owner wants, read from entry data. Before the
    coordinator's one-time migration has run, the legacy flag stands in for it."""
    if CONF_INVERT_INTERVALS in data:
        return intervals.parse(data.get(CONF_INVERT_INTERVALS))
    return [(None, None)] if data.get(CONF_INVERT_BATTERY) else []


def with_desired_intervals(data, desired: list[intervals.Interval]) -> dict:
    """Entry data with the desired intervals written, mirroring CONF_INVERT_BATTERY as
    "everything inverted" so a downgrade reads the right global flag in the common case.
    The one place that writes the pair: the coordinator, the options flow and the
    repair flow all go through it."""
    stored = intervals.dump(intervals.normalize(desired))
    return {
        **data,
        CONF_INVERT_INTERVALS: stored,
        CONF_INVERT_BATTERY: stored == [[None, None]],
    }


def direction_swap_intervals(block: dict) -> list[intervals.Interval]:
    """The intervals the battery_direction repair flips (server spec §10.1).

    A list `swap_intervals` of {"from", "to"} (ISO UTC or null for open) is taken as it
    is. Without the key (an older backend) the v0.43 rule holds: "all" and "partial" flip
    the whole history, the other forms nothing. Anything malformed flips nothing, and so
    does a bound that is not on a whole UTC hour: the direction job swaps whole hours,
    and a bound inside an hour could overlap a window it has already swapped."""
    if "swap_intervals" not in block:
        return [(None, None)] if block.get("form") in _BATTERY_DIRECTION_FIXABLE else []
    raw = block.get("swap_intervals")
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        return []
    swap = intervals.normalize(
        intervals.parse([[item.get("from"), item.get("to")] for item in raw])
    )
    if any(t is not None and t != _floor_hour(t) for interval in swap for t in interval):
        return []
    return swap


# The stored flow fields a sensor stream feeds (server spec §3). Either battery stream
# replaces BOTH battery fields: which sensor lands in which field depends on the invert
# intervals per quarter, so the pair is only ever replaced together.
_STREAM_FIELDS: dict[str, tuple[str, ...]] = {
    "solar": ("solar_kwh",),
    "grid_in": ("grid_import_kwh",),
    "grid_out": ("grid_export_kwh",),
    "batt_in": ("batt_charged_kwh", "batt_discharged_kwh"),
    "batt_out": ("batt_charged_kwh", "batt_discharged_kwh"),
    "external_control": ("external_control",),
}
# The server's field order (its FIELDS); a replacement names its fields in it.
_FIELD_ORDER = ("solar_kwh", "grid_import_kwh", "grid_export_kwh", "batt_charged_kwh",
                "batt_discharged_kwh", "external_control")
_ENERGY_FIELDS = _FIELD_ORDER[:5]


def _parse_fingerprint(raw: str) -> dict[str, set[str]] | None:
    """An entity fingerprint as {stream: set of entity ids} for every stream (a missing
    key is a stream without sensors), or None when it cannot be read."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    out: dict[str, set[str]] = {}
    for stream in _STREAM_FIELDS:
        value = parsed.get(stream) or []
        if not isinstance(value, list):
            return None
        out[stream] = {str(e) for e in value}
    return out


def replaced_streams(applied: str, desired: str) -> dict[str, list[str]]:
    """The streams a sensor change replaces in the stored history, each with the sensors
    added to it (sorted), from two entity fingerprints (JSON strings of
    {stream: [entity ids]}, external_control only when a sensor is chosen).

    A stream is replaced only when a sensor was added to it or swapped in. A change
    that only removes sensors replaces nothing: a removed sensor is most often dead, and
    the history recorded with it was right - the rows still to come simply go without
    it. Nor are the battery fields replaced when either battery stream is left without
    sensors, since the pair is only ever replaced together. An unreadable applied
    fingerprint counts as every sensor added."""
    new = _parse_fingerprint(desired)
    if new is None:
        return {}
    old = _parse_fingerprint(applied)
    out: dict[str, list[str]] = {}
    for stream in _STREAM_FIELDS:
        added = new[stream] - (old[stream] if old is not None else set())
        if added:
            out[stream] = sorted(added)
    if ("batt_in" in out or "batt_out" in out) and not (new["batt_in"] and new["batt_out"]):
        out.pop("batt_in", None)
        out.pop("batt_out", None)
    return out


def replaced_fields(streams: list[str]) -> list[str]:
    """The stored fields a change of `streams` replaces, in the server's order."""
    fields = {f for s in streams for f in _STREAM_FIELDS.get(s, ())}
    return [f for f in _FIELD_ORDER if f in fields]


def _runs(
    hours: list[datetime],
    committed: list[tuple[datetime, datetime]] = (),
    split: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """The windows of an entity job's energy phase from its sorted complete hours:
    maximal runs of consecutive hours, runs at most _ENTITY_GAP_MERGE apart joined, and
    then the smallest gaps joined until at most _ENTITY_MAX_WINDOWS remain (a soft cap:
    only gaps that may be joined at all are).

    A gap is never joined when a `committed` window lies in it (that window's rows are
    replaced already and must not be removed or replaced twice), nor when it touches
    `split`, the switch from hourly to 5-minute statistics, which stays a window
    boundary."""
    runs: list[list[datetime]] = []
    for h in hours:
        if runs and runs[-1][1] == h:
            runs[-1][1] = h + timedelta(hours=1)
        else:
            runs.append([h, h + timedelta(hours=1)])

    def joinable(left: list[datetime], right: list[datetime]) -> bool:
        gap_start, gap_end = left[1], right[0]
        if split is not None and gap_start <= split <= gap_end:
            return False
        return not any(ws < gap_end and gap_start < we for ws, we in committed)

    merged: list[list[datetime]] = []
    for run in runs:
        if (merged and run[0] - merged[-1][1] <= _ENTITY_GAP_MERGE
                and joinable(merged[-1], run)):
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    while len(merged) > _ENTITY_MAX_WINDOWS:
        candidates = [k for k in range(len(merged) - 1)
                      if joinable(merged[k], merged[k + 1])]
        if not candidates:
            break
        i = min(candidates, key=lambda k: merged[k + 1][0] - merged[k][1])
        merged[i][1] = merged[i + 1][1]
        del merged[i + 1]
    return [(start, end) for start, end in merged]


def _energy_windows(
    built: _Built,
    streams: dict[str, dict],
    sources: list[_Source],
    streams_by: dict[str, list[str]],
    external: set[datetime],
    read_from: datetime,
    split: datetime,
    done: list[tuple[datetime, datetime]],
    window: tuple[datetime, datetime] | None,
) -> tuple[list[list], list[list[dict]]] | None:
    """The per-quarter part of WoltaCoordinator._energy_build, pure and run in the
    executor: from the built streams (`streams` with the applied inversion swapped in),
    the windows of the energy phase ([start, end, last_full] in ISO) and the rows of
    each, or None when the added sensors (`streams_by`: replaced stream -> sensors
    added to it) have measured nothing yet. See _energy_build for the rules."""
    energy = sorted(s for s in streams_by if s != "external_control")
    judged = set(energy)
    if judged & {"batt_in", "batt_out"}:
        judged |= {"batt_in", "batt_out"}
    bounded = [built.complete_by_stream[s] for s in sorted(judged)
               if built.complete_by_stream.get(s) is not None]
    if not bounded:
        return None
    begun: int | None = None
    for e in sorted({e for s in energy for e in streams_by[s]}):
        measured: set[int] = set()
        for src in sources:
            measured |= measured_starts(src.raw.get(e) or [], _PERIOD_S[src.agg_fn])
        if not measured:
            return None
        begun = min(measured) if begun is None else max(begun, min(measured))
    complete = set.intersection(*bounded)
    if begun is not None:
        first_measured = datetime.fromtimestamp(begun, tz=timezone.utc)
        complete = {q for q in complete if q >= first_measured}
    if window is not None:
        spans = [window]
    else:
        hours = sorted(
            h for h in {_floor_hour(q) for q in complete}
            if h >= read_from
            and all(h + _QUARTER * k in complete for k in range(4))
            and not any(ws <= h < we for ws, we in done))
        spans = _runs(hours, done, split)
    # Spans are sorted and disjoint: a quarter's span is found by bisection.
    starts = [ws for ws, _we in spans]

    def span_of(q: datetime) -> int | None:
        i = bisect_right(starts, q) - 1
        return i if i >= 0 and q < spans[i][1] else None

    inside = {q for q in complete if span_of(q) is not None}
    rows = stats.replacement_rows(
        streams, built.complete, inside, replaced_fields(energy), external)
    grouped: list[list[dict]] = [[] for _ in spans]
    for row in rows:
        i = span_of(datetime.fromisoformat(row["ts"]))
        if i is not None:
            grouped[i].append(row)
    windows: list[list] = []
    rows_by_window: list[list[dict]] = []
    for (ws, we), mine in zip(spans, grouped):
        if not mine:
            continue
        full = [r["ts"] for r in mine if all(f in r for f in _ENERGY_FIELDS)]
        windows.append([ws.isoformat(), we.isoformat(), max(full) if full else None])
        rows_by_window.append(mine)
    return windows, rows_by_window


def _floor_hour(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _ceil_hour(t: datetime) -> datetime:
    floor = _floor_hour(t)
    return floor if floor == t else floor + timedelta(hours=1)


def _silent_sensors(
    sensors: set[str], newest: dict[str, tuple[int, int]], min_lag: timedelta
) -> set[str]:
    """Selected sensors that have stopped measuring, judged by lag.

    ``newest`` maps a sensor to (start of its newest measured period anywhere in the
    read, the period of the source it was found in). A sensor is silent when it has no
    measured period at all, or when its newest one trails the newest of all selected
    sensors by at least max(``min_lag``, the period of either source). Home Assistant
    compiles the statistics of every sensor in the same run, so a healthy sensor never
    trails the others by a whole period.

    Lag, not "no measured period in the window": every read starts at the bookmark,
    which is the last uploaded complete quarter, so the read always holds that quarter -
    and the rows a sensor that died after it still has in it. Judged by presence such a
    sensor would never be silent, and with complete quarters nothing after the bookmark
    would ever be sent again, without any repair."""
    if not newest:
        return set(sensors)
    top, top_period = max(newest.values())
    silent: set[str] = set()
    for e in sensors:
        if e not in newest:
            silent.add(e)
            continue
        start, period = newest[e]
        limit = max(min_lag.total_seconds(), period, top_period)
        if top - start >= limit:
            silent.add(e)
    return silent


# Daylight a sensor may trail by before it is silent whatever the threshold: the
# largest threshold _silent_sensors applies is one hour (the hourly period).
_MAX_LAG_S = 3600


def _discount_night(
    newest: dict[str, tuple[int, int]],
    solar: set[str],
    first: int,
    step: int,
    sun_down: Callable[[int], bool],
) -> set[str]:
    """Let solar sensors trail the others only by daylight: periods after a sensor's
    newest measured one in which the sun was down are known zeros (spec 6.2 (a)), not
    lag. A sleeping inverter at night is therefore not silent.

    For each solar sensor the periods after its newest measured one (from ``first``,
    the read's first period, when it has none) up to the newest of all selected sensors
    are walked in ``step``; its newest is moved forward by the night periods among them,
    so only the daylight periods count as lag. A sensor that stopped shortly before
    sunset trails by those few daylight minutes, below the threshold. Rule (b) cannot
    move it: it needs a measured period after the gap. ``newest`` is updated in place
    for the sensors the night moved; those are returned. The walk stops after
    _MAX_LAG_S of daylight - the sensor is silent anyway."""
    if not newest:
        return set()
    top = max(s for s, _ in newest.values())
    moved: set[str] = set()
    for e in solar:
        p = newest[e][0] + newest[e][1] if e in newest else first
        daylight = 0          # seconds of daylight between the sensor's newest and top
        saw_night = False
        while p <= top and daylight < _MAX_LAG_S:
            if sun_down(p):
                saw_night = True
            else:
                daylight += step
            p += step
        if not saw_night or daylight >= _MAX_LAG_S:
            continue
        newest[e] = (top - daylight, newest[e][1] if e in newest else step)
        moved.add(e)
    return moved


def _log_known_zero_gaps(
    entity_id: str, rows: list, period_s: int, window: tuple[int, int],
    anchor_start: int | None,
) -> None:
    """Log at debug every gap of an hour or more that rule (b) accepts as known zeros
    (stats.known_zero_gaps) and that ends inside the window. An integration that
    reports a stale, unchanged counter when it reconnects after an outage looks exactly
    like an idle battery; the log makes such a run visible."""
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    for first, after in known_zero_gaps(rows, period_s, anchor_start=anchor_start):
        if after < window[0]:
            continue
        _LOGGER.debug(
            "Known zeros: %s has no statistics from %s to %s (%s) and its counter "
            "stood still across the gap; the gap is sent as zero",
            entity_id,
            datetime.fromtimestamp(first, tz=timezone.utc).isoformat(),
            datetime.fromtimestamp(after, tz=timezone.utc).isoformat(),
            timedelta(seconds=after - first),
        )


_DAY_S = 86400
_EPOCH_DATE = date(1970, 1, 1)


def _daylight(observer: Observer, day: date) -> tuple[float, float] | None:
    """[sunrise, sunset) of the UTC date ``day`` in UNIX seconds, or None when the sun
    stays below the horizon all day.

    astral (2.2, Home Assistant's) computes both as solar noon of the UTC date minus /
    plus the hour angle, so the interval is contiguous around that noon and lies within
    the day before and the day after. On a day without sunrise and sunset it raises
    ValueError (whatever the message, which differs between Python versions), and the
    apparent elevation decides: below the horizon at solar noon is polar night (None);
    above it at solar midnight (noon + 12 h) is midnight sun, and the whole solar day
    (noon +/- 12 h, plus a minute so neighbouring days overlap) is daylight. Near the
    polar circle astral also raises on days on which the sun just grazes the horizon
    (at 67.85 N in early December and January); then the two horizon crossings are
    found by bisection on the elevation. astral 3.x re-anchors both events to the
    requested date instead, which can put the sunset of a short night before the
    sunrise; such a pair is not an interval and takes the elevation path too.

    Sunrise and sunset refer to the upper limb with refraction (-0.833 deg), and the
    fallback uses the apparent elevation and rounds outward: all a few minutes wider
    than "elevation below 0 deg", so an error only makes fewer periods known zeros,
    never more."""
    try:
        rise = astral_sun.sunrise(observer, day).timestamp()
        set_ = astral_sun.sunset(observer, day).timestamp()
        if rise < set_:
            return (rise, set_)
    except ValueError:
        pass
    noon = astral_sun.noon(observer, day)
    half = _DAY_S / 2

    def up(offset: float) -> bool:
        return astral_sun.elevation(observer, noon + timedelta(seconds=offset)) > 0

    if not up(0):
        return None
    mid = noon.timestamp()
    if up(half):
        return (mid - half - 60, mid + half + 60)

    def crossing(down_at: float) -> float:
        """Offset from noon of the horizon crossing between noon (up) and ``down_at``
        (down), to within a minute, on the side of the night."""
        inside, outside = 0.0, down_at
        while abs(outside - inside) > 60:
            m = (inside + outside) / 2
            if up(m):
                inside = m
            else:
                outside = m
        return outside

    return (mid + crossing(-half), mid + crossing(half))


def _make_sun_down(
    observer: Observer, period_s: int, cache: dict[int, tuple[float, float] | None]
) -> Callable[[int], bool]:
    """``sun_down(period_start)``: whether the sun is below the horizon in the middle of
    the period. Daylight is computed once per UTC day (``cache``, keyed by days since
    the epoch) - a year's backfill costs a few hundred astral calls, not one per period.
    The middle of a period can only lie in the daylight of its own UTC day or of the
    day before or after (see _daylight)."""
    half = period_s // 2

    def daylight(d: int) -> tuple[float, float] | None:
        if d not in cache:
            cache[d] = _daylight(observer, _EPOCH_DATE + timedelta(days=d))
        return cache[d]

    def sun_down(start: int) -> bool:
        t = start + half
        day = t // _DAY_S
        for d in (day - 1, day, day + 1):
            span = daylight(d)
            if span is not None and span[0] <= t < span[1]:
                return False
        return True

    return sun_down


# How far back to backfill on first install
# Polling rate: slow at rest, fast while a server-side computation is in progress (so the grade
# shows up shortly after the worker finishes instead of at the next 6h poll).
_SLOW_POLL = timedelta(hours=6)
_FAST_POLL = timedelta(seconds=60)

_BACKFILL_DAYS = 365

# Short-term statistics window — data older than this is only in LTS
_SHORT_TERM_DAYS = 9

# How far before a window the anchor read for a solar sensor's night run looks: longer
# than the longest Swedish night outside the polar night. A run with no measured period
# within it stays unproven (one dawn quarter after a polar night or a longer outage).
_ANCHOR_LOOKBACK = timedelta(hours=36)

# The API's length limit for battery_sources `source` ('ha:<platform>:<entity_id>').
_SOC_SOURCE_MAX = 200

# At most this many replacements are committed per cycle (a direction job has one per
# segment). A bound against a job that never finishes, not a target: the rest waits.
_REPLACEMENTS_PER_CYCLE = 12

# A sensor change's energy fields are replaced over runs of whole hours that are complete
# for the replaced streams. Runs closer than this are one window (the hours between are
# removed as honest gaps); a longer silence of the new sensors stays outside every window
# and keeps the stored rows. At most this many windows per job: beyond it the smallest
# gaps are merged first.
_ENTITY_GAP_MERGE = timedelta(hours=6)
_ENTITY_MAX_WINDOWS = 50
# Where a sensor change's read switches from hourly to 5-minute statistics: a day inside
# the regular short-term limit, so the 5-minute statistics a stored plan relies on are
# still there when it is resumed (Home Assistant keeps them for 10 days by default).
_ENTITY_SHORT_TERM_DAYS = 8
# A stored plan older than this is made anew for what is left, before its 5-minute
# statistics can be purged.
_ENTITY_PLAN_MAX_AGE = timedelta(hours=12)
# Failed reads of the external-control sensor's states before its phase is given up.
_EXTERNAL_MAX_ERRORS = 3
# Cycles in a row in which a started entity job cannot make its energy plan before the
# phase is ended (nothing else can replace a started job).
_ENTITY_PLAN_MAX_FAILURES = 4

# After a commit the server refused with 413 (the result would exceed the profile's row
# limit) the replacement is dropped and the job tries again this much later.
_REPLACEMENT_FULL_RETRY = timedelta(days=1)

# A replacement that failed this many cycles in a row without a verdict from the server
# (5xx, a hook error) is tried once more; the next failure gives it up for
# _REPLACEMENT_GIVE_UP_RETRY, so a deterministic server error cannot pause the regular
# upload for good (staging the rows again keeps moving the server's expiry).
_REPLACEMENT_MAX_FAILURES = 3
_REPLACEMENT_GIVE_UP_RETRY = timedelta(days=1)

# A job that fails at the job level this many cycles in a row - the server refuses it
# with a 4xx it gives no handled code for, a plan cannot be built, the server gives no
# data_since, or it conflicts or closes the replacement again and again - is parked: taken
# out of the slot, and no job of its type is queued for _JOB_PARK_BACKOFF. Otherwise one
# deterministic error would hold every later direction and sensor change for good.
_JOB_MAX_FAILED_CYCLES = 5
_JOB_PARK_BACKOFF = timedelta(hours=24)

# Trigger a recompute when this many new days of data have been uploaded
_RECOMPUTE_INTERVAL_DAYS = 7

# After this many consecutive days of upload failures, raise a repair issue
_REPAIR_FAILURE_DAYS = 7

# Store version / key
_STORE_VERSION = 1
_STORE_KEY = "wolta_coordinator"

# Side-poll cadence: cheap GET /profile so web-side edits show up in HA within
# minutes instead of at the next 6h main cycle. Single-row read server-side.
_PROFILE_SYNC_INTERVAL = timedelta(minutes=5)

# Issue IDs
_ISSUE_UPLOAD_FAILURE = "upload_failure"
_ISSUE_PROFILE_FULL = "profile_full"
_ISSUE_CAPACITY = "measured_capacity"
_ISSUE_POWER = "measured_power"
_ISSUE_EFFICIENCY = "measured_efficiency"
# Backend gave up measuring the declared battery (spec 2026-09-14 §7.2): ask for the
# nameplate pair instead. Unlike the three adopt repairs above this is not a nudge to
# improve a value - without a capacity there is no grade at all.
_ISSUE_BATTERY_NEEDS_INPUT = "battery_needs_input"
# The household energy balance does not add up (backend spec 2026-09-28): the battery takes
# more than solar and grid deliver in a large share of the data, so the sensors don't describe
# the whole house and the grade is kept out of the peer comparison. The backend decides
# (`energy_balance.excluded`, one derivation) - we never re-derive its threshold here. One
# issue id per entry; the translation key carries the cause (solar_low/import_low/
# source_missing/unclear), so a changed cause re-renders the same issue.
_ISSUE_ENERGY_BALANCE = "energy_balance"
_ENERGY_BALANCE_CAUSES = ("solar_low", "import_low", "source_missing", "unclear")
# Laddning och urladdning omkastade enligt batteriets laddningsnivå. Servern avgör
# (battery_direction.state == "reversed", tre-dygnsgolvet ligger där) - vi räknar aldrig om något
# här. The repair is fixable when the block carries a non-empty `swap_intervals` (the
# server placed the boundaries on the hour, spec §10): the fix flips exactly those
# intervals, whatever the form. Without the key (an older backend) the v0.43 rule holds:
# only "all" and "partial" are fixable, by flipping the whole history.
_ISSUE_BATTERY_DIRECTION = "battery_direction"
_BATTERY_DIRECTION_FORMS = ("all", "partial", "since", "ended")
_BATTERY_DIRECTION_FIXABLE = ("all", "partial")
# Forms whose own translation key carries a description, not a fix flow (hassfest allows
# only one of the two per key): when fixable they use "<key>_flip"; "all"/"partial" use
# _ISSUE_BATTERY_DIRECTION_NO_FIX when the server found no boundary to flip.
_BATTERY_DIRECTION_TEXT_FORMS = ("since", "ended")
_ISSUE_BATTERY_DIRECTION_NO_FIX = "battery_direction_no_fix"
_QUARTER = timedelta(minutes=15)
# Fallback for the server's detect_min_days when the profile doesn't carry it (older
# backend). Shared by WoltaData's default and the profile read so the sensor never
# reports a different minimum than the evaluation assumed.
_DETECT_MIN_DAYS_DEFAULT = 30
# A picked compensation sensor that never yields a figure (spec 2026-08-28). The
# usual cause is the wrong state_class: only `total`/`total_increasing` produce the
# SUM statistics monthly_amounts reads, while `measurement` yields min/mean/max and
# nothing this feature can use. Without this the user sees a filled-in field in
# Configure and no effect at all - indistinguishable from the feature working.
_ISSUE_FLEX_NO_STATS = "flex_compensation_no_statistics"

# Consecutive BAD cycles before the repair is raised - a cycle that read nothing, or
# one whose PATCH the server rejected. At the 6h slow poll that is roughly a day -
# far past the few minutes a freshly added sensor needs for its first statistics
# compile, so a correct setup never sees it.
#
# The rejection leg exists because the compensation PATCH is a PERMANENT loop when it
# fails: nothing is bookmarked, so the next cycle reads the same months and re-sends
# the identical payload. A payload the server refuses is therefore refused forever,
# and the empty-read leg cannot notice - there the mapping is non-empty, which is the
# whole problem. Without this the sync dies silently and stays dead.
_FLEX_EMPTY_CYCLES_BEFORE_ISSUE = 3

# A configured energy stream that reads EMPTY. Since stats.py stopped fabricating
# 0.0 buckets for rows without a change value (the measurement-sensor shape), a
# mispicked power (W) sensor produces an empty stream instead of a stream of zeros.
# That trade - corruption for silence - is only safe if the silence is visible:
# without this repair, a filled-in field in Configure plus no data at all is
# indistinguishable from the integration working.
_ISSUE_ENERGY_NO_STATS = "energy_stream_no_statistics"
# One selected sensor is silent (stopped measuring, see _silent_sensors) while the others
# are not. Complete quarters need every selected sensor, so that one sensor stops the
# whole upload - and when its stream has another sensor that delivers, the stream is not
# empty and energy_stream_no_statistics never sees it. Same threshold and minimum
# window as energy_empty below.
_ISSUE_STATISTICS_MISSING = "statistics_missing"
# A graded battery plant that has no state-of-charge sensor picked. The field is optional
# and was only added in v0.40.0, so every earlier install has it empty without ever having
# been asked. Fixable: the repair opens the picker (or records a no).
_ISSUE_SOC_MISSING = "soc_missing"

# Consecutive bad read cycles before the repair is raised. Same threshold and
# rationale as the flex twin above: at the 6 h slow poll this is roughly a day,
# far past the few minutes a freshly added sensor needs for its first statistics
# compile, so a correct setup never sees it.
_ENERGY_EMPTY_CYCLES_BEFORE_ISSUE = 3

# Read windows shorter than this are not judged at all (neither counted nor
# cleared). Right after a successful upload the next window can be seconds wide -
# fast poll runs every 60 s - and legitimately holds no compiled 5-min rows for
# ANY stream. Counting those would trip the repair in minutes on healthy setups;
# clearing on them would give a broken setup a false all-clear. Two 5-min compile
# periods plus margin is the smallest window where "no rows" means something.
_ENERGY_EMPTY_MIN_WINDOW = timedelta(minutes=30)

# Measured-parameter adopt gates (conservative: the backend's observed_* are all-time figures
# from the meter, so we only propose adopting when we're confident the entered value is wrong).
#   MATURE_DAYS   – enough history that the reconstruction is trustworthy
#   PLATEAU_DAYS  – (capacity) the observed ceiling recurred on this many days (real cap)
#   GAP           – ignore differences smaller than this vs the configured value
# For capacity, all-time monotonicity + the GAP give natural hysteresis: after adopting, the
# effective window == measured, the gap collapses, and it can only re-fire on a genuine upward
# ratchet (never ping-pong down).
_CAP_MATURE_DAYS = 60
_CAP_PLATEAU_DAYS = 10
_CAP_GAP = 0.15
# observed_capacity reconstructs the SoC window assuming ~0.95 charge efficiency. A plant whose
# real round-trip efficiency is well below that (parasitic/standby losses – e.g. Emaldo, issue #1)
# accumulates too much, so the measured window OVERSHOOTS true usable. Below this round-trip
# threshold the reconstruction is untrustworthy in the UP direction, so we don't offer to raise
# capacity to it (inflation reaches ~6 % at 0.80 and climbs fast toward the 15 % gap below that).
_CAP_TRUST_EFF = 0.80
# Power: observed peak is a LOWER bound. Raising (observed > configured) is high-confidence, so
# a smaller gap suffices; lowering (observed < configured) risks nudging a correct-but-gently-used
# value down (flattering the grade), so demand a bigger gap.
_POWER_GAP = 0.15
_POWER_GAP_SHRINK = 0.30
# A battery cannot sustain charge/discharge above ~1.5× its declared inverter power (the same
# physical ceiling the backend's per-hour energy cap uses, spec 2026-08-03). observed_power above
# this multiple of the declared power (nameplate or configured) is a sensor artefact → don't
# nudge the user to raise their (correct) value to it.
_POWER_MAX_PHYSICAL = 1.5
_EFF_ABS_GAP = 0.08        # absolute round-trip gap (eff is 0.5–1.0; a true measurement)
_EFF_MAX_PLAUSIBLE = 0.98  # don't adopt an implausibly high round-trip (clamp/boundary artefact)

# Profile fields mirrored from the server into entry.data (cache only – the server
# is the source of truth; entry.data is never used as a diff base since v0.10.0).
_PROFILE_SYNC_KEYS: tuple[str, ...] = (
    CONF_ZONE, CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF, CONF_RESERVE_PCT,
    CONF_COST_SEK, CONF_PURCHASE_DATE, CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE,
    CONF_EXPORT_EXTRA_ORE, CONF_GRID_VAR_PCT, CONF_EXPORT_EXTRA_PCT,
)


# ---------------------------------------------------------------------------
# Public data type + type alias
# ---------------------------------------------------------------------------


@dataclass
class WoltaData:
    """Data exposed by the coordinator to all platform entities."""

    results: dict  # latest /results response
    last_uploaded: datetime | None
    n_days: int
    pending: bool  # server-side job running
    # Serverns batteristämpel ur profil-GET:en (spec 2026-09-14 §4). Separat från
    # `pending` ovan: en kapacitet som ännu inte är MÄTT är inget jobb som kör.
    battery_status: str | None = None
    battery_detect: dict | None = None
    detect_min_days: int = _DETECT_MIN_DAYS_DEFAULT


type WoltaConfigEntry = ConfigEntry[WoltaCoordinator]


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class WoltaCoordinator(DataUpdateCoordinator[WoltaData]):
    """Manage fetching Wolta results and uploading HA statistics."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="wolta",
            config_entry=entry,
            update_interval=_SLOW_POLL,
        )
        self.token: str = entry.data[CONF_TOKEN]
        self._zone: str = entry.data[CONF_ZONE]
        # Visningsläge (bundna anläggningar): polla bara resultat - rör aldrig
        # statistikläsning, PUT /data eller recompute-kadensen. Se const.CONF_VIEW_ONLY.
        self._view_only: bool = bool(entry.data.get(CONF_VIEW_ONLY))
        # Vendor-neutral flex-market flag (spec 2026-08-26): a binary_sensor that is
        # 'on' while the battery is externally controlled (e.g. derived from a Tibber
        # Grid Rewards state sensor). Client-local, never PATCHed to the server -
        # see const.CONF_EXTERNAL_CONTROL for the full rationale.
        self._external_entity: str | None = entry.data.get(CONF_EXTERNAL_CONTROL) or None
        # Compensation sensor (spec 2026-08-28): optional currency sensor whose
        # monthly totals are forwarded as flex_compensation records. Unlike
        # _external_entity this is NOT an upload transformation - it touches no
        # energy row, and therefore stays out of the entity fingerprint (B8).
        self._flex_entity: str | None = entry.data.get(CONF_FLEX_COMPENSATION) or None
        # Optional state-of-charge sensors (spec 2026-10-03). Their statistics ride along
        # with the flow rows as battery_state. Like the flex sensor they stay OUT of the
        # entity fingerprint (a SoC change touches no flow field);
        # adding one runs a SoC-only backfill instead, see _track_soc_selection.
        self._soc_entities: list[str] = list(entry.data.get(CONF_SOC) or [])
        # Per-entry issue id (the form _set_measured_issue already uses), NOT the
        # shared id the two older non-fixable issues carry: with two Wolta entries a
        # shared id would let a healthy plant delete a broken plant's warning - a
        # false all-clear on the exact condition this repair exists to report.
        self._flex_issue_id: str = f"{_ISSUE_FLEX_NO_STATS}_{entry.entry_id}"
        self._energy_issue_id: str = f"{_ISSUE_ENERGY_NO_STATS}_{entry.entry_id}"
        self._stats_issue_id: str = f"{_ISSUE_STATISTICS_MISSING}_{entry.entry_id}"
        # Completeness of the latest regular read (quarters dropped as incomplete and the
        # sensors that caused it most), for the diagnostics. Set by _backfill_rows,
        # _heal_rows and _incremental_rows only: a coverage fill in the same cycle reads
        # older history and must not overwrite the figures of the current upload.
        self._last_completeness: dict = {"incomplete_quarters": 0, "entities": []}
        # Serverns batteristämpel, senast lästa (spec 2026-09-14 §4). Ägs av profil-GET:en
        # i båda pollarna - se _take_battery_state.
        self._battery_status: str | None = None
        self._battery_detect: dict | None = None
        self._detect_min_days: int = _DETECT_MIN_DAYS_DEFAULT

        # Normalise entry data to lists for backward compat with v0.1.0 (plain strings)
        def _to_list(val: str | list | None) -> list[str]:
            if isinstance(val, list):
                return val
            if isinstance(val, str) and val:
                return [val]
            return []

        self._entity_map: dict[str, list[str]] = {
            "batt_in": _to_list(entry.data.get(CONF_BATT_IN)),
            "batt_out": _to_list(entry.data.get(CONF_BATT_OUT)),
            "grid_in": _to_list(entry.data.get(CONF_GRID_IN)),
            "grid_out": _to_list(entry.data.get(CONF_GRID_OUT)),
            "solar": _to_list(entry.data.get(CONF_SOLAR)),
        }
        session = async_get_clientsession(hass)
        self.client = WoltaApiClient(session, base_url=WOLTA_API_BASE)
        self._store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        self._state: dict = {}
        # Daylight per UTC day for the sun computation (_sun_down_fn), keyed by the
        # observer it was computed for so a moved home position starts afresh.
        self._daylight_key: tuple | None = None
        self._daylight_cache: dict[int, tuple[float, float] | None] = {}
        # Replacement job types by the job's "type" (see _run_replacement_job).
        self._replacement_types: dict[str, _ReplacementType] = {
            "direction": _ReplacementType(
                next_replacement=self._direction_next,
                on_committed=self._direction_committed,
                on_finished=self._direction_finished,
            ),
            "entities": _ReplacementType(
                next_replacement=self._entities_next,
                on_committed=self._entities_committed,
                on_finished=self._entities_finished,
                rows_for=self._entities_rows,
            ),
        }
        # The rows an entity job's plan built, per (job target, phase, window start, window
        # end), handed to _entities_rows when the runner asks for them, so a year of
        # statistics is not read again for every window. Taken once; lost on a restart,
        # when the same read is made again.
        self._entity_rows_cache: dict[tuple[str, str, str, str], list[dict]] = {}
        # What the replacement job did in the current cycle (_run_replacement_job): a
        # commit, an open with its rows staged, and the first job-level failure.
        self._job_cycle: dict = {"committed": False, "staged": False, "failure": None}

    # ------------------------------------------------------------------
    # Setup – load persisted state (bookmark, last_recompute, failure counter)
    # ------------------------------------------------------------------

    async def _async_setup(self) -> None:
        """Load persisted coordinator state from Store."""
        loaded = await self._store.async_load()
        self._state = dict(loaded) if loaded else {}
        _LOGGER.debug("Coordinator state loaded: %s", self._state)
        await self._migrate_invert_intervals()
        # Sidopoll för webbändringar (delad profil) – avregistreras vid unload.
        self.config_entry.async_on_unload(
            async_track_time_interval(
                self.hass, self.async_check_profile_sync, _PROFILE_SYNC_INTERVAL
            )
        )

    async def _migrate_invert_intervals(self) -> None:
        """Move the boolean invert flag to intervals, once (no row moves).

        Desired (entry.data): missing intervals become [[None, None]] when
        CONF_INVERT_BATTERY is set, else []. Applied (Store): missing intervals come from
        applied_invert - what the stored rows were built with - or, without that either,
        from the desired value. The legacy keys are left as they are."""
        data = self.config_entry.data
        if CONF_INVERT_INTERVALS in data:
            desired = self._desired_intervals()
        else:
            desired = [(None, None)] if data.get(CONF_INVERT_BATTERY) else []
            self._set_desired_intervals(desired)
        if "applied_invert_intervals" in self._state:
            return
        applied_invert = self._state.get("applied_invert")
        if applied_invert is None:
            applied = desired
        else:
            applied = [(None, None)] if applied_invert else []
        self._state["applied_invert_intervals"] = intervals.dump(applied)
        await self._store.async_save(self._state)

    def _desired_intervals(self) -> list[intervals.Interval]:
        """The battery inversion the owner wants (entry.data)."""
        return intervals.parse(self.config_entry.data.get(CONF_INVERT_INTERVALS))

    def _applied_intervals(self) -> list[intervals.Interval]:
        """The battery inversion the server's stored rows are built with (Store).
        Every row this client builds uses these, never the desired ones."""
        return intervals.parse(self._state.get("applied_invert_intervals"))

    def _set_desired_intervals(self, desired: list[intervals.Interval]) -> None:
        """Write the desired intervals to entry.data, mirroring CONF_INVERT_BATTERY as
        "everything inverted" so a downgrade reads the right global flag in the common
        case. Writes only on an actual change."""
        entry = self.config_entry
        new_data = with_desired_intervals(entry.data, desired)
        if new_data != dict(entry.data):
            self.hass.config_entries.async_update_entry(entry, data=new_data)

    def _set_applied_intervals(self, applied: list[intervals.Interval]) -> None:
        """Record what the server's stored rows are now built with (saved by the caller).
        The legacy applied_invert follows as "everything inverted", so a downgrade to a
        version with the global flag does not re-send the history needlessly."""
        stored = intervals.dump(intervals.normalize(applied))
        self._state["applied_invert_intervals"] = stored
        self._state["applied_invert"] = stored == [[None, None]]

    # ------------------------------------------------------------------
    # Replacement of stored flows (server spec 2026-10-13 §5, §9)
    # ------------------------------------------------------------------

    @staticmethod
    def _new_replacement_job(job_type: str, target) -> dict:
        """A replacement job as kept in Store under "replacement_job". At most one exists.

        `target` is the desired value the job brings to the applied state; `replacement`
        is the server-side replacement in flight ({id, kind, window_start, window_end,
        fields, reason}) or None; `done_windows` lists the committed windows, so a
        restart never repeats one; `retry_at` (ISO) holds the job back after a 429."""
        return {
            "type": job_type,
            "target": target,
            "replacement": None,
            "done_windows": [],
            "retry_at": None,
        }

    async def _run_replacement_job(self, now: datetime, profile: dict | None) -> bool:
        """Advance the replacement job, before the regular upload. Returns True when the
        regular upload must pause this cycle: a replacement is open on the server, and
        every regular write would move the plant's flows_version under it, so its
        commit would be refused (409 conflict, server spec §7). A job that has not
        opened anything (waiting, or held back by a 429) pauses nothing.

        A replacement left in Store by an earlier cycle is resumed by asking the server
        first: committed is applied, open is staged and committed again, and anything
        else (aborted, expired, restored, unknown) starts over with a new id."""
        job = self._state.get("replacement_job")
        if not job:
            return False
        hooks = self._replacement_types.get(job.get("type"))
        if hooks is None:
            # A type this version does not know can never finish; keeping it would hold
            # the regular upload back for good. An open replacement expires server-side.
            _LOGGER.warning("Dropping a Wolta replacement job of unknown type %r", job.get("type"))
            self._state.pop("replacement_job", None)
            await self._store.async_save(self._state)
            return False
        retry_at = job.get("retry_at")
        if retry_at:
            if now < self._parse_retry_at(retry_at):
                return self._replacement_held(job)
            job["retry_at"] = None
            await self._store.async_save(self._state)
        self._job_cycle = {"committed": False, "staged": False, "failure": None}
        held = await self._advance_replacement_job(job, hooks, now, profile)
        if await self._count_job_cycle(job, now):
            return False  # parked: nothing of it is held open
        return held

    async def _advance_replacement_job(
        self, job: dict, hooks: _ReplacementType, now: datetime, profile: dict | None
    ) -> bool:
        """One cycle of the job: up to _REPLACEMENTS_PER_CYCLE replacements, each resumed
        or opened, staged and committed. Returns whether the regular upload must pause
        (_run_replacement_job)."""
        try:
            for _ in range(_REPLACEMENTS_PER_CYCLE):
                rep = job.get("replacement")
                opened = False
                rows: list[dict] | None = None
                if rep is not None:
                    status = await self._replacement_status(rep)
                    if status is not None and status["status"] == "committed":
                        await self._replacement_committed(
                            job, hooks, rep, status.get("result") or {}
                        )
                        continue
                    if status is not None and rep.get("abandoned"):
                        # Given up earlier: it is never committed any more.
                        await self._discard_replacement(rep["id"])
                        status = None
                    if status is None:
                        job["replacement"] = None
                        await self._store.async_save(self._state)
                        rep = None
                    else:
                        opened = True
                        if rep["kind"] == "rows":
                            rows = await hooks.rows_for(job, rep, now)
                            if rows is None:
                                # Its rows cannot be built again: waiting would hold the
                                # upload until the server expires it. Drop it and plan anew.
                                await self._discard_replacement(rep["id"])
                                job["replacement"] = None
                                await self._store.async_save(self._state)
                                rep, opened = None, False
                if rep is None:
                    if profile is None:
                        # Fast-poll (or no profile this cycle): only an open replacement
                        # is carried on; a new one waits for a full cycle.
                        return False
                    spec = await hooks.next_replacement(job, now, profile)
                    if spec is None:
                        return False
                    if spec == "done":
                        await hooks.on_finished(job)
                        self._state.pop("replacement_job", None)
                        await self._store.async_save(self._state)
                        _LOGGER.info("Wolta replacement job (%s) finished", job["type"])
                        return False
                    if self._window_key(spec) in job["done_windows"]:
                        # A hook error: committing the window again would, for a swap,
                        # undo it. Keyed on the phase too: two phases of one job may
                        # replace different fields over the same window.
                        _LOGGER.warning(
                            "Wolta replacement job (%s) asked for a window it already "
                            "committed (%s..%s); not opening it",
                            job["type"], spec["window_start"], spec["window_end"],
                        )
                        return False
                    if spec["kind"] == "rows":
                        # Built before the id is assigned: rows that cannot be built now
                        # leave nothing behind that would pause the upload.
                        rows = await hooks.rows_for(job, spec, now)
                        if rows is None:
                            return False
                    rep = {**spec, "id": str(uuid4())}
                    job["replacement"] = rep
                    job.pop("failures", None)
                result = await self._execute_replacement(rep, rows, now, opened=opened)
                if result is None:
                    return self._replacement_held(job)
                await self._replacement_committed(job, hooks, rep, result)
        except (WoltaAuthError, aiohttp.ClientError, TimeoutError):
            raise  # lost profile → reauth; unreachable server → the cycle fails as before
        except WoltaRateLimitError as err:
            job["retry_at"] = (now + timedelta(seconds=err.retry_after)).isoformat()
            await self._store.async_save(self._state)
            _LOGGER.warning(
                "Wolta replacement rate limited; retrying in %s s", err.retry_after
            )
        except WoltaApiError as err:
            rep = job.get("replacement")
            if err.status is not None and 400 <= err.status < 500:
                self._note_job_failure(f"HTTP {err.status}")
            if rep is not None and err.status is not None and 400 <= err.status < 500:
                # A refusal that repeating the same requests would not change (413: the
                # result would exceed the profile's row limit; 409 without a code: a
                # plant bound to a stream; 422: rows the server will not take). Drop the
                # replacement so the regular upload is not held back by it.
                await self._discard_replacement(rep["id"])
                job["replacement"] = None
                if err.status == 413:
                    job["retry_at"] = (now + _REPLACEMENT_FULL_RETRY).isoformat()
                await self._store.async_save(self._state)
                _LOGGER.warning(
                    "Wolta replacement refused (HTTP %s); retrying later", err.status
                )
            else:
                _LOGGER.warning(
                    "Wolta replacement failed (HTTP %s); retrying next cycle", err.status
                )
                await self._replacement_failed(job, hooks, now)
        except Exception as err:  # pylint: disable=broad-except
            # A job that cannot run must not stop the regular upload: a failing hook is
            # counted like a server error, so an open replacement is given up in time.
            _LOGGER.warning(
                "Wolta replacement job failed (%s); retrying next cycle", type(err).__name__
            )
            _LOGGER.debug("Replacement job failure detail", exc_info=True)
            self._note_job_failure(type(err).__name__)
            await self._replacement_failed(job, hooks, now)
        return self._replacement_held(job)

    def _note_job_failure(self, why: str) -> None:
        """Record a job-level failure in this cycle (see _JOB_MAX_FAILED_CYCLES): one
        that repeating the same cycle would most likely meet again."""
        if self._job_cycle.get("failure") is None:
            self._job_cycle["failure"] = why

    async def _count_job_cycle(self, job: dict, now: datetime) -> bool:
        """Count the cycle the job just ran. Progress - a commit, or an open with its
        rows staged and no job-level failure after it - resets the count; a cycle with a
        job-level failure adds one; a cycle that only waited changes nothing. At
        _JOB_MAX_FAILED_CYCLES in a row the job is parked (_park_job) - unless it still
        holds a replacement: an open one is given up by _replacement_failed first, and
        an abandoned one is never parked, since it may have been committed. Returns
        True when the job was parked."""
        if self._state.get("replacement_job") is not job:
            return False  # finished, or dropped
        cycle = self._job_cycle
        if cycle["committed"] or (cycle["staged"] and cycle["failure"] is None):
            if job.pop("job_failures", None) is not None:
                await self._store.async_save(self._state)
            return False
        if cycle["failure"] is None:
            return False
        job["job_failures"] = job.get("job_failures", 0) + 1
        if job["job_failures"] < _JOB_MAX_FAILED_CYCLES or job.get("replacement") is not None:
            await self._store.async_save(self._state)
            return False
        await self._park_job(job, now, cycle["failure"])
        return True

    async def _park_job(self, job: dict, now: datetime, why: str) -> None:
        """Take a job that keeps failing out of the slot, and queue none of its type
        for _JOB_PARK_BACKOFF, so the other type can run meanwhile. What it committed
        stays: the windows a direction job swapped are folded into the applied
        intervals, so the job queued after the backoff does not swap them back. A
        sensor change keeps its applied fingerprint; its next job replaces the same
        values again where it already did."""
        job_type = job.get("type")
        if job_type == "direction" and job.get("done_windows"):
            self._set_applied_intervals(intervals.sym_diff(
                self._applied_intervals(), intervals.parse(job["done_windows"])))
        self._state.pop("replacement_job", None)
        self._entity_rows_cache = {}
        until = now + _JOB_PARK_BACKOFF
        self._state.setdefault("job_backoff", {})[job_type] = until.isoformat()
        await self._store.async_save(self._state)
        _LOGGER.warning(
            "Wolta replacement job (%s) failed %s cycles in a row (last: %s); parking it "
            "until %s", job_type, job.get("job_failures"), why, until.isoformat(),
        )

    def _job_backed_off(self, job_type: str, now: datetime) -> bool:
        """Whether a job of `job_type` was parked and its backoff has not passed."""
        backoff = self._state.get("job_backoff") or {}
        raw = backoff.get(job_type)
        if raw is None:
            return False
        if now < self._parse_retry_at(raw):
            return True
        backoff.pop(job_type, None)
        if not backoff:
            self._state.pop("job_backoff", None)
        return False

    @staticmethod
    def _window_key(spec: dict) -> list[str]:
        """How a committed window is booked in done_windows: [start, end], plus the
        phase for a job type with phases (a direction job reads its windows back as
        intervals, so they stay pairs)."""
        key = [spec["window_start"], spec["window_end"]]
        if spec.get("phase"):
            key.append(spec["phase"])
        return key

    @staticmethod
    def _replacement_held(job: dict) -> bool:
        """Whether the job holds a replacement open on the server (the upload pauses).
        One given up (`abandoned`) is never committed, so it pauses nothing."""
        rep = job.get("replacement")
        return rep is not None and not rep.get("abandoned")

    @staticmethod
    def _parse_retry_at(value) -> datetime:
        """`retry_at` as an aware datetime; anything unreadable counts as expired."""
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            _LOGGER.debug("Ignoring an unreadable replacement retry_at %r", value)
            return datetime.min.replace(tzinfo=timezone.utc)
        return parsed

    async def _replacement_failed(
        self, job: dict, hooks: _ReplacementType, now: datetime
    ) -> None:
        """Count a cycle that failed without a verdict from the server. Past
        _REPLACEMENT_MAX_FAILURES the replacement is given up: committed after all (a
        502/504 can follow a commit that went through) is booked; anything else is
        deleted and cleared. Either way the job waits _REPLACEMENT_GIVE_UP_RETRY and
        the upload goes on. When the server cannot even say which it is, the
        replacement stays in the job marked `abandoned` - pausing nothing, never
        committed - until a GET can tell, so a committed swap is never repeated."""
        rep = job.get("replacement")
        if rep is None:
            await self._store.async_save(self._state)
            return
        job["failures"] = job.get("failures", 0) + 1
        if job["failures"] <= _REPLACEMENT_MAX_FAILURES:
            await self._store.async_save(self._state)
            return
        _LOGGER.warning(
            "Wolta replacement failed %s cycles in a row; giving it up for now",
            job["failures"],
        )
        booked = False
        try:
            status = await self._replacement_status(rep)
        except WoltaAuthError:
            raise
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Replacement status unreadable while giving up", exc_info=True)
            await self._discard_replacement(rep["id"])
            rep["abandoned"] = True
        else:
            if status is not None and status["status"] == "committed":
                await self._replacement_committed(job, hooks, rep, status.get("result") or {})
                booked = True
            elif status is not None:
                await self._discard_replacement(rep["id"])
            job["replacement"] = None
        job.pop("failures", None)
        if not booked:
            # A window that went through after all is progress: the job goes on next
            # cycle instead of waiting a day.
            job["retry_at"] = (now + _REPLACEMENT_GIVE_UP_RETRY).isoformat()
        await self._store.async_save(self._state)

    async def _replacement_status(self, rep: dict) -> dict | None:
        """The server's view of a replacement this client opened earlier: the GET body
        when it is open or committed, None when it is gone and must start over.

        Only the server's own answers about the replacement count as "gone" (an unknown
        id, 410, or a closed status). Any other refusal of the GET itself - a 403 from a
        proxy, a 400, a 408 - says nothing about the replacement: it is raised as an
        unreadable status, so the replacement is kept (and in the end abandoned, never
        discarded), since it may have been committed."""
        try:
            status = await self.client.get_replacement(self.token, rep["id"])
        except WoltaReplacementGone:
            return None
        except (WoltaAuthError, WoltaRateLimitError):
            raise
        except WoltaApiError as err:
            raise WoltaApiError(
                f"Replacement status unreadable (HTTP {err.status})") from err
        state = status.get("status") if isinstance(status, dict) else None
        if state in ("open", "committed"):
            return status
        if state in ("aborted", "expired", "restored"):
            return None
        # Unreadable: guessing "gone" could swap a committed window twice.
        raise WoltaApiError(f"Unexpected replacement status {state!r}")

    async def _replacement_committed(
        self, job: dict, hooks: _ReplacementType, rep: dict, result: dict
    ) -> None:
        """Book a committed window: done_windows, the type's hook, and no replacement in
        flight - saved together, so a restart neither repeats nor loses the window."""
        self._job_cycle["committed"] = True
        window = self._window_key(rep)
        if window not in job["done_windows"]:
            job["done_windows"].append(window)
        try:
            await hooks.on_committed(job, rep, result)
        except Exception as err:  # pylint: disable=broad-except
            # The window is committed whatever the hook did: never hold it open.
            _LOGGER.warning(
                "Wolta replacement hook failed after a commit (%s)", type(err).__name__
            )
            _LOGGER.debug("on_committed failure detail", exc_info=True)
        job["replacement"] = None
        job.pop("failures", None)
        await self._store.async_save(self._state)
        _LOGGER.info(
            "Wolta replacement committed (%s, %s, %s..%s)",
            job["type"], rep["kind"], rep["window_start"], rep["window_end"],
        )

    async def _execute_replacement(
        self, rep: dict, rows: list[dict] | None, now: datetime, *, opened: bool = False
    ) -> dict | None:
        """Open (idempotent on the id), stage the rows in chunks and commit one
        replacement of the current job. `rows` is None for "swap_battery"; `opened` skips
        the open of a replacement the server reported open.

        Returns the commit result ({} when the server had nothing to replace, 409
        empty), or None to try again later. On the server errors of spec §5.3-5.4 the
        job's replacement is cleared, so the next attempt opens a new id: 409 conflict
        (the server aborted it), 410 or an unknown id (gone), and a second 409
        count_mismatch after the rows were sent again. A 429 on open (archive_full)
        holds the job back until Retry-After. Store is saved before every call that
        can change the server's state."""
        job = self._state["replacement_job"]
        rid = rep["id"]
        if not opened and not await self._open_replacement(job, rep, now):
            return None
        expected = None if rows is None else len({row["ts"] for row in rows})
        try:
            if rows is not None:
                await self._store.async_save(self._state)
                await self.client.stage_replacement_rows(self.token, rid, rows)
            self._job_cycle["staged"] = True
            for attempt in (1, 2):
                await self._store.async_save(self._state)
                try:
                    return await self.client.commit_replacement(self.token, rid, expected)
                except WoltaReplacementConflict as err:
                    if err.code == "count_mismatch" and rows is not None and attempt == 1:
                        _LOGGER.warning(
                            "Wolta replacement staging incomplete; sending the rows again"
                        )
                        await self._store.async_save(self._state)
                        await self.client.stage_replacement_rows(self.token, rid, rows)
                        continue
                    if err.code == "empty":
                        # Nothing to replace in the window; it stays open server-side.
                        await self._discard_replacement(rid)
                        return {}
                    raise
        except WoltaReplacementConflict as err:
            self._note_job_failure(f"409 {err.code}")
            if err.code != "conflict":  # conflict: the server already aborted it
                await self._discard_replacement(rid)
            _LOGGER.warning(
                "Wolta replacement refused (%s); starting over with a new id", err.code
            )
        except WoltaReplacementGone as err:
            self._note_job_failure(f"gone ({err.code or err.status})")
            _LOGGER.warning(
                "Wolta replacement closed by the server (%s); starting over with a new id",
                err.code or err.status,
            )
        job["replacement"] = None
        await self._store.async_save(self._state)
        return None

    async def _open_replacement(self, job: dict, rep: dict, now: datetime) -> bool:
        """Open `rep` on the server after saving its id. False when it was not opened;
        the job then has no replacement, so nothing pauses."""
        await self._store.async_save(self._state)
        try:
            await self.client.open_replacement(
                self.token,
                rep["id"],
                kind=rep["kind"],
                window_start=datetime.fromisoformat(rep["window_start"]),
                window_end=datetime.fromisoformat(rep["window_end"]),
                fields=rep.get("fields"),
                reason=rep["reason"],
            )
        except WoltaAuthError:
            raise
        except WoltaRateLimitError as err:
            # 429 archive_full: the plant's archive of replaced rows is full until its
            # oldest entries age out (Retry-After).
            job["replacement"] = None
            job["retry_at"] = (now + timedelta(seconds=err.retry_after)).isoformat()
            await self._store.async_save(self._state)
            _LOGGER.warning(
                "Wolta cannot open a replacement now (archive full or rate limited); "
                "retrying in %s s",
                err.retry_after,
            )
            return False
        except WoltaApiError as err:
            if err.status is not None and 400 <= err.status < 500:
                # A refusal (422, a stream-bound 409, a conflict or a gone id): counted,
                # so one that never passes parks the job instead of holding the slot.
                self._note_job_failure(f"open refused (HTTP {err.status})")
            job["replacement"] = None
            await self._store.async_save(self._state)
            _LOGGER.warning(
                "Wolta replacement could not be opened (HTTP %s); retrying next cycle",
                err.status,
            )
            return False
        return True

    def _replacement_floor(self, since: datetime) -> datetime:
        """The earliest hour any replacement of this client may touch:
        max(floor_hour(data_since), floor_hour(entry created_at - _BACKFILL_DAYS)).

        This integration never uploads older history than that (its backfill and the
        coverage check both reach _BACKFILL_DAYS back from when it was set up), so rows
        before it came from elsewhere - a CSV upload or the web - and are left alone: a
        direction swap has no state-of-charge grounds there, and a sensor change would
        mix the new sensor's hourly values into rows this client never built. An entry
        without a readable, timezone-aware created_at falls back to data_since."""
        floor = _floor_hour(since)
        created = getattr(self.config_entry, "created_at", None)
        if not isinstance(created, datetime) or created.tzinfo is None:
            _LOGGER.debug(
                "No usable config entry created_at (%r); replacements start at data_since",
                created)
            return floor
        return max(floor, _floor_hour(created - timedelta(days=_BACKFILL_DAYS)))

    async def _discard_replacement(self, rid: str) -> None:
        """Best-effort DELETE of a replacement left open server-side (it expires anyway)."""
        try:
            await self.client.delete_replacement(self.token, rid)
        except WoltaAuthError:
            raise
        except (WoltaApiError, aiohttp.ClientError, TimeoutError):
            _LOGGER.debug("Discarding a replacement failed; it expires server-side", exc_info=True)

    # ------------------------------------------------------------------
    # Direction job: swap the battery fields of stored rows (server spec §9.3)
    # ------------------------------------------------------------------

    async def _queue_direction_job(self, now: datetime | None = None) -> None:
        """Start a direction job when the desired inversion differs from the applied one
        and no job runs. Nothing is re-sent and the bookmark stays: the server swaps the
        battery fields of the rows it holds and queues the recount at commit. A change of
        the desired value while a job runs is picked up here once that job is done.

        A direction change goes before a sensor change: an entity job that has not
        started on the server yet gives way, and is queued again once this one is done.
        None is queued while a parked direction job's backoff lasts (_park_job)."""
        job = self._state.get("replacement_job")
        if job and not self._idle_entity_job(job):
            return
        if self._job_backed_off("direction", now or dt_util.utcnow()):
            return
        desired = self._desired_intervals()
        if not intervals.sym_diff(desired, self._applied_intervals()):
            return
        if job:
            _LOGGER.info(
                "Wolta sensor change postponed: the battery direction is swapped first")
        self._state["replacement_job"] = self._new_replacement_job(
            "direction", intervals.dump(intervals.normalize(desired))
        )
        await self._store.async_save(self._state)
        _LOGGER.info("Wolta battery direction changed; swapping it in the stored history")

    async def _direction_next(self, job: dict, now: datetime, profile: dict | None):
        """The next swap of the direction job, in chronological order.

        The difference D = target Δ applied, less the windows already committed, is
        bounded to [replacement floor, ceil_hour(bookmark + 15 min)) - the floor is
        floor_hour(data_since), but never more than _BACKFILL_DAYS before the entry was
        set up (_replacement_floor); outside it the server holds no rows this client
        uploaded, and later rows are built with the target - and split into whole-hour segments. Only the last segment may reach into the hour
        the bookmark lies in; its commit applies the target at once (_direction_committed),
        before any upload. An earlier one stops at the bookmark's hour, so no row is
        uploaded with the old direction into a window that is already booked."""
        target = intervals.parse(job["target"])
        remaining = intervals.difference(
            intervals.sym_diff(target, self._applied_intervals()),
            intervals.parse(job["done_windows"]),
        )
        bookmark = self._state.get("last_uploaded_ts")
        if not remaining or not bookmark:
            # Nothing stored by this client: the rows still to come carry the target.
            return "done"
        since = coverage.server_since(profile)
        if since is coverage.MISSING:
            since = coverage.server_since(job)  # the last commit's data_since
        if since is coverage.MISSING:
            _LOGGER.debug("No data_since from the server; the direction job waits")
            self._note_job_failure("no data_since from the server")
            return None
        if since is None:
            return "done"
        bookmark_end = datetime.fromisoformat(bookmark) + _QUARTER
        segments = intervals.hour_aligned(
            intervals.clip(remaining, self._replacement_floor(since), _ceil_hour(bookmark_end))
        )
        for index, (start, end) in enumerate(segments):
            final = index == len(segments) - 1
            if not final:
                end = min(end, _floor_hour(bookmark_end))
            if start < end:
                return {
                    "kind": "swap_battery",
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "fields": None,
                    "reason": "direction",
                    "final": final,
                }
        return "done"

    async def _direction_committed(self, job: dict, rep: dict, result: dict) -> None:
        """After a committed swap: keep the server's data_since for the next segment, and
        apply the target right after the last one - unless rows were uploaded past its
        window since it was chosen (a replacement given up and booked later); then the
        next segment covers them."""
        if isinstance(result, dict) and "data_since" in result:
            job["data_since"] = result["data_since"]
        if not rep.get("final"):
            return
        bookmark = self._state.get("last_uploaded_ts")
        if bookmark and _ceil_hour(datetime.fromisoformat(bookmark) + _QUARTER) > (
            datetime.fromisoformat(rep["window_end"])
        ):
            return
        self._set_applied_intervals(intervals.parse(job["target"]))

    async def _direction_finished(self, job: dict) -> None:
        self._set_applied_intervals(intervals.parse(job["target"]))

    # ------------------------------------------------------------------
    # Entity job: replace the fields of changed sensors in stored rows (K7)
    # ------------------------------------------------------------------

    def _entities_fingerprint(self) -> str:
        """The selected sensors per stream as a JSON string - what the stored rows are
        built from. external_control is in it only when a sensor is chosen, so an
        installation without one keeps the fingerprint it had before the key existed.

        The SoC sensors are deliberately NOT part of it (spec §6.5, §16): SoC has its own
        bookkeeping in _track_soc_selection, and a SoC change touches no flow field."""
        fp_map = dict(self._entity_map)
        if self._external_entity:
            fp_map["external_control"] = [self._external_entity]
        return json.dumps(fp_map, sort_keys=True)

    @staticmethod
    def _idle_entity_job(job: dict) -> bool:
        """An entity job that has nothing on the server yet: no replacement in flight
        and no window committed. Such a job may be replaced (a new target, or a direction
        change going first) without leaving anything half done."""
        return (
            job.get("type") == "entities"
            and job.get("replacement") is None
            and not job.get("done_windows")
        )

    async def _queue_entity_job(self, entities_now: str, now: datetime | None = None) -> None:
        """Start an entity job when the selected sensors differ from those the stored
        rows were built with (applied_entities). Nothing is re-sent and the bookmark stays:
        the job replaces only the fields of the streams a sensor was added to or swapped
        in (replaced_streams), from the new sensors' statistics. The first recording of
        the fingerprint (an upgrade) just records it, and a change with nothing to replace
        (sensors only removed) is applied at once.

        A job that has not started on the server is retargeted when the selection changes
        again (a new sensor without statistics would otherwise hold it for good), and
        dropped when the selection is back to the applied one. None is queued while a
        parked entity job's backoff lasts (_park_job)."""
        applied = self._state.get("applied_entities")
        if applied is None:
            self._state["applied_entities"] = entities_now
            await self._store.async_save(self._state)
            return
        job = self._state.get("replacement_job")
        if job and not self._idle_entity_job(job):
            return
        if job and job.get("target") == entities_now:
            return
        if not job and applied == entities_now:
            return
        if self._job_backed_off("entities", now or dt_util.utcnow()):
            return
        retry_at = job.get("retry_at") if job else None
        self._state.pop("replacement_job", None)
        self._entity_rows_cache = {}
        streams = replaced_streams(applied, entities_now)
        if not streams:
            self._state["applied_entities"] = entities_now
            await self._store.async_save(self._state)
            return
        new_job = self._new_replacement_job("entities", entities_now)
        new_job["streams"] = streams
        new_job["fields"] = replaced_fields(list(streams))
        new_job["judged"] = self._judged_selection(streams, entities_now)
        new_job["phases_done"] = []
        new_job["retry_at"] = retry_at  # a 429 still holds for the plant's archive
        self._state["replacement_job"] = new_job
        await self._store.async_save(self._state)
        _LOGGER.info(
            "Wolta sensor selection changed (%s); replacing those fields in the stored "
            "history", ", ".join(streams),
        )

    async def _entities_next(self, job: dict, now: datetime, profile: dict | None):
        """The next replacement of the entity job (K7), or "done".

        Two phases: the energy fields of the replaced energy streams, one replacement
        per window of complete hours (_energy_next), then external_control in one
        replacement of its own (_external_next). They differ in what bounds them - the
        energy fields by the new sensors' statistics, the flag by the binary sensor's
        recorded states - and the server needs every staged row to carry every replaced
        field, so they cannot share rows.

        Nothing stored (no bookmark, or data_since null): the rows still to come are
        built with the new sensors, so the target applies at once."""
        done = job.setdefault("phases_done", [])
        if not self._state.get("last_uploaded_ts"):
            return "done"
        streams = job.get("streams") or {}
        energy = sorted(s for s in streams if s != "external_control")
        for phase, step in (("energy", self._energy_next),
                            ("external", self._external_next)):
            if phase in done or (phase == "energy" and not energy) or (
                    phase == "external" and "external_control" not in streams):
                continue
            result = await step(job, now, profile)
            if result == "phase_done":
                done.append(phase)
                continue
            return result
        return "done"

    def _plan_since(self, profile: dict | None):
        """Where a new plan reads from: floor_hour(data_since), never before the
        replacement floor (_replacement_floor); MISSING to wait (a job-level failure),
        None when the server holds no rows (the job is then done)."""
        since = coverage.server_since(profile)
        if since is coverage.MISSING or since is None:
            if since is coverage.MISSING:
                _LOGGER.debug("No data_since from the server; the sensor change waits")
                self._note_job_failure("no data_since from the server")
            return since
        return self._replacement_floor(since)

    async def _energy_next(self, job: dict, now: datetime, profile: dict | None):
        """The next window of the energy phase, from the job's stored plan.

        The plan is made once - the read from floor_hour(data_since) to now, its
        hourly/5-minute split, and the windows with the latest quarter of each that
        carries every field - so every window and every resume reads the same. One older
        than _ENTITY_PLAN_MAX_AGE is made anew for what is not committed yet, before its
        5-minute statistics can be purged. Returns a spec, None to wait (no statistics
        from the added sensors yet), "phase_done", or "done" (no rows on the server)."""
        if self._selection_left(job, "energy"):
            return self._left_selection_step(
                job, "energy", "the sensors it was planned for are no longer selected")
        plan = job.get("energy_plan")
        if plan is not None and now - datetime.fromisoformat(plan["planned_at"]) > (
            _ENTITY_PLAN_MAX_AGE
        ):
            plan = None
        if plan is None:
            read_from = self._plan_since(profile)
            if read_from is coverage.MISSING:
                return None
            if read_from is None:
                return "done"
            streams = job.get("streams") or {}
            energy = sorted(s for s in streams if s != "external_control")
            added = sorted({e for s in energy for e in streams[s]})
            if not await self._entities_probe(added, read_from, now):
                if self._started(job):
                    return self._end_phase(
                        job, "energy", "the added sensors have no statistics any more")
                _LOGGER.debug("No statistics from the new sensors yet; the sensor change waits")
                return None
            split = _floor(now - timedelta(days=_ENTITY_SHORT_TERM_DAYS), 3600)
            built = await self._energy_build(job, read_from, split, now)
            if built is None:
                _LOGGER.debug("No complete hour from the new sensors yet; the sensor change waits")
                return self._plan_failed(job)
            windows, rows_by_window = built
            if not windows:
                if self._started(job):
                    return "phase_done"
                return None
            job.pop("plan_failures", None)
            plan = {
                "read_from": read_from.isoformat(),
                "read_end": now.isoformat(),
                "split": split.isoformat(),
                "planned_at": now.isoformat(),
                "windows": windows,
            }
            job["energy_plan"] = plan
            for (ws, we, _last), rows in zip(windows, rows_by_window):
                self._entity_rows_cache[(job["target"], "energy", ws, we)] = rows
        windows = plan["windows"]
        for index, (ws, we, last_full) in enumerate(windows):
            if [ws, we, "energy"] in job["done_windows"]:
                continue
            streams = job.get("streams") or {}
            return {
                "kind": "rows",
                "reason": "entities_changed",
                "phase": "energy",
                "window_start": ws,
                "window_end": we,
                "fields": replaced_fields([s for s in streams if s != "external_control"]),
                "read_from": plan["read_from"],
                "read_end": plan["read_end"],
                "split": plan["split"],
                # The latest quarter of the window staged with every field, and whether
                # it is the last window: the bookmark rule (_entities_committed).
                "last_full": last_full,
                "final": index == len(windows) - 1,
            }
        return "phase_done"

    @staticmethod
    def _started(job: dict) -> bool:
        """Whether the job has committed a window: it is no longer idle, so no new
        selection or direction change can replace it - only finishing it can."""
        return bool(job.get("done_windows"))

    @staticmethod
    def _judged_selection(streams: dict, target: str) -> dict[str, list[str]] | None:
        """The sensors of every stream the energy phase judges - the replaced energy
        streams, plus the other battery stream when either is replaced - as the job's
        target selects them. The phase's rows are built from the CURRENT selection
        (_build reads _stream_ids/_statistic_ids), so these are what its windows and rows
        stay valid for. None when the target cannot be read."""
        selected = _parse_fingerprint(target)
        if selected is None:
            return None
        judged = {s for s in streams if s != "external_control"}
        if judged & {"batt_in", "batt_out"}:
            judged |= {"batt_in", "batt_out"}
        return {s: sorted(selected[s]) for s in sorted(judged)}

    def _selection_left(self, job: dict, phase: str) -> bool:
        """Whether the current selection is no longer the one `phase` was planned for
        (the owner reconfigured while the job ran).

        Energy phase: any difference in the sensors of a judged stream (_judged_selection)
        - an added, removed or swapped sensor alike - since every row of the phase is
        built from the current selection: a sensor added alongside would change which
        quarters are complete, and the server removes every stored row in the window
        that has no staged row. External phase: the chosen binary sensor differs.

        It holds whether or not the job has started: a started job ends the phase
        (_end_phase); one that has committed nothing waits, its replacement in flight is
        dropped (_entities_rows gives None), and the next cycle retargets it
        (_queue_entity_job)."""
        streams = job.get("streams") or {}
        if phase == "external":
            return set(streams.get("external_control") or []) != (
                {self._external_entity} if self._external_entity else set())
        judged = job.get("judged")
        if judged is None:
            judged = self._judged_selection(streams, job.get("target"))
        if judged is None:
            return True  # an unreadable target: nothing to build its rows against
        return any(
            set(sensors) != set(self._entity_map.get(stream) or [])
            for stream, sensors in judged.items())

    def _left_selection_step(self, job: dict, phase: str, why: str) -> str | None:
        """The phase's answer once its selection has left: a started job ends the phase;
        one that has committed nothing waits for the next cycle's retarget."""
        if self._started(job):
            return self._end_phase(job, phase, why)
        _LOGGER.debug("Wolta sensor selection changed again; the sensor change is retargeted")
        job.pop("energy_plan", None)
        return None

    @staticmethod
    def _end_phase(job: dict, phase: str, why: str) -> str:
        """End a started job's phase early: the job finishes with its target, and the
        next cycle queues a new job from that target to the current selection."""
        _LOGGER.info("Wolta sensor change: ending the %s phase early (%s)", phase, why)
        job.pop("energy_plan", None)
        job.pop("plan_failures", None)
        return "phase_done"

    def _plan_failed(self, job: dict) -> str | None:
        """A cycle in which the energy plan could not be made (no complete hour from the
        added sensors). A job that has committed nothing just waits; a started one -
        which nothing else can replace - ends its phase after _ENTITY_PLAN_MAX_FAILURES
        cycles in a row."""
        if not self._started(job):
            return None
        job["plan_failures"] = job.get("plan_failures", 0) + 1
        if job["plan_failures"] >= _ENTITY_PLAN_MAX_FAILURES:
            return self._end_phase(
                job, "energy", "the added sensors' statistics cannot be read")
        return None

    async def _external_next(self, job: dict, now: datetime, profile: dict | None):
        """The external_control phase: one replacement over the span the chosen binary
        sensor's recorded states cover. No recorded state: nothing to replace, the phase
        is done. A failed read is tried again next cycle; after _EXTERNAL_MAX_ERRORS in a
        row the phase is given up, so it never holds the job (and with it every later
        direction or sensor change) for good."""
        if self._selection_left(job, "external"):
            return self._left_selection_step(job, "external", "the sensor is no longer selected")
        read_from = self._plan_since(profile)
        if read_from is coverage.MISSING:
            return None
        if read_from is None:
            return "done"
        try:
            plan = await self._external_rows(read_from, now)
        except Exception as err:  # pylint: disable=broad-except
            job["external_errors"] = job.get("external_errors", 0) + 1
            _LOGGER.debug("External-control read failure detail", exc_info=True)
            if job["external_errors"] >= _EXTERNAL_MAX_ERRORS:
                _LOGGER.warning(
                    "Wolta cannot read the external-control sensor's history (%s); its "
                    "flags in the stored history are left as they are", type(err).__name__)
                return "phase_done"
            _LOGGER.warning(
                "External-control history read failed (%s); retrying next cycle",
                type(err).__name__)
            return None
        job.pop("external_errors", None)
        if plan is None:
            return "phase_done"
        ws, we, rows = plan
        spec = {
            "kind": "rows",
            "reason": "entities_changed",
            "phase": "external",
            "window_start": ws.isoformat(),
            "window_end": we.isoformat(),
            "fields": ["external_control"],
            "read_from": read_from.isoformat(),
            "read_end": now.isoformat(),
        }
        self._entity_rows_cache[(job["target"], "external", spec["window_start"],
                                 spec["window_end"])] = rows
        return spec

    async def _entities_rows(
        self, job: dict, spec: dict, now: datetime
    ) -> list[dict] | None:
        """The rows of the entity job's replacement: those its plan just built, or on a
        resume the same read again - everything they depend on is in the spec (read
        start and end, hourly/5-minute split, window), never `now`. None when they cannot
        be built again: the runner then drops the replacement, and the energy plan is
        made anew."""
        phase = spec.get("phase", "energy")
        if self._selection_left(job, phase):
            # Reconfigured meanwhile: rows built now would have another composition than
            # the plan. The runner drops the replacement; then a started job ends the
            # phase and one that has committed nothing waits to be retargeted.
            job.pop("energy_plan", None)
            return None
        key = (job["target"], phase, spec["window_start"], spec["window_end"])
        cached = self._entity_rows_cache.pop(key, None)
        if cached is not None:
            return cached
        window = (datetime.fromisoformat(spec["window_start"]),
                  datetime.fromisoformat(spec["window_end"]))
        read_from = datetime.fromisoformat(spec.get("read_from") or spec["window_start"])
        read_end = datetime.fromisoformat(spec.get("read_end") or spec["window_end"])
        if phase == "external":
            plan = await self._external_rows(read_from, read_end, window=window)
            return None if plan is None else plan[2]
        split = (datetime.fromisoformat(spec["split"]) if spec.get("split")
                 else _floor(read_end - timedelta(days=_ENTITY_SHORT_TERM_DAYS), 3600))
        built = await self._energy_build(job, read_from, split, read_end, window=window)
        if not built or not built[1] or not built[1][0]:
            job.pop("energy_plan", None)
            return None
        return built[1][0]

    async def _entities_probe(
        self, sensors: list[str], read_from: datetime, read_end: datetime
    ) -> bool:
        """Whether any sensor ADDED to the replaced streams has hourly statistics since
        `read_from`: a cheap read before the full one, which a job waiting for a new
        sensor would otherwise repeat for the whole history every cycle."""
        if not sensors:
            return False
        raw = await async_fetch_change(
            self.hass, set(sensors), _read_start(read_from, 3600), read_end, period="hour")
        return any(measured_starts(raw.get(e) or [], 3600) for e in sensors)

    async def _energy_build(
        self,
        job: dict,
        read_from: datetime,
        split: datetime,
        read_end: datetime,
        window: tuple[datetime, datetime] | None = None,
    ) -> tuple[list[list], list[list[dict]]] | None:
        """Read the flows from `read_from` to `read_end` (hourly before `split`, 5-minute
        after) and build the energy phase's windows ([start, end, last_full] in ISO) and
        the rows of each, or None when the added sensors have measured nothing yet.

        A quarter is replaced when every sensor of the replaced streams measured it
        cleanly (both battery streams count when either is replaced, since the pair is
        replaced together), counted only from the first period every ADDED sensor
        measured - before that, a solar sensor's nights would count as known zeros back to
        read_from. The windows are the maximal runs of whole hours whose four quarters
        are all complete, from read_from on and outside the windows already committed:
        a new sensor's first period can never be clean, so a window never removes the
        hour before it, and a window's last hour is a complete one. Runs at most
        _ENTITY_GAP_MERGE apart are one window (the incomplete quarters between are
        removed as honest gaps); a longer silence stays outside every window and keeps
        its stored rows. More than _ENTITY_MAX_WINDOWS runs: the smallest gaps are merged
        first. `window` given (a resume): only that window's rows."""
        streams_by = job.get("streams") or {}
        sources = await self._entities_sources(read_from, split, read_end)
        external = await self._external_quarters(read_from, read_end)
        done = [(datetime.fromisoformat(k[0]), datetime.fromisoformat(k[1]))
                for k in job.get("done_windows") or [] if len(k) == 3 and k[2] == "energy"]
        # `now` as the split implies it, so the solar anchor reads do not move. The
        # judging, the windows and the rows are per quarter over up to a year: they run
        # in the executor with the build (_energy_windows).
        _built, planned = await self._build(
            split + timedelta(days=_SHORT_TERM_DAYS), sources,
            lambda built, streams: _energy_windows(
                built, streams, sources, streams_by, external, read_from, split, done,
                window))
        return planned

    async def _external_rows(
        self,
        read_from: datetime,
        read_end: datetime,
        window: tuple[datetime, datetime] | None = None,
    ) -> tuple[datetime, datetime, list[dict]] | None:
        """The external_control rows: the flag per quarter from the chosen binary
        sensor's recorded states, over the span they cover - from the hour of the first
        recorded state (never before `read_from`) to the hour after the last quarter that
        ended by `read_end`. Every quarter of the window gets a row carrying only
        external_control, so no stored row is left unstaged (the server would remove it);
        a quarter the server holds no row for is skipped there. None when there is
        nothing to replace (no sensor, no recorded state). A failed read raises."""
        entity = self._external_entity
        if not entity:
            return None
        points = await stats.async_fetch_states(self.hass, entity, read_from, read_end)
        if not points:
            return None
        flagged = stats.flagged_quarters(points, read_end, start=read_from)
        last_end = _floor(read_end, 900)  # quarters ending by read_end
        if window is None:
            start = max(read_from, _floor_hour(min(ts for ts, _state in points)))
            if last_end <= start:
                return None
            window = (start, _ceil_hour(last_end))
        start, end = window
        rows = []
        q = start
        while q < min(end, last_end):
            rows.append({"ts": q.isoformat(), "external_control": q in flagged})
            q += _QUARTER
        if not rows:
            return None
        return start, end, rows

    async def _entities_sources(
        self, read_from: datetime, split: datetime, end: datetime
    ) -> list[_Source]:
        """The statistics reads of the energy phase, split like _backfill_rows: hourly
        before `split`, 5-minute after it, each one period early."""
        sources: list[_Source] = []
        hourly_end = min(split, end)
        if hourly_end > read_from:
            raw = await async_fetch_change(
                self.hass, self._statistic_ids(), _read_start(read_from, 3600), hourly_end,
                period="hour")
            sources.append(_Source(raw, split_hour_to_quarters, read_from, hourly_end))
        five_start = max(read_from, split)
        if end > five_start:
            raw = await async_fetch_change(
                self.hass, self._statistic_ids(), _read_start(five_start, 300), end,
                period="5minute")
            sources.append(_Source(raw, aggregate_5min_to_15min, five_start, end))
        return sources

    async def _entities_committed(self, job: dict, rep: dict, result: dict) -> None:
        """After a committed window.

        The energy phase moves the bookmark to the latest quarter of the window staged
        with every field when that is later than the bookmark (the server inserted those
        rows), or when the bookmark lay inside the window (rows after that quarter may
        have been removed). After the last window, a bookmark past it steps back too: the
        quarters after the window still hold the old sensors' values, and the regular
        upload sends them again once they are complete. A step back stops at the
        5-minute boundary of the plan (_ENTITY_SHORT_TERM_DAYS). Nothing moves on an
        empty result: the server replaced nothing. The external phase is done with its one window. The
        applied fingerprint follows once every phase is done (_entities_finished)."""
        phase = rep.get("phase", "energy")
        if phase == "external":
            done = job.setdefault("phases_done", [])
            if phase not in done:
                done.append(phase)
        if isinstance(result, dict) and result:
            _LOGGER.info(
                "Wolta sensor change replaced %s in %s..%s: %s row(s) updated, %s inserted,"
                " %s removed, %s partial row(s) skipped",
                ", ".join(rep.get("fields") or []), rep["window_start"], rep["window_end"],
                result.get("updated_rows"), result.get("inserted_rows"),
                result.get("removed_rows"), result.get("skipped_partial"),
            )
        if phase != "energy" or not result:
            return
        bookmark = self._state.get("last_uploaded_ts")
        if not bookmark:
            return
        mark = datetime.fromisoformat(bookmark)
        start = datetime.fromisoformat(rep["window_start"])
        end = datetime.fromisoformat(rep["window_end"])
        full = datetime.fromisoformat(rep["last_full"]) if rep.get("last_full") else None
        new = None
        if full is not None and (full > mark or start <= mark < end):
            new = full
        elif rep.get("final") and mark >= end:
            new = full if full is not None else end - _QUARTER
        if new is not None and new < mark:
            # A step back never passes the 5-minute boundary (with the plan's margin):
            # from further back the regular upload would heal from hourly statistics and
            # overwrite the recent quarters with hour/4 values. What lies before it is
            # older than the 5-minute statistics anyway, and the window covered it. A
            # bookmark already behind the boundary heals from hourly statistics anyway.
            floor = _floor(dt_util.utcnow() - timedelta(days=_ENTITY_SHORT_TERM_DAYS), 3600)
            if mark > floor:
                new = max(new, floor)
        if new is not None and new != mark:
            self._state["last_uploaded_ts"] = new.isoformat()

    async def _entities_finished(self, job: dict) -> None:
        self._entity_rows_cache = {}
        self._state["applied_entities"] = job["target"]

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    def _apply_profile_sync(self, profile: dict) -> bool:
        """Mirror server-side profile fields into entry.data (write only on change).

        entry.data is a CACHE of the shared profile – the server is the source of
        truth. No update listener is registered, so async_update_entry does not
        trigger a reload; we only write when something actually changed to avoid
        a disk write per poll tick. Returns True when something changed.
        """
        entry = self.config_entry
        new_data = dict(entry.data)
        changed = False
        for key in _PROFILE_SYNC_KEYS:
            server_val = profile.get(key)
            if server_val is None:
                if key in new_data:
                    new_data.pop(key)
                    changed = True
            elif new_data.get(key) != server_val:
                new_data[key] = server_val
                changed = True
        if not changed:
            return False
        zone = new_data.get(CONF_ZONE, self._zone)
        self._zone = zone
        self.hass.config_entries.async_update_entry(
            entry, data=new_data, title=f"Wolta ({zone})"
        )
        return True

    def _take_battery_state(self, profile: dict) -> None:
        """Read the server's battery stamp off a profile GET and (re)evaluate the repair.

        Called from BOTH polls - the 6h main cycle and the 5 min side-poll - because the
        stamp flips to needs_input on the server's own schedule, not on ours: waiting for
        the main cycle would leave the user unasked for up to six hours while the plant
        has no grade at all. Both fields are stored raw; the pending→needs_input rule
        lives in the backend (spec 2026-09-14 §4) and is never re-derived here."""
        # Atomärt: läs alla tre fälten INNAN något tilldelas, så en missbildad rad (anroparna
        # fångar och ignorerar) inte lämnar en halv stämpel (status satt, dygn ej) bakom sig.
        status = profile.get(KEY_BATTERY_STATUS)
        detect = profile.get("battery_detect")
        min_days = int(profile.get("detect_min_days") or _DETECT_MIN_DAYS_DEFAULT)
        self._battery_status = status
        self._battery_detect = detect
        self._detect_min_days = min_days
        self._evaluate_battery_pending()

    async def async_check_profile_sync(self, _now: datetime | None = None) -> None:
        """Side-poll (every 5 min): pick up web-side profile edits within minutes.

        The main cycle only runs every 6h at rest, so without this a change made on
        wolta.se would not show in HA for hours. On a detected change we mirror it
        and run a full refresh – the web already queued the recompute, so the
        pending-detection switches to fast-poll until the fresh grade lands.
        Never raises (timer callback); auth errors are left to the main cycle,
        which owns the reauth path.
        """
        if self.update_interval == _FAST_POLL:
            return  # main cycle is already refreshing every minute
        try:
            profile = await self.client.get_profile(self.token)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Profile side-poll failed; retrying next tick", exc_info=True)
            return
        # A CHANGED battery stamp also warrants the refresh: the stamp is not part of
        # entry.data, so _apply_profile_sync can't see it, and without a rebuilt WoltaData
        # the status sensor would keep saying "measuring" for up to 6h while the repair
        # already asks for the nameplate. Only on a change - refreshing on every tick
        # would run the whole upload cycle every 5 minutes.
        # Stämpelläsningen ligger INNE i try:t: _take_battery_state indexerar
        # battery_detect som dict och int():ar detect_min_days, så en missbildad rad
        # (icke-dict, osiffrigt) kastar. Utanför try:t hade det brutit docstringens
        # "Never raises" var 5:e minut i all oändlighet – servern skickar samma rad
        # nästa tick, så felet hade aldrig läkt av sig självt.
        try:
            previous_status = self._battery_status
            self._take_battery_state(profile)
            battery_changed = self._battery_status != previous_status
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Malformed battery stamp in side-poll; ignoring", exc_info=True)
            battery_changed = False
        if self._apply_profile_sync(profile) or battery_changed:
            _LOGGER.debug("Web-side profile change detected; refreshing results")
            await self.async_request_refresh()

    async def _async_update_data(self) -> WoltaData:
        """Fetch statistics, upload to Wolta, trigger recompute, return results."""
        try:
            now = dt_util.utcnow()
            # Profile sync (server = source of truth): mirror web-side changes into
            # entry.data. Network errors are non-fatal – the cache just stays stale.
            # Skipped during fast-poll (60 s while a server job runs) – nothing
            # profile-related changes on that timescale and it saves a GET per tick.
            profile_payload: dict | None = None
            if self.update_interval != _FAST_POLL:
                try:
                    profile = await self.client.get_profile(self.token)
                except WoltaAuthError:
                    raise  # purged profile → same reauth path as results()
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.debug("Profile sync fetch failed; keeping cache", exc_info=True)
                else:
                    profile_payload = profile
                    self._apply_profile_sync(profile)
                    # Samma skydd som sidopollen: stämpeln är ett VISNINGSFÄLT, och en
                    # missbildad rad (icke-dict battery_detect, osiffrig detect_min_days)
                    # får inte fälla hela uppladdningscykeln.
                    try:
                        self._take_battery_state(profile)
                    except Exception:  # pylint: disable=broad-except
                        _LOGGER.debug("Malformed battery stamp in main cycle; ignoring", exc_info=True)

            # Visningsläge: bara resultat-pollen. Hela strömningsmaskineriet (invert-/
            # entitets-självläkning, bookmark, statistikläsning, PUT /data, recompute-
            # kadens, measured-params-repairs) förutsätter att VI äger datan - här ägs
            # den av anläggningens bindning (Sonnen-webhook/Reduxi), och servern 409:ar
            # dessutom varje skrivförsök.
            if self._view_only:
                results = await self.client.results(self.token)
                return self._build_data(results, last_uploaded=None)

            # A changed battery direction or sensor selection is no longer re-sent: the
            # bookmark stays and the server's stored rows are replaced in place - a
            # direction job swaps the battery fields, an entity job replaces only the
            # changed sensors' fields (both before the replacement runner below; one job
            # at a time, the direction first). The server queues the recount at commit.
            entities_now = self._entities_fingerprint()

            await self._track_soc_selection()
            await self._queue_direction_job(now)
            await self._queue_entity_job(entities_now, now)

            # A replacement of stored flows runs before the regular upload (server spec
            # §9): while one is open on the server, neither the regular PUT /data nor the
            # coverage fill writes, since either would make its commit a 409 conflict.
            paused = await self._run_replacement_job(now, profile_payload)
            put_payload: dict | None = None
            backfill_read = False
            if not paused:
                put_payload, backfill_read = await self._upload_regular(now)

            # Coverage reconciliation (spec 2026-10-10 täckningsavstämning): after the
            # regular upload, so the server state includes our own writes and its spike
            # rule; before _maybe_recompute, so a fill's forced recompute runs this cycle.
            # Not during fast-poll - the profile GET is skipped then for the same reason.
            if not paused and self.update_interval != _FAST_POLL:
                try:
                    await self._reconcile_coverage(
                        now,
                        put_payload if put_payload is not None else profile_payload,
                        full_backfill=backfill_read,
                    )
                except WoltaAuthError:
                    raise  # purged profile → the reauth path below
                except Exception as err:  # pylint: disable=broad-except
                    # The regular upload already went through; a failing fill (statistics
                    # read, SoC read, ...) must not fail the cycle. Retried next cycle.
                    _LOGGER.warning(
                        "Wolta coverage reconciliation failed (%s); retrying next cycle",
                        type(err).__name__,
                    )
                    _LOGGER.debug("Coverage reconciliation failure detail", exc_info=True)

            # Before _maybe_recompute: with a fresh compensation figure already on
            # the profile when the recompute is triggered, it does not have to wait
            # a whole cycle to show up.
            #
            # Skipped during fast-poll for the same reason as the profile GET above,
            # only more so: this is a WRITE plus a recorder month query, and the
            # figure behind it is settled by an aggregator days after the month ends
            # - there is nothing here that moves on a 60-second timescale. put_data
            # is gated by `if rows:`, so ungated this would be the one call hitting
            # the server every minute for as long as a server job runs.
            if self.update_interval != _FAST_POLL:
                await self._push_flex_compensation()

            await self._maybe_recompute(now)

            results = await self.client.results(self.token)
            self._evaluate_measured_params(results)
            self._evaluate_soc_missing_issue(results)
            last_uploaded_str = self._state.get("last_uploaded_ts")
            last_uploaded = (
                datetime.fromisoformat(last_uploaded_str)
                if last_uploaded_str
                else None
            )
            return self._build_data(results, last_uploaded=last_uploaded)

        except WoltaAuthError as err:
            raise ConfigEntryAuthFailed from err

        except WoltaRateLimitError as err:
            # retry_after MUST be a kwarg (not just in the message) – HA 2025.12+ uses
            # it to schedule the next update instead of the fixed 6h interval.
            raise UpdateFailed(
                f"Wolta rate limited; retry after {err.retry_after}s",
                retry_after=err.retry_after,
            ) from err

        except (aiohttp.ClientError, TimeoutError) as err:
            # Visningsläge räknar inte mot upload-failure-repairen: dess text lovar
            # "unable to upload energy data" och en view-only-entry laddar aldrig upp.
            # Felet syns ändå - UpdateFailed gör entiteterna unavailable.
            if not self._view_only:
                self._track_failure(err)
            raise UpdateFailed(f"wolta.se unreachable: {err}") from err

    async def _upload_regular(self, now: datetime) -> tuple[dict | None, bool]:
        """The regular upload: read the flows since the bookmark (or the full backfill
        without one), PUT them with the SoC rows, advance the bookmarks. Returns the PUT
        response (None when nothing was delivered) and whether a full backfill read
        the whole window."""
        bookmark = self._state.get("last_uploaded_ts")  # ISO str | None
        full_backfill = bookmark is None

        backfill_start = now - timedelta(days=_BACKFILL_DAYS)
        # Deliberately NOT floored like _short_term_start(): SoC has no completeness
        # logic, and its hourly/5-minute switch may differ from the flows' by up to 59 min.
        short_term_start = now - timedelta(days=_SHORT_TERM_DAYS)
        if bookmark is None:
            rows = await self._backfill_rows(now)
            flow_start = backfill_start
        else:
            flow_start = datetime.fromisoformat(bookmark)
            if now - flow_start > timedelta(days=_SHORT_TERM_DAYS):
                rows = await self._heal_rows(flow_start, now)
            else:
                rows = await self._incremental_rows(flow_start, now)

        # The SoC read has its OWN bookmark (soc_uploaded_ts), never the flows'. The
        # flow bookmark moves whenever the flows were delivered - also when the SoC
        # read failed or a 422 dropped SoC from the call - and a read from there would
        # lose the quarters in between for good, although HA keeps 5-minute statistics
        # for ~10 days. A pending SoC backfill (sensor added, or first start with
        # sensors chosen) widens the read to the whole backfill window; the flow rows
        # stay those of the branch above, the flow bookmark is never steered by SoC.
        soc_backfill = bool(self._state.get("soc_backfill_pending"))
        soc_start = flow_start  # first time with sensors and no backfill pending
        soc_bookmark = self._soc_bookmark()
        if soc_backfill:
            soc_start = backfill_start
        elif soc_bookmark is not None:
            soc_start = soc_bookmark
        soc_start = max(soc_start, backfill_start)
        soc_hourly_until = max(soc_start, short_term_start)

        put_payload: dict | None = None
        if rows:
            # Read here, not per branch: SoC only rides along with flow rows (the API
            # requires at least one row), so a cycle without rows reads nothing.
            # Hours before the short-term limit, quarters after it, whatever the flow
            # branch is.
            soc, soc_ok = await self._soc_state(
                soc_start, now, hourly_until=soc_hourly_until)
            try:
                soc_delivered, put_response = await self._put_rows(rows, soc)
            except WoltaApiError as err:
                if err.status == 413:
                    # Profile hit MAX_PROFILE_ROWS (80k ≈ 2.3 yr of 15-min data,
                    # reachable for linked profiles with web CSV + HA backfill).
                    # Surface via repairs and skip the upload – results still work.
                    # Bookmark is NOT advanced (nothing was persisted server-side).
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        _ISSUE_PROFILE_FULL,
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key=_ISSUE_PROFILE_FULL,
                    )
                else:
                    raise
            else:
                ir.async_delete_issue(self.hass, DOMAIN, _ISSUE_PROFILE_FULL)
                put_payload = put_response
                self._state["last_uploaded_ts"] = rows[-1]["ts"]
                # The SoC backfill is done only when the read raised nothing AND the
                # PUT carrying it went through; otherwise the next cycle retries it.
                if soc_backfill and soc_ok and soc_delivered:
                    self._state.pop("soc_backfill_pending", None)
                # The SoC bookmark moves on the same condition, and only with sensors
                # chosen: without them no key is ever written (fleet default).
                if self._soc_entities and soc_ok and soc_delivered:
                    self._state["soc_uploaded_ts"] = self._next_soc_bookmark(
                        soc, self._state["last_uploaded_ts"])
                await self._store.async_save(self._state)
                # Clear failure counter on success
                self._state.pop("consecutive_failure_days", None)
                ir.async_delete_issue(self.hass, DOMAIN, _ISSUE_UPLOAD_FAILURE)

        # The full backfill read the whole window when it delivered or found nothing to
        # send; only a failed (413) delivery leaves it unread.
        return put_payload, full_backfill and (put_payload is not None or not rows)

    def _build_data(self, results: dict, *, last_uploaded: datetime | None) -> WoltaData:
        """WoltaData + polltakt ur ett /results-svar. EN sanning för pending-tolkningen,
        delad av strömningsvägen och view-only-grenen.

        `pending` = a server-side computation is in progress. Top-level `status` is NOT
        ENOUGH ALONE: the backend forces status="done" as soon as a grade is cached
        (profile.py), even while the economy recompute (decision/history) is still
        running. In that case `job` is the only reliable in-flight signal. Without this,
        the economy sensors go blank (they only keep their previous value when
        pending=True) and polling falls back to 6h → a finished decision doesn't show up
        until the next slow poll (up to 6h). Poll fast while pending so the grade shows
        up within ~one minute; back to the slow rate once it's done."""
        job = results.get("job") or {}
        # Batteristämpeln hoistas in i results av samma skäl som applied_tariff ligger där:
        # sensorernas value_fn ser BARA results, aldrig dataobjektet (attr_fn ser båda).
        results = {**results, KEY_BATTERY_STATUS: self._battery_status}
        data = WoltaData(
            results=results,
            last_uploaded=last_uploaded,
            n_days=results["period"]["n_days"],
            pending=(
                results.get("status") in ("pending", "running")
                or job.get("status") in ("pending", "running")
            ),
            battery_status=self._battery_status,
            battery_detect=self._battery_detect,
            detect_min_days=self._detect_min_days,
        )
        self.update_interval = _FAST_POLL if data.pending else _SLOW_POLL
        return data

    # ------------------------------------------------------------------
    # Row-fetching helpers
    # ------------------------------------------------------------------

    def _stream_ids(self) -> dict[str, list[str]]:
        """The selected statistic ids per stream, keyed by merge_streams' parameter
        names, as configured: a snapshot taken on the event loop, so the build in the
        executor reads no config entry. The battery direction is NOT applied here - it
        can differ per quarter, so _build_rows swaps the built values instead."""
        return {name: list(ids) for name, ids in self._entity_map.items()}

    async def _build_rows(
        self, now: datetime, sources: list[_Source], external: set[datetime]
    ) -> tuple[_Built, list[dict]]:
        """Anchor the solar sensors (statistics reads, on the loop), then build the
        streams, judge completeness and merge the upload rows in the executor.

        The build is pure but CPU-heavy: a backfill judges a year of hourly and nine
        days of 5-minute periods for every sensor, plus the sun's position per day. Off
        the event loop it cannot stall Home Assistant. What reads Home Assistant - the
        stream ids, the applied invert intervals and the home position for the sun - is
        taken here, on the loop, and handed in.

        The battery direction: the streams are built as the sensors say, then charge and
        discharge swap for every quarter starting inside an APPLIED invert interval - the
        intervals the server's stored rows are built with, so a new row always matches
        its stored neighbours. Completeness is untouched by the swap: both battery
        streams' sensors are judged for every quarter either way. ``built`` keeps the
        unswapped streams; only the rows carry the swap."""
        return await self._build(
            now, sources,
            lambda built, streams: merge_streams(
                **streams, external=external, complete=built.complete))

    async def _build(self, now: datetime, sources: list[_Source], finish: Callable):
        """_build_rows' build with the last step given: `finish(built, swapped streams)`
        runs in the executor after the streams are built and the APPLIED invert intervals
        swapped in, and its result is returned beside `built`."""
        sources = await self._anchor_solar(now, sources)
        ids = self._stream_ids()
        applied = self._applied_intervals()
        sun_down = ({_PERIOD_S[src.agg_fn]: self._sun_down_fn(_PERIOD_S[src.agg_fn])
                     for src in sources} if ids["solar"] else {})

        def job():
            built = self._build_streams(*sources, ids=ids, sun_down=sun_down)
            streams = intervals.apply_invert(built.streams, applied)
            return built, finish(built, streams)

        return await self.hass.async_add_executor_job(job)

    def _build_streams(
        self,
        *sources: _Source,
        ids: dict[str, list[str]] | None = None,
        sun_down: dict[int, Callable[[int], bool]] | None = None,
    ) -> _Built:
        """Build the five per-quarter streams and decide which quarters are complete.

        Values: each source contributes only the quarters inside its own window (the
        windows do not overlap) - backfill passes hourly first and 5-minute second.
        The streams are keyed by merge_streams' parameter names so the callers hand
        them over with ** while _empty_streams inspects the same dicts - one
        build, no chance of judging different data than what is uploaded.

        Completeness: a quarter is complete when EVERY selected sensor measured it
        cleanly (stats.clean_quarters) in the source whose window holds the quarter.
        A stream missing the quarter would otherwise be filled with 0.0, a sum over
        several sensors would be partial, and the first period after a gap carries the
        whole gap. Windows of the sources never overlap (callers split at
        short_term_start), so each quarter is judged by exactly one source.

        Known zeros (spec 6.2): a counter that stood still across a gap counts for every
        sensor; for the solar sensors a missing period while the sun is down counts too
        (_sun_down_fn), and such periods are not lag either (_discount_night). The
        values are untouched - merge_streams emits every complete quarter and fills an
        absent value with 0.0, also when every battery sensor is a known zero.

        ``ids`` (_stream_ids) and ``sun_down`` (per statistics period, _sun_down_fn)
        are computed here when not given; _build_rows hands them in so that this runs in
        the executor without touching Home Assistant."""
        if ids is None:
            ids = self._stream_ids()
        streams: dict[str, dict] = {}
        for name, stream_ids in ids.items():
            merged: dict = {}
            for src in sources:
                # Only the source's own window: the early-read period before it makes a
                # partial quarter that must not override the previous source's value.
                summed = sum_quarter_dicts(
                    [src.agg_fn(src.raw.get(e) or []) for e in stream_ids])
                merged.update({q: v for q, v in summed.items() if src.start <= q < src.end})
            streams[name] = merged

        sensors = {e for stream_ids in ids.values() for e in stream_ids}
        solar = set(ids["solar"])
        periods = {_PERIOD_S[src.agg_fn] for src in sources}
        if sun_down is None:
            sun_down = {p: self._sun_down_fn(p) for p in periods} if solar else {}
        complete: set[datetime] = set()
        by_stream: dict[str, set[datetime] | None] = {
            name: (set() if stream_ids else None) for name, stream_ids in ids.items()}
        blame = {e: 0 for e in sensors}
        # Newest measured period per sensor over the whole read, with its source's period.
        newest: dict[str, tuple[int, int]] = {}
        candidates: set[datetime] = set()
        for src in sources:
            period = _PERIOD_S[src.agg_fn]
            in_window = lambda q, s=src: s.start <= q < s.end  # noqa: E731
            window = (int(src.start.timestamp()), int(src.end.timestamp()))
            clean = {}
            for e in sensors:
                rows = src.raw.get(e) or []
                starts = measured_starts(rows, period)
                if starts and (e not in newest or max(starts) >= newest[e][0]):
                    newest[e] = (max(starts), period)
                clean[e] = {q for q in clean_quarters(
                    rows, period, window=window,
                    sun_down=sun_down[period] if e in solar else None,
                    anchor_start=src.anchors.get(e)) if in_window(q)}
                _log_known_zero_gaps(e, rows, period, window, src.anchors.get(e))
            src_candidates = {q for d in streams.values() for q in d if in_window(q)}
            candidates |= src_candidates
            if sensors:
                src_complete = set.intersection(*clean.values())
            else:
                src_complete = set()
            complete |= src_complete
            for name, stream_ids in ids.items():
                if stream_ids:
                    by_stream[name] |= set.intersection(*(clean[e] for e in stream_ids))
            for e in sensors:
                blame[e] += len(src_candidates - clean[e])
        asleep: set[str] = set()
        if solar and sources:
            step = min(periods)
            first = min(int(_read_start(src.start, _PERIOD_S[src.agg_fn]).timestamp())
                        for src in sources)
            asleep = _discount_night(newest, solar, first, step, sun_down[step])
        silent = _silent_sensors(sensors, newest, _ENERGY_EMPTY_MIN_WINDOW)
        incomplete = len(candidates - complete)
        return _Built(streams, complete, silent, incomplete, blame, asleep - silent,
                      by_stream)

    def _sun_down_fn(self, period_s: int) -> Callable[[int], bool]:
        """``sun_down(period_start)`` for statistics periods of ``period_s`` at Home
        Assistant's position (helpers.sun.get_astral_observer): true when the sun is
        below the horizon in the middle of the period. Daylight is cached per UTC day
        on the coordinator, so later cycles reuse the backfill's days."""
        observer = get_astral_observer(self.hass)
        key = (observer.latitude, observer.longitude, observer.elevation)
        if key != self._daylight_key:
            self._daylight_key, self._daylight_cache = key, {}
        return _make_sun_down(observer, period_s, self._daylight_cache)

    async def _anchor_solar(self, now: datetime, sources: list[_Source]) -> list[_Source]:
        """The sources with ``anchors`` for the solar sensors whose night run reaches
        back before the rows.

        The first measured period after a night run is clean only when every period
        back to the sensor's previous measured one is a known zero (clean_quarters).
        Every read starts one period before its window, so when a window starts at night
        the previous measured period - the evening before - is not in the rows. For each
        solar sensor whose rows start later than the read with nothing but night in
        between, the anchor is taken from an earlier source of the same build, or else
        from one extra statistics read for those sensors only, at most
        _ANCHOR_LOOKBACK before the window (5-minute statistics within the short-term
        window, hourly beyond it - an hour's start is the earlier, stricter anchor).
        Nothing found: no anchor, and the dawn after the run is not clean."""
        solar = sorted(set(self._entity_map["solar"]) & self._statistic_ids())
        if not solar:
            return sources
        out: list[_Source] = []
        for src in sources:
            period = _PERIOD_S[src.agg_fn]
            first = int(_read_start(src.start, period).timestamp())
            sun_down = self._sun_down_fn(period)
            need = []
            for e in solar:
                m = measured_starts(src.raw.get(e) or [], period)
                if m and min(m) > first and all(
                        sun_down(p) for p in range(first, min(m), period)):
                    need.append(e)
            anchors: dict[str, int] = {}
            for e in need:
                earlier = [s for prev in out
                           for s in measured_starts(prev.raw.get(e) or [],
                                                    _PERIOD_S[prev.agg_fn])
                           if s < first]
                if earlier:
                    anchors[e] = max(earlier)
            missing = {e for e in need if e not in anchors}
            if missing:
                lookback = src.start - _ANCHOR_LOOKBACK
                five = period == 300 and lookback >= _short_term_start(now)
                read_period = 300 if five else 3600
                raw = await async_fetch_change(
                    self.hass, missing, _floor(lookback, read_period),
                    datetime.fromtimestamp(first, tz=timezone.utc),
                    period="5minute" if five else "hour")
                for e in missing:
                    found = [s for s in measured_starts(raw.get(e) or [], read_period)
                             if s < first]
                    if found:
                        anchors[e] = max(found)
            out.append(replace(src, anchors=anchors) if anchors else src)
        return out

    def _remember_completeness(self, built: _Built) -> None:
        """Keep the regular read's completeness for the diagnostics: the number of
        quarters dropped as incomplete and the (at most three) sensors that made the
        most of them incomplete."""
        ranked = sorted(built.blame.items(), key=lambda kv: (-kv[1], kv[0]))
        self._last_completeness = {
            "incomplete_quarters": built.incomplete,
            "entities": [e for e, n in ranked if n > 0][:3],
        }

    def _empty_streams(self, built: _Built) -> list[str]:
        """Configured streams that count as empty: no quarter at all, or every selected
        sensor of the stream silent (_silent_sensors). The second leg catches a stream
        whose only sensor died after it had worked - the bookmark quarter keeps such a
        stream non-empty in every read. A stream without configured sensors is never
        empty: solar-less plants are a normal configuration. Nor is a stream whose
        sensors are all asleep (_asleep_streams).

        Named by role as it stands now (_effective_stream): ``built`` holds the
        unswapped streams, so a role's data is the stream of the sensors feeding it,
        and the repair texts pair each role with exactly those sensors."""
        out = []
        for name in built.streams:
            source = self._effective_stream(name)
            entities = set(self._entity_map[source])
            data = built.streams[source]
            if not entities or entities <= built.asleep:
                continue
            if not data or entities <= built.silent:
                out.append(name)
        return out

    def _asleep_streams(self, built: _Built) -> list[str]:
        """Configured streams without a value in the read whose sensors are all solar
        sensors that trail only by night (_discount_night) - an inverter that sleeps
        from sunset, or through a polar night. Such a read says nothing about the
        sensors: it neither counts toward energy_empty nor clears it."""
        out = []
        for name in built.streams:
            source = self._effective_stream(name)  # by role, as in _empty_streams
            entities = set(self._entity_map[source])
            if entities and not built.streams[source] and entities <= built.asleep:
                out.append(name)
        return out

    async def _note_silent_sensors(self, built: _Built, window: timedelta) -> None:
        """Track selected sensors that are silent (_silent_sensors: no measured period,
        or trailing the others) while another selected sensor is not, and raise
        statistics_missing after _ENERGY_EMPTY_CYCLES_BEFORE_ISSUE cycles in a row.
        Complete quarters need every selected sensor, so one silent sensor stops the
        upload - the owner must learn why.

        A sensor whose whole stream counts as empty is energy_empty's case (it explains
        the state_class pitfall) and is left out here. Windows below
        _ENERGY_EMPTY_MIN_WINDOW, and windows in which every selected sensor is silent,
        are neither counted nor cleared. Nor is a solar sensor asleep in the read
        (_discount_night: it trails only by night) - its streak is held, so a dead solar
        sensor is still caught in winter, when no three windows in a row see daylight."""
        if window < _ENERGY_EMPTY_MIN_WINDOW:
            return
        empty_streams = {
            e for name in self._empty_streams(built)
            for e in self._entity_map[self._effective_stream(name)]
        }
        if len(built.silent) >= len(self._statistic_ids()):
            # Everything is silent (Home Assistant down, or the recorder idle): that says
            # nothing about any single sensor, so neither count nor clear.
            return
        silent = built.silent - empty_streams
        prev: dict = self._state.get("silent_sensor_cycles") or {}
        held = {e: int(n) for e, n in prev.items() if e in built.asleep}
        if not silent and not held:
            if "silent_sensor_cycles" in self._state:
                self._state.pop("silent_sensor_cycles")
                await self._store.async_save(self._state)
                ir.async_delete_issue(self.hass, DOMAIN, self._stats_issue_id)
            return
        cycles = {**held, **{e: int(prev.get(e, 0)) + 1 for e in silent}}
        self._state["silent_sensor_cycles"] = cycles
        await self._store.async_save(self._state)
        due = sorted(e for e, n in cycles.items() if n >= _ENERGY_EMPTY_CYCLES_BEFORE_ISSUE)
        if not due:
            # A raised warning may name a sensor that reads again while another has only
            # just gone silent - it must not stand until the new streak is due.
            ir.async_delete_issue(self.hass, DOMAIN, self._stats_issue_id)
            return
        ir.async_create_issue(
            self.hass, DOMAIN, self._stats_issue_id, is_fixable=False,
            severity=ir.IssueSeverity.WARNING, translation_key=_ISSUE_STATISTICS_MISSING,
            translation_placeholders={"entities": ", ".join(due)})

    async def _note_stream_emptiness(self, built: _Built, window: timedelta) -> None:
        """Track configured streams that count as EMPTY (_empty_streams); raise the
        repair once the streak holds (mirrors _note_flex_bad_cycle). Since stats.py
        stopped fabricating 0.0 buckets, a sensor with ``state_class: measurement``
        yields an empty stream - and an empty stream uploads nothing, which looks
        exactly like the integration working. This is the visibility half of that fix.
        A stream whose sensors all died after working counts too: complete quarters
        stop the upload just the same.

        Windows below _ENERGY_EMPTY_MIN_WINDOW are not judged at all: neither
        counted (fast-poll windows legitimately hold no compiled rows and would
        trip the repair on healthy setups) nor cleared (a broken setup would get a
        false all-clear from a window too short to prove anything).

        A stream with no configured entities is expected to be empty and never
        counts - solar-less plants are a normal configuration, not a fault.
        """
        if window < _ENERGY_EMPTY_MIN_WINDOW:
            return

        empty = self._empty_streams(built)
        if not empty:
            if not self._asleep_streams(built):
                await self._clear_energy_empty_streak()
            return

        streak = int(self._state.get("energy_empty_cycles", 0)) + 1
        self._state["energy_empty_cycles"] = streak
        await self._store.async_save(self._state)
        if streak < _ENERGY_EMPTY_CYCLES_BEFORE_ISSUE:
            return
        described = "; ".join(
            f"{name}: {', '.join(self._entity_map[self._effective_stream(name)])}"
            for name in empty
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._energy_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=_ISSUE_ENERGY_NO_STATS,
            translation_placeholders={"streams": described},
        )

    async def _clear_energy_empty_streak(self) -> None:
        """Drop the streak and the repair. No-op unless a streak was recorded, so
        healthy plants pay nothing per cycle (same contract as the flex twin)."""
        if self._state.pop("energy_empty_cycles", None) is None:
            return
        await self._store.async_save(self._state)
        ir.async_delete_issue(self.hass, DOMAIN, self._energy_issue_id)

    def _effective_stream(self, stream: str) -> str:
        """The stream whose sensors feed ``stream``'s role NOW (issue #1): inside an
        applied invert interval batt_in reads the discharge sensor and batt_out the
        charge sensor. Only the battery currents are affected (grid/solar untouched).

        For texts and the silence diagnostics only - the rows swap per quarter
        (_build_rows), and history may be inverted in some intervals but not in others.
        Answers from the APPLIED intervals, like the rows: a desired value that has not
        reached the server's data yet describes nothing that was sent."""
        if not intervals.contains(self._applied_intervals(), dt_util.utcnow()):
            return stream
        if stream == "batt_in":
            return "batt_out"
        if stream == "batt_out":
            return "batt_in"
        return stream

    async def _external_quarters(self, start: datetime, end: datetime) -> set[datetime]:
        """Flagged 15-min quarters from the selected binary sensor's state history.

        No sensor selected -> empty set (every row's external_control stays False,
        and the recorder's states table is never queried - a needless DB read on
        every cycle for the whole fleet would be a real regression).

        A failure here must NOT fail the upload cycle: an unflagged upload beats no
        upload at all. Be clear about what that costs, though - it is NOT
        self-healing. Nothing re-reads quarters below the bookmark; the incremental
        and heal paths both start there, so quarters uploaded unflagged stay
        unflagged until something rewrites them (re-picking the external-control
        sensor, whose entity job replaces the flag in the stored history). The
        error direction is still the safe one - those quarters are graded normally
        rather than wrongly neutralised - but the flag is lost for them, not deferred.

        Both the recorder read and the bucketing are inside the try so the guarantee
        above actually holds for the whole operation: flagged_quarters is pure, but
        leaving it outside would mean a malformed point list still killed the cycle,
        which is exactly what this method promises not to do.
        """
        if not self._external_entity:
            return set()
        try:
            points = await stats.async_fetch_states(
                self.hass, self._external_entity, start, end)
            return stats.flagged_quarters(points, end, start=start)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning(
                "External-control history read failed; uploading unflagged",
                exc_info=True,
            )
            return set()

    async def _push_flex_compensation(self) -> None:
        """Send the compensation sensor's monthly totals on as flex_compensation.

        source is ALWAYS "sensor". The server upserts per (month, source), so the
        web card's "manual" records - the user's own bookkeeping - live beside ours
        and are never touched. By the same rule we only ever send months we actually
        read: a month we say nothing about keeps whatever the server has, and we
        never send amount_sek=null (deleting a month is the user's decision, made in
        the web card, not a side effect of a sensor going quiet).

        Re-read and re-sent EVERY cycle, deliberately. Aggregators settle a month
        days after it ends, so last month's figure changes under us; caching the
        first reading would freeze an estimate that the server then keeps forever
        (and "only changed months are mentioned" hides that: with a still sensor the
        two behaviours are indistinguishable).

        Never fails the cycle. Same soft-degradation contract as the external-control
        read, but unlike that one this IS self-healing: nothing is bookmarked, so the
        next cycle reads the same months again and re-sends them. Auth errors are
        swallowed here too and left to results(), which owns the reauth path - same
        division of labour as the profile side-poll.

        That self-healing is also the failure mode. Re-sending an identical payload is
        recovery when the server was momentarily unavailable, and an infinite loop when
        the server REFUSED the payload: the rejection then repeats forever, inside a
        warning nobody reads. So a run of refusals raises the same repair an empty read
        does (_note_flex_bad_cycle). The empty-read leg cannot cover that case - there
        the mapping is non-empty.

        Reading NOTHING is a different outcome from reading a zero and is reported as
        such: after _FLEX_EMPTY_CYCLES_BEFORE_ISSUE bad cycles a repair goes up, and it
        comes straight back down as soon as a cycle goes through or the picker is
        cleared. Silent no-op was the alternative, and it is indistinguishable from the
        feature working.
        """
        if not self._flex_entity:
            # Clearing the picker is a deliberate "stop doing this", not an
            # unresolved problem: the warning must not outlive the setting.
            await self._clear_flex_bad_streak()
            return
        try:
            amounts = await stats.monthly_amounts(self.hass, self._flex_entity)
        except Exception:  # pylint: disable=broad-except
            # A recorder read that blew up says nothing about the sensor - leave the
            # streak where it is and try again next cycle.
            _LOGGER.warning(
                "Flex compensation read failed; retrying next cycle", exc_info=True
            )
            return
        if not amounts:
            # Nothing to claim - and nothing read is now a DISTINCT state from
            # "the month was zero" (see monthly_amounts), so it can be reported.
            await self._note_flex_bad_cycle()
            return
        try:
            await self.client.patch_profile(
                self.token,
                flex_compensation=[
                    {"month": month, "amount_sek": amount, "source": "sensor"}
                    for month, amount in sorted(amounts.items())
                ],
            )
        except (WoltaAuthError, WoltaRateLimitError):
            # Neither says anything about the compensation payload: reauth belongs to
            # results() and a 429 clears itself. Warn and move on.
            _LOGGER.warning(
                "Flex compensation update failed; retrying next cycle", exc_info=True
            )
        except WoltaApiError as err:
            if err.status is not None and 400 <= err.status < 500:
                # The server looked at THIS payload and refused it (a 4xx other than
                # the 404/429 caught above). An identical payload next cycle earns an
                # identical refusal, forever - count it.
                _LOGGER.warning(
                    "Flex compensation update rejected; retrying next cycle",
                    exc_info=True,
                )
                await self._note_flex_bad_cycle()
            else:
                # A 5xx (or a response with no status at all) is the server's
                # trouble, not this payload's: the same bytes go through once it is
                # back, which is what re-sending is for. Counting it would blame the
                # user's sensor for Wolta's downtime.
                _LOGGER.warning(
                    "Flex compensation update failed; retrying next cycle",
                    exc_info=True,
                )
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Flex compensation update failed; retrying next cycle", exc_info=True
            )
        else:
            await self._clear_flex_bad_streak()

    async def _note_flex_bad_cycle(self) -> None:
        """Count a cycle that got nothing through; raise the repair once the streak
        holds. The two causes - nothing read, and a payload the server refused - share
        one counter and one issue: they are mutually exclusive within a cycle and land
        the user in the same place, no compensation reaching Wolta.

        Thresholded rather than immediate for the same reason _track_failure is: a
        sensor added seconds ago has no compiled statistics yet, and a repair that
        fires on every fresh setup teaches people to dismiss it.
        """
        streak = int(self._state.get("flex_empty_cycles", 0)) + 1
        self._state["flex_empty_cycles"] = streak
        await self._store.async_save(self._state)
        if streak < _FLEX_EMPTY_CYCLES_BEFORE_ISSUE:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._flex_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=_ISSUE_FLEX_NO_STATS,
            translation_placeholders={"entity_id": self._flex_entity or ""},
        )

    async def _clear_flex_bad_streak(self) -> None:
        """Drop the streak and the repair. No-op unless a streak was recorded, so
        the fleet default (no sensor picked) costs nothing per cycle."""
        if self._state.pop("flex_empty_cycles", None) is None:
            return
        await self._store.async_save(self._state)
        ir.async_delete_issue(self.hass, DOMAIN, self._flex_issue_id)

    async def _put_rows(
        self, rows: list[dict], soc: list[dict]
    ) -> tuple[bool, dict | None]:
        """PUT the flow rows, with SoC riding along when there is any. Returns
        (soc_delivered, response): whether the SoC (if any) was delivered, and the response
        of the PUT that went through (the coverage reconciliation reads `data_since` from
        it). Raises WoltaApiError like put_data otherwise.

        Without SoC the call stays exactly (token, rows): the fleet default is
        byte-identical to the pre-feature upload.

        Safety net (spec §6.5, §16): a 422 on a call that carried SoC re-sends the same
        flow rows without it. SoC must never stop the flows, not even if the server's
        rules are tightened after this client shipped. A flow-only 422 is not ours to
        second-guess and is raised as before. After that fallback the returned response
        is the flow-only PUT's - the one that actually stored something."""
        if not soc:
            return True, await self.client.put_data(self.token, rows)
        try:
            response = await self.client.put_data(
                self.token, rows,
                battery_state=soc, battery_sources=self._soc_sources(),
            )
        except WoltaApiError as err:
            if err.status != 422:
                raise
            _LOGGER.warning(
                "Wolta rejected the state-of-charge data (422); "
                "re-sending the energy data without it"
            )
            return False, await self.client.put_data(self.token, rows)
        return True, response

    async def _track_soc_selection(self) -> None:
        """Record the SoC choice and decide whether its history must be backfilled.

        This is the SoC counterpart of the applied_entities fingerprint, and it exists
        precisely so that fingerprint never sees SoC (spec §6.5, §16). It never touches
        last_uploaded_ts or pending_invert_recompute: the flows and the grade are not
        SoC's business. A sensor that is NEW to the choice - or any chosen sensor on the
        first start that records the choice - sets soc_backfill_pending; removing
        sensors alone just stops collecting them."""
        soc_now = sorted(self._soc_entities)
        applied = self._state.get("applied_soc")
        if applied == soc_now:
            return
        if applied is None:
            pending = bool(soc_now)
        else:
            pending = bool(set(soc_now) - set(applied))
        self._state["applied_soc"] = soc_now
        if pending:
            self._state["soc_backfill_pending"] = True
        await self._store.async_save(self._state)

    def _soc_bookmark(self) -> datetime | None:
        """The SoC read bookmark, or None when absent or unreadable (a corrupt value then
        just means 'start where the flows start', never an exception that stops the
        flows)."""
        raw = self._state.get("soc_uploaded_ts")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
        # A naive value would break the comparisons with the aware window bounds.
        return parsed if parsed.tzinfo else None

    def _next_soc_bookmark(self, soc: list[dict], flow_bookmark: str) -> str:
        """The new SoC bookmark after a delivered read.

        With rows: the LATEST row start over all sensors, minus one hour. HA compiles the
        statistics of all sensors in the same run, so sensors do not lag each other; the
        hour is the re-read margin for a late compile (idempotent upsert, so the overlap
        costs nothing). Deliberately NOT the earliest sensor's last row: a selected sensor
        that went silent would then pin the bookmark at its last row, which stays in the
        window for good, and every cycle would re-read and re-upload an ever-growing span
        for all sensors up to the 365-day floor. Without rows (the read worked, there was
        nothing): the new flow bookmark, so a sensor without statistics does not make the
        window grow either.

        The bookmark never moves backwards."""
        if soc:
            candidate = (max(datetime.fromisoformat(row["ts"]) for row in soc)
                         - timedelta(hours=1))
        else:
            candidate = datetime.fromisoformat(flow_bookmark)
        previous = self._soc_bookmark()
        return max(candidate, previous).isoformat() if previous else candidate.isoformat()

    async def _soc_state(
        self, start: datetime, now: datetime, *, hourly_until: datetime
    ) -> tuple[list[dict], bool]:
        """battery_state for [start, now): hourly statistics before hourly_until, quarters
        from 5-minute statistics after it. Returns (rows, ok); ok is False only when the
        read raised, so the caller can tell 'the read failed' (keep a pending SoC backfill
        for the next cycle) from 'there was nothing to read' (the backfill is done).

        A failure here must never stop the flows - they are the product (spec §6) - so
        it degrades to 'no SoC this cycle'. Logged on warning, with the sensor count and
        the error type only: entity ids stay out of the log."""
        if not self._soc_entities:
            return [], True
        ids = {s["unit"] for s in self._soc_sources()}
        if not ids:
            return [], True
        rows: list[dict] = []
        try:
            if hourly_until > start:
                # Hourly statistics rows START on the hour: a window that begins mid-hour
                # (a SoC bookmark at 10:30) would lose the 10:00 row, whose start lies
                # before it. The 5-minute read below needs no flooring.
                hour_start = start.astimezone(timezone.utc).replace(
                    minute=0, second=0, microsecond=0)
                hourly = await async_fetch_measurement(
                    self.hass, ids, hour_start, hourly_until, "hour")
                rows += battery_state_rows(
                    {e: soc_from_hourly(hourly.get(e, [])) for e in ids}, 3600)
            five_start = max(start, hourly_until)
            if now > five_start:
                five = await async_fetch_measurement(
                    self.hass, ids, five_start, now, "5minute")
                rows += battery_state_rows(
                    {e: soc_quarters_from_5min(five.get(e, [])) for e in ids}, 900)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning(
                "State-of-charge statistics read failed for %d sensor(s) (%s); "
                "uploading energy data without it",
                len(ids), type(err).__name__,
            )
            return [], False
        return rows, True

    def _soc_sources(self) -> list[dict]:
        """One battery_sources entry per SENDABLE SoC sensor. The platform comes from the
        entity registry ('unknown' for a sensor not in it); semantics stay 'unknown' - the
        sensor says nothing about whether it reports usable or total capacity.

        This list is also what _soc_state reads, so one filter decides for the read, the
        rows and the sources alike: a sensor whose id breaks the API's `unit` limit
        (stats.soc_unit_ok) or whose label breaks the `source` limit is left out of all
        three. The server 422s the whole call (flows included) on rows whose unit has no
        source in the call (unless it already holds one stored for that unit - we always
        send them) and on a `unit`/`source` over its length limit; a source without rows
        is a no-op, so a sensor that has no statistics yet costs nothing."""
        registry = er.async_get(self.hass)
        out = []
        for entity_id in sorted(self._soc_entities):
            reg_entry = registry.async_get(entity_id)
            platform = reg_entry.platform if reg_entry else "unknown"
            source = f"ha:{platform}:{entity_id}"
            if not soc_unit_ok(entity_id) or len(source) > _SOC_SOURCE_MAX:
                continue
            out.append({"unit": entity_id, "source": source, "semantics": "unknown"})
        return out

    async def _backfill_rows(self, now: datetime) -> list[dict]:
        """Backfill up to 12 months: LTS (÷4) for old data + 5-min for recent."""
        start = now - timedelta(days=_BACKFILL_DAYS)
        short_term_start = _short_term_start(now)

        external = await self._external_quarters(start, now)

        # Fetch hourly LTS for the long window (start → short_term_start), one period
        # early so the window's first hour can be judged against the one before it
        lts_data = await async_fetch_change(
            self.hass,
            self._statistic_ids(),
            _read_start(start, 3600),
            short_term_start,
            period="hour",
        )

        # Fetch 5-min short-term for the recent window, also one period early
        short_data = await async_fetch_change(
            self.hass,
            self._statistic_ids(),
            _read_start(short_term_start, 300),
            now,
            period="5minute",
        )

        # Aggregate and sum per stream; each source contributes only its own window
        built, rows = await self._build_rows(now, [
            _Source(lts_data, split_hour_to_quarters, start, short_term_start),
            _Source(short_data, aggregate_5min_to_15min, short_term_start, now),
        ], external)
        await self._note_stream_emptiness(built, window=now - start)
        self._remember_completeness(built)
        await self._note_silent_sensors(built, window=now - start)
        return rows

    async def _heal_rows(self, start: datetime, now: datetime) -> list[dict]:
        """Heal a gap from LTS (÷4) when the short-term window was missed."""
        external = await self._external_quarters(start, now)
        lts_data = await async_fetch_change(
            self.hass,
            self._statistic_ids(),
            _read_start(start, 3600),
            now,
            period="hour",
        )
        built, rows = await self._build_rows(
            now, [_Source(lts_data, split_hour_to_quarters, start, now)], external)
        await self._note_stream_emptiness(built, window=now - start)
        self._remember_completeness(built)
        await self._note_silent_sensors(built, window=now - start)
        return rows

    async def _incremental_rows(self, start: datetime, now: datetime) -> list[dict]:
        """Fetch short-term 5-min data since bookmark and aggregate to 15-min."""
        external = await self._external_quarters(start, now)
        short_data = await async_fetch_change(
            self.hass,
            self._statistic_ids(),
            _read_start(start, 300),
            now,
            period="5minute",
        )
        built, rows = await self._build_rows(
            now, [_Source(short_data, aggregate_5min_to_15min, start, now)], external)
        await self._note_stream_emptiness(built, window=now - start)
        self._remember_completeness(built)
        await self._note_silent_sensors(built, window=now - start)
        return rows

    async def _gap_rows(self, gap: coverage.Gap, now: datetime) -> list[dict]:
        """Flow rows for a coverage gap [gap.start, gap.end) – the history the server lacks
        before its earliest stored interval (spec 2026-10-10 täckningsavstämning §5.2).

        Same resolution rule as _backfill_rows: hourly LTS ÷ 4 for the part older than
        _SHORT_TERM_DAYS, 5-minute statistics aggregated to quarters after it. Rows are
        filtered to intervals that END by gap.end: a gap ending mid-hour would otherwise
        send the hour's later quarters, which the server already holds.

        Deliberately NO _note_stream_emptiness or _note_silent_sensors: an empty gap is
        the normal outcome on an upgrade (the server already has everything HA holds) and
        must neither count as a broken sensor nor clear a genuine streak. Nor
        _remember_completeness: the diagnostics describe the regular read."""
        short_term_start = _short_term_start(now)
        external = await self._external_quarters(gap.start, gap.end)
        sources: list[_Source] = []
        lts_end = min(gap.end, short_term_start)
        if lts_end > gap.start:
            lts_data = await async_fetch_change(
                self.hass, self._statistic_ids(), _read_start(gap.start, 3600), lts_end,
                period="hour")
            sources.append(_Source(lts_data, split_hour_to_quarters, gap.start, lts_end))
        five_start = max(gap.start, short_term_start)
        if gap.end > five_start:
            short_data = await async_fetch_change(
                self.hass, self._statistic_ids(), _read_start(five_start, 300), gap.end,
                period="5minute")
            sources.append(
                _Source(short_data, aggregate_5min_to_15min, five_start, gap.end))
        _built, rows = await self._build_rows(now, sources, external)
        return coverage.within(rows, gap.end)

    async def _remember_coverage(self, token_fp: str, since: datetime | None) -> None:
        """Persist the coverage memo – only on change (one Store write per actual change,
        not per tick)."""
        new = coverage.memo(token_fp, since)
        if self._state.get("coverage") != new:
            self._state["coverage"] = new
            await self._store.async_save(self._state)

    async def _reconcile_coverage(
        self, now: datetime, payload: object, *, full_backfill: bool
    ) -> None:
        """Coverage reconciliation (spec 2026-10-10 täckningsavstämning §5.2).

        The server is the source of truth for what is stored. Fill only what lies BEFORE
        its earliest stored interval (`data_since`) – never anything it already holds – in
        PUTs of their own that never touch last_uploaded_ts: mixed into the regular
        upload the bookmark could move backwards and the next heal would overwrite quarters.

        `payload` is the regular PUT's response when one went through this cycle (the
        server's state after our own writes and its spike rule), else the profile GET.
        A full backfill this cycle already read the whole window → just remember.

        The fill is sent NEWEST FIRST, one PUT per slice (coverage.fill_slices: flows + SoC
        within MAX_ROWS_PER_PUT, so the proxy's body limit cannot answer 413). A failure
        part-way through then leaves the stored history contiguous up to the server's new
        `data_since`, so the next cycle's gap [window start, data_since) is exactly what is
        left. Oldest first would leave an interior hole that no later gap calculation can
        see."""
        since = coverage.server_since(payload)
        if since is coverage.MISSING:
            return  # older backend, or no usable payload this cycle
        token_fp = coverage.coverage_token(self.token)
        if full_backfill or not coverage.needs_fill(
                self._state.get("coverage"), token_fp, since):
            await self._remember_coverage(token_fp, since)
            return
        gap = coverage.gap_for(since, now, _BACKFILL_DAYS)
        if gap is None:
            await self._remember_coverage(token_fp, since)
            return
        rows = await self._gap_rows(gap, now)
        if not rows:
            await self._remember_coverage(token_fp, since)
            return
        # Deliberately NOT floored like _short_term_start(): SoC has no completeness logic,
        # and its hourly/5-minute switch may differ from the flows' by up to 59 minutes.
        short_term_start = now - timedelta(days=_SHORT_TERM_DAYS)
        soc, soc_ok = await self._soc_state(
            gap.start, gap.end,
            hourly_until=min(max(gap.start, short_term_start), gap.end))
        if not soc_ok:
            # Sending the flows now would let the memo move past the gap, and the gap's
            # SoC would then never be sent. Retry flows and SoC together next cycle.
            _LOGGER.warning(
                "Wolta coverage fill postponed: the state-of-charge read failed; "
                "retrying next cycle")
            return
        slices = coverage.fill_slices(rows, coverage.within(soc, gap.end), MAX_ROWS_PER_PUT)
        response, error = await self._put_fill(slices)
        if error is not None:
            # Only the server's own answer can be a permanent refusal; an aiohttp error that
            # happens to carry a status is a transport problem and is retried.
            status = error.status if isinstance(error, WoltaApiError) else None
            if coverage.is_permanent_refusal(status):
                # Retrying every cycle changes nothing. New data still uploads normally, so
                # no repair issue - just remember and stop.
                _LOGGER.warning(
                    "Wolta coverage fill rejected by the server (HTTP %s)%s; "
                    "older history could not be added",
                    status,
                    " - the storage limit was reached" if status == 413 else "",
                )
                await self._remember_coverage(token_fp, since)
            else:
                _LOGGER.warning(
                    "Wolta coverage fill failed (%s%s); retrying next cycle",
                    type(error).__name__,
                    f" {status}" if status is not None else "",
                )
            return
        # The LAST response is the oldest slice's: the server's state after the whole fill.
        after = coverage.server_since(response)
        await self._remember_coverage(token_fp, since if after is coverage.MISSING else after)
        _LOGGER.info(
            "Wolta: sent %d quarter(s) of history the server was missing before %s",
            len(rows), gap.end.isoformat())

    async def _put_fill(
        self, slices: list[tuple[list[dict], list[dict]]]
    ) -> tuple[object, Exception | None]:
        """Send the fill slices in the given order (newest first). Returns (the last
        response, the error that stopped the fill or None). WoltaAuthError propagates.

        The first stored slice persists pending_invert_recompute at once: new history
        changes the grade however the fill ends - a later failure, or a cancellation
        (HA shutting down mid-fill) that never returns here."""
        response: object = None
        for index, (flows, soc) in enumerate(slices):
            try:
                _soc_delivered, response = await self._put_rows(flows, soc)
            except WoltaAuthError:
                raise  # purged profile → the same reauth path as the regular upload
            except (WoltaApiError, aiohttp.ClientError, TimeoutError) as err:
                return response, err
            if index == 0:
                self._state["pending_invert_recompute"] = True
                await self._store.async_save(self._state)
        return response, None

    # ------------------------------------------------------------------
    # Recompute logic
    # ------------------------------------------------------------------

    async def _maybe_recompute(self, now: datetime) -> None:
        """Trigger recompute when ≥7 new days of data since last recompute."""
        results = None
        # Use cached data if available to avoid an extra API call
        if self.data is not None:
            results = self.data.results
        else:
            try:
                results = await self.client.results(self.token)
            except (WoltaAuthError, WoltaApiError, aiohttp.ClientError, TimeoutError):
                return

        if results is None:
            return

        period_end_str = (results.get("period") or {}).get("end")
        if not period_end_str:
            return

        # New history was just uploaded (a coverage fill) → FORCE a recompute bypassing the 7-day
        # cadence, otherwise the grade won't show it until the nightly rewarm/next tick. A direction
        # swap or a sensor change's replacement does not set it: the server queues the recount at
        # commit. The flag is only cleared once the recompute succeeds (survives a
        # failed attempt).
        force = bool(self._state.get("pending_invert_recompute"))

        last_recompute_str = self._state.get("last_recompute")

        if not force and last_recompute_str:
            try:
                last_recompute_date = datetime.fromisoformat(last_recompute_str).date()
                period_end_date = datetime.fromisoformat(period_end_str).date()
                days_since = (period_end_date - last_recompute_date).days
                if days_since < _RECOMPUTE_INTERVAL_DAYS:
                    return
            except (ValueError, AttributeError):
                pass  # malformed date → fall through to recompute

        # 429-backoff: the server's 24h cooldown rejected our last attempt – retrying
        # every poll is futile and noisy (~20 requests/day per user seen in prod logs).
        # Honor the Retry-After we were given and stay quiet until it has passed.
        # Applies to the forced path too: the server rejects regardless, and the
        # pending_invert_recompute flag survives until a recompute actually succeeds.
        blocked_until_str = self._state.get("recompute_blocked_until")
        if blocked_until_str:
            try:
                if now < datetime.fromisoformat(blocked_until_str):
                    return
            except (ValueError, TypeError):
                pass  # malformed timestamp → drop it and retry
            self._state.pop("recompute_blocked_until", None)

        # Trigger recompute
        try:
            await self.client.recompute(self.token)
            self._state["last_recompute"] = period_end_str
            self._state.pop("pending_invert_recompute", None)  # corrected – clear the force flag
            self._state.pop("recompute_blocked_until", None)
            await self._store.async_save(self._state)
            _LOGGER.debug("Recompute triggered; last_recompute updated to %s", period_end_str)
        except WoltaRateLimitError as err:
            blocked_until = now + timedelta(seconds=err.retry_after)
            self._state["recompute_blocked_until"] = blocked_until.isoformat()
            await self._store.async_save(self._state)
            _LOGGER.debug("Recompute rate-limited; backing off until %s", blocked_until)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Recompute failed; will retry next cycle", exc_info=True)

    # ------------------------------------------------------------------
    # Repair issue tracking
    # ------------------------------------------------------------------

    def _track_failure(self, err: Exception) -> None:
        """Increment failure counter; raise repair issue after >7 days."""
        failure_since_str = self._state.get("failure_since")
        if failure_since_str is None:
            self._state["failure_since"] = dt_util.utcnow().isoformat()
        else:
            try:
                failure_since = datetime.fromisoformat(failure_since_str)
                days_failing = (dt_util.utcnow() - failure_since).days
                if days_failing > _REPAIR_FAILURE_DAYS:
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        _ISSUE_UPLOAD_FAILURE,
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="upload_failure",
                        translation_placeholders={"error": str(err)},
                    )
            except (ValueError, AttributeError):
                pass

    def _set_measured_issue(
        self, key: str, *, fire, translation_key: str, placeholders: dict, data: dict
    ) -> None:
        """Create or clear a per-entry fixable repair (shared boilerplate for the measured
        parameter adopt flows). Per-entry issue id so multiple profiles don't collide."""
        issue_id = f"{key}_{self.config_entry.entry_id}"
        if fire:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=translation_key,
                translation_placeholders=placeholders,
                data={**data, "entry_id": self.config_entry.entry_id},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _evaluate_battery_pending(self) -> None:
        """Repair när backend gett upp mätningen (spec 2026-09-14 §7.2). Ingen ignore-väg:
        utan värden finns inget betyg. Tas bort så fort statusen lämnar needs_input.

        Visningsläge fyrar aldrig: där äger anläggningens bindning profilen, så frågan
        skulle nå fel person - och servern 409:ar skrivningen ändå."""
        detect = self._battery_detect or {}
        days = int(detect.get("n_days") or 0)
        fire = self._battery_status == BATTERY_STATUS_NEEDS_INPUT and not self._view_only
        self._set_measured_issue(
            _ISSUE_BATTERY_NEEDS_INPUT,
            fire=fire,
            translation_key=_ISSUE_BATTERY_NEEDS_INPUT,
            placeholders={"days": str(days)} if fire else {},
            # A custom RepairsFlow does not inherit the issue's translation_placeholders,
            # so the day count the flow's text references has to travel on the issue data.
            data={"days": days} if fire else {},
        )

    def _evaluate_measured_params(self, results: dict) -> None:
        """Evaluate all measured-parameter adopt repairs (capacity, power, efficiency).

        The backend measures these from the actual meter flows (observed_* in the grade
        payload); we compare against the configured values and raise a fixable repair when
        they clearly disagree. Auto-measuring the grade-affecting inputs keeps the grade fair
        without the user having to know nameplate-vs-usable, AC-vs-DC, etc."""
        betyg = results.get("betyg")
        betyg = betyg if isinstance(betyg, dict) else {}
        # Before the preliminary return below: unlike the adopt nudges this is not a
        # measurement that needs a mature window - the backend's floor (a full battery charge
        # of impossible energy) already keeps short windows from firing it.
        # Likaså riktningsreparationen: den vilar på serverns tre-dygnsgolv (inget "reversed"
        # under tre kontrollerade dygn), på samma sätt som energibalansen vilar på sitt golv.
        self._evaluate_battery_direction_issue(betyg)
        self._evaluate_energy_balance_issue(betyg)
        if self._direction_reversed(betyg):
            # Adopt-förslagen är rekonstruerade ur samma felvända flöden. Före den
            # preliminära returen nedan: ett preliminärt men omkastat betyg kan ha kvar
            # adopt-issues från ett tidigare moget betyg, och de ska bort även då.
            for key in (_ISSUE_CAPACITY, _ISSUE_POWER, _ISSUE_EFFICIENCY):
                ir.async_delete_issue(self.hass, DOMAIN, f"{key}_{self.config_entry.entry_id}")
            return
        # Preliminärt betyg (backend api 0.43.0): < 30 dygns data. Mätningen av kapacitet/
        # effekt/verkningsgrad är då systematiskt UNDERSKATTAD (batteriet har inte hunnit
        # visa hela sitt fönster) → adopt-förslagen vore falska. Vänta tills betyget mognar.
        if betyg.get("preliminary"):
            return
        self._evaluate_capacity_issue(betyg)
        self._evaluate_power_issue(betyg)
        self._evaluate_efficiency_issue(betyg)

    def _evaluate_soc_missing_issue(self, results: dict) -> None:
        """Nudge to pick a state-of-charge sensor (README: State of charge monitoring).

        Fires only where the question has an answer and is not noise: the server knows the
        plant has a battery (stamp measured/nameplate - not none, not still being measured,
        not a stamp we never received), the grade is settled (a new install was offered the
        field in setup a moment ago), no sensor is picked and the user has not said no.
        Never in view-only mode: that entry uploads nothing, and the plant's binding
        delivers its own SoC."""
        betyg = results.get("betyg")
        if not isinstance(betyg, dict) or not betyg:
            # No grade block says nothing about the plant (a recompute in flight, an
            # old backend): leave whatever is open as it is rather than flapping.
            return
        fire = (
            not betyg.get("preliminary")
            and not self._view_only
            and not self._soc_entities
            and not self.config_entry.data.get(CONF_SOC_ISSUE_IGNORED)
            and self._battery_status in (BATTERY_STATUS_MEASURED, BATTERY_STATUS_NAMEPLATE)
        )
        self._set_measured_issue(
            _ISSUE_SOC_MISSING, fire=fire, translation_key=_ISSUE_SOC_MISSING,
            placeholders={}, data={})

    @staticmethod
    def _direction_reversed(betyg: dict) -> bool:
        # Only the state matters, not the form: a form this client does not know yet still
        # means the flows point the wrong way, which makes the energy balance and the adopt
        # suggestions unreliable whatever the form.
        block = betyg.get("battery_direction") if isinstance(betyg, dict) else None
        return isinstance(block, dict) and block.get("state") == "reversed"

    def _clear_direction_flags(self) -> None:
        """The direction is healthy again: forget the flip stamp and the ignore flag. The
        ignore covers one fault episode, not a standing preference."""
        data = self.config_entry.data
        if CONF_DIRECTION_FLIPPED_AT not in data and CONF_DIRECTION_ISSUE_IGNORED not in data:
            return
        new_data = {k: v for k, v in data.items()
                    if k not in (CONF_DIRECTION_FLIPPED_AT, CONF_DIRECTION_ISSUE_IGNORED)}
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)

    def _direction_flip_pending(self, block: dict) -> bool:
        """True while a flip has not reached the grade yet: a direction job still runs, or
        the owner's flip is newer than the server's direction data. A missing or
        unreadable as_of counts as "not recounted yet"."""
        job = self._state.get("replacement_job")
        if isinstance(job, dict) and job.get("type") == "direction":
            # The stored rows are not all swapped yet: any grade still describes the old
            # direction, however new its as_of.
            return True
        flipped = self._parse_utc(self.config_entry.data.get(CONF_DIRECTION_FLIPPED_AT))
        if flipped is None:
            return False
        as_of = self._parse_utc(block.get("as_of"))
        return as_of is None or as_of <= flipped

    @staticmethod
    def _parse_utc(value):
        if not isinstance(value, str):
            return None
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)

    def _evaluate_battery_direction_issue(self, betyg: dict) -> None:
        """Repair när servern ser omkastade laddfält ur laddningsnivån. Fixable when the
        server gives intervals to flip (direction_swap_intervals): the fix swaps exactly
        those in the stored rows, nothing is re-sent. Otherwise text only. Saknas blocket
        (äldre backend) eller är state något annat än "reversed" tas issuen bort.

        Aldrig i visningsläget: där mappar Woltas koppling fälten, inte de här sensorerna."""
        issue_id = f"{_ISSUE_BATTERY_DIRECTION}_{self.config_entry.entry_id}"
        block = betyg.get("battery_direction") if isinstance(betyg, dict) else None
        form = block.get("form") if isinstance(block, dict) else None
        if isinstance(block, dict) and block.get("state") in ("ok", "verified"):
            self._clear_direction_flags()
        if (not self._direction_reversed(betyg) or self._view_only
                or form not in _BATTERY_DIRECTION_FORMS):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        swap = direction_swap_intervals(block)
        fixable = bool(swap)
        # Ignoreringen gäller bara den fixbara reparationen (den har en meny att avböja ur);
        # the text-only one is always shown.
        if fixable and self.config_entry.data.get(CONF_DIRECTION_ISSUE_IGNORED):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        if fixable and self._direction_flip_pending(block):
            # Owner flipped the direction and the server has not recounted since: this grade
            # still describes the old direction. Leave the registry alone - a deleted issue
            # stays deleted, and nothing is raised to invite a second flip back.
            return

        def _names(stream: str) -> str:
            # Sensorerna som de är valda (inte _effective_stream): texten ska namnge det
            # användaren ser under Konfigurera.
            return ", ".join(self._entity_map[stream]) or "-"

        placeholders = {
            "reversed_days": str(block.get("reversed_days") or 0),
            "first_checked": block.get("first_checked") or "-",
            "first_reversed": block.get("first_reversed") or "-",
            "last_reversed": block.get("last_reversed") or "-",
            "since": block.get("since") or "-",
            "batt_in": _names("batt_in"),
            "batt_out": _names("batt_out"),
            "swap_period": self._swap_period(swap),
        }
        key = f"{_ISSUE_BATTERY_DIRECTION}_{form}"
        if fixable and form in _BATTERY_DIRECTION_TEXT_FORMS:
            key = f"{key}_flip"
        elif not fixable and form not in _BATTERY_DIRECTION_TEXT_FORMS:
            key = _ISSUE_BATTERY_DIRECTION_NO_FIX
        ir.async_create_issue(
            self.hass, DOMAIN, issue_id,
            is_fixable=fixable,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders=placeholders,
            # En egen RepairsFlow ärver inte translation_placeholders: samma värden följer
            # med på issuen så flödets texter kan fyllas i. The flow flips swap_intervals.
            data={"entry_id": self.config_entry.entry_id, "form": form,
                  "swap_intervals": intervals.dump(swap), **placeholders},
        )

    @staticmethod
    def _swap_period(swap: list[intervals.Interval]) -> str:
        """The intervals to flip in local time, for the repair texts. Language-neutral:
        "2026-09-28 15:00 → …" (an open end shows as …)."""
        def _fmt(t: datetime | None) -> str:
            return "…" if t is None else dt_util.as_local(t).strftime("%Y-%m-%d %H:%M")

        return ", ".join(f"{_fmt(s)} → {_fmt(e)}" for s, e in swap) or "-"

    def _evaluate_energy_balance_issue(self, betyg: dict) -> None:
        """Non-fixable repair when the backend has excluded the grade because the household
        energy balance does not add up. The fix is a different sensor under Reconfigure, so
        there is no flow - the text names the mapped sensors the cause points at.

        Never in view-only mode: there the data comes from a webhook/bridge, not these HA
        sensors, so advice about the sensor mapping would reach the wrong person. A missing
        block or a missing `excluded` (older backend) clears rather than guesses. A reversed
        battery direction replaces this repair: its cause text rests on the same flows."""
        eb = betyg.get("energy_balance") if isinstance(betyg, dict) else None
        eb = eb if isinstance(eb, dict) else {}
        cause = eb.get("cause") if eb.get("cause") in _ENERGY_BALANCE_CAUSES else "unclear"
        issue_id = f"{_ISSUE_ENERGY_BALANCE}_{self.config_entry.entry_id}"
        if (eb.get("excluded") is True and not self._view_only
                and not self._direction_reversed(betyg)):
            def _names(stream: str) -> str:
                return ", ".join(self._entity_map[self._effective_stream(stream)]) or "-"
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=f"{_ISSUE_ENERGY_BALANCE}_{cause}",
                translation_placeholders={
                    "solar": _names("solar"),
                    "grid_in": _names("grid_in"),
                    "batt_in": _names("batt_in"),
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _evaluate_capacity_issue(self, betyg: dict) -> None:
        """Adopt the measured usable capacity. observed_capacity is the all-time DISPATCHABLE
        window at the meter (what the battery has demonstrably moved). Compared against the
        configured EFFECTIVE window (battery_kwh minus any reserve). The fix flow adopts it as
        battery_kwh with the reserve cleared — the measurement already excludes the reserve, so
        it must NOT be reduced again (repairs.py)."""
        oc = betyg.get("observed_capacity") if isinstance(betyg, dict) else None
        configured = self.config_entry.data.get(CONF_BATTERY_KWH)
        reserve = self.config_entry.data.get(CONF_RESERVE_PCT) or 0.0
        effective = configured * (1.0 - reserve / 100.0) if configured else None
        measured = float(oc["kwh"]) if oc and oc.get("kwh") else None
        # Trust gate (issue #1): the reconstruction inflates at low round-trip efficiency. Prefer
        # the MEASURED round-trip (observed_eff, always present once the capacity adopt matures at
        # 60 d), falling back to the configured eff. When it's low we suppress an UP suggestion
        # only – a DOWN one stays valid (true usable is then even lower than the inflated figure).
        oe = betyg.get("observed_eff") if isinstance(betyg, dict) else None
        eff_signal = (float(oe["eff"]) if oe and oe.get("eff")
                      else self.config_entry.data.get(CONF_EFF))
        untrusted_up = (eff_signal is not None and eff_signal < _CAP_TRUST_EFF
                        and measured is not None and effective is not None
                        and measured > effective)
        ignored = bool(self.config_entry.data.get(CONF_CAPACITY_ISSUE_IGNORED))
        fire = (
            measured is not None
            and effective
            and effective > 0
            and oc.get("n_days", 0) >= _CAP_MATURE_DAYS
            and oc.get("plateau_days", 0) >= _CAP_PLATEAU_DAYS
            and abs(measured - effective) / effective >= _CAP_GAP
            and not untrusted_up
            and not ignored
        )
        self._set_measured_issue(
            _ISSUE_CAPACITY,
            fire=fire,
            translation_key=_ISSUE_CAPACITY,
            placeholders={
                "measured": f"{measured:.1f}",
                "configured": f"{effective:.1f}",
                "days": str(oc.get("n_days", 0)),
            } if fire else {},
            data={"measured_kwh": measured, "configured_kwh": effective,
                  "days": oc.get("n_days", 0)} if fire else {},
        )

    def _evaluate_power_issue(self, betyg: dict) -> None:
        """Adopt the measured peak power. observed_power is the all-time max charge/discharge
        power at the meter — a LOWER bound (the controller may never have demanded full power),
        so unlike capacity a too-low value can FLATTER the grade. The fix flow therefore uses an
        editable field pre-filled with the measured value and tells the user to set the real
        maximum: it's likely the measured figure, but higher if the battery can do more."""
        op = betyg.get("observed_power") if isinstance(betyg, dict) else None
        configured = self.config_entry.data.get(CONF_BATTERY_KW)
        measured = float(op["kw"]) if op and op.get("kw") else None
        # Physical-ceiling gate (Jan Hinders, 2026-08-04): observed_power has only spike
        # protection (3rd-highest interval), no hard ceiling. Chronic sensor jumps in a
        # cumulative HA statistic can inflate it far above the hardware maximum (a 9.9 kW battery
        # "measured" at 27.5 kW). A battery can't sustain charge/discharge above its inverter
        # power, so a measurement exceeding the declared power (nameplate if set, else configured)
        # by more than the fysiktak tolerance is a data artefact, not an under-declaration.
        # Suppress the UP suggestion only — a DOWN one (measured < configured) is always below
        # the ceiling and stays valid. Mirrors the capacity trust gate (untrusted_up).
        nameplate = self.config_entry.data.get(CONF_NAMEPLATE_KW)
        ceiling_ref = max(nameplate or 0.0, configured or 0.0)
        untrusted_up = (
            measured is not None and ceiling_ref > 0
            and measured > ceiling_ref * _POWER_MAX_PHYSICAL
        )
        # The user can dismiss the nudge for good (repair "ignore"); they stand by their value.
        ignored = bool(self.config_entry.data.get(CONF_POWER_ISSUE_IGNORED))
        fire = False
        if (
            measured is not None
            and configured
            and configured > 0
            and op.get("n_days", 0) >= _CAP_MATURE_DAYS
            and not untrusted_up
            and not ignored
        ):
            gap = abs(measured - configured) / configured
            # Raise = high-confidence (small gap); lower = risky (needs a bigger gap).
            fire = gap >= (_POWER_GAP if measured > configured else _POWER_GAP_SHRINK)
        self._set_measured_issue(
            _ISSUE_POWER,
            fire=fire,
            translation_key=_ISSUE_POWER,
            placeholders={
                "measured": f"{measured:.1f}",
                "configured": f"{configured:.1f}",
                "days": str(op.get("n_days", 0)),
            } if fire else {},
            # configured_kw/days are threaded to the fix flow too: a custom RepairsFlow does not
            # inherit the issue's translation_placeholders (only the default ConfirmRepairFlow
            # does), so the flow must be given every value its step strings reference.
            data={"measured_kw": measured, "configured_kw": configured,
                  "days": op.get("n_days", 0)} if fire else {},
        )

    def _evaluate_efficiency_issue(self, betyg: dict) -> None:
        """Adopt the measured round-trip efficiency. observed_eff is the lifetime AC round-trip
        (energy out / energy in) — a true measurement, not a lower bound — so a one-click adopt
        is fine. This corrects a stale DEFAULT_EFF from a setup done with thin history."""
        oe = betyg.get("observed_eff") if isinstance(betyg, dict) else None
        configured = self.config_entry.data.get(CONF_EFF)
        measured = float(oe["eff"]) if oe and oe.get("eff") else None
        # observed_eff = Σdischarged/Σcharged over all history. Spurious sensor jumps inflate the
        # charged sum → the ratio is biased LOW (a plausible-but-wrong figure like 0.76 for a real
        # ~0.90 round-trip). There is no clean physical floor that separates a genuine low AC
        # round-trip from an artefact, so unlike power there is no auto-suppression — but the user
        # can dismiss it (the ignore option), which persists here.
        ignored = bool(self.config_entry.data.get(CONF_EFFICIENCY_ISSUE_IGNORED))
        fire = (
            measured is not None
            and configured
            and oe.get("n_days", 0) >= _CAP_MATURE_DAYS
            # A home-battery AC round-trip can't realistically reach ~1.0; a measured value that
            # high is a clamp/window-boundary artefact, so don't offer to adopt it.
            and measured < _EFF_MAX_PLAUSIBLE
            and abs(measured - float(configured)) >= _EFF_ABS_GAP
            and not ignored
        )
        self._set_measured_issue(
            _ISSUE_EFFICIENCY,
            fire=fire,
            translation_key=_ISSUE_EFFICIENCY,
            placeholders={
                "measured": f"{measured:.2f}",
                "configured": f"{float(configured):.2f}",
                "days": str(oe.get("n_days", 0)),
            } if fire else {},
            data={"measured_eff": measured, "configured_eff": float(configured),
                  "days": oe.get("n_days", 0)} if fire else {},
        )

    # ------------------------------------------------------------------
    # Button helper
    # ------------------------------------------------------------------

    async def async_trigger_recompute(self) -> None:
        """Immediately trigger a server-side recompute (button entity + options flow).

        Stamps last_recompute against the current period end on success, so the coordinator
        refresh the caller issues right after does NOT make _maybe_recompute fire a redundant
        second recompute (the backend rejects that with 429). On 429/error the recompute call
        raises before the stamp, so last_recompute is left untouched (retryable next cycle).
        """
        await self.client.recompute(self.token)
        # Success clears any cadence backoff (a PATCH may just have reset the
        # server-side cooldown, so the stored block is stale).
        self._state.pop("recompute_blocked_until", None)
        period_end = None
        if self.data is not None:
            period_end = (self.data.results.get("period") or {}).get("end")
        if period_end:
            self._state["last_recompute"] = period_end
        await self._store.async_save(self._state)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _statistic_ids(self) -> set[str]:
        """Return the flat set of all statistic IDs across all stream lists."""
        ids: set[str] = set()
        for entity_list in self._entity_map.values():
            ids.update(entity_list)
        return ids
