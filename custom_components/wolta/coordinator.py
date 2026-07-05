"""DataUpdateCoordinator for the Wolta integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import WoltaApiClient, WoltaApiError, WoltaAuthError, WoltaRateLimitError
from .const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_SOLAR,
    CONF_TOKEN,
    CONF_ZONE,
    DOMAIN,
    WOLTA_API_BASE,
)
from .stats import (
    aggregate_5min_to_15min,
    async_fetch_change,
    merge_streams,
    split_hour_to_quarters,
)

_LOGGER = logging.getLogger(__name__)

# How far back to backfill on first install
_BACKFILL_DAYS = 365

# Short-term statistics window — data older than this is only in LTS
_SHORT_TERM_DAYS = 9

# Trigger a recompute when this many new days of data have been uploaded
_RECOMPUTE_INTERVAL_DAYS = 7

# After this many consecutive days of upload failures, raise a repair issue
_REPAIR_FAILURE_DAYS = 7

# Store version / key
_STORE_VERSION = 1
_STORE_KEY = "wolta_coordinator"

# Issue IDs
_ISSUE_UPLOAD_FAILURE = "upload_failure"


# ---------------------------------------------------------------------------
# Public data type + type alias
# ---------------------------------------------------------------------------


@dataclass
class WoltaData:
    """Data exposed by the coordinator to all platform entities."""

    results: dict  # latest /results response
    last_uploaded: datetime | None
    n_days: int
    pending: bool  # server-side job running


type WoltaConfigEntry = ConfigEntry[WoltaData]


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class WoltaCoordinator(DataUpdateCoordinator[WoltaData]):
    """Manage fetching Wolta results and uploading HA statistics."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="wolta",
            config_entry=entry,
            update_interval=timedelta(hours=6),
        )
        self.token: str = entry.data[CONF_TOKEN]
        self._zone: str = entry.data[CONF_ZONE]
        self._entity_map: dict[str, str | None] = {
            "batt_in": entry.data.get(CONF_BATT_IN),
            "batt_out": entry.data.get(CONF_BATT_OUT),
            "grid_in": entry.data.get(CONF_GRID_IN),
            "grid_out": entry.data.get(CONF_GRID_OUT),
            "solar": entry.data.get(CONF_SOLAR),
        }
        session = async_get_clientsession(hass)
        self.client = WoltaApiClient(session, base_url=WOLTA_API_BASE)
        self._store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        self._state: dict = {}

    # ------------------------------------------------------------------
    # Setup – load persisted state (bookmark, last_recompute, failure counter)
    # ------------------------------------------------------------------

    async def _async_setup(self) -> None:
        """Load persisted coordinator state from Store."""
        loaded = await self._store.async_load()
        self._state = dict(loaded) if loaded else {}
        _LOGGER.debug("Coordinator state loaded: %s", self._state)

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> WoltaData:
        """Fetch statistics, upload to Wolta, trigger recompute, return results."""
        try:
            now = dt_util.utcnow()
            bookmark = self._state.get("last_uploaded_ts")  # ISO str | None

            if bookmark is None:
                rows = await self._backfill_rows(now)
            else:
                start = datetime.fromisoformat(bookmark)
                if now - start > timedelta(days=_SHORT_TERM_DAYS):
                    rows = await self._heal_rows(start, now)
                else:
                    rows = await self._incremental_rows(start, now)

            if rows:
                await self.client.put_data(self.token, rows)
                self._state["last_uploaded_ts"] = rows[-1]["ts"]
                await self._store.async_save(self._state)
                # Clear failure counter on success
                self._state.pop("consecutive_failure_days", None)
                ir.async_delete_issue(self.hass, DOMAIN, _ISSUE_UPLOAD_FAILURE)

            await self._maybe_recompute(now)

            results = await self.client.results(self.token)
            last_uploaded_str = self._state.get("last_uploaded_ts")
            last_uploaded = (
                datetime.fromisoformat(last_uploaded_str)
                if last_uploaded_str
                else None
            )
            return WoltaData(
                results=results,
                last_uploaded=last_uploaded,
                n_days=results["period"]["n_days"],
                pending=results.get("status") in ("pending", "running"),
            )

        except WoltaAuthError as err:
            raise ConfigEntryAuthFailed from err

        except WoltaRateLimitError as err:
            raise UpdateFailed(f"Wolta rate limited; retry after {err.retry_after}s") from err

        except (aiohttp.ClientError, TimeoutError) as err:
            self._track_failure(err)
            raise UpdateFailed(f"wolta.se unreachable: {err}") from err

    # ------------------------------------------------------------------
    # Row-fetching helpers
    # ------------------------------------------------------------------

    async def _backfill_rows(self, now: datetime) -> list[dict]:
        """Backfill up to 12 months: LTS (÷4) for old data + 5-min for recent."""
        start = now - timedelta(days=_BACKFILL_DAYS)
        short_term_start = now - timedelta(days=_SHORT_TERM_DAYS)

        # Fetch hourly LTS for the long window (start → short_term_start)
        lts_data = await async_fetch_change(
            self.hass,
            self._statistic_ids(),
            start,
            short_term_start,
            period="hour",
        )

        # Fetch 5-min short-term for the recent window
        short_data = await async_fetch_change(
            self.hass,
            self._statistic_ids(),
            short_term_start,
            now,
            period="5minute",
        )

        # Aggregate LTS streams (÷4)
        batt_in_lts = split_hour_to_quarters(lts_data.get(self._entity_map["batt_in"]) or [])
        batt_out_lts = split_hour_to_quarters(lts_data.get(self._entity_map["batt_out"]) or [])
        grid_in_lts = split_hour_to_quarters(lts_data.get(self._entity_map["grid_in"]) or [])
        grid_out_lts = split_hour_to_quarters(lts_data.get(self._entity_map["grid_out"]) or [])
        solar_lts = split_hour_to_quarters(lts_data.get(self._entity_map.get("solar", "")) or [])

        # Aggregate short-term streams (5-min → 15-min)
        batt_in_st = aggregate_5min_to_15min(short_data.get(self._entity_map["batt_in"]) or [])
        batt_out_st = aggregate_5min_to_15min(short_data.get(self._entity_map["batt_out"]) or [])
        grid_in_st = aggregate_5min_to_15min(short_data.get(self._entity_map["grid_in"]) or [])
        grid_out_st = aggregate_5min_to_15min(short_data.get(self._entity_map["grid_out"]) or [])
        solar_st = aggregate_5min_to_15min(short_data.get(self._entity_map.get("solar", "")) or [])

        # Merge both windows (dict union; short-term takes precedence on overlap)
        return merge_streams(
            batt_in={**batt_in_lts, **batt_in_st},
            batt_out={**batt_out_lts, **batt_out_st},
            grid_in={**grid_in_lts, **grid_in_st},
            grid_out={**grid_out_lts, **grid_out_st},
            solar={**solar_lts, **solar_st},
        )

    async def _heal_rows(self, start: datetime, now: datetime) -> list[dict]:
        """Heal a gap from LTS (÷4) when the short-term window was missed."""
        lts_data = await async_fetch_change(
            self.hass,
            self._statistic_ids(),
            start,
            now,
            period="hour",
        )
        return merge_streams(
            batt_in=split_hour_to_quarters(lts_data.get(self._entity_map["batt_in"]) or []),
            batt_out=split_hour_to_quarters(lts_data.get(self._entity_map["batt_out"]) or []),
            grid_in=split_hour_to_quarters(lts_data.get(self._entity_map["grid_in"]) or []),
            grid_out=split_hour_to_quarters(lts_data.get(self._entity_map["grid_out"]) or []),
            solar=split_hour_to_quarters(lts_data.get(self._entity_map.get("solar", "")) or []),
        )

    async def _incremental_rows(self, start: datetime, now: datetime) -> list[dict]:
        """Fetch short-term 5-min data since bookmark and aggregate to 15-min."""
        short_data = await async_fetch_change(
            self.hass,
            self._statistic_ids(),
            start,
            now,
            period="5minute",
        )
        return merge_streams(
            batt_in=aggregate_5min_to_15min(short_data.get(self._entity_map["batt_in"]) or []),
            batt_out=aggregate_5min_to_15min(short_data.get(self._entity_map["batt_out"]) or []),
            grid_in=aggregate_5min_to_15min(short_data.get(self._entity_map["grid_in"]) or []),
            grid_out=aggregate_5min_to_15min(short_data.get(self._entity_map["grid_out"]) or []),
            solar=aggregate_5min_to_15min(short_data.get(self._entity_map.get("solar", "")) or []),
        )

    # ------------------------------------------------------------------
    # Recompute logic
    # ------------------------------------------------------------------

    async def _maybe_recompute(self, now: datetime) -> None:
        """Trigger recompute when ≥7 new days of data since last recompute."""
        results = None
        # Use cached data if available to avoid an extra API call
        if self.data is not None:
            results = self.data.results
        else:
            try:
                results = await self.client.results(self.token)
            except (WoltaAuthError, WoltaApiError, aiohttp.ClientError, TimeoutError):
                return

        if results is None:
            return

        period_end_str = (results.get("period") or {}).get("end")
        if not period_end_str:
            return

        last_recompute_str = self._state.get("last_recompute")

        if last_recompute_str:
            try:
                last_recompute_date = datetime.fromisoformat(last_recompute_str).date()
                period_end_date = datetime.fromisoformat(period_end_str).date()
                days_since = (period_end_date - last_recompute_date).days
                if days_since < _RECOMPUTE_INTERVAL_DAYS:
                    return
            except (ValueError, AttributeError):
                pass  # malformed date → fall through to recompute

        # Trigger recompute
        try:
            await self.client.recompute(self.token)
            self._state["last_recompute"] = period_end_str
            await self._store.async_save(self._state)
            _LOGGER.debug("Recompute triggered; last_recompute updated to %s", period_end_str)
        except WoltaRateLimitError:
            _LOGGER.debug(
                "Recompute rate-limited (retry_after); skipping last_recompute update"
            )
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Recompute failed; will retry next cycle", exc_info=True)

    # ------------------------------------------------------------------
    # Repair issue tracking
    # ------------------------------------------------------------------

    def _track_failure(self, err: Exception) -> None:
        """Increment failure counter; raise repair issue after >7 days."""
        failure_since_str = self._state.get("failure_since")
        if failure_since_str is None:
            self._state["failure_since"] = dt_util.utcnow().isoformat()
        else:
            try:
                failure_since = datetime.fromisoformat(failure_since_str)
                days_failing = (dt_util.utcnow() - failure_since).days
                if days_failing > _REPAIR_FAILURE_DAYS:
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        _ISSUE_UPLOAD_FAILURE,
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="upload_failure",
                        translation_placeholders={"error": str(err)},
                    )
            except (ValueError, AttributeError):
                pass

    # ------------------------------------------------------------------
    # Button helper
    # ------------------------------------------------------------------

    async def async_trigger_recompute(self) -> None:
        """Immediately trigger a server-side recompute (used by the button entity)."""
        await self.client.recompute(self.token)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _statistic_ids(self) -> set[str]:
        """Return the set of statistic IDs from the entity map."""
        ids = set()
        for val in self._entity_map.values():
            if val:
                ids.add(val)
        return ids
