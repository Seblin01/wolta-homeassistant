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

from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict


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

    rows = []
    for qt in sorted(valid_quarters):
        rows.append(
            {
                "ts": qt.isoformat(),
                "batt_charged_kwh": _batt_in.get(qt, 0.0),
                "batt_discharged_kwh": _batt_out.get(qt, 0.0),
                "solar_kwh": _solar.get(qt, 0.0),
                "grid_import_kwh": _grid_in.get(qt, 0.0),
                "grid_export_kwh": _grid_out.get(qt, 0.0),
            }
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
        None,
        {"change"},
    )
