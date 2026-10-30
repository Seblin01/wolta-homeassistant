"""Repair flows for the Wolta integration.

The backend measures the grade-affecting battery parameters from the actual meter flows and,
when they clearly disagree with what's configured, the coordinator raises a fixable repair.
These flows adopt the measured value so the grade stays fair without the user needing to know
nameplate-vs-usable, AC-vs-DC, etc.

All three flows open on a menu — **adopt the measured value** or **ignore and keep my value** —
so a plant with spurious sensor jumps (which can bias any of the three measurements) is never
forced to accept an implausible figure. Ignoring is persisted per parameter and cleared on adopt.

The adopt UX differs per parameter:
- capacity: one-click adopt (a confident estimate of usable kWh) and CLEAR the reserve (the
  measurement already excludes it → don't reduce twice).
- efficiency: one-click adopt (round-trip out/in is a true measurement, not a bound).
- power: an EDITABLE field pre-filled with the measured peak. Observed power is only a LOWER
  bound (the controller may never have demanded full power), so the user sets the battery's real
  maximum, likely the measured figure but higher if the hardware can do more.

SocRequiredRepairFlow answers a different question: pick state-of-charge sensors, or say the
battery has none (soc_absent). Its menu has no "ignore" - the answer is required, so it does not
inherit the adopt menu. Picking sensors is client-local; only the soc_absent answer travels to
the server (it is mirrored back into entry.data, so it is PATCHed before it is written).

ControlSystemRequiredRepairFlow asks for the control system with onboarding's picker (spec
2026-10-20-soc-krav 4.6). It is a plain form with no menu and no "ignore" of its own, for the
same reason as the SoC flow, and it PATCHes before it writes entry.data, since the control
system is mirrored from the server too.

BatteryNeedsInputRepairFlow (spec 2026-09-14 §7.2) is the exception to the menu rule: it fires
when the backend could not measure a declared battery at all, so there is no configured value to
keep and nothing to ignore — without the nameplate pair the plant has no grade.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_BATTERY_KW,
    CONF_BATTERY_KWH,
    CONF_CAPACITY_ISSUE_IGNORED,
    CONF_CONTROL_SYSTEM,
    CONF_CONTROL_SYSTEM_NAME,
    CONF_DIRECTION_ISSUE_IGNORED,
    CONF_EFF,
    CONF_EFFICIENCY_ISSUE_IGNORED,
    CONF_NAMEPLATE_KW,
    CONF_NAMEPLATE_KWH,
    CONF_POWER_ISSUE_IGNORED,
    CONF_RESERVE_PCT,
    CONF_SOC,
    CONF_SOC_ABSENT,
    CONF_SOC_ISSUE_IGNORED,
    DOMAIN,
)
from . import intervals
from .api import WoltaApiError, WoltaCopyBusy, WoltaCopyGone
from .control_system_prefill import (
    ControlSystemChoices,
    async_control_system_choices,
    battery_platforms,
    fallback_choices,
    control_system_name_error,
    control_system_name_selector,
    control_system_selector,
)
from .coordinator import (
    _ISSUE_CONTROL_SYSTEM_REQUIRED,
    _ISSUE_SOC_REQUIRED,
    desired_invert_intervals,
)
from .direction import apply_direction_target
from .soc_picker import soc_candidates, soc_requirement_error

_LOGGER = logging.getLogger(__name__)



def _as_list(value: Any) -> list[str]:
    """entry.data holds sensor ids as a list, or as a plain string in entries from v0.1.0."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        return [value]
    return []


