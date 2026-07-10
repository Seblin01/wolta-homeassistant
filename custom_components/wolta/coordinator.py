"""DataUpdateCoordinator for the Wolta integration."""

from __future__ import annotations

import json
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
    CONF_SOLAR,
    CONF_SURCHARGE_ORE,
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
    sum_quarter_dicts,
)

_LOGGER = logging.getLogger(__name__)

# How far back to backfill on first install
# Polling rate: slow at rest, fast while a server-side computation is in progress (so the grade
# shows up shortly after the worker finishes instead of at the next 6h poll).
_SLOW_POLL = timedelta(hours=6)
_FAST_POLL = timedelta(seconds=60)

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
_ISSUE_PROFILE_FULL = "profile_full"

# Profile fields mirrored from the server into entry.data (cache only – the server
# is the source of truth; entry.data is never used as a diff base since v0.10.0).
_PROFILE_SYNC_KEYS: tuple[str, ...] = (
    CONF_ZONE, CONF_BATTERY_KWH, CONF_BATTERY_KW, CONF_EFF, CONF_RESERVE_PCT,
    CONF_COST_SEK, CONF_PURCHASE_DATE, CONF_GRID_VAR_ORE, CONF_SURCHARGE_ORE,
    CONF_EXPORT_EXTRA_ORE,
)


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


