"""Förslag på styrsystem ur batterisensorernas integrationsdomän (spec 2026-09-14 §7.1).

Mappningen domän → styrsystem ägs av backend (GET /control-systems); här bara uppslag och
majoritetsregeln. Ett förslag som lika gärna kan vara fel (oavgjort, okänd domän) är inget
förslag – då lämnas fältet utan default, som i dag.

Samma regel som webben (web/src/lib/control-system-suggest.ts). Håll dem i takt."""
from __future__ import annotations

from collections import Counter

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


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
