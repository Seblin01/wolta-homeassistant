"""Statistics helpers for the Wolta integration.

Provides pure, recorder-free aggregation functions (unit-testable without HA)
and one async function that calls into the HA recorder executor.

StatisticsRow shape (subset used here):
  {
    "start": float,          # UNIX timestamp (seconds, float)
    "change": float | None,  # accumulated change in the period; None = missing
  }
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

_LOGGER = logging.getLogger(__name__)

# The backend's DataRow validation caps every energy field at 500 kWh per
# quarter (Field(le=500)). One offending row 422:s the whole PUT batch, so
# rows that would violate the cap are dropped client-side instead.
_BACKEND_MAX_KWH = 500.0


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


class StatisticsRow(TypedDict, total=False):
    start: float
    change: float | None


# ---------------------------------------------------------------------------
# Pure aggregation functions (no Home Assistant imports)
# ---------------------------------------------------------------------------


def aggregate_5min_to_15min(
    rows: list[StatisticsRow],
) -> dict[datetime, float]:
    """Sum 5-min StatisticsRow ``change`` values into 15-min UTC buckets.

    Bucket key = ``start`` timestamp floored to the nearest 900-second boundary
    in UTC.  ``change=None`` is treated as 0.0 (HA may emit None for gaps).

    Args:
        rows: List of StatisticsRow dicts with ``start`` (UNIX float) and
              ``change`` (float|None).

    Returns:
        Mapping of UTC-aware datetime (floored to 900 s) → sum of changes.
    """
    result: dict[datetime, float] = {}
    for row in rows:
        unix = row["start"]
        # Floor to 900-second boundary
        bucket_unix = int(unix) - (int(unix) % 900)
        bucket = datetime.fromtimestamp(bucket_unix, tz=timezone.utc)
        change = row.get("change") or 0.0
        result[bucket] = result.get(bucket, 0.0) + change
    return result


def split_hour_to_quarters(
    rows: list[StatisticsRow],
) -> dict[datetime, float]:
    """Distribute an hourly ``change`` value equally across four 15-min quarters.

    Each input row represents one hour; the ``change`` value is divided by 4
    and assigned to the :00, :15, :30, :45 sub-timestamps of that hour.
    Used for backfill/healing from long-term (hourly) statistics.

    ``change=None`` → 0.0 per quarter.

    Args:
        rows: List of StatisticsRow dicts with ``start`` (UNIX float, hour-aligned)
              and ``change`` (float|None).

    Returns:
        Mapping of UTC-aware datetime (quarter boundary) → quarter value.
    """
    result: dict[datetime, float] = {}
    for row in rows:
        unix = row["start"]
        # Round down to the hour boundary
        hour_dt = datetime.fromtimestamp(int(unix) - (int(unix) % 3600), tz=timezone.utc)
        change = row.get("change") or 0.0
        quarter = change / 4.0
        for offset_min in (0, 15, 30, 45):
            key = hour_dt + timedelta(minutes=offset_min)
            result[key] = result.get(key, 0.0) + quarter
    return result


def sum_quarter_dicts(dicts: list[dict[datetime, float]]) -> dict[datetime, float]:
    """Sum several per-15-min-bucket streams by timestamp (multiple inverters → one stream).

    Args:
        dicts: List of per-quarter dicts (each maps UTC datetime → float kWh).

    Returns:
        A single merged dict where values at the same timestamp are summed.
        Empty list → empty dict.
    """
    result: dict[datetime, float] = {}
    for d in dicts:
        for ts, val in d.items():
            result[ts] = result.get(ts, 0.0) + val
    return result


def _compiled_quarters(
    *streams: dict[datetime, float],
) -> set[datetime]:
    """Quarters the recorder has actually compiled statistics for.

    A quarter's presence as a KEY in any stream is the proof: ``statistics_during_period``
    returns a row per compiled period, and both aggregation functions above create the
    bucket even when ``change`` is 0 or None. Absence means the recorder compiled
    nothing there – HA was down, the period is not finished yet, or it predates the
    data.

    Why the flag has to be tested against this set. The external-control flag comes
    from the recorder's STATES table and is HELD FORWARD from the last known state
    point until the next one (see ``flagged_quarters``); the start-time-state row
    carries an old ``last_changed``, so one stale 'on' row keeps the flag asserted
    across an arbitrarily long stretch – the whole heal window, which is at least nine
    days by construction. Emitting a flagged quarter with nothing compiled uploads
    real charge/discharge/grid energy as 0.0 AND drags the coordinator's bookmark
    (``rows[-1]["ts"]``) onto it: the next cycle starts after those quarters and never
    re-reads them, so the zeros stick server-side forever and feed the economy and the
    ``observed_*`` measured parameters.

    This replaces an earlier "statistics frontier" (a global max over all streams,
    tested as ``qt <= frontier``). That only closed the gap ABOVE the newest compiled
    quarter; every INNER gap – below the max but with no statistics – still passed and
    emitted all-zero rows, which is the same defect one step inwards. Per-quarter
    proof closes both with a single test, so the frontier is not kept as a separate
    concept.

    ALL streams count, not only the battery ones: a compiled quarter where only grid
    or solar moved is exactly what forced idle looks like – the house keeps drawing
    from the grid while the flex service holds the battery still – and that quarter
    must still reach the backend.
    """
    compiled: set[datetime] = set()
    for stream in streams:
        compiled |= stream.keys()
    return compiled


def merge_streams(
    batt_in: dict[datetime, float] | None,
    batt_out: dict[datetime, float] | None,
    grid_in: dict[datetime, float] | None,
    grid_out: dict[datetime, float] | None,
    solar: dict[datetime, float] | None,
    external: set[datetime] | None = None,
) -> list[dict[str, Any]]:
    """Merge per-stream quarter dicts into Wolta PUT row dicts.

    Valid quarters are the timestamps present in *batt_in* or *batt_out*, plus the
    quarters flagged in *external* that the recorder actually COMPILED statistics for
    – i.e. that are a key in at least one of the five streams.

    A flagged quarter is emitted even with no battery activity: a flex session forcing
    the battery to stand still produces no battery energy, but the forced idle is still
    external control and must reach the backend (spec 2026-08-26 B4/B5) – dropping
    those would silently lose the flag. A flagged quarter with no compiled statistics
    anywhere is NOT emitted, however: the flag is held forward from the last known
    state point and can span stretches the recorder never compiled, where every field
    would fall to 0.0 and overwrite real energy server-side (see
    ``_compiled_quarters``). Any other quarter where neither battery stream has data is
    excluded. Missing values in any stream default to 0.0.

    Args:
        batt_in:   dict[datetime, float] – battery charge energy per quarter.
        batt_out:  dict[datetime, float] – battery discharge energy per quarter.
        grid_in:   dict[datetime, float] – grid import per quarter.
        grid_out:  dict[datetime, float] – grid export per quarter.
        solar:     dict[datetime, float] – solar generation per quarter (may be
                   None or empty when the user has no solar).
        external:  set[datetime] – quarters flagged by flagged_quarters() as
                   externally controlled (may be None when no sensor is
                   configured; then every row's external_control is False).

    Returns:
        List of row dicts with keys:
            ts                  – ISO 8601 UTC string (tz-aware, Z-suffix-free
                                  but offset "+00:00" via datetime.isoformat())
            batt_charged_kwh
            batt_discharged_kwh
            solar_kwh
            grid_import_kwh
            grid_export_kwh
            external_control    – bool, True when the quarter is in *external*
        Rows are sorted chronologically by ``ts``.
    """
    _batt_in = batt_in or {}
    _batt_out = batt_out or {}
    _grid_in = grid_in or {}
    _grid_out = grid_out or {}
    _solar = solar or {}
    _external = external or set()

    # Only emit rows where at least one battery stream has a value – except a
    # flagged quarter, which is emitted regardless of battery activity (forced idle
    # is still external control; see docstring) PROVIDED the recorder compiled
    # statistics for that very quarter. See _compiled_quarters for why the flag
    # cannot be trusted on its own.
    compiled = _compiled_quarters(_batt_in, _batt_out, _grid_in, _grid_out, _solar)
    valid_quarters = _batt_in.keys() | _batt_out.keys() | (_external & compiled)

    def _nn(v: float) -> float:
        # The recorder's `change` on an energy counter can go slightly negative (float noise
        # or a small meter correction/reset), e.g. -0.025 kWh. These quantities are
        # physically non-negative and the backend rejects negative values (422). Floor to 0.
        return v if v > 0.0 else 0.0

    rows = []
    dropped = 0
    for qt in sorted(valid_quarters):
        row = {
            "ts": qt.isoformat(),
            "batt_charged_kwh": _nn(_batt_in.get(qt, 0.0)),
            "batt_discharged_kwh": _nn(_batt_out.get(qt, 0.0)),
            "solar_kwh": _nn(_solar.get(qt, 0.0)),
            "grid_import_kwh": _nn(_grid_in.get(qt, 0.0)),
            "grid_export_kwh": _nn(_grid_out.get(qt, 0.0)),
            "external_control": qt in _external,
        }
        # A row above the cap (meter reset spike or wrong unit on the sensor) would
        # 422 the whole batch server-side – drop the row instead of losing everything.
        # external_control is a bool flag, not an energy field – exclude it from the cap check.
        if any(
            v > _BACKEND_MAX_KWH for k, v in row.items() if k not in ("ts", "external_control")
        ):
            dropped += 1
            continue
        rows.append(row)
    if dropped:
        _LOGGER.warning(
            "Dropped %d row(s) exceeding %.0f kWh per 15 min before upload; "
            "check that the selected statistics are recorded in kWh "
            "(a meter reset spike can also cause this)",
            dropped,
            _BACKEND_MAX_KWH,
        )
    return rows


def flagged_quarters(
    points: list[tuple[datetime, str]],
    end: datetime,
    start: datetime | None = None,
) -> set[datetime]:
    """900-second buckets where the sensor was 'on' for ANY part of the bucket
    (any-overlap rule, spec 2026-08-26 B4 – the error direction is 'missed penalty',
    never 'wrong penalty'). ``points`` are (timestamp, state) state-change points in
    chronological order; each state holds until the next point (or ``end``).
    'unavailable'/'unknown' count as off – absence of signal never flags.

    ``start`` is the requested window start; quarters beginning before it are dropped.
    ``get_significant_states(include_start_time_state=True)`` returns a synthetic
    first point carrying the state's ORIGINAL ``last_changed``, which can lie long
    before the window. Flooring that to a 900-second boundary would otherwise return
    the quarter BEFORE the window (start 13:52:17 → 13:45) and emit an all-zero row
    for a quarter the caller never asked about, overwriting whatever was stored there.
    Heal/incremental starts come from the bookmark and are already quarter boundaries,
    so clamping is a no-op for them; the backfill path (``now - 365 days``) is the
    only unaligned caller. ``None`` disables the clamp."""
    flagged: set[datetime] = set()
    for i, (ts, state) in enumerate(points):
        if state != "on":
            continue
        stop = points[i + 1][0] if i + 1 < len(points) else end
        unix = int(ts.timestamp())
        q = datetime.fromtimestamp(unix - unix % 900, tz=timezone.utc)
        while q < stop:
            if start is None or q >= start:
                flagged.add(q)
            q += timedelta(seconds=900)
    return flagged


# ---------------------------------------------------------------------------
# Setup auto-prefill: analyse lifetime battery history (pure function)
# ---------------------------------------------------------------------------

# Thresholds for auto-prefill from lifetime battery statistics: the measured
# discharge/charge ratio only converges to the true AC round-trip efficiency with
# months of data and real throughput. Below these we suggest nothing.
_PREFILL_MIN_DAYS = 60
_PREFILL_MIN_CHARGED_KWH = 100.0
# Ratio persistently above 1 means the charge/discharge sensors are swapped
# (signed Shelly/Emaldo, issue #1); small margin for metering noise.
_INVERT_RATIO_THRESHOLD = 1.05


def analyze_battery_history(
    charged_kwh: float,
    discharged_kwh: float,
    first_ts: datetime | None,
    now: datetime,
) -> dict:
    """Suggest eff/purchase_date and detect inverted sensors from lifetime sums.

    Pure function (no HA imports). Returns
    {"eff": float | None, "purchase_date": str | None, "invert_suspected": bool}.
    """
    out: dict = {"eff": None, "purchase_date": None, "invert_suspected": False}
    if first_ts is None:
        return out
    out["purchase_date"] = first_ts.date().isoformat()
    span_days = (now - first_ts).days
    if span_days < _PREFILL_MIN_DAYS or charged_kwh < _PREFILL_MIN_CHARGED_KWH:
        return out
    if discharged_kwh <= 0:
        return out
    ratio = discharged_kwh / charged_kwh
    if ratio > _INVERT_RATIO_THRESHOLD:
        out["invert_suspected"] = True
        ratio = 1.0 / ratio  # mirrored ratio = eff with the streams swapped
    out["eff"] = round(min(max(ratio, 0.5), 1.0), 2)
    return out


# ---------------------------------------------------------------------------
# Recorder-touching function (must run via recorder executor)
# ---------------------------------------------------------------------------


async def async_fetch_change(
    hass: Any,
    statistic_ids: set[str],
    start: datetime,
    end: datetime | None,
    period: str,
) -> dict[str, list[StatisticsRow]]:
    """Fetch statistics from the HA recorder using the executor thread.

    This and ``async_fetch_states`` are the only functions in this module that
    import from ``homeassistant``; the rest are pure and testable without an
    HA runtime.

    ``statistics_during_period`` is a blocking DB call and must be executed
    via ``get_instance(hass).async_add_executor_job`` to avoid blocking the
    event loop.

    Args:
        hass:           HomeAssistant instance.
        statistic_ids:  Set of statistic ID strings to query.
        start:          Query window start (timezone-aware).
        end:            Query window end (timezone-aware), or None for open end.
        period:         One of ``"5minute"``, ``"hour"``, etc.

    Returns:
        Dict mapping each statistic_id → list of StatisticsRow dicts, as
        returned by ``statistics_during_period``.
    """
    # Import here to keep the rest of the module importable without HA installed
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        statistics_during_period,
    )

    return await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        statistic_ids,
        period,
        # Normalize to kWh – statistics are stored in the sensor's own unit, and a
        # Wh sensor would otherwise give 1000× too-large values → 422 from the backend's
        # le=500 validation (seen in prod 2026-07-05/06).
        {"energy": "kWh"},
        {"change"},
    )


async def async_fetch_lifetime(
    hass: Any,
    batt_in_ids: list[str],
    batt_out_ids: list[str],
) -> tuple[float, float, datetime | None]:
    """Lifetime charged/discharged sums + first statistics timestamp.

    Reads monthly LTS 'change' from epoch via async_fetch_change (which carries the
    kWh unit normalization – Wh sensors would otherwise inflate sums 1000×). Used by
    the config flow to prefill eff/purchase_date and detect inverted sensors.
    Returns (charged_kwh, discharged_kwh, first_ts).
    """
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    ids = set(batt_in_ids) | set(batt_out_ids)
    stats = await async_fetch_change(hass, ids, start, None, "month")

    def _sum(entity_ids: list[str]) -> float:
        return sum(
            row["change"] or 0.0
            for eid in entity_ids
            for row in stats.get(eid, [])
            if row.get("change") is not None
        )

    first_ts: datetime | None = None
    for rows in stats.values():
        for row in rows:
            # row["start"] är unix-epoch (samma läsning som aggregate_5min_to_15min)
            ts = datetime.fromtimestamp(row["start"], tz=timezone.utc)
            if first_ts is None or ts < first_ts:
                first_ts = ts
    return _sum(batt_in_ids), _sum(batt_out_ids), first_ts


async def async_fetch_states(
    hass: Any, entity_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, str]]:
    """State-change points for one entity from the recorder's states table.

    Unlike async_fetch_change (long-term statistics, years of retention) this reads
    recorder STATES, whose retention is purge_keep_days (default ~10 days). The window
    is DYNAMIC by construction: we ask for the full range and get whatever retention
    holds (spec B5).

    Do NOT read that as "the flag can only reach purge_keep_days back". It cannot be
    bounded that way, and assuming it could is exactly the reasoning error that once
    emitted 1 340 rows of which 1 336 were all-zero. ``include_start_time_state=True``
    adds a synthetic first point for the state in effect AT ``start``, and every point
    carries its ORIGINAL ``last_changed``, which may lie far outside the window.
    ``flagged_quarters`` holds each state forward until the next point (clamping to
    ``start``), so ONE retained 'on' row flags every quarter back to the query's
    ``start`` – up to a year on the backfill path. Purged history produces no state
    points, so it does not interrupt that hold; it only decides which state gets
    carried, usually the 'off' the sensor was last recorded in.

    What bounds the upload is therefore NOT this function and must not be reintroduced
    here as a window assumption: ``merge_streams`` emits a flagged quarter only when
    the recorder compiled statistics for that very quarter. The bound lives where the
    evidence lives."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder import history  # noqa: PLC0415

    def _job():
        return history.get_significant_states(
            hass, start, end, [entity_id],
            include_start_time_state=True,
            significant_changes_only=False,
            no_attributes=True,
        )

    result = await get_instance(hass).async_add_executor_job(_job)
    return [(s.last_changed, s.state) for s in result.get(entity_id, [])]
