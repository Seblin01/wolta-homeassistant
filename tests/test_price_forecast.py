"""Tests for the three-day forecast (GET /prices/forecast, v0.52.0): the client method,
the coordinator's opt-in fetch and its failure isolation, and the merged price series."""

from __future__ import annotations

import logging

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.wolta.api import WoltaApiClient
from custom_components.wolta.const import CONF_ZONE, WOLTA_API_BASE
from custom_components.wolta.price_coordinator import (
    FULL_FORECAST_KEY,
    WoltaPriceCoordinator,
    forecast_days,
    price_series,
)
from tests.test_price_coordinator import PRICES_URL, _entry, _pt, _resp

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

FORECAST_URL = f"{WOLTA_API_BASE}/api/v1/prices/forecast"

_ACC = {"basis": "upstream", "days": 0, "window_days": 30, "upstream_mae_ore_kwh": 22}


def _day(date: str, horizon: int, hours: list[int], *, issued: str = "2026-10-26T09:00:00Z"):
    return {
        "date": date,
        "horizon": horizon,
        "issued_at": issued,
        "points": [_pt(f"{date}T{h:02d}:00:00Z", float(h)) for h in hours],
        "accuracy": {**_ACC, "days": horizon},
    }


def _full(*days: dict) -> dict:
    return {
        "zone": "SE3",
        "issued_at": "2026-10-26T09:00:00Z",
        "days": list(days),
        "source": "fsrc",
    }


# Tomorrow's forecast as /prices/now serves it: the evening hours of 2026-10-27.
_NOW_FORECAST = {
    "issued_at": "2026-10-26T09:00:00Z",
    "points": [_pt("2026-10-27T18:00:00Z", 70.0), _pt("2026-10-27T19:00:00Z", 80.0)],
    "accuracy": _ACC,
    "source": "fsrc",
}


def _three_days() -> dict:
    return _full(
        _day("2026-10-27", 1, [18, 19]),
        _day("2026-10-28", 2, [0, 1], issued="2026-10-26T09:05:00Z"),
        _day("2026-10-29", 3, [0, 1], issued="2026-10-26T09:10:00Z"),
    )


# --- price_series / forecast_days -------------------------------------------


def test_price_series_includes_days_two_and_three():
    resp = {**_resp(forecast=_NOW_FORECAST), FULL_FORECAST_KEY: _three_days()}

    series = price_series(resp, with_forecast=True)

    starts = [p["start"] for p in series]
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts)), "a timestamp must never be duplicated"
    # today (2) + tomorrow (1) + horizon 1 (2) + horizon 2 (2) + horizon 3 (2)
    assert len(series) == 9
    by_start = {p["start"]: p for p in series}
    assert by_start["2026-10-26T10:00:00Z"] == {
        "start": "2026-10-26T10:00:00Z",
        "price": 0.523,
        "forecast": False,
    }
    assert "horizon" not in by_start["2026-10-27T10:00:00Z"]
    assert by_start["2026-10-27T18:00:00Z"] == {
        "start": "2026-10-27T18:00:00Z",
        "price": 0.7,
        "forecast": True,
        "horizon": 1,
    }
    assert by_start["2026-10-28T00:00:00Z"]["horizon"] == 2
    assert by_start["2026-10-29T01:00:00Z"] == {
        "start": "2026-10-29T01:00:00Z",
        "price": 0.01,
        "forecast": True,
        "horizon": 3,
    }


def test_price_series_is_sorted_even_if_the_days_arrive_out_of_order():
    full = _full(_day("2026-10-29", 3, [0]), _day("2026-10-28", 2, [0]))
    resp = {**_resp(forecast=None), FULL_FORECAST_KEY: full}

    starts = [p["start"] for p in price_series(resp, with_forecast=True)]

    assert starts == sorted(starts)
    assert starts[-2:] == ["2026-10-28T00:00:00Z", "2026-10-29T00:00:00Z"]


def test_price_series_horizon_one_from_now_wins_over_forecast_endpoint():
    """Horizon 1 is in both sources: /prices/now's copy is used, the other is not added."""
    other = _full(_day("2026-10-27", 1, [18, 19]))
    other["days"][0]["points"][0]["ore_kwh"] = 999.0
    resp = {**_resp(forecast=_NOW_FORECAST), FULL_FORECAST_KEY: other}

    series = price_series(resp, with_forecast=True)

    assert [p["price"] for p in series if p.get("forecast")] == [0.7, 0.8]
    assert len(series) == 5


