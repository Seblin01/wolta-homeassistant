"""Tests for custom_components/wolta/api.py (TDD – write first, then implement)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wolta.api import (
    WoltaApiClient,
    WoltaApiError,
    WoltaAuthError,
    WoltaCopyBusy,
    WoltaCopyGone,
    WoltaPlantClaimed,
    WoltaRateLimitError,
    WoltaReplacementConflict,
    WoltaReplacementGone,
)


BASE_URL = "https://wolta.se"
TOKEN = "tok-abc123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(aioclient_mock: AiohttpClientMocker) -> WoltaApiClient:
    """Return a WoltaApiClient backed by the aiohttp mock session."""
    return WoltaApiClient(aioclient_mock.create_session({}), base_url=BASE_URL)


# ---------------------------------------------------------------------------
# create_profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_profile_returns_token(aioclient_mock: AiohttpClientMocker):
    """201 response from POST /api/v1/profile returns the profile token."""
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile",
        status=201,
        json={"profile_token": TOKEN},
    )
    client = _client(aioclient_mock)
    result = await client.create_profile(
        zone="SE3",
        battery_kwh=22.0,
        battery_kw=5.0,
        eff=0.9,
        has_solar=True,
        share_profile=True,
    )
    assert result == TOKEN


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_raises_wolta_auth_error(aioclient_mock: AiohttpClientMocker):
    """404 on any request maps to WoltaAuthError (token purged / unknown)."""
    aioclient_mock.get(
        f"{BASE_URL}/api/v1/profile/results",
        status=404,
        json={"detail": "not found"},
    )
    client = _client(aioclient_mock)
    with pytest.raises(WoltaAuthError):
        await client.results(TOKEN)


@pytest.mark.asyncio
async def test_429_raises_rate_limit_error_with_retry_after(
    aioclient_mock: AiohttpClientMocker,
):
    """429 with Retry-After header raises WoltaRateLimitError carrying retry_after."""
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile/recompute",
        status=429,
        headers={"Retry-After": "3600"},
        json={"detail": "rate limit"},
    )
    client = _client(aioclient_mock)
    with pytest.raises(WoltaRateLimitError) as exc_info:
        await client.recompute(TOKEN)
    assert exc_info.value.retry_after == 3600


@pytest.mark.asyncio
async def test_recompute_sends_bearer_header(aioclient_mock: AiohttpClientMocker):
    """recompute carries the token in the Authorization header, not the URL."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile/recompute", status=202, json={})
    client = _client(aioclient_mock)
    await client.recompute(TOKEN)
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_delete_sends_bearer_header(aioclient_mock: AiohttpClientMocker):
    """delete (right-to-erasure) hits the segment-less path with the token in the
    Authorization header - the token no longer appears in the URL."""
    aioclient_mock.delete(f"{BASE_URL}/api/v1/calibration", status=200, json={})
    client = _client(aioclient_mock)
    await client.delete(TOKEN)
    call = aioclient_mock.mock_calls[-1]
    assert call[0].lower() == "delete"
    assert str(call[1]).endswith("/api/v1/calibration")  # ingen token i URL:en
    assert call[3]["Authorization"] == f"Bearer {TOKEN}"


# ---------------------------------------------------------------------------
# put_data chunking
# ---------------------------------------------------------------------------


def _row(n: int) -> dict:
    return {
        "ts": f"2024-01-01T{n % 24:02d}:00:00Z",
        "batt_charged_kwh": 0.0,
        "batt_discharged_kwh": 0.0,
        "solar_kwh": 0.5,
        "grid_import_kwh": 1.0,
        "grid_export_kwh": 0.0,
    }


@pytest.mark.asyncio
async def test_put_data_chunks_over_limit(aioclient_mock: AiohttpClientMocker):
    """put_data with > MAX_ROWS_PER_PUT rows issues multiple sequential PUTs.

    The chunk size is kept small (well under any reverse-proxy body-size limit) —
    see the 413 incident 2026-07-05. Verify the client actually splits.
    """
    from custom_components.wolta.api import MAX_ROWS_PER_PUT

    url = f"{BASE_URL}/api/v1/profile/data"
    aioclient_mock.put(
        url,
        status=200,
        json={"upserted": MAX_ROWS_PER_PUT, "period_start": "2024-01-01T00:00:00Z",
              "period_end": "2024-12-31T23:00:00Z"},
    )

    # One more than the chunk size → exactly 2 chunks.
    rows = [_row(i) for i in range(MAX_ROWS_PER_PUT + 1)]
    client = _client(aioclient_mock)
    result = await client.put_data(TOKEN, rows)

    put_calls = [c for c in aioclient_mock.mock_calls if c[0].lower() == "put"]
    assert len(put_calls) == 2, f"Expected 2 PUT calls, got {len(put_calls)}"
    # Each chunk must be <= MAX_ROWS_PER_PUT so bodies stay under the proxy limit.
    assert MAX_ROWS_PER_PUT <= 5_000, "chunk must stay small enough for a 1 MB proxy limit"
    assert result is not None
    # Token travels in the Authorization header, not the URL, on every chunk.
    for call in put_calls:
        assert call[3]["Authorization"] == f"Bearer {TOKEN}"


# ---------------------------------------------------------------------------
# results happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_results_returns_parsed_dict(aioclient_mock: AiohttpClientMocker):
    """results() returns the full response JSON as a dict."""
    payload = {
        "status": "ready",
        "currency": "SEK",
        "period": {"start": "2024-01-01", "end": "2024-12-31"},
        "betyg": "A",
        "decision": "keep",
        "history": [],
    }
    aioclient_mock.get(
        f"{BASE_URL}/api/v1/profile/results",
        status=200,
        json=payload,
    )
    client = _client(aioclient_mock)
    result = await client.results(TOKEN)
    assert result == payload
    assert result["betyg"] == "A"
    get_calls = [c for c in aioclient_mock.mock_calls if c[0].lower() == "get"]
    assert get_calls[-1][3]["Authorization"] == f"Bearer {TOKEN}"


# ---------------------------------------------------------------------------
# create_profile with cost_sek / purchase_date (v0.3.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_profile_includes_cost_and_date(aioclient_mock: AiohttpClientMocker):
    """create_profile includes cost_sek and purchase_date in POST body when provided."""
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile",
        status=201,
        json={"profile_token": TOKEN},
    )
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3",
        battery_kwh=22.0,
        battery_kw=5.0,
        eff=0.9,
        has_solar=True,
        share_profile=False,
        cost_sek=89900.0,
        purchase_date="2022-11-15",
    )
    # Inspect the body of the single POST call
    post_calls = [c for c in aioclient_mock.mock_calls if c[0].lower() == "post"]
    assert len(post_calls) == 1
    body = post_calls[0][2]  # (method, url, data) – data is the json kwarg
    assert body["cost_sek"] == 89900.0
    assert body["purchase_date"] == "2022-11-15"


