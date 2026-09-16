"""Förslag på styrsystem ur batterisensorernas integrationsdomän (spec 2026-09-14 §7.1)."""
from __future__ import annotations

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.wolta.control_system_prefill import battery_platforms, suggest_control_system

MAPPING = [
    {"id": "huawei", "label": "Huawei", "ha_domains": ["huawei_solar"]},
    {"id": "sonnen", "label": "Sonnen", "ha_domains": ["sonnenbatterie"]},
    {"id": "other", "label": "Other", "ha_domains": []},
]


def test_majoritet_ger_forslag():
    assert suggest_control_system(["huawei_solar", "huawei_solar", "mqtt"], MAPPING) == "huawei"


def test_okand_domän_ger_none():
    assert suggest_control_system(["mqtt", "template"], MAPPING) is None


def test_oavgjort_ger_none():
    assert suggest_control_system(["huawei_solar", "sonnenbatterie"], MAPPING) is None


def test_tom_indata():
    assert suggest_control_system([], MAPPING) is None
    assert suggest_control_system(["huawei_solar"], []) is None


@pytest.mark.asyncio
async def test_battery_platforms_lasser_ur_registret(hass):
    """platform per entitet ur entity-registret; okänt entity-id hoppas."""
    reg = er.async_get(hass)
    e1 = reg.async_get_or_create("sensor", "huawei_solar", "unique1").entity_id
    e2 = reg.async_get_or_create("sensor", "sonnenbatterie", "unique2").entity_id
    assert battery_platforms(hass, [e1, e2, "sensor.okand_entitet"]) == [
        "huawei_solar",
        "sonnenbatterie",
    ]


@pytest.mark.asyncio
async def test_battery_platforms_tom_lista(hass):
    assert battery_platforms(hass, []) == []


@pytest.mark.asyncio
async def test_battery_platforms_foljer_riemann_hjalparens_kalla(hass):
    """Energy-dashboardens batterisensorer är ofta HA:s `integration`-hjälpare (Riemann-summa
    kWh ur en effektsensor). Deras platform är `integration`, som ingen mappning matchar –
    förslaget tände därför sällan för den VANLIGASTE uppsättningen (verifierat på Bronäs
    2026-09-15). Följ hjälparens `source`-entitet ETT hopp och läs DEN plattformen."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    reg = er.async_get(hass)
    src = reg.async_get_or_create("sensor", "huawei_solar", "batt_power").entity_id
    helper_entry = MockConfigEntry(domain="integration", options={"source": src})
    helper_entry.add_to_hass(hass)
    helper = reg.async_get_or_create(
        "sensor", "integration", "batt_kwh", config_entry=helper_entry
    ).entity_id
    assert battery_platforms(hass, [helper]) == ["huawei_solar"]


@pytest.mark.asyncio
async def test_battery_platforms_hjalpare_utan_kand_kalla_forblir_hjalparen(hass):
    """Saknas källan i registret (eller är hjälparen inte en `integration`) gäller
    entitetens egen plattform – ett hopp, aldrig gissning."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    reg = er.async_get(hass)
    helper_entry = MockConfigEntry(domain="integration", options={"source": "sensor.borta"})
    helper_entry.add_to_hass(hass)
    helper = reg.async_get_or_create(
        "sensor", "integration", "batt_kwh2", config_entry=helper_entry
    ).entity_id
    assert battery_platforms(hass, [helper]) == ["integration"]
