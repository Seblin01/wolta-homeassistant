"""Tests for custom_components/wolta/config_flow.py (TDD)."""

from __future__ import annotations

import contextlib
import string

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

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
    CONF_EXPORT_EXTRA_PCT,
    CONF_EXTERNAL_CONTROL,
    CONF_FLEX_COMPENSATION,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_GRID_VAR_ORE,
    CONF_GRID_VAR_PCT,
    CONF_INVERT_BATTERY,
    CONF_PREFILL_PURCHASE_DATE,
    CONF_PURCHASE_DATE,
    CONF_RESERVE_PCT,
    CONF_SHARE,
    CONF_SOLAR,
    CONF_SURCHARGE_ORE,
    CONF_TOKEN,
    CONF_ZONE,
    DEFAULT_SHARE,
    DOMAIN,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOKEN = "tok-abc123"
ZONE = "SE3"

# Everything the plant step asks for since the two-step rewrite (spec 2026-09-14 §7.1):
# capacity, power, efficiency, nameplate, reserve, economy and tariffs are gone from
# setup (the backend measures the battery; the rest live in options → settings), and
# `share`/`invert_battery` moved here from the removed privacy step.
STEP_PLANT_DATA = {
    CONF_ZONE: ZONE,
    # Mandatory since v0.29.0 (active choice, no default) - see the control_system
    # test block at the end of this file.
    "control_system": "emhass",
    CONF_SHARE: False,
    CONF_INVERT_BATTERY: False,
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


def _mock_client(token: str = TOKEN) -> MagicMock:
    """Return a mock WoltaApiClient whose create_profile returns TOKEN."""
    mock = MagicMock()
    mock.create_profile = AsyncMock(return_value=token)
    # The entities step asks the backend for the domain → control-system mapping; an
    # empty mapping means "no suggestion", which is the neutral default for tests that
    # are not about the prefill.
    mock.get_control_systems = AsyncMock(return_value=[])
    return mock


@contextlib.contextmanager
def _patched(mock_client, *, platforms=(), lifetime=(0.0, 0.0, None)):
    """Patch everything the create flow reaches outside itself.

    `platforms` is what the entity registry reports for the chosen battery sensors
    (patched rather than registered: the test entity ids do not exist in the registry,
    so the real lookup can only ever return an empty list). `lifetime` is the
    (charged, discharged, first_ts) triple the history prefill is computed from.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client)
        )
        stack.enter_context(patch("custom_components.wolta.config_flow.async_get_clientsession"))
        stack.enter_context(
            patch("custom_components.wolta.config_flow._energy_dashboard_defaults", return_value={})
        )
        stack.enter_context(
            patch("custom_components.wolta.stats.async_fetch_lifetime",
                  new=AsyncMock(return_value=lifetime))
        )
        stack.enter_context(
            patch("custom_components.wolta.config_flow.battery_platforms",
                  return_value=list(platforms))
        )
        yield mock_client


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
    # voluptuous wraps an explicit default value in a callable (default_factory);
    # a Required() with NO default at all leaves .default as the raw vol.UNDEFINED
    # sentinel, which is not callable - handle both so this helper also works for
    # fields (like CONF_ZONE) that must render with nothing pre-selected.
    d = marker.default
    return d() if callable(d) else d


# ---------------------------------------------------------------------------
# Full happy-path flow: user → entities → plant → entry created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_creates_entry(hass: HomeAssistant) -> None:
    """Full user→entities→plant flow creates a config entry with correct data."""
    mock_client = _mock_client()

    with _patched(mock_client):
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
            result["flow_id"], user_input=STEP_PLANT_DATA
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
    assert data[CONF_SHARE] is False
    # The battery figures are measured by the backend now, so nothing is cached for
    # them here (spec 2026-09-14 §7.1) - a stale cached value would be re-sent by
    # reauth and overwrite what the measurement found.
    assert CONF_BATTERY_KWH not in data
    assert CONF_BATTERY_KW not in data
    assert CONF_EFF not in data


@pytest.mark.asyncio
async def test_full_flow_unique_id_is_sha256_prefix(hass: HomeAssistant) -> None:
    """Entry unique_id is sha256(token)[:16]."""
    mock_client = _mock_client()
    expected_unique_id = hashlib.sha256(TOKEN.encode()).hexdigest()[:16]

    with _patched(mock_client):
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
            result["flow_id"], user_input=STEP_PLANT_DATA
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

    with _patched(mock_client):
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
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert CONF_SOLAR not in data or not data.get(CONF_SOLAR)


# ---------------------------------------------------------------------------
# External control (spec 2026-08-26): optional binary_sensor picker, client-local
# (same nature as CONF_INVERT_BATTERY - an upload transformation, never PATCHed to
# the server, so it stays out of _PROFILE_SYNC_KEYS).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_with_external_control_entity(hass: HomeAssistant) -> None:
    """Setup flow accepts a selected binary sensor and stores it in entry.data."""
    mock_client = _mock_client()
    entities_with_external = {
        **STEP_ENTITIES_DATA,
        CONF_EXTERNAL_CONTROL: "binary_sensor.grid_rewards_active",
    }

    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=entities_with_external
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_EXTERNAL_CONTROL] == "binary_sensor.grid_rewards_active"


@pytest.mark.asyncio
async def test_full_flow_without_external_control_entity(hass: HomeAssistant) -> None:
    """Flow works unchanged when the field is left empty - key absent, so the
    coordinator sees None (not an empty string)."""
    mock_client = _mock_client()

    with _patched(mock_client):
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
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert CONF_EXTERNAL_CONTROL not in data or not data.get(CONF_EXTERNAL_CONTROL)


@pytest.mark.asyncio
async def test_entities_step_clearing_external_control_after_error_sticks(
    hass: HomeAssistant,
) -> None:
    """F3: a cleared single-value picker must stay cleared across a re-render.

    The setup step re-shows the form with `defaults = user_input` after a validation
    error. Declared with `default=`, voluptuous re-fills the key whenever the frontend
    omits it - and omitting the key is exactly what clearing an optional field does -
    so the entry would be created with a sensor the user explicitly removed. The
    multi-select stream fields are immune (multiple=True submits [] instead of
    omitting), which is why only this field needs `suggested_value`.
    """
    mock_client = _mock_client()
    first_try = {
        **STEP_ENTITIES_DATA,
        CONF_GRID_IN: [],  # invalid -> forces the re-render
        CONF_EXTERNAL_CONTROL: "binary_sensor.grid_rewards_active",
    }
    # The frontend OMITS a cleared optional key rather than sending an empty value.
    second_try = dict(STEP_ENTITIES_DATA)

    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=first_try
        )
        assert result["step_id"] == "entities"
        assert result["errors"].get(CONF_GRID_IN) == "required_sensor"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=second_try
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert not result["data"].get(CONF_EXTERNAL_CONTROL), (
        "the cleared external-control sensor was silently restored"
    )


# ---------------------------------------------------------------------------
# Economy, tariff, reserve and nameplate fields left the plant step with the
# two-step rewrite (spec 2026-09-14 §7.1) - they all live in options → settings,
# whose own tests below cover editing them. What the create path still owes is
# that it sends NONE of them: an unasked value silently seeded into a fresh
# profile is what the options diff would then measure "unchanged" against.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_flow_sends_no_economy_or_tariff_fields(hass: HomeAssistant) -> None:
    """Setup asks for zone/control system/share/invert only, so the POST carries no
    economy, tariff, reserve or nameplate value - and entry.data caches none."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    kwargs = mock_client.create_profile.await_args.kwargs
    for key in ("cost_sek", "purchase_date", "reserve_pct", "nameplate_kwh", "nameplate_kw",
                "grid_var_ore", "surcharge_ore", "export_extra_ore",
                "grid_var_pct", "export_extra_pct"):
        assert kwargs.get(key) is None, f"{key} must not be sent on create"
    for key in (CONF_COST_SEK, CONF_PURCHASE_DATE, CONF_RESERVE_PCT,
                CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE, CONF_EXPORT_EXTRA_ORE,
                CONF_GRID_VAR_PCT, CONF_EXPORT_EXTRA_PCT):
        assert key not in result["data"], f"{key} must not be cached in entry.data"


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
    """WoltaApiError during create_profile shows cannot_connect in the plant step."""
    from custom_components.wolta.api import WoltaApiError

    mock_client = _mock_client()
    mock_client.create_profile = AsyncMock(side_effect=WoltaApiError("failed"))

    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        # The plant step is where the POST happens now, so this is where the failure
        # must land: the form re-shows with the error instead of dead-ending.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "plant"
    assert result["errors"].get("base") == "cannot_connect"


# ---------------------------------------------------------------------------
# share default is False (plant step default)
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

    mock_client = _mock_client()
    mock_client.create_profile = AsyncMock(
        side_effect=WoltaApiError("HTTP 422 from .../profile: ...", status=422)
    )

    with _patched(mock_client):
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
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "plant"
    assert result["errors"].get("base") == "invalid_input"


@pytest.mark.asyncio
async def test_non422_api_error_shows_cannot_connect(hass: HomeAssistant) -> None:
    """HTTP 500 (or other non-422) from create_profile → cannot_connect error."""
    from custom_components.wolta.api import WoltaApiError

    mock_client = _mock_client()
    mock_client.create_profile = AsyncMock(
        side_effect=WoltaApiError("HTTP 500 from .../profile: ...", status=500)
    )

    with _patched(mock_client):
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
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "plant"
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


@pytest.mark.asyncio
async def test_reauth_flow_preserves_all_profile_fields(hass: HomeAssistant) -> None:
    """Reauth recreates the profile from entry.data (the old server profile is gone), so it
    MUST seed every configured field – not just battery basics – or reserve_pct, economy,
    tariffs and nameplate silently reset server-side until the user re-opens Configure."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.wolta.const import CONF_NAMEPLATE_KW, CONF_NAMEPLATE_KWH

    initial_data: dict[str, Any] = {
        CONF_TOKEN: "old-token",
        CONF_ZONE: ZONE,
        CONF_BATT_IN: ["sensor.battery_charge"],
        CONF_BATT_OUT: ["sensor.battery_discharge"],
        CONF_GRID_IN: ["sensor.grid_import"],
        CONF_GRID_OUT: ["sensor.grid_export"],
        CONF_SOLAR: ["sensor.solar"],
        CONF_BATTERY_KWH: 15.36,
        CONF_BATTERY_KW: 5.0,
        CONF_EFF: 0.9,
        CONF_SHARE: True,
        CONF_RESERVE_PCT: 10.0,
        CONF_COST_SEK: 120000.0,
        CONF_PURCHASE_DATE: "2026-03-01",
        CONF_GRID_VAR_ORE: 40.0,
        CONF_SURCHARGE_ORE: 8.0,
        CONF_EXPORT_EXTRA_ORE: 5.0,
        CONF_NAMEPLATE_KWH: 15.36,
        CONF_NAMEPLATE_KW: 8.0,
    }
    entry = MockConfigEntry(
        domain=DOMAIN, title="Wolta (SE3)", data=initial_data,
        source=config_entries.SOURCE_USER, unique_id="reauth-fields")
    entry.add_to_hass(hass)

    mock_client = _mock_client("new-token-xyz")
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=initial_data)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={})

    assert result["reason"] == "reauth_successful"
    kwargs = mock_client.create_profile.call_args.kwargs
    assert kwargs["reserve_pct"] == 10.0
    assert kwargs["cost_sek"] == 120000.0
    assert kwargs["purchase_date"] == "2026-03-01"
    assert kwargs["grid_var_ore"] == 40.0
    assert kwargs["surcharge_ore"] == 8.0
    assert kwargs["export_extra_ore"] == 5.0
    assert kwargs["nameplate_kwh"] == 15.36
    assert kwargs["nameplate_kw"] == 8.0
    # Cachen HAR ett par, så profilen återskapas med det – inte deklarerad.
    assert kwargs["battery_declared"] is False


@pytest.mark.asyncio
async def test_reauth_linked_profile_omits_plant_scoped_cost(hass: HomeAssistant) -> None:
    """En länkad (webbskapad) profil kan vara plant-scopad: dess cost_sek täcker HELA
    anläggningen. Reauth skapar en ny HA-ägd (batteri-scopad) profil, så cost_sek MÅSTE
    utelämnas — annars re-scopar backenden anläggningspriset till batteri-capex och ger
    vilseledande IRR. Scope-neutrala fält (reserve/tariffer/nameplate) seedas ändå."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.wolta.const import CONF_CREATED_BY_HA

    initial_data: dict[str, Any] = {
        CONF_TOKEN: "old-token", CONF_ZONE: ZONE,
        CONF_BATT_IN: ["sensor.bi"], CONF_BATT_OUT: ["sensor.bo"],
        CONF_GRID_IN: ["sensor.gi"], CONF_GRID_OUT: ["sensor.go"],
        CONF_SOLAR: ["sensor.s"],
        CONF_BATTERY_KWH: 15.0, CONF_BATTERY_KW: 5.0, CONF_EFF: 0.9,
        CONF_SHARE: True,
        CONF_COST_SEK: 250000.0,      # plant-nivå-pris speglat från servern
        CONF_RESERVE_PCT: 10.0,
        CONF_CREATED_BY_HA: False,    # länkad (webbskapad) profil
    }
    entry = MockConfigEntry(
        domain=DOMAIN, title="Wolta (SE3)", data=initial_data,
        source=config_entries.SOURCE_USER, unique_id="reauth-linked")
    entry.add_to_hass(hass)

    mock_client = _mock_client("new-token-xyz")
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=initial_data)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={})

    assert result["reason"] == "reauth_successful"
    kwargs = mock_client.create_profile.call_args.kwargs
    assert kwargs["cost_sek"] is None       # plant-scopat pris utelämnas
    assert kwargs["reserve_pct"] == 10.0    # scope-neutrala fält seedas ändå