@pytest.mark.asyncio
async def test_create_profile_omits_cost_and_date_when_none(aioclient_mock: AiohttpClientMocker):
    """create_profile omits cost_sek and purchase_date from POST body when None."""
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile",
        status=201,
        json={"profile_token": TOKEN},
    )
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3",
        battery_kwh=22.0,
        battery_kw=5.0,
        eff=0.9,
        has_solar=False,
        share_profile=False,
        # cost_sek and purchase_date not passed (defaults to None)
    )
    post_calls = [c for c in aioclient_mock.mock_calls if c[0].lower() == "post"]
    body = post_calls[0][2]
    assert "cost_sek" not in body
    assert "purchase_date" not in body


# ---------------------------------------------------------------------------
# create_profile with reserve_pct (plan 38 / task 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_profile_includes_reserve_pct(aioclient_mock: AiohttpClientMocker):
    """create_profile includes reserve_pct in the POST body when provided."""
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile",
        status=201,
        json={"profile_token": TOKEN},
    )
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3",
        battery_kwh=22.0,
        battery_kw=5.0,
        eff=0.9,
        has_solar=True,
        share_profile=False,
        reserve_pct=10.0,
    )
    post_calls = [c for c in aioclient_mock.mock_calls if c[0].lower() == "post"]
    assert len(post_calls) == 1
    body = post_calls[0][2]
    assert body["reserve_pct"] == 10.0


@pytest.mark.asyncio
async def test_create_profile_omits_reserve_pct_when_none(aioclient_mock: AiohttpClientMocker):
    """create_profile omits reserve_pct from the POST body when None (default)."""
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile",
        status=201,
        json={"profile_token": TOKEN},
    )
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3",
        battery_kwh=22.0,
        battery_kw=5.0,
        eff=0.9,
        has_solar=False,
        share_profile=False,
        # reserve_pct not passed (defaults to None)
    )
    post_calls = [c for c in aioclient_mock.mock_calls if c[0].lower() == "post"]
    body = post_calls[0][2]
    assert "reserve_pct" not in body


@pytest.mark.asyncio
async def test_create_profile_reserve_pct_zero_is_included(aioclient_mock: AiohttpClientMocker):
    """A legitimate reserve_pct of 0.0 (no reserve floor) must reach the backend,
    not be swallowed as "unset"."""
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile",
        status=201,
        json={"profile_token": TOKEN},
    )
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3",
        battery_kwh=22.0,
        battery_kw=5.0,
        eff=0.9,
        has_solar=False,
        share_profile=False,
        reserve_pct=0.0,
    )
    post_calls = [c for c in aioclient_mock.mock_calls if c[0].lower() == "post"]
    body = post_calls[0][2]
    assert body["reserve_pct"] == 0.0


# ---------------------------------------------------------------------------
# patch_profile (v0.3.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_profile_issues_patch_request(aioclient_mock: AiohttpClientMocker):
    """patch_profile issues PATCH to /api/v1/profile with provided fields, token in
    the Authorization header."""
    aioclient_mock.patch(
        f"{BASE_URL}/api/v1/profile",
        status=200,
        json={"profile_token": TOKEN},
    )
    client = _client(aioclient_mock)
    await client.patch_profile(TOKEN, cost_sek=95000.0, purchase_date="2023-03-01")

    patch_calls = [c for c in aioclient_mock.mock_calls if c[0].lower() == "patch"]
    assert len(patch_calls) == 1
    body = patch_calls[0][2]
    assert body["cost_sek"] == 95000.0
    assert body["purchase_date"] == "2023-03-01"
    assert patch_calls[0][3]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_patch_profile_404_raises_auth_error(aioclient_mock: AiohttpClientMocker):
    """patch_profile on 404 raises WoltaAuthError."""
    aioclient_mock.patch(
        f"{BASE_URL}/api/v1/profile",
        status=404,
        json={"detail": "not found"},
    )
    client = _client(aioclient_mock)
    with pytest.raises(WoltaAuthError):
        await client.patch_profile(TOKEN, cost_sek=1000.0)


@pytest.mark.asyncio
async def test_patch_profile_429_raises_rate_limit_error(aioclient_mock: AiohttpClientMocker):
    """patch_profile on 429 raises WoltaRateLimitError with retry_after."""
    aioclient_mock.patch(
        f"{BASE_URL}/api/v1/profile",
        status=429,
        headers={"Retry-After": "7200"},
        json={"detail": "rate limit"},
    )
    client = _client(aioclient_mock)
    with pytest.raises(WoltaRateLimitError) as exc_info:
        await client.patch_profile(TOKEN, purchase_date="2024-01-01")
    assert exc_info.value.retry_after == 7200


# ---------------------------------------------------------------------------
# get_profile (delad profil-sync)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_profile_returns_profile_dict(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(
        f"{BASE_URL}/api/v1/profile",
        json={"zone": "SE3", "battery_kwh": 22.0, "grid_var_ore": 25.5,
              "share_profile": True},
    )
    client = _client(aioclient_mock)
    prof = await client.get_profile(TOKEN)
    assert prof["battery_kwh"] == 22.0
    assert prof["grid_var_ore"] == 25.5
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_get_profile_404_raises_auth_error(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(f"{BASE_URL}/api/v1/profile", status=404)
    client = _client(aioclient_mock)
    with pytest.raises(WoltaAuthError):
        await client.get_profile("dead")


@pytest.mark.asyncio
async def test_adopt_profile_posts(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile/adopt", json={"adopted": True}
    )
    client = _client(aioclient_mock)
    resp = await client.adopt_profile(TOKEN)
    assert resp["adopted"] is True
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_adopt_profile_404_raises_auth_error(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile/adopt", status=404)
    client = _client(aioclient_mock)
    with pytest.raises(WoltaAuthError):
        await client.adopt_profile("dead")


@pytest.mark.asyncio
async def test_adopt_profile_sends_client_plant_id(aioclient_mock: AiohttpClientMocker):
    """adopt carries the stable plant identity so the adopted row can be recognised later."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile/adopt", json={"adopted": True})
    client = _client(aioclient_mock)
    await client.adopt_profile(TOKEN, client_plant_id="deadbeef" * 4)
    assert aioclient_mock.mock_calls[-1][2] == {"client_plant_id": "deadbeef" * 4}


@pytest.mark.asyncio
async def test_create_profile_sends_client_plant_id(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile", json={"profile_token": TOKEN})
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3", battery_kwh=10, battery_kw=5, eff=0.9, has_solar=False,
        share_profile=False, client_plant_id="abc123")
    assert aioclient_mock.mock_calls[-1][2]["client_plant_id"] == "abc123"


@pytest.mark.asyncio
async def test_create_profile_omits_plant_id_when_unset(aioclient_mock: AiohttpClientMocker):
    """Older call sites (and any path without an id) must not send a null identity."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile", json={"profile_token": TOKEN})
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3", battery_kwh=10, battery_kw=5, eff=0.9, has_solar=False, share_profile=False)
    assert "client_plant_id" not in aioclient_mock.mock_calls[-1][2]


# ---------------------------------------------------------------------------
# mint_link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_link_returnerar_token(aioclient_mock: AiohttpClientMocker) -> None:
    """Mint är idempotent server-side; klienten anropar den vid VARJE setup och
    skriver över sin cache."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile/link",
                        json={"link_token": "wpl_abc"})
    client = _client(aioclient_mock)
    assert await client.mint_link("tok-1") == "wpl_abc"


@pytest.mark.asyncio
async def test_mint_link_ger_none_vid_fel(aioclient_mock: AiohttpClientMocker) -> None:
    """Ett misslyckat mint får ALDRIG fälla entryn – anroparen faller tillbaka på
    cachen och i sista hand på en tokenlös URL."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile/link", status=503)
    client = _client(aioclient_mock)
    assert await client.mint_link("tok-1") is None


