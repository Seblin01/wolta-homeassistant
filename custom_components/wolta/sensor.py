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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_LINK_TOKEN, DOMAIN, KEY_BATTERY_STATUS, WOLTA_API_BASE, link_url
from .coordinator import WoltaCoordinator, WoltaData
from .price_coordinator import (
    FULL_FORECAST_KEY,
    PURCHASE_KEY,
    WoltaPriceCoordinator,
    current_price,
    current_purchase,
    forecast_days,
    price_series,
    purchase_covers,
    purchase_series,
)


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


def _window_block(results: dict) -> dict | None:
    """The owner's grade window (backend spec 2026-10-21 section 4.5): results.betyg.window,
    {score_on, score_pct, period_start, period_end, preliminary, n_days, start_date,
    rolling_days, clipped}. Present only in data mode and only when the owner has set a
    window; absent (not None) in legacy mode and on every payload cached before it. A block
    without a score_on is not a grade, so it counts as absent and the sensor falls back to
    the comparison grade rather than going unavailable."""
    window = (results.get("betyg") or {}).get("window")
    if isinstance(window, dict) and window.get("score_on") is not None:
        return window
    return None


def _comparison_score(results: dict) -> float | None:
    """Comparison grade in percent: holistic.score_on x 100 (the grade that is compared
    with other installations), published RAW. See _betyg_score for the no-clamping rule."""
    holistic = (results.get("betyg") or {}).get("holistic") or {}
    score_on = holistic.get("score_on")
    if score_on is None:
        return None
    return round(score_on * 100, 2)


def _betyg_score(results: dict) -> float | None:
    """Optimisation grade in percent: the owner's window grade (window.score_on x 100) when
    results.betyg.window exists, otherwise holistic.score_on x 100, published RAW.

    The window grade is the owner's own view (their start date or rolling window); the
    comparison grade over the last full months stays on its own sensor (jamforelsebetyg).
    Both use the same raw publishing rule below.

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
    window = _window_block(results)
    if window is not None:
        return round(window["score_on"] * 100, 2)
    return _comparison_score(results)


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
    return _betyg_score(results) is not None


def _comparison_available(results: dict) -> bool:
    return _comparison_score(results) is not None


def _comparison_grade_attrs(results: dict) -> dict[str, Any]:
    """Attributes that describe the COMPARISON grade (holistic.score_on): its percentile and
    sample size, its period (betyg.period_start/period_end at the top level of the betyg
    block), its maturity, and the details of how it was computed (annual basis, measured
    period figures, price skill, components, external control, benchmark class). Rule:
    an attribute that describes a specific grade follows that grade, so these live on the
    comparison grade sensor always, and on the grade sensor only while it shows the
    comparison grade."""
    betyg = results.get("betyg") or {}
    peer = betyg.get("peer") or {}
    holistic = betyg.get("holistic") or {}
    return {
        "peer_percentile": peer.get("percentile"),
        "peer_n": peer.get("n"),
        "period_start": betyg.get("period_start"),
        "period_end": betyg.get("period_end"),
        "preliminary": betyg.get("preliminary"),
        "n_days": betyg.get("n_days"),
        "annual_basis": (betyg.get("annual") or {}).get("basis"),
        "measured_period_sek": holistic.get("measured_period_sek"),
        "measured_wear_sek": holistic.get("measured_wear_sek"),
        "price_skill": betyg.get("price_skill"),
        "components": betyg.get("components"),
        **_external_control_attr(results),
        **_benchmark_attr(results),
    }


def _grade_basis_attr(results: dict) -> dict[str, Any]:
    """Which grade the grade sensor shows (basis: window | comparison) and the attributes of
    that grade. A window grade carries only its own dates and maturity; everything else that
    describes a grade (percentile, annual basis, measured figures, components, benchmark
    class ...) belongs to the comparison grade and is added only for basis == comparison
    (it is always on the comparison grade sensor)."""
    window = _window_block(results)
    if window is not None:
        return {
            "basis": "window",
            "period_start": window.get("period_start"),
            "period_end": window.get("period_end"),
            "preliminary": window.get("preliminary"),
            "n_days": window.get("n_days"),
        }
    return {"basis": "comparison", **_comparison_grade_attrs(results)}


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
    degrades to "no value" instead of throwing.

    Reversed charge/discharge fields (backend spec 2026-10-12): when the server reports
    battery_direction.state == "reversed" the measured value was measured the wrong way
    round, so this returns None and _battery_value falls back to the modelled decision
    value - the same figure the economics then use.

    Data mode (backend spec 2026-10-16 §8.2, R14): once the grade is computed against the
    data-driven benchmark, the grade card's own `annual` and holistic.measured_period_sek
    follow the benchmark's period, while the economics stay anchored on the legacy window.
    The backend carries those legacy figures in the public block betyg.economy =
    {annual, measured_period_sek, score_on}, and its own anchor
    (warm.measured_battery_annual_from) reads that block first. This reads in the same
    order, with the same gate applied to the block itself - never a mix of the two, and no
    fallback to the top level when the block is present but immature. Without the block
    (legacy mode, older caches) the top-level fields apply as before."""
    betyg = results.get("betyg") or {}
    direction = betyg.get("battery_direction")
    if isinstance(direction, dict) and direction.get("state") == "reversed":
        return None
    economy = betyg.get("economy")
    if isinstance(economy, dict):
        annual, score_on, period = (economy.get("annual"), economy.get("score_on"),
                                    economy.get("measured_period_sek"))
    else:
        holistic = betyg.get("holistic") or {}
        annual, score_on, period = (betyg.get("annual"), holistic.get("score_on"),
                                    holistic.get("measured_period_sek"))
    if not annual or annual.get("factor") is None or score_on is None:
        return None
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


