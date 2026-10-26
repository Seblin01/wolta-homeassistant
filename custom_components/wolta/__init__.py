"""The Wolta integration."""

from __future__ import annotations

import logging
import re
import secrets

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WoltaApiClient, WoltaApiError
from .const import (
    CONF_CREATED_BY_HA,
    CONF_LINK_TOKEN,
    CONF_PLANT_ID,
    CONF_TOKEN,
    CONF_VIEW_ONLY,
    DOMAIN,
    WOLTA_API_BASE,
)
from .coordinator import WoltaCoordinator
from .price_coordinator import WoltaPriceCoordinator

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

    await _migrate_plant_identity(hass, entry, client)

    coordinator = WoltaCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    # Spot prices (information only, never read by the grade). async_refresh, not
    # async_config_entry_first_refresh: a failing price endpoint must not stop the entry.
    coordinator.prices = WoltaPriceCoordinator(hass, entry, client)
    await coordinator.prices.async_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


# What every entry since v0.16.0 sends: secrets.token_hex(16) (config_flow).
_RANDOM_PLANT_ID = re.compile(r"[0-9a-f]{32}")


async def _migrate_plant_identity(
    hass: HomeAssistant, entry: ConfigEntry, client: WoltaApiClient
) -> None:
    """Replace a legacy plant id with a fresh 128-bit secret while the token still works.

    Before v0.16.0 an entry had no id of its own, and reauth fell back to Home Assistant's
    entry_id - a ULID that Home Assistant itself puts in the address bar
    (#config_entry=<id>) and that therefore shows up in screenshots. The backend treats the
    id as a client secret on plants without a wolta.se account (Wolta security review
    2026-10-16), so it has to be one. adopt stamps the new id on our own row - the backend
    allows a row to change its own identity, never to take another row's.

    Best-effort like mint_link above: any failure leaves entry.data untouched and the
    next setup tries again. View-only entries are skipped: their plant belongs to its
    Sonnen/Reduxi binding, and Home Assistant never stamps an identity on it."""
    if entry.data.get(CONF_VIEW_ONLY):
        return
    current = entry.data.get(CONF_PLANT_ID)
    if current and current != entry.entry_id and _RANDOM_PLANT_ID.fullmatch(current):
        return
    new_id = secrets.token_hex(16)
    try:
        await client.adopt_profile(entry.data[CONF_TOKEN], client_plant_id=new_id)
    except (WoltaApiError, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("Plant id migration failed; will retry on next setup: %s", err)
        return
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PLANT_ID: new_id})


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _delete_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the repairs of one config entry. Every per-entry issue id ends with
    _<entry_id>, and the issue registry does not clean up per entry, so a removed
    plant's repairs would stand until the next restart (spec 2026-10-15 F3)."""
    suffix = f"_{entry_id}"
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.endswith(suffix):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry – delete the Wolta profile (right-to-erasure).

    Deleting the config entry IS the "delete all my data" action; documented in
    README and strings.  Errors are swallowed (best-effort; the profile may
    already be purged server-side).
    """
    _delete_entry_issues(hass, entry.entry_id)
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
