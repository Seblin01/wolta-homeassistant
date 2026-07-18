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
        "score_pct": 82.0,
        "holistic": {
            "score_on": 0.82,
            "score_off": 0.61,
            "measured_total_sek": 9800.0,
            "model_total_on_sek": 11200.0,
            "model_total_off_sek": 7100.0,
            "wear_ore": 12.5,
        },
        "price_skill": 0.74,
        "gap_sek": 1400.0,
        "peer": {"n": 312, "percentile": 68},
        "components": [
            {"key": "timing", "label": "Laddtidpunkt", "captured": 0.80, "possible": 1.0, "possible_on": 0.95},
        ],
        "worst_days": [],
    },
    "decision": {
        "avg_annual_sek": 12500.0,
        "avg_battery_sek": 2900.0,
        "avg_solar_sek": 9600.0,
        "irr": 0.073,
        "payback_years": 8.5,
        "effective_capex": 85000.0,
        "years": [],
    },
    "history": {
        "yearly": [
            {"year": 2024, "total_sek": 11800.0},
            {"year": 2025, "total_sek": 12200.0},
        ],
        "breakeven_date": "2030-06-01",
        "breakeven_total_years": 5.5,
        "savings_today_sek": 18000.0,
        "paid_sek": 12500.0,
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
        "score_pct": 75.0,
        "holistic": {
            "score_on": 0.75,
            "score_off": 0.55,
            "measured_total_sek": 800.0,
            "model_total_on_sek": 950.0,
            "model_total_off_sek": 600.0,
            "wear_ore": 10.0,
        },
        "price_skill": 0.68,
        "gap_sek": 150.0,
        "peer": {"n": 200, "percentile": 55},
        "components": [],
        "worst_days": [],
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
        "score_pct": 80.0,
        "holistic": {
            "score_on": 0.80,
            "score_off": 0.58,
            "measured_total_sek": 8000.0,
            "model_total_on_sek": 9500.0,
            "model_total_off_sek": 6000.0,
            "wear_ore": 11.0,
        },
        "price_skill": 0.70,
        "gap_sek": 1500.0,
        "peer": {"n": 280, "percentile": 60},
        "components": [],
        "worst_days": [],
    },
    "decision": None,
    "history": None,
}

