"""Hjälpare för serverns `battery_params` (egna batterivärden). Rena funktioner, inga HA-importer.

Blocket kommer ur GET /api/v1/profile. Varje storhet (capacity, power, eff, reserve) är
`{value, state, source, basis, period_start, period_end, own, measured: {...}}`; `own_timeline`
är per storhet en lista av `{valid_from, value, set_by, set_at}`. Allt läses defensivt: ett
saknat eller missbildat block ger None/False, aldrig ett undantag.
"""

from __future__ import annotations

QUANTITIES: tuple[str, ...] = ("capacity", "power", "eff", "reserve")

# Samma mängd som webbens MEASURED_SOURCES: källor som är en mätning, inte ett antagande.
MEASURED_SOURCES = frozenset({"soc", "flows", "source_reported", "previous_stage"})


def is_active(bp: dict | None) -> bool:
    """Servern har bytt till regel 3 (egna värden) för den här anläggningen."""
    return isinstance(bp, dict) and bp.get("active") is True


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
    """Vad Woltas eget (uppmätta) värde bygger på: "measured", "datasheet", "assumed" eller
    None. Speglar webbens woltaValueKind/hasMeasurement."""
    block = quantity(bp, q)
    measured = block.get("measured") if block is not None else None
    if not isinstance(measured, dict):
        return None
    state = measured.get("state")
    source = measured.get("source")
    if state == "missing" or measured.get("value") is None:
        return None
    if source in MEASURED_SOURCES:
        return "measured"
    if state == "datasheet" or source == "datasheet":
        return "datasheet"
    if source == "assumed":
        return "assumed"
    return None
