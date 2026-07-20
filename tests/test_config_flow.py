"""Tests for custom_components/wolta/config_flow.py (TDD)."""

from __future__ import annotations

import string

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
    CONF_INVERT_BATTERY,
    CONF_PURCHASE_DATE,
    CONF_RESERVE_PCT,
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
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "plant"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_NO_SOLAR
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=step_user_with_tariff
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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
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
# reserve_pct field (plan 38 / task 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_with_reserve_pct_sent_to_create_profile(
    hass: HomeAssistant,
) -> None:
    """reserve_pct filled in the user step is passed to create_profile and stored."""
    mock_client = _mock_client()
    step_user_with_reserve = {**STEP_USER_DATA, CONF_RESERVE_PCT: 10.0}

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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=step_user_with_reserve
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY

    mock_client.create_profile.assert_awaited_once()
    _, kwargs = mock_client.create_profile.call_args
    assert kwargs["reserve_pct"] == 10.0

    data = result["data"]
    assert data[CONF_RESERVE_PCT] == 10.0


@pytest.mark.asyncio
async def test_full_flow_without_reserve_pct_not_sent(hass: HomeAssistant) -> None:
    """Leaving reserve_pct blank means it is not sent and not stored."""
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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY

    mock_client.create_profile.assert_awaited_once()
    _, kwargs = mock_client.create_profile.call_args
    assert kwargs.get("reserve_pct") is None

    data = result["data"]
    assert CONF_RESERVE_PCT not in data


@pytest.mark.asyncio
async def test_full_flow_with_reserve_pct_zero_is_sent(hass: HomeAssistant) -> None:
    """A legitimate reserve_pct of 0.0 (no reserve floor) must reach create_profile,
    not be swallowed as "unset" (the `.get() or None` gotcha)."""
    mock_client = _mock_client()
    step_user_with_zero_reserve = {**STEP_USER_DATA, CONF_RESERVE_PCT: 0.0}

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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=step_user_with_zero_reserve
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY

    mock_client.create_profile.assert_awaited_once()
    _, kwargs = mock_client.create_profile.call_args
    assert kwargs["reserve_pct"] == 0.0

    data = result["data"]
    assert data[CONF_RESERVE_PCT] == 0.0


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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=two_solar_entities
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
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
            result["flow_id"], user_input={"next_step_id": "create"}
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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=step_user_with_cost
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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA  # no cost/date
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


# Profilfält som speglas server ↔ entry.data (delad profil-sync)
_SYNC_KEYS = (
    CONF_ZONE, CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF, CONF_RESERVE_PCT,
    CONF_COST_SEK, CONF_PURCHASE_DATE, CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE,
    CONF_EXPORT_EXTRA_ORE,
)


def _server_profile(entry, **overrides):
    """Bygg ett GET /profile-snapshot som speglar entry.data (+ ev. overrides)."""
    prof = {k: entry.data.get(k) for k in _SYNC_KEYS}
    prof.update(overrides)
    return prof


_OPT_SECTION_OF = {
    CONF_BATTERY_KWH: "battery", CONF_BATTERY_KW: "battery", CONF_EFF: "battery",
    CONF_RESERVE_PCT: "battery",
    # Strängnycklar (inte CONF_-konstanter): nameplate-fälten testas med lokala
    # importer i sina testfall, så en saknad konstant fäller bara de testen –
    # inte hela filens collection.
    "nameplate_kwh": "battery", "nameplate_kw": "battery",
    CONF_COST_SEK: "economy", CONF_PURCHASE_DATE: "economy",
    CONF_GRID_VAR_ORE: "tariffs", CONF_SURCHARGE_ORE: "tariffs",
    CONF_EXPORT_EXTRA_ORE: "tariffs",
}


def _opts(**flat):
    """Bygg sektions-nästlad options-input ur platta fältnycklar."""
    out: dict = {"battery": {}, "economy": {}, "tariffs": {}}
    for k, v in flat.items():
        if k == CONF_INVERT_BATTERY:
            out[CONF_INVERT_BATTERY] = v
        else:
            out[_OPT_SECTION_OF[k]][k] = v
    return out


