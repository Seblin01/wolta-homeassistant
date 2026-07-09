"""Config flow for the Wolta integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    BooleanSelectorConfig,
    DateSelector,
    DateSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import WoltaApiClient, WoltaApiError, WoltaRateLimitError
from .const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_BATTERY_KW,
    CONF_BATTERY_KWH,
    CONF_COST_SEK,
    CONF_EFF,
    CONF_EXPORT_EXTRA_ORE,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_GRID_VAR_ORE,
    CONF_INVERT_BATTERY,
    CONF_PURCHASE_DATE,
    CONF_SHARE,
    CONF_SOLAR,
    CONF_SURCHARGE_ORE,
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


def _date_selector() -> DateSelector:
    return DateSelector(DateSelectorConfig())


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class WoltaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Wolta integration."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> "WoltaOptionsFlow":
        """Return the options flow for this handler."""
        return WoltaOptionsFlow(config_entry)

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
                vol.Optional(CONF_COST_SEK): _number_selector(
                    min_val=0.0, max_val=10_000_000.0, step=100.0, unit="kr"
                ),
                vol.Optional(CONF_PURCHASE_DATE): _date_selector(),
                vol.Optional(CONF_GRID_VAR_ORE): _number_selector(
                    min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh"
                ),
                vol.Optional(CONF_SURCHARGE_ORE): _number_selector(
                    min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh"
                ),
                vol.Optional(CONF_EXPORT_EXTRA_ORE): _number_selector(
                    min_val=-200.0, max_val=500.0, step=0.1, unit="öre/ct per kWh"
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
            cost_sek: float | None = self._user_data.get(CONF_COST_SEK) or None
            purchase_date: str | None = self._user_data.get(CONF_PURCHASE_DATE) or None
            # Tariff fields use a plain .get() (NOT `.get() or None` like cost_sek):
            # 0.0 is a MEANINGFUL value here (a user whose grid fee or export premium is
            # genuinely zero), so it must reach the backend, not be swallowed as "unset".
            # Do not "consistency-refactor" these to `or None`.
            grid_var_ore: float | None = self._user_data.get(CONF_GRID_VAR_ORE)
            surcharge_ore: float | None = self._user_data.get(CONF_SURCHARGE_ORE)
            export_extra_ore: float | None = self._user_data.get(CONF_EXPORT_EXTRA_ORE)

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
                    cost_sek=cost_sek,
                    purchase_date=purchase_date,
                    grid_var_ore=grid_var_ore,
                    surcharge_ore=surcharge_ore,
                    export_extra_ore=export_extra_ore,
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
                if cost_sek is not None:
                    entry_data[CONF_COST_SEK] = cost_sek
                if purchase_date is not None:
                    entry_data[CONF_PURCHASE_DATE] = purchase_date
                if grid_var_ore is not None:
                    entry_data[CONF_GRID_VAR_ORE] = grid_var_ore
                if surcharge_ore is not None:
                    entry_data[CONF_SURCHARGE_ORE] = surcharge_ore
                if export_extra_ore is not None:
                    entry_data[CONF_EXPORT_EXTRA_ORE] = export_extra_ore

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


# ---------------------------------------------------------------------------
# Options flow (v0.4.0) – edit battery specs, cost_sek and purchase_date on an
# existing entry. Only CHANGED fields are PATCHed (a plant change triggers a
# server-side regrade via the follow-up recompute); a cleared optional field is
# PATCHed as null so the backend actually clears it (v0.3.0 silently swallowed
# cleared values).
# ---------------------------------------------------------------------------

# Required fields (always present in the form, prefilled from entry.data)
_OPTIONS_PLANT_KEYS = (CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF)
# Optional fields (absent key when the user clears the prefilled value = clear)
_OPTIONS_CLEARABLE_KEYS = (
    CONF_COST_SEK,
    CONF_PURCHASE_DATE,
    CONF_GRID_VAR_ORE,
    CONF_SURCHARGE_ORE,
    CONF_EXPORT_EXTRA_ORE,
)


class WoltaOptionsFlow(OptionsFlow):
    """Handle options for an existing Wolta config entry."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialise the options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the options form."""
        entry = self._config_entry
        errors: dict[str, str] = {}

        if user_input is not None:
            token: str = entry.data[CONF_TOKEN]

            # Diff against entry.data – only changed fields go in the PATCH payload.
            patch_fields: dict[str, Any] = {}
            new_data = dict(entry.data)

            for key in _OPTIONS_PLANT_KEYS:
                val = user_input[key]
                if val != entry.data.get(key):
                    patch_fields[key] = val
                    new_data[key] = val

            for key in _OPTIONS_CLEARABLE_KEYS:
                val = user_input.get(key)
                if val is None or val == "":
                    # The form prefills current values, so an absent/empty key means
                    # the user actively cleared the field → PATCH null to clear it.
                    if entry.data.get(key) is not None:
                        patch_fields[key] = None
                        new_data.pop(key, None)
                elif val != entry.data.get(key):
                    patch_fields[key] = val
                    new_data[key] = val

            # Invert toggle (issue #1): a CLIENT-side upload transformation, not a backend field
            # → not PATCHed. On change, entry.data is updated and a refresh is triggered; the coordinator
            # self-heals (resets the bookmark → full re-backfill uploads corrected data → backend
            # recomputes on the overwritten data).
            invert_new = bool(user_input.get(CONF_INVERT_BATTERY, False))
            invert_changed = invert_new != bool(entry.data.get(CONF_INVERT_BATTERY, False))
            if invert_changed:
                new_data[CONF_INVERT_BATTERY] = invert_new

            if patch_fields:
                try:
                    session = async_get_clientsession(self.hass)
                    client = WoltaApiClient(session)
                    await client.patch_profile(token, **patch_fields)
                except WoltaApiError as err:
                    _LOGGER.error("Failed to patch Wolta profile: %s", err)
                    errors["base"] = "cannot_connect"

            if not errors:
                if patch_fields or invert_changed:
                    self.hass.config_entries.async_update_entry(entry, data=new_data)

                coordinator = getattr(entry, "runtime_data", None)
                if patch_fields and coordinator is not None:
                    # Trigger recompute so grade + economy reflect the new values.
                    # PATCH cleared the server-side cooldown, so a 202 is expected;
                    # swallow 429 gracefully anyway. (This refresh also self-heals invert.)
                    try:
                        await coordinator.async_trigger_recompute()
                    except WoltaRateLimitError:
                        _LOGGER.debug(
                            "Options flow: recompute rate-limited (cooldown); "
                            "sensors will update on the next results fetch or "
                            "the nightly rewarm."
                        )
                    except Exception:  # pylint: disable=broad-except
                        _LOGGER.debug(
                            "Options flow: recompute failed; will retry.", exc_info=True
                        )
                    else:
                        # Refresh coordinator data so sensors update immediately
                        try:
                            await coordinator.async_request_refresh()
                        except Exception:  # pylint: disable=broad-except
                            pass
                elif invert_changed and coordinator is not None:
                    # Only invert changed (no PATCH): trigger a refresh so the coordinator runs
                    # a full re-backfill with the corrected direction and uploads the overwritten data.
                    try:
                        await coordinator.async_request_refresh()
                    except Exception:  # pylint: disable=broad-except
                        _LOGGER.debug(
                            "Options flow: invert refresh failed; will retry.", exc_info=True
                        )

                return self.async_create_entry(title="", data={})

        # Pre-fill from entry.data
        current_cost = entry.data.get(CONF_COST_SEK)
        current_date = entry.data.get(CONF_PURCHASE_DATE)
        current_grid_var = entry.data.get(CONF_GRID_VAR_ORE)
        current_surcharge = entry.data.get(CONF_SURCHARGE_ORE)
        current_export_extra = entry.data.get(CONF_EXPORT_EXTRA_ORE)

        schema_dict: dict[Any, Any] = {
            vol.Required(
                CONF_BATTERY_KWH,
                default=entry.data.get(CONF_BATTERY_KWH, DEFAULT_BATTERY_KWH),
            ): _number_selector(min_val=MIN_BATTERY_KWH, max_val=500.0, step=0.5, unit="kWh"),
            vol.Required(
                CONF_BATTERY_KW,
                default=entry.data.get(CONF_BATTERY_KW, DEFAULT_BATTERY_KW),
            ): _number_selector(min_val=MIN_BATTERY_KW, max_val=100.0, step=0.1, unit="kW"),
            vol.Required(
                CONF_EFF,
                default=entry.data.get(CONF_EFF, DEFAULT_EFF),
            ): _number_selector(min_val=0.5, max_val=1.0, step=0.01),
            # suggested_value (NOT default): prefills the UI but isn't reinjected by
            # voluptuous when the field is cleared – otherwise a cleared field could never be
            # distinguished from an untouched one (v0.3.0-era bug: cleared values were silently swallowed).
            vol.Optional(
                CONF_COST_SEK,
                description={"suggested_value": current_cost} if current_cost is not None else None,
            ): _number_selector(min_val=0.0, max_val=10_000_000.0, step=100.0, unit="kr"),
            vol.Optional(
                CONF_PURCHASE_DATE,
                description={"suggested_value": current_date} if current_date is not None else None,
            ): _date_selector(),
            vol.Optional(
                CONF_GRID_VAR_ORE,
                description={"suggested_value": current_grid_var}
                if current_grid_var is not None
                else None,
            ): _number_selector(min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh"),
            vol.Optional(
                CONF_SURCHARGE_ORE,
                description={"suggested_value": current_surcharge}
                if current_surcharge is not None
                else None,
            ): _number_selector(min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh"),
            vol.Optional(
                CONF_EXPORT_EXTRA_ORE,
                description={"suggested_value": current_export_extra}
                if current_export_extra is not None
                else None,
            ): _number_selector(min_val=-200.0, max_val=500.0, step=0.1, unit="öre/ct per kWh"),
            # Invert toggle (issue #1): set if the grade is inverted (battery charge/discharge
            # reversed, e.g. signed Shelly/Emaldo) – swaps the currents on upload instead of
            # requiring the user to change their HA sensors.
            vol.Required(
                CONF_INVERT_BATTERY,
                default=bool(entry.data.get(CONF_INVERT_BATTERY, False)),
            ): BooleanSelector(BooleanSelectorConfig()),
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )
