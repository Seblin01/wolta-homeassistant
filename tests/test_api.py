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
