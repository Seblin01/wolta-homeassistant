"""Tests for custom_components/wolta/stats.py (TDD – write first, then implement)."""

from __future__ import annotations

import calendar
import random
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.wolta import stats
from custom_components.wolta.stats import (
    aggregate_5min_to_15min,
    merge_streams,
    split_hour_to_quarters,
    sum_quarter_dicts,
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

    def test_negative_noise_is_floored_to_zero(self):
        """Recorder `change` can be slightly negative (FP noise / meter reset). The
        backend rejects negatives (422) → merge_streams must floor all fields to 0."""
        b = self._base()
        batt_in = {b: -0.024999999999636202}   # the exact value from the 413/422 incident
        batt_out = {b: -0.001}
        grid_in = {b: -5.0}                     # a larger negative is also floored
        grid_out = {b: 0.3}
        solar = {b: -0.0}
        rows = merge_streams(batt_in, batt_out, grid_in, grid_out, solar)
        assert len(rows) == 1
        r = rows[0]
        assert r["batt_charged_kwh"] == 0.0
        assert r["batt_discharged_kwh"] == 0.0
        assert r["grid_import_kwh"] == 0.0
        assert r["grid_export_kwh"] == pytest.approx(0.3)   # positive value untouched
        assert r["solar_kwh"] == 0.0
        # every field must be >= 0 (the backend's DataRow contract)
        for k in ("batt_charged_kwh", "batt_discharged_kwh", "solar_kwh",
                  "grid_import_kwh", "grid_export_kwh"):
            assert r[k] >= 0.0

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

    def test_row_exceeding_backend_cap_is_dropped(self):
        """A quarter where any field exceeds the backend's 500 kWh cap (DataRow
        le=500) is dropped entirely – uploading it would 422 the whole batch.
        Typical causes: Wh-scaled statistics (pre-units-fix) or a meter reset
        producing a huge `change` spike."""
        b = self._base()
        batt_in = {b: 600.0, b.replace(minute=15): 1.0}   # 600 = e.g. 0.6 kWh in Wh
        batt_out = {b: 0.0, b.replace(minute=15): 0.5}
        rows = merge_streams(batt_in, batt_out, {}, {}, {})
        assert len(rows) == 1
        assert rows[0]["batt_charged_kwh"] == pytest.approx(1.0)

    def test_row_exceeding_cap_in_any_field_is_dropped(self):
        """The cap applies to every field, not just the battery streams."""
        b = self._base()
        batt_in = {b: 1.0}
        batt_out = {b: 0.0}
        grid_in = {b: 12345.0}   # meter-reset spike on grid import
        rows = merge_streams(batt_in, batt_out, grid_in, {}, {})
        assert rows == []

    def test_row_at_cap_boundary_is_kept(self):
        """Exactly 500 satisfies the backend's le=500 constraint → keep."""
        b = self._base()
        batt_in = {b: 500.0}
        batt_out = {b: 0.0}
        rows = merge_streams(batt_in, batt_out, {}, {}, {})
        assert len(rows) == 1
        assert rows[0]["batt_charged_kwh"] == pytest.approx(500.0)

    def test_dropped_rows_log_one_warning_with_count(self, caplog):
        """Dropped rows must be visible in the HA log (one warning, with count)."""
        import logging

        b = self._base()
        batt_in = {b: 600.0, b.replace(minute=15): 700.0, b.replace(minute=30): 1.0}
        batt_out = {b: 0.0, b.replace(minute=15): 0.0, b.replace(minute=30): 0.0}
        with caplog.at_level(logging.WARNING, logger="custom_components.wolta.stats"):
            rows = merge_streams(batt_in, batt_out, {}, {}, {})
        assert len(rows) == 1
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "2" in warnings[0].getMessage()

    def test_no_warning_when_nothing_dropped(self, caplog):
        import logging

        b = self._base()
        with caplog.at_level(logging.WARNING, logger="custom_components.wolta.stats"):
            merge_streams({b: 1.0}, {b: 0.0}, {}, {}, {})
        assert not [r for r in caplog.records if r.levelname == "WARNING"]


# ---------------------------------------------------------------------------
# async_fetch_change – unit normalisation
# ---------------------------------------------------------------------------


class TestFetchChangeUnits:
    """async_fetch_change must ask the recorder to convert energy statistics
    to kWh. Without units={"energy": "kWh"} a sensor whose statistics are
    stored in Wh uploads values 1000× too large → deterministic 422 from the
    backend (DataRow le=500) and silently lost data (seen in prod 2026-07-05/06)."""

    @pytest.mark.asyncio
    async def test_statistics_requested_in_kwh(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.wolta import stats as stats_mod

        captured: dict = {}

        async def _fake_executor_job(fn, *args):
            captured["args"] = args
            return {}

        instance = MagicMock()
        instance.async_add_executor_job = AsyncMock(side_effect=_fake_executor_job)

        import homeassistant.components.recorder as recorder_mod

        monkeypatch.setattr(recorder_mod, "get_instance", lambda hass: instance)

        hass = MagicMock()
        start = datetime(2024, 3, 1, tzinfo=timezone.utc)
        result = await stats_mod.async_fetch_change(
            hass, {"sensor.batt_in"}, start, None, "5minute"
        )
        assert result == {}
        # statistics_during_period(hass, start, end, ids, period, units, types)
        args = captured["args"]
        assert args[5] == {"energy": "kWh"}, (
            f"units argument must request kWh conversion, got {args[5]!r}"
        )


# ---------------------------------------------------------------------------
# sum_quarter_dicts
# ---------------------------------------------------------------------------


class TestSumQuarterDicts:
    """sum_quarter_dicts merges multiple per-15-min-bucket dicts by summing values."""

    def _base(self) -> datetime:
        return datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

    def test_empty_list_returns_empty_dict(self):
        result = sum_quarter_dicts([])
        assert result == {}

    def test_single_dict_returned_as_is(self):
        b = self._base()
        d = {b: 1.5, b.replace(minute=15): 2.0}
        result = sum_quarter_dicts([d])
        assert result[b] == pytest.approx(1.5)
        assert result[b.replace(minute=15)] == pytest.approx(2.0)

    def test_two_dicts_overlapping_timestamps_summed(self):
        b = self._base()
        d1 = {b: 1.0, b.replace(minute=15): 2.0}
        d2 = {b: 3.0, b.replace(minute=30): 0.5}
        result = sum_quarter_dicts([d1, d2])
        assert result[b] == pytest.approx(4.0)                        # overlapping: 1.0 + 3.0
        assert result[b.replace(minute=15)] == pytest.approx(2.0)     # only in d1
        assert result[b.replace(minute=30)] == pytest.approx(0.5)     # only in d2

    def test_disjoint_timestamps_collected(self):
        b = self._base()
        d1 = {b: 1.0}
        d2 = {b.replace(minute=15): 2.0}
        result = sum_quarter_dicts([d1, d2])
        assert len(result) == 2
        assert result[b] == pytest.approx(1.0)
        assert result[b.replace(minute=15)] == pytest.approx(2.0)

    def test_three_dicts_all_overlapping(self):
        b = self._base()
        d1 = {b: 0.5}
        d2 = {b: 0.5}
        d3 = {b: 0.5}
        result = sum_quarter_dicts([d1, d2, d3])
        assert result[b] == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# analyze_battery_history (auto-prefill vid setup: eff/purchase_date/invert)
# ---------------------------------------------------------------------------

from custom_components.wolta.stats import analyze_battery_history

_NOW = datetime(2026, 7, 10, tzinfo=timezone.utc)
_OLD = _NOW - timedelta(days=365)


def test_analysis_suggests_eff_and_date():
    out = analyze_battery_history(1000.0, 880.0, _OLD, _NOW)
    assert out["eff"] == 0.88
    assert out["purchase_date"] == _OLD.date().isoformat()
    assert out["invert_suspected"] is False


def test_analysis_detects_inverted_sensors():
    out = analyze_battery_history(880.0, 1000.0, _OLD, _NOW)  # ur > in → omkastat
    assert out["invert_suspected"] is True
    assert out["eff"] == 0.88  # speglad kvot = eff med rättvända sensorer


def test_analysis_requires_enough_history():
    recent = _NOW - timedelta(days=10)
    out = analyze_battery_history(1000.0, 880.0, recent, _NOW)
    assert out["eff"] is None                                  # < 60 dagar
    assert out["purchase_date"] == recent.date().isoformat()   # datum föreslås ändå
    out = analyze_battery_history(50.0, 44.0, _OLD, _NOW)
    assert out["eff"] is None                                  # < 100 kWh laddat


def test_analysis_clamps_eff():
    out = analyze_battery_history(1000.0, 300.0, _OLD, _NOW)  # kvot 0.3 → orimlig
    assert out["eff"] == 0.5


def test_analysis_no_history():
    out = analyze_battery_history(0.0, 0.0, None, _NOW)
    assert out == {"eff": None, "purchase_date": None, "invert_suspected": False}


# ---------------------------------------------------------------------------
# flagged_quarters (external control: any-overlap rule)
# ---------------------------------------------------------------------------


def test_flagged_quarters_nagon_del_regeln():
    """En session :07-:19 spanner tva kvarter - bada flaggas (spec B4)."""
    t = lambda m: datetime(2026, 8, 1, 10, m, tzinfo=timezone.utc)  # noqa: E731
    points = [(t(0), "off"), (t(7), "on"), (t(19), "off")]
    q = stats.flagged_quarters(points, end=t(30))
    assert q == {t(0), t(15)}


def test_flagged_quarters_unavailable_ar_off():
    t = lambda m: datetime(2026, 8, 1, 10, m, tzinfo=timezone.utc)  # noqa: E731
    points = [(t(0), "unavailable"), (t(5), "unknown")]
    assert stats.flagged_quarters(points, end=t(30)) == set()


def test_flagged_quarters_on_till_fonsterslut():
    t = lambda m: datetime(2026, 8, 1, 10, m, tzinfo=timezone.utc)  # noqa: E731
    points = [(t(20), "on")]
    assert stats.flagged_quarters(points, end=t(50)) == {t(15), t(30), t(45)}


def test_flagged_quarters_klampas_till_fonsterstart():
    """F4: the backfill path is the only caller with an UNALIGNED start
    (now - 365 days). The start-time-state row from include_start_time_state
    carries its own (much older) timestamp, which floors to the quarter BEFORE
    the window - a quarter the caller never asked about, uploaded as an all-zero
    row that overwrites whatever the previous backfill stored there."""
    start = datetime(2026, 8, 1, 13, 52, 17, tzinfo=timezone.utc)
    end = datetime(2026, 8, 1, 14, 30, tzinfo=timezone.utc)
    points = [(datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc), "on")]

    q = stats.flagged_quarters(points, end=end, start=start)

    assert datetime(2026, 8, 1, 13, 45, tzinfo=timezone.utc) not in q
    assert q == {
        datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 14, 15, tzinfo=timezone.utc),
    }