@pytest.mark.asyncio
async def test_options_flow_patches_profile_and_updates_entry(hass: HomeAssistant) -> None:
    """Options flow submitting cost+date → patch_profile called + entry.data updated."""
    entry = _make_mock_entry(hass)

    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock(return_value={"profile_token": TOKEN})
    mock_client.get_profile = AsyncMock(return_value=_server_profile(entry))
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
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_COST_SEK: 95000.0,
                CONF_PURCHASE_DATE: "2023-03-01",
            }),
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
    mock_client.get_profile = AsyncMock(return_value=_server_profile(entry))
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
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_COST_SEK: 80000.0,
                CONF_PURCHASE_DATE: "2021-06-01",
            }),
        )

    # Must complete successfully despite the cooldown
    assert result["type"] == FlowResultType.CREATE_ENTRY
    # patch_profile still ran
    mock_client.patch_profile.assert_awaited_once()


def _mock_options_env(entry):
    """Return (client, coordinator) mocks wired to the entry."""
    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock(return_value={"profile_token": TOKEN})
    mock_client.get_profile = AsyncMock(return_value=_server_profile(entry))
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
            user_input=_opts(**{
                CONF_BATTERY_KWH: 30.0,   # changed
                CONF_BATTERY_KW: 5.0,     # unchanged
                CONF_EFF: 0.9,            # unchanged
            }),
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
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_COST_SEK: 75000.0,
                CONF_PURCHASE_DATE: "2020-05-10",
            }),
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
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                # cost_sek/purchase_date omitted = cleared fields
            }),
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
    mock_client.get_profile = AsyncMock(return_value=_server_profile(entry))
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
    # Schema-defaults ska spegla server-snapshotet (= entry.data i detta test).
    # Sektioner nästlar schemat: yttre nyckel → section → inre schema.
    outer = result["data_schema"].schema
    seen = {}
    for sec_key, sec in outer.items():
        sec_name = sec_key.schema if hasattr(sec_key, "schema") else str(sec_key)
        if sec_name not in ("battery", "economy", "tariffs"):
            continue
        for k in sec.schema.schema:
            key_name = k.schema if hasattr(k, "schema") else str(k)
            if key_name in (CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF):
                seen[key_name] = k.default()
            if key_name == CONF_COST_SEK:
                # suggested_value (INTE default) för rensningsbara fält
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
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_GRID_VAR_ORE: 55.0,  # changed
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {"grid_var_ore": 55.0}

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_GRID_VAR_ORE] == 55.0
    mock_coordinator.async_trigger_recompute.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_flow_unrelated_change_preserves_tariff(hass: HomeAssistant) -> None:
    """Max-review-regression (plan 35): changing ONLY an unrelated field (battery_kwh)
    while a prefilled tariff value is re-submitted unchanged must NOT clear the tariff.
    This is the highest-impact silent-failure mode (a user's tariff getting wiped by an
    unrelated edit); the options flow must only PATCH genuinely changed fields."""
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
            user_input=_opts(**{
                CONF_BATTERY_KWH: 25.0,  # changed
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_GRID_VAR_ORE: 40.0,  # unchanged, re-submitted as HA does for suggested_value
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    patched = mock_client.patch_profile.call_args.kwargs
    assert "grid_var_ore" not in patched, "unchanged tariff must not be PATCHed"
    assert patched.get("battery_kwh") == 25.0
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_GRID_VAR_ORE] == 40.0, "tariff must survive an unrelated edit"


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
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                # grid_var_ore/surcharge_ore/export_extra_ore omitted = cleared fields
            }),
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