@pytest.mark.asyncio
async def test_mint_link_ger_none_pa_tom_kropp(aioclient_mock: AiohttpClientMocker) -> None:
    """_request returnerar None på 204/tom kropp (api.py:90-96) – mint_link får inte
    kasta AttributeError på det (plangranskningen, fynd H12)."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile/link", status=200, text="")
    client = _client(aioclient_mock)
    assert await client.mint_link("tok-1") is None


@pytest.mark.asyncio
async def test_mint_link_ger_none_vid_natverksfel(aioclient_mock: AiohttpClientMocker) -> None:
    """Kodgranskning (kritiskt fynd 1): ett RIKTIGT offline-läge – DNS/anslutningsfel –
    kastas av self._session.request(...) (api.py:76) INNAN _request hinner konvertera
    det till en WoltaApiError. Ett smalt `except WoltaApiError` läcker ut det felet, ut
    ur async_setup_entry, och fäller entryn – exakt det brief:en namngav först. Ett HTTP
    503 (test ovan) konverteras av _request och testade aldrig den här grenen."""
    connection_key = aiohttp.client_reqrep.ConnectionKey(
        host="wolta.se", port=443, is_ssl=True, ssl=None,
        proxy=None, proxy_auth=None, proxy_headers_hash=None)
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile/link",
                        exc=aiohttp.ClientConnectorError(
                            connection_key=connection_key, os_error=OSError("offline")))
    client = _client(aioclient_mock)
    assert await client.mint_link("tok-1") is None


@pytest.mark.asyncio
async def test_mint_link_ger_none_vid_timeout(aioclient_mock: AiohttpClientMocker) -> None:
    """Samma gren som ovan, men TimeoutError – den andra hälften av
    coordinator.py:386/:552-mönstret `except (aiohttp.ClientError, TimeoutError)`."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile/link", exc=TimeoutError("timed out"))
    client = _client(aioclient_mock)
    assert await client.mint_link("tok-1") is None


# ---------------------------------------------------------------------------
# mint_claim_code
# ---------------------------------------------------------------------------
#
# Unlike mint_link (degrades to None by design), mint_claim_code RAISES on
# failure - the user is standing in the options flow waiting for a code to
# type in on wolta.se. But everything it raises must land in the ONE
# WoltaApiError family, since the flow's caller only catches that (review
# finding on task 13: an empty body or a real network error used to escape as
# TypeError / aiohttp.ClientError / TimeoutError instead).


@pytest.mark.asyncio
async def test_mint_claim_code_returnerar_kod(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile/claim-code",
        json={"code": "ABCD-EFGH", "expires_in": 600},
    )
    client = _client(aioclient_mock)
    assert await client.mint_claim_code("tok-1") == "ABCD-EFGH"


@pytest.mark.asyncio
async def test_mint_claim_code_hojer_pa_tom_kropp(aioclient_mock: AiohttpClientMocker) -> None:
    """_request returnerar None på 204/tom kropp (api.py:90-96) – samma gap
    mint_link:s isinstance-vakt finns för, direkt ovanför. Utan en vakt här
    TypeError:ar `data["code"]`, vilket läcker förbi flowets `except
    WoltaApiError` och kraschar options-flowet i stället för att avbryta rent."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile/claim-code", status=200, text="")
    client = _client(aioclient_mock)
    with pytest.raises(WoltaApiError):
        await client.mint_claim_code("tok-1")


@pytest.mark.asyncio
async def test_mint_claim_code_hojer_pa_kropp_utan_code(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Ett 2xx-svar som saknar "code" (t.ex. en oväntad JSON-form) ska ge samma
    riktade WoltaApiError, inte en KeyError."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile/claim-code", json={"expires_in": 600})
    client = _client(aioclient_mock)
    with pytest.raises(WoltaApiError):
        await client.mint_claim_code("tok-1")


@pytest.mark.asyncio
async def test_mint_claim_code_hojer_woltaapierror_vid_natverksfel(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Kritiskt fynd (task 13-review): ett RIKTIGT offline-läge – DNS/anslutningsfel –
    kastas av self._session.request(...) (api.py:76) INNAN _request hinner konvertera
    det till en WoltaApiError. Måste omvandlas till WoltaApiError här, annars läcker
    aiohttp.ClientConnectorError ut ur flowets smala `except WoltaApiError` och
    kraschar options-flowet i stället för att avbryta med cannot_connect."""
    connection_key = aiohttp.client_reqrep.ConnectionKey(
        host="wolta.se", port=443, is_ssl=True, ssl=None,
        proxy=None, proxy_auth=None, proxy_headers_hash=None)
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile/claim-code",
        exc=aiohttp.ClientConnectorError(
            connection_key=connection_key, os_error=OSError("offline")))
    client = _client(aioclient_mock)
    with pytest.raises(WoltaApiError):
        await client.mint_claim_code("tok-1")


