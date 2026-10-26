"""Tests for the purchase-price sensor (GET /profile/prices, v0.53.0): the client method,
the coordinator's opt-in fetch and its failure isolation, the series and the sensor."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.wolta.api import WoltaApiClient, WoltaAuthError, WoltaRateLimitError
from custom_components.wolta.const import CONF_ZONE, DOMAIN, WOLTA_API_BASE
from custom_components.wolta.price_coordinator import (
    PURCHASE_KEY,
    WoltaPriceCoordinator,
    current_purchase,
    purchase_covers,
    purchase_series,
)
from custom_components.wolta.sensor import WoltaPurchasePriceSensor
from tests.test_price_coordinator import PRICES_URL, _entry, _resp
from tests.test_price_sensor import NOW, SPOT_ID, UNIQUE_ID, _entity_id, _setup

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")



@pytest.fixture(autouse=True)
def _frozen_now(freezer) -> None:
    """The state is computed locally from the series, so every test needs a fixed clock."""
    freezer.move_to(NOW)


PURCHASE_URL = f"{WOLTA_API_BASE}/api/v1/profile/prices"
PURCHASE_ID = f"{UNIQUE_ID}_purchase_price"
TOKEN = "tok-price-test"

_COMPONENTS = {
    "surcharge_ore": 5.0,
    "grid_var_ore": 25.0,
    "grid_var_pct": None,
    "reduced_energy_tax": False,
    "energy_tax_ore": {"2026": 43.9},
    "vat_factor": 1.25,
}


def _ppt(ts: str, spot: float | None, purchase: float | None, *, omit_purchase: bool = False):
    point = {"ts_utc": ts, "spot_ore_kwh": spot}
    if not omit_purchase:
        point["purchase_ore_kwh"] = purchase
    return point


def _purchase(
    *,
    available: bool = True,
    reason: str | None = None,
) -> dict:
    """The /profile/prices answer for the frozen clock (2026-10-26 10:05 UTC): two quarters
    today (10:00 covers now) and one tomorrow whose purchase price is unknown (null)."""
    omit = not available
    return {
        "zone": "SE3",
        "available": available,
        "reason": reason,
        "components": _COMPONENTS,
        "today": [
            _ppt("2026-10-26T10:00:00Z", 52.3, 123.4, omit_purchase=omit),
            _ppt("2026-10-26T10:15:00Z", 60.0, 130.0, omit_purchase=omit),
        ],
        "tomorrow": [_ppt("2026-10-27T10:00:00Z", 30.0, None, omit_purchase=omit)],
        "tomorrow_complete": False,
        "fx": {"date": "2026-10-23", "eur_sek": 11.0},
    }


# --- series / current -------------------------------------------------------


def test_purchase_series_is_sek_per_kwh_and_keeps_null_purchase():
    series = purchase_series(_purchase())

    assert series == [
        {"start": "2026-10-26T10:00:00Z", "purchase": 1.234, "spot": 0.523},
        {"start": "2026-10-26T10:15:00Z", "purchase": 1.3, "spot": 0.6},
        {"start": "2026-10-27T10:00:00Z", "purchase": None, "spot": 0.3},
    ]


def test_purchase_series_without_purchase_values_has_none():
    series = purchase_series(_purchase(available=False, reason="tariff_not_set"))

    assert [p["purchase"] for p in series] == [None, None, None]
    assert series[0]["spot"] == 0.523


@pytest.mark.parametrize("bad", [None, "x", {"today": "x"}, {"today": [None, 5, {"spot_ore_kwh": 1}]}])
def test_purchase_series_tolerates_garbage(bad):
    assert purchase_series(bad) == []


def test_purchase_series_tolerates_non_numeric_prices():
    resp = _purchase()
    resp["today"][0]["purchase_ore_kwh"] = True
    resp["today"][0]["spot_ore_kwh"] = "52"

    assert purchase_series(resp)[0] == {"start": "2026-10-26T10:00:00Z", "purchase": None, "spot": None}


def test_current_purchase_picks_the_covering_quarter():
    from datetime import datetime, timezone

    resp = _purchase()
    assert current_purchase(resp, datetime(2026, 10, 26, 10, 5, tzinfo=timezone.utc)) == 1.234
    assert current_purchase(resp, datetime(2026, 10, 26, 10, 15, tzinfo=timezone.utc)) == 1.3
    # The last point covers until 10:30; after that nothing does.
    assert current_purchase(resp, datetime(2026, 10, 26, 10, 30, tzinfo=timezone.utc)) is None
    # Before the first point.
    assert current_purchase(resp, datetime(2026, 10, 26, 9, 59, tzinfo=timezone.utc)) is None


def test_current_purchase_none_when_unavailable_or_the_covering_quarter_is_null():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 26, 10, 5, tzinfo=timezone.utc)
    assert current_purchase(_purchase(available=False, reason="tariff_not_set"), now) is None
    resp = _purchase()
    resp["today"][0]["purchase_ore_kwh"] = None
    assert current_purchase(resp, now) is None
    assert current_purchase(None, now) is None


def test_purchase_covers_only_while_a_quarter_covers_now():
    from datetime import datetime, timezone

    resp = _purchase(available=False, reason="tariff_not_set")  # coverage is about the series
    assert purchase_covers(resp, datetime(2026, 10, 26, 10, 5, tzinfo=timezone.utc)) is True
    assert purchase_covers(resp, datetime(2026, 10, 26, 10, 30, tzinfo=timezone.utc)) is False
    assert purchase_covers(None, datetime(2026, 10, 26, 10, 5, tzinfo=timezone.utc)) is False


# --- client -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_purchase_prices_sends_bearer_header_and_no_token_in_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PURCHASE_URL, json=_purchase())
    client = WoltaApiClient(async_get_clientsession(hass), base_url=WOLTA_API_BASE)

    data = await client.get_purchase_prices(TOKEN)

    assert data["available"] is True
    method, url, _body, headers = aioclient_mock.mock_calls[0]
    assert method == "GET"
    assert url.path == "/api/v1/profile/prices"
    assert url.query == {}
    assert TOKEN not in str(url)
    assert headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_get_purchase_prices_none_for_a_non_object_body(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PURCHASE_URL, text="null", headers={"Content-Type": "application/json"})
    client = WoltaApiClient(async_get_clientsession(hass), base_url=WOLTA_API_BASE)

    assert await client.get_purchase_prices(TOKEN) is None


@pytest.mark.asyncio
async def test_get_purchase_prices_error_statuses(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    client = WoltaApiClient(async_get_clientsession(hass), base_url=WOLTA_API_BASE)
    aioclient_mock.get(PURCHASE_URL, status=404)
    with pytest.raises(WoltaAuthError):
        await client.get_purchase_prices(TOKEN)

    aioclient_mock.clear_requests()
    aioclient_mock.get(PURCHASE_URL, status=429, headers={"Retry-After": "30"})
    with pytest.raises(WoltaRateLimitError):
        await client.get_purchase_prices(TOKEN)


# --- coordinator ------------------------------------------------------------


def _purchase_calls(aioclient_mock: AiohttpClientMocker) -> list:
    return [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(PURCHASE_URL)]


def _coordinator(hass: HomeAssistant) -> WoltaPriceCoordinator:
    entry = _entry()
    entry.add_to_hass(hass)
    client = WoltaApiClient(async_get_clientsession(hass), base_url=WOLTA_API_BASE)
    return WoltaPriceCoordinator(hass, entry, client)


@pytest.mark.asyncio
async def test_purchase_endpoint_not_called_without_interest(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, json=_purchase())
    coordinator = _coordinator(hass)

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert _purchase_calls(aioclient_mock) == []
    assert PURCHASE_KEY not in coordinator.data


@pytest.mark.asyncio
async def test_purchase_endpoint_called_once_per_poll_with_interest(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, json=_purchase())
    coordinator = _coordinator(hass)
    unregister = coordinator.async_request_purchase_prices()

    await coordinator.async_refresh()
    assert len(_purchase_calls(aioclient_mock)) == 1
    assert coordinator.data[PURCHASE_KEY]["available"] is True
    assert _purchase_calls(aioclient_mock)[0][3]["Authorization"] == "Bearer tok-price-test"
    await coordinator.async_refresh()
    assert len(_purchase_calls(aioclient_mock)) == 2

    unregister()
    await coordinator.async_refresh()
    assert len(_purchase_calls(aioclient_mock)) == 2
    assert PURCHASE_KEY not in coordinator.data


@pytest.mark.asyncio
async def test_purchase_interest_is_counted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, json=_purchase())
    coordinator = _coordinator(hass)
    first = coordinator.async_request_purchase_prices()
    second = coordinator.async_request_purchase_prices()

    first()
    first()  # a repeated unregister must not drop the other registration
    await coordinator.async_refresh()
    assert len(_purchase_calls(aioclient_mock)) == 1

    second()
    await coordinator.async_refresh()
    assert len(_purchase_calls(aioclient_mock)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 404, 429, "null", "empty"])
async def test_purchase_failure_keeps_prices_now(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    if status == "null":
        aioclient_mock.get(PURCHASE_URL, text="null", headers={"Content-Type": "application/json"})
    elif status == "empty":
        aioclient_mock.get(PURCHASE_URL, text="")
    else:
        aioclient_mock.get(PURCHASE_URL, status=status)
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data["zone"] == "SE3"
    assert coordinator.data[PURCHASE_KEY] is None
    assert len(price_points(coordinator.data)) == 3


def price_points(data: dict) -> list:
    return data["today"] + data["tomorrow"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [aiohttp.ClientError("boom"), TimeoutError()])
async def test_purchase_failure_on_network_error_and_timeout(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, exc: Exception
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, exc=exc)
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data[PURCHASE_KEY] is None


def _own_records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "custom_components.wolta.price_coordinator" and r.levelno >= level
    ]


@pytest.mark.asyncio
async def test_purchase_failure_and_recovery_are_each_logged_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, caplog: pytest.LogCaptureFixture
):
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()

    async def _polls(ok: bool, n: int) -> None:
        aioclient_mock.clear_requests()
        aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
        if ok:
            aioclient_mock.get(PURCHASE_URL, json=_purchase())
        else:
            aioclient_mock.get(PURCHASE_URL, status=500)
        for _ in range(n):
            await coordinator.async_refresh()

    with caplog.at_level(logging.DEBUG, logger="custom_components.wolta.price_coordinator"):
        await _polls(True, 2)
        assert _own_records(caplog, logging.INFO) == []
        await _polls(False, 3)
        broke = _own_records(caplog, logging.INFO)
        assert len(broke) == 1 and "purchase price" in broke[0].lower()
        await _polls(True, 2)
        recovered = _own_records(caplog, logging.INFO)
        assert len(recovered) == 2 and "available again" in recovered[1]
        await _polls(False, 2)
        assert len(_own_records(caplog, logging.INFO)) == 3  # a second outage
    assert not _own_records(caplog, logging.WARNING)
    assert not [r for r in caplog.records if TOKEN in r.getMessage()]


@pytest.mark.asyncio
async def test_purchase_and_prices_now_are_requested_concurrently(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """/prices/now only returns once the purchase request has started, which sequential
    awaiting could never satisfy."""
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    started = asyncio.Event()
    release = asyncio.Event()
    now_done = asyncio.Event()

    async def _now(zone: str) -> dict:
        await started.wait()
        now_done.set()
        return _resp(forecast=None)

    async def _purchase_call(token: str) -> dict:
        assert token == "tok-price-test"
        started.set()
        await release.wait()
        return _purchase()

    coordinator._client.get_prices_now = _now
    coordinator._client.get_purchase_prices = _purchase_call

    refresh = asyncio.ensure_future(coordinator.async_refresh())
    await asyncio.wait_for(now_done.wait(), timeout=5)
    assert not refresh.done()
    release.set()
    await asyncio.wait_for(refresh, timeout=5)

    assert coordinator.data[PURCHASE_KEY]["available"] is True


@pytest.mark.asyncio
async def test_prices_now_failure_still_fails_the_coordinator_and_cancels_the_purchase_task(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """The purchase request is held open while /prices/now fails: the poll must still fail
    and the in-flight purchase task must be cancelled, not left running."""
    from custom_components.wolta.api import WoltaApiError

    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _now(zone: str) -> dict:
        await started.wait()  # the purchase request is in flight before this one fails
        raise WoltaApiError("boom", status=500)

    async def _purchase_call(token: str) -> dict:
        started.set()
        try:
            await asyncio.Event().wait()  # never answers
        except asyncio.CancelledError:
            cancelled.set()
            raise

    coordinator._client.get_prices_now = _now
    coordinator._client.get_purchase_prices = _purchase_call

    await asyncio.wait_for(coordinator.async_refresh(), timeout=5)

    assert coordinator.last_update_success is False
    await asyncio.wait_for(cancelled.wait(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [{"status": 500}, {"status": 404}, {"exc": aiohttp.ClientError("boom")}, {"exc": TimeoutError()}]
)
async def test_failed_purchase_fetch_keeps_the_last_good_response(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, failure: dict
):
    """One failed poll must not blank the purchase data: the sensor guards staleness itself
    (purchase_covers), so the previous series is still the best answer while it covers now."""
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, json=_purchase())
    await coordinator.async_refresh()
    good = coordinator.data[PURCHASE_KEY]
    assert good["available"] is True

    aioclient_mock.clear_requests()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, **failure)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data[PURCHASE_KEY] == good


@pytest.mark.asyncio
async def test_purchase_failure_without_a_previous_good_response_is_none(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, status=500)
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()

    await coordinator.async_refresh()

    assert coordinator.data[PURCHASE_KEY] is None


@pytest.mark.asyncio
async def test_purchase_recovery_replaces_the_kept_response(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, json=_purchase())
    await coordinator.async_refresh()

    aioclient_mock.clear_requests()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, status=500)
    await coordinator.async_refresh()

    aioclient_mock.clear_requests()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, json=_purchase(available=False, reason="tariff_not_set"))
    await coordinator.async_refresh()

    assert coordinator.data[PURCHASE_KEY]["reason"] == "tariff_not_set"


async def _poll_purchase(
    aioclient_mock: AiohttpClientMocker, coordinator: WoltaPriceCoordinator, **purchase_mock
) -> dict | None:
    """One coordinator poll with /profile/prices mocked as given; the purchase data."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, **purchase_mock)
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    return coordinator.data[PURCHASE_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [{"status": 500}, {"status": 404}])
async def test_unavailable_answer_is_not_kept_across_failures(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, failure: dict
):
    """`available: false` has no coverage to bound it, so a stale `reason` would outlive the
    fix (or an outage, or an old server's 404) for good: after it, a failure is None."""
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    answer = await _poll_purchase(
        aioclient_mock, coordinator, json=_purchase(available=False, reason="tariff_not_set"))
    assert answer["reason"] == "tariff_not_set"

    assert await _poll_purchase(aioclient_mock, coordinator, **failure) is None
    assert await _poll_purchase(aioclient_mock, coordinator, **failure) is None


