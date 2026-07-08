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
    """Utan invert-flagga: strömnamnen är oförändrade."""
    client = _mock_client()
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state={})
    assert coordinator._effective_stream("batt_in") == "batt_in"
    assert coordinator._effective_stream("batt_out") == "batt_out"
    assert coordinator._effective_stream("solar") == "solar"


@pytest.mark.asyncio
async def test_effective_stream_swaps_battery_when_inverted(hass: HomeAssistant, mock_entry):
    """Invert-flagga satt: batt_in läser urladdnings-sensorn, batt_out laddnings-sensorn.
    Nät/sol är orörda (bara batteriets riktning vänds)."""
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
    """Flaggan ändrad sedan senaste upload (applied_invert i state) → bokmärket nollas →
    full re-backfill (LTS 'hour'-fönstret hämtas) så historiken skrivs över med rätt riktning."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY

    mock_entry.data = {**mock_entry.data, CONF_INVERT_BATTERY: True}
    client = _mock_client()
    fetch_calls: list[str] = []

    async def mock_fetch(h, ids, start, end, period):
        fetch_calls.append(period)
        return {}

    bookmark_str = (NOW - timedelta(days=1)).isoformat()  # färskt → normalt inkrementellt
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

    assert "hour" in fetch_calls, "invert-ändring ska tvinga full backfill (LTS-fönstret)"
    assert coordinator._state.get("applied_invert") is True


@pytest.mark.asyncio
async def test_unchanged_invert_keeps_incremental(hass: HomeAssistant, mock_entry):
    """Oförändrad flagga (applied_invert == aktuell) → bokmärket behålls → inkrementell väg
    (inget LTS-'hour'-fönster hämtas)."""
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

    assert "hour" not in fetch_calls, "oförändrad flagga ska inte tvinga backfill"
    assert coordinator._state.get("last_uploaded_ts") == bookmark_str