def _flagged_quarters_reference(points, end, start=None):
    """Naive reference: walk every quarter from the floored point and discard the ones
    before `start`. The implementation skips ahead instead (a long-held 'on' yields a
    point whose last_changed can be years old); this pins the two to the same output."""
    out = set()
    for i, (ts, state) in enumerate(points):
        if state != "on":
            continue
        stop = points[i + 1][0] if i + 1 < len(points) else end
        unix = int(ts.timestamp())
        q = datetime.fromtimestamp(unix - unix % 900, tz=timezone.utc)
        while q < stop:
            if start is None or q >= start:
                out.add(q)
            q += timedelta(seconds=900)
    return out


def test_flagged_quarters_matchar_naiv_referens():
    """The skip-ahead must be a pure optimisation: identical output on aligned and
    unaligned starts, on no start at all, and on a point far outside the window."""
    rnd = random.Random(11)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for case in range(200):
        n = rnd.randrange(1, 6)
        offsets = sorted(rnd.randrange(0, 6 * 3600) for _ in range(n))
        points = [
            (base + timedelta(seconds=o), rnd.choice(["on", "off", "unavailable"]))
            for o in offsets
        ]
        end = base + timedelta(seconds=6 * 3600 + rnd.randrange(0, 3600))
        for start in (
            None,
            base,                                             # aligned
            base + timedelta(seconds=rnd.randrange(0, 7200)),  # unaligned
            base - timedelta(days=900),                        # far before the points
        ):
            assert stats.flagged_quarters(points, end, start=start) == \
                _flagged_quarters_reference(points, end, start), f"case {case}"


