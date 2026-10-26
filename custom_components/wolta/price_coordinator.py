"""Coordinator for the public spot-price endpoints (GET /api/v1/prices/now and, only when
the forecast sensor is enabled, GET /api/v1/prices/forecast).

Deliberately separate from WoltaCoordinator. The price forecast is information for the
user and must never influence the optimisation grade, so this module reads nothing the
grade, the upload or the profile use and writes nothing back to them. A failing price
endpoint only makes the price sensors unavailable; it never touches the existing sensors.
The forecast endpoint is an optional extra on top of /prices/now: when it fails, the
coordinator keeps the /prices/now data and only the days beyond tomorrow are missing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import WoltaApiClient, WoltaApiError
from .const import CONF_ZONE, DOMAIN, PRICE_SENSOR_ZONES

_LOGGER = logging.getLogger(__name__)

PRICE_POLL = timedelta(minutes=15)
# The price series is quarter-hourly: a point covers the 15 minutes after its start.
QUARTER = timedelta(minutes=15)
# Key under which the coordinator data carries the /prices/forecast response (None when the
# request failed or there was no forecast). Absent while no forecast sensor is enabled.
FULL_FORECAST_KEY = "full_forecast"


def _price(point: dict[str, Any]) -> float | None:
    """SEK/kWh (ex VAT) from a point's ore_kwh; None when the backend had no FX rate
    (or sent something that is not a number)."""
    ore = point.get("ore_kwh")
    if isinstance(ore, bool) or not isinstance(ore, (int, float)):
        return None
    return round(ore / 100, 5)


def _points(resp: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The well-formed points of one list in the response: dicts with a string ts_utc.
    The attributes are rebuilt on every state write, so a bad point is skipped, never raised."""
    raw = resp.get(key)
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, dict) and isinstance(p.get("ts_utc"), str)]


def _horizon(day: dict[str, Any]) -> int:
    """The day's horizon (1 = tomorrow, 2, 3) as an int; 0 when missing or not an int."""
    horizon = day.get("horizon")
    return horizon if isinstance(horizon, int) and not isinstance(horizon, bool) else 0