@pytest.mark.asyncio
async def test_mint_claim_code_hojer_woltaapierror_vid_timeout(
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Samma gren som ovan, men TimeoutError – den andra hälften av
    coordinator.py:386/:552-mönstret `except (aiohttp.ClientError, TimeoutError)`."""
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile/claim-code", exc=TimeoutError("timed out"))
    client = _client(aioclient_mock)
    with pytest.raises(WoltaApiError):
        await client.mint_claim_code("tok-1")


@pytest.mark.asyncio
async def test_mint_link_skickar_bunden_timeout(aioclient_mock) -> None:
    """Code-review 2026-08-25: mint_link kors som ALLRA forsta handling i
    async_setup_entry och ar frivillig by design. Utan egen timeout gallde
    aiohttps default (total=300 s), sa en svartahalsanslutning kunde stalla
    integrationens uppstart i fem minuter - och kostnaden betalades om vid varje
    ConfigEntryNotReady-retry. Testet pinnar att anropet bar en BUNDEN timeout,
    inte bara att det fungerar."""
    client = _client(aioclient_mock)
    sedda: dict = {}

    async def _spion(method, path, **kwargs):
        sedda.update(kwargs)
        return {"link_token": "wpl_abc"}

    client._request = _spion
    assert await client.mint_link("tok-1") == "wpl_abc"
    tmo = sedda.get("timeout")
    assert tmo is not None, "mint_link skickade ingen timeout - default 300 s galler da"
    assert tmo.total is not None and tmo.total <= 30, f"otillracklig grans: {tmo.total}"


@pytest.mark.asyncio
async def test_mint_claim_code_skickar_bunden_timeout(aioclient_mock) -> None:
    """Samma grans pa systermetoden: anvandaren star i options-dialogen och
    vantar pa en kod. Systerdrift har varit det atervandande felet i det har
    arbetet - de tva mintarna ska ha samma bundna vantan."""
    client = _client(aioclient_mock)
    sedda: dict = {}

    async def _spion(method, path, **kwargs):
        sedda.update(kwargs)
        return {"code": "ABCD-EFGH"}

    client._request = _spion
    assert await client.mint_claim_code("tok-1") == "ABCD-EFGH"
    tmo = sedda.get("timeout")
    assert tmo is not None, "mint_claim_code skickade ingen timeout"
    assert tmo.total is not None and tmo.total <= 30, f"otillracklig grans: {tmo.total}"


# ---------------------------------------------------------------------------
# create_profile – battery_declared (spec 2026-09-14 §7.1) + get_control_systems
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_profile_declared_utan_par():
    client = WoltaApiClient(session=None)
    client._request = AsyncMock(return_value={"profile_token": "t"})
    tok = await client.create_profile(zone="SE3", has_solar=False, share_profile=False,
                                      battery_declared=True, control_system="huawei")
    assert tok == "t"
    payload = client._request.await_args.kwargs["json"]
    assert payload["battery_declared"] is True
    assert "battery_kwh" not in payload and "battery_kw" not in payload and "eff" not in payload


@pytest.mark.asyncio
async def test_create_profile_sends_soc_absent_only_when_given():
    """soc_absent (spec 2026-10-20-soc-krav): omission is meaningful - an OMITTED field
    leaves the server's value alone, so only an explicit True/False is sent."""
    client = WoltaApiClient(session=None)
    client._request = AsyncMock(return_value={"profile_token": "t"})
    await client.create_profile(zone="SE3", has_solar=False, share_profile=True, soc_absent=True)
    assert client._request.await_args.kwargs["json"]["soc_absent"] is True
    await client.create_profile(zone="SE3", has_solar=False, share_profile=True, soc_absent=False)
    assert client._request.await_args.kwargs["json"]["soc_absent"] is False
    await client.create_profile(zone="SE3", has_solar=False, share_profile=True)
    assert "soc_absent" not in client._request.await_args.kwargs["json"]


@pytest.mark.asyncio
async def test_create_profile_med_par_som_forr():
    client = WoltaApiClient(session=None)
    client._request = AsyncMock(return_value={"profile_token": "t"})
    await client.create_profile(zone="SE3", has_solar=False, share_profile=False,
                                battery_kwh=10, battery_kw=5, eff=0.9)
    payload = client._request.await_args.kwargs["json"]
    assert payload["battery_kwh"] == 10 and payload["battery_kw"] == 5 and payload["eff"] == 0.9
    assert "battery_declared" not in payload


@pytest.mark.asyncio
async def test_get_control_systems():
    client = WoltaApiClient(session=None)
    client._request = AsyncMock(return_value=[{"id": "huawei", "label": "Huawei", "ha_domains": ["huawei_solar"]}])
    assert (await client.get_control_systems())[0]["id"] == "huawei"
    assert client._request.await_args.args[:2] == ("GET", "/control-systems")


@pytest.mark.asyncio
async def test_get_control_systems_keeps_a_good_answer_for_a_day(monkeypatch):
    """The endpoint is cacheable a day (Cache-Control max-age=86400); the client keeps a
    non-empty answer that long, so the repair form need not wait on the network."""
    from custom_components.wolta import api as api_mod

    now = [1000.0]
    monkeypatch.setattr(api_mod.time, "monotonic", lambda: now[0])
    client = WoltaApiClient(session=None)
    rows = [{"id": "other", "label": "Annat", "label_en": "Other", "ha_domains": []}]
    client._request = AsyncMock(return_value=rows)
    assert not client.control_systems_cached()
    assert await client.get_control_systems() == rows
    assert client.control_systems_cached()
    assert await client.get_control_systems() == rows
    assert client._request.await_count == 1
    now[0] += api_mod.CONTROL_SYSTEMS_TTL_S
    assert not client.control_systems_cached()
    await client.get_control_systems()
    assert client._request.await_count == 2


@pytest.mark.asyncio
async def test_get_control_systems_never_keeps_an_empty_answer():
    client = WoltaApiClient(session=None)
    client._request = AsyncMock(return_value=[])
    await client.get_control_systems()
    await client.get_control_systems()
    assert client._request.await_count == 2 and not client.control_systems_cached()


@pytest.mark.asyncio
async def test_get_control_systems_skickar_bunden_timeout():
    """Samma grund som de två mintarna: uppslaget awaitas INLINE i övergången
    entities → plant, alltså medan användaren väntar på att nästa steg ska ritas.
    Utan egen timeout gällde aiohttps default (total=300 s), så en blackholead
    anslutning kunde frysa onboardingen i fem minuter för ett FÖRSLAG som ändå
    degraderar tyst (broad except i config_flow → inget förslag)."""
    client = WoltaApiClient(session=None)
    client._request = AsyncMock(return_value=[])
    await client.get_control_systems()
    tmo = client._request.await_args.kwargs.get("timeout")
    assert tmo is not None, "get_control_systems skickade ingen timeout - default 300 s galler da"
    assert tmo.total is not None and tmo.total <= 15, f"otillräcklig gräns: {tmo.total}"


# ---------------------------------------------------------------------------
# Client version in the User-Agent
# ---------------------------------------------------------------------------
#
# The backend had no way to tell an old client from a new one: nothing in the
# request carried the integration version. That mattered on 2026-09-22, when the
# question "should we delete old releases that no longer work against the current
# backend?" could not be answered - GET /grade/check-plant is 405 today, so some
# old version WOULD fail, but nobody could tell whether anyone still runs one.
#
# The User-Agent is the cheapest place to carry it: wolta.se's nginx already logs
# "$http_user_agent" in its own `proxied` format, which ships to Loki. No backend
# change, no deploy - the answer simply shows up in the logs.


@pytest.mark.asyncio
async def test_every_request_identifies_the_integration_version(
    aioclient_mock: AiohttpClientMocker,
):
    """The User-Agent names the integration and its version from the manifest."""
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path("custom_components/wolta/manifest.json")).read_text(encoding="utf-8")
    )
    aioclient_mock.get(f"{BASE_URL}/api/v1/profile", status=200, json={})

    await _client(aioclient_mock).get_profile(TOKEN)

    sent = aioclient_mock.mock_calls[0][3]
    ua = sent.get("User-Agent", "")
    assert "wolta-hacs" in ua, f"User-Agent identifierar inte integrationen: {ua!r}"
    assert manifest["version"] in ua, (
        f"User-Agent bär inte manifestets version {manifest['version']}: {ua!r}"
    )


