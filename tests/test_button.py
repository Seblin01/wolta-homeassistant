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


@pytest.mark.asyncio
async def test_button_press_api_error_is_translated():
    """v0.8.0: the error toast is raised via translation_key (English default,
    Swedish in sv.json) instead of a hardcoded Swedish string."""
    coord = _make_coordinator(
        raise_on_recompute=WoltaApiError("HTTP 422 from .../recompute: cooldown", status=422)
    )
    btn = _make_button(coord)
    with pytest.raises(HomeAssistantError) as ei:
        await btn.async_press()
    assert ei.value.translation_domain == "wolta"
    assert ei.value.translation_key == "recompute_failed"


# ---------------------------------------------------------------------------
# unique_id and device info
# ---------------------------------------------------------------------------


def test_button_unique_id():
    coord = _make_coordinator()
    btn = _make_button(coord)
    assert btn.unique_id == f"{ENTRY_ID}_recompute"


def test_button_has_translation_key():
    """v0.4.3: the name comes from translations (sv: Räkna om / en: Recompute)."""
    coord = _make_coordinator()
    btn = _make_button(coord)
    assert btn._attr_translation_key == "recompute"
    assert btn._attr_has_entity_name is True


# ---------------------------------------------------------------------------
# configuration_url: the read link, never the owner token (spec 2026-08-24)
# ---------------------------------------------------------------------------


def test_button_device_info_configuration_url_anvander_laslanken() -> None:
    """Kodgranskning (kritiskt fynd 2): sensor.py and button.py both set
    configuration_url independently, so a sensor-only test does not guard this file.
    Builds a REAL WoltaRecomputeButton from an entry with BOTH a distinct owner token
    and a distinct link token, and checks the produced configuration_url."""
    from custom_components.wolta.const import CONF_LINK_TOKEN, CONF_TOKEN

    coord = _make_coordinator()
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    entry.unique_id = ENTRY_ID
    entry.data = {CONF_TOKEN: "OWNER-SECRET-DO-NOT-LEAK", CONF_LINK_TOKEN: "wpl_readonly"}
    entry.runtime_data = coord

    btn = WoltaRecomputeButton(coordinator=coord, entry=entry)
    url = btn._attr_device_info["configuration_url"]

    assert "wpl_readonly" in url
    assert "OWNER-SECRET-DO-NOT-LEAK" not in url


def test_button_device_info_configuration_url_fallback_utan_lanken() -> None:
    """Ingen cachad länk (mint har aldrig lyckats) → tokenlös sida, och fortfarande
    ALDRIG ägar-tokenet."""
    from custom_components.wolta.const import CONF_TOKEN

    coord = _make_coordinator()
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    entry.unique_id = ENTRY_ID
    entry.data = {CONF_TOKEN: "OWNER-SECRET-DO-NOT-LEAK"}  # no CONF_LINK_TOKEN
    entry.runtime_data = coord

    btn = WoltaRecomputeButton(coordinator=coord, entry=entry)
    url = btn._attr_device_info["configuration_url"]

    assert url == "https://wolta.se/anlaggning"
    assert "OWNER-SECRET-DO-NOT-LEAK" not in url