@pytest.mark.asyncio
async def test_options_flow_changes_reserve_pct(hass: HomeAssistant) -> None:
    """Plan 38 task 5: changing reserve_pct in the options flow → patch_profile
    is called with the new value."""
    entry = _make_mock_entry(
        hass,
        extra_data={CONF_RESERVE_PCT: 5.0},
    )
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_RESERVE_PCT: 15.0,  # changed
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {"reserve_pct": 15.0}

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_RESERVE_PCT] == 15.0
    mock_coordinator.async_trigger_recompute.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_flow_unrelated_change_preserves_reserve_pct(
    hass: HomeAssistant,
) -> None:
    """Changing ONLY an unrelated field (battery_kwh) while a prefilled reserve_pct
    value is re-submitted unchanged must NOT clear the reserve (mirrors the tariff
    max-review regression from plan 35)."""
    entry = _make_mock_entry(
        hass,
        extra_data={CONF_RESERVE_PCT: 5.0},
    )
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{
                CONF_BATTERY_KWH: 25.0,  # changed
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_RESERVE_PCT: 5.0,  # unchanged, re-submitted as HA does for suggested_value
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    patched = mock_client.patch_profile.call_args.kwargs
    assert "reserve_pct" not in patched, "unchanged reserve_pct must not be PATCHed"
    assert patched.get("battery_kwh") == 25.0
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_RESERVE_PCT] == 5.0, "reserve_pct must survive an unrelated edit"


@pytest.mark.asyncio
async def test_options_flow_clears_reserve_pct(hass: HomeAssistant) -> None:
    """Plan 38 task 5: clearing a previously-set reserve_pct (absent key) →
    patch_profile is called with None (clear-to-default) and the key is removed
    from entry.data."""
    entry = _make_mock_entry(
        hass,
        extra_data={CONF_RESERVE_PCT: 5.0},
    )
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                # reserve_pct omitted = cleared field
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {"reserve_pct": None}
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert CONF_RESERVE_PCT not in updated.data


@pytest.mark.asyncio
async def test_options_flow_reserve_pct_zero_is_sent(hass: HomeAssistant) -> None:
    """A legitimate reserve_pct of 0.0 (no reserve floor) must be PATCHed, not
    treated as an empty/cleared field."""
    entry = _make_mock_entry(
        hass,
        extra_data={CONF_RESERVE_PCT: 5.0},
    )
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_RESERVE_PCT: 0.0,  # changed to legit zero
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {"reserve_pct": 0.0}
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_RESERVE_PCT] == 0.0


async def test_options_flow_invert_toggle_updates_entry_no_patch(hass: HomeAssistant) -> None:
    """Toggling battery-invert in options → entry.data is updated + coordinator refresh (self-heal
    re-backfill), and NO patch_profile (client-side upload transform, not a backend field)."""
    entry = _make_mock_entry(hass)

    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock()
    mock_client.get_profile = AsyncMock(return_value=_server_profile(entry))
    mock_coordinator = MagicMock()
    mock_coordinator.async_trigger_recompute = AsyncMock()
    mock_coordinator.async_request_refresh = AsyncMock()
    entry.runtime_data = mock_coordinator

    with (
        patch(
            "custom_components.wolta.config_flow.WoltaApiClient",
            return_value=mock_client,
        ),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,   # unchanged plant values
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_INVERT_BATTERY: True,  # turned on
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # Invert is client-side → no backend PATCH
    mock_client.patch_profile.assert_not_awaited()
    # The flag is stored in entry.data
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_INVERT_BATTERY] is True
    # The coordinator is refreshed → self-heal backfill runs
    mock_coordinator.async_request_refresh.assert_awaited()


