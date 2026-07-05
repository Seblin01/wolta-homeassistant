"""Tests for custom_components/wolta/sensor.py (TDD)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from custom_components.wolta.coordinator import WoltaCoordinator, WoltaData
from custom_components.wolta.sensor import SENSOR_DESCRIPTIONS, WoltaSensor, async_setup_entry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TOKEN = "tok-test-b6"
ENTRY_ID = "entry_b6"

RESULTS_FULL = {
    "status": "ready",
    "currency": "SEK",
    "period": {
        "start": "2025-01-01",
        "end": "2025-05-31",
        "n_days": 150,
    },
    "betyg": {
        "holistic": {
            "score_on": 0.82,
            "diagnos": "Bra timing, kan optimeras mer på morgontimmarna.",
            "peer_percentile": 68,
            "mal": "score_on >= 0.90",
        }
    },
    "decision": {
        "avg_annual_sek": 12500.0,
        "irr": 0.073,
        "payback_years": 8.5,
        "verdict": "keep",
    },
    "history": {
        "yearly": [
            {"year": 2024, "total_sek": 11800.0},
            {"year": 2025, "total_sek": 12200.0},
        ],
        "breakeven_date": "2030-06-01",
        "breakeven_years": 5.5,
    },
}

RESULTS_EUR = {
    "status": "ready",
    "currency": "EUR",
    "period": {
        "start": "2025-01-01",
        "end": "2025-05-31",
        "n_days": 150,
    },
    "betyg": {
        "holistic": {
            "score_on": 0.75,
            "diagnos": "Goed timing.",
            "peer_percentile": 55,
            "mal": "score_on >= 0.90",
        }
    },
    "decision": None,
    "history": None,
}

RESULTS_NO_BETYG = {
    "status": "pending",
    "currency": "SEK",
    "period": {"start": "2025-01-01", "end": "2025-05-31", "n_days": 10},
    "betyg": None,
    "decision": None,
    "history": None,
}

RESULTS_NO_DECISION = {
    "status": "ready",
    "currency": "SEK",
    "period": {"start": "2025-01-01", "end": "2025-05-31", "n_days": 150},
    "betyg": {
        "holistic": {
            "score_on": 0.80,
            "diagnos": "OK.",
            "peer_percentile": 60,
            "mal": "score_on >= 0.90",
        }
    },
    "decision": None,
    "history": None,
}

RESULTS_NO_HISTORY = {
    "status": "ready",
    "currency": "SEK",
    "period": {"start": "2025-01-01", "end": "2025-05-31", "n_days": 150},
    "betyg": {
        "holistic": {
            "score_on": 0.80,
            "diagnos": "OK.",
            "peer_percentile": 60,
            "mal": "score_on >= 0.90",
        }
    },
    "decision": {
        "avg_annual_sek": 11000.0,
        "irr": 0.065,
        "payback_years": 9.0,
        "verdict": "keep",
    },
    "history": None,
}


def _make_coordinator(results: dict) -> WoltaCoordinator:
    """Return a mocked WoltaCoordinator with given results."""
    coord = MagicMock(spec=WoltaCoordinator)
    coord.last_update_success = True
    coord.data = WoltaData(
        results=results,
        last_uploaded=datetime(2025, 5, 31, 0, 0, 0, tzinfo=timezone.utc),
        n_days=results["period"]["n_days"],
        pending=results.get("status") in ("pending", "running"),
    )
    return coord


def _make_entry(entry_id: str = ENTRY_ID) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.unique_id = entry_id
    entry.runtime_data = None  # will be set per test
    return entry


# ---------------------------------------------------------------------------
# Helper: find sensor by key
# ---------------------------------------------------------------------------


def _sensor(key: str, results: dict) -> WoltaSensor:
    """Build a WoltaSensor for the given key with given results fixture."""
    coord = _make_coordinator(results)
    entry = _make_entry()
    entry.runtime_data = coord

    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
    sensor = WoltaSensor(coordinator=coord, entry=entry, description=description)
    return sensor


# ---------------------------------------------------------------------------
# optimeringsbetyg
# ---------------------------------------------------------------------------


def test_optimeringsbetyg_value():
    """Score is score_on * 100 rounded to 2 decimals."""
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    assert s.native_value == pytest.approx(82.0, abs=0.01)


def test_optimeringsbetyg_unit():
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    assert s.native_unit_of_measurement == "%"


def test_optimeringsbetyg_attributes():
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    attrs = s.extra_state_attributes
    assert "diagnos" in attrs
    assert "peer_percentile" in attrs
    assert "mal" in attrs


def test_optimeringsbetyg_unavailable_when_betyg_none():
    s = _sensor("optimeringsbetyg", RESULTS_NO_BETYG)
    assert s.available is False


def test_optimeringsbetyg_unavailable_when_score_on_missing():
    """None score_on → unavailable, no crash."""
    results = {
        **RESULTS_FULL,
        "betyg": {"holistic": {"score_on": None}},
    }
    s = _sensor("optimeringsbetyg", results)
    assert s.available is False


# ---------------------------------------------------------------------------
# batterivarde_ar
# ---------------------------------------------------------------------------


def test_batterivarde_ar_value():
    s = _sensor("batterivarde_ar", RESULTS_FULL)
    assert s.native_value == pytest.approx(12500.0)


def test_batterivarde_ar_unit_sek():
    s = _sensor("batterivarde_ar", RESULTS_FULL)
    assert s.native_unit_of_measurement == "SEK"


def test_batterivarde_ar_unit_eur():
    """EUR currency is used when results.currency == EUR."""
    # EUR profile has decision=None → unavailable, but we test unit when available
    results = {
        **RESULTS_FULL,
        "currency": "EUR",
        "decision": {
            "avg_annual_sek": 1200.0,
            "irr": 0.07,
            "payback_years": 8.0,
            "verdict": "keep",
        },
    }
    s = _sensor("batterivarde_ar", results)
    assert s.native_unit_of_measurement == "EUR"


def test_batterivarde_ar_unavailable_when_decision_none():
    s = _sensor("batterivarde_ar", RESULTS_NO_DECISION)
    assert s.available is False


# ---------------------------------------------------------------------------
# irr
# ---------------------------------------------------------------------------


def test_irr_value():
    s = _sensor("irr", RESULTS_FULL)
    assert s.native_value == pytest.approx(7.3, abs=0.01)


def test_irr_unit():
    s = _sensor("irr", RESULTS_FULL)
    assert s.native_unit_of_measurement == "%"


def test_irr_unavailable_when_decision_none():
    s = _sensor("irr", RESULTS_NO_DECISION)
    assert s.available is False


# ---------------------------------------------------------------------------
# payback
# ---------------------------------------------------------------------------


def test_payback_value():
    s = _sensor("payback", RESULTS_FULL)
    assert s.native_value == pytest.approx(8.5)


def test_payback_unit():
    s = _sensor("payback", RESULTS_FULL)
    assert s.native_unit_of_measurement == "år"


def test_payback_unavailable_when_decision_none():
    s = _sensor("payback", RESULTS_NO_DECISION)
    assert s.available is False


# ---------------------------------------------------------------------------
# facit_i_ar
# ---------------------------------------------------------------------------


def test_facit_i_ar_value():
    """Uses last entry in yearly list."""
    s = _sensor("facit_i_ar", RESULTS_FULL)
    assert s.native_value == pytest.approx(12200.0)


def test_facit_i_ar_unit_sek():
    s = _sensor("facit_i_ar", RESULTS_FULL)
    assert s.native_unit_of_measurement == "SEK"


def test_facit_i_ar_attributes():
    s = _sensor("facit_i_ar", RESULTS_FULL)
    attrs = s.extra_state_attributes
    assert "yearly" in attrs
    assert "breakeven_date" in attrs


def test_facit_i_ar_unavailable_when_history_none():
    s = _sensor("facit_i_ar", RESULTS_NO_HISTORY)
    assert s.available is False


# ---------------------------------------------------------------------------
# datastatus
# ---------------------------------------------------------------------------


def test_datastatus_value():
    """Value is period.end as ISO string (timestamp device class)."""
    s = _sensor("datastatus", RESULTS_FULL)
    assert s.native_value == "2025-05-31"


def test_datastatus_attributes():
    s = _sensor("datastatus", RESULTS_FULL)
    attrs = s.extra_state_attributes
    assert "n_days" in attrs
    assert "pending" in attrs
    assert "last_uploaded" in attrs


def test_datastatus_always_available():
    """datastatus is always available (period.end is always in results)."""
    s = _sensor("datastatus", RESULTS_NO_BETYG)
    assert s.available is True


# ---------------------------------------------------------------------------
# SP3 – non-SE profile: betyg OK, economy unavailable
# ---------------------------------------------------------------------------


def test_sp3_nonse_grade_available():
    """Non-SE profile: optimeringsbetyg is available (has betyg)."""
    s = _sensor("optimeringsbetyg", RESULTS_EUR)
    assert s.available is True
    assert s.native_value == pytest.approx(75.0, abs=0.01)


def test_sp3_nonse_economy_sensors_unavailable():
    """Non-SE profile: all economy sensors unavailable (decision=None)."""
    for key in ("batterivarde_ar", "irr", "payback"):
        s = _sensor(key, RESULTS_EUR)
        assert s.available is False, f"{key} should be unavailable for non-SE"


def test_sp3_nonse_facit_unavailable():
    """Non-SE profile: facit_i_ar unavailable (history=None)."""
    s = _sensor("facit_i_ar", RESULTS_EUR)
    assert s.available is False


def test_sp3_nonse_datastatus_available():
    """datastatus always available regardless of zone."""
    s = _sensor("datastatus", RESULTS_EUR)
    assert s.available is True


# ---------------------------------------------------------------------------
# unique_id
# ---------------------------------------------------------------------------


def test_unique_id_format():
    """unique_id is entry.unique_id + '_' + key."""
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    assert s.unique_id == f"{ENTRY_ID}_optimeringsbetyg"
