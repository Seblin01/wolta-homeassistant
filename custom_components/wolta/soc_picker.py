"""Validation and candidate listing for the state-of-charge sensor pickers.

Shared by the setup flow, the reconfigure flow (config_flow.py) and the soc_missing
repair (repairs.py): one set of rules for what counts as a usable SoC sensor, so the
three pickers can never drift apart.
"""

from __future__ import annotations

from typing import Any

from . import stats

# The API's battery_sources takes at most 16 units; more would 422 the whole upload,
# flows included, so the pickers refuse to go past it.
MAX_SOC_SENSORS = 16

# Device classes that are reported in % with state_class measurement but are not a
# charge level. They pass validation (we cannot prove a % sensor is NOT a SoC), but the
# repair's candidate list leaves them out so a house full of humidity sensors does not
# bury the one battery sensor. battery and None are kept: inverter integrations expose
# SoC under either.
_NOT_SOC_DEVICE_CLASSES = frozenset({"humidity", "moisture", "power_factor"})


def soc_unvetted(hass: Any, entity_id: str) -> bool:
    """True when a SoC sensor cannot be vetted: it has no state at all (removed, not yet
    loaded), or its state is `unavailable`/`unknown` AND state_class or the unit is
    missing from the attributes.

    HA keeps both attributes on an `unavailable` sensor (helpers/entity.py writes the
    capability attributes and the unit before it checks `available`; the entity
    registry's write_unavailable_state does the same for entities not yet loaded after a
    restart), and an `unknown` sensor carries them as usual. So a valid sensor that is
    offline for the moment is vetted on its attributes like any other and must not block
    the form. Only a dead state WITHOUT them (set by hand, never reported) is
    unvettable - judging that on its attributes would blame the sensor's kind for what
    we simply cannot see."""
    state = hass.states.get(entity_id)
    if state is None:
        return True
    return state.state in ("unavailable", "unknown") and (
        state.attributes.get("state_class") is None
        or state.attributes.get("unit_of_measurement") is None
    )


def soc_invalid(hass: Any, entity_ids: list[str] | None) -> bool:
    """True if any picked SoC sensor (that can be vetted, see soc_unvetted) cannot serve
    as a charge-level source.

    Without state_class "measurement" there are no long-term statistics to read, and
    without unit "%" we cannot know the value is a charge level (spec 2026-10-03 §6.5).
    A sensor that cannot be vetted (soc_unvetted) is soc_error's "soc_unavailable",
    checked first.
    """
    for entity_id in entity_ids or []:
        if soc_unvetted(hass, entity_id):
            continue
        state = hass.states.get(entity_id)
        if (
            state.attributes.get("state_class") != "measurement"
            or state.attributes.get("unit_of_measurement") != "%"
        ):
            return True
    return False


def soc_error(hass: Any, entity_ids: list[str] | None) -> str | None:
    """Validation error key for a SoC picker, or None when the choice is fine.

    Order: unavailable -> not_measurement -> id_too_long -> too_many. "unavailable" is a
    sensor that cannot be vetted (soc_unvetted); an id over the API's `unit` limit would
    never be sent (stats.soc_unit_ok, the same rule the upload uses)."""
    ids = entity_ids or []
    if any(soc_unvetted(hass, entity_id) for entity_id in ids):
        return "soc_unavailable"
    if soc_invalid(hass, ids):
        return "soc_not_measurement"
    if not all(stats.soc_unit_ok(entity_id) for entity_id in ids):
        return "soc_id_too_long"
    if len(ids) > MAX_SOC_SENSORS:
        return "soc_too_many"
    return None


def soc_candidates(hass: Any) -> list[str]:
    """Sensors worth offering as a SoC source: a value in % with long-term statistics,
    minus the device classes that are known not to be a charge level. Sorted so the
    list is stable between renders."""
    return sorted(
        state.entity_id for state in hass.states.async_all("sensor")
        if state.attributes.get("unit_of_measurement") == "%"
        and state.attributes.get("state_class") == "measurement"
        and state.attributes.get("device_class") not in _NOT_SOC_DEVICE_CLASSES
    )
