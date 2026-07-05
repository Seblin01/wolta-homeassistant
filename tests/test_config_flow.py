"""Tests for custom_components/wolta/config_flow.py (TDD)."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

# All tests in this module that use the `hass` fixture need the wolta integration
# to be discoverable. This mark applies enable_custom_integrations to all tests
# without having to list it in every function signature.
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

from custom_components.wolta.const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_BATTERY_KW,
    CONF_BATTERY_KWH,
    CONF_EFF,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_SHARE,
    CONF_SOLAR,
    CONF_TOKEN,
    CONF_ZONE,
    DEFAULT_EFF,
    DEFAULT_SHARE,
    DOMAIN,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOKEN = "tok-abc123"
ZONE = "SE3"

STEP_USER_DATA = {
    CONF_ZONE: ZONE,
    CONF_BATTERY_KWH: 22.0,
    CONF_BATTERY_KW: 5.0,
    CONF_EFF: 0.9,
}

STEP_ENTITIES_DATA = {
    CONF_BATT_IN: ["sensor.battery_charge"],
    CONF_BATT_OUT: ["sensor.battery_discharge"],
    CONF_GRID_IN: ["sensor.grid_import"],
    CONF_GRID_OUT: ["sensor.grid_export"],
    CONF_SOLAR: ["sensor.solar_production"],
}

STEP_ENTITIES_NO_SOLAR = {
    CONF_BATT_IN: ["sensor.battery_charge"],
    CONF_BATT_OUT: ["sensor.battery_discharge"],
    CONF_GRID_IN: ["sensor.grid_import"],
    CONF_GRID_OUT: ["sensor.grid_export"],
}

STEP_PRIVACY_DATA = {
    CONF_SHARE: False,
}


def _mock_client(token: str = TOKEN) -> MagicMock:
    """Return a mock WoltaApiClient whose create_profile returns TOKEN."""
    mock = MagicMock()
    mock.create_profile = AsyncMock(return_value=token)
    return mock


# ---------------------------------------------------------------------------
# Full happy-path flow: user → entities → privacy → entry created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_creates_entry(hass: HomeAssistant) -> None:
    """Full user→entities→privacy flow creates a config entry with correct data."""
    mock_client = _mock_client()

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
        patch(
            "custom_components.wolta.config_flow._energy_dashboard_defaults",
            return_value={},
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "entities"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "privacy"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Wolta ({ZONE})"

    data = result["data"]
    assert data[CONF_TOKEN] == TOKEN
    assert data[CONF_ZONE] == ZONE
    assert data[CONF_BATT_IN] == ["sensor.battery_charge"]
    assert data[CONF_BATT_OUT] == ["sensor.battery_discharge"]
    assert data[CONF_GRID_IN] == ["sensor.grid_import"]
    assert data[CONF_GRID_OUT] == ["sensor.grid_export"]
    assert data[CONF_SOLAR] == ["sensor.solar_production"]
    assert data[CONF_BATTERY_KWH] == 22.0
    assert data[CONF_BATTERY_KW] == 5.0
    assert data[CONF_EFF] == 0.9
    assert data[CONF_SHARE] is False


@pytest.mark.asyncio
async def test_full_flow_unique_id_is_sha256_prefix(hass: HomeAssistant) -> None:
    """Entry unique_id is sha256(token)[:16]."""
    mock_client = _mock_client()
    expected_unique_id = hashlib.sha256(TOKEN.encode()).hexdigest()[:16]

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
        patch(
            "custom_components.wolta.config_flow._energy_dashboard_defaults",
            return_value={},
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # The entry is returned from async_create_entry; unique_id is set on flow
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].unique_id == expected_unique_id


@pytest.mark.asyncio
async def test_full_flow_no_solar(hass: HomeAssistant) -> None:
    """Flow completes without solar entity (solar is optional)."""
    mock_client = _mock_client()

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
        patch(
            "custom_components.wolta.config_flow._energy_dashboard_defaults",
            return_value={},
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_NO_SOLAR
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert CONF_SOLAR not in data or not data.get(CONF_SOLAR)


# ---------------------------------------------------------------------------
# Energy-dashboard prefill
# ---------------------------------------------------------------------------


def _make_energy_manager(sources: list[dict]) -> MagicMock:
    """Return a mock energy manager with the given sources."""
    manager = MagicMock()
    manager.data = {"energy_sources": sources}
    return manager


@pytest.mark.asyncio
async def test_energy_prefill_unified_grid_format(hass: HomeAssistant) -> None:
    """Unified grid format (stat_energy_from/to directly on source) prefills correctly."""
    from custom_components.wolta.config_flow import _energy_dashboard_defaults

    manager = _make_energy_manager(
        [
            {
                "type": "battery",
                "stat_energy_to": "sensor.batt_in",
                "stat_energy_from": "sensor.batt_out",
            },
            {
                "type": "grid",
                "stat_energy_from": "sensor.grid_in",
                "stat_energy_to": "sensor.grid_out",
            },
            {
                "type": "solar",
                "stat_energy_from": "sensor.solar",
            },
        ]
    )

    with patch(
        "custom_components.wolta.config_flow.async_get_manager",
        return_value=manager,
    ):
        defaults = await _energy_dashboard_defaults(hass)

    assert defaults[CONF_BATT_IN] == ["sensor.batt_in"]
    assert defaults[CONF_BATT_OUT] == ["sensor.batt_out"]
    assert defaults[CONF_GRID_IN] == ["sensor.grid_in"]
    assert defaults[CONF_GRID_OUT] == ["sensor.grid_out"]
    assert defaults[CONF_SOLAR] == ["sensor.solar"]


@pytest.mark.asyncio
async def test_energy_prefill_legacy_flow_format(hass: HomeAssistant) -> None:
    """Legacy grid format (flow_from/flow_to lists) prefills correctly."""
    from custom_components.wolta.config_flow import _energy_dashboard_defaults

    manager = _make_energy_manager(
        [
            {
                "type": "battery",
                "stat_energy_to": "sensor.batt_in",
                "stat_energy_from": "sensor.batt_out",
            },
            {
                "type": "grid",
                "flow_from": [{"stat_energy_from": "sensor.grid_in_legacy"}],
                "flow_to": [{"stat_energy_to": "sensor.grid_out_legacy"}],
            },
        ]
    )

    with patch(
        "custom_components.wolta.config_flow.async_get_manager",
        return_value=manager,
    ):
        defaults = await _energy_dashboard_defaults(hass)

    assert defaults[CONF_GRID_IN] == ["sensor.grid_in_legacy"]
    assert defaults[CONF_GRID_OUT] == ["sensor.grid_out_legacy"]


@pytest.mark.asyncio
async def test_energy_prefill_no_energy_dashboard(hass: HomeAssistant) -> None:
    """When energy component is unavailable, prefill returns empty dict."""
    from custom_components.wolta.config_flow import _energy_dashboard_defaults

    with patch(
        "custom_components.wolta.config_flow.async_get_manager",
        side_effect=Exception("energy not configured"),
    ):
        defaults = await _energy_dashboard_defaults(hass)

    assert defaults == {}


@pytest.mark.asyncio
async def test_energy_prefill_skips_non_sensor_entities(hass: HomeAssistant) -> None:
    """Prefill only includes values starting with 'sensor.'."""
    from custom_components.wolta.config_flow import _energy_dashboard_defaults

    manager = _make_energy_manager(
        [
            {
                "type": "grid",
                "stat_energy_from": "input_number.grid_in",  # NOT a sensor
                "stat_energy_to": "sensor.grid_out",
            },
        ]
    )

    with patch(
        "custom_components.wolta.config_flow.async_get_manager",
        return_value=manager,
    ):
        defaults = await _energy_dashboard_defaults(hass)

    assert CONF_GRID_IN not in defaults
    assert defaults.get(CONF_GRID_OUT) == ["sensor.grid_out"]


# ---------------------------------------------------------------------------
# API error on create → cannot_connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_error_shows_cannot_connect(hass: HomeAssistant) -> None:
    """WoltaApiError during create_profile shows cannot_connect in privacy step."""
    from custom_components.wolta.api import WoltaApiError

    mock_client = MagicMock()
    mock_client.create_profile = AsyncMock(side_effect=WoltaApiError("failed"))

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
        patch(
            "custom_components.wolta.config_flow._energy_dashboard_defaults",
            return_value={},
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        # Privacy step with API failure
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "privacy"
    assert result["errors"].get("base") == "cannot_connect"


# ---------------------------------------------------------------------------
# share default is False (privacy step default)
# ---------------------------------------------------------------------------


def test_default_share_is_false() -> None:
    """DEFAULT_SHARE must be False (privacy opt-in, not opt-out)."""
    assert DEFAULT_SHARE is False


# ---------------------------------------------------------------------------
# C1: battery defaults are non-zero and min_val > 0
# ---------------------------------------------------------------------------


def test_battery_defaults_nonzero() -> None:
    """DEFAULT_BATTERY_KWH and DEFAULT_BATTERY_KW must be > 0 to avoid 422."""
    from custom_components.wolta.const import (
        DEFAULT_BATTERY_KW,
        DEFAULT_BATTERY_KWH,
        MIN_BATTERY_KW,
        MIN_BATTERY_KWH,
    )

    assert DEFAULT_BATTERY_KWH > 0, "DEFAULT_BATTERY_KWH must be > 0"
    assert DEFAULT_BATTERY_KW > 0, "DEFAULT_BATTERY_KW must be > 0"
    assert MIN_BATTERY_KWH > 0, "MIN_BATTERY_KWH must be > 0"
    assert MIN_BATTERY_KW > 0, "MIN_BATTERY_KW must be > 0"


# ---------------------------------------------------------------------------
# C1: 422 → invalid_input (not cannot_connect)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_422_shows_invalid_input(hass: HomeAssistant) -> None:
    """HTTP 422 from create_profile (bad battery params) → invalid_input error."""
    from custom_components.wolta.api import WoltaApiError

    mock_client = MagicMock()
    mock_client.create_profile = AsyncMock(
        side_effect=WoltaApiError("HTTP 422 from .../profile: ...", status=422)
    )

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
        patch(
            "custom_components.wolta.config_flow._energy_dashboard_defaults",
            return_value={},
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "privacy"
    assert result["errors"].get("base") == "invalid_input"


@pytest.mark.asyncio
async def test_non422_api_error_shows_cannot_connect(hass: HomeAssistant) -> None:
    """HTTP 500 (or other non-422) from create_profile → cannot_connect error."""
    from custom_components.wolta.api import WoltaApiError

    mock_client = MagicMock()
    mock_client.create_profile = AsyncMock(
        side_effect=WoltaApiError("HTTP 500 from .../profile: ...", status=500)
    )

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
        patch(
            "custom_components.wolta.config_flow._energy_dashboard_defaults",
            return_value={},
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "privacy"
    assert result["errors"].get("base") == "cannot_connect"


# ---------------------------------------------------------------------------
# Reauth flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reauth_flow_updates_token(hass: HomeAssistant) -> None:
    """Reauth flow calls create_profile and updates only the token in the entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    initial_data: dict[str, Any] = {
        CONF_TOKEN: "old-token",
        CONF_ZONE: ZONE,
        CONF_BATT_IN: ["sensor.battery_charge"],
        CONF_BATT_OUT: ["sensor.battery_discharge"],
        CONF_GRID_IN: ["sensor.grid_import"],
        CONF_GRID_OUT: ["sensor.grid_export"],
        CONF_SOLAR: ["sensor.solar"],
        CONF_BATTERY_KWH: 22.0,
        CONF_BATTERY_KW: 5.0,
        CONF_EFF: 0.9,
        CONF_SHARE: False,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Wolta (SE3)",
        data=initial_data,
        source=config_entries.SOURCE_USER,
        unique_id="old-unique-id",
    )
    entry.add_to_hass(hass)

    new_token = "new-token-xyz"
    mock_client = _mock_client(new_token)

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=initial_data,
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"

    # The token must have been updated
    updated_entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated_entry.data[CONF_TOKEN] == new_token
    # Other data must be intact
    assert updated_entry.data[CONF_ZONE] == ZONE


