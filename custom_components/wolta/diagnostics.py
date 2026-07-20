"""Diagnostics support for Wolta (profile token and plant id redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PLANT_ID, CONF_TOKEN

# CONF_PLANT_ID is redacted alongside the token: it is the key the backend uses to recognise
# this plant, so anyone holding it can re-onboard as this plant and take over its row. Users
# routinely attach diagnostics dumps to GitHub issues and forum posts.
TO_REDACT = [CONF_TOKEN, CONF_PLANT_ID]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (token redacted)."""
    coordinator = getattr(entry, "runtime_data", None)
    state: dict[str, Any] = dict(getattr(coordinator, "_state", {}) or {})
    data = getattr(coordinator, "data", None)
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "coordinator_state": state,
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "results": getattr(data, "results", None),
    }