@pytest.mark.asyncio
async def test_user_agent_does_not_clobber_the_auth_header(
    aioclient_mock: AiohttpClientMocker,
):
    """Adding the User-Agent must leave the caller's own headers intact."""
    aioclient_mock.get(f"{BASE_URL}/api/v1/profile", status=200, json={})

    await _client(aioclient_mock).get_profile(TOKEN)

    sent = aioclient_mock.mock_calls[0][3]
    assert sent.get("Authorization") == f"Bearer {TOKEN}", (
        f"auth-headern gick förlorad när User-Agent lades till: {sent!r}"
    )
    assert "wolta-hacs" in sent.get("User-Agent", "")


@pytest.mark.asyncio
async def test_user_agent_keeps_home_assistants_own(
    aioclient_mock: AiohttpClientMocker,
):
    """Ours is PREPENDED to Home Assistant's User-Agent, never a replacement.

    v0.37.3 replaced it, and the loss showed up in the logs within hours: HA's
    shared session already sends "HomeAssistant/<core version> aiohttp/... ",
    which is how the backend could see WHICH Home Assistant releases are in the
    field - the very thing hacs.json's minimum (2025.12.0) is about. Carrying the
    integration version must add information, not trade one fact for another.
    """
    ha_ua = "HomeAssistant/2026.9.3 aiohttp/3.14.3 Python/3.14"
    session = aioclient_mock.create_session({})
    session.headers["User-Agent"] = ha_ua        # what HA's shared session sets
    client = WoltaApiClient(session, base_url=BASE_URL)
    aioclient_mock.get(f"{BASE_URL}/api/v1/profile", status=200, json={})

    await client.get_profile(TOKEN)

    ua = aioclient_mock.mock_calls[0][3].get("User-Agent", "")
    assert "wolta-hacs/" in ua, f"integrationens version saknas: {ua!r}"
    assert ha_ua in ua, f"Home Assistants egen User-Agent gick förlorad: {ua!r}"


# ---------------------------------------------------------------------------
# put_data – battery_state (SoC) alongside the flow rows
# ---------------------------------------------------------------------------


def _flow_rows(n: int) -> list[dict]:
    return [{"ts": f"r{i}"} for i in range(n)]


def _soc_rows(n: int) -> list[dict]:
    return [{"unit": "sensor.soc", "ts": f"s{i}"} for i in range(n)]


@pytest.mark.asyncio
async def test_put_data_without_soc_sends_exactly_rows():
    client = WoltaApiClient(session=None)
    client._request = AsyncMock(return_value={"ok": True})
    rows = _flow_rows(3)
    await client.put_data(TOKEN, rows)
    assert client._request.await_count == 1
    assert client._request.await_args.kwargs["json"] == {"rows": rows}


@pytest.mark.asyncio
async def test_put_data_more_flow_chunks_than_soc_chunks_carries_soc_only_once():
    """6 000 flow rows need two calls, 10 SoC rows one: SoC rides with the first call only.
    The second call is flows alone - exactly {"rows": chunk}, no battery_state and no
    battery_sources - so the SoC rows are not sent twice."""
    client = WoltaApiClient(session=None)
    client._request = AsyncMock(return_value={"ok": True})
    rows, soc = _flow_rows(6_000), _soc_rows(10)
    sources = [{"unit": "sensor.soc", "source": "ha", "semantics": "mean"}]
    await client.put_data(TOKEN, rows, battery_state=soc, battery_sources=sources)
    assert client._request.await_count == 2
    first, second = (c.kwargs["json"] for c in client._request.await_args_list)
    assert first == {"rows": rows[:5_000], "battery_state": soc, "battery_sources": sources}
    assert second == {"rows": rows[5_000:]}


@pytest.mark.asyncio
async def test_put_data_soc_chunks_follow_flow_chunks_and_repeat_the_last():
    """6 000 SoC rows need two calls though 10 flow rows need one: the second call repeats
    the last flow chunk (`rows` is mandatory, the upsert is idempotent)."""
    client = WoltaApiClient(session=None)
    client._request = AsyncMock(return_value={"ok": True})
    rows, soc = _flow_rows(10), _soc_rows(6_000)
    sources = [{"unit": "sensor.soc", "source": "ha", "semantics": "mean"}]
    await client.put_data(TOKEN, rows, battery_state=soc, battery_sources=sources)
    assert client._request.await_count == 2
    first, second = (c.kwargs["json"] for c in client._request.await_args_list)
    assert first["rows"] == rows and second["rows"] == rows
    assert first["battery_state"] == soc[:5_000]
    assert second["battery_state"] == soc[5_000:]
    assert len(first["battery_state"]) == 5_000 and len(second["battery_state"]) == 1_000
    assert first["battery_sources"] == sources and second["battery_sources"] == sources


# ---------------------------------------------------------------------------
# Replacement endpoints (server spec 2026-10-13 §5)
# ---------------------------------------------------------------------------

RID = "11111111-2222-4333-8444-555555555555"
REP_URL = f"{BASE_URL}/api/v1/profile/replacements/{RID}"


def _last(aioclient_mock: AiohttpClientMocker):
    return aioclient_mock.mock_calls[-1]


