"""Config flow for the Wolta integration."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.data_entry_flow import section
from homeassistant.helpers import issue_registry as ir
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

from . import coverage, intervals, stats
from .soc_picker import (  # noqa: F401 - MAX_SOC_SENSORS and _soc_error re-exported
    MAX_SOC_SENSORS,
    soc_error as _soc_error,
    soc_requirement_error,
)
from .api import (
    WoltaApiClient,
    WoltaApiError,
    WoltaAuthError,
    WoltaPlantClaimed,
    WoltaRateLimitError,
)
from .control_system_prefill import (
    ControlSystemChoices,
    async_control_system_choices,
    battery_platforms,
    fallback_choices,
    control_system_name_error,
    control_system_name_selector,
    control_system_selector,
)
from .coordinator import _ISSUE_BATTERY_DIRECTION, desired_invert_intervals
from .direction import Verdict, apply_direction_target, read_verdict, requires_override
from .const import (
    KEY_BATTERY_EVENTS,
    KEY_BATTERY_STATUS,
    KEY_EXPORT_EVENTS,
    KEY_GRID_BENEFIT_EVENTS,
    KEY_GRID_BENEFIT_PERIODS,
    KEY_GRID_FEE_PERIODS,
    KEY_RETAIL_EVENTS,
    KEY_SOLAR_EVENTS,
    BATTERY_STATUS_NEEDS_INPUT,
    BATTERY_STATUS_NONE,
    BATTERY_STATUS_PENDING,
    CONF_BATT_IN,
    CONF_CREATED_BY_HA,
    CONF_DIRECTION_CHANGED_AT,
    CONF_DIRECTION_FLIPPED_AT,
    CONF_DIRECTION_ISSUE_IGNORED,
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
    CONF_GRID_BENEFIT_ORE,
    CONF_GRID_VAR_ORE,
    CONF_GRID_VAR_PCT,
    CONF_INVERT_BATTERY,
    CONF_NAMEPLATE_KW,
    CONF_NAMEPLATE_KWH,
    CONF_PLANT_ID,
    CONF_PREFILL_PURCHASE_DATE,
    CONF_PURCHASE_DATE,
    CONF_REDUCED_ENERGY_TAX,
    CONF_RESERVE_PCT,
    CONF_RETAIL_FIXED_ORE,
    CONF_RETAIL_PRICE_MODEL,
    CONF_SHARE,
    CONF_SOC,
    CONF_SOC_ABSENT,
    CONF_SOC_ISSUE_IGNORED,
    CONF_SOLAR,
    CONF_SURCHARGE_ORE,
    CONF_TOKEN,
    CONF_VIEW_ONLY,
    CONF_ZONE,
    DEFAULT_BATTERY_KW,
    DEFAULT_BATTERY_KWH,
    DEFAULT_EFF,
    DEFAULT_RETAIL_PRICE_MODEL,
    DEFAULT_ZONE,
    DOMAIN,
    MIN_BATTERY_KW,
    MIN_BATTERY_KWH,
    RETAIL_PRICE_MODELS,
    SUPPORTED_ZONES,
)

_LOGGER = logging.getLogger(__name__)

# The SoC picker rules live in soc_picker.py, shared with the soc_required repair.


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


def _as_list(val: str | list | None) -> list[str]:
    """Normalise a stored sensor setting to a list (v0.1.0 entries hold a plain string)."""
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        return [val]
    return []


def _battery_sensors(data: dict) -> tuple[list[str], list[str]]:
    """The battery pair (charge, discharge), normalised and order-independent, so only a real
    change of sensors compares unequal."""
    return (sorted(_as_list(data.get(CONF_BATT_IN))), sorted(_as_list(data.get(CONF_BATT_OUT))))


def _battery_roles_swapped(old: dict, new: dict) -> bool:
    """Whether a sensor moved between charge and discharge: one in the new charge list was a
    discharge sensor before, or the other way round. A newly added sensor is no swap."""
    old_in, old_out = set(_as_list(old.get(CONF_BATT_IN))), set(_as_list(old.get(CONF_BATT_OUT)))
    return bool(set(_as_list(new.get(CONF_BATT_IN))) & old_out
                or set(_as_list(new.get(CONF_BATT_OUT))) & old_in)


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


# "grid_var_pct kräver grid_var_ore": the "ä" plain or as the JSON escape (backslash, u00e4).
_GRID_FIXED_RULE = re.compile(r"grid_var_pct\s+kr(?:ä|\\u00e4)ver\s+grid_var_ore", re.IGNORECASE)


def _is_grid_fixed_error(err: WoltaApiError) -> bool:
    """The server's 422 for "a percentage of spot without the fixed part of the grid fee".

    The detail is the sentence `grid_var_pct kräver grid_var_ore (nätavgiftens fasta del, 0 om
    du bara har procent)`. api.py reduces an error body to one line and the server escapes
    non-ASCII in its JSON, so the "ä" may arrive as six characters (backslash, u00e4). The match is
    the rule's own wording in either form, not just the two field names: another 422 that
    happens to name both fields (a type error, say) is not this rule."""
    return (getattr(err, "status", None) == 422
            and _GRID_FIXED_RULE.search(str(err)) is not None)


# The server's two 422s for the owner's grid benefit (api 0.119.0), each matched on the rule's own
# wording (the "ä" plain or as the JSON escape, as for _GRID_FIXED_RULE) and mapped to the error
# code the options form shows at the field:
#   export_extra_pct kräver grid_benefit_ore (nätnyttans fasta del, 0 om du bara har procent)
#   grid_benefit_ore gäller bara SE1–SE4
_GRID_BENEFIT_RULES = (
    (re.compile(r"export_extra_pct\s+kr(?:ä|\\u00e4)ver\s+grid_benefit_ore", re.IGNORECASE),
     "grid_benefit_fixed_required"),
    (re.compile(r"grid_benefit_ore\s+g(?:ä|\\u00e4)ller\s+bara", re.IGNORECASE),
     "grid_benefit_zone"),
)


def _grid_benefit_error(err: WoltaApiError) -> str | None:
    """The options-form error code for one of the server's grid-benefit 422s, else None.

    Like _is_grid_fixed_error, the match is the rule's own wording and not just the field names:
    another 422 that happens to name the same fields (a type error, say) is not these rules.
    Both are shown at grid_benefit_ore, the field the owner has to fill in or clear."""
    if getattr(err, "status", None) != 422:
        return None
    text = str(err)
    for rule, code in _GRID_BENEFIT_RULES:
        if rule.search(text):
            return code
    return None


# The electricity contract that applies today (api 0.119.0), SE1-SE4 only: the contract type and
# the fixed price. The markup (surcharge_ore) is part of the contract too, but exists in every
# zone. The names are the profile's own, which are also the keyword names of
# WoltaApiClient.create_profile. A change to any of the three adds an entry dated today to the
# contract history on the server (or corrects today's entry); the history, corrections back in
# time and the time periods are edited on wolta.se, never from here (spec
# 2026-10-27-marknadsanpassning, Tillägg 2-4).
_CONTRACT_KEYS = (CONF_RETAIL_PRICE_MODEL, CONF_RETAIL_FIXED_ORE)
# Select fields that always carry a value in the form (their default), so that an untouched form
# does not turn the server's "not set" into a PATCH of the default: both sides of the comparison
# read "not set" as the default, which is what the server does too.
_SELECT_DEFAULTS = {
    CONF_RETAIL_PRICE_MODEL: DEFAULT_RETAIL_PRICE_MODEL,
}
# The contract type's extra choice while the contract that applies today has no type (code review
# C1, server rulings 14, 15 and 22). The server prices a contract without a type as a quarter-hour
# price and keeps it without a type in the contract history: a markup change gives an entry with
# no type, and the first type chosen while no entry has one applies to every entry, each with its
# own markup. A fixed price does so only when no such entry carries a markup (ruling 24: a markup
# only exists in a spot contract); otherwise it applies from today. On a mixed history the server
# applies a chosen type from today. An answer is stored literally, "quarter" included (ruling 15). Without this choice
# the select showed "Quarter-hour price" as its default and never sent it, so an owner on a
# quarter-hour price could not say so. A form value only: never sent to the server, never cached.
_RETAIL_NOT_STATED = "not_stated"


def _contract_unanswered(server: dict[str, Any]) -> bool:
    """Whether the contract that applies today has no type: today's entry of the contract history
    (the one with the latest date; "from the start" is the earliest) when there is one, else the
    server's scalar contract type. The server mirrors today's entry in the scalar, so both say the
    same; the history is read so that a history whose today's entry has no type still offers "not
    stated" (server ruling 22). A malformed history counts as none."""
    events = server.get(KEY_RETAIL_EVENTS)
    posts = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    if posts:
        today = max(posts, key=lambda e: (e.get("date") is not None, str(e.get("date") or "")))
        return today.get(CONF_RETAIL_PRICE_MODEL) is None
    return server.get(CONF_RETAIL_PRICE_MODEL) is None


def _contract_profile_kwargs(zone: Any, data: dict[str, Any]) -> dict[str, Any]:
    """The cached contract as create_profile keywords: the contract type and the fixed price
    (None outside SE1-SE4, where the server refuses them with 422: a value cached from before a
    zone correction would block the reauth, as the energy-tax answer does outside SE1-SE3), and
    the markup, which is never sent with a fixed price (a fixed price is the retailer's whole
    price and the server refuses a markup next to it)."""
    asked = _benefit_zone(zone)
    contract = {key: (data.get(key) if asked else None) for key in _CONTRACT_KEYS}
    fixed = contract[CONF_RETAIL_PRICE_MODEL] == "fixed"
    return {**contract, CONF_SURCHARGE_ORE: None if fixed else data.get(CONF_SURCHARGE_ORE)}


# The server's 422s for the time periods, the contract and the grid benefit history (api 0.119.0)
# that a save from here can meet. Each is matched on the rule's own wording and not on the field
# names alone, so another 422 that happens to name the same fields (a type error, say) is not one
# of them. The server escapes non-ASCII in its JSON and api.py reduces an error body to one line,
# so the match runs on the text with \uXXXX escapes decoded:
#   tidsperioder kräver ett baspris (grid_var_ore resp. grid_benefit_ore)
#   retail_fixed_ore krävs med retail_price_model fixed och får inte sättas annars
#   surcharge_ore får inte sättas med retail_price_model fixed (det fasta priset är elhandelns
#     hela pris)
#   tidsperioderna, helgdagsreglerna och elhandelsavtalet gäller bara SE1–SE4
#   en nätnyttepost behöver datum (inte i framtiden, inte två på samma dag) och ett baspris
#     (grid_benefit_ore)
#   purchase_date härleds ur etapperna och kan bara sättas TIDIGARE än den första etappen (...)
# The purchase date's rule is not a tariff rule but is met the same way: a plant with stages (a
# battery bought years after the solar panels) where the owner types the later date here
# (2026-10-29). The stages are edited on wolta.se.
# The grid benefit history's rule (Tillägg 5) is the one a save from here meets by
# clearing the base price while the server keeps a grid benefit history: today's entry would have
# no base price. The server checks it before the periods' rule, so it is matched first. The
# retailer's terms have no such case (a cleared value is plain spot from today, the server's
# ruling 10), so their list's rule is not here. The rules for the period lists themselves and
# for the contract history are not here either: this form never sends them.
_SERVER_RULES = (
    (re.compile(r"en\s+nätnyttepost\s+behöver\s+datum", re.IGNORECASE),
     "grid_benefit_history_requires_base"),
    (re.compile(r"tidsperioder\s+kräver\s+ett\s+baspris", re.IGNORECASE),
     "periods_require_base"),
    (re.compile(r"retail_fixed_ore\s+krävs\s+med", re.IGNORECASE), "retail_fixed_invalid"),
    (re.compile(r"surcharge_ore\s+får\s+inte\s+sättas\s+med\s+retail_price_model\s+fixed",
                re.IGNORECASE),
     "retail_fixed_surcharge"),
    (re.compile(r"elhandelsavtalet\s+gäller\s+bara", re.IGNORECASE), "tariff_zone_se_only"),
    (re.compile(r"purchase_date\s+härleds\s+ur\s+etapperna", re.IGNORECASE),
     "purchase_date_after_stages"),
)
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
# api.py's WoltaApiError text: "HTTP <status> from <url>: <body, one line, at most 200 chars>".
_ERROR_BODY = re.compile(r"^HTTP \d+ from \S+: (.*)$", re.DOTALL)
# The detail string; the closing quote is optional because api.py may have cut the body inside it.
_DETAIL_FIELD = re.compile(r'"detail"\s*:\s*"((?:[^"\\]|\\.)*)(")?')


