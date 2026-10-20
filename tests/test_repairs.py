"""Tests for the measured-capacity repair flow."""

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import voluptuous as vol
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

    # Step 1: menu (adopt or ignore).
    menu = await flow.async_step_init()
    assert menu["type"] == "menu"
    assert set(menu["menu_options"]) == {"confirm", "ignore"}

    # Step 2: the confirm form.
    form = await flow.async_step_confirm()
    assert form["type"] == "form"
    assert form["step_id"] == "confirm"
    assert form["description_placeholders"]["measured"] == "11.0"

    # Step 3: confirm.
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


from custom_components.wolta.const import CONF_POWER_ISSUE_IGNORED  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402


@pytest.mark.asyncio
async def test_power_repair_menu_offers_set_and_ignore(hass: HomeAssistant):
    """The power repair opens on a menu so the user can either set the real power or dismiss it."""
    entry, _ = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    entry.data = {CONF_BATTERY_KW: 10.0}

    flow = MeasuredPowerRepairFlow(entry, 3.6)
    flow.hass = hass
    menu = await flow.async_step_init()
    assert menu["type"] == "menu"
    assert set(menu["menu_options"]) == {"set_power", "ignore"}


@pytest.mark.asyncio
async def test_power_repair_uses_editable_field(hass: HomeAssistant):
    """The set-power branch shows a field pre-filled with the measured peak; the user can raise
    it, and the submitted value (not the measured one) is what gets PATCHed."""
    entry, coordinator = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    entry.data = {CONF_BATTERY_KW: 10.0}
    hass.config_entries.async_update_entry = MagicMock()

    flow = MeasuredPowerRepairFlow(entry, 3.6)
    flow.hass = hass
    form = await flow.async_step_set_power()
    assert form["type"] == "form"
    assert form["step_id"] == "set_power"
    assert form["description_placeholders"]["measured"] == "3.6"

    # User raises it to the real inverter limit (5 kW) rather than accepting 3.6.
    result = await flow.async_step_set_power({CONF_BATTERY_KW: 5.0})
    assert result["type"] == "create_entry"
    coordinator.client.patch_profile.assert_awaited_once_with("tok", battery_kw=5.0)
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_BATTERY_KW] == 5.0


@pytest.mark.asyncio
async def test_power_repair_form_shows_full_context(hass: HomeAssistant):
    """The set-power form restates the configured value and history length (threaded from the
    issue), not just the measured peak — so the placeholders in the string are all filled."""
    entry, _ = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    entry.data = {CONF_BATTERY_KW: 9.9}
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow = await async_create_fix_flow(
        hass, "measured_power_e1",
        {"entry_id": "e1", "measured_kw": 27.5, "configured_kw": 9.9, "days": 317})
    flow.hass = hass
    form = await flow.async_step_set_power()
    ph = form["description_placeholders"]
    assert ph["measured"] == "27.5"
    assert ph["configured"] == "9.9"
    assert ph["days"] == "317"


@pytest.mark.asyncio
async def test_power_repair_set_power_clears_ignore_flag(hass: HomeAssistant):
    """Adopting a value re-engages the user → any earlier 'ignore' is cleared, so a future
    genuine mismatch can surface again."""
    entry, _ = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    entry.data = {CONF_BATTERY_KW: 10.0, CONF_POWER_ISSUE_IGNORED: True}
    hass.config_entries.async_update_entry = MagicMock()

    flow = MeasuredPowerRepairFlow(entry, 3.6)
    flow.hass = hass
    await flow.async_step_set_power({CONF_BATTERY_KW: 5.0})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert CONF_POWER_ISSUE_IGNORED not in new_data


@pytest.mark.asyncio
async def test_power_repair_ignore_persists_flag_and_clears_issue(hass: HomeAssistant):
    """The ignore branch persists the dismissal flag and deletes the open repair immediately."""
    entry, _ = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    entry.data = {CONF_BATTERY_KW: 10.0}
    hass.config_entries.async_update_entry = MagicMock()
    ir.async_create_issue(
        hass, "wolta", "measured_power_e1", is_fixable=True,
        severity=ir.IssueSeverity.WARNING, translation_key="measured_power")

    flow = MeasuredPowerRepairFlow(entry, 27.5)
    flow.hass = hass
    result = await flow.async_step_ignore()
    assert result["type"] == "create_entry"
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_POWER_ISSUE_IGNORED] is True
    assert ir.async_get(hass).async_get_issue("wolta", "measured_power_e1") is None


@pytest.mark.asyncio
async def test_efficiency_repair_adopts_measured(hass: HomeAssistant):
    entry, coordinator = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    entry.data = {CONF_EFF: 0.9}
    hass.config_entries.async_update_entry = MagicMock()

    flow = MeasuredEfficiencyRepairFlow(entry, 0.72)
    flow.hass = hass
    menu = await flow.async_step_init()
    assert menu["type"] == "menu"
    assert set(menu["menu_options"]) == {"confirm", "ignore"}
    form = await flow.async_step_confirm()
    assert form["step_id"] == "confirm"
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    coordinator.client.patch_profile.assert_awaited_once_with("tok", eff=0.72)
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_EFF] == 0.72


from custom_components.wolta.const import (  # noqa: E402
    CONF_CAPACITY_ISSUE_IGNORED,
    CONF_EFFICIENCY_ISSUE_IGNORED,
)


