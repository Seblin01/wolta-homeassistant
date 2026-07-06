"""Sensor platform for the Wolta integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_TOKEN, DOMAIN, profile_url
from .coordinator import WoltaCoordinator, WoltaData


# ---------------------------------------------------------------------------
# Extended description dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class WoltaSensorEntityDescription(SensorEntityDescription):
    """Sensor description with value/availability/attribute callables."""

    value_fn: Callable[[dict], Any] = lambda r: None
    attr_fn: Callable[[WoltaData], dict[str, Any]] = lambda d: {}
    available_fn: Callable[[dict], bool] = lambda r: True


# ---------------------------------------------------------------------------
# Sensor descriptions
# ---------------------------------------------------------------------------


def _betyg_score(results: dict) -> float | None:
    betyg = results.get("betyg") or {}
    holistic = betyg.get("holistic") or {}
    score_on = holistic.get("score_on")
    if score_on is None:
        return None
    return round(score_on * 100, 2)


def _period_end_ts(results: dict) -> datetime | None:
    """period.end är en date-sträng ('YYYY-MM-DD'); en TIMESTAMP-sensor kräver en
    aware datetime, annars blir state:t ogiltigt i HA. Parsa + fäst UTC om naiv."""
    end = (results.get("period") or {}).get("end")
    if not end:
        return None
    try:
        parsed = datetime.fromisoformat(end)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _betyg_available(results: dict) -> bool:
    betyg = results.get("betyg") or {}
    holistic = betyg.get("holistic") or {}
    return holistic.get("score_on") is not None


def _decision_available(results: dict) -> bool:
    return results.get("decision") is not None


def _history_available(results: dict) -> bool:
    hist = results.get("history")
    if hist is None:
        return False
    yearly = hist.get("yearly") if isinstance(hist, dict) else None
    return bool(yearly)


def _currency(results: dict) -> str:
    return results.get("currency") or "SEK"


# Serverstatus → stabilt enum-state (slug) för statussensorn. Visningen översätts via
# translation_key (sv: Klar/Beräknar/Väntar på data/Fel; en: Done/Computing/...) – v0.4.3.
_STATUS_MAP = {
    "done": "done",
    "pending": "computing",
    "running": "computing",
    "error": "error",
    "cold": "waiting_for_data",
    "no_data": "waiting_for_data",
}
_STATUS_OPTIONS = ["done", "computing", "waiting_for_data", "error"]


SENSOR_DESCRIPTIONS: tuple[WoltaSensorEntityDescription, ...] = (
    WoltaSensorEntityDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=_STATUS_OPTIONS,
        value_fn=lambda r: _STATUS_MAP.get(r.get("status"), "waiting_for_data"),
        attr_fn=lambda data: {
            "server_status": data.results.get("status"),
            "job": (data.results.get("job") or {}).get("status"),
            "step": (data.results.get("job") or {}).get("step"),
        },
    ),
    WoltaSensorEntityDescription(
        key="optimeringsbetyg",
        translation_key="optimeringsbetyg",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_betyg_score,
        available_fn=_betyg_available,
        attr_fn=lambda data: (
            {
                "peer_percentile": (
                    (data.results.get("betyg") or {}).get("peer") or {}
                ).get("percentile"),
                "peer_n": (
                    (data.results.get("betyg") or {}).get("peer") or {}
                ).get("n"),
                "gap_sek": (data.results.get("betyg") or {}).get("gap_sek"),
                "price_skill": (data.results.get("betyg") or {}).get("price_skill"),
                "components": (data.results.get("betyg") or {}).get("components"),
            }
            if _betyg_available(data.results)
            else {"reason": "not enough data for a grade yet"}
        ),
    ),
    WoltaSensorEntityDescription(
        key="batterivarde_ar",
        translation_key="batterivarde_ar",
        # unit set dynamically in sensor class
        suggested_display_precision=0,
        value_fn=lambda r: (r.get("decision") or {}).get("avg_annual_sek"),
        available_fn=_decision_available,
        attr_fn=lambda data: (
            {}
            if _decision_available(data.results)
            else {"reason": "economy calculations are only available for Swedish price zones"}
        ),
    ),
    WoltaSensorEntityDescription(
        key="irr",
        translation_key="irr",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: (
            round((r.get("decision") or {}).get("irr") * 100, 2)
            if (r.get("decision") or {}).get("irr") is not None
            else None
        ),
        available_fn=_decision_available,
        attr_fn=lambda data: (
            {}
            if _decision_available(data.results)
            else {"reason": "economy calculations are only available for Swedish price zones"}
        ),
    ),
    WoltaSensorEntityDescription(
        key="payback",
        translation_key="payback",
        native_unit_of_measurement="yr",
        value_fn=lambda r: (r.get("decision") or {}).get("payback_years"),
        available_fn=_decision_available,
        attr_fn=lambda data: (
            {}
            if _decision_available(data.results)
            else {"reason": "economy calculations are only available for Swedish price zones"}
        ),
    ),
    WoltaSensorEntityDescription(
        key="facit_i_ar",
        translation_key="facit_i_ar",
        # unit set dynamically in sensor class
        suggested_display_precision=0,
        value_fn=lambda r: (
            (r.get("history") or {}).get("yearly", [{}])[-1].get("total_sek")
            if (r.get("history") or {}).get("yearly")
            else None
        ),
        available_fn=_history_available,
        attr_fn=lambda data: (
            {
                "yearly": (data.results.get("history") or {}).get("yearly"),
                "breakeven_date": (data.results.get("history") or {}).get(
                    "breakeven_date"
                ),
                "breakeven_total_years": (data.results.get("history") or {}).get(
                    "breakeven_total_years"
                ),
            }
            if _history_available(data.results)
            else {"reason": "economy calculations are only available for Swedish price zones"}
        ),
    ),
    WoltaSensorEntityDescription(
        key="datastatus",
        translation_key="datastatus",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_period_end_ts,
        available_fn=lambda r: _period_end_ts(r) is not None,
        attr_fn=lambda data: {
            "n_days": data.n_days,
            "pending": data.pending,
            "last_uploaded": (
                data.last_uploaded.isoformat() if data.last_uploaded else None
            ),
        },
    ),
)

# Keys whose unit depends on results.currency
_CURRENCY_KEYS = frozenset({"batterivarde_ar", "facit_i_ar"})


# ---------------------------------------------------------------------------
# Entity class
# ---------------------------------------------------------------------------


class WoltaSensor(CoordinatorEntity[WoltaCoordinator], SensorEntity):
    """A single Wolta sensor backed by the coordinator."""

    entity_description: WoltaSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WoltaCoordinator,
        entry: ConfigEntry,
        description: WoltaSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        # v0.4.2: senaste kända värde/attribut – serveras under en pågående server-
        # beräkning (recompute byter fingerprint → betyg/decision saknas någon minut)
        # så sensorerna slipper unavailable-blippen. Bara i minnet: efter HA-omstart
        # mitt i en beräkning blir sensorn unavailable tills beräkningen är klar (ok).
        self._last_value: Any = None
        self._last_attrs: dict[str, Any] | None = None
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Wolta",
            manufacturer="Wolta",
            entry_type="service",
            configuration_url=profile_url(entry.data[CONF_TOKEN]),
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Sensor is available when coordinator has data and source is not None.
        Under en pågående server-beräkning (pending) räknas sensorn som available
        om ett tidigare känt värde finns att behålla."""
        if not self.coordinator.last_update_success or self.coordinator.data is None:
            return False
        if self.entity_description.available_fn(self.coordinator.data.results):
            return True
        return self.coordinator.data.pending and self._last_value is not None

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        val = self.entity_description.value_fn(self.coordinator.data.results)
        if val is None and self.coordinator.data.pending:
            return self._last_value
        if val is not None:
            self._last_value = val
        return val

    @property
    def native_unit_of_measurement(self) -> str | None:
        """For currency sensors, read unit from results.currency dynamically."""
        if self.entity_description.key in _CURRENCY_KEYS:
            if self.coordinator.data is None:
                return None
            return _currency(self.coordinator.data.results)
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        data = self.coordinator.data
        if (
            not self.entity_description.available_fn(data.results)
            and data.pending
            and self._last_attrs is not None
        ):
            # Behållna attribut under omräkning, flaggade så det syns i UI:t (v0.4.4: eng. nycklar)
            return {**self._last_attrs, "computing": True}
        attrs = self.entity_description.attr_fn(data)
        if self.entity_description.available_fn(data.results):
            self._last_attrs = attrs
        return attrs


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wolta sensors from a config entry."""
    coordinator: WoltaCoordinator = entry.runtime_data
    async_add_entities(
        WoltaSensor(coordinator=coordinator, entry=entry, description=desc)
        for desc in SENSOR_DESCRIPTIONS
    )
