"""Täckningsavstämning (spec 2026-10-10 täckningsavstämning, §5).

Servern är sanningskällan för vad som är lagrat. Klienten fyller bara intervallet FÖRE
serverns tidigaste lagrade intervall (`data_since`) och skriver därför aldrig över en rad
servern redan har. Varför inte "nollställ bokmärket vid nytt token": ett nytt token kan
betyda att servern ÅTERANVÄNDE den gamla raden med all strömmad data (reauth skickar
client_plant_id, servern om-onboardar) - en full backfill hade då skrivit om dag 10-365
med timstatistik ÷ 4 ovanpå kvartar ur 5-minutersstatistik som HA sedan rensat.

Rena funktioner utan Home Assistant; coordinatorn äger all I/O.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

# Skiljer minnets hash från serverns token_hash (= sha256(token)). Diagnostiken dumpar
# hela Store-tillståndet och hamnar i publika issues - ett prefix av databasens nyckel
# hör inte hemma där.
_TOKEN_DOMAIN: Final = "wolta-coverage:"
_FLOW_PERIOD_S: Final = 900


class _Missing:
    """Typen för MISSING: servern skickade inget (tolkningsbart) `data_since`."""

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Final = _Missing()


def coverage_token(token: str) -> str:
    """Binder minnet till profilen utan att lagra tokenet en gång till."""
    return hashlib.sha256((_TOKEN_DOMAIN + token).encode()).hexdigest()[:16]


def _parse(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def server_since(payload: Any) -> datetime | None | _Missing:
    """`data_since` ur ett profil- eller PUT-svar.

    MISSING när nyckeln saknas (äldre backend) eller värdet inte går att tolka som en
    tz-medveten tidpunkt - då görs ingen avstämning alls. None betyder att servern saknar
    rader."""
    if not isinstance(payload, dict) or "data_since" not in payload:
        return MISSING
    raw = payload["data_since"]
    if raw is None:
        return None
    parsed = _parse(raw)
    return MISSING if parsed is None else parsed


@dataclass(frozen=True)
class Gap:
    """Halvöppet intervall [start, end) som ska fyllas."""

    start: datetime
    end: datetime


def needs_fill(memo: Any, token_fp: str, since: datetime | None) -> bool:
    """Behövs en fyllning? Ja när minnet saknas eller hör till ett annat token (uppgradering,
    reauth), och när serverns tidigaste rad ligger SENARE än minnet säger (ledande data har
    försvunnit). Har servern fått data tidigare än minnet räcker det att minnet uppdateras."""
    if not isinstance(memo, dict) or memo.get("token") != token_fp:
        return True
    remembered = memo.get("since")
    if since is None:
        return remembered is not None
    if remembered is None:
        return False
    remembered_dt = _parse(remembered)
    if remembered_dt is None:
        # Korrupt minne: en fyllning i onödan kostar en statistikläsning, en missad kostar
        # historik. Fyllningen skriver ändå bara före serverns första rad.
        return True
    return since > remembered_dt


def gap_for(since: datetime | None, now: datetime, window_days: int) -> Gap | None:
    """Luckan [nu − fönster, serverns första rad) – hela fönstret om servern saknar rader.
    None när servern redan täcker fönstrets början."""
    start = now - timedelta(days=window_days)
    end = now if since is None else since
    return Gap(start, end) if end > start else None


def memo(token_fp: str, since: datetime | None) -> dict[str, str | None]:
    """Minnets lagrade form (Store-nyckeln "coverage")."""
    return {"token": token_fp, "since": since.isoformat() if since is not None else None}


def within(rows: list[dict], end: datetime) -> list[dict]:
    """Rader vars HELA intervall slutar senast `end`. Flödesrader är kvartar; SoC-rader bär
    `period_s` (timrader börjar på hel timme och kan annars sträcka sig in över serverns
    första rad)."""
    kept = []
    for row in rows:
        start = datetime.fromisoformat(row["ts"])
        if start + timedelta(seconds=row.get("period_s", _FLOW_PERIOD_S)) <= end:
            kept.append(row)
    return kept
