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
from pytest_homeassistant_custom_component.common import async_fire_time_changed

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
from tests.test_price_forecast import _ACC, _NOW_FORECAST, _three_days
from tests.test_sensor import RESULTS_FULL

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

NOW = "2026-10-26T10:05:00+00:00"


@pytest.fixture(autouse=True)
def _frozen_now(freezer) -> None:
    """The state is computed locally from the series, so every test needs a fixed clock."""
    freezer.move_to(NOW)

PRICES_URL = f"{WOLTA_API_BASE}/api/v1/prices/now"
FORECAST_URL = f"{WOLTA_API_BASE}/api/v1/prices/forecast"
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
        # The server's `now` is deliberately different from the quarter that covers the
        # frozen clock: the sensor must follow its own series, not this field.
        "now": _pt("2026-10-26T10:00:00Z", 44.0),
        "today": [_pt("2026-10-26T10:00:00Z", 52.3), _pt("2026-10-26T10:15:00Z", 60.0)],
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
    forecast_json: dict | None = None,
    forecast_status: int = 200,
    enable_forecast: bool = False,
    hold_price_refresh: asyncio.Event | None = None,
) -> MockConfigEntry:
    aioclient_mock.post(f"{WOLTA_API_BASE}/api/v1/profile/link", json={"link_token": "wpl_x"})
    if prices_status == 200:
        aioclient_mock.get(PRICES_URL, json=prices_json or _resp(forecast=None))
    else:
        aioclient_mock.get(PRICES_URL, status=prices_status)
    if forecast_status == 200:
        aioclient_mock.get(FORECAST_URL, json=forecast_json)
    else:
        aioclient_mock.get(FORECAST_URL, status=forecast_status)
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


def _forecast_calls(aioclient_mock: AiohttpClientMocker) -> list:
    return [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(FORECAST_URL)]


@pytest.mark.asyncio
async def test_spot_only_makes_no_forecast_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """The forecast sensor is disabled by default: nobody needs /prices/forecast."""
    entry = await _setup(
        hass, aioclient_mock, prices_json=_resp(forecast=_NOW_FORECAST), forecast_json=_three_days()
    )
    await entry.runtime_data.prices.async_refresh()

    assert _forecast_calls(aioclient_mock) == []
    attrs = hass.states.get(_entity_id(hass, SPOT_ID)).attributes
    assert len(attrs["prices"]) == 3


