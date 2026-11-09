"""VAT conversion helpers for tariff fields.

Tariff prices are stored excluding VAT but may be shown and entered
including VAT in the options flow. Pure functions, standard library only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def vat_from(profile: Mapping[str, Any] | None) -> float | None:
    """Return the VAT multiplier from a profile, or None if missing/invalid."""
    if not profile:
        return None
    value = profile.get("vat")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def to_shown(stored: float | None, vat: float) -> float | None:
    """Convert a stored (ex-VAT) value to the value shown to the user."""
    if stored is None:
        return None
    return round(stored * vat, 2)


def to_stored(shown: float | None, vat: float) -> float | None:
    """Convert a value entered incl. VAT back to the stored (ex-VAT) value."""
    if shown is None:
        return None
    return round(shown / vat, 4)


def is_untouched(entered: float | None, prefilled: float | None) -> bool:
    """Return True if the entered value equals the prefilled one (at 2 decimals)."""
    if entered is None and prefilled is None:
        return True
    if entered is None or prefilled is None:
        return False
    return round(entered, 2) == round(prefilled, 2)


def max_shown(vat: float) -> float:
    """Return the largest allowed shown value (500 ex-VAT times VAT, floored)."""
    return math.floor(500 * vat * 100) / 100