@pytest.mark.asyncio
async def test_reauth_without_pair_sends_declared(hass: HomeAssistant) -> None:
    """En entry skapad väntande har inga kWh/kW i cachen (backend mäter dem), så reauth
    måste återskapa profilen DEKLARERAD – aldrig med ett halvt par, som 422:ar."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    initial_data: dict[str, Any] = {
        CONF_TOKEN: "old-token",
        CONF_ZONE: ZONE,
        CONF_BATT_IN: ["sensor.battery_charge"],
        CONF_BATT_OUT: ["sensor.battery_discharge"],
        CONF_GRID_IN: ["sensor.grid_import"],
        CONF_GRID_OUT: ["sensor.grid_export"],
        CONF_SHARE: False,
    }
    entry = MockConfigEntry(
        domain=DOMAIN, title="Wolta (SE3)", data=initial_data,
        source=config_entries.SOURCE_USER, unique_id="reauth-declared")
    entry.add_to_hass(hass)

    mock_client = _mock_client("new-token-xyz")
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=initial_data)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={})

    assert result["reason"] == "reauth_successful"
    kwargs = mock_client.create_profile.await_args.kwargs
    assert kwargs["battery_declared"] is True
    assert kwargs["battery_kwh"] is None and kwargs["battery_kw"] is None
    assert kwargs["eff"] is None


@pytest.mark.asyncio
async def test_reauth_half_pair_sends_declared_not_nulls(hass: HomeAssistant) -> None:
    """En HALV cache (bara kWh) får inte bli `battery_kwh: null, battery_kw: null` utan
    deklaration – det 422:ar eller skapar en batterilös rad. Halva paret deklareras och
    mäts om i stället, och paret går aldrig ut halvt."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    initial_data: dict[str, Any] = {
        CONF_TOKEN: "old-token",
        CONF_ZONE: ZONE,
        CONF_BATT_IN: ["sensor.battery_charge"],
        CONF_BATT_OUT: ["sensor.battery_discharge"],
        CONF_GRID_IN: ["sensor.grid_import"],
        CONF_GRID_OUT: ["sensor.grid_export"],
        CONF_BATTERY_KWH: 22.0,   # effekten saknas – halv cache
        CONF_SHARE: False,
    }
    entry = MockConfigEntry(
        domain=DOMAIN, title="Wolta (SE3)", data=initial_data,
        source=config_entries.SOURCE_USER, unique_id="reauth-half-pair")
    entry.add_to_hass(hass)

    mock_client = _mock_client("new-token-xyz")
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=initial_data)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={})

    assert result["reason"] == "reauth_successful"
    kwargs = mock_client.create_profile.await_args.kwargs
    assert kwargs["battery_declared"] is True
    assert kwargs["battery_kwh"] is None and kwargs["battery_kw"] is None


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

    with _patched(mock_client):
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
            result["flow_id"], user_input=STEP_PLANT_DATA
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

    with _patched(mock_client):
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
    CONF_EXPORT_EXTRA_ORE, CONF_GRID_VAR_PCT, CONF_EXPORT_EXTRA_PCT,
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
    CONF_GRID_VAR_PCT: "tariffs", CONF_EXPORT_EXTRA_PCT: "tariffs",
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
        assert result["type"] == FlowResultType.MENU
        assert result["step_id"] == "init"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "settings"

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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "settings"
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