def _later_days(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """The days of the /prices/forecast response beyond tomorrow (horizon 2 and 3), in
    horizon order. Horizon 1 is tomorrow's forecast, which /prices/now already carries."""
    full = resp.get(FULL_FORECAST_KEY)
    days = full.get("days") if isinstance(full, dict) else None
    if not isinstance(days, list):
        return []
    later = [d for d in days if isinstance(d, dict) and _horizon(d) >= 2]
    return sorted(later, key=_horizon)


def forecast_days(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-day forecast metadata for horizons 2 and 3: [{"date", "horizon", "issued_at",
    "accuracy"}]. No points - they are in the price series. Each day has its own issue
    time and accuracy, which is why this is a list and not one object."""
    return [
        {
            "date": day.get("date"),
            "horizon": _horizon(day),
            "issued_at": day.get("issued_at"),
            "accuracy": day.get("accuracy"),
        }
        for day in _later_days(resp)
    ]


def _start_key(item: dict[str, Any]) -> float:
    """Sort key of a series item: its start as a timestamp; an unparsable start sorts last."""
    start = dt_util.parse_datetime(item["start"])
    return start.timestamp() if start is not None and start.tzinfo is not None else float("inf")


def price_series(resp: dict[str, Any], *, with_forecast: bool) -> list[dict[str, Any]]:
    """Hourly/quarterly price points for a price graph: today, tomorrow, then (with
    with_forecast) the forecast points: tomorrow's from /prices/now (horizon 1) and, when
    the coordinator fetched /prices/forecast, the days beyond (horizon 2 and 3). Sorted by
    start, and a timestamp is never repeated: the first source wins, so horizon 1 comes
    from /prices/now even when /prices/forecast has it too.

    Each point is {"start": ts_utc, "price": SEK/kWh EXCLUDING VAT | None} (the API's
    ore_kwh is ex VAT; the VAT factor travels separately as `vat_factor` in the response
    and is not applied here); with_forecast adds "forecast": bool so a chart can tell
    real prices from forecast ones and, on forecast points, "horizon": 1 (tomorrow), 2 or
    3 (days ahead; the uncertainty grows with it)."""
    series: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(points: list[dict[str, Any]], is_forecast: bool, horizon: int | None = None) -> None:
        for point in points:
            start = point["ts_utc"]
            if start in seen:
                continue
            seen.add(start)
            item: dict[str, Any] = {"start": start, "price": _price(point)}
            if with_forecast:
                item["forecast"] = is_forecast
                if horizon is not None:
                    item["horizon"] = horizon
            series.append(item)

    _add(_points(resp, "today"), False)
    _add(_points(resp, "tomorrow"), False)
    if with_forecast:
        forecast = resp.get("forecast")
        if isinstance(forecast, dict):
            _add(_points(forecast, "points"), True, 1)
        later = _later_days(resp)
        for day in later:
            _add(_points(day, "points"), True, _horizon(day))
        if later:
            # The API sorts each list, but not across them: sort the merged series (stable).
            series.sort(key=_start_key)
    return series


def current_price(resp: dict[str, Any], now: datetime) -> float | None:
    """SEK/kWh (ex VAT) of the quarter that covers `now`, from the real prices (today and
    tomorrow, never the forecast). Computed locally instead of reading the server's `now`:
    that one is only as fresh as the last poll (which is not aligned to the quarter) and
    the server's own cache. None when no point covers `now` - stale data after a missed
    poll or midnight - or the covering point has no price."""
    # The 15-minute width (QUARTER) assumes Wolta's quarter-hour price rows (since 2025-10-01).
    best: tuple[datetime, dict[str, Any]] | None = None
    for key in ("today", "tomorrow"):
        for point in _points(resp, key):
            start = dt_util.parse_datetime(point["ts_utc"])
            if start is None or start.tzinfo is None or start > now:
                continue
            if best is None or start > best[0]:
                best = (start, point)
    if best is None or now - best[0] >= QUARTER:
        return None
    return _price(best[1])


class WoltaPriceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls /prices/now for the entry's zone (public endpoint, no token) and, while an
    entity has asked for it (async_request_full_forecast), /prices/forecast as well."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: WoltaApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_prices",
            config_entry=entry,
            update_interval=PRICE_POLL,
        )
        self._client = client
        # How many entities currently want the days beyond tomorrow; 0 = never request them.
        self._full_forecast_requests = 0
        # True while the last /prices/forecast request failed: a lasting outage is logged
        # once at info level, the repeats at debug.
        self._full_forecast_failing = False

    @callback
    def async_request_full_forecast(self) -> Callable[[], None]:
        """Register interest in /prices/forecast (the days beyond tomorrow). While no
        registration exists the coordinator makes no forecast request. Returns the function
        that withdraws this registration (safe to call more than once)."""
        self._full_forecast_requests += 1
        active = True

        @callback
        def _release() -> None:
            nonlocal active
            if active:
                active = False
                self._full_forecast_requests -= 1

        return _release

    async def _fetch_full_forecast(self, zone: str) -> dict[str, Any] | None:
        """/prices/forecast, or None. Never raises: this endpoint only adds the days beyond
        tomorrow, so its failure must not fail the coordinator and with it the spot price,
        which comes from /prices/now."""
        try:
            forecast = await self._client.get_price_forecast(zone)
        except (WoltaApiError, aiohttp.ClientError, TimeoutError) as err:
            log = _LOGGER.debug if self._full_forecast_failing else _LOGGER.info
            log(
                "Could not fetch the price forecast for the days after tomorrow (%s); "
                "showing the forecast for tomorrow only until it works again",
                err,
            )
            self._full_forecast_failing = True
            return None
        self._full_forecast_failing = False
        return forecast

    async def _async_update_data(self) -> dict[str, Any]:
        # Read the zone on every poll: the options step "Correct the price zone"
        # updates entry.data without a reload (the main coordinator does the same).
        zone = self.config_entry.data[CONF_ZONE]
        if zone not in PRICE_SENSOR_ZONES:
            # The sensors show SEK with Swedish VAT; another zone's prices would be
            # mislabelled. Make no request and stay unavailable until a reload.
            raise UpdateFailed(
                f"Price zone {zone} is not a Swedish zone; the spot price sensors "
                "are unavailable until the integration is reloaded"
            )
        try:
            data = await self._client.get_prices_now(zone)
        except (WoltaApiError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Could not fetch Wolta prices: {err}") from err
        if not isinstance(data, dict):
            raise UpdateFailed("Unexpected response from Wolta prices endpoint")
        if self._full_forecast_requests > 0:
            data = {**data, FULL_FORECAST_KEY: await self._fetch_full_forecast(zone)}
        return data