class _AdoptRepairFlow(RepairsFlow):
    """Shared menu (adopt / ignore), ignore-persistence, and finish sequence.

    Subclasses set ``_issue_prefix`` (the issue id without the entry suffix), ``_ignore_key`` (the
    entry.data flag the coordinator checks to suppress the repair) and ``_adopt_step`` (the menu
    option / step id that runs the adopt), and implement that adopt step plus ``_placeholders``."""

    _issue_prefix: str = ""
    _ignore_key: str = ""
    _adopt_step: str = "confirm"

    def __init__(self, entry: ConfigEntry | None) -> None:
        self._entry = entry

    def _placeholders(self) -> dict[str, str]:
        return {}

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        # The config entry can be removed between the repair being raised and the user opening it
        # (async_create_fix_flow then hands us entry=None). Abort cleanly instead of crashing on
        # self._entry.runtime_data / .data.
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        return self.async_show_menu(
            step_id="init", menu_options=[self._adopt_step, "ignore"],
            description_placeholders=self._placeholders())

    async def async_step_ignore(self, user_input=None) -> data_entry_flow.FlowResult:
        # Persist the dismissal (the coordinator suppresses the repair while it's set) and clear
        # the currently-open issue immediately instead of waiting for the next poll. No server
        # PATCH/recompute: the configured value is unchanged, only the client-side nudge is muted.
        new_data = {**self._entry.data, self._ignore_key: True}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{self._issue_prefix}_{self._entry.entry_id}")
        return self.async_create_entry(title="", data={})

    async def _finish(self, *, patch: dict, new_data: dict) -> data_entry_flow.FlowResult:
        # Adopting re-engages the user with the value → clear any prior ignore so a future genuine
        # mismatch can surface again.
        new_data.pop(self._ignore_key, None)
        coordinator = self._entry.runtime_data
        await coordinator.client.patch_profile(coordinator.token, **patch)
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        # PATCH cleared the server cooldown, so a recompute is expected; swallow rate limits
        # gracefully (the nightly rewarm still picks the change up).
        try:
            await coordinator.async_trigger_recompute()
        except Exception:  # noqa: BLE001 - best-effort; sensors self-heal on next poll
            pass
        await coordinator.async_request_refresh()
        return self.async_create_entry(title="", data={})


class MeasuredCapacityRepairFlow(_AdoptRepairFlow):
    """Adopt the measured usable capacity and clear the reserve floor — or ignore it."""

    _issue_prefix = "measured_capacity"
    _ignore_key = CONF_CAPACITY_ISSUE_IGNORED
    _adopt_step = "confirm"

    def __init__(self, entry: ConfigEntry, measured_kwh: float, *,
                 configured_kwh: float | None = None, days: int = 0) -> None:
        super().__init__(entry)
        self._measured_kwh = measured_kwh
        self._configured_kwh = configured_kwh
        self._days = days

    def _placeholders(self) -> dict[str, str]:
        return {
            "measured": f"{self._measured_kwh:.1f}",
            "configured": f"{self._configured_kwh:.1f}" if self._configured_kwh else "?",
            "days": str(self._days),
        }

    async def async_step_confirm(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            new_data = {**self._entry.data, CONF_BATTERY_KWH: self._measured_kwh}
            new_data.pop(CONF_RESERVE_PCT, None)
            # reserve_pct=None is passed EXPLICITLY so the server clears it (an omitted field
            # would leave the old reserve in place → the window would be reduced twice).
            return await self._finish(
                patch={"battery_kwh": self._measured_kwh, "reserve_pct": None},
                new_data=new_data,
            )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders(),
        )


class MeasuredEfficiencyRepairFlow(_AdoptRepairFlow):
    """Adopt the measured round-trip efficiency — or ignore it."""

    _issue_prefix = "measured_efficiency"
    _ignore_key = CONF_EFFICIENCY_ISSUE_IGNORED
    _adopt_step = "confirm"

    def __init__(self, entry: ConfigEntry, measured_eff: float, *,
                 configured_eff: float | None = None, days: int = 0) -> None:
        super().__init__(entry)
        self._measured_eff = measured_eff
        self._configured_eff = configured_eff
        self._days = days

    def _placeholders(self) -> dict[str, str]:
        return {
            "measured": f"{self._measured_eff:.2f}",
            "configured": f"{self._configured_eff:.2f}" if self._configured_eff else "?",
            "days": str(self._days),
        }

    async def async_step_confirm(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            new_data = {**self._entry.data, CONF_EFF: self._measured_eff}
            return await self._finish(
                patch={"eff": self._measured_eff}, new_data=new_data)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders(),
        )


