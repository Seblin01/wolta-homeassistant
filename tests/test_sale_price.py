"""Tests for the sale-price sensor and the two purchase/sale forecast sensors (v0.54.0),
all fed by the same GET /profile/prices answer (API 0.119.0): the series helpers, the
coordinator's keep rule and the sensors."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.wolta.const import DOMAIN, WOLTA_API_BASE
from custom_components.wolta.price_coordinator import (
    current_purchase,
    current_sale,
    purchase_series,
    sale_covers,
    sale_series,
    with_forecast_series,
)
from custom_components.wolta.sensor import (
    WoltaPurchasePriceWithForecastSensor,
    WoltaSalePriceSensor,
    WoltaSalePriceWithForecastSensor,
)
from tests.test_price_coordinator import PRICES_URL, _resp
from tests.test_purchase_price import (
    _COMPONENTS,
    PURCHASE_ID,
    PURCHASE_URL,
    _coordinator,
    _poll_purchase,
    _purchase_calls,
)
from tests.test_price_sensor import NOW, SPOT_ID, UNIQUE_ID, _entity_id, _setup

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

SALE_ID = f"{UNIQUE_ID}_sale_price"
PURCHASE_FC_ID = f"{UNIQUE_ID}_purchase_price_with_forecast"
SALE_FC_ID = f"{UNIQUE_ID}_sale_price_with_forecast"
FORECAST_SOURCE = "Prognos: EpexPredictor, energy-charts, Open-Meteo"

_SALE_COMPONENTS = {
    **_COMPONENTS,
    "grid_benefit_ore": 6.0,
    "export_extra_ore": -1.0,
    "export_extra_pct": None,
    "export_credit_ore": {"2026": 0.0},
    # Time periods and today's contract (spec 2026-10-27-marknadsanpassning): handed over as the
    # server sends them.
    "grid_fee_periods": [{"months": [1, 2, 3, 11, 12], "days": "weekdays", "start_hour": 6,
                          "end_hour": 22, "price_ore": 61.2}],
    "grid_fee_holidays": "public_eves",
    "grid_benefit_periods": [{"months": [1, 2, 3, 11, 12], "days": "weekdays", "start_hour": 6,
                              "end_hour": 22, "price_ore": 10.0}],
    "grid_benefit_holidays": "public_eves",
    "retail_price_model": "quarter",
    "retail_fixed_ore": None,
}


@pytest.fixture(autouse=True)
def _frozen_now(freezer) -> None:
    freezer.move_to(NOW)


def _pt(ts, spot, purchase, sale, in_period, *, with_purchase=True):
    # `in_period`: the quarter lies in the SALE side's time period, so its grid benefit is the period
    # price (10.0) instead of the base (6.0). The purchase side follows its own periods; here it
    # is the opposite (the fee's period price 61.2 when the benefit has its base), so a series
    # that read the other side's value would show.
    point = {"ts_utc": ts, "spot_ore_kwh": spot, "sale_ore_kwh": sale,
             "grid_benefit_ore": 10.0 if in_period else 6.0,
             "grid_fee_ore": 25.0 if in_period else 61.2}
    if with_purchase:
        point["purchase_ore_kwh"] = purchase
    return point


def _response(
    *,
    available: bool = True,
    reason: str | None = None,
    sale_available: bool = True,
    sale_reason: str | None = None,
    forecast: bool = True,
) -> dict:
    """The /profile/prices answer of API 0.119.0 for the frozen clock (2026-10-26 10:05 UTC).

    Today: 10:00 (covers now) and 10:15 (in the sale side's time period, as test data). Tomorrow: one quarter
    with no purchase price. Forecast: tomorrow's 10:00 again (published prices win) and 11:00,
    and day 2; the days are listed out of order on purpose."""
    p = available
    s = sale_available

    def pt(ts, spot, purchase, sale, in_period):
        return _pt(
            ts, spot, purchase if p else None, sale if s else None, in_period, with_purchase=p
        )

    return {
        "zone": "SE3",
        "available": available,
        "reason": reason,
        "sale_available": sale_available,
        "sale_reason": sale_reason,
        "components": _SALE_COMPONENTS,
        "today": [
            pt("2026-10-26T10:00:00Z", 52.3, 123.4, 60.35, False),
            pt("2026-10-26T10:15:00Z", 60.0, 130.0, 68.0, True),
        ],
        "tomorrow": [pt("2026-10-27T10:00:00Z", 30.0, None, 38.0, False)],
        "tomorrow_complete": False,
        "fx": {"date": "2026-10-23", "eur_sek": 11.0},
        "forecast": (
            [
                {
                    "date": "2026-10-28",
                    "horizon": 2,
                    "issued_at": "2026-10-26T09:00:00Z",
                    "points": [pt("2026-10-28T10:00:00Z", 20.0, 90.0, 27.5, False)],
                },
                {
                    "date": "2026-10-27",
                    "horizon": 1,
                    "issued_at": "2026-10-26T09:00:00Z",
                    "points": [
                        pt("2026-10-27T11:00:00Z", 25.0, 100.0, 33.0, True),
                        pt("2026-10-27T10:00:00Z", 999.0, 999.0, 999.0, True),
                    ],
                },
            ]
            if forecast
            else []
        ),
        "forecast_source": FORECAST_SOURCE if forecast else None,
    }


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# --- sale series / current ----------------------------------------------------


def test_sale_series_is_sek_per_kwh_with_the_grid_benefit_and_keeps_null_sale():
    resp = _response()
    resp["tomorrow"][0]["sale_ore_kwh"] = None

    assert sale_series(resp) == [
        {"start": "2026-10-26T10:00:00Z", "sale": 0.6035, "spot": 0.523, "grid_benefit": 0.06},
        {"start": "2026-10-26T10:15:00Z", "sale": 0.68, "spot": 0.6, "grid_benefit": 0.1},
        {"start": "2026-10-27T10:00:00Z", "sale": None, "spot": 0.3, "grid_benefit": 0.06},
    ]


def test_sale_series_has_no_sale_when_the_server_has_none():
    resp = _response(sale_available=False, sale_reason="grid_benefit_not_set")

    assert [p["sale"] for p in sale_series(resp)] == [None, None, None]
    assert sale_series(resp)[0]["spot"] == 0.523


@pytest.mark.parametrize("bad", [None, "x", {"today": "x"}, {"today": [None, 5, {"sale_ore_kwh": 1}]}])
def test_sale_series_tolerates_garbage(bad):
    assert sale_series(bad) == []


def test_sale_series_tolerates_non_numeric_values_and_a_missing_grid_benefit():
    """A missing grid_benefit_ore (the owner has not given the base price) is None, not a guess."""
    resp = _response()
    resp["today"][0]["sale_ore_kwh"] = True
    resp["today"][0]["spot_ore_kwh"] = "52"
    del resp["today"][0]["grid_benefit_ore"]
    resp["today"][1]["grid_benefit_ore"] = None

    assert sale_series(resp)[0] == {
        "start": "2026-10-26T10:00:00Z", "sale": None, "spot": None, "grid_benefit": None}
    assert sale_series(resp)[1]["grid_benefit"] is None


def test_purchase_series_carries_the_purchase_sides_own_grid_fee():
    """grid_fee (the fee's own periods), never the sale side's grid benefit, and the other way
    round."""
    assert purchase_series(_response())[0] == {
        "start": "2026-10-26T10:00:00Z", "purchase": 1.234, "spot": 0.523, "grid_fee": 0.612}
    assert [p["grid_fee"] for p in purchase_series(_response())] == [0.612, 0.25, 0.612]
    assert [p["grid_benefit"] for p in sale_series(_response())] == [0.06, 0.1, 0.06]
    assert all("grid_benefit" not in p for p in purchase_series(_response()))
    assert all("grid_fee" not in p for p in sale_series(_response()))


def test_grid_fee_is_the_servers_whole_variable_fee_with_nothing_added():
    """grid_fee_ore already holds the percentage of spot (the server's grid_fee_term_ore): the
    series divides by 100 and adds nothing, whatever the components say."""
    resp = _response()
    resp["components"] = {**resp["components"], "grid_var_pct": 10.0, "grid_var_ore": 25.0}
    resp["today"][0]["grid_fee_ore"] = 30.23
    assert purchase_series(resp)[0]["grid_fee"] == 0.3023


def test_grid_benefit_is_the_servers_whole_benefit_term_with_nothing_added():
    """grid_benefit_ore already holds the grid benefit's percentage of spot (the server's
    grid_benefit_term_ore, its ruling 17): the series divides by 100 and adds nothing, whatever
    the components say, so the sale price is spot + grid_benefit + the retailer's terms + the
    tax reduction."""
    resp = _response()
    resp["components"] = {**resp["components"], "export_extra_pct": 2.0, "grid_benefit_ore": 6.0}
    resp["today"][0]["grid_benefit_ore"] = 7.05
    assert sale_series(resp)[0]["grid_benefit"] == 0.0705


def test_the_never_released_flags_are_not_read_any_more():
    """grid_peak, grid_fee_peak and grid_benefit_peak were never released: an answer that only
    has them gives no grid fee and no grid benefit, and no flag reaches the attributes."""
    resp = _response()
    for point in resp["today"] + resp["tomorrow"]:
        point.update({"grid_peak": True, "grid_fee_peak": True, "grid_benefit_peak": False})
        del point["grid_benefit_ore"], point["grid_fee_ore"]
    assert [p["grid_benefit"] for p in sale_series(resp)] == [None, None, None]
    assert [p["grid_fee"] for p in purchase_series(resp)] == [None, None, None]
    for point in sale_series(resp) + purchase_series(resp):
        assert not {"grid_peak", "grid_fee_peak", "grid_benefit_peak"} & set(point)


@pytest.mark.parametrize("bad", ["25", True, [25.0], {"x": 1}])
def test_purchase_series_grid_fee_is_none_for_a_non_number(bad):
    resp = _response()
    resp["today"][0]["grid_fee_ore"] = bad
    assert purchase_series(resp)[0]["grid_fee"] is None


def test_current_sale_follows_the_quarter_in_utc():
    resp = _response()
    assert current_sale(resp, _utc(2026, 10, 26, 10, 5)) == 0.6035
    assert current_sale(resp, _utc(2026, 10, 26, 10, 15)) == 0.68
    # The last of today's points ends at 10:30 and tomorrow's is a day later.
    assert current_sale(resp, _utc(2026, 10, 26, 10, 30)) is None
    assert current_sale(resp, _utc(2026, 10, 26, 9, 59)) is None


def test_current_sale_none_when_sale_is_not_available_even_with_values():
    resp = _response()
    resp["sale_available"] = False
    resp["sale_reason"] = "grid_benefit_not_set"
    assert current_sale(resp, _utc(2026, 10, 26, 10, 5)) is None
    assert current_sale(None, _utc(2026, 10, 26, 10, 5)) is None
    assert current_sale(_response(), _utc(2026, 10, 26, 10, 5)) is not None
    # The purchase side does not depend on the sale side.
    assert current_purchase(resp, _utc(2026, 10, 26, 10, 5)) == 1.234


def test_current_sale_none_for_a_quarter_without_a_sale_price():
    resp = _response()
    resp["today"][0]["sale_ore_kwh"] = None
    assert current_sale(resp, _utc(2026, 10, 26, 10, 5)) is None


def test_sale_covers_only_while_a_quarter_covers_now():
    resp = _response(sale_available=False, sale_reason="grid_benefit_not_set")
    assert sale_covers(resp, _utc(2026, 10, 26, 10, 5)) is True
    assert sale_covers(resp, _utc(2026, 10, 26, 10, 30)) is False
    assert sale_covers(None, _utc(2026, 10, 26, 10, 5)) is False


# --- series with forecast ----------------------------------------------------------


def test_sale_forecast_series_marks_forecast_points_and_never_repeats_a_timestamp():
    series = with_forecast_series(_response(), "sale")

    assert series == [
        {"start": "2026-10-26T10:00:00Z", "sale": 0.6035, "spot": 0.523, "grid_benefit": 0.06,
         "forecast": False},
        {"start": "2026-10-26T10:15:00Z", "sale": 0.68, "spot": 0.6, "grid_benefit": 0.1,
         "forecast": False},
        # 2026-10-27T10:00 is tomorrow's published price; the forecast copy (999) loses.
        {"start": "2026-10-27T10:00:00Z", "sale": 0.38, "spot": 0.3, "grid_benefit": 0.06,
         "forecast": False},
        {"start": "2026-10-27T11:00:00Z", "sale": 0.33, "spot": 0.25, "grid_benefit": 0.1,
         "forecast": True, "horizon": 1},
        {"start": "2026-10-28T10:00:00Z", "sale": 0.275, "spot": 0.2, "grid_benefit": 0.06,
         "forecast": True, "horizon": 2},
    ]
    starts = [p["start"] for p in series]
    assert starts == sorted(starts) and len(starts) == len(set(starts))


def test_purchase_forecast_series_has_purchase_and_the_same_marking():
    series = with_forecast_series(_response(), "purchase")

    assert series[0] == {
        "start": "2026-10-26T10:00:00Z", "purchase": 1.234, "spot": 0.523, "grid_fee": 0.612,
        "forecast": False}
    assert series[2] == {
        "start": "2026-10-27T10:00:00Z", "purchase": None, "spot": 0.3, "grid_fee": 0.612,
        "forecast": False}
    assert series[3] == {
        "start": "2026-10-27T11:00:00Z", "purchase": 1.0, "spot": 0.25, "grid_fee": 0.25,
        "forecast": True, "horizon": 1}
    assert [p.get("horizon") for p in series] == [None, None, None, 1, 2]
    assert all("grid_benefit" not in p for p in series)


def test_forecast_series_without_a_forecast_is_just_the_published_prices():
    resp = _response(forecast=False)
    assert [p["start"] for p in with_forecast_series(resp, "sale")] == [
        "2026-10-26T10:00:00Z", "2026-10-26T10:15:00Z", "2026-10-27T10:00:00Z"]
    resp["forecast"] = None
    assert len(with_forecast_series(resp, "purchase")) == 3
    del resp["forecast"]  # a server before 0.119.0
    assert len(with_forecast_series(resp, "sale")) == 3


@pytest.mark.parametrize("bad", [None, "x", {"forecast": "x"}, {"forecast": [None, 3, {"points": 4}]}])
@pytest.mark.parametrize("key", ["purchase", "sale"])
def test_forecast_series_tolerates_garbage(bad, key):
    assert with_forecast_series(bad, key) == []


def test_forecast_series_skips_bad_forecast_days_and_points():
    resp = _response()
    resp["forecast"].insert(0, {"date": "2026-10-29", "horizon": 3, "points": [None, {"x": 1}]})
    resp["forecast"].append("junk")
    series = with_forecast_series(resp, "sale")
    assert [p["start"] for p in series][-2:] == ["2026-10-27T11:00:00Z", "2026-10-28T10:00:00Z"]


def test_forecast_series_horizon_defaults_to_zero_when_missing():
    resp = _response()
    del resp["forecast"][0]["horizon"]
    assert with_forecast_series(resp, "sale")[-1]["horizon"] == 0


def test_forecast_series_rejects_an_unknown_key():
    with pytest.raises(ValueError):
        with_forecast_series(_response(), "spot")


# --- coordinator keep rule ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sale_only_answer_is_kept_across_a_failed_poll(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """`available: false` with `sale_available: true` is a priced answer for the sale side:
    one failed poll must not blank the sale sensor (purchase_covers/sale_covers bound it)."""
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    answer = await _poll_purchase(
        aioclient_mock, coordinator,
        json=_response(available=False, reason="tariff_not_set"))
    assert answer["sale_available"] is True

    assert await _poll_purchase(aioclient_mock, coordinator, status=500) == answer


@pytest.mark.asyncio
async def test_a_purchase_only_answer_is_still_kept(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    answer = await _poll_purchase(
        aioclient_mock, coordinator,
        json=_response(sale_available=False, sale_reason="grid_benefit_not_set"))

    assert await _poll_purchase(aioclient_mock, coordinator, status=500) == answer


@pytest.mark.asyncio
async def test_an_answer_with_no_price_at_all_is_not_kept(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    await _poll_purchase(
        aioclient_mock, coordinator,
        json=_response(
            available=False, reason="tariff_not_set",
            sale_available=False, sale_reason="grid_benefit_not_set"))

    assert await _poll_purchase(aioclient_mock, coordinator, status=500) is None


@pytest.mark.asyncio
async def test_the_newest_answer_replaces_a_kept_sale_answer(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    await _poll_purchase(aioclient_mock, coordinator, json=_response())
    await _poll_purchase(
        aioclient_mock, coordinator,
        json=_response(
            available=False, reason="tariff_not_set",
            sale_available=False, sale_reason="grid_benefit_not_set"))

    assert await _poll_purchase(aioclient_mock, coordinator, status=500) is None


# --- sensors -----------------------------------------------------------------------


async def _setup_sensors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    enabled: tuple[str, ...],
    *,
    purchase_json: dict | None = None,
    purchase_status: int = 200,
):
    """Set the integration up with the given price-sensor unique ids switched on (they are
    disabled by default); the registry entries are made the way a user who enabled them
    would have them, right when the config entry is added."""
    if purchase_status == 200:
        aioclient_mock.get(PURCHASE_URL, json=purchase_json or _response())
    else:
        aioclient_mock.get(PURCHASE_URL, status=purchase_status)
    original = MockConfigEntry.add_to_hass

    def _add(self, hass_):
        original(self, hass_)
        for unique_id in enabled:
            er.async_get(hass_).async_get_or_create(
                "sensor", DOMAIN, unique_id, config_entry=self, disabled_by=None
            )

    with patch.object(MockConfigEntry, "add_to_hass", _add):
        return await _setup(hass, aioclient_mock)


ALL_NEW = (SALE_ID, PURCHASE_FC_ID, SALE_FC_ID)


@pytest.mark.asyncio
async def test_the_three_sensors_are_disabled_by_default_and_make_no_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """None of the four price sensors enabled (purchase, sale, purchase with forecast, sale
    with forecast) -> /profile/prices is never called, however often the coordinator polls."""
    aioclient_mock.get(PURCHASE_URL, json=_response())
    entry = await _setup(hass, aioclient_mock)
    await entry.runtime_data.prices.async_refresh()
    await entry.runtime_data.prices.async_refresh()

    registry = er.async_get(hass)
    for unique_id in (PURCHASE_ID, *ALL_NEW):
        reg = registry.async_get(_entity_id(hass, unique_id))
        assert reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION, unique_id
        assert hass.states.get(reg.entity_id) is None, unique_id
    assert hass.states.get(_entity_id(hass, SPOT_ID)).state == "0.523"
    assert _purchase_calls(aioclient_mock) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unique_id", ALL_NEW)
async def test_each_new_sensor_alone_makes_the_one_request_per_poll(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, unique_id: str
):
    entry = await _setup_sensors(hass, aioclient_mock, (unique_id,))
    assert len(_purchase_calls(aioclient_mock)) == 1

    await entry.runtime_data.prices.async_refresh()
    assert len(_purchase_calls(aioclient_mock)) == 2


@pytest.mark.asyncio
async def test_all_the_price_sensors_together_share_one_request_per_poll(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    entry = await _setup_sensors(hass, aioclient_mock, (PURCHASE_ID, *ALL_NEW))
    assert len(_purchase_calls(aioclient_mock)) == 1

    await entry.runtime_data.prices.async_refresh()
    assert len(_purchase_calls(aioclient_mock)) == 2


@pytest.mark.asyncio
async def test_sale_sensor_state_attributes_and_unit(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup_sensors(hass, aioclient_mock, (SALE_ID,))

    state = hass.states.get(_entity_id(hass, SALE_ID))

    assert state.state == "0.6035"
    assert state.attributes["unit_of_measurement"] == "SEK/kWh"
    assert "device_class" not in state.attributes
    assert state.attributes["reason"] is None
    assert state.attributes["components"] == _SALE_COMPONENTS
    assert state.attributes["tomorrow_complete"] is False
    assert state.attributes["prices"] == [
        {"start": "2026-10-26T10:00:00Z", "sale": 0.6035, "spot": 0.523, "grid_benefit": 0.06},
        {"start": "2026-10-26T10:15:00Z", "sale": 0.68, "spot": 0.6, "grid_benefit": 0.1},
        {"start": "2026-10-27T10:00:00Z", "sale": 0.38, "spot": 0.3, "grid_benefit": 0.06},
    ]
    # Published prices only: no forecast key, nothing to filter out.
    assert "forecast_source" not in state.attributes
    assert state.attributes.get("attribution") == "Price source"


@pytest.mark.asyncio
async def test_sale_sensor_follows_quarter_boundaries_without_a_poll(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    await _setup_sensors(hass, aioclient_mock, (SALE_ID,))
    entity_id = _entity_id(hass, SALE_ID)
    assert hass.states.get(entity_id).state == "0.6035"
    polls = len(aioclient_mock.mock_calls)

    freezer.move_to("2026-10-26T10:15:02+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "0.68"
    assert len(aioclient_mock.mock_calls) == polls

    # Nothing covers 10:30: unavailable, not a stale price.
    freezer.move_to("2026-10-26T10:30:02+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"


@pytest.mark.asyncio
async def test_quarter_change_follows_the_utc_timestamps_over_local_midnight(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    """Local midnight on 2026-10-27 is 23:00 UTC (winter time). A point covers the 15
    minutes after its ts_utc, whatever the local clock says, and the state flips there
    without a poll."""
    resp = _response()
    resp["today"] = [
        _pt("2026-10-26T22:45:00Z", 10.0, 50.0, 20.0, False),
        _pt("2026-10-26T23:00:00Z", 11.0, 51.0, 21.0, False),
    ]
    resp["tomorrow"] = []
    resp["forecast"] = []
    freezer.move_to("2026-10-26T22:50:00+00:00")
    await _setup_sensors(hass, aioclient_mock, (SALE_ID,), purchase_json=resp)
    entity_id = _entity_id(hass, SALE_ID)
    assert hass.states.get(entity_id).state == "0.2"

    freezer.move_to("2026-10-26T23:00:02+00:00")  # 00:00 local time
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "0.21"


@pytest.mark.asyncio
async def test_sale_unavailable_while_purchase_is_available(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """The sale price lacks the grid benefit; the purchase price is fine. The two sides have
    separate availability and reasons."""
    await _setup_sensors(
        hass, aioclient_mock, (PURCHASE_ID, SALE_ID, SALE_FC_ID),
        purchase_json=_response(sale_available=False, sale_reason="grid_benefit_not_set"))

    sale = hass.states.get(_entity_id(hass, SALE_ID))
    sale_fc = hass.states.get(_entity_id(hass, SALE_FC_ID))
    purchase = hass.states.get(_entity_id(hass, PURCHASE_ID))

    # `unknown`, not `unavailable`: Home Assistant hides the attributes of an unavailable entity.
    assert sale.state == "unknown"
    assert sale.attributes["reason"] == "grid_benefit_not_set"
    assert [p["sale"] for p in sale.attributes["prices"]] == [None, None, None]
    assert sale.attributes["prices"][0]["spot"] == 0.523
    assert sale_fc.state == "unknown"
    assert sale_fc.attributes["reason"] == "grid_benefit_not_set"
    assert purchase.state == "1.234"
    assert purchase.attributes["reason"] is None


@pytest.mark.asyncio
async def test_purchase_unavailable_while_sale_is_available(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup_sensors(
        hass, aioclient_mock, (PURCHASE_ID, PURCHASE_FC_ID, SALE_ID),
        purchase_json=_response(available=False, reason="tariff_not_set"))

    purchase = hass.states.get(_entity_id(hass, PURCHASE_ID))
    purchase_fc = hass.states.get(_entity_id(hass, PURCHASE_FC_ID))
    sale = hass.states.get(_entity_id(hass, SALE_ID))

    assert purchase.state == "unknown"
    assert purchase.attributes["reason"] == "tariff_not_set"
    assert purchase_fc.state == "unknown"
    assert purchase_fc.attributes["reason"] == "tariff_not_set"
    assert sale.state == "0.6035"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["zone_not_supported", "grid_benefit_not_set", "fx_missing"]
)
async def test_sale_sensor_shows_the_servers_sale_reason(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, reason: str
):
    await _setup_sensors(
        hass, aioclient_mock, (SALE_ID,),
        purchase_json=_response(sale_available=False, sale_reason=reason))

    state = hass.states.get(_entity_id(hass, SALE_ID))
    assert state.state == "unknown"
    assert state.attributes["reason"] == reason


@pytest.mark.asyncio
async def test_sale_sensor_unknown_for_a_quarter_without_a_sale_price(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    resp = _response()
    resp["today"][1]["sale_ore_kwh"] = None  # e.g. a quarter whose spot price is null
    await _setup_sensors(hass, aioclient_mock, (SALE_ID,), purchase_json=resp)
    entity_id = _entity_id(hass, SALE_ID)

    freezer.move_to("2026-10-26T10:15:02+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == "unknown"
    assert state.attributes["reason"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 429, 500])
async def test_endpoint_error_makes_only_the_new_sensors_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
):
    await _setup_sensors(
        hass, aioclient_mock, ALL_NEW, purchase_status=status)

    for unique_id in ALL_NEW:
        assert hass.states.get(_entity_id(hass, unique_id)).state == "unavailable", unique_id
    assert hass.states.get(_entity_id(hass, SPOT_ID)).state == "0.523"


@pytest.mark.asyncio
async def test_sale_sensor_keeps_its_price_through_a_failed_poll_while_the_series_covers_now(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    """A sale-only answer (purchase unavailable) is kept like a priced one."""
    entry = await _setup_sensors(
        hass, aioclient_mock, (SALE_ID,),
        purchase_json=_response(available=False, reason="tariff_not_set"))
    entity_id = _entity_id(hass, SALE_ID)
    assert hass.states.get(entity_id).state == "0.6035"

    aioclient_mock.clear_requests()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, status=500)
    freezer.move_to("2026-10-26T10:20:05+00:00")
    await entry.runtime_data.prices.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "0.68"

    freezer.move_to("2026-10-26T10:35:05+00:00")
    await entry.runtime_data.prices.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"


@pytest.mark.asyncio
async def test_forecast_sensors_carry_forecast_points_source_and_attribution(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup_sensors(hass, aioclient_mock, (PURCHASE_ID, PURCHASE_FC_ID, SALE_ID, SALE_FC_ID))

    purchase_fc = hass.states.get(_entity_id(hass, PURCHASE_FC_ID))
    sale_fc = hass.states.get(_entity_id(hass, SALE_FC_ID))
    plain_purchase = hass.states.get(_entity_id(hass, PURCHASE_ID))
    plain_sale = hass.states.get(_entity_id(hass, SALE_ID))

    # The state is the current published price, the same as the plain sensors.
    assert purchase_fc.state == plain_purchase.state == "1.234"
    assert sale_fc.state == plain_sale.state == "0.6035"
    # The plain sensors are untouched: no forecast points, no source.
    assert len(plain_purchase.attributes["prices"]) == 3
    assert len(plain_sale.attributes["prices"]) == 3
    assert "forecast_source" not in plain_sale.attributes
    assert "forecast_source" not in plain_purchase.attributes

    assert [p["forecast"] for p in sale_fc.attributes["prices"]] == [False] * 3 + [True] * 2
    assert [p.get("horizon") for p in sale_fc.attributes["prices"]] == [None] * 3 + [1, 2]
    assert [p["forecast"] for p in purchase_fc.attributes["prices"]] == [False] * 3 + [True] * 2
    assert sale_fc.attributes["prices"][-1]["grid_benefit"] == 0.06
    assert purchase_fc.attributes["prices"][-1]["grid_fee"] == 0.612
    for state in (purchase_fc, sale_fc):
        assert state.attributes["forecast_source"] == FORECAST_SOURCE
        assert state.attributes["attribution"] == f"Price source {FORECAST_SOURCE}"
        assert state.attributes["forecast_days"] == [
            {"date": "2026-10-27", "horizon": 1, "issued_at": "2026-10-26T09:00:00Z"},
            {"date": "2026-10-28", "horizon": 2, "issued_at": "2026-10-26T09:00:00Z"},
        ]
        assert state.attributes["components"] == _SALE_COMPONENTS


@pytest.mark.asyncio
async def test_forecast_sensors_without_a_forecast_behave_like_the_plain_ones(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup_sensors(
        hass, aioclient_mock, (PURCHASE_FC_ID, SALE_FC_ID),
        purchase_json=_response(forecast=False))

    for unique_id in (PURCHASE_FC_ID, SALE_FC_ID):
        state = hass.states.get(_entity_id(hass, unique_id))
        assert len(state.attributes["prices"]) == 3, unique_id
        assert all(p["forecast"] is False for p in state.attributes["prices"]), unique_id
        assert state.attributes["forecast_source"] is None
        assert state.attributes["forecast_days"] == []
        assert state.attributes["attribution"] == "Price source"


@pytest.mark.asyncio
async def test_forecast_sensors_work_against_a_server_before_0_119_0(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """API 0.118.x answers /profile/prices without the sale and forecast fields. The
    purchase sensor works as before; the sale sensors show nothing rather than guessing."""
    resp = _response(forecast=False)
    for key in ("sale_available", "sale_reason", "forecast", "forecast_source"):
        del resp[key]
    for point in resp["today"] + resp["tomorrow"]:
        del point["sale_ore_kwh"], point["grid_benefit_ore"], point["grid_fee_ore"]
    await _setup_sensors(
        hass, aioclient_mock, (PURCHASE_ID, PURCHASE_FC_ID, SALE_ID), purchase_json=resp)

    assert hass.states.get(_entity_id(hass, PURCHASE_ID)).state == "1.234"
    assert hass.states.get(_entity_id(hass, PURCHASE_FC_ID)).state == "1.234"
    sale = hass.states.get(_entity_id(hass, SALE_ID))
    assert sale.state == "unknown"
    assert sale.attributes["reason"] is None


@pytest.mark.parametrize(
    "cls", [WoltaSalePriceSensor, WoltaPurchasePriceWithForecastSensor, WoltaSalePriceWithForecastSensor]
)
def test_prices_attribute_not_recorded(cls):
    assert "prices" in cls._unrecorded_attributes


_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "wolta"


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json", "translations/sv.json"])
def test_the_new_sensors_have_names(name: str):
    data = json.loads((_COMPONENT / name).read_text(encoding="utf-8"))
    sensors = data["entity"]["sensor"]
    for key in ("sale_price", "purchase_price_with_forecast", "sale_price_with_forecast"):
        assert sensors[key]["name"], f"{name}: {key}"


# --- the side without a price in a kept half-priced answer ---------------------------


async def _fail_polls_past_coverage(hass, aioclient_mock, freezer, entry, entity_ids):
    """Fail the fetch from now on; the kept answer covers 10:20 and not 10:35."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, status=500)
    prices = entry.runtime_data.prices

    freezer.move_to("2026-10-26T10:20:05+00:00")
    await prices.async_refresh()
    await hass.async_block_till_done()
    covered = [hass.states.get(e) for e in entity_ids]

    freezer.move_to("2026-10-26T10:35:05+00:00")
    await prices.async_refresh()
    await hass.async_block_till_done()
    return covered, [hass.states.get(e).state for e in entity_ids]


@pytest.mark.asyncio
async def test_purchase_sensor_without_a_price_goes_unavailable_when_a_kept_sale_only_answer_stops_covering(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    """A sale-only answer is kept across a failed fetch. The purchase sensor sits at
    `unknown` with its reason while the answer covers now, and is `unavailable` once it does
    not, not stuck on an old reason."""
    entry = await _setup_sensors(
        hass, aioclient_mock, (PURCHASE_ID, SALE_ID),
        purchase_json=_response(available=False, reason="tariff_not_set"))
    ids = [_entity_id(hass, PURCHASE_ID), _entity_id(hass, SALE_ID)]
    assert hass.states.get(ids[0]).state == "unknown"

    covered, after = await _fail_polls_past_coverage(hass, aioclient_mock, freezer, entry, ids)

    assert covered[0].state == "unknown" and covered[0].attributes["reason"] == "tariff_not_set"
    assert covered[1].state == "0.68"
    assert after == ["unavailable", "unavailable"]


@pytest.mark.asyncio
async def test_sale_sensor_without_a_price_goes_unavailable_when_a_kept_purchase_only_answer_stops_covering(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    entry = await _setup_sensors(
        hass, aioclient_mock, (PURCHASE_ID, SALE_ID),
        purchase_json=_response(sale_available=False, sale_reason="grid_benefit_not_set"))
    ids = [_entity_id(hass, PURCHASE_ID), _entity_id(hass, SALE_ID)]
    assert hass.states.get(ids[1]).state == "unknown"

    covered, after = await _fail_polls_past_coverage(hass, aioclient_mock, freezer, entry, ids)

    assert covered[1].state == "unknown"
    assert covered[1].attributes["reason"] == "grid_benefit_not_set"
    assert covered[0].state == "1.3"
    assert after == ["unavailable", "unavailable"]


@pytest.mark.asyncio
async def test_an_answer_without_any_points_keeps_showing_its_reason(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    """Nothing to bound: a zone the server does not price answers with a reason and no
    points, and that reason must stay readable."""
    resp = _response(
        available=False, reason="zone_not_supported",
        sale_available=False, sale_reason="zone_not_supported", forecast=False)
    resp["today"], resp["tomorrow"] = [], []
    await _setup_sensors(hass, aioclient_mock, (PURCHASE_ID, SALE_ID), purchase_json=resp)

    freezer.move_to("2026-10-26T14:00:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    for unique_id in (PURCHASE_ID, SALE_ID):
        state = hass.states.get(_entity_id(hass, unique_id))
        assert state.state == "unknown", unique_id
        assert state.attributes["reason"] == "zone_not_supported", unique_id


# --- attribution --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forecast_sensor_attribution_is_the_forecast_source_alone_without_a_price_source(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """/prices/now sends no `source`: the forecast sensors still credit the forecast, and
    the plain sensors have no attribution at all."""
    prices_now = _resp(forecast=None)
    del prices_now["source"]
    aioclient_mock.get(PURCHASE_URL, json=_response())
    original = MockConfigEntry.add_to_hass

    def _add(self, hass_):
        original(self, hass_)
        for unique_id in (SALE_ID, PURCHASE_FC_ID, SALE_FC_ID):
            er.async_get(hass_).async_get_or_create(
                "sensor", DOMAIN, unique_id, config_entry=self, disabled_by=None)

    with patch.object(MockConfigEntry, "add_to_hass", _add):
        await _setup(hass, aioclient_mock, prices_json=prices_now)

    for unique_id in (PURCHASE_FC_ID, SALE_FC_ID):
        state = hass.states.get(_entity_id(hass, unique_id))
        assert state.attributes["attribution"] == FORECAST_SOURCE, unique_id
    assert "attribution" not in hass.states.get(_entity_id(hass, SALE_ID)).attributes
