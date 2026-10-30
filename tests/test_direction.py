"""Tests for direction.py: the finish shared by every direction change, and the SoC verdict."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.wolta.const import (
    CONF_DIRECTION_CHANGED_AT,
    CONF_DIRECTION_FLIPPED_AT,
    CONF_DIRECTION_ISSUE_IGNORED,
    CONF_DIRECTION_ORIGIN,
    CONF_DIRECTION_OVERRIDE,
    CONF_INVERT_BATTERY,
    CONF_INVERT_INTERVALS,
    DOMAIN,
)
from custom_components.wolta.coordinator import (
    _ISSUE_BATTERY_DIRECTION,
    WoltaCoordinator,
)
from custom_components.wolta.direction import (
    Verdict,
    apply_direction_target,
    read_verdict,
    requires_override,
)

_AS_OF = "2026-10-30T04:45:10+00:00"


def _betyg(**block):
    return {"battery_direction": {"state": "verified", "checked_days": 40,
                                  "reversed_days": 0, "as_of": _AS_OF, **block}}


# ---------------------------------------------------------------------------
# read_verdict / requires_override
# ---------------------------------------------------------------------------

def test_verdict_current_when_as_of_after_change():
    verdict = read_verdict(_betyg(), "2026-10-29T12:00:00+00:00")
    assert verdict == Verdict(
        state="verified", checked_days=40, reversed_days=0,
        as_of=datetime(2026, 10, 30, 4, 45, 10, tzinfo=timezone.utc),
        fixable=False, current=True)
    assert requires_override(verdict) is True


@pytest.mark.parametrize("changed_at", [
    "2026-10-30T09:00:00+00:00",  # ändrat samma dag, efter nattens dom
    _AS_OF,                       # samma ögonblick: domen är inte senare
])
def test_verdict_stale_when_change_after_as_of(changed_at):
    """The owner changed the direction after the verdict was counted: it describes the old
    direction and gates nothing (undoing the same day needs no override)."""
    verdict = read_verdict(_betyg(), changed_at)
    assert verdict.current is False
    assert requires_override(verdict) is False


@pytest.mark.parametrize("as_of", [_AS_OF, None])
def test_verdict_current_without_change_stamp(as_of):
    """No change since the upgrade: whatever the server says is about today's direction."""
    verdict = read_verdict(_betyg(as_of=as_of), None)
    assert verdict.current is True
    assert requires_override(verdict) is True


@pytest.mark.parametrize("state,expected", [
    ("ok", True), ("verified", True), ("reversed", True), ("unknown", False), (None, False),
])
def test_requires_override_for_ok_verified_reversed(state, expected):
    assert requires_override(read_verdict(_betyg(state=state), None)) is expected


def test_requires_override_false_without_verdict():
    assert requires_override(None) is False


def test_read_verdict_none_without_betyg():
    """No grade loaded (e.g. right after a restart) is not the same as "no verdict"."""
    assert read_verdict(None, None) is None
    # A grade without the block (an older backend, or no SoC): no verdict, never current.
    empty = read_verdict({}, None)
    assert empty == Verdict(state=None, checked_days=0, reversed_days=0, as_of=None,
                            fixable=False, current=False)
    assert requires_override(empty) is False
    assert read_verdict({"battery_direction": "trasigt"}, None) == empty


def test_read_verdict_tolerates_malformed_fields():
    verdict = read_verdict(_betyg(checked_days="40", reversed_days=None, as_of="inte-ett-datum"),
                           "2026-10-29T12:00:00+00:00")
    assert verdict.checked_days == 0
    assert verdict.reversed_days == 0
    assert verdict.as_of is None
    # An unreadable as_of cannot be shown newer than the change.
    assert verdict.current is False
    assert read_verdict(_betyg(checked_days=True), None).checked_days == 0


def test_read_verdict_naive_times_are_utc():
    verdict = read_verdict(_betyg(as_of="2026-10-30T04:45:10"), "2026-10-30T04:00:00")
    assert verdict.as_of == datetime(2026, 10, 30, 4, 45, 10, tzinfo=timezone.utc)
    assert verdict.current is True
    # Offsets are compared as instants: 06:00+02:00 is 04:00Z, before the verdict.
    assert read_verdict(_betyg(), "2026-10-30T06:00:00+02:00").current is True
    assert read_verdict(_betyg(), "2026-10-30T07:00:00+02:00").current is False