@pytest.mark.asyncio
async def test_open_replacement_puts_body_with_bearer(aioclient_mock: AiohttpClientMocker):
    from datetime import datetime, timedelta, timezone

    from custom_components.wolta.api import _integration_version

    aioclient_mock.put(REP_URL, status=201, json={"id": RID, "status": "open"})
    client = _client(aioclient_mock)
    start = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    # A non-UTC aware datetime must be serialized as UTC.
    end = (start + timedelta(hours=3)).astimezone(timezone(timedelta(hours=2)))
    out = await client.open_replacement(
        TOKEN, RID, kind="rows", window_start=start, window_end=end,
        fields=["solar_kwh", "grid_import_kwh"], reason="entities_changed",
    )
    assert out == {"id": RID, "status": "open"}
    method, url, body, headers = _last(aioclient_mock)
    assert method.lower() == "put" and str(url).endswith(f"/api/v1/profile/replacements/{RID}")
    assert body == {
        "kind": "rows",
        "window_start": "2026-09-28T12:00:00+00:00",
        "window_end": "2026-09-28T15:00:00+00:00",
        "fields": ["solar_kwh", "grid_import_kwh"],
        "reason": "entities_changed",
        "client": f"wolta-hacs/{_integration_version()}",
    }
    assert headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_open_replacement_swap_omits_fields(aioclient_mock: AiohttpClientMocker):
    from datetime import datetime, timezone

    aioclient_mock.put(REP_URL, status=200, json={"id": RID})
    client = _client(aioclient_mock)
    t = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    await client.open_replacement(
        TOKEN, RID, kind="swap_battery", window_start=t, window_end=t,
        fields=None, reason="direction",
    )
    body = _last(aioclient_mock)[2]
    assert "fields" not in body and body["kind"] == "swap_battery"
    assert body["reason"] == "direction"


@pytest.mark.asyncio
async def test_open_replacement_sends_override_only_when_true(
    aioclient_mock: AiohttpClientMocker,
):
    """`override` is the owner's explicit decision against a SoC verdict: sent as true
    when asked for, and left out entirely otherwise (never `"override": false`)."""
    from datetime import datetime, timezone

    aioclient_mock.put(REP_URL, status=200, json={"id": RID})
    client = _client(aioclient_mock)
    t = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    await client.open_replacement(
        TOKEN, RID, kind="swap_battery", window_start=t, window_end=t,
        fields=None, reason="direction_manual", override=True,
    )
    assert _last(aioclient_mock)[2]["override"] is True
    await client.open_replacement(
        TOKEN, RID, kind="swap_battery", window_start=t, window_end=t,
        fields=None, reason="direction",
    )
    assert "override" not in _last(aioclient_mock)[2]


@pytest.mark.asyncio
async def test_open_replacement_sends_direction_manual(aioclient_mock: AiohttpClientMocker):
    from datetime import datetime, timezone

    aioclient_mock.put(REP_URL, status=201, json={"id": RID})
    client = _client(aioclient_mock)
    t = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    await client.open_replacement(
        TOKEN, RID, kind="swap_battery", window_start=t, window_end=t,
        fields=None, reason="direction_manual", override=False,
    )
    body = _last(aioclient_mock)[2]
    assert body["kind"] == "swap_battery" and body["reason"] == "direction_manual"
    assert "override" not in body


@pytest.mark.asyncio
async def test_open_replacement_rejects_naive_datetimes():
    from datetime import datetime

    client = WoltaApiClient(session=None)
    naive = datetime(2026, 9, 28, 12)
    with pytest.raises(ValueError):
        await client.open_replacement(
            TOKEN, RID, kind="rows", window_start=naive, window_end=naive,
            fields=["solar_kwh"], reason="entities_changed",
        )


@pytest.mark.asyncio
async def test_stage_replacement_rows_chunks_and_returns_last(
    aioclient_mock: AiohttpClientMocker,
):
    from custom_components.wolta.api import MAX_ROWS_PER_PUT

    aioclient_mock.put(
        f"{REP_URL}/rows", status=200, json={"staged_rows": 1, "expires_at": "x"}
    )
    client = _client(aioclient_mock)
    rows = [_row(i) for i in range(MAX_ROWS_PER_PUT + 1)]
    result = await client.stage_replacement_rows(TOKEN, RID, rows)
    puts = [c for c in aioclient_mock.mock_calls if c[0].lower() == "put"]
    assert len(puts) == 2
    assert len(puts[0][2]["rows"]) == MAX_ROWS_PER_PUT and len(puts[1][2]["rows"]) == 1
    assert set(puts[0][2]) == {"rows"}
    assert all(c[3]["Authorization"] == f"Bearer {TOKEN}" for c in puts)
    assert result == {"staged_rows": 1, "expires_at": "x"}


@pytest.mark.asyncio
async def test_stage_replacement_rows_empty_sends_nothing(aioclient_mock: AiohttpClientMocker):
    client = _client(aioclient_mock)
    assert await client.stage_replacement_rows(TOKEN, RID, []) == {}
    assert aioclient_mock.call_count == 0


