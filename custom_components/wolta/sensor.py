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
    """Optimisation grade in percent: holistic.score_on x 100, published RAW.

    Spec 2026-07-27 (nettoperiod): the grade is now net of battery wear on both sides, so
    score_on has no lower bound - a plant that cycles harder than is profitable scores
    BELOW zero (worse than a fully passive battery). The value is deliberately not floored
    at 0 here, unlike wolta.se, which refuses to render a negative grade as a number at all
    and shows an "under the baseline" state instead.

    The two surfaces differ because they serve different consumers. A number on a web page
    is a claim made to a reader, and "0" would read as "exactly at the baseline" - a lie
    about a plant that did materially worse. A sensor state is data: it feeds automations,
    history and statistics, where the sign is the actionable signal and flooring would
    silently erase how bad the situation is (and make "at baseline" and "far below
    baseline" indistinguishable forever in the recorder).

    Upper end is untouched too: the backend clamps score_on at 1.05, so values slightly
    above 100 percent are expected and are not an error.
    """
    betyg = results.get("betyg") or {}
    holistic = betyg.get("holistic") or {}
    score_on = holistic.get("score_on")
    if score_on is None:
        return None
    return round(score_on * 100, 2)


def _period_end_ts(results: dict) -> datetime | None:
    """period.end is a date string ('YYYY-MM-DD'); a TIMESTAMP sensor requires an
    aware datetime, otherwise the state becomes invalid in HA. Parse + attach UTC if naive."""
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


def _measured_battery_value(results: dict) -> float | None:
    """Annualized measured battery value: holistic.measured_period_sek x annual.factor.

    Spec 2026-07-27 (nettoperiod): the payload went period-first - measured_period_sek is
    a WINDOW sum, not a pre-annualized figure, so the sensor does the x annual.factor
    multiplication itself to keep a stable kr/year unit (HA statistics require it, and the
    energy dashboard would break on a rolling-window unit).

    GROSS, not net. measured_period_sek is the measured battery value BEFORE the wear
    deduction (the backend sets it from g.measured_total_sek and reports the wear
    separately as measured_wear_sek). Keeping gross here is deliberate: it preserves
    continuity of the long-term HA statistics for this entity (the value did not silently
    shift basis mid-history) and it matches the decision.avg_battery_sek fallback in
    _battery_value below, which is a gross modelled figure - a fallback that changed
    meaning depending on which branch produced it would be worse than a documented
    mismatch with the website. Do not "fix" this by subtracting measured_wear_sek without
    also changing the fallback and accepting the statistics discontinuity.

    Comparing this against wolta.se: two things differ, WEAR and UNIT, and only the wear
    part is a basis choice. This sensor is always SEK/year. The site's "Du fangade" figure
    follows the upload length: below 365 days (annual.basis "extrapolated") it is a SUM IN
    KR FOR THE PERIOD, and only from 365 days ("measured") is it a kr/year average. The
    directly comparable figure on the site is therefore the greyed "uppraknat till helar"
    line below 365 days, and "Du fangade" itself from 365 days. That figure is net while
    the site's wear toggle is on (default), so it sits measured_wear_sek x annual.factor
    below this sensor; with the toggle off it matches exactly.

    Worked example, 120-day plant (period 1123, wear 210, factor 3.044): sensor 3418
    kr/year, site "Du fangade 913 kr" (period sum, net), site's yearly projection 2779
    kr/year. 3418 - 2779 = 639 = the wear; the rest of the way down to 913 is the unit.
    An earlier version of this note claimed the gap was measured_wear_sek x 3.65 against
    "Du fangade" - that only ever held for >= 365-day plants, where the units agree.

    Note that the GRADE (see _betyg_score) IS net of wear on both sides. Grade and battery
    value answer different questions - how well you controlled it, versus what it earned -
    and do not have to share a wear convention.

    Gate: annual must be present WITH a factor, AND score_on must be set. annual's presence
    is the backend-owned maturity threshold (same 30-day floor as the old preliminary flag,
    now computed server-side) - but annual is also present on >= 30-day FALLBACK payloads
    (score_on still None), so the score_on check must stay or a fallback plant would start
    showing a value where it correctly shows none today.

    The factor check is DEFENSIVE, not a live branch. Since the F12 audit (2026-08-20) the
    backend refuses to annualise a window shorter than 180 days - a 32-day summer window
    x 11.4 is not a yearly figure (two HA instances against the SAME battery reported 3713
    and 5789 SEK/year for exactly that reason) - but it signals that by omitting the whole
    `annual` block, which the `not annual` leg above already handles. It deliberately does
    NOT send a factor-less dict: versions up to v0.27.0 did `annual["factor"]` as soon as
    the block was truthy, which raises KeyError inside native_value/available and leaves a
    dead sensor until the user updates. Keep this check anyway so a future payload change
    degrades to "no value" instead of throwing."""
    betyg = results.get("betyg") or {}
    holistic = betyg.get("holistic") or {}
    annual = betyg.get("annual")
    if not annual or annual.get("factor") is None or holistic.get("score_on") is None:
        return None
    period = holistic.get("measured_period_sek")
    return round(period * annual["factor"], 2) if period is not None else None


