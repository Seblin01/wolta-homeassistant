"""Repair flows for the Wolta integration.

The backend measures the grade-affecting battery parameters from the actual meter flows and,
when they clearly disagree with what's configured, the coordinator raises a fixable repair.
These flows adopt the measured value so the grade stays fair without the user needing to know
nameplate-vs-usable, AC-vs-DC, etc.

Three flows, with deliberately different UX:
- capacity: one-click adopt (the measurement is a confident estimate of usable kWh) and CLEAR
  the reserve (the measurement already excludes it → don't reduce twice).
- efficiency: one-click adopt (round-trip out/in is a true measurement, not a bound).
- power: an EDITABLE field pre-filled with the measured peak. Observed power is only a LOWER
  bound (the controller may never have demanded full power), so a too-low value would flatter
  the grade — the user is asked to set the battery's real maximum, likely the measured figure
  but higher if the hardware can do more.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_BATTERY_KW,
    CONF_BATTERY_KWH,
    CONF_EFF,
    CONF_POWER_ISSUE_IGNORED,
    CONF_RESERVE_PCT,
    DOMAIN,
)


class _AdoptRepairFlow(RepairsFlow):
    """Shared finish sequence: PATCH the server profile, mirror into entry.data, recompute."""

    def __init__(self, entry: ConfigEntry | None) -> None:
        self._entry = entry

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        # The config entry can be removed between the repair being raised and the user opening
        # it (async_create_fix_flow then hands us entry=None). Abort cleanly instead of
        # crashing on self._entry.runtime_data in _finish.
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        return await self.async_step_confirm()

    async def _finish(self, *, patch: dict, new_data: dict) -> data_entry_flow.FlowResult:
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
    """Adopt the measured usable capacity and clear the reserve floor."""

    def __init__(self, entry: ConfigEntry, measured_kwh: float) -> None:
        super().__init__(entry)
        self._measured_kwh = measured_kwh

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
            description_placeholders={"measured": f"{self._measured_kwh:.1f}"},
        )


class MeasuredEfficiencyRepairFlow(_AdoptRepairFlow):
    """Adopt the measured round-trip efficiency."""

    def __init__(self, entry: ConfigEntry, measured_eff: float) -> None:
        super().__init__(entry)
        self._measured_eff = measured_eff

    async def async_step_confirm(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            new_data = {**self._entry.data, CONF_EFF: self._measured_eff}
            return await self._finish(
                patch={"eff": self._measured_eff}, new_data=new_data)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"measured": f"{self._measured_eff:.2f}"},
        )


class MeasuredPowerRepairFlow(_AdoptRepairFlow):
    """Set the peak power, pre-filled with the measured value but editable — or ignore it.

    Observed power is only a LOWER bound (the controller may never have demanded full power), and
    it has no hard physical ceiling, so chronic sensor jumps can inflate it above the hardware
    maximum. The user therefore gets a menu: set the battery's real maximum, or ignore the nudge
    and keep their configured value (persisted so it stops re-appearing)."""

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

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        if self._entry is None:
            return self.async_abort(reason="entry_not_found")
        return self.async_show_menu(
            step_id="init", menu_options=["set_power", "ignore"],
            description_placeholders=self._placeholders())

    async def async_step_set_power(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            kw = float(user_input[CONF_BATTERY_KW])
            new_data = {**self._entry.data, CONF_BATTERY_KW: kw}
            # The user re-engaged with the value → clear any prior "ignore" so a future genuine
            # mismatch can surface again.
            new_data.pop(CONF_POWER_ISSUE_IGNORED, None)
            return await self._finish(patch={"battery_kw": kw}, new_data=new_data)
        return self.async_show_form(
            step_id="set_power",
            data_schema=vol.Schema({
                vol.Required(CONF_BATTERY_KW, default=self._measured_kw): vol.All(
                    vol.Coerce(float), vol.Range(min=0.1, max=100.0)),
            }),
            description_placeholders=self._placeholders(),
        )

    async def async_step_ignore(self, user_input=None) -> data_entry_flow.FlowResult:
        # Persist the dismissal (the coordinator suppresses the repair while it's set) and clear
        # the currently-open issue immediately instead of waiting for the next poll. No server
        # PATCH/recompute: the configured value is unchanged, only the client-side nudge is muted.
        new_data = {**self._entry.data, CONF_POWER_ISSUE_IGNORED: True}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        ir.async_delete_issue(
            self.hass, DOMAIN, f"measured_power_{self._entry.entry_id}")
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Dispatch to the right adopt flow based on the issue id prefix. ``data`` carries the
    entry id and the measured value the coordinator put on the issue."""
    data = data or {}
    entry = hass.config_entries.async_get_entry(data.get("entry_id", ""))
    if issue_id.startswith("measured_power"):
        configured = data.get("configured_kw")
        return MeasuredPowerRepairFlow(
            entry, float(data.get("measured_kw", 0.0)),
            configured_kw=float(configured) if configured is not None else None,
            days=int(data.get("days", 0)))
    if issue_id.startswith("measured_efficiency"):
        return MeasuredEfficiencyRepairFlow(entry, float(data.get("measured_eff", 0.0)))
    return MeasuredCapacityRepairFlow(entry, float(data.get("measured_kwh", 0.0)))