@pytest.mark.asyncio
async def test_unavailable_answer_replaces_a_kept_priced_response(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """The server's newest answer wins: once it says "no price", an older priced series must
    not come back after a later failure."""
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    await _poll_purchase(aioclient_mock, coordinator, json=_purchase())
    await _poll_purchase(
        aioclient_mock, coordinator, json=_purchase(available=False, reason="tariff_not_set"))

    assert await _poll_purchase(aioclient_mock, coordinator, status=500) is None


@pytest.mark.asyncio
async def test_kept_response_is_dropped_when_the_zone_changes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """"Correct the price zone" updates entry.data without a reload: a series fetched for
    the old zone is another zone's price and must not be served for the new one."""
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    await _poll_purchase(aioclient_mock, coordinator, json=_purchase())
    assert await _poll_purchase(aioclient_mock, coordinator, status=500) is not None  # same zone

    entry = coordinator.config_entry
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONE: "SE4"})

    assert await _poll_purchase(aioclient_mock, coordinator, status=500) is None
    # ... and going back to the first zone does not resurrect it either.
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ZONE: "SE3"})
    assert await _poll_purchase(aioclient_mock, coordinator, status=500) is None


@pytest.mark.asyncio
async def test_forgetting_the_purchase_response_drops_the_kept_one(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """After a tariff save the kept series was priced with the OLD tariff: the options flow
    forgets it, so a failed refresh right after the save cannot show it."""
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()
    await _poll_purchase(aioclient_mock, coordinator, json=_purchase())
    assert await _poll_purchase(aioclient_mock, coordinator, status=500) is not None

    coordinator.async_forget_purchase()

    assert await _poll_purchase(aioclient_mock, coordinator, status=500) is None
    # A later good answer is kept again.
    await _poll_purchase(aioclient_mock, coordinator, json=_purchase())
    assert await _poll_purchase(aioclient_mock, coordinator, status=500) is not None


async def _gated_fetch_during_forget(
    aioclient_mock: AiohttpClientMocker, coordinator: WoltaPriceCoordinator, *, forget_first: bool
) -> None:
    """One poll whose /profile/prices request is held open and answered with a priced series;
    the tariff save (async_forget_purchase) happens before the request starts
    (forget_first) or while it is in flight."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _purchase_call(token: str) -> dict:
        started.set()
        await release.wait()
        return _purchase()

    aioclient_mock.clear_requests()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    if forget_first:
        coordinator.async_forget_purchase()
    with patch.object(coordinator._client, "get_purchase_prices", _purchase_call):
        refresh = asyncio.ensure_future(coordinator.async_refresh())
        await asyncio.wait_for(started.wait(), timeout=5)
        if not forget_first:
            coordinator.async_forget_purchase()
        release.set()
        await asyncio.wait_for(refresh, timeout=5)
    assert coordinator.last_update_success is True


@pytest.mark.asyncio
async def test_fetch_in_flight_during_a_tariff_save_is_not_kept(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """A /profile/prices request that started before the save answers with the OLD tariff's
    series. It must not be stored: if the refresh queued by the save then fails, the old
    price would be served until it stops covering the quarter-hour."""
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()

    await _gated_fetch_during_forget(aioclient_mock, coordinator, forget_first=False)

    assert await _poll_purchase(aioclient_mock, coordinator, status=500) is None


@pytest.mark.asyncio
async def test_fetch_started_after_a_tariff_save_is_kept(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """The new tariff's answer (the request started after the save) is kept as before."""
    coordinator = _coordinator(hass)
    coordinator.async_request_purchase_prices()

    await _gated_fetch_during_forget(aioclient_mock, coordinator, forget_first=True)

    assert await _poll_purchase(aioclient_mock, coordinator, status=500) == _purchase()


# --- sensor -----------------------------------------------------------------


async def _setup_purchase(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    *,
    purchase_json: dict | None = None,
    purchase_status: int = 200,
    enable: bool = True,
):
    if purchase_status == 200:
        aioclient_mock.get(PURCHASE_URL, json=purchase_json or _purchase())
    else:
        aioclient_mock.get(PURCHASE_URL, status=purchase_status)
    if not enable:
        return await _setup(hass, aioclient_mock)

    # The sensor is disabled by default: pre-create its registry entry as enabled, the way
    # a user who switched it on would have it, right when _setup adds the config entry.
    original = MockConfigEntry.add_to_hass

    def _add(self, hass_):
        original(self, hass_)
        er.async_get(hass_).async_get_or_create(
            "sensor", DOMAIN, PURCHASE_ID, config_entry=self, disabled_by=None
        )

    with patch.object(MockConfigEntry, "add_to_hass", _add):
        return await _setup(hass, aioclient_mock)


@pytest.mark.asyncio
async def test_purchase_sensor_is_disabled_by_default_and_makes_no_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    entry = await _setup_purchase(hass, aioclient_mock, enable=False)
    await entry.runtime_data.prices.async_refresh()

    registry = er.async_get(hass)
    reg = registry.async_get(_entity_id(hass, PURCHASE_ID))
    assert reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert registry.async_get(_entity_id(hass, SPOT_ID)).disabled_by is None
    assert hass.states.get(reg.entity_id) is None
    assert _purchase_calls(aioclient_mock) == []


@pytest.mark.asyncio
async def test_purchase_sensor_state_attributes_and_one_request_per_poll(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    entry = await _setup_purchase(hass, aioclient_mock)
    assert len(_purchase_calls(aioclient_mock)) == 1

    state = hass.states.get(_entity_id(hass, PURCHASE_ID))

    assert state is not None
    assert state.state == "1.234"
    assert state.attributes["unit_of_measurement"] == "SEK/kWh"
    assert "device_class" not in state.attributes
    assert state.attributes["reason"] is None
    assert state.attributes["components"] == _COMPONENTS
    assert state.attributes["prices"] == [
        {"start": "2026-10-26T10:00:00Z", "purchase": 1.234, "spot": 0.523},
        {"start": "2026-10-26T10:15:00Z", "purchase": 1.3, "spot": 0.6},
        {"start": "2026-10-27T10:00:00Z", "purchase": None, "spot": 0.3},
    ]
    # The spot sensor is untouched: still ex VAT.
    assert hass.states.get(_entity_id(hass, SPOT_ID)).state == "0.523"

    await entry.runtime_data.prices.async_refresh()
    assert len(_purchase_calls(aioclient_mock)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "zone_not_supported",
        "tariff_not_set",
        "grid_fixed_missing",
        "energy_tax_unconfirmed",
        "fx_missing",
    ],
)
async def test_purchase_sensor_has_no_value_and_shows_the_servers_reason(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, reason: str
):
    """available: false -> no value, and the reason must be readable. Home Assistant drops
    all attributes of an `unavailable` entity, so the state is `unknown` here (the entity
    itself stays available); a hard `unavailable` is for "no data" (see the error test)."""
    await _setup_purchase(
        hass, aioclient_mock, purchase_json=_purchase(available=False, reason=reason)
    )

    state = hass.states.get(_entity_id(hass, PURCHASE_ID))

    assert state.state == "unknown"
    assert state.attributes["reason"] == reason
    assert state.attributes["components"] == _COMPONENTS
    assert [p["purchase"] for p in state.attributes["prices"]] == [None, None, None]
    assert state.attributes["prices"][0]["spot"] == 0.523


@pytest.mark.asyncio
async def test_purchase_sensor_has_no_value_when_available_false_even_with_prices(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    resp = _purchase()
    resp["available"] = False
    resp["reason"] = "tariff_not_set"
    await _setup_purchase(hass, aioclient_mock, purchase_json=resp)

    state = hass.states.get(_entity_id(hass, PURCHASE_ID))
    assert state.state == "unknown"
    assert state.attributes["reason"] == "tariff_not_set"


@pytest.mark.asyncio
async def test_purchase_sensor_unknown_for_a_quarter_without_a_price(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    """A quarter whose year has no energy-tax rule has purchase null: no price, never last
    year's tax. The attributes stay readable."""
    await _setup_purchase(hass, aioclient_mock)
    entity_id = _entity_id(hass, PURCHASE_ID)

    freezer.move_to("2026-10-27T10:00:02+00:00")  # tomorrow's quarter, purchase null
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == "unknown"
    assert state.attributes["reason"] is None
    assert state.attributes["prices"][-1] == {
        "start": "2026-10-27T10:00:00Z",
        "purchase": None,
        "spot": 0.3,
    }


@pytest.mark.asyncio
async def test_purchase_sensor_state_follows_quarter_boundaries_without_a_poll(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    await _setup_purchase(hass, aioclient_mock)
    entity_id = _entity_id(hass, PURCHASE_ID)
    assert hass.states.get(entity_id).state == "1.234"
    polls = len(aioclient_mock.mock_calls)

    freezer.move_to("2026-10-26T10:15:02+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "1.3"
    assert len(aioclient_mock.mock_calls) == polls

    # No point covers 10:30 (the last one ended): unavailable, not a stale price.
    freezer.move_to("2026-10-26T10:30:02+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 429, 500])
async def test_purchase_endpoint_error_makes_only_the_sensor_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
):
    await _setup_purchase(hass, aioclient_mock, purchase_status=status)

    purchase = hass.states.get(_entity_id(hass, PURCHASE_ID))
    spot = hass.states.get(_entity_id(hass, SPOT_ID))

    assert purchase.state == "unavailable"
    assert purchase.attributes.get("reason") is None
    assert spot.state == "0.523"
    assert len(spot.attributes["prices"]) == 3


@pytest.mark.asyncio
async def test_purchase_sensor_keeps_its_price_through_a_failed_poll_while_the_series_covers_now(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
):
    entry = await _setup_purchase(hass, aioclient_mock)
    entity_id = _entity_id(hass, PURCHASE_ID)
    assert hass.states.get(entity_id).state == "1.234"

    # The next poll (T+15 min) fails; the series from the good poll still covers 10:20.
    aioclient_mock.clear_requests()
    aioclient_mock.get(PRICES_URL, json=_resp(forecast=None))
    aioclient_mock.get(PURCHASE_URL, status=500)
    base = len(_purchase_calls(aioclient_mock))

    prices = entry.runtime_data.prices

    async def _poll(at: str) -> None:
        freezer.move_to(at)
        await prices.async_refresh()
        await hass.async_block_till_done()

    await _poll("2026-10-26T10:20:05+00:00")

    assert len(_purchase_calls(aioclient_mock)) == base + 1  # the poll really ran and failed
    assert hass.states.get(entity_id).state == "1.3"

    # Once the kept series no longer covers now, the sensor is unavailable, not stale.
    await _poll("2026-10-26T10:35:05+00:00")

    assert len(_purchase_calls(aioclient_mock)) == base + 2
    assert hass.states.get(entity_id).state == "unavailable"


def test_purchase_prices_attribute_not_recorded():
    assert "prices" in WoltaPurchasePriceSensor._unrecorded_attributes