def test_price_series_skips_timestamps_that_are_already_published():
    # 2026-10-27T10:00Z is already a published tomorrow price.
    resp = {**_resp(forecast=None), FULL_FORECAST_KEY: _full(_day("2026-10-27", 2, [10]))}

    series = price_series(resp, with_forecast=True)

    assert [p["start"] for p in series].count("2026-10-27T10:00:00Z") == 1
    assert all(p["forecast"] is False for p in series)


def test_price_series_spot_only_ignores_full_forecast():
    resp = {**_resp(forecast=_NOW_FORECAST), FULL_FORECAST_KEY: _three_days()}

    series = price_series(resp, with_forecast=False)

    assert len(series) == 3
    assert all(set(p) == {"start", "price"} for p in series)


@pytest.mark.parametrize(
    "full", [None, "garbage", {"days": "x"}, {"days": [None, 5, {"horizon": "2"}]}]
)
def test_price_series_full_forecast_none_or_garbage(full):
    resp = {**_resp(forecast=_NOW_FORECAST), FULL_FORECAST_KEY: full}

    assert len(price_series(resp, with_forecast=True)) == 5
    assert forecast_days(resp) == []


def test_price_series_tolerates_malformed_extra_points():
    day = _day("2026-10-28", 2, [0])
    day["points"] += [{"ore_kwh": 1.0}, "x", {"ts_utc": "2026-10-28T01:00:00Z", "ore_kwh": None}]
    resp = {**_resp(forecast=None), FULL_FORECAST_KEY: _full(day)}

    series = price_series(resp, with_forecast=True)

    assert [p for p in series if p.get("horizon") == 2] == [
        {"start": "2026-10-28T00:00:00Z", "price": 0.0, "forecast": True, "horizon": 2},
        {"start": "2026-10-28T01:00:00Z", "price": None, "forecast": True, "horizon": 2},
    ]


def test_forecast_days_lists_horizons_two_and_three_without_points():
    resp = {**_resp(forecast=_NOW_FORECAST), FULL_FORECAST_KEY: _three_days()}

    assert forecast_days(resp) == [
        {
            "date": "2026-10-28",
            "horizon": 2,
            "issued_at": "2026-10-26T09:05:00Z",
            "accuracy": {**_ACC, "days": 2},
        },
        {
            "date": "2026-10-29",
            "horizon": 3,
            "issued_at": "2026-10-26T09:10:00Z",
            "accuracy": {**_ACC, "days": 3},
        },
    ]


def test_forecast_days_short_list_and_missing():
    short = {**_resp(forecast=None), FULL_FORECAST_KEY: _full(_day("2026-10-28", 2, [0]))}
    assert [d["horizon"] for d in forecast_days(short)] == [2]
    assert forecast_days(_resp(forecast=None)) == []


# --- client ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_price_forecast_has_no_auth_header(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(FORECAST_URL, json=_three_days())
    client = WoltaApiClient(async_get_clientsession(hass), base_url=WOLTA_API_BASE)

    data = await client.get_price_forecast("SE3")

    assert data["days"][1]["horizon"] == 2
    method, url, _body, headers = aioclient_mock.mock_calls[0]
    assert method == "GET"
    assert url.path == "/api/v1/prices/forecast"
    assert url.query == {"zone": "SE3"}
    assert "Authorization" not in (headers or {})


# --- coordinator -----------------------------------------------------------


def _forecast_calls(aioclient_mock: AiohttpClientMocker) -> list:
    return [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(FORECAST_URL)]


def _coordinator(hass: HomeAssistant) -> WoltaPriceCoordinator:
    entry = _entry()
    entry.add_to_hass(hass)
    client = WoltaApiClient(async_get_clientsession(hass), base_url=WOLTA_API_BASE)
    return WoltaPriceCoordinator(hass, entry, client)


@pytest.mark.asyncio
async def test_forecast_endpoint_not_called_without_interest(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=_NOW_FORECAST))
    aioclient_mock.get(FORECAST_URL, json=_three_days())
    coordinator = _coordinator(hass)

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert _forecast_calls(aioclient_mock) == []
    assert coordinator.data.get(FULL_FORECAST_KEY) is None


