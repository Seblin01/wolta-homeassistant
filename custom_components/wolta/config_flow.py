"""Config flow for the Wolta integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.data_entry_flow import section
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

from homeassistant.util import dt as dt_util

from . import stats
from .api import WoltaApiClient, WoltaApiError, WoltaAuthError, WoltaRateLimitError
from .const import (
    CONF_BATT_IN,
    CONF_CREATED_BY_HA,
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
    CONF_NAMEPLATE_KW,
    CONF_NAMEPLATE_KWH,
    CONF_PURCHASE_DATE,
    CONF_RESERVE_PCT,
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


def extract_token(value: str) -> str:
    """Accept a raw profile token OR a full wolta.se link with ?profile=."""
    value = value.strip()
    if "profile=" in value:
        from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

        qs = parse_qs(urlparse(value).query)
        candidates = qs.get("profile")
        if candidates:
            return candidates[0].strip()
    return value


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class WoltaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Wolta integration."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> "WoltaOptionsFlow":
        """Return the options flow for this handler."""
        return WoltaOptionsFlow()

    def __init__(self) -> None:
        """Initialise the flow."""
        self._plant_data: dict[str, Any] = {}
        self._entities_data: dict[str, Any] = {}
        self._link_token: str | None = None
        self._link_profile: dict[str, Any] | None = None
        self._prefill: dict[str, Any] = {}  # eff/purchase_date/invert från statistiken

    # ------------------------------------------------------------------
    # Step 1: menu – create a new profile or link an existing one
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point: choose create-new or link-existing."""
        return self.async_show_menu(step_id="user", menu_options=["create", "link"])

    async def async_step_create(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create-new path: start with the entity selectors."""
        return await self.async_step_entities()

    async def async_step_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Link an existing wolta.se profile (token or Besök link)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = extract_token(user_input["profile_input"])
            session = async_get_clientsession(self.hass)
            client = WoltaApiClient(session)
            try:
                self._link_profile = await client.get_profile(token)
            except WoltaAuthError:
                errors["profile_input"] = "invalid_token"
            except WoltaApiError:
                errors["base"] = "cannot_connect"
            else:
                prof = self._link_profile or {}
                if not prof.get(CONF_BATTERY_KWH) or not prof.get(CONF_BATTERY_KW):
                    # Solar-only-profil: integrationen förutsätter batteri (grade-
                    # semantiken + reauth läser battery_kwh/kw ur entry.data).
                    errors["profile_input"] = "profile_no_battery"
                    return self._show_link_form(errors)
                unique_id = hashlib.sha256(token.encode()).hexdigest()[:16]
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                # Adoptera profilen (upload → integration-kind): utan detta 404:ar
                # backendens kind-gate PUT /data, /recompute och /results för
                # webbskapade profiler → reauth skulle tyst ersätta användarens token
                # med en ny tom profil. Idempotent för redan-integrationsprofiler.
                try:
                    await client.adopt_profile(token)
                except WoltaAuthError:
                    errors["profile_input"] = "invalid_token"
                    return self._show_link_form(errors)
                except WoltaApiError as err:
                    if getattr(err, "status", None) == 422:
                        errors["profile_input"] = "profile_no_battery"
                    else:
                        errors["base"] = "cannot_connect"
                    return self._show_link_form(errors)
                self._link_token = token
                return await self.async_step_entities()
        return self._show_link_form(errors)

    def _show_link_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Visa (eller åter-visa med fel) koppla-formuläret."""
        return self.async_show_form(
            step_id="link",
            data_schema=vol.Schema(
                {
                    vol.Required("profile_input"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    ),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Plant parameters (create path) – prefilled from HA config + history
    # ------------------------------------------------------------------

    async def async_step_plant(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Plant parameters, prefilled from HA location and battery history."""
        if user_input is not None:
            self._plant_data = user_input
            return await self.async_step_privacy()

        from .zone_prefill import suggest_zone  # noqa: PLC0415

        supported = {z for z, _ in SUPPORTED_ZONES}
        guess = suggest_zone(self.hass.config.country, self.hass.config.latitude)
        zone_default = guess if guess in supported else DEFAULT_ZONE
        eff_suggested = self._prefill.get("eff")
        date_suggested = self._prefill.get("purchase_date")
        invert_default = bool(self._prefill.get("invert_suspected"))

        schema = vol.Schema(
            {
                vol.Required(CONF_ZONE, default=zone_default): _zone_selector(),
                vol.Required(CONF_BATTERY_KWH, default=DEFAULT_BATTERY_KWH): _number_selector(
                    min_val=MIN_BATTERY_KWH, max_val=500.0, step=0.5, unit="kWh"
                ),
                vol.Optional(CONF_NAMEPLATE_KWH): _number_selector(
                    min_val=MIN_BATTERY_KWH, max_val=500.0, step=0.5, unit="kWh"
                ),
                vol.Required(CONF_BATTERY_KW, default=DEFAULT_BATTERY_KW): _number_selector(
                    min_val=MIN_BATTERY_KW, max_val=100.0, step=0.1, unit="kW"
                ),
                vol.Optional(CONF_NAMEPLATE_KW): _number_selector(
                    min_val=MIN_BATTERY_KW, max_val=100.0, step=0.1, unit="kW"
                ),
                # Uppmätt AC-round-trip ur användarens egen historik när underlaget
                # räcker (stats.analyze_battery_history) – bättre än databladsgissning.
                vol.Required(CONF_EFF, default=eff_suggested or DEFAULT_EFF): _number_selector(
                    min_val=0.5, max_val=1.0, step=0.01
                ),
                vol.Optional(CONF_RESERVE_PCT): _number_selector(
                    min_val=0.0, max_val=100.0, step=1.0, unit="%"
                ),
                vol.Optional(CONF_COST_SEK): _number_selector(
                    min_val=0.0, max_val=10_000_000.0, step=100.0, unit="kr"
                ),
                vol.Optional(
                    CONF_PURCHASE_DATE,
                    description={"suggested_value": date_suggested} if date_suggested else None,
                ): _date_selector(),
                vol.Optional(CONF_GRID_VAR_ORE): _number_selector(
                    min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh"
                ),
                vol.Optional(CONF_SURCHARGE_ORE): _number_selector(
                    min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh"
                ),
                vol.Optional(CONF_EXPORT_EXTRA_ORE): _number_selector(
                    min_val=-200.0, max_val=500.0, step=0.1, unit="öre/ct per kWh"
                ),
                # Förvald när historiken visar stadigt ur > in (omkastade sensorer)
                vol.Required(CONF_INVERT_BATTERY, default=invert_default): BooleanSelector(
                    BooleanSelectorConfig()
                ),
            }
        )

        return self.async_show_form(step_id="plant", data_schema=schema)

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
                # Auto-prefill ur användarens egen historik (eff/datum/invert-detektion).
                try:
                    charged, discharged, first_ts = await stats.async_fetch_lifetime(
                        self.hass,
                        user_input[CONF_BATT_IN],
                        user_input[CONF_BATT_OUT],
                    )
                    self._prefill = stats.analyze_battery_history(
                        charged, discharged, first_ts, dt_util.utcnow()
                    )
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.debug("Prefill analysis failed; using defaults", exc_info=True)
                    self._prefill = {}
                if self._link_token is not None:
                    if self._prefill.get("invert_suspected"):
                        return await self.async_step_invert_check()
                    return self._create_linked_entry(invert=False)
                return await self.async_step_plant()

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
            zone = self._plant_data[CONF_ZONE]
            solar = self._entities_data.get(CONF_SOLAR)
            cost_sek: float | None = self._plant_data.get(CONF_COST_SEK) or None
            purchase_date: str | None = self._plant_data.get(CONF_PURCHASE_DATE) or None
            # Tariff fields (and reserve_pct) use a plain .get() (NOT `.get() or None`
            # like cost_sek): 0.0 is a MEANINGFUL value here (a user whose grid fee or
            # export premium is genuinely zero, or whose control system keeps zero
            # reserve floor), so it must reach the backend, not be swallowed as "unset".
            # Do not "consistency-refactor" these to `or None`.
            grid_var_ore: float | None = self._plant_data.get(CONF_GRID_VAR_ORE)
            surcharge_ore: float | None = self._plant_data.get(CONF_SURCHARGE_ORE)
            export_extra_ore: float | None = self._plant_data.get(CONF_EXPORT_EXTRA_ORE)
            reserve_pct: float | None = self._plant_data.get(CONF_RESERVE_PCT)
            nameplate_kwh: float | None = self._plant_data.get(CONF_NAMEPLATE_KWH)
            nameplate_kw: float | None = self._plant_data.get(CONF_NAMEPLATE_KW)

            try:
                session = async_get_clientsession(self.hass)
                client = WoltaApiClient(session)
                token = await client.create_profile(
                    zone=zone,
                    battery_kwh=self._plant_data[CONF_BATTERY_KWH],
                    battery_kw=self._plant_data[CONF_BATTERY_KW],
                    eff=self._plant_data[CONF_EFF],
                    has_solar=bool(solar),
                    share_profile=share,
                    cost_sek=cost_sek,
                    purchase_date=purchase_date,
                    grid_var_ore=grid_var_ore,
                    surcharge_ore=surcharge_ore,
                    export_extra_ore=export_extra_ore,
                    reserve_pct=reserve_pct,
                    nameplate_kwh=nameplate_kwh,
                    nameplate_kw=nameplate_kw,
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
                    CONF_BATTERY_KWH: self._plant_data[CONF_BATTERY_KWH],
                    CONF_BATTERY_KW: self._plant_data[CONF_BATTERY_KW],
                    CONF_EFF: self._plant_data[CONF_EFF],
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
                if reserve_pct is not None:
                    entry_data[CONF_RESERVE_PCT] = reserve_pct
                if nameplate_kwh is not None:
                    entry_data[CONF_NAMEPLATE_KWH] = nameplate_kwh
                if nameplate_kw is not None:
                    entry_data[CONF_NAMEPLATE_KW] = nameplate_kw
                entry_data[CONF_CREATED_BY_HA] = True
                entry_data[CONF_INVERT_BATTERY] = bool(
                    self._plant_data.get(CONF_INVERT_BATTERY, False)
                )

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
    # Reconfigure: change entity selections without delete + re-add
    # (removal would delete an HA-created profile server-side)
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the energy entity selections on an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            for key in (CONF_BATT_IN, CONF_BATT_OUT, CONF_GRID_IN, CONF_GRID_OUT):
                if not user_input.get(key):
                    errors[key] = "required_sensor"
            if not errors:
                await self.async_set_unique_id(entry.unique_id)
                self._abort_if_unique_id_mismatch()
                data_updates: dict[str, Any] = {
                    CONF_BATT_IN: user_input[CONF_BATT_IN],
                    CONF_BATT_OUT: user_input[CONF_BATT_OUT],
                    CONF_GRID_IN: user_input[CONF_GRID_IN],
                    CONF_GRID_OUT: user_input[CONF_GRID_OUT],
                    CONF_SOLAR: user_input.get(CONF_SOLAR) or [],
                }
                # Reload → coordinatorn ser nytt entity-fingerprint → bookmark-reset →
                # full re-backfill skriver över historiken från de nya sensorerna.
                return self.async_update_reload_and_abort(entry, data_updates=data_updates)

        defaults = {
            k: entry.data.get(k)
            for k in (CONF_BATT_IN, CONF_BATT_OUT, CONF_GRID_IN, CONF_GRID_OUT, CONF_SOLAR)
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_BATT_IN, default=defaults[CONF_BATT_IN]): _energy_entity_selector(),
                vol.Required(CONF_BATT_OUT, default=defaults[CONF_BATT_OUT]): _energy_entity_selector(),
                vol.Required(CONF_GRID_IN, default=defaults[CONF_GRID_IN]): _energy_entity_selector(),
                vol.Required(CONF_GRID_OUT, default=defaults[CONF_GRID_OUT]): _energy_entity_selector(),
                vol.Optional(
                    CONF_SOLAR, default=defaults[CONF_SOLAR] or vol.UNDEFINED
                ): _energy_entity_selector(),
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    # ------------------------------------------------------------------
    # Linked-profile entry creation (+ invert check when stats suggest swap)
    # ------------------------------------------------------------------

    def _create_linked_entry(self, *, invert: bool) -> ConfigFlowResult:
        """Create the entry for a linked (web-created) profile.

        Profile fields are cached from the GET snapshot; the coordinator keeps
        them in sync from here on. created_by_ha=False → removal never deletes
        the profile server-side.
        """
        prof = self._link_profile or {}
        entry_data: dict[str, Any] = {
            CONF_TOKEN: self._link_token,
            CONF_CREATED_BY_HA: False,
            CONF_INVERT_BATTERY: invert,
            CONF_BATT_IN: self._entities_data[CONF_BATT_IN],
            CONF_BATT_OUT: self._entities_data[CONF_BATT_OUT],
            CONF_GRID_IN: self._entities_data[CONF_GRID_IN],
            CONF_GRID_OUT: self._entities_data[CONF_GRID_OUT],
        }
        if self._entities_data.get(CONF_SOLAR):
            entry_data[CONF_SOLAR] = self._entities_data[CONF_SOLAR]
        for key in (
            CONF_ZONE, CONF_BATTERY_KWH, CONF_NAMEPLATE_KWH, CONF_BATTERY_KW,
            CONF_NAMEPLATE_KW, CONF_EFF,
            CONF_RESERVE_PCT, CONF_COST_SEK, CONF_PURCHASE_DATE,
            CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE, CONF_EXPORT_EXTRA_ORE,
        ):
            if prof.get(key) is not None:
                entry_data[key] = prof[key]
        # Zone måste alltid finnas – WoltaCoordinator.__init__ läser entry.data[zone]
        # ovillkorligt och skulle annars KeyError:a vid setup (defensivt; servern
        # returnerar normalt alltid zone).
        entry_data.setdefault(CONF_ZONE, DEFAULT_ZONE)
        zone = entry_data[CONF_ZONE]
        return self.async_create_entry(title=f"Wolta ({zone})", data=entry_data)

    async def async_step_invert_check(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Shown only when linked-profile stats suggest swapped battery sensors."""
        if user_input is not None:
            return self._create_linked_entry(
                invert=bool(user_input.get(CONF_INVERT_BATTERY, True))
            )
        return self.async_show_form(
            step_id="invert_check",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INVERT_BATTERY, default=True): BooleanSelector(
                        BooleanSelectorConfig()
                    ),
                }
            ),
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
# Options flow – edit the SHARED Wolta profile. The server is the source of
# truth: the form is prefilled from a fresh GET /profile and the diff is
# computed against that snapshot, never against entry.data (which is only a
# cache) – otherwise a web-side change could be silently clobbered with stale
# values. Fields are grouped in collapsible sections (battery/economy/tariffs);
# sections nest user_input one level.
# ---------------------------------------------------------------------------

_SEC_BATTERY = "battery"
_SEC_ECONOMY = "economy"
_SEC_TARIFFS = "tariffs"
_SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    _SEC_BATTERY: (
        CONF_BATTERY_KWH, CONF_NAMEPLATE_KWH, CONF_BATTERY_KW, CONF_NAMEPLATE_KW,
        CONF_EFF, CONF_RESERVE_PCT,
    ),
    _SEC_ECONOMY: (CONF_COST_SEK, CONF_PURCHASE_DATE),
    _SEC_TARIFFS: (CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE, CONF_EXPORT_EXTRA_ORE),
}
# Required fields (always present in the form, prefilled from the server snapshot)
_REQUIRED_FIELDS = (CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF)


class WoltaOptionsFlow(OptionsFlow):
    """Handle options for an existing Wolta config entry (shared-profile edit)."""

    def __init__(self) -> None:
        """Initialise the options flow."""
        self._server: dict[str, Any] | None = None  # fresh GET /profile snapshot

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the options form."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        token: str = entry.data[CONF_TOKEN]
        session = async_get_clientsession(self.hass)
        client = WoltaApiClient(session)

        if self._server is None:
            try:
                self._server = await client.get_profile(token)
            except WoltaAuthError:
                # Purgad/okänd profil: cannot_connect vore vilseledande – starta
                # reauth-flödet direkt istället för att vänta på nästa poll.
                entry.async_start_reauth(self.hass)
                return self.async_abort(reason="reauth_required")
            except WoltaApiError:
                # A PATCH would fail too – profile editing needs the server.
                return self.async_abort(reason="cannot_connect")

        if user_input is not None:
            # Un-nest the section structure to flat {field: value}.
            flat: dict[str, Any] = {}
            for sec, fields in _SECTION_FIELDS.items():
                sec_input = user_input.get(sec) or {}
                for key in fields:
                    if key in sec_input:
                        flat[key] = sec_input[key]

            # cost_scope (backend 2026-07-18): "plant" = the scalar price covers the
            # WHOLE plant (solar + battery; wolta.se guide profiles adopted into HA).
            # Our field is explicitly battery-only, so it is hidden from the form for
            # those profiles (the price is edited on wolta.se where the labels match) –
            # and must be skipped in the diff too: an absent optional field otherwise
            # means "actively cleared" → PATCH null would wipe the plant price on
            # every save. Older backends never send cost_scope → False → unchanged.
            plant_scoped_cost = self._server.get("cost_scope") == "plant"

            # Diff against the SERVER snapshot – only changed fields are PATCHed.
            patch_fields: dict[str, Any] = {}
            for sec, fields in _SECTION_FIELDS.items():
                for key in fields:
                    if key == CONF_COST_SEK and plant_scoped_cost:
                        continue
                    val = flat.get(key)
                    server_val = self._server.get(key)
                    if key in _REQUIRED_FIELDS:
                        if val != server_val:
                            patch_fields[key] = val
                    elif val is None or val == "":
                        # The form prefills current values, so an absent/empty key means
                        # the user actively cleared the field → PATCH null to clear it.
                        if server_val is not None:
                            patch_fields[key] = None
                    elif val != server_val:
                        patch_fields[key] = val

            # Invert toggle (issue #1): a CLIENT-side upload transformation, not a backend
            # field → not PATCHed. On change the coordinator self-heals (bookmark reset →
            # full re-backfill uploads corrected data).
            invert_new = bool(user_input.get(CONF_INVERT_BATTERY, False))
            invert_changed = invert_new != bool(
                entry.data.get(CONF_INVERT_BATTERY, False)
            )

            if patch_fields:
                try:
                    await client.patch_profile(token, **patch_fields)
                except WoltaApiError as err:
                    _LOGGER.error("Failed to patch Wolta profile: %s", err)
                    errors["base"] = "cannot_connect"

            if not errors:
                if patch_fields or invert_changed:
                    new_data = dict(entry.data)
                    for key, val in patch_fields.items():
                        if val is None:
                            new_data.pop(key, None)
                        else:
                            new_data[key] = val
                    if invert_changed:
                        new_data[CONF_INVERT_BATTERY] = invert_new
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
                    # Only invert changed (no PATCH): trigger a refresh so the coordinator
                    # runs a full re-backfill with the corrected direction.
                    try:
                        await coordinator.async_request_refresh()
                    except Exception:  # pylint: disable=broad-except
                        _LOGGER.debug(
                            "Options flow: invert refresh failed; will retry.", exc_info=True
                        )

                return self.async_create_entry(title="", data={})

        srv = self._server

        def _opt(key: str, selector: Any) -> tuple[Any, Any]:
            """vol.Optional med suggested_value (INTE default) för rensningsbara fält –
            en default reinjiceras av voluptuous när fältet töms och rensningen skulle
            aldrig gå att skilja från orört (v0.3.0-buggen)."""
            cur = srv.get(key)
            marker = vol.Optional(
                key,
                description={"suggested_value": cur} if cur is not None else None,
            )
            return marker, selector

        battery_schema = vol.Schema(dict([
            (vol.Required(CONF_BATTERY_KWH, default=srv.get(CONF_BATTERY_KWH, DEFAULT_BATTERY_KWH)),
             _number_selector(min_val=MIN_BATTERY_KWH, max_val=500.0, step=0.5, unit="kWh")),
            _opt(CONF_NAMEPLATE_KWH, _number_selector(min_val=MIN_BATTERY_KWH, max_val=500.0, step=0.5, unit="kWh")),
            (vol.Required(CONF_BATTERY_KW, default=srv.get(CONF_BATTERY_KW, DEFAULT_BATTERY_KW)),
             _number_selector(min_val=MIN_BATTERY_KW, max_val=100.0, step=0.1, unit="kW")),
            _opt(CONF_NAMEPLATE_KW, _number_selector(min_val=MIN_BATTERY_KW, max_val=100.0, step=0.1, unit="kW")),
            (vol.Required(CONF_EFF, default=srv.get(CONF_EFF, DEFAULT_EFF)),
             _number_selector(min_val=0.5, max_val=1.0, step=0.01)),
            _opt(CONF_RESERVE_PCT, _number_selector(min_val=0.0, max_val=100.0, step=1.0, unit="%")),
        ]))
        # Se plant_scoped_cost-kommentaren i diff-grenen ovan: fältet döljs helt för
        # plant-scopade profiler (redigeras på wolta.se där etiketterna stämmer).
        economy_schema = vol.Schema(dict(
            ([] if srv.get("cost_scope") == "plant"
             else [_opt(CONF_COST_SEK, _number_selector(min_val=0.0, max_val=10_000_000.0, step=100.0, unit="kr"))])
            + [_opt(CONF_PURCHASE_DATE, _date_selector())]
        ))
        tariffs_schema = vol.Schema(dict([
            _opt(CONF_GRID_VAR_ORE, _number_selector(min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh")),
            _opt(CONF_SURCHARGE_ORE, _number_selector(min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh")),
            _opt(CONF_EXPORT_EXTRA_ORE, _number_selector(min_val=-200.0, max_val=500.0, step=0.1, unit="öre/ct per kWh")),
        ]))

        schema = vol.Schema({
            vol.Required(_SEC_BATTERY): section(battery_schema, {"collapsed": False}),
            vol.Required(_SEC_ECONOMY): section(economy_schema, {"collapsed": False}),
            vol.Required(_SEC_TARIFFS): section(tariffs_schema, {"collapsed": True}),
            vol.Required(
                CONF_INVERT_BATTERY,
                default=bool(entry.data.get(CONF_INVERT_BATTERY, False)),
            ): BooleanSelector(BooleanSelectorConfig()),
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
