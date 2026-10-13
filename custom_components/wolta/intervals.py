"""Half-open UTC intervals for the per-quarter battery direction.

An interval is ``(start, end)`` covering ``start <= t < end``; ``None`` on either
side means open (minus/plus infinity). The stored form (config entry, Store) is a
list of ``[iso | None, iso | None]`` pairs; ``parse`` and ``dump`` convert to and
from it. Everything here is pure: no Home Assistant imports, no I/O.

Internally an open end is the datetime.min/max sentinel so ordering, merging and
the boundary sweep need no special cases; the sentinels are turned back into
``None`` on the way out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

Interval = tuple[datetime | None, datetime | None]

_UTC = timezone.utc
_MIN = datetime.min.replace(tzinfo=_UTC)
_MAX = datetime.max.replace(tzinfo=_UTC)
_HOUR = timedelta(hours=1)

_Pair = tuple[datetime, datetime]


def _lo(value: datetime | None) -> datetime:
    return _MIN if value is None else value


def _hi(value: datetime | None) -> datetime:
    return _MAX if value is None else value


def _out(pair: _Pair) -> Interval:
    start, end = pair
    return (None if start == _MIN else start, None if end == _MAX else end)


def _merge(pairs: list[_Pair]) -> list[_Pair]:
    """Sort, drop empty (start >= end) and merge overlapping or touching pairs."""
    merged: list[_Pair] = []
    for start, end in sorted(p for p in pairs if p[0] < p[1]):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _internal(intervals: list[Interval]) -> list[_Pair]:
    return _merge([(_lo(s), _hi(e)) for s, e in intervals])


def _parse_bound(raw: Any) -> tuple[bool, datetime | None]:
    """(ok, value). Only None or an ISO string with a UTC offset is accepted."""
    if raw is None:
        return True, None
    if not isinstance(raw, str):
        return False, None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return False, None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False, None  # a naive time is not an instant
    try:
        return True, parsed.astimezone(_UTC)
    except OverflowError:  # e.g. year 1 at +02:00 has no UTC representation
        return False, None


def parse(raw: Any) -> list[Interval]:
    """Stored form -> intervals in UTC. Any malformed value yields [] as a whole."""
    if not isinstance(raw, (list, tuple)):
        return []
    result: list[Interval] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return []
        ok_start, start = _parse_bound(item[0])
        ok_end, end = _parse_bound(item[1])
        if not (ok_start and ok_end):
            return []
        result.append((start, end))
    return result


def dump(intervals: list[Interval]) -> list[list[str | None]]:
    """Intervals -> stored form (ISO 8601 UTC strings, None for open)."""
    return [
        [None if b is None else b.astimezone(_UTC).isoformat() for b in (start, end)]
        for start, end in intervals
    ]


def normalize(intervals: list[Interval]) -> list[Interval]:
    """Sorted, merged, empty intervals dropped."""
    return [_out(p) for p in _internal(intervals)]


def sym_diff(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """The instants in exactly one of the two interval sets."""
    na, nb = _internal(a), _internal(b)
    points = sorted({p for pair in na + nb for p in pair})
    pieces: list[_Pair] = []
    for lo, hi in zip(points, points[1:]):
        # Between two consecutive boundaries each set is wholly in or wholly out.
        in_a = any(s <= lo and hi <= e for s, e in na)
        in_b = any(s <= lo and hi <= e for s, e in nb)
        if in_a != in_b:
            pieces.append((lo, hi))
    return [_out(p) for p in _merge(pieces)]


def contains(intervals: list[Interval], ts: datetime) -> bool:
    """Whether ts lies in any interval. ts must be timezone-aware."""
    return any(_lo(s) <= ts < _hi(e) for s, e in intervals)


def clip(
    intervals: list[Interval], lo: datetime, hi: datetime
) -> list[tuple[datetime, datetime]]:
    """Intersect with [lo, hi); the result has no open ends. lo and hi must be
    timezone-aware."""
    return _merge(
        [(max(s, lo), min(e, hi)) for s, e in _internal(intervals)]
    )


def hour_aligned(
    windows: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Floor each start and ceil each end to a whole UTC hour, then re-merge."""
    aligned: list[_Pair] = []
    for start, end in windows:
        start = start.astimezone(_UTC)
        end = end.astimezone(_UTC)
        floor_start = start.replace(minute=0, second=0, microsecond=0)
        floor_end = end.replace(minute=0, second=0, microsecond=0)
        aligned.append((floor_start, floor_end if floor_end == end else floor_end + _HOUR))
    return _merge(aligned)


def apply_invert(
    streams: dict[str, dict[datetime, float]], intervals: list[Interval]
) -> dict[str, dict[datetime, float]]:
    """Swap batt_in and batt_out for every quarter starting inside the intervals.

    Returns new dicts; the input is not modified. A quarter present in only one of
    the two streams moves to the other (a missing key swaps with a missing key).
    """
    out = {name: dict(values) for name, values in streams.items()}
    batt_in = streams.get("batt_in", {})
    batt_out = streams.get("batt_out", {})
    if not intervals or not (batt_in or batt_out):
        return out
    new_in, new_out = dict(batt_in), dict(batt_out)
    for q in set(batt_in) | set(batt_out):
        if not contains(intervals, q):
            continue
        for target, value in ((new_in, batt_out.get(q)), (new_out, batt_in.get(q))):
            if value is None:
                target.pop(q, None)
            else:
                target[q] = value
    out["batt_in"], out["batt_out"] = new_in, new_out
    return out
