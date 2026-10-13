"""Tests for the replacement job runner in coordinator.py (server spec 2026-10-13 §5, §9).

The job types themselves (direction swap, sensor change) plug in through
_ReplacementType; these tests register a minimal "test" type that replaces one fixed
window and, when done, applies the job's target to the applied invert intervals."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.wolta.api import (
    WoltaApiError,
    WoltaRateLimitError,
    WoltaReplacementConflict,
    WoltaReplacementGone,
)
from custom_components.wolta.coordinator import _ReplacementType

from tests.test_coordinator import (  # noqa: F401 - mock_entry is a fixture
    BASE_PROFILE,
    NOW,
    _make_coordinator,
    _make_rows,
    _mock_client,
    mock_entry,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

WS = "2025-05-01T00:00:00+00:00"
WE = "2025-05-02T00:00:00+00:00"
OLD_ID = "00000000-0000-4000-8000-000000000001"
TARGET = [[None, None]]
SWAP = {
    "kind": "swap_battery",
    "window_start": WS,
    "window_end": WE,
    "fields": None,
    "reason": "direction",
}
ROWS_SPEC = {
    "kind": "rows",
    "window_start": WS,
    "window_end": WE,
    "fields": ["solar_kwh"],
    "reason": "entities_changed",
}
REPLACEMENT_ROWS = [
    {"ts": f"2025-05-01T0{h}:00:00+00:00", "solar_kwh": 0.5} for h in range(3)
]
COMMIT_RESULT = {"updated_rows": 96, "data_since": "2025-01-01T00:00:00+00:00"}


def _client(events: list | None = None):
    """_mock_client with the five replacement calls, each logging to `events`."""
    client = _mock_client()
    client.get_profile = AsyncMock(return_value=dict(BASE_PROFILE))
    log = events if events is not None else []

    def _logged(name):
        async def call(token, rid, *args, **kwargs):
            log.append((name, rid))
        return call

    client.open_replacement = AsyncMock(side_effect=_logged("open"), return_value={})
    client.stage_replacement_rows = AsyncMock(side_effect=_logged("stage"))
    client.commit_replacement = AsyncMock(return_value=dict(COMMIT_RESULT))
    client.get_replacement = AsyncMock(return_value={"status": "open"})
    client.delete_replacement = AsyncMock()
    return client


def _job(spec: dict, replacement_id: str | None = None) -> dict:
    return {
        "type": "test",
        "target": TARGET,
        "replacement": None if replacement_id is None else {**spec, "id": replacement_id},
        "done_windows": [],
        "retry_at": None,
    }


def _register(coordinator, spec: dict, rows: list[dict] | None = None) -> list:
    """The "test" type: one window, `spec`; done once that window is committed."""
    committed: list = []

    async def next_replacement(job, now, profile):
        if [spec["window_start"], spec["window_end"]] in job["done_windows"]:
            return "done"
        return dict(spec)

    async def rows_for(job, rep, now):
        return rows

    async def on_committed(job, rep, result):
        committed.append(result)

    async def on_finished(job):
        coordinator._state["applied_invert_intervals"] = job["target"]

    coordinator._replacement_types["test"] = _ReplacementType(
        next_replacement=next_replacement,
        on_committed=on_committed,
        on_finished=on_finished,
        rows_for=rows_for,
    )
    return committed


async def _coordinator(hass, mock_entry, client, job, spec, rows=None, events=None):
    coordinator = await _make_coordinator(
        hass, mock_entry, client,
        store_state={
            "last_uploaded_ts": (NOW - timedelta(hours=1)).isoformat(),
            "applied_invert": False,
            "applied_invert_intervals": [],
            "replacement_job": job,
        },
    )
    log = events if events is not None else []

    async def save(state):
        rep = (state.get("replacement_job") or {}).get("replacement")
        log.append(("save", rep["id"] if rep else None))

    coordinator._store.async_save = AsyncMock(side_effect=save)
    coordinator._incremental_rows = AsyncMock(return_value=_make_rows(NOW))
    coordinator._reconcile_coverage = AsyncMock()
    committed = _register(coordinator, spec, rows)
    return coordinator, committed


async def _cycle(coordinator, now=NOW):
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=now),
        patch("custom_components.wolta.coordinator.async_fetch_change", return_value={}),
    ):
        return await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# Resuming a replacement left in Store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_committed_applies_target_and_uploads_the_same_cycle(
    hass: HomeAssistant, mock_entry
):
    """GET says committed (the commit went through before a restart): nothing is opened
    or committed again, the target is applied, the job is gone and the regular upload
    runs in the same cycle."""
    client = _client()
    client.get_replacement.return_value = {"status": "committed", "result": COMMIT_RESULT}
    coordinator, committed = await _coordinator(
        hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)

    await _cycle(coordinator)

    client.get_replacement.assert_awaited_once()
    assert client.get_replacement.await_args.args[1] == OLD_ID
    client.open_replacement.assert_not_called()
    client.commit_replacement.assert_not_called()
    assert committed == [COMMIT_RESULT]
    assert coordinator._state["applied_invert_intervals"] == TARGET
    assert "replacement_job" not in coordinator._state
    client.put_data.assert_awaited()
    coordinator._reconcile_coverage.assert_awaited()


@pytest.mark.asyncio
async def test_resume_open_commits_the_same_id(hass: HomeAssistant, mock_entry):
    """GET says open: the same id is committed (no new open), and the job finishes."""
    client = _client()
    coordinator, committed = await _coordinator(
        hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)

    await _cycle(coordinator)

    client.open_replacement.assert_not_called()
    client.commit_replacement.assert_awaited_once_with(coordinator.token, OLD_ID, None)
    assert committed == [COMMIT_RESULT]
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_invert_intervals"] == TARGET
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_resume_open_rows_are_sent_again_before_the_commit(
    hass: HomeAssistant, mock_entry
):
    """A "rows" replacement found open is staged again (the upsert makes that safe) and
    committed with the number of distinct timestamps."""
    client = _client()
    coordinator, _ = await _coordinator(
        hass, mock_entry, client, _job(ROWS_SPEC, OLD_ID), ROWS_SPEC, rows=REPLACEMENT_ROWS)

    await _cycle(coordinator)

    client.stage_replacement_rows.assert_awaited_once_with(
        coordinator.token, OLD_ID, REPLACEMENT_ROWS)
    client.commit_replacement.assert_awaited_once_with(coordinator.token, OLD_ID, 3)
    assert "replacement_job" not in coordinator._state


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["aborted", "expired", "restored"])
async def test_resume_closed_status_starts_over_with_a_new_id(
    hass: HomeAssistant, mock_entry, status
):
    client = _client()
    client.get_replacement.return_value = {"status": status}
    coordinator, _ = await _coordinator(
        hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)

    await _cycle(coordinator)

    client.open_replacement.assert_awaited_once()
    new_id = client.open_replacement.await_args.args[1]
    assert new_id != OLD_ID
    client.commit_replacement.assert_awaited_once_with(coordinator.token, new_id, None)


@pytest.mark.asyncio
async def test_resume_gone_saves_the_new_id_before_opening_it(
    hass: HomeAssistant, mock_entry
):
    """GET raises WoltaReplacementGone: a new id is generated and persisted to Store
    BEFORE the open call, so a restart in between resumes that id."""
    events: list = []
    client = _client(events)
    client.get_replacement.side_effect = WoltaReplacementGone(
        "gone", status=404, code="unknown_replacement")
    coordinator, _ = await _coordinator(
        hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP, events=events)

    await _cycle(coordinator)

    client.open_replacement.assert_awaited_once()
    new_id = client.open_replacement.await_args.args[1]
    assert new_id != OLD_ID
    opened_at = events.index(("open", new_id))
    assert events[opened_at - 1] == ("save", new_id)
    assert client.open_replacement.await_args.kwargs == {
        "kind": "swap_battery",
        "window_start": datetime.fromisoformat(WS),
        "window_end": datetime.fromisoformat(WE),
        "fields": None,
        "reason": "direction",
    }


@pytest.mark.asyncio
async def test_every_state_changing_call_follows_a_save(hass: HomeAssistant, mock_entry):
    """Store is saved immediately before open and stage (and commit, which logs no
    event of its own here), with the replacement's id in it."""
    events: list = []
    client = _client(events)

    async def commit(token, rid, expected):
        events.append(("commit", rid))
        return dict(COMMIT_RESULT)

    client.commit_replacement.side_effect = commit
    coordinator, _ = await _coordinator(
        hass, mock_entry, client, _job(ROWS_SPEC), ROWS_SPEC, rows=REPLACEMENT_ROWS,
        events=events)

    await _cycle(coordinator)

    calls = [i for i, (name, _) in enumerate(events) if name in ("open", "stage", "commit")]
    assert [events[i][0] for i in calls] == ["open", "stage", "commit"]
    for i in calls:
        assert events[i - 1] == ("save", events[i][1])