class MeasuredPowerRepairFlow(_AdoptRepairFlow):
    """Set the peak power, pre-filled with the measured value but editable — or ignore it.

    Observed power is only a LOWER bound (the controller may never have demanded full power), and
    it has no hard physical ceiling, so chronic sensor jumps can inflate it above the hardware
    maximum. The user therefore gets a menu: set the battery's real maximum, or ignore the nudge
    and keep their configured value (persisted so it stops re-appearing)."""

    _issue_prefix = "measured_power"
    _ignore_key = CONF_POWER_ISSUE_IGNORED
    _adopt_step = "set_power"

    def __init__(self, entry: ConfigEntry, measured_kw: float, *,
                 configured_kw: float | None = None, days: int = 0) -> None:
        super().__init__(entry)
        self._measured_kw = measured_kw
        self._configured_kw = configured_kw
        self._days = days

    def _placeholders(self) -> dict[str, str]:
        return {
            "measured": f"{self._measured_kw:.1f}",
            "configured": f"{self._configured_kw:.1f}" if self._configured_kw else "?",
            "days": str(self._days),
        }

    async def async_step_set_power(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            kw = float(user_input[CONF_BATTERY_KW])
            new_data = {**self._entry.data, CONF_BATTERY_KW: kw}
            return await self._finish(patch={"battery_kw": kw}, new_data=new_data)
        return self.async_show_form(
            step_id="set_power",
            data_schema=vol.Schema({
                vol.Required(CONF_BATTERY_KW, default=self._measured_kw): vol.All(
                    vol.Coerce(float), vol.Range(min=0.1, max=100.0)),
            }),
            description_placeholders=self._placeholders(),
        )


class SocRequiredRepairFlow(RepairsFlow):
    """The battery's state of charge is required: pick the sensors, or say there are none.

    Two answers, no "ignore" (spec 2026-10-20-soc-krav 4.3), so this is not an
    _AdoptRepairFlow: the base menu offers ignore.

    - pick_sensors: the choice is written to entry.data and the entry reloaded - the
      coordinator reads the sensors at start and its own bookkeeping (_track_soc_selection)
      schedules the SoC backfill. Only CONF_SOC changes, so the flow sensors' fingerprint is
      untouched. The entity ids are client-local and never sent. If the owner had earlier said
      "no sensor", that answer is withdrawn on the server first.
    - soc_absent: PATCH soc_absent=True.

    CONF_SOC_ABSENT is mirrored from the server (_PROFILE_SYNC_KEYS: a server value overwrites
    entry.data on every sync), so a local write the server does not hold would be reverted by
    the next sync. Both steps therefore PATCH first and write entry.data only once the PATCH
    went through; on a failure they abort, write nothing and leave the issue open."""

    def __init__(self, entry: ConfigEntry | None) -> None:
        self._entry = entry

    def _soc_issue_id(self) -> str:
        return f"{_ISSUE_SOC_REQUIRED}_{self._entry.entry_id}"

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        # Same race as _AdoptRepairFlow: the entry can be removed between the issue being
        # raised and the user opening it.
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        return self.async_show_menu(
            step_id="init", menu_options=["pick_sensors", "soc_absent"])

    async def _patch_soc_absent(self, value: bool) -> bool:
        """PATCH the owner's answer. False on a connection/HTTP failure (nothing written)."""
        coordinator = self._entry.runtime_data
        try:
            await coordinator.client.patch_profile(coordinator.token, soc_absent=value)
        except (WoltaApiError, aiohttp.ClientError, TimeoutError):
            return False
        return True

    async def async_step_soc_absent(self, user_input=None) -> data_entry_flow.FlowResult:
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        if not await self._patch_soc_absent(True):
            return self.async_abort(reason="failed")
        new_data = {**self._entry.data, CONF_SOC_ABSENT: True}
        new_data.pop(CONF_SOC_ISSUE_IGNORED, None)
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        ir.async_delete_issue(self.hass, DOMAIN, self._soc_issue_id())
        return self.async_create_entry(title="", data={})

    async def async_step_pick_sensors(self, user_input=None) -> data_entry_flow.FlowResult:
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        errors: dict[str, str] = {}
        if user_input is not None:
            picked = list(user_input.get(CONF_SOC) or [])
            # An empty choice is not a choice: "no SoC sensor" is the menu's other option.
            # Validated before anything is PATCHed, so a bad pick leaves an earlier "no
            # sensor" answer standing.
            error = soc_requirement_error(self.hass, picked, absent=False)
            if error:
                errors[CONF_SOC] = error
            else:
                new_data = {**self._entry.data, CONF_SOC: picked}
                if self._entry.data.get(CONF_SOC_ABSENT):
                    if not await self._patch_soc_absent(False):
                        return self.async_abort(reason="failed")
                    new_data[CONF_SOC_ABSENT] = False
                # The old "ignore" answered an offer, not this requirement.
                new_data.pop(CONF_SOC_ISSUE_IGNORED, None)
                self.hass.config_entries.async_update_entry(self._entry, data=new_data)
                ir.async_delete_issue(self.hass, DOMAIN, self._soc_issue_id())
                self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
                return self.async_create_entry(title="", data={})
        # Narrowed to what can pass validation. With no candidate at all an include list
        # would render a dead picker, so then every sensor is offered and the validation
        # error explains what is missing.
        candidates = soc_candidates(self.hass)
        config = EntitySelectorConfig(domain="sensor", multiple=True)
        if candidates:
            config["include_entities"] = candidates
        return self.async_show_form(
            step_id="pick_sensors",
            # Optional, not Required: the frontend refuses to submit an empty Required
            # field, which would hide the soc_required guidance ("no SoC sensor" is a menu
            # choice).
            data_schema=vol.Schema({vol.Optional(CONF_SOC): EntitySelector(config)}),
            errors=errors,
        )


class ControlSystemRequiredRepairFlow(RepairsFlow):
    """The control system is required: pick it with the same picker as in setup.

    The list, the suggestion from the battery sensors' integration and the rule that
    "other" needs a name are onboarding's (control_system_prefill), not copies. No menu and
    no "ignore" (HA's own Ignore stays), so this is not an _AdoptRepairFlow.

    CONF_CONTROL_SYSTEM and CONF_CONTROL_SYSTEM_NAME are mirrored from the server
    (_PROFILE_SYNC_KEYS: a server value overwrites entry.data on every sync), so the answer
    is PATCHed first and entry.data is written only once the PATCH went through. Without
    that write a failed profile read right after would see the old entry.data and raise the
    issue again. On a failure the flow aborts, writes nothing and leaves the issue open."""

    def __init__(self, entry: ConfigEntry | None) -> None:
        self._entry = entry
        # The options and the suggestion are looked up once per flow, not on every re-show
        # after an error.
        self._cs_choices: ControlSystemChoices | None = None

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        # Same race as _AdoptRepairFlow: the entry can be removed between the issue being
        # raised and the user opening it.
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        return await self.async_step_pick()

    async def _choices(self) -> ControlSystemChoices:
        """Onboarding's options and suggestion (the server's list, else const)."""
        if self._cs_choices is None:
            coordinator = self._entry.runtime_data
            batt = [
                entity_id
                for key in (CONF_BATT_IN, CONF_BATT_OUT)
                for entity_id in _as_list(self._entry.data.get(key))
            ]
            try:
                self._cs_choices = await async_control_system_choices(
                    coordinator.client, battery_platforms(self.hass, batt))
            except Exception:  # noqa: BLE001 - the lookup is optional, never blocks the form
                _LOGGER.debug("control_system lookup failed; const list, no default",
                              exc_info=True)
                self._cs_choices = fallback_choices()
        return self._cs_choices

    def _default_choice(self, choices: ControlSystemChoices) -> str | None:
        """The control system the form starts on: what the entry has (an "other" that only
        lacks its name), else onboarding's suggestion, else nothing (no guess)."""
        current = self._entry.data.get(CONF_CONTROL_SYSTEM)
        # Same guard as the suggestion: an id that is not one of the options (an older
        # fallback list) would be a default HA rejects.
        if current in {c for c, _ in choices.options}:
            return current
        return choices.suggestion

    async def async_step_pick(self, user_input=None) -> data_entry_flow.FlowResult:
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        errors: dict[str, str] = {}
        if user_input is not None:
            control_system = user_input[CONF_CONTROL_SYSTEM]
            name = (user_input.get(CONF_CONTROL_SYSTEM_NAME) or "").strip()
            error = control_system_name_error(control_system, name)
            if error:
                errors[CONF_CONTROL_SYSTEM_NAME] = error
            else:
                # The name only means something together with "other" (as in setup).
                payload: dict[str, Any] = {CONF_CONTROL_SYSTEM: control_system}
                if control_system == "other":
                    payload[CONF_CONTROL_SYSTEM_NAME] = name
                coordinator = self._entry.runtime_data
                try:
                    await coordinator.client.patch_profile(coordinator.token, **payload)
                except (WoltaApiError, aiohttp.ClientError, TimeoutError):
                    return self.async_abort(reason="failed")
                new_data = {**self._entry.data, **payload}
                if control_system != "other":
                    new_data.pop(CONF_CONTROL_SYSTEM_NAME, None)
                self.hass.config_entries.async_update_entry(self._entry, data=new_data)
                ir.async_delete_issue(
                    self.hass, DOMAIN,
                    f"{_ISSUE_CONTROL_SYSTEM_REQUIRED}_{self._entry.entry_id}")
                # Not awaited: entry.data already holds the answer and the issue is gone, so
                # the dialog need not wait for a whole update cycle (upload, profile, results).
                self.hass.async_create_task(coordinator.async_request_refresh())
                return self.async_create_entry(title="", data={})
        choices = await self._choices()
        default = self._default_choice(choices)
        cs_field = (vol.Required(CONF_CONTROL_SYSTEM, default=default)
                    if default else vol.Required(CONF_CONTROL_SYSTEM))
        schema = vol.Schema({
            cs_field: control_system_selector(choices.options),
            vol.Optional(CONF_CONTROL_SYSTEM_NAME): control_system_name_selector(),
        })
        # After an error the form keeps what was typed; the first time it offers the stored name.
        shown = user_input if errors else {
            CONF_CONTROL_SYSTEM_NAME: self._entry.data.get(CONF_CONTROL_SYSTEM_NAME)}
        return self.async_show_form(
            step_id="pick",
            data_schema=self.add_suggested_values_to_schema(schema, shown),
            errors=errors,
        )


class BatteryNeedsInputRepairFlow(RepairsFlow):
    """Backend kunde inte mäta batteriets storlek (spec 2026-09-14 §7.2): be om märkskyltens
    värden. Ingen ignore-meny – utan värden finns inget betyg. PATCH:ar båda; backend härleder
    synkront (märkdata som fallback) och raden blir känd."""

    def __init__(self, entry: ConfigEntry | None, *, days: int = 0) -> None:
        self._entry = entry
        self._days = days

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        # Samma kapplöpning som i _AdoptRepairFlow: entryn kan vara borttagen mellan att
        # issuen restes och att användaren öppnar den.
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        return await self.async_step_set_nameplate()

    async def async_step_set_nameplate(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            coordinator = self._entry.runtime_data
            kwh = float(user_input["nameplate_kwh"])
            kw = float(user_input["nameplate_kw"])
            # Båda värdena i EN PATCH: backend härleder kapaciteten ur paret, så ett halvt
            # par lämnar raden utan betyg (samma regel som deklarationen i config-flowet).
            await coordinator.client.patch_profile(
                coordinator.token, nameplate_kwh=kwh, nameplate_kw=kw)
            # Spegla paret i entry.data, precis som _finish speglar de levererbara värdena.
            # Märkdatanycklarna ligger INTE i coordinatorns _PROFILE_SYNC_KEYS, så de kommer
            # aldrig tillbaka via profilspeglingen – en reauth bygger en ny profil ur
            # entry.data och hade återskapat raden utan märkdata, alltså rakt tillbaka i
            # needs_input som användaren just svarat på.
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={**self._entry.data, CONF_NAMEPLATE_KWH: kwh, CONF_NAMEPLATE_KW: kw},
            )
            # Repairs-managern har stämplat issue_id på flödet (components/repairs/
            # issue_handler.py) – radera DEN id:t i stället för att bygga strängen igen och
            # riskera att den glider ur fas med den som restes.
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            await coordinator.async_request_refresh()
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="set_nameplate",
            data_schema=vol.Schema({
                vol.Required("nameplate_kwh"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=500.0)),
                vol.Required("nameplate_kw"): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=100.0)),
            }),
            description_placeholders={"days": str(self._days)})


