"""Tests for custom_components/wolta/api.py (TDD – write first, then implement)."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wolta.api import (
    WoltaApiClient,
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
        f"{BASE_URL}/api/v1/profile/{TOKEN}/results",
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
        f"{BASE_URL}/api/v1/profile/{TOKEN}/recompute",
        status=429,
        headers={"Retry-After": "3600"},
        json={"detail": "rate limit"},
    )
    client = _client(aioclient_mock)
    with pytest.raises(WoltaRateLimitError) as exc_info:
        await client.recompute(TOKEN)
    assert exc_info.value.retry_after == 3600


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

    url = f"{BASE_URL}/api/v1/profile/{TOKEN}/data"
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
        f"{BASE_URL}/api/v1/profile/{TOKEN}/results",
        status=200,
        json=payload,
    )
    client = _client(aioclient_mock)
    result = await client.results(TOKEN)
    assert result == payload
    assert result["betyg"] == "A"


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
# patch_profile (v0.3.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_profile_issues_patch_request(aioclient_mock: AiohttpClientMocker):
    """patch_profile issues PATCH to /api/v1/profile/{token} with provided fields."""
    aioclient_mock.patch(
        f"{BASE_URL}/api/v1/profile/{TOKEN}",
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


@pytest.mark.asyncio
async def test_patch_profile_404_raises_auth_error(aioclient_mock: AiohttpClientMocker):
    """patch_profile on 404 raises WoltaAuthError."""
    aioclient_mock.patch(
        f"{BASE_URL}/api/v1/profile/{TOKEN}",
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
        f"{BASE_URL}/api/v1/profile/{TOKEN}",
        status=429,
        headers={"Retry-After": "7200"},
        json={"detail": "rate limit"},
    )
    client = _client(aioclient_mock)
    with pytest.raises(WoltaRateLimitError) as exc_info:
        await client.patch_profile(TOKEN, purchase_date="2024-01-01")
    assert exc_info.value.retry_after == 7200
