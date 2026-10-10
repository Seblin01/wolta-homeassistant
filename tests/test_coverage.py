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
