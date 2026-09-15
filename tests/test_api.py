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
    WoltaRateLimitError,
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