def test_flagged_quarters_gammal_punkt_ger_bara_fonstrets_kvarter():
    """A sensor 'on' since long before the window: the retained state point carries a
    years-old last_changed, but only the window's own quarters may come back."""
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    ancient = start - timedelta(days=900)

    q = stats.flagged_quarters([(ancient, "on")], end, start=start)

    assert q == {start + timedelta(minutes=m) for m in (0, 15, 30, 45)}


def test_flagged_quarters_aligned_start_behaller_startkvarteret():
    """Heal/incremental starts come from the bookmark and are quarter boundaries -
    clamping must not eat the first quarter there."""
    t = lambda m: datetime(2026, 8, 1, 10, m, tzinfo=timezone.utc)  # noqa: E731
    points = [(t(0), "on")]
    assert stats.flagged_quarters(points, end=t(30), start=t(0)) == {t(0), t(15)}


# ---------------------------------------------------------------------------
# merge_streams – external control flagging
# ---------------------------------------------------------------------------


def test_merge_streams_flaggar_och_emitterar_vilokvarter():
    qt1 = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    qt2 = datetime(2026, 8, 1, 10, 15, tzinfo=timezone.utc)
    # qt2 ar tvingad vila: batteriet star still, men sensorn ar INSPELAD, sa recordern
    # kompilerar en rad med change=0.0 och kvarten ar redan en nyckel i batt_in.
    # (Tidigare bar detta test den falska premissen att en vilokvart SAKNAR
    # batteristatistik och darfor maste slappas in pa natstrommens bevis - se
    # test_merge_streams_flagga_utan_batteristatistik_fabricerar_inga_rader.)
    rows = stats.merge_streams(
        batt_in={qt1: 0.5, qt2: 0.0}, batt_out={}, grid_in={qt2: 0.2}, grid_out={},
        solar={}, external={qt1, qt2})
    assert [r["ts"] for r in rows] == [qt1.isoformat(), qt2.isoformat()]
    assert all(r["external_control"] is True for r in rows)
    assert rows[1]["grid_import_kwh"] == 0.2