# ---------------------------------------------------------------------------
# Multi-sensor: two solar sensors → entry.data stores list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_two_solar_sensors(hass: HomeAssistant) -> None:
    """Flow with two solar entity IDs stores a list in entry.data."""
    mock_client = _mock_client()

    two_solar_entities = {
        CONF_BATT_IN: ["sensor.battery_charge"],
        CONF_BATT_OUT: ["sensor.battery_discharge"],
        CONF_GRID_IN: ["sensor.grid_import"],
        CONF_GRID_OUT: ["sensor.grid_export"],
        CONF_SOLAR: ["sensor.solar_inverter_a", "sensor.solar_inverter_b"],
    }

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
        patch(
            "custom_components.wolta.config_flow._energy_dashboard_defaults",
            return_value={},
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=two_solar_entities
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_SOLAR] == ["sensor.solar_inverter_a", "sensor.solar_inverter_b"]


# ---------------------------------------------------------------------------
# Multi-sensor: Required stream with empty list → validation error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_required_stream_empty_list_shows_error(hass: HomeAssistant) -> None:
    """Required stream (batt_in) with empty list → re-shows form with required_sensor error."""
    mock_client = _mock_client()

    bad_entities = {
        CONF_BATT_IN: [],  # empty = invalid for required stream
        CONF_BATT_OUT: ["sensor.battery_discharge"],
        CONF_GRID_IN: ["sensor.grid_import"],
        CONF_GRID_OUT: ["sensor.grid_export"],
    }

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
        patch(
            "custom_components.wolta.config_flow._energy_dashboard_defaults",
            return_value={},
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=bad_entities
        )

    # Must re-show the entities form with the required_sensor error
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "entities"
    assert result["errors"].get(CONF_BATT_IN) == "required_sensor"


# ---------------------------------------------------------------------------
# Multi-sensor: energy prefill collects multiple solar sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_energy_prefill_collects_multiple_solar_sources(hass: HomeAssistant) -> None:
    """Two solar sources in energy dashboard → both collected in solar list."""
    from custom_components.wolta.config_flow import _energy_dashboard_defaults

    manager = _make_energy_manager(
        [
            {
                "type": "battery",
                "stat_energy_to": "sensor.batt_in",
                "stat_energy_from": "sensor.batt_out",
            },
            {
                "type": "solar",
                "stat_energy_from": "sensor.solar_a",
            },
            {
                "type": "solar",
                "stat_energy_from": "sensor.solar_b",
            },
        ]
    )

    with patch(
        "custom_components.wolta.config_flow.async_get_manager",
        return_value=manager,
    ):
        defaults = await _energy_dashboard_defaults(hass)

    solar = defaults.get(CONF_SOLAR)
    assert isinstance(solar, list), "solar prefill must be a list"
    assert "sensor.solar_a" in solar
    assert "sensor.solar_b" in solar
    assert len(solar) == 2