# ---------------------------------------------------------------------------
# Server refusals (K11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_conflict_keeps_the_job_and_does_not_pause(
    hass: HomeAssistant, mock_entry
):
    """409 conflict: the server aborted the replacement. The job keeps its target, its
    replacement is cleared (the next cycle opens a new id) and, with nothing open, the
    regular upload is not paused."""
    client = _client()
    client.commit_replacement.side_effect = WoltaReplacementConflict("conflict")
    coordinator, committed = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    await _cycle(coordinator)

    job = coordinator._state["replacement_job"]
    assert job["target"] == TARGET
    assert job["replacement"] is None
    assert job["done_windows"] == []
    assert committed == []
    assert coordinator._state["applied_invert_intervals"] == []
    client.delete_replacement.assert_not_called()  # the server already aborted it
    client.put_data.assert_awaited()
    coordinator._reconcile_coverage.assert_awaited()


@pytest.mark.asyncio
async def test_count_mismatch_sends_the_rows_again_and_commits_once_more(
    hass: HomeAssistant, mock_entry
):
    client = _client()
    client.commit_replacement.side_effect = [
        WoltaReplacementConflict("count_mismatch"), dict(COMMIT_RESULT)]
    coordinator, committed = await _coordinator(
        hass, mock_entry, client, _job(ROWS_SPEC), ROWS_SPEC, rows=REPLACEMENT_ROWS)

    await _cycle(coordinator)

    rid = client.open_replacement.await_args.args[1]
    assert client.stage_replacement_rows.await_count == 2
    for call in client.stage_replacement_rows.await_args_list:
        assert call.args == (coordinator.token, rid, REPLACEMENT_ROWS)
    assert client.commit_replacement.await_count == 2
    assert committed == [COMMIT_RESULT]
    assert "replacement_job" not in coordinator._state


