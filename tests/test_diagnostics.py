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
