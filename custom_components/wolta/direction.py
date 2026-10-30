"""Batteriets riktning: what every change of the desired direction shares.

A change is finished the same way whether it comes from the battery_direction repair or
from the owner by hand: one entry update with the new desired intervals and the stamps,
then the repair is taken down and the coordinator refreshed. Its direction job swaps the
battery fields of the stored rows on the server; nothing is re-sent.

The server's SoC verdict (the betyg's ``battery_direction`` block) is read here too, with
whether it is current: a verdict counted before the owner's last change describes the old
direction and must not gate undoing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DIRECTION_CHANGED_AT,
    CONF_DIRECTION_FLIPPED_AT,
    CONF_DIRECTION_ISSUE_IGNORED,
    CONF_DIRECTION_ORIGIN,
    CONF_DIRECTION_OVERRIDE,
    DOMAIN,
)
from .coordinator import (
    _ISSUE_BATTERY_DIRECTION,
    direction_swap_intervals,
    with_desired_intervals,
)
from .intervals import Interval

# Domar som servern står fast vid: en vändning emot dem nekas utan override.
_GATED_STATES = ("ok", "verified", "reversed")


@dataclass(frozen=True)
class Verdict:
    """The server's SoC verdict on the battery direction, as this client reads it."""

    state: str | None  # unknown / ok / verified / reversed; None without a block
    checked_days: int  # includes the unclear days
    reversed_days: int
    as_of: datetime | None  # UTC; when the server last counted the direction days
    fixable: bool  # the server gives intervals the repair could flip
    current: bool  # counted after the owner's last change of the direction


def _parse_utc(value: Any) -> datetime | None:
    """An ISO time as an aware UTC datetime. A naive time is taken as UTC; anything
    unreadable is None."""
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return dt_util.as_utc(parsed) if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)


def _count(value: Any) -> int:
    # bool är en int i Python, men inget antal dygn.
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def read_verdict(betyg: dict | None, changed_at: str | None) -> Verdict | None:
    """The verdict in ``betyg``, current or not against ``changed_at``
    (CONF_DIRECTION_CHANGED_AT). None when no grade is loaded at all - not the same as a
    grade without a verdict, which is a Verdict with state None that is never current.

    A verdict is current when its as_of is later than the last change, or when there is
    no (readable) change stamp: then nothing has changed the direction since it was
    counted."""
    if betyg is None:
        return None
    block = betyg.get("battery_direction") if isinstance(betyg, dict) else None
    if not isinstance(block, dict):
        return Verdict(state=None, checked_days=0, reversed_days=0, as_of=None,
                       fixable=False, current=False)
    state = block.get("state")
    as_of = _parse_utc(block.get("as_of"))
    changed = _parse_utc(changed_at)
    return Verdict(
        state=state if isinstance(state, str) else None,
        checked_days=_count(block.get("checked_days")),
        reversed_days=_count(block.get("reversed_days")),
        as_of=as_of,
        fixable=bool(direction_swap_intervals(block)),
        current=changed is None or (as_of is not None and as_of > changed),
    )


def requires_override(verdict: Verdict | None) -> bool:
    """Whether a manual change must be confirmed against the verdict: the server refuses
    a swap that goes against a current ok, verified or reversed verdict without override."""
    return verdict is not None and verdict.current and verdict.state in _GATED_STATES


async def apply_direction_target(
    hass: HomeAssistant,
    entry: ConfigEntry,
    target: list[Interval],
    *,
    origin: Literal["repair", "manual"],
    override: bool = False,
) -> None:
    """Make ``target`` the desired battery inversion. ONE entry update: the intervals, both
    stamps, the origin and override the coordinator copies to the direction job, and the
    ignore flag cleared (the owner engaged with the direction)."""
    now = dt_util.utcnow().isoformat()
    new_data = with_desired_intervals(entry.data, target)
    new_data[CONF_DIRECTION_FLIPPED_AT] = now
    new_data[CONF_DIRECTION_CHANGED_AT] = now
    new_data[CONF_DIRECTION_ORIGIN] = origin
    new_data[CONF_DIRECTION_OVERRIDE] = override
    new_data.pop(CONF_DIRECTION_ISSUE_IGNORED, None)
    hass.config_entries.async_update_entry(entry, data=new_data)
    # Issuen tas bort direkt, och koordinatorn håller den nere medan riktningsjobbet
    # pågår och tills serverns riktningsdata (battery_direction.as_of) är nyare än
    # CONF_DIRECTION_FLIPPED_AT: ett betyg hämtat före omräkningen säger fortfarande
    # "reversed" och hade annars rest reparationen igen (och ett andra klick vänt tillbaka).
    ir.async_delete_issue(hass, DOMAIN, f"{_ISSUE_BATTERY_DIRECTION}_{entry.entry_id}")
    await entry.runtime_data.async_request_refresh()