@pytest.mark.asyncio
async def test_second_count_mismatch_starts_over_with_a_new_id(
    hass: HomeAssistant, mock_entry
):
    client = _client()
    client.commit_replacement.side_effect = WoltaReplacementConflict("count_mismatch")
    coordinator, committed = await _coordinator(
        hass, mock_entry, client, _job(ROWS_SPEC), ROWS_SPEC, rows=REPLACEMENT_ROWS)

    await _cycle(coordinator)

    rid = client.open_replacement.await_args.args[1]
    assert client.commit_replacement.await_count == 2
    client.delete_replacement.assert_awaited_once_with(coordinator.token, rid)
    assert coordinator._state["replacement_job"]["replacement"] is None
    assert committed == []
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_empty_counts_as_done_for_the_window(hass: HomeAssistant, mock_entry):
    """409 empty: nothing to replace in the window - treated as committed for it."""
    client = _client()
    client.commit_replacement.side_effect = WoltaReplacementConflict("empty")
    coordinator, committed = await _coordinator(
        hass, mock_entry, client, _job(ROWS_SPEC), ROWS_SPEC, rows=REPLACEMENT_ROWS)

    await _cycle(coordinator)

    assert committed == [{}]
    client.delete_replacement.assert_awaited_once()
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_invert_intervals"] == TARGET


@pytest.mark.asyncio
async def test_archive_full_waits_until_retry_after_without_pausing(
    hass: HomeAssistant, mock_entry
):
    """429 archive_full on open: nothing is open, so nothing pauses; the job waits until
    Retry-After and opens nothing before then."""
    client = _client()
    client.open_replacement.side_effect = WoltaRateLimitError(86400)
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    await _cycle(coordinator)

    job = coordinator._state["replacement_job"]
    assert job["replacement"] is None
    assert job["retry_at"] == (NOW + timedelta(seconds=86400)).isoformat()
    client.put_data.assert_awaited()
    coordinator._reconcile_coverage.assert_awaited()

    client.open_replacement.reset_mock()
    client.open_replacement.side_effect = None
    client.put_data.reset_mock()
    await _cycle(coordinator, NOW + timedelta(hours=12))
    client.open_replacement.assert_not_called()
    client.put_data.assert_awaited()

    await _cycle(coordinator, NOW + timedelta(days=1, minutes=1))
    client.open_replacement.assert_awaited_once()
    assert "replacement_job" not in coordinator._state


