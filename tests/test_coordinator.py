"""Tests for custom_components/wolta/coordinator.py (TDD)."""

from __future__ import annotations

import dataclasses
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.wolta.api import (
    WoltaApiError,
    WoltaAuthError,
    WoltaRateLimitError,
)
from custom_components.wolta.const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_EXTERNAL_CONTROL,
    CONF_FLEX_COMPENSATION,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_SOC,
    CONF_SOLAR,
    CONF_TOKEN,
    CONF_ZONE,
    DOMAIN,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

TOKEN = "tok-test-abc"
ZONE = "SE3"

ENTRY_DATA = {
    CONF_TOKEN: TOKEN,
    CONF_ZONE: ZONE,
    CONF_BATT_IN: ["sensor.batt_in"],
    CONF_BATT_OUT: ["sensor.batt_out"],
    CONF_GRID_IN: ["sensor.grid_in"],
    CONF_GRID_OUT: ["sensor.grid_out"],
    CONF_SOLAR: ["sensor.solar"],
}

NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

RESULTS_PAYLOAD = {
    "status": "ready",
    "currency": "SEK",
    "period": {
        "start": "2025-01-01",
        "end": "2025-05-31",
        "n_days": 150,
    },
    "betyg": "A",
    "decision": "keep",
    "history": [],
}

RESULTS_PENDING = {
    "status": "running",
    "currency": "SEK",
    "period": {
        "start": "2025-01-01",
        "end": "2025-05-31",
        "n_days": 150,
    },
    "betyg": None,
    "decision": None,
    "history": [],
}

# Backend forces top-level status="done" as soon as a grade is cached (profile.py),
# even while the economy recompute is still running — the `job` object is then the only
# reliable "compute in flight" signal. Grade present, economy (decision/history) not yet.
RESULTS_DONE_JOB_RUNNING = {
    "status": "done",
    "currency": "SEK",
    "period": {
        "start": "2025-01-01",
        "end": "2025-05-31",
        "n_days": 150,
    },
    "job": {"status": "running", "step": None},
    "betyg": {"holistic": {"score_on": 0.67}},
    "decision": None,
    "history": None,
}

# A fully settled result: grade + economy present, job in a terminal state.
RESULTS_DONE_JOB_SETTLED = {
    "status": "done",
    "currency": "SEK",
    "period": {
        "start": "2025-01-01",
        "end": "2025-05-31",
        "n_days": 150,
    },
    "job": {"status": "done", "step": None},
    "betyg": {"holistic": {"score_on": 0.67}},
    "decision": {"irr": 0.1},
    "history": {"yearly": []},
}


def _make_rows(start: datetime, count: int = 4) -> list[dict]:
    """Produce minimal Wolta PUT rows for testing."""
    rows = []
    for i in range(count):
        ts = start + timedelta(minutes=15 * i)
        rows.append(
            {
                "ts": ts.isoformat(),
                "batt_charged_kwh": 0.1,
                "batt_discharged_kwh": 0.05,
                "solar_kwh": 0.2,
                "grid_import_kwh": 0.3,
                "grid_export_kwh": 0.1,
            }
        )
    return rows


def _mock_client(results=None, raise_on_put=None, raise_on_recompute=None):
    """Return a mock WoltaApiClient."""
    client = MagicMock()
    client.put_data = AsyncMock(side_effect=raise_on_put)
    client.results = AsyncMock(return_value=results or RESULTS_PAYLOAD)
    client.recompute = AsyncMock(side_effect=raise_on_recompute)
    client.delete = AsyncMock()
    client.patch_profile = AsyncMock(return_value={})
    return client


def _make_stats_response(
    statistic_id: str,
    rows: list,
) -> dict:
    """Wrap rows in stats_during_period dict format."""
    return {statistic_id: rows}


def _make_lts_row(unix_ts: float, change: float) -> dict:
    return {"start": unix_ts, "change": change}


def _make_short_row(unix_ts: float, change: float) -> dict:
    return {"start": unix_ts, "change": change}


@contextmanager
def _patch_aggregator(name: str, **kwargs):
    """Patch a coordinator aggregation function and register the mock's statistics
    period: _build_streams looks the period up by function identity (_PERIOD_S), so a
    replaced function must carry the period of the one it replaces."""
    from custom_components.wolta import coordinator as coord

    period = coord._PERIOD_S[getattr(coord, name)]
    with patch(f"custom_components.wolta.coordinator.{name}", **kwargs) as mock:
        with patch.dict(coord._PERIOD_S, {mock: period}):
            yield mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_entry(hass: HomeAssistant):
    """Create a mock ConfigEntry for testing."""
    from homeassistant.config_entries import ConfigEntry
    from unittest.mock import PropertyMock

    # The users live at 55-69 °N, and solar periods without statistics are known zeros
    # while the sun is down (spec 6.2). Pin a Swedish position, so that each test's
    # clock decides whether the sun is up: NOW (12:00 UTC on 1 June) is daylight here.
    hass.config.latitude = 59.3
    hass.config.longitude = 16.5
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.domain = DOMAIN
    entry.data = ENTRY_DATA.copy()
    entry.state = ConfigEntryState.SETUP_IN_PROGRESS
    entry.unique_id = "test_unique"
    return entry


async def _make_coordinator(hass: HomeAssistant, mock_entry, client, store_state=None):
    """Build a WoltaCoordinator with a pre-configured mock client and Store."""
    from custom_components.wolta.coordinator import WoltaCoordinator

    coordinator = WoltaCoordinator(hass, mock_entry)
    coordinator.client = client

    # Inject pre-loaded store state
    state = store_state if store_state is not None else {}
    coordinator._state = dict(state)
    coordinator._store = MagicMock()
    coordinator._store.async_save = AsyncMock()
    coordinator._store.async_load = AsyncMock(return_value=state)

    return coordinator


# ---------------------------------------------------------------------------
# Test: first run (no bookmark) → backfill path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_uses_backfill_path(hass: HomeAssistant, mock_entry):
    """No bookmark → backfills using LTS (split_hour_to_quarters) for old + 5-min for recent."""
    client = _mock_client()

    # LTS rows for the old window (12 months ago until ~9 days ago)
    lts_rows = [_make_lts_row((NOW - timedelta(days=30, hours=h)).timestamp(), 0.5) for h in range(24)]
    # Short-term rows for the recent window (last 9 days)
    short_rows = [_make_short_row((NOW - timedelta(days=3, minutes=m * 5)).timestamp(), 0.1) for m in range(12)]

    empty_stats = {
        "sensor.batt_in": [],
        "sensor.batt_out": [],
        "sensor.grid_in": [],
        "sensor.grid_out": [],
        "sensor.solar": [],
    }
    lts_stats = {
        "sensor.batt_in": lts_rows,
        "sensor.batt_out": lts_rows,
        "sensor.grid_in": lts_rows,
        "sensor.grid_out": lts_rows,
        "sensor.solar": lts_rows,
    }
    short_stats = {
        "sensor.batt_in": short_rows,
        "sensor.batt_out": short_rows,
        "sensor.grid_in": short_rows,
        "sensor.grid_out": short_rows,
        "sensor.solar": short_rows,
    }

    # Track calls by period argument
    fetch_calls = []

    async def mock_fetch(h, ids, start, end, period):
        fetch_calls.append(period)
        if period == "hour":
            return lts_stats
        if period == "5minute":
            return short_stats
        return empty_stats

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
        _patch_aggregator("split_hour_to_quarters") as mock_lts,
        _patch_aggregator("aggregate_5min_to_15min") as mock_5min,
        patch("custom_components.wolta.coordinator.merge_streams") as mock_merge,
    ):
        # Setup mock_lts/mock_5min/mock_merge to produce something
        mock_lts.return_value = {}
        mock_5min.return_value = {}
        mock_merge.return_value = _make_rows(NOW - timedelta(days=30))

        coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
        result = await coordinator._async_update_data()

    # Both LTS and 5-min paths must be called during backfill
    assert "hour" in fetch_calls, "LTS (hour) fetch must be called during backfill"
    assert "5minute" in fetch_calls, "5-min fetch must be called during backfill"
    # split_hour_to_quarters must be used for LTS data
    assert mock_lts.called, "split_hour_to_quarters must be called for LTS data"
    # aggregate_5min_to_15min must be used for recent data
    assert mock_5min.called, "aggregate_5min_to_15min must be called for short-term data"
    # Client put_data must be called since we have rows
    assert client.put_data.called, "put_data must be called when rows are available"
    # Bookmark must advance
    assert coordinator._state.get("last_uploaded_ts") is not None
    # Results fetched
    assert result.n_days == 150
    assert result.pending is False


# ---------------------------------------------------------------------------
# Test: second run (bookmark exists, recent) → incremental path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_run_since_bookmark(hass: HomeAssistant, mock_entry):
    """With recent bookmark → only 5-minute fetch since bookmark, no LTS."""
    client = _mock_client()
    bookmark_dt = NOW - timedelta(hours=2)
    bookmark_str = bookmark_dt.isoformat()

    short_rows = [_make_short_row((bookmark_dt + timedelta(minutes=m * 5)).timestamp(), 0.1) for m in range(4)]
    short_stats = {
        "sensor.batt_in": short_rows,
        "sensor.batt_out": short_rows,
        "sensor.grid_in": short_rows,
        "sensor.grid_out": short_rows,
        "sensor.solar": short_rows,
    }

    fetch_calls = []

    async def mock_fetch(h, ids, start, end, period):
        fetch_calls.append(period)
        return short_stats

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
        _patch_aggregator("split_hour_to_quarters") as mock_lts,
        _patch_aggregator("aggregate_5min_to_15min") as mock_5min,
        patch("custom_components.wolta.coordinator.merge_streams") as mock_merge,
    ):
        mock_lts.return_value = {}
        mock_5min.return_value = {}
        mock_merge.return_value = _make_rows(bookmark_dt)

        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        result = await coordinator._async_update_data()

    # Only 5-minute fetch in incremental mode
    assert "5minute" in fetch_calls
    assert "hour" not in fetch_calls, "LTS must NOT be fetched in incremental mode"
    assert mock_lts.called is False, "split_hour_to_quarters must NOT be called in incremental mode"
    assert client.put_data.called
    assert result.n_days == 150


# ---------------------------------------------------------------------------
# Test: recompute triggered when n_days advanced ≥ 7 since last_recompute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompute_triggered_after_7_new_days(hass: HomeAssistant, mock_entry):
    """≥7 new days since last_recompute → recompute() is called."""
    client = _mock_client()
    last_recompute_date = "2025-05-01"  # 31 days before NOW's period end (2025-05-31)
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={
                "last_uploaded_ts": bookmark_str,
                "last_recompute": last_recompute_date,
            }
        )
        await coordinator._async_update_data()

    assert client.recompute.called, "recompute() must be called after ≥7 new days"


# ---------------------------------------------------------------------------
# Test: recompute 429 (WoltaRateLimitError) is swallowed, last_recompute unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompute_rate_limit_swallowed(hass: HomeAssistant, mock_entry):
    """WoltaRateLimitError from recompute is swallowed; last_recompute NOT updated."""
    client = _mock_client(raise_on_recompute=WoltaRateLimitError(3600))
    last_recompute_date = "2025-05-01"
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()
    original_state = {
        "last_uploaded_ts": bookmark_str,
        "last_recompute": last_recompute_date,
    }

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state=dict(original_state)
        )
        # Must not raise
        result = await coordinator._async_update_data()

    assert client.recompute.called
    # last_recompute must NOT have been updated (still old value)
    assert coordinator._state.get("last_recompute") == last_recompute_date, \
        "last_recompute must not advance when recompute() raises WoltaRateLimitError"
    assert result is not None  # update succeeded despite rate-limit on recompute


# ---------------------------------------------------------------------------
# Test: WoltaAuthError → ConfigEntryAuthFailed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_error_raises_config_entry_auth_failed(hass: HomeAssistant, mock_entry):
    """WoltaAuthError from put_data → ConfigEntryAuthFailed."""
    client = _mock_client(raise_on_put=WoltaAuthError("token purged"))
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams",
              return_value=_make_rows(NOW - timedelta(hours=2))),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# Test: network error → UpdateFailed and bookmark NOT advanced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_error_update_failed_bookmark_unchanged(hass: HomeAssistant, mock_entry):
    """aiohttp.ClientError → UpdateFailed; bookmark must NOT advance."""
    import aiohttp

    client = _mock_client(raise_on_put=aiohttp.ClientError("connection refused"))
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams",
              return_value=_make_rows(NOW - timedelta(hours=2))),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    # Bookmark must remain at the original value
    assert coordinator._state.get("last_uploaded_ts") == bookmark_str, \
        "Bookmark must not advance on network error"


# ---------------------------------------------------------------------------
# Test: rate limit on upload → UpdateFailed carries retry_after kwarg (HA 2025.12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_on_put_sets_retry_after(hass: HomeAssistant, mock_entry):
    """WoltaRateLimitError from put_data → UpdateFailed(retry_after=<value>) so HA backs off."""
    client = _mock_client(raise_on_put=WoltaRateLimitError(1234))
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams",
              return_value=_make_rows(NOW - timedelta(hours=2))),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        with pytest.raises(UpdateFailed) as exc_info:
            await coordinator._async_update_data()

    # retry_after must be the kwarg (not just in the message) so HA schedules the backoff
    assert exc_info.value.retry_after == 1234
    # bookmark must not advance
    assert coordinator._state.get("last_uploaded_ts") == bookmark_str


# ---------------------------------------------------------------------------
# Test: timeout error → UpdateFailed and bookmark NOT advanced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_error_update_failed_bookmark_unchanged(hass: HomeAssistant, mock_entry):
    """TimeoutError → UpdateFailed; bookmark must NOT advance."""
    client = _mock_client(raise_on_put=TimeoutError("timed out"))
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams",
              return_value=_make_rows(NOW - timedelta(hours=2))),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator._state.get("last_uploaded_ts") == bookmark_str


# ---------------------------------------------------------------------------
# Test: pending status flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_flag_set_when_status_running(hass: HomeAssistant, mock_entry):
    """WoltaData.pending=True when server status is 'running' or 'pending'."""
    client = _mock_client(results=RESULTS_PENDING)
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        result = await coordinator._async_update_data()

    assert result.pending is True


@pytest.mark.asyncio
async def test_pending_flag_tracks_job_status_when_status_done(hass: HomeAssistant, mock_entry):
    """pending must track job.status even when top-level status is 'done'.

    The backend forces status='done' the moment a grade is cached, while a fresh economy
    recompute is still running (job.status='running', decision=None). If pending keyed only
    off the top-level status, the economy sensors would blank instead of holding their last
    value, and polling would drop to the 6 h slow interval — so the finished decision would
    not be picked up for hours. Regression for the options-flow value-change path.
    """
    client = _mock_client(results=RESULTS_DONE_JOB_RUNNING)
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        result = await coordinator._async_update_data()

    assert result.pending is True


@pytest.mark.asyncio
async def test_pending_flag_false_when_job_settled(hass: HomeAssistant, mock_entry):
    """A settled job (terminal status) must leave pending False so the coordinator returns
    to the slow poll and sensors expose the fresh values — guards against a naive fix that
    would treat the mere presence of a job object as 'pending'."""
    client = _mock_client(results=RESULTS_DONE_JOB_SETTLED)
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        result = await coordinator._async_update_data()

    assert result.pending is False


# ---------------------------------------------------------------------------
# Test: WoltaData structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wolta_data_structure(hass: HomeAssistant, mock_entry):
    """WoltaData has expected attributes after a successful update."""
    client = _mock_client()
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        result = await coordinator._async_update_data()

    assert hasattr(result, "results")
    assert hasattr(result, "last_uploaded")
    assert hasattr(result, "n_days")
    assert hasattr(result, "pending")
    # results är sensorernas vy, inte en råkopia: batteristämpeln hoistas in (spec
    # 2026-09-14 §7.2) eftersom value_fn bara ser results.
    assert result.results == {**RESULTS_PAYLOAD, "battery_status": None}
    assert result.n_days == 150
    assert result.pending is False


# ---------------------------------------------------------------------------
# Test: heal path (bookmark older than 9 days)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heal_path_used_when_bookmark_older_than_9_days(hass: HomeAssistant, mock_entry):
    """When bookmark is older than 9 days → LTS heal path is used."""
    client = _mock_client()
    old_bookmark = NOW - timedelta(days=15)
    bookmark_str = old_bookmark.isoformat()

    lts_rows = [_make_lts_row((old_bookmark + timedelta(hours=h)).timestamp(), 0.5) for h in range(10)]
    lts_stats = {
        "sensor.batt_in": lts_rows,
        "sensor.batt_out": lts_rows,
        "sensor.grid_in": lts_rows,
        "sensor.grid_out": lts_rows,
        "sensor.solar": lts_rows,
    }

    fetch_calls = []

    async def mock_fetch(h, ids, start, end, period):
        fetch_calls.append(period)
        if period == "hour":
            return lts_stats
        return {}

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
        _patch_aggregator("split_hour_to_quarters") as mock_lts,
        patch("custom_components.wolta.coordinator.merge_streams") as mock_merge,
    ):
        mock_lts.return_value = {}
        mock_merge.return_value = []

        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        await coordinator._async_update_data()

    assert "hour" in fetch_calls, "LTS (hour) must be used for heal path"
    assert mock_lts.called, "split_hour_to_quarters must be called for heal path"


# ---------------------------------------------------------------------------
# Test: async_trigger_recompute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_trigger_recompute_calls_client(hass: HomeAssistant, mock_entry):
    """async_trigger_recompute() calls client.recompute() with the token."""
    client = _mock_client()

    with patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client):
        coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
        await coordinator.async_trigger_recompute()

    assert client.recompute.called
    call_args = client.recompute.call_args
    assert call_args[0][0] == TOKEN or call_args.args[0] == TOKEN


@pytest.mark.asyncio
async def test_trigger_recompute_stamps_last_recompute_to_suppress_double_fire(
    hass: HomeAssistant, mock_entry
):
    """async_trigger_recompute stamps last_recompute against the current period end on
    success, so the coordinator refresh the button/options flow issues immediately after
    does not make _maybe_recompute fire a redundant SECOND recompute (the backend answers
    that with 429). The refresh's _maybe_recompute must therefore be a no-op."""
    from custom_components.wolta.coordinator import WoltaData

    client = _mock_client(results=RESULTS_PAYLOAD)  # period end 2025-05-31

    with patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client):
        coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
        # Simulate a prior successful refresh: coordinator.data holds the latest results.
        coordinator.data = WoltaData(
            results=RESULTS_PAYLOAD,
            last_uploaded=None,
            n_days=RESULTS_PAYLOAD["period"]["n_days"],
            pending=False,
        )

        await coordinator.async_trigger_recompute()
        assert coordinator._state.get("last_recompute") == "2025-05-31"

        # Follow-up _maybe_recompute (from the caller's refresh) must not recompute again.
        client.recompute.reset_mock()
        with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW):
            await coordinator._maybe_recompute(NOW)
        client.recompute.assert_not_called()


# ---------------------------------------------------------------------------
# Test: two solar sensors → solar_kwh equals sum of both
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_solar_sensors_summed(hass: HomeAssistant, mock_entry):
    """With two solar entity IDs, coordinator sums both per-bucket values."""
    two_solar_entry_data = {
        CONF_TOKEN: TOKEN,
        CONF_ZONE: ZONE,
        CONF_BATT_IN: ["sensor.batt_in"],
        CONF_BATT_OUT: ["sensor.batt_out"],
        CONF_GRID_IN: ["sensor.grid_in"],
        CONF_GRID_OUT: ["sensor.grid_out"],
        CONF_SOLAR: ["sensor.solar_a", "sensor.solar_b"],
    }
    mock_entry.data = two_solar_entry_data

    # async_fetch_change returns two solar entity IDs with different values. Every
    # sensor is measured over the whole read (from the start the mock receives), so
    # every quarter in the window is complete and uploaded.
    per_period = {
        "sensor.batt_in": 0.1,
        "sensor.batt_out": 0.0,
        "sensor.grid_in": 0.0,
        "sensor.grid_out": 0.0,
        "sensor.solar_a": 1.0,
        "sensor.solar_b": 2.0,
    }

    def short_stats(h, ids, start, end, period):
        starts = range(int(start.timestamp()), int(end.timestamp()), 300)
        return {sid: [{"start": float(t), "change": v} for t in starts]
                for sid, v in per_period.items()}

    client = _mock_client()

    captured_rows = []

    async def mock_put(token, rows):
        captured_rows.extend(rows)

    client.put_data = AsyncMock(side_effect=mock_put)

    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch(
            "custom_components.wolta.coordinator.async_fetch_change",
            side_effect=short_stats,
        ),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        await coordinator._async_update_data()

    bucket = NOW - timedelta(hours=1)
    matching = [r for r in captured_rows if r["ts"] == bucket.isoformat()]
    assert len(matching) == 1, "Expected one row for the bucket"
    # solar_kwh must be the sum over both inverters and the quarter's three periods:
    # 3 x (1.0 + 2.0) = 9.0
    assert matching[0]["solar_kwh"] == pytest.approx(9.0), (
        f"solar_kwh should be 9.0 (sum of both inverters), got {matching[0]['solar_kwh']}"
    )


# ---------------------------------------------------------------------------
# Test: backward compat — plain string in entry.data normalised to [str]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backward_compat_plain_string_solar(hass: HomeAssistant, mock_entry):
    """v0.1.0 entry with plain string solar value still works (normalised to list)."""
    # v0.1.0 format: plain strings, not lists
    old_entry_data = {
        CONF_TOKEN: TOKEN,
        CONF_ZONE: ZONE,
        CONF_BATT_IN: "sensor.batt_in",   # plain string
        CONF_BATT_OUT: "sensor.batt_out",
        CONF_GRID_IN: "sensor.grid_in",
        CONF_GRID_OUT: "sensor.grid_out",
        CONF_SOLAR: "sensor.solar",        # plain string
    }
    mock_entry.data = old_entry_data

    client = _mock_client()
    bookmark_str = (NOW - timedelta(hours=2)).isoformat()

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
    ):
        # Must not raise; coordinator must normalise plain strings to lists
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        # Check normalisation happened
        assert coordinator._entity_map["solar"] == ["sensor.solar"]
        assert coordinator._entity_map["batt_in"] == ["sensor.batt_in"]
        # Must complete without error
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# Dynamic polling: fast while a server-side job is pending, slow when done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_interval_fast_while_pending_slow_when_done(hass: HomeAssistant, mock_entry):
    """While /results reports a running job the coordinator polls fast (~60s) so the grade
    appears quickly; once done it returns to the slow 6h cadence."""
    from custom_components.wolta.coordinator import _FAST_POLL, _SLOW_POLL

    bookmark = (NOW - timedelta(hours=2)).isoformat()
    # last_recompute today → _maybe_recompute won't fire (< 7 days)
    store = {"last_uploaded_ts": bookmark, "last_recompute": NOW.date().isoformat()}

    async def _run(results):
        client = _mock_client(results=results)
        with (
            patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
            patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
            patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
            patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
            _patch_aggregator("aggregate_5min_to_15min", return_value={}),
        ):
            coordinator = await _make_coordinator(hass, mock_entry, client, store_state=dict(store))
            await coordinator._async_update_data()
            return coordinator.update_interval

    assert await _run(RESULTS_PENDING) == _FAST_POLL
    assert await _run(RESULTS_PAYLOAD) == _SLOW_POLL


# ---------------------------------------------------------------------------
# Battery-invert toggle (issue #1): swap charge/discharge + full re-backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effective_stream_identity_without_invert(hass: HomeAssistant, mock_entry):
    """Without the invert flag: stream names are unchanged."""
    client = _mock_client()
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
    assert coordinator._effective_stream("batt_in") == "batt_in"
    assert coordinator._effective_stream("batt_out") == "batt_out"
    assert coordinator._effective_stream("solar") == "solar"


@pytest.mark.asyncio
async def test_effective_stream_swaps_battery_when_inverted(hass: HomeAssistant, mock_entry):
    """Applied inversion covering now: batt_in reads the discharge sensor, batt_out the
    charge sensor. Grid/solar are untouched (only the battery direction is flipped)."""
    client = _mock_client()
    coordinator = await _make_coordinator(
        hass, mock_entry, client,
        store_state={"applied_invert_intervals": [[None, None]]})
    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW):
        assert coordinator._effective_stream("batt_in") == "batt_out"
        assert coordinator._effective_stream("batt_out") == "batt_in"
        assert coordinator._effective_stream("grid_in") == "grid_in"
        assert coordinator._effective_stream("solar") == "solar"


@pytest.mark.asyncio
async def test_effective_stream_answers_for_now_from_the_applied_intervals(
    hass: HomeAssistant, mock_entry
):
    """The texts name what feeds each role NOW: an applied interval that ended before
    now does not swap, one that started before now does - whatever the desired flag."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY, CONF_INVERT_INTERVALS

    t = (NOW - timedelta(hours=1)).isoformat()
    # Desired says "invert everything": it must not leak into the answer - only the
    # applied intervals (what the stored rows are built with) decide.
    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: True,
                       CONF_INVERT_INTERVALS: [[None, None]]}
    client = _mock_client()
    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW):
        ended = await _make_coordinator(
            hass, mock_entry, client, store_state={"applied_invert_intervals": [[None, t]]})
        assert ended._effective_stream("batt_in") == "batt_in"
        started = await _make_coordinator(
            hass, mock_entry, client, store_state={"applied_invert_intervals": [[t, None]]})
        assert started._effective_stream("batt_in") == "batt_out"


@pytest.mark.asyncio
async def test_stream_ids_never_swap(hass: HomeAssistant, mock_entry):
    """The streams are built as the sensors say; the swap happens per quarter later."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY, CONF_INVERT_INTERVALS

    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: True,
                       CONF_INVERT_INTERVALS: [[None, None]]}
    client = _mock_client()
    coordinator = await _make_coordinator(
        hass, mock_entry, client, store_state={"applied_invert_intervals": [[None, None]]})
    ids = coordinator._stream_ids()
    assert ids["batt_in"] == ["sensor.batt_in"]
    assert ids["batt_out"] == ["sensor.batt_out"]


@pytest.mark.asyncio
async def test_applied_and_desired_intervals_are_parsed(hass: HomeAssistant, mock_entry):
    from custom_components.wolta.const import CONF_INVERT_INTERVALS

    t = NOW - timedelta(hours=3)
    mock_entry.data = {**mock_entry.data, CONF_INVERT_INTERVALS: [[t.isoformat(), None]]}
    client = _mock_client()
    coordinator = await _make_coordinator(
        hass, mock_entry, client, store_state={"applied_invert_intervals": [[None, None]]})
    assert coordinator._desired_intervals() == [(t, None)]
    assert coordinator._applied_intervals() == [(None, None)]


async def _run_setup(hass, coordinator):
    """Run the coordinator's _async_setup with the entry write and the side-poll timer
    patched out; returns the async_update_entry mock."""
    with (
        patch.object(hass.config_entries, "async_update_entry") as mock_upd,
        patch("custom_components.wolta.coordinator.async_track_time_interval"),
    ):
        await coordinator._async_setup()
    return mock_upd


@pytest.mark.asyncio
@pytest.mark.parametrize(("flag", "expected"), [(True, [[None, None]]), (False, [])])
async def test_setup_migrates_the_invert_flag_to_intervals(
    hass: HomeAssistant, mock_entry, flag, expected
):
    """Migration: an entry without intervals gets them from the boolean flag, and the
    Store's applied intervals follow (no applied_invert either -> from the desired value)."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY, CONF_INVERT_INTERVALS

    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: flag}
    client = _mock_client()
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
    mock_upd = await _run_setup(hass, coordinator)

    mock_upd.assert_called_once()
    data = mock_upd.call_args.kwargs["data"]
    assert data[CONF_INVERT_INTERVALS] == expected
    assert data[CONF_INVERT_BATTERY] is flag              # mirrored, unchanged
    assert coordinator._state["applied_invert_intervals"] == expected
    coordinator._store.async_save.assert_awaited()


@pytest.mark.asyncio
async def test_setup_migration_without_any_flag_gives_empty_intervals(
    hass: HomeAssistant, mock_entry
):
    from custom_components.wolta.const import CONF_INVERT_BATTERY, CONF_INVERT_INTERVALS

    client = _mock_client()
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
    mock_upd = await _run_setup(hass, coordinator)
    data = mock_upd.call_args.kwargs["data"]
    assert data[CONF_INVERT_INTERVALS] == []
    assert data[CONF_INVERT_BATTERY] is False
    assert coordinator._state["applied_invert_intervals"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("applied_invert", "expected"),
                         [(True, [[None, None]]), (False, [])])
async def test_setup_derives_applied_intervals_from_applied_invert(
    hass: HomeAssistant, mock_entry, applied_invert, expected
):
    """Migration: the stored rows were built with applied_invert, not with whatever the
    entry now desires - the applied intervals come from it when present."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY, CONF_INVERT_INTERVALS

    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: not applied_invert,
                       CONF_INVERT_INTERVALS: [] if applied_invert else [[None, None]]}
    client = _mock_client()
    coordinator = await _make_coordinator(
        hass, mock_entry, client, store_state={"applied_invert": applied_invert})
    mock_upd = await _run_setup(hass, coordinator)

    mock_upd.assert_not_called()                          # entry already migrated
    assert coordinator._state["applied_invert_intervals"] == expected
    assert coordinator._state["applied_invert"] is applied_invert  # legacy key untouched


@pytest.mark.asyncio
async def test_setup_migration_is_a_no_op_once_done(hass: HomeAssistant, mock_entry):
    from custom_components.wolta.const import CONF_INVERT_INTERVALS

    t = (NOW - timedelta(hours=3)).isoformat()
    mock_entry.data = {**mock_entry.data, CONF_INVERT_INTERVALS: [[t, None]]}
    client = _mock_client()
    coordinator = await _make_coordinator(
        hass, mock_entry, client,
        store_state={"applied_invert": True, "applied_invert_intervals": [[None, t]]})
    mock_upd = await _run_setup(hass, coordinator)

    mock_upd.assert_not_called()
    coordinator._store.async_save.assert_not_awaited()
    assert coordinator._state["applied_invert_intervals"] == [[None, t]]


def _distinct_battery_stats(start, end) -> dict:
    """_energy_stats with the discharge sensor at 0.2 per period (charge stays 0.1), so a
    swapped row is told apart from an unswapped one."""
    stats = _energy_stats(start, end)
    stats["sensor.batt_out"] = [{**r, "change": 0.2} for r in stats["sensor.batt_out"]]
    return stats


