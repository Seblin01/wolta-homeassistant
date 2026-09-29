"""Huawei-solsensor på AC-sidan i stället för DC-ingången.

Med ett DC-kopplat Huawei-batteri (LUNA2000) mäter växelriktarens *Total yield* AC-utgången:
solel som går rakt in i batteriet syns inte, men batteriets urladdning gör det. Wolta räknar
då ut en husförbrukning som inte går ihop, och betyget hålls utanför jämförelsen med andra
(prod 2026-09-29, anläggning 156: 21,8 % omöjlig energi). Rätt sensor är *Total DC input
energy* – samma råd som wolta.se/optimeringsbetyg/huawei.

Känns igen via ENTITETSREGISTRET (plattform + translation_key), aldrig via entity_id: ägaren
kan ha döpt om entiteten. Nycklarna är verifierade i wlcrs/huawei_solar (sensor.py +
translations/en.json, main 2026-09-22): translation_key = description.key.lower() och
unique_id = f"{serienummer}_{key}".
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

HUAWEI_PLATFORM = "huawei_solar"
# "Total yield" (växelriktare) och "Inverter total energy yield" (EMMA, summan av
# växelriktarnas AC-utgångar). DC-ingången heter `total_dc_input_power` hos huawei_solar
# trots att den är en energisensor – den är den RÄTTA och flaggas aldrig.
AC_YIELD_KEYS = frozenset({"accumulated_yield_energy", "inverter_total_energy_yield"})


def huawei_ac_yield_sensors(hass: HomeAssistant, entity_ids: list[str]) -> list[str]:
    """De entiteter i `entity_ids` som är huawei_solars AC-räknare för solproduktion."""
    reg = er.async_get(hass)
    out: list[str] = []
    for eid in entity_ids:
        entry = reg.async_get(eid)
        if entry is None or entry.platform != HUAWEI_PLATFORM:
            continue
        if _key(entry) in AC_YIELD_KEYS:
            out.append(eid)
    return out


def _key(entry: er.RegistryEntry) -> str | None:
    if entry.translation_key:
        return entry.translation_key
    # Äldre registerposter saknar translation_key; unique_id bär nyckeln som suffix.
    uid = (entry.unique_id or "").lower()
    return next((k for k in AC_YIELD_KEYS if uid.endswith(f"_{k}")), None)