@pytest.mark.asyncio
async def test_options_prefills_from_server_not_cache(hass: HomeAssistant) -> None:
    """entry.data säger 22 kWh men servern 25 → formuläret förifylls med serverns 25
    (webben ändrade profilen; stale cache får inte visas eller diffas mot)."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(
        return_value=_server_profile(entry, battery_kwh=25.0))

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    outer = result["data_schema"].schema
    battery = next(
        v for k, v in outer.items()
        if (k.schema if hasattr(k, "schema") else str(k)) == "battery"
    )
    kwh_default = next(
        k.default() for k in battery.schema.schema
        if (k.schema if hasattr(k, "schema") else str(k)) == CONF_BATTERY_KWH
    )
    assert kwh_default == 25.0

    # Oförändrat submit mot SERVERNS värden → ingen PATCH (diff-basen är snapshotet).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_opts(**{
            CONF_BATTERY_KWH: 25.0,
            CONF_BATTERY_KW: 5.0,
            CONF_EFF: 0.9,
        }),
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_flow_get_failure_aborts(hass: HomeAssistant) -> None:
    """GET /profile misslyckas → flowet avbryts med cannot_connect (PATCH hade
    ändå misslyckats; profilredigering kräver servern)."""
    from custom_components.wolta.api import WoltaApiError

    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(side_effect=WoltaApiError("boom", status=500))

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


# ---------------------------------------------------------------------------
# Delad profil-sync (B6): meny, koppla befintlig profil, flödesomordning,
# auto-prefill (zon/eff/datum/invert)
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

from custom_components.wolta.const import CONF_CREATED_BY_HA, CONF_PLANT_ID  # noqa: E402

LINK_TOKEN = "tok-linked-xyz"
LINK_PROFILE = {
    "zone": "SE4", "battery_kwh": 15.0, "battery_kw": 8.0, "eff": 0.92,
    "reserve_pct": None, "cost_sek": 80000.0, "purchase_date": "2024-03-01",
    "grid_var_ore": 30.0, "surcharge_ore": None, "export_extra_ore": None,
}


def _patch_flow_env(mock_client):
    """Patcha klient/session/energiprefill/statistik för flödestester."""
    async def _no_lifetime(hass_, in_ids, out_ids):
        return 0.0, 0.0, None

    return (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
        patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={}),
        patch("custom_components.wolta.stats.async_fetch_lifetime", side_effect=_no_lifetime),
    )


@pytest.mark.asyncio
async def test_user_step_shows_menu(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == FlowResultType.MENU
    assert set(result["menu_options"]) == {"create", "link"}


def test_extract_token_accepts_url_and_raw() -> None:
    from custom_components.wolta.config_flow import extract_token
    assert extract_token("abc123") == "abc123"
    assert extract_token("https://wolta.se/optimeringsbetyg?profile=abc%2B123") == "abc+123"
    assert extract_token("  abc123  ") == "abc123"
    assert extract_token("https://wolta.se/kalkylator/lonar-det-sig?profile=tok1&x=1") == "tok1"


@pytest.mark.asyncio
async def test_link_flow_creates_entry_with_server_profile(hass: HomeAssistant) -> None:
    """Koppla-spåret: token ur Besök-länk → entities → entry utan ny profil (POST)."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value=dict(LINK_PROFILE))
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"), \
         patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={}), \
         patch("custom_components.wolta.stats.async_fetch_lifetime",
               new=AsyncMock(return_value=(0.0, 0.0, None))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        assert result["step_id"] == "link"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"profile_input": f"https://wolta.se/optimeringsbetyg?profile={LINK_TOKEN}"})
        assert result["step_id"] == "entities"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_ENTITIES_DATA)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Wolta (SE4)"
    data = result["data"]
    assert data[CONF_TOKEN] == LINK_TOKEN
    assert data[CONF_CREATED_BY_HA] is False
    assert data[CONF_BATTERY_KWH] == 15.0
    assert data[CONF_GRID_VAR_ORE] == 30.0
    assert CONF_SURCHARGE_ORE not in data  # None-fält cache:as inte
    mock_client.create_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_flow_invalid_token_shows_error(hass: HomeAssistant) -> None:
    from custom_components.wolta.api import WoltaAuthError

    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(side_effect=WoltaAuthError("404"))

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": "dead"})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "link"
    assert result["errors"] == {"profile_input": "invalid_token"}