@pytest.mark.asyncio
async def test_stream_bound_refusal_keeps_the_job_without_pausing(
    hass: HomeAssistant, mock_entry
):
    """A 409 with a string detail (plant bound to a stream) is a plain failure: the job
    stays, nothing is open, the upload goes on."""
    client = _client()
    client.commit_replacement.side_effect = WoltaApiError("bound", status=409)
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    await _cycle(coordinator)

    job = coordinator._state["replacement_job"]
    assert job["target"] == TARGET
    assert job["replacement"] is None
    client.put_data.assert_awaited()


# ---------------------------------------------------------------------------
# The pause (K5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_replacement_pauses_put_data_and_the_coverage_fill(
    hass: HomeAssistant, mock_entry
):
    """While a replacement stays open (here: the commit fails transiently), neither the
    regular PUT /data nor the coverage fill runs; results are still fetched."""
    client = _client()
    client.commit_replacement.side_effect = WoltaApiError("down", status=503)
    coordinator, _ = await _coordinator(
        hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)
    bookmark = coordinator._state["last_uploaded_ts"]

    data = await _cycle(coordinator)

    assert coordinator._state["replacement_job"]["replacement"]["id"] == OLD_ID
    client.put_data.assert_not_called()
    coordinator._incremental_rows.assert_not_called()
    coordinator._reconcile_coverage.assert_not_called()
    assert coordinator._state["last_uploaded_ts"] == bookmark
    client.results.assert_awaited()
    assert data.n_days == 150


@pytest.mark.asyncio
async def test_rate_limited_commit_keeps_the_replacement_and_stays_paused(
    hass: HomeAssistant, mock_entry
):
    client = _client()
    client.commit_replacement.side_effect = WoltaRateLimitError(600)
    coordinator, _ = await _coordinator(
        hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)

    await _cycle(coordinator)
    job = coordinator._state["replacement_job"]
    assert job["replacement"]["id"] == OLD_ID
    assert job["retry_at"] == (NOW + timedelta(seconds=600)).isoformat()
    client.put_data.assert_not_called()

    client.get_replacement.reset_mock()
    await _cycle(coordinator, NOW + timedelta(minutes=5))
    client.get_replacement.assert_not_called()
    client.put_data.assert_not_called()


@pytest.mark.asyncio
async def test_waiting_job_pauses_nothing(hass: HomeAssistant, mock_entry):
    """next_replacement returning None (e.g. the new sensors have no statistics yet):
    nothing is opened and the upload runs."""
    client = _client()
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    async def wait(job, now, profile):
        return None

    coordinator._replacement_types["test"] = _ReplacementType(
        next_replacement=wait,
        on_committed=AsyncMock(),
        on_finished=AsyncMock(),
    )
    await _cycle(coordinator)

    client.open_replacement.assert_not_called()
    assert coordinator._state["replacement_job"]["replacement"] is None
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_unknown_job_type_is_dropped(hass: HomeAssistant, mock_entry):
    client = _client()
    job = {**_job(SWAP, OLD_ID), "type": "from-a-newer-version"}
    coordinator, _ = await _coordinator(hass, mock_entry, client, job, SWAP)

    await _cycle(coordinator)

    assert "replacement_job" not in coordinator._state
    client.get_replacement.assert_not_called()
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_store_holds_only_json(hass: HomeAssistant, mock_entry):
    """The job round-trips through JSON (HA's Store serialises it)."""
    client = _client()
    client.commit_replacement.side_effect = WoltaReplacementConflict("conflict")
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    await _cycle(coordinator)

    job = coordinator._state["replacement_job"]
    assert json.loads(json.dumps(job)) == job


# ---------------------------------------------------------------------------
# Bounded failures, hook guards, fast-poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_5xx_gives_up_after_three_cycles(hass: HomeAssistant, mock_entry):
    """A commit that keeps failing with 503 pauses the upload for three cycles; on the
    fourth the replacement is given up (it is still open: deleted), the job waits a day
    and the upload resumes."""
    client = _client()
    client.commit_replacement.side_effect = WoltaApiError("down", status=503)
    coordinator, committed = await _coordinator(
        hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)

    for n in range(3):
        await _cycle(coordinator, NOW + timedelta(hours=n))
        client.put_data.assert_not_called()
        assert coordinator._state["replacement_job"]["failures"] == n + 1

    fourth = NOW + timedelta(hours=3)
    await _cycle(coordinator, fourth)

    job = coordinator._state["replacement_job"]
    assert job["replacement"] is None
    assert "failures" not in job
    assert job["retry_at"] == (fourth + timedelta(days=1)).isoformat()
    client.delete_replacement.assert_awaited_once_with(coordinator.token, OLD_ID)
    assert committed == []
    client.put_data.assert_awaited()
    coordinator._reconcile_coverage.assert_awaited()


