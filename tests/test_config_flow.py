"""Tests for custom_components/wolta/config_flow.py (TDD)."""

from __future__ import annotations

import asyncio
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
    CONF_GRID_BENEFIT_ORE,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_GRID_VAR_ORE,
    CONF_GRID_VAR_PCT,
    CONF_INVERT_BATTERY,
    CONF_PREFILL_PURCHASE_DATE,
    CONF_PLANT_ID,
    CONF_PURCHASE_DATE,
    CONF_REDUCED_ENERGY_TAX,
    CONF_RESERVE_PCT,
    CONF_RETAIL_FIXED_ORE,
    CONF_RETAIL_PRICE_MODEL,
    CONF_SHARE,
    CONF_SOC,
    CONF_SOC_ABSENT,
    CONF_SOC_ISSUE_IGNORED,
    CONF_SOLAR,
    CONF_SURCHARGE_ORE,
    CONF_TOKEN,
    CONF_ZONE,
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
# `invert_battery` moved here from the removed privacy step. The `share` checkbox
# that used to sit alongside it is gone too (2026-09-15 decision): it never gated
# corpus membership or the raw-data retention it claimed to, so entries are always
# created with share_profile=True now - see CONF_SHARE in const.py.
STEP_PLANT_DATA = {
    CONF_ZONE: ZONE,
    # Mandatory since v0.29.0 (active choice, no default) - see the control_system
    # test block at the end of this file.
    "control_system": "emhass",
    CONF_INVERT_BATTERY: False,
}

STEP_ENTITIES_DATA = {
    CONF_BATT_IN: ["sensor.battery_charge"],
    CONF_BATT_OUT: ["sensor.battery_discharge"],
    CONF_GRID_IN: ["sensor.grid_import"],
    CONF_GRID_OUT: ["sensor.grid_export"],
    CONF_SOLAR: ["sensor.solar_production"],
    # SoC is required since v0.50.0 (spec 2026-10-20 §4.1): a sensor or this box. The
    # box keeps the tests that are not about SoC free of a sensor state to set up; the
    # SoC tests below swap it for a sensor.
    CONF_SOC_ABSENT: True,
}

STEP_ENTITIES_NO_SOLAR = {
    CONF_BATT_IN: ["sensor.battery_charge"],
    CONF_BATT_OUT: ["sensor.battery_discharge"],
    CONF_GRID_IN: ["sensor.grid_import"],
    CONF_GRID_OUT: ["sensor.grid_export"],
    CONF_SOC_ABSENT: True,
}


def _mock_client(token: str = TOKEN) -> MagicMock:
    """Return a mock WoltaApiClient whose create_profile returns TOKEN."""
    mock = MagicMock()
    mock.create_profile = AsyncMock(return_value=token)
    # The entities step asks the backend for the domain → control-system mapping; an
    # empty mapping means "no suggestion", which is the neutral default for tests that
    # are not about the prefill.
    mock.get_control_systems = AsyncMock(return_value=[])
    # The link path PATCHes soc_absent when the answer differs from the server's.
    mock.patch_profile = AsyncMock(return_value={})
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
    # The share checkbox is gone (2026-09-15) - every entry is created shared now.
    assert data[CONF_SHARE] is True
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
# The share checkbox is gone (2026-09-15 decision, see const.py CONF_SHARE) - it
# never gated corpus membership (corpus_submission ignores it) or the raw-data
# retention it claimed to gate (warm.py keeps the series for every integration
# row regardless); the only real effect was disabling the expansion calculator.
# So the plant step no longer asks, and every plant is created/reauthed shared.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plant_schema_has_no_share_field(hass: HomeAssistant) -> None:
    """The plant step's schema must not offer a share toggle any more."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
        keys = {(k.schema if hasattr(k, "schema") else str(k))
                for k in result["data_schema"].schema}
    assert CONF_SHARE not in keys


@pytest.mark.asyncio
async def test_create_always_sends_share_profile_true(hass: HomeAssistant) -> None:
    """Create must send share_profile=True unconditionally, and persist share: True
    in entry.data, even though the plant step no longer collects it."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], STEP_PLANT_DATA)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.create_profile.await_args.kwargs["share_profile"] is True
    assert result["data"][CONF_SHARE] is True


@pytest.mark.asyncio
async def test_reauth_sends_share_profile_true_even_if_stored_false(
    hass: HomeAssistant,
) -> None:
    """An entry created before this change may still carry share: False in
    entry.data (the key is kept for reauth/diagnostics). Reauth must NOT read it -
    it always sends share_profile=True, since a hidden False would be an invisible
    dead end with no privacy upside (the series is kept regardless, and a linked
    row can't in practice carry False - the web opt-out never saves a returning
    token to link with)."""
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
        source=config_entries.SOURCE_USER, unique_id="reauth-share-false")
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
    assert mock_client.create_profile.await_args.kwargs["share_profile"] is True


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
        CONF_SOC_ABSENT: True,
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
        # The answer to the SoC requirement (v0.50.0), so a reconfigure that keeps the
        # box ticked (_reconfigure_input) changes nothing and PATCHes nothing.
        CONF_SOC_ABSENT: True,
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
    CONF_REDUCED_ENERGY_TAX, CONF_GRID_BENEFIT_ORE,
    CONF_RETAIL_PRICE_MODEL, CONF_RETAIL_FIXED_ORE,
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
    CONF_GRID_BENEFIT_ORE: "tariffs",
    CONF_GRID_VAR_PCT: "tariffs", CONF_EXPORT_EXTRA_PCT: "tariffs",
    CONF_REDUCED_ENERGY_TAX: "tariffs",
    CONF_RETAIL_PRICE_MODEL: "tariffs", CONF_RETAIL_FIXED_ORE: "tariffs",
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
    mätningen så fort användaren sparade något annat i dialogen.

    eff är UNDANTAGET och förblir Required: backend lagrar alltid eff på en deklarerad
    rad (0.9), så fältet är förifyllt även medan paret mäts. Som Optional hade det gått
    att rensa → PATCH eff: null → backend 422 ("eff kan inte tas bort") som i dialogen
    bara syns som cannot_connect."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(return_value=_server_profile(
        entry, battery_kwh=None, battery_kw=None, eff=0.9, battery_status="pending"))

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
        for field in (CONF_BATTERY_KWH, CONF_BATTERY_KW):
            assert isinstance(_settings_marker(result, "battery", field), vol.Optional), field
        eff_marker = _settings_marker(result, "battery", CONF_EFF)
        assert isinstance(eff_marker, vol.Required), "eff får inte bli rensningsbar"
        assert eff_marker.default() == 0.9
        # Ett tomt par går igenom och PATCH:ar ingenting – varken värden eller nullar.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**{CONF_EFF: 0.9}))

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_explicit_null_faller_till_default(hass: HomeAssistant) -> None:
    """Servern kan skicka nycklarna MED null (till skillnad från att utelämna dem).
    `srv.get(key, DEFAULT)` ger då None, och vol.Required(default=None) går inte att
    spara – formuläret låser sig. Defaulten måste falla på null, inte bara på frånvaro."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(return_value=_server_profile(
        entry, battery_kwh=None, battery_kw=None, eff=None))

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})

    for field in (CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF):
        marker = _settings_marker(result, "battery", field)
        assert isinstance(marker, vol.Required), field
        assert marker.default() is not None, f"{field}: null-nyckel gav default=None"


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
    avvisar ett halvt par.

    Felet sätts på BÅDE det saknade fältet och "base": fältfelet sitter på ett fält
    INUTI section("battery"), och renderar frontend inte fältfel i sektioner ser
    användaren bara ett formulär som vägrar spara utan att säga varför."""
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
    assert result["errors"] == {CONF_BATTERY_KW: "battery_pair_incomplete",
                                "base": "battery_pair_incomplete"}
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
    """Toggling battery-invert in options → entry.data is updated + coordinator refresh (it
    starts the direction job), and NO patch_profile (client-side transform, not a backend field)."""
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
    # The coordinator is refreshed → the direction job starts
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
    # Personliga länken bär token i fragmentet sedan Woltas säkerhetsgranskning 2026-10-16
    # (fragmentet når aldrig servern). Båda formerna ska fungera att klistra in.
    assert extract_token("https://wolta.se/anlaggning#profile=abc%2B123") == "abc+123"
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
    assert kwargs["share_profile"] is True
    assert CONF_BATTERY_KWH not in result["data"] and CONF_EFF not in result["data"]


@pytest.mark.asyncio
async def test_plant_step_has_no_privacy_step(hass: HomeAssistant) -> None:
    """The plant step asks for exactly four things (the invert toggle moved here from
    the removed privacy step; the share checkbox that used to sit next to it is gone
    entirely, 2026-09-15); everything else is measured or lives in options. The field
    list IS the contract - a stray field here would be a value the user is asked for
    twice."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
        keys = {(k.schema if hasattr(k, "schema") else str(k))
                for k in result["data_schema"].schema}
    assert keys == {CONF_ZONE, "control_system", "control_system_name",
                    CONF_INVERT_BATTERY}


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
async def test_control_system_options_come_from_the_server(hass: HomeAssistant) -> None:
    """With label_en on every row the plant step lists the server's control systems, so one
    added on the backend can be picked without a HACS release (2026-10-29)."""
    mock_client = _mock_client()
    mock_client.get_control_systems = AsyncMock(return_value=[
        {"id": "tibber", "label": "Tibber", "label_en": "Tibber", "ha_domains": []},
        {"id": "darkstar", "label": "Darkstar", "label_en": "Darkstar", "ha_domains": []},
        {"id": "other", "label": "Annat", "label_en": "Other", "ha_domains": []}])
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
    marker = next(k for k in result["data_schema"].schema if str(k) == "control_system")
    selector = result["data_schema"].schema[marker]
    assert [o["value"] for o in selector.config["options"]] == ["darkstar", "tibber", "other"]


@pytest.mark.asyncio
async def test_control_system_options_fall_back_when_the_fetch_fails(
        hass: HomeAssistant) -> None:
    from custom_components.wolta.const import CONTROL_SYSTEMS

    mock_client = _mock_client()
    mock_client.get_control_systems = AsyncMock(side_effect=TimeoutError)
    with _patched(mock_client):
        result = await _drive_create_to_plant(hass)
    marker = next(k for k in result["data_schema"].schema if str(k) == "control_system")
    selector = result["data_schema"].schema[marker]
    assert [o["value"] for o in selector.config["options"]] == [c for c, _ in CONTROL_SYSTEMS]
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
                CONF_SOC_ABSENT: True,
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
                CONF_SOC_ABSENT: True,
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
                CONF_SOC_ABSENT: True,
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
                CONF_SOC_ABSENT: True,
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
                CONF_SOC_ABSENT: True,
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
                CONF_SOC_ABSENT: True,
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
# Battery state-of-charge sensors (spec 2026-10-03): optional multi-select, one
# sensor per battery unit. Client-local configuration like the two pickers above -
# the entity ids never reach the server, only the statistics read from them do.
# ---------------------------------------------------------------------------

SOC_ENTITY = "sensor.battery_soc"
SOC_ATTRS = {"state_class": "measurement", "unit_of_measurement": "%"}


async def _drive_entities(hass, entities_input, mock_client):
    """Meny -> create -> entities; returns the result of submitting `entities_input`."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "create"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=entities_input
    )


@pytest.mark.asyncio
async def test_full_flow_with_soc_entities(hass: HomeAssistant) -> None:
    """Setup stores the picked SoC sensors as a list in entry.data."""
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: [SOC_ENTITY]}, mock_client
        )
        assert result["step_id"] == "plant"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOC] == [SOC_ENTITY]


@pytest.mark.asyncio
async def test_entities_step_rejects_soc_sensor_without_state_class(
    hass: HomeAssistant,
) -> None:
    """Without state_class measurement there are no statistics to read (spec 6.5)."""
    hass.states.async_set(SOC_ENTITY, "55", {"unit_of_measurement": "%"})
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: [SOC_ENTITY]}, mock_client
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "entities"
    assert result["errors"] == {CONF_SOC: "soc_not_measurement"}


@pytest.mark.asyncio
async def test_entities_step_rejects_soc_sensor_not_in_percent(
    hass: HomeAssistant,
) -> None:
    """A measurement sensor in kWh is not a charge level - unit must be %."""
    hass.states.async_set(
        SOC_ENTITY, "5.5", {"state_class": "measurement", "unit_of_measurement": "kWh"}
    )
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: [SOC_ENTITY]}, mock_client
        )

    assert result["errors"] == {CONF_SOC: "soc_not_measurement"}


@pytest.mark.asyncio
async def test_entities_step_rejects_unavailable_soc_sensor(
    hass: HomeAssistant,
) -> None:
    """A sensor with no state at all cannot be vetted, so it is not accepted - and the
    error says it is missing, not that it is the wrong kind of sensor."""
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: ["sensor.no_such_soc"]}, mock_client
        )

    assert result["errors"] == {CONF_SOC: "soc_unavailable"}


# HA keeps a sensor's capability attributes (state_class) and its unit on the state even
# when the entity is `unavailable` (helpers/entity.py __async_calculate_state writes them
# before it checks `available`; the entity registry's write_unavailable_state does the
# same for entities that have not loaded yet after a restart). An `unknown` sensor is
# available and carries everything. So a valid SoC sensor that is only offline for the
# moment must be accepted, and the attributes are judged as for any other state.


@pytest.mark.asyncio
@pytest.mark.parametrize("dead_state", ["unavailable", "unknown"])
async def test_entities_step_accepts_valid_soc_sensor_that_is_offline(
    hass: HomeAssistant, dead_state: str
) -> None:
    """A measurement/% sensor whose state is `unavailable`/`unknown` (battery API down,
    HA just restarted) still carries its attributes and is accepted."""
    hass.states.async_set(SOC_ENTITY, dead_state, SOC_ATTRS)
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: [SOC_ENTITY]}, mock_client
        )

    assert not result.get("errors")
    assert result["step_id"] == "plant"


@pytest.mark.asyncio
@pytest.mark.parametrize("dead_state", ["unavailable", "unknown"])
async def test_entities_step_offline_soc_sensor_without_attributes_is_unavailable(
    hass: HomeAssistant, dead_state: str
) -> None:
    """A dead state with no state_class and no unit (a state set by hand, an entity that
    never reported) cannot be vetted - `soc_unavailable`, not the misleading
    `soc_not_measurement`."""
    hass.states.async_set(SOC_ENTITY, dead_state)
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: [SOC_ENTITY]}, mock_client
        )

    assert result["errors"] == {CONF_SOC: "soc_unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("dead_state", ["unavailable", "unknown"])
async def test_soc_error_judges_attributes_of_an_offline_sensor(
    hass: HomeAssistant, dead_state: str
) -> None:
    """The attributes decide even when the state is dead: state_class total_increasing
    (an energy counter, wrong kind) is `soc_not_measurement`, a measurement/% sensor
    passes, and a sensor with neither attribute is `soc_unavailable` - which also wins
    over a wrong-kind sensor in the same pick (order unavailable -> not_measurement)."""
    from custom_components.wolta.config_flow import _soc_error

    hass.states.async_set("sensor.counter", dead_state,
                          {"state_class": "total_increasing",
                           "unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.ok", dead_state, SOC_ATTRS)
    hass.states.async_set("sensor.bare", dead_state)

    assert _soc_error(hass, ["sensor.counter"]) == "soc_not_measurement"
    assert _soc_error(hass, ["sensor.ok"]) is None
    assert _soc_error(hass, ["sensor.bare"]) == "soc_unavailable"
    assert _soc_error(hass, ["sensor.counter", "sensor.bare"]) == "soc_unavailable"


@pytest.mark.asyncio
async def test_entities_step_offline_soc_sensor_of_wrong_kind_is_not_measurement(
    hass: HomeAssistant,
) -> None:
    """unavailable + state_class total_increasing: HA did write the attributes, so the
    sensor is judged on them."""
    hass.states.async_set(SOC_ENTITY, "unavailable",
                          {"state_class": "total_increasing", "unit_of_measurement": "%"})
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: [SOC_ENTITY]}, mock_client
        )

    assert result["errors"] == {CONF_SOC: "soc_not_measurement"}


@pytest.mark.asyncio
async def test_entities_step_rejects_soc_entity_id_over_128_chars(
    hass: HomeAssistant,
) -> None:
    """The API's `unit` is at most 128 characters; a longer id would never be sent, so
    the picker says so instead of accepting a sensor that is silently skipped."""
    long_id = "sensor." + "s" * 122  # 129 chars
    hass.states.async_set(long_id, "55", SOC_ATTRS)
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: [long_id]}, mock_client
        )

    assert result["errors"] == {CONF_SOC: "soc_id_too_long"}


@pytest.mark.asyncio
async def test_soc_error_order_unavailable_before_not_measurement_before_too_long(
    hass: HomeAssistant,
) -> None:
    """Order: unavailable -> not_measurement -> id_too_long -> too_many."""
    from custom_components.wolta.config_flow import _soc_error

    long_id = "sensor." + "s" * 122
    hass.states.async_set(long_id, "55", SOC_ATTRS)
    hass.states.async_set("sensor.kwh", "5", {"state_class": "measurement",
                                              "unit_of_measurement": "kWh"})
    many = [f"sensor.battery_soc_{i}" for i in range(17)]
    for entity_id in many:
        hass.states.async_set(entity_id, "55", SOC_ATTRS)

    assert _soc_error(hass, [long_id, "sensor.kwh", "sensor.missing"]) == "soc_unavailable"
    assert _soc_error(hass, [long_id, "sensor.kwh"]) == "soc_not_measurement"
    assert _soc_error(hass, [*many, long_id]) == "soc_id_too_long"
    assert _soc_error(hass, many) == "soc_too_many"
    assert _soc_error(hass, many[:16]) is None