def _server_reason(err: WoltaApiError) -> str:
    """The server's own words for a refusal the form has no rule for, shown instead of
    cannot_connect: the `detail` of its JSON with its escapes decoded (marked with " …" when
    api.py's 200-character cut fell inside it), else the body as api.py kept it (a proxy page)."""
    text = str(err)
    body = m.group(1) if (m := _ERROR_BODY.match(text)) else text
    if not (detail := _DETAIL_FIELD.search(body)):
        return body.strip()
    raw, closed = detail.group(1), detail.group(2)
    try:
        reason = json.loads(f'"{raw}"')
    except ValueError:  # the cut fell inside an escape
        reason = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), raw)
    return reason.strip() if closed else f"{reason.strip()} …"


def _first_stage_date(server: dict[str, Any] | None) -> date | None:
    """The earliest date among the plant's battery and solar stages in a GET /profile snapshot
    (the server's tl.first_date over both lists), else None: no stages, or an older server
    without the lists. Malformed entries are skipped, never guessed at."""
    dates = [
        parsed
        for key in (KEY_BATTERY_EVENTS, KEY_SOLAR_EVENTS)
        for event in ((server or {}).get(key) or [])
        if isinstance(event, dict)
        and (parsed := dt_util.parse_date(str(event.get("date") or ""))) is not None
    ]
    return min(dates) if dates else None


def _refusal(err: WoltaApiError, errors: dict[str, str]) -> str:
    """Set errors["base"] for an API error without a rule of its own, and return the text for
    the form's server_detail placeholder. A 422 is the server's answer, shown in its own words
    (server_rejected): an owner told to check the internet connection checks the wrong thing
    (2026-10-29). Anything else (no answer, 5xx) is cannot_connect, with no detail."""
    if getattr(err, "status", None) == 422:
        errors["base"] = "server_rejected"
        return _server_reason(err)
    errors["base"] = "cannot_connect"
    return ""


def _server_rule_error(err: WoltaApiError) -> str | None:
    """The options-form error code for one of the server's period, contract or grid benefit
    history 422s, else None."""
    if getattr(err, "status", None) != 422:
        return None
    text = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), str(err))
    for rule, code in _SERVER_RULES:
        if rule.search(text):
            return code
    return None


def _server_rule_field(code: str, patch_fields: dict[str, Any]) -> str:
    """The field a server period, contract or grid benefit history 422 is shown at.

    The periods text names both base prices: the one this save cleared is the one (the grid
    fee's when neither was cleared, a cache out of step with the server)."""
    if code == "grid_benefit_history_requires_base":
        return CONF_GRID_BENEFIT_ORE
    if code == "periods_require_base":
        if CONF_GRID_VAR_ORE not in patch_fields and CONF_GRID_BENEFIT_ORE in patch_fields:
            return CONF_GRID_BENEFIT_ORE
        return CONF_GRID_VAR_ORE
    if code == "retail_fixed_invalid":
        return CONF_RETAIL_FIXED_ORE
    if code == "retail_fixed_surcharge":
        return CONF_SURCHARGE_ORE
    if code == "purchase_date_after_stages":
        return CONF_PURCHASE_DATE
    return CONF_RETAIL_PRICE_MODEL


# The read-only summary of what is edited on wolta.se (the time periods, the contract history and
# the sale side's history), in the user's HA language (sv/en, else English), like _zone_hint:
# counts, dates and the holiday rule are formatted here, so the sentence is too. Empty when there
# is nothing to summarise.
_WEB_SUMMARY: dict[str, dict[str, str]] = {
    "sv": {
        "fee_one": "Nätavgiften har 1 tidsperiod med eget pris",
        "fee_many": "Nätavgiften har {count} tidsperioder med eget pris",
        "benefit_one": "Nätnyttan har 1 tidsperiod med eget pris",
        "benefit_many": "Nätnyttan har {count} tidsperioder med eget pris",
        "holidays": " (helgdagar som inte räknas som vardagar: {rule})",
        "contract_one": "Avtalshistoriken har 1 avtal, som gäller hela historiken.",
        "contract_many": "Avtalshistoriken har {count} avtal, det som gäller i dag sedan {date}.",
        "benefit_history_one": "Nätnyttans historik har 1 post, som gäller hela historiken.",
        "benefit_history_many": ("Nätnyttans historik har {count} poster, den som gäller i dag "
                                 "sedan {date}."),
        "export_one": ("Historiken för elhandelns villkor för såld el har 1 post, som gäller hela "
                       "historiken."),
        "export_many": ("Historiken för elhandelns villkor för såld el har {count} poster, den som "
                        "gäller i dag sedan {date}."),
        "tail": "De ändras på wolta.se.",
    },
    "en": {
        "fee_one": "The grid fee has 1 time period with a price of its own",
        "fee_many": "The grid fee has {count} time periods with a price of their own",
        "benefit_one": "The grid benefit has 1 time period with a price of its own",
        "benefit_many": "The grid benefit has {count} time periods with a price of their own",
        "holidays": " (holidays not counted as weekdays: {rule})",
        "contract_one": "The contract history has 1 contract, which applies to all of your history.",
        "contract_many": "The contract history has {count} contracts, today's since {date}.",
        "benefit_history_one": ("The grid benefit history has 1 entry, which applies to all of "
                                "your history."),
        "benefit_history_many": ("The grid benefit history has {count} entries, today's since "
                                 "{date}."),
        "export_one": ("The history of your retailer's terms for sold electricity has 1 entry, "
                       "which applies to all of your history."),
        "export_many": ("The history of your retailer's terms for sold electricity has {count} "
                        "entries, today's since {date}."),
        "tail": "These are changed on wolta.se.",
    },
}
# The history lists the summary counts, in the order it names them (the purchase side's contract
# first, then the sale side: the grid benefit and the retailer's terms), with their text keys.
_HISTORY_LISTS = ((KEY_RETAIL_EVENTS, "contract"), (KEY_GRID_BENEFIT_EVENTS, "benefit_history"),
                  (KEY_EXPORT_EVENTS, "export"))
# The holiday rules by name (the server's HOLIDAY_RULES, as wolta.se names them). A rule this
# version does not know is shown by its key. The server counts a list without a rule with its
# default, public_eves (tariff.DEFAULT_HOLIDAYS), so that is the rule shown then.
_HOLIDAY_NAMES: dict[str, dict[str, str]] = {
    "sv": {"none": "inga", "public": "allmänna helgdagar",
           "public_eves": "allmänna helgdagar, julafton och nyårsafton",
           "public_eves_maundy": "allmänna helgdagar, julafton, nyårsafton och skärtorsdag"},
    "en": {"none": "none", "public": "public holidays",
           "public_eves": "public holidays, Christmas Eve and New Year's Eve",
           "public_eves_maundy": "public holidays, Christmas Eve, New Year's Eve and Maundy Thursday"},
}
_DEFAULT_HOLIDAYS = "public_eves"


def _web_summary(hass_language: str | None, server: dict[str, Any]) -> str:
    """One short paragraph on the server's time periods, contract history and sale history, or "".

    Shown, never edited: the lists are changed on wolta.se, and nothing here sends them."""
    lang = (hass_language or "en").split("-")[0]
    lang = lang if lang in _WEB_SUMMARY else "en"
    text = _WEB_SUMMARY[lang]
    parts: list[str] = []
    for key, holidays_key, prefix in (
            (KEY_GRID_FEE_PERIODS, "grid_fee_holidays", "fee"),
            (KEY_GRID_BENEFIT_PERIODS, "grid_benefit_holidays", "benefit")):
        periods = server.get(key)
        if not (isinstance(periods, list) and periods):
            continue
        sentence = (text[f"{prefix}_one"] if len(periods) == 1
                    else text[f"{prefix}_many"].format(count=len(periods)))
        # The holiday rule only decides anything for a period on weekdays.
        if any(isinstance(p, dict) and p.get("days") == "weekdays" for p in periods):
            rule = server.get(holidays_key) or _DEFAULT_HOLIDAYS
            sentence += text["holidays"].format(rule=_HOLIDAY_NAMES[lang].get(rule, rule))
        parts.append(sentence + ".")
    # The three histories share the server's rules (Tillägg 3-5): sorted by date, no future date,
    # only the first entry may lack one ("from the start"), and the first entry applies back to
    # the start of the history whatever its date (tariff.retail_post_on, export_post_on,
    # grid_benefit_post_on: before the first entry, the first entry).
    for key, prefix in _HISTORY_LISTS:
        events = server.get(key)
        if not (isinstance(events, list) and events and isinstance(events[-1], dict)):
            continue
        if len(events) == 1:
            # A lone entry has no "since".
            parts.append(text[f"{prefix}_one"])
        else:
            # Today's entry is the last one.
            since = events[-1].get("date")
            if since:
                parts.append(text[f"{prefix}_many"].format(count=len(events), date=since))
    if not parts:
        return ""
    return " ".join([*parts, text["tail"]])