async def _drive_create_to_plant(hass):
    """Hjälpare: meny → create → entities (giltigt val) → returnera plant-stegets result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "create"})
    assert result["step_id"] == "entities"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], STEP_ENTITIES_DATA)
    assert result["step_id"] == "plant"
    return result


def _plant_schema_default(result, field):
    marker = next(k for k in result["data_schema"].schema
                  if (k.schema if hasattr(k, "schema") else str(k)) == field)
    return marker.default()


@pytest.mark.asyncio
async def test_create_flow_order_entities_then_plant(hass: HomeAssistant) -> None:
    """Ny ordning: meny → entities → plant → privacy; POST /profile först i privacy."""
    mock_client = _mock_client()
    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"), \
         patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={}), \
         patch("custom_components.wolta.stats.async_fetch_lifetime",
               new=AsyncMock(return_value=(0.0, 0.0, None))):
        result = await _drive_create_to_plant(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_USER_DATA)
        assert result["step_id"] == "privacy"
        mock_client.create_profile.assert_not_awaited()  # POST först i privacy-steget
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_PRIVACY_DATA)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CREATED_BY_HA] is True
    mock_client.create_profile.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_flow_zone_prefill_from_ha_config(hass: HomeAssistant) -> None:
    hass.config.country = "SE"
    hass.config.latitude = 59.33
    mock_client = _mock_client()
    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"), \
         patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={}), \
         patch("custom_components.wolta.stats.async_fetch_lifetime",
               new=AsyncMock(return_value=(0.0, 0.0, None))):
        result = await _drive_create_to_plant(hass)
    assert _plant_schema_default(result, CONF_ZONE) == "SE3"


@pytest.mark.asyncio
async def test_create_flow_invert_detection_prefills_toggle(hass: HomeAssistant) -> None:
    """Ur > in i historiken → invert-togglen förvald + eff-förslag = speglad kvot."""
    mock_client = _mock_client()
    first = datetime(2025, 1, 1, tzinfo=timezone.utc)
    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"), \
         patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={}), \
         patch("custom_components.wolta.stats.async_fetch_lifetime",
               new=AsyncMock(return_value=(880.0, 1000.0, first))):
        result = await _drive_create_to_plant(hass)
    assert _plant_schema_default(result, CONF_INVERT_BATTERY) is True
    assert _plant_schema_default(result, CONF_EFF) == 0.88


@pytest.mark.asyncio
async def test_link_flow_invert_suspected_shows_check_step(hass: HomeAssistant) -> None:
    """Koppla-spåret saknar plant-steg → misstänkt inversion ger eget kontrollsteg."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value=dict(LINK_PROFILE))
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})
    first = datetime(2025, 1, 1, tzinfo=timezone.utc)

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"), \
         patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={}), \
         patch("custom_components.wolta.stats.async_fetch_lifetime",
               new=AsyncMock(return_value=(880.0, 1000.0, first))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": LINK_TOKEN})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_ENTITIES_DATA)
        assert result["step_id"] == "invert_check"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_INVERT_BATTERY: True})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INVERT_BATTERY] is True
    assert result["data"][CONF_CREATED_BY_HA] is False


# ---------------------------------------------------------------------------
# B7: reconfigure-flow för sensorval (utan ta bort + lägg till)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_updates_entities_no_new_entry(hass: HomeAssistant) -> None:
    entry = _make_mock_entry(hass)
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATT_IN: ["sensor.new_batt_in"],
                CONF_BATT_OUT: ["sensor.battery_discharge"],
                CONF_GRID_IN: ["sensor.grid_import"],
                CONF_GRID_OUT: ["sensor.grid_export"],
                CONF_SOLAR: ["sensor.solar"],
            },
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_BATT_IN] == ["sensor.new_batt_in"]
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.asyncio
async def test_reconfigure_requires_mandatory_streams(hass: HomeAssistant) -> None:
    entry = _make_mock_entry(hass)
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATT_IN: [],
                CONF_BATT_OUT: ["sensor.battery_discharge"],
                CONF_GRID_IN: ["sensor.grid_import"],
                CONF_GRID_OUT: ["sensor.grid_export"],
            },
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"].get(CONF_BATT_IN) == "required_sensor"