@pytest.mark.asyncio
async def test_capacity_repair_ignore_persists_flag_and_clears_issue(hass: HomeAssistant):
    entry, _ = _entry_with_coordinator(hass, battery_kwh=15.0, reserve=None)
    entry.entry_id = "e1"
    hass.config_entries.async_update_entry = MagicMock()
    ir.async_create_issue(
        hass, "wolta", "measured_capacity_e1", is_fixable=True,
        severity=ir.IssueSeverity.WARNING, translation_key="measured_capacity")

    flow = MeasuredCapacityRepairFlow(entry, 11.0)
    flow.hass = hass
    result = await flow.async_step_ignore()
    assert result["type"] == "create_entry"
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_CAPACITY_ISSUE_IGNORED] is True
    assert ir.async_get(hass).async_get_issue("wolta", "measured_capacity_e1") is None
    # An adopt-only flow must not silently PATCH the server when the user chose to ignore.
    flow._entry.runtime_data.client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_capacity_repair_form_shows_full_context(hass: HomeAssistant):
    """Confirm form restates configured + days (threaded from the issue), not just measured."""
    entry, _ = _entry_with_coordinator(hass, battery_kwh=15.0, reserve=None)
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow = await async_create_fix_flow(
        hass, "measured_capacity_e1",
        {"entry_id": "e1", "measured_kwh": 11.0, "configured_kwh": 15.0, "days": 200})
    flow.hass = hass
    form = await flow.async_step_confirm()
    ph = form["description_placeholders"]
    assert ph["measured"] == "11.0"
    assert ph["configured"] == "15.0"
    assert ph["days"] == "200"


@pytest.mark.asyncio
async def test_efficiency_repair_form_shows_full_context(hass: HomeAssistant):
    entry, _ = _entry_with_coordinator(hass, battery_kwh=15.0, reserve=None)
    entry.data = {CONF_EFF: 0.9}
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow = await async_create_fix_flow(
        hass, "measured_efficiency_e1",
        {"entry_id": "e1", "measured_eff": 0.76, "configured_eff": 0.9, "days": 318})
    flow.hass = hass
    form = await flow.async_step_confirm()
    ph = form["description_placeholders"]
    assert ph["measured"] == "0.76"
    assert ph["configured"] == "0.90"
    assert ph["days"] == "318"


@pytest.mark.asyncio
async def test_capacity_repair_adopt_clears_ignore_flag(hass: HomeAssistant):
    entry, _ = _entry_with_coordinator(hass, battery_kwh=15.0, reserve=None)
    entry.data = {CONF_BATTERY_KWH: 15.0, CONF_CAPACITY_ISSUE_IGNORED: True}
    hass.config_entries.async_update_entry = MagicMock()
    flow = MeasuredCapacityRepairFlow(entry, 11.0)
    flow.hass = hass
    await flow.async_step_confirm({})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert CONF_CAPACITY_ISSUE_IGNORED not in new_data


@pytest.mark.asyncio
async def test_efficiency_repair_ignore_persists_flag_and_clears_issue(hass: HomeAssistant):
    entry, _ = _entry_with_coordinator(hass, battery_kwh=15.0, reserve=None)
    entry.entry_id = "e1"
    entry.data = {CONF_EFF: 0.9}
    hass.config_entries.async_update_entry = MagicMock()
    ir.async_create_issue(
        hass, "wolta", "measured_efficiency_e1", is_fixable=True,
        severity=ir.IssueSeverity.WARNING, translation_key="measured_efficiency")

    flow = MeasuredEfficiencyRepairFlow(entry, 0.76)
    flow.hass = hass
    result = await flow.async_step_ignore()
    assert result["type"] == "create_entry"
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_EFFICIENCY_ISSUE_IGNORED] is True
    assert ir.async_get(hass).async_get_issue("wolta", "measured_efficiency_e1") is None


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


@pytest.mark.asyncio
async def test_repair_aborts_when_entry_missing(hass: HomeAssistant):
    """Review #2: entry borttagen mellan issue och fix → abort, ingen krasch."""
    flow = MeasuredCapacityRepairFlow(None, 11.0)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "abort"
    assert result["reason"] == "entry_not_found"


# ---------------------------------------------------------------------------
# battery_needs_input (spec 2026-09-14 §7.2): backend gav upp mätningen
# ---------------------------------------------------------------------------

from custom_components.wolta.repairs import BatteryNeedsInputRepairFlow  # noqa: E402