def _settings_marker(result, section_name: str, field: str):
    """Hämta voluptuous-markören för ett fält i en options-sektion."""
    outer = result["data_schema"].schema
    sec_key = next(k for k in outer
                   if (k.schema if hasattr(k, "schema") else str(k)) == section_name)
    inner = outer[sec_key].schema.schema
    return next(k for k in inner
                if (k.schema if hasattr(k, "schema") else str(k)) == field)


@pytest.mark.asyncio
async def test_options_pending_battery_pair_is_optional(hass: HomeAssistant) -> None:
    """Väntande/needs_input-rad: servern HAR inget par än, så formuläret får inte kräva
    det. Ett vol.Required hade renderat DEFAULT_BATTERY_KWH och skrivit 22 kWh över
    mätningen så fort användaren sparade något annat i dialogen."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(return_value=_server_profile(
        entry, battery_kwh=None, battery_kw=None, eff=None, battery_status="pending"))

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
        for field in (CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF):
            assert isinstance(_settings_marker(result, "battery", field), vol.Optional), field
        # Ett tomt par går igenom och PATCH:ar ingenting – varken värden eller nullar.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts())

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_purchase_date_suggests_prefill(hass: HomeAssistant) -> None:
    """Datumförslaget ur statistiken skickades aldrig till servern, så options är där
    användaren bekräftar det (spec 2026-09-14 §7.1) – men serverns egna datum vinner."""
    entry = _make_mock_entry(hass, {CONF_PREFILL_PURCHASE_DATE: "2024-03-01"})
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(
        return_value=_server_profile(entry, purchase_date=None))

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
    marker = _settings_marker(result, "economy", CONF_PURCHASE_DATE)
    assert (marker.description or {}).get("suggested_value") == "2024-03-01"

    # Samma entry, nytt options-flöde: nu HAR servern ett datum och det vinner.
    mock_client.get_profile = AsyncMock(
        return_value=_server_profile(entry, purchase_date="2023-01-15"))
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
    marker = _settings_marker(result, "economy", CONF_PURCHASE_DATE)
    assert (marker.description or {}).get("suggested_value") == "2023-01-15"


@pytest.mark.asyncio
async def test_options_purchase_date_prefill_is_offered_once(hass: HomeAssistant) -> None:
    """Förslaget är ett ENGÅNGSerbjudande: har formuläret visat det en gång tas nyckeln
    bort. Annars hade varje senare sparning föreslagit datumet igen och därmed
    återuppväckt ett datum användaren medvetet rensade."""
    entry = _make_mock_entry(hass, {CONF_PREFILL_PURCHASE_DATE: "2024-03-01"})
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(
        return_value=_server_profile(entry, purchase_date=None))

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
        marker = _settings_marker(result, "economy", CONF_PURCHASE_DATE)
        assert (marker.description or {}).get("suggested_value") == "2024-03-01"
        # Användaren sparar med datumet RENSAT (nyckeln utelämnad, som frontenden gör).
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**{CONF_RESERVE_PCT: 10.0}))

    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert CONF_PREFILL_PURCHASE_DATE not in entry.data

    # Andra rundan: erbjudandet är förbrukat, fältet är tomt igen.
    mock_client.get_profile = AsyncMock(
        return_value=_server_profile(entry, purchase_date=None))
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
    marker = _settings_marker(result, "economy", CONF_PURCHASE_DATE)
    assert (marker.description or {}).get("suggested_value") is None


@pytest.mark.asyncio
async def test_options_rejects_half_battery_pair(hass: HomeAssistant) -> None:
    """Väntande rad: paret är Optional, så ett ensamt kWh är submitbart. Det får inte
    PATCH:as – ett halvt par i cachen kan reauth bara deklarera bort, och backend
    avvisar ett halvt par."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(return_value=_server_profile(
        entry, battery_kwh=None, battery_kw=None, eff=None, battery_status="pending"))

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**{CONF_BATTERY_KWH: 22.0}))

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_BATTERY_KW: "battery_pair_incomplete"}
    mock_client.patch_profile.assert_not_awaited()


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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "settings"
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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


