"""Tests for custom_components/wolta/stats.py (TDD – write first, then implement)."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone

import pytest

from custom_components.wolta.stats import (
    aggregate_5min_to_15min,
    merge_streams,
    split_hour_to_quarters,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unix(dt: datetime) -> float:
    """Convert a timezone-aware datetime to a UNIX float (as HA provides)."""
    return dt.timestamp()


def _row(dt: datetime, change: float | None) -> dict:
    """Minimal StatisticsRow-shaped dict with unix start and change value."""
    return {"start": _unix(dt), "change": change}


# ---------------------------------------------------------------------------
# aggregate_5min_to_15min
# ---------------------------------------------------------------------------


class TestAggregate5MinTo15Min:
    """12 five-minute rows → 4 fifteen-minute buckets with correct sums."""

    def _make_rows(self):
        """12 rows at 5-min intervals starting at 2024-03-01T10:00Z.

        Expected 15-min buckets:
          10:00–10:14 → rows 0,1,2  (change 0.1, 0.2, 0.3)  → 0.6
          10:15–10:29 → rows 3,4,5  (change 0.4, 0.5, 0.6)  → 1.5
          10:30–10:44 → rows 6,7,8  (change 0.7, 0.8, 0.9)  → 2.4
          10:45–10:59 → rows 9,10,11 (change 1.0, 1.1, 1.2) → 3.3
        """
        base = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(12):
            dt = base.replace(minute=i * 5)
            rows.append(_row(dt, (i + 1) * 0.1))
        return rows

    def test_returns_four_buckets(self):
        rows = self._make_rows()
        result = aggregate_5min_to_15min(rows)
        assert len(result) == 4

    def test_bucket_keys_are_utc_datetimes(self):
        rows = self._make_rows()
        result = aggregate_5min_to_15min(rows)
        for key in result:
            assert isinstance(key, datetime)
            assert key.tzinfo == timezone.utc

    def test_bucket_keys_are_floored_to_900s(self):
        rows = self._make_rows()
        result = aggregate_5min_to_15min(rows)
        for key in result:
            assert int(key.timestamp()) % 900 == 0

    def test_bucket_sums_are_correct(self):
        rows = self._make_rows()
        result = aggregate_5min_to_15min(rows)
        base = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        b0 = base.replace(minute=0)
        b1 = base.replace(minute=15)
        b2 = base.replace(minute=30)
        b3 = base.replace(minute=45)
        assert result[b0] == pytest.approx(0.6, rel=1e-9)
        assert result[b1] == pytest.approx(1.5, rel=1e-9)
        assert result[b2] == pytest.approx(2.4, rel=1e-9)
        assert result[b3] == pytest.approx(3.3, rel=1e-9)

    def test_change_none_treated_as_zero(self):
        """HA can emit change=None; it should not crash and should be treated as 0.0."""
        base = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        rows = [
            _row(base.replace(minute=0), None),
            _row(base.replace(minute=5), 1.0),
            _row(base.replace(minute=10), None),
        ]
        result = aggregate_5min_to_15min(rows)
        assert len(result) == 1
        assert list(result.values())[0] == pytest.approx(1.0, rel=1e-9)

    def test_empty_input_returns_empty_dict(self):
        result = aggregate_5min_to_15min([])
        assert result == {}


class TestAggregate5MinTo15MinDST:
    """DST boundary: buckets are by UTC, so a DST transition does not misalign them."""

    def test_dst_boundary_does_not_split_buckets(self):
        """Europe/Stockholm transitions from CET (UTC+1) to CEST (UTC+2) in late March.
        2024-03-31 01:00 UTC clocks spring forward to 03:00 local.
        Rows constructed in UTC are bucketed purely by UTC, so no split occurs.
        """
        # 12 rows starting at 2024-03-31T00:45Z – straddles the EU DST boundary
        # (at 01:00 UTC on 2024-03-31 clocks go forward in some TZ)
        base = datetime(2024, 3, 31, 0, 45, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(12):
            # 5-min steps: 00:45, 00:50, 00:55, 01:00, 01:05, ... 01:55
            minutes_offset = i * 5
            ts = datetime.fromtimestamp(
                base.timestamp() + minutes_offset * 60, tz=timezone.utc
            )
            rows.append(_row(ts, 1.0))
        result = aggregate_5min_to_15min(rows)
        # 4 UTC-aligned 15-min buckets: 00:45, 01:00, 01:15, 01:30
        # (00:45 is not floor-of-900: 00:45=2700s within hour, floor to 00:45? No —
        #  floor to 900s boundary: 00:45=2700s, 2700//900=3, so bucket = 00:45)
        assert len(result) == 4
        # Each bucket should have exactly 3 rows × 1.0 = 3.0
        for v in result.values():
            assert v == pytest.approx(3.0, rel=1e-9)


# ---------------------------------------------------------------------------
# split_hour_to_quarters
# ---------------------------------------------------------------------------


class TestSplitHourToQuarters:
    """One hourly row of change=1.0 → four quarters each 0.25."""

    def test_single_hour_produces_four_quarters(self):
        dt = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        rows = [_row(dt, 1.0)]
        result = split_hour_to_quarters(rows)
        assert len(result) == 4

    def test_quarter_values_are_correct(self):
        dt = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        rows = [_row(dt, 1.0)]
        result = split_hour_to_quarters(rows)
        base = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        assert result[base.replace(minute=0)] == pytest.approx(0.25)
        assert result[base.replace(minute=15)] == pytest.approx(0.25)
        assert result[base.replace(minute=30)] == pytest.approx(0.25)
        assert result[base.replace(minute=45)] == pytest.approx(0.25)

    def test_quarter_keys_are_utc_datetimes(self):
        dt = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        rows = [_row(dt, 1.0)]
        result = split_hour_to_quarters(rows)
        for key in result:
            assert key.tzinfo == timezone.utc

    def test_multiple_hours(self):
        dt1 = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        dt2 = datetime(2024, 3, 1, 11, 0, 0, tzinfo=timezone.utc)
        rows = [_row(dt1, 2.0), _row(dt2, 4.0)]
        result = split_hour_to_quarters(rows)
        assert len(result) == 8
        assert result[dt1.replace(minute=15)] == pytest.approx(0.5)
        assert result[dt2.replace(minute=30)] == pytest.approx(1.0)

    def test_change_none_treated_as_zero(self):
        dt = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        rows = [_row(dt, None)]
        result = split_hour_to_quarters(rows)
        assert len(result) == 4
        for v in result.values():
            assert v == pytest.approx(0.0)

    def test_empty_input_returns_empty_dict(self):
        result = split_hour_to_quarters([])
        assert result == {}


# ---------------------------------------------------------------------------
# merge_streams
# ---------------------------------------------------------------------------


def _make_quarter_stream(base: datetime, values: list[float]) -> dict[datetime, float]:
    """Build a per-quarter stream from a base timestamp and list of values."""
    return {
        base.replace(minute=i * 15): v for i, v in enumerate(values)
    }


class TestMergeStreams:
    """merge_streams combines per-stream dicts into Wolta PUT rows."""

    def _base(self) -> datetime:
        return datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

    def test_basic_merge_produces_rows(self):
        b = self._base()
        batt_in = _make_quarter_stream(b, [1.0, 0.0, 0.5, 0.0])
        batt_out = _make_quarter_stream(b, [0.0, 2.0, 0.0, 1.5])
        grid_in = _make_quarter_stream(b, [0.5, 0.0, 0.3, 0.0])
        grid_out = _make_quarter_stream(b, [0.0, 1.0, 0.0, 0.5])
        solar = _make_quarter_stream(b, [0.3, 0.0, 0.2, 0.1])
        rows = merge_streams(batt_in, batt_out, grid_in, grid_out, solar)
        assert len(rows) == 4

    def test_row_has_correct_keys(self):
        b = self._base()
        batt_in = _make_quarter_stream(b, [1.0])
        batt_out = _make_quarter_stream(b, [0.0])
        grid_in = _make_quarter_stream(b, [0.5])
        grid_out = _make_quarter_stream(b, [0.0])
        solar = _make_quarter_stream(b, [0.3])
        rows = merge_streams(batt_in, batt_out, grid_in, grid_out, solar)
        row = rows[0]
        assert "ts" in row
        assert "batt_charged_kwh" in row
        assert "batt_discharged_kwh" in row
        assert "solar_kwh" in row
        assert "grid_import_kwh" in row
        assert "grid_export_kwh" in row

    def test_ts_is_utc_iso_string(self):
        b = self._base()
        batt_in = {b: 1.0}
        batt_out = {b: 0.0}
        grid_in = {b: 0.5}
        grid_out = {b: 0.0}
        solar = {b: 0.3}
        rows = merge_streams(batt_in, batt_out, grid_in, grid_out, solar)
        ts = rows[0]["ts"]
        # Must be parseable as ISO and contain UTC offset
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0.0

    def test_missing_solar_fills_zero(self):
        """When solar stream is empty (or None), solar_kwh must be 0.0."""
        b = self._base()
        batt_in = {b: 1.0}
        batt_out = {b: 0.0}
        grid_in = {b: 0.5}
        grid_out = {b: 0.0}
        solar: dict[datetime, float] = {}
        rows = merge_streams(batt_in, batt_out, grid_in, grid_out, solar)
        assert len(rows) == 1
        assert rows[0]["solar_kwh"] == pytest.approx(0.0)

    def test_missing_solar_none_fills_zero(self):
        """Passing None as solar stream → solar_kwh = 0.0 (no crash)."""
        b = self._base()
        batt_in = {b: 1.0}
        batt_out = {b: 0.0}
        grid_in = {b: 0.5}
        grid_out = {b: 0.0}
        rows = merge_streams(batt_in, batt_out, grid_in, grid_out, None)
        assert len(rows) == 1
        assert rows[0]["solar_kwh"] == pytest.approx(0.0)

    def test_quarter_without_battery_data_is_skipped(self):
        """A timestamp where both batt_in and batt_out are absent is excluded."""
        b = self._base()
        # batt_in and batt_out only have 10:00 entry; grid has 10:00 and 10:15
        batt_in = {b.replace(minute=0): 1.0}
        batt_out = {b.replace(minute=0): 0.0}
        grid_in = {b.replace(minute=0): 0.5, b.replace(minute=15): 0.3}
        grid_out = {b.replace(minute=0): 0.0, b.replace(minute=15): 0.1}
        solar: dict[datetime, float] = {}
        rows = merge_streams(batt_in, batt_out, grid_in, grid_out, solar)
        # Only the 10:00 row should appear (10:15 has no battery data)
        assert len(rows) == 1
        ts_parsed = datetime.fromisoformat(rows[0]["ts"])
        assert ts_parsed.replace(tzinfo=timezone.utc) == b.replace(minute=0) or ts_parsed == b.replace(minute=0)

    def test_row_values_are_correct(self):
        b = self._base()
        batt_in = {b: 1.2}
        batt_out = {b: 0.8}
        grid_in = {b: 0.5}
        grid_out = {b: 0.3}
        solar = {b: 0.9}
        rows = merge_streams(batt_in, batt_out, grid_in, grid_out, solar)
        row = rows[0]
        assert row["batt_charged_kwh"] == pytest.approx(1.2)
        assert row["batt_discharged_kwh"] == pytest.approx(0.8)
        assert row["grid_import_kwh"] == pytest.approx(0.5)
        assert row["grid_export_kwh"] == pytest.approx(0.3)
        assert row["solar_kwh"] == pytest.approx(0.9)

    def test_rows_sorted_by_ts(self):
        """Rows should be returned in chronological order."""
        b = self._base()
        batt_in = {b.replace(minute=30): 0.5, b.replace(minute=0): 1.0}
        batt_out = {b.replace(minute=30): 0.0, b.replace(minute=0): 0.0}
        grid_in = {}
        grid_out = {}
        solar = {}
        rows = merge_streams(batt_in, batt_out, grid_in, grid_out, solar)
        assert len(rows) == 2
        t0 = datetime.fromisoformat(rows[0]["ts"])
        t1 = datetime.fromisoformat(rows[1]["ts"])
        assert t0 < t1
