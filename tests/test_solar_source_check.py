"""Huawei-solsensor på AC-sidan (Total yield) i stället för DC-ingången.

Bakgrund (prod 2026-09-29, anläggning 156): en huawei_solar-ägare valde växelriktarens
*Total yield* som solsensor. Med ett DC-kopplat LUNA2000 mäter den AC-utgången – solel som
går rakt in i batteriet syns inte – så husets energi gick inte ihop (21,8 % omöjlig energi)
och betyget hölls utanför korpusen. Rätt sensor är *Total DC input energy*.

Nycklarna är verifierade i wlcrs/huawei_solar (sensor.py + translations/en.json, main
2026-09-22): translation_key = description.key.lower(), unique_id = f"{serial}_{key}"."""
from __future__ import annotations

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.wolta.solar_source_check import huawei_ac_yield_sensors


def _reg(hass, platform, unique_id, *, translation_key=None, object_id=None):
    return er.async_get(hass).async_get_or_create(
        "sensor", platform, unique_id, translation_key=translation_key,
        suggested_object_id=object_id).entity_id


@pytest.mark.asyncio
async def test_total_yield_flaggas(hass):
    eid = _reg(hass, "huawei_solar", "HV23A_accumulated_yield_energy",
               translation_key="accumulated_yield_energy", object_id="inverter_total_yield")
    assert huawei_ac_yield_sensors(hass, [eid]) == [eid]


@pytest.mark.asyncio
async def test_emma_inverter_total_energy_yield_flaggas(hass):
    eid = _reg(hass, "huawei_solar", "EMMA1_inverter_total_energy_yield",
               translation_key="inverter_total_energy_yield")
    assert huawei_ac_yield_sensors(hass, [eid]) == [eid]


@pytest.mark.asyncio
async def test_dc_input_energy_flaggas_inte(hass):
    eid = _reg(hass, "huawei_solar", "HV23A_total_dc_input_power",
               translation_key="total_dc_input_power", object_id="inverter_total_dc_input_energy")
    assert huawei_ac_yield_sensors(hass, [eid]) == []


@pytest.mark.asyncio
async def test_omdopt_entity_id_flaggas_anda(hass):
    """Registret, inte namnet, avgör: ägaren kan ha döpt om entiteten till vad som helst."""
    eid = _reg(hass, "huawei_solar", "HV23A_accumulated_yield_energy",
               translation_key="accumulated_yield_energy", object_id="solproduktion")
    assert eid == "sensor.solproduktion"
    assert huawei_ac_yield_sensors(hass, [eid]) == [eid]


@pytest.mark.asyncio
async def test_unique_id_fallback_utan_translation_key(hass):
    """Äldre registerposter kan sakna translation_key – unique_id bär nyckeln som suffix."""
    eid = _reg(hass, "huawei_solar", "HV23A_accumulated_yield_energy")
    assert huawei_ac_yield_sensors(hass, [eid]) == [eid]


@pytest.mark.asyncio
async def test_annan_plattform_med_samma_nyckel_flaggas_inte(hass):
    eid = _reg(hass, "sungrow", "X_accumulated_yield_energy",
               translation_key="accumulated_yield_energy")
    assert huawei_ac_yield_sensors(hass, [eid]) == []


@pytest.mark.asyncio
async def test_okand_entitet_och_tom_lista(hass):
    assert huawei_ac_yield_sensors(hass, ["sensor.finns_inte"]) == []
    assert huawei_ac_yield_sensors(hass, []) == []