def _external_control_attr(results: dict) -> dict[str, Any]:
    """external_control (spec 2026-08-26): the backend's ADDITIVE block on results.betyg,
    present only when at least one interval in the grade's window was flagged as
    externally controlled by a flex-market service (e.g. Tibber Grid Rewards). Omitted
    entirely (not None) for an unflagged plant and for every payload cached before this
    backend deploy - same absent-vs-false contract as _capacity_hint_attr above, and the
    same lesson as the annual["factor"] KeyError (<= v0.27.0): a truthy-but-keyless block
    check here would kill this sensor for every user on an older backend/cache."""
    ext = (results.get("betyg") or {}).get("external_control")
    if ext is None:
        return {}
    return {"external_control": ext}


def _benchmark_attr(results: dict) -> dict[str, Any]:
    """benchmark (backend spec 2026-10-16 §8.2): the public block on results.betyg,
    {method, comparable, reasons}, saying whether the grade is compared with other plants
    (comparable) or is a grade of the plant's own, and why. Derived in one place by the
    backend worker and handed over verbatim - the sensor never re-derives the class.
    Present only in data mode; omitted entirely (not None) in legacy mode and for every
    payload cached before the flip, the same absent-vs-None contract as
    _external_control_attr."""
    block = (results.get("betyg") or {}).get("benchmark")
    if block is None:
        return {}
    return {"benchmark": block}