@pytest.mark.asyncio
async def test_battery_needs_input_repair_patches_nameplate_pair(hass: HomeAssistant):
    """Formuläret PATCH:ar BÅDA märkskyltsvärdena – backend härleder kapaciteten synkront
    ur paret, så ett halvt par lämnar raden utan betyg."""
    entry, coordinator = _entry_with_coordinator(hass, battery_kwh=None, reserve=None)
    hass.config_entries.async_update_entry = MagicMock()
    flow = BatteryNeedsInputRepairFlow(entry, days=240)
    flow.hass = hass
    flow.issue_id = "battery_needs_input_e1"
    form = await flow.async_step_init()
    assert form["type"] == "form" and form["step_id"] == "set_nameplate"
    result = await flow.async_step_set_nameplate({"nameplate_kwh": 12.0, "nameplate_kw": 6.0})
    assert result["type"] == "create_entry"
    coordinator.client.patch_profile.assert_awaited_once_with(
        "tok", nameplate_kwh=12.0, nameplate_kw=6.0)
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_battery_needs_input_repair_has_no_ignore_option(hass: HomeAssistant):
    """Ingen ignore-väg: utan värden finns inget betyg, så det finns inget att vifta bort.
    Steget öppnar direkt i formuläret istället för i de andra flödenas meny."""
    entry, _ = _entry_with_coordinator(hass, battery_kwh=None, reserve=None)
    flow = BatteryNeedsInputRepairFlow(entry, days=240)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] == "form"
    assert form["description_placeholders"]["days"] == "240"
    assert not hasattr(flow, "async_step_ignore")


@pytest.mark.asyncio
async def test_battery_needs_input_repair_clears_issue(hass: HomeAssistant):
    """Issuen tas bort direkt vid inskickat par istället för vid nästa poll."""
    entry, _ = _entry_with_coordinator(hass, battery_kwh=None, reserve=None)
    hass.config_entries.async_update_entry = MagicMock()
    ir.async_create_issue(
        hass, "wolta", "battery_needs_input_e1", is_fixable=True,
        severity=ir.IssueSeverity.WARNING, translation_key="battery_needs_input")
    flow = BatteryNeedsInputRepairFlow(entry, days=240)
    flow.hass = hass
    # Repairs-managern stämplar issue_id på flödet innan något steg körs
    # (components/repairs/issue_handler.py) – flödet raderar den id:t, inte en
    # egenbyggd sträng som kan glida ur fas med den som restes.
    flow.issue_id = "battery_needs_input_e1"
    await flow.async_step_set_nameplate({"nameplate_kwh": 12.0, "nameplate_kw": 6.0})
    assert ir.async_get(hass).async_get_issue("wolta", "battery_needs_input_e1") is None


@pytest.mark.asyncio
async def test_battery_needs_input_repair_speglar_parat_i_entry_data(hass: HomeAssistant):
    """Märkdata måste speglas i entry.data precis som _finish speglar de levererbara
    värdena. Nameplate-nycklarna ligger INTE i coordinatorns _PROFILE_SYNC_KEYS, så
    de kommer aldrig tillbaka från servern via profilspeglingen – en reauth bygger en
    ny profil ur entry.data och hade då återskapat raden UTAN märkdata, alltså rakt
    tillbaka i needs_input som användaren just svarat på."""
    from custom_components.wolta.const import CONF_NAMEPLATE_KW, CONF_NAMEPLATE_KWH

    entry, _ = _entry_with_coordinator(hass, battery_kwh=None, reserve=None)
    hass.config_entries.async_update_entry = MagicMock()
    flow = BatteryNeedsInputRepairFlow(entry, days=240)
    flow.hass = hass
    flow.issue_id = "battery_needs_input_e1"
    await flow.async_step_set_nameplate({"nameplate_kwh": 12.0, "nameplate_kw": 6.0})

    hass.config_entries.async_update_entry.assert_called_once()
    data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert data[CONF_NAMEPLATE_KWH] == 12.0
    assert data[CONF_NAMEPLATE_KW] == 6.0
    # Resten av entryn ska vara orörd (spread, inte ersättning).
    assert CONF_BATTERY_KWH in data


@pytest.mark.asyncio
async def test_battery_needs_input_repair_aborts_when_entry_missing(hass: HomeAssistant):
    flow = BatteryNeedsInputRepairFlow(None, days=240)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "abort"
    assert result["reason"] == "entry_not_found"


@pytest.mark.asyncio
async def test_fix_flow_dispatch_battery_needs_input(hass: HomeAssistant):
    entry, _ = _entry_with_coordinator(hass, battery_kwh=None, reserve=None)
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow = await async_create_fix_flow(hass, "battery_needs_input_e1", {"entry_id": "e1", "days": 7})
    assert isinstance(flow, BatteryNeedsInputRepairFlow)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["description_placeholders"]["days"] == "7"


# ---------------------------------------------------------------------------
# soc_required (spec 2026-10-20-soc-krav, 4.3): pick a SoC sensor, or say there is none
# ---------------------------------------------------------------------------

from homeassistant.helpers import issue_registry as ir  # noqa: E402

from custom_components.wolta.api import WoltaApiError  # noqa: E402
from custom_components.wolta.const import (  # noqa: E402
    CONF_SOC,
    CONF_SOC_ABSENT,
    CONF_SOC_ISSUE_IGNORED,
)
from custom_components.wolta.repairs import SocRequiredRepairFlow  # noqa: E402


def _soc_flow(hass, **data):
    entry, coordinator = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    entry.data = {**entry.data, **data}
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_schedule_reload = MagicMock()
    flow = SocRequiredRepairFlow(entry)
    flow.hass = hass
    flow.issue_id = "soc_required_e1"
    return flow, entry, coordinator


def _soc_sensor(hass, entity_id="sensor.batt_soc", *, unit="%", state_class="measurement"):
    attrs = {"unit_of_measurement": unit}
    if state_class is not None:
        attrs["state_class"] = state_class
    hass.states.async_set(entity_id, "55", attrs)