def test_merge_streams_utan_external_ar_ofdrandrad():
    qt1 = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    rows = stats.merge_streams(batt_in={qt1: 0.5}, batt_out={}, grid_in={}, grid_out={},
                               solar={}, external=None)
    assert rows[0]["external_control"] is False


# ---------------------------------------------------------------------------
# merge_streams – a flagged quarter needs COMPILED statistics (F1 + C1)
#
# HA only compiles statistics for completed periods AND only for periods the
# recorder was actually running, while the flag - read from the recorder's STATES
# table - is held forward from the last known state point and can therefore span
# arbitrary stretches with no statistics at all (HA down, purge horizon, the
# start-time-state row). A flagged quarter emitted where nothing was compiled
# uploads real energy as 0.0 and drags the coordinator's bookmark
# (rows[-1]["ts"]) past it, so the next cycle never re-reads it: the zeros stick
# server-side forever and feed economy + observed_* parameters.
#
# The rule is one line: emit a flagged quarter only when that quarter exists as a
# key in at least one of the five streams - proof the recorder compiled it. This
# closes the gap ABOVE the newest compiled quarter and every gap INSIDE the range
# with the same test; a global max/frontier only closed the former (C1).
# ---------------------------------------------------------------------------


def _q(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 1, hour, minute, tzinfo=timezone.utc)


_QS = [datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * i)
       for i in range(48)]


def test_merge_streams_flaggat_kvarter_bortom_sista_statistiken_emitteras_ej():
    """The heal scenario: LTS reaches 11:45, the flag reaches 12:45.

    Quarters 12:00-12:45 must NOT be emitted - they would upload as all-zero rows
    and move the bookmark to 12:45, permanently zeroing the real charge/discharge
    that the recorder had not yet compiled.
    """
    newest = _q(11, 45)
    batt_in = {_q(11, 30): 0.4, newest: 0.6}
    external = {_q(11, 30), newest, _q(12, 0), _q(12, 15), _q(12, 30), _q(12, 45)}

    rows = stats.merge_streams(
        batt_in=batt_in, batt_out={}, grid_in={}, grid_out={}, solar={},
        external=external,
    )

    assert [r["ts"] for r in rows] == [_q(11, 30).isoformat(), newest.isoformat()]
    # The bookmark the coordinator would persist must not pass the real data.
    assert rows[-1]["ts"] == newest.isoformat()


def test_merge_streams_flaggat_kvarter_i_inre_lucka_emitteras_ej():
    """C1: a flagged quarter INSIDE the compiled range but with no statistics of
    its own must be dropped too.

    A global max frontier passed these - they sit below the newest compiled quarter
    - and emitted them with all five energy fields at 0.0. That is the same F1
    failure moved from above the frontier to inside it. Here the recorder compiled
    10:00 and 11:00 and nothing between (HA was down, or the states are older than
    the purge horizon).
    """
    batt_in = {_q(10, 0): 0.4, _q(11, 0): 0.5}
    external = {_q(10, 0), _q(10, 15), _q(10, 30), _q(10, 45), _q(11, 0)}

    rows = stats.merge_streams(
        batt_in=batt_in, batt_out={}, grid_in={}, grid_out={}, solar={},
        external=external,
    )

    assert [r["ts"] for r in rows] == [_q(10, 0).isoformat(), _q(11, 0).isoformat()]