def _flex_compensation_attr(results: dict) -> dict[str, Any]:
    """flex_compensation (spec 2026-08-28): the backend's ADDITIVE block on results.betyg
    holding what the flex-market participation paid - measured from the amounts the owner
    entered (or read off the compensation sensor) and/or estimated from the aggregator's
    capacity prices - so it can be shown next to what that participation cost in foregone
    spot value. Present only when at least one of the two amounts exists; omitted entirely
    (not None) for a plant with no flex signal at all and for every payload cached before
    this backend deploy, exactly like _external_control_attr above.

    Handed over VERBATIM, never rebuilt field by field. Two reasons, both load-bearing:
    the amounts have DIFFERENT bases (the estimate spans the whole grade window, the
    measured amount only the months actually entered) so the coverage ratios must travel
    with them for the comparison to mean anything, and a keyed lookup into an optional
    block is the annual["factor"] KeyError (<= v0.27.0) that took the whole sensor down for
    every user on an older backend."""
    flex = (results.get("betyg") or {}).get("flex_compensation")
    if flex is None:
        return {}
    return {"flex_compensation": flex}


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
_STATUS_OPTIONS = [
    "done", "computing", "waiting_for_data", "error",
    "measuring_battery", "needs_battery_input",
]
# Serverns battery_status → statussensorns slug (spec 2026-09-14 §7.2). Bara de två
# väntande lägena mappas; measured/nameplate/none betyder att jobbstatusen är det
# intressanta igen.
_BATTERY_STATUS_FIRST = {"pending": "measuring_battery", "needs_input": "needs_battery_input"}


def _status_value(r: dict) -> str:
    # Batteritillståndet har företräde (spec 2026-09-14 §7.2): utan känd kapacitet finns
    # inget jobb att rapportera status för.
    bs = _BATTERY_STATUS_FIRST.get(r.get(KEY_BATTERY_STATUS) or "")
    return bs or _STATUS_MAP.get(r.get("status"), "waiting_for_data")


