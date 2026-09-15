"""Config flow for the Wolta integration."""

from __future__ import annotations

import hashlib
import logging
import secrets
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
from .control_system_prefill import battery_platforms, suggest_control_system
from .const import (
    BATTERY_STATUS_NEEDS_INPUT,
    BATTERY_STATUS_NONE,
    BATTERY_STATUS_PENDING,
    CONF_BATT_IN,
    CONF_CREATED_BY_HA,
    CONF_BATT_OUT,
    CONF_BATTERY_KW,
    CONF_BATTERY_KWH,
    CONF_CONTROL_SYSTEM,
    CONF_CONTROL_SYSTEM_NAME,
    CONF_COST_SEK,
    CONF_EFF,
    CONF_EXPORT_EXTRA_ORE,
    CONF_EXPORT_EXTRA_PCT,
    CONF_EXTERNAL_CONTROL,
    CONF_FLEX_COMPENSATION,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_GRID_VAR_ORE,
    CONF_GRID_VAR_PCT,
    CONF_INVERT_BATTERY,
    CONF_NAMEPLATE_KW,
    CONF_NAMEPLATE_KWH,
    CONF_PLANT_ID,
    CONF_PREFILL_PURCHASE_DATE,
    CONF_PURCHASE_DATE,
    CONF_RESERVE_PCT,
    CONF_SHARE,
    CONF_SOLAR,
    CONF_SURCHARGE_ORE,
    CONF_TOKEN,
    CONF_VIEW_ONLY,
    CONF_ZONE,
    CONTROL_SYSTEMS,
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


_ZONE_SUGGESTED_LABEL: dict[str, str] = {
    "sv": "föreslaget ur din position",
    "en": "suggested from your location",
}


def _zone_selector(suggested: str | None = None, lang: str = "en") -> SelectSelector:
    """Zonlistan. Med `suggested` ligger den zonen FÖRST med en etikett – men utan default
    (V14: SelectSelector väljer inget tyst; ordningen bevaras eftersom sort defaultar
    False, dvs. sorteringen är stabil och bara flyttar den föreslagna raden upp)."""
    lang = lang if lang in _ZONE_SUGGESTED_LABEL else "en"
    rows = list(SUPPORTED_ZONES)
    if suggested in dict(rows):
        rows.sort(key=lambda r: r[0] != suggested)
    options = [
        SelectOptionDict(
            value=zone_id,
            label=(f"{label} – {_ZONE_SUGGESTED_LABEL[lang]}"
                   if zone_id == suggested else label),
        )
        for zone_id, label in rows
    ]
    return SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )


def _control_system_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=cs_id, label=label)
                for cs_id, label in CONTROL_SYSTEMS
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


def _is_read_link(value: str) -> bool:
    """Besök-länken är sedan v0.30.0 en LÄSlänk (?link=wpl_…), inte ägar-tokenet.
    Riktat felmeddelande i BÅDA stegen som tar token-inmatning – användaren har
    klistrat in rätt sorts länk för fel syfte, och ett generiskt 'ogiltig token'
    hade lämnat henne utan väg framåt."""
    v = value.strip()
    return "link=wpl_" in v or v.startswith("wpl_")


# ---------------------------------------------------------------------------
# Zone hint (plant step) – location-guess text shown next to the (now defaultless,
# see CONF_ZONE in async_step_plant) zone field.
#
# This prose lives in Python, not strings.json, because the two cases are
# STRUCTURALLY different sentences (guess vs no-guess), not one sentence with a
# value slot – a translation-string placeholder can only ever carry a single,
# already-fixed language's prose, so it cannot hold "the whole sentence, in
# whichever language the user runs HA in". SUPPORTED_ZONES/CONTROL_SYSTEMS don't
# set a precedent here: those are identifier labels ("SE3 – Stockholm" reads the
# same regardless of UI language), not sentences. Keyed on hass.config.language's
# two-letter prefix; anything besides sv/en falls back to English. That is the
# tradeoff being made – a small un-DRY table instead of a runtime
# translation-string lookup – and it should not need rediscovering later.
# ---------------------------------------------------------------------------

_ZONE_HINT_GUESS: dict[str, str] = {
    "sv": "Utifrån din Home Assistant-plats verkar {zone} stämma – men du måste "
          "välja zonen själv.",
    "en": "Based on your Home Assistant location, {zone} looks likely – but you "
          "must still choose the zone yourself.",
}
_ZONE_HINT_NO_GUESS: dict[str, str] = {
    "sv": "Home Assistant kunde inte föreslå en zon utifrån din inställda plats "
          "– kolla din elräkning eller ditt elnätsbolag för att hitta rätt zon.",
    "en": "Home Assistant could not suggest a zone from your configured location "
          "– check your electricity bill or grid operator to find yours.",
}