@pytest.mark.asyncio
async def test_build_swaps_the_battery_per_quarter_from_the_applied_intervals(
    hass: HomeAssistant, mock_entry
):
    """Per-quarter inversion: with applied [[T, None]] the quarters before T carry
    charge/discharge as the sensors say and the quarters from T carry them swapped."""
    from custom_components.wolta.const import CONF_INVERT_INTERVALS

    t = NOW - timedelta(hours=3)
    mock_entry.data = {**mock_entry.data, CONF_INVERT_INTERVALS: []}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    async def fetch(h, ids, start, end, period):
        return _distinct_battery_stats(start, end)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - timedelta(hours=6)).isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint(),
                         "applied_invert_intervals": [[t.isoformat(), None]]})
        await coordinator._async_update_data()

    sent = [r for call in client.put_data.call_args_list for r in call.args[1]]
    before = [r for r in sent if datetime.fromisoformat(r["ts"]) < t]
    after = [r for r in sent if datetime.fromisoformat(r["ts"]) >= t]
    assert before and after
    for r in before:
        assert r["batt_charged_kwh"] == pytest.approx(0.3)
        assert r["batt_discharged_kwh"] == pytest.approx(0.6)
    for r in after:
        assert r["batt_charged_kwh"] == pytest.approx(0.6)
        assert r["batt_discharged_kwh"] == pytest.approx(0.3)
        assert r["grid_import_kwh"] == pytest.approx(0.3)   # nothing else moves


@pytest.mark.asyncio
async def test_build_ignores_the_desired_intervals(hass: HomeAssistant, mock_entry):
    """Until a replacement makes it applied, a desired inversion swaps nothing."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY, CONF_INVERT_INTERVALS

    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: True,
                       CONF_INVERT_INTERVALS: [[None, None]]}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    async def fetch(h, ids, start, end, period):
        return _distinct_battery_stats(start, end)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - timedelta(hours=6)).isoformat(),
                         "applied_invert": True, "applied_entities": _fingerprint(),
                         "applied_invert_intervals": []})
        await coordinator._async_update_data()

    sent = [r for call in client.put_data.call_args_list for r in call.args[1]]
    assert sent
    assert all(r["batt_charged_kwh"] == pytest.approx(0.3) for r in sent)
    assert all(r["batt_discharged_kwh"] == pytest.approx(0.6) for r in sent)


@pytest.mark.asyncio
async def test_complete_by_stream_judges_each_stream_by_its_own_sensors(
    hass: HomeAssistant, mock_entry
):
    """A solar period missing makes its quarter incomplete for solar (and overall) but
    not for grid_in; a stream without sensors is unbounded (None)."""
    from custom_components.wolta.coordinator import _Source
    from custom_components.wolta.stats import aggregate_5min_to_15min

    start = NOW - timedelta(hours=2)
    hole = NOW - timedelta(hours=1)            # the quarter [hole, hole + 15 min)
    raw = _energy_stats(start - timedelta(minutes=5), NOW)
    raw["sensor.solar"] = [r for r in raw["sensor.solar"]
                           if r["start"] != (hole + timedelta(minutes=5)).timestamp()]
    client = _mock_client()
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
    src = _Source(raw, aggregate_5min_to_15min, start, NOW)

    built = coordinator._build_streams(src)
    by_stream = built.complete_by_stream
    assert hole not in built.complete
    assert hole not in by_stream["solar"]
    assert hole in by_stream["grid_in"]
    assert hole in by_stream["batt_in"] and hole in by_stream["batt_out"]
    assert start in by_stream["solar"]
    assert by_stream["grid_in"] >= built.complete

    no_solar = coordinator._build_streams(
        src, ids={**coordinator._stream_ids(), "solar": []})
    assert no_solar.complete_by_stream["solar"] is None
    assert hole in no_solar.complete                    # solar no longer decides


@pytest.mark.asyncio
async def test_empty_stream_text_names_the_sensors_feeding_the_role_under_inversion(
    hass: HomeAssistant, mock_entry
):
    """The streams are built unswapped, the repair text answers for now: with the
    discharge sensor empty and the direction inverted, the charge role is the empty one
    and the text names the sensor that is actually silent."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.batt_out",), cycles=3,
        extra_state={"applied_invert_intervals": [[None, None]]})
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._energy_issue_id)
    assert issue is not None
    assert issue.translation_placeholders["streams"] == "batt_in: sensor.batt_out"


@pytest.mark.asyncio
async def test_unchanged_invert_keeps_incremental(hass: HomeAssistant, mock_entry):
    """Unchanged flag (applied_invert == current) → the bookmark is kept → incremental path
    (no LTS 'hour' window is fetched)."""
    client = _mock_client()
    fetch_calls: list[str] = []

    async def mock_fetch(h, ids, start, end, period):
        fetch_calls.append(period)
        return {}

    bookmark_str = (NOW - timedelta(days=1)).isoformat()
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str, "applied_invert": False},
        )
        await coordinator._async_update_data()

    assert "hour" not in fetch_calls, "unchanged flag should not force a backfill"
    assert coordinator._state.get("last_uploaded_ts") == bookmark_str


@pytest.mark.asyncio
async def test_force_recompute_flag_survives_rate_limit(hass: HomeAssistant, mock_entry):
    """If the forced recompute gets a 429, the force flag should be retained (retry on the next tick).
    The flag is set by a coverage fill or a sensor change; a direction change no longer sets it."""
    client = _mock_client(raise_on_recompute=WoltaRateLimitError(3600))

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
        _patch_aggregator("split_hour_to_quarters", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={
                "last_uploaded_ts": (NOW - timedelta(hours=2)).isoformat(),
                "last_recompute": "2025-05-29",
                "pending_invert_recompute": True,
            },
        )
        await coordinator._async_update_data()

    assert client.recompute.called
    assert coordinator._state.get("pending_invert_recompute") is True, \
        "the force flag should survive a 429 so the next tick retries"


# ---------------------------------------------------------------------------
# Delad profil-sync: GET /profile speglas in i entry.data (cache) + 413-repair
# ---------------------------------------------------------------------------

from homeassistant.helpers import issue_registry as ir  # noqa: E402

from custom_components.wolta.api import WoltaApiError  # noqa: E402

# Serverprofil som exakt matchar ENTRY_DATA (inga profilfält satta) → ingen ändring.
BASE_PROFILE = {
    "zone": ZONE, "battery_kwh": None, "battery_kw": None, "eff": None,
    "reserve_pct": None, "cost_sek": None, "purchase_date": None,
    "grid_var_ore": None, "surcharge_ore": None, "export_extra_ore": None,
}

_RECENT_BOOKMARK = (NOW - timedelta(hours=1)).isoformat()


async def _sync_refresh(hass, mock_entry, client, rows=None):
    """Kör en refresh på incremental-vägen med kontrollerade rows."""
    empty = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                             "sensor.grid_out", "sensor.solar")}

    async def mock_fetch(h, ids, start, end, period):
        return empty

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": _RECENT_BOOKMARK,
                         "applied_invert": False,
                         "applied_entities": None})
        if rows is not None:
            coordinator._incremental_rows = AsyncMock(return_value=rows)
        result = await coordinator._async_update_data()
    return coordinator, result


@pytest.mark.asyncio
async def test_profile_sync_updates_entry_data(hass: HomeAssistant, mock_entry):
    """Webbändring (battery_kwh satt på servern) ska uppdatera entry.data-cachen."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value={**BASE_PROFILE, "battery_kwh": 25.0})
    with patch.object(hass.config_entries, "async_update_entry") as mock_upd:
        await _sync_refresh(hass, mock_entry, client)
    assert mock_upd.called
    _, kwargs = mock_upd.call_args
    assert kwargs["data"]["battery_kwh"] == 25.0
    assert kwargs["title"] == f"Wolta ({ZONE})"


@pytest.mark.asyncio
async def test_profile_sync_mirrors_control_system(hass: HomeAssistant, mock_entry):
    """The control system is changed on wolta.se (a declared switch). entry.data must follow:
    reauth echoes entry.data, and an install-time value there used to rewrite the owner's
    latest timeline entry on the server (Wolta B4, 2026-10-20). The name follows its
    "other" and is dropped when the server has none."""
    mock_entry.data = {**mock_entry.data, "control_system": "other",
                       "control_system_name": "Egen styrning"}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value={
        **BASE_PROFILE, "control_system": "emhass", "control_system_name": None})
    with patch.object(hass.config_entries, "async_update_entry") as mock_upd:
        await _sync_refresh(hass, mock_entry, client)
    _, kwargs = mock_upd.call_args
    assert kwargs["data"]["control_system"] == "emhass"
    assert "control_system_name" not in kwargs["data"]


@pytest.mark.asyncio
async def test_profile_sync_mirrors_soc_absent_including_false(
    hass: HomeAssistant, mock_entry
):
    """soc_absent is server-owned (the owner flips it on wolta.se). A server False is a
    real answer ("has a sensor") and must be stored, not popped like a missing value;
    only None (never answered) drops the key."""
    from custom_components.wolta.const import CONF_SOC_ABSENT

    client = _mock_client()
    client.get_profile = AsyncMock(return_value={**BASE_PROFILE, "soc_absent": False})
    with patch.object(hass.config_entries, "async_update_entry") as mock_upd:
        await _sync_refresh(hass, mock_entry, client)
    _, kwargs = mock_upd.call_args
    assert kwargs["data"][CONF_SOC_ABSENT] is False

    mock_entry.data = {**mock_entry.data, CONF_SOC_ABSENT: True}
    client.get_profile = AsyncMock(return_value={**BASE_PROFILE, "soc_absent": None})
    with patch.object(hass.config_entries, "async_update_entry") as mock_upd:
        await _sync_refresh(hass, mock_entry, client)
    _, kwargs = mock_upd.call_args
    assert CONF_SOC_ABSENT not in kwargs["data"]


@pytest.mark.asyncio
async def test_profile_sync_zone_change_updates_title(hass: HomeAssistant, mock_entry):
    client = _mock_client()
    client.get_profile = AsyncMock(return_value={**BASE_PROFILE, "zone": "SE4"})
    with patch.object(hass.config_entries, "async_update_entry") as mock_upd:
        await _sync_refresh(hass, mock_entry, client)
    _, kwargs = mock_upd.call_args
    assert kwargs["data"]["zone"] == "SE4"
    assert kwargs["title"] == "Wolta (SE4)"


@pytest.mark.asyncio
async def test_profile_sync_no_write_when_unchanged(hass: HomeAssistant, mock_entry):
    """Oförändrad profil får inte trigga async_update_entry (diskskrivning per tick)."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    with patch.object(hass.config_entries, "async_update_entry") as mock_upd:
        await _sync_refresh(hass, mock_entry, client)
    mock_upd.assert_not_called()


@pytest.mark.asyncio
async def test_profile_sync_fetch_error_keeps_cache(hass: HomeAssistant, mock_entry):
    """GET-fel (nät/5xx) → behåll cachen, refreshen lyckas ändå."""
    client = _mock_client()
    client.get_profile = AsyncMock(side_effect=WoltaApiError("boom", status=500))
    with patch.object(hass.config_entries, "async_update_entry") as mock_upd:
        _, result = await _sync_refresh(hass, mock_entry, client)
    mock_upd.assert_not_called()
    assert result.n_days == 150


@pytest.mark.asyncio
async def test_put_413_raises_repair_issue_not_crash(hass: HomeAssistant, mock_entry):
    """413 på PUT /data → repair-issue profile_full; bookmark orörd; results hämtas."""
    client = _mock_client(raise_on_put=WoltaApiError("full", status=413))
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator, result = await _sync_refresh(
        hass, mock_entry, client, rows=_make_rows(NOW))
    assert result.n_days == 150
    assert coordinator._state["last_uploaded_ts"] == _RECENT_BOOKMARK  # ej avancerad
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"profile_full_{mock_entry.entry_id}")
    assert issue is not None and issue.translation_key == "profile_full"


@pytest.mark.asyncio
async def test_one_entrys_successful_upload_keeps_the_other_entrys_profile_full(
    hass: HomeAssistant, mock_entry
):
    """Bronäs runs two Wolta entries: entry B's 413 must survive entry A's good PUT
    (spec 2026-10-15 F1)."""
    from homeassistant.config_entries import ConfigEntry

    other = MagicMock(spec=ConfigEntry)
    other.entry_id = "other_entry_id"
    other.domain = DOMAIN
    other.data = ENTRY_DATA.copy()
    other.state = mock_entry.state
    other.unique_id = "other_unique"

    full = _mock_client(raise_on_put=WoltaApiError("full", status=413))
    full.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    await _sync_refresh(hass, other, full, rows=_make_rows(NOW))
    good = _mock_client()
    good.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    await _sync_refresh(hass, mock_entry, good, rows=_make_rows(NOW))

    reg = ir.async_get(hass)
    assert reg.async_get_issue(DOMAIN, "profile_full_other_entry_id") is not None


@pytest.mark.asyncio
async def test_entity_change_keeps_bookmark_and_queues_a_replacement(
    hass: HomeAssistant, mock_entry
):
    """Changed sensor selection (reconfigure): the bookmark stays and nothing is re-sent
    or force-recomputed; an entity job replaces the changed fields on the server
    (tests/test_coordinator_replacement.py). An unreadable fingerprint counts as every
    stream changed (those with a sensor now are replaced). Without data_since from the
    server the job waits."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    empty = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                             "sensor.grid_out", "sensor.solar")}

    async def mock_fetch(h, ids, start, end, period):
        return empty

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": _RECENT_BOOKMARK,
                         "applied_invert": False,
                         "applied_entities": "OLD-FINGERPRINT"})
        await coordinator._async_update_data()

    assert coordinator._state["last_uploaded_ts"] == _RECENT_BOOKMARK
    assert coordinator._state["applied_entities"] == "OLD-FINGERPRINT"
    assert "pending_invert_recompute" not in coordinator._state
    job = coordinator._state["replacement_job"]
    assert job["type"] == "entities"
    assert job["target"] == coordinator._entities_fingerprint()
    # external_control has no sensor chosen: a removed (or never chosen) one is not
    # replaced.
    assert job["fields"] == [
        "solar_kwh", "grid_import_kwh", "grid_export_kwh", "batt_charged_kwh",
        "batt_discharged_kwh"]


@pytest.mark.asyncio
async def test_entity_fingerprint_first_recording_keeps_bookmark(hass, mock_entry):
    """Befintlig installation som uppgraderar: fingerprint initieras utan bookmark-reset."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    empty = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                             "sensor.grid_out", "sensor.solar")}

    async def mock_fetch(h, ids, start, end, period):
        return empty

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": _RECENT_BOOKMARK, "applied_invert": False})
        await coordinator._async_update_data()

    assert coordinator._state["last_uploaded_ts"] == _RECENT_BOOKMARK
    assert "applied_entities" in coordinator._state


# ---------------------------------------------------------------------------
# External control (spec 2026-08-26): optional binary_sensor picker.
# Client-local key (same nature as CONF_INVERT_BATTERY) - the coordinator's
# fingerprint must only include it when a sensor is actually selected, so an
# upgrading fleet with no sensor configured never sees a fingerprint change.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_reads_external_control_entity(hass: HomeAssistant, mock_entry):
    """A configured external_control_entity is exposed as coordinator._external_entity."""
    from custom_components.wolta.coordinator import WoltaCoordinator

    mock_entry.data = {**ENTRY_DATA, CONF_EXTERNAL_CONTROL: "binary_sensor.grid_rewards_active"}
    coordinator = WoltaCoordinator(hass, mock_entry)
    assert coordinator._external_entity == "binary_sensor.grid_rewards_active"


@pytest.mark.asyncio
async def test_coordinator_external_control_entity_absent_is_none(
    hass: HomeAssistant, mock_entry
):
    """No key in entry.data (every pre-existing entry) -> None, not KeyError."""
    from custom_components.wolta.coordinator import WoltaCoordinator

    coordinator = WoltaCoordinator(hass, mock_entry)
    assert coordinator._external_entity is None


@pytest.mark.asyncio
async def test_coordinator_external_control_empty_string_is_none(
    hass: HomeAssistant, mock_entry
):
    """An empty string (cleared field) normalises to None, same as an absent key."""
    from custom_components.wolta.coordinator import WoltaCoordinator

    mock_entry.data = {**ENTRY_DATA, CONF_EXTERNAL_CONTROL: ""}
    coordinator = WoltaCoordinator(hass, mock_entry)
    assert coordinator._external_entity is None


@pytest.mark.asyncio
async def test_entity_fingerprint_changes_when_external_control_added(
    hass: HomeAssistant, mock_entry
):
    """Selecting an external-control sensor changes the fingerprint like any other
    entity selection: an entity job replaces the external_control field, and the
    bookmark stays."""
    import json as _json

    mock_entry.data = {**ENTRY_DATA, CONF_EXTERNAL_CONTROL: "binary_sensor.grid_rewards_active"}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    empty = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                             "sensor.grid_out", "sensor.solar")}

    async def mock_fetch(h, ids, start, end, period):
        return empty

    # Fingerprint format from before this feature existed - no external_control key.
    pre_existing_fingerprint = _json.dumps({
        "batt_in": ["sensor.batt_in"], "batt_out": ["sensor.batt_out"],
        "grid_in": ["sensor.grid_in"], "grid_out": ["sensor.grid_out"],
        "solar": ["sensor.solar"],
    }, sort_keys=True)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": _RECENT_BOOKMARK,
                         "applied_invert": False,
                         "applied_entities": pre_existing_fingerprint})
        await coordinator._async_update_data()

    assert coordinator._state["last_uploaded_ts"] == _RECENT_BOOKMARK
    assert coordinator._state["applied_entities"] == pre_existing_fingerprint
    job = coordinator._state["replacement_job"]
    assert job["target"] != pre_existing_fingerprint
    assert job["fields"] == ["external_control"]


@pytest.mark.asyncio
async def test_entity_fingerprint_unchanged_on_upgrade_without_external_control(
    hass: HomeAssistant, mock_entry
):
    """An existing installation with NO external_control sensor configured must not
    see its fingerprint change on upgrade - the key is omitted entirely when unset,
    otherwise every installed integration would re-backfill a year of history the
    moment it updates."""
    import json as _json

    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    empty = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                             "sensor.grid_out", "sensor.solar")}

    async def mock_fetch(h, ids, start, end, period):
        return empty

    pre_upgrade_fingerprint = _json.dumps({
        "batt_in": ["sensor.batt_in"], "batt_out": ["sensor.batt_out"],
        "grid_in": ["sensor.grid_in"], "grid_out": ["sensor.grid_out"],
        "solar": ["sensor.solar"],
    }, sort_keys=True)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
    ):
        # mock_entry.data is ENTRY_DATA (no CONF_EXTERNAL_CONTROL key) - the upgrade case.
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": _RECENT_BOOKMARK,
                         "applied_invert": False,
                         "applied_entities": pre_upgrade_fingerprint})
        await coordinator._async_update_data()

    assert coordinator._state.get("last_uploaded_ts") == _RECENT_BOOKMARK
    assert coordinator._state["applied_entities"] == pre_upgrade_fingerprint


@pytest.mark.asyncio
async def test_profile_sync_skipped_during_fast_poll(hass: HomeAssistant, mock_entry):
    """Under fast-poll (60 s medan server-jobb pågår) hoppas profil-syncen över."""
    from custom_components.wolta.coordinator import _FAST_POLL

    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    empty = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                             "sensor.grid_out", "sensor.solar")}

    async def mock_fetch(h, ids, start, end, period):
        return empty

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": _RECENT_BOOKMARK,
                         "applied_invert": False,
                         "applied_entities": None})
        coordinator.update_interval = _FAST_POLL
        await coordinator._async_update_data()

    client.get_profile.assert_not_awaited()


# ---------------------------------------------------------------------------
# Sidopoll (v0.11.0): webbändringar ska synas i HA inom minuter, inte timmar
# ---------------------------------------------------------------------------


async def _side_poll_coordinator(hass, mock_entry, client):
    coordinator = await _make_coordinator(
        hass, mock_entry, client,
        store_state={"last_uploaded_ts": _RECENT_BOOKMARK,
                     "applied_invert": False, "applied_entities": None})
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


@pytest.mark.asyncio
async def test_side_poll_applies_change_and_refreshes(hass: HomeAssistant, mock_entry):
    """Webbändring upptäckt av sidopollen → entry.data speglas + full refresh triggas."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value={**BASE_PROFILE, "battery_kwh": 25.0})
    coordinator = await _side_poll_coordinator(hass, mock_entry, client)
    with patch.object(hass.config_entries, "async_update_entry") as mock_upd:
        await coordinator.async_check_profile_sync()
    assert mock_upd.called
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_side_poll_no_change_no_refresh(hass: HomeAssistant, mock_entry):
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _side_poll_coordinator(hass, mock_entry, client)
    with patch.object(hass.config_entries, "async_update_entry") as mock_upd:
        await coordinator.async_check_profile_sync()
    mock_upd.assert_not_called()
    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_side_poll_skipped_during_fast_poll(hass: HomeAssistant, mock_entry):
    """Under fast-poll refreshar huvudcykeln redan varje minut – sidopollen vilar."""
    from custom_components.wolta.coordinator import _FAST_POLL

    client = _mock_client()
    client.get_profile = AsyncMock(return_value={**BASE_PROFILE, "battery_kwh": 25.0})
    coordinator = await _side_poll_coordinator(hass, mock_entry, client)
    coordinator.update_interval = _FAST_POLL
    await coordinator.async_check_profile_sync()
    client.get_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_side_poll_swallows_errors(hass: HomeAssistant, mock_entry):
    """Nätfel i sidopollen får aldrig bubbla (timer-callback) – huvudcykeln tar auth."""
    client = _mock_client()
    client.get_profile = AsyncMock(side_effect=WoltaAuthError("purged"))
    coordinator = await _side_poll_coordinator(hass, mock_entry, client)
    await coordinator.async_check_profile_sync()  # får inte kasta
    coordinator.async_request_refresh.assert_not_awaited()


# ---------------------------------------------------------------------------
# v0.11.1: 429-backoff – respektera Retry-After istället för försök varje poll
# ---------------------------------------------------------------------------


async def _cadence_refresh(hass, mock_entry, client, extra_state=None):
    """Refresh på incremental-vägen med 8 nya dygn sedan last_recompute (kadens-träff)."""
    empty = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                             "sensor.grid_out", "sensor.solar")}

    async def mock_fetch(h, ids, start, end, period):
        return empty

    state = {"last_uploaded_ts": _RECENT_BOOKMARK, "applied_invert": False,
             "applied_entities": None, "last_recompute": "2025-05-23"}
    if extra_state:
        state.update(extra_state)
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
    ):
        coordinator = await _make_coordinator(hass, mock_entry, client, store_state=state)
        await coordinator._async_update_data()
    return coordinator


@pytest.mark.asyncio
async def test_recompute_429_sets_backoff(hass: HomeAssistant, mock_entry):
    """429 på recompute → recompute_blocked_until = nu + Retry-After sparas i staten."""
    client = _mock_client(raise_on_recompute=WoltaRateLimitError(retry_after=7200))
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _cadence_refresh(hass, mock_entry, client)
    client.recompute.assert_awaited_once()
    blocked = datetime.fromisoformat(coordinator._state["recompute_blocked_until"])
    assert blocked == NOW + timedelta(seconds=7200)


@pytest.mark.asyncio
async def test_recompute_skipped_while_backoff_active(hass: HomeAssistant, mock_entry):
    """Aktiv backoff → inget recompute-försök alls (bruset borta)."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _cadence_refresh(
        hass, mock_entry, client,
        extra_state={"recompute_blocked_until": (NOW + timedelta(hours=3)).isoformat()})
    client.recompute.assert_not_awaited()
    assert "recompute_blocked_until" in coordinator._state  # kvar tills den passerats


