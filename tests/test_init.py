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


# ---------------------------------------------------------------------------
# B8: radera-skydd – länkade profiler (created_by_ha=False) överlever borttagning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_remove_entry_keeps_linked_profile(hass: HomeAssistant):
    """created_by_ha=False (koppla-spåret) → INGEN server-DELETE vid borttagning.
    Webbprofilen inkl. CSV-historik är inte vår att radera."""
    from custom_components.wolta.const import CONF_CREATED_BY_HA

    mock_entry = MagicMock()
    mock_entry.data = {**ENTRY_DATA, CONF_CREATED_BY_HA: False}

    mock_client = MagicMock()
    mock_client.delete = AsyncMock()

    with (
        patch("custom_components.wolta.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.async_get_clientsession"),
    ):
        from custom_components.wolta import async_remove_entry

        await async_remove_entry(hass, mock_entry)

    mock_client.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_remove_entry_legacy_entry_still_deletes(hass: HomeAssistant):
    """Nyckeln saknas (pre-v0.10.0-entry) → default True → DELETE som dokumenterat."""
    mock_entry = MagicMock()
    mock_entry.data = ENTRY_DATA.copy()
    assert "created_by_ha" not in mock_entry.data

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
# View-only smoke: real coordinator + real platform setup over an entry
# without any stream/entity fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_view_only_entry_platforms_smoke(hass: HomeAssistant):
    """Real WoltaCoordinator + real sensor/button setup over a view-only entry.

    A view-only entry has NO stream fields (batt_in/out, grid_in/out, solar) - this pins
    that neither the coordinator constructor nor the platform setups assume them.
    (Coverage pin: green from day one; guards future platform code against reading
    entry.data streaming keys unconditionally.)
    """
    from homeassistant.config_entries import ConfigEntry

    from custom_components.wolta import button as button_mod
    from custom_components.wolta import sensor as sensor_mod
    from custom_components.wolta.const import CONF_VIEW_ONLY
    from custom_components.wolta.coordinator import WoltaCoordinator

    results = {
        "status": "done",
        "currency": "SEK",
        "period": {"start": "2025-01-01", "end": "2025-05-31", "n_days": 150},
        "job": {"status": "done", "step": None},
        "betyg": {"holistic": {"score_on": 0.67}},
        "decision": {"irr": 0.1},
        "history": {"yearly": []},
    }

    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "view_only_smoke"
    entry.domain = DOMAIN
    entry.data = {CONF_TOKEN: TOKEN, CONF_ZONE: ZONE, CONF_VIEW_ONLY: True}
    entry.state = ConfigEntryState.SETUP_IN_PROGRESS
    entry.unique_id = "uid-view-smoke"

    coordinator = WoltaCoordinator(hass, entry)
    client = MagicMock()
    client.results = AsyncMock(return_value=results)
    client.get_profile = AsyncMock(side_effect=Exception("sync skipped in test"))
    coordinator.client = client
    coordinator._state = {}
    coordinator._store = MagicMock()
    coordinator._store.async_save = AsyncMock()

    data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(data)
    client.put_data.assert_not_called()

    entry.runtime_data = coordinator

    async def _run_platform(mod):
        entities: list = []
        await mod.async_setup_entry(hass, entry, lambda new: entities.extend(new))
        return entities

    sensors = await _run_platform(sensor_mod)
    buttons = await _run_platform(button_mod)
    assert len(sensors) == len(sensor_mod.SENSOR_DESCRIPTIONS)
    assert len(buttons) >= 1

    # Varje entitets värde/attribut ska gå att läsa utan att kasta.
    for ent in sensors:
        _ = ent.native_value
        _ = ent.extra_state_attributes
        _ = ent.available
