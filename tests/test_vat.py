"""Tests for the VAT conversion helpers."""

import pytest

from custom_components.wolta.vat import (
    is_untouched,
    max_shown,
    to_shown,
    to_stored,
    vat_from,
)


def test_to_shown():
    assert to_shown(40, 1.25) == 50
    assert to_shown(9.872, 1.25) == 12.34
    assert to_shown(None, 1.25) is None


def test_to_stored():
    assert to_stored(50, 1.25) == 40
    assert to_stored(12.34, 1.25) == 9.872
    assert to_stored(76, 1.25) == 60.8
    assert to_stored(None, 1.25) is None


@pytest.mark.parametrize("stored", [0, 9.872, 25, 35.6, 61.2, 500])
def test_roundtrip_125(stored):
    assert to_stored(to_shown(stored, 1.25), 1.25) == stored


@pytest.mark.parametrize("stored", [0, 25, 61.2])
def test_roundtrip_1255(stored):
    assert to_stored(to_shown(stored, 1.255), 1.255) == stored


def test_vat_from_valid():
    assert vat_from({"vat": 1.25}) == 1.25


@pytest.mark.parametrize(
    "profile", [{}, {"vat": 0}, {"vat": None}, {"vat": "x"}, {"vat": -1}, None]
)
def test_vat_from_invalid(profile):
    assert vat_from(profile) is None


def test_is_untouched():
    assert is_untouched(12.34, 12.34)
    assert is_untouched(12.340000001, 12.34)
    assert not is_untouched(12.35, 12.34)
    assert is_untouched(None, None)
    assert not is_untouched(None, 12.34)


def test_max_shown():
    assert max_shown(1.25) == 625
    assert max_shown(1.255) == 627.5
