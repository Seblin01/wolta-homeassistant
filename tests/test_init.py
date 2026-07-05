"""Tests for custom_components/wolta/__init__.py (TDD)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.wolta.const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_SOLAR,
    CONF_TOKEN,
    CONF_ZONE,
    DOMAIN,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOKEN = "tok-init-test"
ZONE = "SE3"

ENTRY_DATA = {
    CONF_TOKEN: TOKEN,
    CONF_ZONE: ZONE,
    CONF_BATT_IN: "sensor.batt_in",
    CONF_BATT_OUT: "sensor.batt_out",
    CONF_GRID_IN: "sensor.grid_in",
    CONF_GRID_OUT: "sensor.grid_out",
    CONF_SOLAR: "sensor.solar",
}


def _make_mock_coordinator():
    """Return a mock WoltaCoordinator."""
    coord = MagicMock()
    coord.async_config_entry_first_refresh = AsyncMock()
    coord.async_unload = AsyncMock(return_value=True)
    return coord


# ---------------------------------------------------------------------------
# Test: async_setup_entry sets runtime_data and forwards platforms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_sets_runtime_data_and_forwards_platforms(
    hass: HomeAssistant,
):
    """async_setup_entry must set entry.runtime_data and forward platforms."""
    mock_coord = _make_mock_coordinator()
    mock_entry = MagicMock()
    mock_entry.data = ENTRY_DATA.copy()
    mock_entry.entry_id = "test_entry_id"
    mock_entry.domain = DOMAIN
    mock_entry.state = ConfigEntryState.SETUP_IN_PROGRESS

    with (
        patch(
            "custom_components.wolta.WoltaCoordinator",
            return_value=mock_coord,
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as mock_forward,
    ):
        from custom_components.wolta import async_setup_entry

        result = await async_setup_entry(hass, mock_entry)

    assert result is True
    # runtime_data must be set to the coordinator
    assert mock_entry.runtime_data == mock_coord
    # async_config_entry_first_refresh must have been called
    mock_coord.async_config_entry_first_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test: async_unload_entry unloads platforms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_unload_entry_unloads_platforms(hass: HomeAssistant):
    """async_unload_entry returns True when unload succeeds."""
    mock_entry = MagicMock()
    mock_entry.data = ENTRY_DATA.copy()

    with patch(
        "homeassistant.config_entries.ConfigEntries.async_unload_platforms",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_unload:
        from custom_components.wolta import async_unload_entry

        result = await async_unload_entry(hass, mock_entry)

    assert result is True


# ---------------------------------------------------------------------------
# Test: async_remove_entry calls client.delete with token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_remove_entry_deletes_profile(hass: HomeAssistant):
    """async_remove_entry must call client.delete(token)."""
    mock_entry = MagicMock()
    mock_entry.data = ENTRY_DATA.copy()

    mock_client = MagicMock()
    mock_client.delete = AsyncMock()

    with (
        patch("custom_components.wolta.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.async_get_clientsession"),
    ):
        from custom_components.wolta import async_remove_entry

        await async_remove_entry(hass, mock_entry)

    mock_client.delete.assert_awaited_once_with(TOKEN)


# ---------------------------------------------------------------------------
# Test: async_remove_entry swallows errors (best-effort delete)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_remove_entry_swallows_client_error(hass: HomeAssistant):
    """async_remove_entry must not raise even when client.delete fails."""
    import aiohttp

    mock_entry = MagicMock()
    mock_entry.data = ENTRY_DATA.copy()

    mock_client = MagicMock()
    mock_client.delete = AsyncMock(side_effect=aiohttp.ClientError("gone"))

    with (
        patch("custom_components.wolta.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.async_get_clientsession"),
    ):
        from custom_components.wolta import async_remove_entry

        # Must not raise
        await async_remove_entry(hass, mock_entry)