@pytest.mark.asyncio
async def test_commit_replacement_sends_expected_rows(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{REP_URL}/commit", status=200, json={"data_since": "d"})
    client = _client(aioclient_mock)
    assert await client.commit_replacement(TOKEN, RID, 42) == {"data_since": "d"}
    method, url, body, headers = _last(aioclient_mock)
    assert method.lower() == "post" and str(url).endswith(f"/replacements/{RID}/commit")
    assert body == {"expected_rows": 42}
    assert headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_commit_replacement_swap_sends_empty_body(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{REP_URL}/commit", status=200, json={})
    client = _client(aioclient_mock)
    await client.commit_replacement(TOKEN, RID, None)
    assert _last(aioclient_mock)[2] == {}


@pytest.mark.asyncio
async def test_get_replacement(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(REP_URL, status=200, json={"status": "open", "staged_rows": 3})
    client = _client(aioclient_mock)
    assert await client.get_replacement(TOKEN, RID) == {"status": "open", "staged_rows": 3}
    method, _url, _body, headers = _last(aioclient_mock)
    assert method.lower() == "get" and headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_delete_replacement_returns_none(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.delete(REP_URL, status=204)
    client = _client(aioclient_mock)
    assert await client.delete_replacement(TOKEN, RID) is None
    method, _url, _body, headers = _last(aioclient_mock)
    assert method.lower() == "delete" and headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_replacement_404_with_error_detail_is_gone_not_auth(
    aioclient_mock: AiohttpClientMocker,
):
    aioclient_mock.get(REP_URL, status=404, json={"detail": {"error": "unknown_replacement"}})
    client = _client(aioclient_mock)
    with pytest.raises(WoltaReplacementGone) as exc_info:
        await client.get_replacement(TOKEN, RID)
    assert not isinstance(exc_info.value, WoltaAuthError)
    assert exc_info.value.status == 404 and exc_info.value.code == "unknown_replacement"


@pytest.mark.asyncio
async def test_replacement_404_with_string_detail_is_auth_error(
    aioclient_mock: AiohttpClientMocker,
):
    aioclient_mock.get(REP_URL, status=404, json={"detail": "okänd profil"})
    client = _client(aioclient_mock)
    with pytest.raises(WoltaAuthError):
        await client.get_replacement(TOKEN, RID)


@pytest.mark.asyncio
async def test_replacement_404_with_unreadable_body_is_auth_error(
    aioclient_mock: AiohttpClientMocker,
):
    aioclient_mock.get(REP_URL, status=404, text="<html>not found</html>")
    client = _client(aioclient_mock)
    with pytest.raises(WoltaAuthError):
        await client.get_replacement(TOKEN, RID)


@pytest.mark.asyncio
async def test_replacement_410_is_gone(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.put(
        f"{REP_URL}/rows", status=410, json={"detail": {"error": "replacement_closed"}}
    )
    client = _client(aioclient_mock)
    with pytest.raises(WoltaReplacementGone) as exc_info:
        await client.stage_replacement_rows(TOKEN, RID, [_row(0)])
    assert exc_info.value.status == 410 and exc_info.value.code == "replacement_closed"


@pytest.mark.asyncio
async def test_replacement_410_without_body_is_still_gone(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{REP_URL}/commit", status=410)
    client = _client(aioclient_mock)
    with pytest.raises(WoltaReplacementGone):
        await client.commit_replacement(TOKEN, RID, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["conflict", "count_mismatch", "empty"])
async def test_replacement_409_carries_the_error_code(
    aioclient_mock: AiohttpClientMocker, code: str
):
    aioclient_mock.post(
        f"{REP_URL}/commit", status=409, json={"detail": {"error": code, "message": "m"}}
    )
    client = _client(aioclient_mock)
    with pytest.raises(WoltaReplacementConflict) as exc_info:
        await client.commit_replacement(TOKEN, RID, 5)
    assert exc_info.value.code == code and exc_info.value.status == 409


@pytest.mark.asyncio
async def test_conflict_carries_detail(aioclient_mock: AiohttpClientMocker):
    """The whole server `detail` rides along, so a caller can show what a refusal
    was based on (here the SoC verdict behind `direction_verified`)."""
    from datetime import datetime, timezone

    detail = {
        "error": "direction_verified",
        "ok_days": 10,
        "reversed_days": 0,
        "as_of": "2026-10-30T04:45:10+00:00",
    }
    aioclient_mock.put(REP_URL, status=409, json={"detail": detail})
    client = _client(aioclient_mock)
    t = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    with pytest.raises(WoltaReplacementConflict) as exc_info:
        await client.open_replacement(
            TOKEN, RID, kind="swap_battery", window_start=t, window_end=t,
            fields=None, reason="direction",
        )
    assert exc_info.value.code == "direction_verified"
    assert exc_info.value.detail == detail and exc_info.value.detail["ok_days"] == 10
    # Raised without a detail (as tests and older callers do), it is an empty dict.
    assert WoltaReplacementConflict("conflict").detail == {}


@pytest.mark.asyncio
async def test_replacement_409_without_error_detail_is_generic_api_error(
    aioclient_mock: AiohttpClientMocker,
):
    aioclient_mock.post(f"{REP_URL}/commit", status=409, json={"detail": "x"})
    client = _client(aioclient_mock)
    with pytest.raises(WoltaApiError) as exc_info:
        await client.commit_replacement(TOKEN, RID, 5)
    assert not isinstance(exc_info.value, WoltaReplacementConflict)
    assert exc_info.value.status == 409


@pytest.mark.asyncio
async def test_replacement_409_with_string_detail_is_plain_api_error(
    aioclient_mock: AiohttpClientMocker,
):
    """The stream-bound refusal has a string detail: a plain WoltaApiError(409), never a
    WoltaReplacementConflict."""
    aioclient_mock.put(REP_URL, status=409, json={"detail": "stream-bound plant"})
    client = _client(aioclient_mock)
    from datetime import datetime, timezone

    t = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    with pytest.raises(WoltaApiError) as exc_info:
        await client.open_replacement(
            TOKEN, RID, kind="swap_battery", window_start=t, window_end=t,
            fields=None, reason="direction",
        )
    assert type(exc_info.value) is WoltaApiError and exc_info.value.status == 409


@pytest.mark.asyncio
async def test_replacement_429_archive_full_carries_retry_after(
    aioclient_mock: AiohttpClientMocker,
):
    from datetime import datetime, timezone

    aioclient_mock.put(
        REP_URL, status=429, headers={"Retry-After": "86400"},
        json={"detail": {"error": "archive_full"}},
    )
    client = _client(aioclient_mock)
    t = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    with pytest.raises(WoltaRateLimitError) as exc_info:
        await client.open_replacement(
            TOKEN, RID, kind="swap_battery", window_start=t, window_end=t,
            fields=None, reason="direction",
        )
    assert exc_info.value.retry_after == 86400


@pytest.mark.asyncio
async def test_ordinary_404_elsewhere_is_still_auth_error(aioclient_mock: AiohttpClientMocker):
    """The structured-404 reading is confined to the replacement calls."""
    aioclient_mock.post(
        f"{BASE_URL}/api/v1/profile/recompute", status=404,
        json={"detail": {"error": "unknown_replacement"}},
    )
    client = _client(aioclient_mock)
    with pytest.raises(WoltaAuthError):
        await client.recompute(TOKEN)


# ---------------------------------------------------------------------------
# copies
# ---------------------------------------------------------------------------

_LISTING = {"copies": [{"id": 12, "status": "active", "uploaded": "2026-03-02",
                        "period": {"from": "2025-03-01", "to": "2026-03-01"}, "n_days": 365,
                        "source": "ha_upload", "transport": None, "removable": True}],
            "self": None, "purge_days": 30}


@pytest.mark.asyncio
async def test_get_copies_sends_bearer(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(f"{BASE_URL}/api/v1/copies", json=_LISTING)
    client = _client(aioclient_mock)
    assert await client.get_copies("tok") == _LISTING
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_get_copies_plain_404_returns_none(aioclient_mock: AiohttpClientMocker):
    """An older server has no /copies (FastAPI's plain 404). That is not a lost token: the
    profile call earlier in the same cycle owns re-authentication."""
    aioclient_mock.get(f"{BASE_URL}/api/v1/copies", status=404, json={"detail": "Not Found"})
    client = _client(aioclient_mock)
    assert await client.get_copies("tok") is None


@pytest.mark.asyncio
async def test_remove_copy_404_copy_not_found_is_not_auth(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE_URL}/api/v1/copies/12/remove", status=404,
                        json={"detail": {"error": "copy_not_found", "message": "x"}})
    client = _client(aioclient_mock)
    with pytest.raises(WoltaCopyGone):
        await client.remove_copy("tok", 12)


@pytest.mark.asyncio
async def test_remove_copy_409_busy(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE_URL}/api/v1/copies/12/remove", status=409,
                        json={"detail": {"error": "busy", "message": "x"}})
    client = _client(aioclient_mock)
    with pytest.raises(WoltaCopyBusy):
        await client.remove_copy("tok", 12)


@pytest.mark.asyncio
async def test_remove_copy_plain_404_is_still_auth(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE_URL}/api/v1/copies/12/remove", status=404,
                        json={"detail": "okänd token"})
    client = _client(aioclient_mock)
    with pytest.raises(WoltaAuthError):
        await client.remove_copy("tok", 12)


@pytest.mark.asyncio
async def test_remove_copy_returns_listing(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE_URL}/api/v1/copies/12/remove", json=_LISTING)
    client = _client(aioclient_mock)
    assert await client.remove_copy("tok", 12) == _LISTING


# ---------------------------------------------------------------------------
# Återtagning kräver bevis (Wolta säkerhetsgranskning 2026-10-16, P2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["plant_owned", "plant_exists"])
async def test_create_profile_409_reclaim_raises_plant_claimed(
    aioclient_mock: AiohttpClientMocker, code: str
):
    """Backend nekar att ge en ny token till en anläggning som ett konto äger (eller som
    finns utan bevis). Klienten ska kunna skilja det från ett vanligt fel - reauth-flödet
    leder då användaren till att klistra in en ny token från webben."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile", status=409,
                        json={"detail": {"error": code, "message": "x"}})
    client = _client(aioclient_mock)
    with pytest.raises(WoltaPlantClaimed) as exc:
        await client.create_profile(zone="SE3", has_solar=False, share_profile=True,
                                    battery_kwh=10, battery_kw=5,
                                    client_plant_id="a" * 32)
    assert exc.value.code == code
    assert exc.value.status == 409


@pytest.mark.asyncio
async def test_create_profile_plain_409_is_not_plant_claimed(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile", status=409, json={"detail": "annat"})
    client = _client(aioclient_mock)
    with pytest.raises(WoltaApiError) as exc:
        await client.create_profile(zone="SE3", has_solar=False, share_profile=True,
                                    battery_kwh=10, battery_kw=5)
    assert not isinstance(exc.value, WoltaPlantClaimed)


# ---------------------------------------------------------------------------
# create_profile / patch_profile with the owner's grid benefit (api 0.119.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_profile_sends_the_grid_benefit_fields_including_zero(
    aioclient_mock: AiohttpClientMocker,
):
    """0 is an answer ("no grid benefit"), so it is sent; None is omitted."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile", status=201, json={"profile_token": TOKEN})
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3", battery_kwh=22.0, battery_kw=5.0, has_solar=True, share_profile=False,
        grid_benefit_ore=0.0, export_extra_ore=-3.0,
    )
    body = [c for c in aioclient_mock.mock_calls if c[0].lower() == "post"][0][2]
    assert body["grid_benefit_ore"] == 0.0
    assert body["export_extra_ore"] == -3.0


@pytest.mark.asyncio
async def test_create_profile_omits_the_grid_benefit_when_none(
    aioclient_mock: AiohttpClientMocker,
):
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile", status=201, json={"profile_token": TOKEN})
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3", battery_kwh=22.0, battery_kw=5.0, has_solar=True, share_profile=False)
    body = [c for c in aioclient_mock.mock_calls if c[0].lower() == "post"][0][2]
    assert "grid_benefit_ore" not in body


@pytest.mark.asyncio
async def test_patch_profile_passes_the_grid_benefit_through(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.patch(f"{BASE_URL}/api/v1/profile", status=200, json={})
    client = _client(aioclient_mock)
    await client.patch_profile(
        TOKEN, grid_benefit_ore=None, export_extra_ore=5.0)
    body = [c for c in aioclient_mock.mock_calls if c[0].lower() == "patch"][0][2]
    assert body == {"grid_benefit_ore": None, "export_extra_ore": 5.0}


# ---------------------------------------------------------------------------
# create_profile / patch_profile with today's electricity contract (api 0.119.0). The time
# periods and the contract history are never sent from Home Assistant.
# ---------------------------------------------------------------------------

_CONTRACT_PAYLOAD = {"retail_price_model": "fixed", "retail_fixed_ore": 0.0}


@pytest.mark.asyncio
async def test_create_profile_sends_the_contract_including_zero(
    aioclient_mock: AiohttpClientMocker,
):
    """A fixed price of 0 is an answer, so only None is omitted."""
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile", status=201, json={"profile_token": TOKEN})
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3", battery_kwh=22.0, battery_kw=5.0, has_solar=True, share_profile=False,
        **_CONTRACT_PAYLOAD)
    body = [c for c in aioclient_mock.mock_calls if c[0].lower() == "post"][0][2]
    for key, value in _CONTRACT_PAYLOAD.items():
        assert body[key] == value, key
        assert type(body[key]) is type(value), key


@pytest.mark.asyncio
async def test_create_profile_omits_the_contract_when_none(
    aioclient_mock: AiohttpClientMocker,
):
    aioclient_mock.post(f"{BASE_URL}/api/v1/profile", status=201, json={"profile_token": TOKEN})
    client = _client(aioclient_mock)
    await client.create_profile(
        zone="SE3", battery_kwh=22.0, battery_kw=5.0, has_solar=True, share_profile=False)
    body = [c for c in aioclient_mock.mock_calls if c[0].lower() == "post"][0][2]
    for key in _CONTRACT_PAYLOAD:
        assert key not in body, key


@pytest.mark.asyncio
async def test_patch_profile_passes_the_contract_through(aioclient_mock: AiohttpClientMocker):
    aioclient_mock.patch(f"{BASE_URL}/api/v1/profile", status=200, json={})
    client = _client(aioclient_mock)
    await client.patch_profile(
        TOKEN, retail_price_model="month", retail_fixed_ore=None)
    body = [c for c in aioclient_mock.mock_calls if c[0].lower() == "patch"][0][2]
    assert body == {"retail_price_model": "month", "retail_fixed_ore": None}
