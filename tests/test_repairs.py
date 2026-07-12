"""Tests for the measured-capacity repair flow."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.wolta.const import (
    CONF_BATTERY_KWH,
    CONF_RESERVE_PCT,
)
from custom_components.wolta.repairs import (
    MeasuredCapacityRepairFlow,
    async_create_fix_flow,
)


def _entry_with_coordinator(hass, *, battery_kwh, reserve):
    entry = MagicMock()
    entry.entry_id = "e1"
    data = {CONF_BATTERY_KWH: battery_kwh}
    if reserve is not None:
        data[CONF_RESERVE_PCT] = reserve
    entry.data = data
    coordinator = MagicMock()
    coordinator.token = "tok"
    coordinator.client = MagicMock()
    coordinator.client.patch_profile = AsyncMock(return_value={})
    coordinator.async_trigger_recompute = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()
    entry.runtime_data = coordinator
    return entry, coordinator


@pytest.mark.asyncio
async def test_repair_adopts_measured_and_clears_reserve(hass: HomeAssistant):
    """Confirm → PATCH battery_kwh=measured + reserve_pct=None (explicit, clears server-side),
    entry data updated, reserve removed, recompute triggered."""
    entry, coordinator = _entry_with_coordinator(hass, battery_kwh=15.0, reserve=10.0)
    hass.config_entries.async_update_entry = MagicMock()

    flow = MeasuredCapacityRepairFlow(entry, 11.0)
    flow.hass = hass

    # Step 1: shows the confirm form.
    form = await flow.async_step_init()
    assert form["type"] == "form"
    assert form["step_id"] == "confirm"
    assert form["description_placeholders"]["measured"] == "11.0"

    # Step 2: confirm.
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"

    # Reserve MUST be cleared explicitly (None), not omitted → server clears it → no double
    # reduction on top of the already-reserve-excluded measurement.
    coordinator.client.patch_profile.assert_awaited_once_with(
        "tok", battery_kwh=11.0, reserve_pct=None)
    coordinator.async_trigger_recompute.assert_awaited_once()
    coordinator.async_request_refresh.assert_awaited_once()

    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_BATTERY_KWH] == 11.0
    assert CONF_RESERVE_PCT not in new_data


@pytest.mark.asyncio
async def test_repair_recompute_rate_limit_swallowed(hass: HomeAssistant):
    """A rate-limited recompute must not blow up the fix flow."""
    entry, coordinator = _entry_with_coordinator(hass, battery_kwh=15.0, reserve=None)
    coordinator.async_trigger_recompute = AsyncMock(side_effect=Exception("429"))
    hass.config_entries.async_update_entry = MagicMock()

    flow = MeasuredCapacityRepairFlow(entry, 11.0)
    flow.hass = hass
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_fix_flow_resolves_entry_and_value(hass: HomeAssistant):
    entry, _ = _entry_with_coordinator(hass, battery_kwh=15.0, reserve=None)
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow = await async_create_fix_flow(
        hass, "measured_capacity_e1", {"entry_id": "e1", "measured_kwh": 11.5})
    assert isinstance(flow, MeasuredCapacityRepairFlow)
    assert flow._measured_kwh == 11.5
    assert flow._entry is entry


from custom_components.wolta.const import CONF_BATTERY_KW, CONF_EFF  # noqa: E402
from custom_components.wolta.repairs import (  # noqa: E402
    MeasuredEfficiencyRepairFlow,
    MeasuredPowerRepairFlow,
)


@pytest.mark.asyncio
async def test_power_repair_uses_editable_field(hass: HomeAssistant):
    """Power flow shows a field pre-filled with the measured peak; the user can raise it,
    and the submitted value (not the measured one) is what gets PATCHed."""
    entry, coordinator = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    entry.data = {CONF_BATTERY_KW: 10.0}
    hass.config_entries.async_update_entry = MagicMock()

    flow = MeasuredPowerRepairFlow(entry, 3.6)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] == "form"
    assert form["description_placeholders"]["measured"] == "3.6"

    # User raises it to the real inverter limit (5 kW) rather than accepting 3.6.
    result = await flow.async_step_confirm({CONF_BATTERY_KW: 5.0})
    assert result["type"] == "create_entry"
    coordinator.client.patch_profile.assert_awaited_once_with("tok", battery_kw=5.0)
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_BATTERY_KW] == 5.0


@pytest.mark.asyncio
async def test_efficiency_repair_adopts_measured(hass: HomeAssistant):
    entry, coordinator = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    entry.data = {CONF_EFF: 0.9}
    hass.config_entries.async_update_entry = MagicMock()

    flow = MeasuredEfficiencyRepairFlow(entry, 0.72)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["step_id"] == "confirm"
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    coordinator.client.patch_profile.assert_awaited_once_with("tok", eff=0.72)
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_EFF] == 0.72


@pytest.mark.asyncio
async def test_create_fix_flow_dispatches_by_issue_prefix(hass: HomeAssistant):
    entry, _ = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    p = await async_create_fix_flow(hass, "measured_power_e1", {"entry_id": "e1", "measured_kw": 3.6})
    assert isinstance(p, MeasuredPowerRepairFlow)
    e = await async_create_fix_flow(hass, "measured_efficiency_e1", {"entry_id": "e1", "measured_eff": 0.72})
    assert isinstance(e, MeasuredEfficiencyRepairFlow)
    c = await async_create_fix_flow(hass, "measured_capacity_e1", {"entry_id": "e1", "measured_kwh": 11.0})
    assert isinstance(c, MeasuredCapacityRepairFlow)
