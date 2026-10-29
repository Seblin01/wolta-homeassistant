"""Förslag på styrsystem ur batterisensorernas integrationsdomän (spec 2026-09-14 §7.1).

Mappningen domän → styrsystem ägs av backend (GET /control-systems); här bara uppslag och
majoritetsregeln. Ett förslag som lika gärna kan vara fel (oavgjort, okänd domän) är inget
förslag – då lämnas fältet utan default, som i dag.

Samma regel som webben (web/src/lib/control-system-suggest.ts). Håll dem i takt.

Här bor också väljaren och reglerna som onboardingens plant-steg och reparationen
control_system_required (repairs.py) delar: en lista, ett förslag, en namnregel.

Listan kommer sedan 2026-10-29 ur samma svar som mappningen (`label_en`, api >= 0.123.0), så
att ett styrsystem som läggs till på servern går att välja utan en ny release. const.py är
reserven när anropet misslyckas eller en äldre server svarar utan engelska etiketter. Ett
halvt svar används aldrig: en lista som tappat ett system hade dolt ägarens eget val."""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
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

_LOGGER = logging.getLogger(__name__)


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


def sorted_options(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Alphabetical by label (case-insensitive), Other last - the order on wolta.se too."""
    rest = sorted((p for p in pairs if p[0] != "other"), key=lambda p: p[1].casefold())
    return rest + [p for p in pairs if p[0] == "other"]


def options_from_server(rows: Any) -> list[tuple[str, str]] | None:
    """The picker's options from GET /control-systems, or None when the answer is not a
    complete list: every row needs an id and an English label, the ids are unique, and Other
    is there (without it an unlisted system cannot be named). None means: use const."""
    if not isinstance(rows, list) or not rows:
        return None
    # A server from before api 0.123.0 sends no label_en at all: the expected fallback, quiet.
    # An answer where SOME rows carry it is a broken server list and is logged as a warning,
    # since the fallback then hides whatever the server added after this release.
    if not any(isinstance(r, dict) and "label_en" in r for r in rows):
        return None
    pairs: list[tuple[str, str]] = []
    problem = None
    for row in rows:
        if not isinstance(row, dict):
            problem = f"row is not an object: {row!r}"
            break
        cs_id, label = row.get("id"), row.get("label_en")
        if not (isinstance(cs_id, str) and cs_id and isinstance(label, str) and label.strip()):
            problem = f"row without id or label_en: {row!r}"
            break
        pairs.append((cs_id, label.strip()))
    if problem is None:
        ids = [cs_id for cs_id, _ in pairs]
        if "other" not in ids:
            problem = "no 'other' row"
        elif len(ids) != len(set(ids)):
            problem = "duplicate ids"
    if problem is not None:
        _LOGGER.warning("Wolta control-system list incomplete (%s); using the built-in list",
                        problem)
        return None
    return sorted_options(pairs)


@dataclass(frozen=True)
class ControlSystemChoices:
    """What the picker shows and what it starts on. `suggestion` is always one of
    `options` or None: a default outside the options is rejected by SelectSelector."""

    options: list[tuple[str, str]]
    suggestion: str | None


def fallback_choices() -> ControlSystemChoices:
    return ControlSystemChoices(sorted_options(CONTROL_SYSTEMS), None)


async def async_control_system_choices(client: Any, platforms: list[str]) -> ControlSystemChoices:
    """The options (the server's list, else const) and the suggestion from the battery
    sensors' integration domains against the backend's mapping, from ONE request.

    Raises on a network error - the caller catches and uses fallback_choices(), so a failed
    lookup never stops a flow."""
    rows = await client.get_control_systems()
    options = options_from_server(rows) or sorted_options(CONTROL_SYSTEMS)
    suggestion = suggest_control_system(platforms, rows if isinstance(rows, list) else [])
    return ControlSystemChoices(
        options, suggestion if suggestion in {c for c, _ in options} else None)


def control_system_selector(options: list[tuple[str, str]] | None = None) -> SelectSelector:
    """The picker (same in onboarding and the repair). Without options: const's list."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=cs_id, label=label)
                for cs_id, label in (options or sorted_options(CONTROL_SYSTEMS))
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