# Batteriets riktning (options): the sentences that depend on the desired direction and the
# server's SoC verdict, in the user's HA language (sv/en, else English), like _WEB_SUMMARY: a
# step has one description, so what varies is passed in as a placeholder. Dates are local and
# language-neutral. The verdict never shows a count of ok days. The setting is described as
# what Wolta does ("swaps"); "reversed" is kept for the SoC verdict alone.
_DIRECTION_TEXT: dict[str, dict[str, str]] = {
    "sv": {
        "none": "Wolta lagrar allt som sensorerna visar det.",
        "all": "Wolta byter plats på laddat och urladdat i hela historiken.",
        "from": "Wolta byter plats på laddat och urladdat från {start}.",
        "periods": "Wolta byter plats på laddat och urladdat för {periods}.",
        "ok": ("Batteriets laddningsnivå säger att riktningen stämmer: kontrollerat "
               "{checked_days} dygn, {reversed_days} omkastade, per {as_of}."),
        "reversed": ("Batteriets laddningsnivå säger att riktningen är omkastad: kontrollerat "
                     "{checked_days} dygn, {reversed_days} omkastade, per {as_of}."),
        "stale": " Kontrollen är från före din senaste ändring.",
        "no_check": "Wolta kan inte kontrollera riktningen mot laddningsnivån.",
        # The gate's lead, true for each state: a swap against ok/verified is refused by the
        # server; with nothing stored to swap nothing is refused, but it still goes against
        # the check; against reversed the owner confirms having checked the sensors.
        "gate_swap": ("\n\n{verdict} Wolta nekar ett byte som går emot laddningsnivån om du inte "
                      "kryssar i rutan nedan."),
        "gate_unstored": ("\n\n{verdict} Ändringen går emot laddningsnivån; kryssa i rutan "
                          "nedan för att göra den ändå."),
        "gate_reversed": ("\n\n{verdict} Kryssa i rutan nedan för att bekräfta att du har "
                          "kontrollerat sensorerna."),
        "gate_repair": (" Reparationen under Inställningar → System → Reparationer byter exakt "
                        "den period Wolta har sett."),
    },
    "en": {
        "none": "Wolta stores everything as the sensors report it.",
        "all": "Wolta swaps charged and discharged for the whole history.",
        "from": "Wolta swaps charged and discharged from {start}.",
        "periods": "Wolta swaps charged and discharged for {periods}.",
        "ok": ("The battery's state of charge says the direction is right: checked "
               "{checked_days} days, {reversed_days} reversed, as of {as_of}."),
        "reversed": ("The battery's state of charge says the direction is reversed: checked "
                     "{checked_days} days, {reversed_days} reversed, as of {as_of}."),
        "stale": " The check predates your latest change.",
        "no_check": "Wolta cannot check the direction against the state of charge.",
        "gate_swap": ("\n\n{verdict} Wolta refuses a swap that goes against the state of "
                      "charge unless you tick the box below."),
        "gate_unstored": ("\n\n{verdict} The change goes against the state of charge; tick the "
                          "box below to make it anyway."),
        "gate_reversed": ("\n\n{verdict} Tick the box below to confirm that you have checked "
                          "the sensors."),
        "gate_repair": (" The repair under Settings → System → Repairs swaps exactly the period "
                        "Wolta has seen."),
    },
}
_DIRECTION_OVERRIDE = "direction_override"
_CONFIRM_SWAP = "confirm_swap"  # Reconfigure: a battery role swap, confirmed
_DIRECTION_FLIP_FROM = "start"
_TICK = timedelta(microseconds=1)  # an end is exclusive: its last instant is the one before


def _direction_text(hass_language: str | None) -> dict[str, str]:
    lang = (hass_language or "en").split("-")[0]
    return _DIRECTION_TEXT.get(lang, _DIRECTION_TEXT["en"])


def _at_midnight(value: datetime | None) -> bool:
    """Open, or at local midnight."""
    if value is None:
        return True
    local = dt_util.as_local(value)
    return (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)


def _local_time(value: datetime) -> str:
    """Date and time in local time. In the hour that occurs twice when summer time ends, with
    the UTC offset: "2026-10-25 02:00+02:00" and "2026-10-25 02:00+01:00" are two instants."""
    local = dt_util.as_local(value)
    text = local.strftime("%Y-%m-%d %H:%M")
    if local.replace(fold=1 - local.fold).utcoffset() != local.utcoffset():
        text += local.isoformat(timespec="minutes")[-6:]
    return text


def _local_moment(value: datetime | None) -> str:
    """An instant in local time: the date alone at midnight, else with the time; … if open."""
    if value is None:
        return "…"
    if _at_midnight(value):
        return dt_util.as_local(value).date().isoformat()
    return _local_time(value)


def _local_period(start: datetime | None, end: datetime | None) -> str:
    """A half-open period in local time, the one format of the menu and the confirmation.

    Two conventions, told apart by their separator so neither reads as off by one:
    - Both bounds at local midnight (or open): dates, the END AS THE LAST DAY INCLUDED,
      with "–": "2026-10-01 – 2026-10-02"; one day is one date; an open end is "…".
    - A bound inside a day: date and time on both bounds, the END EXCLUSIVE, with "→":
      "2026-10-11 00:00 → 2026-10-11 12:00" (see _local_time for the repeated hour)."""
    if _at_midnight(start) and _at_midnight(end):
        first = "…" if start is None else dt_util.as_local(start).date().isoformat()
        last = "…" if end is None else dt_util.as_local(end - _TICK).date().isoformat()
        return first if first == last and start is not None else f"{first} – {last}"

    def _fmt(value: datetime | None) -> str:
        return "…" if value is None else _local_time(value)

    return f"{_fmt(start)} → {_fmt(end)}"


def _desired_text(text: dict[str, str], desired: list[intervals.Interval]) -> str:
    """What is reversed now: nothing, all of it, from a moment, or the periods."""
    if not desired:
        return text["none"]
    if desired == [(None, None)]:
        return text["all"]
    if len(desired) == 1 and desired[0][1] is None:
        return text["from"].format(start=_local_moment(desired[0][0]))
    return text["periods"].format(
        periods=", ".join(_local_period(start, end) for start, end in desired))


def _verdict_text(text: dict[str, str], verdict: Verdict) -> str:
    """The SoC verdict in one sentence. State unknown (too few days checked) is no verdict."""
    if verdict.state not in ("ok", "verified", "reversed"):
        return text["no_check"]
    as_of = "-" if verdict.as_of is None else dt_util.as_local(verdict.as_of).date().isoformat()
    sentence = text["reversed" if verdict.state == "reversed" else "ok"].format(
        checked_days=verdict.checked_days, reversed_days=verdict.reversed_days, as_of=as_of)
    return sentence if verdict.current else sentence + text["stale"]


def _local_days(windows: list[tuple[datetime, datetime]]) -> int:
    """The number of local calendar days the windows touch."""
    days: set[date] = set()
    for start, end in windows:
        day = dt_util.as_local(start).date()
        last = dt_util.as_local(end - _TICK).date()
        while day <= last:
            days.add(day)
            day += timedelta(days=1)
    return len(days)


def _with_cleared_fields_empty(schema: vol.Schema, user_input: dict[str, Any]) -> dict[str, Any]:
    """`user_input` as suggested values for the re-shown form, a cleared field included.

    A cleared field is absent from the submitted section, and add_suggested_values_to_schema
    leaves a field it has no value for on the suggestion the schema was built with, which is
    the STORED value. Re-showing that would let the next submit save the old value the user
    just removed. So a section field that carries a stored suggestion but is absent from
    what was submitted is suggested as empty (None)."""
    out = dict(user_input)
    for sec_key, sec in schema.schema.items():
        if not isinstance(sec, section):
            continue
        submitted = user_input.get(sec_key.schema)
        typed = dict(submitted) if isinstance(submitted, dict) else {}
        for field in sec.schema.schema:
            description = getattr(field, "description", None) or {}
            if field.schema not in typed and description.get("suggested_value") is not None:
                typed[field.schema] = None
        out[sec_key.schema] = typed
    return out


