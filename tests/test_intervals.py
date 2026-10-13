"""Half-open UTC intervals for the battery direction (spec 2026-10-13 §9.3)."""
from datetime import datetime, timedelta, timezone

from custom_components.wolta import intervals as iv

UTC = timezone.utc
T = datetime(2026, 9, 28, 13, tzinfo=UTC)
H = timedelta(hours=1)


def test_parse_and_dump_roundtrip():
    raw = [[None, T.isoformat()], [(T + H).isoformat(), None]]
    assert iv.dump(iv.parse(raw)) == raw


def test_parse_invalid_is_empty():
    assert iv.parse("x") == [] and iv.parse([[1, 2]]) == [] and iv.parse(None) == []


def test_parse_rejects_naive_string():
    # No timezone means no unambiguous instant: reject the whole value.
    assert iv.parse([["2026-09-28T13:00:00", None]]) == []
    assert iv.parse([[None, "2026-09-28T13:00:00"]]) == []


def test_parse_rejects_non_string_and_malformed_items():
    assert iv.parse([[T, None]]) == []          # datetime objects are not the stored form
    assert iv.parse([["not-a-date", None]]) == []
    assert iv.parse([[None]]) == []
    assert iv.parse([[None, None, None]]) == []
    assert iv.parse({"a": 1}) == []


def test_parse_converts_offsets_to_utc():
    out = iv.parse([["2026-09-28T15:00:00+02:00", None]])
    assert out == [(T, None)]
    assert out[0][0].utcoffset() == timedelta(0)


def test_parse_interval_crossing_dst_change():
    # Europe/Stockholm falls back on 2026-10-25 (03:00 CEST -> 02:00 CET). The wall
    # clock span 00:30+02:00 .. 03:30+01:00 is four hours of real time, not three.
    raw = [["2026-10-25T00:30:00+02:00", "2026-10-25T03:30:00+01:00"]]
    (start, end), = iv.parse(raw)
    assert start == datetime(2026, 10, 24, 22, 30, tzinfo=UTC)
    assert end == datetime(2026, 10, 25, 2, 30, tzinfo=UTC)
    assert end - start == timedelta(hours=4)
    assert iv.dump([(start, end)]) == [["2026-10-24T22:30:00+00:00", "2026-10-25T02:30:00+00:00"]]
    # The repeated wall-clock hour (02:xx) is inside, one instant per offset.
    assert iv.contains([(start, end)], datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    assert iv.contains([(start, end)], datetime(2026, 10, 25, 1, 30, tzinfo=UTC))


def test_normalize_merges_and_sorts():
    a = [(T + H, T + 3 * H), (T, T + 2 * H), (T + 5 * H, T + 5 * H)]
    assert iv.normalize(a) == [(T, T + 3 * H)]


def test_normalize_merges_touching_and_keeps_open_ends():
    assert iv.normalize([(T, T + H), (T + H, None)]) == [(T, None)]
    assert iv.normalize([(None, T), (T, None)]) == [(None, None)]
    assert iv.normalize([(None, T), (T + H, None)]) == [(None, T), (T + H, None)]
    assert iv.normalize([]) == []


def test_sym_diff_open_ended():
    assert iv.sym_diff([], [(None, None)]) == [(None, None)]
    assert iv.sym_diff([(None, None)], [(None, None)]) == []
    assert iv.sym_diff([(None, None)], [(T, None)]) == [(None, T)]
    assert iv.sym_diff([(None, T)], [(T, None)]) == [(None, None)]


def test_sym_diff_partial_overlap():
    a = [(T, T + 3 * H)]
    b = [(T + 2 * H, T + 5 * H)]
    assert iv.sym_diff(a, b) == [(T, T + 2 * H), (T + 3 * H, T + 5 * H)]
    assert iv.sym_diff(a, a) == []
    assert iv.sym_diff(a, []) == a


def test_contains_half_open():
    assert iv.contains([(T, T + H)], T) and not iv.contains([(T, T + H)], T + H)
    assert iv.contains([(None, None)], T) and not iv.contains([], T)
    assert iv.contains([(None, T)], T - timedelta(seconds=1)) and not iv.contains([(None, T)], T)
    assert iv.contains([(T, None)], T) and not iv.contains([(T, None)], T - timedelta(seconds=1))


def test_clip_and_hour_aligned():
    lo, hi = T - 2 * H, T + 2 * H
    assert iv.clip([(None, T + timedelta(minutes=30))], lo, hi) == [(lo, T + timedelta(minutes=30))]
    assert iv.hour_aligned([(T + timedelta(minutes=15), T + timedelta(minutes=45))]) == [(T, T + H)]


def test_clip_drops_outside_and_empty():
    lo, hi = T, T + 2 * H
    assert iv.clip([(T - 3 * H, T - H), (T + 2 * H, None)], lo, hi) == []
    assert iv.clip([(None, None)], lo, hi) == [(lo, hi)]
    assert iv.clip([(T - H, T), (T + H, T + 3 * H)], lo, hi) == [(T + H, hi)]


def test_hour_aligned_remerges_and_keeps_aligned_ends():
    q = timedelta(minutes=15)
    out = iv.hour_aligned([(T + q, T + 2 * q), (T + 3 * q, T + H), (T + H, T + 2 * H)])
    assert out == [(T, T + 2 * H)]


def test_apply_invert_swaps_only_inside():
    q0, q1 = T - timedelta(minutes=15), T
    streams = {"batt_in": {q0: 1.0, q1: 2.0}, "batt_out": {q1: 0.5}, "solar": {q1: 3.0}}
    out = iv.apply_invert(streams, [(T, None)])
    assert out["batt_in"] == {q0: 1.0, q1: 0.5}
    assert out["batt_out"] == {q1: 2.0}
    assert out["solar"] == {q1: 3.0}
    assert streams["batt_in"][q1] == 2.0      # input untouched


def test_apply_invert_missing_key_swaps_with_missing_key():
    q0, q1 = T, T + timedelta(minutes=15)
    streams = {"batt_in": {q0: 1.0}, "batt_out": {q1: 4.0}}
    out = iv.apply_invert(streams, [(None, None)])
    assert out["batt_in"] == {q1: 4.0}
    assert out["batt_out"] == {q0: 1.0}


def test_apply_invert_no_intervals_is_identity():
    streams = {"batt_in": {T: 1.0}, "batt_out": {T: 2.0}}
    assert iv.apply_invert(streams, []) == streams