# ---------------------------------------------------------------------------
# Max-granskningsfixar: adopt vid länkning, batterikrav, zon-fallback, reauth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_flow_adopts_web_profile(hass: HomeAssistant) -> None:
    """Länkning ska adoptera profilen (upload→integration-kind) – annars 404:ar
    PUT/recompute/results på webbskapade profiler och reauth ersätter token tyst."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value=dict(LINK_PROFILE))
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"), \
         patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={}), \
         patch("custom_components.wolta.stats.async_fetch_lifetime",
               new=AsyncMock(return_value=(0.0, 0.0, None))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": LINK_TOKEN})
        assert result["step_id"] == "entities"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_ENTITIES_DATA)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # The adopt call also stamps this entry's stable plant identity on the adopted row.
    plant_id = result["data"][CONF_PLANT_ID]
    mock_client.adopt_profile.assert_awaited_once_with(LINK_TOKEN, client_plant_id=plant_id)


@pytest.mark.asyncio
async def test_link_flow_stores_plant_id(hass: HomeAssistant) -> None:
    """A linked entry carries the same plant id it stamped during adopt (128-bit hex)."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value=dict(LINK_PROFILE))
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"), \
         patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={}), \
         patch("custom_components.wolta.stats.async_fetch_lifetime",
               new=AsyncMock(return_value=(0.0, 0.0, None))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": LINK_TOKEN})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_ENTITIES_DATA)

    # Det som betyder något är att id:t vi SKICKADE är id:t vi LAGRAR – divergerar de blir
    # dedupen tyst verkningslös vid nästa reauth.
    plant_id = result["data"][CONF_PLANT_ID]
    sent = mock_client.adopt_profile.await_args.kwargs["client_plant_id"]
    assert sent == plant_id, "skickat och lagrat id måste vara samma"
    assert len(plant_id) == 32 and all(c in string.hexdigits for c in plant_id)