RESULTS_NO_HISTORY = {
    "status": "ready",
    "currency": "SEK",
    "period": {"start": "2025-01-01", "end": "2025-05-31", "n_days": 150},
    "betyg": {
        "score_pct": 80.0,
        "holistic": {
            "score_on": 0.80,
            "score_off": 0.58,
            "measured_total_sek": 8000.0,
            "model_total_on_sek": 9500.0,
            "model_total_off_sek": 6000.0,
            "wear_ore": 11.0,
        },
        "price_skill": 0.70,
        "gap_sek": 1500.0,
        "peer": {"n": 280, "percentile": 60},
        "components": [],
        "worst_days": [],
    },
    "decision": {
        "avg_annual_sek": 11000.0,
        "irr": 0.065,
        "payback_years": 9.0,
        "effective_capex": 75000.0,
        "years": [],
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
    entry.data = {"token": "tok-test"}
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
    """Attributes must use real API keys; stale keys (diagnos/mal) must be absent."""
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    attrs = s.extra_state_attributes
    # Real keys present
    assert "peer_percentile" in attrs
    assert attrs["peer_percentile"] == 68
    assert "peer_n" in attrs
    assert attrs["peer_n"] == 312
    assert "gap_sek" in attrs
    assert attrs["gap_sek"] == pytest.approx(1400.0)
    assert "price_skill" in attrs
    assert attrs["price_skill"] == pytest.approx(0.74)
    assert "components" in attrs
    # Stale keys must NOT be present
    assert "diagnos" not in attrs
    assert "mal" not in attrs
    assert "peer_percentil" not in attrs  # svensk v0.4.3-nyckel, ersatt i v0.4.4
    assert "komponenter" not in attrs


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


def test_batterivarde_ar_value_is_measured_battery_value():
    """v0.5.0 (plan 33): the sensor shows the grade's MEASURED battery value
    (betyg.holistic.measured_total_sek) – the same figure as the website's "Du fångade" –
    not decision.avg_annual_sek which is the ENTIRE plant's savings (solar+battery)."""
    s = _sensor("batterivarde_ar", RESULTS_FULL)
    assert s.native_value == pytest.approx(9800.0)


def test_batterivarde_ar_falls_back_to_modelled_battery_value():
    """Without grade but with decision → decision.avg_battery_sek (battery-only), NEVER
    the plant total."""
    results = {
        **RESULTS_FULL,
        "betyg": None,
    }
    s = _sensor("batterivarde_ar", results)
    assert s.native_value == pytest.approx(2900.0)


def test_batterivarde_ar_exposes_plant_total_attr():
    s = _sensor("batterivarde_ar", RESULTS_FULL)
    attrs = s.extra_state_attributes
    assert attrs.get("source") == "measured"
    assert attrs.get("plant_total_sek") == pytest.approx(12500.0)


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


def test_batterivarde_ar_available_from_grade_without_decision():
    """v0.5.0: measured battery value comes from the GRADE → available even when
    decision is missing (e.g. non-SE profiles)."""
    s = _sensor("batterivarde_ar", RESULTS_NO_DECISION)
    assert s.available is True
    assert s.native_value == pytest.approx(8000.0)
    assert s.extra_state_attributes.get("source") == "measured"


def test_batterivarde_ar_unavailable_without_grade_and_decision():
    results = {**RESULTS_NO_DECISION, "betyg": None}
    s = _sensor("batterivarde_ar", results)
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
    assert s.native_unit_of_measurement == "yr"


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
    """Attributes expose breakeven_total_years (not breakeven_years) and breakeven_date."""
    s = _sensor("facit_i_ar", RESULTS_FULL)
    attrs = s.extra_state_attributes
    assert "yearly" in attrs
    assert "breakeven_date" in attrs
    assert attrs["breakeven_date"] == "2030-06-01"
    assert "breakeven_total_years" in attrs
    assert attrs["breakeven_total_years"] == pytest.approx(5.5)
    # Old stale key must NOT be present
    assert "breakeven_years" not in attrs


def test_facit_i_ar_unavailable_when_history_none():
    s = _sensor("facit_i_ar", RESULTS_NO_HISTORY)
    assert s.available is False


# ---------------------------------------------------------------------------
# datastatus
# ---------------------------------------------------------------------------


def test_datastatus_value():
    """Value is period.end parsed to an aware datetime (TIMESTAMP device class requires
    a datetime, not a string, or HA marks the state invalid)."""
    from datetime import datetime, timezone

    s = _sensor("datastatus", RESULTS_FULL)
    assert s.native_value == datetime(2025, 5, 31, tzinfo=timezone.utc)
    assert s.native_value.tzinfo is not None


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
    """Non-SE profile: IRR/payback/plant-savings require decision (SE-only).
    The battery value has been MEASURED from the grade since v0.5.0 → available even non-SE."""
    for key in ("irr", "payback", "anlaggningsbesparing_ar"):
        s = _sensor(key, RESULTS_EUR)
        assert s.available is False, f"{key} should be unavailable for non-SE"


def test_sp3_nonse_battery_value_available_from_grade():
    s = _sensor("batterivarde_ar", RESULTS_EUR)
    assert s.available is True
    assert s.native_value == pytest.approx(800.0)


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


# ---------------------------------------------------------------------------
# v0.4.1: configuration_url → the profile's full results on wolta.se
# ---------------------------------------------------------------------------


def test_device_info_configuration_url():
    """The device's configuration_url points to the plant hub /anlaggning (token mode:
    ?profile=) – /optimeringsbetyg is the public surface since the 2026-07-15 IA."""
    sensor = _sensor("optimeringsbetyg", RESULTS_FULL)
    url = sensor._attr_device_info["configuration_url"]
    assert url == "https://wolta.se/anlaggning?profile=tok-test"


def test_profile_url_quotes_token():
    """Token is URL-encoded (future-proofing in case the token format changes)."""
    from custom_components.wolta.const import profile_url

    assert profile_url("a/b+c") == "https://wolta.se/anlaggning?profile=a%2Fb%2Bc"


# ---------------------------------------------------------------------------
# v0.4.2: retain values during ongoing recompute + status sensor
# ---------------------------------------------------------------------------

RESULTS_RECOMPUTING = {
    # recompute changed fingerprint → grade/decision/history missing while the worker is computing
    "status": "running",
    "currency": "SEK",
    "period": {"start": "2025-01-01", "end": "2025-05-31", "n_days": 150},
    "betyg": None,
    "decision": None,
    "history": None,
}


def _swap_results(sensor: WoltaSensor, results: dict) -> None:
    """Simulate a new coordinator update with different results."""
    sensor.coordinator.data = WoltaData(
        results=results,
        last_uploaded=datetime(2025, 5, 31, 0, 0, 0, tzinfo=timezone.utc),
        n_days=results["period"]["n_days"],
        pending=results.get("status") in ("pending", "running"),
    )


def test_retains_value_during_recompute():
    """Ongoing recompute (pending) → the sensor retains the last known value
    instead of an unavailable blip."""
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    assert s.native_value == pytest.approx(82.0, abs=0.01)  # populates last-value

    _swap_results(s, RESULTS_RECOMPUTING)
    assert s.available is True, "should be available during ongoing computation"
    assert s.native_value == pytest.approx(82.0, abs=0.01), "the last value should be retained"


def test_retained_attrs_flag_computing():
    """Retained attributes are flagged with computing: True during the recompute."""
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    _ = s.native_value
    _ = s.extra_state_attributes  # populates last-attrs

    _swap_results(s, RESULTS_RECOMPUTING)
    attrs = s.extra_state_attributes
    assert attrs.get("computing") is True
    assert attrs.get("peer_percentile") == 68, "the previous attribute should be retained"


def test_unavailable_when_missing_and_not_pending():
    """Missing value WITHOUT an ongoing computation → unavailable as before (no masking)."""
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    _ = s.native_value
    _swap_results(s, {**RESULTS_RECOMPUTING, "status": "done"})
    assert s.available is False


def test_fresh_sensor_during_recompute_still_unavailable():
    """Without a previously known value (e.g. after an HA restart) → unavailable even during pending."""
    s = _sensor("optimeringsbetyg", RESULTS_RECOMPUTING)
    assert s.available is False


def test_status_sensor_mapping():
    """The status sensor maps server status → stable slugs (the display is translated via translation_key)."""
    cases = [
        ({**RESULTS_FULL, "status": "done"}, "done"),
        (RESULTS_RECOMPUTING, "computing"),
        ({**RESULTS_RECOMPUTING, "status": "pending"}, "computing"),
        ({**RESULTS_RECOMPUTING, "status": "error"}, "error"),
        ({**RESULTS_RECOMPUTING, "status": "cold"}, "waiting_for_data"),
        ({**RESULTS_RECOMPUTING, "status": "no_data"}, "waiting_for_data"),
    ]
    for results, expected in cases:
        s = _sensor("status", results)
        assert s.available is True
        assert s.native_value == expected, f"{results['status']} → {expected}"


def test_sek_sensors_suggest_zero_decimals():
    """The two kr-valued sensors display without decimals by default
    (dashboard entity cards can't set precision – it must come from the
    integration via suggested_display_precision)."""
    for key in ("batterivarde_ar", "facit_i_ar"):
        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        assert desc.suggested_display_precision == 0, key


# ---------------------------------------------------------------------------
# anlaggningsbesparing_ar (plan 33)
# ---------------------------------------------------------------------------


def test_plant_savings_value_is_avg_annual():
    s = _sensor("anlaggningsbesparing_ar", RESULTS_FULL)
    assert s.native_value == pytest.approx(12500.0)


def test_plant_savings_unavailable_without_decision():
    s = _sensor("anlaggningsbesparing_ar", RESULTS_NO_DECISION)
    assert s.available is False


# ---------------------------------------------------------------------------
# applied_tariff on optimeringsbetyg (plan 35 / task 6)
# ---------------------------------------------------------------------------


def test_optimeringsbetyg_exposes_applied_tariff_user():
    """When results carries a top-level applied_tariff (user override), the grade
    sensor's attributes expose it (source + the three values)."""
    results = {
        **RESULTS_FULL,
        "applied_tariff": {
            "source": "user",
            "grid_var_ore": 40.0,
            "surcharge_ore": 8.0,
            "export_extra_ore": 5.0,
            "currency": "SEK",
        },
    }
    s = _sensor("optimeringsbetyg", results)
    attrs = s.extra_state_attributes
    assert attrs.get("applied_tariff") == {
        "source": "user",
        "grid_var_ore": 40.0,
        "surcharge_ore": 8.0,
        "export_extra_ore": 5.0,
        "currency": "SEK",
    }


def test_optimeringsbetyg_exposes_applied_tariff_country_default():
    """Source can be country_default when no override was set."""
    results = {
        **RESULTS_FULL,
        "applied_tariff": {
            "source": "country_default",
            "grid_var_ore": 25.0,
            "surcharge_ore": 6.0,
            "export_extra_ore": 0.0,
            "currency": "SEK",
        },
    }
    s = _sensor("optimeringsbetyg", results)
    attrs = s.extra_state_attributes
    assert attrs["applied_tariff"]["source"] == "country_default"


def test_optimeringsbetyg_applied_tariff_absent_when_missing():
    """No applied_tariff in results (older backend/no data yet) → key omitted,
    no crash."""
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    attrs = s.extra_state_attributes
    assert "applied_tariff" not in attrs


# ---------------------------------------------------------------------------
# capacity_hint on optimeringsbetyg (plan 37 / issue #1)
# ---------------------------------------------------------------------------


def test_optimeringsbetyg_exposes_capacity_hint():
    """When betyg carries capacity_hint (entered capacity >> observed usable),
    the grade sensor exposes it as a structured attribute."""
    results = {
        **RESULTS_FULL,
        "betyg": {
            **RESULTS_FULL["betyg"],
            "capacity_hint": {
                "entered_kwh": 15.0,
                "observed_usable_kwh": 10.45,
                "suggested_kwh": 10.5,
            },
        },
    }
    s = _sensor("optimeringsbetyg", results)
    attrs = s.extra_state_attributes
    assert attrs.get("capacity_hint") == {
        "entered_kwh": 15.0,
        "observed_usable_kwh": 10.45,
        "suggested_kwh": 10.5,
    }


def test_optimeringsbetyg_omits_capacity_hint_when_absent():
    """No capacity_hint key in betyg (backend didn't flag, or older cached
    response) -> attribute omitted entirely, not None."""
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    assert "capacity_hint" not in s.extra_state_attributes


# ---------------------------------------------------------------------------
# applied_reserve on optimeringsbetyg (plan 38 / task 6)
# ---------------------------------------------------------------------------


def test_optimeringsbetyg_exposes_applied_reserve():
    """When results carries a top-level applied_reserve (a reserve floor was
    configured), the grade sensor's attributes expose it."""
    results = {
        **RESULTS_FULL,
        "applied_reserve": {
            "reserve_pct": 10.0,
            "effective_kwh": 9.45,
        },
    }
    s = _sensor("optimeringsbetyg", results)
    attrs = s.extra_state_attributes
    assert attrs.get("applied_reserve") == {
        "reserve_pct": 10.0,
        "effective_kwh": 9.45,
    }


def test_optimeringsbetyg_applied_reserve_absent_when_null():
    """Backend sends applied_reserve: null (no grade cached yet, or no reserve
    configured) -> attribute omitted entirely, not set to None."""
    results = {**RESULTS_FULL, "applied_reserve": None}
    s = _sensor("optimeringsbetyg", results)
    attrs = s.extra_state_attributes
    assert "applied_reserve" not in attrs


def test_optimeringsbetyg_applied_reserve_absent_when_missing():
    """No applied_reserve key in results at all (older backend) -> attribute
    omitted, no crash."""
    s = _sensor("optimeringsbetyg", RESULTS_FULL)
    attrs = s.extra_state_attributes
    assert "applied_reserve" not in attrs


# ---------------------------------------------------------------------------
# capex_scope attribute (backend 2026-07-18)
# ---------------------------------------------------------------------------


def test_irr_payback_capex_scope_attribute_present():
    results = {**RESULTS_FULL, "decision": {**RESULTS_FULL["decision"], "capex_scope": "plant"}}
    for key in ("irr", "payback"):
        s = _sensor(key, results)
        assert s.extra_state_attributes.get("capex_scope") == "plant"


def test_irr_payback_capex_scope_attribute_absent_on_older_api():
    for key in ("irr", "payback"):
        s = _sensor(key, RESULTS_FULL)  # ingen capex_scope i svaret (äldre backend)
        assert "capex_scope" not in s.extra_state_attributes
