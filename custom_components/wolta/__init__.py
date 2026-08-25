"""The Wolta integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WoltaApiClient
from .const import CONF_CREATED_BY_HA, CONF_LINK_TOKEN, CONF_TOKEN, WOLTA_API_BASE
from .coordinator import WoltaCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wolta from a config entry."""
    # Mint vid VARJE setup (spec §4.1). Idempotent server-side, så det här är gratis –
    # och det är vad som gör flödet självläkande utan specialfall: reauth till annan
    # anläggning, rotation/revoke gjord utifrån, två HA-instanser mot samma anläggning
    # och ägar-token-rotation hanteras alla av att nästa setup hämtar aktuell länk.
    # Ingen reload-risk: ingen update listener är registrerad (se coordinator.py:217).
    client = WoltaApiClient(async_get_clientsession(hass), base_url=WOLTA_API_BASE)
    link_token = await client.mint_link(entry.data[CONF_TOKEN])
    if link_token and link_token != entry.data.get(CONF_LINK_TOKEN):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_LINK_TOKEN: link_token})

    coordinator = WoltaCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry – delete the Wolta profile (right-to-erasure).

    Deleting the config entry IS the "delete all my data" action; documented in
    README and strings.  Errors are swallowed (best-effort; the profile may
    already be purged server-side).
    """
    # Linked profiles (created on wolta.se, adopted via the link flow) must survive
    # integration removal – the user's web profile incl. CSV history is not ours to
    # delete. Default True keeps the documented delete-on-remove behaviour for all
    # HA-created entries (every pre-v0.10.0 install).
    if not entry.data.get(CONF_CREATED_BY_HA, True):
        _LOGGER.debug("Linked Wolta profile – skipping server-side delete on removal")
        return

    token = entry.data[CONF_TOKEN]
    try:
        session = async_get_clientsession(hass)
        client = WoltaApiClient(session, base_url=WOLTA_API_BASE)
        await client.delete(token)
        _LOGGER.debug("Wolta profile %s deleted on entry removal", token[:8])
    except Exception:  # pylint: disable=broad-except
        _LOGGER.warning(
            "Could not delete Wolta profile on entry removal (best-effort); "
            "the profile may already be purged or the server was unreachable.",
            exc_info=True,
        )
