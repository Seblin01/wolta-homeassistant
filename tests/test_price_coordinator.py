"""Tests for the spot-price client method and WoltaPriceCoordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.wolta.api import WoltaApiClient
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
from custom_components.wolta.price_coordinator import WoltaPriceCoordinator, price_series

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

PRICES_URL = f"{WOLTA_API_BASE}/api/v1/prices/now"


def _pt(ts: str, ore: float | None) -> dict:
    return {"ts_utc": ts, "eur_mwh": 40.0, "ore_kwh": ore}


def _resp(*, forecast: dict | None) -> dict:
    return {
        "zone": "SE3",
        "now": _pt("2026-10-26T10:00:00Z", 44.0),
        "today": [_pt("2026-10-26T10:00:00Z", 52.3), _pt("2026-10-26T11:00:00Z", 60.0)],
        "tomorrow": [_pt("2026-10-27T10:00:00Z", 30.0)],
        "tomorrow_complete": False,
        "fx": {"date": "2026-10-23", "eur_sek": 11.0},
        "vat_factor": 1.25,
        "source": "src",
        "forecast": forecast,
    }


_FORECAST = {
    "issued_at": "2026-10-26T09:00:00Z",
    "points": [
        _pt("2026-10-28T10:00:00Z", 10.0),
        _pt("2026-10-28T11:00:00Z", 20.0),
        _pt("2026-10-28T12:00:00Z", 30.0),
    ],
    "accuracy": {"basis": "upstream", "days": 0, "window_days": 30},
    "source": "fsrc",
}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
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


@pytest.mark.asyncio
async def test_get_prices_now_has_no_auth_header(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    client = WoltaApiClient(async_get_clientsession(hass), base_url=WOLTA_API_BASE)

    data = await client.get_prices_now("SE3")

    assert data["zone"] == "SE3"
    method, url, _body, headers = aioclient_mock.mock_calls[0]
    assert method == "GET"
    assert url.query == {"zone": "SE3"}
    assert "Authorization" not in (headers or {})


def test_price_series_spot_only():
    series = price_series(_resp(forecast=_FORECAST), with_forecast=False)

    assert len(series) == 3
    assert all("forecast" not in p for p in series)
    assert series[0] == {"start": "2026-10-26T10:00:00Z", "price": 0.523}


def test_price_series_with_forecast_flags():
    series = price_series(_resp(forecast=_FORECAST), with_forecast=True)

    assert len(series) == 6
    assert [p["forecast"] for p in series] == [False, False, False, True, True, True]
    assert series[3] == {"start": "2026-10-28T10:00:00Z", "price": 0.1, "forecast": True}


def test_price_series_forecast_null():
    series = price_series(_resp(forecast=None), with_forecast=True)

    assert len(series) == 3
    assert all(p["forecast"] is False for p in series)


def test_price_series_missing_fx():
    resp = _resp(forecast=None)
    resp["today"] = [_pt("2026-10-26T10:00:00Z", None)]
    resp["tomorrow"] = []

    series = price_series(resp, with_forecast=False)

    assert series == [{"start": "2026-10-26T10:00:00Z", "price": None}]


def test_price_series_tolerates_malformed_points():
    """The attributes are built on every state write: a bad point must never raise."""
    resp = _resp(forecast=None)
    resp["today"] = [
        {"eur_mwh": 40.0, "ore_kwh": 10.0},  # no ts_utc: skipped
        "garbage",  # not a point: skipped
        {"ts_utc": "2026-10-26T10:00:00Z", "ore_kwh": "abc"},  # non-numeric: price None
        {"ts_utc": "2026-10-26T10:15:00Z", "ore_kwh": True},  # bool is not a price
        _pt("2026-10-26T10:30:00Z", 20.0),
    ]
    resp["tomorrow"] = None

    series = price_series(resp, with_forecast=False)

    assert series == [
        {"start": "2026-10-26T10:00:00Z", "price": None},
        {"start": "2026-10-26T10:15:00Z", "price": None},
        {"start": "2026-10-26T10:30:00Z", "price": 0.2},
    ]


@pytest.mark.asyncio
async def test_setup_survives_price_endpoint_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """A failing price endpoint must never stop the entry from loading."""
    aioclient_mock.post(f"{WOLTA_API_BASE}/api/v1/profile/link", json={"link_token": "wpl_x"})
    aioclient_mock.get(PRICES_URL, status=500)
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wolta.coordinator.WoltaCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        # The first price refresh runs as a background task (see async_setup_entry).
        await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.LOADED
    prices = entry.runtime_data.prices
    assert prices is not None
    assert prices.last_update_success is False


@pytest.mark.asyncio
async def test_setup_does_not_wait_for_price_refresh(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """A hung price endpoint must not delay the existing sensors: platform forwarding
    happens while the first price refresh is still pending."""
    import asyncio

    aioclient_mock.post(f"{WOLTA_API_BASE}/api/v1/profile/link", json={"link_token": "wpl_x"})
    entry = _entry()
    entry.add_to_hass(hass)
    release = asyncio.Event()

    async def _hung_refresh(self) -> None:
        await release.wait()

    try:
        with (
            patch(
                "custom_components.wolta.coordinator.WoltaCoordinator.async_config_entry_first_refresh",
                new_callable=AsyncMock,
            ),
            patch(
                "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
                new_callable=AsyncMock,
            ) as forward,
            patch.object(WoltaPriceCoordinator, "async_refresh", _hung_refresh),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            assert entry.state is ConfigEntryState.LOADED
            forward.assert_awaited_once()
            assert entry.runtime_data.prices is not None
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.asyncio
async def test_price_coordinator_follows_zone_change(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """The options step "Correct the price zone" updates entry.data without a reload;
    the next price poll must use the new zone, not the one from setup."""
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    entry = _entry()
    entry.add_to_hass(hass)
    client = WoltaApiClient(async_get_clientsession(hass), base_url=WOLTA_API_BASE)
    coordinator = WoltaPriceCoordinator(hass, entry, client)

    await coordinator.async_refresh()
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONE: "SE4"})
    await coordinator.async_refresh()

    zones = [call[1].query["zone"] for call in aioclient_mock.mock_calls]
    assert zones == ["SE3", "SE4"]