@pytest.mark.asyncio
async def test_entities_step_rejects_more_than_16_soc_sensors(
    hass: HomeAssistant,
) -> None:
    """The API's battery_sources takes at most 16; more would 422 the whole upload."""
    ids = [f"sensor.battery_soc_{i}" for i in range(17)]
    for entity_id in ids:
        hass.states.async_set(entity_id, "55", SOC_ATTRS)
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: ids}, mock_client
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_SOC: "soc_too_many"}


@pytest.mark.asyncio
async def test_entities_step_accepts_exactly_16_soc_sensors(
    hass: HomeAssistant,
) -> None:
    """The limit is inclusive."""
    ids = [f"sensor.battery_soc_{i}" for i in range(16)]
    for entity_id in ids:
        hass.states.async_set(entity_id, "55", SOC_ATTRS)
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: ids}, mock_client
        )

    assert result["step_id"] == "plant"


@pytest.mark.asyncio
async def test_soc_error_does_not_mask_missing_required_stream(
    hass: HomeAssistant,
) -> None:
    """Required streams are checked first; both errors are shown together."""
    hass.states.async_set(SOC_ENTITY, "55", {"unit_of_measurement": "%"})
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass,
            {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_GRID_IN: [], CONF_SOC: [SOC_ENTITY]},
            mock_client,
        )

    assert result["errors"].get(CONF_GRID_IN) == "required_sensor"


@pytest.mark.asyncio
async def test_full_flow_without_soc_entities(hass: HomeAssistant) -> None:
    """Left empty, the key is absent - the coordinator then collects no SoC at all."""
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(hass, STEP_ENTITIES_DATA, mock_client)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_SOC not in result["data"]


@pytest.mark.asyncio
async def test_entities_step_clearing_soc_after_error_sticks(
    hass: HomeAssistant,
) -> None:
    """A SoC list cleared after a validation error must stay cleared.

    The field is declared with `suggested_value`, not `default=`: a `default=` pointing
    at the previously submitted list would be re-filled by voluptuous whenever the
    frontend omits the key.
    """
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
    mock_client = _mock_client()

    with _patched(mock_client):
        result = await _drive_entities(
            hass,
            {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_GRID_IN: [], CONF_SOC: [SOC_ENTITY]},
            mock_client,
        )
        assert result["step_id"] == "entities"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=dict(STEP_ENTITIES_DATA)
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert not result["data"].get(CONF_SOC), "the cleared SoC sensor was silently restored"


def _reconfigure_input(**extra):
    """Reconfigure input; the box stays ticked unless SoC sensors are picked."""
    return {
        CONF_BATT_IN: ["sensor.battery_charge"],
        CONF_BATT_OUT: ["sensor.battery_discharge"],
        CONF_GRID_IN: ["sensor.grid_import"],
        CONF_GRID_OUT: ["sensor.grid_export"],
        CONF_SOLAR: ["sensor.solar"],
        CONF_SOC_ABSENT: not extra.get(CONF_SOC),
        **extra,
    }


@pytest.mark.asyncio
async def test_reconfigure_updates_soc_entities(hass: HomeAssistant) -> None:
    """Reconfigure stores newly picked SoC sensors."""
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
    entry = _make_mock_entry(hass, extra_data={CONF_SOC_ABSENT: False})
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_reconfigure_input(**{CONF_SOC: [SOC_ENTITY]})
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_SOC] == [SOC_ENTITY]


@pytest.mark.asyncio
async def test_reconfigure_clears_soc_entities_when_left_empty(
    hass: HomeAssistant,
) -> None:
    """Clearing the picker stores an empty list (coordinator: no SoC collection)."""
    entry = _make_mock_entry(hass, extra_data={CONF_SOC: [SOC_ENTITY]})
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_reconfigure_input()
        )
    assert result["type"] == FlowResultType.ABORT
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_SOC] == []


@pytest.mark.asyncio
async def test_reconfigure_prefills_stored_soc_entities(hass: HomeAssistant) -> None:
    """The stored list is offered back as suggested_value, so a second visit shows it."""
    entry = _make_mock_entry(hass, extra_data={CONF_SOC: [SOC_ENTITY]})
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
    marker = next(k for k in result["data_schema"].schema
                  if (k.schema if hasattr(k, "schema") else str(k)) == CONF_SOC)
    assert marker.description == {"suggested_value": [SOC_ENTITY]}


@pytest.mark.asyncio
async def test_reconfigure_rejects_invalid_soc_sensor(hass: HomeAssistant) -> None:
    """Same vetting as setup: a sensor without state_class measurement is refused."""
    hass.states.async_set(SOC_ENTITY, "55", {"unit_of_measurement": "%"})
    entry = _make_mock_entry(hass, extra_data={CONF_SOC_ABSENT: False})
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_reconfigure_input(**{CONF_SOC: [SOC_ENTITY]})
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_SOC: "soc_not_measurement"}
    assert CONF_SOC not in hass.config_entries.async_get_entry(entry.entry_id).data


@pytest.mark.asyncio
@pytest.mark.parametrize("dead_state", ["unavailable", "unknown"])
async def test_reconfigure_accepts_valid_soc_sensor_that_is_offline(
    hass: HomeAssistant, dead_state: str
) -> None:
    """The case that matters: the battery's local API is down, the user reconfigures to
    add something else, and the SoC choice saved earlier (state `unavailable`, but with
    its state_class and unit as HA keeps them) must not block saving."""
    hass.states.async_set(SOC_ENTITY, dead_state, SOC_ATTRS)
    entry = _make_mock_entry(hass, extra_data={CONF_SOC_ABSENT: False})
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_reconfigure_input(**{CONF_SOC: [SOC_ENTITY]})
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_SOC] == [SOC_ENTITY]


@pytest.mark.asyncio
@pytest.mark.parametrize("dead_state", ["unavailable", "unknown"])
async def test_reconfigure_offline_soc_sensor_without_attributes_is_unavailable(
    hass: HomeAssistant, dead_state: str
) -> None:
    """No state_class and no unit on a dead state: cannot be vetted, `soc_unavailable`."""
    hass.states.async_set(SOC_ENTITY, dead_state)
    entry = _make_mock_entry(hass, extra_data={CONF_SOC_ABSENT: False})
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_reconfigure_input(**{CONF_SOC: [SOC_ENTITY]})
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_SOC: "soc_unavailable"}
    assert CONF_SOC not in hass.config_entries.async_get_entry(entry.entry_id).data


@pytest.mark.asyncio
async def test_reconfigure_rejects_more_than_16_soc_sensors(
    hass: HomeAssistant,
) -> None:
    ids = [f"sensor.battery_soc_{i}" for i in range(17)]
    for entity_id in ids:
        hass.states.async_set(entity_id, "55", SOC_ATTRS)
    entry = _make_mock_entry(hass, extra_data={CONF_SOC_ABSENT: False})
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_reconfigure_input(**{CONF_SOC: ids})
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_SOC: "soc_too_many"}


@pytest.mark.asyncio
async def test_link_flow_carries_soc_entities(hass: HomeAssistant) -> None:
    """The linked-profile path stores the SoC list like the create path does."""
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
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
            result["flow_id"], {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: [SOC_ENTITY]})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOC] == [SOC_ENTITY]


@pytest.mark.asyncio
async def test_link_flow_invert_check_path_carries_soc_entities(
    hass: HomeAssistant,
) -> None:
    """Also through the invert-check detour, which builds the entry from the same data."""
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
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
            result["flow_id"], {**STEP_ENTITIES_DATA, CONF_SOC_ABSENT: False, CONF_SOC: [SOC_ENTITY]})
        assert result["step_id"] == "invert_check"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_INVERT_BATTERY: False})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOC] == [SOC_ENTITY]


# ---------------------------------------------------------------------------
# SoC requirement (spec 2026-10-20 §4.1): a SoC sensor or the "no SoC sensor" box,
# in onboarding, Reconfigure and reauth.
# ---------------------------------------------------------------------------

# The streams alone - neither a SoC sensor nor the box, so each test states its choice.
_STREAMS_ONLY = {k: v for k, v in STEP_ENTITIES_DATA.items() if k != CONF_SOC_ABSENT}


def _schema_marker(result, field):
    return next(k for k in result["data_schema"].schema
                if (k.schema if hasattr(k, "schema") else str(k)) == field)


def _entry_without_answer(hass, **extra):
    """A mock entry with no stored soc_absent at all (an entry from before v0.50.0)."""
    entry = _make_mock_entry(hass, extra_data=extra or None)
    hass.config_entries.async_update_entry(
        entry, data={k: v for k, v in entry.data.items() if k != CONF_SOC_ABSENT})
    return entry


def test_soc_requirement_error_rules(hass: HomeAssistant) -> None:
    """One rule set: conflict, required, then the sensors' own vetting."""
    from custom_components.wolta.soc_picker import soc_requirement_error

    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
    hass.states.async_set("sensor.kwh", "1", {"state_class": "total", "unit_of_measurement": "kWh"})
    assert soc_requirement_error(hass, [], absent=False) == "soc_required"
    assert soc_requirement_error(hass, None, absent=False) == "soc_required"
    assert soc_requirement_error(hass, [SOC_ENTITY], absent=True) == "soc_conflict"
    # Conflict wins even over a sensor that would fail vetting: the contradiction is
    # the thing to fix first.
    assert soc_requirement_error(hass, ["sensor.kwh"], absent=True) == "soc_conflict"
    assert soc_requirement_error(hass, ["sensor.kwh"], absent=False) == "soc_not_measurement"
    assert soc_requirement_error(hass, [SOC_ENTITY], absent=False) is None
    assert soc_requirement_error(hass, [], absent=True) is None


@pytest.mark.asyncio
async def test_entities_requires_soc_or_absent(hass: HomeAssistant) -> None:
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**_STREAMS_ONLY, CONF_SOC_ABSENT: False}, mock_client)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "entities"
    assert result["errors"] == {CONF_SOC: "soc_required"}


@pytest.mark.asyncio
async def test_entities_requires_soc_when_box_omitted(hass: HomeAssistant) -> None:
    """The box defaults to False: a submit without it is the same as unticked."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_entities(hass, dict(_STREAMS_ONLY), mock_client)
    assert result["errors"] == {CONF_SOC: "soc_required"}


@pytest.mark.asyncio
async def test_entities_conflict(hass: HomeAssistant) -> None:
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**_STREAMS_ONLY, CONF_SOC: [SOC_ENTITY], CONF_SOC_ABSENT: True},
            mock_client)
    assert result["step_id"] == "entities"
    assert result["errors"] == {CONF_SOC: "soc_conflict"}


@pytest.mark.asyncio
async def test_entities_schema_keeps_soc_optional_and_adds_the_box(
    hass: HomeAssistant,
) -> None:
    """SoC stays vol.Optional (a Required field would be blocked by the frontend's
    generic not_all_required_fields before our soc_required could show), and the box
    follows it, default False."""
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "create"})
    keys = [str(k.schema) for k in result["data_schema"].schema]
    assert keys.index(CONF_SOC_ABSENT) == keys.index(CONF_SOC) + 1
    assert isinstance(_schema_marker(result, CONF_SOC), vol.Optional)
    box = _schema_marker(result, CONF_SOC_ABSENT)
    assert isinstance(box, vol.Optional)
    assert box.default() is False


@pytest.mark.asyncio
async def test_absent_is_stored_and_sent_on_create(hass: HomeAssistant) -> None:
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**_STREAMS_ONLY, CONF_SOC_ABSENT: True}, mock_client)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOC_ABSENT] is True
    assert mock_client.create_profile.call_args.kwargs["soc_absent"] is True


@pytest.mark.asyncio
async def test_create_with_soc_sensor_sends_absent_false(hass: HomeAssistant) -> None:
    """A picked sensor is an answer too: the server records "has a sensor"."""
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
    mock_client = _mock_client()
    with _patched(mock_client):
        result = await _drive_entities(
            hass, {**_STREAMS_ONLY, CONF_SOC: [SOC_ENTITY]}, mock_client)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=STEP_PLANT_DATA)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOC_ABSENT] is False
    assert mock_client.create_profile.call_args.kwargs["soc_absent"] is False


async def _drive_link_entities(hass, mock_client, entities_input):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "link"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"profile_input": LINK_TOKEN})
    assert result["step_id"] == "entities"
    return await hass.config_entries.flow.async_configure(result["flow_id"], entities_input)


@pytest.mark.asyncio
async def test_link_flow_absent_is_patched_then_stored(hass: HomeAssistant) -> None:
    """A linked profile already exists, so there is no create to carry the answer: it
    is PATCHed, and stored only after the server has it (the coordinator mirrors the
    server's value, so a local-only True would be wiped on the first sync)."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value=dict(LINK_PROFILE))
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})
    with _patched(mock_client):
        result = await _drive_link_entities(
            hass, mock_client, {**_STREAMS_ONLY, CONF_SOC_ABSENT: True})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once_with(LINK_TOKEN, soc_absent=True)
    assert result["data"][CONF_SOC_ABSENT] is True
    mock_client.create_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_flow_sensor_without_server_answer_does_not_patch(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value=dict(LINK_PROFILE))
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})
    with _patched(mock_client):
        result = await _drive_link_entities(
            hass, mock_client, {**_STREAMS_ONLY, CONF_SOC: [SOC_ENTITY]})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()
    assert CONF_SOC_ABSENT not in result["data"]


@pytest.mark.asyncio
async def test_link_flow_absent_patch_failure_keeps_the_form(hass: HomeAssistant) -> None:
    from custom_components.wolta.api import WoltaApiError

    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value=dict(LINK_PROFILE))
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})
    mock_client.patch_profile = AsyncMock(side_effect=WoltaApiError("boom"))
    with _patched(mock_client):
        result = await _drive_link_entities(
            hass, mock_client, {**_STREAMS_ONLY, CONF_SOC_ABSENT: True})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "entities"
    assert result["errors"] == {"base": "cannot_connect"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def _reconfigure(hass, entry, mock_client, user_input):
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}), \
         patch("custom_components.wolta.config_flow.WoltaApiClient",
               return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"):
        result = await entry.start_reconfigure_flow(hass)
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=user_input)


def _reconfigure_streams(**extra):
    base = {k: v for k, v in _reconfigure_input().items() if k != CONF_SOC_ABSENT}
    return {**base, **extra}


@pytest.mark.asyncio
async def test_reconfigure_requires_soc_or_absent(hass: HomeAssistant) -> None:
    entry = _entry_without_answer(hass)
    entry_data = dict(entry.data)
    mock_client = _mock_client()
    result = await _reconfigure(
        hass, entry, mock_client, _reconfigure_streams(**{CONF_SOC_ABSENT: False}))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_SOC: "soc_required"}
    mock_client.patch_profile.assert_not_awaited()
    assert dict(hass.config_entries.async_get_entry(entry.entry_id).data) == entry_data


@pytest.mark.asyncio
async def test_reconfigure_conflict(hass: HomeAssistant) -> None:
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
    entry = _make_mock_entry(hass)
    mock_client = _mock_client()
    result = await _reconfigure(
        hass, entry, mock_client,
        _reconfigure_streams(**{CONF_SOC: [SOC_ENTITY], CONF_SOC_ABSENT: True}))
    assert result["errors"] == {CONF_SOC: "soc_conflict"}
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_prefills_the_stored_box(hass: HomeAssistant) -> None:
    entry = _make_mock_entry(hass, extra_data={CONF_SOC_ABSENT: True})
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
    keys = [str(k.schema) for k in result["data_schema"].schema]
    assert keys.index(CONF_SOC_ABSENT) == keys.index(CONF_SOC) + 1
    assert isinstance(_schema_marker(result, CONF_SOC), vol.Optional)
    assert _schema_marker(result, CONF_SOC_ABSENT).default() is True


@pytest.mark.asyncio
async def test_reconfigure_unchecking_absent_with_sensor_patches_false(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
    entry = _make_mock_entry(
        hass, extra_data={CONF_SOC_ABSENT: True, CONF_SOC_ISSUE_IGNORED: True})
    mock_client = _mock_client()
    result = await _reconfigure(
        hass, entry, mock_client,
        _reconfigure_streams(**{CONF_SOC: [SOC_ENTITY], CONF_SOC_ABSENT: False}))
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    mock_client.patch_profile.assert_awaited_once_with(TOKEN, soc_absent=False)
    data = hass.config_entries.async_get_entry(entry.entry_id).data
    assert data.get(CONF_SOC_ABSENT) is False
    assert data[CONF_SOC] == [SOC_ENTITY]
    assert CONF_SOC_ISSUE_IGNORED not in data


@pytest.mark.asyncio
async def test_reconfigure_ticking_absent_patches_true(hass: HomeAssistant) -> None:
    entry = _entry_without_answer(hass)
    mock_client = _mock_client()
    result = await _reconfigure(
        hass, entry, mock_client, _reconfigure_streams(**{CONF_SOC_ABSENT: True}))
    assert result["reason"] == "reconfigure_successful"
    mock_client.patch_profile.assert_awaited_once_with(TOKEN, soc_absent=True)
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_SOC_ABSENT] is True


