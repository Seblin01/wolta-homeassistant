"""Hjälpare för serverns `battery_params` (egna batterivärden). Rena funktioner, inga HA-importer.

Blocket kommer ur GET /api/v1/profile. Varje storhet (capacity, power, eff, reserve) är
`{value, state, source, basis, period_start, period_end, own, measured: {...}}`; `own_timeline`
är per storhet en lista av `{valid_from, value, set_by, set_at}`. Allt läses defensivt: ett
saknat eller missbildat block ger None/False, aldrig ett undantag.
"""

from __future__ import annotations

QUANTITIES: tuple[str, ...] = ("capacity", "power", "eff", "reserve")

# Källor där Woltas värde är en mätning av batteriet som det är i dag.
MEASURED_SOURCES = frozenset({"soc", "flows", "source_reported"})

# Förra epokens mätning, buren över en etapp (api simulate/battery_params.py CARRIED_SOURCE,
# tillståndet "preliminary"). Etappen kan vara en utbyggnad eller ett byte, inlagd av ägaren
# eller upptäckt, så den beskrivs som "före batteriets senaste ändring" och kallas aldrig uppmätt.
CARRIED_SOURCE = "previous_stage"


def is_active(bp: dict | None) -> bool:
    """Servern har bytt till regel 3 (egna värden) för den här anläggningen."""
    return isinstance(bp, dict) and bp.get("active") is True


def rule(bp: object) -> bool | None:
    """Vad blocket säger om regeln: True/False när `active` är ett uttryckligt booleskt värde,
    annars None (blocket saknas eller är missbildat och säger ingenting)."""
    if isinstance(bp, dict) and isinstance(bp.get("active"), bool):
        return bp["active"]
    return None


def quantity(bp: dict | None, q: str) -> dict | None:
    """Storhetens block, eller None när det saknas eller inte är en dict."""
    if not isinstance(bp, dict):
        return None
    value = bp.get(q)
    return value if isinstance(value, dict) else None


def own_value(bp: dict | None, q: str) -> float | None:
    """Ägarens eget värde för storheten, eller None."""
    block = quantity(bp, q)
    return block.get("own") if block is not None else None


def own_values(bp: dict | None) -> tuple[float | None, ...]:
    """Ägarens egna värden i QUANTITIES-ordning: det som flyttar betyget. Uppmätta värden och
    perioder ingår inte, de rör sig efter varje omräkning."""
    return tuple(own_value(bp, q) for q in QUANTITIES)


def has_dated_own_rows(bp: dict | None, q: str) -> bool:
    """Har storhetens egna tidslinje någon rad med `valid_from` satt?"""
    if not isinstance(bp, dict):
        return False
    timeline = bp.get("own_timeline")
    rows = timeline.get(q) if isinstance(timeline, dict) else None
    if not isinstance(rows, list):
        return False
    return any(isinstance(r, dict) and r.get("valid_from") for r in rows)


def wolta_value_kind(bp: dict | None, q: str) -> str | None:
    """Vad Woltas eget värde bygger på: "measured", "previous_stage" (mätningen före batteriets
    senaste ändring), "datasheet", "assumed" eller None.

    Ett antagande går före tillståndet: servern ger verkningsgraden 0,9 och reserven 0 % källan
    "assumed" med tillståndet "datasheet" när Wolta inte kan mäta dem, och de är inget
    databladsvärde."""
    block = quantity(bp, q)
    measured = block.get("measured") if block is not None else None
    if not isinstance(measured, dict):
        return None
    state = measured.get("state")
    source = measured.get("source")
    if state == "missing" or measured.get("value") is None:
        return None
    if source == CARRIED_SOURCE:
        return "previous_stage"
    if source in MEASURED_SOURCES:
        return "measured"
    if source == "assumed":
        return "assumed"
    if state == "datasheet" or source == "datasheet":
        return "datasheet"
    return None