def test_read_verdict_fixable_follows_swap_intervals():
    swap = [{"from": "2026-09-28T13:00:00+00:00", "to": None}]
    assert read_verdict(_betyg(state="reversed", swap_intervals=swap), None).fixable is True
    assert read_verdict(_betyg(state="reversed", swap_intervals=[]), None).fixable is False


# ---------------------------------------------------------------------------
# apply_direction_target
# ---------------------------------------------------------------------------

def _entry(data):
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = data
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    entry.runtime_data = coordinator
    return entry, coordinator


@pytest.mark.asyncio
async def test_apply_target_writes_all_keys_in_one_update(hass: HomeAssistant):
    issue_id = f"{_ISSUE_BATTERY_DIRECTION}_e1"
    assert issue_id == "battery_direction_e1"
    ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=True,
                          severity=ir.IssueSeverity.WARNING,
                          translation_key="battery_direction_all",
                          translation_placeholders={})
    entry, coordinator = _entry({CONF_INVERT_BATTERY: False, CONF_INVERT_INTERVALS: [],
                                 CONF_DIRECTION_ISSUE_IGNORED: True, "token": "tok"})
    hass.config_entries.async_update_entry = MagicMock()

    await apply_direction_target(hass, entry, [(None, None)], origin="manual", override=True)

    hass.config_entries.async_update_entry.assert_called_once()
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_INVERT_INTERVALS] == [[None, None]]
    assert new_data[CONF_INVERT_BATTERY] is True
    assert new_data[CONF_DIRECTION_ORIGIN] == "manual"
    assert new_data[CONF_DIRECTION_OVERRIDE] is True
    assert CONF_DIRECTION_ISSUE_IGNORED not in new_data
    assert new_data["token"] == "tok"
    assert new_data[CONF_DIRECTION_FLIPPED_AT] == new_data[CONF_DIRECTION_CHANGED_AT]
    stamp = dt_util.parse_datetime(new_data[CONF_DIRECTION_CHANGED_AT])
    assert stamp is not None and stamp.tzinfo is not None
    assert abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=1)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_target_override_defaults_to_false(hass: HomeAssistant):
    entry, _c = _entry({CONF_INVERT_INTERVALS: [[None, None]], CONF_INVERT_BATTERY: True})
    hass.config_entries.async_update_entry = MagicMock()
    await apply_direction_target(hass, entry, [], origin="repair")
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert new_data[CONF_INVERT_INTERVALS] == []
    assert new_data[CONF_INVERT_BATTERY] is False
    assert new_data[CONF_DIRECTION_ORIGIN] == "repair"
    assert new_data[CONF_DIRECTION_OVERRIDE] is False


@pytest.mark.asyncio
async def test_clear_direction_flags_keeps_changed_at_origin_override(hass: HomeAssistant):
    """A healthy verdict forgets the flip stamp and the ignore flag, never the change stamp
    (it decides whether a later verdict is current) nor the pending job's origin/override."""
    entry, _c = _entry({
        CONF_DIRECTION_FLIPPED_AT: "2026-10-12T10:00:00+00:00",
        CONF_DIRECTION_ISSUE_IGNORED: True,
        CONF_DIRECTION_CHANGED_AT: "2026-10-12T10:00:00+00:00",
        CONF_DIRECTION_ORIGIN: "manual",
        CONF_DIRECTION_OVERRIDE: True,
    })
    hass.config_entries.async_update_entry = MagicMock()
    WoltaCoordinator._clear_direction_flags(SimpleNamespace(hass=hass, config_entry=entry))
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert CONF_DIRECTION_FLIPPED_AT not in new_data
    assert CONF_DIRECTION_ISSUE_IGNORED not in new_data
    assert new_data[CONF_DIRECTION_CHANGED_AT] == "2026-10-12T10:00:00+00:00"
    assert new_data[CONF_DIRECTION_ORIGIN] == "manual"
    assert new_data[CONF_DIRECTION_OVERRIDE] is True