@pytest.mark.asyncio
async def test_recompute_resumes_after_backoff_expired(hass: HomeAssistant, mock_entry):
    """Passerad backoff → nyckeln rensas och recompute försöker igen."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _cadence_refresh(
        hass, mock_entry, client,
        extra_state={"recompute_blocked_until": (NOW - timedelta(minutes=1)).isoformat()})
    client.recompute.assert_awaited_once()
    assert "recompute_blocked_until" not in coordinator._state


@pytest.mark.asyncio
async def test_trigger_recompute_success_clears_backoff(hass: HomeAssistant, mock_entry):
    """Användarinitierad recompute (options/knapp) som lyckas rensar backoffen."""
    client = _mock_client()
    coordinator = await _make_coordinator(
        hass, mock_entry, client,
        store_state={"recompute_blocked_until": (NOW + timedelta(hours=3)).isoformat()})
    coordinator.data = MagicMock(results={"period": {"end": "2025-05-31"}})
    await coordinator.async_trigger_recompute()
    assert "recompute_blocked_until" not in coordinator._state


# ---------------------------------------------------------------------------
# Measured-capacity adopt gate (_evaluate_measured_params)
# ---------------------------------------------------------------------------

from custom_components.wolta.const import (  # noqa: E402
    CONF_BATTERY_KWH as _CBK,
    CONF_RESERVE_PCT as _CRP,
)

_CAP_ISSUE_ID = "measured_capacity_test_entry_id"


def _oc_results(kwh, *, n_days=90, plateau=20):
    return {"betyg": {"observed_capacity": {
        "kwh": kwh, "n_days": n_days, "plateau_days": plateau}}}


async def _cap_coordinator(hass, mock_entry, **entry_over):
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())
    mock_entry.data = {**ENTRY_DATA, **entry_over}
    return coordinator


@pytest.mark.asyncio
async def test_capacity_issue_fires_on_mature_gap(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry, **{_CBK: 15.0})
    c._evaluate_measured_params(_oc_results(11.0))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID)
    assert issue is not None
    assert issue.data["measured_kwh"] == 11.0
    assert issue.data["entry_id"] == "test_entry_id"


@pytest.mark.asyncio
async def test_capacity_issue_not_fired_when_immature(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry, **{_CBK: 15.0})
    c._evaluate_measured_params(_oc_results(11.0, n_days=40))   # < 60 dygn
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_capacity_issue_not_fired_weak_plateau(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry, **{_CBK: 15.0})
    c._evaluate_measured_params(_oc_results(11.0, plateau=5))   # < 10 platå-dygn
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_capacity_issue_not_fired_within_tolerance(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry, **{_CBK: 11.5})
    c._evaluate_measured_params(_oc_results(11.0))              # gap ~4 % < 15 %
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_capacity_issue_accounts_for_reserve(hass, mock_entry):
    """Reserv-bryggan: 13 kWh full + 10 % reserv → effektivt 11,7; uppmätt 11,7 → matchar,
    ingen issue (bevisar att reserven vägs in, inte dubbelräknas)."""
    c = await _cap_coordinator(hass, mock_entry, **{_CBK: 13.0, _CRP: 10.0})
    c._evaluate_measured_params(_oc_results(11.7))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_capacity_issue_cleared_when_no_observed(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry, **{_CBK: 15.0})
    c._evaluate_measured_params(_oc_results(11.0))             # skapa
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is not None
    c._evaluate_measured_params({"betyg": None})              # ingen oc → rensa
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_capacity_issue_suppressed_when_low_efficiency_and_up(hass, mock_entry):
    """Issue #1 (Emaldo/Leif): vid låg round-trip-verkningsgrad är observed_capacity uppblåst
    (0.95-laddantagandet stämmer inte, parasitförluster) → erbjud INTE att HÖJA kapaciteten
    till det opålitliga värdet, trots att glappet passerar 15 %."""
    from custom_components.wolta.const import CONF_BATTERY_KWH as _K

    c = await _cap_coordinator(hass, mock_entry, **{_K: 13.0})
    results = {"betyg": {
        "observed_capacity": {"kwh": 15.75, "n_days": 90, "plateau_days": 20},
        "observed_eff": {"eff": 0.70, "n_days": 90}}}
    c._evaluate_measured_params(results)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_capacity_issue_fires_down_even_when_low_efficiency(hass, mock_entry):
    """Låg verkningsgrad tystar bara UPP-förslag. Ett NER-förslag är fortfarande giltigt:
    är det uppblåsta uppmätta ÄNDÅ lägre än konfigurerat är verklig användbar ännu lägre."""
    from custom_components.wolta.const import CONF_BATTERY_KWH as _K

    c = await _cap_coordinator(hass, mock_entry, **{_K: 20.0})
    results = {"betyg": {
        "observed_capacity": {"kwh": 15.0, "n_days": 90, "plateau_days": 20},
        "observed_eff": {"eff": 0.70, "n_days": 90}}}
    c._evaluate_measured_params(results)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_capacity_issue_fires_up_when_efficiency_ok(hass, mock_entry):
    """God verkningsgrad → observed_capacity är pålitligt → HÖJ-förslag går fram som vanligt
    (grinden får inte tysta legitima adopts för normala batterier)."""
    from custom_components.wolta.const import CONF_BATTERY_KWH as _K

    c = await _cap_coordinator(hass, mock_entry, **{_K: 11.0})
    results = {"betyg": {
        "observed_capacity": {"kwh": 14.0, "n_days": 90, "plateau_days": 20},
        "observed_eff": {"eff": 0.90, "n_days": 90}}}
    c._evaluate_measured_params(results)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is not None


# ---------------------------------------------------------------------------
# Measured-power and measured-efficiency adopt gates
# ---------------------------------------------------------------------------

from custom_components.wolta.const import (  # noqa: E402
    CONF_BATTERY_KW as _CBKW,
    CONF_EFF as _CEFF,
)

_POWER_ISSUE_ID = "measured_power_test_entry_id"
_EFF_ISSUE_ID = "measured_efficiency_test_entry_id"


def _op_results(kw, *, n_days=90):
    return {"betyg": {"observed_power": {"kw": kw, "n_days": n_days}}}


def _oe_results(eff, *, n_days=90):
    return {"betyg": {"observed_eff": {"eff": eff, "n_days": n_days}}}


@pytest.mark.asyncio
async def test_power_issue_fires_on_gap(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry, **{_CBKW: 10.0})
    c._evaluate_measured_params(_op_results(3.6))            # nameplate 10 vs uppmätt 3.6
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _POWER_ISSUE_ID)
    assert issue is not None
    assert issue.data["measured_kw"] == 3.6


@pytest.mark.asyncio
async def test_power_issue_not_fired_within_tolerance(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry, **{_CBKW: 3.7})
    c._evaluate_measured_params(_op_results(3.6))            # ~3 % gap
    assert ir.async_get(hass).async_get_issue(DOMAIN, _POWER_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_power_issue_not_fired_when_immature(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry, **{_CBKW: 10.0})
    c._evaluate_measured_params(_op_results(3.6, n_days=40))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _POWER_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_efficiency_issue_fires_on_gap(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry, **{_CEFF: 0.9})
    c._evaluate_measured_params(_oe_results(0.72))           # 0.9 vs 0.72 → gap 0.18
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _EFF_ISSUE_ID)
    assert issue is not None
    assert issue.data["measured_eff"] == 0.72


@pytest.mark.asyncio
async def test_efficiency_issue_not_fired_within_tolerance(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry, **{_CEFF: 0.9})
    c._evaluate_measured_params(_oe_results(0.88))           # gap 0.02 < 0.08
    assert ir.async_get(hass).async_get_issue(DOMAIN, _EFF_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_efficiency_issue_not_fired_at_implausible_ceiling(hass, mock_entry):
    """Review #3: en uppmätt eff över 0.98 är en clamp-/randartefakt, inte ett riktigt
    round-trip – ska inte erbjudas för adoption."""
    c = await _cap_coordinator(hass, mock_entry, **{_CEFF: 0.85})
    c._evaluate_measured_params(_oe_results(1.0))          # gap 0.15 men 1.0 > 0.98
    assert ir.async_get(hass).async_get_issue(DOMAIN, _EFF_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_efficiency_issue_fires_at_plausible_ceiling(hass, mock_entry):
    """Taket ingår, som i serverns observed_eff (spec 2026-10-13 §4): 0.98 är en giltig
    mätning (hybrid som mäter batteriet på DC-sidan) och ska kunna adopteras."""
    c = await _cap_coordinator(hass, mock_entry, **{_CEFF: 0.85})
    c._evaluate_measured_params(_oe_results(0.98))         # gap 0.13, 0.98 ≤ 0.98
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _EFF_ISSUE_ID)
    assert issue is not None
    assert issue.data["measured_eff"] == 0.98


@pytest.mark.asyncio
async def test_power_issue_shrink_needs_larger_gap(hass, mock_entry):
    """Review #4: krymp-riktningen (uppmätt < inmatat) kräver större gap – ett korrekt satt
    men snällt använt batteri (5 kW, topp 3.6 → 28 %) ska INTE nudgas ned."""
    c = await _cap_coordinator(hass, mock_entry, **{_CBKW: 5.0})
    c._evaluate_measured_params(_op_results(3.6))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _POWER_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_power_issue_raise_fires_on_small_gap(hass, mock_entry):
    """Höj-riktningen (uppmätt > inmatat) är högkonfident → mindre gap räcker (3.0 → 3.6 = 20 %)."""
    c = await _cap_coordinator(hass, mock_entry, **{_CBKW: 3.0})
    c._evaluate_measured_params(_op_results(3.6))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _POWER_ISSUE_ID) is not None


from custom_components.wolta.const import (  # noqa: E402
    CONF_NAMEPLATE_KW as _CNPKW,
    CONF_POWER_ISSUE_IGNORED as _CPII,
)


@pytest.mark.asyncio
async def test_power_issue_suppressed_above_physical_ceiling(hass, mock_entry):
    """Jans fall: 9.9 kW-batteri, uppmätt 27.5 kW (sensor-hopp i kumulativ HA-statistik som
    observed_power inte kan skilja från äkta effekt). 27.5 > 9.9×1.5 = 14.85 → fysiskt omöjligt
    → artefakt, inte en underdeklaration → höj-förslaget tystas (kcommer inte tillbaka)."""
    c = await _cap_coordinator(hass, mock_entry, **{_CBKW: 9.9})
    c._evaluate_measured_params(_op_results(27.5))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _POWER_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_power_issue_still_fires_within_physical_ceiling(hass, mock_entry):
    """En äkta måttlig underdeklaration (satte 9.9, batteriet gör 12 = 1.21× < 1.5×) är fysiskt
    rimlig → höj-nudgen ska fortfarande komma (grinden får inte översuppa)."""
    c = await _cap_coordinator(hass, mock_entry, **{_CBKW: 9.9})
    c._evaluate_measured_params(_op_results(12.0))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _POWER_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_power_ceiling_uses_nameplate_when_higher(hass, mock_entry):
    """Fysiktaket refererar märkeffekten när den är satt och högre: batteri 9.9 men märkt 20 kW
    → tak = max(20, 9.9)×1.5 = 30 → uppmätt 25 (över 9.9-referensen men under märk-taket) ska
    fortfarande nudga upp, inte tystas."""
    c = await _cap_coordinator(hass, mock_entry, **{_CBKW: 9.9, _CNPKW: 20.0})
    c._evaluate_measured_params(_op_results(25.0))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _POWER_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_power_issue_suppressed_when_ignored(hass, mock_entry):
    """Användaren har tryckt 'ignorera' → flaggan tystar en annars eldande nudge (3.0 vs 3.6)."""
    c = await _cap_coordinator(hass, mock_entry, **{_CBKW: 3.0, _CPII: True})
    c._evaluate_measured_params(_op_results(3.6))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _POWER_ISSUE_ID) is None


from custom_components.wolta.const import (  # noqa: E402
    CONF_CAPACITY_ISSUE_IGNORED as _CCII,
    CONF_EFFICIENCY_ISSUE_IGNORED as _CEII,
)


@pytest.mark.asyncio
async def test_capacity_issue_suppressed_when_ignored(hass, mock_entry):
    """Ignorera-flaggan tystar kapacitetsnudgen (15 vs uppmätt 11 skulle annars elda)."""
    c = await _cap_coordinator(hass, mock_entry, **{_CBK: 15.0, _CCII: True})
    c._evaluate_measured_params(_oc_results(11.0))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_efficiency_issue_suppressed_when_ignored(hass, mock_entry):
    """Jans fall: ignorera-flaggan tystar verkningsgradsnudgen (0.9 vs uppmätt 0.76)."""
    c = await _cap_coordinator(hass, mock_entry, **{_CEFF: 0.9, _CEII: True})
    c._evaluate_measured_params(_oe_results(0.76))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _EFF_ISSUE_ID) is None


# ---------------------------------------------------------------------------
# View-only entries (bound plants): never upload, never recompute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_view_only_never_uploads(hass: HomeAssistant, mock_entry):
    """A view-only entry polls results but must never touch the streaming machinery:
    no stats read, no PUT /data (the server 409s it for bound plants - but the client
    must not even try), no recompute cadence. The plant's own binding (Sonnen webhook /
    Reduxi) owns the data."""
    from custom_components.wolta.const import CONF_VIEW_ONLY

    mock_entry.data = {CONF_TOKEN: TOKEN, CONF_ZONE: ZONE, CONF_VIEW_ONLY: True}
    client = _mock_client(results=RESULTS_DONE_JOB_SETTLED)
    client.get_profile = AsyncMock(side_effect=Exception("sync skipped in test"))
    coordinator = await _make_coordinator(hass, mock_entry, client)

    with patch(
        "custom_components.wolta.stats.async_fetch_change",
        new=AsyncMock(side_effect=AssertionError("view-only far inte lasa statistik")),
    ):
        data = await coordinator._async_update_data()

    client.put_data.assert_not_awaited()
    client.recompute.assert_not_awaited()
    client.results.assert_awaited_once()
    assert data.results == {**RESULTS_DONE_JOB_SETTLED, "battery_status": None}
    assert data.last_uploaded is None


@pytest.mark.asyncio
async def test_view_only_network_failure_skips_upload_repair_tracking(hass: HomeAssistant,
                                                                      mock_entry):
    """Nätfel i visningsläge får inte mata upload-failure-repairen: dess text lovar
    "unable to upload energy data" och en view-only-entry laddar aldrig upp. Felet syns
    ändå (UpdateFailed → entiteterna unavailable)."""
    import aiohttp

    from custom_components.wolta.const import CONF_VIEW_ONLY

    mock_entry.data = {CONF_TOKEN: TOKEN, CONF_ZONE: ZONE, CONF_VIEW_ONLY: True}
    client = _mock_client()
    client.get_profile = AsyncMock(side_effect=Exception("sync skipped in test"))
    client.results = AsyncMock(side_effect=aiohttp.ClientError("boom"))
    coordinator = await _make_coordinator(hass, mock_entry, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert "failure_since" not in coordinator._state, \
        "view-only ska inte räkna mot upload-failure-repairen"


# Same state as _sync_refresh: a bookmark and the applied settings, so no correction job
# is queued and the cycle only does what the test stubs.
_QUIET_STATE = {"last_uploaded_ts": _RECENT_BOOKMARK, "applied_invert": False,
                "applied_entities": None}


async def _failure_cycle(coordinator, client, when: datetime, *, fail: bool) -> None:
    """One cycle at ``when``: the upload itself is stubbed (nothing to send); results
    raises a network error when ``fail``."""
    import aiohttp

    coordinator._upload_regular = AsyncMock(return_value=(None, False))
    coordinator._reconcile_coverage = AsyncMock()
    client.results = AsyncMock(
        side_effect=aiohttp.ClientError("down") if fail else None,
        return_value=RESULTS_PAYLOAD)
    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=when):
        if fail:
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
        else:
            await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_a_good_cycle_ends_the_failure_streak(hass: HomeAssistant, mock_entry):
    """One network failure, a good cycle, then another failure ten days later: no
    "more than 7 days" repair - the good cycle ended the first streak (spec F2)."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state=_QUIET_STATE)
    await _failure_cycle(coordinator, client, NOW, fail=True)
    assert coordinator._state["failure_since"] == NOW.isoformat()
    coordinator._store.async_save.assert_awaited()
    await _failure_cycle(coordinator, client, NOW + timedelta(hours=1), fail=False)
    assert "failure_since" not in coordinator._state
    later = NOW + timedelta(days=10)
    await _failure_cycle(coordinator, client, later, fail=True)
    assert coordinator._state["failure_since"] == later.isoformat()
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._upload_failure_issue_id) is None


@pytest.mark.asyncio
async def test_failing_for_more_than_seven_days_raises_upload_failure(
    hass: HomeAssistant, mock_entry
):
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state=_QUIET_STATE)
    await _failure_cycle(coordinator, client, NOW, fail=True)
    await _failure_cycle(coordinator, client, NOW + timedelta(days=9), fail=True)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"upload_failure_{mock_entry.entry_id}")
    assert issue is not None and issue.translation_key == "upload_failure"
    await _failure_cycle(coordinator, client, NOW + timedelta(days=9, hours=1), fail=False)
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"upload_failure_{mock_entry.entry_id}") is None


@pytest.mark.asyncio
async def test_view_only_with_soc_sensors_never_reads_soc(hass: HomeAssistant, mock_entry):
    """A view-only plant's data is owned by its binding and the server 409s every write, so
    SoC sensors picked on such an entry (e.g. a leftover choice) are never read: no
    recorder query, no PUT, and none of the SoC state keys (a pending backfill flag or a
    bookmark would be bookkeeping for an upload that can never happen)."""
    from custom_components.wolta.const import CONF_VIEW_ONLY

    mock_entry.data = {CONF_TOKEN: TOKEN, CONF_ZONE: ZONE, CONF_VIEW_ONLY: True,
                       CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client(results=RESULTS_DONE_JOB_SETTLED)
    client.get_profile = AsyncMock(side_effect=Exception("sync skipped in test"))
    coordinator = await _make_coordinator(hass, mock_entry, client)
    measurement_mock = AsyncMock(side_effect=AssertionError("view-only far inte lasa SoC"))
    # The flow read is patched too (the coordinator's own imported name): with the
    # view-only gate removed the cycle then fails on THIS test's assertions, not on a
    # missing recorder instance.
    change_mock = AsyncMock(return_value=_EMPTY_BATT_STATS)

    with (
        patch("custom_components.wolta.coordinator.async_fetch_measurement",
              measurement_mock),
        patch("custom_components.wolta.coordinator.async_fetch_change", change_mock),
    ):
        await coordinator._async_update_data()

    measurement_mock.assert_not_awaited()
    change_mock.assert_not_awaited()
    client.put_data.assert_not_awaited()
    assert not any(key in coordinator._state
                   for key in ("applied_soc", "soc_backfill_pending", "soc_uploaded_ts"))


@pytest.mark.asyncio
async def test_measured_params_skipped_for_preliminary_grade(hass, mock_entry):
    """Ett preliminärt betyg (< 30 dygns data, backend api 0.43.0) får inte driva
    measured-params-repairs: 7 dygns observerad kapacitet är systematiskt UNDERSKATTAD
    (batteriet har inte hunnit visa hela sitt fönster) → falska adopt-förslag."""
    c = await _cap_coordinator(hass, mock_entry, **{_CBK: 15.0})
    results = _oc_results(11.0)
    results["betyg"]["preliminary"] = True
    c._evaluate_measured_params(results)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _CAP_ISSUE_ID) is None


# ---------------------------------------------------------------------------
# External control: coordinator wiring across all three fetch modes
# (spec 2026-08-26 / task 13). _external_quarters() is the single choke point:
# no sensor selected -> the recorder's states table must never be queried, and
# a read failure must never fail the upload cycle (upload unflagged instead -
# the next healing pass repairs a missed neutralization; a failed cycle loses
# a whole upload).
# ---------------------------------------------------------------------------

_EXT_ENTITY = "binary_sensor.grid_rewards_active"
# A flex session from :07 to :19 past the hour any-overlaps TWO 15-min quarters
# (:00-:15 and :15-:30) under the any-overlap rule in stats.flagged_quarters.
_SESSION_HOUR = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
_SESSION_POINTS = [
    (_SESSION_HOUR + timedelta(minutes=7), "on"),
    (_SESSION_HOUR + timedelta(minutes=19), "off"),
]
_FLAGGED_QUARTERS = {_SESSION_HOUR, _SESSION_HOUR + timedelta(minutes=15)}

_EMPTY_BATT_STATS = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                                     "sensor.grid_out", "sensor.solar")}

def _full_stats(start, end, period, values: dict[str, float] | None = None) -> dict:
    """Recorder-shaped rows for every stream sensor over the whole read [start, end):
    every selected sensor is measured in every period, so every quarter in the window
    is complete. ``values`` overrides the per-period change per sensor (default 0.1).
    Rows follow the ``start`` the mock receives, so the period before the window - which
    every read now includes - is there too."""
    step = 3600 if period == "hour" else 300
    first = -(-int(start.timestamp()) // step) * step
    changes = {sid: 0.1 for sid in _EMPTY_BATT_STATS}
    changes.update(values or {})
    return {sid: [{"start": float(t), "change": v}
                  for t in range(first, int(end.timestamp()), step)]
            for sid, v in changes.items()}


async def _run_fetch_mode(coordinator, mode: str):
    """Invoke one of the three row-fetching coroutines with its own realistic window."""
    if mode == "backfill":
        return await coordinator._backfill_rows(NOW)
    if mode == "heal":
        return await coordinator._heal_rows(NOW - timedelta(days=15), NOW)
    return await coordinator._incremental_rows(NOW - timedelta(hours=2), NOW)


def _mode_expected_start(mode: str):
    from custom_components.wolta.coordinator import _BACKFILL_DAYS

    if mode == "backfill":
        return NOW - timedelta(days=_BACKFILL_DAYS)
    if mode == "heal":
        return NOW - timedelta(days=15)
    return NOW - timedelta(hours=2)


@pytest.mark.asyncio
async def test_external_quarters_no_entity_skips_recorder(hass: HomeAssistant, mock_entry):
    """No _external_entity -> async_fetch_states must never be awaited, empty set back."""
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())
    assert coordinator._external_entity is None

    mock_fetch = AsyncMock(side_effect=AssertionError("must not query recorder states"))
    with patch("custom_components.wolta.stats.async_fetch_states", mock_fetch):
        result = await coordinator._external_quarters(NOW - timedelta(days=1), NOW)

    assert result == set()
    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_quarters_read_failure_returns_empty_and_logs(
    hass: HomeAssistant, mock_entry, caplog
):
    """A recorder read failure must be swallowed - not propagated - and logged as a
    warning: an unflagged upload is repaired by the next healing pass, whereas a
    raised exception here would fail the entire upload cycle (no data at all)."""
    import logging

    mock_entry.data = {**ENTRY_DATA, CONF_EXTERNAL_CONTROL: _EXT_ENTITY}
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())

    with (
        caplog.at_level(logging.WARNING, logger="custom_components.wolta.coordinator"),
        patch("custom_components.wolta.stats.async_fetch_states",
              AsyncMock(side_effect=RuntimeError("recorder unavailable"))),
    ):
        result = await coordinator._external_quarters(NOW - timedelta(days=1), NOW)

    assert result == set()
    assert "External-control history read failed" in caplog.text


@pytest.mark.asyncio
async def test_external_quarters_bucketing_failure_also_degrades_softly(
    hass: HomeAssistant, mock_entry, caplog
):
    """The soft-degrade guarantee covers the BUCKETING too, not just the DB read.

    flagged_quarters is pure, but it sits inside the same try: a malformed point list
    (here a row with no last_changed at all) must not be the one thing that kills the
    whole upload cycle, since that is precisely what this method promises not to do.
    Previously the call sat outside the try and would have propagated.
    """
    import logging

    mock_entry.data = {**ENTRY_DATA, CONF_EXTERNAL_CONTROL: _EXT_ENTITY}
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())

    with (
        caplog.at_level(logging.WARNING, logger="custom_components.wolta.coordinator"),
        patch("custom_components.wolta.stats.async_fetch_states",
              AsyncMock(return_value=[(None, "on")])),
    ):
        result = await coordinator._external_quarters(NOW - timedelta(days=1), NOW)

    assert result == set()
    assert "External-control history read failed" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["backfill", "heal", "incremental"])
async def test_fetch_modes_flag_external_control_quarters(
    hass: HomeAssistant, mock_entry, mode
):
    """All three fetch modes (full backfill, gap healing, incremental tick) must flag
    the same external-control session, each looking up its own time window - a flag
    that only landed on one path would make neutralization depend on upload timing."""
    mock_entry.data = {**ENTRY_DATA, CONF_EXTERNAL_CONTROL: _EXT_ENTITY}
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())

    fetch_states = AsyncMock(return_value=_SESSION_POINTS)

    # Every selected sensor measured over the whole read: a quarter is only uploaded
    # when all of them are. The flag alone never makes a row - it only marks quarters
    # the battery streams carry. change=0.0 is the realistic shape of forced idle: the
    # battery is held still but the sensor is still recorded, so the recorder compiles
    # a row and the quarter is a key - which is why forced idle needs no special case.
    async def mock_fetch(h, ids, start, end, period):
        return _full_stats(start, end, period,
                           {"sensor.batt_in": 0.0, "sensor.batt_out": 0.0})

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change",
              side_effect=mock_fetch),
        patch("custom_components.wolta.stats.async_fetch_states", fetch_states),
    ):
        rows = await _run_fetch_mode(coordinator, mode)

    by_ts = {row["ts"]: row for row in rows}
    for qt in _FLAGGED_QUARTERS:
        row = by_ts.get(qt.isoformat())
        assert row is not None, f"{mode}: quarter {qt.isoformat()} was not uploaded"
        assert row["external_control"] is True, f"{mode}: quarter {qt.isoformat()} not flagged"

    fetch_states.assert_awaited_once()
    passed_hass, passed_entity, passed_start, passed_end = fetch_states.await_args.args
    assert passed_entity == _EXT_ENTITY
    assert passed_end == NOW
    assert passed_start == _mode_expected_start(mode), (
        f"{mode}: must look up its OWN window, not another mode's"
    )


@pytest.mark.asyncio
async def test_heal_emits_only_quarters_with_compiled_statistics(
    hass: HomeAssistant, mock_entry
):
    """F1 + C1 regression, end to end on the heal path.

    The integration has been down for two weeks; the flex sensor's last recorded
    state is a stale 'on', which async_fetch_states hands back with its ORIGINAL
    last_changed, so the flag is held across the entire 14-day window. Hourly LTS
    exists for exactly two hours, 09:00 and 10:00, for every selected sensor. 09:00 is
    the period before 10:00 (a quarter is complete only when the period before it was
    measured too), so only 10:00's quarters (10:00-10:45) are complete.

    Only those four quarters may be emitted. Everything else - the two weeks below
    them AND the 11:00-11:45 quarters above them - would upload real
    charge/discharge/grid energy as 0.0, and the bookmark (rows[-1]["ts"]) would
    land on the last of them, so the next cycle starts after the zeros and they
    stick server-side forever.

    This asserts the exact row SET, not just its endpoints: an earlier version of
    this guard checked only "rows exist", "the last one is the newest compiled
    quarter" and "nothing lies beyond" - all of which pass on a 1 340-row result
    where 1 336 rows are all-zero. `all(external_control is True)` cannot tell the
    intended forced-idle quarters from the false ones either, since both are
    flagged. Count and form are what make this guard bite.
    """
    mock_entry.data = {**ENTRY_DATA, CONF_EXTERNAL_CONTROL: _EXT_ENTITY}
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())

    start = NOW - timedelta(days=14)
    compiled_hour = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    expected = [
        (compiled_hour + timedelta(minutes=m)).isoformat() for m in (0, 15, 30, 45)
    ]
    lts = {
        sid: [{"start": (compiled_hour - timedelta(hours=1)).timestamp(), "change": 0.0},
              {"start": compiled_hour.timestamp(), "change": 0.0}]
        for sid in _EMPTY_BATT_STATS
    }
    lts["sensor.batt_in"] = [
        {"start": (compiled_hour - timedelta(hours=1)).timestamp(), "change": 0.8},
        {"start": compiled_hour.timestamp(), "change": 1.2},
    ]
    # One stale 'on' row, held forward across the whole window by flagged_quarters.
    points = [(start, "on")]

    with (
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value=lts),
        patch("custom_components.wolta.stats.async_fetch_states",
              AsyncMock(return_value=points)),
    ):
        rows = await coordinator._heal_rows(start, NOW)

    assert [r["ts"] for r in rows] == expected, (
        f"expected exactly the 4 compiled quarters, got {len(rows)} rows"
    )
    # A PROXY for the assertion above, not the invariant: an all-zero row is not
    # forbidden in itself - a genuinely compiled quarter where all five sensors moved
    # by exactly 0.0 is legitimate and must upload as zeros. It bites here only because
    # this fixture's compiled quarters carry non-zero values.
    zeroed = [
        r["ts"] for r in rows
        if all(v == 0.0 for k, v in r.items() if k not in ("ts", "external_control"))
    ]
    assert zeroed == [], f"all-zero rows would zero real energy: {zeroed}"
    # The half that must NOT regress: the compiled quarters are still flagged.
    assert all(r["external_control"] is True for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["backfill", "heal", "incremental"])
async def test_fetch_modes_skip_recorder_when_no_entity_selected(
    hass: HomeAssistant, mock_entry, mode
):
    """No external_control_entity configured (the fleet default) -> the recorder's
    states table must NEVER be queried (an unconditional extra DB read per cycle for
    every uninterested user would be a real regression), and every uploaded row keeps
    external_control False - byte-identical to the pre-feature output."""
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())
    assert coordinator._external_entity is None

    # Every selected sensor measured over the whole read: a quarter is only uploaded
    # when all of them are.
    async def mock_fetch(h, ids, start, end, period):
        return _full_stats(start, end, period, {"sensor.batt_in": 0.4})

    fetch_states = AsyncMock(side_effect=AssertionError("must not query recorder states"))
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
        patch("custom_components.wolta.stats.async_fetch_states", fetch_states),
    ):
        rows = await _run_fetch_mode(coordinator, mode)

    fetch_states.assert_not_awaited()
    assert rows, f"{mode}: expected at least one uploaded row from the battery data"
    assert all(row["external_control"] is False for row in rows)


# ---------------------------------------------------------------------------
# Flex compensation (spec 2026-08-28): an optional currency sensor is read
# monthly and PATCHed to the server as flex_compensation records.
#
# Two things separate it from every other picker in this integration:
#   B8 - it must stay OUT of the entity fingerprint. It changes no energy row,
#        and a fingerprint change wipes the bookmark and re-backfills a year.
#   §4 - its keys must stay out of _PROFILE_SYNC_KEYS. That list mirrors SERVER
#        fields into entry.data; flex_compensation is a list of records the
#        server owns per (month, source), not a scalar profile field.
# ---------------------------------------------------------------------------

_FLEX_ENTITY = "sensor.checkwatt_monthly_compensation"


def _fingerprint(**extra) -> str:
    """The entity fingerprint as the pre-flex code wrote it."""
    import json as _json

    base = {
        "batt_in": ["sensor.batt_in"], "batt_out": ["sensor.batt_out"],
        "grid_in": ["sensor.grid_in"], "grid_out": ["sensor.grid_out"],
        "solar": ["sensor.solar"],
    }
    base.update(extra)
    return _json.dumps(base, sort_keys=True)


async def _run_cycles(
    hass, mock_entry, client, monthly_side_effect, cycles: int = 1,
    fast_poll: bool = False,
):
    """Run N incremental cycles with a patched monthly_amounts. Returns the coordinator."""
    from custom_components.wolta.coordinator import _FAST_POLL

    empty = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                             "sensor.grid_out", "sensor.solar")}

    async def mock_fetch(h, ids, start, end, period):
        return empty

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
        patch("custom_components.wolta.stats.monthly_amounts",
              new=AsyncMock(side_effect=monthly_side_effect)) as monthly,
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": _RECENT_BOOKMARK,
                         "applied_invert": False,
                         "applied_entities": _fingerprint()})
        for _ in range(cycles):
            if fast_poll:
                # _build_data resets the interval at the end of every cycle, so a
                # fast-poll run has to be re-asserted before each one.
                coordinator.update_interval = _FAST_POLL
            await coordinator._async_update_data()
    coordinator._monthly_mock = monthly
    return coordinator


def _flex_payloads(client) -> list[list[dict]]:
    """Every flex_compensation list this cycle PATCHed, in order."""
    return [
        call.kwargs["flex_compensation"]
        for call in client.patch_profile.await_args_list
        if "flex_compensation" in call.kwargs
    ]


@pytest.mark.asyncio
async def test_coordinator_reads_flex_compensation_entity(
    hass: HomeAssistant, mock_entry
):
    """A configured entity is exposed; absent/empty normalises to None."""
    from custom_components.wolta.coordinator import WoltaCoordinator

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    assert WoltaCoordinator(hass, mock_entry)._flex_entity == _FLEX_ENTITY

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: ""}
    assert WoltaCoordinator(hass, mock_entry)._flex_entity is None

    mock_entry.data = dict(ENTRY_DATA)
    assert WoltaCoordinator(hass, mock_entry)._flex_entity is None


@pytest.mark.asyncio
async def test_flex_compensation_patched_with_sensor_source(
    hass: HomeAssistant, mock_entry
):
    """Each month read becomes one record. source is ALWAYS "sensor": the web
    card's "manual" records are the user's own entry and the server upserts per
    (month, source), so writing "manual" here would silently overwrite them."""
    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=[{"2025-05": 511.5, "2025-06": 812.0}],
    )

    payloads = _flex_payloads(client)
    assert len(payloads) == 1, f"expected exactly one PATCH, got {len(payloads)}"
    assert payloads[0] == [
        {"month": "2025-05", "amount_sek": 511.5, "source": "sensor"},
        {"month": "2025-06", "amount_sek": 812.0, "source": "sensor"},
    ]


@pytest.mark.asyncio
async def test_flex_compensation_reads_the_picked_entity_on_the_default_window(
    hass: HomeAssistant, mock_entry
):
    """The coordinator reads the SELECTED entity and does not narrow the window.

    What that window is (current month + the previous one - the current month is
    still accruing and last month is only final once the aggregator has settled it)
    is stats.monthly_amounts' own default, guarded in
    test_stats.py::test_window_starts_at_first_of_the_oldest_wanted_month. Asserting
    it again from a mocked call here could only check that no argument was passed,
    which is what this test does say."""
    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    coordinator = await _run_cycles(
        hass, mock_entry, client, monthly_side_effect=[{}]
    )

    call = coordinator._monthly_mock.await_args
    assert call.args[1:] == (_FLEX_ENTITY,), f"unexpected positional args: {call.args}"
    assert call.kwargs == {}, f"the default window must not be overridden: {call.kwargs}"

    from custom_components.wolta import stats as stats_mod
    import inspect

    assert (
        inspect.signature(stats_mod.monthly_amounts).parameters["month_count"].default
        == 2
    )


@pytest.mark.asyncio
async def test_flex_compensation_resends_a_changed_amount(
    hass: HomeAssistant, mock_entry
):
    """THE case this feature broke on once already.

    "Only the months you mention are touched" means a guard where the sensor
    STANDS STILL cannot tell working code from code that reads once, caches, and
    never looks again - the server keeps the first value either way. So move the
    number: an aggregator that settles May from a 511.50 estimate to 634.00 must
    reach the server on the very next cycle, and the second PATCH must carry the
    NEW figure, not a repeat of the first.
    """
    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=[
            {"2025-05": 511.5, "2025-06": 100.0},
            {"2025-05": 634.0, "2025-06": 275.25},
        ],
        cycles=2,
    )

    payloads = _flex_payloads(client)
    assert len(payloads) == 2, (
        f"the sensor must be re-read every cycle, got {len(payloads)} PATCH(es)"
    )
    assert payloads[1] == [
        {"month": "2025-05", "amount_sek": 634.0, "source": "sensor"},
        {"month": "2025-06", "amount_sek": 275.25, "source": "sensor"},
    ]


