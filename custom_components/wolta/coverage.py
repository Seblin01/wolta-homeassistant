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


# Statusar som betyder att servern vägrar just den här requesten, så att ett nytt försök
# nästa cykel inte ändrar något. 413 tolkas som profilens lagringstak: fill_slices håller
# varje PUT inom samma radtak som en vanlig uppladdningsbit, så proxyns body-gräns nås i
# praktiken inte (taket räknar rader, inte byte – SoC-rader är något större). Allt annat
# (401, 408, 429, 5xx, saknad status, nätfel) görs om nästa cykel.
PERMANENT_REFUSAL: Final = frozenset({400, 403, 409, 413, 422})


def is_permanent_refusal(status: int | None) -> bool:
    """Ska fyllningen ge upp för den här profilen i stället för att försöka igen?"""
    return status in PERMANENT_REFUSAL


def fill_slices(
    rows: list[dict], soc: list[dict], max_rows: int
) -> list[tuple[list[dict], list[dict]]]:
    """Fyllningens PUT:ar, NYAST FÖRST, som (flödesrader, SoC-rader) i stigande ordning.

    Varje skiva bär de SoC-rader som STARTAR i dess tidsintervall, och flöden + SoC ryms
    tillsammans inom `max_rows`. put_data skickar SoC-biten i samma request som
    flödesbiten, och MAX_ROWS_PER_PUT är dimensionerad efter proxyns body-gräns för EN
    bit. Utan den här räkningen kunde en skiva bli dubbelt så stor och få en 413 från
    proxyn, som annars inte går att skilja från profilens lagringstak. Den äldsta skivan
    tar också SoC som startar före första flödesraden. En enda flödesrad med fler
    SoC-rader än `max_rows` blir en egen för stor skiva, och put_data delar då upp SoC.

    `rows` är stigande (merge_streams). `soc` sorteras här, eftersom battery_state_rows
    ordnar per enhet och inte globalt. Varje tidsstämpel tolkas en gång."""
    soc_by_start = sorted(
        ((datetime.fromisoformat(r["ts"]), r) for r in soc), key=lambda pair: pair[0])
    slices: list[tuple[list[dict], list[dict]]] = []
    flows_acc: list[dict] = []      # current slice, newest → oldest (reversed on close)
    soc_acc: list[dict] = []
    unassigned = len(soc_by_start)  # soc_by_start[unassigned:] already has a slice
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        start = datetime.fromisoformat(row["ts"])
        first = unassigned
        if index == 0:
            first = 0               # the oldest row also takes everything before it
        else:
            while first > 0 and soc_by_start[first - 1][0] >= start:
                first -= 1
        own_soc = [r for _, r in soc_by_start[first:unassigned]]
        if flows_acc and len(flows_acc) + len(soc_acc) + 1 + len(own_soc) > max_rows:
            slices.append((flows_acc[::-1], soc_acc[::-1]))
            flows_acc, soc_acc = [], []
        flows_acc.append(row)
        soc_acc.extend(reversed(own_soc))
        unassigned = first
    if flows_acc:
        slices.append((flows_acc[::-1], soc_acc[::-1]))
    return slices


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
