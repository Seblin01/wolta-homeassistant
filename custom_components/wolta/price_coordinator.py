"""Coordinator for the public spot-price endpoint (GET /api/v1/prices/now).

Deliberately separate from WoltaCoordinator. The price forecast is information for the
user and must never influence the optimisation grade, so this module reads nothing the
grade, the upload or the profile use and writes nothing back to them. A failing price
endpoint only makes the price sensors unavailable; it never touches the existing sensors.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import WoltaApiClient, WoltaApiError
from .const import CONF_ZONE, DOMAIN

_LOGGER = logging.getLogger(__name__)

PRICE_POLL = timedelta(minutes=15)
# The price series is quarter-hourly: a point covers the 15 minutes after its start.
QUARTER = timedelta(minutes=15)


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


def price_series(resp: dict[str, Any], *, with_forecast: bool) -> list[dict[str, Any]]:
    """Hourly/quarterly price points for a price graph: today, tomorrow, then (with
    with_forecast and a non-null forecast) the forecast points. The API guarantees the
    three lists are sorted and do not overlap, so concatenation keeps time order.

    Each point is {"start": ts_utc, "price": SEK/kWh EXCLUDING VAT | None} (the API's
    ore_kwh is ex VAT; the VAT factor travels separately as `vat_factor` in the response
    and is not applied here); with_forecast adds
    "forecast": bool so a chart can tell real prices from forecast ones."""
    series: list[dict[str, Any]] = []

    def _add(points: list[dict[str, Any]], is_forecast: bool) -> None:
        for point in points:
            item: dict[str, Any] = {"start": point["ts_utc"], "price": _price(point)}
            if with_forecast:
                item["forecast"] = is_forecast
            series.append(item)

    _add(_points(resp, "today"), False)
    _add(_points(resp, "tomorrow"), False)
    if with_forecast:
        forecast = resp.get("forecast")
        if isinstance(forecast, dict):
            _add(_points(forecast, "points"), True)
    return series


def current_price(resp: dict[str, Any], now: datetime) -> float | None:
    """SEK/kWh (ex VAT) of the quarter that covers `now`, from the real prices (today and
    tomorrow, never the forecast). Computed locally instead of reading the server's `now`:
    that one is only as fresh as the last poll (which is not aligned to the quarter) and
    the server's own cache. None when no point covers `now` - stale data after a missed
    poll or midnight - or the covering point has no price."""
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
    """Polls /prices/now for the entry's zone (public endpoint, no token)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: WoltaApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_prices",
            config_entry=entry,
            update_interval=PRICE_POLL,
        )
        self._client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            # Read the zone on every poll: the options step "Correct the price zone"
            # updates entry.data without a reload (the main coordinator does the same).
            data = await self._client.get_prices_now(self.config_entry.data[CONF_ZONE])
        except (WoltaApiError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Could not fetch Wolta prices: {err}") from err
        if not isinstance(data, dict):
            raise UpdateFailed("Unexpected response from Wolta prices endpoint")
        return data