@pytest.mark.asyncio
async def test_no_flex_entity_means_no_read_and_no_patch(
    hass: HomeAssistant, mock_entry
):
    """The fleet default. Neither the recorder nor the profile endpoint is touched -
    an unconditional PATCH would write an empty list over the user's manual rows'
    neighbours and cost every install a request per cycle for nothing."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    coordinator = await _run_cycles(
        hass, mock_entry, client, monthly_side_effect=[{"2025-06": 1.0}]
    )

    coordinator._monthly_mock.assert_not_awaited()
    assert _flex_payloads(client) == []


@pytest.mark.asyncio
async def test_empty_month_mapping_is_not_patched(hass: HomeAssistant, mock_entry):
    """No statistics for the picked sensor -> nothing to say. An empty list is a
    statement we do not have grounds to make."""
    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    await _run_cycles(hass, mock_entry, client, monthly_side_effect=[{}])

    assert _flex_payloads(client) == []


@pytest.mark.asyncio
async def test_flex_patch_failure_logs_and_does_not_fail_the_cycle(
    hass: HomeAssistant, mock_entry, caplog
):
    """A failed PATCH must not take the upload cycle down with it. Unlike the
    external-control read this one IS self-healing: the next cycle re-reads the
    same months and sends them again."""
    import logging

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    client.patch_profile = AsyncMock(side_effect=WoltaApiError("boom", status=500))

    with caplog.at_level(logging.WARNING, logger="custom_components.wolta.coordinator"):
        coordinator = await _run_cycles(
            hass, mock_entry, client,
            monthly_side_effect=[{"2025-06": 12.0}, {"2025-06": 12.0}],
            cycles=2,
        )

    # _run_cycles awaits _async_update_data directly - a raised exception would
    # already have failed the test. results() is the last call of a completed cycle.
    assert client.results.await_count >= 2, "the cycle must have completed both times"
    assert client.patch_profile.await_count == 2, "the next cycle must retry"
    assert any("flex" in r.message.lower() or "flex" in r.getMessage().lower()
               for r in caplog.records if r.levelname == "WARNING")


@pytest.mark.asyncio
async def test_flex_statistics_read_failure_does_not_fail_the_cycle(
    hass: HomeAssistant, mock_entry
):
    """Same guarantee on the recorder side of the read."""
    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=RuntimeError("recorder down"),
    )

    assert client.results.await_count >= 1, "the cycle must have completed"
    assert _flex_payloads(client) == []


@pytest.mark.asyncio
async def test_flex_entity_stays_out_of_the_entity_fingerprint(
    hass: HomeAssistant, mock_entry
):
    """B8. The external-control sensor DOES enter the fingerprint because it
    rewrites energy rows; this one does not touch a single row. Letting it in
    would wipe the bookmark and re-upload a year of history the moment a user
    picks their compensation sensor - for data that has no bearing on those rows.
    """
    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    coordinator = await _run_cycles(
        hass, mock_entry, client, monthly_side_effect=[{"2025-06": 5.0}]
    )

    assert coordinator._state["applied_entities"] == _fingerprint(), (
        "the flex sensor leaked into the entity fingerprint"
    )
    assert coordinator._state.get("last_uploaded_ts") == _RECENT_BOOKMARK, (
        "the bookmark was reset -> a full year re-backfill for nothing"
    )
    assert _FLEX_ENTITY not in coordinator._state["applied_entities"]


@pytest.mark.asyncio
async def test_flex_entity_alongside_external_control_keeps_only_the_latter(
    hass: HomeAssistant, mock_entry
):
    """Both pickers set: the fingerprint carries external_control and nothing else
    new. Guards against a blanket "add every configured entity" refactor."""
    mock_entry.data = {
        **ENTRY_DATA,
        CONF_EXTERNAL_CONTROL: "binary_sensor.grid_rewards_active",
        CONF_FLEX_COMPENSATION: _FLEX_ENTITY,
    }
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    expected = _fingerprint(external_control=["binary_sensor.grid_rewards_active"])

    empty = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                             "sensor.grid_out", "sensor.solar")}

    async def mock_fetch(h, ids, start, end, period):
        return empty

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
        patch("custom_components.wolta.stats.monthly_amounts",
              new=AsyncMock(return_value={})),
        patch("custom_components.wolta.stats.async_fetch_states",
              new=AsyncMock(return_value=[])),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": _RECENT_BOOKMARK,
                         "applied_invert": False,
                         "applied_entities": expected})
        await coordinator._async_update_data()

    assert coordinator._state["applied_entities"] == expected
    assert coordinator._state.get("last_uploaded_ts") == _RECENT_BOOKMARK


@pytest.mark.asyncio
async def test_profile_sync_keys_unchanged_by_flex_compensation():
    """§4. _PROFILE_SYNC_KEYS mirrors SERVER-owned scalar profile fields into
    entry.data. flex_compensation is neither a scalar nor entry.data's business -
    mirroring it would push a list of records into the config entry and, worse,
    make the client's own writes look like server state on the next side-poll.

    Asserted as an exact tuple, not a membership check: the point is that this
    list did not grow, and `not in` would pass for a key added under any other
    name."""
    from custom_components.wolta.coordinator import _PROFILE_SYNC_KEYS
    from custom_components.wolta.const import (
        CONF_BATTERY_KW, CONF_BATTERY_KWH, CONF_COST_SEK, CONF_EFF,
        CONF_EXPORT_EXTRA_ORE, CONF_EXPORT_EXTRA_PCT, CONF_GRID_VAR_ORE,
        CONF_GRID_VAR_PCT, CONF_PURCHASE_DATE, CONF_RESERVE_PCT,
        CONF_SURCHARGE_ORE, CONF_ZONE, CONF_CONTROL_SYSTEM, CONF_CONTROL_SYSTEM_NAME,
        CONF_SOC_ABSENT,
    )

    assert _PROFILE_SYNC_KEYS == (
        CONF_ZONE, CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF, CONF_RESERVE_PCT,
        CONF_COST_SEK, CONF_PURCHASE_DATE, CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE,
        CONF_EXPORT_EXTRA_ORE, CONF_GRID_VAR_PCT, CONF_EXPORT_EXTRA_PCT,
        CONF_CONTROL_SYSTEM, CONF_CONTROL_SYSTEM_NAME, CONF_SOC_ABSENT,
    )
    assert CONF_FLEX_COMPENSATION not in _PROFILE_SYNC_KEYS


@pytest.mark.asyncio
async def test_view_only_entry_never_patches_flex_compensation(
    hass: HomeAssistant, mock_entry
):
    """A bound plant (Sonnen/Reduxi) owns its own data; this entry only polls
    results. It must not read HA statistics for a compensation sensor either."""
    from custom_components.wolta.const import CONF_VIEW_ONLY

    mock_entry.data = {
        **ENTRY_DATA, CONF_VIEW_ONLY: True, CONF_FLEX_COMPENSATION: _FLEX_ENTITY,
    }
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.stats.monthly_amounts",
              new=AsyncMock(return_value={"2025-06": 9.0})) as monthly,
    ):
        coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
        await coordinator._async_update_data()

    monthly.assert_not_awaited()
    assert _flex_payloads(client) == []


@pytest.mark.asyncio
async def test_flex_compensation_skipped_during_fast_poll(
    hass: HomeAssistant, mock_entry
):
    """I1. Fast-poll is 60 s while a server job runs. The profile GET is already
    skipped there because nothing profile-related moves on that timescale - and this
    is a WRITE plus a recorder month query, on a figure an aggregator settles days
    after the month ends. put_data is gated by "if rows:", so without this gate the
    compensation PATCH would be the only thing hitting the server every minute."""
    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    coordinator = await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=[{"2025-06": 812.0}],
        fast_poll=True,
    )

    coordinator._monthly_mock.assert_not_awaited()
    assert _flex_payloads(client) == []


@pytest.mark.asyncio
async def test_empty_reads_raise_a_repair_after_the_threshold(
    hass: HomeAssistant, mock_entry
):
    """I2. A picked sensor that yields nothing must not fail silently.

    Without this the user sees a filled-in field in Configure and no effect
    whatsoever - indistinguishable from the feature working. The threshold keeps it
    from crying wolf on the ordinary case of statistics that have not compiled yet.
    """
    from custom_components.wolta.coordinator import _FLEX_EMPTY_CYCLES_BEFORE_ISSUE

    # Pinned to a LITERAL on purpose. Deriving the cycle counts below from the
    # constant would make this test move with it: lowering the threshold to 1 would
    # turn the "quiet" run into zero cycles and the assertion would pass vacuously.
    # Changing the threshold should mean changing this line, deliberately.
    assert _FLEX_EMPTY_CYCLES_BEFORE_ISSUE == 3

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    # Two empty cycles - one short of the threshold: still quiet.
    coordinator = await _run_cycles(
        hass, mock_entry, client, monthly_side_effect=[{}, {}], cycles=2,
    )
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._flex_issue_id
    ) is None, "the repair fired early - it will cry wolf on every fresh setup"

    # The third one trips it.
    coordinator = await _run_cycles(
        hass, mock_entry, client, monthly_side_effect=[{}, {}, {}], cycles=3,
    )
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._flex_issue_id)
    assert issue is not None, "a sensor that never yields a figure must be surfaced"
    assert issue.translation_key == "flex_compensation_no_statistics"
    assert issue.is_fixable is False


@pytest.mark.asyncio
async def test_repair_cleared_as_soon_as_an_amount_is_read(
    hass: HomeAssistant, mock_entry
):
    """The user fixes their sensor -> the warning clears itself."""
    from custom_components.wolta.coordinator import _FLEX_EMPTY_CYCLES_BEFORE_ISSUE

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    coordinator = await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=[{}] * _FLEX_EMPTY_CYCLES_BEFORE_ISSUE + [{"2025-06": 5.0}],
        cycles=_FLEX_EMPTY_CYCLES_BEFORE_ISSUE + 1,
    )

    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._flex_issue_id) is None
    assert _flex_payloads(client) == [
        [{"month": "2025-06", "amount_sek": 5.0, "source": "sensor"}]
    ]


@pytest.mark.asyncio
async def test_repair_cleared_when_the_picker_is_emptied(
    hass: HomeAssistant, mock_entry
):
    """Clearing the field is a deliberate "stop doing this", not an unresolved
    problem - the warning must not outlive the setting that caused it."""
    from custom_components.wolta.coordinator import _FLEX_EMPTY_CYCLES_BEFORE_ISSUE

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    coordinator = await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=[{}] * _FLEX_EMPTY_CYCLES_BEFORE_ISSUE,
        cycles=_FLEX_EMPTY_CYCLES_BEFORE_ISSUE,
    )
    issue_id = coordinator._flex_issue_id
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # The user clears the picker; the entry reloads and the counter survives in Store.
    carried_state = dict(coordinator._state)
    mock_entry.data = dict(ENTRY_DATA)
    empty = {k: [] for k in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                             "sensor.grid_out", "sensor.solar")}

    async def mock_fetch(h, ids, start, end, period):
        return empty

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client, store_state=carried_state)
        await coordinator._async_update_data()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.asyncio
async def test_flex_repair_id_is_per_entry(hass: HomeAssistant, mock_entry):
    """Two Wolta entries must not clear each other's warning: every non-fixable issue
    uses the per-entry form, the flex one included (spec 2026-10-15 F1)."""
    from custom_components.wolta.coordinator import _ISSUE_FLEX_NO_STATS, WoltaCoordinator

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    coordinator = WoltaCoordinator(hass, mock_entry)
    assert coordinator._flex_issue_id == f"{_ISSUE_FLEX_NO_STATS}_{mock_entry.entry_id}"
    assert coordinator._flex_issue_id != _ISSUE_FLEX_NO_STATS
    assert coordinator._upload_failure_issue_id == f"upload_failure_{mock_entry.entry_id}"
    assert coordinator._profile_full_issue_id == f"profile_full_{mock_entry.entry_id}"


@pytest.mark.asyncio
async def test_a_rejected_patch_raises_the_repair_after_the_threshold(
    hass: HomeAssistant, mock_entry
):
    """A payload the server REFUSES is refused forever, and nothing else notices.

    Nothing is bookmarked on this path, so the next cycle reads the same months and
    sends the byte-identical payload - a permanent loop inside a warning nobody
    reads. The empty-read leg cannot cover it: there the mapping is non-empty, which
    is exactly the problem. Without this leg the compensation sync dies silently and
    stays dead.
    """
    from custom_components.wolta.api import WoltaApiError
    from custom_components.wolta.coordinator import _FLEX_EMPTY_CYCLES_BEFORE_ISSUE

    assert _FLEX_EMPTY_CYCLES_BEFORE_ISSUE == 3

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    async def _reject(token, **fields):
        if "flex_compensation" in fields:
            raise WoltaApiError("amount_sek maste vara >= 0", status=422)
        return {}

    client.patch_profile = AsyncMock(side_effect=_reject)

    # Two rejected cycles - one short of the threshold: still quiet.
    coordinator = await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=[{"2025-06": 5.0}] * 2, cycles=2,
    )
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._flex_issue_id
    ) is None, "the repair fired early - it will cry wolf on one flaky cycle"

    # The third one trips it.
    coordinator = await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=[{"2025-06": 5.0}] * 3, cycles=3,
    )
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._flex_issue_id)
    assert issue is not None, (
        "a PATCH the server keeps refusing must be surfaced - it never self-heals"
    )
    assert issue.translation_key == "flex_compensation_no_statistics"


@pytest.mark.asyncio
async def test_rejection_repair_clears_once_a_patch_goes_through(
    hass: HomeAssistant, mock_entry
):
    """Same two-way contract as the empty-read leg: the warning must not outlive the
    condition. Shares one counter and one issue with that leg because the two are
    mutually exclusive within a cycle."""
    from custom_components.wolta.api import WoltaApiError
    from custom_components.wolta.coordinator import _FLEX_EMPTY_CYCLES_BEFORE_ISSUE

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    calls = {"n": 0}

    async def _reject_then_accept(token, **fields):
        if "flex_compensation" not in fields:
            return {}
        calls["n"] += 1
        if calls["n"] <= _FLEX_EMPTY_CYCLES_BEFORE_ISSUE:
            raise WoltaApiError("nope", status=422)
        return {}

    client.patch_profile = AsyncMock(side_effect=_reject_then_accept)

    coordinator = await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=[{"2025-06": 5.0}] * (_FLEX_EMPTY_CYCLES_BEFORE_ISSUE + 1),
        cycles=_FLEX_EMPTY_CYCLES_BEFORE_ISSUE + 1,
    )

    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._flex_issue_id) is None


@pytest.mark.asyncio
async def test_auth_and_rate_limit_do_not_count_toward_the_flex_repair(
    hass: HomeAssistant, mock_entry
):
    """Neither says anything about the compensation payload. A 404 belongs to the
    reauth path results() owns and a 429 clears itself, so blaming the user's sensor
    for either would be a false diagnosis on a warning they cannot act on."""
    from custom_components.wolta.api import WoltaAuthError
    from custom_components.wolta.coordinator import _FLEX_EMPTY_CYCLES_BEFORE_ISSUE

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    async def _auth_fail(token, **fields):
        if "flex_compensation" in fields:
            raise WoltaAuthError("token purged", status=404)
        return {}

    client.patch_profile = AsyncMock(side_effect=_auth_fail)

    coordinator = await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=[{"2025-06": 5.0}] * (_FLEX_EMPTY_CYCLES_BEFORE_ISSUE + 2),
        cycles=_FLEX_EMPTY_CYCLES_BEFORE_ISSUE + 2,
    )

    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._flex_issue_id
    ) is None, "an auth failure must not be reported as a broken compensation sensor"


@pytest.mark.asyncio
async def test_server_errors_do_not_count_toward_the_flex_repair(
    hass: HomeAssistant, mock_entry
):
    """A 5xx says the SERVER is in trouble, not that this payload was refused.

    The refusal leg exists because a rejected payload is re-sent byte-identical
    forever. A 502 from a deploy or a 503 from an overloaded backend is the opposite
    case: the identical payload will go through once the server is back, and that
    is exactly what re-sending is for. Counting it would raise a repair that blames
    the user's sensor for Wolta's downtime - a warning they cannot act on, and
    one that teaches them to dismiss the real one.
    """
    from custom_components.wolta.api import WoltaApiError
    from custom_components.wolta.coordinator import _FLEX_EMPTY_CYCLES_BEFORE_ISSUE

    mock_entry.data = {**ENTRY_DATA, CONF_FLEX_COMPENSATION: _FLEX_ENTITY}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    statuses = iter([500, 502, 503, 500, 503])

    async def _server_error(token, **fields):
        if "flex_compensation" in fields:
            raise WoltaApiError("upstream unavailable", status=next(statuses))
        return {}

    client.patch_profile = AsyncMock(side_effect=_server_error)

    coordinator = await _run_cycles(
        hass, mock_entry, client,
        monthly_side_effect=[{"2025-06": 5.0}] * (_FLEX_EMPTY_CYCLES_BEFORE_ISSUE + 2),
        cycles=_FLEX_EMPTY_CYCLES_BEFORE_ISSUE + 2,
    )

    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._flex_issue_id
    ) is None, "a server-side failure must not be reported as a refused payload"


# ---------------------------------------------------------------------------
# Repair: an energy stream that yields nothing (measurement-sensor mistake)
# ---------------------------------------------------------------------------
#
# stats.py no longer fabricates 0.0 buckets for rows without a change value, so a
# mispicked power (W) sensor now produces an EMPTY stream instead of a stream of
# zeros. Silence looks identical to "working" - these tests pin the repair issue
# that makes it visible, mirroring the flex_compensation_no_statistics contract.


def _energy_stats(start, end, *, empty: tuple[str, ...] = ()) -> dict:
    """Recorder-shaped 5-min rows for every stream sensor; ``empty`` streams get []."""
    rows = []
    t = start.timestamp()
    while t < end.timestamp():
        rows.append({"start": t, "change": 0.1})
        t += 300
    return {
        sid: ([] if sid in empty else list(rows))
        for sid in ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                    "sensor.grid_out", "sensor.solar", "sensor.solar2")
    }


async def _run_energy_cycles(
    hass, mock_entry, client, *, empty: tuple[str, ...], cycles: int,
    bookmark_age: timedelta = timedelta(hours=6),
    advance: timedelta = timedelta(hours=6),
    fingerprint: str | None = None,
    extra_state: dict | None = None,
):
    """Run N incremental cycles with the given streams empty. The clock advances
    ``advance`` per cycle so each read window is a real slow-poll window - with a
    frozen clock the bookmark catches up and every later window is seconds wide."""
    clock = {"now": NOW}

    async def mock_fetch(h, ids, start, end, period):
        return _energy_stats(start, end, empty=empty)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow",
              side_effect=lambda: clock["now"]),
        patch("custom_components.wolta.coordinator.async_fetch_change",
              side_effect=mock_fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - bookmark_age).isoformat(),
                         "applied_invert": False,
                         "applied_entities": fingerprint or _fingerprint(),
                         **(extra_state or {})})
        for _ in range(cycles):
            await coordinator._async_update_data()
            clock["now"] = clock["now"] + advance
    return coordinator


@pytest.mark.asyncio
async def test_empty_energy_stream_raises_repair_after_threshold(
    hass: HomeAssistant, mock_entry
):
    """A configured stream that reads empty while its siblings deliver is the
    broken-sensor signature (state_class: measurement gives no change column).
    Without the repair the fix upstream in stats.py trades corruption for silence.
    """
    from custom_components.wolta.coordinator import _ENERGY_EMPTY_CYCLES_BEFORE_ISSUE

    # Pinned to a LITERAL on purpose, same reasoning as the flex twin: deriving the
    # cycle counts from the constant would let a lowered threshold pass vacuously.
    assert _ENERGY_EMPTY_CYCLES_BEFORE_ISSUE == 3

    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    # Two bad cycles - one short of the threshold: still quiet.
    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar",), cycles=2,
    )
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._energy_issue_id
    ) is None, "the repair fired early - it will cry wolf on every fresh setup"

    # The third one trips it.
    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar",), cycles=3,
    )
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._energy_issue_id)
    assert issue is not None, "a stream that never yields data must be surfaced"
    assert issue.translation_key == "energy_stream_no_statistics"
    assert issue.is_fixable is False
    assert "sensor.solar" in (issue.translation_placeholders or {}).get("streams", "")


@pytest.mark.asyncio
async def test_energy_repair_clears_when_the_stream_reads_again(
    hass: HomeAssistant, mock_entry
):
    """The user re-picks a proper energy sensor -> the warning clears itself."""
    from custom_components.wolta.coordinator import _ENERGY_EMPTY_CYCLES_BEFORE_ISSUE

    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar",),
        cycles=_ENERGY_EMPTY_CYCLES_BEFORE_ISSUE,
    )
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._energy_issue_id) is not None

    # One healthy cycle on the SAME coordinator: issue and streak both go.
    clock = {"now": NOW + timedelta(days=2)}
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow",
              side_effect=lambda: clock["now"]),
        patch("custom_components.wolta.coordinator.async_fetch_change",
              side_effect=lambda h, ids, start, end, period:
              _energy_stats(start, end, empty=())),
    ):
        await coordinator._async_update_data()

    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._energy_issue_id) is None
    assert coordinator._state.get("energy_empty_cycles") is None


@pytest.mark.asyncio
async def test_all_empty_short_window_does_not_count(
    hass: HomeAssistant, mock_entry
):
    """A window shorter than a statistics-compile period legitimately has no rows
    for ANY stream (fast poll right after an upload). Counting those cycles would
    trip the repair in minutes on perfectly healthy setups."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    coordinator = await _run_energy_cycles(
        hass, mock_entry, client,
        empty=("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
               "sensor.grid_out", "sensor.solar"),
        cycles=3,
        bookmark_age=timedelta(minutes=2),
        advance=timedelta(0),  # frozen clock: the window stays 2 minutes wide
    )
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._energy_issue_id) is None
    assert coordinator._state.get("energy_empty_cycles") is None, (
        "short-window cycles must not even count toward the streak"
    )


@pytest.mark.asyncio
async def test_unconfigured_stream_is_not_flagged(hass: HomeAssistant, mock_entry):
    """No solar sensor configured -> an empty solar stream is expected, not broken."""
    entry_data = {k: v for k, v in ENTRY_DATA.items() if k != CONF_SOLAR}
    mock_entry.data = entry_data
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar",), cycles=3,
        fingerprint=_fingerprint(solar=[]),
    )
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, coordinator._energy_issue_id) is None


# ---------------------------------------------------------------------------
# Repair: one selected sensor of a stream goes silent (statistics_missing)
# ---------------------------------------------------------------------------
#
# Complete quarters need every selected sensor, so a single silent sensor stops the
# whole upload while its stream still has values from the other sensor - energy_empty
# cannot see it. statistics_missing tells the owner which sensor it is.

_TWO_SOLAR_FP = _fingerprint(solar=["sensor.solar", "sensor.solar2"])


def _two_solar_entry(hass, mock_entry):
    """Same entry with two solar sensors: sensor.solar and sensor.solar2."""
    mock_entry.data = {**mock_entry.data, CONF_SOLAR: ["sensor.solar", "sensor.solar2"]}


@pytest.mark.asyncio
async def test_silent_sensor_raises_statistics_missing_after_three_cycles(
    hass: HomeAssistant, mock_entry
):
    _two_solar_entry(hass, mock_entry)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar2",), cycles=2,
        fingerprint=_TWO_SOLAR_FP)
    reg = ir.async_get(hass)
    assert reg.async_get_issue(DOMAIN, f"statistics_missing_{mock_entry.entry_id}") is None
    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar2",), cycles=3,
        fingerprint=_TWO_SOLAR_FP)
    issue = reg.async_get_issue(DOMAIN, f"statistics_missing_{mock_entry.entry_id}")
    assert issue is not None and issue.translation_key == "statistics_missing"
    assert issue.is_fixable is False
    assert issue.translation_placeholders["entities"] == "sensor.solar2"
    # The stream still has a sensor that delivers -> energy_empty is NOT raised.
    assert reg.async_get_issue(DOMAIN, coordinator._energy_issue_id) is None


@pytest.mark.asyncio
async def test_single_silent_sensor_of_a_stream_is_energy_empty_not_statistics_missing(
    hass: HomeAssistant, mock_entry
):
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar",), cycles=3)
    reg = ir.async_get(hass)
    assert reg.async_get_issue(DOMAIN, coordinator._energy_issue_id) is not None
    assert reg.async_get_issue(DOMAIN, f"statistics_missing_{mock_entry.entry_id}") is None


@pytest.mark.asyncio
async def test_statistics_missing_clears_when_the_sensor_reads_again(
    hass: HomeAssistant, mock_entry
):
    _two_solar_entry(hass, mock_entry)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar2",), cycles=3,
        fingerprint=_TWO_SOLAR_FP)
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"statistics_missing_{mock_entry.entry_id}") is not None
    clock = {"now": NOW + timedelta(days=2)}
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow",
              side_effect=lambda: clock["now"]),
        patch("custom_components.wolta.coordinator.async_fetch_change",
              side_effect=lambda h, ids, start, end, period: _energy_stats(start, end)),
    ):
        await coordinator._async_update_data()
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"statistics_missing_{mock_entry.entry_id}") is None
    assert coordinator._state.get("silent_sensor_cycles") is None


@pytest.mark.asyncio
async def test_an_all_silent_window_neither_counts_nor_clears_statistics_missing(
    hass: HomeAssistant, mock_entry
):
    """When every selected sensor is silent in a long window nothing can be said about
    any single sensor: the raised issue and the streak stay as they were, and only a
    window in which the sensor measures again clears it."""
    _two_solar_entry(hass, mock_entry)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar2",), cycles=3,
        fingerprint=_TWO_SOLAR_FP)
    issue_id = f"statistics_missing_{mock_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    streak = dict(coordinator._state["silent_sensor_cycles"])

    all_sensors = ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in",
                   "sensor.grid_out", "sensor.solar", "sensor.solar2")
    clock = {"now": NOW + timedelta(days=2)}
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow",
              side_effect=lambda: clock["now"]),
        patch("custom_components.wolta.coordinator.async_fetch_change",
              side_effect=lambda h, ids, start, end, period:
              _energy_stats(start, end, empty=all_sensors)),
    ):
        await coordinator._async_update_data()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert coordinator._state["silent_sensor_cycles"] == streak

    clock["now"] = NOW + timedelta(days=3)
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow",
              side_effect=lambda: clock["now"]),
        patch("custom_components.wolta.coordinator.async_fetch_change",
              side_effect=lambda h, ids, start, end, period: _energy_stats(start, end)),
    ):
        await coordinator._async_update_data()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert coordinator._state.get("silent_sensor_cycles") is None


@pytest.mark.asyncio
async def test_statistics_missing_never_names_a_sensor_that_reads_again(
    hass: HomeAssistant, mock_entry
):
    """solar2 recovers in the same cycle as solar goes silent: the streak restarts for
    solar, and the warning naming solar2 must not stay up until solar's streak is due."""
    _two_solar_entry(hass, mock_entry)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar2",), cycles=3,
        fingerprint=_TWO_SOLAR_FP)
    issue_id = f"statistics_missing_{mock_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow",
              return_value=NOW + timedelta(days=2)),
        patch("custom_components.wolta.coordinator.async_fetch_change",
              side_effect=lambda h, ids, start, end, period:
              _energy_stats(start, end, empty=("sensor.solar",))),
    ):
        await coordinator._async_update_data()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert coordinator._state["silent_sensor_cycles"] == {"sensor.solar": 1}


@pytest.mark.asyncio
async def test_short_window_does_not_count_silent_sensors(hass: HomeAssistant, mock_entry):
    _two_solar_entry(hass, mock_entry)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _run_energy_cycles(
        hass, mock_entry, client, empty=("sensor.solar2",), cycles=3,
        bookmark_age=timedelta(minutes=2), advance=timedelta(0),
        fingerprint=_TWO_SOLAR_FP)
    assert coordinator._state.get("silent_sensor_cycles") is None


@pytest.mark.asyncio
async def test_a_sensor_measured_only_before_the_window_is_silent(
    hass: HomeAssistant, mock_entry
):
    """Every read starts one period early. A row in that period says nothing about the
    window itself, so a sensor whose only row lies there is silent in the window."""
    from custom_components.wolta.coordinator import _Source, aggregate_5min_to_15min

    _two_solar_entry(hass, mock_entry)
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())
    start, end = NOW - timedelta(hours=6), NOW
    early = start - timedelta(minutes=5)
    raw = _energy_stats(early, end)
    raw["sensor.solar2"] = [{"start": early.timestamp(), "change": 0.1}]
    built = coordinator._build_streams(_Source(raw, aggregate_5min_to_15min, start, end))
    assert built.silent == {"sensor.solar2"}


# A sensor that dies AFTER it has worked. The bookmark is the last uploaded complete
# quarter and every read starts there, so the read always holds that quarter - and the
# dead sensor's rows in it. Silence is therefore judged by lag: a sensor is silent when
# its newest measured period trails the newest of all selected sensors by at least
# max(30 min, the read's period).


def _dying_fetch(stops: dict[str, datetime]):
    """A recorder in which every sensor (sensor.solar2 included) measures every period
    of the read, except that the sensors in ``stops`` have no rows from that moment on.
    Rows follow the requested period, so hourly reads get hourly rows."""
    async def fetch(h, ids, start, end, period):
        data = _full_stats(start, end, period, values={"sensor.solar2": 0.1})
        for sid, stop in stops.items():
            data[sid] = [r for r in data[sid] if r["start"] < stop.timestamp()]
        return data
    return fetch


async def _run_dying_cycles(
    hass, mock_entry, client, *, stops: dict[str, datetime], bookmark: datetime,
    cycles: int, fingerprint: str | None = None,
    advance: timedelta = timedelta(hours=6), reads: list[str] | None = None,
):
    """Run slow-poll cycles on ONE coordinator and record after each cycle whether
    (statistics_missing, energy_stream_no_statistics) is raised. ``reads`` collects the
    period of every statistics read.

    The sun never sets here: the cycles span a night, and a dead solar sensor's night
    periods would be known zeros (spec 6.2) - the night quarters complete, the bookmark
    moving on. These scenarios are about lag in daylight; the night has its own tests
    (known zeros, below)."""
    clock = {"now": NOW}
    reg = ir.async_get(hass)
    seen: list[tuple[bool, bool]] = []
    fetch = _dying_fetch(stops)

    async def recording_fetch(h, ids, start, end, period):
        if reads is not None:
            reads.append(period)
        return await fetch(h, ids, start, end, period)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow",
              side_effect=lambda: clock["now"]),
        patch("custom_components.wolta.coordinator.async_fetch_change",
              side_effect=recording_fetch),
        patch("custom_components.wolta.coordinator.WoltaCoordinator._sun_down_fn",
              lambda self, period_s: lambda start: False),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark.isoformat(),
                         "applied_invert": False,
                         "applied_entities": fingerprint or _fingerprint()})
        for _ in range(cycles):
            await coordinator._async_update_data()
            seen.append((
                reg.async_get_issue(DOMAIN, coordinator._stats_issue_id) is not None,
                reg.async_get_issue(DOMAIN, coordinator._energy_issue_id) is not None,
            ))
            clock["now"] = clock["now"] + advance
    return coordinator, seen


