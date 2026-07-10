"""Tests for zone suggestion from HA country/latitude."""

from __future__ import annotations

from custom_components.wolta.zone_prefill import suggest_zone


def test_single_zone_countries():
    assert suggest_zone("FI", None) == "FI"
    assert suggest_zone("NL", 52.0) == "NL"


def test_luxembourg_maps_to_delu():
    assert suggest_zone("LU", 49.6) == "DELU"
    assert suggest_zone("DE", 52.5) == "DELU"


def test_sweden_latitude_bands():
    assert suggest_zone("SE", 67.85) == "SE1"  # Kiruna
    assert suggest_zone("SE", 63.83) == "SE2"  # Umeå
    assert suggest_zone("SE", 59.33) == "SE3"  # Stockholm
    assert suggest_zone("SE", 55.60) == "SE4"  # Malmö


def test_sweden_without_latitude_falls_back_to_none():
    assert suggest_zone("SE", None) is None


def test_multizone_defaults_most_populous():
    assert suggest_zone("NO", 60.0) == "NO1"
    assert suggest_zone("DK", 55.7) == "DK2"
    assert suggest_zone("IT", 45.5) == "IT_NORD"


def test_unknown_or_missing_country():
    assert suggest_zone("US", 40.0) is None
    assert suggest_zone(None, 59.0) is None