@pytest.mark.asyncio
async def test_reconfigure_pre_v050_entry_with_sensors_saves_without_patch(
    hass: HomeAssistant,
) -> None:
    """The common path for existing users after the update: an entry from before v0.50.0
    already has SoC sensors and no stored soc_absent. Opening Reconfigure and saving with
    the sensors kept and the box unticked is no change in the answer (missing == False),
    so nothing is PATCHed and no key is invented in entry.data."""
    hass.states.async_set(SOC_ENTITY, "55", SOC_ATTRS)
    entry = _entry_without_answer(hass, **{CONF_SOC: [SOC_ENTITY]})
    assert CONF_SOC_ABSENT not in entry.data
    mock_client = _mock_client()
    result = await _reconfigure(
        hass, entry, mock_client,
        _reconfigure_streams(**{CONF_SOC: [SOC_ENTITY], CONF_SOC_ABSENT: False}))
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    mock_client.patch_profile.assert_not_awaited()
    data = hass.config_entries.async_get_entry(entry.entry_id).data
    assert CONF_SOC_ABSENT not in data
    assert data[CONF_SOC] == [SOC_ENTITY]


@pytest.mark.asyncio
async def test_reconfigure_absent_patch_failure_saves_nothing(hass: HomeAssistant) -> None:
    """Ruling: soc_absent is mirrored from the server, so it is written locally only
    after the PATCH succeeded. A failed PATCH shows cannot_connect and saves nothing -
    not even the sensor changes from the same submit - so the user just retries."""
    from custom_components.wolta.api import WoltaApiError

    entry = _entry_without_answer(hass, **{CONF_SOC_ISSUE_IGNORED: True})
    entry_data = dict(entry.data)
    mock_client = _mock_client()
    mock_client.patch_profile = AsyncMock(side_effect=WoltaApiError("boom"))
    result = await _reconfigure(
        hass, entry, mock_client,
        _reconfigure_streams(**{CONF_SOC_ABSENT: True, CONF_SOLAR: ["sensor.new_solar"]}))
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}
    assert dict(hass.config_entries.async_get_entry(entry.entry_id).data) == entry_data
    # The re-shown form keeps every choice, so the retry does not drop the solar edit.
    assert _schema_marker(result, CONF_SOLAR).default() == ["sensor.new_solar"]
    assert _schema_marker(result, CONF_SOC_ABSENT).default() is True


@pytest.mark.asyncio
async def test_reconfigure_unchanged_absent_does_not_patch_but_drops_old_ignore(
    hass: HomeAssistant,
) -> None:
    """The old "ignore" answered an offer, not the requirement (spec §4.2): any save
    removes it, whether or not soc_absent changed."""
    entry = _make_mock_entry(
        hass, extra_data={CONF_SOC_ABSENT: True, CONF_SOC_ISSUE_IGNORED: True})
    mock_client = _mock_client()
    result = await _reconfigure(
        hass, entry, mock_client,
        _reconfigure_streams(**{CONF_SOC_ABSENT: True, CONF_SOLAR: ["sensor.new_solar"]}))
    assert result["reason"] == "reconfigure_successful"
    mock_client.patch_profile.assert_not_awaited()
    data = hass.config_entries.async_get_entry(entry.entry_id).data
    assert data[CONF_SOC_ABSENT] is True
    assert data[CONF_SOLAR] == ["sensor.new_solar"]
    assert CONF_SOC_ISSUE_IGNORED not in data


async def _reauth(hass, entry, mock_client):
    with patch("custom_components.wolta.config_flow.WoltaApiClient",
               return_value=mock_client), \
         patch("custom_components.wolta.config_flow.async_get_clientsession"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=dict(entry.data),
        )
        return await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})


@pytest.mark.asyncio
async def test_reauth_keeps_soc_absent(hass: HomeAssistant) -> None:
    entry = _make_mock_entry(hass, extra_data={CONF_SOC_ABSENT: True})
    mock_client = _mock_client("new-token")
    result = await _reauth(hass, entry, mock_client)
    assert result["reason"] == "reauth_successful"
    assert mock_client.create_profile.call_args.kwargs["soc_absent"] is True


@pytest.mark.asyncio
async def test_reauth_without_stored_answer_sends_none(hass: HomeAssistant) -> None:
    """None is omitted by the client, so an older server value is kept."""
    entry = _entry_without_answer(hass)
    mock_client = _mock_client("new-token")
    result = await _reauth(hass, entry, mock_client)
    assert result["reason"] == "reauth_successful"
    assert mock_client.create_profile.call_args.kwargs["soc_absent"] is None


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
    """Changing grid_var_pct in the options flow → patch_profile with the new value.
    The fixed part is there (a percentage requires it, inköpspris), so only the percent moves."""
    entry = _make_mock_entry(
        hass,
        extra_data={CONF_GRID_VAR_ORE: 20.0, CONF_GRID_VAR_PCT: 5.61},
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
                CONF_GRID_VAR_ORE: 20.0,
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
@pytest.mark.parametrize(
    "target, kept",
    [("SE4", False), ("SE1", True)],
    ids=["leaves_se1_se3", "stays_in_se1_se3"],
)
async def test_zone_correction_drops_a_stale_energy_tax_answer(
    hass: HomeAssistant, target, kept
) -> None:
    """The energy-tax answer exists only in SE1-SE3 (the server 422s `true` anywhere else), so
    a correction out of that range must not leave it in entry.data for the next reauth."""
    entry = _make_mock_entry(hass, extra_data={CONF_REDUCED_ENERGY_TAX: True})
    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock(return_value={"zone": target})

    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "zone_correction"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_ZONE: target})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = hass.config_entries.async_get_entry(entry.entry_id).data
    assert data[CONF_ZONE] == target
    assert (CONF_REDUCED_ENERGY_TAX in data) is kept


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


# ---------------------------------------------------------------------------
# Riktningsreparationen: andra vägar att vända stämplar också vändningen
# ---------------------------------------------------------------------------


def _raise_direction_issue(hass: HomeAssistant, entry) -> str:
    from homeassistant.helpers import issue_registry as ir
    issue_id = f"battery_direction_{entry.entry_id}"
    ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=True,
                          severity=ir.IssueSeverity.WARNING,
                          translation_key="battery_direction_all",
                          translation_placeholders={})
    return issue_id


@pytest.mark.asyncio
async def test_options_invert_change_stamps_flip_and_clears_direction_repair(
        hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir
    from custom_components.wolta.const import (
        CONF_DIRECTION_FLIPPED_AT, CONF_DIRECTION_ISSUE_IGNORED)
    entry = _make_mock_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_DIRECTION_ISSUE_IGNORED: True})
    issue_id = _raise_direction_issue(hass, entry)
    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock()
    mock_client.get_profile = AsyncMock(return_value=_server_profile(entry))
    mock_coordinator = MagicMock()
    mock_coordinator.async_trigger_recompute = AsyncMock()
    mock_coordinator.async_request_refresh = AsyncMock()
    entry.runtime_data = mock_coordinator
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**{CONF_BATTERY_KWH: 22.0, CONF_BATTERY_KW: 5.0, CONF_EFF: 0.9,
                                CONF_INVERT_BATTERY: True}),
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_DIRECTION_FLIPPED_AT]
    assert CONF_DIRECTION_ISSUE_IGNORED not in updated.data
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.asyncio
async def test_reconfigure_battery_sensor_swap_stamps_flip_and_clears_repair(
        hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir
    from custom_components.wolta.const import (
        CONF_DIRECTION_FLIPPED_AT, CONF_DIRECTION_ISSUE_IGNORED)
    entry = _make_mock_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_DIRECTION_ISSUE_IGNORED: True})
    issue_id = _raise_direction_issue(hass, entry)
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=_reconfigure_input(**{
                CONF_BATT_IN: ["sensor.battery_discharge"],
                CONF_BATT_OUT: ["sensor.battery_charge"],
            }),
        )
    assert result["reason"] == "reconfigure_successful"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_DIRECTION_FLIPPED_AT]
    assert CONF_DIRECTION_ISSUE_IGNORED not in updated.data
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.asyncio
async def test_reconfigure_without_battery_change_does_not_stamp_flip(
        hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    entry = _make_mock_entry(hass)
    issue_id = _raise_direction_issue(hass, entry)
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=_reconfigure_input(**{CONF_SOLAR: ["sensor.other_solar"]}),
        )
    assert result["reason"] == "reconfigure_successful"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert CONF_DIRECTION_FLIPPED_AT not in updated.data
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


@pytest.mark.asyncio
async def test_reconfigure_reordered_battery_sensors_do_not_stamp_flip(
        hass: HomeAssistant) -> None:
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    entry = _make_mock_entry(hass, {
        CONF_BATT_IN: ["sensor.battery_charge", "sensor.battery_charge_2"]})
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=_reconfigure_input(**{
                CONF_BATT_IN: ["sensor.battery_charge_2", "sensor.battery_charge"]}),
        )
    assert result["reason"] == "reconfigure_successful"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert CONF_DIRECTION_FLIPPED_AT not in updated.data


@pytest.mark.asyncio
async def test_reconfigure_legacy_string_sensor_vs_same_list_does_not_stamp_flip(
        hass: HomeAssistant) -> None:
    """En v0.1.0-post lagrar sensorn som ren sträng; samma sensor som lista är ingen ändring."""
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    entry = _make_mock_entry(hass, {CONF_BATT_IN: "sensor.battery_charge"})
    with patch("custom_components.wolta.config_flow._energy_dashboard_defaults",
               return_value={}):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_reconfigure_input())
    assert result["reason"] == "reconfigure_successful"
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert CONF_DIRECTION_FLIPPED_AT not in updated.data


# ---------------------------------------------------------------------------
# The invert toggle means "the whole history" over the desired intervals
# ---------------------------------------------------------------------------


async def _submit_invert(hass, entry, invert: bool):
    """Open the options flow, return the settings form's invert default, and submit the
    unchanged plant values with `invert`."""
    mock_client = MagicMock()
    mock_client.patch_profile = AsyncMock()
    mock_client.get_profile = AsyncMock(return_value=_server_profile(entry))
    mock_coordinator = MagicMock()
    mock_coordinator.async_trigger_recompute = AsyncMock()
    mock_coordinator.async_request_refresh = AsyncMock()
    entry.runtime_data = mock_coordinator
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "settings"})
        default = next(
            k.default() for k in result["data_schema"].schema
            if str(k) == CONF_INVERT_BATTERY)
        with patch.object(hass.config_entries, "async_update_entry",
                          wraps=hass.config_entries.async_update_entry) as upd:
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                user_input=_opts(**{CONF_BATTERY_KWH: 22.0, CONF_BATTERY_KW: 5.0,
                                    CONF_EFF: 0.9, CONF_INVERT_BATTERY: invert}),
            )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    return default, upd, mock_coordinator


@pytest.mark.asyncio
async def test_options_invert_on_writes_the_whole_history(hass: HomeAssistant) -> None:
    from custom_components.wolta.const import CONF_INVERT_INTERVALS
    entry = _make_mock_entry(hass, {CONF_INVERT_INTERVALS: [], CONF_INVERT_BATTERY: False})
    default, upd, coordinator = await _submit_invert(hass, entry, True)
    assert default is False
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_INVERT_INTERVALS] == [[None, None]]
    assert updated.data[CONF_INVERT_BATTERY] is True
    coordinator.async_request_refresh.assert_awaited()


@pytest.mark.asyncio
async def test_options_invert_off_clears_every_interval(hass: HomeAssistant) -> None:
    """A partial flip (from a repair) shows as on; turning it off restores [] everywhere."""
    from custom_components.wolta.const import CONF_INVERT_INTERVALS
    entry = _make_mock_entry(hass, {
        CONF_INVERT_INTERVALS: [["2026-09-28T13:00:00+00:00", None]],
        CONF_INVERT_BATTERY: False})
    default, _upd, _c = await _submit_invert(hass, entry, False)
    assert default is True
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_INVERT_INTERVALS] == []
    assert updated.data[CONF_INVERT_BATTERY] is False


@pytest.mark.asyncio
async def test_options_invert_unchanged_writes_nothing(hass: HomeAssistant) -> None:
    """Left on over a partial flip: no write, the partial intervals stay."""
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT, CONF_INVERT_INTERVALS
    partial = [["2026-09-28T13:00:00+00:00", None]]
    entry = _make_mock_entry(hass, {CONF_INVERT_INTERVALS: partial, CONF_INVERT_BATTERY: False})
    _default, upd, coordinator = await _submit_invert(hass, entry, True)
    # (Finishing an options flow writes the options; entry.data is not written.)
    assert all("data" not in c.kwargs for c in upd.call_args_list)
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_INVERT_INTERVALS] == partial
    assert CONF_DIRECTION_FLIPPED_AT not in updated.data
    coordinator.async_request_refresh.assert_not_awaited()


# ---------------------------------------------------------------------------
# Reauth på en kontoägd anläggning (Wolta säkerhetsgranskning 2026-10-16, P2 D7)
# ---------------------------------------------------------------------------

_OWNED_PLANT_ID = "c" * 32


def _owned_entry(hass: HomeAssistant):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    data: dict[str, Any] = {CONF_TOKEN: "dead-token", CONF_ZONE: ZONE,
                            CONF_BATTERY_KWH: 22.0, CONF_BATTERY_KW: 5.0,
                            CONF_PLANT_ID: _OWNED_PLANT_ID}
    entry = MockConfigEntry(domain=DOMAIN, data=data, source=config_entries.SOURCE_USER,
                            unique_id="uid-owned")
    entry.add_to_hass(hass)
    return entry, data


async def _reauth_to_new_token_step(hass, mock_client):
    from custom_components.wolta.api import WoltaPlantClaimed
    entry, data = _owned_entry(hass)
    mock_client.create_profile = AsyncMock(side_effect=WoltaPlantClaimed("plant_owned"))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=data)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    return entry, result


@pytest.mark.asyncio
async def test_reauth_owned_plant_asks_for_new_token(hass: HomeAssistant) -> None:
    """Backend ger inte längre en ny token till en kontoägd anläggning bara för att HA
    visar sitt id. Reauth ska då be om en token som ägaren skapat på webben - inte visa
    "cannot_connect" och fastna."""
    mock_client = _mock_client()
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": False, "plant_id_set": False})
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        entry, result = await _reauth_to_new_token_step(hass, mock_client)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_new_token"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": "https://wolta.se/anlaggning#profile=NYTOKEN"})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    # Samma identitet stämplas: adopt är idempotent för samma rad och 409:ar om tokenen
    # tillhör en ANNAN anläggning - det är kontrollen att rätt token klistrats in.
    mock_client.adopt_profile.assert_awaited_once_with("NYTOKEN", client_plant_id=_OWNED_PLANT_ID)
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_TOKEN] == "NYTOKEN"


@pytest.mark.asyncio
async def test_reauth_new_token_of_another_plant_is_rejected(hass: HomeAssistant) -> None:
    from custom_components.wolta.api import WoltaApiError
    mock_client = _mock_client()
    mock_client.adopt_profile = AsyncMock(side_effect=WoltaApiError("409", status=409))
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        entry, result = await _reauth_to_new_token_step(hass, mock_client)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": "ANNANTOKEN"})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"profile_input": "token_other_plant"}
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_TOKEN] == "dead-token"


@pytest.mark.asyncio
async def test_reauth_new_token_invalid(hass: HomeAssistant) -> None:
    from custom_components.wolta.api import WoltaAuthError
    mock_client = _mock_client()
    mock_client.adopt_profile = AsyncMock(side_effect=WoltaAuthError("404"))
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        _, result = await _reauth_to_new_token_step(hass, mock_client)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": "FEL"})

    assert result["errors"] == {"profile_input": "invalid_token"}


@pytest.mark.asyncio
async def test_reauth_new_token_rejects_read_link(hass: HomeAssistant) -> None:
    mock_client = _mock_client()
    mock_client.adopt_profile = AsyncMock()
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        _, result = await _reauth_to_new_token_step(hass, mock_client)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": "https://wolta.se/anlaggning#link=wpl_zzz"})

    assert result["errors"] == {"profile_input": "link_is_read_link"}
    mock_client.adopt_profile.assert_not_called()


@pytest.mark.asyncio
async def test_reconfigure_patch_failure_keeps_a_cleared_picker_cleared(hass: HomeAssistant) -> None:
    """HA leaves a cleared Optional picker out of user_input. After a failed soc_absent
    PATCH the re-shown form must keep it cleared, or the retry silently saves the stored
    sensor back (code review 2026-10-21)."""
    from custom_components.wolta.api import WoltaApiError

    # External control uses suggested_value (no default=), so clearing it really omits
    # the key; the solar field has default= and cannot be cleared in the form at all.
    entry = _entry_without_answer(hass, **{CONF_EXTERNAL_CONTROL: "binary_sensor.old_flex"})
    mock_client = _mock_client()
    mock_client.patch_profile = AsyncMock(side_effect=WoltaApiError("boom"))
    streams = _reconfigure_streams(**{CONF_SOC_ABSENT: True})
    streams.pop(CONF_EXTERNAL_CONTROL, None)
    result = await _reconfigure(hass, entry, mock_client, streams)
    assert result["errors"] == {"base": "cannot_connect"}
    marker = _schema_marker(result, CONF_EXTERNAL_CONTROL)
    assert not (marker.description or {}).get("suggested_value")