@pytest.mark.asyncio
async def test_a_sensor_that_dies_after_working_raises_statistics_missing(
    hass: HomeAssistant, mock_entry
):
    """solar2 stops 15 minutes after the bookmark. The bookmark quarter stays complete
    and is re-read every cycle, so solar2 is measured in every read - and nothing
    after it is ever sent. Its lag behind the other sensors makes it silent."""
    _two_solar_entry(hass, mock_entry)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    bookmark = NOW - timedelta(hours=6)
    coordinator, seen = await _run_dying_cycles(
        hass, mock_entry, client,
        stops={"sensor.solar2": bookmark + timedelta(minutes=15)},
        bookmark=bookmark, cycles=4, fingerprint=_TWO_SOLAR_FP)
    assert coordinator._state["last_uploaded_ts"] == bookmark.isoformat(), (
        "the scenario needs the bookmark to stay on the quarter solar2 still measured")
    assert seen == [(False, False), (False, False), (True, False), (True, False)]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._stats_issue_id)
    assert issue.translation_placeholders["entities"] == "sensor.solar2"


@pytest.mark.asyncio
async def test_a_single_sensor_stream_that_dies_after_working_raises_energy_empty(
    hass: HomeAssistant, mock_entry
):
    """The only solar sensor dies the same way: its stream still holds the bookmark
    quarter, so the stream is not empty - but every sensor of it is silent."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    bookmark = NOW - timedelta(hours=6)
    coordinator, seen = await _run_dying_cycles(
        hass, mock_entry, client,
        stops={"sensor.solar": bookmark + timedelta(minutes=15)},
        bookmark=bookmark, cycles=4)
    assert seen == [(False, False), (False, False), (False, True), (False, True)]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._energy_issue_id)
    assert issue.translation_key == "energy_stream_no_statistics"
    assert "sensor.solar" in issue.translation_placeholders["streams"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dying", "two_solar", "expected"),
    [("sensor.solar2", True, (True, False)), ("sensor.solar", False, (False, True))],
)
async def test_a_sensor_that_dies_after_working_is_caught_on_the_heal_path(
    hass: HomeAssistant, mock_entry, dying, two_solar, expected
):
    """A stall longer than the short-term window: the heal path reads hourly
    statistics. The sensor has hourly rows up to bookmark + 2 h and none after."""
    from custom_components.wolta.coordinator import _SHORT_TERM_DAYS

    if two_solar:
        _two_solar_entry(hass, mock_entry)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    bookmark = NOW - timedelta(days=_SHORT_TERM_DAYS + 6)
    reads: list[str] = []
    coordinator, seen = await _run_dying_cycles(
        hass, mock_entry, client, stops={dying: bookmark + timedelta(hours=3)},
        bookmark=bookmark, cycles=4,
        fingerprint=_TWO_SOLAR_FP if two_solar else None, reads=reads)
    assert reads and set(reads) == {"hour"}, "the scenario must stay on the heal path"
    assert seen[:2] == [(False, False), (False, False)]
    assert seen[2:] == [expected, expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("bookmark_age", [timedelta(hours=6), timedelta(days=15)])
async def test_a_healthy_setup_never_counts_a_silent_sensor(
    hass: HomeAssistant, mock_entry, bookmark_age
):
    """Every sensor measured up to the same latest period: nothing lags, nothing counts,
    neither on the incremental path nor on the heal path."""
    _two_solar_entry(hass, mock_entry)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator, seen = await _run_dying_cycles(
        hass, mock_entry, client, stops={}, bookmark=NOW - bookmark_age, cycles=4,
        fingerprint=_TWO_SOLAR_FP)
    assert seen == [(False, False)] * 4
    assert coordinator._state.get("silent_sensor_cycles") is None
    assert coordinator._state.get("energy_empty_cycles") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("period", "lag", "silent"),
    [("5minute", timedelta(minutes=25), False),
     ("5minute", timedelta(minutes=30), True),
     ("hour", timedelta(0), False),
     ("hour", timedelta(hours=1), True)],
)
async def test_silence_is_a_lag_of_at_least_max_30_min_and_the_period(
    hass: HomeAssistant, mock_entry, period, lag, silent
):
    """The threshold is max(30 min, the read's period): 30 minutes for 5-minute reads,
    one hour for hourly reads."""
    from custom_components.wolta.coordinator import (
        _Source,
        aggregate_5min_to_15min,
        split_hour_to_quarters,
    )

    _two_solar_entry(hass, mock_entry)
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())
    step = 300 if period == "5minute" else 3600
    start, end = NOW - timedelta(hours=12), NOW
    raw = _full_stats(start - timedelta(seconds=step), end, period,
                      values={"sensor.solar2": 0.1})
    newest_all = end.timestamp() - step
    raw["sensor.solar2"] = [r for r in raw["sensor.solar2"]
                            if r["start"] <= newest_all - lag.total_seconds()]
    agg = aggregate_5min_to_15min if period == "5minute" else split_hour_to_quarters
    built = coordinator._build_streams(_Source(raw, agg, start, end))
    assert built.silent == ({"sensor.solar2"} if silent else set())


# ---------------------------------------------------------------------------
# Upload stalled (spec 2026-10-15): every selected sensor stops at once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lag", "stalled"),
    [(None, False),
     (timedelta(hours=12) - timedelta(seconds=1), False),
     (timedelta(hours=12), True),
     (timedelta(hours=1, seconds=10), False)],
)
def test_upload_stalled_is_a_lag_of_at_least_twelve_hours(lag, stalled):
    """The read stands still when the newest measured period of every selected sensor
    ended at least 12 h before now. None (nothing measured) is never a stall: that is
    energy_empty's case. 1 h 10 s is the hourly compile delay (HA compiles the hour with
    the :55 period, at hh+1:00:10)."""
    from custom_components.wolta.coordinator import _upload_stalled

    until = None if lag is None else int((NOW - lag).timestamp())
    assert _upload_stalled(until, NOW) is stalled


@pytest.mark.asyncio
@pytest.mark.parametrize(("period", "step"), [("5minute", 300), ("hour", 3600)])
async def test_measured_until_is_the_end_of_the_newest_measured_period(
    hass: HomeAssistant, mock_entry, period, step
):
    """measured_until = start + period of the newest row of ANY selected sensor; a sensor
    that stopped earlier does not lower it."""
    from custom_components.wolta.coordinator import (
        _Source,
        aggregate_5min_to_15min,
        split_hour_to_quarters,
    )

    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())
    start, end = NOW - timedelta(hours=6), NOW
    raw = _full_stats(start - timedelta(seconds=step), end, period)
    raw["sensor.solar"] = [r for r in raw["sensor.solar"]
                           if r["start"] < (NOW - timedelta(hours=3)).timestamp()]
    agg = aggregate_5min_to_15min if period == "5minute" else split_hour_to_quarters
    built = coordinator._build_streams(_Source(raw, agg, start, end))
    assert built.measured_until == int(NOW.timestamp())


@pytest.mark.asyncio
async def test_measured_until_is_none_without_any_measured_period(
    hass: HomeAssistant, mock_entry
):
    from custom_components.wolta.coordinator import _Source, aggregate_5min_to_15min

    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())
    start, end = NOW - timedelta(hours=6), NOW
    built = coordinator._build_streams(
        _Source(dict(_EMPTY_BATT_STATS), aggregate_5min_to_15min, start, end))
    assert built.measured_until is None


async def _run_stall_cycles(
    hass, mock_entry, client, *, bookmark: datetime, plan: list[dict[str, datetime]],
    fingerprint: str | None = None, advance: timedelta = timedelta(hours=6),
    empty_cycles: frozenset[int] = frozenset(),
):
    """One coordinator, one cycle per entry of ``plan``: the recorder of that cycle has
    no rows for the sensors in the dict from their moment on (_dying_fetch). A cycle in
    ``empty_cycles`` reads no rows at all. Records (upload_stalled raised,
    statistics_missing raised) after each cycle. The sun never sets (see
    _run_dying_cycles)."""
    clock = {"now": NOW}
    reg = ir.async_get(hass)
    seen: list[tuple[bool, bool]] = []
    i = {"n": 0}

    async def fetch(h, ids, start, end, period):
        if i["n"] in empty_cycles:
            return {sid: [] for sid in ids}
        return await _dying_fetch(plan[i["n"]])(h, ids, start, end, period)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow",
              side_effect=lambda: clock["now"]),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
        patch("custom_components.wolta.coordinator.WoltaCoordinator._sun_down_fn",
              lambda self, period_s: lambda start: False),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark.isoformat(),
                         "applied_invert": False,
                         "applied_entities": fingerprint or _fingerprint()})
        for n in range(len(plan)):
            i["n"] = n
            await coordinator._async_update_data()
            seen.append((
                reg.async_get_issue(DOMAIN, coordinator._stalled_issue_id) is not None,
                reg.async_get_issue(DOMAIN, coordinator._stats_issue_id) is not None,
            ))
            clock["now"] = clock["now"] + advance
    return coordinator, seen


_ALL_FIVE = ("sensor.batt_in", "sensor.batt_out", "sensor.grid_in", "sensor.grid_out",
             "sensor.solar")


@pytest.mark.asyncio
async def test_all_sensors_stopping_at_once_raises_upload_stalled(
    hass: HomeAssistant, mock_entry
):
    """Every selected sensor stops 15 min after the bookmark. Nothing lags behind
    anything else, so statistics_missing and energy_empty stay quiet - the stall repair
    is raised once the newest measured period is 12 h old."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    bookmark = NOW - timedelta(hours=6)
    stop = bookmark + timedelta(minutes=15)
    coordinator, seen = await _run_stall_cycles(
        hass, mock_entry, client, bookmark=bookmark,
        plan=[{sid: stop for sid in _ALL_FIVE}] * 4)
    # lags: 5h45, 11h45, 17h45, 23h45
    assert seen == [(False, False), (False, False), (True, False), (True, False)]
    assert coordinator._state["last_uploaded_ts"] == bookmark.isoformat()
    reg = ir.async_get(hass)
    assert reg.async_get_issue(DOMAIN, coordinator._energy_issue_id) is None
    issue = reg.async_get_issue(DOMAIN, f"upload_stalled_{mock_entry.entry_id}")
    assert issue.translation_key == "upload_stalled"
    assert issue.is_fixable is False
    assert issue.translation_placeholders == {
        "since": dt_util.as_local(stop).strftime("%Y-%m-%d %H:%M"),
        "entities": ", ".join(sorted(_ALL_FIVE)),
    }


@pytest.mark.asyncio
async def test_upload_stalled_clears_when_the_sensors_return(hass: HomeAssistant, mock_entry):
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    bookmark = NOW - timedelta(hours=6)
    stop = bookmark + timedelta(minutes=15)
    _, seen = await _run_stall_cycles(
        hass, mock_entry, client, bookmark=bookmark,
        plan=[{sid: stop for sid in _ALL_FIVE}] * 3 + [{}])
    assert [s for s, _ in seen] == [False, False, True, False]


@pytest.mark.asyncio
async def test_a_read_without_any_measured_period_neither_raises_nor_clears_the_stall(
    hass: HomeAssistant, mock_entry
):
    """Nothing measured at all is energy_empty's case: the stall repair stays as it was."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    bookmark = NOW - timedelta(hours=6)
    stop = bookmark + timedelta(minutes=15)
    _, seen = await _run_stall_cycles(
        hass, mock_entry, client, bookmark=bookmark,
        plan=[{sid: stop for sid in _ALL_FIVE}] * 4, empty_cycles=frozenset({3}))
    assert [s for s, _ in seen] == [False, False, True, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("bookmark_age", [timedelta(hours=6), timedelta(days=15)])
async def test_a_healthy_setup_never_stalls(hass: HomeAssistant, mock_entry, bookmark_age):
    """Incremental and heal path (hourly read): every sensor measures to now."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    _, seen = await _run_stall_cycles(
        hass, mock_entry, client, bookmark=NOW - bookmark_age, plan=[{}] * 4)
    assert seen == [(False, False)] * 4


@pytest.mark.asyncio
async def test_fast_cycles_do_not_raise_stall_early(hass: HomeAssistant, mock_entry):
    """Sixty-second cycles (fast poll) only see the stall once the lag itself reaches
    12 h - no cycle count to hit in minutes."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    bookmark = NOW - timedelta(hours=6)
    stop = bookmark + timedelta(minutes=15)
    _, seen = await _run_stall_cycles(
        hass, mock_entry, client, bookmark=bookmark,
        plan=[{sid: stop for sid in _ALL_FIVE}] * 5, advance=timedelta(seconds=60))
    assert [s for s, _ in seen] == [False] * 5


@pytest.mark.asyncio
async def test_no_stall_is_judged_before_home_assistant_has_started(
    hass: HomeAssistant, mock_entry
):
    """The recorder compiles the periods missed while Home Assistant was down only after
    startup has finished, and the first refresh runs during setup. After an outage of
    12 h or more every sensor's newest row is the shutdown, which is not a stall: while
    Home Assistant is starting, the stall is neither raised nor cleared."""
    from homeassistant.core import CoreState

    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    bookmark = NOW - timedelta(hours=20)
    shutdown = bookmark + timedelta(minutes=15)
    hass.set_state(CoreState.starting)
    try:
        _, seen = await _run_stall_cycles(
            hass, mock_entry, client, bookmark=bookmark,
            plan=[{sid: shutdown for sid in _ALL_FIVE}])
    finally:
        hass.set_state(CoreState.running)
    assert seen == [(False, False)]


@pytest.mark.asyncio
async def test_statistics_missing_is_held_while_the_upload_stands_still(
    hass: HomeAssistant, mock_entry
):
    """solar2 dies first and is named after three cycles. Then the rest die: its text
    ("your other selected sensors do") is false, so it is withdrawn while the stall
    stands - its streak kept - and it returns at once when the others measure again
    while solar2 is still dead."""
    _two_solar_entry(hass, mock_entry)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    bookmark = NOW - timedelta(hours=6)
    early = bookmark + timedelta(minutes=15)
    rest = {sid: NOW for sid in _ALL_FIVE}
    dead_later = {"sensor.solar2": early, **rest}
    coordinator, seen = await _run_stall_cycles(
        hass, mock_entry, client, bookmark=bookmark, fingerprint=_TWO_SOLAR_FP,
        advance=timedelta(hours=3),
        plan=[dead_later] * 5 + [{"sensor.solar2": early}])
    # cycles at NOW, +3h, +6h, +9h, +12h (lag 12h: stalled), +15h (others back)
    assert seen == [(False, False), (False, False), (False, True), (False, True),
                    (True, False), (False, True)]
    assert coordinator._state["silent_sensor_cycles"]["sensor.solar2"] == 5


@pytest.mark.asyncio
async def test_coverage_fill_keeps_the_regular_reads_completeness(
    hass: HomeAssistant, mock_entry
):
    """The diagnostics' completeness describes the regular read. A coverage fill in the
    same cycle reads older history and must not overwrite those figures."""
    bookmark = NOW - timedelta(hours=6)
    client = _mock_client()
    client.get_profile = AsyncMock(
        return_value={**BASE_PROFILE, "data_since": bookmark.isoformat()})
    client.put_data = AsyncMock(return_value={"data_since": bookmark.isoformat()})
    hole = (NOW - timedelta(hours=2), NOW - timedelta(hours=2) + timedelta(minutes=10))
    fill_reads: list = []

    async def fetch(h, ids, start, end, period):
        if end > bookmark:  # the regular incremental read
            return _stats_with_hole(start, end, sensor="sensor.grid_in", hole=hole)
        fill_reads.append((start, end, period))  # the coverage fill, before the bookmark
        return _full_stats(start, end, period)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark.isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint()})
        await coordinator._async_update_data()

    assert fill_reads, "the cycle must run a coverage fill for this test to mean anything"
    assert client.put_data.await_count >= 2
    assert coordinator._last_completeness == {
        "incomplete_quarters": 1, "entities": ["sensor.grid_in"]}


# ---------------------------------------------------------------------------
# Complete quarters: every selected sensor measured, no catch-up after a gap
# ---------------------------------------------------------------------------


def _stats_with_hole(start, end, *, sensor: str, hole: tuple[datetime, datetime]) -> dict:
    """Like _energy_stats, but ``sensor`` has no rows in [hole[0], hole[1])."""
    data = _energy_stats(start, end)
    data[sensor] = [r for r in data[sensor]
                    if not (hole[0].timestamp() <= r["start"] < hole[1].timestamp())]
    return data


@pytest.mark.asyncio
async def test_quarter_with_a_missing_grid_period_and_the_catch_up_are_not_sent(
    hass: HomeAssistant, mock_entry
):
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    hole = (NOW - timedelta(hours=2), NOW - timedelta(hours=2) + timedelta(minutes=10))

    async def fetch(h, ids, start, end, period):
        return _stats_with_hole(start, end, sensor="sensor.grid_in", hole=hole)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - timedelta(hours=6)).isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint()})
        await coordinator._async_update_data()

    sent = {r["ts"] for call in client.put_data.call_args_list for r in call.args[1]}
    q = NOW - timedelta(hours=2)
    assert q.isoformat() not in sent                                   # hole inside
    assert (q + timedelta(minutes=15)).isoformat() in sent             # catch-up is 10:10, inside q
    assert (q - timedelta(minutes=15)).isoformat() in sent


@pytest.mark.asyncio
async def test_catch_up_in_the_next_quarter_is_not_sent(hass: HomeAssistant, mock_entry):
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    q = NOW - timedelta(hours=2)
    hole = (q, q + timedelta(minutes=15))                              # whole quarter q

    async def fetch(h, ids, start, end, period):
        return _stats_with_hole(start, end, sensor="sensor.solar", hole=hole)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - timedelta(hours=6)).isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint()})
        await coordinator._async_update_data()

    sent = {r["ts"] for call in client.put_data.call_args_list for r in call.args[1]}
    assert q.isoformat() not in sent
    assert (q + timedelta(minutes=15)).isoformat() not in sent         # carries the catch-up
    assert (q + timedelta(minutes=30)).isoformat() in sent


@pytest.mark.asyncio
async def test_fetch_starts_one_period_early_and_the_first_quarter_is_judged(
    hass: HomeAssistant, mock_entry
):
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    starts = []

    async def fetch(h, ids, start, end, period):
        starts.append((period, start))
        return _energy_stats(start, end)

    bookmark = NOW - timedelta(hours=6)
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark.isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint()})
        await coordinator._async_update_data()

    assert ("5minute", bookmark - timedelta(minutes=5)) in starts
    sent = sorted(r["ts"] for call in client.put_data.call_args_list for r in call.args[1])
    assert sent[0] == bookmark.isoformat()                             # not the early period


@pytest.mark.asyncio
async def test_inverted_battery_judges_the_same_sensors(hass: HomeAssistant, mock_entry):
    """Flipping the direction swaps which sensor feeds which stream, never which
    sensors must be measured."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    from custom_components.wolta.const import CONF_INVERT_BATTERY
    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: True}

    async def fetch(h, ids, start, end, period):
        return _energy_stats(start, end)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - timedelta(hours=6)).isoformat(),
                         "applied_invert": True, "applied_entities": _fingerprint(),
                         "applied_invert_intervals": [[None, None]]})
        await coordinator._async_update_data()

    sent = [r for call in client.put_data.call_args_list for r in call.args[1]]
    assert len(sent) >= 6 * 4 - 1                                     # nothing lost to the flip


@pytest.mark.asyncio
async def test_backfill_switches_resolution_on_a_whole_hour(hass: HomeAssistant, mock_entry):
    """An unaligned switch from hourly to 5-minute statistics would leave the quarter
    straddling it judged complete by the hour but valued by the 5-minute periods after
    the switch - a partial quarter. On a whole hour every quarter's value and
    completeness come from one source."""
    from custom_components.wolta.coordinator import _BACKFILL_DAYS

    now = NOW + timedelta(minutes=7)
    boundary = NOW - timedelta(days=9)                                # floor_hour(now - 9 d)
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())
    calls = []

    async def fetch(h, ids, start, end, period):
        calls.append((period, start, end))
        return _full_stats(start, end, period,
                           {"sensor.grid_in": 2.0 if period == "hour" else 0.1})

    with patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch):
        rows = await coordinator._backfill_rows(now)

    backfill_hour = NOW - timedelta(days=_BACKFILL_DAYS)              # floor_hour(now - 365 d)
    assert calls == [("hour", backfill_hour - timedelta(hours=1), boundary),
                     ("5minute", boundary - timedelta(minutes=5), now)]
    by_ts = {r["ts"]: r for r in rows}
    assert by_ts[boundary.isoformat()]["grid_import_kwh"] == pytest.approx(3 * 0.1)
    before = (boundary - timedelta(minutes=15)).isoformat()
    assert by_ts[before]["grid_import_kwh"] == pytest.approx(2.0 / 4)


@pytest.mark.asyncio
async def test_heal_with_an_unaligned_start_reads_the_hour_before_it(
    hass: HomeAssistant, mock_entry
):
    """The recorder returns rows from the read start on, without flooring: a heal
    starting at :37 must read from the whole hour before, or the window's first quarter
    has no period before it and is never complete."""
    hour = NOW - timedelta(days=15)
    start = hour + timedelta(minutes=37)
    coordinator = await _make_coordinator(hass, mock_entry, _mock_client())
    calls = []

    async def fetch(h, ids, s, e, period):
        calls.append((period, s))
        return _full_stats(s, e, period)

    with patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch):
        rows = await coordinator._heal_rows(start, NOW)

    assert calls == [("hour", hour - timedelta(hours=1))]
    sent = {r["ts"] for r in rows}
    assert (hour + timedelta(minutes=45)).isoformat() in sent        # first quarter in window
    assert (hour + timedelta(minutes=30)).isoformat() not in sent    # before the window


# ---------------------------------------------------------------------------
# Väntande batterikapacitet (spec 2026-09-14 §7.2): stämpeln ur profil-GET:en
# ---------------------------------------------------------------------------

_BATTERY_ISSUE_ID = "battery_needs_input_test_entry_id"


def _battery_profile(status, **over):
    """Profil-GET som backend (api 0.88.0) svarar för en deklarerad men omätt batterirad."""
    return {**BASE_PROFILE, "battery_status": status, **over}


@pytest.mark.asyncio
async def test_battery_needs_input_raises_fixable_issue(hass: HomeAssistant, mock_entry):
    """needs_input = backend har gett upp mätningen → fråga användaren om märkskylten."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=_battery_profile(
        "needs_input", battery_detect={"n_days": 240}))
    _, data = await _sync_refresh(hass, mock_entry, client)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, _BATTERY_ISSUE_ID)
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.translation_placeholders == {"days": "240"}
    assert issue.data["days"] == 240
    assert issue.data["entry_id"] == "test_entry_id"
    # Stämpeln ska nå både dataobjektet och results (sensorernas value_fn ser bara results).
    assert data.battery_status == "needs_input"
    assert data.results["battery_status"] == "needs_input"
    assert data.battery_detect == {"n_days": 240}
    assert data.detect_min_days == 30


@pytest.mark.asyncio
async def test_battery_pending_clears_issue_and_keeps_slow_poll(hass: HomeAssistant, mock_entry):
    """pending = mätningen pågår. Issuen (från en tidigare needs_input) ska bort, och
    väntan får INTE dra upp polltakten – det är serverns mätfönster som rör sig, inte ett jobb."""
    from custom_components.wolta.coordinator import _SLOW_POLL

    ir.async_create_issue(
        hass, DOMAIN, _BATTERY_ISSUE_ID, is_fixable=True,
        severity=ir.IssueSeverity.WARNING, translation_key="battery_needs_input")
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=_battery_profile(
        "pending", battery_detect={"n_days": 4}, detect_min_days=14))
    coordinator, data = await _sync_refresh(hass, mock_entry, client)

    assert ir.async_get(hass).async_get_issue(DOMAIN, _BATTERY_ISSUE_ID) is None
    assert data.battery_status == "pending"
    assert data.detect_min_days == 14
    assert data.pending is False, "väntande kapacitet är ingen jobbstatus"
    assert coordinator.update_interval == _SLOW_POLL


@pytest.mark.asyncio
async def test_battery_state_evaluated_by_side_poll(hass: HomeAssistant, mock_entry):
    """Sidopollen (5 min) måste utvärdera stämpeln också – annars dröjer frågan upp till 6 h.
    Profilen är i övrigt oförändrad, så ingen spegling sker: utvärderingen får inte hänga på den."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=_battery_profile(
        "needs_input", battery_detect={"n_days": 90}))
    coordinator = await _side_poll_coordinator(hass, mock_entry, client)
    await coordinator.async_check_profile_sync()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, _BATTERY_ISSUE_ID)
    assert issue is not None
    assert issue.translation_placeholders == {"days": "90"}
    # Stämpeln ligger inte i entry.data, så _apply_profile_sync ser den inte: utan den
    # egna refreshen skulle statussensorn säga "mäter" i upp till 6 h efter att repairen
    # redan bett om märkskylten.
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_side_poll_survives_malformed_battery_stamp(hass: HomeAssistant, mock_entry, caplog):
    """Sidopollen är en timer-callback och lovar i sin docstring "Never raises" – men
    stämpelläsningen låg UTANFÖR try:t. En missbildad stämpel (icke-dict battery_detect,
    osiffrigt detect_min_days) kastade då AttributeError/ValueError rakt ut i HA:s
    timer var 5:e minut, i all oändlighet: servern skickar samma trasiga rad nästa tick."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=_battery_profile(
        "needs_input", battery_detect="trasig", detect_min_days="x"))
    coordinator = await _side_poll_coordinator(hass, mock_entry, client)

    await coordinator.async_check_profile_sync()  # får inte kasta

    assert "side-poll" in caplog.text.lower() or "battery" in caplog.text.lower()


@pytest.mark.asyncio
async def test_battery_state_unchanged_side_poll_does_not_refresh(hass: HomeAssistant, mock_entry):
    """Oförändrad stämpel får INTE refresha – det vore hela uppladdningscykeln var 5:e minut."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=_battery_profile(
        "needs_input", battery_detect={"n_days": 90}))
    coordinator = await _side_poll_coordinator(hass, mock_entry, client)
    await coordinator.async_check_profile_sync()   # första tick: None → needs_input
    coordinator.async_request_refresh.reset_mock()
    await coordinator.async_check_profile_sync()   # andra tick: oförändrad
    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_needs_input_never_fires_in_view_only(hass: HomeAssistant, mock_entry):
    """Visningsläge äger inte profilen (bunden anläggning) – att be den ägaren om märkdata
    vore att skicka frågan till fel person, och servern 409:ar skrivningen ändå."""
    from custom_components.wolta.const import CONF_VIEW_ONLY

    mock_entry.data = {**ENTRY_DATA, CONF_VIEW_ONLY: True}
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=_battery_profile(
        "needs_input", battery_detect={"n_days": 240}))
    _, data = await _sync_refresh(hass, mock_entry, client)

    assert ir.async_get(hass).async_get_issue(DOMAIN, _BATTERY_ISSUE_ID) is None
    # Stämpeln syns ändå i sensorn – bara repairen är tystad.
    assert data.battery_status == "needs_input"


@pytest.mark.asyncio
async def test_battery_status_absent_on_old_backend(hass: HomeAssistant, mock_entry):
    """Profil utan stämpel (äldre backend) → inget fält, ingen issue, inget krångel."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    _, data = await _sync_refresh(hass, mock_entry, client)

    assert ir.async_get(hass).async_get_issue(DOMAIN, _BATTERY_ISSUE_ID) is None
    assert data.battery_status is None
    assert data.results["battery_status"] is None
    assert data.detect_min_days == 30


@pytest.mark.asyncio
async def test_missbildad_batteristampel_faller_inte_huvudcykeln(hass: HomeAssistant, mock_entry):
    """Sidopollen skyddar _take_battery_state (missbildad rad → debug-logg), men HUVUDcykeln
    gjorde det inte: `int("abc")` på detect_min_days gav UpdateFailed-liknande avbrott av
    hela uppladdningscykeln – för ett visningsfält. Stämpeln ska ignoreras, cykeln gå vidare."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value={
        **BASE_PROFILE, "battery_status": "pending",
        "battery_detect": "trasig", "detect_min_days": "abc",
    })
    coordinator, data = await _sync_refresh(hass, mock_entry, client)
    assert data.results["status"] == RESULTS_PAYLOAD["status"]
    # Ingen halv stämpel: status och dygn sätts atomärt eller inte alls.
    assert data.battery_status is None
    assert data.detect_min_days == 30


# ---------------------------------------------------------------------------
# Husets energibalans (backend !202/!203): repair per orsak när grinden fäller
# ---------------------------------------------------------------------------

_EB_ISSUE_ID = "energy_balance_test_entry_id"


def _eb_results(excluded, cause="solar_low", *, preliminary=False):
    return {"betyg": {"preliminary": preliminary,
                      "energy_balance": {"share": 0.4, "impossible_kwh": 500.0,
                                         "cause": cause, "excluded": excluded}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["solar_low", "import_low", "source_missing", "unclear"])
async def test_energy_balance_issue_fires_per_cause(hass, mock_entry, cause):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_eb_results(True, cause))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _EB_ISSUE_ID)
    assert issue is not None
    assert issue.translation_key == f"energy_balance_{cause}"
    assert issue.is_fixable is False


@pytest.mark.asyncio
async def test_energy_balance_issue_names_the_mapped_sensors(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_eb_results(True, "solar_low"))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _EB_ISSUE_ID)
    assert issue.translation_placeholders["solar"] == "sensor.solar"
    assert issue.translation_placeholders["grid_in"] == "sensor.grid_in"
    assert issue.translation_placeholders["batt_in"] == "sensor.batt_in"


@pytest.mark.asyncio
async def test_energy_balance_issue_follows_backend_not_own_threshold(hass, mock_entry):
    """HACS räknar aldrig om tröskeln: share 0,4 men excluded False → ingen repair."""
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_eb_results(False))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _EB_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_energy_balance_issue_clears_when_balance_recovers(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_eb_results(True))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _EB_ISSUE_ID) is not None
    c._evaluate_measured_params(_eb_results(False))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _EB_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_energy_balance_issue_fires_also_when_preliminary(hass, mock_entry):
    """Adopt-repairerna väntar på moget betyg; balansen gör det inte - backendens golv (en
    hel batteriladdning) skyddar redan korta fönster."""
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_eb_results(True, preliminary=True))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _EB_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_energy_balance_issue_never_in_view_only(hass, mock_entry):
    """Visningsläge: datan kommer från webhooken/bryggan, inte HA-sensorerna - ett råd om
    sensormappning vore riktat till fel person."""
    from custom_components.wolta.const import CONF_VIEW_ONLY
    c = await _cap_coordinator(hass, mock_entry)
    c._view_only = True
    c._evaluate_measured_params(_eb_results(True))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _EB_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_energy_balance_issue_absent_block_or_old_backend(hass, mock_entry):
    """Block saknas (äldre backend) eller saknar excluded → ingen repair, aldrig en gissning."""
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params({"betyg": {}})
    c._evaluate_measured_params({"betyg": {"energy_balance": {"share": 0.9, "cause": "unclear"}}})
    assert ir.async_get(hass).async_get_issue(DOMAIN, _EB_ISSUE_ID) is None


