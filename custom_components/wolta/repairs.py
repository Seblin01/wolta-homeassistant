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

from .const import CONF_BATTERY_KW, CONF_BATTERY_KWH, CONF_EFF, CONF_RESERVE_PCT


class _AdoptRepairFlow(RepairsFlow):
    """Shared finish sequence: PATCH the server profile, mirror into entry.data, recompute."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

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

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

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

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

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
    """Set the peak power, pre-filled with the measured value but editable.

    Observed power is a lower bound, so the user confirms or raises it to the battery's real
    maximum instead of blindly adopting a possibly-too-low measurement."""

    def __init__(self, entry: ConfigEntry, measured_kw: float) -> None:
        super().__init__(entry)
        self._measured_kw = measured_kw

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            kw = float(user_input[CONF_BATTERY_KW])
            new_data = {**self._entry.data, CONF_BATTERY_KW: kw}
            return await self._finish(patch={"battery_kw": kw}, new_data=new_data)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_BATTERY_KW, default=self._measured_kw): vol.All(
                    vol.Coerce(float), vol.Range(min=0.1, max=100.0)),
            }),
            description_placeholders={"measured": f"{self._measured_kw:.1f}"},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Dispatch to the right adopt flow based on the issue id prefix. ``data`` carries the
    entry id and the measured value the coordinator put on the issue."""
    data = data or {}
    entry = hass.config_entries.async_get_entry(data.get("entry_id", ""))
    if issue_id.startswith("measured_power"):
        return MeasuredPowerRepairFlow(entry, float(data.get("measured_kw", 0.0)))
    if issue_id.startswith("measured_efficiency"):
        return MeasuredEfficiencyRepairFlow(entry, float(data.get("measured_eff", 0.0)))
    return MeasuredCapacityRepairFlow(entry, float(data.get("measured_kwh", 0.0)))