@pytest.mark.asyncio
async def test_options_ar_en_meny(hass: HomeAssistant) -> None:
    """Options-flowen öppnar med en meny (beslut 2026-08-25): dagens formulär bakom
    "settings", kopplingskoden bakom "account_link", zonrättelsen bakom "zone_correction"
    (v0.32.0 - posten visas bara när landet har mer än en zon, se _zone_group)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_TOKEN: "tok", CONF_ZONE: ZONE,
                             CONF_BATTERY_KWH: 10.0, CONF_BATTERY_KW: 5.0,
                             CONF_EFF: 0.9, **STEP_ENTITIES_DATA},
        unique_id="opts-menu")
    entry.add_to_hass(hass)
    mock_client, _ = _mock_options_env(entry)
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.MENU
        assert set(result["menu_options"]) == {
            "settings", "account_link", "zone_correction"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "settings"


@pytest.mark.asyncio
async def test_options_visar_kopplingskod(hass: HomeAssistant) -> None:
    """Kopplingskoden (spec 2026-08-24 §4.2): options-menyns account_link-val visar
    en engångskod från POST /profile/claim-code i description_placeholders."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_TOKEN: "tok", CONF_ZONE: ZONE,
                             CONF_BATTERY_KWH: 10.0, CONF_BATTERY_KW: 5.0,
                             CONF_EFF: 0.9, **STEP_ENTITIES_DATA},
        unique_id="claim-ui")
    entry.add_to_hass(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.mint_claim_code = AsyncMock(return_value="ABCD-EFGH")
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "account_link"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "account_link"
    assert "ABCD-EFGH" in str(result["description_placeholders"])


@pytest.mark.asyncio
async def test_options_kopplingskod_fel_ger_abort(hass: HomeAssistant) -> None:
    """Ett misslyckat mint_claim_code får inte visa ett tomt formulär (till skillnad
    från mint_link, som degraderar tyst) - flowet ska avbryta med cannot_connect."""
    from custom_components.wolta.api import WoltaApiError
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_TOKEN: "tok", CONF_ZONE: ZONE,
                             CONF_BATTERY_KWH: 10.0, CONF_BATTERY_KW: 5.0,
                             CONF_EFF: 0.9, **STEP_ENTITIES_DATA},
        unique_id="claim-fail")
    entry.add_to_hass(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.mint_claim_code = AsyncMock(side_effect=WoltaApiError("boom", status=500))
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "account_link"})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


@pytest.mark.asyncio
async def test_options_kopplingskod_purgad_profil_startar_reauth(
    hass: HomeAssistant,
) -> None:
    """WoltaAuthError (404 - purgad/okänd profil) på mint_claim_code ska starta
    reauth precis som async_step_settings gör på get_profile, inte falla ner i det
    bredare `except WoltaApiError` och abortera med det vilseledande cannot_connect
    (review-fynd: IMPORTANT 1, config_flow.py)."""
    from custom_components.wolta.api import WoltaAuthError
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_TOKEN: "tok", CONF_ZONE: ZONE,
                             CONF_BATTERY_KWH: 10.0, CONF_BATTERY_KW: 5.0,
                             CONF_EFF: 0.9, **STEP_ENTITIES_DATA},
        unique_id="claim-purged")
    entry.add_to_hass(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.mint_claim_code = AsyncMock(side_effect=WoltaAuthError("404"))
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "account_link"})
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_required"
    reauth_flows = [
        f for f in hass.config_entries.flow.async_progress()
        if f["context"].get("source") == config_entries.SOURCE_REAUTH
    ]
    assert len(reauth_flows) == 1


@pytest.mark.asyncio
async def test_options_kopplingskod_tom_kropp_ger_abort(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Task 13-reviewfynd: ett 2xx-svar utan body fick TypeErrora ut ur flowet
    (data["code"] på None) i stället för att avbryta rent. Testar mot den RIKTIGA
    WoltaApiClient (inte en mockad mint_claim_code) så att api.py:s vakt faktiskt
    körs - en mockad metod hade dolt regressionen helt."""
    from custom_components.wolta.api import WoltaApiClient
    from custom_components.wolta.const import WOLTA_API_BASE
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_TOKEN: "tok", CONF_ZONE: ZONE,
                             CONF_BATTERY_KWH: 10.0, CONF_BATTERY_KW: 5.0,
                             CONF_EFF: 0.9, **STEP_ENTITIES_DATA},
        unique_id="claim-empty-body")
    entry.add_to_hass(hass)
    aioclient_mock.post(
        f"{WOLTA_API_BASE}/api/v1/profile/claim-code", status=200, text="")
    real_client = WoltaApiClient(aioclient_mock.create_session({}), base_url=WOLTA_API_BASE)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=real_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "account_link"})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


@pytest.mark.asyncio
async def test_options_kopplingskod_natverksfel_ger_abort(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Task 13-reviewfynd: ett riktigt offline-läge (DNS/anslutningsfel) kastas av
    self._session.request(...) INNAN _request hinner konvertera det till en
    WoltaApiError - ett smalt `except WoltaApiError` i flowet läckte det ut som en
    oskyddad aiohttp.ClientConnectorError. Samma verkliga-klient-uppställning som
    testet ovan, så api.py:s except-gren faktiskt körs."""
    import aiohttp
    from custom_components.wolta.api import WoltaApiClient
    from custom_components.wolta.const import WOLTA_API_BASE
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_TOKEN: "tok", CONF_ZONE: ZONE,
                             CONF_BATTERY_KWH: 10.0, CONF_BATTERY_KW: 5.0,
                             CONF_EFF: 0.9, **STEP_ENTITIES_DATA},
        unique_id="claim-network-fail")
    entry.add_to_hass(hass)
    connection_key = aiohttp.client_reqrep.ConnectionKey(
        host="wolta.se", port=443, is_ssl=True, ssl=None,
        proxy=None, proxy_auth=None, proxy_headers_hash=None)
    aioclient_mock.post(
        f"{WOLTA_API_BASE}/api/v1/profile/claim-code",
        exc=aiohttp.ClientConnectorError(
            connection_key=connection_key, os_error=OSError("offline")))
    real_client = WoltaApiClient(aioclient_mock.create_session({}), base_url=WOLTA_API_BASE)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=real_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "account_link"})
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

    with _patched(mock_client):
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


@pytest.mark.asyncio
async def test_link_steget_avvisar_laslank(hass: HomeAssistant) -> None:
    """Efter bytet är Besök-länken en ?link=wpl_… som INTE duger som ägar-token.
    Användaren ska få veta varför, inte ett generiskt 'ogiltig token'."""
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=_mock_client()),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"profile_input": "https://wolta.se/anlaggning?link=wpl_abc"})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"profile_input": "link_is_read_link"}


@pytest.mark.asyncio
async def test_link_steget_avvisar_ral_wpl_token(hass: HomeAssistant) -> None:
    """En rå wpl_-token (utan ?link=) ska avvisas lika tydligt som en full länk."""
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=_mock_client()),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"profile_input": "wpl_abcdef"})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"profile_input": "link_is_read_link"}