# ---------------------------------------------------------------------------
# Omkastade laddfält: servern ser via batteriets laddningsnivå att laddning och urladdning är förväxlade
# ---------------------------------------------------------------------------

_DIR_ISSUE_ID = "battery_direction_test_entry_id"


def _dir_results(form="all", *, state="reversed", preliminary=False, eb_excluded=False,
                 as_of=None):
    return {"betyg": {
        "preliminary": preliminary,
        "battery_direction": {"state": state, "form": form, "checked_days": 120, "as_of": as_of,
                              "reversed_days": 120, "first_checked": "2025-10-10",
                              "first_reversed": "2025-10-10", "last_reversed": "2026-10-12",
                              "since": "2026-09-29" if form == "since" else None},
        "energy_balance": {"share": 0.4, "impossible_kwh": 500.0, "cause": "solar_low",
                           "excluded": eb_excluded},
        # Mogna uppmätta värden som skiljer sig från konfigurerade: adopt-förslagen skulle
        # annars fyra (se supersede-testet).
        "observed_capacity": {"kwh": 5.0, "n_days": 90, "plateau_days": 20},
        "observed_power": {"kw": 2.0, "n_days": 90},
        "observed_eff": {"eff": 0.7, "n_days": 90},
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("form,fixable", [("all", True), ("partial", True),
                                          ("since", False), ("ended", False)])
async def test_direction_issue_per_form(hass, mock_entry, form, fixable):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_dir_results(form))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID)
    assert issue is not None
    assert issue.translation_key == f"battery_direction_{form}"
    assert issue.is_fixable is fixable
    assert issue.data["form"] == form


@pytest.mark.asyncio
async def test_direction_issue_placeholders(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_dir_results("since"))
    p = ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID).translation_placeholders
    assert p["reversed_days"] == "120" and p["since"] == "2026-09-29"
    assert p["first_checked"] == "2025-10-10" and p["last_reversed"] == "2026-10-12"
    assert p["batt_in"] == "sensor.batt_in" and p["batt_out"] == "sensor.batt_out"
    assert "invert_state" not in p


@pytest.mark.asyncio
async def test_direction_issue_data_carries_placeholders_for_the_fix_flow(hass, mock_entry):
    """En egen RepairsFlow ärver inte issuens translation_placeholders: flödets texter
    behöver samma värden på issue.data."""
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_dir_results("all"))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID)
    assert issue.data["entry_id"] == "test_entry_id" and issue.data["form"] == "all"
    for k, v in issue.translation_placeholders.items():
        assert issue.data[k] == v, k
    assert issue.data["reversed_days"] == "120"
    assert issue.data["batt_in"] == "sensor.batt_in"


@pytest.mark.asyncio
async def test_direction_issue_clears_and_ignores_other_states(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_dir_results("all"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None
    for st in ("ok", "verified", "unknown"):
        c._evaluate_measured_params(_dir_results("all", state=st))
        assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None, st
    c._evaluate_measured_params({"betyg": {}})
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_direction_issue_fires_when_preliminary_but_never_in_view_only(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_dir_results("all", preliminary=True))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None
    c._view_only = True
    c._evaluate_measured_params(_dir_results("all"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_direction_supersedes_energy_balance_and_adopt_repairs(hass, mock_entry):
    """Omkastad riktning: energibalansens orsakstext och adopt-förslagen bygger på felvända
    flöden. De tas bort i samma utvärdering, även om de var öppna."""
    keys = ("measured_capacity", "measured_power", "measured_efficiency")
    c = await _cap_coordinator(hass, mock_entry, **{_CBK: 15.0, _CBKW: 5.0, _CEFF: 0.9})
    reg = ir.async_get(hass)
    # Utan omkastning fyrar adopt-förslagen och energibalansen (annars bevisar testet inget).
    c._evaluate_measured_params(_dir_results("all", state="ok", eb_excluded=True))
    assert reg.async_get_issue(DOMAIN, _EB_ISSUE_ID) is not None
    for key in keys:
        assert reg.async_get_issue(DOMAIN, f"{key}_test_entry_id") is not None, key
    c._evaluate_measured_params(_dir_results("all", eb_excluded=True))
    assert reg.async_get_issue(DOMAIN, _EB_ISSUE_ID) is None
    for key in keys:
        assert reg.async_get_issue(DOMAIN, f"{key}_test_entry_id") is None, key
    assert reg.async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_direction_clears_leftover_adopt_issues_when_preliminary(hass, mock_entry):
    """Ett preliminärt men omkastat betyg (rest av ett moget) ska också radera adopt-issues
    som blev kvar: de är rekonstruerade ur samma felvända flöden."""
    keys = ("measured_capacity", "measured_power", "measured_efficiency")
    c = await _cap_coordinator(hass, mock_entry, **{_CBK: 15.0, _CBKW: 5.0, _CEFF: 0.9})
    reg = ir.async_get(hass)
    c._evaluate_measured_params(_dir_results("all", state="ok"))
    for key in keys:
        assert reg.async_get_issue(DOMAIN, f"{key}_test_entry_id") is not None, key
    c._evaluate_measured_params(_dir_results("all", preliminary=True))
    for key in keys:
        assert reg.async_get_issue(DOMAIN, f"{key}_test_entry_id") is None, key
    assert reg.async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_direction_issue_is_per_entry_and_healthy_entry_leaves_other_alone(hass, mock_entry):
    """Issue-id bär entry-id: en frisk entry raderar bara sitt eget id."""
    c = await _cap_coordinator(hass, mock_entry)
    ir.async_create_issue(hass, DOMAIN, "battery_direction_other_entry", is_fixable=False,
                          severity=ir.IssueSeverity.WARNING,
                          translation_key="battery_direction_since",
                          translation_placeholders={})
    c._evaluate_measured_params(_dir_results("all", state="ok"))
    reg = ir.async_get(hass)
    assert reg.async_get_issue(DOMAIN, "battery_direction_other_entry") is not None
    assert reg.async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None


# ---------------------------------------------------------------------------
# SoC upload (spec 2026-10-03): optional state-of-charge sensors are read from
# the recorder's mean/min/max statistics and sent with the flow rows as
# battery_state (+ battery_sources). Three things matter:
#   - without SoC sensors put_data keeps its exact two-argument call, so the
#     fleet default is byte-identical to the pre-feature output;
#   - a failing SoC read never costs the flows (they are the product);
#   - the SoC choice NEVER touches the flow fingerprint or the flow bookmark: a
#     flow re-backfill would overwrite ~a year of quarter data (from 5-minute
#     statistics HA has since purged) with hourly values / 4 and recompute the
#     grade. Adding a sensor instead runs a separate SoC-only backfill.
# ---------------------------------------------------------------------------

_SOC_ENTITY = "sensor.soc"
_SOC_HOUR = datetime(2025, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
_SOC_QUARTER = datetime(2025, 6, 1, 11, 30, 0, tzinfo=timezone.utc)


async def _run_soc_cycle(hass, mock_entry, client, measurement, store_state=None):
    """One cycle with flows mocked to a fixed row set and a patched SoC read.

    Returns (coordinator, async_fetch_change periods, async_fetch_measurement mock)."""
    change_periods: list[str] = []

    async def mock_change(h, ids, start, end, period):
        change_periods.append(period)
        return _EMPTY_BATT_STATS

    measurement_mock = AsyncMock(side_effect=measurement)
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=mock_change),
        patch("custom_components.wolta.coordinator.async_fetch_measurement", measurement_mock),
        patch("custom_components.wolta.coordinator.merge_streams",
              return_value=_make_rows(NOW - timedelta(hours=1))),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client, store_state=store_state if store_state is not None else {})
        await coordinator._async_update_data()
    return coordinator, change_periods, measurement_mock


async def _soc_measurement(h, ids, start, end, period):
    if period == "hour":
        return {_SOC_ENTITY: [{"start": _SOC_HOUR.timestamp(), "mean": 55.0,
                               "min": 40.0, "max": 70.0}]}
    q = _SOC_QUARTER.timestamp()
    return {_SOC_ENTITY: [
        {"start": q + 0, "mean": 50.0, "min": 49.0, "max": 51.0},
        {"start": q + 300, "mean": 52.0, "min": 51.0, "max": 53.0},
        {"start": q + 600, "mean": 54.0, "min": 53.0, "max": 55.0},
    ]}


@pytest.mark.asyncio
async def test_soc_sent_with_flows_and_registry_source(hass: HomeAssistant, mock_entry):
    """First run: hourly SoC for the long window, quarters from 5-minute statistics for
    the recent one, both sent next to the flows; battery_sources names the integration
    that owns each sensor, taken from the entity registry."""
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", "testplatform", "soc-uid", suggested_object_id="soc")
    assert reg.async_get(_SOC_ENTITY) is not None
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()

    await _run_soc_cycle(hass, mock_entry, client, _soc_measurement)

    client.put_data.assert_awaited_once()
    args, kwargs = client.put_data.call_args
    assert args[0] == TOKEN
    assert len(args[1]) == 4  # the flow rows are untouched
    by_period = {r["period_s"]: r for r in kwargs["battery_state"]}
    assert set(by_period) == {3600, 900}
    assert by_period[3600]["unit"] == _SOC_ENTITY
    assert by_period[3600]["ts"] == _SOC_HOUR.isoformat()
    assert (by_period[3600]["soc_mean"], by_period[3600]["soc_min"],
            by_period[3600]["soc_max"]) == (55.0, 40.0, 70.0)
    assert by_period[900]["ts"] == _SOC_QUARTER.isoformat()
    assert (by_period[900]["soc_mean"], by_period[900]["soc_min"],
            by_period[900]["soc_max"]) == (52.0, 49.0, 55.0)
    assert kwargs["battery_sources"] == [
        {"unit": _SOC_ENTITY, "source": f"ha:testplatform:{_SOC_ENTITY}",
         "semantics": "unknown"}
    ]


@pytest.mark.asyncio
async def test_soc_source_falls_back_to_unknown_platform(hass: HomeAssistant, mock_entry):
    """A sensor that is not in the entity registry (e.g. a YAML-only statistic) still gets
    a source row - the API requires one per unit - with 'unknown' as the platform."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: ["sensor.not_registered"]}
    client = _mock_client()

    async def measurement(h, ids, start, end, period):
        return {}

    coordinator, _, _ = await _run_soc_cycle(hass, mock_entry, client, measurement)

    assert coordinator._soc_sources() == [
        {"unit": "sensor.not_registered", "source": "ha:unknown:sensor.not_registered",
         "semantics": "unknown"}
    ]


@pytest.mark.asyncio
async def test_no_soc_sensors_keeps_exact_two_argument_put(hass: HomeAssistant, mock_entry):
    """Fleet default: no SoC sensor -> put_data(token, rows) with exactly two arguments, and
    the recorder's measurement statistics are never queried."""
    client = _mock_client()

    async def measurement(h, ids, start, end, period):
        raise AssertionError("must not read SoC statistics")

    _, _, measurement_mock = await _run_soc_cycle(hass, mock_entry, client, measurement)

    client.put_data.assert_awaited_once()
    args, kwargs = client.put_data.call_args
    assert len(args) == 2 and kwargs == {}
    measurement_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_soc_fetch_failure_still_uploads_flows(hass: HomeAssistant, mock_entry):
    """The flows are the product: a SoC statistics read that raises must not stop them -
    they go up without battery_state (and without battery_sources)."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()

    async def measurement(h, ids, start, end, period):
        raise RuntimeError("recorder busy")

    coordinator, _, measurement_mock = await _run_soc_cycle(
        hass, mock_entry, client, measurement)

    measurement_mock.assert_awaited()
    client.put_data.assert_awaited_once()
    args, kwargs = client.put_data.call_args
    assert len(args[1]) == 4
    assert kwargs == {}
    # The bookmark still advances: the flows were delivered.
    assert coordinator._state.get("last_uploaded_ts") is not None


_SETTLED_STATE = {"last_uploaded_ts": _RECENT_BOOKMARK, "applied_invert": False,
                  "applied_entities": _fingerprint()}


async def _run_soc_cycles(hass, mock_entry, client, measurements, store_state):
    """Several cycles on ONE coordinator (state carries over), one SoC read per cycle.

    Returns (coordinator, async_fetch_change periods per cycle, measurement calls per
    cycle as (start, end, period), exceptions per cycle (None when the cycle returned))."""
    change_periods: list[list[str]] = []
    soc_calls: list[list[tuple]] = []
    errors: list[BaseException | None] = []
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state=store_state)
    for measurement in measurements:
        periods: list[str] = []
        calls: list[tuple] = []

        async def mock_change(h, ids, start, end, period, _p=periods):
            _p.append(period)
            return _EMPTY_BATT_STATS

        async def mock_measurement(h, ids, start, end, period, _c=calls, _m=measurement):
            _c.append((start, end, period))
            return await _m(h, ids, start, end, period)

        with (
            patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
            patch("custom_components.wolta.coordinator.async_fetch_change",
                  side_effect=mock_change),
            patch("custom_components.wolta.coordinator.async_fetch_measurement",
                  side_effect=mock_measurement),
            patch("custom_components.wolta.coordinator.merge_streams",
                  return_value=_make_rows(NOW - timedelta(hours=1))),
        ):
            try:
                await coordinator._async_update_data()
            except Exception as err:  # noqa: BLE001 - the test inspects it
                errors.append(err)
            else:
                errors.append(None)
        change_periods.append(periods)
        soc_calls.append(calls)
    return coordinator, change_periods, soc_calls, errors


async def _soc_raises(h, ids, start, end, period):
    raise RuntimeError("recorder busy")


@pytest.mark.asyncio
@pytest.mark.parametrize("applied_soc", ["absent", "empty"])
async def test_adding_soc_sensor_backfills_soc_without_touching_flows(
    hass: HomeAssistant, mock_entry, applied_soc
):
    """Adding a SoC sensor (or upgrading with one chosen) on an entry with a bookmark:
    the bookmark and the flow fingerprint stay, the flows are read only for the
    incremental window, and the SoC history for the whole backfill window (hourly before
    now - 9 d) rides along with them. Afterwards the flag is cleared."""
    from custom_components.wolta.coordinator import _BACKFILL_DAYS, _SHORT_TERM_DAYS

    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()
    state = dict(_SETTLED_STATE)
    if applied_soc == "empty":
        state["applied_soc"] = []

    coordinator, change_periods, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement], state)

    assert errors == [None]
    # Flows: incremental only - no hourly LTS read, i.e. _backfill_rows never ran.
    assert change_periods == [["5minute"]]
    assert coordinator._state["applied_entities"] == _fingerprint()
    assert "pending_invert_recompute" not in coordinator._state
    # SoC: the whole backfill window, hourly part first.
    assert soc_calls[0] == [
        (NOW - timedelta(days=_BACKFILL_DAYS), NOW - timedelta(days=_SHORT_TERM_DAYS), "hour"),
        (NOW - timedelta(days=_SHORT_TERM_DAYS), NOW, "5minute"),
    ]
    client.put_data.assert_awaited_once()
    args, kwargs = client.put_data.call_args
    assert args == (TOKEN, _make_rows(NOW - timedelta(hours=1)))
    hourly = [r for r in kwargs["battery_state"] if r["period_s"] == 3600]
    assert [r["ts"] for r in hourly] == [_SOC_HOUR.isoformat()]
    # The bookmark moved exactly as a plain incremental upload moves it.
    assert coordinator._state["last_uploaded_ts"] == args[1][-1]["ts"]
    assert coordinator._state["applied_soc"] == [_SOC_ENTITY]
    assert not coordinator._state.get("soc_backfill_pending")


@pytest.mark.asyncio
async def test_soc_backfill_flag_survives_failed_read_and_clears_next_cycle(
    hass: HomeAssistant, mock_entry
):
    """A SoC read that raises costs neither the flows nor the backfill: the flows go up
    without SoC, the flag stays, and the next cycle retries the whole SoC window."""
    from custom_components.wolta.coordinator import _BACKFILL_DAYS

    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()

    coordinator, change_periods, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_raises, _soc_measurement],
        {**_SETTLED_STATE, "applied_soc": []})

    assert errors == [None, None]
    assert all("hour" not in periods for periods in change_periods)
    first, second = client.put_data.call_args_list
    assert first.kwargs == {} and len(first.args) == 2
    # Cycle 2 still asks for the full SoC window - the flag survived cycle 1.
    assert soc_calls[1][0] == (NOW - timedelta(days=_BACKFILL_DAYS),
                               soc_calls[1][0][1], "hour")
    assert any(r["period_s"] == 3600 for r in second.kwargs["battery_state"])
    assert not coordinator._state.get("soc_backfill_pending")


@pytest.mark.asyncio
async def test_soc_backfill_flag_survives_failed_put(hass: HomeAssistant, mock_entry):
    """A PUT that fails (not the 422 safety net) delivers nothing - neither the bookmark
    nor the SoC flag may move."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client(raise_on_put=WoltaApiError("server error", status=500))

    coordinator, _, _, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement], {**_SETTLED_STATE, "applied_soc": []})

    assert isinstance(errors[0], WoltaApiError)
    assert coordinator._state["last_uploaded_ts"] == _RECENT_BOOKMARK
    assert coordinator._state["soc_backfill_pending"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before, after",
    [([_SOC_ENTITY], []), ([_SOC_ENTITY, "sensor.soc_2"], [_SOC_ENTITY])],
    ids=["remove_all", "remove_one"],
)
async def test_removing_soc_sensors_never_backfills(
    hass: HomeAssistant, mock_entry, before, after
):
    """Removing sensors only stops collecting them: no SoC backfill, no flow re-backfill,
    fingerprint unchanged."""
    mock_entry.data = {**ENTRY_DATA, **({CONF_SOC: after} if after else {})}
    client = _mock_client()

    coordinator, change_periods, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement],
        {**_SETTLED_STATE, "applied_soc": sorted(before)})

    assert errors == [None]
    assert change_periods == [["5minute"]]
    assert all(period != "hour" for _, _, period in soc_calls[0])
    assert coordinator._state["applied_entities"] == _fingerprint()
    assert coordinator._state["applied_soc"] == sorted(after)
    assert not coordinator._state.get("soc_backfill_pending")
    assert "pending_invert_recompute" not in coordinator._state
    assert coordinator._state["last_uploaded_ts"] != _RECENT_BOOKMARK  # moved, not reset


@pytest.mark.asyncio
async def test_soc_sensor_over_api_length_limits_is_left_out_of_rows_and_sources(
    hass: HomeAssistant, mock_entry
):
    """One rule for rows AND sources: a sensor whose entity id exceeds 128 characters
    (`unit`), or whose source label 'ha:<platform>:<entity_id>' exceeds 200, is left out
    of the read, the rows and battery_sources alike. The server 422s the whole call, flows
    included, on a too-long `unit`/`source` and on rows whose unit has no source in the
    call (unless it already stores one for the unit - we always send them); a source
    without rows is only a no-op. So the rows and the sources must agree."""
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", "testplatform", "soc-uid", suggested_object_id="soc")
    # entity id 120 chars (fine as a unit) but source 'ha:' + 80 + ':' + 120 = 204 chars.
    long_source = reg.async_get_or_create(
        "sensor", "p" * 80, "soc-long-src", suggested_object_id="s" * 113).entity_id
    assert len(long_source) == 120
    long_unit = "sensor." + "u" * 122  # 129 chars, not in the registry
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY, long_source, long_unit]}
    client = _mock_client()
    asked: list[set] = []

    async def measurement(h, ids, start, end, period):
        asked.append(set(ids))
        q = _SOC_QUARTER.timestamp()
        return {e: [{"start": q + k, "mean": 50.0, "min": 49.0, "max": 51.0}
                    for k in (0, 300, 600)] for e in ids}

    await _run_soc_cycles(hass, mock_entry, client, [measurement],
                          {**_SETTLED_STATE, "applied_soc": sorted(
                              [_SOC_ENTITY, long_source, long_unit])})

    assert asked and all(ids == {_SOC_ENTITY} for ids in asked)
    _, kwargs = client.put_data.call_args
    assert {r["unit"] for r in kwargs["battery_state"]} == {_SOC_ENTITY}
    assert [s["unit"] for s in kwargs["battery_sources"]] == [_SOC_ENTITY]


@pytest.mark.asyncio
async def test_put_with_soc_rejected_422_resends_flows_without_soc(
    hass: HomeAssistant, mock_entry
):
    """Safety net (spec §6.5, §16): if the server 422s a call that carried SoC, the same
    flow rows go up again WITHOUT SoC (exactly two arguments). The bookmark moves as
    usual; the SoC backfill stays pending since its rows were not delivered."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client(raise_on_put=[WoltaApiError("unprocessable", status=422), None])

    coordinator, _, _, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement], {**_SETTLED_STATE, "applied_soc": []})

    assert errors == [None]
    first, second = client.put_data.call_args_list
    assert "battery_state" in first.kwargs
    assert second.args == first.args and second.kwargs == {} and len(second.args) == 2
    assert coordinator._state["last_uploaded_ts"] == first.args[1][-1]["ts"]
    assert coordinator._state["soc_backfill_pending"] is True


@pytest.mark.asyncio
async def test_put_without_soc_rejected_422_is_not_retried(hass: HomeAssistant, mock_entry):
    """The safety net is only for calls that carried SoC: a flow-only 422 behaves as
    before - raised, one call, bookmark untouched."""
    client = _mock_client(raise_on_put=WoltaApiError("unprocessable", status=422))

    coordinator, _, _, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement], dict(_SETTLED_STATE))

    assert isinstance(errors[0], WoltaApiError)
    client.put_data.assert_awaited_once()
    assert coordinator._state["last_uploaded_ts"] == _RECENT_BOOKMARK


@pytest.mark.asyncio
async def test_upgrade_without_soc_sensors_records_empty_choice_only(
    hass: HomeAssistant, mock_entry
):
    """Upgrade safety: an install without SoC sensors gets applied_soc=[] and nothing
    else - no flag, no measurement read, no hourly read, a two-argument PUT and the
    fingerprint byte-identical to the pre-feature one."""
    client = _mock_client()

    coordinator, change_periods, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement], dict(_SETTLED_STATE))

    assert errors == [None]
    assert change_periods == [["5minute"]]
    assert soc_calls == [[]]
    assert coordinator._state["applied_soc"] == []
    assert "soc_backfill_pending" not in coordinator._state
    assert coordinator._state["applied_entities"] == _fingerprint()
    args, kwargs = client.put_data.call_args
    assert len(args) == 2 and kwargs == {}


# ---------------------------------------------------------------------------
# SoC bookmark (soc_uploaded_ts): outside a pending SoC backfill the SoC read starts at
# its OWN bookmark, never at the flow bookmark. Before this, a SoC read that failed (or
# a 422 that dropped SoC) still let last_uploaded_ts move, and the next cycle read
# 5-minute statistics only from the new flow bookmark - the quarters in between were
# never read again although HA still holds them for ~10 days. The SoC bookmark moves only
# when the SoC read worked AND the call carrying it went through, and it never touches
# last_uploaded_ts, pending_invert_recompute or the flow fingerprint.
# ---------------------------------------------------------------------------

_OLD_SOC_BOOKMARK = (NOW - timedelta(hours=3)).isoformat()
# After a delivered read the bookmark is the latest row start minus a one-hour re-read
# margin (late compiles), never earlier than the previous bookmark.
_SOC_BOOKMARK_AFTER = (_SOC_QUARTER - timedelta(hours=1)).isoformat()


def _soc_settled_state(**extra) -> dict:
    """Flows settled, SoC choice already recorded, SoC bookmark three hours behind."""
    return {**_SETTLED_STATE, "applied_soc": [_SOC_ENTITY],
            "soc_uploaded_ts": _OLD_SOC_BOOKMARK, **extra}


@pytest.mark.asyncio
async def test_transient_soc_read_failure_keeps_soc_bookmark_and_rereads_gap(
    hass: HomeAssistant, mock_entry
):
    """Cycle 1: the SoC read raises. The flows go up and the flow bookmark moves, but the
    SoC bookmark stays. Cycle 2 reads from the OLD SoC bookmark - not from the moved flow
    bookmark - so the quarters of cycle 1 are not lost; when it is delivered the SoC
    bookmark moves to the latest quarter that was read."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()

    coordinator, _, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_raises, _soc_measurement], _soc_settled_state())

    assert errors == [None, None]
    assert coordinator._state["last_uploaded_ts"] != _RECENT_BOOKMARK  # flows moved
    # Cycle 1 asked from the old bookmark; the failure left it where it was.
    assert soc_calls[0] == [(datetime.fromisoformat(_OLD_SOC_BOOKMARK), NOW, "5minute")]
    # Cycle 2 asks from the SAME old bookmark although last_uploaded_ts has moved.
    assert soc_calls[1] == [(datetime.fromisoformat(_OLD_SOC_BOOKMARK), NOW, "5minute")]
    first, second = client.put_data.call_args_list
    assert first.kwargs == {} and len(first.args) == 2
    assert [r["ts"] for r in second.kwargs["battery_state"]] == [_SOC_QUARTER.isoformat()]
    assert coordinator._state["soc_uploaded_ts"] == _SOC_BOOKMARK_AFTER


@pytest.mark.asyncio
async def test_soc_rejected_422_does_not_move_soc_bookmark(hass: HomeAssistant, mock_entry):
    """The 422 safety net re-sends the flows without SoC: the flow bookmark moves, the SoC
    bookmark does not - its rows were not delivered."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client(raise_on_put=[WoltaApiError("unprocessable", status=422), None])

    coordinator, _, _, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement], _soc_settled_state())

    assert errors == [None]
    assert coordinator._state["last_uploaded_ts"] != _RECENT_BOOKMARK
    assert coordinator._state["soc_uploaded_ts"] == _OLD_SOC_BOOKMARK


@pytest.mark.asyncio
async def test_failed_put_does_not_move_soc_bookmark(hass: HomeAssistant, mock_entry):
    """A PUT that fails delivers nothing: neither bookmark moves."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client(raise_on_put=WoltaApiError("server error", status=500))

    coordinator, _, _, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement], _soc_settled_state())

    assert isinstance(errors[0], WoltaApiError)
    assert coordinator._state["last_uploaded_ts"] == _RECENT_BOOKMARK
    assert coordinator._state["soc_uploaded_ts"] == _OLD_SOC_BOOKMARK


@pytest.mark.asyncio
async def test_heal_branch_reads_soc_quarters_from_soc_bookmark_not_hours(
    hass: HomeAssistant, mock_entry
):
    """The flow heal branch (flow bookmark > 9 days old) used to hand SoC an all-hourly
    window. With a fresh SoC bookmark the SoC read is 5-minute quarters from that
    bookmark, whatever the flow branch."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()
    stale_flows = (NOW - timedelta(days=12)).isoformat()

    coordinator, change_periods, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement],
        _soc_settled_state(last_uploaded_ts=stale_flows))

    assert errors == [None]
    assert "hour" in change_periods[0]  # the flows really took the heal branch
    assert soc_calls[0] == [(datetime.fromisoformat(_OLD_SOC_BOOKMARK), NOW, "5minute")]
    assert coordinator._state["soc_uploaded_ts"] == _SOC_BOOKMARK_AFTER


@pytest.mark.asyncio
async def test_old_soc_bookmark_reads_hours_then_quarters(hass: HomeAssistant, mock_entry):
    """A SoC bookmark older than the 5-minute retention reads hourly statistics up to
    now - 9 days and quarters after it (the same split as the backfill)."""
    from custom_components.wolta.coordinator import _SHORT_TERM_DAYS

    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()
    old = NOW - timedelta(days=20)

    _, _, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement],
        _soc_settled_state(soc_uploaded_ts=old.isoformat()))

    assert errors == [None]
    assert soc_calls[0] == [
        (old, NOW - timedelta(days=_SHORT_TERM_DAYS), "hour"),
        (NOW - timedelta(days=_SHORT_TERM_DAYS), NOW, "5minute"),
    ]


@pytest.mark.asyncio
async def test_hourly_soc_read_starts_on_a_whole_hour(hass: HomeAssistant, mock_entry):
    """HA's long-term statistics have rows that START on the hour. A bookmark in the middle
    of an hour (10:30) used as the window start would miss the 10:00-11:00 row, since its
    start lies before the window - and the next bookmark would then sit past it for good.
    The hourly read is therefore floored to the whole hour (UTC); the 5-minute read keeps
    the bookmark as it is, its rows start every five minutes."""
    from custom_components.wolta.coordinator import _SHORT_TERM_DAYS

    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()
    bookmark = (NOW - timedelta(days=20)).replace(hour=10, minute=30)

    _, _, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement],
        _soc_settled_state(soc_uploaded_ts=bookmark.isoformat()))

    assert errors == [None]
    assert soc_calls[0] == [
        (bookmark.replace(minute=0), NOW - timedelta(days=_SHORT_TERM_DAYS), "hour"),
        (NOW - timedelta(days=_SHORT_TERM_DAYS), NOW, "5minute"),
    ]


@pytest.mark.asyncio
async def test_quarter_soc_read_keeps_a_mid_hour_bookmark(hass: HomeAssistant, mock_entry):
    """The flooring is for the hourly read only: a recent bookmark (inside the 5-minute
    retention) goes to the 5-minute read untouched."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()
    bookmark = (NOW - timedelta(days=1)).replace(minute=30)

    _, _, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement],
        _soc_settled_state(soc_uploaded_ts=bookmark.isoformat()))

    assert errors == [None]
    assert soc_calls[0] == [(bookmark, NOW, "5minute")]


@pytest.mark.asyncio
async def test_soc_bookmark_never_reaches_further_back_than_the_backfill_window(
    hass: HomeAssistant, mock_entry
):
    """Floor: a bookmark older than the backfill window (a client that sat off for a year)
    is read from now - 365 days at the earliest."""
    from custom_components.wolta.coordinator import _BACKFILL_DAYS, _SHORT_TERM_DAYS

    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()

    _, _, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement],
        _soc_settled_state(soc_uploaded_ts=(NOW - timedelta(days=500)).isoformat()))

    assert errors == [None]
    assert soc_calls[0][0] == (NOW - timedelta(days=_BACKFILL_DAYS),
                               NOW - timedelta(days=_SHORT_TERM_DAYS), "hour")