@pytest.mark.asyncio
async def test_forecast_endpoint_called_once_per_poll_with_interest(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=_NOW_FORECAST))
    aioclient_mock.get(FORECAST_URL, json=_three_days())
    coordinator = _coordinator(hass)
    unregister = coordinator.async_request_full_forecast()

    await coordinator.async_refresh()
    assert len(_forecast_calls(aioclient_mock)) == 1
    assert coordinator.data[FULL_FORECAST_KEY]["days"][2]["horizon"] == 3
    await coordinator.async_refresh()
    assert len(_forecast_calls(aioclient_mock)) == 2
    assert _forecast_calls(aioclient_mock)[0][1].query == {"zone": "SE3"}

    unregister()
    await coordinator.async_refresh()
    assert len(_forecast_calls(aioclient_mock)) == 2
    assert coordinator.data.get(FULL_FORECAST_KEY) is None


@pytest.mark.asyncio
async def test_forecast_interest_is_counted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(FORECAST_URL, json=_three_days())
    coordinator = _coordinator(hass)
    first = coordinator.async_request_full_forecast()
    second = coordinator.async_request_full_forecast()

    first()
    first()  # a repeated unregister must not drop the other registration
    await coordinator.async_refresh()
    assert len(_forecast_calls(aioclient_mock)) == 1

    second()
    await coordinator.async_refresh()
    assert len(_forecast_calls(aioclient_mock)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 404, 429, 200])
async def test_forecast_failure_keeps_prices_now(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
):
    """/prices/forecast failing or answering null must not fail the coordinator."""
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=_NOW_FORECAST))
    if status == 200:
        aioclient_mock.get(FORECAST_URL, json=None)
    else:
        aioclient_mock.get(FORECAST_URL, status=status)
    coordinator = _coordinator(hass)
    coordinator.async_request_full_forecast()

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data["zone"] == "SE3"
    assert coordinator.data[FULL_FORECAST_KEY] is None
    # today, tomorrow and horizon 1 from /prices/now
    assert len(price_series(coordinator.data, with_forecast=True)) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [aiohttp.ClientError("boom"), TimeoutError()])
async def test_forecast_failure_on_network_error_and_timeout(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, exc: Exception
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(FORECAST_URL, exc=exc)
    coordinator = _coordinator(hass)
    coordinator.async_request_full_forecast()

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data[FULL_FORECAST_KEY] is None


@pytest.mark.asyncio
async def test_forecast_failure_is_logged_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, caplog: pytest.LogCaptureFixture
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(FORECAST_URL, status=500)
    coordinator = _coordinator(hass)
    coordinator.async_request_full_forecast()

    with caplog.at_level(logging.DEBUG, logger="custom_components.wolta.price_coordinator"):
        for _ in range(3):
            await coordinator.async_refresh()

    loud = [
        r
        for r in caplog.records
        if r.name == "custom_components.wolta.price_coordinator"
        and r.levelno >= logging.INFO
        and "forecast" in r.getMessage().lower()
    ]
    assert len(loud) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_forecast_recovers_after_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(FORECAST_URL, status=500)
    coordinator = _coordinator(hass)
    coordinator.async_request_full_forecast()
    await coordinator.async_refresh()
    assert coordinator.data[FULL_FORECAST_KEY] is None

    aioclient_mock.clear_requests()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(FORECAST_URL, json=_three_days())
    await coordinator.async_refresh()

    assert coordinator.data[FULL_FORECAST_KEY]["days"][1]["horizon"] == 2


@pytest.mark.asyncio
async def test_prices_now_failure_still_fails_the_coordinator_without_forecast_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, status=500)
    aioclient_mock.get(FORECAST_URL, json=_three_days())
    coordinator = _coordinator(hass)
    coordinator.async_request_full_forecast()

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert _forecast_calls(aioclient_mock) == []


@pytest.mark.asyncio
async def test_non_swedish_zone_makes_no_requests_even_with_interest(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(FORECAST_URL, json=_three_days())
    coordinator = _coordinator(hass)
    coordinator.async_request_full_forecast()
    hass.config_entries.async_update_entry(
        coordinator.config_entry, data={**coordinator.config_entry.data, CONF_ZONE: "NO1"}
    )

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert aioclient_mock.call_count == 0