@pytest.mark.asyncio
async def test_reauth_view_only_avvisar_laslank(hass: HomeAssistant) -> None:
    """Samma vakt i den andra anroparen: reauth_view_only avvisar en inklistrad
    läslänk med samma riktade felmeddelande som länk-steget."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.wolta.const import CONF_VIEW_ONLY

    data: dict[str, Any] = {CONF_TOKEN: "dead-token", CONF_ZONE: ZONE,
                            CONF_VIEW_ONLY: True, CONF_CREATED_BY_HA: False,
                            CONF_BATTERY_KWH: 22.0, CONF_BATTERY_KW: 5.0}
    entry = MockConfigEntry(domain=DOMAIN, data=data, source=config_entries.SOURCE_USER,
                            unique_id="uid-view-laslank")
    entry.add_to_hass(hass)
    mock_client = _mock_client()

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=data)
        assert result["type"] == FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": "https://wolta.se/anlaggning?link=wpl_zzz"})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"profile_input": "link_is_read_link"}
    mock_client.get_profile.assert_not_called()


@pytest.mark.asyncio
async def test_create_flow_order_entities_then_plant_creates_entry(
    hass: HomeAssistant,
) -> None:
    """Två steg (spec 2026-09-14 §7.1): entities → plant → entry. POST /profile i
    plant-steget, utan battery_kwh/kw/eff, med battery_declared."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
        mock_client.create_profile.assert_not_awaited()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_PLANT_DATA)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CREATED_BY_HA] is True
    mock_client.create_profile.assert_awaited_once()
    kwargs = mock_client.create_profile.await_args.kwargs
    assert kwargs["battery_declared"] is True
    assert kwargs.get("battery_kwh") is None and kwargs.get("battery_kw") is None
    assert kwargs.get("eff") is None
    assert kwargs["share_profile"] is False
    assert CONF_BATTERY_KWH not in result["data"] and CONF_EFF not in result["data"]


@pytest.mark.asyncio
async def test_plant_step_has_no_privacy_step(hass: HomeAssistant) -> None:
    """The plant step asks for exactly four things (share and the invert toggle moved
    here from the removed privacy step); everything else is measured or lives in
    options. The field list IS the contract - a stray number field here would be a
    value the user is asked for twice."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
        keys = {(k.schema if hasattr(k, "schema") else str(k))
                for k in result["data_schema"].schema}
    assert keys == {CONF_ZONE, "control_system", "control_system_name",
                    CONF_SHARE, CONF_INVERT_BATTERY}


@pytest.mark.asyncio
async def test_zone_suggestion_first_with_label_no_default(hass: HomeAssistant) -> None:
    """Zone is an ACTIVE choice (directive 2026-08-24): it selects the price series the
    grade AND the economics are measured against, and the server treats it as immutable
    after creation. So the location guess is surfaced by MOVING that zone to the top of
    the dropdown with a labelled hint (spec 2026-09-14 §7.1) - never as a default, which
    would be acceptance-by-inaction."""
    hass.config.country = "SE"
    hass.config.latitude = 59.33  # -> SE3, per the SE latitude bands
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
    assert _plant_schema_default(result, CONF_ZONE) is vol.UNDEFINED
    zone_marker = next(k for k in result["data_schema"].schema
                       if (k.schema if hasattr(k, "schema") else str(k)) == CONF_ZONE)
    options = result["data_schema"].schema[zone_marker].config["options"]
    assert options[0]["value"] == "SE3"
    assert "föreslag" in options[0]["label"].lower() or "suggest" in options[0]["label"].lower()
    # The rest of the list keeps its order, so the dropdown is not reshuffled.
    assert [o["value"] for o in options[1:4]] == ["SE1", "SE2", "SE4"]


@pytest.mark.asyncio
async def test_create_flow_zone_guess_surfaced_as_text(hass: HomeAssistant) -> None:
    """The location guess is demoted from decision to information: it must still
    reach the user, just as text in the description, not as a pre-selected value."""
    hass.config.country = "SE"
    hass.config.latitude = 59.33  # -> SE3, per the SE latitude bands
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
    hint = result["description_placeholders"]["zone_hint"]
    assert "SE3" in hint
    assert "still choose" in hint


@pytest.mark.asyncio
async def test_create_flow_zone_no_guess_reads_sensibly(hass: HomeAssistant) -> None:
    """No HA location configured (latitude defaults to 0.0) -> suggest_zone() returns
    None. The description must degrade to a neutral phrase, not 'looks likely: None'."""
    hass.config.country = "SE"
    hass.config.latitude = 0.0
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
    hint = result["description_placeholders"]["zone_hint"]
    assert "None" not in hint
    assert "could not suggest" in hint


@pytest.mark.asyncio
async def test_create_flow_zone_no_guess_unsupported_country(hass: HomeAssistant) -> None:
    """A country the band logic doesn't cover also degrades to the neutral phrase."""
    hass.config.country = "US"
    hass.config.latitude = 40.0
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
    hint = result["description_placeholders"]["zone_hint"]
    assert "None" not in hint
    assert "could not suggest" in hint


@pytest.mark.asyncio
async def test_plant_step_requires_zone(hass: HomeAssistant) -> None:
    """Submitting the plant step without a zone must not pass - it must never
    silently create a plant with the old SE3 default."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
        submission = {k: v for k, v in STEP_PLANT_DATA.items() if k != CONF_ZONE}
        with pytest.raises(Exception):  # vol.MultipleInvalid: required key missing
            await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input=submission
            )


@pytest.mark.asyncio
async def test_control_system_default_from_platforms(hass: HomeAssistant) -> None:
    """The battery sensors' integration domain identifies the control system well
    enough to PRE-SELECT it (spec 2026-09-14 §7.1) - unlike the zone, a wrong value
    here is correctable afterwards, and the mapping comes from the backend."""
    mock_client = _mock_client()
    mock_client.get_control_systems = AsyncMock(return_value=[
        {"id": "huawei", "label": "Huawei", "ha_domains": ["huawei_solar"]}])
    with _patched(mock_client, platforms=["huawei_solar", "huawei_solar"]):
        result = await _drive_create_to_plant(hass)
    assert _plant_schema_default(result, "control_system") == "huawei"


@pytest.mark.asyncio
async def test_control_system_unknown_id_is_not_defaulted(hass: HomeAssistant) -> None:
    """Backendens lista ligger före vår egen (const.CONTROL_SYSTEMS): ett id vi inte har
    som alternativ hade förvalt ett värde SelectSelector själv avvisar → MultipleInvalid
    i sista onboarding-steget. Okänt förslag ⇒ ingen default."""
    mock_client = _mock_client()
    mock_client.get_control_systems = AsyncMock(return_value=[
        {"id": "brandnew", "label": "Brand New", "ha_domains": ["brandnew_battery"]}])
    with _patched(mock_client, platforms=["brandnew_battery"]):
        result = await _drive_create_to_plant(hass)
    assert _plant_schema_default(result, "control_system") is vol.UNDEFINED


@pytest.mark.asyncio
async def test_control_system_no_default_without_suggestion(hass: HomeAssistant) -> None:
    """No suggestion (unknown domain, a tie, or a failed mapping fetch) leaves the
    field without a default, as before the prefill existed - a guessed default would
    park every inattentive user in the same corpus bucket."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
    assert _plant_schema_default(result, "control_system") is vol.UNDEFINED