@pytest.mark.asyncio
async def test_first_soc_cycle_without_pending_backfill_starts_at_flow_bookmark(
    hass: HomeAssistant, mock_entry
):
    """No SoC bookmark yet and no pending backfill (a state written before the bookmark
    existed): the first read follows the flow branch's start, exactly as before."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()
    state = {**_SETTLED_STATE, "applied_soc": [_SOC_ENTITY]}

    coordinator, _, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement], state)

    assert errors == [None]
    assert soc_calls[0] == [(datetime.fromisoformat(_RECENT_BOOKMARK), NOW, "5minute")]
    assert coordinator._state["soc_uploaded_ts"] == _SOC_BOOKMARK_AFTER


def _soc_quarter_rows(quarter: datetime) -> list[dict]:
    base = quarter.timestamp()
    return [{"start": base + k, "mean": 50.0, "min": 49.0, "max": 51.0}
            for k in (0, 300, 600)]


async def _soc_one_stale(h, ids, start, end, period):
    """sensor.soc reports up to 11:30; sensor.soc_2 died three days ago."""
    return {_SOC_ENTITY: _soc_quarter_rows(_SOC_QUARTER),
            "sensor.soc_2": _soc_quarter_rows(NOW - timedelta(days=3))}


@pytest.mark.asyncio
async def test_silent_sensor_does_not_pin_the_soc_bookmark(hass: HomeAssistant, mock_entry):
    """A selected SoC sensor that stopped reporting must not hold the window open: the
    bookmark follows the sensor that is current (latest row minus the one-hour margin), so
    the next cycle reads from there - not from the dead sensor's last row, which would be
    re-read and re-uploaded every cycle, an ever-growing span, up to the 365-day floor."""
    both = sorted([_SOC_ENTITY, "sensor.soc_2"])
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: both}
    client = _mock_client()

    coordinator, _, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_one_stale, _soc_one_stale],
        _soc_settled_state(applied_soc=both,
                           soc_uploaded_ts=(NOW - timedelta(days=4)).isoformat()))

    assert errors == [None, None]
    assert soc_calls[0][0][0] == NOW - timedelta(days=4)
    assert soc_calls[1][0][0] == datetime.fromisoformat(_SOC_BOOKMARK_AFTER)
    assert coordinator._state["soc_uploaded_ts"] == _SOC_BOOKMARK_AFTER


@pytest.mark.asyncio
async def test_soc_bookmark_never_moves_backwards(hass: HomeAssistant, mock_entry):
    """A previous bookmark later than latest row minus the margin is kept."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()
    later = (NOW - timedelta(minutes=15)).isoformat()  # 11:45, after 11:30 - 1 h

    coordinator, _, _, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement],
        _soc_settled_state(soc_uploaded_ts=later))

    assert errors == [None]
    assert coordinator._state["soc_uploaded_ts"] == later


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["not a timestamp", "2025-06-01T09:00:00"],
                         ids=["garbage", "naive"])
async def test_corrupt_soc_bookmark_falls_back_to_flow_start(
    hass: HomeAssistant, mock_entry, corrupt
):
    """An unreadable (or timezone-naive) bookmark never stops the flows: it counts as
    absent, so the read starts where the flows start, and the bookmark is repaired."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()

    coordinator, _, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement],
        _soc_settled_state(soc_uploaded_ts=corrupt))

    assert errors == [None]
    assert soc_calls[0] == [(datetime.fromisoformat(_RECENT_BOOKMARK), NOW, "5minute")]
    assert coordinator._state["soc_uploaded_ts"] == _SOC_BOOKMARK_AFTER


@pytest.mark.asyncio
async def test_profile_full_413_does_not_move_soc_bookmark(hass: HomeAssistant, mock_entry):
    """A 413 (profile full) persists nothing: neither bookmark moves, no error escapes."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client(raise_on_put=WoltaApiError("too large", status=413))

    coordinator, _, _, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement], _soc_settled_state())

    assert errors == [None]
    assert coordinator._state["last_uploaded_ts"] == _RECENT_BOOKMARK
    assert coordinator._state["soc_uploaded_ts"] == _OLD_SOC_BOOKMARK


@pytest.mark.asyncio
async def test_soc_ok_without_rows_moves_soc_bookmark_to_new_flow_bookmark(
    hass: HomeAssistant, mock_entry
):
    """A sensor with no statistics in the window (read fine, nothing there) must not leave
    a window that grows every cycle: the SoC bookmark follows the flow bookmark."""
    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()

    async def empty(h, ids, start, end, period):
        return {}

    coordinator, _, _, errors = await _run_soc_cycles(
        hass, mock_entry, client, [empty], _soc_settled_state())

    assert errors == [None]
    assert coordinator._state["soc_uploaded_ts"] == coordinator._state["last_uploaded_ts"]
    assert coordinator._state["soc_uploaded_ts"] != _OLD_SOC_BOOKMARK


@pytest.mark.asyncio
async def test_pending_backfill_overrides_soc_bookmark_and_sets_it_after(
    hass: HomeAssistant, mock_entry
):
    """A pending SoC backfill reads the whole window even if a bookmark exists; once it is
    delivered the bookmark is the latest quarter read."""
    from custom_components.wolta.coordinator import _BACKFILL_DAYS, _SHORT_TERM_DAYS

    mock_entry.data = {**ENTRY_DATA, CONF_SOC: [_SOC_ENTITY]}
    client = _mock_client()

    coordinator, _, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement],
        _soc_settled_state(soc_backfill_pending=True))

    assert errors == [None]
    assert soc_calls[0][0] == (NOW - timedelta(days=_BACKFILL_DAYS),
                               NOW - timedelta(days=_SHORT_TERM_DAYS), "hour")
    assert not coordinator._state.get("soc_backfill_pending")
    assert coordinator._state["soc_uploaded_ts"] == _SOC_BOOKMARK_AFTER


@pytest.mark.asyncio
async def test_without_soc_sensors_no_soc_bookmark_and_no_statistics_read(
    hass: HomeAssistant, mock_entry
):
    """Fleet default: no SoC sensor -> no soc_uploaded_ts key, no statistics read, a
    two-argument put_data."""
    client = _mock_client()

    coordinator, _, soc_calls, errors = await _run_soc_cycles(
        hass, mock_entry, client, [_soc_measurement], dict(_SETTLED_STATE))

    assert errors == [None]
    assert soc_calls == [[]]
    assert "soc_uploaded_ts" not in coordinator._state
    args, kwargs = client.put_data.call_args
    assert len(args) == 2 and kwargs == {}


# ---------------------------------------------------------------------------
# soc_missing: a nudge to pick a state-of-charge sensor (v0.41.0)
# ---------------------------------------------------------------------------

_SOC_ISSUE_ID = "soc_missing_test_entry_id"
_MATURE = {"betyg": {"preliminary": False, "score_pct": 64}}


async def _soc_coordinator(hass, mock_entry, *, status="nameplate", **entry_over):
    coordinator = await _cap_coordinator(hass, mock_entry, **entry_over)
    coordinator._battery_status = status
    return coordinator


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["nameplate", "measured"])
async def test_soc_missing_fires_for_a_graded_battery_without_soc(hass, mock_entry, status):
    c = await _soc_coordinator(hass, mock_entry, status=status)
    c._evaluate_soc_missing_issue(_MATURE)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID)
    assert issue is not None
    assert issue.translation_key == "soc_missing"
    assert issue.is_fixable is True
    # The fix flow resolves the entry from the issue data.
    assert issue.data == {"entry_id": "test_entry_id"}


@pytest.mark.asyncio
async def test_soc_missing_not_when_a_sensor_is_chosen(hass, mock_entry):
    from custom_components.wolta.const import CONF_SOC
    c = await _soc_coordinator(hass, mock_entry, **{CONF_SOC: ["sensor.soc"]})
    c._soc_entities = ["sensor.soc"]
    c._evaluate_soc_missing_issue(_MATURE)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_soc_missing_clears_once_a_sensor_is_chosen(hass, mock_entry):
    c = await _soc_coordinator(hass, mock_entry)
    c._evaluate_soc_missing_issue(_MATURE)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is not None
    c._soc_entities = ["sensor.soc"]
    c._evaluate_soc_missing_issue(_MATURE)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_soc_missing_respects_ignore(hass, mock_entry):
    from custom_components.wolta.const import CONF_SOC_ISSUE_IGNORED
    c = await _soc_coordinator(hass, mock_entry, **{CONF_SOC_ISSUE_IGNORED: True})
    c._evaluate_soc_missing_issue(_MATURE)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["none", "pending", "needs_input", None])
async def test_soc_missing_needs_a_known_battery(hass, mock_entry, status):
    """No battery, a battery still being measured, or a stamp we never got: nothing to
    ask about yet."""
    c = await _soc_coordinator(hass, mock_entry, status=status)
    c._evaluate_soc_missing_issue(_MATURE)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("results", [
    {}, {"betyg": None}, {"betyg": {}}, {"betyg": {"preliminary": True, "score_pct": 50}},
])
async def test_soc_missing_waits_for_a_mature_grade(hass, mock_entry, results):
    """A new install was just offered the field in setup; do not ask again until the
    plant has a settled grade."""
    c = await _soc_coordinator(hass, mock_entry)
    c._evaluate_soc_missing_issue(results)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("results", [{}, {"betyg": None}, {"betyg": {}}])
async def test_soc_missing_keeps_the_issue_when_a_cycle_lacks_a_grade(hass, mock_entry, results):
    """A results payload without a grade block says nothing about the plant - it must not
    delete an open repair, or the repair would flap on every such cycle."""
    c = await _soc_coordinator(hass, mock_entry)
    c._evaluate_soc_missing_issue(_MATURE)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is not None
    c._evaluate_soc_missing_issue(results)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is not None
    # A preliminary grade, by contrast, is an answer: the plant is not settled -> clear.
    c._evaluate_soc_missing_issue({"betyg": {"preliminary": True, "score_pct": 50}})
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_soc_missing_never_in_view_only(hass, mock_entry):
    c = await _soc_coordinator(hass, mock_entry)
    c._view_only = True
    c._evaluate_soc_missing_issue(_MATURE)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_soc_missing_is_evaluated_in_the_upload_cycle(hass, mock_entry):
    """The main cycle evaluates it, with the cycle's own results and battery stamp."""
    client = _mock_client(results={**RESULTS_PAYLOAD, **_MATURE})
    client.get_profile = AsyncMock(return_value={**BASE_PROFILE, "battery_status": "measured"})
    await _sync_refresh(hass, mock_entry, client)
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_soc_missing_is_not_persistent_and_only_the_main_cycle_raises_it(hass, mock_entry):
    """The issue is re-derived every main cycle from live state, never persisted: after a
    restart it is absent until the first main cycle re-raises it. The side poll
    (_take_battery_state) updates the battery stamp but must not raise it - the stamp
    alone says nothing about the grade."""
    c = await _soc_coordinator(hass, mock_entry, status=None)
    c._take_battery_state({**BASE_PROFILE, "battery_status": "measured"})
    assert c._battery_status == "measured"
    assert ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID) is None
    c._evaluate_soc_missing_issue(_MATURE)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _SOC_ISSUE_ID)
    assert issue is not None and issue.is_persistent is False


@pytest.mark.asyncio
async def test_direction_issue_respects_ignore_for_fixable_forms(hass, mock_entry):
    from custom_components.wolta.const import CONF_DIRECTION_ISSUE_IGNORED
    for form in ("all", "partial"):
        c = await _cap_coordinator(hass, mock_entry, **{CONF_DIRECTION_ISSUE_IGNORED: True})
        c._evaluate_measured_params(_dir_results(form))
        assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None, form


@pytest.mark.asyncio
async def test_direction_ignore_clears_an_open_issue(hass, mock_entry):
    from custom_components.wolta.const import CONF_DIRECTION_ISSUE_IGNORED
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_dir_results("all"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None
    mock_entry.data = {**mock_entry.data, CONF_DIRECTION_ISSUE_IGNORED: True}
    c._evaluate_measured_params(_dir_results("all"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["since", "ended"])
async def test_direction_ignore_does_not_hide_non_fixable_forms(hass, mock_entry, form):
    """since/ended har ingen meny att ignorera ur: de visas alltid."""
    from custom_components.wolta.const import CONF_DIRECTION_ISSUE_IGNORED
    c = await _cap_coordinator(hass, mock_entry, **{CONF_DIRECTION_ISSUE_IGNORED: True})
    c._evaluate_measured_params(_dir_results(form))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None


_FLIPPED = "2026-10-12T10:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("as_of", ["2026-10-12T04:00:00+00:00", _FLIPPED, None, "inte-ett-datum"])
async def test_direction_issue_stays_down_until_server_recounts_after_flip(hass, mock_entry, as_of):
    """Efter vändningen räknar servern om riktningsdygnen först nästa natt: ett betyg som är
    äldre än vändningen (eller saknar/har oläsbar as_of) får inte resa reparationen igen."""
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    c = await _cap_coordinator(hass, mock_entry, **{CONF_DIRECTION_FLIPPED_AT: _FLIPPED})
    c._evaluate_measured_params(_dir_results("all", as_of=as_of))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_direction_flip_guard_leaves_registry_alone(hass, mock_entry):
    """Vakten rör varken create eller delete: en issue som finns kvar (annan väg) förblir."""
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_dir_results("all"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None
    mock_entry.data = {**mock_entry.data, CONF_DIRECTION_FLIPPED_AT: _FLIPPED}
    c._evaluate_measured_params(_dir_results("all", as_of="2026-10-12T04:00:00+00:00"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_direction_issue_returns_when_recount_is_newer_than_flip(hass, mock_entry):
    """Servern har räknat om efter vändningen och säger fortfarande omkastat: vändningen hjälpte
    inte, reparationen reses igen."""
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    c = await _cap_coordinator(hass, mock_entry, **{CONF_DIRECTION_FLIPPED_AT: _FLIPPED})
    c._evaluate_measured_params(_dir_results("all", as_of="2026-10-13T04:00:00Z"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_direction_flip_guard_does_not_affect_non_fixable_forms(hass, mock_entry):
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    c = await _cap_coordinator(hass, mock_entry, **{CONF_DIRECTION_FLIPPED_AT: _FLIPPED})
    c._evaluate_measured_params(_dir_results("since", as_of="2026-10-12T04:00:00+00:00"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ok", "verified"])
async def test_direction_healthy_clears_flip_stamp_and_ignore_flag(hass, mock_entry, state):
    from custom_components.wolta.const import (
        CONF_DIRECTION_FLIPPED_AT, CONF_DIRECTION_ISSUE_IGNORED)
    c = await _cap_coordinator(hass, mock_entry, **{
        CONF_DIRECTION_FLIPPED_AT: _FLIPPED, CONF_DIRECTION_ISSUE_IGNORED: True})
    with patch.object(hass.config_entries, "async_update_entry") as upd:
        c._evaluate_measured_params(_dir_results("all", state=state))
    upd.assert_called_once()
    new_data = upd.call_args.kwargs["data"]
    assert CONF_DIRECTION_FLIPPED_AT not in new_data
    assert CONF_DIRECTION_ISSUE_IGNORED not in new_data


@pytest.mark.asyncio
async def test_direction_healthy_without_flags_does_not_write_entry(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry)
    with patch.object(hass.config_entries, "async_update_entry") as upd:
        c._evaluate_measured_params(_dir_results("all", state="ok"))
    upd.assert_not_called()


@pytest.mark.asyncio
async def test_direction_guard_holds_while_a_direction_job_runs(hass, mock_entry):
    """as_of efter vändningen räcker inte medan riktningsjobbet pågår: de lagrade raderna är
    inte alla vända, så betyget beskriver fortfarande den gamla riktningen."""
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    c = await _cap_coordinator(hass, mock_entry, **{CONF_DIRECTION_FLIPPED_AT: _FLIPPED})
    c._state["replacement_job"] = c._new_replacement_job("direction", [[None, None]])
    c._evaluate_measured_params(_dir_results("all", as_of="2026-10-13T04:00:00Z"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None
    c._state.pop("replacement_job")
    c._evaluate_measured_params(_dir_results("all", as_of="2026-10-13T04:00:00Z"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_direction_guard_ignores_the_legacy_recompute_flag(hass, mock_entry):
    """pending_invert_recompute no longer stands for a flip (the server queues the recount
    at commit): with a newer as_of and no job the repair returns."""
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    c = await _cap_coordinator(hass, mock_entry, **{CONF_DIRECTION_FLIPPED_AT: _FLIPPED})
    c._state["pending_invert_recompute"] = True
    c._evaluate_measured_params(_dir_results("all", as_of="2026-10-13T04:00:00Z"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_direction_guard_holds_for_a_direction_job_without_a_flip_stamp(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry)
    c._state["replacement_job"] = c._new_replacement_job("direction", [[None, None]])
    c._evaluate_measured_params(_dir_results("all", as_of="2026-10-13T04:00:00Z"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None


def _swap_results(form, swap, **kw):
    results = _dir_results(form, **kw)
    results["betyg"]["battery_direction"]["swap_intervals"] = swap
    return results


_SWAP_FROM = {"from": "2026-09-28T13:00:00+00:00", "to": None,
              "gap_hours_from": 1, "gap_hours_to": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["since", "ended", "all", "partial"])
async def test_direction_issue_fixable_for_every_form_with_swap_intervals(
    hass, mock_entry, form
):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_swap_results(form, [_SWAP_FROM]))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID)
    assert issue.is_fixable is True
    expected = (f"battery_direction_{form}_flip" if form in ("since", "ended")
                else f"battery_direction_{form}")
    assert issue.translation_key == expected
    assert issue.data["swap_intervals"] == [["2026-09-28T13:00:00+00:00", None]]
    assert issue.translation_placeholders["swap_period"].endswith("→ …")


@pytest.mark.asyncio
@pytest.mark.parametrize("form,key", [
    ("since", "battery_direction_since"), ("ended", "battery_direction_ended"),
    ("all", "battery_direction_no_fix"), ("partial", "battery_direction_no_fix")])
async def test_direction_issue_with_empty_swap_intervals_is_text_only(
    hass, mock_entry, form, key
):
    """No boundary the server could place with evidence: the text without a fix."""
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_swap_results(form, []))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID)
    assert issue.is_fixable is False
    assert issue.translation_key == key
    assert issue.data["swap_intervals"] == []


@pytest.mark.asyncio
async def test_direction_issue_without_the_key_keeps_the_v043_rule(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_dir_results("all"))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID)
    assert issue.is_fixable is True
    assert issue.data["swap_intervals"] == [[None, None]]


@pytest.mark.asyncio
@pytest.mark.parametrize("swap", ["x", [1], [{"from": "not-a-date", "to": None}]])
async def test_direction_issue_with_malformed_swap_intervals_is_not_fixable(
    hass, mock_entry, swap
):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_swap_results("since", swap))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID).is_fixable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("form,key", [
    ("since", "battery_direction_since"), ("ended", "battery_direction_ended"),
    ("all", "battery_direction_no_fix")])
@pytest.mark.parametrize("swap", [
    [{"from": "2026-09-28T13:30:00+00:00", "to": None}],
    [{"from": None, "to": "2026-09-28T13:00:05+00:00"}],
    # Judged in UTC: 14:30 at UTC+01:00 is 13:30 UTC.
    [{"from": "2026-09-28T13:00:00+00:00", "to": "2026-09-30T14:30:00+01:00"}],
])
async def test_direction_issue_with_swap_bounds_off_the_hour_is_text_only(
    hass, mock_entry, form, key, swap
):
    """The direction job swaps whole UTC hours: a bound inside an hour is not offered as
    a fix (it could overlap a window already swapped); the issue only explains."""
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_swap_results(form, swap))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID)
    assert issue.is_fixable is False
    assert issue.translation_key == key
    assert issue.data["swap_intervals"] == []


@pytest.mark.asyncio
async def test_direction_issue_with_swap_bounds_on_the_hour_in_another_offset_is_fixable(
    hass, mock_entry
):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_measured_params(_swap_results(
        "since", [{"from": "2026-09-28T15:00:00+02:00", "to": None}]))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID)
    assert issue.is_fixable is True
    assert issue.data["swap_intervals"] == [["2026-09-28T13:00:00+00:00", None]]


@pytest.mark.asyncio
async def test_direction_ignore_applies_to_a_fixable_since_issue(hass, mock_entry):
    from custom_components.wolta.const import CONF_DIRECTION_ISSUE_IGNORED
    c = await _cap_coordinator(hass, mock_entry, **{CONF_DIRECTION_ISSUE_IGNORED: True})
    c._evaluate_measured_params(_swap_results("since", [_SWAP_FROM]))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None
    c._evaluate_measured_params(_swap_results("since", []))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_direction_guard_holds_for_a_flipped_since_issue(hass, mock_entry):
    """After a flip, with a direction job in Store, the fixable issue is not raised again
    however new as_of is."""
    from custom_components.wolta.const import CONF_DIRECTION_FLIPPED_AT
    c = await _cap_coordinator(hass, mock_entry, **{CONF_DIRECTION_FLIPPED_AT: _FLIPPED})
    c._state["replacement_job"] = c._new_replacement_job(
        "direction", [["2026-09-28T13:00:00+00:00", None]])
    c._evaluate_measured_params(_swap_results("since", [_SWAP_FROM],
                                              as_of="2026-10-14T04:00:00Z"))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _DIR_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_last_quarter_not_yet_compiled_is_sent_next_cycle(
    hass: HomeAssistant, mock_entry
):
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    clock = {"now": NOW}

    async def fetch(h, ids, start, end, period):
        # The recorder has compiled up to now - 10 min: the last quarter has 1 of 3 rows.
        return _energy_stats(start, min(end, clock["now"] - timedelta(minutes=10)))

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow",
              side_effect=lambda: clock["now"]),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - timedelta(hours=1)).isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint()})
        await coordinator._async_update_data()
        first = {r["ts"] for c in client.put_data.call_args_list for r in c.args[1]}
        last_q = NOW - timedelta(minutes=15)
        assert last_q.isoformat() not in first
        clock["now"] = NOW + timedelta(minutes=30)
        await coordinator._async_update_data()

    all_sent = {r["ts"] for c in client.put_data.call_args_list for r in c.args[1]}
    assert last_q.isoformat() in all_sent


@pytest.mark.asyncio
async def test_backfill_quarter_after_short_term_start_is_judged_by_5min_only(
    hass: HomeAssistant, mock_entry
):
    from custom_components.wolta.coordinator import _short_term_start
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    boundary = _short_term_start(NOW)             # whole hour: hourly before, 5-minute after
    q = boundary + timedelta(hours=2)

    async def fetch(h, ids, start, end, period):
        if period == "hour":
            rows, t = [], start.replace(minute=0, second=0, microsecond=0).timestamp()
            while t < end.timestamp():
                rows.append({"start": t, "change": 0.4})
                t += 3600
            return {s: list(rows) for s in ("sensor.batt_in", "sensor.batt_out",
                                            "sensor.grid_in", "sensor.grid_out",
                                            "sensor.solar")}
        return _stats_with_hole(start, end, sensor="sensor.grid_in",
                                hole=(q, q + timedelta(minutes=5)))

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
        rows = await coordinator._backfill_rows(NOW)

    sent = {r["ts"] for r in rows}
    assert q.isoformat() not in sent
    assert (boundary - timedelta(hours=3)).isoformat() in sent    # hourly part still sent
    assert (boundary + timedelta(hours=3)).isoformat() in sent    # intact 5-minute part too


# ---------------------------------------------------------------------------
# Known zeros (spec 6.2): solar at night, a counter that stood still across a gap
# ---------------------------------------------------------------------------
#
# The sun times below are astral's for 59.3 °N 16.5 °E (the mock_entry position):
# 2025-10-11 sunset 15:57 UTC, 2025-10-12 sunrise 05:25 UTC; 2024-12-21 sunrise 07:50,
# sunset 13:54 UTC; 2025-06-01 sunrise 01:50, sunset 19:54 UTC. Solar rows are kept
# only inside a range of UTC hours, so every missing period outside it is either a
# night period or a daylight gap the test names.


def _solar_by_hours(data: dict, lo: float, hi: float, *, sensors=("sensor.solar",),
                    hole: tuple[datetime, datetime] | None = None) -> dict:
    """Keep the solar sensors' rows whose UTC time of day lies in [lo, hi) hours and
    outside ``hole``."""
    def keep(r):
        t = datetime.fromtimestamp(r["start"], tz=timezone.utc)
        h = t.hour + t.minute / 60
        in_hole = hole is not None and hole[0].timestamp() <= r["start"] < hole[1].timestamp()
        return lo <= h < hi and not in_hole
    for sid in sensors:
        data[sid] = [r for r in data[sid] if keep(r)]
    return data


@pytest.mark.asyncio
async def test_solar_without_statistics_at_night_is_sent_as_zero(
    hass: HomeAssistant, mock_entry
):
    """The inverter sleeps at night and leaves no statistics. Night quarters are sent
    with solar 0.0 through the real astral path; a solar gap in daylight is still a gap
    and its quarter and the catch-up quarter are not sent."""
    now = datetime(2025, 10, 12, 14, 0, tzinfo=timezone.utc)
    bookmark = now - timedelta(hours=24)
    hole = (datetime(2025, 10, 12, 11, 0, tzinfo=timezone.utc),
            datetime(2025, 10, 12, 11, 15, tzinfo=timezone.utc))
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    async def fetch(h, ids, start, end, period):
        return _solar_by_hours(_full_stats(start, end, period), 5, 17, hole=hole)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=now),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark.isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint()})
        await coordinator._async_update_data()

    sent = {r["ts"]: r for c in client.put_data.call_args_list for r in c.args[1]}
    night = [datetime(2025, 10, 11, 17, 0, tzinfo=timezone.utc) + timedelta(minutes=15 * k)
             for k in range(48)]                                   # 17:00 - 05:00 UTC
    for q in night:
        assert q.isoformat() in sent, f"night quarter {q} not sent"
        assert sent[q.isoformat()]["solar_kwh"] == 0.0
    morning = datetime(2025, 10, 12, 5, 0, tzinfo=timezone.utc)
    assert morning.isoformat() in sent                             # after the night
    assert sent[(morning + timedelta(hours=4)).isoformat()]["solar_kwh"] == pytest.approx(0.3)
    assert hole[0].isoformat() not in sent                         # daylight gap
    assert hole[1].isoformat() not in sent                         # its catch-up
    assert (hole[1] + timedelta(minutes=15)).isoformat() in sent


@pytest.mark.asyncio
async def test_battery_gap_followed_by_zero_change_is_sent(hass: HomeAssistant, mock_entry):
    """A battery counter that stood still across a gap: the first period after it has
    change 0, so the gap held no energy and its quarters are complete."""
    q = NOW - timedelta(hours=2)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    async def fetch(h, ids, start, end, period):
        data = _stats_with_hole(start, end, sensor="sensor.batt_in",
                                hole=(q, q + timedelta(minutes=15)))
        data["sensor.batt_in"] = [
            {**r, "change": 0.0} if r["start"] == (q + timedelta(minutes=15)).timestamp()
            else r
            for r in data["sensor.batt_in"]]
        return data

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - timedelta(hours=6)).isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint()})
        await coordinator._async_update_data()

    sent = {r["ts"]: r for c in client.put_data.call_args_list for r in c.args[1]}
    assert q.isoformat() in sent
    assert sent[q.isoformat()]["batt_charged_kwh"] == 0.0
    assert (q + timedelta(minutes=15)).isoformat() in sent
    assert sent[(q + timedelta(minutes=15)).isoformat()]["batt_charged_kwh"] == pytest.approx(0.2)


def _batteries_stand_still(data: dict, gap: tuple[datetime, datetime]) -> dict:
    """Both battery sensors have no rows in [gap[0], gap[1]) and change 0 in the first
    period after it: a counter that stood still across the gap (rule (b))."""
    for sid in ("sensor.batt_in", "sensor.batt_out"):
        data[sid] = [
            {**r, "change": 0.0} if r["start"] == gap[1].timestamp() else r
            for r in data[sid]
            if not gap[0].timestamp() <= r["start"] < gap[1].timestamp()]
    return data


@pytest.mark.asyncio
async def test_both_battery_sensors_standing_still_are_sent(hass: HomeAssistant, mock_entry):
    """Both battery counters miss 10:00-10:25 and show change 0 at 10:30 while the grid
    is measured: the quarters 10:00 and 10:15 have no battery value anywhere, yet they
    are complete, so they are sent with battery 0.0 and the real grid values."""
    q = NOW - timedelta(hours=2)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    async def fetch(h, ids, start, end, period):
        return _batteries_stand_still(_energy_stats(start, end),
                                      (q, q + timedelta(minutes=30)))

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - timedelta(hours=6)).isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint()})
        await coordinator._async_update_data()

    sent = {r["ts"]: r for c in client.put_data.call_args_list for r in c.args[1]}
    for ts in (q, q + timedelta(minutes=15)):
        row = sent[ts.isoformat()]
        assert row["batt_charged_kwh"] == 0.0
        assert row["batt_discharged_kwh"] == 0.0
        assert row["grid_import_kwh"] == pytest.approx(0.3)
        assert row["grid_export_kwh"] == pytest.approx(0.3)
    assert (q + timedelta(minutes=30)).isoformat() in sent


@pytest.mark.asyncio
async def test_both_battery_sensors_standing_still_hourly_are_sent(
    hass: HomeAssistant, mock_entry
):
    """Hourly (heal): both battery counters miss two hours and show change 0 in the
    hour after; the grid is measured. The quarters of the two hours are sent with
    battery 0.0 and a quarter of the grid's hour."""
    h = (NOW - timedelta(days=12)).replace(minute=0, second=0, microsecond=0)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    async def fetch(hs, ids, start, end, period):
        assert period == "hour"
        return _batteries_stand_still(_full_stats(start, end, period),
                                      (h, h + timedelta(hours=2)))

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
        rows = await coordinator._heal_rows(NOW - timedelta(days=15), NOW)

    sent = {r["ts"]: r for r in rows}
    for k in range(8):
        row = sent[(h + timedelta(minutes=15 * k)).isoformat()]
        assert row["batt_charged_kwh"] == 0.0
        assert row["batt_discharged_kwh"] == 0.0
        assert row["grid_import_kwh"] == pytest.approx(0.025)
    assert (h + timedelta(hours=2)).isoformat() in sent