@pytest.mark.asyncio
async def test_give_up_books_a_replacement_that_was_committed_after_all(
    hass: HomeAssistant, mock_entry
):
    """A 502/504 can follow a commit that went through: the give-up GET finds it
    committed, so the window is booked, nothing is deleted or opened again."""
    client = _client()
    client.commit_replacement.side_effect = WoltaApiError("gateway", status=504)
    client.get_replacement.side_effect = [{"status": "open"}] * 4 + [
        {"status": "committed", "result": COMMIT_RESULT}]
    coordinator, committed = await _coordinator(
        hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)

    for n in range(4):
        await _cycle(coordinator, NOW + timedelta(hours=n))

    job = coordinator._state["replacement_job"]
    assert committed == [COMMIT_RESULT]
    assert job["done_windows"] == [[WS, WE]]
    assert job["replacement"] is None
    # A booked window is progress: the job is not held back a day.
    assert job["retry_at"] is None
    client.delete_replacement.assert_not_called()
    client.open_replacement.assert_not_called()
    client.put_data.assert_awaited_once()

    await _cycle(coordinator, NOW + timedelta(hours=4))
    assert "replacement_job" not in coordinator._state


@pytest.mark.asyncio
async def test_give_up_without_a_status_abandons_without_pausing(
    hass: HomeAssistant, mock_entry
):
    """When even the give-up GET fails, the replacement is kept but marked abandoned:
    it pauses nothing and is never committed; once a GET can tell, an open one is
    deleted and the job starts over with a new id."""
    client = _client()
    client.commit_replacement.side_effect = WoltaApiError("down", status=503)
    client.get_replacement.side_effect = [{"status": "open"}] * 4 + [
        WoltaApiError("down", status=503)]
    coordinator, _ = await _coordinator(
        hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)

    for n in range(4):
        await _cycle(coordinator, NOW + timedelta(hours=n))

    job = coordinator._state["replacement_job"]
    assert job["replacement"]["id"] == OLD_ID
    assert job["replacement"]["abandoned"] is True
    client.put_data.assert_awaited_once()

    client.get_replacement.side_effect = None
    client.get_replacement.return_value = {"status": "open"}
    client.commit_replacement.side_effect = None
    client.commit_replacement.return_value = dict(COMMIT_RESULT)
    client.delete_replacement.reset_mock()
    client.put_data.reset_mock()
    await _cycle(coordinator, NOW + timedelta(days=2))

    client.delete_replacement.assert_awaited_with(coordinator.token, OLD_ID)
    new_id = client.open_replacement.await_args.args[1]
    assert new_id != OLD_ID
    client.commit_replacement.assert_awaited_with(coordinator.token, new_id, None)
    assert "replacement_job" not in coordinator._state


@pytest.mark.asyncio
async def test_spec_for_a_committed_window_is_not_opened(hass: HomeAssistant, mock_entry):
    """A hook that asks for a window it already committed is a hook error: nothing is
    opened (for a swap that would undo the window) and nothing pauses."""
    client = _client()
    job = {**_job(SWAP), "done_windows": [[WS, WE]]}
    coordinator, _ = await _coordinator(hass, mock_entry, client, job, SWAP)

    async def again(job, now, profile):
        return dict(SWAP)

    coordinator._replacement_types["test"] = _ReplacementType(
        next_replacement=again, on_committed=AsyncMock(), on_finished=AsyncMock())
    await _cycle(coordinator)

    client.open_replacement.assert_not_called()
    assert coordinator._state["replacement_job"]["replacement"] is None
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_failing_on_committed_still_books_and_clears(hass: HomeAssistant, mock_entry):
    """GET says committed and the type's on_committed raises: the window is still booked
    and the replacement cleared, so the upload is never held by a hook."""
    client = _client()
    client.get_replacement.return_value = {"status": "committed", "result": COMMIT_RESULT}
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)
    hooks = coordinator._replacement_types["test"]

    async def broken(job, rep, result):
        raise RuntimeError("hook bug")

    coordinator._replacement_types["test"] = _ReplacementType(
        next_replacement=hooks.next_replacement,
        on_committed=broken,
        on_finished=hooks.on_finished,
    )
    await _cycle(coordinator)

    assert "replacement_job" not in coordinator._state  # booked, then "done"
    assert coordinator._state["applied_invert_intervals"] == TARGET
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_fast_poll_never_starts_a_replacement(hass: HomeAssistant, mock_entry):
    """Fast-poll skips the profile GET (profile None): no new replacement is started and
    neither next_replacement nor rows_for is asked."""
    from custom_components.wolta.coordinator import _FAST_POLL

    client = _client()
    coordinator, _ = await _coordinator(
        hass, mock_entry, client, _job(ROWS_SPEC), ROWS_SPEC, rows=REPLACEMENT_ROWS)
    next_spy = AsyncMock(return_value=dict(ROWS_SPEC))
    rows_spy = AsyncMock(return_value=REPLACEMENT_ROWS)
    coordinator._replacement_types["test"] = _ReplacementType(
        next_replacement=next_spy, on_committed=AsyncMock(), on_finished=AsyncMock(),
        rows_for=rows_spy)
    coordinator.update_interval = _FAST_POLL

    await _cycle(coordinator)

    client.get_profile.assert_not_called()
    next_spy.assert_not_called()
    rows_spy.assert_not_called()
    client.open_replacement.assert_not_called()
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_fast_poll_resumes_an_open_replacement(hass: HomeAssistant, mock_entry):
    from custom_components.wolta.coordinator import _FAST_POLL

    client = _client()
    coordinator, committed = await _coordinator(
        hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)
    coordinator.update_interval = _FAST_POLL

    await _cycle(coordinator)

    client.commit_replacement.assert_awaited_once_with(coordinator.token, OLD_ID, None)
    assert committed == [COMMIT_RESULT]
    job = coordinator._state["replacement_job"]  # finishing waits for a full cycle
    assert job["replacement"] is None
    assert job["done_windows"] == [[WS, WE]]