SENSOR_DESCRIPTIONS: tuple[WoltaSensorEntityDescription, ...] = (
    WoltaSensorEntityDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=_STATUS_OPTIONS,
        value_fn=_status_value,
        attr_fn=lambda data: {
            "server_status": data.results.get("status"),
            "job": (data.results.get("job") or {}).get("status"),
            "step": (data.results.get("job") or {}).get("step"),
            # Mätningens framsteg (spec 2026-09-14 §7.2): dygn hittills mot serverns
            # minsta krav, så väntan går att visa utan att gissa hur länge den håller på.
            KEY_BATTERY_STATUS: data.battery_status,
            "detect_days": (data.battery_detect or {}).get("n_days"),
            "detect_min_days": data.detect_min_days,
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
                **_grade_basis_attr(data.results),
                # Owner-field attributes (the owner's inputs vs the data, not a grade) stay
                # whichever grade is shown.
                **_applied_tariff_attr(data.results),
                **_applied_reserve_attr(data.results),
                **_capacity_hint_attr(data.results),
                **_measured_params_attr(data.results),
                **_flex_compensation_attr(data.results),
            }
            if _betyg_available(data.results)
            else {"reason": "not enough data for a grade yet"}
        ),
    ),
    WoltaSensorEntityDescription(
        key="jamforelsebetyg",
        translation_key="jamforelsebetyg",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        # Always the comparison grade (holistic.score_on), whether or not the owner has a
        # grade window: this is the grade that is compared with other installations.
        value_fn=_comparison_score,
        available_fn=_comparison_available,
        attr_fn=lambda data: (
            {
                **_comparison_grade_attrs(data.results),
            }
            if _comparison_available(data.results)
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


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """The one Wolta device every sensor of an entry attaches to."""
    link = entry.data.get(CONF_LINK_TOKEN)
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Wolta",
        manufacturer="Wolta",
        entry_type="service",
        configuration_url=link_url(link) if link else f"{WOLTA_API_BASE}/anlaggning",
    )


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
        # Läslänk, aldrig ägar-tokenet (spec 2026-08-24). Saknas den cachade länken
        # (mint har aldrig lyckats) faller vi tillbaka på den tokenlösa sidan.
        self._attr_device_info = _device_info(entry)

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


class WoltaSpotPriceSensor(CoordinatorEntity[WoltaPriceCoordinator], SensorEntity):
    """Current spot price (SEK/kWh, excluding VAT) with the price series as an attribute.

    Information only: backed by WoltaPriceCoordinator and nothing else, so it reads and
    writes nothing the grade, the upload or the profile use, and a failing price endpoint
    only makes these two sensors unavailable. The with_forecast variant adds the forecast
    points (up to three days ahead) to the series; it is disabled by default, and only an
    enabled one makes the coordinator fetch the days beyond tomorrow.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "SEK/kWh"
    _attr_suggested_display_precision = 3
    # The series is hundreds of points; the recorder would store it on every change.
    _unrecorded_attributes = frozenset({"prices"})

    def __init__(
        self,
        coordinator: WoltaPriceCoordinator,
        entry: ConfigEntry,
        *,
        with_forecast: bool,
    ) -> None:
        super().__init__(coordinator)
        self._with_forecast = with_forecast
        key = "spot_price_with_forecast" if with_forecast else "spot_price"
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_entity_registry_enabled_default = not with_forecast
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._with_forecast:
            # Only an enabled entity is ever added to hass (a disabled one is just a
            # registry entry), so being here means the user wants the forecast: ask the
            # coordinator to fetch the days beyond tomorrow too. The spot-only sensor never
            # asks, so it causes no extra request. Enabling or disabling the entity
            # reloads the entry, which re-evaluates this.
            self.async_on_remove(self.coordinator.async_request_full_forecast())
        # The coordinator polls every 15 minutes, unaligned to the quarter. Re-evaluate
        # the state at each quarter boundary so it flips on time without a poll.
        self.async_on_remove(
            async_track_utc_time_change(
                self.hass, self._quarter_boundary, minute=(0, 15, 30, 45), second=1
            )
        )

    @callback
    def _quarter_boundary(self, _now: datetime) -> None:
        self.async_write_ha_state()

    def _current(self) -> float | None:
        data = self.coordinator.data
        return None if data is None else current_price(data, dt_util.utcnow())

    @property
    def available(self) -> bool:
        # data is None until the first price refresh has finished: that refresh runs
        # after platform setup and last_update_success defaults to True before it.
        # _current() is None when no point covers this quarter (stale data) or the
        # covering point has no price (no exchange rate).
        return self.coordinator.last_update_success and self._current() is not None

    @property
    def native_value(self) -> float | None:
        return self._current()

    def _forecast(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        forecast = data.get("forecast") if self._with_forecast else None
        return forecast if isinstance(forecast, dict) else None

    @property
    def attribution(self) -> str | None:
        data = self.coordinator.data or {}
        source = data.get("source")
        forecast = self._forecast()
        forecast_source = forecast.get("source") if forecast else None
        if forecast_source is None and self._with_forecast and forecast_days(data):
            # Tomorrow's prices are out, so /prices/now has no forecast any more, but the
            # days beyond tomorrow are forecast and need the same credit.
            full = data.get(FULL_FORECAST_KEY)
            forecast_source = full.get("source") if isinstance(full, dict) else None
        if source and forecast_source:
            return f"{source} {forecast_source}"
        return source or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        attrs: dict[str, Any] = {
            "prices": price_series(data, with_forecast=self._with_forecast),
            "vat_factor": data.get("vat_factor"),
            "tomorrow_complete": data.get("tomorrow_complete"),
        }
        if self._with_forecast:
            forecast = self._forecast()
            attrs["forecast_issued_at"] = forecast.get("issued_at") if forecast else None
            # The API's accuracy object, passed through unchanged.
            attrs["forecast_accuracy"] = forecast.get("accuracy") if forecast else None
            # Days 2 and 3: issue time and accuracy per day (the points are in `prices`).
            attrs["forecast_days"] = forecast_days(data)
        return attrs


class WoltaPurchasePriceSensor(CoordinatorEntity[WoltaPriceCoordinator], SensorEntity):
    """What the owner pays for electricity right now: SEK/kWh INCLUDING VAT, for the
    current quarter-hour (spot + supplier markup + energy tax after any reduction + variable
    grid fee, VAT on all of it). Subscription, fuse fee, power tariff and fixed-price
    contracts are not in it. The spot price sensors next to it stay EXCLUDING VAT.

    Information only, like the spot sensors: backed by WoltaPriceCoordinator and nothing
    else, so it reads and writes nothing the grade, the upload or the profile use. The
    server works the price out from the tariff the owner has set (GET /profile/prices); the
    integration only reads it. Disabled by default, and only an enabled one makes the
    coordinator call that endpoint. The state switches at each quarter boundary from the
    series already fetched, without a poll.

    States: a price; `unknown` with the `reason` attribute when the server has no price for
    the owner (tariff not set, ...) or the covering quarter has none; `unavailable` only when
    there is no data at all (see `available`).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "purchase_price"
    _attr_native_unit_of_measurement = "SEK/kWh"
    _attr_suggested_display_precision = 3
    _attr_entity_registry_enabled_default = False
    # The series is up to ~190 points; the recorder would store it on every change.
    _unrecorded_attributes = frozenset({"prices"})

    def __init__(self, coordinator: WoltaPriceCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_purchase_price"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Only an enabled entity is ever added to hass (a disabled one is just a registry
        # entry), so being here means the user wants the purchase price: ask the coordinator
        # to fetch it. Enabling or disabling the entity reloads the entry, which
        # re-evaluates this.
        self.async_on_remove(self.coordinator.async_request_purchase_prices())
        self.async_on_remove(
            async_track_utc_time_change(
                self.hass, self._quarter_boundary, minute=(0, 15, 30, 45), second=1
            )
        )

    @callback
    def _quarter_boundary(self, _now: datetime) -> None:
        self.async_write_ha_state()

    def _purchase(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        purchase = data.get(PURCHASE_KEY)
        return purchase if isinstance(purchase, dict) else None

    def _current(self) -> float | None:
        return current_purchase(self._purchase(), dt_util.utcnow())

    @property
    def available(self) -> bool:
        # Unavailable: /prices/now failed (the coordinator fails, like the spot sensors),
        # there is no purchase data (not fetched yet, a server before 0.118.0, or the request
        # failed with no earlier answer to fall back on) or the data is stale (no quarter
        # covers now). A failed poll alone does not make it unavailable: the coordinator keeps
        # the last good response, and this check ends it once that series no longer covers now.
        #
        # When the server ANSWERED that it has no price (available: false, with a `reason`),
        # or the covering quarter has none (that year's energy-tax rule is missing), the
        # entity is available with the state `unknown` instead. Home Assistant drops every
        # attribute of an unavailable entity, and `reason` (what the owner has to do) is the
        # whole point of that answer.
        purchase = self._purchase()
        if not self.coordinator.last_update_success or purchase is None:
            return False
        if purchase.get("available") is not True:
            return True
        return purchase_covers(purchase, dt_util.utcnow())

    @property
    def native_value(self) -> float | None:
        return self._current()

    @property
    def attribution(self) -> str | None:
        # The spot price inside the purchase price is the same market data the spot sensors
        # show; the credit comes from /prices/now.
        source = (self.coordinator.data or {}).get("source")
        return source or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        purchase = self._purchase()
        if purchase is None:
            return {}
        return {
            "prices": purchase_series(purchase),
            "components": purchase.get("components"),
            "reason": purchase.get("reason"),
            "tomorrow_complete": purchase.get("tomorrow_complete"),
        }


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
    # Spot prices: only when the price coordinator exists. Separate from the entities
    # above on purpose - see WoltaSpotPriceSensor.
    if coordinator.prices is not None:
        async_add_entities(
            WoltaSpotPriceSensor(coordinator.prices, entry, with_forecast=flag)
            for flag in (False, True)
        )
        async_add_entities([WoltaPurchasePriceSensor(coordinator.prices, entry)])