def _battery_value(results: dict) -> float | None:
    """Battery value per year – MEASURED from a mature grade when available
    (holistic.measured_period_sek x annual.factor; wolta.se itself shows the raw window
    sum primarily under 365 days, the sensor applies the same factor to stay annualized),
    otherwise the decision engine's modeled battery share (avg_battery_sek).
    NEVER decision.avg_annual_sek – that's the whole plant's savings
    including the solar value (plan 33)."""
    measured = _measured_battery_value(results)
    if measured is not None:
        return measured
    return (results.get("decision") or {}).get("avg_battery_sek")


def _battery_value_available(results: dict) -> bool:
    return _battery_value(results) is not None


def _history_available(results: dict) -> bool:
    hist = results.get("history")
    if hist is None:
        return False
    yearly = hist.get("yearly") if isinstance(hist, dict) else None
    return bool(yearly)


def _currency(results: dict) -> str:
    return results.get("currency") or "SEK"


def _applied_tariff_attr(results: dict) -> dict[str, Any]:
    """applied_tariff (plan 35 / issue #1): the grid fee/markup/export premium the
    grade calculation actually used (own values or the Swedish default). Top-level
    key on /results — omitted entirely (not None) when the backend hasn't sent it,
    so older cached responses don't show a stale/empty attribute."""
    applied = results.get("applied_tariff")
    if applied is None:
        return {}
    return {"applied_tariff": applied}


def _capex_scope_attr(results: dict) -> dict[str, Any]:
    """capex_scope (backend 2026-07-18): whether the decision block's irr/payback are
    attributed to the battery alone ("battery", plan 33 – native HA profiles) or to the
    whole plant ("plant" – profiles whose scalar purchase price covers solar + battery,
    e.g. wolta.se guide profiles adopted into HA). Omitted entirely when the backend
    hasn't sent it (older API), so cached responses stay clean."""
    scope = (results.get("decision") or {}).get("capex_scope")
    if scope is None:
        return {}
    return {"capex_scope": scope}


def _applied_reserve_attr(results: dict) -> dict[str, Any]:
    """applied_reserve (plan 38 / issue #1): the SoC reserve floor the grade
    calculation actually used, when one is configured. Top-level key on /results —
    omitted entirely (not None) when the backend hasn't sent one (no reserve
    configured, or no grade cached yet), so existing automations checking for its
    presence keep working unchanged."""
    applied = results.get("applied_reserve")
    if applied is None:
        return {}
    return {"applied_reserve": applied}


def _capacity_hint_attr(results: dict) -> dict[str, Any]:
    """capacity_hint (plan 37 / issue #1): set by the backend when the entered battery
    capacity is clearly higher than what the measured data shows (nameplate vs usable).
    Omitted entirely (not None) when absent, so older cached responses stay clean."""
    hint = (results.get("betyg") or {}).get("capacity_hint")
    if hint is None:
        return {}
    return {"capacity_hint": hint}