@pytest.mark.asyncio
async def test_unreadable_retry_at_counts_as_expired(hass: HomeAssistant, mock_entry):
    client = _client()
    job = {**_job(SWAP), "retry_at": "not a time"}
    coordinator, _ = await _coordinator(hass, mock_entry, client, job, SWAP)

    await _cycle(coordinator)

    client.open_replacement.assert_awaited_once()
    assert "replacement_job" not in coordinator._state


@pytest.mark.asyncio
async def test_stream_bound_refusal_on_open_keeps_the_job_without_pausing(
    hass: HomeAssistant, mock_entry
):
    client = _client()
    client.open_replacement.side_effect = WoltaApiError("bound", status=409)
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    await _cycle(coordinator)

    job = coordinator._state["replacement_job"]
    assert job["target"] == TARGET
    assert job["replacement"] is None
    client.commit_replacement.assert_not_called()
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_profile_full_on_commit_waits_a_day(hass: HomeAssistant, mock_entry):
    """413 on commit (the result would exceed the profile's row limit): the replacement
    is deleted, the job waits a day, the upload goes on."""
    client = _client()
    client.commit_replacement.side_effect = WoltaApiError("full", status=413)
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    await _cycle(coordinator)

    job = coordinator._state["replacement_job"]
    rid = client.open_replacement.await_args.args[1]
    client.delete_replacement.assert_awaited_once_with(coordinator.token, rid)
    assert job["replacement"] is None
    assert job["retry_at"] == (NOW + timedelta(days=1)).isoformat()
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_conflict_reopens_with_a_new_id_next_cycle(hass: HomeAssistant, mock_entry):
    client = _client()
    client.commit_replacement.side_effect = [
        WoltaReplacementConflict("conflict"), dict(COMMIT_RESULT)]
    coordinator, committed = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    await _cycle(coordinator)
    first = client.open_replacement.await_args.args[1]
    assert client.open_replacement.await_count == 1

    await _cycle(coordinator, NOW + timedelta(hours=6))
    second = client.open_replacement.await_args.args[1]
    assert client.open_replacement.await_count == 2
    assert second != first
    client.commit_replacement.assert_awaited_with(coordinator.token, second, None)
    assert committed == [COMMIT_RESULT]
    assert "replacement_job" not in coordinator._state


# ---------------------------------------------------------------------------
# Direction job (K6): swap the battery fields of stored rows on the server
# ---------------------------------------------------------------------------

from datetime import timezone  # noqa: E402

from custom_components.wolta.const import (  # noqa: E402
    CONF_INVERT_BATTERY,
    CONF_INVERT_INTERVALS,
)