type WoltaConfigEntry = ConfigEntry[WoltaCoordinator]


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
            update_interval=_SLOW_POLL,
        )
        self.token: str = entry.data[CONF_TOKEN]
        self._zone: str = entry.data[CONF_ZONE]

        # Normalise entry data to lists for backward compat with v0.1.0 (plain strings)
        def _to_list(val: str | list | None) -> list[str]:
            if isinstance(val, list):
                return val
            if isinstance(val, str) and val:
                return [val]
            return []

        self._entity_map: dict[str, list[str]] = {
            "batt_in": _to_list(entry.data.get(CONF_BATT_IN)),
            "batt_out": _to_list(entry.data.get(CONF_BATT_OUT)),
            "grid_in": _to_list(entry.data.get(CONF_GRID_IN)),
            "grid_out": _to_list(entry.data.get(CONF_GRID_OUT)),
            "solar": _to_list(entry.data.get(CONF_SOLAR)),
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

    def _apply_profile_sync(self, profile: dict) -> None:
        """Mirror server-side profile fields into entry.data (write only on change).

        entry.data is a CACHE of the shared profile – the server is the source of
        truth. No update listener is registered, so async_update_entry does not
        trigger a reload; we only write when something actually changed to avoid
        a disk write per poll tick.
        """
        entry = self.config_entry
        new_data = dict(entry.data)
        changed = False
        for key in _PROFILE_SYNC_KEYS:
            server_val = profile.get(key)
            if server_val is None:
                if key in new_data:
                    new_data.pop(key)
                    changed = True
            elif new_data.get(key) != server_val:
                new_data[key] = server_val
                changed = True
        if not changed:
            return
        zone = new_data.get(CONF_ZONE, self._zone)
        self._zone = zone
        self.hass.config_entries.async_update_entry(
            entry, data=new_data, title=f"Wolta ({zone})"
        )

    async def _async_update_data(self) -> WoltaData:
        """Fetch statistics, upload to Wolta, trigger recompute, return results."""
        try:
            now = dt_util.utcnow()
            # Profile sync (server = source of truth): mirror web-side changes into
            # entry.data. Network errors are non-fatal – the cache just stays stale.
            # Skipped during fast-poll (60 s while a server job runs) – nothing
            # profile-related changes on that timescale and it saves a GET per tick.
            if self.update_interval != _FAST_POLL:
                try:
                    profile = await self.client.get_profile(self.token)
                except WoltaAuthError:
                    raise  # purged profile → same reauth path as results()
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.debug("Profile sync fetch failed; keeping cache", exc_info=True)
                else:
                    self._apply_profile_sync(profile)

            # Invert flag changed since last upload (issue #1) → reset the bookmark so the next step
            # runs a FULL re-backfill: the entire history is re-read and overwritten (PUT upserts) with
            # the corrected battery direction. applied_invert is recorded in state so this only happens
            # on an actual change, not on every tick.
            invert_now = bool(self.config_entry.data.get(CONF_INVERT_BATTERY))
            applied_invert = self._state.get("applied_invert")
            if applied_invert is None:
                # First-time recording (e.g. an existing install upgrading): initialize without
                # touching the bookmark – the already-uploaded data was built with the current flag.
                self._state["applied_invert"] = invert_now
                await self._store.async_save(self._state)
            elif applied_invert != invert_now:
                # Actual change → reset the bookmark for a full re-backfill with the corrected direction,
                # and flag that the grade should be recomputed IMMEDIATELY after the upload (bypassing the
                # 7-day cadence in _maybe_recompute) so the corrected grade shows up without a ~24h delay.
                self._state.pop("last_uploaded_ts", None)
                self._state["applied_invert"] = invert_now
                self._state["pending_invert_recompute"] = True
                await self._store.async_save(self._state)

            # Entity selections changed (reconfigure flow) → full re-backfill so the
            # server-side history is rebuilt from the new sensors. Same self-heal
            # pattern as applied_invert above; first-time recording (upgrade) leaves
            # the bookmark untouched.
            entities_now = json.dumps(self._entity_map, sort_keys=True)
            applied_entities = self._state.get("applied_entities")
            if applied_entities is None:
                self._state["applied_entities"] = entities_now
                await self._store.async_save(self._state)
            elif applied_entities != entities_now:
                self._state.pop("last_uploaded_ts", None)
                self._state["applied_entities"] = entities_now
                self._state["pending_invert_recompute"] = True
                await self._store.async_save(self._state)

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
                try:
                    await self.client.put_data(self.token, rows)
                except WoltaApiError as err:
                    if err.status == 413:
                        # Profile hit MAX_PROFILE_ROWS (80k ≈ 2.3 yr of 15-min data,
                        # reachable for linked profiles with web CSV + HA backfill).
                        # Surface via repairs and skip the upload – results still work.
                        # Bookmark is NOT advanced (nothing was persisted server-side).
                        ir.async_create_issue(
                            self.hass,
                            DOMAIN,
                            _ISSUE_PROFILE_FULL,
                            is_fixable=False,
                            severity=ir.IssueSeverity.WARNING,
                            translation_key=_ISSUE_PROFILE_FULL,
                        )
                    else:
                        raise
                else:
                    ir.async_delete_issue(self.hass, DOMAIN, _ISSUE_PROFILE_FULL)
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
            # `pending` = a server-side computation is in progress. Top-level `status` is NOT ENOUGH
            # ALONE: the backend forces status="done" as soon as a grade is cached (profile.py),
            # even while the economy recompute (decision/history) is still running. In that case `job`
            # is the only reliable in-flight signal. Without this, the economy sensors go blank
            # (they only keep their previous value when pending=True) and polling falls back to 6h
            # → a finished decision doesn't show up until the next slow poll (up to 6h).
            job = results.get("job") or {}
            data = WoltaData(
                results=results,
                last_uploaded=last_uploaded,
                n_days=results["period"]["n_days"],
                pending=(
                    results.get("status") in ("pending", "running")
                    or job.get("status") in ("pending", "running")
                ),
            )
            # Poll fast while a server-side computation is in progress so the grade shows up within ~one
            # minute instead of up to 6h; back to the slow rate once it's done.
            self.update_interval = _FAST_POLL if data.pending else _SLOW_POLL
            return data

        except WoltaAuthError as err:
            raise ConfigEntryAuthFailed from err

        except WoltaRateLimitError as err:
            # retry_after MUST be a kwarg (not just in the message) – HA 2025.12+ uses
            # it to schedule the next update instead of the fixed 6h interval.
            raise UpdateFailed(
                f"Wolta rate limited; retry after {err.retry_after}s",
                retry_after=err.retry_after,
            ) from err

        except (aiohttp.ClientError, TimeoutError) as err:
            self._track_failure(err)
            raise UpdateFailed(f"wolta.se unreachable: {err}") from err

    # ------------------------------------------------------------------
    # Row-fetching helpers
    # ------------------------------------------------------------------

    def _sum_stream(
        self,
        raw_data: dict,
        stream: str,
        agg_fn,
    ) -> dict:
        """Aggregate and sum all entity rows for one stream.

        Args:
            raw_data: mapping of statistic_id → rows (from async_fetch_change)
            stream:   stream name key in _entity_map (e.g. "solar")
            agg_fn:   aggregation function — split_hour_to_quarters or aggregate_5min_to_15min

        Returns:
            Summed per-quarter dict for the stream.
        """
        per_entity = [
            agg_fn(raw_data.get(entity_id) or [])
            for entity_id in self._entity_map[self._effective_stream(stream)]
        ]
        return sum_quarter_dicts(per_entity)

    def _effective_stream(self, stream: str) -> str:
        """With the invert flag (issue #1), batt_in reads the discharge sensor and batt_out
        the charge sensor – the battery direction is flipped so a reversed sensor mapping is corrected
        without the user touching their HA sensors. Only the battery currents are affected (grid/solar untouched).
        The flag is read live from config_entry so an options change takes effect without a reload."""
        if not self.config_entry.data.get(CONF_INVERT_BATTERY):
            return stream
        if stream == "batt_in":
            return "batt_out"
        if stream == "batt_out":
            return "batt_in"
        return stream

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

        # Aggregate and sum per stream (short-term takes precedence on overlap via dict merge)
        return merge_streams(
            batt_in={
                **self._sum_stream(lts_data, "batt_in", split_hour_to_quarters),
                **self._sum_stream(short_data, "batt_in", aggregate_5min_to_15min),
            },
            batt_out={
                **self._sum_stream(lts_data, "batt_out", split_hour_to_quarters),
                **self._sum_stream(short_data, "batt_out", aggregate_5min_to_15min),
            },
            grid_in={
                **self._sum_stream(lts_data, "grid_in", split_hour_to_quarters),
                **self._sum_stream(short_data, "grid_in", aggregate_5min_to_15min),
            },
            grid_out={
                **self._sum_stream(lts_data, "grid_out", split_hour_to_quarters),
                **self._sum_stream(short_data, "grid_out", aggregate_5min_to_15min),
            },
            solar={
                **self._sum_stream(lts_data, "solar", split_hour_to_quarters),
                **self._sum_stream(short_data, "solar", aggregate_5min_to_15min),
            },
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
            batt_in=self._sum_stream(lts_data, "batt_in", split_hour_to_quarters),
            batt_out=self._sum_stream(lts_data, "batt_out", split_hour_to_quarters),
            grid_in=self._sum_stream(lts_data, "grid_in", split_hour_to_quarters),
            grid_out=self._sum_stream(lts_data, "grid_out", split_hour_to_quarters),
            solar=self._sum_stream(lts_data, "solar", split_hour_to_quarters),
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
            batt_in=self._sum_stream(short_data, "batt_in", aggregate_5min_to_15min),
            batt_out=self._sum_stream(short_data, "batt_out", aggregate_5min_to_15min),
            grid_in=self._sum_stream(short_data, "grid_in", aggregate_5min_to_15min),
            grid_out=self._sum_stream(short_data, "grid_out", aggregate_5min_to_15min),
            solar=self._sum_stream(short_data, "solar", aggregate_5min_to_15min),
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

        # An invert change (issue #1) just uploaded corrected data via a full re-backfill →
        # FORCE a recompute bypassing the 7-day cadence, otherwise the corrected grade won't show until
        # the nightly rewarm/next tick. The flag is set by the self-heal and only cleared once the recompute
        # succeeds (survives a failed attempt).
        force = bool(self._state.get("pending_invert_recompute"))

        last_recompute_str = self._state.get("last_recompute")

        if not force and last_recompute_str:
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
            self._state.pop("pending_invert_recompute", None)  # corrected – clear the force flag
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
        """Immediately trigger a server-side recompute (button entity + options flow).

        Stamps last_recompute against the current period end on success, so the coordinator
        refresh the caller issues right after does NOT make _maybe_recompute fire a redundant
        second recompute (the backend rejects that with 429). On 429/error the recompute call
        raises before the stamp, so last_recompute is left untouched (retryable next cycle).
        """
        await self.client.recompute(self.token)
        period_end = None
        if self.data is not None:
            period_end = (self.data.results.get("period") or {}).get("end")
        if period_end:
            self._state["last_recompute"] = period_end
            await self._store.async_save(self._state)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _statistic_ids(self) -> set[str]:
        """Return the flat set of all statistic IDs across all stream lists."""
        ids: set[str] = set()
        for entity_list in self._entity_map.values():
            ids.update(entity_list)
        return ids
