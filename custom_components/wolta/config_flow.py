"""Config flow for the Wolta integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    BooleanSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import WoltaApiClient, WoltaApiError
from .const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_BATTERY_KW,
    CONF_BATTERY_KWH,
    CONF_EFF,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_SHARE,
    CONF_SOLAR,
    CONF_TOKEN,
    CONF_ZONE,
    DEFAULT_BATTERY_KW,
    DEFAULT_BATTERY_KWH,
    DEFAULT_EFF,
    DEFAULT_SHARE,
    DEFAULT_ZONE,
    DOMAIN,
    MIN_BATTERY_KW,
    MIN_BATTERY_KWH,
    SUPPORTED_ZONES,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Energy dashboard prefill helper
# ---------------------------------------------------------------------------


async def _energy_dashboard_defaults(hass: Any) -> dict:
    """Extract entity IDs from the HA energy dashboard configuration.

    Returns lists per stream, since multiple sources of the same type are allowed
    (e.g. two solar inverters). Each list contains sensor.* entity IDs only.
    Handles both the unified grid format (stat_energy_from/to directly on the
    source dict) and the legacy flow_from/flow_to list format.

    Returns an empty dict when the energy component is not configured.
    """
    try:
        manager = await async_get_manager(hass)
        prefs = manager.data
    except Exception:
        return {}

    batt_in: list[str] = []
    batt_out: list[str] = []
    grid_in: list[str] = []
    grid_out: list[str] = []
    solar: list[str] = []

    def _sensor(val: Any) -> str | None:
        return val if isinstance(val, str) and val.startswith("sensor.") else None

    for src in (prefs or {}).get("energy_sources", []):
        if src["type"] == "battery":
            # stat_energy_to = charge-from-grid meter (batt_in)
            # stat_energy_from = discharge-to-home meter (batt_out)
            v = _sensor(src.get("stat_energy_to"))
            if v and v not in batt_in:
                batt_in.append(v)
            v = _sensor(src.get("stat_energy_from"))
            if v and v not in batt_out:
                batt_out.append(v)
        elif src["type"] == "solar":
            v = _sensor(src.get("stat_energy_from"))
            if v and v not in solar:
                solar.append(v)
        elif src["type"] == "grid":
            # Unified format: stat_energy_from/to directly on the source
            if src.get("stat_energy_from"):
                v = _sensor(src["stat_energy_from"])
                if v and v not in grid_in:
                    grid_in.append(v)
                v = _sensor(src.get("stat_energy_to"))
                if v and v not in grid_out:
                    grid_out.append(v)
            # Legacy format: flow_from/flow_to lists
            for f in src.get("flow_from", []) or []:
                v = _sensor(f.get("stat_energy_from"))
                if v and v not in grid_in:
                    grid_in.append(v)
            for f in src.get("flow_to", []) or []:
                v = _sensor(f.get("stat_energy_to"))
                if v and v not in grid_out:
                    grid_out.append(v)

    out: dict[str, list[str]] = {}
    if batt_in:
        out["batt_in"] = batt_in
    if batt_out:
        out["batt_out"] = batt_out
    if grid_in:
        out["grid_in"] = grid_in
    if grid_out:
        out["grid_out"] = grid_out
    if solar:
        out["solar"] = solar
    return out


# ---------------------------------------------------------------------------
# Selector helpers
# ---------------------------------------------------------------------------


def _zone_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=zone_id, label=label)
                for zone_id, label in SUPPORTED_ZONES
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _number_selector(
    min_val: float = 0.0,
    max_val: float = 1000.0,
    step: float = 0.1,
    unit: str | None = None,
) -> NumberSelector:
    cfg: dict[str, Any] = {
        "min": min_val,
        "max": max_val,
        "step": step,
        "mode": NumberSelectorMode.BOX,
    }
    if unit:
        cfg["unit_of_measurement"] = unit
    return NumberSelector(NumberSelectorConfig(**cfg))


def _energy_entity_selector() -> EntitySelector:
    return EntitySelector(
        EntitySelectorConfig(domain="sensor", device_class="energy", multiple=True)
    )


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class WoltaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Wolta integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._user_data: dict[str, Any] = {}
        self._entities_data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1: zone + battery parameters
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the zone and battery configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._user_data = user_input
            return await self.async_step_entities()

        schema = vol.Schema(
            {
                vol.Required(CONF_ZONE, default=DEFAULT_ZONE): _zone_selector(),
                vol.Required(CONF_BATTERY_KWH, default=DEFAULT_BATTERY_KWH): _number_selector(
                    min_val=MIN_BATTERY_KWH, max_val=500.0, step=0.5, unit="kWh"
                ),
                vol.Required(CONF_BATTERY_KW, default=DEFAULT_BATTERY_KW): _number_selector(
                    min_val=MIN_BATTERY_KW, max_val=100.0, step=0.1, unit="kW"
                ),
                vol.Required(CONF_EFF, default=DEFAULT_EFF): _number_selector(
                    min_val=0.5, max_val=1.0, step=0.01
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2: entity selectors (with energy-dashboard prefill)
    # ------------------------------------------------------------------

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the entity-selector step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate: required streams must have at least one sensor selected
            required_streams = [CONF_BATT_IN, CONF_BATT_OUT, CONF_GRID_IN, CONF_GRID_OUT]
            for key in required_streams:
                val = user_input.get(key)
                if not val:  # None, missing, or empty list
                    errors[key] = "required_sensor"

            if not errors:
                self._entities_data = user_input
                return await self.async_step_privacy()

        # Prefill from HA energy dashboard if configured (returns lists)
        # When re-showing after errors, use submitted values as defaults
        if errors and user_input is not None:
            defaults = user_input
        else:
            defaults = await _energy_dashboard_defaults(self.hass)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_BATT_IN,
                    default=defaults.get(CONF_BATT_IN, vol.UNDEFINED),
                ): _energy_entity_selector(),
                vol.Required(
                    CONF_BATT_OUT,
                    default=defaults.get(CONF_BATT_OUT, vol.UNDEFINED),
                ): _energy_entity_selector(),
                vol.Required(
                    CONF_GRID_IN,
                    default=defaults.get(CONF_GRID_IN, vol.UNDEFINED),
                ): _energy_entity_selector(),
                vol.Required(
                    CONF_GRID_OUT,
                    default=defaults.get(CONF_GRID_OUT, vol.UNDEFINED),
                ): _energy_entity_selector(),
                vol.Optional(
                    CONF_SOLAR,
                    default=defaults.get(CONF_SOLAR, vol.UNDEFINED),
                ): _energy_entity_selector(),
            }
        )

        return self.async_show_form(
            step_id="entities",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3: privacy consent + create profile
    # ------------------------------------------------------------------

    async def async_step_privacy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the privacy/consent step and create the profile."""
        errors: dict[str, str] = {}

        if user_input is not None:
            share = user_input.get(CONF_SHARE, DEFAULT_SHARE)
            zone = self._user_data[CONF_ZONE]
            solar = self._entities_data.get(CONF_SOLAR)

            try:
                session = async_get_clientsession(self.hass)
                client = WoltaApiClient(session)
                token = await client.create_profile(
                    zone=zone,
                    battery_kwh=self._user_data[CONF_BATTERY_KWH],
                    battery_kw=self._user_data[CONF_BATTERY_KW],
                    eff=self._user_data[CONF_EFF],
                    has_solar=bool(solar),
                    share_profile=share,
                )
            except WoltaApiError as err:
                _LOGGER.error("Failed to create Wolta profile: %s", err)
                if getattr(err, "status", None) == 422:
                    errors["base"] = "invalid_input"
                else:
                    errors["base"] = "cannot_connect"
            else:
                unique_id = hashlib.sha256(token.encode()).hexdigest()[:16]
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                entry_data: dict[str, Any] = {
                    CONF_TOKEN: token,
                    CONF_ZONE: zone,
                    CONF_BATT_IN: self._entities_data[CONF_BATT_IN],
                    CONF_BATT_OUT: self._entities_data[CONF_BATT_OUT],
                    CONF_GRID_IN: self._entities_data[CONF_GRID_IN],
                    CONF_GRID_OUT: self._entities_data[CONF_GRID_OUT],
                    CONF_BATTERY_KWH: self._user_data[CONF_BATTERY_KWH],
                    CONF_BATTERY_KW: self._user_data[CONF_BATTERY_KW],
                    CONF_EFF: self._user_data[CONF_EFF],
                    CONF_SHARE: share,
                }
                if solar:
                    entry_data[CONF_SOLAR] = solar

                return self.async_create_entry(
                    title=f"Wolta ({zone})",
                    data=entry_data,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_SHARE, default=DEFAULT_SHARE): BooleanSelector(
                    BooleanSelectorConfig()
                ),
            }
        )

        return self.async_show_form(
            step_id="privacy",
            data_schema=schema,
            errors=errors,
            description_placeholders={},
        )

    # ------------------------------------------------------------------
    # Reauth flow
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication (e.g. after profile purge)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the reauth confirmation step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            entry_data = dict(reauth_entry.data)

            try:
                session = async_get_clientsession(self.hass)
                client = WoltaApiClient(session)
                new_token = await client.create_profile(
                    zone=entry_data[CONF_ZONE],
                    battery_kwh=entry_data[CONF_BATTERY_KWH],
                    battery_kw=entry_data[CONF_BATTERY_KW],
                    eff=entry_data[CONF_EFF],
                    has_solar=bool(entry_data.get(CONF_SOLAR)),
                    share_profile=entry_data.get(CONF_SHARE, DEFAULT_SHARE),
                )
            except WoltaApiError as err:
                _LOGGER.error("Reauth failed – could not create Wolta profile: %s", err)
                if getattr(err, "status", None) == 422:
                    errors["base"] = "invalid_input"
                else:
                    errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_TOKEN: new_token},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
        )