@pytest.mark.asyncio
async def test_link_flow_rejects_batteryless_profile(hass: HomeAssistant) -> None:
    """Solar-only-profil (battery_kwh=null) → formulärfel, ingen adopt."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(
        return_value={**LINK_PROFILE, "battery_kwh": None, "battery_kw": None})
    mock_client.adopt_profile = AsyncMock()

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": LINK_TOKEN})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"profile_input": "profile_no_battery"}
    mock_client.adopt_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_flow_zone_fallback_when_server_lacks_zone(hass: HomeAssistant) -> None:
    """Zone saknas i serversvaret (defensivt) → DEFAULT_ZONE, aldrig KeyError vid setup."""
    from custom_components.wolta.const import DEFAULT_ZONE

    mock_client = _mock_client()
    profile = dict(LINK_PROFILE)
    profile["zone"] = None
    mock_client.get_profile = AsyncMock(return_value=profile)
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"), \
         patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={}), \
         patch("custom_components.wolta.stats.async_fetch_lifetime",
               new=AsyncMock(return_value=(0.0, 0.0, None))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": LINK_TOKEN})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_ENTITIES_DATA)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["result"].data[CONF_ZONE] == DEFAULT_ZONE


@pytest.mark.asyncio
async def test_options_flow_purged_profile_starts_reauth(hass: HomeAssistant) -> None:
    """WoltaAuthError på options-GET (purgad profil) → reauth startas + abort."""
    from custom_components.wolta.api import WoltaAuthError

    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(side_effect=WoltaAuthError("404"))

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_required"
    reauth_flows = [
        f for f in hass.config_entries.flow.async_progress()
        if f["context"].get("source") == config_entries.SOURCE_REAUTH
    ]
    assert len(reauth_flows) == 1


# ---------------------------------------------------------------------------
# nameplate_kwh field (backend spec 2026-07-14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_with_nameplate_kwh_sent_to_create_profile(
    hass: HomeAssistant,
) -> None:
    """nameplate_kwh filled in the plant step is passed to create_profile and cached."""
    from custom_components.wolta.const import CONF_NAMEPLATE_KWH

    mock_client = _mock_client()
    step_user_with_nameplate = {**STEP_USER_DATA, CONF_NAMEPLATE_KWH: 22.0}

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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=step_user_with_nameplate
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.create_profile.assert_awaited_once()
    _, kwargs = mock_client.create_profile.call_args
    assert kwargs["nameplate_kwh"] == 22.0
    assert result["data"][CONF_NAMEPLATE_KWH] == 22.0


@pytest.mark.asyncio
async def test_full_flow_without_nameplate_kwh_not_sent(hass: HomeAssistant) -> None:
    """Leaving nameplate_kwh blank means None is sent and nothing is cached."""
    from custom_components.wolta.const import CONF_NAMEPLATE_KWH

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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    _, kwargs = mock_client.create_profile.call_args
    assert kwargs["nameplate_kwh"] is None
    assert CONF_NAMEPLATE_KWH not in result["data"]


# ---------------------------------------------------------------------------
# nameplate_kw field (v0.15.0 – parity with nameplate_kwh, backend spec 2026-07-15)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_with_nameplate_kw_sent_to_create_profile(
    hass: HomeAssistant,
) -> None:
    """nameplate_kw filled in the plant step is passed to create_profile and cached."""
    from custom_components.wolta.const import CONF_NAMEPLATE_KW

    mock_client = _mock_client()
    step_user_with_nameplate = {**STEP_USER_DATA, CONF_NAMEPLATE_KW: 8.0}

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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=step_user_with_nameplate
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.create_profile.assert_awaited_once()
    _, kwargs = mock_client.create_profile.call_args
    assert kwargs["nameplate_kw"] == 8.0
    assert result["data"][CONF_NAMEPLATE_KW] == 8.0


@pytest.mark.asyncio
async def test_full_flow_without_nameplate_kw_not_sent(hass: HomeAssistant) -> None:
    """Leaving nameplate_kw blank means None is sent and nothing is cached."""
    from custom_components.wolta.const import CONF_NAMEPLATE_KW

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
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_USER_DATA
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PRIVACY_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    _, kwargs = mock_client.create_profile.call_args
    assert kwargs["nameplate_kw"] is None
    assert CONF_NAMEPLATE_KW not in result["data"]


@pytest.mark.asyncio
async def test_options_flow_changes_nameplate_kw(hass: HomeAssistant) -> None:
    """Changing nameplate_kw in the options flow → patch_profile gets the new value."""
    from custom_components.wolta.const import CONF_NAMEPLATE_KW

    entry = _make_mock_entry(hass)
    mock_client, mock_coordinator = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(
        return_value=_server_profile(entry, nameplate_kw=6.0)
    )

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_NAMEPLATE_KW: 8.0,  # changed (server has 6.0)
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {"nameplate_kw": 8.0}
    mock_coordinator.async_trigger_recompute.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_flow_clears_nameplate_kw(hass: HomeAssistant) -> None:
    """Clearing a prefilled nameplate_kw → PATCH null (the clearable-field pattern)."""
    from custom_components.wolta.const import CONF_NAMEPLATE_KW  # noqa: F401

    entry = _make_mock_entry(hass)
    mock_client, mock_coordinator = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(
        return_value=_server_profile(entry, nameplate_kw=6.0)
    )

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                # nameplate_kw absent = actively cleared
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {"nameplate_kw": None}


@pytest.mark.asyncio
async def test_options_flow_hides_cost_for_plant_scoped_profile(hass: HomeAssistant) -> None:
    """cost_scope='plant' (adopted wolta.se guide profile whose scalar price covers the
    whole plant): the battery-only cost field is hidden AND skipped in the diff – an
    absent optional field otherwise means 'actively cleared' and a PATCH null would
    wipe the plant price on every save."""
    entry = _make_mock_entry(hass, extra_data={CONF_COST_SEK: 250000.0})

    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock(return_value={"profile_token": TOKEN})
    mock_client.get_profile = AsyncMock(
        return_value=_server_profile(entry, cost_scope="plant", cost_sek=250000.0)
    )
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

        # Formuläret ska SAKNA cost-fältet (inte bara diffen skippa det) – annars vore
        # en redigering en tyst no-op i st f dolt fält (max-review-fynd 2026-07-18).
        schema_dict = result["data_schema"].schema
        econ_section = next(v for k, v in schema_dict.items() if str(k) == "economy")
        econ_keys = {str(k) for k in econ_section.schema.schema}
        assert CONF_COST_SEK not in econ_keys
        assert CONF_PURCHASE_DATE in econ_keys

        # Ändra ett tariff-fält (tvingar en PATCH); cost-fältet finns inte i formuläret.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_GRID_VAR_ORE: 30.0,
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    kwargs = mock_client.patch_profile.call_args.kwargs
    assert kwargs == {"grid_var_ore": 30.0}, (
        "plant-scoped cost_sek must be neither patched nor nulled"
    )


# ---------------------------------------------------------------------------
# Stable plant identity (backend plant_fingerprint) — v0.16.0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_flow_sends_and_stores_plant_id(hass: HomeAssistant) -> None:
    """The create path mints a 128-bit id, sends it as client_plant_id and stores it.

    Without it a re-onboarded plant gets a brand new backend row: the corpus counts one
    plant twice and the old row is orphaned with its streamed history.
    """
    mock_client = _mock_client()

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"), \
         patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={}), \
         patch("custom_components.wolta.stats.async_fetch_lifetime",
               new=AsyncMock(return_value=(0.0, 0.0, None))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "create"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_ENTITIES_DATA)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_USER_DATA)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_PRIVACY_DATA)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    sent = mock_client.create_profile.await_args.kwargs["client_plant_id"]
    stored = result["data"][CONF_PLANT_ID]
    assert sent == stored, "the id we sent must be the id we persist"
    assert len(stored) == 32 and all(c in string.hexdigits for c in stored)


@pytest.mark.asyncio
async def test_reauth_reuses_stored_plant_id(hass: HomeAssistant) -> None:
    """Reauth must send the ENTRY's id, not the fresh flow object's minted one.

    Reauth runs in a new flow instance; sending its minted id would present the plant as a
    new one and abandon whatever history survived the purge.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    data: dict[str, Any] = {
        CONF_TOKEN: "old-token", CONF_ZONE: ZONE,
        CONF_BATT_IN: ["sensor.a"], CONF_BATT_OUT: ["sensor.b"],
        CONF_GRID_IN: ["sensor.c"], CONF_GRID_OUT: ["sensor.d"],
        CONF_BATTERY_KWH: 22.0, CONF_BATTERY_KW: 5.0, CONF_EFF: 0.9, CONF_SHARE: False,
        CONF_PLANT_ID: "a" * 32,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, source=config_entries.SOURCE_USER,
                            unique_id="uid-1")
    entry.add_to_hass(hass)
    mock_client = _mock_client("new-token")

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=data)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})

    assert result["reason"] == "reauth_successful"
    assert mock_client.create_profile.await_args.kwargs["client_plant_id"] == "a" * 32