def _create_soc_issue(hass):
    ir.async_create_issue(hass, "wolta", "soc_required_e1", is_fixable=True,
                          severity=ir.IssueSeverity.WARNING, translation_key="soc_required")


def _soc_issue_open(hass):
    return ir.async_get(hass).async_get_issue("wolta", "soc_required_e1") is not None


_NET_ERRORS = [WoltaApiError("boom"), aiohttp.ClientError("down"), TimeoutError()]


@pytest.mark.asyncio
async def test_soc_repair_menu_has_no_ignore(hass: HomeAssistant):
    """A SoC answer is required, so there is nothing to dismiss: the menu offers the two
    answers and no 'ignore'."""
    flow, _, _ = _soc_flow(hass)
    menu = await flow.async_step_init()
    assert menu["type"] == "menu"
    assert menu["menu_options"] == ["pick_sensors", "soc_absent"]
    assert not hasattr(flow, "async_step_ignore")


@pytest.mark.asyncio
async def test_soc_repair_saves_choice_and_reloads(hass: HomeAssistant):
    """Picking sensors stores them in entry.data and reloads the entry - the coordinator
    reads the choice at start and schedules the SoC backfill itself. Nothing is PATCHed
    (the entity ids are client-local) unless the owner had said there is no sensor."""
    _soc_sensor(hass)
    flow, entry, coordinator = _soc_flow(hass)
    form = await flow.async_step_pick_sensors()
    assert form["type"] == "form" and form["step_id"] == "pick_sensors"

    result = await flow.async_step_pick_sensors({CONF_SOC: ["sensor.batt_soc"]})
    assert result["type"] == "create_entry"
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_SOC] == ["sensor.batt_soc"]
    hass.config_entries.async_schedule_reload.assert_called_once_with("e1")
    coordinator.client.patch_profile.assert_not_called()
    coordinator.async_trigger_recompute.assert_not_called()


@pytest.mark.asyncio
async def test_soc_repair_keeps_the_flow_sensors_untouched(hass: HomeAssistant):
    """Only the SoC key changes - the flow sensors (and with them the entity fingerprint
    that would trigger a full re-backfill) are carried over as they were."""
    _soc_sensor(hass)
    flow, entry, _ = _soc_flow(hass, batt_in=["sensor.a"], grid_in=["sensor.b"])
    before = dict(entry.data)
    await flow.async_step_pick_sensors({CONF_SOC: ["sensor.batt_soc"]})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert {k: v for k, v in new_data.items() if k != CONF_SOC} == before


@pytest.mark.asyncio
async def test_soc_repair_pick_clears_issue_and_stale_ignore_flag(hass: HomeAssistant):
    _soc_sensor(hass)
    flow, _, _ = _soc_flow(hass, **{CONF_SOC_ISSUE_IGNORED: True})
    _create_soc_issue(hass)
    await flow.async_step_pick_sensors({CONF_SOC: ["sensor.batt_soc"]})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert CONF_SOC_ISSUE_IGNORED not in new_data
    assert not _soc_issue_open(hass)


@pytest.mark.asyncio
async def test_soc_repair_pick_withdraws_an_earlier_absent_answer(hass: HomeAssistant):
    """CONF_SOC_ABSENT is mirrored from the server (a server value overwrites entry.data on
    every sync), so picking a sensor after 'no sensor' PATCHes soc_absent=False first and
    only then saves the sensors."""
    _soc_sensor(hass)
    flow, entry, coordinator = _soc_flow(hass, **{CONF_SOC_ABSENT: True})
    _create_soc_issue(hass)
    order: list[str] = []
    coordinator.client.patch_profile.side_effect = (
        lambda *a, **k: order.append("patch") or {})
    hass.config_entries.async_update_entry.side_effect = (
        lambda *a, **k: order.append("update"))

    result = await flow.async_step_pick_sensors({CONF_SOC: ["sensor.batt_soc"]})

    assert result["type"] == "create_entry"
    coordinator.client.patch_profile.assert_awaited_once_with("tok", soc_absent=False)
    assert order == ["patch", "update"]
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_SOC] == ["sensor.batt_soc"]
    assert not new_data.get(CONF_SOC_ABSENT)
    hass.config_entries.async_schedule_reload.assert_called_once_with("e1")
    assert not _soc_issue_open(hass)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", _NET_ERRORS)
async def test_soc_repair_pick_aborts_without_writing_when_the_patch_fails(
        hass: HomeAssistant, error):
    """The server still says 'no sensor': saving the sensors locally would be overwritten by
    the next profile sync. Abort, write nothing, leave the issue open."""
    _soc_sensor(hass)
    flow, _, coordinator = _soc_flow(hass, **{CONF_SOC_ABSENT: True})
    coordinator.client.patch_profile.side_effect = error
    _create_soc_issue(hass)
    result = await flow.async_step_pick_sensors({CONF_SOC: ["sensor.batt_soc"]})
    assert result["type"] == "abort" and result["reason"] == "failed"
    hass.config_entries.async_update_entry.assert_not_called()
    hass.config_entries.async_schedule_reload.assert_not_called()
    assert _soc_issue_open(hass)


