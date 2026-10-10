"""Täckningsavstämningen i coordinatorn (spec 2026-10-10 täckningsavstämning §5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.wolta import coverage
from custom_components.wolta.api import WoltaApiError, WoltaAuthError, WoltaRateLimitError
from custom_components.wolta.const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_SOC,
    CONF_SOLAR,
    CONF_TOKEN,
    CONF_VIEW_ONLY,
    CONF_ZONE,
    DOMAIN,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

TOKEN = "tok-coverage"
FP = coverage.coverage_token(TOKEN)
NOW = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
WINDOW_START = NOW - timedelta(days=365)
ENTRY_DATA = {
    CONF_TOKEN: TOKEN,
    CONF_ZONE: "SE3",
    CONF_BATT_IN: ["sensor.batt_in"],
    CONF_BATT_OUT: ["sensor.batt_out"],
    CONF_GRID_IN: ["sensor.grid_in"],
    CONF_GRID_OUT: ["sensor.grid_out"],
    CONF_SOLAR: ["sensor.solar"],
}
RESULTS = {"status": "done", "currency": "SEK",
           "period": {"start": "2026-08-04", "end": "2026-10-10", "n_days": 67},
           "job": {"status": "done", "step": None}, "betyg": None,
           "decision": None, "history": None}
PROFILE = {"zone": "SE3", "battery_kwh": None, "battery_kw": None, "eff": None,
           "reserve_pct": None, "cost_sek": None, "purchase_date": None,
           "grid_var_ore": None, "surcharge_ore": None, "export_extra_ore": None}


def _rows(start: datetime, n: int) -> list[dict]:
    return [{"ts": (start + timedelta(minutes=15 * i)).isoformat(),
             "batt_charged_kwh": 0.1, "batt_discharged_kwh": 0.0, "solar_kwh": 0.0,
             "grid_import_kwh": 0.2, "grid_export_kwh": 0.0} for i in range(n)]


def _since(dt: datetime | None) -> dict:
    return {"data_since": dt.isoformat() if dt is not None else None}


def _client(*, put=None, profile=None) -> MagicMock:
    """put: lista av svar/undantag för put_data i anropsordning. profile: get_profile-svar
    (None = omockad, som i test_coordinator.py – GET:en failar då tyst)."""
    client = MagicMock()
    client.put_data = AsyncMock(side_effect=list(put or []))
    client.results = AsyncMock(return_value=RESULTS)
    client.recompute = AsyncMock()
    if profile is not None:
        client.get_profile = AsyncMock(return_value=profile)
    return client


@pytest.fixture
def entry(hass: HomeAssistant):
    e = MagicMock(spec=ConfigEntry)
    e.entry_id = "coverage_entry"
    e.domain = DOMAIN
    e.data = dict(ENTRY_DATA)
    e.state = ConfigEntryState.SETUP_IN_PROGRESS
    e.unique_id = "coverage_unique"
    return e


async def _coordinator(hass, entry, client, state):
    from custom_components.wolta.coordinator import WoltaCoordinator

    c = WoltaCoordinator(hass, entry)
    c.client = client
    c._state = dict(state)
    c._store = MagicMock()
    c._store.async_save = AsyncMock()
    c._store.async_load = AsyncMock(return_value=dict(state))
    return c


def _state(bookmark: datetime | None = None, **extra) -> dict:
    """last_recompute = resultatets periodslut: utan tvångsflaggan räknar
    _maybe_recompute då INTE om (0 < 7 dygn) - ett recompute-anrop bevisar flaggan."""
    s = {"applied_invert": False, "applied_entities": None, "last_recompute": "2026-10-10"}
    if bookmark is not None:
        s["last_uploaded_ts"] = bookmark.isoformat()
    s.update(extra)
    return s


@pytest.mark.asyncio
async def test_put_rows_returns_the_response(hass, entry):
    client = _client(put=[{"data_since": "2026-08-04T00:00:00+00:00", "upserted": 2}])
    c = await _coordinator(hass, entry, client, _state())
    delivered, response = await c._put_rows(_rows(NOW, 2), [])
    assert delivered is True
    assert response == {"data_since": "2026-08-04T00:00:00+00:00", "upserted": 2}


@pytest.mark.asyncio
async def test_put_rows_422_fallback_returns_the_flow_only_response(hass, entry):
    entry.data = {**ENTRY_DATA, CONF_SOC: ["sensor.soc"]}
    client = _client(put=[WoltaApiError("bad soc", status=422), {"data_since": None}])
    c = await _coordinator(hass, entry, client, _state())
    soc = [{"unit": "sensor.soc", "ts": NOW.isoformat(), "period_s": 900,
            "soc_mean": 50.0, "soc_min": 49.0, "soc_max": 51.0}]
    delivered, response = await c._put_rows(_rows(NOW, 1), soc)
    assert delivered is False
    assert response == {"data_since": None}
