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
    CONF_COST_SEK,
    CONF_EFF,
    CONF_EXPORT_EXTRA_ORE,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_GRID_VAR_ORE,
    CONF_PURCHASE_DATE,
    CONF_SHARE,
    CONF_SOLAR,
    CONF_SURCHARGE_ORE,
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
# Tariff override fields (plan 35 / task 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_with_tariff_fields_sent_to_create_profile(
    hass: HomeAssistant,
) -> None:
    """Tariff fields filled in the user step are passed to create_profile and stored."""
    mock_client = _mock_client()
    step_user_with_tariff = {
        **STEP_USER_DATA,
        CONF_GRID_VAR_ORE: 40.0,
        CONF_SURCHARGE_ORE: 8.0,
        CONF_EXPORT_EXTRA_ORE: 5.0,
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
            result["flow_id"], user_input=step_user_with_tariff
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY

    mock_client.create_profile.assert_awaited_once()
    _, kwargs = mock_client.create_profile.call_args
    assert kwargs["grid_var_ore"] == 40.0
    assert kwargs["surcharge_ore"] == 8.0
    assert kwargs["export_extra_ore"] == 5.0

    data = result["data"]
    assert data[CONF_GRID_VAR_ORE] == 40.0
    assert data[CONF_SURCHARGE_ORE] == 8.0
    assert data[CONF_EXPORT_EXTRA_ORE] == 5.0


@pytest.mark.asyncio
async def test_full_flow_without_tariff_fields_not_sent(hass: HomeAssistant) -> None:
    """Leaving tariff fields blank means they are not sent and not stored."""
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
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY

    mock_client.create_profile.assert_awaited_once()
    _, kwargs = mock_client.create_profile.call_args
    assert kwargs.get("grid_var_ore") is None
    assert kwargs.get("surcharge_ore") is None
    assert kwargs.get("export_extra_ore") is None

    data = result["data"]
    assert CONF_GRID_VAR_ORE not in data
    assert CONF_SURCHARGE_ORE not in data
    assert CONF_EXPORT_EXTRA_ORE not in data


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


# ---------------------------------------------------------------------------
# v0.3.0: cost_sek + purchase_date in initial config flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_with_cost_and_date(hass: HomeAssistant) -> None:
    """Full flow with cost_sek + purchase_date stores them in entry.data and passes them to create_profile."""
    mock_client = _mock_client()

    step_user_with_cost = {
        **STEP_USER_DATA,
        CONF_COST_SEK: 89900.0,
        CONF_PURCHASE_DATE: "2022-11-15",
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
            result["flow_id"], user_input=step_user_with_cost
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_COST_SEK] == 89900.0
    assert data[CONF_PURCHASE_DATE] == "2022-11-15"

    # create_profile must have received cost_sek and purchase_date
    mock_client.create_profile.assert_awaited_once()
    call_kwargs = mock_client.create_profile.call_args
    assert call_kwargs.kwargs["cost_sek"] == 89900.0
    assert call_kwargs.kwargs["purchase_date"] == "2022-11-15"


@pytest.mark.asyncio
async def test_full_flow_without_cost_and_date(hass: HomeAssistant) -> None:
    """Full flow without cost/date → entry.data has no cost/date keys, create_profile called without them (or with None)."""
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
            result["flow_id"], user_input=STEP_USER_DATA  # no cost/date
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    # cost and date must not be in entry.data (or be None/absent)
    assert not data.get(CONF_COST_SEK)
    assert not data.get(CONF_PURCHASE_DATE)

    # create_profile must have been called with cost_sek=None or not at all
    call_kwargs = mock_client.create_profile.call_args
    assert call_kwargs.kwargs.get("cost_sek") is None
    assert call_kwargs.kwargs.get("purchase_date") is None


# ---------------------------------------------------------------------------
# v0.3.0: OptionsFlow
# ---------------------------------------------------------------------------


def _make_mock_entry(hass: HomeAssistant, extra_data: dict | None = None) -> Any:
    """Create and add a mock config entry to hass."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    base_data: dict[str, Any] = {
        CONF_TOKEN: TOKEN,
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
    if extra_data:
        base_data.update(extra_data)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Wolta ({ZONE})",
        data=base_data,
        source=config_entries.SOURCE_USER,
        unique_id="test-unique-id",
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_options_flow_patches_profile_and_updates_entry(hass: HomeAssistant) -> None:
    """Options flow submitting cost+date → patch_profile called + entry.data updated."""
    entry = _make_mock_entry(hass)

    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock(return_value={"profile_token": TOKEN})
    mock_coordinator = MagicMock()
    mock_coordinator.async_trigger_recompute = AsyncMock()
    mock_coordinator.async_request_refresh = AsyncMock()
    # Attach coordinator as runtime_data (as the real integration does)
    entry.runtime_data = mock_coordinator

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_COST_SEK: 95000.0,
                CONF_PURCHASE_DATE: "2023-03-01",
            },
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY

    # patch_profile must have been called with ONLY the changed fields
    mock_client.patch_profile.assert_awaited_once()
    call_args = mock_client.patch_profile.call_args
    assert call_args.args[0] == TOKEN  # first positional arg is token
    assert call_args.kwargs == {"cost_sek": 95000.0, "purchase_date": "2023-03-01"}, (
        "unchanged plant fields must not be PATCHed"
    )

    # entry.data must be updated with the new values
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_COST_SEK] == 95000.0
    assert updated.data[CONF_PURCHASE_DATE] == "2023-03-01"

    # recompute must have been triggered
    mock_coordinator.async_trigger_recompute.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_flow_swallows_recompute_rate_limit(hass: HomeAssistant) -> None:
    """Options flow succeeds even when recompute returns 429 (cooldown)."""
    from custom_components.wolta.api import WoltaRateLimitError

    entry = _make_mock_entry(hass)

    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock(return_value={"profile_token": TOKEN})
    mock_coordinator = MagicMock()
    mock_coordinator.async_trigger_recompute = AsyncMock(
        side_effect=WoltaRateLimitError(retry_after=3600)
    )
    mock_coordinator.async_request_refresh = AsyncMock()
    entry.runtime_data = mock_coordinator

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_COST_SEK: 80000.0,
                CONF_PURCHASE_DATE: "2021-06-01",
            },
        )

    # Must complete successfully despite the cooldown
    assert result["type"] == FlowResultType.CREATE_ENTRY
    # patch_profile still ran
    mock_client.patch_profile.assert_awaited_once()


def _mock_options_env(entry):
    """Return (client, coordinator) mocks wired to the entry."""
    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock(return_value={"profile_token": TOKEN})
    mock_coordinator = MagicMock()
    mock_coordinator.async_trigger_recompute = AsyncMock()
    mock_coordinator.async_request_refresh = AsyncMock()
    entry.runtime_data = mock_coordinator
    return mock_client, mock_coordinator


@pytest.mark.asyncio
async def test_options_flow_patches_only_changed_plant_fields(hass: HomeAssistant) -> None:
    """v0.4.0: changing battery_kwh only → PATCH contains only battery_kwh."""
    entry = _make_mock_entry(hass)
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATTERY_KWH: 30.0,   # ändrad
                CONF_BATTERY_KW: 5.0,     # oförändrad
                CONF_EFF: 0.9,            # oförändrad
            },
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {"battery_kwh": 30.0}

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_BATTERY_KWH] == 30.0
    assert updated.data[CONF_BATTERY_KW] == 5.0
    mock_coordinator.async_trigger_recompute.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_flow_unchanged_form_no_patch(hass: HomeAssistant) -> None:
    """v0.4.0: submitting the untouched form → no PATCH, no recompute."""
    entry = _make_mock_entry(
        hass, extra_data={CONF_COST_SEK: 75000.0, CONF_PURCHASE_DATE: "2020-05-10"}
    )
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_COST_SEK: 75000.0,
                CONF_PURCHASE_DATE: "2020-05-10",
            },
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()
    mock_coordinator.async_trigger_recompute.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_flow_clears_cost_and_date(hass: HomeAssistant) -> None:
    """v0.4.0: clearing prefilled cost/date (absent keys) → PATCH null + keys removed
    from entry.data (v0.3.0 silently swallowed cleared values)."""
    entry = _make_mock_entry(
        hass, extra_data={CONF_COST_SEK: 75000.0, CONF_PURCHASE_DATE: "2020-05-10"}
    )
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                # cost_sek/purchase_date utelämnade = rensade fält
            },
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {
        "cost_sek": None,
        "purchase_date": None,
    }
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert CONF_COST_SEK not in updated.data
    assert CONF_PURCHASE_DATE not in updated.data


@pytest.mark.asyncio
async def test_options_flow_prefills_existing_values(hass: HomeAssistant) -> None:
    """Options flow pre-fills form with existing cost/date from entry.data."""
    entry = _make_mock_entry(
        hass,
        extra_data={CONF_COST_SEK: 75000.0, CONF_PURCHASE_DATE: "2020-05-10"},
    )

    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock(return_value={"profile_token": TOKEN})
    mock_coordinator = MagicMock()
    mock_coordinator.async_trigger_recompute = AsyncMock()
    mock_coordinator.async_request_refresh = AsyncMock()
    entry.runtime_data = mock_coordinator

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.wolta.config_flow.async_get_clientsession",
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    # The schema defaults should reflect the existing entry data
    schema = result["data_schema"].schema
    schema_keys = {k.schema if hasattr(k, "schema") else str(k): k for k in schema}
    # Plant defaults pre-filled from entry.data; cost via suggested_value (NOT default –
    # a default would be re-injected by voluptuous when the user clears the field)
    seen = {}
    for k in schema:
        key_name = k.schema if hasattr(k, "schema") else str(k)
        if key_name in (CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF):
            seen[key_name] = k.default()
        if key_name == CONF_COST_SEK:
            seen[key_name] = (k.description or {}).get("suggested_value")
    assert seen[CONF_COST_SEK] == 75000.0
    assert seen[CONF_BATTERY_KWH] == 22.0
    assert seen[CONF_BATTERY_KW] == 5.0
    assert seen[CONF_EFF] == 0.9


@pytest.mark.asyncio
async def test_options_flow_changes_tariff_field(hass: HomeAssistant) -> None:
    """Plan 35 task 5: changing grid_var_ore in the options flow → patch_profile
    is called with the new value."""
    entry = _make_mock_entry(
        hass,
        extra_data={CONF_GRID_VAR_ORE: 40.0},
    )
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_GRID_VAR_ORE: 55.0,  # changed
            },
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {"grid_var_ore": 55.0}

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_GRID_VAR_ORE] == 55.0
    mock_coordinator.async_trigger_recompute.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_flow_clears_tariff_fields(hass: HomeAssistant) -> None:
    """Plan 35 task 5: clearing a previously-set tariff field (absent key) →
    patch_profile is called with None (clear-to-default) and the key is removed
    from entry.data."""
    entry = _make_mock_entry(
        hass,
        extra_data={
            CONF_GRID_VAR_ORE: 40.0,
            CONF_SURCHARGE_ORE: 8.0,
            CONF_EXPORT_EXTRA_ORE: 5.0,
        },
    )
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                # grid_var_ore/surcharge_ore/export_extra_ore utelämnade = rensade fält
            },
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {
        "grid_var_ore": None,
        "surcharge_ore": None,
        "export_extra_ore": None,
    }
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert CONF_GRID_VAR_ORE not in updated.data
    assert CONF_SURCHARGE_ORE not in updated.data
    assert CONF_EXPORT_EXTRA_ORE not in updated.data
