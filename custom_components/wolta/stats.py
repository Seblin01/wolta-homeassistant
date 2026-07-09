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


def merge_streams(
    batt_in: dict[datetime, float] | None,
    batt_out: dict[datetime, float] | None,
    grid_in: dict[datetime, float] | None,
    grid_out: dict[datetime, float] | None,
    solar: dict[datetime, float] | None,
) -> list[dict[str, Any]]:
    """Merge per-stream quarter dicts into Wolta PUT row dicts.

    The union of timestamps present in *batt_in* and *batt_out* defines the set
    of valid quarters.  Any quarter where neither battery stream has data is
    excluded from the output.  Missing values in any stream default to 0.0.

    Args:
        batt_in:   dict[datetime, float] – battery charge energy per quarter.
        batt_out:  dict[datetime, float] – battery discharge energy per quarter.
        grid_in:   dict[datetime, float] – grid import per quarter.
        grid_out:  dict[datetime, float] – grid export per quarter.
        solar:     dict[datetime, float] – solar generation per quarter (may be
                   None or empty when the user has no solar).

    Returns:
        List of row dicts with keys:
            ts                  – ISO 8601 UTC string (tz-aware, Z-suffix-free
                                  but offset "+00:00" via datetime.isoformat())
            batt_charged_kwh
            batt_discharged_kwh
            solar_kwh
            grid_import_kwh
            grid_export_kwh
        Rows are sorted chronologically by ``ts``.
    """
    _batt_in = batt_in or {}
    _batt_out = batt_out or {}
    _grid_in = grid_in or {}
    _grid_out = grid_out or {}
    _solar = solar or {}

    # Only emit rows where at least one battery stream has a value
    valid_quarters = _batt_in.keys() | _batt_out.keys()

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
        }
        # A row above the cap (meter reset spike or wrong unit on the sensor) would
        # 422 the whole batch server-side – drop the row instead of losing everything.
        if any(v > _BACKEND_MAX_KWH for k, v in row.items() if k != "ts"):
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

    This is the *only* function in this module that imports from
    ``homeassistant``; the rest are pure and testable without an HA runtime.

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