@pytest.mark.asyncio
async def test_invert_shown_always_prefilled_on_suspicion(hass: HomeAssistant) -> None:
    """Steady out > in in the history means the two battery meters are swapped, so the
    toggle is pre-ticked. The field itself is shown either way (decision 2026-09-14) -
    see test_plant_step_has_no_privacy_step, which finds it without any suspicion."""
    mock_client = _mock_client()
    # charged/discharged/first_ts: ratio 1.2 > the 1.05 invert threshold, over enough
    # days and kWh for stats.analyze_battery_history to trust the ratio at all.
    with _patched(mock_client,
                  lifetime=(1000.0, 1200.0, datetime(2025, 1, 1, tzinfo=timezone.utc))):
        result = await _drive_create_to_plant(hass)
    assert _plant_schema_default(result, CONF_INVERT_BATTERY) is True


@pytest.mark.asyncio
async def test_prefill_purchase_date_stored_not_sent(hass: HomeAssistant) -> None:
    """The first statistics timestamp is not necessarily a purchase date, and a
    silently filled one steers the payback calculation - so it is cached in entry.data
    for options → Economy to offer, never sent on create (spec 2026-09-14 §7.1)."""
    mock_client = _mock_client()
    with _patched(mock_client,
                  lifetime=(100.0, 90.0, datetime(2024, 3, 1, tzinfo=timezone.utc))):
        result = await _drive_create_to_plant(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_PLANT_DATA)
    assert result["data"][CONF_PREFILL_PURCHASE_DATE] == "2024-03-01"
    assert CONF_PURCHASE_DATE not in result["data"]
    kwargs = mock_client.create_profile.await_args.kwargs
    assert kwargs.get("purchase_date") is None


def test_zone_hint_localized_by_language() -> None:
    """The zone hint is a full, structurally different sentence per branch (guess
    vs no-guess) - not one translation string with a value placeholder - so it
    lives in the Python-side _ZONE_HINT_* tables (see the comment above them),
    keyed on the two-letter language prefix. Distinctive substrings only, so a
    copy edit doesn't break this test."""
    from custom_components.wolta.config_flow import _zone_hint  # noqa: PLC0415

    supported = {"SE3"}

    assert "stämma" in _zone_hint("sv", "SE3", supported)
    assert "looks likely" in _zone_hint("en", "SE3", supported)
    assert "kunde inte föreslå" in _zone_hint("sv", None, supported)
    assert "could not suggest" in _zone_hint("en", None, supported)
    # Unrelated language (e.g. a German-language HA install) falls back to English.
    assert "looks likely" in _zone_hint("de", "SE3", supported)
    # Locale variants (e.g. "sv-SE") are matched on the two-letter prefix only.
    assert "stämma" in _zone_hint("sv-SE", "SE3", supported)


@pytest.mark.asyncio
async def test_create_flow_zone_hint_wired_to_ha_language(hass: HomeAssistant) -> None:
    """End-to-end check that hass.config.language actually reaches the rendered
    hint (not just the pure _zone_hint() table tested above)."""
    hass.config.language = "sv"
    hass.config.country = "SE"
    hass.config.latitude = 59.33  # -> SE3, per the SE latitude bands
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
    hint = result["description_placeholders"]["zone_hint"]
    assert "stämma" in hint
    assert "looks likely" not in hint