@pytest.mark.asyncio
async def test_reauth_backfills_plant_id_from_entry_id(hass: HomeAssistant) -> None:
    """Pre-v0.16.0 entries have no stored id → fall back to entry_id and persist it, so a
    later reauth lands on the same plant row."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    data: dict[str, Any] = {
        CONF_TOKEN: "old-token", CONF_ZONE: ZONE,
        CONF_BATT_IN: ["sensor.a"], CONF_BATT_OUT: ["sensor.b"],
        CONF_GRID_IN: ["sensor.c"], CONF_GRID_OUT: ["sensor.d"],
        CONF_BATTERY_KWH: 22.0, CONF_BATTERY_KW: 5.0, CONF_EFF: 0.9, CONF_SHARE: False,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, source=config_entries.SOURCE_USER,
                            unique_id="uid-2")
    entry.add_to_hass(hass)
    mock_client = _mock_client("new-token")

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=data)
        await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})

    assert mock_client.create_profile.await_args.kwargs["client_plant_id"] == entry.entry_id
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_PLANT_ID] == entry.entry_id


@pytest.mark.asyncio
async def test_link_flow_409_shows_identity_conflict(hass: HomeAssistant) -> None:
    """A 409 from adopt (plant identity bound to another row) must not masquerade as
    "cannot connect" – the connection worked fine; the identity is taken. Practically
    unreachable with our freshly minted 128-bit ids, but if it ever fires the message
    must point at the actual conflict."""
    from custom_components.wolta.api import WoltaApiError

    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value=dict(LINK_PROFILE))
    mock_client.adopt_profile = AsyncMock(
        side_effect=WoltaApiError("HTTP 409 from .../adopt", status=409))

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": LINK_TOKEN})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"profile_input": "identity_conflict"}