async def _refresh_prices(prices: Any) -> None:
    """Refresh the price coordinator after a tariff save. Run as a background task, so it
    swallows everything: nothing is left to retrieve the exception from."""
    try:
        await prices.async_request_refresh()
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("Options flow: price refresh failed.", exc_info=True)


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
    """Accept a raw profile token OR a full wolta.se link carrying it.

    The personal link carries the token in the fragment (`#profile=`) since the Wolta
    security review 2026-10-16 - a fragment never reaches a server, a log or a Referer.
    Older links used the query (`?profile=`); both keep working."""
    value = value.strip()
    if "profile=" in value:
        from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

        parsed = urlparse(value)
        for part in (parsed.fragment, parsed.query):
            candidates = parse_qs(part).get("profile")
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
        # Reauth: set when the server refused the re-created profile because the cached
        # tariff has a percentage of spot without the fixed part; the form then asks for it.
        self._reauth_ask_fixed: bool = False
        # The server's words for the last refusal shown as server_rejected (the placeholder of
        # that error's text). Set together with the error, so a stale value is never shown.
        self._server_detail: str = ""
        self._prefill: dict[str, Any] = {}  # eff/purchase_date/invert från statistiken
        # Styrsystemets val och förslag (sätts i entitetssteget): serverns lista eller
        # const.CONTROL_SYSTEMS, och förslaget ur batterisensorernas integrationsdomän.
        # suggestion None = inget förslag → plant-fältet får ingen default.
        self._cs_choices: ControlSystemChoices = fallback_choices()
        # Stable plant identity for this entry, minted once here and persisted in entry.data.
        # Minted at flow start (not at entry creation) because BOTH the create path and the
        # link path need it while the flow is still running. See const.CONF_PLANT_ID for why
        # this is not the config entry_id.
        self._plant_id: str = secrets.token_hex(16)
        # Reconfigure, a battery role swap waiting for its confirmation: the entry.data
        # updates of the submit, the battery pair they were computed against
        # (_battery_sensors), the soc_absent answer still to PATCH (None: unchanged), and
        # the rebuilt period shown.
        self._swap_updates: dict[str, Any] | None = None
        self._swap_from: tuple[list[str], list[str]] | None = None
        self._swap_soc_absent: bool | None = None
        self._swap_placeholders: dict[str, str] = {}

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
                status = prof.get(KEY_BATTERY_STATUS)
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
            # Same rule as the repair (control_system_prefill.control_system_name_error).
            name_error = control_system_name_error(
                user_input.get(CONF_CONTROL_SYSTEM), user_input.get(CONF_CONTROL_SYSTEM_NAME))
            if name_error:
                errors[CONF_CONTROL_SYSTEM_NAME] = name_error
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
        suggestion = self._cs_choices.suggestion
        cs_field = (vol.Required(CONF_CONTROL_SYSTEM, default=suggestion)
                    if suggestion else vol.Required(CONF_CONTROL_SYSTEM))
        schema = vol.Schema(
            {
                # Deliberately NO default: the zone is an ACTIVE choice (directive
                # 2026-08-24) - it selects the price series the grade AND the economics
                # are computed against, and it is immutable server-side after creation.
                # A pre-selected dropdown is acceptance-by-inaction, so the location
                # guess is surfaced by moving that zone to the TOP of the list with a
                # label (and repeated as text in the description) instead.
                vol.Required(CONF_ZONE): _zone_selector(guess, lang),
                cs_field: control_system_selector(self._cs_choices.options),
                vol.Optional(CONF_CONTROL_SYSTEM_NAME): control_system_name_selector(),
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
        # The answer to the SoC requirement (spec 2026-10-20 §4.1), sent either way: a
        # picked sensor is an answer too ("has one"). The entry stores it because the
        # server has it from this POST - the coordinator mirrors it from here on.
        soc_absent = bool(self._entities_data.get(CONF_SOC_ABSENT, False))

        try:
            session = async_get_clientsession(self.hass)
            client = WoltaApiClient(session)
            token = await client.create_profile(
                zone=zone,
                has_solar=bool(solar),
                # The share checkbox is gone (2026-09-15 decision): it never gated
                # corpus membership or the raw-data retention it claimed to, so
                # every plant is created shared now. Full reasoning in the reauth
                # branch below, which sends the same constant True.
                share_profile=True,
                battery_declared=True,
                client_plant_id=self._plant_id,
                control_system=control_system,
                control_system_name=control_system_name,
                soc_absent=soc_absent,
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
            # Key kept (reauth/diagnostics still read it) - always True now.
            CONF_SHARE: True,
            CONF_CREATED_BY_HA: True,
            CONF_INVERT_BATTERY: bool(
                self._plant_data.get(CONF_INVERT_BATTERY, False)
            ),
            CONF_SOC_ABSENT: soc_absent,
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
        if self._entities_data.get(CONF_SOC):
            entry_data[CONF_SOC] = list(self._entities_data[CONF_SOC])
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
            soc_error = soc_requirement_error(
                self.hass, user_input.get(CONF_SOC),
                absent=bool(user_input.get(CONF_SOC_ABSENT, False)),
            )
            if soc_error:
                errors[CONF_SOC] = soc_error

            if not errors and self._link_token is not None:
                # A linked profile already exists, so no POST carries the answer: PATCH it
                # when it differs from the server's. Only then may the entry store it -
                # the coordinator mirrors soc_absent from the server and would wipe a
                # local-only value on the first sync.
                await self._async_sync_link_soc_absent(
                    bool(user_input.get(CONF_SOC_ABSENT, False)), errors)

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
                self._cs_choices = fallback_choices()
                if self._link_token is None:
                    try:
                        platforms = battery_platforms(
                            self.hass,
                            list(user_input[CONF_BATT_IN]) + list(user_input[CONF_BATT_OUT]),
                        )
                        # Förslaget är alltid ett av alternativen (se
                        # async_control_system_choices): annars MultipleInvalid i sista
                        # onboarding-steget.
                        self._cs_choices = await async_control_system_choices(
                            WoltaApiClient(async_get_clientsession(self.hass)), platforms)
                    except Exception:  # pylint: disable=broad-except
                        _LOGGER.debug(
                            "control_system prefill failed; const list, no suggestion",
                            exc_info=True,
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
            # Linking an existing profile: start the "no SoC sensor" box on the owner's
            # answer from wolta.se, so they are not asked a question they already answered.
            if self._link_profile is not None and self._link_profile.get(CONF_SOC_ABSENT):
                defaults = {**defaults, CONF_SOC_ABSENT: True}

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
                # Multi-select, but `suggested_value` rather than `default=` for the same
                # reason as the pickers above: clearing the field must actually clear it,
                # and a `default=` would re-fill the list from the previous submission.
                #
                # Required in effect (a sensor or the box below), but vol.Optional on
                # purpose: the frontend's checkAllRequiredFields (step-flow-form.ts)
                # blocks a submit with an empty Required field behind its generic
                # not_all_required_fields, so our soc_required explanation would never
                # show. The requirement lives in soc_requirement_error instead.
                vol.Optional(
                    CONF_SOC,
                    description=(
                        {"suggested_value": defaults[CONF_SOC]}
                        if defaults.get(CONF_SOC) else None
                    ),
                ): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
                vol.Optional(
                    CONF_SOC_ABSENT,
                    default=bool(defaults.get(CONF_SOC_ABSENT, False)),
                ): bool,
            }
        )

        return self.async_show_form(
            step_id="entities",
            data_schema=schema,
            errors=errors,
            description_placeholders={"server_detail": self._server_detail},
        )

    async def _async_sync_link_soc_absent(
        self, absent: bool, errors: dict[str, str]
    ) -> None:
        """PATCH soc_absent on a linked profile when it differs from the server's.

        On success the cached GET snapshot is updated, so _create_linked_entry copies
        the value the server now holds (and nothing when the server holds none). A
        failed PATCH sets server_rejected or cannot_connect (_refusal) and the entities form
        is shown again."""
        prof = self._link_profile if self._link_profile is not None else {}
        if absent == bool(prof.get(CONF_SOC_ABSENT)):
            return
        try:
            await WoltaApiClient(async_get_clientsession(self.hass)).patch_profile(
                self._link_token, soc_absent=absent)
        except WoltaApiError as err:
            _LOGGER.error("Failed to patch soc_absent on the linked profile: %s", err)
            self._server_detail = _refusal(err, errors)
            return
        prof[CONF_SOC_ABSENT] = absent
        self._link_profile = prof

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
            swap_floor: datetime | None = None
            for key in (CONF_BATT_IN, CONF_BATT_OUT, CONF_GRID_IN, CONF_GRID_OUT):
                if not user_input.get(key):
                    errors[key] = "required_sensor"
            soc_absent = bool(user_input.get(CONF_SOC_ABSENT, False))
            soc_error = soc_requirement_error(
                self.hass, user_input.get(CONF_SOC), absent=soc_absent)
            if soc_error:
                errors[CONF_SOC] = soc_error
            if not errors:
                await self.async_set_unique_id(entry.unique_id)
                self._abort_if_unique_id_mismatch()
                # A sensor moved between charge and discharge: the entity job then rebuilds
                # both battery fields of every stored row from the replacement floor, months
                # of history, so the save waits for a confirmation that names the period
                # (async_step_reconfigure_battery_swap). Nothing uploaded, or no rows on the
                # server: nothing stored changes, and the save goes ahead unasked. Decided
                # before the PATCH, so an abort here leaves the server as it was too.
                if _battery_roles_swapped(entry.data, user_input):
                    coordinator = getattr(entry, "runtime_data", None)
                    if coordinator is None:
                        # Not running: what is uploaded is unknown, and the reload after a
                        # save would rebuild it unasked.
                        return self.async_abort(reason="battery_swap_not_loaded")
                    if coordinator.has_uploaded():
                        floor = coordinator.replacement_floor()
                        if floor is coverage.MISSING or floor is None:
                            # Never a confirmation without a period: the profile poll
                            # brings data_since shortly. None ("no rows") although rows
                            # are uploaded counts as unknown too.
                            return self.async_abort(reason="battery_swap_period_unknown")
                        swap_floor = floor
                # soc_absent is mirrored from the server (coordinator _PROFILE_SYNC_KEYS),
                # so a changed answer is PATCHed first and stored only once the server has
                # it. A failed PATCH saves nothing from this submit: the form shows the
                # server's reason or cannot_connect (_refusal) and the user retries.
                # Behind a swap confirmation the PATCH waits for it, so Cancel sends nothing.
                soc_absent_changed = soc_absent != bool(entry.data.get(CONF_SOC_ABSENT))
                if soc_absent_changed and swap_floor is None:
                    try:
                        await WoltaApiClient(
                            async_get_clientsession(self.hass)
                        ).patch_profile(entry.data[CONF_TOKEN], soc_absent=soc_absent)
                    except WoltaApiError as err:
                        _LOGGER.error("Failed to patch soc_absent: %s", err)
                        self._server_detail = _refusal(err, errors)
            if not errors:
                data_updates: dict[str, Any] = {
                    CONF_BATT_IN: user_input[CONF_BATT_IN],
                    CONF_BATT_OUT: user_input[CONF_BATT_OUT],
                    CONF_GRID_IN: user_input[CONF_GRID_IN],
                    CONF_GRID_OUT: user_input[CONF_GRID_OUT],
                    CONF_SOLAR: user_input.get(CONF_SOLAR) or [],
                    CONF_EXTERNAL_CONTROL: user_input.get(CONF_EXTERNAL_CONTROL) or None,
                    CONF_FLEX_COMPENSATION: user_input.get(CONF_FLEX_COMPENSATION) or None,
                    # Cleared picker -> [] (like solar), so the coordinator stops
                    # collecting SoC.
                    CONF_SOC: list(user_input.get(CONF_SOC) or []),
                }
                if soc_absent_changed:
                    # Unchanged, the entry keeps the server's mirrored value as it is.
                    data_updates[CONF_SOC_ABSENT] = soc_absent
                if swap_floor is not None:
                    self._swap_updates = data_updates
                    self._swap_from = _battery_sensors(entry.data)
                    self._swap_soc_absent = soc_absent if soc_absent_changed else None
                    self._swap_placeholders = {
                        "start": dt_util.as_local(swap_floor).date().isoformat(),
                        "days": str(_local_days([(swap_floor, dt_util.utcnow())])),
                    }
                    return await self.async_step_reconfigure_battery_swap()
                return self._save_reconfigure(entry, data_updates)

        defaults = {
            k: entry.data.get(k)
            for k in (
                CONF_BATT_IN, CONF_BATT_OUT, CONF_GRID_IN, CONF_GRID_OUT, CONF_SOLAR,
                CONF_EXTERNAL_CONTROL, CONF_FLEX_COMPENSATION, CONF_SOC,
            )
        }
        defaults[CONF_SOC_ABSENT] = bool(entry.data.get(CONF_SOC_ABSENT))
        # A failed submit re-shows what the user chose, not the stored value.
        if errors and user_input is not None:
            defaults[CONF_SOC] = user_input.get(CONF_SOC)
            defaults[CONF_SOC_ABSENT] = bool(user_input.get(CONF_SOC_ABSENT, False))
            if errors.get("base") == "cannot_connect":
                # The soc_absent PATCH failed after the whole form validated: keep every
                # choice, so a retry does not silently drop the other edits. HA leaves a
                # cleared Optional picker out of user_input, so a missing key means "cleared",
                # not "unchanged": read every field from the submit, not only present keys.
                defaults.update({k: user_input.get(k) for k in defaults if k != CONF_SOC_ABSENT})
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
                # Multi-select with `suggested_value` (see the setup step): a `default=`
                # would make the list un-clearable. vol.Optional although it is required
                # in effect - same reason as in the setup step (the frontend's generic
                # not_all_required_fields would hide soc_required).
                vol.Optional(
                    CONF_SOC,
                    description=(
                        {"suggested_value": defaults[CONF_SOC]}
                        if defaults[CONF_SOC] else None
                    ),
                ): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
                # A boolean always has a value, so `default=` is safe here (unlike the
                # pickers): unticking sends False.
                vol.Optional(CONF_SOC_ABSENT, default=defaults[CONF_SOC_ABSENT]): bool,
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors,
            description_placeholders={"server_detail": self._server_detail})

    def _save_reconfigure(
        self, entry: ConfigEntry, data_updates: dict[str, Any]
    ) -> ConfigFlowResult:
        """Store a reconfigure submit and reload. Merged into entry.data as it is now: a
        swap confirmation answered later (which re-checks only the battery pair) does not
        revert other keys written meanwhile."""
        new_data = {**entry.data, **data_updates}
        # The old "ignore" of the soc_missing repair answered an offer, not the
        # requirement (spec 2026-10-20 §4.2): it is never read again, and any
        # save drops it.
        new_data.pop(CONF_SOC_ISSUE_IGNORED, None)
        # Changed battery sensors replace the stored battery fields (an entity job),
        # so an open direction repair rests on the old grade: stamp as for a flip
        # so it is not raised again until the server has recounted.
        # Order-independent and normalised like the coordinator (a v0.1.0 entry
        # stores a plain string), so only a real change of sensors stamps.
        # CONF_DIRECTION_CHANGED_AT is deliberately not set: it dates changes of the desired
        # direction, and a sensor change is none. A SoC verdict counted before the entity job
        # therefore still counts as current, and Battery direction may show its gate when
        # the rebuilt rows no longer need it. That is the conservative side, and the next
        # recount settles it.
        if _battery_sensors(data_updates) != _battery_sensors(entry.data):
            new_data[CONF_DIRECTION_FLIPPED_AT] = dt_util.utcnow().isoformat()
            ir.async_delete_issue(
                self.hass, DOMAIN, f"battery_direction_{entry.entry_id}")
            new_data.pop(CONF_DIRECTION_ISSUE_IGNORED, None)
        # Reload: the coordinator sees a new entity fingerprint and an entity job
        # replaces only the changed sensors' fields in the stored history.
        # `data=` (whole dict), not `data_updates=`: the ignore flags above must
        # be removed, which a merge cannot do.
        return self.async_update_reload_and_abort(entry, data=new_data)

    async def async_step_reconfigure_battery_swap(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A sensor moved between charge and discharge over uploaded data: the period the
        entity job rebuilds, and Battery direction (Configure) as the way to fix mixed-up
        charge and discharge. A menu, since a form has one button: go on to the
        confirmation, or cancel and save nothing."""
        return self.async_show_menu(
            step_id="reconfigure_battery_swap",
            menu_options=["reconfigure_battery_swap_confirm", "reconfigure_battery_swap_cancel"],
            description_placeholders=self._swap_placeholders,
        )

    async def async_step_reconfigure_battery_swap_cancel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason="battery_swap_cancelled")

    async def async_step_reconfigure_battery_swap_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The specific confirmation, unticked: ticked, the submit is saved as the form
        would have saved it, a changed soc_absent PATCHed first (held back from the submit,
        so Cancel sends nothing); a failed PATCH saves nothing and is shown here. The battery
        pair is checked again first: the confirmation was computed from it, and two dialogs
        open at once must not stack two changes."""
        entry = self._get_reconfigure_entry()
        if self._swap_updates is None or _battery_sensors(entry.data) != self._swap_from:
            return self.async_abort(reason="battery_swap_changed")
        errors: dict[str, str] = {}
        self._server_detail = ""
        if user_input is not None:
            if not user_input.get(_CONFIRM_SWAP):
                errors[_CONFIRM_SWAP] = "override_required"
            elif self._swap_soc_absent is not None:
                try:
                    await WoltaApiClient(
                        async_get_clientsession(self.hass)
                    ).patch_profile(entry.data[CONF_TOKEN], soc_absent=self._swap_soc_absent)
                except WoltaApiError as err:
                    _LOGGER.error("Failed to patch soc_absent: %s", err)
                    self._server_detail = _refusal(err, errors)
            if not errors:
                return self._save_reconfigure(entry, self._swap_updates)
        return self.async_show_form(
            step_id="reconfigure_battery_swap_confirm",
            data_schema=vol.Schema({
                vol.Required(_CONFIRM_SWAP, default=False): BooleanSelector(
                    BooleanSelectorConfig()),
            }),
            errors=errors,
            description_placeholders={
                **self._swap_placeholders, "server_detail": self._server_detail},
        )

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
        if self._entities_data.get(CONF_SOC):
            entry_data[CONF_SOC] = list(self._entities_data[CONF_SOC])
        # CONF_SOC_ABSENT: what the server holds, which async_step_entities has just
        # PATCHed to the user's answer when the two differed.
        for key in (
            CONF_ZONE, CONF_BATTERY_KWH, CONF_NAMEPLATE_KWH, CONF_BATTERY_KW,
            CONF_NAMEPLATE_KW, CONF_EFF,
            CONF_RESERVE_PCT, CONF_COST_SEK, CONF_PURCHASE_DATE,
            CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE, CONF_EXPORT_EXTRA_ORE,
            CONF_GRID_VAR_PCT, CONF_EXPORT_EXTRA_PCT, CONF_SOC_ABSENT,
            CONF_REDUCED_ENERGY_TAX, CONF_GRID_BENEFIT_ORE, *_CONTRACT_KEYS,
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
                CONF_GRID_VAR_PCT, CONF_EXPORT_EXTRA_PCT, CONF_REDUCED_ENERGY_TAX,
                CONF_GRID_BENEFIT_ORE, *_CONTRACT_KEYS,
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
            # Only present after the server refused a percentage without its fixed part:
            # the answer is used for this create and kept in the cache.
            fixed_in = user_input.get(CONF_GRID_VAR_ORE)
            if fixed_in is not None:
                entry_data[CONF_GRID_VAR_ORE] = fixed_in

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
                    # Always True (2026-09-15), NOT read from entry_data: the flag's
                    # only real effect on an integration row is disabling the
                    # expansion calculator - nothing is withheld from the comparison
                    # (corpus membership was never gated by it) and the series is
                    # kept regardless (streaming needs it), so a hidden False here
                    # would be an invisible dead end with no privacy upside. A
                    # linked row can't in practice carry False either - the web
                    # opt-out never saves a returning token to link with.
                    share_profile=True,
                    reserve_pct=entry_data.get(CONF_RESERVE_PCT),
                    cost_sek=entry_data.get(CONF_COST_SEK) if created_by_ha else None,
                    purchase_date=entry_data.get(CONF_PURCHASE_DATE),
                    grid_var_ore=entry_data.get(CONF_GRID_VAR_ORE),
                    export_extra_ore=entry_data.get(CONF_EXPORT_EXTRA_ORE),
                    grid_var_pct=entry_data.get(CONF_GRID_VAR_PCT),
                    export_extra_pct=entry_data.get(CONF_EXPORT_EXTRA_PCT),
                    # The grid benefit (api 0.119.0). A row that survived keeps the server's
                    # tariff and ignores this; a NEW row after a purge takes it from here, so
                    # the cache is the only copy of it. Omitted outside SE1-SE4, which the
                    # server refuses (422): a value cached from before a zone correction
                    # would block the reauth, as the energy-tax answer below.
                    grid_benefit_ore=(entry_data.get(CONF_GRID_BENEFIT_ORE)
                                      if _benefit_zone(entry_data[CONF_ZONE]) else None),
                    # The electricity contract that applied today (api 0.119.0) and the markup.
                    # Same rule as the grid benefit above: a surviving row keeps the server's
                    # contract and its history, a new row takes today's contract from here, and
                    # outside SE1-SE4 the contract is omitted. The markup is never sent with a
                    # fixed price. The time periods and the contract history are not cached and
                    # not sent: they are wolta.se's, and a new row starts without them.
                    **_contract_profile_kwargs(entry_data[CONF_ZONE], entry_data),
                    nameplate_kwh=entry_data.get(CONF_NAMEPLATE_KWH),
                    nameplate_kw=entry_data.get(CONF_NAMEPLATE_KW),
                    client_plant_id=entry_data.get(CONF_PLANT_ID) or reauth_entry.entry_id,
                    # Entries from before v0.29.0 have no stored field -> None is
                    # OMITTED by the client, and the backend (>= 0.79.0) preserves the
                    # stored value on omission instead of clobbering it to null. Since
                    # v0.50.0 the value is mirrored from the server (coordinator
                    # _PROFILE_SYNC_KEYS), and the backend (>= 0.110.1) only uses it to
                    # fill an empty column - it never rewrites the owner's declaration.
                    control_system=entry_data.get(CONF_CONTROL_SYSTEM),
                    control_system_name=entry_data.get(CONF_CONTROL_SYSTEM_NAME),
                    # The SoC answer survives the new profile. An entry from before
                    # v0.50.0 has none -> None is omitted by the client, so whatever an
                    # older server row holds is kept.
                    soc_absent=entry_data.get(CONF_SOC_ABSENT),
                    # The energy-tax answer (None = never answered, omitted by the client).
                    # The backend (>= 0.118.0) merges the tariff key by key on a re-onboarding
                    # of a known plant, so an omitted answer keeps the stored one.
                    # Omitted outside SE1-SE3: a `true` cached from before a zone correction
                    # would be refused by the server (422) and block the reauth.
                    reduced_energy_tax=(
                        entry_data.get(CONF_REDUCED_ENERGY_TAX)
                        if str(entry_data[CONF_ZONE]).upper() in _REDUCED_TAX_ZONES else None
                    ),
                )
            except WoltaPlantClaimed:
                # A wolta.se account owns the plant (Wolta security review 2026-10-16): the
                # backend no longer hands out a new token for our plant id alone, so the
                # owner creates one on the plant page and pastes it in the next step.
                return await self.async_step_reauth_new_token()
            except WoltaApiError as err:
                _LOGGER.error("Reauth failed – could not create Wolta profile: %s", err)
                if _is_grid_fixed_error(err):
                    # The old profile is gone, so Configure cannot open to fix this (it reads
                    # the profile first): the reauth form itself asks for the fixed part.
                    self._reauth_ask_fixed = True
                    errors["base"] = "grid_fixed_required"
                elif getattr(err, "status", None) == 422:
                    errors["base"] = "invalid_input"
                else:
                    errors["base"] = "cannot_connect"
            else:
                data_updates = {
                    CONF_TOKEN: new_token,
                    # Persist the id too: on a pre-v0.16.0 entry it was just derived from
                    # entry_id, and a later reauth must send the SAME value to land on the
                    # same plant row.
                    CONF_PLANT_ID: entry_data.get(CONF_PLANT_ID) or reauth_entry.entry_id,
                }
                if fixed_in is not None:
                    data_updates[CONF_GRID_VAR_ORE] = fixed_in
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=data_updates)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=(
                vol.Schema({vol.Required(CONF_GRID_VAR_ORE): _number_selector(
                    min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh")})
                if self._reauth_ask_fixed else vol.Schema({})
            ),
            errors=errors,
        )

    async def async_step_reauth_new_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reauth for a plant a wolta.se account owns: paste a token the owner created.

        The pasted token is checked by stamping THIS entry's plant id on its row via adopt:
        idempotent when the token belongs to the same plant, 409 when our id already sits on
        another row - i.e. the token is for a different plant, and streaming into it would
        put this home's data on someone else's plant."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if _is_read_link(user_input["profile_input"]):
                errors["profile_input"] = "link_is_read_link"
            else:
                token = extract_token(user_input["profile_input"])
                entry = self._get_reauth_entry()
                plant_id = entry.data.get(CONF_PLANT_ID) or entry.entry_id
                try:
                    client = WoltaApiClient(async_get_clientsession(self.hass))
                    await client.adopt_profile(token, client_plant_id=plant_id)
                except WoltaAuthError:
                    errors["profile_input"] = "invalid_token"
                except WoltaApiError as err:
                    if getattr(err, "status", None) == 409:
                        errors["profile_input"] = "token_other_plant"
                    else:
                        self._server_detail = _refusal(err, errors)
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_TOKEN: token, CONF_PLANT_ID: plant_id}
                    )
        return self.async_show_form(
            step_id="reauth_new_token",
            data_schema=vol.Schema(
                {
                    vol.Required("profile_input"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    )
                }
            ),
            errors=errors,
            description_placeholders={"server_detail": self._server_detail},
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
    _SEC_TARIFFS: (CONF_GRID_VAR_ORE, CONF_GRID_VAR_PCT,
                   CONF_RETAIL_PRICE_MODEL, CONF_RETAIL_FIXED_ORE, CONF_SURCHARGE_ORE,
                   CONF_GRID_BENEFIT_ORE, CONF_EXPORT_EXTRA_ORE, CONF_EXPORT_EXTRA_PCT,
                   CONF_REDUCED_ENERGY_TAX),
}
# The energy-tax answer is tri-state on the server (null / false / true) and a boolean selector
# can only give true/false, so the form uses a list select whose values map to those three.
_TAX_NOT_SET = "not_set"
_TAX_CHOICES = (_TAX_NOT_SET, "no", "yes")
# Zones where the question is asked. The deduction is Swedish and no affected municipality lies
# in SE4 (spec 2026-10-26-inkopspris, boio.se/el/elomrade), so SE4 and other countries never see
# it; the wolta.se form hides it the same way.
_REDUCED_TAX_ZONES = ("SE1", "SE2", "SE3")
# Zones with a grid-benefit field. The server refuses the benefit fields outside SE1-SE4
# (tariff.check_grid_benefit_zone, 422), so the form does not offer them elsewhere. The same
# goes for the electricity contract (the contract type and the fixed price).
_BENEFIT_ZONES = ("SE1", "SE2", "SE3", "SE4")
# Span of the grid-benefit field, öre/kWh excl. VAT: the server's own (api 0.119.0,
# simulate/tariff.py); a value outside it is refused with 422.
_BENEFIT_MIN_ORE = -20.0
_BENEFIT_MAX_ORE = 50.0


def _benefit_zone(zone: Any) -> bool:
    """Whether the owner's grid benefit can be given in this zone."""
    return str(zone).upper() in _BENEFIT_ZONES


def _tax_to_choice(value: Any) -> str:
    """Server tri-state → select value."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return _TAX_NOT_SET


def _choice_to_tax(choice: Any) -> bool | None:
    """Select value → PATCH value (not_set clears the answer with null)."""
    return {"yes": True, "no": False}.get(choice)
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


# The tariff fields the kept /profile/prices answer was priced with: the import side and the
# export side (grid benefit, retailer's terms). Saving any of them drops the kept answer and
# fetches a fresh one.
_TARIFF_PRICE_KEYS = frozenset(
    {
        CONF_GRID_VAR_ORE, CONF_GRID_VAR_PCT, CONF_SURCHARGE_ORE, CONF_REDUCED_ENERGY_TAX,
        CONF_GRID_BENEFIT_ORE,
        CONF_EXPORT_EXTRA_ORE, CONF_EXPORT_EXTRA_PCT,
        # The contract decides what the owner pays per quarter.
        *_CONTRACT_KEYS,
    }
)


class WoltaOptionsFlow(OptionsFlow):
    """Handle options for an existing Wolta config entry (shared-profile edit)."""

    def __init__(self) -> None:
        """Initialise the options flow."""
        self._server: dict[str, Any] | None = None  # fresh GET /profile snapshot
        self._direction_target: list[intervals.Interval] | None = None  # the chosen change
        # The desired direction the change was chosen from: another dialog may change it.
        self._direction_from: list[intervals.Interval] | None = None
        # Whether the confirmation shown said stored rows change (direction_confirm) or not
        # (direction_confirm_unstored); a submit that would do the other aborts.
        self._direction_shown_stored: bool | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Meny (beslut 2026-08-25): options växte från ETT formulär till flera
        åtgärder. Kostar ett klick för den som bara ska ändra ett värde, men ger
        varje ny åtgärd en plats utan att formuläret sväller."""
        options = ["settings"]
        # Batteriets riktning ändrar lagrad historik, så den har ett eget steg i stället
        # för en ruta bland inställningarna. Inte i visningsläget: där mappar Woltas
        # koppling fälten, och den här klienten laddar inte upp något.
        if not self.config_entry.data.get(CONF_VIEW_ONLY):
            options.append("battery_direction")
        options.append("account_link")
        # Zonrättelsen visas bara när det FINNS något att byta till: ett land med en enda
        # zon (t.ex. FI) skulle annars få en meny-post som leder till ett formulär med ett
        # enda alternativ - det redan valda.
        if len(_zone_group(self.config_entry.data.get(CONF_ZONE, ""))) > 1:
            options.append("zone_correction")
        return self.async_show_menu(step_id="init", menu_options=options)

    def _benefit_asked(self) -> bool:
        """Whether the grid-benefit fields apply: the plant's zone (the server's, else the
        cached one) is SE1-SE4."""
        zone = (self._server or {}).get(CONF_ZONE) or self.config_entry.data.get(CONF_ZONE) or ""
        return _benefit_zone(zone)

    def _tax_asked(self) -> bool:
        """Whether the energy-tax question applies: the plant's zone (the server's, else the
        cached one) is SE1-SE3."""
        zone = (self._server or {}).get(CONF_ZONE) or self.config_entry.data.get(CONF_ZONE) or ""
        return str(zone).upper() in _REDUCED_TAX_ZONES

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
                    new_data = {**entry.data, CONF_ZONE: chosen}
                    if str(chosen).upper() not in _REDUCED_TAX_ZONES:
                        # The energy-tax answer exists only in SE1-SE3 and the server
                        # refuses `true` anywhere else (422), so a cached answer from the old
                        # zone must not travel with the next reauth.
                        new_data.pop(CONF_REDUCED_ENERGY_TAX, None)
                    self.hass.config_entries.async_update_entry(entry, data=new_data)
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

    # ------------------------------------------------------------------
    # Batteriets riktning
    # ------------------------------------------------------------------

    def _direction_pending(self) -> bool:
        """A direction change is still on its way to the stored rows (a direction job, or
        desired ≠ applied). Another change now would be judged from a grade of the old
        direction, so the step waits, like the repair."""
        coordinator = getattr(self.config_entry, "runtime_data", None)
        pending = getattr(coordinator, "direction_change_pending", None)
        return bool(pending()) if callable(pending) else False

    def _direction_verdict(self) -> Verdict | None:
        """The SoC verdict of the grade the coordinator holds. None while no grade is loaded:
        after a restart before the first fetch, with the entry not loaded, or with results
        that carry no grade. A grade without the block is a verdict with state None."""
        coordinator = getattr(self.config_entry, "runtime_data", None)
        results = getattr(getattr(coordinator, "data", None), "results", None)
        betyg = results.get("betyg") if isinstance(results, dict) else None
        if not isinstance(betyg, dict):
            return None
        return read_verdict(betyg, self.config_entry.data.get(CONF_DIRECTION_CHANGED_AT))

    def _desired_direction(self) -> list[intervals.Interval]:
        return intervals.normalize(desired_invert_intervals(self.config_entry.data))

    async def async_step_battery_direction(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Swap charged and discharged in the stored history, for a period the owner chooses
        (it replaces the invert box in Settings). What is reversed now and the server's
        SoC verdict are shown first; only the choices that change something are offered,
        and each leads to a confirmation that names the period."""
        if self._direction_pending():
            return self.async_abort(reason="direction_pending")
        verdict = self._direction_verdict()
        if verdict is None:
            return self.async_abort(reason="results_not_loaded")
        desired = self._desired_direction()
        options = []
        if desired != [(None, None)]:
            options.append("direction_flip_all")
        options.append("direction_flip_from")
        if desired:
            options.append("direction_reset")
        text = _direction_text(self.hass.config.language)
        return self.async_show_menu(
            step_id="battery_direction", menu_options=options,
            description_placeholders={"desired": _desired_text(text, desired),
                                      "verdict": _verdict_text(text, verdict)})

    async def _direction_choose(self, target: list[intervals.Interval]) -> ConfigFlowResult:
        """A choice made: remember the target and the desired direction it was chosen from,
        and go on to the confirmation."""
        self._direction_from = self._desired_direction()
        self._direction_target = target
        self._direction_shown_stored = None
        return await self.async_step_direction_confirm()

    async def async_step_direction_flip_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._direction_choose([(None, None)])

    async def async_step_direction_reset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._direction_choose([])

    async def async_step_direction_flip_from(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Swap from the start of a local day onward: what is swapped after it stops being
        swapped, the rest is swapped. The first whole UTC hour at or after local midnight in
        HA's time zone (the direction job swaps whole hours): in a zone whose offset is not
        whole hours, rounding down would reach into the day before."""
        errors: dict[str, str] = {}
        if user_input is not None:
            day = dt_util.parse_date(str(user_input.get(_DIRECTION_FLIP_FROM)))
            if day is None:
                errors[_DIRECTION_FLIP_FROM] = "invalid_date"
            else:
                midnight = dt_util.as_utc(dt_util.start_of_local_day(day))
                start = midnight.replace(minute=0, second=0, microsecond=0)
                if start < midnight:
                    start += timedelta(hours=1)
                return await self._direction_choose(
                    intervals.sym_diff(self._desired_direction(), [(start, None)]))
        return self.async_show_form(
            step_id="direction_flip_from",
            data_schema=vol.Schema(
                {vol.Required(_DIRECTION_FLIP_FROM): DateSelector(DateSelectorConfig())}),
            errors=errors,
        )

    async def async_step_direction_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The specific confirmation every choice leads to: the stored period that changes
        and how many days, never a confirmation without a period. Two texts, by what the
        coordinator says about the effect (direction_effect):

        - direction_confirm: the windows of stored rows that swap, as local periods;
        - direction_confirm_unstored: nothing stored changes (the change lies outside what
          the server holds from this client, or nothing has been uploaded yet), only the
          rows still to come follow the new setting.

        Rows are uploaded but the server's data_since is not known yet (the profile poll
        brings it shortly): the period cannot be named, so the step aborts and asks to try
        again. Against a current ok/verified/reversed verdict the server refuses the swap
        without override, so the form then asks for it, unticked. All of it is checked
        again on submit: two dialogs open at once must not stack two changes."""
        entry = self.config_entry
        if self._direction_from is None or self._direction_from != self._desired_direction():
            return self.async_abort(reason="direction_changed")
        if self._direction_pending():
            return self.async_abort(reason="direction_pending")
        verdict = self._direction_verdict()
        target = self._direction_target
        if verdict is None or target is None:
            return self.async_abort(reason="results_not_loaded")
        coordinator = entry.runtime_data
        effect = coordinator.direction_effect(target)
        if effect is None:
            if coordinator.has_uploaded():
                return self.async_abort(reason="direction_period_unknown")
            effect = []  # nothing uploaded: the direction job has nothing stored to swap
        if (user_input is not None and self._direction_shown_stored is not None
                and bool(effect) != self._direction_shown_stored):
            # The form said stored rows change and now none would, or the other way round:
            # what was confirmed is not what would happen.
            return self.async_abort(reason="direction_changed")
        gated = requires_override(verdict)
        errors: dict[str, str] = {}
        if user_input is not None:
            # The field exists only when the gate was shown, so a tick means it was.
            override = bool(user_input.get(_DIRECTION_OVERRIDE))
            if gated and not override:
                errors[_DIRECTION_OVERRIDE] = "override_required"
            else:
                await apply_direction_target(
                    self.hass, entry, target, origin="manual", override=override)
                # The options are not touched: the change lives in entry.data.
                return self.async_create_entry(title="", data=dict(entry.options))

        placeholders = {"periods": "", "days": "0", "gate": ""}
        step_id = "direction_confirm" if effect else "direction_confirm_unstored"
        self._direction_shown_stored = bool(effect)
        if effect:
            placeholders["periods"] = ", ".join(_local_period(s, e) for s, e in effect)
            placeholders["days"] = str(_local_days(effect))
        schema: dict[Any, Any] = {}
        if gated:
            text = _direction_text(self.hass.config.language)
            if verdict.state == "reversed":
                gate = text["gate_reversed"]
                # Only while the repair is there to be opened: the registry, not the verdict,
                # since the coordinator also keeps it down for some forms and while a flip
                # is pending, and an ignored one is not shown.
                issue = ir.async_get(self.hass).async_get_issue(
                    DOMAIN, f"{_ISSUE_BATTERY_DIRECTION}_{entry.entry_id}")
                if issue is not None and issue.is_fixable and issue.dismissed_version is None:
                    gate += text["gate_repair"]
            else:
                gate = text["gate_swap" if effect else "gate_unstored"]
            placeholders["gate"] = gate.format(verdict=_verdict_text(text, verdict))
            schema[vol.Required(_DIRECTION_OVERRIDE, default=False)] = BooleanSelector(
                BooleanSelectorConfig())
        return self.async_show_form(
            step_id=step_id, data_schema=vol.Schema(schema), errors=errors,
            description_placeholders=placeholders)

    async def async_step_direction_confirm_unstored(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_direction_confirm(user_input)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the options form."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        server_detail = ""   # the server's words for a refusal without a rule here
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
                        value = sec_input[key]
                        if key == CONF_REDUCED_ENERGY_TAX:
                            value = _choice_to_tax(value)
                        flat[key] = value

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
                # Även på "base": fältfelet sitter på ett fält INUTI section("battery"),
                # och renderar frontend inte fältfel i sektioner ser användaren bara ett
                # formulär som vägrar spara utan att säga varför.
                errors["base"] = "battery_pair_incomplete"

            # cost_scope (backend 2026-07-18): "plant" = the scalar price covers the
            # WHOLE plant (solar + battery; wolta.se guide profiles adopted into HA).
            # Our field is explicitly battery-only, so it is hidden from the form for
            # those profiles (the price is edited on wolta.se where the labels match) –
            # and must be skipped in the diff too: an absent optional field otherwise
            # means "actively cleared" → PATCH null would wipe the plant price on
            # every save. Older backends never send cost_scope → False → unchanged.
            plant_scoped_cost = self._server.get("cost_scope") == "plant"
            # The energy-tax select only exists in SE1-SE3 (see _REDUCED_TAX_ZONES). Where it
            # is hidden its absence is not "not set": the stored answer must survive, so the
            # diff skips the key (same reasoning as plant_scoped_cost above).
            tax_asked = self._tax_asked()
            # Same for the grid-benefit field and the contract fields, which only exist in
            # SE1-SE4. The time periods, their holiday rules, the contract history and the sale
            # side's history (export_events, grid_benefit_events) are not in the form at all and
            # are never part of the diff: they are edited on wolta.se. The sale fields here are
            # today's values the server mirrors from its history, so an untouched save sends
            # nothing, and a change becomes an entry dated today on the server.
            benefit_asked = self._benefit_asked()

            # Diff against the SERVER snapshot – only changed fields are PATCHed.
            patch_fields: dict[str, Any] = {}
            for sec, fields in _SECTION_FIELDS.items():
                for key in fields:
                    if key == CONF_COST_SEK and plant_scoped_cost:
                        continue
                    if key == CONF_REDUCED_ENERGY_TAX and not tax_asked:
                        continue
                    if key in (CONF_GRID_BENEFIT_ORE, *_CONTRACT_KEYS) and not benefit_asked:
                        continue
                    val = flat.get(key)
                    server_val = self._server.get(key)
                    if key == CONF_RETAIL_PRICE_MODEL and val == _RETAIL_NOT_STATED:
                        # Only offered while today's contract has no type: nothing to send.
                        continue
                    if (key == CONF_RETAIL_PRICE_MODEL and val
                            and _contract_unanswered(self._server)):
                        # The form offered "not stated", so any type chosen is an answer, the
                        # default quarter-hour price included (code review C1).
                        patch_fields[key] = val
                        continue
                    if key in _SELECT_DEFAULTS:
                        # Always filled in the form, and "not set" on the server is the default.
                        default = _SELECT_DEFAULTS[key]
                        val = val or default
                        if val != (server_val or default):
                            patch_fields[key] = val
                        continue
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

            # A percentage of spot needs the fixed part of the grid fee (0 is an answer): the
            # server refuses the PATCH otherwise (422), so the form says it first and sends
            # nothing. Mirrors the server's own condition: only a save that TOUCHES one of the
            # two fields is checked, so a legacy profile with just the percentage can still
            # save an unrelated change (the grid_fixed_missing repair asks for the fixed part).
            if (
                (CONF_GRID_VAR_ORE in patch_fields or CONF_GRID_VAR_PCT in patch_fields)
                and flat.get(CONF_GRID_VAR_PCT) is not None
                and flat.get(CONF_GRID_VAR_ORE) is None
            ):
                errors["base"] = "grid_fixed_required"

            # The grid benefit and the retailer's terms are two halves of what used to be one
            # number (export_extra_ore), and a legacy value in the retailer's field is often the
            # grid benefit. The owner who touches either sees and confirms both, so both go in
            # the PATCH. An empty retailer field next to a grid benefit is answered 0 (the
            # retailer pays plain spot), which is what the server counts a missing one as. With a
            # sale history on the server the unchanged half equals today's entry and adds no
            # entry (tariff.apply_sale_scalar_change); the changed half adds one dated today.
            if CONF_GRID_BENEFIT_ORE in patch_fields or CONF_EXPORT_EXTRA_ORE in patch_fields:
                benefit_in = flat.get(CONF_GRID_BENEFIT_ORE) if benefit_asked else None
                terms_in = flat.get(CONF_EXPORT_EXTRA_ORE)
                if benefit_in is not None:
                    patch_fields[CONF_GRID_BENEFIT_ORE] = benefit_in
                    patch_fields[CONF_EXPORT_EXTRA_ORE] = 0.0 if terms_in is None else terms_in
                elif terms_in is not None:
                    patch_fields[CONF_EXPORT_EXTRA_ORE] = terms_in

            # The server's grid-benefit combination rule, said before sending. It is checked on
            # the state after the save and only when the save touches one of its fields, like the
            # server (tariff.patch_override_updates): a legacy profile with just
            # a percentage can still save an unrelated change, and the repair
            # grid_benefit_fixed_missing asks for the fixed part. Shown at grid_benefit_ore, the
            # field that is missing, and ALSO on base (same code), exactly like
            # battery_pair_incomplete above: the field sits inside a section, which the frontend
            # may not render field errors for, and a form that refuses to save without saying
            # why is the worst outcome. The texts name the field and the section.
            if benefit_asked:
                benefit_after = flat.get(CONF_GRID_BENEFIT_ORE)
                pct_after = flat.get(CONF_EXPORT_EXTRA_PCT)
                if (
                    {CONF_GRID_BENEFIT_ORE, CONF_EXPORT_EXTRA_PCT} & patch_fields.keys()
                    and pct_after not in (None, 0) and benefit_after is None
                ):
                    errors[CONF_GRID_BENEFIT_ORE] = errors["base"] = "grid_benefit_fixed_required"

            # A time-period list needs its base price, the price outside the periods (0 is an
            # answer): the server refuses clearing the base under a list (check_periods, 422), so
            # the form says it first, at the cleared field and on base. The lists themselves are
            # never sent from here; they are the server's (edited on wolta.se), so the snapshot is
            # what decides whether one exists.
            for base_key, list_key in ((CONF_GRID_VAR_ORE, KEY_GRID_FEE_PERIODS),
                                       (CONF_GRID_BENEFIT_ORE, KEY_GRID_BENEFIT_PERIODS)):
                if (base_key in patch_fields and patch_fields[base_key] is None
                        and self._server.get(list_key)):
                    errors[base_key] = errors["base"] = "periods_require_base"

            # The grid benefit history (Tillägg 5) needs a base price in today's entry, so the
            # server refuses clearing it while a history exists (GRID_BENEFIT_EVENTS_INVALID_MSG,
            # checked before the periods' rule, which is why it replaces that error here). The
            # history is removed or corrected on wolta.se. The retailer's terms have no such rule:
            # cleared, they are plain spot from today.
            if (CONF_GRID_BENEFIT_ORE in patch_fields
                    and patch_fields[CONF_GRID_BENEFIT_ORE] is None
                    and self._server.get(KEY_GRID_BENEFIT_EVENTS)):
                errors[CONF_GRID_BENEFIT_ORE] = errors["base"] = (
                    "grid_benefit_history_requires_base")

            # A plant built in stages (set on wolta.se) has the first stage's purchase date: the
            # server refuses a later date and a cleared one (calibration.patch_profile_core), so
            # the form says it first, at the field and on base. A later purchase, such as a
            # battery bought years after the solar panels, is a stage of its own (2026-10-29).
            if CONF_PURCHASE_DATE in patch_fields:
                first_stage = _first_stage_date(self._server)
                if first_stage is not None:
                    typed = dt_util.parse_date(str(patch_fields[CONF_PURCHASE_DATE] or ""))
                    if typed is None or typed > first_stage:
                        errors[CONF_PURCHASE_DATE] = errors["base"] = (
                            "purchase_date_after_stages")

            # The electricity contract, said before sending (field AND base, like the above). A
            # change applies from today: the server adds an entry dated today to the contract
            # history (or corrects today's), and the history is never rewritten from here. Only a
            # save that touches one of the contract's fields is checked, like the server.
            dropped: set[str] = set()
            if benefit_asked:
                model_after = flat.get(CONF_RETAIL_PRICE_MODEL)
                if model_after in (None, "", _RETAIL_NOT_STATED):
                    # Not stated is priced as a quarter-hour price, by the server too.
                    model_after = DEFAULT_RETAIL_PRICE_MODEL
                # A fixed price carries no markup and only a fixed price carries a fixed price: the
                # server drops the one that no longer applies when the contract changes type. The
                # form still shows the old value, so a cleared one is not sent back and the cache
                # follows. A NEW value for a field that does not apply is refused below.
                dropped.add(CONF_SURCHARGE_ORE if model_after == "fixed" else CONF_RETAIL_FIXED_ORE)
                for key in dropped:
                    if key in patch_fields and patch_fields[key] is None:
                        del patch_fields[key]
                if {CONF_RETAIL_PRICE_MODEL, CONF_RETAIL_FIXED_ORE,
                        CONF_SURCHARGE_ORE} & patch_fields.keys():
                    if model_after == "fixed" and flat.get(CONF_RETAIL_FIXED_ORE) is None:
                        errors[CONF_RETAIL_FIXED_ORE] = errors["base"] = "retail_fixed_invalid"
                    # The other direction: a value typed for a contract it does not belong to. An
                    # unchanged stale value is not in the PATCH and passes; surcharge_ore is never
                    # sent with a fixed price.
                    if model_after != "fixed" and patch_fields.get(CONF_RETAIL_FIXED_ORE) is not None:
                        errors[CONF_RETAIL_FIXED_ORE] = errors["base"] = "retail_fixed_invalid"
                    if model_after == "fixed" and patch_fields.get(CONF_SURCHARGE_ORE) is not None:
                        errors[CONF_SURCHARGE_ORE] = errors["base"] = "retail_fixed_surcharge"

            if patch_fields and not errors:
                try:
                    await client.patch_profile(token, **patch_fields)
                except WoltaApiError as err:
                    _LOGGER.error("Failed to patch Wolta profile: %s", err)
                    # The same refusal from the server (a cache that is out of step with it)
                    # reads as the same fault, not as a connection problem.
                    benefit_err = _grid_benefit_error(err)
                    rule_err = _server_rule_error(err)
                    if benefit_err is not None:
                        # Field AND base, as for the prechecks above. A zone refusal can come
                        # while the field is hidden (a cache out of step), where base is the
                        # only place the owner can read it.
                        errors[CONF_GRID_BENEFIT_ORE] = errors["base"] = benefit_err
                    elif rule_err is not None:
                        errors[_server_rule_field(rule_err, patch_fields)] = rule_err
                        errors["base"] = rule_err
                    elif _is_grid_fixed_error(err):
                        errors["base"] = "grid_fixed_required"
                    else:
                        server_detail = _refusal(err, errors)

            if not errors:
                # Datumförslaget ur statistiken är ett ENGÅNGSerbjudande: formuläret har
                # nu visat det, så nyckeln tas bort. Utan det hade varje senare sparning
                # föreslagit datumet igen och därmed återuppväckt ett datum användaren
                # medvetet rensade.
                prefill_offered = CONF_PREFILL_PURCHASE_DATE in entry.data
                if patch_fields or prefill_offered:
                    new_data = dict(entry.data)
                    for key, val in patch_fields.items():
                        if val is None:
                            new_data.pop(key, None)
                        else:
                            new_data[key] = val
                    if CONF_RETAIL_PRICE_MODEL in patch_fields:
                        # What the server drops with a change of contract type (see `dropped`
                        # above): the fixed price leaving `fixed`, the markup going to it.
                        for key in dropped:
                            new_data.pop(key, None)
                    new_data.pop(CONF_PREFILL_PURCHASE_DATE, None)
                    self.hass.config_entries.async_update_entry(entry, data=new_data)

                coordinator = getattr(entry, "runtime_data", None)
                prices = getattr(coordinator, "prices", None)
                if prices is not None and _TARIFF_PRICE_KEYS & patch_fields.keys():
                    # The purchase-price sensor shows what THIS tariff costs: refresh it now
                    # instead of at the next 15-minute poll. A request on top of the poll, and
                    # only if the owner changed the tariff; it fetches /profile/prices only when
                    # that sensor is enabled. A price problem never fails the save (see
                    # _refresh_prices).
                    # Scheduled, not awaited: the fetch can take up to the lookup timeout and
                    # the dialog must not wait for it. A background task, so Home Assistant
                    # does not wait for it at startup.
                    # The entry owns the task, so unloading it cancels the refresh. The kept
                    # answer was priced with the old tariff: forget it first, so a failing
                    # refresh cannot show it.
                    prices.async_forget_purchase()
                    entry.async_create_background_task(
                        self.hass, _refresh_prices(prices), "wolta_options_price_refresh")
                if patch_fields and coordinator is not None:
                    # Trigger recompute so grade + economy reflect the new values.
                    # PATCH cleared the server-side cooldown, so a 202 is expected;
                    # swallow 429 gracefully anyway.
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
        battery_pending = srv.get(KEY_BATTERY_STATUS) in (
            BATTERY_STATUS_PENDING, BATTERY_STATUS_NEEDS_INPUT
        )
        kwh_selector = _number_selector(min_val=MIN_BATTERY_KWH, max_val=500.0, step=0.5, unit="kWh")
        kw_selector = _number_selector(min_val=MIN_BATTERY_KW, max_val=100.0, step=0.1, unit="kW")
        eff_selector = _number_selector(min_val=0.5, max_val=1.0, step=0.01)
        # eff är Required i BÅDA grenarna – bara KAPACITETSPARET går Optional. Backend
        # lagrar alltid eff (0.9) på en deklarerad rad, så fältet är förifyllt även
        # medan paret mäts; det finns ingen mätning att skriva över. Som Optional hade
        # det gått att rensa → PATCH eff: null → backend 422 ("eff kan inte tas bort"),
        # som i dialogen bara syns som cannot_connect.
        #
        # `or DEFAULT` och inte `srv.get(key, DEFAULT)`: servern kan skicka nyckeln MED
        # null (inte bara utelämna den), och då ger tvåargumentsformen None →
        # vol.Required(default=None) går inte att spara alls.
        eff_row = (vol.Required(CONF_EFF, default=srv.get(CONF_EFF) or DEFAULT_EFF),
                   eff_selector)
        if battery_pending:
            kwh_row = _opt(CONF_BATTERY_KWH, kwh_selector)
            kw_row = _opt(CONF_BATTERY_KW, kw_selector)
        else:
            kwh_row = (vol.Required(CONF_BATTERY_KWH,
                                    default=srv.get(CONF_BATTERY_KWH) or DEFAULT_BATTERY_KWH),
                       kwh_selector)
            kw_row = (vol.Required(CONF_BATTERY_KW,
                                   default=srv.get(CONF_BATTERY_KW) or DEFAULT_BATTERY_KW),
                      kw_selector)
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
        ore_selector = _number_selector(min_val=0.0, max_val=500.0, step=0.1, unit="öre/ct per kWh")
        # The fixed price exists in SE1-SE4 only, where the unit is öre.
        se_ore_selector = _number_selector(min_val=0.0, max_val=500.0, step=0.1, unit="öre per kWh")

        def _select_row(key: str, known: tuple[str, ...], default: str, translation_key: str,
                        mode: SelectSelectorMode, *, not_stated: bool = False) -> tuple[Any, Any]:
            """A select that always carries a value: the server's, else the default. A value this
            version does not know (a newer server) is offered as one more option, its raw key as
            its label: the select validates against its options, so without it the injected value
            would be refused and no save could pass. Untouched, it is sent back unchanged and the
            diff finds nothing to PATCH. With `not_stated` the select leads with "not stated" and
            starts on it (today's contract has no type; see _RETAIL_NOT_STATED)."""
            if not_stated:
                current = _RETAIL_NOT_STATED
                options = [_RETAIL_NOT_STATED, *known]
            else:
                current = srv.get(key) or default
                options = list(known) if current in known else [*known, current]
            return (vol.Optional(key, default=current),
                    SelectSelector(SelectSelectorConfig(
                        options=options, mode=mode, translation_key=translation_key)))
        contract_asked = self._benefit_asked()

        # The grid fee (the base price outside any time periods) and its percentage; the
        # electricity contract that applies today with the supplier's markup (spec
        # 2026-10-27-marknadsanpassning). The time periods and the contract history are shown as
        # a summary in the description and edited on wolta.se.
        tariff_rows = [
            _opt(CONF_GRID_VAR_ORE, ore_selector),
            _opt(CONF_GRID_VAR_PCT, _number_selector(min_val=0.0, max_val=100.0, step=0.01, unit="% of spot")),
        ]
        if contract_asked:
            tariff_rows += [
                # default (not suggested_value), like the energy-tax select: it always carries a
                # value, so the form always submits one. While today's contract has no type (no
                # stored type, or a history whose today's entry has none) it is "not stated" (an
                # untouched save sends nothing); otherwise the server's type, or the quarter-hour
                # price when the server mirrors none.
                _select_row(CONF_RETAIL_PRICE_MODEL, RETAIL_PRICE_MODELS,
                            DEFAULT_RETAIL_PRICE_MODEL, CONF_RETAIL_PRICE_MODEL,
                            SelectSelectorMode.LIST, not_stated=_contract_unanswered(srv)),
                _opt(CONF_RETAIL_FIXED_ORE, se_ore_selector),
            ]
        tariff_rows.append(_opt(CONF_SURCHARGE_ORE, ore_selector))
        if contract_asked:
            # The grid benefit first, then the retailer's terms next to it (spec
            # 2026-10-26-forsaljningspris): the owner who enters the benefit sees the retailer
            # value and confirms it.
            tariff_rows.append(_opt(CONF_GRID_BENEFIT_ORE, _number_selector(
                min_val=_BENEFIT_MIN_ORE, max_val=_BENEFIT_MAX_ORE, step=0.1, unit="öre per kWh")))
        tariff_rows += [
            _opt(CONF_EXPORT_EXTRA_ORE, _number_selector(min_val=-200.0, max_val=500.0, step=0.1, unit="öre/ct per kWh")),
            _opt(CONF_EXPORT_EXTRA_PCT, _number_selector(min_val=-100.0, max_val=100.0, step=0.01, unit="% of spot")),
        ]
        if self._tax_asked():
            # default (not suggested_value): the select always carries a value, "not set"
            # included, so the form always submits one and an untouched form changes nothing.
            tariff_rows.append((
                vol.Optional(CONF_REDUCED_ENERGY_TAX,
                             default=_tax_to_choice(srv.get(CONF_REDUCED_ENERGY_TAX))),
                SelectSelector(SelectSelectorConfig(
                    options=list(_TAX_CHOICES),
                    mode=SelectSelectorMode.LIST,
                    translation_key=CONF_REDUCED_ENERGY_TAX)),
            ))
        tariffs_schema = vol.Schema(dict(tariff_rows))

        schema = vol.Schema({
            vol.Required(_SEC_BATTERY): section(battery_schema, {"collapsed": False}),
            vol.Required(_SEC_ECONOMY): section(economy_schema, {"collapsed": False}),
            # Open when the fixed-part error is shown: base errors render at the top of the
            # form, and the field they ask for sits in this section. Also open for an error on any
            # field inside it (the grid benefit, the base prices, the contract).
            vol.Required(_SEC_TARIFFS): section(
                tariffs_schema,
                {"collapsed": not (errors.get("base") == "grid_fixed_required"
                                   or any(key in errors
                                          for key in _SECTION_FIELDS[_SEC_TARIFFS]))}),
        })
        # Re-show after an error keeps what the user typed (suggested values, section by
        # section), so a refused save does not send them back to the stored values. A field
        # the user cleared is absent from user_input and re-shows empty, not as the stored value.
        if errors and user_input is not None:
            schema = self.add_suggested_values_to_schema(
                schema, _with_cleared_fields_empty(schema, user_input))
        # The time periods and the contract history are shown, never edited (spec
        # 2026-10-27-marknadsanpassning, Tillägg 2 and 4): a short summary in the description,
        # empty when the plant has none. Always passed, so the description never shows a raw
        # placeholder; server_detail too, for the server_rejected error text.
        summary = _web_summary(self.hass.config.language, srv)
        return self.async_show_form(
            step_id="settings", data_schema=schema, errors=errors,
            description_placeholders={"web_summary": f"\n\n{summary}" if summary else "",
                                      "server_detail": server_detail})