@pytest.mark.asyncio
async def test_soc_repair_pick_validates_before_patching(hass: HomeAssistant):
    """A bad pick must not withdraw the earlier 'no sensor' answer."""
    _soc_sensor(hass, unit="kWh")
    flow, _, coordinator = _soc_flow(hass, **{CONF_SOC_ABSENT: True})
    result = await flow.async_step_pick_sensors({CONF_SOC: ["sensor.batt_soc"]})
    assert result["type"] == "form"
    assert result["errors"] == {CONF_SOC: "soc_not_measurement"}
    coordinator.client.patch_profile.assert_not_called()
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_soc_repair_absent_patches_then_writes_flag_and_closes(hass: HomeAssistant):
    """'No SoC sensor': PATCH soc_absent=True first, then entry.data, then the issue is
    deleted. No reload (the coordinator reads the flag live) and no ignore flag."""
    flow, entry, coordinator = _soc_flow(hass, **{CONF_SOC_ISSUE_IGNORED: True})
    _create_soc_issue(hass)
    order: list[str] = []
    coordinator.client.patch_profile.side_effect = (
        lambda *a, **k: order.append("patch") or {})
    hass.config_entries.async_update_entry.side_effect = (
        lambda *a, **k: order.append("update"))

    result = await flow.async_step_soc_absent()

    assert result["type"] == "create_entry"
    coordinator.client.patch_profile.assert_awaited_once_with("tok", soc_absent=True)
    assert order == ["patch", "update"]
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_SOC_ABSENT] is True
    assert CONF_SOC_ISSUE_IGNORED not in new_data
    hass.config_entries.async_schedule_reload.assert_not_called()
    assert not _soc_issue_open(hass)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", _NET_ERRORS)
async def test_soc_repair_absent_aborts_without_writing_when_the_patch_fails(
        hass: HomeAssistant, error):
    """A network error must not leave a local flag the server does not hold (the next
    profile sync would flip it back). Abort, write nothing, keep the issue open."""
    flow, _, coordinator = _soc_flow(hass)
    coordinator.client.patch_profile.side_effect = error
    _create_soc_issue(hass)
    result = await flow.async_step_soc_absent()
    assert result["type"] == "abort" and result["reason"] == "failed"
    hass.config_entries.async_update_entry.assert_not_called()
    assert _soc_issue_open(hass)


@pytest.mark.asyncio
@pytest.mark.parametrize("picked, sensor_kw, error", [
    ([], {}, "soc_required"),
    (["sensor.batt_soc"], {"unit": "kWh"}, "soc_not_measurement"),
    (["sensor.batt_soc"], {"state_class": "total"}, "soc_not_measurement"),
    (["sensor.gone"], {}, "soc_unavailable"),
])
async def test_soc_repair_rejects_bad_choice(hass: HomeAssistant, picked, sensor_kw, error):
    """Same rules as the setup and reconfigure pickers (soc_requirement_error): an empty
    choice is not a choice - 'no SoC sensor' is the menu's other option."""
    _soc_sensor(hass, **sensor_kw)
    flow, _, _ = _soc_flow(hass)
    result = await flow.async_step_pick_sensors({CONF_SOC: picked})
    assert result["type"] == "form"
    assert result["errors"] == {CONF_SOC: error}
    hass.config_entries.async_update_entry.assert_not_called()
    hass.config_entries.async_schedule_reload.assert_not_called()


def _picker(form):
    return next(v for k, v in form["data_schema"].schema.items() if k == CONF_SOC)


@pytest.mark.asyncio
async def test_soc_repair_field_is_optional_so_an_empty_submit_reaches_the_guidance(hass: HomeAssistant):
    """vol.Required would make the frontend refuse an empty submit, and the soc_required
    text (which points at the 'no SoC sensor' option) could never show."""
    flow, _, _ = _soc_flow(hass)
    form = await flow.async_step_pick_sensors()
    key = next(k for k in form["data_schema"].schema if k == CONF_SOC)
    assert isinstance(key, vol.Optional)


@pytest.mark.asyncio
async def test_soc_repair_lists_only_percent_measurement_sensors(hass: HomeAssistant):
    """The picker is narrowed to sensors that can pass validation: a level in % with
    statistics is listed, an energy counter or a % sensor without state_class is not."""
    _soc_sensor(hass, "sensor.batt_soc")
    _soc_sensor(hass, "sensor.energy", unit="kWh", state_class="total_increasing")
    _soc_sensor(hass, "sensor.percent_no_class", state_class=None)
    # % + measurement but known not to be a charge level: left out of the list (they
    # would still pass validation if picked by hand under Reconfigure).
    hass.states.async_set("sensor.bathroom_humidity", "61", {
        "unit_of_measurement": "%", "state_class": "measurement", "device_class": "humidity"})
    hass.states.async_set("sensor.inverter_power_factor", "98", {
        "unit_of_measurement": "%", "state_class": "measurement", "device_class": "power_factor"})
    # device_class battery is kept, and so is a % sensor without any device_class
    # (inverter integrations expose SoC either way).
    hass.states.async_set("sensor.phone_battery", "80", {
        "unit_of_measurement": "%", "state_class": "measurement", "device_class": "battery"})
    flow, _, _ = _soc_flow(hass)
    form = await flow.async_step_pick_sensors()
    selector = _picker(form)
    assert selector.config["include_entities"] == ["sensor.batt_soc", "sensor.phone_battery"]
    assert selector.config["multiple"] is True


