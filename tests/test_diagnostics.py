"""Tests for custom_components/wolta/diagnostics.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

from custom_components.wolta.const import CONF_TOKEN, CONF_ZONE


@pytest.mark.asyncio
async def test_diagnostics_redacts_token(hass: HomeAssistant):
    from custom_components.wolta.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MagicMock()
    entry.data = {CONF_TOKEN: "super-secret", CONF_ZONE: "SE3"}
    coordinator = MagicMock()
    coordinator._state = {"last_uploaded_ts": "2026-07-01T00:00:00+00:00"}
    coordinator.last_update_success = True
    coordinator.data = MagicMock(results={"status": "done"})
    entry.runtime_data = coordinator

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry_data"][CONF_TOKEN] == "**REDACTED**"
    assert diag["entry_data"][CONF_ZONE] == "SE3"
    assert diag["coordinator_state"]["last_uploaded_ts"] == "2026-07-01T00:00:00+00:00"
    assert diag["results"] == {"status": "done"}
    assert diag["last_update_success"] is True


@pytest.mark.asyncio
async def test_diagnostics_survives_missing_coordinator(hass: HomeAssistant):
    from custom_components.wolta.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = MagicMock()
    entry.data = {CONF_TOKEN: "super-secret"}
    entry.runtime_data = None

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry_data"][CONF_TOKEN] == "**REDACTED**"
    assert diag["results"] is None
    assert diag["completeness"] is None


@pytest.mark.asyncio
async def test_diagnostics_show_completeness_of_the_latest_read(hass: HomeAssistant):
    """How many quarters the latest read dropped as incomplete, and the sensors that
    caused it most - the first thing to look at when uploads stall."""
    from custom_components.wolta.diagnostics import async_get_config_entry_diagnostics

    entry = MagicMock()
    entry.data = {CONF_TOKEN: "super-secret", CONF_ZONE: "SE3"}
    coordinator = MagicMock()
    coordinator._state = {}
    coordinator._last_completeness = {
        "incomplete_quarters": 4, "entities": ["sensor.grid_in"]}
    coordinator.last_update_success = True
    coordinator.data = MagicMock(results={"status": "done"})
    entry.runtime_data = coordinator

    diag = await async_get_config_entry_diagnostics(hass, entry)

    completeness = diag["completeness"]
    assert isinstance(completeness["incomplete_quarters"], int)
    assert isinstance(completeness["entities"], list)
    assert completeness == {"incomplete_quarters": 4, "entities": ["sensor.grid_in"]}
    # A copy: the dump must not alias the coordinator's live dict.
    assert completeness is not coordinator._last_completeness


@pytest.mark.asyncio
async def test_diagnostics_redacts_plant_id(hass: HomeAssistant):
    """The plant id is a takeover key, not a label — it must not survive into a shared dump.

    Anyone holding it can re-onboard as this plant and take over its backend row, and users
    routinely attach diagnostics dumps to GitHub issues and forum posts.
    """
    from custom_components.wolta.const import CONF_PLANT_ID
    from custom_components.wolta.diagnostics import async_get_config_entry_diagnostics

    entry = MagicMock()
    entry.data = {CONF_TOKEN: "super-secret", CONF_ZONE: "SE3",
                  CONF_PLANT_ID: "a1b2c3d4" * 4}
    entry.runtime_data = None

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert "a1b2c3d4" not in str(diag)
    assert diag["entry_data"][CONF_PLANT_ID] == "**REDACTED**"


@pytest.mark.asyncio
async def test_diagnostics_redacts_link_token(hass: HomeAssistant):
    """The read link's token must not survive into a shared diagnostics dump either –
    it is a lesser-privileged credential than the owner token, but still a live,
    revocable capability (read + scoped economy/tariff writes) worth keeping out of
    GitHub issues and forum posts."""
    from custom_components.wolta.const import CONF_LINK_TOKEN
    from custom_components.wolta.diagnostics import async_get_config_entry_diagnostics

    entry = MagicMock()
    entry.data = {CONF_TOKEN: "super-secret", CONF_ZONE: "SE3",
                  CONF_LINK_TOKEN: "wpl_leak_me_not"}
    entry.runtime_data = None

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert "wpl_leak_me_not" not in str(diag)
    assert diag["entry_data"][CONF_LINK_TOKEN] == "**REDACTED**"


@pytest.mark.asyncio
async def test_diagnostics_show_coverage_memo_without_server_hash_prefix(hass: HomeAssistant):
    """Täckningsminnet syns i diagnostiken (felsökning), men dess tokenhash får inte vara
    ett prefix av serverns token_hash (= sha256(token)) – diagnostik klistras in i publika
    issues."""
    import hashlib

    from custom_components.wolta import coverage
    from custom_components.wolta.diagnostics import async_get_config_entry_diagnostics

    token = "super-secret"
    memo = coverage.memo(coverage.coverage_token(token), None)
    entry = MagicMock()
    entry.data = {CONF_TOKEN: token, CONF_ZONE: "SE3"}
    coordinator = MagicMock()
    coordinator._state = {"coverage": memo}
    coordinator.last_update_success = True
    coordinator.data = MagicMock(results={"status": "done"})
    entry.runtime_data = coordinator

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["coordinator_state"]["coverage"] == memo
    assert not hashlib.sha256(token.encode()).hexdigest().startswith(memo["token"])


@pytest.mark.asyncio
async def test_diagnostics_show_replacement_state_without_token(hass: HomeAssistant):
    """Desired against applied direction intervals, the running job and the backoff are
    visible for troubleshooting; the token is not."""
    from custom_components.wolta.const import CONF_INVERT_INTERVALS
    from custom_components.wolta.diagnostics import async_get_config_entry_diagnostics

    entry = MagicMock()
    entry.data = {CONF_TOKEN: "super-secret", CONF_ZONE: "SE3",
                  CONF_INVERT_INTERVALS: [["2026-10-01T00:00:00+00:00", None]]}
    coordinator = MagicMock()
    coordinator._state = {
        "applied_invert_intervals": [],
        "replacement_job": {
            "type": "direction",
            "target": [["2026-10-01T00:00:00+00:00", None]],
            "replacement": {
                "id": "rep-1", "kind": "swap_battery", "phase": "energy",
                "window_start": "2026-10-01T00:00:00+00:00",
                "window_end": "2026-10-05T00:00:00+00:00",
                "fields": None, "reason": "direction",
            },
            "done_windows": [["a", "b"], ["c", "d"]],
            "retry_at": "2026-10-13T12:00:00+00:00",
        },
        "job_backoff": {"entities": "2026-10-14T00:00:00+00:00"},
    }
    coordinator.last_update_success = True
    coordinator.data = MagicMock(results={"status": "done"})
    entry.runtime_data = coordinator

    diag = await async_get_config_entry_diagnostics(hass, entry)

    rep = diag["replacement"]
    assert rep["desired_invert_intervals"] == [["2026-10-01T00:00:00+00:00", None]]
    assert rep["applied_invert_intervals"] == []
    job = rep["job"]
    assert job["type"] == "direction"
    assert job["done_windows"] == 2
    assert job["retry_at"] == "2026-10-13T12:00:00+00:00"
    assert job["replacement"]["kind"] == "swap_battery"
    assert job["replacement"]["window_start"] == "2026-10-01T00:00:00+00:00"
    assert job["replacement"]["window_end"] == "2026-10-05T00:00:00+00:00"
    assert job["replacement"]["reason"] == "direction"
    assert rep["job_backoff"] == {"entities": "2026-10-14T00:00:00+00:00"}
    assert "super-secret" not in str(rep)


@pytest.mark.asyncio
async def test_diagnostics_replacement_without_job(hass: HomeAssistant):
    from custom_components.wolta.diagnostics import async_get_config_entry_diagnostics

    entry = MagicMock()
    entry.data = {CONF_TOKEN: "super-secret"}
    coordinator = MagicMock()
    coordinator._state = {}
    coordinator.last_update_success = True
    coordinator.data = MagicMock(results={})
    entry.runtime_data = coordinator

    rep = (await async_get_config_entry_diagnostics(hass, entry))["replacement"]
    assert rep == {"desired_invert_intervals": None, "applied_invert_intervals": None,
                   "job": None, "job_backoff": None}