@pytest.mark.asyncio
async def test_link_flow_box_starts_on_the_servers_answer(hass: HomeAssistant) -> None:
    """An owner who already answered "no SoC sensor" on wolta.se sees the box ticked when
    linking, instead of being asked again (code review 2026-10-21)."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value={**LINK_PROFILE, CONF_SOC_ABSENT: True})
    mock_client.adopt_profile = AsyncMock(return_value={"adopted": True})

    with _patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "link"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"profile_input": LINK_TOKEN})

    assert result["step_id"] == "entities"
    assert _schema_marker(result, CONF_SOC_ABSENT).default() is True


# ---------------------------------------------------------------------------
# Inköpspris (HACS 0.53.0): reduced energy tax choice + fixed grid fee required with a
# percentage. The server owns both rules; the form mirrors them so the owner gets a
# readable error at the form instead of a bare cannot_connect.
# ---------------------------------------------------------------------------

# What WoltaApiError carries for the server's 422: one line, the JSON body escaped as the
# server sends it (api.py _request).
_GRID_FIXED_DETAIL = (
    'HTTP 422 from https://wolta.se/api/v1/profile: {"detail":"grid_var_pct kr\\u00e4ver '
    'grid_var_ore (n\\u00e4tavgiftens fasta del, 0 om du bara har procent)"}')


def _tariff_field(result, field: str):
    """(marker, selector) for a field in the tariffs section of the settings form."""
    outer = result["data_schema"].schema
    sec_key = next(k for k in outer
                   if (k.schema if hasattr(k, "schema") else str(k)) == "tariffs")
    inner = outer[sec_key].schema.schema
    marker = next(k for k in inner
                  if (k.schema if hasattr(k, "schema") else str(k)) == field)
    return marker, inner[marker]


def _section_options(result, name: str) -> dict:
    """The `section(...)` options (e.g. collapsed) of a section in the settings form."""
    outer = result["data_schema"].schema
    sec_key = next(k for k in outer
                   if (k.schema if hasattr(k, "schema") else str(k)) == name)
    return outer[sec_key].options


def _tariff_fields(result) -> list[str]:
    outer = result["data_schema"].schema
    sec_key = next(k for k in outer
                   if (k.schema if hasattr(k, "schema") else str(k)) == "tariffs")
    return [k.schema if hasattr(k, "schema") else str(k)
            for k in outer[sec_key].schema.schema]


async def _settings_session(hass, entry):
    """Open Configure → settings (the client is patched by the caller)."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "settings"})


def _core(**extra) -> dict:
    return {CONF_BATTERY_KWH: 22.0, CONF_BATTERY_KW: 5.0, CONF_EFF: 0.9, **extra}


@contextlib.contextmanager
def _options_patched(mock_client):
    with (
        patch("custom_components.wolta.config_flow.WoltaApiClient", return_value=mock_client),
        patch("custom_components.wolta.config_flow.async_get_clientsession"),
    ):
        yield


@pytest.mark.asyncio
async def test_options_tax_select_has_three_choices_default_not_set(hass: HomeAssistant) -> None:
    """A boolean cannot carry "not answered" (the form always saves true or false), so the
    choice is a list select with three values, and a server without the field starts on
    not_set."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    assert mock_client.get_profile.return_value.get(CONF_REDUCED_ENERGY_TAX) is None
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    marker, selector = _tariff_field(result, CONF_REDUCED_ENERGY_TAX)
    assert marker.default() == "not_set"
    cfg = selector.config
    assert [o if isinstance(o, str) else o["value"] for o in cfg["options"]] == [
        "not_set", "no", "yes"]
    assert cfg["mode"] == "list"
    assert cfg["translation_key"] == "reduced_energy_tax"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored, shown", [(True, "yes"), (False, "no"), (None, "not_set")])
async def test_options_tax_select_starts_on_the_servers_answer(
    hass: HomeAssistant, stored, shown
) -> None:
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(
        return_value=_server_profile(entry, **{CONF_REDUCED_ENERGY_TAX: stored}))
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    marker, _ = _tariff_field(result, CONF_REDUCED_ENERGY_TAX)
    assert marker.default() == shown


@pytest.mark.asyncio
@pytest.mark.parametrize("choice, sent", [("yes", True), ("no", False)])
async def test_options_tax_answer_is_patched_and_mirrored(
    hass: HomeAssistant, choice, sent
) -> None:
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_REDUCED_ENERGY_TAX: choice})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    assert mock_client.patch_profile.call_args.kwargs == {CONF_REDUCED_ENERGY_TAX: sent}
    stored = hass.config_entries.async_get_entry(entry.entry_id).data[CONF_REDUCED_ENERGY_TAX]
    assert stored is sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields, refreshed",
    [
        ({CONF_REDUCED_ENERGY_TAX: "yes"}, True),
        ({CONF_SURCHARGE_ORE: 11.5}, True),
        ({CONF_COST_SEK: 99000.0}, False),
    ],
    ids=["energy_tax", "surcharge", "plant_field"],
)
async def test_options_tariff_change_refreshes_the_purchase_price(
    hass: HomeAssistant, fields, refreshed
) -> None:
    """The purchase price follows the owner's tariff: a saved tariff change must not wait
    for the next 15-minute price poll. A change that does not touch the tariff leaves the
    price coordinator alone."""
    entry = _make_mock_entry(hass)
    mock_client, mock_coordinator = _mock_options_env(entry)
    mock_coordinator.prices = MagicMock()
    mock_coordinator.prices.async_request_refresh = AsyncMock()
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**fields)))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    # The refresh runs as a background task: the save does not wait for it.
    await hass.async_block_till_done(wait_background_tasks=True)
    if refreshed:
        mock_coordinator.prices.async_request_refresh.assert_awaited_once()
        # The kept answer was priced with the old tariff: it is dropped before the refresh.
        names = [c[0] for c in mock_coordinator.prices.mock_calls]
        assert names == ["async_forget_purchase", "async_request_refresh"]
    else:
        mock_coordinator.prices.async_request_refresh.assert_not_awaited()
        mock_coordinator.prices.async_forget_purchase.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {CONF_GRID_BENEFIT_ORE: 11.0},
        {CONF_EXPORT_EXTRA_ORE: 6.0},
        {CONF_EXPORT_EXTRA_PCT: 3.0},
    ],
    ids=["benefit", "retailer_terms", "benefit_pct"],
)
async def test_options_export_side_change_refreshes_the_purchase_price(
    hass: HomeAssistant, fields
) -> None:
    """The export-side tariff fields (grid benefit, retailer's terms) are part of the tariff
    the kept /profile/prices answer was priced with: saving one drops that answer and fetches
    a fresh one, exactly as for the import-side fields (docs/configuration.md promises both)."""
    entry = _make_mock_entry(
        hass, extra_data={CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: 5.0})
    mock_client, mock_coordinator = _mock_options_env(entry)
    mock_coordinator.prices = MagicMock()
    mock_coordinator.prices.async_request_refresh = AsyncMock()
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{
                CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: 5.0, **fields})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    names = [c[0] for c in mock_coordinator.prices.mock_calls]
    assert names == ["async_forget_purchase", "async_request_refresh"]


@pytest.mark.asyncio
async def test_options_save_does_not_wait_for_the_price_refresh(hass: HomeAssistant) -> None:
    """The refresh fetches /profile/prices (up to the lookup timeout): a slow or hanging
    price endpoint must not hold the Configure dialog open."""
    entry = _make_mock_entry(hass)
    mock_client, mock_coordinator = _mock_options_env(entry)
    release = asyncio.Event()

    async def _slow_refresh() -> None:
        await release.wait()

    mock_coordinator.prices = MagicMock()
    mock_coordinator.prices.async_request_refresh = AsyncMock(side_effect=_slow_refresh)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await asyncio.wait_for(
            hass.config_entries.options.async_configure(
                result["flow_id"],
                user_input=_opts(**_core(**{CONF_SURCHARGE_ORE: 11.5}))),
            timeout=5)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_coordinator.prices.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_price_refresh_task_belongs_to_the_entry(hass: HomeAssistant) -> None:
    """The scheduled refresh is an entry background task, so unloading the entry cancels it."""
    entry = _make_mock_entry(hass)
    mock_client, mock_coordinator = _mock_options_env(entry)
    release = asyncio.Event()
    started = asyncio.Event()

    async def _slow_refresh() -> None:
        started.set()
        await release.wait()

    mock_coordinator.prices = MagicMock()
    mock_coordinator.prices.async_request_refresh = AsyncMock(side_effect=_slow_refresh)
    with (
        _options_patched(mock_client),
        patch.object(
            entry, "async_create_background_task", wraps=entry.async_create_background_task
        ) as spawn,
    ):
        result = await _settings_session(hass, entry)
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_SURCHARGE_ORE: 11.5})))
        await asyncio.wait_for(started.wait(), timeout=5)
    spawn.assert_called_once()
    assert spawn.call_args.args[0] is hass
    assert spawn.call_args.args[2] == "wolta_options_price_refresh"
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.asyncio
async def test_options_price_refresh_error_is_swallowed(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _make_mock_entry(hass)
    mock_client, mock_coordinator = _mock_options_env(entry)
    mock_coordinator.prices = MagicMock()
    mock_coordinator.prices.async_request_refresh = AsyncMock(side_effect=RuntimeError("boom"))
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_SURCHARGE_ORE: 11.5})))
        await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_coordinator.prices.async_request_refresh.assert_awaited_once()
    assert not [r for r in caplog.records
                if r.name.startswith("custom_components.wolta") and r.levelname == "ERROR"]
    assert "Task exception was never retrieved" not in caplog.text


@pytest.mark.asyncio
async def test_options_tax_back_to_not_set_removes_the_answer(hass: HomeAssistant) -> None:
    """PATCH null removes the server's answer; the cache drops the key the same way."""
    entry = _make_mock_entry(hass, extra_data={CONF_REDUCED_ENERGY_TAX: True})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_REDUCED_ENERGY_TAX: "not_set"})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_REDUCED_ENERGY_TAX: None}
    assert CONF_REDUCED_ENERGY_TAX not in hass.config_entries.async_get_entry(entry.entry_id).data


@pytest.mark.asyncio
async def test_options_tax_untouched_is_not_patched(hass: HomeAssistant) -> None:
    entry = _make_mock_entry(hass, extra_data={CONF_REDUCED_ENERGY_TAX: False})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        # The key is left out on purpose: HA fills the form default (the server's "no").
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core()))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["SE4", "NO1", "DE-LU"])
async def test_options_tax_hidden_outside_se1_se3(hass: HomeAssistant, zone) -> None:
    """No affected municipality lies in SE4 and the rule is Swedish: the web hides the choice
    there too. A stored answer survives an unrelated save (it is not cleared by absence)."""
    entry = _make_mock_entry(hass, extra_data={CONF_ZONE: zone, CONF_REDUCED_ENERGY_TAX: True})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        assert CONF_REDUCED_ENERGY_TAX not in _tariff_fields(result)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_BATTERY_KWH: 25.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_BATTERY_KWH: 25.0}
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_REDUCED_ENERGY_TAX] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["SE1", "SE2", "SE3"])
async def test_options_tax_shown_in_se1_se3(hass: HomeAssistant, zone) -> None:
    entry = _make_mock_entry(hass, extra_data={CONF_ZONE: zone})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    assert CONF_REDUCED_ENERGY_TAX in _tariff_fields(result)


@pytest.mark.asyncio
async def test_options_percent_without_fixed_part_shows_the_form_again(
    hass: HomeAssistant,
) -> None:
    """The server refuses a percentage without the fixed part (422). The form says so
    first and sends nothing."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_GRID_VAR_PCT: 5.6})))
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "settings"
    assert result["errors"] == {"base": "grid_fixed_required"}
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_fixed_part_error_opens_the_tariffs_section(hass: HomeAssistant) -> None:
    """The base error renders at the top; the section holding the field must be open."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        assert _section_options(result, "tariffs")["collapsed"] is True
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_GRID_VAR_PCT: 5.6})))
    assert result["errors"] == {"base": "grid_fixed_required"}
    assert _section_options(result, "tariffs")["collapsed"] is False


@pytest.mark.asyncio
async def test_options_error_form_keeps_what_the_user_typed(hass: HomeAssistant) -> None:
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_VAR_PCT: 5.6, CONF_SURCHARGE_ORE: 12.5})))
    assert result["errors"] == {"base": "grid_fixed_required"}
    for field, expected in ((CONF_GRID_VAR_PCT, 5.6), (CONF_SURCHARGE_ORE, 12.5)):
        marker, _ = _tariff_field(result, field)
        assert (marker.description or {}).get("suggested_value") == expected


@pytest.mark.asyncio
async def test_options_cleared_field_stays_empty_on_the_error_form(hass: HomeAssistant) -> None:
    """A cleared field is absent from user_input. The re-shown form must not fall back to
    the stored value for it, or resubmitting would silently save the old value."""
    entry = _make_mock_entry(
        hass, extra_data={CONF_GRID_VAR_ORE: 20.0, CONF_GRID_VAR_PCT: 5.6,
                          CONF_SURCHARGE_ORE: 7.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        marker, _ = _tariff_field(result, CONF_GRID_VAR_ORE)
        assert (marker.description or {}).get("suggested_value") == 20.0  # first show: stored
        # The fixed part is cleared (absent); the percentage and the markup are kept.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_VAR_PCT: 5.6, CONF_SURCHARGE_ORE: 7.0})))
    assert result["errors"] == {"base": "grid_fixed_required"}
    marker, _ = _tariff_field(result, CONF_GRID_VAR_ORE)
    assert (marker.description or {}).get("suggested_value") is None
    for field, expected in ((CONF_GRID_VAR_PCT, 5.6), (CONF_SURCHARGE_ORE, 7.0)):
        marker, _ = _tariff_field(result, field)
        assert (marker.description or {}).get("suggested_value") == expected


@pytest.mark.asyncio
async def test_options_cleared_economy_fields_stay_empty_on_the_error_form(
    hass: HomeAssistant,
) -> None:
    """The same for the other sections: a cleared cost does not come back from the snapshot."""
    entry = _make_mock_entry(hass, extra_data={CONF_COST_SEK: 99000.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_VAR_PCT: 5.6})))  # cost cleared, error
    assert result["errors"] == {"base": "grid_fixed_required"}
    outer = result["data_schema"].schema
    econ = next(k for k in outer if (k.schema if hasattr(k, "schema") else str(k)) == "economy")
    marker = next(k for k in outer[econ].schema.schema if k.schema == CONF_COST_SEK)
    assert (marker.description or {}).get("suggested_value") is None