class BatteryDirectionRepairFlow(RepairsFlow):
    """Vänd batteriets riktning när servern ser omkastade laddfält ur SoC. The desired
    invert intervals become desired Δ swap_intervals (the intervals the server placed on
    the hour); the coordinator's direction job then swaps the battery fields of the
    stored rows on the server. Nothing is re-sent. An issue raised by an older version
    carries no swap_intervals: the v0.43 rule then flips the whole history."""

    def __init__(self, entry: ConfigEntry | None,
                 placeholders: dict[str, Any] | None = None,
                 swap_intervals: list | None = None) -> None:
        self._entry = entry
        # En egen RepairsFlow ärver inte issuens translation_placeholders: koordinatorn lägger
        # dem också i issue-datan, och de skickas in i varje steg här.
        self._placeholders = {k: str(v) for k, v in (placeholders or {}).items()}
        self._swap = swap_intervals

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        # Samma kapplöpning som i _AdoptRepairFlow: entryn kan vara borttagen.
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        return self.async_show_menu(
            step_id="init", menu_options=["flip", "ignore"],
            description_placeholders=self._placeholders)

    def _change_pending(self) -> bool:
        """A direction change is still on its way to the stored rows (a direction job,
        or desired ≠ applied). A flip now would be judged from a grade of the old
        direction and swap the same period back."""
        coordinator = getattr(self._entry, "runtime_data", None)
        pending = getattr(coordinator, "direction_change_pending", None)
        return bool(pending()) if callable(pending) else False

    async def async_step_flip(self, user_input=None) -> data_entry_flow.FlowResult:
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        if self._change_pending():
            return self.async_abort(reason="direction_pending")
        if user_input is None:
            return self.async_show_form(
                step_id="flip", data_schema=vol.Schema({}),
                description_placeholders=self._placeholders)
        swap = [(None, None)] if self._swap is None else intervals.parse(self._swap)
        # The CURRENT desired value is read here, not when the issue was raised: it may
        # have changed under Configure meanwhile.
        desired = intervals.sym_diff(desired_invert_intervals(self._entry.data), swap)
        # Stamps, the issue and the refresh: see apply_direction_target. The repair never
        # overrides the server's SoC verdict - it follows it.
        await apply_direction_target(self.hass, self._entry, desired, origin="repair")
        return self.async_create_entry(title="", data={})

    async def async_step_ignore(self, user_input=None) -> data_entry_flow.FlowResult:
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        new_data = {**self._entry.data, CONF_DIRECTION_ISSUE_IGNORED: True}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(title="", data={})