@pytest.mark.asyncio
async def test_link_flow_invert_suspected_shows_check_step(hass: HomeAssistant) -> None:
    """Koppla-spåret saknar plant-steg → misstänkt inversion ger eget kontrollsteg."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value=dict(LINK_PROFILE))
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})
    first = datetime(2025, 1, 1, tzinfo=timezone.utc)

    with _patched(mock_client, lifetime=(880.0, 1000.0, first)):
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


@pytest.mark.asyncio
async def test_reconfigure_updates_external_control_entity(hass: HomeAssistant) -> None:
    """Reconfigure stores a newly picked external-control sensor."""
    entry = _make_mock_entry(hass)
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATT_IN: ["sensor.battery_charge"],
                CONF_BATT_OUT: ["sensor.battery_discharge"],
                CONF_GRID_IN: ["sensor.grid_import"],
                CONF_GRID_OUT: ["sensor.grid_export"],
                CONF_SOLAR: ["sensor.solar"],
                CONF_EXTERNAL_CONTROL: "binary_sensor.grid_rewards_active",
            },
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_EXTERNAL_CONTROL] == "binary_sensor.grid_rewards_active"


@pytest.mark.asyncio
async def test_reconfigure_clears_external_control_entity_when_left_empty(
    hass: HomeAssistant,
) -> None:
    """Clearing a previously-set sensor selection on reconfigure normalises back to
    absence. The field uses `suggested_value` rather than `default=` (see the
    comment in config_flow.py) precisely so that omitting the key - what the real
    frontend does when an optional single-entity picker is cleared - is not
    re-filled with the stale stored value by voluptuous."""
    entry = _make_mock_entry(
        hass, extra_data={CONF_EXTERNAL_CONTROL: "binary_sensor.old_grid_rewards"}
    )
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATT_IN: ["sensor.battery_charge"],
                CONF_BATT_OUT: ["sensor.battery_discharge"],
                CONF_GRID_IN: ["sensor.grid_import"],
                CONF_GRID_OUT: ["sensor.grid_export"],
                CONF_SOLAR: ["sensor.solar"],
            },
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert not updated.data.get(CONF_EXTERNAL_CONTROL)


# ---------------------------------------------------------------------------
# Flex compensation (spec 2026-08-28): optional currency-sensor picker.
# Unlike the external-control picker this one is NOT an upload transformation -
# it is read monthly and PATCHed to the server as flex_compensation records with
# source="sensor". The selector mechanics are the same, though: single-value
# EntitySelector, suggested_value (never `default=`), clearing == absence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_with_flex_compensation_entity(hass: HomeAssistant) -> None:
    """Setup flow accepts a selected sensor and stores it in entry.data."""
    mock_client = _mock_client()
    entities_with_flex = {
        **STEP_ENTITIES_DATA,
        CONF_FLEX_COMPENSATION: "sensor.checkwatt_monthly_compensation",
    }

    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=entities_with_flex
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert (
        result["data"][CONF_FLEX_COMPENSATION]
        == "sensor.checkwatt_monthly_compensation"
    )


@pytest.mark.asyncio
async def test_full_flow_without_flex_compensation_entity(hass: HomeAssistant) -> None:
    """The field is optional: left empty the key is absent, so the coordinator
    sees None and never reads (or PATCHes) anything."""
    mock_client = _mock_client()

    with _patched(mock_client):
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
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert CONF_FLEX_COMPENSATION not in data or not data.get(CONF_FLEX_COMPENSATION)


@pytest.mark.asyncio
async def test_entities_step_clearing_flex_compensation_after_error_sticks(
    hass: HomeAssistant,
) -> None:
    """A cleared single-value picker must stay cleared across a re-render.

    Same v0.3.0 trap as the external-control field: declared with `default=`,
    voluptuous re-fills the key whenever the frontend omits it - and omitting the
    key is exactly what clearing an optional field does - so the entry would be
    created with a sensor the user explicitly removed, and we would go on PATCHing
    compensation figures they asked us to stop sending.
    """
    mock_client = _mock_client()
    first_try = {
        **STEP_ENTITIES_DATA,
        CONF_GRID_IN: [],  # invalid -> forces the re-render
        CONF_FLEX_COMPENSATION: "sensor.checkwatt_monthly_compensation",
    }
    # The frontend OMITS a cleared optional key rather than sending an empty value.
    second_try = dict(STEP_ENTITIES_DATA)

    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=first_try
        )
        assert result["step_id"] == "entities"
        assert result["errors"].get(CONF_GRID_IN) == "required_sensor"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=second_try
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert not result["data"].get(CONF_FLEX_COMPENSATION), (
        "the cleared flex-compensation sensor was silently restored"
    )


@pytest.mark.asyncio
async def test_reconfigure_updates_flex_compensation_entity(
    hass: HomeAssistant,
) -> None:
    """Reconfigure stores a newly picked compensation sensor."""
    entry = _make_mock_entry(hass)
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATT_IN: ["sensor.battery_charge"],
                CONF_BATT_OUT: ["sensor.battery_discharge"],
                CONF_GRID_IN: ["sensor.grid_import"],
                CONF_GRID_OUT: ["sensor.grid_export"],
                CONF_SOLAR: ["sensor.solar"],
                CONF_FLEX_COMPENSATION: "sensor.checkwatt_monthly_compensation",
            },
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert (
        updated.data[CONF_FLEX_COMPENSATION]
        == "sensor.checkwatt_monthly_compensation"
    )


@pytest.mark.asyncio
async def test_reconfigure_clears_flex_compensation_entity_when_left_empty(
    hass: HomeAssistant,
) -> None:
    """Clearing a previously-set selection normalises back to absence, so the
    coordinator stops reading the sensor. (Stopping the reads does NOT delete the
    months already stored server-side - that is the web card's job, and the
    integration never sends amount_sek=null on its own.)"""
    entry = _make_mock_entry(
        hass, extra_data={CONF_FLEX_COMPENSATION: "sensor.old_compensation"}
    )
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_BATT_IN: ["sensor.battery_charge"],
                CONF_BATT_OUT: ["sensor.battery_discharge"],
                CONF_GRID_IN: ["sensor.grid_import"],
                CONF_GRID_OUT: ["sensor.grid_export"],
                CONF_SOLAR: ["sensor.solar"],
            },
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert not updated.data.get(CONF_FLEX_COMPENSATION)


@pytest.mark.asyncio
async def test_flex_compensation_is_never_sent_to_create_profile(
    hass: HomeAssistant,
) -> None:
    """The ENTITY ID is client-local configuration - only the monthly AMOUNTS ever
    reach the server, and only through the coordinator's PATCH. An entity id in the
    profile payload would leak the user's HA naming for no benefit."""
    mock_client = _mock_client()
    entities_with_flex = {
        **STEP_ENTITIES_DATA,
        CONF_FLEX_COMPENSATION: "sensor.checkwatt_monthly_compensation",
    }

    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=entities_with_flex
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    kwargs = mock_client.create_profile.call_args.kwargs
    assert CONF_FLEX_COMPENSATION not in kwargs
    assert "flex_compensation" not in kwargs


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

    with _patched(mock_client):
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

    with _patched(mock_client):
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
async def test_link_accepts_pending_profile(hass: HomeAssistant) -> None:
    """A profile whose capacity is still being measured has no kWh/kW pair yet, but it
    HAS a battery - the pair-is-missing rule would reject it (spec 2026-09-14 §7.2)."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value={"zone": "SE3", "battery_kwh": None,
        "battery_kw": None, "battery_status": "pending", "derived": {}})
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})
    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": TOKEN})
    assert result["step_id"] == "entities"


@pytest.mark.asyncio
async def test_link_rejects_none_status(hass: HomeAssistant) -> None:
    """battery_status "none" is the one value that still means solar-only: the
    integration's grade semantics assume a battery."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value={"zone": "SE3", "battery_kwh": None,
        "battery_kw": None, "battery_status": "none", "derived": {}})
    mock_client.adopt_profile = AsyncMock()
    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": TOKEN})
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

    with _patched(mock_client):
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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_required"
    reauth_flows = [
        f for f in hass.config_entries.flow.async_progress()
        if f["context"].get("source") == config_entries.SOURCE_REAUTH
    ]
    assert len(reauth_flows) == 1


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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
            result["flow_id"], user_input={"next_step_id": "settings"})
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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
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

    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "create"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_ENTITIES_DATA)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_PLANT_DATA)

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


@pytest.mark.asyncio
async def test_link_flow_offers_view_only_for_bound_plant(hass: HomeAssistant) -> None:
    """A streaming-bound plant (Sonnen webhook / Reduxi) is offered VIEW-ONLY linking:
    a confirm step instead of the entities form. No adopt (the backend 409s it and no
    identity should be stamped), no entity selection, and the entry is marked view_only
    so the coordinator never uploads. This replaces the hard already_streaming error
    from v0.17.0 - the block became a mode."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(
        return_value={**LINK_PROFILE, "derived": {"transport": "sonnen_webhook"}})
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": False})

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": LINK_TOKEN})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "view_only"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    from custom_components.wolta.const import CONF_VIEW_ONLY
    data = result["data"]
    assert data[CONF_VIEW_ONLY] is True
    assert data[CONF_CREATED_BY_HA] is False, "view-only far ALDRIG radera profilen server-side"
    assert CONF_BATT_IN not in data, "inga entiteter i visningslage"
    mock_client.adopt_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_view_only_reauth_asks_for_new_token(hass: HomeAssistant) -> None:
    """Reauth on a view-only entry must NEVER create a new profile (the default reauth
    does, by design, for streaming entries). Instead it asks for a fresh token - the
    web-side owner can mint one - and just swaps CONF_TOKEN."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.wolta.const import CONF_VIEW_ONLY

    data: dict[str, Any] = {CONF_TOKEN: "dead-token", CONF_ZONE: ZONE,
                            CONF_VIEW_ONLY: True, CONF_CREATED_BY_HA: False,
                            CONF_BATTERY_KWH: 22.0, CONF_BATTERY_KW: 5.0}
    entry = MockConfigEntry(domain=DOMAIN, data=data, source=config_entries.SOURCE_USER,
                            unique_id="uid-view-1")
    entry.add_to_hass(hass)
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(
        return_value={**LINK_PROFILE, "derived": {"transport": "sonnen_webhook"}})

    with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=data)
        assert result["type"] == FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": "fresh-token-xyz"})

    assert result["reason"] == "reauth_successful"
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_TOKEN] == "fresh-token-xyz"
    mock_client.create_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_flow_allows_plain_ha_transport(hass: HomeAssistant) -> None:
    """transport 'ha' (or a missing derived block from an older server) must NOT block —
    re-linking one's own HA-streamed or web-created profile is the normal case."""
    for derived in ({"transport": "ha"}, {}):
        mock_client = _mock_client()
        mock_client.get_profile = AsyncMock(return_value={**LINK_PROFILE, "derived": derived})
        mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})

        with patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client), \
             patch("custom_components.wolta.config_flow.async_get_clientsession"):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER})
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"next_step_id": "link"})
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"profile_input": LINK_TOKEN})

        assert result["step_id"] == "entities", f"blocked with derived={derived}"
        mock_client.adopt_profile.assert_awaited_once()
        # unique_id städas mellan varven (dinglande flow avbryts, inget entry skapades)
        hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.asyncio