@pytest.mark.asyncio
async def test_options_clearing_the_fixed_part_under_a_percentage_is_refused(
    hass: HomeAssistant,
) -> None:
    entry = _make_mock_entry(
        hass, extra_data={CONF_GRID_VAR_ORE: 20.0, CONF_GRID_VAR_PCT: 5.6})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_GRID_VAR_PCT: 5.6})))
    assert result["errors"] == {"base": "grid_fixed_required"}
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_percent_with_fixed_part_zero_is_accepted(hass: HomeAssistant) -> None:
    """0 is a valid fixed part ("0 if you only have a percentage") and must not read as empty."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_VAR_ORE: 0.0, CONF_GRID_VAR_PCT: 5.6})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {
        CONF_GRID_VAR_ORE: 0.0, CONF_GRID_VAR_PCT: 5.6}


@pytest.mark.asyncio
async def test_options_legacy_percent_only_profile_can_still_save_other_fields(
    hass: HomeAssistant,
) -> None:
    """Review focus 5. An installation that only has a percentage (from before the
    requirement) opens Configure and saves without touching the tariff: no crash, no silent
    failure, and the server is not asked about the tariff at all (it only checks a PATCH
    that touches grid_var_ore or grid_var_pct), so an unrelated edit is never locked out.
    The repair grid_fixed_missing is what tells that owner to fill the fixed part in."""
    entry = _make_mock_entry(hass, extra_data={CONF_GRID_VAR_PCT: 5.6})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_BATTERY_KWH: 25.0, CONF_GRID_VAR_PCT: 5.6})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_BATTERY_KWH: 25.0}


@pytest.mark.asyncio
async def test_options_server_422_for_the_fixed_part_gives_the_same_error(
    hass: HomeAssistant,
) -> None:
    """If the form's own check is ever out of step with the server (older cache, a rule that
    moves), the server's 422 for this reason reads as the same field error, not as a
    connection problem."""
    from custom_components.wolta.api import WoltaApiError

    entry = _make_mock_entry(hass, extra_data={CONF_GRID_VAR_ORE: 20.0})
    mock_client, _ = _mock_options_env(entry)
    mock_client.patch_profile = AsyncMock(
        side_effect=WoltaApiError(_GRID_FIXED_DETAIL, status=422))
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_VAR_ORE: 30.0})))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "grid_fixed_required"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail, matched",
    [
        (_GRID_FIXED_DETAIL, True),  # unicode-escaped, as api.py reduces the body
        ('HTTP 422 from x: {"detail":"grid_var_pct kräver grid_var_ore (nätavgiftens fasta del)"}',
         True),  # plain letters
        ('HTTP 422 from x: {"detail":"grid_var_pct and grid_var_ore must be numbers"}', False),
        ('HTTP 422 from x: {"detail":"grid_var_ore kräver grid_var_pct"}', False),
        ('HTTP 422 from x: {"detail":"grid_var_pct kräver something_else"}', False),
    ],
    ids=["escaped", "plain", "other-422-both-fields", "reversed", "other-requirement"],
)
def test_is_grid_fixed_error_needs_the_rules_own_wording(detail, matched) -> None:
    from custom_components.wolta.api import WoltaApiError
    from custom_components.wolta.config_flow import _is_grid_fixed_error

    assert _is_grid_fixed_error(WoltaApiError(detail, status=422)) is matched
    assert _is_grid_fixed_error(WoltaApiError(detail, status=500)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message, shown",
    [
        ('HTTP 422 from x: {"detail":"n\\u00e5got annat"}', "något annat"),
        ('HTTP 422 from x: {"detail":"något annat"}', "något annat"),
        # Not the server's JSON (a proxy page, a body cut at 200 characters): the body as is.
        ("HTTP 422 from x: something else", "something else"),
    ],
    ids=["escaped", "plain", "not-json"],
)
async def test_options_other_422_shows_the_servers_reason(
    hass: HomeAssistant, message: str, shown: str
) -> None:
    """A refusal the form has no rule for is the server's answer, not a connection problem:
    an owner told to check the internet connection checks the wrong thing (2026-10-29)."""
    from custom_components.wolta.api import WoltaApiError

    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.patch_profile = AsyncMock(side_effect=WoltaApiError(message, status=422))
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_VAR_ORE: 30.0})))
    assert result["errors"] == {"base": "server_rejected"}
    assert result["description_placeholders"]["server_detail"] == shown


@pytest.mark.asyncio
async def test_options_network_error_is_still_cannot_connect(hass: HomeAssistant) -> None:
    from custom_components.wolta.api import WoltaApiError

    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.patch_profile = AsyncMock(side_effect=WoltaApiError("Network error: timeout"))
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_VAR_ORE: 30.0})))
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_options_server_422_for_the_purchase_date_shows_at_the_field_and_on_base(
    hass: HomeAssistant,
) -> None:
    """A plant with stages (solar 2020, battery bought later) where the owner types the
    battery's date as the purchase date: the server refuses a date after the first stage. The
    form says so at the field, where the date was typed, and on base (the field sits in a
    section)."""
    from custom_components.wolta.api import WoltaApiError

    entry = _make_mock_entry(hass, extra_data={CONF_PURCHASE_DATE: "2020-09-30"})
    mock_client, _ = _mock_options_env(entry)
    mock_client.patch_profile = AsyncMock(
        side_effect=WoltaApiError(_PURCHASE_DATE_DETAIL, status=422))
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_PURCHASE_DATE: "2026-10-02"})))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_PURCHASE_DATE: "purchase_date_after_stages",
                                "base": "purchase_date_after_stages"}


# Reauth: the stored answer travels with the re-created profile, and a 422 for the
# fixed-part rule is explained instead of surfacing as a bare invalid_input.

_REAUTH_BASE = {
    CONF_TOKEN: "old-token", CONF_ZONE: ZONE,
    CONF_BATT_IN: ["sensor.battery_charge"], CONF_BATT_OUT: ["sensor.battery_discharge"],
    CONF_GRID_IN: ["sensor.grid_import"], CONF_GRID_OUT: ["sensor.grid_export"],
    CONF_BATTERY_KWH: 22.0, CONF_BATTERY_KW: 5.0, CONF_EFF: 0.9,
}


async def _start_reauth(hass, data, mock_client):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, title="Wolta (SE3)", data=data,
        source=config_entries.SOURCE_USER, unique_id="reauth-tax")
    entry.add_to_hass(hass)
    with _options_patched(mock_client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_REAUTH,
                             "entry_id": entry.entry_id}, data=data)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={})
    return entry, result


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [True, False])
async def test_reauth_sends_the_reduced_energy_tax_answer(hass: HomeAssistant, stored) -> None:
    mock_client = _mock_client("new-token")
    await _start_reauth(hass, {**_REAUTH_BASE, CONF_REDUCED_ENERGY_TAX: stored}, mock_client)
    assert mock_client.create_profile.call_args.kwargs["reduced_energy_tax"] is stored


@pytest.mark.asyncio
async def test_reauth_outside_se1_se3_does_not_send_a_cached_tax_answer(
    hass: HomeAssistant,
) -> None:
    """A `true` cached from before a zone correction would be refused by the server (422)
    in SE4, so the reauth would never get through."""
    mock_client = _mock_client("new-token")
    await _start_reauth(
        hass, {**_REAUTH_BASE, CONF_ZONE: "SE4", CONF_REDUCED_ENERGY_TAX: True}, mock_client)
    assert mock_client.create_profile.call_args.kwargs["reduced_energy_tax"] is None


@pytest.mark.asyncio
async def test_reauth_without_an_answer_sends_none(hass: HomeAssistant) -> None:
    mock_client = _mock_client("new-token")
    await _start_reauth(hass, dict(_REAUTH_BASE), mock_client)
    assert mock_client.create_profile.call_args.kwargs["reduced_energy_tax"] is None


@pytest.mark.asyncio
async def test_reauth_422_for_the_fixed_part_asks_for_it(hass: HomeAssistant) -> None:
    """The old profile is gone, so Configure cannot open (it reads the profile first): the
    reauth form itself has to ask for the missing fixed part, or the owner is stuck."""
    from custom_components.wolta.api import WoltaApiError

    mock_client = _mock_client("new-token")
    mock_client.create_profile = AsyncMock(
        side_effect=[WoltaApiError(_GRID_FIXED_DETAIL, status=422), "new-token"])
    entry, result = await _start_reauth(
        hass, {**_REAUTH_BASE, CONF_GRID_VAR_PCT: 5.6}, mock_client)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "grid_fixed_required"}
    assert [k.schema for k in result["data_schema"].schema] == [CONF_GRID_VAR_ORE]

    with _options_patched(mock_client):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_GRID_VAR_ORE: 0.0})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_client.create_profile.call_args.kwargs["grid_var_ore"] == 0.0
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_GRID_VAR_ORE] == 0.0
    assert updated.data[CONF_TOKEN] == "new-token"


@pytest.mark.asyncio
async def test_reauth_other_422_stays_invalid_input(hass: HomeAssistant) -> None:
    from custom_components.wolta.api import WoltaApiError

    mock_client = _mock_client("new-token")
    mock_client.create_profile = AsyncMock(
        side_effect=WoltaApiError("HTTP 422 from x: zone", status=422))
    _, result = await _start_reauth(hass, dict(_REAUTH_BASE), mock_client)
    assert result["errors"] == {"base": "invalid_input"}
    assert list(result["data_schema"].schema) == []


@pytest.mark.asyncio
async def test_link_flow_mirrors_the_reduced_energy_tax_answer(hass: HomeAssistant) -> None:
    """A linked profile that already has the answer on wolta.se starts with it cached, like
    the other tariff fields (the coordinator's profile sync keeps it fresh)."""
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(
        return_value={**LINK_PROFILE, CONF_REDUCED_ENERGY_TAX: False})
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

    assert result["data"][CONF_REDUCED_ENERGY_TAX] is False


# ---------------------------------------------------------------------------
# Försäljningspris (HACS 0.54.0, server api 0.119.0): the owner's grid benefit has a field of
# its own, export_extra_ore becomes the retailer's terms, and the server's two new 422s are
# explained AT THE FIELD (never cannot_connect).
# ---------------------------------------------------------------------------

# What WoltaApiError carries for each of the server's 422s (one line, JSON escaped as sent).
_BENEFIT_FIXED_DETAIL = (
    'HTTP 422 from https://wolta.se/api/v1/profile: {"detail":"export_extra_pct kr\\u00e4ver '
    'grid_benefit_ore (n\\u00e4tnyttans fasta del, 0 om du bara har procent)"}')
_BENEFIT_ZONE_DETAIL = (
    'HTTP 422 from https://wolta.se/api/v1/profile: {"detail":"grid_benefit_ore '
    'g\\u00e4ller bara SE1\\u2013SE4"}')


@pytest.mark.asyncio
async def test_options_tariffs_section_orders_the_benefit_fields_before_the_retailer_terms(
    hass: HomeAssistant,
) -> None:
    """Grid benefit, retailer terms, grid benefit % of spot."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    fields = _tariff_fields(result)
    order = [CONF_GRID_BENEFIT_ORE, CONF_EXPORT_EXTRA_ORE, CONF_EXPORT_EXTRA_PCT]
    assert all(f in fields for f in order)
    assert [fields.index(f) for f in order] == sorted(fields.index(f) for f in order)
    _, sel = _tariff_field(result, CONF_GRID_BENEFIT_ORE)
    assert (sel.config["min"], sel.config["max"]) == (-20.0, 50.0)


@pytest.mark.asyncio
async def test_options_benefit_fields_are_hidden_outside_se1_se4(hass: HomeAssistant) -> None:
    """The server refuses them outside SE1-SE4 (422), so the form does not offer them, and a
    save there leaves a cached value alone instead of PATCHing null over it."""
    entry = _make_mock_entry(
        hass, extra_data={CONF_ZONE: "FI", CONF_GRID_BENEFIT_ORE: 10.4})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        fields = _tariff_fields(result)
        assert CONF_GRID_BENEFIT_ORE not in fields
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core()))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_grid_benefit_patches_both_benefit_and_retailer_terms(
    hass: HomeAssistant,
) -> None:
    """The owner who enters the grid benefit sees and confirms the retailer value: both go."""
    entry = _make_mock_entry(hass, extra_data={CONF_EXPORT_EXTRA_ORE: 5.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: 5.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {
        CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: 5.0}
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_GRID_BENEFIT_ORE] == 10.4
    assert updated.data[CONF_EXPORT_EXTRA_ORE] == 5.0


@pytest.mark.asyncio
async def test_options_changed_retailer_terms_send_the_unchanged_benefit_too(
    hass: HomeAssistant,
) -> None:
    entry = _make_mock_entry(
        hass, extra_data={CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: 5.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: -3.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {
        CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: -3.0}


@pytest.mark.asyncio
async def test_options_benefit_without_retailer_terms_confirms_zero(hass: HomeAssistant) -> None:
    """An empty retailer field next to a grid benefit is answered 0 (the retailer pays pure
    spot): the server counts a missing value as 0 anyway, and the stored value is then one the
    owner has seen."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_GRID_BENEFIT_ORE: 0.0})))
    assert mock_client.patch_profile.call_args.kwargs == {
        CONF_GRID_BENEFIT_ORE: 0.0, CONF_EXPORT_EXTRA_ORE: 0.0}


@pytest.mark.asyncio
async def test_options_untouched_benefit_and_terms_patch_nothing(hass: HomeAssistant) -> None:
    entry = _make_mock_entry(
        hass, extra_data={CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: 5.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: 5.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail, code",
    [
        (_BENEFIT_FIXED_DETAIL, "grid_benefit_fixed_required"),
        (_BENEFIT_ZONE_DETAIL, "grid_benefit_zone"),
    ],
    ids=["percent-needs-fixed", "zone"],
)
async def test_options_server_422_for_the_benefit_rules_shows_at_the_field(
    hass: HomeAssistant, detail: str, code: str,
) -> None:
    """Never cannot_connect: the specific code is on grid_benefit_ore, the field that is missing
    (or, for the zone rule, the one that is refused), AND on base, like battery_pair_incomplete,
    because the field sits inside a section and the frontend may not render field errors there.
    The section opens."""
    from custom_components.wolta.api import WoltaApiError

    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    mock_client.patch_profile = AsyncMock(side_effect=WoltaApiError(detail, status=422))
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_GRID_BENEFIT_ORE: 10.4})))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_GRID_BENEFIT_ORE: code, "base": code}
    assert _section_options(result, "tariffs")["collapsed"] is False


@pytest.mark.asyncio
async def test_options_export_percent_without_benefit_fixed_part_shows_the_error_first(
    hass: HomeAssistant,
) -> None:
    """The server's rule, said before sending: a non-zero percentage needs the fixed part
    (0 if there is none). Nothing is sent."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_EXPORT_EXTRA_PCT: -2.8})))
    assert result["errors"] == {
        CONF_GRID_BENEFIT_ORE: "grid_benefit_fixed_required", "base": "grid_benefit_fixed_required"}
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_export_percent_zero_needs_no_benefit(hass: HomeAssistant) -> None:
    """The server flags and refuses only a percentage that is not 0."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_EXPORT_EXTRA_PCT: 0.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_EXPORT_EXTRA_PCT: 0.0}


@pytest.mark.asyncio
async def test_options_legacy_export_percent_profile_can_still_save_other_fields(
    hass: HomeAssistant,
) -> None:
    """Mirror of the import-side rule: the server checks a PATCH only when it touches the
    percentage or the benefit, so an unrelated save is never locked out by a legacy row."""
    entry = _make_mock_entry(hass, extra_data={CONF_EXPORT_EXTRA_PCT: -2.8})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_BATTERY_KWH: 25.0, CONF_EXPORT_EXTRA_PCT: -2.8})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_BATTERY_KWH: 25.0}


@pytest.mark.parametrize(
    "detail, code",
    [
        (_BENEFIT_FIXED_DETAIL, "grid_benefit_fixed_required"),
        (_BENEFIT_ZONE_DETAIL, "grid_benefit_zone"),
        # Plain letters, and the former wording that named the never-released high-load field:
        # not this rule any more.
        ('HTTP 422 from x: {"detail":"grid_benefit_ore och grid_benefit_peak_ore gäller bara '
         'SE1–SE4"}', None),
        ('HTTP 422 from x: {"detail":"export_extra_pct kräver grid_benefit_ore (nätnyttans fasta del)"}',
         "grid_benefit_fixed_required"),  # plain letters
        ('HTTP 422 from x: {"detail":"export_extra_pct and grid_benefit_ore must be numbers"}', None),
        ('HTTP 422 from x: {"detail":"grid_benefit_ore kräver export_extra_pct"}', None),
        ('HTTP 422 from x: {"detail":"grid_var_pct kräver grid_var_ore (nätavgiftens fasta del)"}',
         None),
    ],
    ids=["fixed", "zone", "old-zone-wording", "plain", "other-422-both-fields", "reversed",
         "import-side-rule"],
)
def test_benefit_error_code_needs_the_rules_own_wording(detail, code) -> None:
    from custom_components.wolta.api import WoltaApiError
    from custom_components.wolta.config_flow import _grid_benefit_error

    assert _grid_benefit_error(WoltaApiError(detail, status=422)) == code
    assert _grid_benefit_error(WoltaApiError(detail, status=500)) is None


@pytest.mark.asyncio
async def test_reauth_sends_the_cached_grid_benefit(hass: HomeAssistant) -> None:
    """A new row after a purged profile takes the tariff from this call (a surviving row keeps
    the server's), so the benefit and the retailer terms travel with it."""
    mock_client = _mock_client("new-token")
    await _start_reauth(
        hass,
        {**_REAUTH_BASE, CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: 5.0},
        mock_client)
    kwargs = mock_client.create_profile.call_args.kwargs
    assert kwargs["grid_benefit_ore"] == 10.4
    assert kwargs["export_extra_ore"] == 5.0


@pytest.mark.asyncio
async def test_reauth_without_a_benefit_sends_none(hass: HomeAssistant) -> None:
    mock_client = _mock_client("new-token")
    await _start_reauth(hass, dict(_REAUTH_BASE), mock_client)
    kwargs = mock_client.create_profile.call_args.kwargs
    assert kwargs["grid_benefit_ore"] is None


@pytest.mark.asyncio
async def test_reauth_outside_se1_se4_does_not_send_a_cached_benefit(hass: HomeAssistant) -> None:
    """The server refuses the benefit fields outside SE1-SE4, which would block the reauth
    for a cache that outlived a zone correction."""
    mock_client = _mock_client("new-token")
    await _start_reauth(
        hass, {**_REAUTH_BASE, CONF_ZONE: "FI", CONF_GRID_BENEFIT_ORE: 10.4}, mock_client)
    assert mock_client.create_profile.call_args.kwargs["grid_benefit_ore"] is None


@pytest.mark.asyncio
async def test_link_flow_mirrors_the_grid_benefit(hass: HomeAssistant) -> None:
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value={
        **LINK_PROFILE, CONF_GRID_BENEFIT_ORE: 10.4})
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

    assert result["data"][CONF_GRID_BENEFIT_ORE] == 10.4


@pytest.mark.asyncio
async def test_options_zone_422_is_readable_on_base_when_the_field_is_hidden(
    hass: HomeAssistant,
) -> None:
    """A cache out of step with the server: the form has no benefit field for this zone, so
    base is the only place the refusal can be read (review M1)."""
    from custom_components.wolta.api import WoltaApiError

    entry = _make_mock_entry(hass, extra_data={CONF_ZONE: "FI"})
    mock_client, _ = _mock_options_env(entry)
    mock_client.patch_profile = AsyncMock(
        side_effect=WoltaApiError(_BENEFIT_ZONE_DETAIL, status=422))
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        assert CONF_GRID_BENEFIT_ORE not in _tariff_fields(result)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_EXPORT_EXTRA_ORE: 5.0})))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "grid_benefit_zone"