@pytest.mark.asyncio
@pytest.mark.parametrize(("minutes", "logged"), [(120, True), (30, False)])
async def test_a_long_still_run_is_logged_at_debug(
    hass: HomeAssistant, mock_entry, caplog, minutes, logged
):
    """A gap of an hour or more accepted as known zeros because the counter stood still
    is logged at debug with the sensor and the run, so an integration that reports a
    stale counter on reconnect is visible. A shorter run is not logged."""
    q = NOW - timedelta(hours=4)
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    async def fetch(h, ids, start, end, period):
        return _batteries_stand_still(_energy_stats(start, end),
                                      (q, q + timedelta(minutes=minutes)))

    caplog.set_level(logging.DEBUG, logger="custom_components.wolta.coordinator")
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - timedelta(hours=6)).isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint()})
        await coordinator._async_update_data()

    lines = [r.getMessage() for r in caplog.records
             if r.levelno == logging.DEBUG and "stood still" in r.getMessage()]
    if not logged:
        assert lines == []
        return
    for sid in ("sensor.batt_in", "sensor.batt_out"):
        line = next(m for m in lines if sid in m)
        assert q.isoformat() in line
        assert (q + timedelta(minutes=minutes)).isoformat() in line
        assert "2:00:00" in line


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["backfill", "heal", "incremental", "gap"])
async def test_the_build_runs_off_the_event_loop(hass: HomeAssistant, mock_entry, mode):
    """The completeness judgement (clean_quarters) and the merge run in the executor,
    never on the event-loop thread, and the rows are the same as before."""
    import threading

    from custom_components.wolta import coverage
    from custom_components.wolta import coordinator as coord_mod

    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    loop_thread = threading.get_ident()
    threads: set[int] = set()
    real_clean, real_merge = coord_mod.clean_quarters, coord_mod.merge_streams

    def clean(*args, **kwargs):
        threads.add(threading.get_ident())
        return real_clean(*args, **kwargs)

    def merge(*args, **kwargs):
        threads.add(threading.get_ident())
        return real_merge(*args, **kwargs)

    async def fetch(h, ids, start, end, period):
        return _full_stats(start, end, period)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
        patch("custom_components.wolta.coordinator.clean_quarters", side_effect=clean),
        patch("custom_components.wolta.coordinator.merge_streams", side_effect=merge),
    ):
        coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
        if mode == "gap":
            rows = await coordinator._gap_rows(
                coverage.Gap(NOW - timedelta(days=12), NOW - timedelta(days=1)), NOW)
        else:
            rows = await _run_fetch_mode(coordinator, mode)

    assert rows
    assert threads and loop_thread not in threads


async def _run_solar_cycles(
    hass, mock_entry, client, *, now: datetime, cycles: int, hours: tuple[float, float],
    dead: tuple[str, ...] = (), advance: timedelta = timedelta(hours=6),
):
    """Slow-poll cycles on ONE coordinator with two solar sensors whose rows exist only
    inside ``hours`` (UTC); the sensors in ``dead`` have no rows at all. Returns the
    coordinator and, per cycle, (statistics_missing, energy_stream_no_statistics)."""
    _two_solar_entry(hass, mock_entry)
    clock = {"now": now}
    reg = ir.async_get(hass)
    seen: list[tuple[bool, bool]] = []

    async def fetch(h, ids, start, end, period):
        data = _solar_by_hours(
            _full_stats(start, end, period, values={"sensor.solar2": 0.1}), *hours,
            sensors=("sensor.solar", "sensor.solar2"))
        for sid in dead:
            data[sid] = []
        return data

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow",
              side_effect=lambda: clock["now"]),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (now - advance).isoformat(),
                         "applied_invert": False, "applied_entities": _TWO_SOLAR_FP})
        for _ in range(cycles):
            await coordinator._async_update_data()
            seen.append((
                reg.async_get_issue(DOMAIN, coordinator._stats_issue_id) is not None,
                reg.async_get_issue(DOMAIN, coordinator._energy_issue_id) is not None,
            ))
            clock["now"] = clock["now"] + advance
    return coordinator, seen


@pytest.mark.asyncio
async def test_sleeping_solar_at_night_raises_no_repair(hass: HomeAssistant, mock_entry):
    """Both solar sensors measure until sunset and sleep through a December night: the
    windows 12-18, 18-24 and 00-06 UTC raise neither repair - the solar stream is empty
    in two of them, and both sensors trail the others only by known zeros."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator, seen = await _run_solar_cycles(
        hass, mock_entry, client, now=datetime(2024, 12, 21, 18, 0, tzinfo=timezone.utc),
        cycles=3, hours=(8, 13 + 55 / 60))
    assert seen == [(False, False)] * 3
    assert coordinator._state.get("silent_sensor_cycles") is None
    assert coordinator._state.get("energy_empty_cycles") is None


@pytest.mark.asyncio
async def test_sleeping_solar_neither_counts_nor_clears_energy_empty(
    hass: HomeAssistant, mock_entry
):
    """A solar stream without values only because the sun is down says nothing about
    the sensor: a raised energy_empty repair and its streak stay until daylight."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator = await _make_coordinator(hass, mock_entry, client)
    coordinator._state["energy_empty_cycles"] = 3
    ir.async_create_issue(
        hass, DOMAIN, coordinator._energy_issue_id, is_fixable=False,
        severity=ir.IssueSeverity.WARNING, translation_key="energy_stream_no_statistics",
        translation_placeholders={"streams": "solar: sensor.solar"})
    now = datetime(2024, 12, 21, 23, 0, tzinfo=timezone.utc)
    coordinator._state.update({"last_uploaded_ts": (now - timedelta(hours=6)).isoformat(),
                               "applied_invert": False, "applied_entities": _fingerprint()})

    async def fetch(h, ids, start, end, period):
        return _solar_by_hours(_full_stats(start, end, period), 8, 13.5)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=now),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        await coordinator._async_update_data()

    assert coordinator._state.get("energy_empty_cycles") == 3
    assert ir.async_get(hass).async_get_issue(DOMAIN, coordinator._energy_issue_id)


@pytest.mark.asyncio
async def test_polar_night_raises_no_repair(hass: HomeAssistant, mock_entry):
    """Near Kiruna the sun does not rise in late December (astral raises ValueError for
    sunrise and sunset): solar without any statistics for four days is no fault."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    hass.config.latitude, hass.config.longitude = 67.85, 20.2
    coordinator, seen = await _run_solar_cycles(
        hass, mock_entry, client, now=datetime(2024, 12, 20, 12, 0, tzinfo=timezone.utc),
        cycles=16, hours=(0, 0))
    assert seen == [(False, False)] * 16
    sent = {r["ts"] for c in client.put_data.call_args_list for r in c.args[1]}
    assert datetime(2024, 12, 21, 10, 0, tzinfo=timezone.utc).isoformat() in sent


@pytest.mark.parametrize(
    ("position", "when", "down"),
    [
        ((59.3, 16.5), datetime(2025, 10, 12, 3, 0), True),
        ((59.3, 16.5), datetime(2025, 10, 12, 12, 0), False),
        ((55.4, 13.0), datetime(2025, 6, 21, 23, 0), True),
        # Near Kiruna: polar night (astral raises), a day on which the sun only grazes
        # the horizon around noon (astral raises too) and midnight sun.
        ((67.85, 20.2), datetime(2024, 12, 21, 10, 40), True),
        ((67.85, 20.2), datetime(2025, 1, 9, 10, 40), False),
        ((67.85, 20.2), datetime(2025, 1, 9, 20, 0), True),
        ((67.85, 20.2), datetime(2025, 6, 21, 23, 0), False),
        ((67.85, 20.2), datetime(2025, 5, 25, 12, 0), False),
    ],
)
def test_sun_down_at_swedish_latitudes(position, when, down):
    """sun_down(period_start) judges the middle of the 5-minute period."""
    from astral import Observer

    from custom_components.wolta.coordinator import _make_sun_down

    sun_down = _make_sun_down(Observer(*position, 0), 300, {})
    assert sun_down(int(when.replace(tzinfo=timezone.utc).timestamp())) is down


def test_a_sunset_before_the_sunrise_takes_the_elevation_path():
    """astral 3.x re-anchors sunrise and sunset to the requested date, which can put the
    sunset of a short night before the sunrise. Such a pair is no interval: the
    elevation decides, and the late-May noon near Kiruna is daylight."""
    import astral.sun
    from astral import Observer

    from custom_components.wolta.coordinator import _make_sun_down

    real_sunset = astral.sun.sunset

    def early_sunset(observer, date=None, tzinfo=timezone.utc):
        return real_sunset(observer, date, tzinfo) - timedelta(days=1)

    with patch("custom_components.wolta.coordinator.astral_sun.sunset", early_sunset):
        sun_down = _make_sun_down(Observer(67.85, 20.2, 0), 300, {})
        noon = datetime(2025, 5, 25, 12, 0, tzinfo=timezone.utc)
        assert sun_down(int(noon.timestamp())) is False


@pytest.mark.asyncio
async def test_solar_silent_in_daylight_raises_the_repair(hass: HomeAssistant, mock_entry):
    """The same sensor without statistics in daylight is silent: windows 06-12, 12-18
    and 18-24 UTC on 1 June (sun up 01:50-19:54 UTC) raise statistics_missing."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator, seen = await _run_solar_cycles(
        hass, mock_entry, client, now=NOW, cycles=3, hours=(0, 24), dead=("sensor.solar2",))
    assert seen == [(False, False), (False, False), (True, False)]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._stats_issue_id)
    assert issue.translation_placeholders["entities"] == "sensor.solar2"


@pytest.mark.asyncio
async def test_a_dead_solar_sensor_is_held_through_the_night_not_cleared(
    hass: HomeAssistant, mock_entry
):
    """A December day has fewer than three daylight windows in a row. The night says
    nothing about a dead solar sensor - its streak is held, not cleared, so the repair
    is raised on the next day's first daylight window and stays through the night."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator, seen = await _run_solar_cycles(
        hass, mock_entry, client, now=datetime(2024, 12, 21, 12, 0, tzinfo=timezone.utc),
        cycles=7, hours=(8, 13 + 55 / 60), dead=("sensor.solar2",))
    # windows 06-12 and 12-18 count, 18-24 and 00-06 hold, 06-12 trips, 12-18 counts,
    # 18-24 holds
    assert seen == [(False, False)] * 4 + [(True, False)] * 3
    assert coordinator._state["silent_sensor_cycles"] == {"sensor.solar2": 4}


@pytest.mark.asyncio
async def test_solar_that_stops_shortly_before_sunset_is_not_silent_at_night(
    hass: HomeAssistant, mock_entry
):
    """An inverter that goes to standby 20 minutes before sunset trails the others by
    20 minutes of daylight and then only by known zeros: below the 30-minute lag."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    coordinator, seen = await _run_solar_cycles(
        hass, mock_entry, client, now=datetime(2024, 12, 21, 18, 0, tzinfo=timezone.utc),
        cycles=3, hours=(8, 13 + 35 / 60))
    assert seen == [(False, False)] * 3


@pytest.mark.asyncio
async def test_backfill_over_a_year_with_solar_sleeping_every_night(
    hass: HomeAssistant, mock_entry
):
    """365 days of hourly statistics and 9 days of 5-minute statistics with a solar
    sensor that only has rows 06:00-18:00 UTC. Night quarters are sent all year round,
    daylight gaps in summer are not, and the sun computation stays cheap."""
    import time

    client = _mock_client()

    async def fetch(h, ids, start, end, period):
        return _solar_by_hours(_full_stats(start, end, period), 6, 18)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
        t0 = time.perf_counter()
        rows = await coordinator._backfill_rows(NOW)
        elapsed = time.perf_counter() - t0

    sent = {r["ts"]: r for r in rows}
    winter_night = datetime(2025, 1, 15, 22, 0, tzinfo=timezone.utc)
    summer_night = datetime(2024, 6, 21, 23, 0, tzinfo=timezone.utc)
    summer_dawn = datetime(2024, 6, 21, 4, 0, tzinfo=timezone.utc)   # sun up, no rows
    recent_night = NOW.replace(hour=0) - timedelta(days=1)            # 5-minute part
    for q in (winter_night, summer_night, recent_night):
        assert sent[q.isoformat()]["solar_kwh"] == 0.0
    assert summer_dawn.isoformat() not in sent
    assert elapsed < 10.0, f"backfill took {elapsed:.2f} s"  # gross-regression guard


# A gap that began in daylight is not cleared by the night that follows: the first
# measured period after it carries the afternoon's energy. A night run proves nothing
# unless the sensor's last measured period before it is known (read further back when
# the window starts at night, at most _ANCHOR_LOOKBACK).

_OCT11 = datetime(2025, 10, 11, tzinfo=timezone.utc)
_OCT12 = datetime(2025, 10, 12, tzinfo=timezone.utc)


async def _run_solar_read(hass, mock_entry, *, now, bookmark, fetch_solar, reads=None):
    """One cycle; the solar sensor's rows are filtered by ``fetch_solar(data, period)``.
    Returns the sent rows by ts."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))

    async def fetch(h, ids, start, end, period):
        if reads is not None:
            reads.append((set(ids), start, end, period))
        return fetch_solar(_full_stats(start, end, period), period)

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=now),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark.isoformat(),
                         "applied_invert": False, "applied_entities": _fingerprint()})
        await coordinator._async_update_data()
    return {r["ts"]: r for c in client.put_data.call_args_list for r in c.args[1]}


@pytest.mark.asyncio
@pytest.mark.parametrize(("gap_start", "dawn_sent"), [(13, False), (17, True)])
async def test_dawn_after_a_gap_that_began_in_daylight_is_not_sent(
    hass: HomeAssistant, mock_entry, gap_start, dawn_sent
):
    """The solar sensor stops at 13:00 (daylight, sunset 15:57) or 17:00 (night) and
    returns at 05:00. Night quarters are zeros either way; the dawn quarter is sent only
    when the whole gap was night."""
    hole = (_OCT11 + timedelta(hours=gap_start), _OCT12 + timedelta(hours=5))
    sent = await _run_solar_read(
        hass, mock_entry, now=_OCT12 + timedelta(hours=8),
        bookmark=_OCT11 + timedelta(hours=12),
        fetch_solar=lambda data, period: _solar_by_hours(data, 5, 17, hole=hole))
    dawn = _OCT12 + timedelta(hours=5)
    assert (dawn.isoformat() in sent) is dawn_sent
    assert (dawn + timedelta(minutes=15)).isoformat() in sent
    assert sent[(_OCT11 + timedelta(hours=22)).isoformat()]["solar_kwh"] == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(("gap_start", "dawn_sent"), [(13, False), (17, True)])
async def test_hourly_dawn_after_a_gap_that_began_in_daylight_is_not_sent(
    hass: HomeAssistant, mock_entry, gap_start, dawn_sent
):
    """The same gap on the heal path (hourly statistics, bookmark two weeks old)."""
    hole = (_OCT11 + timedelta(hours=gap_start), _OCT12 + timedelta(hours=5))
    reads: list = []
    sent = await _run_solar_read(
        hass, mock_entry, now=_OCT12 + timedelta(days=13, hours=8),
        bookmark=_OCT11 + timedelta(hours=12), reads=reads,
        fetch_solar=lambda data, period: _solar_by_hours(data, 5, 17, hole=hole))
    assert {r[3] for r in reads} == {"hour"}, "the scenario must stay on the heal path"
    dawn = _OCT12 + timedelta(hours=5)
    for m in (0, 15, 30, 45):
        assert ((dawn + timedelta(minutes=m)).isoformat() in sent) is dawn_sent
    assert (dawn + timedelta(hours=1)).isoformat() in sent
    assert sent[(_OCT11 + timedelta(hours=22)).isoformat()]["solar_kwh"] == 0.0


@pytest.mark.asyncio
async def test_a_window_that_starts_at_night_reads_back_to_the_evening(
    hass: HomeAssistant, mock_entry
):
    """The window starts at 23:00, the solar sensor's last row is 16:55 - six hours
    before it. One extra read for the solar sensor finds it, the night since is proven,
    and the dawn quarter is sent."""
    reads: list = []
    window_start = _OCT11 + timedelta(hours=23)
    sent = await _run_solar_read(
        hass, mock_entry, now=_OCT12 + timedelta(hours=8), bookmark=window_start,
        reads=reads, fetch_solar=lambda data, period: _solar_by_hours(data, 5, 17))
    assert (_OCT12 + timedelta(hours=5)).isoformat() in sent
    extra = [r for r in reads if r[0] == {"sensor.solar"}]
    assert len(extra) == 1
    ids, start, end, period = extra[0]
    assert period == "5minute"
    assert start <= window_start - timedelta(hours=36)
    assert end <= window_start


@pytest.mark.asyncio
async def test_no_measured_row_within_the_lookback_leaves_the_dawn_unproven(
    hass: HomeAssistant, mock_entry
):
    """The solar sensor has no row before 05:00 on the 12th at all: nothing proves the
    night run, so the dawn quarter is not sent - the next one is."""
    first_row = _OCT12 + timedelta(hours=5)

    def solar(data, period):
        data = _solar_by_hours(data, 5, 17)
        data["sensor.solar"] = [r for r in data["sensor.solar"]
                                if r["start"] >= first_row.timestamp()]
        return data

    sent = await _run_solar_read(
        hass, mock_entry, now=_OCT12 + timedelta(hours=8),
        bookmark=_OCT11 + timedelta(hours=23), fetch_solar=solar)
    assert first_row.isoformat() not in sent
    assert (first_row + timedelta(minutes=15)).isoformat() in sent
    assert sent[(_OCT12 + timedelta(hours=2)).isoformat()]["solar_kwh"] == 0.0


# ---------------------------------------------------------------------------
# Copies of this plant: the house_copies repair
# ---------------------------------------------------------------------------

_COPIES_ISSUE_ID = "house_copies_test_entry_id"


def _copy(cid, *, status="active", removable=True, frm="2025-03-01", to="2026-03-01"):
    return {"id": cid, "status": status, "uploaded": "2026-03-02",
            "period": {"from": frm, "to": to}, "n_days": 365, "source": "ha_upload",
            "transport": None, "removable": removable}


async def _copies_coordinator(hass, mock_entry, listing):
    client = _mock_client()
    client.get_copies = AsyncMock(return_value=listing)
    return await _make_coordinator(hass, mock_entry, client)


@pytest.mark.asyncio
async def test_copies_issue_lists_active_removable_copies(hass, mock_entry):
    c = await _copies_coordinator(hass, mock_entry, {"copies": [
        _copy(13), _copy(12), _copy(14, status="removed"), _copy(15, removable=False),
        _copy(16, frm=None, to=None)], "self": None, "purge_days": 30})
    await c._evaluate_copies()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _COPIES_ISSUE_ID)
    assert issue is not None and issue.is_fixable and not issue.is_persistent
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.data["ids"] == [12, 13, 16]
    assert issue.data["lines"] == ("- 2026-03-02 (2025-03-01 – 2026-03-01)\n"
                                   "- 2026-03-02 (2025-03-01 – 2026-03-01)\n- 2026-03-02")
    assert issue.data["purge_days"] == 30
    assert issue.translation_placeholders == {"count": "3", "days": "30"}


@pytest.mark.asyncio
async def test_copies_issue_cleared_when_none(hass, mock_entry):
    c = await _copies_coordinator(hass, mock_entry, {"copies": [_copy(12)], "self": None,
                                                     "purge_days": 30})
    await c._evaluate_copies()
    c.client.get_copies = AsyncMock(return_value={"copies": [], "self": None, "purge_days": 30})
    await c._evaluate_copies()
    assert ir.async_get(hass).async_get_issue(DOMAIN, _COPIES_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_copies_issue_never_in_view_only(hass, mock_entry):
    """The repair is never raised in view-only mode, and the listing is not even fetched."""
    c = await _copies_coordinator(hass, mock_entry, {"copies": [_copy(12)], "self": None,
                                                     "purge_days": 30})
    c._view_only = True
    await c._evaluate_copies()
    assert ir.async_get(hass).async_get_issue(DOMAIN, _COPIES_ISSUE_ID) is None
    c.client.get_copies.assert_not_awaited()


@pytest.mark.asyncio
async def test_copies_issue_untouched_on_fetch_error_or_old_server(hass, mock_entry):
    c = await _copies_coordinator(hass, mock_entry, {"copies": [_copy(12)], "self": None,
                                                     "purge_days": 30})
    await c._evaluate_copies()
    c.client.get_copies = AsyncMock(side_effect=RuntimeError("boom"))
    await c._evaluate_copies()
    assert ir.async_get(hass).async_get_issue(DOMAIN, _COPIES_ISSUE_ID) is not None
    c.client.get_copies = AsyncMock(return_value=None)
    await c._evaluate_copies()
    assert ir.async_get(hass).async_get_issue(DOMAIN, _COPIES_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_ignored_copies_issue_resurfaces_only_on_new_id(hass, mock_entry):
    """HA keeps an ignored issue ignored on re-create; only delete + create releases it, so
    the repair is deleted first exactly when the server lists an id not seen before."""
    reg = ir.async_get(hass)
    c = await _copies_coordinator(hass, mock_entry, {"copies": [_copy(12), _copy(13)],
                                                     "self": None, "purge_days": 30})
    await c._evaluate_copies()
    reg.async_ignore(DOMAIN, _COPIES_ISSUE_ID, True)
    assert reg.async_get_issue(DOMAIN, _COPIES_ISSUE_ID).dismissed_version is not None

    c.client.get_copies = AsyncMock(return_value={"copies": [_copy(12)], "self": None,
                                                  "purge_days": 30})
    await c._evaluate_copies()                                   # fewer: stays ignored
    assert reg.async_get_issue(DOMAIN, _COPIES_ISSUE_ID).dismissed_version is not None

    c.client.get_copies = AsyncMock(return_value={"copies": [_copy(12), _copy(20)],
                                                  "self": None, "purge_days": 30})
    await c._evaluate_copies()                                   # new id: surfaces again
    issue = reg.async_get_issue(DOMAIN, _COPIES_ISSUE_ID)
    assert issue.dismissed_version is None and issue.data["ids"] == [12, 20]


@pytest.mark.asyncio
async def test_ignored_copies_issue_stays_ignored_after_restart(hass, mock_entry):
    """A non-persistent issue comes back from the registry without its data after a restart;
    the ids already seen live in the coordinator's store, so the ignore survives."""
    reg = ir.async_get(hass)
    c = await _copies_coordinator(hass, mock_entry, {"copies": [_copy(12), _copy(13)],
                                                     "self": None, "purge_days": 30})
    await c._evaluate_copies()
    assert c._state["copies_seen_ids"] == [12, 13]
    reg.async_ignore(DOMAIN, _COPIES_ISSUE_ID, True)
    # What IssueRegistry._async_load does with a non-persistent issue: data is dropped.
    key = (DOMAIN, _COPIES_ISSUE_ID)
    reg.issues[key] = dataclasses.replace(reg.issues[key], data=None)
    assert reg.async_get_issue(DOMAIN, _COPIES_ISSUE_ID).data is None
    await c._evaluate_copies()
    assert reg.async_get_issue(DOMAIN, _COPIES_ISSUE_ID).dismissed_version is not None


@pytest.mark.asyncio
async def test_copies_seen_ids_in_stored_state_decide_the_release(hass, mock_entry):
    reg = ir.async_get(hass)
    for listed, released in (([12, 13], False), ([12, 20], True)):
        ir.async_delete_issue(hass, DOMAIN, _COPIES_ISSUE_ID)
        client = _mock_client()
        client.get_copies = AsyncMock(return_value={
            "copies": [_copy(i) for i in (12, 13)], "self": None, "purge_days": 30})
        c = await _make_coordinator(hass, mock_entry, client,
                                    store_state={"copies_seen_ids": [12, 13]})
        await c._evaluate_copies()
        reg.async_ignore(DOMAIN, _COPIES_ISSUE_ID, True)
        c.client.get_copies = AsyncMock(return_value={
            "copies": [_copy(i) for i in listed], "self": None, "purge_days": 30})
        await c._evaluate_copies()
        issue = reg.async_get_issue(DOMAIN, _COPIES_ISSUE_ID)
        assert (issue.dismissed_version is None) is released, listed
        assert c._state["copies_seen_ids"] == sorted({12, 13} | set(listed))


@pytest.mark.asyncio
async def test_ignored_copies_issue_survives_an_empty_listing(hass, mock_entry):
    """An ignored issue is invisible; deleting it on an empty listing would drop the ignore,
    so the same ids coming back would show the repair again."""
    reg = ir.async_get(hass)
    both = {"copies": [_copy(12), _copy(13)], "self": None, "purge_days": 30}
    c = await _copies_coordinator(hass, mock_entry, both)
    await c._evaluate_copies()
    reg.async_ignore(DOMAIN, _COPIES_ISSUE_ID, True)
    c.client.get_copies = AsyncMock(return_value={"copies": [], "self": None, "purge_days": 30})
    await c._evaluate_copies()
    assert reg.async_get_issue(DOMAIN, _COPIES_ISSUE_ID).dismissed_version is not None
    c.client.get_copies = AsyncMock(return_value=both)
    await c._evaluate_copies()
    assert reg.async_get_issue(DOMAIN, _COPIES_ISSUE_ID).dismissed_version is not None
    assert c._state["copies_seen_ids"] == [12, 13]


@pytest.mark.asyncio
async def test_ignored_copies_issue_survives_remove_then_undo(hass, mock_entry):
    """The seen set never shrinks: [12, 13] -> [12] (13 removed) -> [12, 13] (undone) is not a
    new copy, so the ignore stays."""
    reg = ir.async_get(hass)
    c = await _copies_coordinator(hass, mock_entry, {"copies": [_copy(12), _copy(13)],
                                                     "self": None, "purge_days": 30})
    await c._evaluate_copies()
    reg.async_ignore(DOMAIN, _COPIES_ISSUE_ID, True)
    c.client.get_copies = AsyncMock(return_value={"copies": [_copy(12)], "self": None,
                                                  "purge_days": 30})
    await c._evaluate_copies()
    assert c._state["copies_seen_ids"] == [12, 13]
    c.client.get_copies = AsyncMock(return_value={"copies": [_copy(12), _copy(13)],
                                                  "self": None, "purge_days": 30})
    await c._evaluate_copies()
    issue = reg.async_get_issue(DOMAIN, _COPIES_ISSUE_ID)
    assert issue.dismissed_version is not None and issue.data["ids"] == [12, 13]


@pytest.mark.asyncio
async def test_copies_endpoint_unavailable_is_logged_and_keeps_issue(hass, mock_entry, caplog):
    c = await _copies_coordinator(hass, mock_entry, {"copies": [_copy(12)], "self": None,
                                                     "purge_days": 30})
    await c._evaluate_copies()
    c.client.get_copies = AsyncMock(return_value=None)
    with caplog.at_level("DEBUG", logger="custom_components.wolta.coordinator"):
        await c._evaluate_copies()
    assert "Copies endpoint unavailable" in caplog.text
    assert ir.async_get(hass).async_get_issue(DOMAIN, _COPIES_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_update_cycle_fetches_copies_only_on_the_slow_poll(hass, mock_entry):
    """The main cycle evaluates copies; the 60 s fast poll does not. A None listing (older
    server or lost token) raises nothing, in particular no re-authentication."""
    from custom_components.wolta.coordinator import _FAST_POLL, _SLOW_POLL

    client = _mock_client()
    client.get_copies = AsyncMock(return_value=None)
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
        _patch_aggregator("aggregate_5min_to_15min", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": (NOW - timedelta(hours=2)).isoformat(),
                         "last_recompute": NOW.date().isoformat()})
        coordinator.update_interval = _FAST_POLL
        await coordinator._async_update_data()
        client.get_copies.assert_not_awaited()
        coordinator.update_interval = _SLOW_POLL
        await coordinator._async_update_data()   # must not raise ConfigEntryAuthFailed
        client.get_copies.assert_awaited_once_with(coordinator.token)


@pytest.mark.asyncio
async def test_copies_issue_untouched_on_malformed_listing(hass, mock_entry):
    c = await _copies_coordinator(hass, mock_entry, {"copies": [_copy(12)], "self": None,
                                                     "purge_days": 30})
    await c._evaluate_copies()
    before = ir.async_get(hass).async_get_issue(DOMAIN, _COPIES_ISSUE_ID)
    for bad in ({"copies": [{"status": "active", "removable": True}], "purge_days": 30},
                {"copies": [_copy(12)], "purge_days": "x"}):
        c.client.get_copies = AsyncMock(return_value=bad)
        await c._evaluate_copies()
        after = ir.async_get(hass).async_get_issue(DOMAIN, _COPIES_ISSUE_ID)
        assert after is not None and after.data == before.data
        assert c._state["copies_seen_ids"] == [12]


# ---------------------------------------------------------------------------
# grid_fee_implausible: the owner's grid fee is above the server's plausibility limit
# (server results.tariff_check, api 0.110.0). Likely VAT and energy tax included, which
# Wolta adds itself - the energy tax is then counted twice.
# ---------------------------------------------------------------------------

_GRID_ISSUE_ID = "grid_fee_implausible_test_entry_id"


def _tariff_check(flag: bool) -> dict:
    return {"tariff_check": {"grid_var_implausible": flag, "grid_var_limit_ore": 60.0}}


@pytest.mark.asyncio
async def test_grid_fee_implausible_fires_from_the_server_flag(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_grid_fee_issue(_tariff_check(True))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _GRID_ISSUE_ID)
    assert issue is not None
    assert issue.translation_key == "grid_fee_implausible"
    assert issue.is_fixable is False
    # The limit travels from the server; the client never holds its own.
    assert issue.translation_placeholders == {"limit": "60"}


@pytest.mark.asyncio
async def test_grid_fee_implausible_clears_when_the_server_says_plausible(hass, mock_entry):
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_grid_fee_issue(_tariff_check(True))
    c._evaluate_grid_fee_issue(_tariff_check(False))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _GRID_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_grid_fee_implausible_left_alone_without_the_block(hass, mock_entry):
    """An older server sends no tariff_check: say nothing new, change nothing open."""
    c = await _cap_coordinator(hass, mock_entry)
    c._evaluate_grid_fee_issue(_tariff_check(True))
    c._evaluate_grid_fee_issue({})
    assert ir.async_get(hass).async_get_issue(DOMAIN, _GRID_ISSUE_ID) is not None


@pytest.mark.asyncio
async def test_grid_fee_implausible_not_in_view_only(hass, mock_entry):
    """View-only: the plant's tariff is set on wolta.se, not under Configure here."""
    c = await _cap_coordinator(hass, mock_entry)
    c._view_only = True
    c._evaluate_grid_fee_issue(_tariff_check(True))
    assert ir.async_get(hass).async_get_issue(DOMAIN, _GRID_ISSUE_ID) is None
