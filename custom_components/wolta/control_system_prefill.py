"""Förslag på styrsystem ur batterisensorernas integrationsdomän (spec 2026-09-14 §7.1).

Mappningen domän → styrsystem ägs av backend (GET /control-systems); här bara uppslag och
majoritetsregeln. Ett förslag som lika gärna kan vara fel (oavgjort, okänd domän) är inget
förslag – då lämnas fältet utan default, som i dag.

Samma regel som webben (web/src/lib/control-system-suggest.ts). Håll dem i takt.

Här bor också väljaren och reglerna som onboardingens plant-steg och reparationen
control_system_required (repairs.py) delar: en lista, ett förslag, en namnregel."""
from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import CONTROL_SYSTEMS


# HA:s Riemann-summa-hjälpare (domän `integration`, kWh integrerat ur en effektsensor) är den
# VANLIGASTE batterisensorn i Energy-dashboarden (verifierat på Bronäs 2026-09-15). Dess
# platform är `integration`, som ingen mappning matchar, så förslaget tände sällan. Hjälparens
# config entry bär källan under `source` (homeassistant/components/integration/const.py,
# CONF_SOURCE_SENSOR) – följ den ETT hopp och läs källans plattform.
_SOURCE_HELPER_DOMAINS = frozenset({"integration"})
_CONF_SOURCE = "source"


def battery_platforms(hass: HomeAssistant, entity_ids: list[str]) -> list[str]:
    """Integrationsdomänen (RegistryEntry.platform) för varje entitet som finns i registret.

    En `integration`-hjälpare rapporterar sin KÄLLAS plattform om källan finns i registret;
    annars sin egen. Ett hopp, aldrig rekursion – en hjälpare på en hjälpare är inte värd
    en gissning."""
    reg = er.async_get(hass)
    out: list[str] = []
    for eid in entity_ids:
        entry = reg.async_get(eid)
        if entry is None or not entry.platform:
            continue
        out.append(_source_platform(hass, reg, entry) or entry.platform)
    return out


def _source_platform(
    hass: HomeAssistant, reg: er.EntityRegistry, entry: er.RegistryEntry
) -> str | None:
    if entry.platform not in _SOURCE_HELPER_DOMAINS or not entry.config_entry_id:
        return None
    cfg = hass.config_entries.async_get_entry(entry.config_entry_id)
    if cfg is None:
        return None
    source_id = cfg.options.get(_CONF_SOURCE) or cfg.data.get(_CONF_SOURCE)
    if not isinstance(source_id, str):
        return None
    src = reg.async_get(source_id)
    return src.platform if src is not None and src.platform else None


def suggest_control_system(platforms: list[str], mapping: list[dict]) -> str | None:
    by_domain: dict[str, str] = {}
    for row in mapping:
        for d in row.get("ha_domains") or []:
            by_domain[d] = row["id"]
    votes = Counter(by_domain[p] for p in platforms if p in by_domain)
    if not votes:
        return None
    ranked = votes.most_common(2)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


async def async_control_system_suggestion(client: Any, platforms: list[str]) -> str | None:
    """Förslaget för väljaren: batterisensorernas integrationsdomäner slagna mot backendens
    mappning (GET /control-systems), eller None.

    Kastar vid nätfel – anroparen fångar och går vidare utan förslag, så ett halvt uppslag
    aldrig stoppar ett flöde. Backendens lista ligger FÖRE den lokala (se
    const.CONTROL_SYSTEMS): ett id vi inte känner igen som default hade förvalt ett värde som
    SelectSelector själv avvisar, så okänt ⇒ inget förslag."""
    suggestion = suggest_control_system(platforms, await client.get_control_systems())
    return suggestion if suggestion in {c for c, _ in CONTROL_SYSTEMS} else None


def control_system_selector() -> SelectSelector:
    """Väljaren med const.CONTROL_SYSTEMS (samma lista i onboardingen och reparationen)."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=cs_id, label=label)
                for cs_id, label in CONTROL_SYSTEMS
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def control_system_name_selector() -> TextSelector:
    return TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))


def control_system_name_error(control_system: str | None, name: str | None) -> str | None:
    """Felnyckel för namnregeln, eller None. "other" utan fritextnamn ger ingen
    segmenteringssignal – samma regel som webbguiden (lib/guide.ts canAdvance)."""
    if control_system == "other" and not (name or "").strip():
        return "control_system_name_required"
    return None