# ---------------------------------------------------------------------------
# Time periods and the contract history (HACS 0.54.0, server api 0.119.0, spec
# 2026-10-27-marknadsanpassning Tillägg 2-4): the options form edits only today's simple values,
# the base prices and the contract's scalars. A contract change applies from today (the server
# adds the entry). The time periods, their holiday rules and the contract history are shown as a
# summary and edited on wolta.se; nothing here ever sends them.
# ---------------------------------------------------------------------------

_CONTRACT_FIELDS = (CONF_RETAIL_PRICE_MODEL, CONF_RETAIL_FIXED_ORE)
# What the server keeps and only wolta.se edits. Never in a PATCH, a create or the cache. The
# sale side's history lists (Tillägg 5) are the server's too: the form edits today's values only.
_WEB_ONLY_FIELDS = ("grid_fee_periods", "grid_fee_holidays", "grid_benefit_periods",
                    "grid_benefit_holidays", "retail_events", "export_events",
                    "grid_benefit_events")
# Tekniska verken's alternative price list (spec Underlag): day 06-23 every day all year.
_ONE_PERIOD = [{"months": list(range(1, 13)), "days": "all", "start_hour": 6, "end_hour": 23,
                "price_ore": 18.0}]
# A winter high-load period and a summer one.
_TWO_PERIODS = [
    {"months": [1, 2, 3, 11, 12], "days": "weekdays", "start_hour": 6, "end_hour": 22,
     "price_ore": 61.2},
    {"months": [4, 5, 6, 7, 8, 9, 10], "days": "weekdays", "start_hour": 6, "end_hour": 22,
     "price_ore": 30.0},
]
# The Bronäs scenario (Review Focus 2): the markup lowered to 3.5 on 2026-01-09.
_BRONAS_EVENTS = [
    {"date": "2023-01-01", "retail_price_model": "quarter", "surcharge_ore": 6.0},
    {"date": "2026-01-09", "retail_price_model": "quarter", "surcharge_ore": 3.5},
]
_BRONAS = {CONF_GRID_VAR_ORE: 9.0, CONF_SURCHARGE_ORE: 3.5, CONF_GRID_BENEFIT_ORE: 5.0,
           CONF_EXPORT_EXTRA_ORE: 0.0}
_BRONAS_SERVER = {
    "grid_fee_periods": _TWO_PERIODS, "grid_fee_holidays": "public_eves",
    "grid_benefit_periods": _ONE_PERIOD, "grid_benefit_holidays": "none",
    "retail_events": _BRONAS_EVENTS,
}

# The server's 422 texts, verbatim, as WoltaApiError carries them (one line, the non-ASCII
# escaped the way the server's JSON sends it).
_PERIODS_BASE_DETAIL = (
    'HTTP 422 from https://wolta.se/api/v1/profile: {"detail":"tidsperioder kr\\u00e4ver ett '
    'baspris (grid_var_ore resp. grid_benefit_ore)"}')
_RETAIL_FIXED_DETAIL = (
    'HTTP 422 from https://wolta.se/api/v1/profile: {"detail":"retail_fixed_ore kr\\u00e4vs med '
    'retail_price_model fixed och f\\u00e5r inte s\\u00e4ttas annars"}')
_RETAIL_FIXED_SURCHARGE_DETAIL = (
    'HTTP 422 from https://wolta.se/api/v1/profile: {"detail":"surcharge_ore f\\u00e5r inte '
    's\\u00e4ttas med retail_price_model fixed (det fasta priset \\u00e4r elhandelns hela pris)"}')
_TARIFF_ZONE_DETAIL = (
    'HTTP 422 from https://wolta.se/api/v1/profile: {"detail":"tidsperioderna, helgdagsreglerna '
    'och elhandelsavtalet g\\u00e4ller bara SE1\\u2013SE4"}')
# Plain letters, exactly as an owner's log showed it (2026-10-29): the server does not always
# escape.
_PURCHASE_DATE_DETAIL = (
    'HTTP 422 from https://wolta.se/api/v1/profile: {"detail":"purchase_date härleds ur '
    'etapperna och kan bara sättas TIDIGARE än den första etappen (2020-09-30) – ett senare '
    'datum säger emot etapperna; redigera etapperna i stället"}')


def _selector_values(selector) -> list[str]:
    return [o if isinstance(o, str) else o["value"] for o in selector.config["options"]]


def _bronas_entry(hass, **extra):
    """An SE3 entry whose server snapshot has two fee periods, one benefit period and the Bronäs
    contract history (the cache never holds the lists)."""
    entry = _make_mock_entry(hass, extra_data={**_BRONAS, **extra})
    mock_client, coordinator = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(return_value={**_server_profile(entry), **_BRONAS_SERVER})
    return entry, mock_client, coordinator


def _bronas_input(**changes) -> dict:
    """The form as the Bronäs snapshot fills it, with `changes` on top."""
    return _opts(**_core(**{**_BRONAS, **changes}))


@pytest.mark.asyncio
async def test_options_tariffs_section_holds_only_todays_simple_values(hass: HomeAssistant) -> None:
    """The base prices, their percentages, today's contract and the markup. No time periods, no
    holiday rule, no contract date and no history: those are edited on wolta.se."""
    entry, mock_client, _ = _bronas_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    assert _tariff_fields(result) == [
        CONF_GRID_VAR_ORE, CONF_GRID_VAR_PCT, CONF_RETAIL_PRICE_MODEL, CONF_RETAIL_FIXED_ORE,
        CONF_SURCHARGE_ORE, CONF_GRID_BENEFIT_ORE, CONF_EXPORT_EXTRA_ORE, CONF_EXPORT_EXTRA_PCT,
        CONF_REDUCED_ENERGY_TAX]


@pytest.mark.asyncio
async def test_options_contract_field_selectors_match_the_servers_ranges(
    hass: HomeAssistant,
) -> None:
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    marker, sel = _tariff_field(result, CONF_RETAIL_PRICE_MODEL)
    # The server has no contract type stored (and no history): "not stated" leads (code review
    # C1, see test_options_unanswered_contract_*).
    assert _selector_values(sel) == ["not_stated", "quarter", "month", "fixed"]
    assert sel.config["translation_key"] == "retail_price_model"
    assert marker.default() == "not_stated"
    _, sel = _tariff_field(result, CONF_RETAIL_FIXED_ORE)
    assert (sel.config["min"], sel.config["max"]) == (0.0, 500.0)
    # The fixed price exists in SE1-SE4 only, where the unit is öre.
    assert sel.config["unit_of_measurement"] == "öre per kWh"