def test_merge_streams_hallen_on_over_recorder_lucka_ger_bara_kompilerade_kvarter():
    """The real-world shape of C1, driven through flagged_quarters like production.

    async_fetch_states returns `last_changed`, and the include_start_time_state row
    carries an OLD one, so a single stale 'on' row holds the flag across the whole
    heal window (>= 9 days by construction). Only the quarters the recorder actually
    compiled may be emitted - otherwise two weeks of real operation upload as zeros
    and get neutralised in the grade on the strength of one old state row.
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    start = now - timedelta(days=14)
    compiled_hour = now - timedelta(hours=2)

    flagged = stats.flagged_quarters([(start, "on")], now, start=start)
    assert len(flagged) > 1000, "the held-on flag must really span the window"

    batt_in = stats.split_hour_to_quarters(
        [{"start": compiled_hour.timestamp(), "change": 1.2}]
    )
    rows = stats.merge_streams(batt_in, {}, {}, {}, {}, external=flagged)

    assert [r["ts"] for r in rows] == sorted(k.isoformat() for k in batt_in)
    assert len(rows) == 4, f"expected only the one compiled hour, got {len(rows)}"
    # The ts list above is the real guard. The all-zero check below is a PROXY for it,
    # not the invariant: an all-zero row is not forbidden in itself - a genuinely
    # compiled quarter in which all five sensors moved by exactly 0.0 is legitimate and
    # must upload as zeros. It only bites here because this fixture's compiled quarters
    # carry non-zero values, so any zero row could only have come from an uncompiled
    # one. Do not turn it into a general rule.
    zeroed = [
        r for r in rows
        if all(v == 0.0 for k, v in r.items() if k not in ("ts", "external_control"))
    ]
    assert zeroed == [], f"{len(zeroed)} all-zero rows would zero real energy"


def test_merge_streams_tvingad_vila_har_egen_batteristatistik_och_emitteras():
    """Forced idle needs NO special case: a held-still battery is still a RECORDED
    sensor, so the recorder compiles a row with change=0.0 and the quarter is already
    a key in batt_in/batt_out. It was emitted before this branch existed and still is.

    This is the premise the 'any stream may prove it' rule was built on, and it was
    wrong - see test_merge_streams_flagga_kan_aldrig_lagga_till_rader.
    """
    rows = stats.merge_streams(
        batt_in={_q(10, 0): 0.5, _q(10, 15): 0.0, _q(10, 30): 0.0}, batt_out={},
        grid_in={_q(10, 0): 0.1, _q(10, 15): 0.3, _q(10, 30): 0.25}, grid_out={},
        solar={},
        external={_q(10, 15), _q(10, 30)},
    )

    assert [r["ts"] for r in rows] == [
        _q(10, 0).isoformat(), _q(10, 15).isoformat(), _q(10, 30).isoformat()
    ]
    idle = rows[1]
    assert idle["external_control"] is True
    assert idle["batt_charged_kwh"] == 0.0
    assert idle["batt_discharged_kwh"] == 0.0
    assert idle["grid_import_kwh"] == 0.3


def test_merge_streams_flagga_utan_batteristatistik_fabricerar_inga_rader():
    """The battery sensor is missing for these quarters - unavailable, not yet
    installed, or not recorded - while the grid meter has a full year of history.

    Emitting the flagged quarters on the strength of the GRID stream writes
    batt_charged = batt_discharged = 0.0 for quarters in which the battery was never
    observed. That 0.0 is not a measurement, it is a fabrication, and it lands
    server-side where nothing was uploaded before.
    """
    rows = stats.merge_streams(
        batt_in={_q(11, 0): 0.4}, batt_out={}, grid_in={
            _q(9, 0): 0.2, _q(9, 15): 0.2, _q(9, 30): 0.2, _q(9, 45): 0.2,
            _q(10, 0): 0.2, _q(11, 0): 0.3,
        }, grid_out={}, solar={},
        external={_q(9, 0), _q(9, 15), _q(9, 30), _q(9, 45), _q(10, 0), _q(11, 0)},
    )

    assert [r["ts"] for r in rows] == [_q(11, 0).isoformat()], (
        "only the quarter the battery was actually recorded in"
    )


def test_merge_streams_flagga_kan_aldrig_lagga_till_rader():
    """The invariant that replaces three rounds of reformulation: the flag MARKS
    already-valid quarters and can never add one.

    Valid quarters are exactly the battery timestamps, with or without a flag. Any
    rule that lets a non-battery stream admit a flagged quarter necessarily emits
    rows whose battery fields are fabricated zeros.
    """
    marked = 0
    for ext_density in (0.0, 0.3, 1.0):
        for seed in range(60):
            rnd = random.Random((seed, ext_density).__hash__())
            def mk(p):
                return {q: round(rnd.uniform(0, 3), 3)
                        for q in _QS if rnd.random() < p}
            a, b, c, d, e = mk(0.3), mk(0.3), mk(0.6), mk(0.4), mk(0.5)
            ext = {q for q in _QS if rnd.random() < ext_density}

            with_flag = stats.merge_streams(a, b, c, d, e, external=ext)
            without = stats.merge_streams(a, b, c, d, e, external=None)

            assert [r["ts"] for r in with_flag] == [r["ts"] for r in without], (
                "the flag changed WHICH quarters are emitted"
            )
            marked += sum(1 for r in with_flag if r["external_control"])
    assert marked > 0, "the flag must still mark rows, or this proves nothing"


def test_merge_streams_flagga_utan_nagon_statistik_ger_inga_rader():
    """No statistics at all -> nothing to mark, nothing emitted."""
    rows = stats.merge_streams(
        batt_in={}, batt_out={}, grid_in={}, grid_out={}, solar={},
        external={_q(10, 0), _q(10, 15)},
    )
    assert rows == []


# ---------------------------------------------------------------------------
# async_fetch_states – recorder STATES reader (states table, not statistics)
# ---------------------------------------------------------------------------


class TestFetchStates:
    """async_fetch_states reads the recorder's states table (short retention,
    purge_keep_days) via history.get_significant_states through the executor."""

    @pytest.mark.asyncio
    async def test_reads_states_via_executor(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        captured: dict = {}

        async def _fake_executor_job(fn, *args):
            captured["fn"] = fn
            return fn()

        instance = MagicMock()
        instance.async_add_executor_job = AsyncMock(side_effect=_fake_executor_job)

        import homeassistant.components.recorder as recorder_mod
        from homeassistant.components.recorder import history as history_mod

        monkeypatch.setattr(recorder_mod, "get_instance", lambda hass: instance)

        t0 = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 8, 1, 10, 7, tzinfo=timezone.utc)

        class _FakeState:
            def __init__(self, last_changed, state):
                self.last_changed = last_changed
                self.state = state

        fake_result = {
            "binary_sensor.flex": [
                _FakeState(t0, "off"),
                _FakeState(t1, "on"),
            ]
        }

        def _fake_get_significant_states(hass, start, end, entity_ids, **kwargs):
            captured["call_args"] = (start, end, entity_ids, kwargs)
            return fake_result

        monkeypatch.setattr(
            history_mod, "get_significant_states", _fake_get_significant_states
        )

        hass = MagicMock()
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 2, tzinfo=timezone.utc)
        result = await stats.async_fetch_states(hass, "binary_sensor.flex", start, end)

        # Guard the event-loop safety, not just the result: the recorder call must
        # go through the executor, never run directly on the event loop. Without
        # this assertion a regression that calls history.get_significant_states()
        # inline would still populate captured["call_args"] via the monkeypatch
        # and the test would pass despite blocking the loop.
        instance.async_add_executor_job.assert_awaited_once()
        assert callable(captured["fn"])

        assert result == [(t0, "off"), (t1, "on")]
        call_start, call_end, call_entity_ids, kwargs = captured["call_args"]
        assert call_start == start
        assert call_end == end
        assert call_entity_ids == ["binary_sensor.flex"]
        assert kwargs.get("include_start_time_state") is True
        assert kwargs.get("significant_changes_only") is False

    @pytest.mark.asyncio
    async def test_missing_entity_returns_empty_list(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        async def _fake_executor_job(fn, *args):
            return fn()

        instance = MagicMock()
        instance.async_add_executor_job = AsyncMock(side_effect=_fake_executor_job)

        import homeassistant.components.recorder as recorder_mod
        from homeassistant.components.recorder import history as history_mod

        monkeypatch.setattr(recorder_mod, "get_instance", lambda hass: instance)
        monkeypatch.setattr(
            history_mod, "get_significant_states", lambda *a, **k: {}
        )

        hass = MagicMock()
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 2, tzinfo=timezone.utc)
        result = await stats.async_fetch_states(hass, "binary_sensor.flex", start, end)
        assert result == []