async def test_reconfigure_aborts_for_view_only(hass: HomeAssistant) -> None:
    """Reconfigure on a view-only entry must abort, not show the entity form.

    The form's stream fields are Required with empty defaults (no entities exist on the
    entry) - a dead end - and had the user filled them in, the selections would be stored
    but IGNORED by the coordinator's view-only branch: it would look like streaming was
    enabled without anything streaming."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.wolta.const import CONF_VIEW_ONLY

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TOKEN: "tok-view", CONF_ZONE: ZONE, CONF_VIEW_ONLY: True,
              CONF_CREATED_BY_HA: False},
        source=config_entries.SOURCE_USER, unique_id="uid-view-reconf")
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "view_only_no_reconfigure"


@pytest.mark.asyncio
async def test_options_flow_changes_pct_field(hass: HomeAssistant) -> None:
    """Changing grid_var_pct in the options flow → patch_profile with the new value."""
    entry = _make_mock_entry(
        hass,
        extra_data={CONF_GRID_VAR_PCT: 5.61},
    )
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                CONF_GRID_VAR_PCT: 7.5,  # changed
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {"grid_var_pct": 7.5}
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_GRID_VAR_PCT] == 7.5


@pytest.mark.asyncio
async def test_options_flow_clears_pct_field(hass: HomeAssistant) -> None:
    """Clearing a previously set pct field → patch_profile null (clear-to-schablon),
    and an unrelated change preserves an untouched pct field (no silent wipe)."""
    entry = _make_mock_entry(
        hass,
        extra_data={CONF_GRID_VAR_PCT: 5.61},
    )
    mock_client, mock_coordinator = _mock_options_env(entry)

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{
                CONF_BATTERY_KWH: 22.0,
                CONF_BATTERY_KW: 5.0,
                CONF_EFF: 0.9,
                # grid_var_pct omitted = actively cleared
            }),
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {"grid_var_pct": None}


# ---------------------------------------------------------------------------
# Control system is an ACTIVE choice (audit finding 2026-08-24): every plant
# onboarded through this integration became 'unknown' in the corpus statistics
# because neither the flow nor the backend path could set control_system. The
# plant step now requires a strictly validated selection (mirrors the web
# guide's rule from MR !100), "other" additionally requires a free-text name,
# and both values are sent on create and stored in entry data so reauth
# re-sends them. String literals (not const imports) are deliberate: the test
# was written before the constants existed (TDD red run).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plant_step_requires_control_system(hass: HomeAssistant) -> None:
    """Submitting the plant step without a control system must not pass."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "create"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_ENTITIES_DATA
        )
        assert result["step_id"] == "plant"
        with pytest.raises(Exception):  # vol.MultipleInvalid: required key missing
            await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={k: v for k, v in STEP_PLANT_DATA.items()
                            if k != "control_system"},
            )


@pytest.mark.asyncio
async def test_plant_step_other_requires_name(hass: HomeAssistant) -> None:
    """control_system='other' without a name re-shows the form with an error."""
    mock_client = _mock_client()
    with _patched(mock_client):
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
            result["flow_id"],
            user_input={**STEP_PLANT_DATA, "control_system": "other"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "plant"
        assert result["errors"] == {"control_system_name": "control_system_name_required"}


@pytest.mark.asyncio
async def test_create_profile_sends_and_stores_control_system(hass: HomeAssistant) -> None:
    """The chosen control system reaches create_profile and lands in entry data."""
    mock_client = _mock_client()
    with _patched(mock_client):
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
            result["flow_id"],
            user_input={**STEP_PLANT_DATA, "control_system": "emhass"},
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.create_profile.call_args.kwargs["control_system"] == "emhass"
    assert mock_client.create_profile.call_args.kwargs["control_system_name"] is None
    assert result["data"]["control_system"] == "emhass"


@pytest.mark.asyncio
async def test_reauth_resends_stored_control_system(hass: HomeAssistant) -> None:
    """Reauth re-creates the profile WITH the stored control system; an entry
    from before v0.29.0 (no stored field) must OMIT it (None) - the backend
    preserves the stored value on omission, so omitting is the safe default."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    for stored, expected in (({"control_system": "sonnen"}, "sonnen"), ({}, None)):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_TOKEN: "old-token", CONF_ZONE: ZONE,
                CONF_BATTERY_KWH: 22.0, CONF_BATTERY_KW: 5.0, CONF_EFF: 0.9,
                CONF_CREATED_BY_HA: True,
                **STEP_ENTITIES_DATA,
                **stored,
            },
            unique_id=f"reauth-cs-{expected}",
        )
        entry.add_to_hass(hass)
        mock_client = _mock_client("new-token")
        with (
            patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
            patch("custom_components.wolta.config_flow.async_get_clientsession"),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_REAUTH,
                         "entry_id": entry.entry_id},
                data=entry.data,
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={}
            )
        assert mock_client.create_profile.call_args.kwargs.get("control_system") == expected


# ---------------------------------------------------------------------------
# v0.32.0: zone correction (backend api 0.80.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zone_correction_patches_and_updates_entry(hass: HomeAssistant) -> None:
    """A zone correction must PATCH the server AND move the entry's stored zone.

    The stored zone is not cosmetic: reauth resends entry_data[CONF_ZONE], and the server
    refuses a re-onboard whose zone differs from the one on record. Correcting only one of
    the two leaves the integration in a state where the next reauth fails.
    """
    entry = _make_mock_entry(hass)
    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock(return_value={"zone": "SE4"})

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "zone_correction"})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "zone_correction"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_ZONE: "SE4"})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {"zone": "SE4"}
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_ZONE] == "SE4"


@pytest.mark.asyncio
async def test_zone_correction_unchanged_does_not_patch(hass: HomeAssistant) -> None:
    """Re-picking the zone that is already stored is a no-op, not a PATCH."""
    entry = _make_mock_entry(hass)
    stored = entry.data[CONF_ZONE]
    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock()

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "zone_correction"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_ZONE: stored})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_zone_correction_rejected_keeps_stored_zone(hass: HomeAssistant) -> None:
    """A server rejection must NOT move the entry's zone.

    Otherwise the integration would believe in a zone the server never accepted - and every
    later reauth would resend it and fail.
    """
    from custom_components.wolta.api import WoltaApiError

    entry = _make_mock_entry(hass)
    stored = entry.data[CONF_ZONE]
    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock(side_effect=WoltaApiError("422"))

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "zone_correction"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_ZONE: "SE1"})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "zone_rejected"}
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_ZONE] == stored


def test_zone_group_never_crosses_currency() -> None:
    """_zone_group offers only same-country zones - strictly narrower than the server's
    same-currency rule, so the menu can never propose a change the server would 422."""
    from custom_components.wolta.config_flow import _zone_group

    se = [z for z, _ in _zone_group("SE3")]
    assert se == ["SE1", "SE2", "SE3", "SE4"]
    assert all(z.startswith("SE") for z in se)
    # A single-zone country yields one entry, which is why async_step_init hides the menu
    # item in that case rather than offering a form with only the current value.
    assert len(_zone_group("SE3")) > 1