@pytest.mark.asyncio
async def test_soc_repair_unfiltered_when_nothing_qualifies(hass: HomeAssistant):
    """An empty include list would render a dead picker; fall back to all sensors and
    let validation explain."""
    flow, _, _ = _soc_flow(hass)
    form = await flow.async_step_pick_sensors()
    assert not _picker(form).config.get("include_entities")


@pytest.mark.asyncio
async def test_soc_repair_aborts_when_entry_missing(hass: HomeAssistant):
    flow = SocRequiredRepairFlow(None)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "abort" and result["reason"] == "entry_not_found"


@pytest.mark.asyncio
async def test_fix_flow_dispatch_soc_required(hass: HomeAssistant):
    entry, _ = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow = await async_create_fix_flow(hass, "soc_required_e1", {"entry_id": "e1"})
    assert isinstance(flow, SocRequiredRepairFlow)


@pytest.mark.asyncio
async def test_fix_flow_dispatch_no_longer_knows_soc_missing(hass: HomeAssistant):
    """The legacy issue is deleted by the coordinator; a stale soc_missing id must not
    open the SoC flow (it falls through to the default dispatch)."""
    entry, _ = _entry_with_coordinator(hass, battery_kwh=10.0, reserve=None)
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow = await async_create_fix_flow(hass, "soc_missing_e1", {"entry_id": "e1"})
    assert not isinstance(flow, SocRequiredRepairFlow)


# ---------------------------------------------------------------------------
# Omkastade laddfält: fixflödet vänder CONF_INVERT_BATTERY
# ---------------------------------------------------------------------------

from custom_components.wolta.const import (  # noqa: E402
    CONF_DIRECTION_ISSUE_IGNORED,
    CONF_INVERT_BATTERY,
)
from custom_components.wolta.repairs import BatteryDirectionRepairFlow  # noqa: E402


def _direction_entry(invert):
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {CONF_INVERT_BATTERY: invert} if invert is not None else {}
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    coordinator.direction_change_pending = MagicMock(return_value=False)
    entry.runtime_data = coordinator
    return entry, coordinator


def _direction_flow(hass, entry, placeholders=None):
    flow = BatteryDirectionRepairFlow(entry, placeholders)
    flow.hass = hass
    flow.issue_id = "battery_direction_e1"
    return flow


@pytest.mark.asyncio
@pytest.mark.parametrize("before,after", [(None, True), (False, True), (True, False)])
async def test_direction_flip_toggles_invert_and_refreshes(hass: HomeAssistant, before, after):
    entry, coordinator = _direction_entry(before)
    hass.config_entries.async_update_entry = MagicMock()
    flow = _direction_flow(hass, entry)
    menu = await flow.async_step_init()
    assert set(menu["menu_options"]) == {"flip", "ignore"}
    form = await flow.async_step_flip()
    assert form["type"] == "form"
    result = await flow.async_step_flip({})
    assert result["type"] == "create_entry"
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_INVERT_BATTERY] is after
    assert CONF_DIRECTION_ISSUE_IGNORED not in new_data
    coordinator.async_request_refresh.assert_awaited()


@pytest.mark.asyncio
async def test_direction_flip_clears_ignore_flag_and_deletes_issue(hass: HomeAssistant):
    """Vändningen tar bort issuen direkt: servern har inte räknat om, och ett andra klick
    innan dess hade vänt tillbaka. Ignore-flaggan rensas (användaren engagerade sig)."""
    from homeassistant.helpers import issue_registry as ir
    ir.async_create_issue(hass, "wolta", "battery_direction_e1", is_fixable=True,
                          severity=ir.IssueSeverity.WARNING,
                          translation_key="battery_direction_all",
                          translation_placeholders={})
    entry, _c = _direction_entry(False)
    entry.data = {CONF_INVERT_BATTERY: False, CONF_DIRECTION_ISSUE_IGNORED: True}
    hass.config_entries.async_update_entry = MagicMock()
    flow = _direction_flow(hass, entry)
    await flow.async_step_flip({})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert CONF_DIRECTION_ISSUE_IGNORED not in new_data
    assert new_data[CONF_INVERT_BATTERY] is True
    assert ir.async_get(hass).async_get_issue("wolta", "battery_direction_e1") is None


@pytest.mark.asyncio
async def test_direction_flip_reads_current_flag_not_a_snapshot(hass: HomeAssistant):
    """Flaggan läses vid själva klicket (entry.data), inte vid flödets skapande."""
    entry, _c = _direction_entry(False)
    hass.config_entries.async_update_entry = MagicMock()
    flow = _direction_flow(hass, entry)
    entry.data = {CONF_INVERT_BATTERY: True}  # ändrades via options medan formuläret var öppet
    await flow.async_step_flip({})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_INVERT_BATTERY] is False


@pytest.mark.asyncio
async def test_direction_ignore_persists_flag(hass: HomeAssistant):
    entry, _c = _direction_entry(False)
    hass.config_entries.async_update_entry = MagicMock()
    flow = _direction_flow(hass, entry)
    result = await flow.async_step_ignore()
    assert result["type"] == "create_entry"
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_DIRECTION_ISSUE_IGNORED] is True
    assert new_data[CONF_INVERT_BATTERY] is False