@pytest.mark.asyncio
async def test_options_contract_fields_show_the_servers_values(hass: HomeAssistant) -> None:
    entry = _make_mock_entry(hass, extra_data={
        CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    model, _ = _tariff_field(result, CONF_RETAIL_PRICE_MODEL)
    assert model.default() == "fixed"
    fixed, _ = _tariff_field(result, CONF_RETAIL_FIXED_ORE)
    assert fixed.description["suggested_value"] == 85.0


@pytest.mark.asyncio
async def test_options_contract_fields_are_hidden_outside_se1_se4(hass: HomeAssistant) -> None:
    """The server refuses them outside SE1-SE4 (422), so the form does not offer them and a save
    there leaves a cached value alone instead of PATCHing null (or a default) over it."""
    entry = _make_mock_entry(hass, extra_data={CONF_ZONE: "FI", CONF_RETAIL_FIXED_ORE: 40.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        fields = _tariff_fields(result)
        for field in _CONTRACT_FIELDS:
            assert field not in fields, field
        # The markup exists in every zone.
        assert CONF_SURCHARGE_ORE in fields
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core()))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_untouched_contract_default_patches_nothing(hass: HomeAssistant) -> None:
    """The contract select always carries a value in the form (its default), so an untouched form
    must not turn the server's "not set" into a PATCH of the default."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core()))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [None, "quarter"], ids=["cache-unset", "cache-quarter"])
@pytest.mark.parametrize("in_form", ["quarter", None], ids=["form-quarter", "form-absent"])
async def test_options_server_quarter_and_untouched_form_patch_nothing(
    hass: HomeAssistant, cached, in_form
) -> None:
    """The server stores an answered quarter-hour price literally as "quarter" (its ruling 15:
    "not set" means never answered). The form reads "not set" and "quarter" alike, so an
    untouched save sends no contract type, whatever the cache holds and whether the select comes
    back filled or empty."""
    extra = {CONF_SURCHARGE_ORE: 5.0}
    if cached is not None:
        extra[CONF_RETAIL_PRICE_MODEL] = cached
    entry = _make_mock_entry(hass, extra_data=extra)
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(return_value=_server_profile(
        entry, **{CONF_RETAIL_PRICE_MODEL: "quarter"}))
    submitted = {CONF_SURCHARGE_ORE: 5.0}
    if in_form is not None:
        submitted[CONF_RETAIL_PRICE_MODEL] = in_form
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**submitted)))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


# --- Code review C1: an explicit quarter-hour price can be answered from here. The server counts
# the contract type as unanswered while it has none stored and no contract history (its ruling 14:
# the first answer then applies to all of the history), and stores an answer literally (ruling 15).
# Without a "not stated" choice the select showed "Quarter-hour price" as its default and never
# sent it, so an owner on a quarter-hour price could not say so, and a later switch to a monthly
# or fixed price applied from the start of the history. ---


def _unanswered_entry(hass, **server):
    """An SE3 entry whose server snapshot has no contract type and no contract history."""
    entry = _make_mock_entry(hass, extra_data={CONF_SURCHARGE_ORE: 6.0})
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(return_value=_server_profile(
        entry, **{CONF_RETAIL_PRICE_MODEL: None, **server}))
    return entry, mock_client


@pytest.mark.asyncio
@pytest.mark.parametrize("in_form", ["not_stated", None], ids=["form-not-stated", "form-absent"])
async def test_options_unanswered_contract_defaults_to_not_stated_and_sends_nothing(
    hass: HomeAssistant, in_form
) -> None:
    """(a) "Not stated" is offered first and is the default; an untouched save PATCHes nothing,
    and "not_stated" never reaches the server or the cache."""
    entry, mock_client = _unanswered_entry(hass)
    submitted = {CONF_SURCHARGE_ORE: 6.0}
    if in_form is not None:
        submitted[CONF_RETAIL_PRICE_MODEL] = in_form
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        marker, sel = _tariff_field(result, CONF_RETAIL_PRICE_MODEL)
        assert _selector_values(sel) == ["not_stated", "quarter", "month", "fixed"]
        assert marker.default() == "not_stated"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**submitted)))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()
    assert CONF_RETAIL_PRICE_MODEL not in hass.config_entries.async_get_entry(entry.entry_id).data


@pytest.mark.asyncio
async def test_options_unanswered_contract_sends_an_explicit_quarter_hour_price(
    hass: HomeAssistant,
) -> None:
    """(b) Choosing "Quarter-hour price" is an answer: it is sent, and the cache follows."""
    entry, mock_client = _unanswered_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{
                CONF_SURCHARGE_ORE: 6.0, CONF_RETAIL_PRICE_MODEL: "quarter"})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_RETAIL_PRICE_MODEL: "quarter"}
    assert hass.config_entries.async_get_entry(entry.entry_id).data[
        CONF_RETAIL_PRICE_MODEL] == "quarter"


@pytest.mark.asyncio
@pytest.mark.parametrize("server", [{CONF_RETAIL_PRICE_MODEL: "quarter"},
                                    {"retail_events": _BRONAS_EVENTS}],
                         ids=["stored-quarter", "history-without-model"])
async def test_options_answered_contract_has_no_not_stated_choice(
    hass: HomeAssistant, server
) -> None:
    """(c) With a stored contract type (or a contract history) the question is answered: no "not
    stated" choice, the default is the server's (else quarter), an untouched save PATCHes
    nothing."""
    entry, mock_client = _unanswered_entry(hass, **server)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        marker, sel = _tariff_field(result, CONF_RETAIL_PRICE_MODEL)
        assert _selector_values(sel) == ["quarter", "month", "fixed"]
        assert marker.default() == "quarter"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_SURCHARGE_ORE: 6.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


_UNTYPED_EVENTS = [
    {"date": None, "retail_price_model": None, "surcharge_ore": 4.0},
    {"date": "2026-10-27", "retail_price_model": None, "surcharge_ore": 6.0},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice, sent", [("not_stated", None),
                                          ("month", {CONF_RETAIL_PRICE_MODEL: "month"}),
                                          ("quarter", {CONF_RETAIL_PRICE_MODEL: "quarter"})],
                         ids=["untouched", "month", "quarter"])
async def test_options_history_without_a_type_today_offers_not_stated(
    hass: HomeAssistant, choice, sent
) -> None:
    """Server ruling 22: a markup change on a plant without a contract type gives history entries
    without a type, and the server mirrors no type. Today's entry has no type, so the select still
    leads with "not stated" and starts on it; an untouched save PATCHes nothing, and a chosen type
    is sent, the quarter-hour price included (the server then gives every entry that type)."""
    entry, mock_client = _unanswered_entry(hass, retail_events=_UNTYPED_EVENTS)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        marker, sel = _tariff_field(result, CONF_RETAIL_PRICE_MODEL)
        assert _selector_values(sel) == ["not_stated", "quarter", "month", "fixed"]
        assert marker.default() == "not_stated"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{
                CONF_SURCHARGE_ORE: 6.0, CONF_RETAIL_PRICE_MODEL: choice})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    if sent is None:
        mock_client.patch_profile.assert_not_awaited()
    else:
        assert mock_client.patch_profile.call_args.kwargs == sent


@pytest.mark.parametrize("events, expected", [
    # Today's entry has a type (an older one has none): answered, no "not stated".
    ([_UNTYPED_EVENTS[0], {**_UNTYPED_EVENTS[1], "retail_price_model": "month"}], False),
    # Order in the list does not matter: today's entry is the one with the latest date, and the
    # entry "from the start" is the earliest whatever its place.
    ([_UNTYPED_EVENTS[1], {**_UNTYPED_EVENTS[0], "retail_price_model": "month"}], True),
    # A malformed history counts as none: the scalar decides (None here).
    (["x"], True),
], ids=["today-typed", "unsorted", "malformed"])
def test_contract_unanswered_reads_todays_entry(events, expected) -> None:
    from custom_components.wolta.config_flow import _contract_unanswered
    assert _contract_unanswered({CONF_RETAIL_PRICE_MODEL: None, "retail_events": events}) is expected
    # Without a history the scalar decides, as before.
    assert _contract_unanswered({CONF_RETAIL_PRICE_MODEL: "quarter"}) is False
    assert _contract_unanswered({}) is True


@pytest.mark.asyncio
async def test_options_unanswered_contract_fixed_price_is_sent_as_before(
    hass: HomeAssistant,
) -> None:
    """(d) Choosing a fixed price with its price sends both and no markup, as before."""
    entry, mock_client = _unanswered_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{
                CONF_SURCHARGE_ORE: 6.0, CONF_RETAIL_PRICE_MODEL: "fixed",
                CONF_RETAIL_FIXED_ORE: 85.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {
        CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0}


@pytest.mark.asyncio
async def test_options_not_stated_with_a_new_markup_sends_only_the_markup(
    hass: HomeAssistant,
) -> None:
    """"Not stated" counts as a quarter-hour price for the contract rules (the server counts it
    so): a markup is allowed next to it and is sent alone."""
    entry, mock_client = _unanswered_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{
                CONF_SURCHARGE_ORE: 7.5, CONF_RETAIL_PRICE_MODEL: "not_stated"})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_SURCHARGE_ORE: 7.5}


# --- Review Focus 5: with time periods the form shows a summary, an untouched save PATCHes
# nothing, and no save ever writes the period lists. ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored, server, needles",
    [
        ({CONF_GRID_VAR_ORE: 9.0},
         {"grid_fee_periods": _ONE_PERIOD, "grid_fee_holidays": "none"},
         ["The grid fee has 1 time period with a price of its own."]),
        ({CONF_GRID_VAR_ORE: 9.0},
         {"grid_fee_periods": _TWO_PERIODS, "grid_fee_holidays": "none"},
         ["The grid fee has 2 time periods with a price of their own "
          "(holidays not counted as weekdays: none)."]),
        # The full Bronäs snapshot: both sides' periods, the contract history and today's markup
        # 3.5 echoed from its last entry.
        (_BRONAS, _BRONAS_SERVER,
         ["The grid fee has 2 time periods",
          "The grid benefit has 1 time period with a price of its own.",
          "The contract history has 2 contracts, today's since 2026-01-09."]),
    ],
    ids=["one-period", "two-periods", "bronas"],
)
async def test_options_with_time_periods_shows_the_summary_and_an_untouched_save_patches_nothing(
    hass: HomeAssistant, stored, server, needles
) -> None:
    entry = _make_mock_entry(hass, extra_data=stored)
    mock_client, _ = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(return_value={**_server_profile(entry), **server})
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        summary = result["description_placeholders"]["web_summary"]
        for needle in needles:
            assert needle in summary, (needle, summary)
        assert "wolta.se" in summary
        # Not one field per period: the lists are not editable here.
        for field in _WEB_ONLY_FIELDS:
            assert field not in _tariff_fields(result), field
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**stored)))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {CONF_GRID_VAR_ORE: 10.0},
        {CONF_GRID_BENEFIT_ORE: 6.0},
        {CONF_SURCHARGE_ORE: 7.0},
        {CONF_RETAIL_PRICE_MODEL: "month"},
        {CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0},
        {CONF_GRID_VAR_PCT: 5.0},
        {CONF_EXPORT_EXTRA_ORE: None},
        {CONF_BATTERY_KWH: 25.0},
    ],
    ids=["base-fee", "base-benefit", "markup", "month", "fixed", "pct", "cleared", "battery"],
)
async def test_options_no_save_ever_writes_the_period_lists_or_the_history(
    hass: HomeAssistant, changes
) -> None:
    entry, mock_client, _ = _bronas_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        submitted = _bronas_input(**changes)
        if CONF_EXPORT_EXTRA_ORE in changes:
            del submitted["tariffs"][CONF_EXPORT_EXTRA_ORE]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=submitted)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_awaited_once()
    sent = mock_client.patch_profile.call_args.kwargs
    assert not set(sent) & set(_WEB_ONLY_FIELDS), sent
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert not set(updated.data) & set(_WEB_ONLY_FIELDS)


@pytest.mark.asyncio
async def test_options_summary_names_the_contract_history_in_swedish(hass: HomeAssistant) -> None:
    hass.config.language = "sv"
    entry, mock_client, _ = _bronas_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    summary = result["description_placeholders"]["web_summary"]
    assert summary == (
        "\n\nNätavgiften har 2 tidsperioder med eget pris (helgdagar som inte räknas som vardagar: "
        "allmänna helgdagar, julafton och nyårsafton). Nätnyttan har 1 tidsperiod med eget pris. "
        "Avtalshistoriken har 2 avtal, det som gäller i dag sedan 2026-01-09. De ändras på "
        "wolta.se.")


@pytest.mark.asyncio
async def test_options_summary_is_empty_without_periods_or_history(hass: HomeAssistant) -> None:
    """Always passed (the description must never show a raw placeholder), empty when there is
    nothing to summarise. server_detail likewise, for the server_rejected error."""
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    assert result["description_placeholders"] == {"web_summary": "", "server_detail": ""}


def test_web_summary_reads_only_well_formed_lists() -> None:
    from custom_components.wolta.config_flow import _web_summary

    assert _web_summary("en", {"grid_fee_periods": [], "retail_events": None}) == ""
    assert _web_summary("en", {"grid_fee_periods": "x", "retail_events": ["x"]}) == ""
    # An unknown language falls back to English, a regional one to its language.
    assert _web_summary("de", {"grid_benefit_periods": _ONE_PERIOD}).startswith(
        "The grid benefit has 1 time period")
    assert _web_summary("sv-SE", {"grid_benefit_periods": _TWO_PERIODS}).startswith(
        "Nätnyttan har 2 tidsperioder")
    assert _web_summary(None, {"retail_events": _BRONAS_EVENTS}) == (
        "The contract history has 2 contracts, today's since 2026-01-09. These are changed on "
        "wolta.se.")


@pytest.mark.parametrize(
    "lang, expected",
    [("en", "The contract history has 1 contract, which applies to all of your history."),
     ("sv", "Avtalshistoriken har 1 avtal, som gäller hela historiken.")],
)
def test_web_summary_a_lone_contract_is_singular_and_has_no_since(lang, expected) -> None:
    """The first contract applies back to the start of the history whatever its date, so a lone
    dated contract must not read "1 contracts, today's since <date>"."""
    from custom_components.wolta.config_flow import _web_summary

    text = _web_summary(lang, {"retail_events": [_BRONAS_EVENTS[1]]})
    assert text.startswith(expected), text
    assert "2026-01-09" not in text


@pytest.mark.parametrize(
    "rule, expected",
    [(None, "public holidays, Christmas Eve and New Year's Eve"),
     ("public_eves_maundy", "public holidays, Christmas Eve, New Year's Eve and Maundy Thursday"),
     ("future_rule", "future_rule")],
    ids=["default", "named", "unknown"],
)
def test_web_summary_names_the_holiday_rule_of_weekday_periods(rule, expected) -> None:
    from custom_components.wolta.config_flow import _web_summary

    text = _web_summary("en", {"grid_benefit_periods": _TWO_PERIODS, "grid_benefit_holidays": rule})
    assert f"(holidays not counted as weekdays: {expected})." in text, text
    # A list with only all-day periods: the rule decides nothing and is not shown.
    assert "holidays" not in _web_summary(
        "en", {"grid_fee_periods": _ONE_PERIOD, "grid_fee_holidays": "public"})


# --- The contract: a change applies from today and sends only the scalars that changed. ---


@pytest.mark.asyncio
async def test_options_bronas_markup_change_sends_only_the_markup(hass: HomeAssistant) -> None:
    """Review Focus 2 on this side: raising the markup to 7.0 today sends surcharge_ore alone.
    The server adds the entry dated today and leaves the 2026-01-09 entry as it is; nothing here
    sends the history, a date or the contract type."""
    entry, mock_client, _ = _bronas_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_bronas_input(**{CONF_SURCHARGE_ORE: 7.0}))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_SURCHARGE_ORE: 7.0}
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_SURCHARGE_ORE] == 7.0


@pytest.mark.asyncio
async def test_options_month_contract_sends_only_the_contract_type(hass: HomeAssistant) -> None:
    entry = _make_mock_entry(hass, extra_data={CONF_SURCHARGE_ORE: 6.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{
                CONF_SURCHARGE_ORE: 6.0, CONF_RETAIL_PRICE_MODEL: "month"})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_RETAIL_PRICE_MODEL: "month"}
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_RETAIL_PRICE_MODEL] == "month"
    assert updated.data[CONF_SURCHARGE_ORE] == 6.0


@pytest.mark.asyncio
async def test_options_fixed_contract_is_sent_without_the_markup(hass: HomeAssistant) -> None:
    """The form still shows the old markup next to the new fixed price. It is not sent (a fixed
    price is the retailer's whole price; the server refuses a markup with it and drops the old
    one itself), and the cache follows the server."""
    entry = _make_mock_entry(hass, extra_data={CONF_SURCHARGE_ORE: 6.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{
                CONF_SURCHARGE_ORE: 6.0, CONF_RETAIL_PRICE_MODEL: "fixed",
                CONF_RETAIL_FIXED_ORE: 85.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {
        CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0}
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_RETAIL_FIXED_ORE] == 85.0
    assert CONF_SURCHARGE_ORE not in updated.data


@pytest.mark.asyncio
async def test_options_fixed_contract_with_the_markup_cleared_sends_no_markup(
    hass: HomeAssistant,
) -> None:
    """Clearing the markup while choosing a fixed price is what the help text asks for: the null
    is not sent either (never surcharge_ore with fixed)."""
    entry = _make_mock_entry(hass, extra_data={CONF_SURCHARGE_ORE: 6.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{
                CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {
        CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored, submitted",
    [
        # Switching to a fixed price with a new markup typed.
        ({CONF_SURCHARGE_ORE: 6.0},
         {CONF_SURCHARGE_ORE: 7.0, CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0}),
        # A fixed price already, and a markup typed next to it.
        ({CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0},
         {CONF_SURCHARGE_ORE: 5.0, CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0}),
    ],
    ids=["switching", "already-fixed"],
)
async def test_options_a_markup_with_a_fixed_price_is_refused_before_sending(
    hass: HomeAssistant, stored, submitted
) -> None:
    """A typed value is never dropped silently: the form says it, at the markup and on base."""
    entry = _make_mock_entry(hass, extra_data=stored)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**submitted)))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_SURCHARGE_ORE: "retail_fixed_surcharge",
                                "base": "retail_fixed_surcharge"}
    assert _section_options(result, "tariffs")["collapsed"] is False
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_model", ["month", "quarter"])
@pytest.mark.parametrize("fixed_in_form", [80.0, None], ids=["shown", "cleared"])
async def test_options_leaving_the_fixed_contract_lets_the_server_drop_the_fixed_price(
    hass: HomeAssistant, new_model: str, fixed_in_form
) -> None:
    """The server drops retail_fixed_ore when the contract leaves `fixed`. The form still shows
    the old value, so it is not sent back (shown or cleared), and the cache follows the server."""
    entry = _make_mock_entry(hass, extra_data={
        CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 80.0})
    mock_client, _ = _mock_options_env(entry)
    submitted = {CONF_RETAIL_PRICE_MODEL: new_model}
    if fixed_in_form is not None:
        submitted[CONF_RETAIL_FIXED_ORE] = fixed_in_form
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**submitted)))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_RETAIL_PRICE_MODEL: new_model}
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_RETAIL_PRICE_MODEL] == new_model
    assert CONF_RETAIL_FIXED_ORE not in updated.data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {CONF_RETAIL_PRICE_MODEL: "fixed"},
        {CONF_RETAIL_FIXED_ORE: 85.0},
        {CONF_RETAIL_PRICE_MODEL: "month", CONF_RETAIL_FIXED_ORE: 85.0},
    ],
    ids=["fixed-needs-price", "price-with-quarter", "price-with-month"],
)
async def test_options_contract_rules_are_said_before_sending(hass: HomeAssistant, fields) -> None:
    entry = _make_mock_entry(hass)
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        assert _section_options(result, "tariffs")["collapsed"] is True
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**fields)))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_RETAIL_FIXED_ORE: "retail_fixed_invalid",
                                "base": "retail_fixed_invalid"}
    assert _section_options(result, "tariffs")["collapsed"] is False
    mock_client.patch_profile.assert_not_awaited()


# --- A time-period list needs its base price. ---


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [CONF_GRID_VAR_ORE, CONF_GRID_BENEFIT_ORE])
async def test_options_clearing_a_base_price_under_time_periods_is_refused_before_sending(
    hass: HomeAssistant, field: str
) -> None:
    entry, mock_client, _ = _bronas_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        submitted = _bronas_input()
        del submitted["tariffs"][field]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=submitted)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {field: "periods_require_base", "base": "periods_require_base"}
    assert _section_options(result, "tariffs")["collapsed"] is False
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_clearing_a_base_price_without_time_periods_is_sent(
    hass: HomeAssistant,
) -> None:
    entry = _make_mock_entry(hass, extra_data={CONF_GRID_VAR_ORE: 9.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core()))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_GRID_VAR_ORE: None}


# --- The server's own 422s, for when the prechecks cannot see it (a cache out of step). ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail, stored, submitted, field, code",
    [
        (_PERIODS_BASE_DETAIL, {CONF_GRID_VAR_ORE: 9.0}, {}, CONF_GRID_VAR_ORE,
         "periods_require_base"),
        (_PERIODS_BASE_DETAIL, {CONF_GRID_BENEFIT_ORE: 5.0}, {CONF_EXPORT_EXTRA_ORE: 0.0},
         CONF_GRID_BENEFIT_ORE, "periods_require_base"),
        (_RETAIL_FIXED_DETAIL, {},
         {CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0},
         CONF_RETAIL_FIXED_ORE, "retail_fixed_invalid"),
        (_RETAIL_FIXED_SURCHARGE_DETAIL, {CONF_SURCHARGE_ORE: 6.0},
         {CONF_SURCHARGE_ORE: 7.0}, CONF_SURCHARGE_ORE, "retail_fixed_surcharge"),
        (_TARIFF_ZONE_DETAIL, {}, {CONF_RETAIL_PRICE_MODEL: "month"},
         CONF_RETAIL_PRICE_MODEL, "tariff_zone_se_only"),
    ],
    ids=["periods-fee-base", "periods-benefit-base", "fixed", "fixed-markup", "zone"],
)
async def test_options_server_422_for_the_period_and_contract_rules_shows_at_the_field_and_on_base(
    hass: HomeAssistant, detail: str, stored, submitted, field: str, code: str
) -> None:
    """Never cannot_connect."""
    from custom_components.wolta.api import WoltaApiError

    entry = _make_mock_entry(hass, extra_data=stored)
    mock_client, _ = _mock_options_env(entry)
    mock_client.patch_profile = AsyncMock(side_effect=WoltaApiError(detail, status=422))
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**submitted)))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {field: code, "base": code}
    assert _section_options(result, "tariffs")["collapsed"] is False


@pytest.mark.parametrize(
    "detail, code",
    [
        (_PERIODS_BASE_DETAIL, "periods_require_base"),
        (_RETAIL_FIXED_DETAIL, "retail_fixed_invalid"),
        (_RETAIL_FIXED_SURCHARGE_DETAIL, "retail_fixed_surcharge"),
        (_TARIFF_ZONE_DETAIL, "tariff_zone_se_only"),
        (_PURCHASE_DATE_DETAIL, "purchase_date_after_stages"),
        ('HTTP 422 from x: {"detail":"purchase_date h\\u00e4rleds ur etapperna och kan bara '
         's\\u00e4ttas TIDIGARE"}', "purchase_date_after_stages"),
        ('HTTP 422 from x: {"detail":"purchase_date m\\u00e5ste vara ISO-format"}', None),
        # Plain letters, as a server that does not escape would send them.
        ('HTTP 422 from x: {"detail":"retail_fixed_ore krävs med retail_price_model fixed"}',
         "retail_fixed_invalid"),
        # Another 422 that names the same fields (a type error) is not these rules.
        ('HTTP 422 from x: {"detail":"retail_fixed_ore must be a number"}', None),
        ('HTTP 422 from x: {"detail":"surcharge_ore must be a number"}', None),
        ('HTTP 422 from x: {"detail":"grid_var_pct kr\\u00e4ver grid_var_ore (n\\u00e4t)"}', None),
        # The rules for the lists themselves: this form never sends a list, so they are not
        # mapped to one of its fields.
        ('HTTP 422 from x: {"detail":"tv\\u00e5 tidsperioder \\u00f6verlappar"}', None),
    ],
)
def test_tariff_rule_error_matches_the_rules_own_wording(detail: str, code) -> None:
    from custom_components.wolta.api import WoltaApiError
    from custom_components.wolta.config_flow import _tariff_rule_error

    assert _tariff_rule_error(WoltaApiError(detail, status=422)) == code
    assert _tariff_rule_error(WoltaApiError(detail, status=500)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields", [{CONF_RETAIL_PRICE_MODEL: "month"},
               {CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0}],
    ids=["month", "fixed"],
)
async def test_options_contract_change_refreshes_the_purchase_price(
    hass: HomeAssistant, fields
) -> None:
    """The contract changes what the owner pays: the kept /profile/prices answer was priced
    without it, so it is dropped and a fresh one fetched."""
    entry = _make_mock_entry(hass)
    mock_client, mock_coordinator = _mock_options_env(entry)
    mock_coordinator.prices = MagicMock()
    mock_coordinator.prices.async_request_refresh = AsyncMock()
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**fields)))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    names = [c[0] for c in mock_coordinator.prices.mock_calls]
    assert names == ["async_forget_purchase", "async_request_refresh"]


def test_every_contract_field_is_a_price_key_and_a_synced_field() -> None:
    from custom_components.wolta.config_flow import _TARIFF_PRICE_KEYS
    from custom_components.wolta.coordinator import _PROFILE_SYNC_KEYS

    for field in _CONTRACT_FIELDS:
        assert field in _TARIFF_PRICE_KEYS, field
        assert field in _PROFILE_SYNC_KEYS, field
    # The lists are the server's and wolta.se's: never cached, never a form field.
    for field in _WEB_ONLY_FIELDS:
        assert field not in _PROFILE_SYNC_KEYS, field
        assert field not in _TARIFF_PRICE_KEYS, field


# --- Reauth and linking carry today's contract, never the lists. ---


@pytest.mark.asyncio
async def test_reauth_sends_the_cached_contract(hass: HomeAssistant) -> None:
    """A surviving row keeps the server's contract and history and ignores these; a row created
    after a purge takes today's contract from here, so the cache is the only copy."""
    mock_client = _mock_client("new-token")
    await _start_reauth(hass, {**_REAUTH_BASE, CONF_RETAIL_PRICE_MODEL: "month",
                               CONF_SURCHARGE_ORE: 3.5}, mock_client)
    kwargs = mock_client.create_profile.call_args.kwargs
    assert kwargs[CONF_RETAIL_PRICE_MODEL] == "month"
    assert kwargs[CONF_RETAIL_FIXED_ORE] is None
    assert kwargs[CONF_SURCHARGE_ORE] == 3.5
    assert not set(kwargs) & set(_WEB_ONLY_FIELDS)


@pytest.mark.asyncio
async def test_reauth_never_sends_a_markup_with_a_fixed_price(hass: HomeAssistant) -> None:
    """A cache from before the server dropped the markup would otherwise be refused (422) and
    block the reauth."""
    mock_client = _mock_client("new-token")
    await _start_reauth(hass, {**_REAUTH_BASE, CONF_RETAIL_PRICE_MODEL: "fixed",
                               CONF_RETAIL_FIXED_ORE: 85.0, CONF_SURCHARGE_ORE: 6.0}, mock_client)
    kwargs = mock_client.create_profile.call_args.kwargs
    assert kwargs[CONF_RETAIL_PRICE_MODEL] == "fixed"
    assert kwargs[CONF_RETAIL_FIXED_ORE] == 85.0
    assert kwargs[CONF_SURCHARGE_ORE] is None


@pytest.mark.asyncio
async def test_reauth_outside_se1_se4_sends_the_markup_but_not_the_contract(
    hass: HomeAssistant,
) -> None:
    """The server refuses the contract fields outside SE1-SE4 (422), which would block the reauth
    for a cache that outlived a zone correction. The markup exists in every zone."""
    mock_client = _mock_client("new-token")
    await _start_reauth(hass, {**_REAUTH_BASE, CONF_ZONE: "FI", CONF_RETAIL_PRICE_MODEL: "fixed",
                               CONF_RETAIL_FIXED_ORE: 85.0, CONF_SURCHARGE_ORE: 2.0}, mock_client)
    kwargs = mock_client.create_profile.call_args.kwargs
    for key in _CONTRACT_FIELDS:
        assert kwargs[key] is None, key
    assert kwargs[CONF_SURCHARGE_ORE] == 2.0


@pytest.mark.asyncio
async def test_link_flow_mirrors_the_contract_but_not_the_lists(hass: HomeAssistant) -> None:
    mock_client = _mock_client()
    mock_client.get_profile = AsyncMock(return_value={
        **LINK_PROFILE, CONF_RETAIL_PRICE_MODEL: "fixed", CONF_RETAIL_FIXED_ORE: 85.0,
        **_BRONAS_SERVER})
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

    assert result["data"][CONF_RETAIL_PRICE_MODEL] == "fixed"
    assert result["data"][CONF_RETAIL_FIXED_ORE] == 85.0
    assert not set(result["data"]) & set(_WEB_ONLY_FIELDS)


@pytest.mark.asyncio
async def test_options_an_unknown_server_contract_does_not_block_an_untouched_save(
    hass: HomeAssistant,
) -> None:
    """A contract this version does not know (a newer server) must stay selectable: otherwise the
    select rejects the injected value and NO options save works. The unknown value is offered as
    an option (its raw key as label), an untouched save sends it back unchanged and the diff
    PATCHes nothing."""
    entry = _make_mock_entry(hass, extra_data={CONF_RETAIL_PRICE_MODEL: "future_model"})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        marker, sel = _tariff_field(result, CONF_RETAIL_PRICE_MODEL)
        assert "future_model" in _selector_values(sel)
        assert marker.default() == "future_model"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core()))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


