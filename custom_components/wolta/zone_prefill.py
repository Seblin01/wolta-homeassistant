"""Suggest a default bidding zone from HA's configured country + latitude.

Only a suggestion (dropdown default) – the user can always change it.
Single-zone countries map directly; multi-zone countries (SE/NO/DK/IT) get the
most populous zone, except SE where latitude gives a better guess.
"""

from __future__ import annotations

# ISO 3166-1 alpha-2 → Wolta zone code. Source: Wolta backend ENTSOE_ZONES registry
# (29 countries, 43 zones). NOTE: Luxembourg shares the DE-LU bidding zone which the
# registry files under DE – "LU" must be mapped explicitly or LU users get no default.
_COUNTRY_ZONE: dict[str, str] = {
    "AT": "AT", "BE": "BE", "BG": "BG", "CH": "CH", "CZ": "CZ",
    "DE": "DELU", "LU": "DELU",
    "EE": "EE", "ES": "ES", "FI": "FI", "FR": "FR", "GR": "GR", "HR": "HR",
    "HU": "HU", "IE": "IE", "LT": "LT", "LV": "LV", "ME": "ME", "MK": "MK",
    "NL": "NL", "PL": "PL", "PT": "PT", "RO": "RO", "RS": "RS", "SI": "SI",
    "SK": "SK",
    # Multi-zone: most populous zone as default (Oslo, Copenhagen, Milan).
    "NO": "NO1", "DK": "DK2", "IT": "IT_NORD",
}

# SE latitude bands, approximated from Svk's elområde map: snitt 1 between
# Skellefteå (SE1) and Umeå (SE2) ≈ 64.3°N, snitt 2 just north of Gävle ≈ 60.8°N,
# snitt 4 through southern Halland/Småland ≈ 57.0°N. Suggestion only.
_SE_BANDS: list[tuple[float, str]] = [(64.3, "SE1"), (60.8, "SE2"), (57.0, "SE3")]


def suggest_zone(country: str | None, latitude: float | None) -> str | None:
    """Best-guess zone for the given country/latitude; None when no good guess.

    Callers must validate the result against their supported-zone list before use.
    """
    if country == "SE":
        if latitude is None:
            return None
        for floor, zone in _SE_BANDS:
            if latitude >= floor:
                return zone
        return "SE4"
    return _COUNTRY_ZONE.get(country or "")
