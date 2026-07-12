"""Repair flows for the Wolta integration.

Currently one flow: adopt the server-measured usable battery capacity. The backend measures
the all-time DISPATCHABLE window at the meter (observed_capacity) and the coordinator raises a
fixable issue when it clearly disagrees with the configured capacity. Confirming adopts the
measured value as ``battery_kwh`` and CLEARS any reserve — the measurement already excludes the
reserve, so applying it again would reduce the window twice (double count)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BATTERY_KWH, CONF_RESERVE_PCT


class MeasuredCapacityRepairFlow(RepairsFlow):
    """Confirm-and-adopt flow for the measured battery capacity."""

    def __init__(self, entry: ConfigEntry, measured_kwh: float) -> None:
        self._entry = entry
        self._measured_kwh = measured_kwh

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            coordinator = self._entry.runtime_data
            # Adopt the measured dispatchable window as the capacity AND clear the reserve.
            # reserve_pct=None is passed EXPLICITLY so the server clears it (an omitted field
            # would leave the old reserve in place → the window would be reduced twice).
            await coordinator.client.patch_profile(
                coordinator.token,
                battery_kwh=self._measured_kwh,
                reserve_pct=None,
            )
            new_data = {**self._entry.data, CONF_BATTERY_KWH: self._measured_kwh}
            new_data.pop(CONF_RESERVE_PCT, None)
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)
            # PATCH cleared the server cooldown, so a recompute is expected; swallow rate
            # limits gracefully (the nightly rewarm still picks the change up).
            try:
                await coordinator.async_trigger_recompute()
            except Exception:  # noqa: BLE001 - best-effort; sensors self-heal on next poll
                pass
            await coordinator.async_request_refresh()
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"measured": f"{self._measured_kwh:.1f}"},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Build the fix flow for a measured-capacity issue.

    ``data`` carries the entry id and the measured value the coordinator put on the issue."""
    data = data or {}
    entry = hass.config_entries.async_get_entry(data.get("entry_id", ""))
    return MeasuredCapacityRepairFlow(entry, float(data.get("measured_kwh", 0.0)))