UTC = timezone.utc
D_NOW = datetime(2026, 10, 13, 9, 5, tzinfo=UTC)
D_SINCE = "2025-10-01T00:00:00+00:00"
D_BOOKMARK = "2026-10-13T08:30:00+00:00"
D_END = datetime(2026, 10, 13, 9, tzinfo=UTC)  # ceil_hour(08:30 + 15 min)
FROM = "2026-09-28T13:00:00+00:00"
A = "2026-01-01T00:00:00+00:00"
B = "2026-06-01T00:00:00+00:00"


async def _direction_coordinator(
    hass, mock_entry, client, *, desired, applied, bookmark=D_BOOKMARK, job=None
):
    mock_entry.data = {
        **mock_entry.data,
        CONF_INVERT_INTERVALS: desired,
        CONF_INVERT_BATTERY: desired == [[None, None]],
    }
    state = {"applied_invert_intervals": applied}
    if bookmark is not None:
        state["last_uploaded_ts"] = bookmark
    if job is not None:
        state["replacement_job"] = job
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state=state)
    seen: list = []  # the applied intervals each regular read was built with

    async def rows(*_args):
        seen.append(coordinator._state["applied_invert_intervals"])
        return _make_rows(D_NOW - timedelta(minutes=20))

    coordinator._incremental_rows = AsyncMock(side_effect=rows)
    coordinator._backfill_rows = AsyncMock(side_effect=rows)
    coordinator._reconcile_coverage = AsyncMock()
    return coordinator, seen


def _direction_client(data_since=D_SINCE, *, with_key=True):
    client = _client()
    profile = dict(BASE_PROFILE)
    if with_key:
        profile["data_since"] = data_since
    client.get_profile = AsyncMock(return_value=profile)
    return client


def _opened(client) -> list[tuple]:
    return [
        (c.kwargs["kind"], c.kwargs["window_start"], c.kwargs["window_end"], c.kwargs["reason"])
        for c in client.open_replacement.call_args_list
    ]


@pytest.mark.asyncio
async def test_direction_change_swaps_one_window_and_keeps_the_bookmark(
    hass: HomeAssistant, mock_entry
):
    """Desired [[T, None]] over applied []: exactly one swap_battery from T to the hour
    after the bookmark's quarter. Nothing is re-sent: no bookmark reset, no forced
    recompute flag, the regular upload stays incremental and is built with the new
    intervals, after the commit."""
    client = _direction_client()
    puts_before_commit: list = []

    async def commit(token, rid, expected):
        puts_before_commit.append(client.put_data.await_count)
        return dict(COMMIT_RESULT)

    client.commit_replacement.side_effect = commit
    coordinator, seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[])

    await _cycle(coordinator, D_NOW)

    assert _opened(client) == [
        ("swap_battery", datetime(2026, 9, 28, 13, tzinfo=UTC), D_END, "direction")]
    assert client.open_replacement.call_args.kwargs["fields"] is None
    client.commit_replacement.assert_awaited_once()
    assert client.commit_replacement.await_args.args[2] is None
    assert puts_before_commit == [0]
    assert coordinator._state["applied_invert_intervals"] == [[FROM, None]]
    assert "replacement_job" not in coordinator._state
    assert "pending_invert_recompute" not in coordinator._state
    coordinator._incremental_rows.assert_awaited_once()
    coordinator._backfill_rows.assert_not_called()
    assert seen == [[[FROM, None]]]
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_direction_whole_history_window_starts_at_the_floored_data_since(
    hass: HomeAssistant, mock_entry
):
    client = _direction_client("2025-10-01T00:20:00+00:00")
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[None, None]], applied=[])

    await _cycle(coordinator, D_NOW)

    assert _opened(client) == [
        ("swap_battery", datetime(2025, 10, 1, tzinfo=UTC), D_END, "direction")]
    assert coordinator._state["applied_invert_intervals"] == [[None, None]]
    assert coordinator._state["applied_invert"] is True


@pytest.mark.asyncio
async def test_direction_two_segments_are_swapped_in_order(hass: HomeAssistant, mock_entry):
    """Applied [[None, A], [B, None]] back to []: two swaps in chronological order, each
    committed window booked before the next; only the last may reach the bookmark."""
    client = _direction_client()
    at_commit: list = []

    async def commit(token, rid, expected):
        job = coordinator._state["replacement_job"]
        at_commit.append((len(job["done_windows"]), job["replacement"]["final"]))
        return dict(COMMIT_RESULT)

    client.commit_replacement.side_effect = commit
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[], applied=[[None, A], [B, None]])

    await _cycle(coordinator, D_NOW)

    assert _opened(client) == [
        ("swap_battery", datetime(2025, 10, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC),
         "direction"),
        ("swap_battery", datetime(2026, 6, 1, tzinfo=UTC), D_END, "direction"),
    ]
    assert at_commit == [(0, False), (1, True)]
    assert coordinator._state["applied_invert_intervals"] == []
    assert "replacement_job" not in coordinator._state


