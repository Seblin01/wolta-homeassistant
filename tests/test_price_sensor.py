"""Tests for the two spot-price sensors (WoltaSpotPriceSensor)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.wolta.const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_PLANT_ID,
    CONF_SOLAR,
    CONF_TOKEN,
    CONF_ZONE,
    DOMAIN,
    WOLTA_API_BASE,
)
from custom_components.wolta.coordinator import WoltaCoordinator, WoltaData
from custom_components.wolta.sensor import WoltaSpotPriceSensor
from tests.test_sensor import RESULTS_FULL

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

PRICES_URL = f"{WOLTA_API_BASE}/api/v1/prices/now"
UNIQUE_ID = "uid-price-test"
SPOT_ID = f"{UNIQUE_ID}_spot_price"
FORECAST_ID = f"{UNIQUE_ID}_spot_price_with_forecast"
GRADE_ID = f"{UNIQUE_ID}_optimeringsbetyg"


def _pt(ts: str, ore: float | None) -> dict:
    return {"ts_utc": ts, "eur_mwh": 40.0, "ore_kwh": ore}


_ACCURACY = {
    "basis": "upstream",
    "days": 0,
    "window_days": 30,
    "mae_ore_kwh": None,
    "peak_mae_ore_kwh": None,
    "upstream_mae_ore_kwh": 22,
}

_FORECAST = {
    "issued_at": "2026-10-26T09:00:00Z",
    "points": [_pt("2026-10-28T10:00:00Z", 10.0), _pt("2026-10-28T11:00:00Z", 20.0)],
    "accuracy": _ACCURACY,
    "source": "Forecast source",
}


def _resp(*, forecast: dict | None) -> dict:
    return {
        "zone": "SE3",
        "now": _pt("2026-10-26T10:00:00Z", 52.3),
        "today": [_pt("2026-10-26T10:00:00Z", 52.3), _pt("2026-10-26T11:00:00Z", 60.0)],
        "tomorrow": [_pt("2026-10-27T10:00:00Z", 30.0)],
        "tomorrow_complete": False,
        "fx": {"date": "2026-10-23", "eur_sek": 11.0},
        "vat_factor": 1.25,
        "source": "Price source",
        "forecast": forecast,
    }


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=UNIQUE_ID,
        data={
            CONF_TOKEN: "tok-price-test",
            CONF_ZONE: "SE3",
            CONF_BATT_IN: "sensor.batt_in",
            CONF_BATT_OUT: "sensor.batt_out",
            CONF_GRID_IN: "sensor.grid_in",
            CONF_GRID_OUT: "sensor.grid_out",
            CONF_SOLAR: "sensor.solar",
            CONF_PLANT_ID: "a" * 32,
        },
    )


async def _main_first_refresh(self: WoltaCoordinator) -> None:
    """Stand-in for the main coordinator's first refresh: serve a ready grade."""
    self.async_set_updated_data(
        WoltaData(
            results=RESULTS_FULL,
            last_uploaded=datetime(2025, 5, 31, tzinfo=timezone.utc),
            n_days=150,
            pending=False,
        )
    )


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    *,
    prices_json: dict | None = None,
    prices_status: int = 200,
    enable_forecast: bool = False,
    hold_price_refresh: asyncio.Event | None = None,
) -> MockConfigEntry:
    aioclient_mock.post(f"{WOLTA_API_BASE}/api/v1/profile/link", json={"link_token": "wpl_x"})
    if prices_status == 200:
        aioclient_mock.get(PRICES_URL, json=prices_json or _resp(forecast=None))
    else:
        aioclient_mock.get(PRICES_URL, status=prices_status)
    entry = _entry()
    entry.add_to_hass(hass)
    if enable_forecast:
        # The forecast sensor is disabled by default: pre-create its registry entry as
        # enabled, the way a user who switched it on would have it.
        er.async_get(hass).async_get_or_create(
            "sensor", DOMAIN, FORECAST_ID, config_entry=entry, disabled_by=None
        )
    patches = [
        patch(
            "custom_components.wolta.coordinator.WoltaCoordinator.async_config_entry_first_refresh",
            _main_first_refresh,
        ),
    ]
    if hold_price_refresh is not None:

        async def _held(self) -> None:
            await hold_price_refresh.wait()

        patches.append(
            patch(
                "custom_components.wolta.price_coordinator.WoltaPriceCoordinator.async_refresh",
                _held,
            )
        )
    for p in patches:
        p.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=hold_price_refresh is None)
    finally:
        for p in patches:
            p.stop()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _entity_id(hass: HomeAssistant, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None, f"no registry entry for {unique_id}"
    return entity_id


@pytest.mark.asyncio
async def test_spot_price_state(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    await _setup(hass, aioclient_mock)

    state = hass.states.get(_entity_id(hass, SPOT_ID))

    assert state is not None
    assert state.state == "0.523"
    assert state.attributes["unit_of_measurement"] == "SEK/kWh"
    assert "device_class" not in state.attributes
    assert "state_class" not in state.attributes
    assert state.attributes["attribution"] == "Price source"


@pytest.mark.asyncio
async def test_spot_price_attributes_have_no_forecast(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    # The API returns a forecast, but the plain spot sensor must not expose it.
    await _setup(hass, aioclient_mock, prices_json=_resp(forecast=_FORECAST))

    attrs = hass.states.get(_entity_id(hass, SPOT_ID)).attributes

    assert len(attrs["prices"]) == 3
    assert all("forecast" not in p for p in attrs["prices"])
    assert attrs["vat_factor"] == 1.25
    assert attrs["tomorrow_complete"] is False
    assert not [k for k in attrs if k.startswith("forecast")]
    assert attrs["attribution"] == "Price source"


@pytest.mark.asyncio
async def test_forecast_sensor_disabled_by_default(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup(hass, aioclient_mock, prices_json=_resp(forecast=_FORECAST))

    registry = er.async_get(hass)
    entry = registry.async_get(_entity_id(hass, FORECAST_ID))

    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert registry.async_get(_entity_id(hass, SPOT_ID)).disabled_by is None


@pytest.mark.asyncio
async def test_forecast_sensor_series(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    await _setup(
        hass, aioclient_mock, prices_json=_resp(forecast=_FORECAST), enable_forecast=True
    )

    state = hass.states.get(_entity_id(hass, FORECAST_ID))

    assert state is not None
    assert state.state == "0.523"
    attrs = state.attributes
    assert len(attrs["prices"]) == 5
    assert attrs["prices"][-1]["forecast"] is True
    assert attrs["prices"][0]["forecast"] is False
    assert attrs["forecast_issued_at"] == "2026-10-26T09:00:00Z"
    assert attrs["forecast_accuracy"] == _ACCURACY
    assert attrs["forecast_accuracy"]["basis"] == "upstream"
    assert attrs["attribution"] == "Price source Forecast source"


@pytest.mark.asyncio
async def test_forecast_sensor_without_forecast(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup(hass, aioclient_mock, prices_json=_resp(forecast=None), enable_forecast=True)

    attrs = hass.states.get(_entity_id(hass, FORECAST_ID)).attributes

    assert attrs["forecast_issued_at"] is None
    assert attrs["forecast_accuracy"] is None
    assert all(p["forecast"] is False for p in attrs["prices"])
    assert attrs["attribution"] == "Price source"


def test_prices_attribute_not_recorded():
    assert "prices" in WoltaSpotPriceSensor._unrecorded_attributes


@pytest.mark.asyncio
async def test_missing_fx_makes_sensors_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """now.ore_kwh is null when the backend had no FX rate: no state rather than a wrong one."""
    resp = _resp(forecast=None)
    resp["now"] = _pt("2026-10-26T10:00:00Z", None)
    await _setup(hass, aioclient_mock, prices_json=resp)

    assert hass.states.get(_entity_id(hass, SPOT_ID)).state == "unavailable"


@pytest.mark.asyncio
async def test_main_sensors_unaffected_by_price_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup(hass, aioclient_mock, prices_status=500, enable_forecast=True)

    grade = hass.states.get(_entity_id(hass, GRADE_ID))
    assert grade is not None and grade.state != "unavailable"
    assert hass.states.get(_entity_id(hass, SPOT_ID)).state == "unavailable"
    assert hass.states.get(_entity_id(hass, FORECAST_ID)).state == "unavailable"


@pytest.mark.asyncio
async def test_price_sensors_unavailable_until_first_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """The first price refresh runs after platform forwarding, so for a while
    prices.data is None while last_update_success still defaults to True."""
    release = asyncio.Event()
    entry = await _setup(
        hass,
        aioclient_mock,
        enable_forecast=True,
        hold_price_refresh=release,
    )
    try:
        prices = entry.runtime_data.prices
        assert prices.data is None
        spot_id = _entity_id(hass, SPOT_ID)
        forecast_id = _entity_id(hass, FORECAST_ID)
        assert hass.states.get(spot_id).state == "unavailable"
        assert hass.states.get(forecast_id).state == "unavailable"

        prices.async_set_updated_data(_resp(forecast=_FORECAST))
        await hass.async_block_till_done()

        assert hass.states.get(spot_id).state == "0.523"
        assert hass.states.get(forecast_id).state == "0.523"
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