@pytest.mark.asyncio
async def test_direction_aborts_when_entry_missing(hass: HomeAssistant):
    flow = BatteryDirectionRepairFlow(None)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "abort" and result["reason"] == "entry_not_found"


@pytest.mark.asyncio
async def test_create_fix_flow_dispatches_direction(hass: HomeAssistant):
    entry, _c = _direction_entry(False)
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow = await async_create_fix_flow(hass, "battery_direction_e1",
                                       {"entry_id": "e1", "form": "all", "reversed_days": "120",
                                        "batt_in": "sensor.a"})
    assert isinstance(flow, BatteryDirectionRepairFlow)
    flow.hass = hass
    menu = await flow.async_step_init()
    assert menu["description_placeholders"]["reversed_days"] == "120"
    assert menu["description_placeholders"]["batt_in"] == "sensor.a"
    # entry_id/form är flödets egna uppgifter, inte textens platshållare.
    assert "entry_id" not in menu["description_placeholders"]
    assert "form" not in menu["description_placeholders"]
    form = await flow.async_step_flip()
    assert form["description_placeholders"]["reversed_days"] == "120"
    assert form["description_placeholders"]["batt_in"] == "sensor.a"


@pytest.mark.asyncio
async def test_direction_flip_stamps_flipped_at(hass: HomeAssistant):
    """Stämpeln håller reparationen nere tills serverns riktningsdata är nyare."""
    from datetime import datetime, timedelta, timezone
    from homeassistant.util import dt as dt_util
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    entry, _c = _direction_entry(False)
    hass.config_entries.async_update_entry = MagicMock()
    flow = _direction_flow(hass, entry)
    await flow.async_step_flip({})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    stamp = dt_util.parse_datetime(new_data[CONF_DIRECTION_FLIPPED_AT])
    assert stamp is not None and stamp.tzinfo is not None
    assert abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=1)


# ---------------------------------------------------------------------------
# swap_intervals (server spec §10.1): the fix flips exactly the server's intervals
# ---------------------------------------------------------------------------

from custom_components.wolta.const import CONF_INVERT_INTERVALS  # noqa: E402

_SWAP_SINCE = [["2026-09-28T13:00:00+00:00", None]]


@pytest.mark.asyncio
async def test_direction_flip_writes_desired_sym_diff_swap_intervals(hass: HomeAssistant):
    """Desired [] Δ [[T, None]] = [[T, None]]: only the period is flipped, and the legacy
    flag stays off (the whole history is not inverted)."""
    entry, coordinator = _direction_entry(False)
    entry.data = {CONF_INVERT_BATTERY: False, CONF_INVERT_INTERVALS: []}
    hass.config_entries.async_update_entry = MagicMock()
    flow = BatteryDirectionRepairFlow(entry, {}, _SWAP_SINCE)
    flow.hass = hass
    flow.issue_id = "battery_direction_e1"
    await flow.async_step_flip({})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_INVERT_INTERVALS] == [["2026-09-28T13:00:00+00:00", None]]
    assert new_data[CONF_INVERT_BATTERY] is False
    coordinator.async_request_refresh.assert_awaited()


@pytest.mark.asyncio
async def test_direction_flip_with_everything_inverted_keeps_the_time_before(
    hass: HomeAssistant
):
    """Desired [[None, None]] Δ [[T, None]] = [[None, T]]: the period after T flips back."""
    entry, _c = _direction_entry(True)
    entry.data = {CONF_INVERT_BATTERY: True, CONF_INVERT_INTERVALS: [[None, None]]}
    hass.config_entries.async_update_entry = MagicMock()
    flow = BatteryDirectionRepairFlow(entry, {}, _SWAP_SINCE)
    flow.hass = hass
    flow.issue_id = "battery_direction_e1"
    await flow.async_step_flip({})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_INVERT_INTERVALS] == [[None, "2026-09-28T13:00:00+00:00"]]
    assert new_data[CONF_INVERT_BATTERY] is False


@pytest.mark.asyncio
async def test_direction_flip_without_swap_intervals_flips_the_whole_history(
    hass: HomeAssistant
):
    """An issue raised by an older version has no swap_intervals: the v0.43 rule."""
    entry, _c = _direction_entry(False)
    entry.data = {CONF_INVERT_BATTERY: False, CONF_INVERT_INTERVALS: []}
    hass.config_entries.async_update_entry = MagicMock()
    flow = _direction_flow(hass, entry)
    await flow.async_step_flip({})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_INVERT_INTERVALS] == [[None, None]]
    assert new_data[CONF_INVERT_BATTERY] is True


@pytest.mark.asyncio
async def test_create_fix_flow_passes_swap_intervals_not_as_a_placeholder(hass: HomeAssistant):
    entry, _c = _direction_entry(False)
    entry.data = {CONF_INVERT_BATTERY: False, CONF_INVERT_INTERVALS: []}
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    hass.config_entries.async_update_entry = MagicMock()
    flow = await async_create_fix_flow(hass, "battery_direction_e1", {
        "entry_id": "e1", "form": "since", "swap_intervals": _SWAP_SINCE,
        "swap_period": "2026-09-28 15:00 → …"})
    flow.hass = hass
    flow.issue_id = "battery_direction_e1"
    menu = await flow.async_step_init()
    assert "swap_intervals" not in menu["description_placeholders"]
    assert menu["description_placeholders"]["swap_period"] == "2026-09-28 15:00 → …"
    await flow.async_step_flip({})
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_INVERT_INTERVALS] == _SWAP_SINCE