class HouseCopiesRepairFlow(RepairsFlow):
    """Remove uploads at Wolta that carry this plant's metering data.

    The server decides which rows qualify (same house, same owner) and puts them out of the
    comparison at once; they are deleted after the grace period unless whoever holds their
    personal link keeps them. One confirmation step: Home Assistant's repair flow is itself the
    confirmation, and undo lives on wolta.se (the text says where)."""

    def __init__(self, entry: ConfigEntry | None, ids: list[int], lines: str, days: int) -> None:
        self._entry = entry
        self._ids = ids
        self._lines = lines
        self._days = days

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="confirm", data_schema=vol.Schema({}),
                description_placeholders={"count": str(len(self._ids)), "list": self._lines,
                                          "days": str(self._days)})
        coordinator = self._entry.runtime_data
        for copy_id in self._ids:
            try:
                await coordinator.client.remove_copy(coordinator.token, copy_id)
            except WoltaCopyGone:
                continue  # already removed, or no longer a copy this plant may handle
            except WoltaCopyBusy:
                return self.async_abort(reason="busy")
            except (WoltaApiError, aiohttp.ClientError, TimeoutError):
                return self.async_abort(reason="failed")
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Dispatch to the right adopt flow based on the issue id prefix. ``data`` carries the
    entry id and the measured value the coordinator put on the issue."""
    data = data or {}
    entry = hass.config_entries.async_get_entry(data.get("entry_id", ""))
    days = int(data.get("days", 0))
    if issue_id.startswith("battery_direction"):
        return BatteryDirectionRepairFlow(
            entry,
            {k: v for k, v in data.items()
             if k not in ("entry_id", "form", "swap_intervals")},
            data.get("swap_intervals"))
    if issue_id.startswith(_ISSUE_SOC_REQUIRED):
        return SocRequiredRepairFlow(entry)
    if issue_id.startswith(_ISSUE_CONTROL_SYSTEM_REQUIRED):
        return ControlSystemRequiredRepairFlow(entry)
    if issue_id.startswith("battery_needs_input"):
        return BatteryNeedsInputRepairFlow(entry, days=days)
    if issue_id.startswith("measured_power"):
        configured = data.get("configured_kw")
        return MeasuredPowerRepairFlow(
            entry, float(data.get("measured_kw", 0.0)),
            configured_kw=float(configured) if configured is not None else None,
            days=days)
    if issue_id.startswith("measured_efficiency"):
        configured = data.get("configured_eff")
        return MeasuredEfficiencyRepairFlow(
            entry, float(data.get("measured_eff", 0.0)),
            configured_eff=float(configured) if configured is not None else None,
            days=days)
    if issue_id.startswith("house_copies"):
        return HouseCopiesRepairFlow(
            entry, [int(x) for x in data.get("ids") or []], str(data.get("lines") or ""),
            int(data.get("purge_days") or 30))
    configured = data.get("configured_kwh")
    return MeasuredCapacityRepairFlow(
        entry, float(data.get("measured_kwh", 0.0)),
        configured_kwh=float(configured) if configured is not None else None,
        days=days)
