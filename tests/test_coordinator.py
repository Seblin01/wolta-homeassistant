"""Tests for custom_components/wolta/coordinator.py (TDD)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.wolta.api import WoltaAuthError, WoltaRateLimitError
from custom_components.wolta.const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_GRID_IN,
    CONF_GRID_OUT,
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_entry(hass: HomeAssistant):
    """Create a mock ConfigEntry for testing."""
    from homeassistant.config_entries import ConfigEntry
    from unittest.mock import PropertyMock

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
        patch("custom_components.wolta.coordinator.split_hour_to_quarters") as mock_lts,
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min") as mock_5min,
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
        patch("custom_components.wolta.coordinator.split_hour_to_quarters") as mock_lts,
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min") as mock_5min,
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
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
    assert result.results == RESULTS_PAYLOAD
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
        patch("custom_components.wolta.coordinator.split_hour_to_quarters") as mock_lts,
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

    # async_fetch_change returns two solar entity IDs with different values
    short_stats = {
        "sensor.batt_in": [{"start": 1700000000.0, "change": 0.1}],
        "sensor.batt_out": [{"start": 1700000000.0, "change": 0.0}],
        "sensor.grid_in": [{"start": 1700000000.0, "change": 0.0}],
        "sensor.grid_out": [{"start": 1700000000.0, "change": 0.0}],
        "sensor.solar_a": [{"start": 1700000000.0, "change": 1.0}],
        "sensor.solar_b": [{"start": 1700000000.0, "change": 2.0}],
    }

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
            return_value=short_stats,
        ),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={"last_uploaded_ts": bookmark_str}
        )
        await coordinator._async_update_data()

    # Find the row for bucket at 1700000000 floored to 900s
    from datetime import timezone as tz
    bucket = datetime.fromtimestamp(1700000000 - (1700000000 % 900), tz=tz.utc)
    matching = [r for r in captured_rows if r["ts"] == bucket.isoformat()]
    assert len(matching) == 1, "Expected one row for the bucket"
    # solar_kwh must be the sum: 1.0 + 2.0 = 3.0
    assert matching[0]["solar_kwh"] == pytest.approx(3.0), (
        f"solar_kwh should be 3.0 (sum of both inverters), got {matching[0]['solar_kwh']}"
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
            patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
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
    """Invert flag set: batt_in reads the discharge sensor, batt_out the charge sensor.
    Grid/solar are untouched (only the battery direction is flipped)."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY

    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: True}
    client = _mock_client()
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
    assert coordinator._effective_stream("batt_in") == "batt_out"
    assert coordinator._effective_stream("batt_out") == "batt_in"
    assert coordinator._effective_stream("grid_in") == "grid_in"
    assert coordinator._effective_stream("solar") == "solar"


@pytest.mark.asyncio
async def test_invert_change_forces_full_backfill(hass: HomeAssistant, mock_entry):
    """Flag changed since the last upload (applied_invert in state) → the bookmark is reset →
    full re-backfill (the LTS 'hour' window is fetched) so history is overwritten with the correct direction."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY

    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: True}
    client = _mock_client()
    fetch_calls: list[str] = []

    async def mock_fetch(h, ids, start, end, period):
        fetch_calls.append(period)
        return {}

    bookmark_str = (NOW - timedelta(days=1)).isoformat()  # fresh → normally incremental
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

    assert "hour" in fetch_calls, "invert change should force a full backfill (the LTS window)"
    assert coordinator._state.get("applied_invert") is True


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
async def test_invert_change_forces_recompute_despite_recent_last_recompute(
    hass: HomeAssistant, mock_entry
):
    """After an invert change (issue #1), the grade should be recomputed IMMEDIATELY, bypassing the 7-day gate –
    even if last_recompute is fresh. The force flag is set by the self-heal and cleared once the recompute
    succeeds."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY

    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: True}
    client = _mock_client()
    recent_last_recompute = "2025-05-29"  # 2 days before period-end 2025-05-31 → normally SKIP

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
        patch("custom_components.wolta.coordinator.split_hour_to_quarters", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={
                "last_uploaded_ts": (NOW - timedelta(hours=2)).isoformat(),
                "last_recompute": recent_last_recompute,
                "applied_invert": False,  # differs from the entry flag → self-heal fires
            },
        )
        await coordinator._async_update_data()

    assert client.recompute.called, "invert change should force recompute past the 7-day gate"
    assert coordinator._state.get("pending_invert_recompute") is None, \
        "the force flag should be cleared once the recompute succeeds"


@pytest.mark.asyncio
async def test_force_recompute_flag_survives_rate_limit(hass: HomeAssistant, mock_entry):
    """If the forced recompute gets a 429, the force flag should be retained (retry on the next tick)."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY

    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: True}
    client = _mock_client(raise_on_recompute=WoltaRateLimitError(3600))

    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW),
        patch("custom_components.wolta.coordinator.WoltaApiClient", return_value=client),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=[]),
        patch("custom_components.wolta.coordinator.aggregate_5min_to_15min", return_value={}),
        patch("custom_components.wolta.coordinator.split_hour_to_quarters", return_value={}),
    ):
        coordinator = await _make_coordinator(
            hass, mock_entry, client,
            store_state={
                "last_uploaded_ts": (NOW - timedelta(hours=2)).isoformat(),
                "last_recompute": "2025-05-29",
                "applied_invert": False,
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
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "profile_full")
    assert issue is not None


@pytest.mark.asyncio
async def test_entity_change_resets_bookmark(hass: HomeAssistant, mock_entry):
    """Ändrade sensorval (reconfigure) → bookmark nollas → full re-backfill,
    samma självläkningsmönster som invert-togglen."""
    import json as _json

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

    assert "last_uploaded_ts" not in coordinator._state
    assert coordinator._state["applied_entities"] == _json.dumps(
        coordinator._entity_map, sort_keys=True)
    # pending-flaggan sätts och konsumeras i samma tick → recompute triggad direkt
    client.recompute.assert_awaited()


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
    """Review #3: en uppmätt eff ≥ 0.98 är en clamp-/randartefakt, inte ett riktigt
    round-trip – ska inte erbjudas för adoption."""
    c = await _cap_coordinator(hass, mock_entry, **{_CEFF: 0.85})
    c._evaluate_measured_params(_oe_results(1.0))          # gap 0.15 men 1.0 ≥ 0.98
    assert ir.async_get(hass).async_get_issue(DOMAIN, _EFF_ISSUE_ID) is None


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