@pytest.mark.asyncio
async def test_direction_restart_after_the_first_commit_opens_only_the_second(
    hass: HomeAssistant, mock_entry
):
    client = _direction_client()
    job = {
        "type": "direction",
        "target": [],
        "replacement": None,
        "done_windows": [[D_SINCE, A]],
        "retry_at": None,
    }
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[], applied=[[None, A], [B, None]], job=job)

    await _cycle(coordinator, D_NOW)

    assert _opened(client) == [
        ("swap_battery", datetime(2026, 6, 1, tzinfo=UTC), D_END, "direction")]
    assert coordinator._state["applied_invert_intervals"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bookmark,data_since", [(None, D_SINCE), (D_BOOKMARK, None)])
async def test_direction_with_nothing_stored_applies_at_once(
    hass: HomeAssistant, mock_entry, bookmark, data_since
):
    """No bookmark, or no rows on the server: no call, applied := desired right away, and
    the upload in the same cycle builds with it."""
    client = _direction_client(data_since)
    coordinator, seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[], bookmark=bookmark)

    await _cycle(coordinator, D_NOW)

    client.open_replacement.assert_not_called()
    assert coordinator._state["applied_invert_intervals"] == [[FROM, None]]
    assert "replacement_job" not in coordinator._state
    assert seen == [[[FROM, None]]]


@pytest.mark.asyncio
async def test_direction_without_data_since_waits_without_pausing(
    hass: HomeAssistant, mock_entry
):
    client = _direction_client(with_key=False)
    coordinator, seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[])

    await _cycle(coordinator, D_NOW)

    client.open_replacement.assert_not_called()
    assert coordinator._state["replacement_job"]["type"] == "direction"
    assert coordinator._state["applied_invert_intervals"] == []
    assert seen == [[]]
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_direction_change_during_a_job_is_taken_up_by_a_new_job(
    hass: HomeAssistant, mock_entry
):
    """The running job finishes against its own target; the next cycle sees the new
    difference and starts a fresh job."""
    client = _direction_client()
    job = {
        "type": "direction",
        "target": [[None, None]],
        "replacement": None,
        "done_windows": [],
        "retry_at": None,
    }
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[], applied=[], job=job)

    await _cycle(coordinator, D_NOW)
    assert client.open_replacement.await_count == 1
    assert coordinator._state["applied_invert_intervals"] == [[None, None]]
    assert "replacement_job" not in coordinator._state

    await _cycle(coordinator, D_NOW + timedelta(hours=1))
    assert client.open_replacement.await_count == 2
    first, second = client.open_replacement.call_args_list
    assert first.args[1] != second.args[1]
    assert coordinator._state["applied_invert_intervals"] == []


@pytest.mark.asyncio
async def test_direction_final_commit_applies_only_when_no_row_passed_its_window(
    hass: HomeAssistant, mock_entry
):
    """A final window booked late (after rows were uploaded past it) does not apply the
    target: the next segment covers the rows uploaded with the old direction."""
    client = _direction_client()
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[None, None]], applied=[],
        bookmark="2026-10-13T10:00:00+00:00")
    job = coordinator._new_replacement_job("direction", [[None, None]])
    rep = {"kind": "swap_battery", "window_start": D_SINCE,
           "window_end": D_END.isoformat(), "final": True}
    job["done_windows"].append([D_SINCE, D_END.isoformat()])

    await coordinator._direction_committed(job, rep, {})
    assert coordinator._state["applied_invert_intervals"] == []

    spec = await coordinator._direction_next(job, D_NOW, {"data_since": D_SINCE})
    assert (spec["window_start"], spec["window_end"], spec["final"]) == (
        D_END.isoformat(), "2026-10-13T11:00:00+00:00", True)

    coordinator._state["last_uploaded_ts"] = D_BOOKMARK
    await coordinator._direction_committed(job, rep, {})
    assert coordinator._state["applied_invert_intervals"] == [[None, None]]


@pytest.mark.asyncio
async def test_unchanged_direction_starts_no_job(hass: HomeAssistant, mock_entry):
    client = _direction_client()
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[[FROM, None]])

    await _cycle(coordinator, D_NOW)

    client.open_replacement.assert_not_called()
    assert "replacement_job" not in coordinator._state