@pytest.mark.asyncio
async def test_forecast_sensor_makes_one_forecast_request_per_poll(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    entry = await _setup(
        hass,
        aioclient_mock,
        prices_json=_resp(forecast=_NOW_FORECAST),
        forecast_json=_three_days(),
        enable_forecast=True,
    )
    assert len(_forecast_calls(aioclient_mock)) == 1

    await entry.runtime_data.prices.async_refresh()
    assert len(_forecast_calls(aioclient_mock)) == 2


@pytest.mark.asyncio
async def test_forecast_sensor_series_has_three_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup(
        hass,
        aioclient_mock,
        prices_json=_resp(forecast=_NOW_FORECAST),
        forecast_json=_three_days(),
        enable_forecast=True,
    )

    state = hass.states.get(_entity_id(hass, FORECAST_ID))

    assert state.state == "0.523"
    prices = state.attributes["prices"]
    starts = [p["start"] for p in prices]
    assert starts == sorted(starts) and len(starts) == len(set(starts))
    assert [p.get("horizon") for p in prices] == [None, None, None, 1, 1, 2, 2, 3, 3]
    assert [p["forecast"] for p in prices] == [False] * 3 + [True] * 6
    assert state.attributes["forecast_issued_at"] == "2026-10-26T09:00:00Z"
    assert state.attributes["forecast_accuracy"] == _ACC
    assert [d["horizon"] for d in state.attributes["forecast_days"]] == [2, 3]
    assert state.attributes["forecast_days"][0] == {
        "date": "2026-10-28",
        "horizon": 2,
        "issued_at": "2026-10-26T09:05:00Z",
        "accuracy": {**_ACC, "days": 2},
    }
    assert all("points" not in d for d in state.attributes["forecast_days"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "null"])
async def test_forecast_endpoint_failure_keeps_the_rest(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, failure: str
):
    await _setup(
        hass,
        aioclient_mock,
        prices_json=_resp(forecast=_NOW_FORECAST),
        forecast_status=500 if failure == "status" else 200,
        forecast_json=None,
        enable_forecast=True,
    )

    state = hass.states.get(_entity_id(hass, FORECAST_ID))

    assert state.state == "0.523"
    attrs = state.attributes
    assert [p.get("horizon") for p in attrs["prices"]] == [None, None, None, 1, 1]
    assert attrs["forecast_issued_at"] == "2026-10-26T09:00:00Z"
    assert attrs["forecast_accuracy"] == _ACC
    assert attrs["forecast_days"] == []


@pytest.mark.asyncio
async def test_forecast_sensor_after_tomorrow_is_published(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """Once tomorrow's prices are out /prices/now has no forecast, but days 2 and 3 remain:
    they are still shown, still credited, and tomorrow's forecast attributes are null."""
    full = _three_days()
    full["days"] = full["days"][1:]
    await _setup(
        hass,
        aioclient_mock,
        prices_json=_resp(forecast=None),
        forecast_json=full,
        enable_forecast=True,
    )

    attrs = hass.states.get(_entity_id(hass, FORECAST_ID)).attributes

    assert [p.get("horizon") for p in attrs["prices"]] == [None, None, None, 2, 2, 3, 3]
    assert attrs["forecast_issued_at"] is None
    assert [d["horizon"] for d in attrs["forecast_days"]] == [2, 3]
    assert attrs["attribution"] == "Price source fsrc"


@pytest.mark.asyncio
async def test_spot_sensor_never_gets_forecast_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup(
        hass,
        aioclient_mock,
        prices_json=_resp(forecast=_NOW_FORECAST),
        forecast_json=_three_days(),
        enable_forecast=True,
    )

    attrs = hass.states.get(_entity_id(hass, SPOT_ID)).attributes

    assert len(attrs["prices"]) == 3
    assert not [k for k in attrs if k.startswith("forecast")]


def test_prices_attribute_not_recorded():
    assert "prices" in WoltaSpotPriceSensor._unrecorded_attributes


@pytest.mark.asyncio
async def test_missing_fx_makes_sensors_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """now.ore_kwh is null when the backend had no FX rate: no state rather than a wrong one."""
    resp = _resp(forecast=None)
    resp["today"][0] = _pt("2026-10-26T10:00:00Z", None)
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


@pytest.mark.asyncio
async def test_state_follows_quarter_boundaries_without_a_poll(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    await _setup(hass, aioclient_mock)
    entity_id = _entity_id(hass, SPOT_ID)
    assert hass.states.get(entity_id).state == "0.523"
    polls = len(aioclient_mock.mock_calls)

    freezer.move_to("2026-10-26T10:15:02+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "0.6"
    assert len(aioclient_mock.mock_calls) == polls


@pytest.mark.asyncio
async def test_unavailable_when_no_point_covers_now(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    """Stale data (e.g. the next poll has not arrived after midnight, or the last point
    is over) must not keep serving an old price."""
    await _setup(hass, aioclient_mock)
    entity_id = _entity_id(hass, SPOT_ID)

    # The last point, 10:15, covers until 10:30.
    freezer.move_to("2026-10-26T10:30:02+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "unavailable"


@pytest.mark.asyncio
async def test_unavailable_when_all_points_are_in_the_future(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    resp = _resp(forecast=None)
    resp["today"] = [_pt("2026-10-26T12:00:00Z", 52.3)]
    resp["tomorrow"] = []
    await _setup(hass, aioclient_mock, prices_json=resp)

    assert hass.states.get(_entity_id(hass, SPOT_ID)).state == "unavailable"


@pytest.mark.asyncio
async def test_price_sensors_not_created_outside_sweden(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """The prices are converted to SEK with Swedish VAT: other zones get no price
    sensors and the price endpoint is never called."""
    aioclient_mock.post(f"{WOLTA_API_BASE}/api/v1/profile/link", json={"link_token": "wpl_x"})
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONE: "NO1"})

    with patch(
        "custom_components.wolta.coordinator.WoltaCoordinator.async_config_entry_first_refresh",
        _main_first_refresh,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.prices is None
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, SPOT_ID) is None
    assert registry.async_get_entity_id("sensor", DOMAIN, FORECAST_ID) is None
    assert registry.async_get_entity_id("sensor", DOMAIN, GRADE_ID) is not None
    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(PRICES_URL)]
    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(FORECAST_URL)]


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["SE1", "SE2", "SE3", "SE4"])
async def test_price_sensors_created_in_every_swedish_zone(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, zone: str
):
    aioclient_mock.post(f"{WOLTA_API_BASE}/api/v1/profile/link", json={"link_token": "wpl_x"})
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONE: zone})

    with patch(
        "custom_components.wolta.coordinator.WoltaCoordinator.async_config_entry_first_refresh",
        _main_first_refresh,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, SPOT_ID) is not None


@pytest.mark.asyncio
async def test_zone_changed_to_non_swedish_makes_sensors_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """The options step "Correct the price zone" changes the zone without a reload. The
    coordinator must not keep serving prices for a zone the sensors cannot represent
    (SEK and Swedish VAT): no request, and both sensors unavailable until a reload."""
    entry = await _setup(hass, aioclient_mock, enable_forecast=True)
    spot_id = _entity_id(hass, SPOT_ID)
    forecast_id = _entity_id(hass, FORECAST_ID)
    assert hass.states.get(spot_id).state == "0.523"
    polls = len(aioclient_mock.mock_calls)

    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONE: "NO1"})
    await entry.runtime_data.prices.async_refresh()
    await hass.async_block_till_done()

    assert len(aioclient_mock.mock_calls) == polls
    assert entry.runtime_data.prices.last_update_success is False
    assert hass.states.get(spot_id).state == "unavailable"
    assert hass.states.get(forecast_id).state == "unavailable"