# ---------------------------------------------------------------------------
# The sale side's history (HACS 0.54.0, server api 0.119.0, spec 2026-10-27-marknadsanpassning
# Tillägg 5): the server keeps a history of the retailer's terms (export_events) and of the grid
# benefit (grid_benefit_events). The form edits today's values with the same fields as before; a
# change becomes an entry dated today on the server (or applies from the start, its rulings 18
# and 19). The lists are summarised and edited on wolta.se; nothing here ever sends them.
# ---------------------------------------------------------------------------

# Review Focus 1: the network operator's yearly price list, 6.0 until 2025 and 10.4 from 2026;
# the retailer's terms lowered from 3.0 to -1.5 on 2025-07-01. The scalars mirror today's entry.
_GRID_BENEFIT_EVENTS = [{"date": None, "grid_benefit_ore": 6.0},
                        {"date": "2026-01-01", "grid_benefit_ore": 10.4}]
_EXPORT_EVENTS = [{"date": None, "export_extra_ore": 3.0},
                  {"date": "2025-07-01", "export_extra_ore": -1.5}]
_SALE = {CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: -1.5}
_SALE_SERVER = {"export_events": _EXPORT_EVENTS, "grid_benefit_events": _GRID_BENEFIT_EVENTS}
_SALE_LISTS = ("export_events", "grid_benefit_events")
# The server's 422 for clearing the grid benefit's base price under its history (today's entry
# would have no base price), verbatim as WoltaApiError carries it.
_GRID_BENEFIT_EVENTS_DETAIL = (
    'HTTP 422 from https://wolta.se/api/v1/profile: {"detail":"en n\\u00e4tnyttepost beh\\u00f6ver '
    'datum (inte i framtiden, inte tv\\u00e5 p\\u00e5 samma dag) och ett baspris '
    '(grid_benefit_ore)"}')
_EXPORT_EVENTS_DETAIL = (
    'HTTP 422 from https://wolta.se/api/v1/profile: {"detail":"ett s\\u00e4ljavtal beh\\u00f6ver '
    'datum (inte i framtiden, inte tv\\u00e5 p\\u00e5 samma dag) och elhandelns villkor '
    '(export_extra_ore)"}')


def _sale_entry(hass, server: dict | None = None, **extra):
    """An SE3 entry whose server snapshot has both sale history lists (the cache never holds
    them)."""
    entry = _make_mock_entry(hass, extra_data={**_SALE, **extra})
    mock_client, coordinator = _mock_options_env(entry)
    mock_client.get_profile = AsyncMock(return_value={
        **_server_profile(entry), **(_SALE_SERVER if server is None else server)})
    return entry, mock_client, coordinator


@pytest.mark.asyncio
async def test_options_untouched_save_with_sale_history_patches_nothing(
    hass: HomeAssistant,
) -> None:
    """The scalars mirror today's entry, so the form shows them and an untouched save sends
    nothing: no scalar, and never a list. The lists are not form fields."""
    entry, mock_client, _ = _sale_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        fields = _tariff_fields(result)
        for field in _SALE_LISTS:
            assert field not in fields, field
        benefit, _ = _tariff_field(result, CONF_GRID_BENEFIT_ORE)
        assert benefit.description["suggested_value"] == 10.4
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**_SALE)))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes, sent",
    [
        # The retailer's terms raised today: the server adds an entry dated today and keeps
        # -1.5 from 2025-07-01. The unchanged grid benefit goes with it (both halves are
        # confirmed together); equal to today's entry, it adds nothing on the server.
        ({CONF_EXPORT_EXTRA_ORE: 2.0}, {CONF_EXPORT_EXTRA_ORE: 2.0, CONF_GRID_BENEFIT_ORE: 10.4}),
        # The operator's new price list: 11.0 from today.
        ({CONF_GRID_BENEFIT_ORE: 11.0}, {CONF_GRID_BENEFIT_ORE: 11.0, CONF_EXPORT_EXTRA_ORE: -1.5}),
        # The percentage is part of the grid benefit's block (ruling 19): an entry dated today.
        ({CONF_EXPORT_EXTRA_PCT: 5.0}, {CONF_EXPORT_EXTRA_PCT: 5.0}),
        # Cleared terms are plain spot from today (ruling 10); the history keeps the answer.
        ({CONF_EXPORT_EXTRA_ORE: None}, {CONF_EXPORT_EXTRA_ORE: 0.0, CONF_GRID_BENEFIT_ORE: 10.4}),
    ],
    ids=["terms", "benefit", "pct", "terms-cleared"],
)
async def test_options_sale_change_with_sale_history_sends_only_the_scalars(
    hass: HomeAssistant, changes, sent
) -> None:
    entry, mock_client, _ = _sale_entry(hass)
    submitted = {**_SALE, **changes}
    if changes.get(CONF_EXPORT_EXTRA_ORE, 0) is None:
        del submitted[CONF_EXPORT_EXTRA_ORE]
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**submitted)))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == sent
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert not set(updated.data) & set(_WEB_ONLY_FIELDS)
    for key, val in sent.items():
        assert updated.data[key] == val, key


@pytest.mark.asyncio
async def test_options_benefit_change_echoes_the_filled_in_zero_terms(hass: HomeAssistant) -> None:
    """The confirm-both echo next to ruling 18: the stored terms are the 0.0 every new row gets
    and there is no history. Changing the grid benefit sends that 0.0 back with it, exactly
    these two keys: equal to the stored value, it is no change on the server, so the terms stay
    unanswered and a later first value other than 0 still applies from the start."""
    entry = _make_mock_entry(hass, extra_data={CONF_EXPORT_EXTRA_ORE: 0.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=_opts(**_core(**{CONF_GRID_BENEFIT_ORE: 6.0, CONF_EXPORT_EXTRA_ORE: 0.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {
        CONF_GRID_BENEFIT_ORE: 6.0, CONF_EXPORT_EXTRA_ORE: 0.0}


@pytest.mark.asyncio
async def test_options_terms_change_never_invents_a_first_grid_benefit(hass: HomeAssistant) -> None:
    """The echo goes one way only: with no grid benefit stored (and none typed), changing the
    terms sends the terms alone. A grid_benefit_ore key here would be a first answer, which the
    server applies to all of the history (ruling 19)."""
    entry = _make_mock_entry(hass, extra_data={CONF_EXPORT_EXTRA_ORE: 0.0})
    mock_client, _ = _mock_options_env(entry)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=_opts(**_core(**{CONF_EXPORT_EXTRA_ORE: 2.0})))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {CONF_EXPORT_EXTRA_ORE: 2.0}
    assert CONF_GRID_BENEFIT_ORE not in hass.config_entries.async_get_entry(entry.entry_id).data


@pytest.mark.asyncio
async def test_options_summary_names_the_sale_history(hass: HomeAssistant) -> None:
    entry, mock_client, _ = _sale_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    assert result["description_placeholders"]["web_summary"] == (
        "\n\nThe grid benefit history has 2 entries, today's since 2026-01-01. The history of "
        "your retailer's terms for sold electricity has 2 entries, today's since 2025-07-01. "
        "These are changed on wolta.se.")


@pytest.mark.asyncio
async def test_options_summary_names_the_sale_history_in_swedish(hass: HomeAssistant) -> None:
    hass.config.language = "sv"
    entry, mock_client, _ = _sale_entry(hass)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
    assert result["description_placeholders"]["web_summary"] == (
        "\n\nNätnyttans historik har 2 poster, den som gäller i dag sedan 2026-01-01. Historiken "
        "för elhandelns villkor för såld el har 2 poster, den som gäller i dag sedan 2025-07-01. "
        "De ändras på wolta.se.")


@pytest.mark.parametrize(
    "lang, key, expected",
    [("en", "grid_benefit_events",
      "The grid benefit history has 1 entry, which applies to all of your history."),
     ("en", "export_events", "The history of your retailer's terms for sold electricity has 1 "
      "entry, which applies to all of your history."),
     ("sv", "grid_benefit_events", "Nätnyttans historik har 1 post, som gäller hela historiken."),
     ("sv", "export_events", "Historiken för elhandelns villkor för såld el har 1 post, som "
      "gäller hela historiken.")],
)
def test_web_summary_a_lone_sale_entry_is_singular_and_has_no_since(lang, key, expected) -> None:
    """The first entry applies back to the start of the history whatever its date (the server
    keeps a lone dated entry as a list), so it reads "1 entry" without a "since"."""
    from custom_components.wolta.config_flow import _web_summary

    events = _GRID_BENEFIT_EVENTS if key == "grid_benefit_events" else _EXPORT_EVENTS
    text = _web_summary(lang, {key: [events[1]]})
    assert text.startswith(expected), text
    assert events[1]["date"] not in text


def test_web_summary_sale_history_comes_after_the_contract_and_reads_only_lists() -> None:
    from custom_components.wolta.config_flow import _web_summary

    text = _web_summary("en", {**_BRONAS_SERVER, **_SALE_SERVER})
    order = [text.index(s) for s in ("The grid fee has", "The grid benefit has 1 time period",
                                     "The contract history", "The grid benefit history",
                                     "The history of your retailer's terms")]
    assert order == sorted(order), text
    for bad in (None, [], "x", ["x"]):
        assert _web_summary("en", {"export_events": bad, "grid_benefit_events": bad}) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server", [_SALE_SERVER, {**_SALE_SERVER, "grid_benefit_periods": _ONE_PERIOD}],
    ids=["history", "history-and-periods"])
async def test_options_clearing_the_grid_benefit_under_its_history_is_refused_before_sending(
    hass: HomeAssistant, server
) -> None:
    """Today's entry would have no base price: the server refuses it (422, the history's own
    text, ahead of the periods' rule), so the form says it first, at the field and on base, and
    points to the history on wolta.se."""
    entry, mock_client, _ = _sale_entry(hass, server=server)
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        submitted = _opts(**_core(**_SALE))
        del submitted["tariffs"][CONF_GRID_BENEFIT_ORE]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=submitted)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_GRID_BENEFIT_ORE: "grid_benefit_history_requires_base",
                                "base": "grid_benefit_history_requires_base"}
    assert _section_options(result, "tariffs")["collapsed"] is False
    mock_client.patch_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_clearing_the_retailer_terms_under_their_history_is_sent(
    hass: HomeAssistant,
) -> None:
    """Only the grid benefit's history needs a base: cleared terms are plain spot from today."""
    entry, mock_client, _ = _sale_entry(hass, server={"export_events": _EXPORT_EVENTS})
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        submitted = _opts(**_core(**_SALE))
        del submitted["tariffs"][CONF_EXPORT_EXTRA_ORE]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=submitted)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert mock_client.patch_profile.call_args.kwargs == {
        CONF_GRID_BENEFIT_ORE: 10.4, CONF_EXPORT_EXTRA_ORE: 0.0}


@pytest.mark.asyncio
async def test_options_server_422_for_the_grid_benefit_history_shows_at_the_field_and_on_base(
    hass: HomeAssistant,
) -> None:
    """A snapshot without the list (a history added on wolta.se while the form was open): the
    server's refusal reads as the same fault, never cannot_connect."""
    from custom_components.wolta.api import WoltaApiError

    entry, mock_client, _ = _sale_entry(hass, server={})
    mock_client.patch_profile = AsyncMock(
        side_effect=WoltaApiError(_GRID_BENEFIT_EVENTS_DETAIL, status=422))
    with _options_patched(mock_client):
        result = await _settings_session(hass, entry)
        submitted = _opts(**_core(**_SALE))
        del submitted["tariffs"][CONF_GRID_BENEFIT_ORE]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=submitted)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_GRID_BENEFIT_ORE: "grid_benefit_history_requires_base",
                                "base": "grid_benefit_history_requires_base"}
    assert _section_options(result, "tariffs")["collapsed"] is False


@pytest.mark.parametrize(
    "detail, code",
    [
        (_GRID_BENEFIT_EVENTS_DETAIL, "grid_benefit_history_requires_base"),
        # Plain letters, as a server that does not escape would send them.
        ('HTTP 422 from x: {"detail":"en nätnyttepost behöver datum (inte i framtiden, inte två '
         'på samma dag) och ett baspris (grid_benefit_ore)"}', "grid_benefit_history_requires_base"),
        # The retailer's terms cannot meet their list's rule from here: a cleared value is plain
        # spot (ruling 10), so a scalar save never leaves today's entry without terms. The rule is
        # about the list, which this form never sends.
        (_EXPORT_EVENTS_DETAIL, None),
        # Another 422 that names the field (a type error) is not the rule.
        ('HTTP 422 from x: {"detail":"grid_benefit_ore must be a number"}', None),
    ],
    ids=["benefit-history", "benefit-history-plain", "export-history", "type-error"],
)
def test_tariff_rule_error_matches_the_sale_history_rule(detail: str, code) -> None:
    from custom_components.wolta.api import WoltaApiError
    from custom_components.wolta.config_flow import _tariff_rule_error, _tariff_rule_field

    assert _tariff_rule_error(WoltaApiError(detail, status=422)) == code
    assert _tariff_rule_error(WoltaApiError(detail, status=500)) is None
    if code is not None:
        assert _tariff_rule_field(code, {CONF_GRID_BENEFIT_ORE: None}) == CONF_GRID_BENEFIT_ORE


@pytest.mark.asyncio
async def test_reauth_never_sends_the_sale_history(hass: HomeAssistant) -> None:
    """Re-authentication carries today's grid benefit and terms from the cache, never a list."""
    mock_client = _mock_client("new-token")
    await _start_reauth(hass, {**_REAUTH_BASE, **_SALE}, mock_client)
    kwargs = mock_client.create_profile.call_args.kwargs
    assert not set(kwargs) & set(_SALE_LISTS)


# --- The never-released fields are gone (Global Constraints, "Tas bort helt"). ---

_REMOVED_NAMES = (
    "grid_var_peak_ore", "grid_benefit_peak_ore",
    "grid_fee_peak_start_hour", "grid_fee_peak_end_hour", "grid_fee_peak_holidays",
    "grid_benefit_peak_start_hour", "grid_benefit_peak_end_hour", "grid_benefit_peak_holidays",
    "retail_price_model_since", "grid_fee_peak", "grid_benefit_peak", "grid_peak",
    "peak_holidays", "peak_hours_invalid", "retail_since_invalid",
)


def test_the_removed_fields_and_texts_are_gone_from_the_integration() -> None:
    from pathlib import Path

    from custom_components.wolta import const

    root = Path(const.__file__).parent
    files = [*root.glob("*.py"), *root.glob("*.json"), *(root / "translations").glob("*.json")]
    assert len(files) > 10
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name in _REMOVED_NAMES:
            assert name not in text, (path.name, name)
    for attr in ("CONF_GRID_VAR_PEAK_ORE", "CONF_GRID_BENEFIT_PEAK_ORE",
                 "CONF_RETAIL_PRICE_MODEL_SINCE", "PEAK_HOLIDAY_RULES", "DEFAULT_PEAK_HOLIDAYS"):
        assert not hasattr(const, attr), attr


def test_create_profile_takes_no_removed_field_and_no_list() -> None:
    import inspect

    from custom_components.wolta.api import WoltaApiClient

    params = set(inspect.signature(WoltaApiClient.create_profile).parameters)
    assert not params & set(_REMOVED_NAMES)
    assert not params & set(_WEB_ONLY_FIELDS)
    assert {CONF_RETAIL_PRICE_MODEL, CONF_RETAIL_FIXED_ORE} <= params
