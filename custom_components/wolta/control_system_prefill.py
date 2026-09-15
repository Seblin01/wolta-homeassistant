"""Förslag på styrsystem ur batterisensorernas integrationsdomän (spec 2026-09-14 §7.1).

Mappningen domän → styrsystem ägs av backend (GET /control-systems); här bara uppslag och
majoritetsregeln. Ett förslag som lika gärna kan vara fel (oavgjort, okänd domän) är inget
förslag – då lämnas fältet utan default, som i dag.

Samma regel som webben (web/src/lib/control-system-suggest.ts). Håll dem i takt."""
from __future__ import annotations

from collections import Counter

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


def battery_platforms(hass: HomeAssistant, entity_ids: list[str]) -> list[str]:
    """Integrationsdomänen (RegistryEntry.platform) för varje entitet som finns i registret."""
    reg = er.async_get(hass)
    out: list[str] = []
    for eid in entity_ids:
        entry = reg.async_get(eid)
        if entry is not None and entry.platform:
            out.append(entry.platform)
    return out


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