def _measured_params_attr(results: dict) -> dict[str, Any]:
    """Measured battery parameters: capacity/power/efficiency as the uploaded meter data
    actually shows them (the observed_* dicts, in the payload since the v0.12.0
    adopt-repairs era), plus a status enum (ok/immature/unmeasurable) explaining why a
    value is absent (observed_*_status, backend api 0.51.0). Mirrors the payload as-is –
    no preliminary-gating here, unlike the adopt repairs
    (coordinator._evaluate_measured_params), which suggest CHANGING the config and
    therefore must not act on immature measurements. Every key is omitted entirely (not
    None) when its payload field is absent – an older cached grade can carry values
    without statuses, or neither – same absent-vs-false contract as _capacity_hint_attr
    above."""
    betyg = results.get("betyg") or {}
    attrs: dict[str, Any] = {}
    for field, value_key, value_attr, status_attr in (
        ("observed_capacity", "kwh", "measured_capacity_kwh", "measured_capacity_status"),
        ("observed_power", "kw", "measured_power_kw", "measured_power_status"),
        ("observed_eff", "eff", "measured_efficiency", "measured_efficiency_status"),
    ):
        obs = betyg.get(field)
        if obs:
            attrs[value_attr] = obs[value_key]
        status = betyg.get(f"{field}_status")
        if status is not None:
            attrs[status_attr] = status
    return attrs


# Server status → stable enum state (slug) for the status sensor. The display is translated via
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
                "annual_basis": (
                    (data.results.get("betyg") or {}).get("annual") or {}
                ).get("basis"),
                "measured_period_sek": (
                    (data.results.get("betyg") or {}).get("holistic") or {}
                ).get("measured_period_sek"),
                "measured_wear_sek": (
                    (data.results.get("betyg") or {}).get("holistic") or {}
                ).get("measured_wear_sek"),
                "price_skill": (data.results.get("betyg") or {}).get("price_skill"),
                "components": (data.results.get("betyg") or {}).get("components"),
                # Preliminärt betyg (backend api 0.43.0): score från 7 dygn, flaggat tills
                # 30. Attribut (inte egen sensor) - dashboards/automationer kan märka
                # osäkerheten utan att betygsentiteten byter identitet.
                "preliminary": (data.results.get("betyg") or {}).get("preliminary"),
                "n_days": (data.results.get("betyg") or {}).get("n_days"),
                **_applied_tariff_attr(data.results),
                **_applied_reserve_attr(data.results),
                **_capacity_hint_attr(data.results),
                **_measured_params_attr(data.results),
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
        value_fn=_battery_value,
        available_fn=_battery_value_available,
        attr_fn=lambda data: (
            {
                # Samma mognadsgrind som value_fn: ett omoget betyg får inte stämpla
                # modellvärdet som "measured".
                "source": (
                    "measured"
                    if _measured_battery_value(data.results) is not None
                    else "modelled"
                ),
                "plant_total_sek": (data.results.get("decision") or {}).get(
                    "avg_annual_sek"
                ),
            }
            if _battery_value_available(data.results)
            else {"reason": "not enough data for a grade yet"}
        ),
    ),
    WoltaSensorEntityDescription(
        key="anlaggningsbesparing_ar",
        translation_key="anlaggningsbesparing_ar",
        # unit set dynamically in sensor class
        suggested_display_precision=0,
        value_fn=lambda r: (r.get("decision") or {}).get("avg_annual_sek"),
        available_fn=_decision_available,
        attr_fn=lambda data: (
            {
                "battery_sek": (data.results.get("decision") or {}).get(
                    "avg_battery_sek"
                ),
                "solar_sek": (data.results.get("decision") or {}).get(
                    "avg_solar_sek"
                ),
            }
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
            _capex_scope_attr(data.results)
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
            _capex_scope_attr(data.results)
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
        # v0.4.2: last known value/attribute – served during an in-progress server
        # computation (recompute changes the fingerprint → betyg/decision is missing for a minute)
        # so the sensors avoid the unavailable blip. In-memory only: after an HA restart
        # mid-computation, the sensor is unavailable until the computation finishes (fine).
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
        During an in-progress server computation (pending), the sensor counts as available
        if a previously known value is there to keep."""
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
            # Retained attributes during recompute, flagged so it's visible in the UI (v0.4.4: eng. keys)
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
