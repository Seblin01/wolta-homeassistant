"""Täckningsavstämningen i coordinatorn (spec 2026-10-10 täckningsavstämning §5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.wolta import coverage
from custom_components.wolta.api import WoltaApiError, WoltaAuthError, WoltaRateLimitError
from custom_components.wolta.const import (
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_GRID_IN,
    CONF_GRID_OUT,
    CONF_SOC,
    CONF_SOLAR,
    CONF_TOKEN,
    CONF_VIEW_ONLY,
    CONF_ZONE,
    DOMAIN,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

TOKEN = "tok-coverage"
FP = coverage.coverage_token(TOKEN)
NOW = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
WINDOW_START = NOW - timedelta(days=365)
ENTRY_DATA = {
    CONF_TOKEN: TOKEN,
    CONF_ZONE: "SE3",
    CONF_BATT_IN: ["sensor.batt_in"],
    CONF_BATT_OUT: ["sensor.batt_out"],
    CONF_GRID_IN: ["sensor.grid_in"],
    CONF_GRID_OUT: ["sensor.grid_out"],
    CONF_SOLAR: ["sensor.solar"],
}
RESULTS = {"status": "done", "currency": "SEK",
           "period": {"start": "2026-08-04", "end": "2026-10-10", "n_days": 67},
           "job": {"status": "done", "step": None}, "betyg": None,
           "decision": None, "history": None}
PROFILE = {"zone": "SE3", "battery_kwh": None, "battery_kw": None, "eff": None,
           "reserve_pct": None, "cost_sek": None, "purchase_date": None,
           "grid_var_ore": None, "surcharge_ore": None, "export_extra_ore": None}


def _rows(start: datetime, n: int) -> list[dict]:
    return [{"ts": (start + timedelta(minutes=15 * i)).isoformat(),
             "batt_charged_kwh": 0.1, "batt_discharged_kwh": 0.0, "solar_kwh": 0.0,
             "grid_import_kwh": 0.2, "grid_export_kwh": 0.0} for i in range(n)]


def _since(dt: datetime | None) -> dict:
    return {"data_since": dt.isoformat() if dt is not None else None}


def _client(*, put=None, profile=None) -> MagicMock:
    """put: lista av svar/undantag för put_data i anropsordning. profile: get_profile-svar
    (None = omockad, som i test_coordinator.py – GET:en failar då tyst)."""
    client = MagicMock()
    client.put_data = AsyncMock(side_effect=list(put or []))
    client.results = AsyncMock(return_value=RESULTS)
    client.recompute = AsyncMock()
    if profile is not None:
        client.get_profile = AsyncMock(return_value=profile)
    return client


@pytest.fixture
def entry(hass: HomeAssistant):
    e = MagicMock(spec=ConfigEntry)
    e.entry_id = "coverage_entry"
    e.domain = DOMAIN
    e.data = dict(ENTRY_DATA)
    e.state = ConfigEntryState.SETUP_IN_PROGRESS
    e.unique_id = "coverage_unique"
    return e


async def _coordinator(hass, entry, client, state):
    from custom_components.wolta.coordinator import WoltaCoordinator

    c = WoltaCoordinator(hass, entry)
    c.client = client
    c._state = dict(state)
    c._store = MagicMock()
    c._store.async_save = AsyncMock()
    c._store.async_load = AsyncMock(return_value=dict(state))
    return c


def _state(bookmark: datetime | None = None, **extra) -> dict:
    """last_recompute = resultatets periodslut: utan tvångsflaggan räknar
    _maybe_recompute då INTE om (0 < 7 dygn) - ett recompute-anrop bevisar flaggan."""
    s = {"applied_invert": False, "applied_entities": None, "last_recompute": "2026-10-10"}
    if bookmark is not None:
        s["last_uploaded_ts"] = bookmark.isoformat()
    s.update(extra)
    return s


@pytest.mark.asyncio
async def test_put_rows_returns_the_response(hass, entry):
    client = _client(put=[{"data_since": "2026-08-04T00:00:00+00:00", "upserted": 2}])
    c = await _coordinator(hass, entry, client, _state())
    delivered, response = await c._put_rows(_rows(NOW, 2), [])
    assert delivered is True
    assert response == {"data_since": "2026-08-04T00:00:00+00:00", "upserted": 2}


@pytest.mark.asyncio
async def test_put_rows_422_fallback_returns_the_flow_only_response(hass, entry):
    entry.data = {**ENTRY_DATA, CONF_SOC: ["sensor.soc"]}
    client = _client(put=[WoltaApiError("bad soc", status=422), {"data_since": None}])
    c = await _coordinator(hass, entry, client, _state())
    soc = [{"unit": "sensor.soc", "ts": NOW.isoformat(), "period_s": 900,
            "soc_mean": 50.0, "soc_min": 49.0, "soc_max": 51.0}]
    delivered, response = await c._put_rows(_rows(NOW, 1), soc)
    assert delivered is False
    assert response == {"data_since": None}


def _stats_capture():
    """async_fetch_change-ersättare som loggar (period, start, end)."""
    calls = []

    async def fetch(hass, ids, start, end, period):
        calls.append((period, start, end))
        return {}
    return calls, fetch


@pytest.mark.asyncio
async def test_gap_rows_old_gap_reads_hourly_only(hass, entry):
    calls, fetch = _stats_capture()
    gap = coverage.Gap(WINDOW_START, NOW - timedelta(days=60))
    c = await _coordinator(hass, entry, _client(), _state())
    with patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch):
        await c._gap_rows(gap, NOW)
    assert calls == [("hour", gap.start, gap.end)]


@pytest.mark.asyncio
async def test_gap_rows_spanning_short_term_reads_both_resolutions(hass, entry):
    calls, fetch = _stats_capture()
    gap = coverage.Gap(WINDOW_START, NOW - timedelta(hours=2))
    short_start = NOW - timedelta(days=9)
    c = await _coordinator(hass, entry, _client(), _state())
    with patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch):
        await c._gap_rows(gap, NOW)
    assert calls == [("hour", gap.start, short_start), ("5minute", short_start, gap.end)]


@pytest.mark.asyncio
async def test_gap_rows_recent_gap_reads_5min_only(hass, entry):
    calls, fetch = _stats_capture()
    gap = coverage.Gap(NOW - timedelta(days=2), NOW - timedelta(hours=2))
    c = await _coordinator(hass, entry, _client(), _state())
    with patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch):
        await c._gap_rows(gap, NOW)
    assert calls == [("5minute", gap.start, gap.end)]


@pytest.mark.asyncio
async def test_gap_rows_filters_to_before_end_and_never_judges_emptiness(hass, entry):
    """Luckan slutar mitt i en timme: timstatistik ÷ 4 ger kvartar efter slutet – de får
    inte skickas (servern har dem redan). En tom lucka är normalt vid uppgradering och får
    varken räknas som sensorfel eller nollställa en äkta felsvit."""
    end = NOW - timedelta(days=60) + timedelta(minutes=30)
    merged = _rows(end - timedelta(minutes=30), 4)          # :00 :15 :30 :45
    c = await _coordinator(hass, entry, _client(), _state(energy_empty_cycles=2))
    c._note_stream_emptiness = AsyncMock()
    with (
        patch("custom_components.wolta.coordinator.async_fetch_change",
              side_effect=_stats_capture()[1]),
        patch("custom_components.wolta.coordinator.merge_streams", return_value=merged),
    ):
        rows = await c._gap_rows(coverage.Gap(WINDOW_START, end), NOW)
    assert rows == merged[:2]
    c._note_stream_emptiness.assert_not_called()
    assert c._state["energy_empty_cycles"] == 2


async def _cycle(hass, entry, client, state, *, regular=(), gap=(), fast_poll=False):
    """En _async_update_data-cykel med kontrollerade rader. Den vanliga vägen (backfill/
    heal/incremental) och luckläsningen mockas – _gap_rows egen logik testas ovan."""
    c = await _coordinator(hass, entry, client, state)
    for name in ("_backfill_rows", "_heal_rows", "_incremental_rows"):
        setattr(c, name, AsyncMock(return_value=list(regular)))
    c._gap_rows = AsyncMock(return_value=list(gap))
    if fast_poll:
        from custom_components.wolta.coordinator import _FAST_POLL
        c.update_interval = _FAST_POLL
    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW):
        await c._async_update_data()
    return c


@pytest.mark.asyncio
async def test_recreated_profile_gets_history_before_server_since(hass, entry):
    """Återskapad profil: servern har bara dagens data, HA har sedan
    augusti. En egen PUT med luckans rader; bokmärket orört; minnet ur svaret; omräkning."""
    bookmark = NOW - timedelta(hours=2)
    regular = _rows(bookmark, 4)
    gap = _rows(NOW - timedelta(days=67), 8)
    client = _client(put=[_since(bookmark), _since(NOW - timedelta(days=67))])
    c = await _cycle(hass, entry, client, _state(bookmark), regular=regular, gap=gap)
    assert client.put_data.await_count == 2
    assert client.put_data.await_args_list[1].args == (TOKEN, gap)
    c._gap_rows.assert_awaited_once_with(coverage.Gap(WINDOW_START, bookmark), NOW)
    assert c._state["last_uploaded_ts"] == regular[-1]["ts"]
    assert c._state["coverage"] == coverage.memo(FP, NOW - timedelta(days=67))
    client.recompute.assert_awaited_once()


@pytest.mark.asyncio
async def test_reused_row_sends_nothing(hass, entry):
    """Tokenrotation: servern återanvände raden och har data sedan länge – ingen läsning,
    ingen extra PUT, minnet satt."""
    bookmark = NOW - timedelta(hours=2)
    old = NOW - timedelta(days=400)
    client = _client(put=[_since(old)])
    c = await _cycle(hass, entry, client, _state(bookmark), regular=_rows(bookmark, 4))
    assert client.put_data.await_count == 1
    c._gap_rows.assert_not_awaited()
    assert c._state["coverage"] == coverage.memo(FP, old)
    client.recompute.assert_not_awaited()


@pytest.mark.asyncio
async def test_healthy_upgrade_with_empty_gap_sends_nothing(hass, entry):
    """HA-historiken är kortare än ett år: luckan finns men HA har inget i den."""
    bookmark = NOW - timedelta(hours=2)
    first = NOW - timedelta(days=100)
    client = _client(put=[_since(first)])
    c = await _cycle(hass, entry, client, _state(bookmark), regular=_rows(bookmark, 4), gap=[])
    c._gap_rows.assert_awaited_once()
    assert client.put_data.await_count == 1
    assert c._state["coverage"] == coverage.memo(FP, first)
    client.recompute.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_fill_loop_when_server_drops_the_first_hour(hass, entry):
    """Spikregeln tar bort luckans första timme: svaret säger data_since en timme senare än
    första skickade rad. Minnet tar SVARETS värde, så nästa cykel fyller inte igen."""
    bookmark = NOW - timedelta(hours=2)
    gap_first = NOW - timedelta(days=67)
    after_rule = gap_first + timedelta(hours=1)
    client = _client(put=[_since(bookmark), _since(after_rule), _since(after_rule)])
    c = await _cycle(hass, entry, client, _state(bookmark), regular=_rows(bookmark, 4),
                     gap=_rows(gap_first, 8))
    assert c._state["coverage"] == coverage.memo(FP, after_rule)
    state_after = dict(c._state)
    c2 = await _cycle(hass, entry, client, state_after, regular=_rows(bookmark, 4),
                      gap=_rows(gap_first, 8))
    c2._gap_rows.assert_not_awaited()
    assert client.put_data.await_count == 3          # 2 i cykel 1, 1 (vanlig) i cykel 2


@pytest.mark.asyncio
async def test_empty_server_without_new_rows_fills_the_whole_window(hass, entry):
    """Ingen vanlig PUT i cykeln (inget nytt i HA): serverns läge kommer från profil-GET:en.
    Tom server → luckan är hela fönstret. Bokmärket orört."""
    bookmark = NOW - timedelta(hours=2)
    gap = _rows(NOW - timedelta(days=30), 4)
    client = _client(put=[_since(NOW - timedelta(days=30))],
                     profile={**PROFILE, "data_since": None})
    c = await _cycle(hass, entry, client, _state(bookmark), regular=[], gap=gap)
    c._gap_rows.assert_awaited_once_with(coverage.Gap(WINDOW_START, NOW), NOW)
    assert client.put_data.await_args_list[0].args == (TOKEN, gap)
    assert c._state["last_uploaded_ts"] == bookmark.isoformat()


@pytest.mark.asyncio
async def test_old_backend_without_field_does_nothing(hass, entry):
    bookmark = NOW - timedelta(hours=2)
    client = _client(put=[{"upserted": 4}], profile=dict(PROFILE))
    c = await _cycle(hass, entry, client, _state(bookmark), regular=_rows(bookmark, 4))
    c._gap_rows.assert_not_awaited()
    assert "coverage" not in c._state


@pytest.mark.asyncio
async def test_view_only_does_nothing(hass, entry):
    entry.data = {**ENTRY_DATA, CONF_VIEW_ONLY: True}
    client = _client(profile={**PROFILE, "data_since": None})
    c = await _cycle(hass, entry, client, _state(), gap=_rows(NOW - timedelta(days=30), 4))
    c._gap_rows.assert_not_awaited()
    client.put_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_fast_poll_does_nothing(hass, entry):
    bookmark = NOW - timedelta(hours=2)
    client = _client(put=[_since(bookmark)])
    c = await _cycle(hass, entry, client, _state(bookmark), regular=_rows(bookmark, 4),
                     gap=_rows(NOW - timedelta(days=30), 4), fast_poll=True)
    c._gap_rows.assert_not_awaited()
    assert "coverage" not in c._state


@pytest.mark.asyncio
async def test_413_on_fill_raises_repair_and_remembers(hass, entry):
    bookmark = NOW - timedelta(hours=2)
    client = _client(put=[_since(bookmark), WoltaApiError("full", status=413)])
    with patch("custom_components.wolta.coordinator.ir.async_create_issue") as issue:
        c = await _cycle(hass, entry, client, _state(bookmark), regular=_rows(bookmark, 4),
                         gap=_rows(NOW - timedelta(days=30), 4))
    assert any(call.args[2] == "profile_full" for call in issue.call_args_list)
    assert c._state["coverage"] == coverage.memo(FP, bookmark)


@pytest.mark.asyncio
async def test_429_on_fill_keeps_memo_and_cycle_succeeds(hass, entry):
    bookmark = NOW - timedelta(hours=2)
    client = _client(put=[_since(bookmark), WoltaRateLimitError(retry_after=60)])
    c = await _cycle(hass, entry, client, _state(bookmark), regular=_rows(bookmark, 4),
                     gap=_rows(NOW - timedelta(days=30), 4))
    assert "coverage" not in c._state
    assert c._state["last_uploaded_ts"] == _rows(bookmark, 4)[-1]["ts"]


@pytest.mark.asyncio
async def test_auth_error_on_fill_goes_to_reauth(hass, entry):
    bookmark = NOW - timedelta(hours=2)
    client = _client(put=[_since(bookmark), WoltaAuthError("gone", status=404)])
    with pytest.raises(ConfigEntryAuthFailed):
        await _cycle(hass, entry, client, _state(bookmark), regular=_rows(bookmark, 4),
                     gap=_rows(NOW - timedelta(days=30), 4))


@pytest.mark.asyncio
async def test_fill_soc_only_within_gap(hass, entry):
    """SoC följer med för luckans intervall; en timrad som börjar före slutet men sträcker
    sig förbi det skickas inte."""
    entry.data = {**ENTRY_DATA, CONF_SOC: ["sensor.soc"]}
    bookmark = NOW - timedelta(hours=2)
    end = bookmark                                     # serverns första rad
    inside = {"unit": "sensor.soc", "ts": (end - timedelta(minutes=15)).isoformat(),
              "period_s": 900, "soc_mean": 50.0, "soc_min": 49.0, "soc_max": 51.0}
    straddle = {"unit": "sensor.soc", "ts": (end - timedelta(minutes=30)).isoformat(),
                "period_s": 3600, "soc_mean": 50.0, "soc_min": 49.0, "soc_max": 51.0}
    client = _client(put=[_since(end), _since(NOW - timedelta(days=30))])
    c = await _coordinator(hass, entry, client, _state(bookmark, applied_soc=["sensor.soc"]))
    for name in ("_backfill_rows", "_heal_rows", "_incremental_rows"):
        setattr(c, name, AsyncMock(return_value=_rows(bookmark, 4)))
    c._gap_rows = AsyncMock(return_value=_rows(NOW - timedelta(days=30), 4))
    c._soc_state = AsyncMock(side_effect=[([], True), ([inside, straddle], True)])
    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW):
        await c._async_update_data()
    gap_call = client.put_data.await_args_list[1]
    assert gap_call.kwargs["battery_state"] == [inside]
    soc_gap_args = c._soc_state.await_args_list[1]
    assert soc_gap_args.args == (WINDOW_START, end)


@pytest.mark.asyncio
async def test_new_install_full_backfill_sets_memo_without_extra_read(hass, entry):
    first = NOW - timedelta(days=200)
    client = _client(put=[_since(first)])
    c = await _cycle(hass, entry, client, _state(), regular=_rows(first, 8))
    c._backfill_rows.assert_awaited_once()
    c._gap_rows.assert_not_awaited()
    assert c._state["coverage"] == coverage.memo(FP, first)


@pytest.mark.asyncio
async def test_partial_fill_goes_newest_first_and_next_cycle_completes(hass, entry):
    """Ett fel mitt i fyllningen får inte lämna ett inre hål: skivorna går nyast först, så
    serverns data_since flyttar sig bakåt i takt med att lagrade rader växer och nästa
    cykels lucka är exakt resten."""
    bookmark = NOW - timedelta(hours=2)
    gap_rows = _rows(NOW - timedelta(days=67), 8)
    mid = datetime.fromisoformat(gap_rows[4]["ts"])
    first = datetime.fromisoformat(gap_rows[0]["ts"])
    client = _client(put=[_since(bookmark), _since(mid), WoltaRateLimitError(retry_after=60)])
    with patch("custom_components.wolta.coordinator.MAX_ROWS_PER_PUT", 4):
        c = await _cycle(hass, entry, client, _state(bookmark), regular=_rows(bookmark, 4),
                         gap=gap_rows)
    assert client.put_data.await_args_list[1].args == (TOKEN, gap_rows[4:])
    assert client.put_data.await_args_list[2].args == (TOKEN, gap_rows[:4])
    assert "coverage" not in c._state
    client.recompute.assert_awaited_once()

    # Nästa cykel: den vanliga PUT:ens svar säger att servern nu börjar vid `mid`.
    client2 = _client(put=[_since(mid), _since(first)])
    with patch("custom_components.wolta.coordinator.MAX_ROWS_PER_PUT", 4):
        c2 = await _cycle(hass, entry, client2, dict(c._state), regular=_rows(bookmark, 4),
                          gap=gap_rows[:4])
    c2._gap_rows.assert_awaited_once_with(coverage.Gap(WINDOW_START, mid), NOW)
    assert client2.put_data.await_args_list[1].args == (TOKEN, gap_rows[:4])
    assert c2._state["coverage"] == coverage.memo(FP, first)


@pytest.mark.asyncio
async def test_error_in_gap_read_does_not_fail_the_cycle(hass, entry):
    bookmark = NOW - timedelta(hours=2)
    regular = _rows(bookmark, 4)
    client = _client(put=[_since(bookmark)])
    c = await _coordinator(hass, entry, client, _state(bookmark))
    for name in ("_backfill_rows", "_heal_rows", "_incremental_rows"):
        setattr(c, name, AsyncMock(return_value=list(regular)))
    c._gap_rows = AsyncMock(side_effect=RuntimeError("stats down"))
    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=NOW):
        await c._async_update_data()
    assert c._state["last_uploaded_ts"] == regular[-1]["ts"]
    assert "coverage" not in c._state


@pytest.mark.asyncio
async def test_memo_for_another_token_triggers_fill(hass, entry):
    """Reauth: minnet hör till ett annat token och servern börjar efter fönstrets start."""
    bookmark = NOW - timedelta(hours=2)
    gap = _rows(NOW - timedelta(days=30), 4)
    client = _client(put=[_since(bookmark), _since(NOW - timedelta(days=30))])
    state = _state(bookmark, coverage=coverage.memo("annat-token", NOW - timedelta(days=364)))
    c = await _cycle(hass, entry, client, state, regular=_rows(bookmark, 4), gap=gap)
    c._gap_rows.assert_awaited_once_with(coverage.Gap(WINDOW_START, bookmark), NOW)
    assert client.put_data.await_args_list[1].args == (TOKEN, gap)
    assert c._state["coverage"] == coverage.memo(FP, NOW - timedelta(days=30))


@pytest.mark.asyncio
async def test_no_regular_put_and_old_backend_profile_does_nothing(hass, entry):
    bookmark = NOW - timedelta(hours=2)
    client = _client(profile=dict(PROFILE))
    c = await _cycle(hass, entry, client, _state(bookmark), regular=[],
                     gap=_rows(NOW - timedelta(days=30), 4))
    c._gap_rows.assert_not_awaited()
    client.put_data.assert_not_awaited()
    assert "coverage" not in c._state
