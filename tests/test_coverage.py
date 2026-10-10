"""Täckningsavstämningens rena logik (spec 2026-10-10 täckningsavstämning §5)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from custom_components.wolta import coverage

NOW = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
FP = coverage.coverage_token("tok")


def test_coverage_token_is_not_a_prefix_of_the_servers_token_hash():
    """Servern lagrar sha256(token) som token_hash; diagnostiken dumpar minnet publikt."""
    server_hash = hashlib.sha256(b"tok").hexdigest()
    assert len(FP) == 16
    assert not server_hash.startswith(FP)
    assert coverage.coverage_token("tok") == FP          # deterministisk
    assert coverage.coverage_token("other") != FP


def test_server_since_parses_iso_utc():
    assert coverage.server_since({"data_since": "2026-08-04T00:00:00+00:00"}) == datetime(
        2026, 8, 4, tzinfo=timezone.utc)


def test_server_since_null_means_no_rows():
    assert coverage.server_since({"data_since": None}) is None


def test_server_since_missing_for_old_backend_or_unexpected_payload():
    assert coverage.server_since({}) is coverage.MISSING
    assert coverage.server_since(None) is coverage.MISSING
    assert coverage.server_since("x") is coverage.MISSING
    assert coverage.server_since({"data_since": "not a date"}) is coverage.MISSING
    assert coverage.server_since({"data_since": "2026-08-04T00:00:00"}) is coverage.MISSING  # naiv
    assert coverage.server_since({"data_since": 5}) is coverage.MISSING


def test_needs_fill_without_memo_or_with_other_token():
    since = NOW - timedelta(days=3)
    assert coverage.needs_fill(None, FP, since) is True
    assert coverage.needs_fill({"token": "other", "since": since.isoformat()}, FP, since) is True


def test_needs_fill_when_server_lost_leading_data():
    memo = coverage.memo(FP, NOW - timedelta(days=60))
    assert coverage.needs_fill(memo, FP, NOW - timedelta(days=59)) is True
    assert coverage.needs_fill(memo, FP, None) is True          # servern blev tom


def test_no_fill_when_server_unchanged_or_gained_data():
    memo = coverage.memo(FP, NOW - timedelta(days=60))
    assert coverage.needs_fill(memo, FP, NOW - timedelta(days=60)) is False
    assert coverage.needs_fill(memo, FP, NOW - timedelta(days=90)) is False
    empty = coverage.memo(FP, None)
    assert coverage.needs_fill(empty, FP, None) is False
    assert coverage.needs_fill(empty, FP, NOW - timedelta(days=5)) is False


def test_corrupt_memo_since_triggers_one_fill():
    assert coverage.needs_fill({"token": FP, "since": "garbage"}, FP, NOW) is True


def test_gap_for_ends_at_server_since_or_now():
    since = NOW - timedelta(hours=2)
    assert coverage.gap_for(since, NOW, 365) == coverage.Gap(NOW - timedelta(days=365), since)
    assert coverage.gap_for(None, NOW, 365) == coverage.Gap(NOW - timedelta(days=365), NOW)


def test_gap_for_is_none_when_server_covers_the_window():
    assert coverage.gap_for(NOW - timedelta(days=400), NOW, 365) is None
    assert coverage.gap_for(NOW - timedelta(days=365), NOW, 365) is None


def test_memo_shape():
    since = NOW - timedelta(days=1)
    assert coverage.memo(FP, since) == {"token": FP, "since": since.isoformat()}
    assert coverage.memo(FP, None) == {"token": FP, "since": None}


def test_within_keeps_only_intervals_that_end_before_end():
    end = datetime(2026, 10, 10, 15, 30, tzinfo=timezone.utc)
    flows = [{"ts": (end - timedelta(minutes=15)).isoformat()},   # 15:15–15:30 – med
             {"ts": end.isoformat()}]                               # 15:30 – utanför
    assert coverage.within(flows, end) == flows[:1]
    soc = [{"ts": "2026-10-10T15:00:00+00:00", "period_s": 3600},   # timrad förbi end – bort
           {"ts": "2026-10-10T15:00:00+00:00", "period_s": 900}]    # kvart före end – med
    assert coverage.within(soc, end) == soc[1:]


def _flow(ts: datetime) -> dict:
    return {"ts": ts.isoformat()}


def _soc(ts: datetime, period_s: int = 900) -> dict:
    return {"unit": "sensor.soc", "ts": ts.isoformat(), "period_s": period_s}


def test_permanent_refusal_is_an_explicit_list():
    for status in (400, 403, 409, 413, 422):
        assert coverage.is_permanent_refusal(status) is True
    for status in (None, 401, 404, 408, 425, 429, 500, 502, 503):
        assert coverage.is_permanent_refusal(status) is False


def test_fill_slices_without_soc_are_newest_first_and_full_from_the_newest():
    t0 = NOW - timedelta(days=30)
    rows = [_flow(t0 + timedelta(minutes=15 * i)) for i in range(10)]
    slices = coverage.fill_slices(rows, [], 4)
    assert [flows for flows, _ in slices] == [rows[6:10], rows[2:6], rows[0:2]]
    assert all(soc == [] for _, soc in slices)


def test_fill_slices_count_soc_against_the_limit_and_assign_by_start():
    t0 = NOW - timedelta(days=30)
    t = [t0 + timedelta(minutes=15 * i) for i in range(8)]
    rows = [_flow(x) for x in t]
    before_first = _soc(t[0] - timedelta(minutes=15))
    in_old_a, in_old_b = _soc(t[1]), _soc(t[3])
    in_new_a, in_new_b = _soc(t[4]), _soc(t[7])
    soc = [in_new_b, before_first, in_old_a, in_new_a, in_old_b]   # not globally sorted
    slices = coverage.fill_slices(rows, soc, 4)
    assert slices == [
        (rows[5:8], [in_new_b]),
        (rows[3:5], [in_old_b, in_new_a]),
        (rows[1:3], [in_old_a]),
        (rows[0:1], [before_first]),
    ]
    assert all(len(f) + len(s) <= 4 for f, s in slices)
    assert sorted(r["ts"] for _, s in slices for r in s) == sorted(r["ts"] for r in soc)


def test_fill_slices_hourly_soc_goes_with_the_flow_row_it_starts_at():
    t0 = (NOW - timedelta(days=30)).replace(minute=0, second=0, microsecond=0)
    rows = [_flow(t0 + timedelta(minutes=15 * i)) for i in range(8)]
    hour0, hour1 = _soc(t0, 3600), _soc(t0 + timedelta(hours=1), 3600)
    slices = coverage.fill_slices(rows, [hour0, hour1], 100)
    assert slices == [(rows, [hour0, hour1])]


def test_fill_slices_oversized_single_row_still_ships():
    """En enda flödesrad med fler SoC-rader än gränsen blir en egen (för stor) skiva –
    put_data delar då upp SoC i egna bitar."""
    t0 = NOW - timedelta(days=30)
    rows = [_flow(t0), _flow(t0 + timedelta(minutes=15))]
    many = [_soc(t0 + timedelta(minutes=15) + timedelta(seconds=i)) for i in range(5)]
    slices = coverage.fill_slices(rows, many, 4)
    assert slices == [([rows[1]], many), ([rows[0]], [])]


def test_fill_slices_empty_rows():
    assert coverage.fill_slices([], [], 4) == []