@pytest.mark.asyncio
@pytest.mark.parametrize("user_input", [None, {}])
async def test_direction_flip_is_refused_while_a_direction_change_is_pending(
    hass: HomeAssistant, user_input
):
    """A direction job exists or desired ≠ applied: the stored rows do not show the last
    change yet, and a flip computed from a grade of the old direction would swap the
    same period back. The flip aborts with a reason and writes nothing."""
    entry, coordinator = _direction_entry(False)
    entry.data = {CONF_INVERT_BATTERY: False, CONF_INVERT_INTERVALS: []}
    coordinator.direction_change_pending.return_value = True
    hass.config_entries.async_update_entry = MagicMock()
    flow = BatteryDirectionRepairFlow(entry, {}, _SWAP_SINCE)
    flow.hass = hass
    flow.issue_id = "battery_direction_e1"

    result = await flow.async_step_flip(user_input)

    assert result["type"] == "abort"
    assert result["reason"] == "direction_pending"
    hass.config_entries.async_update_entry.assert_not_called()
    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.parametrize("lang", ["strings", "en", "sv"])
def test_direction_pending_abort_is_translated_for_every_fixable_key(lang):
    import json
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "custom_components" / "wolta"
    path = base / "strings.json" if lang == "strings" else base / "translations" / f"{lang}.json"
    issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
    for key in ("battery_direction_all", "battery_direction_partial",
                "battery_direction_since_flip", "battery_direction_ended_flip"):
        assert issues[key]["fix_flow"]["abort"]["direction_pending"]


from custom_components.wolta.api import (  # noqa: E402
    WoltaApiError,
    WoltaCopyBusy,
    WoltaCopyGone,
)
from custom_components.wolta.repairs import HouseCopiesRepairFlow  # noqa: E402


def _copies_entry(remove_side_effect=None):
    entry = MagicMock()
    entry.entry_id = "e1"
    coordinator = MagicMock()
    coordinator.token = "tok"
    coordinator.client = MagicMock()
    coordinator.client.remove_copy = AsyncMock(side_effect=remove_side_effect)
    entry.runtime_data = coordinator
    return entry, coordinator


@pytest.mark.asyncio
async def test_copies_flow_shows_list_and_removes_each(hass: HomeAssistant):
    entry, coordinator = _copies_entry()
    flow = HouseCopiesRepairFlow(entry, [12, 13], "- 2026-03-02\n- 2026-03-03", 30)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] == "form" and form["step_id"] == "confirm"
    assert form["description_placeholders"] == {
        "count": "2", "list": "- 2026-03-02\n- 2026-03-03", "days": "30"}
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    assert [c.args for c in coordinator.client.remove_copy.await_args_list] == [
        ("tok", 12), ("tok", 13)]


@pytest.mark.asyncio
async def test_copies_flow_skips_gone(hass: HomeAssistant):
    entry, coordinator = _copies_entry([WoltaCopyGone("gone"), None])
    flow = HouseCopiesRepairFlow(entry, [12, 13], "", 30)
    flow.hass = hass
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    assert coordinator.client.remove_copy.await_count == 2


@pytest.mark.asyncio
async def test_copies_flow_aborts_on_busy_and_stops(hass: HomeAssistant):
    """Busy aborts with reason "busy" and the loop stops at the first busy copy."""
    entry, coordinator = _copies_entry([WoltaCopyBusy("busy"), None])
    flow = HouseCopiesRepairFlow(entry, [12, 13], "", 30)
    flow.hass = hass
    result = await flow.async_step_confirm({})
    assert result["type"] == "abort" and result["reason"] == "busy"
    assert coordinator.client.remove_copy.await_count == 1


@pytest.mark.asyncio
async def test_copies_flow_aborts_on_other_errors(hass: HomeAssistant):
    entry, _ = _copies_entry([WoltaApiError("boom")])
    flow = HouseCopiesRepairFlow(entry, [12], "", 30)
    flow.hass = hass
    result = await flow.async_step_confirm({})
    assert result["type"] == "abort" and result["reason"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [aiohttp.ClientError("down"), TimeoutError()])
async def test_copies_flow_aborts_on_network_errors(hass: HomeAssistant, error):
    entry, _ = _copies_entry([error])
    flow = HouseCopiesRepairFlow(entry, [12], "", 30)
    flow.hass = hass
    result = await flow.async_step_confirm({})
    assert result["type"] == "abort" and result["reason"] == "failed"


@pytest.mark.asyncio
async def test_copies_flow_without_entry_aborts(hass: HomeAssistant):
    flow = HouseCopiesRepairFlow(None, [12], "", 30)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "abort" and result["reason"] == "entry_not_found"


@pytest.mark.asyncio
async def test_create_fix_flow_dispatches_house_copies(hass: HomeAssistant):
    flow = await async_create_fix_flow(
        hass, "house_copies_e1",
        {"entry_id": "e1", "ids": [12], "lines": "- 2026-03-02", "purge_days": 30})
    assert isinstance(flow, HouseCopiesRepairFlow)