def _zone_hint(hass_language: str, guess: str | None, supported: set[str]) -> str:
    """Build the plant step's zone_hint text in the user's HA language (sv/en,
    else English)."""
    lang = hass_language.split("-")[0]
    lang = lang if lang in _ZONE_HINT_GUESS else "en"
    if guess in supported:
        guess_label = dict(SUPPORTED_ZONES)[guess]
        return _ZONE_HINT_GUESS[lang].format(zone=guess_label)
    return _ZONE_HINT_NO_GUESS[lang]


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
        # Styrsystem-förslaget ur batterisensorernas integrationsdomän (sätts i
        # entitetssteget). None = inget förslag → plant-fältet får ingen default.
        self._cs_suggest: str | None = None
        # Stable plant identity for this entry, minted once here and persisted in entry.data.
        # Minted at flow start (not at entry creation) because BOTH the create path and the
        # link path need it while the flow is still running. See const.CONF_PLANT_ID for why
        # this is not the config entry_id.
        self._plant_id: str = secrets.token_hex(16)

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
            if _is_read_link(user_input["profile_input"]):
                errors["profile_input"] = "link_is_read_link"
                return self._show_link_form(errors)
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
                # Spec 2026-09-14 §7.2: en VÄNTANDE rad har inget kWh/kW-par än (backend
                # mäter det) men har ett batteri, så par-regeln nedan hade avvisat den.
                # `battery_status` är auktoriteten när servern skickar den; en äldre
                # server utan fältet faller tillbaka på den gamla par-regeln.
                status = prof.get("battery_status")
                no_battery = (
                    status == BATTERY_STATUS_NONE if status is not None
                    else not (prof.get(CONF_BATTERY_KWH) and prof.get(CONF_BATTERY_KW))
                )
                if no_battery:
                    # Solar-only-profil: integrationen förutsätter batteri (grade-
                    # semantiken + reauth läser battery_kwh/kw ur entry.data).
                    errors["profile_input"] = "profile_no_battery"
                    return self._show_link_form(errors)
                # Anläggning som redan strömmas via en bindning (Sonnen-webhook/Reduxi):
                # erbjud VISNINGSLÄGE i stället för entitetssteget (v0.18.0; v0.17.0
                # stoppade hårt här). Ingen adopt - backend 409:ar den för bundna rader,
                # och ingen HA-identitet ska stämplas på en anläggning vi inte strömmar.
                # Fullföljd strömmande länkning hade gett två skrivare mot samma timrader
                # (sist vinner per timme, olika mätare) → betyget räknas på en blandning.
                # 'ha'/saknat fält (äldre server) tar strömningsvägen som vanligt -
                # om-länkning av egen HA-profil är normalfallet.
                transport = (prof.get("derived") or {}).get("transport")
                if transport in ("sonnen_webhook", "reduxi_mqtt"):
                    unique_id = hashlib.sha256(token.encode()).hexdigest()[:16]
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()
                    self._link_token = token
                    return await self.async_step_view_only()
                unique_id = hashlib.sha256(token.encode()).hexdigest()[:16]
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                # Adoptera profilen (upload → integration-kind): utan detta 404:ar
                # backendens kind-gate PUT /data, /recompute och /results för
                # webbskapade profiler → reauth skulle tyst ersätta användarens token
                # med en ny tom profil. Idempotent för redan-integrationsprofiler.
                try:
                    await client.adopt_profile(token, client_plant_id=self._plant_id)
                except WoltaAuthError:
                    errors["profile_input"] = "invalid_token"
                    return self._show_link_form(errors)
                except WoltaApiError as err:
                    if getattr(err, "status", None) == 422:
                        errors["profile_input"] = "profile_no_battery"
                    elif getattr(err, "status", None) == 409:
                        # Identiteten (client_plant_id) är redan knuten till en ANNAN rad.
                        # Onåbart i praktiken (id:t mintas färskt, 128 bit) men får inte
                        # maskeras som "cannot connect" - anslutningen fungerade ju.
                        errors["profile_input"] = "identity_conflict"
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
    # Plant step (create path) – the LAST step: it also creates the profile
    # ------------------------------------------------------------------

    async def async_step_plant(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Elområde + styrsystem + delning + sensorriktning (spec 2026-09-14 §7.1).

        Kapacitet, effekt och verkningsgrad frågas inte: backend mäter dem ur
        uppladdningen (battery_declared). Skapar profilen och entryn direkt – inget
        privacy-steg (delningsvalet ligger sist i det här formuläret i stället).
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            # "other" without a free-text name gives zero segmentation signal -
            # same rule as the web guide (lib/guide.ts canAdvance).
            if (user_input.get(CONF_CONTROL_SYSTEM) == "other"
                    and not (user_input.get(CONF_CONTROL_SYSTEM_NAME) or "").strip()):
                errors[CONF_CONTROL_SYSTEM_NAME] = "control_system_name_required"
            if not errors:
                self._plant_data = user_input
                return await self._create_profile_and_entry()

        return self._show_plant_form(errors, user_input)

    def _show_plant_form(
        self, errors: dict[str, str], user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Visa (eller åter-visa med fel) plant-formuläret.

        One place builds the schema AND the zone hint, because the form renders from
        three directions - first show, a validation error, and a failed POST in
        _create_profile_and_entry - and a divergence between them would show the user
        different fields depending on which way they arrived.
        """
        from .zone_prefill import suggest_zone  # noqa: PLC0415

        supported = {z for z, _ in SUPPORTED_ZONES}
        guess = suggest_zone(self.hass.config.country, self.hass.config.latitude)
        guess = guess if guess in supported else None
        lang = (self.hass.config.language or "en").split("-")[0]
        # See _zone_hint()/_ZONE_HINT_GUESS above for why this text is localized in
        # Python rather than in strings.json.
        zone_hint = _zone_hint(self.hass.config.language, guess, supported)
        invert_default = bool(self._prefill.get("invert_suspected"))

        # The control-system SUGGESTION may be a default (unlike the zone): it is
        # corrigible in options afterwards, the domain -> system mapping comes from the
        # backend rather than from a guess, and a plant whose battery integration we
        # recognise is not an inattentive user being parked in a bucket. No suggestion
        # (unknown domain, a tie, a failed fetch) -> no default, as before.
        cs_field = (vol.Required(CONF_CONTROL_SYSTEM, default=self._cs_suggest)
                    if self._cs_suggest else vol.Required(CONF_CONTROL_SYSTEM))
        schema = vol.Schema(
            {
                # Deliberately NO default: the zone is an ACTIVE choice (directive
                # 2026-08-24) - it selects the price series the grade AND the economics
                # are computed against, and it is immutable server-side after creation.
                # A pre-selected dropdown is acceptance-by-inaction, so the location
                # guess is surfaced by moving that zone to the TOP of the list with a
                # label (and repeated as text in the description) instead.
                vol.Required(CONF_ZONE): _zone_selector(guess, lang),
                cs_field: _control_system_selector(),
                vol.Optional(CONF_CONTROL_SYSTEM_NAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Required(CONF_SHARE, default=DEFAULT_SHARE): BooleanSelector(
                    BooleanSelectorConfig()
                ),
                # Visas ALLTID (beslut 2026-09-14), förbockad vid misstanke ur
                # historiken (stadigt ur > in = omkastade batterisensorer).
                vol.Required(CONF_INVERT_BATTERY, default=invert_default): BooleanSelector(
                    BooleanSelectorConfig()
                ),
            }
        )

        # Re-show after an error keeps what the user chose (suggested values), instead
        # of wiping the form back to prefill defaults.
        if errors and user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="plant",
            data_schema=schema,
            errors=errors,
            description_placeholders={"zone_hint": zone_hint},
        )

    async def _create_profile_and_entry(self) -> ConfigFlowResult:
        """POST /profile med vad de två stegen samlat in, och skapa config-entryn.

        Batteriet DEKLARERAS (battery_declared): kapacitet/effekt/verkningsgrad
        utelämnas ur både payloaden och entry.data, eftersom backend mäter dem ur
        uppladdningen. Ett cachat gissningsvärde här hade återsänts av reauth och
        skrivit över mätningen.
        """
        errors: dict[str, str] = {}
        zone = self._plant_data[CONF_ZONE]
        share = self._plant_data.get(CONF_SHARE, DEFAULT_SHARE)
        solar = self._entities_data.get(CONF_SOLAR)
        # Client-local upload transformation (same nature as invert_battery below):
        # empty string (cleared selector) normalises to absence, never PATCHed to
        # the server.
        external_control = self._entities_data.get(CONF_EXTERNAL_CONTROL) or None
        # The ENTITY ID is client-local config and is never sent to the server -
        # only the monthly amounts it yields are, via the coordinator's PATCH.
        flex_compensation = self._entities_data.get(CONF_FLEX_COMPENSATION) or None
        control_system: str | None = self._plant_data.get(CONF_CONTROL_SYSTEM)
        # The name only means something together with "other" (the web sends the
        # same pair); a stray name next to a brand value would be orphaned.
        control_system_name: str | None = (
            (self._plant_data.get(CONF_CONTROL_SYSTEM_NAME) or "").strip() or None
            if control_system == "other" else None
        )

        try:
            session = async_get_clientsession(self.hass)
            client = WoltaApiClient(session)
            token = await client.create_profile(
                zone=zone,
                has_solar=bool(solar),
                share_profile=share,
                battery_declared=True,
                client_plant_id=self._plant_id,
                control_system=control_system,
                control_system_name=control_system_name,
            )
        except WoltaApiError as err:
            _LOGGER.error("Failed to create Wolta profile: %s", err)
            if getattr(err, "status", None) == 422:
                errors["base"] = "invalid_input"
            else:
                errors["base"] = "cannot_connect"
            return self._show_plant_form(errors, self._plant_data)

        unique_id = hashlib.sha256(token.encode()).hexdigest()[:16]
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        entry_data: dict[str, Any] = {
            CONF_TOKEN: token,
            CONF_PLANT_ID: self._plant_id,
            CONF_ZONE: zone,
            CONF_BATT_IN: self._entities_data[CONF_BATT_IN],
            CONF_BATT_OUT: self._entities_data[CONF_BATT_OUT],
            CONF_GRID_IN: self._entities_data[CONF_GRID_IN],
            CONF_GRID_OUT: self._entities_data[CONF_GRID_OUT],
            CONF_SHARE: share,
            CONF_CREATED_BY_HA: True,
            CONF_INVERT_BATTERY: bool(
                self._plant_data.get(CONF_INVERT_BATTERY, False)
            ),
        }
        if solar:
            entry_data[CONF_SOLAR] = solar
        if control_system is not None:
            entry_data[CONF_CONTROL_SYSTEM] = control_system
        if control_system_name is not None:
            entry_data[CONF_CONTROL_SYSTEM_NAME] = control_system_name
        if external_control:
            entry_data[CONF_EXTERNAL_CONTROL] = external_control
        if flex_compensation:
            entry_data[CONF_FLEX_COMPENSATION] = flex_compensation
        # Datumförslaget ur statistiken skickas INTE till servern (första datapunkten
        # är inte nödvändigtvis ett inköpsdatum, och ett tyst ifyllt datum styr
        # återbetalningskalkylen). Det lagras här och erbjuds som suggested_value för
        # purchase_date i options → Economy, där användaren bekräftar det.
        if self._prefill.get("purchase_date"):
            entry_data[CONF_PREFILL_PURCHASE_DATE] = self._prefill["purchase_date"]

        return self.async_create_entry(title=f"Wolta ({zone})", data=entry_data)

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
                # Spec 2026-09-14 §7.1: styrsystem-förslag ur batterisensorernas
                # integrationsdomän, uppslaget mot backendens mappning. Nätfel ⇒ inget
                # förslag (fältet utan default, som i dag) – ett halvt uppslag får
                # aldrig stoppa onboardingen. Bara skapa-spåret frågar efter styrsystem,
                # så koppla-spåret slipper anropet.
                self._cs_suggest = None
                if self._link_token is None:
                    try:
                        platforms = battery_platforms(
                            self.hass,
                            list(user_input[CONF_BATT_IN]) + list(user_input[CONF_BATT_OUT]),
                        )
                        mapping = await WoltaApiClient(
                            async_get_clientsession(self.hass)
                        ).get_control_systems()
                        suggestion = suggest_control_system(platforms, mapping)
                        # Backendens lista ligger FÖRE den lokala (se const.CONTROL_SYSTEMS):
                        # ett id vi inte känner igen som default hade förvalt ett värde som
                        # SelectSelector själv avvisar → MultipleInvalid i sista
                        # onboarding-steget, utan väg framåt. Okänt ⇒ inget förslag.
                        self._cs_suggest = (
                            suggestion if suggestion in {c for c, _ in CONTROL_SYSTEMS}
                            else None
                        )
                    except Exception:  # pylint: disable=broad-except
                        _LOGGER.debug(
                            "control_system prefill failed; no suggestion", exc_info=True
                        )
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
                # No `default=`, unlike the streams above - same reason as in the
                # reconfigure step: this is a single-value EntitySelector (not
                # `multiple`), so it has no valid "empty" value, and a `default=`
                # makes it un-clearable. After a validation error this form re-renders
                # with `defaults = user_input`; the user clears the picker, the
                # frontend OMITS the key, and voluptuous puts the stale entity right
                # back. `suggested_value` only pre-fills the form for display.
                vol.Optional(
                    CONF_EXTERNAL_CONTROL,
                    description=(
                        {"suggested_value": defaults[CONF_EXTERNAL_CONTROL]}
                        if defaults.get(CONF_EXTERNAL_CONTROL) else None
                    ),
                ): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
                # Same single-value picker mechanics (and the same v0.3.0 trap) as
                # the external-control field above: `suggested_value`, never
                # `default=`. Not domain-filtered on device_class - a compensation
                # figure is just as often a template sensor without one, and the
                # only hard requirement (long-term statistics) is not expressible
                # in the selector.
                vol.Optional(
                    CONF_FLEX_COMPENSATION,
                    description=(
                        {"suggested_value": defaults[CONF_FLEX_COMPENSATION]}
                        if defaults.get(CONF_FLEX_COMPENSATION) else None
                    ),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            }
        )

        return self.async_show_form(
            step_id="entities",
            data_schema=schema,
            errors=errors,
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
        # Visningsläge har inga sensorval: formulärets stream-fält är Required med tomma
        # defaults (återvändsgränd), och ifyllda sensorer hade lagrats men IGNORERATS av
        # coordinatorns view-only-gren - det ser ut som strömning utan att något strömmar.
        # Vill man börja strömma: ta bort entryn och länka om profilen.
        if entry.data.get(CONF_VIEW_ONLY):
            return self.async_abort(reason="view_only_no_reconfigure")
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
                    CONF_EXTERNAL_CONTROL: user_input.get(CONF_EXTERNAL_CONTROL) or None,
                    CONF_FLEX_COMPENSATION: user_input.get(CONF_FLEX_COMPENSATION) or None,
                }
                # Reload → coordinatorn ser nytt entity-fingerprint → bookmark-reset →
                # full re-backfill skriver över historiken från de nya sensorerna.
                return self.async_update_reload_and_abort(entry, data_updates=data_updates)

        defaults = {
            k: entry.data.get(k)
            for k in (
                CONF_BATT_IN, CONF_BATT_OUT, CONF_GRID_IN, CONF_GRID_OUT, CONF_SOLAR,
                CONF_EXTERNAL_CONTROL, CONF_FLEX_COMPENSATION,
            )
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
                # No `default=`, unlike the streams above: this is a single-value
                # EntitySelector (not `multiple`), which has no valid "empty" value to
                # fall back to - a `default=` pointing at the stored sensor would make
                # the field un-clearable (voluptuous re-fills it whenever the frontend
                # omits the key, which is exactly what clearing an optional field does).
                # `suggested_value` only pre-fills the form for display, same pattern
                # as CONF_PURCHASE_DATE in the plant step above.
                vol.Optional(
                    CONF_EXTERNAL_CONTROL,
                    description=(
                        {"suggested_value": defaults[CONF_EXTERNAL_CONTROL]}
                        if defaults[CONF_EXTERNAL_CONTROL] else None
                    ),
                ): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
                # `suggested_value` for the same reason as the field above: clearing
                # this picker must actually stop the monthly PATCHes.
                vol.Optional(
                    CONF_FLEX_COMPENSATION,
                    description=(
                        {"suggested_value": defaults[CONF_FLEX_COMPENSATION]}
                        if defaults[CONF_FLEX_COMPENSATION] else None
                    ),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
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
            CONF_PLANT_ID: self._plant_id,
            CONF_CREATED_BY_HA: False,
            CONF_INVERT_BATTERY: invert,
            CONF_BATT_IN: self._entities_data[CONF_BATT_IN],
            CONF_BATT_OUT: self._entities_data[CONF_BATT_OUT],
            CONF_GRID_IN: self._entities_data[CONF_GRID_IN],
            CONF_GRID_OUT: self._entities_data[CONF_GRID_OUT],
        }
        if self._entities_data.get(CONF_SOLAR):
            entry_data[CONF_SOLAR] = self._entities_data[CONF_SOLAR]
        if self._entities_data.get(CONF_EXTERNAL_CONTROL):
            entry_data[CONF_EXTERNAL_CONTROL] = self._entities_data[CONF_EXTERNAL_CONTROL]
        if self._entities_data.get(CONF_FLEX_COMPENSATION):
            entry_data[CONF_FLEX_COMPENSATION] = self._entities_data[CONF_FLEX_COMPENSATION]
        for key in (
            CONF_ZONE, CONF_BATTERY_KWH, CONF_NAMEPLATE_KWH, CONF_BATTERY_KW,
            CONF_NAMEPLATE_KW, CONF_EFF,
            CONF_RESERVE_PCT, CONF_COST_SEK, CONF_PURCHASE_DATE,
            CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE, CONF_EXPORT_EXTRA_ORE,
            CONF_GRID_VAR_PCT, CONF_EXPORT_EXTRA_PCT,
        ):
            if prof.get(key) is not None:
                entry_data[key] = prof[key]
        # Zone måste alltid finnas – WoltaCoordinator.__init__ läser entry.data[zone]
        # ovillkorligt och skulle annars KeyError:a vid setup (defensivt; servern
        # returnerar normalt alltid zone).
        entry_data.setdefault(CONF_ZONE, DEFAULT_ZONE)
        zone = entry_data[CONF_ZONE]
        return self.async_create_entry(title=f"Wolta ({zone})", data=entry_data)

    async def async_step_view_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm view-only linking of a streaming-bound plant (Sonnen webhook/Reduxi).

        The entry only POLLS results: sensors and the recompute button work, but no
        entities are selected, nothing is uploaded and no adopt runs. created_by_ha is
        False so removing the entry never deletes the plant server-side - the binding
        owns it. Profile fields are cached like the linked path; the coordinator's
        profile sync keeps them fresh."""
        if user_input is not None:
            prof = self._link_profile or {}
            entry_data: dict[str, Any] = {
                CONF_TOKEN: self._link_token,
                CONF_VIEW_ONLY: True,
                CONF_CREATED_BY_HA: False,
            }
            for key in (
                CONF_ZONE, CONF_BATTERY_KWH, CONF_NAMEPLATE_KWH, CONF_BATTERY_KW,
                CONF_NAMEPLATE_KW, CONF_EFF,
                CONF_RESERVE_PCT, CONF_COST_SEK, CONF_PURCHASE_DATE,
                CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE, CONF_EXPORT_EXTRA_ORE,
                CONF_GRID_VAR_PCT, CONF_EXPORT_EXTRA_PCT,
            ):
                if prof.get(key) is not None:
                    entry_data[key] = prof[key]
            entry_data.setdefault(CONF_ZONE, DEFAULT_ZONE)
            zone = entry_data[CONF_ZONE]
            return self.async_create_entry(title=f"Wolta ({zone})", data=entry_data)
        return self.async_show_form(step_id="view_only", data_schema=vol.Schema({}))

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

        # View-only entries får ALDRIG skapa en ny profil (default-reauthen nedan gör det,
        # avsiktligt, för strömmande entries): anläggningen ägs av sin bindning och en ny
        # tom profil vore fel rad. Be om en färsk token i stället - ägaren mintar en på
        # anläggningssidan (inloggad) på wolta.se.
        if self._get_reauth_entry().data.get(CONF_VIEW_ONLY):
            return await self.async_step_reauth_view_only(user_input)

        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            entry_data = dict(reauth_entry.data)

            try:
                session = async_get_clientsession(self.hass)
                client = WoltaApiClient(session)
                # Send the entry's existing plant id (not self._plant_id — reauth runs in a
                # fresh flow object, whose minted id would be a NEW identity). With it the
                # backend recognises the plant and keeps whatever history survived, instead
                # of silently starting an empty profile. Entries created before v0.16.0 have
                # no stored id; they fall back to entry_id, which IS available here (a reauth
                # flow carries the entry) and is stable and unique to this plant.
                # The old server profile is gone (purged/rotated), so this create SEEDS a
                # fresh one and entry.data is the only surviving copy of the user's config.
                # Pass EVERY configured field, not just the battery basics – otherwise
                # reserve_pct, economy, tariffs and nameplate silently reset server-side until
                # the user re-opens Configure. Optional fields default to None in the client
                # (omitted from the payload) so an unset field stays unset.
                #
                # cost_sek is the exception: create scopes it as "battery" (profile.py). A LINKED
                # (web-created) profile may be PLANT-scoped – its cost covers the whole system –
                # and we cannot GET cost_scope here (the old profile is gone). Sending that value
                # would misattribute the plant price as battery capex → misleading IRR. So skip it
                # for linked entries (created_by_ha False); the plant price is owned/edited on the
                # web anyway. HA-created entries (default True) are battery-scoped → send it.
                created_by_ha = entry_data.get(CONF_CREATED_BY_HA, True)
                # Paret skickas bara när cachen har det HELT. Allt annat återskapas
                # DEKLARERAT och mäts om av backend: en väntande entry (ingen kapacitet
                # i cachen) men också en HALV cache, eftersom alternativet vore att
                # skicka `battery_kwh: null, battery_kw: null` utan deklaration – vilket
                # 422:ar eller skapar en batterilös rad. Ett halvt par går aldrig ut.
                has_pair = (entry_data.get(CONF_BATTERY_KWH) is not None
                            and entry_data.get(CONF_BATTERY_KW) is not None)
                declared = not has_pair
                new_token = await client.create_profile(
                    zone=entry_data[CONF_ZONE],
                    battery_kwh=entry_data.get(CONF_BATTERY_KWH) if has_pair else None,
                    battery_kw=entry_data.get(CONF_BATTERY_KW) if has_pair else None,
                    eff=entry_data.get(CONF_EFF),
                    battery_declared=declared,
                    has_solar=bool(entry_data.get(CONF_SOLAR)),
                    share_profile=entry_data.get(CONF_SHARE, DEFAULT_SHARE),
                    reserve_pct=entry_data.get(CONF_RESERVE_PCT),
                    cost_sek=entry_data.get(CONF_COST_SEK) if created_by_ha else None,
                    purchase_date=entry_data.get(CONF_PURCHASE_DATE),
                    grid_var_ore=entry_data.get(CONF_GRID_VAR_ORE),
                    surcharge_ore=entry_data.get(CONF_SURCHARGE_ORE),
                    export_extra_ore=entry_data.get(CONF_EXPORT_EXTRA_ORE),
                    grid_var_pct=entry_data.get(CONF_GRID_VAR_PCT),
                    export_extra_pct=entry_data.get(CONF_EXPORT_EXTRA_PCT),
                    nameplate_kwh=entry_data.get(CONF_NAMEPLATE_KWH),
                    nameplate_kw=entry_data.get(CONF_NAMEPLATE_KW),
                    client_plant_id=entry_data.get(CONF_PLANT_ID) or reauth_entry.entry_id,
                    # Entries from before v0.29.0 have no stored field -> None is
                    # OMITTED by the client, and the backend (>= 0.79.0) preserves the
                    # stored value on omission instead of clobbering it to null.
                    control_system=entry_data.get(CONF_CONTROL_SYSTEM),
                    control_system_name=entry_data.get(CONF_CONTROL_SYSTEM_NAME),
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
                    # Persist the id too: on a pre-v0.16.0 entry it was just derived from
                    # entry_id, and a later reauth must send the SAME value to land on the
                    # same plant row.
                    data_updates={
                        CONF_TOKEN: new_token,
                        CONF_PLANT_ID: entry_data.get(CONF_PLANT_ID) or reauth_entry.entry_id,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_reauth_view_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reauth for a view-only entry: swap in a fresh token, create nothing.

        The token died because it was rotated web-side (the owner minted a new one) or
        the profile was purged. Any valid profile token is accepted - the entry never
        writes, so a non-bound token is harmless too."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if _is_read_link(user_input["profile_input"]):
                errors["profile_input"] = "link_is_read_link"
            else:
                token = extract_token(user_input["profile_input"])
                try:
                    session = async_get_clientsession(self.hass)
                    client = WoltaApiClient(session)
                    await client.get_profile(token)
                except WoltaAuthError:
                    errors["profile_input"] = "invalid_token"
                except WoltaApiError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data_updates={CONF_TOKEN: token}
                    )
        return self.async_show_form(
            step_id="reauth_view_only",
            data_schema=vol.Schema(
                {
                    vol.Required("profile_input"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    )
                }
            ),
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Options flow – opens with a MENU (decision 2026-08-25): "settings" is today's
# form (edit the SHARED Wolta profile) and "account_link" mints a one-time code
# to link the plant to a wolta.se account (full editing rights back, after
# v0.30.0 replaced the owner token in configuration_url with a read-only link).
#
# settings: the server is the source of truth for the profile fields – the
# form is prefilled from a fresh GET /profile and the diff is computed against
# that snapshot, never against entry.data (which is only a cache) – otherwise a
# web-side change could be silently clobbered with stale values. Fields are
# grouped in collapsible sections (battery/economy/tariffs); sections nest
# user_input one level.
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
    _SEC_TARIFFS: (CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE, CONF_EXPORT_EXTRA_ORE,
                   CONF_GRID_VAR_PCT, CONF_EXPORT_EXTRA_PCT),
}
# Required fields (always present in the form, prefilled from the server snapshot)
_REQUIRED_FIELDS = (CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF)


def _zone_group(zone: str) -> list[tuple[str, str]]:
    """Zones sharing `zone`'s two-letter country prefix, e.g. SE -> SE1..SE4.

    This mirrors the backend's currency rule without duplicating it. The server accepts a
    zone correction only when grade_currency() is unchanged (SEK for SE zones, EUR for
    everything else), because the stored money columns hold raw numbers whose unit follows
    the zone. Same-country is strictly NARROWER than that rule, so this list can never
    offer something the server would reject with 422 - and it avoids the nonsense of
    offering a Norwegian plant a move to Finland just because both are EUR.
    """
    prefix = zone[:2].upper()
    return [(z, label) for z, label in SUPPORTED_ZONES if z[:2].upper() == prefix]


class WoltaOptionsFlow(OptionsFlow):
    """Handle options for an existing Wolta config entry (shared-profile edit)."""

    def __init__(self) -> None:
        """Initialise the options flow."""
        self._server: dict[str, Any] | None = None  # fresh GET /profile snapshot

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Meny (beslut 2026-08-25): options växte från ETT formulär till flera
        åtgärder. Kostar ett klick för den som bara ska ändra ett värde, men ger
        varje ny åtgärd en plats utan att formuläret sväller."""
        options = ["settings", "account_link"]
        # Zonrättelsen visas bara när det FINNS något att byta till: ett land med en enda
        # zon (t.ex. FI) skulle annars få en meny-post som leder till ett formulär med ett
        # enda alternativ - det redan valda.
        if len(_zone_group(self.config_entry.data.get(CONF_ZONE, ""))) > 1:
            options.append("zone_correction")
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_account_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Visa en kopplingskod för wolta.se-kontot (spec 2026-08-24 §4.2)."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})
        session = async_get_clientsession(self.hass)
        client = WoltaApiClient(session)
        try:
            code = await client.mint_claim_code(self.config_entry.data[CONF_TOKEN])
        except WoltaAuthError:
            # Purgad/okänd profil: cannot_connect vore vilseledande – starta
            # reauth-flödet direkt istället för att låta användaren gissa
            # fritt om koden aldrig kommer.
            self.config_entry.async_start_reauth(self.hass)
            return self.async_abort(reason="reauth_required")
        except WoltaApiError as err:
            _LOGGER.error("Could not create linking code: %s", err)
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(
            step_id="account_link",
            data_schema=vol.Schema({}),
            description_placeholders={"code": code},
        )

    async def async_step_zone_correction(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Correct a price zone that was set wrong when the plant was created.

        The zone decides which price series the grade and the economics are measured
        against. It used to be immutable, which made a wrong choice permanent - and until
        2026-08-25 several setup paths silently pre-filled SE3, so wrong choices happened.
        The server now accepts a correction on the owner paths within the same currency
        (api 0.80.0).

        The current zone IS pre-selected here, unlike in setup. That is deliberate and not
        a contradiction: in setup a pre-filled dropdown is acceptance by inaction, whereas
        someone who navigated into "Correct the price zone" has already made an active
        decision to change it - and needs to see what is stored today to change it.
        """
        entry = self.config_entry
        errors: dict[str, str] = {}
        current: str = entry.data.get(CONF_ZONE, "")

        if user_input is not None:
            chosen = user_input[CONF_ZONE]
            if chosen != current:
                session = async_get_clientsession(self.hass)
                client = WoltaApiClient(session)
                try:
                    await client.patch_profile(entry.data[CONF_TOKEN], zone=chosen)
                except WoltaAuthError:
                    entry.async_start_reauth(self.hass)
                    return self.async_abort(reason="reauth_required")
                except WoltaApiError as err:
                    _LOGGER.error("Zone correction rejected: %s", err)
                    errors["base"] = "zone_rejected"
                else:
                    # The stored zone MUST follow the server in the same breath. Reauth
                    # resends entry_data[CONF_ZONE], and the server refuses a re-onboard
                    # whose zone differs from the stored one (reidentify_integration_plant
                    # raises) - so a correction without this line would make the next
                    # reauth fail with "already registered in another price zone".
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_ZONE: chosen}
                    )
            if not errors:
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="zone_correction",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ZONE, default=current): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=z, label=label)
                                for z, label in _zone_group(current)
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_settings(
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

            # Kapacitet och effekt är BÅDA eller INGEN. På en väntande rad är de Optional
            # (se battery_pending nedan), så ett ensamt värde är submitbart – och ett halvt
            # par här blir ett halvt par i cachen, som reauth sedan bara kan deklarera bort
            # (backend avvisar ett halvt par). Felet sätts på det SAKNADE fältet.
            kwh_in = flat.get(CONF_BATTERY_KWH)
            kw_in = flat.get(CONF_BATTERY_KW)
            if (kwh_in is None) != (kw_in is None):
                errors[CONF_BATTERY_KWH if kwh_in is None else CONF_BATTERY_KW] = (
                    "battery_pair_incomplete"
                )

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

            if patch_fields and not errors:
                try:
                    await client.patch_profile(token, **patch_fields)
                except WoltaApiError as err:
                    _LOGGER.error("Failed to patch Wolta profile: %s", err)
                    errors["base"] = "cannot_connect"

            if not errors:
                # Datumförslaget ur statistiken är ett ENGÅNGSerbjudande: formuläret har
                # nu visat det, så nyckeln tas bort. Utan det hade varje senare sparning
                # föreslagit datumet igen och därmed återuppväckt ett datum användaren
                # medvetet rensade.
                prefill_offered = CONF_PREFILL_PURCHASE_DATE in entry.data
                if patch_fields or invert_changed or prefill_offered:
                    new_data = dict(entry.data)
                    for key, val in patch_fields.items():
                        if val is None:
                            new_data.pop(key, None)
                        else:
                            new_data[key] = val
                    if invert_changed:
                        new_data[CONF_INVERT_BATTERY] = invert_new
                    new_data.pop(CONF_PREFILL_PURCHASE_DATE, None)
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

        # Väntande/needs_input-rad (spec 2026-09-14): servern HAR inget kapacitetspar
        # än – backend mäter det ur uppladdningen. Då får formuläret inte KRÄVA paret:
        # ett vol.Required hade renderat DEFAULT_BATTERY_KWH och skrivit den gissningen
        # över mätningen så fort användaren sparade något annat i dialogen. Diff-logiken
        # ovan är oförändrad: fälten ligger kvar i _REQUIRED_FIELDS, så de jämförs rakt
        # mot server-snapshotet – tomt mot tomt (den väntande raden) PATCH:ar ingenting,
        # medan ett rensat visat värde PATCH:ar null precis som förut. Ett komplett par
        # nollställer flaggan server-side (plan A Task 7).
        battery_pending = srv.get("battery_status") in (
            BATTERY_STATUS_PENDING, BATTERY_STATUS_NEEDS_INPUT
        )
        kwh_selector = _number_selector(min_val=MIN_BATTERY_KWH, max_val=500.0, step=0.5, unit="kWh")
        kw_selector = _number_selector(min_val=MIN_BATTERY_KW, max_val=100.0, step=0.1, unit="kW")
        eff_selector = _number_selector(min_val=0.5, max_val=1.0, step=0.01)
        if battery_pending:
            kwh_row = _opt(CONF_BATTERY_KWH, kwh_selector)
            kw_row = _opt(CONF_BATTERY_KW, kw_selector)
            eff_row = _opt(CONF_EFF, eff_selector)
        else:
            kwh_row = (vol.Required(CONF_BATTERY_KWH,
                                    default=srv.get(CONF_BATTERY_KWH, DEFAULT_BATTERY_KWH)),
                       kwh_selector)
            kw_row = (vol.Required(CONF_BATTERY_KW,
                                   default=srv.get(CONF_BATTERY_KW, DEFAULT_BATTERY_KW)),
                      kw_selector)
            eff_row = (vol.Required(CONF_EFF, default=srv.get(CONF_EFF, DEFAULT_EFF)),
                       eff_selector)
        battery_schema = vol.Schema(dict([
            kwh_row,
            _opt(CONF_NAMEPLATE_KWH, _number_selector(min_val=MIN_BATTERY_KWH, max_val=500.0, step=0.5, unit="kWh")),
            kw_row,
            _opt(CONF_NAMEPLATE_KW, _number_selector(min_val=MIN_BATTERY_KW, max_val=100.0, step=0.1, unit="kW")),
            eff_row,
            _opt(CONF_RESERVE_PCT, _number_selector(min_val=0.0, max_val=100.0, step=1.0, unit="%")),
        ]))
        # Se plant_scoped_cost-kommentaren i diff-grenen ovan: fältet döljs helt för
        # plant-scopade profiler (redigeras på wolta.se där etiketterna stämmer).
        # Datumförslaget ur statistiken skickades aldrig till servern (spec 2026-09-14
        # §7.1) – HÄR är stället där användaren bekräftar det. Serverns eget datum
        # vinner: förslaget erbjuds bara när servern saknar ett.
        date_suggested = (srv.get(CONF_PURCHASE_DATE)
                          or entry.data.get(CONF_PREFILL_PURCHASE_DATE))
        date_row = (
            vol.Optional(
                CONF_PURCHASE_DATE,
                description={"suggested_value": date_suggested} if date_suggested else None,
            ),
            _date_selector(),
        )
        economy_schema = vol.Schema(dict(
            ([] if srv.get("cost_scope") == "plant"
             else [_opt(CONF_COST_SEK, _number_selector(min_val=0.0, max_val=10_000_000.0, step=100.0, unit="kr"))])
            + [date_row]
        ))
        tariffs_schema = vol.Schema(dict([
            _opt(CONF_GRID_VAR_ORE, _number_selector(min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh")),
            _opt(CONF_SURCHARGE_ORE, _number_selector(min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh")),
            _opt(CONF_EXPORT_EXTRA_ORE, _number_selector(min_val=-200.0, max_val=500.0, step=0.1, unit="öre/ct per kWh")),
            _opt(CONF_GRID_VAR_PCT, _number_selector(min_val=0.0, max_val=100.0, step=0.01, unit="% of spot")),
            _opt(CONF_EXPORT_EXTRA_PCT, _number_selector(min_val=-100.0, max_val=100.0, step=0.01, unit="% of spot")),
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
        return self.async_show_form(step_id="settings", data_schema=schema, errors=errors)
