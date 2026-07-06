"""Tests for custom_components/wolta/button.py (TDD)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.wolta.api import WoltaApiError, WoltaRateLimitError
from custom_components.wolta.button import WoltaRecomputeButton
from custom_components.wolta.coordinator import WoltaCoordinator, WoltaData

TOKEN = "tok-test-b6-btn"
ENTRY_ID = "entry_b6_btn"


def _make_coordinator(raise_on_recompute=None) -> WoltaCoordinator:
    coord = MagicMock(spec=WoltaCoordinator)
    coord.data = MagicMock(spec=WoltaData)
    coord.async_trigger_recompute = AsyncMock(side_effect=raise_on_recompute)
    coord.async_request_refresh = AsyncMock()
    return coord


def _make_button(coord: WoltaCoordinator) -> WoltaRecomputeButton:
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    entry.unique_id = ENTRY_ID
    entry.data = {"token": "tok-test"}
    entry.runtime_data = coord
    btn = WoltaRecomputeButton(coordinator=coord, entry=entry)
    return btn


# ---------------------------------------------------------------------------
# press calls coordinator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_button_press_calls_recompute():
    """press() calls recompute AND refreshes results so the latest grade shows."""
    coord = _make_coordinator()
    btn = _make_button(coord)
    await btn.async_press()
    coord.async_trigger_recompute.assert_awaited_once()
    coord.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# rate-limit (cooldown) → silent refresh, NO error toast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_button_press_rate_limit_refreshes_without_error():
    """A recompute cooldown (429) must NOT raise a scary error — the grade is already
    computed; the button just refreshes the shown results."""
    coord = _make_coordinator(raise_on_recompute=WoltaRateLimitError(retry_after=3600))
    btn = _make_button(coord)
    # Must not raise
    await btn.async_press()
    coord.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# M3: WoltaApiError (e.g. 422) → HomeAssistantError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_button_press_api_error_raises_homeassistant_error():
    """WoltaApiError (e.g. 422 / cooldown) → HomeAssistantError, not raw exception."""
    coord = _make_coordinator(
        raise_on_recompute=WoltaApiError("HTTP 422 from .../recompute: cooldown", status=422)
    )
    btn = _make_button(coord)
    with pytest.raises(HomeAssistantError):
        await btn.async_press()


# ---------------------------------------------------------------------------
# unique_id and device info
# ---------------------------------------------------------------------------


def test_button_unique_id():
    coord = _make_coordinator()
    btn = _make_button(coord)
    assert btn.unique_id == f"{ENTRY_ID}_recompute"


def test_button_has_translation_key():
    """v0.4.3: namnet kommer från translations (sv: Räkna om / en: Recompute)."""
    coord = _make_coordinator()
    btn = _make_button(coord)
    assert btn._attr_translation_key == "recompute"
    assert btn._attr_has_entity_name is True
