"""Coordinator for the public spot-price endpoint (GET /api/v1/prices/now).

Deliberately separate from WoltaCoordinator. The price forecast is information for the
user and must never influence the optimisation grade, so this module reads nothing the
grade, the upload or the profile use and writes nothing back to them. A failing price
endpoint only makes the price sensors unavailable; it never touches the existing sensors.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WoltaApiClient, WoltaApiError
from .const import CONF_ZONE, DOMAIN

_LOGGER = logging.getLogger(__name__)

PRICE_POLL = timedelta(minutes=15)


def _price(point: dict[str, Any]) -> float | None:
    """SEK/kWh (ex VAT) from a point's ore_kwh; None when the backend had no FX rate."""
    ore = point.get("ore_kwh")
    if ore is None:
        return None
    return round(ore / 100, 5)


def price_series(resp: dict[str, Any], *, with_forecast: bool) -> list[dict[str, Any]]:
    """Hourly/quarterly price points for a price graph: today, tomorrow, then (with
    with_forecast and a non-null forecast) the forecast points. The API guarantees the
    three lists are sorted and do not overlap, so concatenation keeps time order.

    Each point is {"start": ts_utc, "price": SEK/kWh | None}; with_forecast adds
    "forecast": bool so a chart can tell real prices from forecast ones."""
    series: list[dict[str, Any]] = []

    def _add(points: list[dict[str, Any]] | None, is_forecast: bool) -> None:
        for point in points or []:
            item: dict[str, Any] = {"start": point["ts_utc"], "price": _price(point)}
            if with_forecast:
                item["forecast"] = is_forecast
            series.append(item)

    _add(resp.get("today"), False)
    _add(resp.get("tomorrow"), False)
    if with_forecast:
        forecast = resp.get("forecast")
        if isinstance(forecast, dict):
            _add(forecast.get("points"), True)
    return series


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
        self._zone: str = entry.data[CONF_ZONE]

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self._client.get_prices_now(self._zone)
        except (WoltaApiError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Could not fetch Wolta prices: {err}") from err
        if not isinstance(data, dict):
            raise UpdateFailed("Unexpected response from Wolta prices endpoint")
        return data
