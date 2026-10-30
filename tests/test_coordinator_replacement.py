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
from custom_components.wolta.coordinator import _JOB_MAX_FAILED_CYCLES, _ReplacementType

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
# Server refusals: start over, send again, wait or count as done
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
# The pause: no regular upload while a replacement is open
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
@pytest.mark.parametrize("status", [400, 403, 408])
async def test_refused_status_get_keeps_a_replacement_that_may_be_committed(
    hass: HomeAssistant, mock_entry, status
):
    """The commit's response is lost (a 504 after it went through), and the next status
    GET is refused by something in between (a proxy's 403, a 400, a 408). That says
    nothing about the replacement: it is kept as it is - never deleted, never opened
    again under a new id - and once a GET can tell it is committed, the window is booked
    exactly once."""
    client = _client()
    client.commit_replacement.side_effect = WoltaApiError("gateway", status=504)
    coordinator, committed = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    await _cycle(coordinator)
    rid = client.open_replacement.await_args.args[1]
    assert coordinator._state["replacement_job"]["replacement"]["id"] == rid

    client.get_replacement.side_effect = WoltaApiError("refused", status=status)
    await _cycle(coordinator, NOW + timedelta(hours=1))

    job = coordinator._state["replacement_job"]
    assert job["replacement"]["id"] == rid
    assert client.open_replacement.await_count == 1
    client.delete_replacement.assert_not_called()

    client.get_replacement.side_effect = None
    client.get_replacement.return_value = {"status": "committed", "result": COMMIT_RESULT}
    await _cycle(coordinator, NOW + timedelta(hours=2))

    assert committed == [COMMIT_RESULT]
    assert client.open_replacement.await_count == 1
    assert client.commit_replacement.await_count == 1
    client.delete_replacement.assert_not_called()
    assert "replacement_job" not in coordinator._state


@pytest.mark.asyncio
async def test_refused_status_get_at_give_up_abandons_the_replacement(
    hass: HomeAssistant, mock_entry
):
    """A status GET that keeps being refused with a 4xx ends like an unreadable status:
    the replacement is abandoned (kept, pausing nothing, never committed), not discarded."""
    client = _client()
    client.commit_replacement.side_effect = WoltaApiError("gateway", status=504)
    coordinator, committed = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    await _cycle(coordinator)
    rid = client.open_replacement.await_args.args[1]
    client.get_replacement.side_effect = WoltaApiError("refused", status=403)
    for n in range(1, 4):
        await _cycle(coordinator, NOW + timedelta(hours=n))

    job = coordinator._state["replacement_job"]
    assert job["replacement"]["id"] == rid
    assert job["replacement"]["abandoned"] is True
    assert client.open_replacement.await_count == 1
    assert committed == []
    client.put_data.assert_awaited()


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
# Direction job: swap the battery fields of stored rows on the server
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
@pytest.mark.parametrize("desired,applied,job,pending", [
    ([[FROM, None]], [[FROM, None]], None, False),
    ([[FROM, None]], [], None, True),
    ([], [], {"type": "direction", "target": [], "replacement": None,
              "done_windows": [], "retry_at": None}, True),
    ([], [], {"type": "entities", "target": "X", "replacement": None,
              "done_windows": [], "retry_at": None}, False),
])
async def test_direction_change_pending(
    hass: HomeAssistant, mock_entry, desired, applied, job, pending
):
    """What the direction repair asks before it flips: a direction job, or desired
    against applied."""
    client = _direction_client()
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=desired, applied=applied, job=job)
    assert coordinator.direction_change_pending() is pending


@pytest.mark.asyncio
async def test_unchanged_direction_starts_no_job(hass: HomeAssistant, mock_entry):
    client = _direction_client()
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[[FROM, None]])

    await _cycle(coordinator, D_NOW)

    client.open_replacement.assert_not_called()
    assert "replacement_job" not in coordinator._state


# ---------------------------------------------------------------------------
# Entity job: a sensor change replaces only the changed fields
# ---------------------------------------------------------------------------

from custom_components.wolta.const import (  # noqa: E402
    CONF_BATT_IN,
    CONF_BATT_OUT,
    CONF_EXTERNAL_CONTROL,
    CONF_GRID_IN,
    CONF_SOLAR,
)
from custom_components.wolta.coordinator import (  # noqa: E402
    replaced_fields,
    replaced_streams,
)

E_NOW = datetime(2026, 10, 13, 9, 5, tzinfo=UTC)
E_SINCE = "2025-10-01T00:00:00+00:00"
E_BOOKMARK = "2026-10-13T08:30:00+00:00"
E_NEW_FROM = datetime(2026, 6, 1, 10, tzinfo=UTC)   # the new sensor's first statistics
# Its first period cannot be judged (no period before it), so the first hour whose four
# quarters are all complete - the window start - is the one after.
E_START = datetime(2026, 6, 1, 11, tzinfo=UTC)
E_OTHERS_FROM = datetime(2026, 10, 12, tzinfo=UTC)  # the other sensors' (5-minute only)
E_END = datetime(2026, 10, 13, 9, tzinfo=UTC)       # hour after the last complete quarter
EXTERNAL = "binary_sensor.flex_active"
# The binary sensor's recorded states: first one on 2026-10-10 07:20, 'on' 10:05-11:00
# on 2026-10-12.
E_POINTS = [
    (datetime(2026, 10, 10, 7, 20, tzinfo=UTC), "off"),
    (datetime(2026, 10, 12, 10, 5, tzinfo=UTC), "on"),
    (datetime(2026, 10, 12, 11, 0, tzinfo=UTC), "off"),
]
OLD_FP = json.dumps({
    "batt_in": ["sensor.batt_in"], "batt_out": ["sensor.batt_out"],
    "grid_in": ["sensor.grid_in"], "grid_out": ["sensor.grid_out"],
    "solar": ["sensor.solar"],
}, sort_keys=True)


def _fake_statistics(available: dict):
    """async_fetch_change over sensors measured every period from their date in
    `available` until E_NOW, or over a list of (from, to) spans: hourly 0.4 kWh,
    5-minute 0.1 kWh."""
    calls: list = []

    async def fetch(hass, ids, start, end, period="hour"):
        calls.append((period, start, end, set(ids)))
        step = timedelta(hours=1) if period == "hour" else timedelta(minutes=5)
        change = 0.4 if period == "hour" else 0.1
        out = {}
        for e in ids:
            spans = available.get(e)
            if spans is None:
                continue
            if isinstance(spans, datetime):
                spans = [(spans, E_NOW)]
            rows = []
            for since, until in spans:
                t = max(start, since)
                while t < min(end, until, E_NOW):
                    rows.append({"start": t.timestamp(), "change": change})
                    t += step
            out[e] = rows
        return out

    fetch.calls = calls
    return fetch


def _others(**extra) -> dict[str, datetime]:
    return {"sensor.batt_in": E_OTHERS_FROM, "sensor.batt_out": E_OTHERS_FROM,
            "sensor.grid_in": E_OTHERS_FROM, "sensor.grid_out": E_OTHERS_FROM, **extra}


async def _entity_coordinator(
    hass, mock_entry, client, *, data_updates, applied=OLD_FP, bookmark=E_BOOKMARK, job=None
):
    mock_entry.data = {**mock_entry.data, **data_updates}
    state = {"applied_invert_intervals": [], "applied_invert": False}
    if applied is not None:
        state["applied_entities"] = applied
    if bookmark is not None:
        state["last_uploaded_ts"] = bookmark
    if job is not None:
        state["replacement_job"] = job
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state=state)
    starts: list = []  # the bookmark each regular read started from

    async def rows(start, now):
        starts.append(start)
        return _make_rows(E_NOW - timedelta(minutes=20))

    coordinator._incremental_rows = AsyncMock(side_effect=rows)
    coordinator._heal_rows = AsyncMock(side_effect=rows)
    coordinator._backfill_rows = AsyncMock(return_value=[])
    coordinator._reconcile_coverage = AsyncMock()
    return coordinator, starts


async def _entity_cycle(coordinator, available, now=E_NOW, points=None):
    fetch = _fake_statistics(available)
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=now),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
        patch("custom_components.wolta.stats.async_fetch_states",
              new=AsyncMock(return_value=list(points or []))),
    ):
        await coordinator._async_update_data()
    return fetch


def _staged(client, index=None) -> list[dict]:
    calls = client.stage_replacement_rows.call_args_list
    if index is not None:
        calls = [calls[index]]
    return [row for c in calls for row in c.args[2]]


def _fp(**streams) -> str:
    return json.dumps({**json.loads(OLD_FP), **streams}, sort_keys=True)


def test_replaced_streams_and_fields():
    assert replaced_streams(OLD_FP, _fp(solar=["sensor.solar_new"])) == {
        "solar": ["sensor.solar_new"]}
    assert replaced_fields(["solar"]) == ["solar_kwh"]
    assert replaced_fields(["batt_out"]) == ["batt_charged_kwh", "batt_discharged_kwh"]
    assert replaced_fields(["external_control", "grid_in"]) == [
        "grid_import_kwh", "external_control"]
    assert replaced_streams(OLD_FP, _fp(external_control=["binary_sensor.x"])) == {
        "external_control": ["binary_sensor.x"]}
    # Same sensors in another order: nothing changed.
    assert replaced_streams(_fp(solar=["sensor.a", "sensor.b"]),
                            _fp(solar=["sensor.b", "sensor.a"])) == {}
    # Only removed: nothing replaced. Added or swapped: replaced, with the added ones.
    assert replaced_streams(_fp(solar=["sensor.a", "sensor.b"]), _fp(solar=["sensor.a"])) == {}
    assert replaced_streams(_fp(solar=["sensor.a", "sensor.b"]),
                            _fp(solar=["sensor.a", "sensor.c"])) == {"solar": ["sensor.c"]}
    assert replaced_streams(_fp(external_control=["binary_sensor.x"]), OLD_FP) == {}
    # One battery stream left without sensors while the other is swapped: no battery field.
    assert replaced_streams(OLD_FP, _fp(batt_in=[], batt_out=["sensor.batt_out_new"])) == {}
    assert replaced_streams("OLD-FINGERPRINT", OLD_FP) == {
        "batt_in": ["sensor.batt_in"], "batt_out": ["sensor.batt_out"],
        "grid_in": ["sensor.grid_in"], "grid_out": ["sensor.grid_out"],
        "solar": ["sensor.solar"]}


@pytest.mark.asyncio
async def test_entity_change_replaces_only_the_solar_field(hass: HomeAssistant, mock_entry):
    """sensor.solar -> sensor.solar_new with statistics from 2026-06-01 10:00, data_since
    2025-10-01: one "rows" replacement of solar_kwh from 11:00 that day (the first whole
    hour with every quarter complete: the 10:00 period has no period before it and is
    never clean, and a window from 10:00 would remove that hour's stored rows) to the
    hour after the last complete quarter. Quarters complete for every sensor carry every
    field; the others only solar_kwh. After the commit the new fingerprint is applied,
    the bookmark follows the last full quarter, nothing forces a recompute and the
    regular upload goes on incrementally."""
    client = _direction_client(E_SINCE)
    coordinator, starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]})

    fetch = await _entity_cycle(
        coordinator, _others(**{"sensor.solar_new": E_NEW_FROM}))

    client.open_replacement.assert_awaited_once()
    open_call = client.open_replacement.call_args
    assert open_call.kwargs["kind"] == "rows"
    assert open_call.kwargs["fields"] == ["solar_kwh"]
    assert open_call.kwargs["reason"] == "entities_changed"
    assert open_call.kwargs["window_start"] == E_START
    assert open_call.kwargs["window_end"] == E_END
    rows = _staged(client)
    assert all("solar_kwh" in row for row in rows)
    partial = [r for r in rows if len(r) == 2]
    full = [r for r in rows if len(r) == 7]
    assert len(partial) + len(full) == len(rows)
    assert rows[0]["ts"] == "2026-06-01T11:00:00+00:00"
    assert partial[-1]["ts"] < "2026-10-12T00:15:00+00:00" <= full[0]["ts"]
    assert full[-1]["ts"] == "2026-10-13T08:45:00+00:00"
    assert client.commit_replacement.await_args.args[2] == len(rows)
    # A cheap probe of the new sensor first, then the read from the floored data_since:
    # hourly, then 5-minute statistics.
    assert fetch.calls[0] == (
        "hour", datetime(2025, 9, 30, 23, tzinfo=UTC), E_NOW, {"sensor.solar_new"})
    assert fetch.calls[1][:2] == ("hour", datetime(2025, 9, 30, 23, tzinfo=UTC))
    assert len(fetch.calls[1][3]) == 5

    new_fp = coordinator._entities_fingerprint()
    assert coordinator._state["applied_entities"] == new_fp
    assert json.loads(new_fp)["solar"] == ["sensor.solar_new"]
    assert "replacement_job" not in coordinator._state
    assert "pending_invert_recompute" not in coordinator._state
    # The bookmark lay inside the window: it moves to the last full quarter.
    assert starts == [datetime(2026, 10, 13, 8, 45, tzinfo=UTC)]
    coordinator._backfill_rows.assert_not_called()
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_entity_change_of_one_battery_stream_replaces_both_fields(
    hass: HomeAssistant, mock_entry
):
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_BATT_IN: ["sensor.batt_in_new"]})

    await _entity_cycle(coordinator, {
        "sensor.batt_in_new": E_NEW_FROM, "sensor.batt_out": E_NEW_FROM,
        "sensor.grid_in": E_OTHERS_FROM, "sensor.grid_out": E_OTHERS_FROM,
        "sensor.solar": E_OTHERS_FROM,
    })

    kwargs = client.open_replacement.call_args.kwargs
    assert kwargs["fields"] == ["batt_charged_kwh", "batt_discharged_kwh"]
    assert kwargs["window_start"] == E_START
    rows = _staged(client)
    assert rows
    assert all({"batt_charged_kwh", "batt_discharged_kwh"} <= row.keys() for row in rows)


@pytest.mark.asyncio
async def test_solar_swap_with_an_added_external_sensor(hass: HomeAssistant, mock_entry):
    """Solar swapped and an external-control sensor added in one reconfigure. The new
    binary sensor never pulls the energy window back to data_since (that would remove
    every stored row between data_since and the new solar sensor's start): the energy
    window still starts at the solar sensor's first complete hour. The flag follows in a
    replacement of its own over the span the binary sensor's states cover, with rows
    carrying only external_control."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client,
        data_updates={CONF_SOLAR: ["sensor.solar_new"], CONF_EXTERNAL_CONTROL: EXTERNAL})

    await _entity_cycle(
        coordinator, _others(**{"sensor.solar_new": E_NEW_FROM}), points=E_POINTS)

    opened = [(c.kwargs["fields"], c.kwargs["window_start"], c.kwargs["window_end"])
              for c in client.open_replacement.call_args_list]
    assert opened == [
        (["solar_kwh"], E_START, E_END),
        (["external_control"], datetime(2026, 10, 10, 7, tzinfo=UTC), E_END),
    ]
    energy_rows = _staged(client, 0)
    assert energy_rows[0]["ts"] == E_START.isoformat()
    flag_rows = _staged(client, 1)
    assert all(set(r) == {"ts", "external_control"} for r in flag_rows)
    assert flag_rows[0]["ts"] == "2026-10-10T07:00:00+00:00"
    assert flag_rows[-1]["ts"] == "2026-10-13T08:45:00+00:00"
    assert len(flag_rows) == (3 * 24 + 2) * 4  # every quarter: no stored row unstaged
    flagged = [r["ts"] for r in flag_rows if r["external_control"]]
    assert flagged == [f"2026-10-12T10:{m:02d}:00+00:00" for m in (0, 15, 30, 45)]
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()
    assert "replacement_job" not in coordinator._state


@pytest.mark.asyncio
async def test_external_only_change_replaces_the_flag_from_the_first_state(
    hass: HomeAssistant, mock_entry
):
    client = _direction_client(E_SINCE)
    coordinator, starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_EXTERNAL_CONTROL: EXTERNAL})

    fetch = await _entity_cycle(coordinator, _others(**{"sensor.solar": E_NEW_FROM}),
                                points=E_POINTS)

    kwargs = client.open_replacement.call_args.kwargs
    assert kwargs["fields"] == ["external_control"]
    assert kwargs["window_start"] == datetime(2026, 10, 10, 7, tzinfo=UTC)
    assert kwargs["window_end"] == E_END
    assert all(set(r) == {"ts", "external_control"} for r in _staged(client))
    assert fetch.calls == []  # no statistics read: the flag comes from states
    # No full row was staged: the bookmark stays.
    assert starts == [datetime.fromisoformat(E_BOOKMARK)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old,updates",
    [
        # The external-control sensor removed: the stored flags stay.
        (json.dumps({**json.loads(OLD_FP), "external_control": [EXTERNAL]},
                    sort_keys=True), {}),
        # Every solar sensor removed: the stored solar history stays.
        (OLD_FP, {CONF_SOLAR: []}),
        # One of two solar sensors removed: the stored solar history stays.
        (_fp(solar=["sensor.solar", "sensor.solar_b"]), {}),
        # One battery stream emptied while the other is swapped: no battery field.
        (OLD_FP, {CONF_BATT_IN: [], CONF_BATT_OUT: ["sensor.batt_out_new"]}),
    ],
)
async def test_removed_sensor_replaces_nothing(
    hass: HomeAssistant, mock_entry, old, updates
):
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates=updates, applied=old)

    await _entity_cycle(coordinator, _others(**{"sensor.solar": E_NEW_FROM}))

    client.open_replacement.assert_not_called()
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


@pytest.mark.asyncio
async def test_entity_change_without_statistics_waits_and_uploads(
    hass: HomeAssistant, mock_entry
):
    """A new sensor with no statistics yet: nothing is opened, the regular upload goes
    on, and the job stays to try again next cycle. Only the cheap probe of the new
    sensor is read, not the whole history."""
    client = _direction_client(E_SINCE)
    coordinator, starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]})

    fetch = await _entity_cycle(coordinator, _others())

    client.open_replacement.assert_not_called()
    client.put_data.assert_awaited()
    assert starts == [datetime.fromisoformat(E_BOOKMARK)]
    assert [c[3] for c in fetch.calls] == [{"sensor.solar_new"}]
    job = coordinator._state["replacement_job"]
    assert job["type"] == "entities"
    assert job["fields"] == ["solar_kwh"]
    assert job["replacement"] is None
    assert coordinator._state["applied_entities"] == OLD_FP

    # Statistics arrive: the next cycle replaces.
    coordinator._state["last_uploaded_ts"] = E_BOOKMARK
    await _entity_cycle(coordinator, _others(**{"sensor.solar_new": E_NEW_FROM}))
    client.open_replacement.assert_awaited_once()
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


@pytest.mark.asyncio
async def test_entity_first_recording_starts_no_job(hass: HomeAssistant, mock_entry):
    client = _direction_client(E_SINCE)
    coordinator, starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={}, applied=None)

    await _entity_cycle(coordinator, _others(**{"sensor.solar": E_NEW_FROM}))

    client.open_replacement.assert_not_called()
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()
    assert starts == [datetime.fromisoformat(E_BOOKMARK)]


@pytest.mark.asyncio
@pytest.mark.parametrize("bookmark,data_since", [(None, E_SINCE), (E_BOOKMARK, None)])
async def test_entity_change_with_nothing_stored_applies_at_once(
    hass: HomeAssistant, mock_entry, bookmark, data_since
):
    client = _direction_client(data_since)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]},
        bookmark=bookmark)

    await _entity_cycle(coordinator, _others(**{"sensor.solar_new": E_NEW_FROM}))

    client.open_replacement.assert_not_called()
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


@pytest.mark.asyncio
async def test_entity_change_without_data_since_key_waits(hass: HomeAssistant, mock_entry):
    client = _direction_client(with_key=False)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]})

    await _entity_cycle(coordinator, _others(**{"sensor.solar_new": E_NEW_FROM}))

    client.open_replacement.assert_not_called()
    assert coordinator._state["replacement_job"]["type"] == "entities"
    assert coordinator._state["applied_entities"] == OLD_FP
    client.put_data.assert_awaited()


@pytest.mark.asyncio
async def test_entity_job_resumed_builds_the_same_rows(hass: HomeAssistant, mock_entry):
    """A replacement left open (the commit failed) is staged again after a restart with
    the very same rows, read over the range fixed in the spec and not up to the new
    `now` (more statistics have arrived by then)."""
    client = _direction_client(E_SINCE)
    client.commit_replacement.side_effect = WoltaApiError("down", status=503)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]})
    available = _others(**{"sensor.solar_new": E_NEW_FROM})

    await _entity_cycle(coordinator, available)
    first = _staged(client)
    assert first
    rep = coordinator._state["replacement_job"]["replacement"]
    rid = client.open_replacement.call_args.args[1]
    assert rep["id"] == rid
    assert rep["read_end"] == E_NOW.isoformat()
    client.put_data.assert_not_called()  # paused while it is open

    client.stage_replacement_rows.reset_mock()
    client.commit_replacement.side_effect = None
    coordinator._entity_rows_cache = {}  # a restart
    fetch = await _entity_cycle(coordinator, available, now=E_NOW + timedelta(hours=1))

    client.open_replacement.assert_awaited_once()
    assert _staged(client) == first
    assert max(c[2] for c in fetch.calls[:2]) == E_NOW
    assert client.commit_replacement.await_args.args[1] == rid
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


@pytest.mark.asyncio
async def test_entity_rows_that_cannot_be_rebuilt_wait(hass: HomeAssistant, mock_entry):
    """Hook contract: None when the rows cannot be built now (the statistics are gone)."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]})
    job = coordinator._new_replacement_job("entities", coordinator._entities_fingerprint())
    job["streams"], job["fields"] = {"solar": ["sensor.solar_new"]}, ["solar_kwh"]
    job["energy_plan"] = {"windows": []}
    assert not coordinator._selection_left(job, "energy")  # the rebuild itself fails
    spec = {"kind": "rows", "phase": "energy", "fields": ["solar_kwh"],
            "window_start": E_START.isoformat(), "window_end": E_END.isoformat(),
            "read_from": "2025-10-01T00:00:00+00:00", "read_end": E_NOW.isoformat(),
            "split": "2026-10-04T09:00:00+00:00"}
    with patch("custom_components.wolta.coordinator.async_fetch_change",
               side_effect=_fake_statistics(_others())):
        assert await coordinator._entities_rows(job, spec, E_NOW) is None
    assert "energy_plan" not in job  # made anew next time


@pytest.mark.asyncio
async def test_direction_goes_before_a_pending_sensor_change(
    hass: HomeAssistant, mock_entry
):
    """Both changed: the direction job runs first; the sensor change follows next cycle."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client,
        data_updates={CONF_SOLAR: ["sensor.solar_new"],
                      CONF_INVERT_INTERVALS: [[None, None]], CONF_INVERT_BATTERY: True})
    available = _others(**{"sensor.solar_new": E_NEW_FROM})

    await _entity_cycle(coordinator, available)
    assert [c.kwargs["kind"] for c in client.open_replacement.call_args_list] == [
        "swap_battery"]
    assert coordinator._state["applied_invert_intervals"] == [[None, None]]
    assert coordinator._state["applied_entities"] == OLD_FP

    coordinator._state["last_uploaded_ts"] = E_BOOKMARK
    await _entity_cycle(coordinator, available)
    assert [c.kwargs["kind"] for c in client.open_replacement.call_args_list] == [
        "swap_battery", "rows"]
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


@pytest.mark.asyncio
async def test_direction_change_preempts_a_waiting_entity_job(
    hass: HomeAssistant, mock_entry
):
    client = _direction_client(E_SINCE)
    waiting = {
        "type": "entities", "target": "WAITING", "replacement": None,
        "done_windows": [], "retry_at": None, "phases_done": [],
        "streams": {"solar": ["sensor.solar_new"]}, "fields": ["solar_kwh"],
    }
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client,
        data_updates={CONF_INVERT_INTERVALS: [[None, None]], CONF_INVERT_BATTERY: True},
        job=waiting)

    await coordinator._queue_direction_job()

    assert coordinator._state["replacement_job"]["type"] == "direction"


@pytest.mark.asyncio
async def test_waiting_entity_job_follows_a_new_selection(hass: HomeAssistant, mock_entry):
    """A job that has opened nothing takes the latest selection as its target (keeping a
    429's retry_at), and goes away when the selection is back to the applied one."""
    client = _direction_client(E_SINCE)
    retry_at = (E_NOW + timedelta(hours=3)).isoformat()
    stale = {
        "type": "entities", "target": "STALE", "replacement": None,
        "done_windows": [], "retry_at": retry_at, "phases_done": [],
        "streams": {"solar": ["sensor.solar_new"]}, "fields": ["solar_kwh"],
    }
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: ["sensor.grid_in_new"]},
        job=stale)

    await _entity_cycle(coordinator, _others())

    job = coordinator._state["replacement_job"]
    assert job["target"] == coordinator._entities_fingerprint()
    assert job["streams"] == {"grid_in": ["sensor.grid_in_new"]}
    assert job["fields"] == ["grid_import_kwh"]
    assert job["retry_at"] == retry_at
    client.open_replacement.assert_not_called()

    coordinator._entity_map["grid_in"] = ["sensor.grid_in"]
    await _entity_cycle(coordinator, _others())
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_entities"] == OLD_FP


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bookmark,last_full,result,expected",
    [
        # Inside the window, the last full quarter earlier: moves back.
        ("2026-10-13T08:30:00+00:00", "2026-10-13T08:00:00+00:00", COMMIT_RESULT,
         "2026-10-13T08:00:00+00:00"),
        # Past the window, the last full quarter earlier: unchanged.
        ("2026-10-13T09:15:00+00:00", "2026-10-13T08:00:00+00:00", COMMIT_RESULT,
         "2026-10-13T09:15:00+00:00"),
        # The last full quarter later than the bookmark: moves forward.
        ("2026-06-01T09:00:00+00:00", "2026-10-13T08:45:00+00:00", COMMIT_RESULT,
         "2026-10-13T08:45:00+00:00"),
        # No full quarter staged: unchanged.
        ("2026-10-13T08:30:00+00:00", None, COMMIT_RESULT, "2026-10-13T08:30:00+00:00"),
        # The server replaced nothing (409 empty): unchanged.
        ("2026-10-13T08:30:00+00:00", "2026-10-13T08:00:00+00:00", {},
         "2026-10-13T08:30:00+00:00"),
    ],
)
@pytest.mark.parametrize("final", [False, True])
async def test_entity_commit_moves_the_bookmark(
    hass: HomeAssistant, mock_entry, bookmark, last_full, result, expected, final
):
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={}, bookmark=bookmark)
    job = coordinator._new_replacement_job("entities", "NEW")
    rep = {"kind": "rows", "phase": "energy", "fields": ["solar_kwh"],
           "window_start": "2026-06-01T10:00:00+00:00",
           "window_end": "2026-10-13T09:00:00+00:00", "last_full": last_full,
           "final": final}

    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=E_NOW):
        await coordinator._entities_committed(job, rep, dict(result))

    if final and result and bookmark == "2026-10-13T09:15:00+00:00":
        # Past the last window: steps back, so the tail is uploaded again.
        expected = "2026-10-13T08:00:00+00:00"
    assert coordinator._state["last_uploaded_ts"] == expected


@pytest.mark.asyncio
async def test_entity_commit_after_the_last_window_without_a_full_quarter(
    hass: HomeAssistant, mock_entry
):
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={}, bookmark="2026-10-13T09:15:00+00:00")
    job = coordinator._new_replacement_job("entities", "NEW")
    rep = {"kind": "rows", "phase": "energy", "fields": ["solar_kwh"],
           "window_start": "2026-06-01T10:00:00+00:00",
           "window_end": "2026-10-13T07:00:00+00:00", "last_full": None, "final": True}

    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=E_NOW):
        await coordinator._entities_committed(job, rep, dict(COMMIT_RESULT))

    assert coordinator._state["last_uploaded_ts"] == "2026-10-13T06:45:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bookmark,window_end,last_full,expected",
    [
        # The last window ended long before the 5-minute boundary: the step back stops
        # at the boundary (E_NOW - 8 days, on the hour), not at the window's end.
        ("2026-10-13T08:30:00+00:00", "2026-09-01T00:00:00+00:00",
         "2026-08-31T23:45:00+00:00", "2026-10-05T09:00:00+00:00"),
        # The bookmark inside a window, the last full quarter far back: the same stop.
        ("2026-10-13T08:30:00+00:00", "2026-10-13T09:00:00+00:00",
         "2026-09-20T12:00:00+00:00", "2026-10-05T09:00:00+00:00"),
        # A bookmark already behind the boundary heals from hourly statistics anyway:
        # the step back is not limited.
        ("2026-10-01T00:00:00+00:00", "2026-10-13T09:00:00+00:00",
         "2026-09-20T12:00:00+00:00", "2026-09-20T12:00:00+00:00"),
    ],
)
async def test_entity_commit_never_steps_the_bookmark_back_past_the_5_minute_boundary(
    hass: HomeAssistant, mock_entry, bookmark, window_end, last_full, expected
):
    """Stepping back further would make the next upload heal from hourly statistics
    and overwrite the recent quarters with hour/4 values."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={}, bookmark=bookmark)
    job = coordinator._new_replacement_job("entities", "NEW")
    rep = {"kind": "rows", "phase": "energy", "fields": ["solar_kwh"],
           "window_start": "2026-06-01T10:00:00+00:00", "window_end": window_end,
           "last_full": last_full, "final": True}

    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=E_NOW):
        await coordinator._entities_committed(job, rep, dict(COMMIT_RESULT))

    assert coordinator._state["last_uploaded_ts"] == expected


# --- Round 2: windows of complete hours, removal-only changes, the external phase ---

G_NEW = "sensor.grid_in_new"


def _opened_windows(client) -> list[tuple]:
    return [(c.kwargs["window_start"], c.kwargs["window_end"])
            for c in client.open_replacement.call_args_list]


@pytest.mark.asyncio
async def test_added_sensor_drives_the_window_start(hass: HomeAssistant, mock_entry):
    """Solar [a, b] -> [a, c]: replaced from c's first complete hour; a's long history
    does not pull the start back."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client,
        data_updates={CONF_SOLAR: ["sensor.solar", "sensor.solar_c"]},
        applied=_fp(solar=["sensor.solar", "sensor.solar_b"]))

    await _entity_cycle(coordinator, _others(**{
        "sensor.solar": datetime(2025, 9, 1, tzinfo=UTC), "sensor.solar_c": E_NEW_FROM}))

    kwargs = client.open_replacement.call_args.kwargs
    assert kwargs["fields"] == ["solar_kwh"]
    assert kwargs["window_start"] == E_START


@pytest.mark.asyncio
async def test_short_gap_of_the_new_sensor_stays_inside_one_window(
    hass: HomeAssistant, mock_entry
):
    """A 2 h silence: runs 3 h apart are one window, and the gap's quarters are removed
    as honest gaps (nothing staged for them)."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: [G_NEW]})
    gap = (datetime(2026, 6, 10, 12, tzinfo=UTC), datetime(2026, 6, 10, 14, tzinfo=UTC))

    await _entity_cycle(coordinator, _others(**{
        G_NEW: [(E_NEW_FROM, gap[0]), (gap[1], E_NOW)]}))

    assert _opened_windows(client) == [(E_START, E_END)]
    staged = {r["ts"] for r in _staged(client)}
    assert "2026-06-10T12:00:00+00:00" not in staged
    assert "2026-06-10T15:00:00+00:00" in staged


@pytest.mark.asyncio
async def test_energy_windows_and_rows_are_planned_off_the_event_loop(
    hass: HomeAssistant, mock_entry
):
    """The per-quarter judging, the windows and the rows (a year of quarters) run in the
    executor, never on the event loop."""
    import threading

    from custom_components.wolta import coordinator as coordinator_module

    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: [G_NEW]})
    loop_thread = threading.current_thread()
    threads: list = []
    real = coordinator_module._energy_windows

    def spy(*args, **kwargs):
        threads.append(threading.current_thread())
        return real(*args, **kwargs)

    with patch.object(coordinator_module, "_energy_windows", side_effect=spy):
        await _entity_cycle(coordinator, _others(**{G_NEW: E_NEW_FROM}))

    assert threads and all(t is not loop_thread for t in threads)
    assert _opened_windows(client) == [(E_START, E_END)]


@pytest.mark.asyncio
async def test_long_gap_of_the_new_sensor_splits_the_windows(
    hass: HomeAssistant, mock_entry
):
    """A 3-day silence stays outside every window: two windows, nothing staged inside
    the gap, so the stored rows there are kept."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: [G_NEW]})
    stop, resume = datetime(2026, 6, 10, 12, tzinfo=UTC), datetime(2026, 6, 13, 12, tzinfo=UTC)

    await _entity_cycle(coordinator, _others(**{G_NEW: [(E_NEW_FROM, stop), (resume, E_NOW)]}))

    assert _opened_windows(client) == [
        (E_START, stop), (resume + timedelta(hours=1), E_END)]
    assert not [r for r in _staged(client)
                if stop.isoformat() <= r["ts"] < (resume + timedelta(hours=1)).isoformat()]
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


@pytest.mark.asyncio
async def test_more_than_fifty_runs_are_merged_to_fifty(hass: HomeAssistant, mock_entry):
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: [G_NEW]})
    base = datetime(2026, 1, 1, tzinfo=UTC)
    spans = [(base + timedelta(hours=12 * k), base + timedelta(hours=12 * k + 3))
             for k in range(60)]

    await _entity_cycle(coordinator, _others(**{G_NEW: spans}))

    plan = coordinator._state["replacement_job"]["energy_plan"]
    assert len(plan["windows"]) == 50
    assert client.open_replacement.await_count == 12  # per cycle; the rest follows


@pytest.mark.asyncio
async def test_window_ends_at_the_last_complete_hour_and_the_tail_is_sent_again(
    hass: HomeAssistant, mock_entry
):
    """The new sensor stops at 07:20: the window ends at 07:00 (the 07:00 hour is not
    complete), so the stored rows after it are not removed; the bookmark steps back to
    the last full quarter and the regular upload sends the tail again."""
    client = _direction_client(E_SINCE)
    coordinator, starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: [G_NEW]})
    stop = datetime(2026, 10, 13, 7, 20, tzinfo=UTC)

    await _entity_cycle(coordinator, _others(**{
        G_NEW: [(E_NEW_FROM, stop)], "sensor.solar": E_OTHERS_FROM}))

    assert _opened_windows(client) == [(E_START, datetime(2026, 10, 13, 7, tzinfo=UTC))]
    assert max(r["ts"] for r in _staged(client)) == "2026-10-13T06:45:00+00:00"
    assert starts == [datetime(2026, 10, 13, 6, 45, tzinfo=UTC)]


@pytest.mark.asyncio
async def test_external_phase_without_states_is_done_at_once(
    hass: HomeAssistant, mock_entry
):
    """No recorded state for the new binary sensor: nothing to replace. After the energy
    phase the job finishes and the fingerprint is applied."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client,
        data_updates={CONF_SOLAR: ["sensor.solar_new"], CONF_EXTERNAL_CONTROL: EXTERNAL})

    await _entity_cycle(coordinator, _others(**{"sensor.solar_new": E_NEW_FROM}), points=[])

    assert [c.kwargs["fields"] for c in client.open_replacement.call_args_list] == [
        ["solar_kwh"]]
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


@pytest.mark.asyncio
async def test_external_read_errors_give_the_phase_up_after_three(
    hass: HomeAssistant, mock_entry
):
    """The state read fails every time: retried for two cycles, then given up, so the
    job finishes and a direction change queued meanwhile is not blocked for good."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client,
        data_updates={CONF_SOLAR: ["sensor.solar_new"], CONF_EXTERNAL_CONTROL: EXTERNAL})
    available = _others(**{"sensor.solar_new": E_NEW_FROM})
    failing = AsyncMock(side_effect=RuntimeError("recorder down"))

    async def cycle(now):
        coordinator._state["last_uploaded_ts"] = E_BOOKMARK
        with patch("custom_components.wolta.stats.async_fetch_states", new=failing):
            fetch = _fake_statistics(available)
            with (
                patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=now),
                patch("custom_components.wolta.coordinator.async_fetch_change",
                      side_effect=fetch),
            ):
                await coordinator._async_update_data()

    await cycle(E_NOW)
    job = coordinator._state["replacement_job"]
    assert job["phases_done"] == ["energy"]
    assert job["external_errors"] == 1

    mock_entry.data = {**mock_entry.data, CONF_INVERT_INTERVALS: [[None, None]],
                       CONF_INVERT_BATTERY: True}
    await cycle(E_NOW + timedelta(hours=6))
    assert coordinator._state["replacement_job"]["type"] == "entities"
    await cycle(E_NOW + timedelta(hours=12))
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()

    await cycle(E_NOW + timedelta(hours=18))
    kinds = [c.kwargs["kind"] for c in client.open_replacement.call_args_list]
    assert kinds == ["rows", "swap_battery"]


@pytest.mark.asyncio
async def test_done_window_guard_is_keyed_on_the_phase(hass: HomeAssistant, mock_entry):
    """A rows window committed in one phase is refused again in that phase, not in
    another."""
    client = _client()
    energy = {**ROWS_SPEC, "phase": "energy"}
    job = {**_job(energy), "done_windows": [[WS, WE, "energy"]]}
    coordinator, _ = await _coordinator(
        hass, mock_entry, client, job, energy, rows=REPLACEMENT_ROWS)

    await _cycle(coordinator)
    client.open_replacement.assert_not_called()

    external = {**ROWS_SPEC, "phase": "external", "fields": ["external_control"]}
    _register(coordinator, external, REPLACEMENT_ROWS)
    await _cycle(coordinator)
    client.open_replacement.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_replacement_whose_rows_cannot_be_built_is_dropped(
    hass: HomeAssistant, mock_entry
):
    """Resumed open, but its rows cannot be built again: deleted and planned anew at
    once instead of holding the upload until the server expires it."""
    client = _client()
    coordinator, _ = await _coordinator(
        hass, mock_entry, client, _job(ROWS_SPEC, OLD_ID), ROWS_SPEC, rows=None)

    await _cycle(coordinator)

    client.delete_replacement.assert_awaited_once_with(coordinator.token, OLD_ID)
    client.open_replacement.assert_not_called()
    assert coordinator._state["replacement_job"]["replacement"] is None
    client.put_data.assert_awaited()


# --- Round 3: a started job never waits for good; merging rules; re-plans ----------

from custom_components.wolta.coordinator import _runs  # noqa: E402

H = timedelta(hours=1)
T0 = datetime(2026, 3, 1, tzinfo=UTC)


def _hours(*spans) -> list[datetime]:
    out = []
    for start, n in spans:
        out += [start + H * k for k in range(n)]
    return out


def test_runs_merge_short_gaps_but_not_across_a_committed_window_or_the_split():
    hours = _hours((T0, 2), (T0 + 5 * H, 2))  # gap 3 h
    assert _runs(hours) == [(T0, T0 + 7 * H)]
    # A committed window inside the gap: kept apart.
    assert _runs(hours, committed=[(T0 + 3 * H, T0 + 4 * H)]) == [
        (T0, T0 + 2 * H), (T0 + 5 * H, T0 + 7 * H)]
    # The hourly/5-minute switch in (or at the edge of) the gap: kept apart.
    for split in (T0 + 2 * H, T0 + 3 * H, T0 + 5 * H):
        assert _runs(hours, split=split) == [(T0, T0 + 2 * H), (T0 + 5 * H, T0 + 7 * H)]
    # A long gap stays apart.
    assert _runs(_hours((T0, 1), (T0 + 8 * H, 1))) == [
        (T0, T0 + H), (T0 + 8 * H, T0 + 9 * H)]


def test_runs_cap_merges_the_smallest_joinable_gaps_only():
    hours = _hours(*[(T0 + 10 * H * k, 1) for k in range(55)])  # gaps of 9 h
    assert len(_runs(hours)) == 50
    # Every gap blocked by a committed window: the cap gives way.
    committed = [(T0 + 10 * H * k + 5 * H, T0 + 10 * H * k + 6 * H) for k in range(55)]
    assert len(_runs(hours, committed=committed)) == 55


def _started_job(coordinator, done_windows, plan=None) -> dict:
    job = coordinator._new_replacement_job("entities", coordinator._entities_fingerprint())
    job.update({"streams": {"grid_in": [G_NEW]}, "fields": ["grid_import_kwh"],
                "phases_done": [], "done_windows": done_windows})
    if plan is not None:
        job["energy_plan"] = plan
    return job


STALE_PLAN = {
    "read_from": "2025-10-01T00:00:00+00:00", "read_end": "2026-10-12T00:00:00+00:00",
    "split": "2026-10-04T00:00:00+00:00", "planned_at": "2026-10-12T00:00:00+00:00",
    "windows": [[E_START.isoformat(), "2026-10-12T00:00:00+00:00", None]],
}


@pytest.mark.asyncio
async def test_replan_after_twelve_hours_skips_committed_windows(
    hass: HomeAssistant, mock_entry
):
    """A plan older than 12 h is made anew for what is not committed: the new windows
    start after the committed one and never overlap it."""
    client = _direction_client(E_SINCE)
    mid = datetime(2026, 8, 1, tzinfo=UTC)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: [G_NEW]})
    coordinator._state["replacement_job"] = _started_job(
        coordinator, [[E_START.isoformat(), mid.isoformat(), "energy"]], STALE_PLAN)

    await _entity_cycle(coordinator, _others(**{G_NEW: E_NEW_FROM}))

    assert _opened_windows(client) == [(mid, E_END)]
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


@pytest.mark.asyncio
async def test_replan_without_windows_left_ends_the_phase(hass: HomeAssistant, mock_entry):
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: [G_NEW]})
    coordinator._state["replacement_job"] = _started_job(
        coordinator, [[E_START.isoformat(), E_END.isoformat(), "energy"]], STALE_PLAN)

    await _entity_cycle(coordinator, _others(**{G_NEW: E_NEW_FROM}))

    client.open_replacement.assert_not_called()
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


@pytest.mark.asyncio
async def test_started_job_ends_when_its_added_sensor_has_no_statistics(
    hass: HomeAssistant, mock_entry
):
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: [G_NEW]})
    job = _started_job(coordinator, [[E_START.isoformat(), "2026-08-01T00:00:00+00:00",
                                      "energy"]])
    with patch("custom_components.wolta.coordinator.async_fetch_change",
               side_effect=_fake_statistics(_others())):
        assert await coordinator._energy_next(
            job, E_NOW, {"data_since": E_SINCE}) == "phase_done"


@pytest.mark.asyncio
async def test_started_job_ends_after_four_cycles_without_a_plan(
    hass: HomeAssistant, mock_entry
):
    from custom_components.wolta.coordinator import WoltaCoordinator

    coordinator = WoltaCoordinator(hass, mock_entry)
    job = {"done_windows": [["a", "b", "energy"]]}
    assert [coordinator._plan_failed(job) for _ in range(4)] == [
        None, None, None, "phase_done"]
    assert coordinator._plan_failed({"done_windows": []}) is None  # idle: just waits


@pytest.mark.asyncio
async def test_reconfigure_mid_job_finishes_it_and_queues_a_new_one(
    hass: HomeAssistant, mock_entry
):
    """12 of 50 windows committed, then the added sensor is swapped for another: the job
    ends its phase and finishes with its target, a direction change queued meanwhile
    runs next, and then a new job goes from that target to the current selection."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: [G_NEW]})
    base = datetime(2026, 1, 1, tzinfo=UTC)
    spans = [(base + timedelta(hours=12 * k), base + timedelta(hours=12 * k + 3))
             for k in range(60)]
    available = _others(**{G_NEW: spans})

    async def cycle(now):
        coordinator._state["last_uploaded_ts"] = E_BOOKMARK
        await _entity_cycle(coordinator, available, now=now)

    await cycle(E_NOW)
    first_target = coordinator._state["replacement_job"]["target"]
    assert client.open_replacement.await_count == 12

    other = "sensor.grid_in_other"
    mock_entry.data = {**mock_entry.data, CONF_GRID_IN: [other],
                       CONF_INVERT_INTERVALS: [[None, None]], CONF_INVERT_BATTERY: True}
    coordinator._entity_map["grid_in"] = [other]

    await cycle(E_NOW + timedelta(hours=1))
    assert client.open_replacement.await_count == 12
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_entities"] == first_target

    await cycle(E_NOW + timedelta(hours=2))
    assert client.open_replacement.call_args.kwargs["kind"] == "swap_battery"

    await cycle(E_NOW + timedelta(hours=3))
    job = coordinator._state["replacement_job"]
    assert job["type"] == "entities"
    assert job["streams"] == {"grid_in": [other]}


# --- Round 4: the judged selection; parking a job that keeps failing ----------------

SOLAR_EXTRA = "sensor.solar_extra"


@pytest.mark.asyncio
async def test_sensor_added_alongside_drops_an_unstarted_replacement_and_retargets(
    hass: HomeAssistant, mock_entry
):
    """A replacement left open (its commit failed), and meanwhile a second solar sensor
    is added next to the new one: rows built now would judge other quarters complete,
    and the server removes every stored row in the window that has no staged row. So
    nothing of another composition is staged: the replacement is deleted, the job waits
    without pausing the upload, and the next cycle retargets it."""
    client = _direction_client(E_SINCE)
    client.commit_replacement.side_effect = WoltaApiError("down", status=503)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]})
    available = _others(**{"sensor.solar_new": E_NEW_FROM, SOLAR_EXTRA: E_NEW_FROM})

    await _entity_cycle(coordinator, available)
    job = coordinator._state["replacement_job"]
    assert job["judged"] == {"solar": ["sensor.solar_new"]}
    rid = job["replacement"]["id"]
    staged = client.stage_replacement_rows.await_count

    coordinator._entity_map["solar"] = ["sensor.solar_new", SOLAR_EXTRA]
    client.commit_replacement.side_effect = None
    client.put_data.reset_mock()
    await _entity_cycle(coordinator, available, now=E_NOW + timedelta(hours=1))

    assert client.stage_replacement_rows.await_count == staged
    client.delete_replacement.assert_awaited_with(coordinator.token, rid)
    client.open_replacement.assert_awaited_once()
    client.commit_replacement.assert_awaited_once()  # only the failed one of cycle 1
    job = coordinator._state["replacement_job"]
    assert job["replacement"] is None and not job["done_windows"]
    assert coordinator._state["applied_entities"] == OLD_FP
    client.put_data.assert_awaited()  # not paused

    await _entity_cycle(coordinator, available, now=E_NOW + timedelta(hours=2))
    assert client.open_replacement.await_count == 2
    assert client.open_replacement.await_args.args[1] != rid
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()
    assert json.loads(coordinator._state["applied_entities"])["solar"] == [
        "sensor.solar_new", SOLAR_EXTRA]


@pytest.mark.asyncio
async def test_started_job_ends_when_a_sensor_is_added_alongside(
    hass: HomeAssistant, mock_entry
):
    """12 windows committed, then a sensor is added next to the new one (not instead of
    it): the later windows would be built from another selection, so the phase ends with
    nothing more opened, and a new job goes from the job's target to the selection."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_GRID_IN: [G_NEW]})
    base = datetime(2026, 1, 1, tzinfo=UTC)
    spans = [(base + timedelta(hours=12 * k), base + timedelta(hours=12 * k + 3))
             for k in range(60)]
    extra = "sensor.grid_in_extra"
    available = _others(**{G_NEW: spans, extra: E_NEW_FROM})

    async def cycle(now):
        coordinator._state["last_uploaded_ts"] = E_BOOKMARK
        await _entity_cycle(coordinator, available, now=now)

    await cycle(E_NOW)
    first_target = coordinator._state["replacement_job"]["target"]
    assert client.open_replacement.await_count == 12

    coordinator._entity_map["grid_in"] = [G_NEW, extra]
    await cycle(E_NOW + timedelta(hours=1))
    assert client.open_replacement.await_count == 12
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["applied_entities"] == first_target

    await cycle(E_NOW + timedelta(hours=2))
    job = coordinator._state["replacement_job"]
    assert job["streams"] == {"grid_in": [extra]}
    assert job["judged"] == {"grid_in": sorted([G_NEW, extra])}


@pytest.mark.asyncio
async def test_selection_left_counts_the_battery_partner(hass: HomeAssistant, mock_entry):
    from custom_components.wolta.coordinator import WoltaCoordinator

    coordinator = WoltaCoordinator(hass, mock_entry)
    target = _fp(batt_in=["sensor.batt_in_new"])
    coordinator._entity_map = {k: list(v) for k, v in json.loads(target).items()}
    coordinator._external_entity = None
    streams = {"batt_in": ["sensor.batt_in_new"]}
    job = {"streams": streams, "target": target, "done_windows": [],
           "judged": coordinator._judged_selection(streams, target)}
    assert job["judged"] == {"batt_in": ["sensor.batt_in_new"],
                             "batt_out": ["sensor.batt_out"]}
    assert not coordinator._selection_left(job, "energy")
    coordinator._entity_map["grid_out"] = ["sensor.grid_out", "sensor.grid_out_2"]
    assert not coordinator._selection_left(job, "energy")  # not judged
    coordinator._entity_map["batt_out"] = ["sensor.batt_out", "sensor.batt_out_2"]
    assert coordinator._selection_left(job, "energy")
    # Unstarted: the phase waits for the retarget instead of ending.
    assert await coordinator._energy_next(job, E_NOW, {"data_since": E_SINCE}) is None
    assert "phases_done" not in job


@pytest.mark.asyncio
async def test_direction_refused_on_open_is_parked_and_the_sensor_change_runs(
    hass: HomeAssistant, mock_entry
):
    """A deterministic 422 on every open of the swap: after five cycles in a row the
    direction job is parked for 24 h, the pending sensor change runs the next cycle, and
    the direction job is queued again once the backoff has passed."""
    client = _direction_client(E_SINCE)

    async def open_(token, rid, **kwargs):
        if kwargs["kind"] == "swap_battery":
            raise WoltaApiError("refused", status=422)

    client.open_replacement = AsyncMock(side_effect=open_)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client,
        data_updates={CONF_SOLAR: ["sensor.solar_new"],
                      CONF_INVERT_INTERVALS: [[None, None]], CONF_INVERT_BATTERY: True})
    available = _others(**{"sensor.solar_new": E_NEW_FROM})

    async def cycle(hours):
        coordinator._state["last_uploaded_ts"] = E_BOOKMARK
        await _entity_cycle(coordinator, available, now=E_NOW + timedelta(hours=hours))

    for n in range(4):
        await cycle(n)
        assert coordinator._state["replacement_job"]["job_failures"] == n + 1
    await cycle(4)
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["job_backoff"] == {
        "direction": (E_NOW + timedelta(hours=28)).isoformat()}
    assert coordinator._state["applied_invert_intervals"] == []
    assert client.put_data.await_count == 5  # never paused

    await cycle(5)
    kinds = [c.kwargs["kind"] for c in client.open_replacement.call_args_list]
    assert kinds == ["swap_battery"] * 5 + ["rows"]
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()

    await cycle(27)
    assert client.open_replacement.await_count == 6  # still parked
    await cycle(28)
    assert client.open_replacement.await_args.kwargs["kind"] == "swap_battery"
    assert coordinator._state["replacement_job"]["type"] == "direction"
    assert "job_backoff" not in coordinator._state


@pytest.mark.asyncio
async def test_entity_job_whose_plan_cannot_be_built_is_parked(
    hass: HomeAssistant, mock_entry
):
    """The plan build raises every cycle: parked after five, a direction change runs
    meanwhile, and the sensor change is queued again after 24 h."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]})
    coordinator._energy_build = AsyncMock(side_effect=RuntimeError("cannot build"))
    available = _others(**{"sensor.solar_new": E_NEW_FROM})

    async def cycle(hours):
        coordinator._state["last_uploaded_ts"] = E_BOOKMARK
        await _entity_cycle(coordinator, available, now=E_NOW + timedelta(hours=hours))

    for n in range(5):
        await cycle(n)
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["job_backoff"] == {
        "entities": (E_NOW + timedelta(hours=28)).isoformat()}
    assert coordinator._state["applied_entities"] == OLD_FP
    client.open_replacement.assert_not_called()

    mock_entry.data = {**mock_entry.data, CONF_INVERT_INTERVALS: [[None, None]],
                       CONF_INVERT_BATTERY: True}
    await cycle(5)
    assert [c.kwargs["kind"] for c in client.open_replacement.call_args_list] == [
        "swap_battery"]
    assert coordinator._state["applied_invert_intervals"] == [[None, None]]
    assert "replacement_job" not in coordinator._state  # the sensor change still waits

    await cycle(28)
    job = coordinator._state["replacement_job"]
    assert job["type"] == "entities"
    assert job["job_failures"] == 1


@pytest.mark.asyncio
async def test_parked_direction_job_keeps_its_committed_windows(
    hass: HomeAssistant, mock_entry
):
    """A direction job parked after swapping one segment: that segment is folded into
    the applied intervals, so the job queued after the backoff never swaps it back."""
    client = _direction_client()
    job = {**_job(SWAP), "type": "direction", "target": [[A, None]],
           "done_windows": [[A, B]], "job_failures": 4}
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[A, None]], applied=[], job=job)
    coordinator._job_cycle["failure"] = "test"

    assert await coordinator._count_job_cycle(coordinator._state["replacement_job"], D_NOW)

    assert coordinator._state["applied_invert_intervals"] == [[A, B]]
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["job_backoff"] == {
        "direction": (D_NOW + timedelta(hours=24)).isoformat()}


@pytest.mark.asyncio
async def test_repeated_conflict_parks_the_job(hass: HomeAssistant, mock_entry):
    """Opened, but the commit conflicts every cycle: the cycle still counts as failed
    (the failure comes after the open), and the fifth parks the job."""
    client = _client()
    client.commit_replacement.side_effect = WoltaReplacementConflict("conflict")
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    for n in range(5):
        await _cycle(coordinator, NOW + timedelta(hours=n))

    assert client.open_replacement.await_count == 5
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["job_backoff"] == {
        "test": (NOW + timedelta(hours=28)).isoformat()}


@pytest.mark.asyncio
async def test_progress_resets_the_failure_count(hass: HomeAssistant, mock_entry):
    """Four refused opens, then one that opens (its commit fails with a 5xx, which is no
    job-level failure): the count starts over."""
    client = _client()
    client.open_replacement.side_effect = [WoltaApiError("no", status=422)] * 4 + [None] * 3
    client.commit_replacement.side_effect = WoltaApiError("down", status=503)
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    for n in range(4):
        await _cycle(coordinator, NOW + timedelta(hours=n))
    assert coordinator._state["replacement_job"]["job_failures"] == 4
    await _cycle(coordinator, NOW + timedelta(hours=4))
    job = coordinator._state["replacement_job"]
    assert "job_failures" not in job
    assert job["replacement"] is not None
    assert "job_backoff" not in coordinator._state


@pytest.mark.asyncio
async def test_job_with_an_abandoned_replacement_is_never_parked(
    hass: HomeAssistant, mock_entry
):
    """The replacement's status cannot be read (it may have been committed): however
    long the failures go on, the job keeps it and is never parked."""
    client = _client()
    client.get_replacement.side_effect = RuntimeError("unreadable")
    job = _job(SWAP, OLD_ID)
    job["replacement"]["abandoned"] = True
    job["job_failures"] = 4
    coordinator, _ = await _coordinator(hass, mock_entry, client, job, SWAP)

    for n in range(6):
        await _cycle(coordinator, NOW + timedelta(days=2 * n))

    job = coordinator._state["replacement_job"]
    assert job["replacement"]["id"] == OLD_ID
    assert job["replacement"]["abandoned"] is True
    assert job["job_failures"] >= _JOB_MAX_FAILED_CYCLES
    assert "job_backoff" not in coordinator._state
    client.open_replacement.assert_not_called()


# ---------------------------------------------------------------------------
# Lower bound: never older than one year before the integration was set up
# ---------------------------------------------------------------------------

from custom_components.wolta.const import CONF_CREATED_BY_HA  # noqa: E402

# A linked plant whose web/CSV history reaches three years back, set up 30 days ago.
OLD_SINCE = "2023-10-13T00:00:00+00:00"
L_FLOOR = datetime(2025, 9, 13, 9, tzinfo=UTC)  # floor_hour(created_at - 365 days)


@pytest.mark.asyncio
async def test_direction_on_a_linked_plant_stops_one_year_before_setup(
    hass: HomeAssistant, mock_entry
):
    """The integration never uploaded anything older than a year before it was set up:
    a swap of the whole history starts there, not at the server's data_since, and the
    web/CSV history before it is left as it is."""
    mock_entry.created_at = D_NOW - timedelta(days=30)
    mock_entry.data = {**mock_entry.data, CONF_CREATED_BY_HA: False}
    client = _direction_client(OLD_SINCE)
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[None, None]], applied=[])

    await _cycle(coordinator, D_NOW)

    assert _opened(client) == [("swap_battery", L_FLOOR, D_END, "direction")]
    assert coordinator._state["applied_invert_intervals"] == [[None, None]]


@pytest.mark.asyncio
@pytest.mark.parametrize("created_at", [None, datetime(2026, 9, 13, 9, 5)])
async def test_direction_without_a_usable_created_at_starts_at_data_since(
    hass: HomeAssistant, mock_entry, created_at
):
    """No created_at, or a naive one: the bound falls back to data_since."""
    mock_entry.created_at = created_at
    client = _direction_client(OLD_SINCE)
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[None, None]], applied=[])

    await _cycle(coordinator, D_NOW)

    assert _opened(client) == [
        ("swap_battery", datetime(2023, 10, 13, tzinfo=UTC), D_END, "direction")]


@pytest.mark.asyncio
async def test_direction_on_a_plant_created_by_home_assistant_is_unchanged(
    hass: HomeAssistant, mock_entry
):
    """A plant this integration created holds nothing older than its own backfill:
    the window starts at data_since as before."""
    mock_entry.created_at = D_NOW - timedelta(days=30)
    mock_entry.data = {**mock_entry.data, CONF_CREATED_BY_HA: True}
    client = _direction_client(D_SINCE)
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[None, None]], applied=[])

    await _cycle(coordinator, D_NOW)

    assert _opened(client) == [
        ("swap_battery", datetime(2025, 10, 1, tzinfo=UTC), D_END, "direction")]


@pytest.mark.asyncio
async def test_sensor_change_on_a_linked_plant_stops_one_year_before_setup(
    hass: HomeAssistant, mock_entry
):
    """The new sensor has statistics from long before the setup, and the server's
    history reaches three years back: the read and the window start one year before
    the setup, never at data_since."""
    mock_entry.created_at = E_NOW - timedelta(days=30)
    client = _direction_client(OLD_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client,
        data_updates={CONF_SOLAR: ["sensor.solar_new"], CONF_CREATED_BY_HA: False})

    fetch = await _entity_cycle(
        coordinator, _others(**{"sensor.solar_new": datetime(2024, 1, 1, tzinfo=UTC)}))

    assert _opened_windows(client) == [(L_FLOOR, E_END)]
    assert min(c[1] for c in fetch.calls) == L_FLOOR - timedelta(hours=1)
    assert _staged(client)[0]["ts"] == L_FLOOR.isoformat()
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


@pytest.mark.asyncio
async def test_external_flag_on_a_linked_plant_stops_one_year_before_setup(
    hass: HomeAssistant, mock_entry
):
    """The external-control phase reads the binary sensor's states from the same bound."""
    mock_entry.created_at = E_NOW - timedelta(days=30)
    client = _direction_client(OLD_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_EXTERNAL_CONTROL: EXTERNAL})
    old_points = [(datetime(2024, 1, 1, tzinfo=UTC), "off"), *E_POINTS]

    states = AsyncMock(return_value=old_points)
    await _entity_cycle_with_states(coordinator, states)

    assert states.await_args.args[2] == L_FLOOR
    assert _opened_windows(client)[0][0] == L_FLOOR


async def _entity_cycle_with_states(coordinator, states, now=E_NOW):
    fetch = _fake_statistics(_others())
    with (
        patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=now),
        patch("custom_components.wolta.coordinator.async_fetch_change", side_effect=fetch),
        patch("custom_components.wolta.stats.async_fetch_states", new=states),
    ):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# Upgrade from v0.44: the first cycle replaces nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_from_v044_replaces_nothing_and_uploads_as_before(
    hass: HomeAssistant, mock_entry
):
    """A v0.44 install: the entry has the boolean invert flag (on) and no intervals; the
    Store has applied_invert, the entity fingerprint and a bookmark. Setup migrates the
    flag to intervals on both sides, and the first full cycle makes no replacement call
    at all - no job, the regular PUT /data from the bookmark, the rows still inverted."""
    from custom_components.wolta.const import CONF_INVERT_BATTERY, CONF_INVERT_INTERVALS

    data = {**mock_entry.data, CONF_INVERT_BATTERY: True}
    data.pop(CONF_INVERT_INTERVALS, None)
    mock_entry.data = data
    bookmark = (NOW - timedelta(hours=1)).isoformat()
    client = _client()
    client.get_profile = AsyncMock(
        return_value={**BASE_PROFILE, "data_since": "2024-06-01T00:00:00+00:00"})
    coordinator = await _make_coordinator(hass, mock_entry, client, store_state={
        "applied_invert": True,
        "applied_entities": OLD_FP,
        "last_uploaded_ts": bookmark,
    })
    seen: list = []

    async def rows(start, now):
        seen.append((start, coordinator._applied_intervals()))
        return _make_rows(NOW)

    coordinator._incremental_rows = AsyncMock(side_effect=rows)
    coordinator._reconcile_coverage = AsyncMock()

    def update_entry(entry, *, data):
        entry.data = data
        return True

    with (
        patch.object(hass.config_entries, "async_update_entry", side_effect=update_entry),
        patch("custom_components.wolta.coordinator.async_track_time_interval"),
    ):
        await coordinator._async_setup()
        await _cycle(coordinator)

    # Migration done, on both sides, with nothing to correct.
    assert mock_entry.data[CONF_INVERT_INTERVALS] == [[None, None]]
    assert mock_entry.data[CONF_INVERT_BATTERY] is True
    assert coordinator._state["applied_invert_intervals"] == [[None, None]]
    assert coordinator._state["applied_entities"] == OLD_FP
    assert "replacement_job" not in coordinator._state
    # Not one replacement call.
    for call in (client.open_replacement, client.stage_replacement_rows,
                 client.commit_replacement, client.get_replacement,
                 client.delete_replacement):
        call.assert_not_called()
    # The regular upload from the bookmark, built with the inversion as before.
    assert seen == [(datetime.fromisoformat(bookmark), [(None, None)])]
    client.put_data.assert_awaited_once()
    assert coordinator._state["last_uploaded_ts"] != bookmark
    assert "pending_invert_recompute" not in coordinator._state


# ---------------------------------------------------------------------------
# A persistent server error (5xx, or an unreachable server) parks the job too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direction_failing_with_500_on_open_is_parked_after_twelve_cycles(
    hass: HomeAssistant, mock_entry
):
    """A deterministic 500 on every open of the swap: eleven full cycles in a row keep
    the job, the twelfth parks it for 24 h, and the pending sensor change runs the next
    cycle. The upload never pauses (nothing is open)."""
    from custom_components.wolta.coordinator import _JOB_MAX_SERVER_FAILED_CYCLES

    assert _JOB_MAX_SERVER_FAILED_CYCLES == 12
    client = _direction_client(E_SINCE)

    async def open_(token, rid, **kwargs):
        if kwargs["kind"] == "swap_battery":
            raise WoltaApiError("server error", status=500)

    client.open_replacement = AsyncMock(side_effect=open_)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client,
        data_updates={CONF_SOLAR: ["sensor.solar_new"],
                      CONF_INVERT_INTERVALS: [[None, None]], CONF_INVERT_BATTERY: True})
    available = _others(**{"sensor.solar_new": E_NEW_FROM})

    async def cycle(hours):
        coordinator._state["last_uploaded_ts"] = E_BOOKMARK
        await _entity_cycle(coordinator, available, now=E_NOW + timedelta(hours=hours))

    for n in range(11):
        await cycle(n)
        job = coordinator._state["replacement_job"]
        assert job["type"] == "direction"
        assert job["server_failures"] == n + 1
        assert "job_failures" not in job  # a 5xx is no refusal
    assert "job_backoff" not in coordinator._state

    await cycle(11)
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["job_backoff"] == {
        "direction": (E_NOW + timedelta(hours=35)).isoformat()}
    assert coordinator._state["applied_invert_intervals"] == []
    assert client.put_data.await_count == 12  # never paused

    await cycle(12)
    kinds = [c.kwargs["kind"] for c in client.open_replacement.call_args_list]
    assert kinds[:12] == ["swap_battery"] * 12
    assert kinds[12:] and set(kinds[12:]) == {"rows"}
    assert coordinator._state["applied_entities"] == coordinator._entities_fingerprint()


def _endless(coordinator) -> None:
    """A "test" type with one window per day, without end: a commit never finishes it."""

    async def next_replacement(job, now, profile):
        start = datetime.fromisoformat(WS) + timedelta(days=len(job["done_windows"]))
        return {**SWAP, "window_start": start.isoformat(),
                "window_end": (start + timedelta(days=1)).isoformat()}

    coordinator._replacement_types["test"] = _ReplacementType(
        next_replacement=next_replacement, on_committed=AsyncMock(),
        on_finished=AsyncMock())


@pytest.mark.asyncio
async def test_a_commit_resets_the_server_failure_count(hass: HomeAssistant, mock_entry):
    """Six cycles of 500 on open, then one that commits a window (the next window's
    open fails again in the same cycle): the count starts over, so eleven more failing
    cycles keep the job and only the twelfth parks it."""
    down = WoltaApiError("server error", status=500)
    client = _client()
    client.open_replacement.side_effect = [down] * 6 + [None] + [down] * 13
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)
    _endless(coordinator)

    for n in range(6):
        await _cycle(coordinator, NOW + timedelta(hours=n))
    assert coordinator._state["replacement_job"]["server_failures"] == 6

    await _cycle(coordinator, NOW + timedelta(hours=6))
    job = coordinator._state["replacement_job"]
    assert job["done_windows"] == [[WS, WE]]
    assert "server_failures" not in job

    for n in range(11):
        await _cycle(coordinator, NOW + timedelta(hours=7 + n))
    assert coordinator._state["replacement_job"]["server_failures"] == 11
    assert "job_backoff" not in coordinator._state

    await _cycle(coordinator, NOW + timedelta(hours=18))
    assert "replacement_job" not in coordinator._state
    assert coordinator._state["job_backoff"] == {
        "test": (NOW + timedelta(hours=42)).isoformat()}


@pytest.mark.asyncio
async def test_open_replacement_with_a_failing_commit_is_never_parked_while_held(
    hass: HomeAssistant, mock_entry
):
    """A commit that keeps failing with 503 counts every full cycle, but a job that
    holds its replacement is not parked: it is given up first (_replacement_failed)."""
    client = _client()
    client.commit_replacement.side_effect = WoltaApiError("down", status=503)
    job = _job(SWAP, OLD_ID)
    job["server_failures"] = 11
    coordinator, _ = await _coordinator(hass, mock_entry, client, job, SWAP)

    await _cycle(coordinator)

    job = coordinator._state["replacement_job"]
    assert job["server_failures"] == 12
    assert job["replacement"]["id"] == OLD_ID
    assert "job_backoff" not in coordinator._state


@pytest.mark.asyncio
async def test_fast_poll_cycles_do_not_count_server_errors(hass: HomeAssistant, mock_entry):
    from custom_components.wolta.coordinator import _FAST_POLL

    client = _client()
    client.commit_replacement.side_effect = WoltaApiError("down", status=503)
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP, OLD_ID), SWAP)
    coordinator.update_interval = _FAST_POLL

    await _cycle(coordinator)

    client.commit_replacement.assert_awaited_once()
    assert "server_failures" not in coordinator._state["replacement_job"]


@pytest.mark.asyncio
async def test_an_unreachable_server_counts_like_a_5xx(hass: HomeAssistant, mock_entry):
    """A transport error fails the cycle as before, and is counted toward parking."""
    import aiohttp
    from homeassistant.helpers.update_coordinator import UpdateFailed

    client = _client()
    client.open_replacement.side_effect = aiohttp.ClientConnectionError("unreachable")
    job = _job(SWAP)
    coordinator, _ = await _coordinator(hass, mock_entry, client, job, SWAP)

    with pytest.raises(UpdateFailed):
        await _cycle(coordinator)

    assert coordinator._state["replacement_job"]["server_failures"] == 1


# ---------------------------------------------------------------------------
# A full archive (429 on open) is shown as a repair until the job moves on
# ---------------------------------------------------------------------------

from homeassistant.helpers import issue_registry as ir  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.wolta.const import DOMAIN  # noqa: E402


def _waiting_issue(hass, mock_entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"replacement_waiting_{mock_entry.entry_id}")


@pytest.mark.asyncio
async def test_archive_full_raises_the_waiting_repair_and_a_commit_clears_it(
    hass: HomeAssistant, mock_entry
):
    """429 on the open of a swap: a non-fixable warning that names the retry time in
    local time and that a direction change waits. The commit after Retry-After clears
    it."""
    client = _direction_client()
    client.open_replacement.side_effect = WoltaRateLimitError(86400)
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[None, None]], applied=[])

    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=D_NOW):
        await coordinator._async_update_data()

    issue = _waiting_issue(hass, mock_entry)
    assert issue is not None
    assert issue.translation_key == "replacement_waiting_direction"
    assert issue.is_fixable is False
    assert issue.severity == ir.IssueSeverity.WARNING
    retry = dt_util.as_local(D_NOW + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    assert issue.translation_placeholders == {"retry_at": retry}

    client.open_replacement.side_effect = None
    later = D_NOW + timedelta(days=1, minutes=1)
    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=later):
        await coordinator._async_update_data()

    assert coordinator._state["applied_invert_intervals"] == [[None, None]]
    assert _waiting_issue(hass, mock_entry) is None


@pytest.mark.asyncio
async def test_waiting_repair_is_cleared_when_the_job_is_dropped(
    hass: HomeAssistant, mock_entry
):
    """A sensor change held back by a full archive: the repair says a sensor change
    waits, and goes away when the selection is set back and the job is dropped."""
    client = _direction_client(E_SINCE)
    client.open_replacement.side_effect = WoltaRateLimitError(3600)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]})
    available = _others(**{"sensor.solar_new": E_NEW_FROM})

    await _entity_cycle(coordinator, available)

    issue = _waiting_issue(hass, mock_entry)
    assert issue is not None
    assert issue.translation_key == "replacement_waiting_entities"

    coordinator._entity_map["solar"] = ["sensor.solar"]  # back to the applied selection
    await _entity_cycle(coordinator, available, now=E_NOW + timedelta(minutes=30))

    assert "replacement_job" not in coordinator._state
    assert _waiting_issue(hass, mock_entry) is None


@pytest.mark.asyncio
async def test_waiting_repair_is_cleared_when_the_job_is_parked(
    hass: HomeAssistant, mock_entry
):
    client = _client()
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)
    retry = NOW + timedelta(hours=1)
    coordinator._raise_waiting_issue(coordinator._state["replacement_job"], retry)
    assert _waiting_issue(hass, mock_entry) is not None

    await coordinator._park_job(coordinator._state["replacement_job"], NOW, "test", 5)

    assert _waiting_issue(hass, mock_entry) is None


@pytest.mark.asyncio
async def test_waiting_repair_is_cleared_in_view_only_mode(hass: HomeAssistant, mock_entry):
    client = _client()
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)
    coordinator._raise_waiting_issue(
        coordinator._state["replacement_job"], NOW + timedelta(hours=1))
    coordinator._view_only = True

    await _cycle(coordinator)

    assert _waiting_issue(hass, mock_entry) is None


# ---------------------------------------------------------------------------
# The bookmark after an energy window follows the rows actually staged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bookmark_follows_the_rows_staged_on_a_resume(hass: HomeAssistant, mock_entry):
    """The plan's last full quarter is 08:45. The commit fails and, before the resume, a
    sensor is added to grid export (a stream the solar change does not judge) that has
    stopped reporting at 07:20. The rows built again carry 08:45 as a partial row only,
    which the server skips where it holds no row; the bookmark must land on the last
    quarter staged with every field, not on 08:45."""
    client = _direction_client(E_SINCE)
    client.commit_replacement.side_effect = WoltaApiError("down", status=503)
    coordinator, starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]})
    available = _others(**{"sensor.solar_new": E_NEW_FROM})

    await _entity_cycle(coordinator, available)
    rep = coordinator._state["replacement_job"]["replacement"]
    planned = "2026-10-13T08:45:00+00:00"
    assert rep["last_full"] == planned
    assert starts == []  # paused while it is open

    coordinator._entity_map["grid_out"] = ["sensor.grid_out", "sensor.grid_out_2"]
    coordinator._entity_rows_cache = {}  # a restart
    client.stage_replacement_rows.reset_mock()
    client.commit_replacement.side_effect = None
    stop = datetime(2026, 10, 13, 7, 20, tzinfo=UTC)
    available = {**available, "sensor.grid_out_2": [(E_OTHERS_FROM, stop)]}
    await _entity_cycle(coordinator, available, now=E_NOW + timedelta(hours=1))

    rows = _staged(client)
    by_ts = {r["ts"]: r for r in rows}
    assert set(by_ts[planned]) == {"ts", "solar_kwh"}  # partial now
    full = max(r["ts"] for r in rows if len(r) == 7)
    assert full < planned
    assert full == "2026-10-13T07:00:00+00:00"
    assert starts == [datetime.fromisoformat(full)]


@pytest.mark.asyncio
async def test_waiting_repair_is_raised_again_after_a_restart(hass: HomeAssistant, mock_entry):
    """The repair is not persistent: after a restart during the wait, the held job raises
    it again with the same retry time."""
    import copy

    client = _direction_client()
    client.open_replacement.side_effect = WoltaRateLimitError(86400)
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[None, None]], applied=[])
    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=D_NOW):
        await coordinator._async_update_data()
    saved = copy.deepcopy(coordinator._state["replacement_job"])
    assert saved["waiting_retry"] == (D_NOW + timedelta(days=1)).isoformat()

    ir.async_delete_issue(hass, DOMAIN, f"replacement_waiting_{mock_entry.entry_id}")
    restarted, _seen = await _direction_coordinator(
        hass, mock_entry, _direction_client(), desired=[[None, None]], applied=[],
        job=saved)
    later = D_NOW + timedelta(hours=6)
    with patch("custom_components.wolta.coordinator.dt_util.utcnow", return_value=later):
        await restarted._async_update_data()

    issue = _waiting_issue(hass, mock_entry)
    assert issue is not None
    assert issue.translation_key == "replacement_waiting_direction"
    retry = dt_util.as_local(D_NOW + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    assert issue.translation_placeholders == {"retry_at": retry}


@pytest.mark.asyncio
async def test_waiting_repair_follows_a_retargeted_sensor_change(
    hass: HomeAssistant, mock_entry
):
    """A sensor change held back by a full archive is retargeted to another sensor: the
    new job keeps the wait, and the repair stays with the same retry time."""
    client = _direction_client(E_SINCE)
    client.open_replacement.side_effect = WoltaRateLimitError(3600)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client, data_updates={CONF_SOLAR: ["sensor.solar_new"]})
    available = _others(**{"sensor.solar_new": E_NEW_FROM})
    await _entity_cycle(coordinator, available)
    first = coordinator._state["replacement_job"]

    coordinator._entity_map["solar"] = ["sensor.solar_other"]
    await _entity_cycle(coordinator, available, now=E_NOW + timedelta(minutes=30))

    job = coordinator._state["replacement_job"]
    assert job is not first
    assert job["streams"] == {"solar": ["sensor.solar_other"]}
    assert job["retry_at"] == job["waiting_retry"] == first["retry_at"]
    issue = _waiting_issue(hass, mock_entry)
    assert issue is not None
    assert issue.translation_key == "replacement_waiting_entities"
    retry = dt_util.as_local(E_NOW + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    assert issue.translation_placeholders == {"retry_at": retry}


@pytest.mark.asyncio
async def test_waiting_repair_is_cleared_when_an_open_succeeds_after_the_wait(
    hass: HomeAssistant, mock_entry
):
    """After Retry-After the open goes through but the commit fails: the past retry time
    is no longer shown while the replacement is open."""
    client = _client()
    client.open_replacement.side_effect = WoltaRateLimitError(3600)
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)
    await _cycle(coordinator)
    assert _waiting_issue(hass, mock_entry) is not None

    client.open_replacement.side_effect = None
    client.commit_replacement.side_effect = WoltaApiError("down", status=503)
    await _cycle(coordinator, NOW + timedelta(hours=1, minutes=1))

    job = coordinator._state["replacement_job"]
    assert job["replacement"] is not None
    assert "waiting_retry" not in job
    assert _waiting_issue(hass, mock_entry) is None


@pytest.mark.asyncio
async def test_a_4xx_without_a_replacement_is_no_server_failure(
    hass: HomeAssistant, mock_entry
):
    """A refusal raised while no replacement is in flight (here from the hook) counts as a
    job-level failure only, never toward the server-error count."""
    client = _client()
    coordinator, _ = await _coordinator(hass, mock_entry, client, _job(SWAP), SWAP)

    async def refuse(job, now, profile):
        raise WoltaApiError("bad request", status=400)

    coordinator._replacement_types["test"] = _ReplacementType(
        next_replacement=refuse, on_committed=AsyncMock(), on_finished=AsyncMock())

    await _cycle(coordinator)

    job = coordinator._state["replacement_job"]
    assert job["job_failures"] == 1
    assert "server_failures" not in job


# ---------------------------------------------------------------------------
# Batteriets riktning: origin, override and the server's direction_verified
# ---------------------------------------------------------------------------

from custom_components.wolta import intervals  # noqa: E402
from custom_components.wolta.direction import read_verdict, requires_override  # noqa: E402
from custom_components.wolta.const import (  # noqa: E402
    CONF_DIRECTION_CHANGED_AT,
    CONF_DIRECTION_ORIGIN,
    CONF_DIRECTION_OVERRIDE,
)

CHANGED_AT = "2026-10-13T08:00:00+00:00"
VERIFIED = {"error": "direction_verified", "ok_days": 10, "reversed_days": 0,
            "as_of": "2026-10-13T04:45:10+00:00"}


def _writes_entry(hass):
    """The mock entry is not registered: apply the coordinator's entry writes to it."""
    return patch.object(hass.config_entries, "async_update_entry",
                        side_effect=lambda e, **kw: setattr(e, "data", kw["data"]) or True)


def _change(mock_entry, origin=None, override=None) -> None:
    """What direction.apply_direction_target leaves in entry.data next to the intervals."""
    data = {**mock_entry.data, CONF_DIRECTION_CHANGED_AT: CHANGED_AT}
    if origin is not None:
        data[CONF_DIRECTION_ORIGIN] = origin
    if override is not None:
        data[CONF_DIRECTION_OVERRIDE] = override
    mock_entry.data = data


def _refused_issue(hass, mock_entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"battery_direction_refused_{mock_entry.entry_id}")


@pytest.mark.asyncio
async def test_manual_origin_sends_direction_manual_and_override(
    hass: HomeAssistant, mock_entry
):
    """A change the owner made by hand, confirmed against the verdict: the job carries
    origin and override from entry.data, and the swap is opened as direction_manual with
    override."""
    client = _direction_client()
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[])
    _change(mock_entry, "manual", True)
    at_commit: list = []

    async def commit(token, rid, expected):
        job = coordinator._state["replacement_job"]
        at_commit.append((job["origin"], job["override"], job["replacement"]["override"]))
        return dict(COMMIT_RESULT)

    client.commit_replacement.side_effect = commit

    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)

    assert _opened(client) == [
        ("swap_battery", datetime(2026, 9, 28, 13, tzinfo=UTC), D_END, "direction_manual")]
    assert client.open_replacement.call_args.kwargs["override"] is True
    assert at_commit == [("manual", True, True)]
    assert coordinator._state["applied_invert_intervals"] == [[FROM, None]]


@pytest.mark.asyncio
async def test_repair_origin_sends_direction_without_override(
    hass: HomeAssistant, mock_entry
):
    client = _direction_client()
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[])
    _change(mock_entry, "repair", False)
    at_commit: list = []

    async def commit(token, rid, expected):
        job = coordinator._state["replacement_job"]
        at_commit.append((job["origin"], job["override"], "override" in job["replacement"]))
        return dict(COMMIT_RESULT)

    client.commit_replacement.side_effect = commit

    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)

    assert _opened(client) == [
        ("swap_battery", datetime(2026, 9, 28, 13, tzinfo=UTC), D_END, "direction")]
    assert client.open_replacement.call_args.kwargs.get("override", False) is False
    assert at_commit == [("repair", False, False)]


@pytest.mark.asyncio
async def test_legacy_job_without_origin_sends_direction(hass: HomeAssistant, mock_entry):
    """A direction job saved before the upgrade has neither origin nor override: it is
    swapped as a repair, without override."""
    client = _direction_client()
    job = {"type": "direction", "target": [[FROM, None]], "replacement": None,
           "done_windows": [], "retry_at": None}
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[], job=job)

    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)

    assert _opened(client) == [
        ("swap_battery", datetime(2026, 9, 28, 13, tzinfo=UTC), D_END, "direction")]
    assert client.open_replacement.call_args.kwargs.get("override", False) is False
    assert coordinator._state["applied_invert_intervals"] == [[FROM, None]]


@pytest.mark.asyncio
async def test_parked_manual_job_keeps_origin_and_override(hass: HomeAssistant, mock_entry):
    """A manual job refused on every open is parked; the job queued again after the
    backoff still carries the owner's origin and override (they live in entry.data, not
    only in the parked job)."""
    client = _direction_client()
    client.open_replacement.side_effect = WoltaApiError("refused", status=422)
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[None, None]], applied=[])
    _change(mock_entry, "manual", True)

    with _writes_entry(hass):
        for n in range(_JOB_MAX_FAILED_CYCLES):
            await _cycle(coordinator, D_NOW + timedelta(hours=n))
        assert "replacement_job" not in coordinator._state
        assert "direction" in coordinator._state["job_backoff"]
        assert mock_entry.data[CONF_DIRECTION_ORIGIN] == "manual"
        assert mock_entry.data[CONF_DIRECTION_OVERRIDE] is True

        await _cycle(coordinator, D_NOW + timedelta(hours=_JOB_MAX_FAILED_CYCLES + 24))

    job = coordinator._state["replacement_job"]
    assert (job["origin"], job["override"]) == ("manual", True)
    assert client.open_replacement.await_count == _JOB_MAX_FAILED_CYCLES + 1
    assert client.open_replacement.call_args.kwargs["reason"] == "direction_manual"
    assert client.open_replacement.call_args.kwargs["override"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("bookmark", [D_BOOKMARK, None])
async def test_origin_and_override_cleared_when_applied_reaches_desired(
    hass: HomeAssistant, mock_entry, bookmark
):
    """Once the stored rows carry the desired direction - after the final swap, or at once
    with nothing stored - origin and override are removed, so the override can never ride
    along with a later change. The change stamp stays."""
    client = _direction_client()
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[], bookmark=bookmark)
    _change(mock_entry, "manual", True)

    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)

    assert coordinator._state["applied_invert_intervals"] == [[FROM, None]]
    assert "replacement_job" not in coordinator._state
    assert CONF_DIRECTION_ORIGIN not in mock_entry.data
    assert CONF_DIRECTION_OVERRIDE not in mock_entry.data
    assert mock_entry.data[CONF_DIRECTION_CHANGED_AT] == CHANGED_AT
    assert mock_entry.data[CONF_INVERT_INTERVALS] == [[FROM, None]]


@pytest.mark.asyncio
async def test_direction_verified_resets_desired_and_raises_issue(
    hass: HomeAssistant, mock_entry
):
    """409 direction_verified on the open: a final no. The window already swapped stays
    (folded into applied), desired becomes applied in one entry write that also drops
    origin and override, the job is gone without a failure count, backoff or pause, and a
    non-fixable warning tells the owner. The next cycle opens nothing."""
    client = _direction_client()
    client.open_replacement.side_effect = WoltaReplacementConflict(
        "direction_verified", detail=dict(VERIFIED))
    job = {"type": "direction", "target": [[None, None]], "replacement": None,
           "done_windows": [[A, B]], "retry_at": None, "origin": "manual",
           "override": False, "job_failures": 2}
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[None, None]], applied=[], job=job)
    _change(mock_entry, "manual", False)

    with _writes_entry(hass) as upd:
        await _cycle(coordinator, D_NOW)

    assert client.open_replacement.await_count == 1
    assert "replacement_job" not in coordinator._state
    assert "job_backoff" not in coordinator._state
    assert coordinator._state["applied_invert_intervals"] == [[A, B]]
    assert mock_entry.data[CONF_INVERT_INTERVALS] == [[A, B]]
    assert mock_entry.data[CONF_INVERT_BATTERY] is False
    assert CONF_DIRECTION_ORIGIN not in mock_entry.data
    assert CONF_DIRECTION_OVERRIDE not in mock_entry.data
    # The stamp is set to the second before the refused verdict, also with a window
    # already swapped: the 409 shows the server holds that verdict current, so a second try
    # by hand must be confirmed against it.
    assert mock_entry.data[CONF_DIRECTION_CHANGED_AT] == "2026-10-13T04:45:09+00:00"
    assert requires_override(_verdict(mock_entry)) is True
    assert upd.call_count == 1
    coordinator._store.async_save.assert_awaited()
    client.put_data.assert_awaited()  # never paused

    issue = _refused_issue(hass, mock_entry)
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.is_persistent is True
    assert issue.data == {"entry_id": mock_entry.entry_id, "as_of": VERIFIED["as_of"]}
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_key == "battery_direction_refused"
    as_of = dt_util.as_local(datetime(2026, 10, 13, 4, 45, 10, tzinfo=UTC))
    assert issue.translation_placeholders == {
        "ok_days": "10", "reversed_days": "0", "as_of": as_of.strftime("%Y-%m-%d %H:%M")}

    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW + timedelta(hours=6))
    assert client.open_replacement.await_count == 1
    assert "replacement_job" not in coordinator._state


@pytest.mark.asyncio
async def test_direction_verified_without_detail_says_dash(hass: HomeAssistant, mock_entry):
    client = _direction_client()
    client.open_replacement.side_effect = WoltaReplacementConflict("direction_verified")
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[])
    _change(mock_entry, "manual", False)

    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)

    assert mock_entry.data[CONF_INVERT_INTERVALS] == []
    assert _refused_issue(hass, mock_entry).translation_placeholders == {
        "ok_days": "-", "reversed_days": "-", "as_of": "-"}
    # No verdict time, no earlier change, nothing swapped: the stamp goes, and every
    # verdict counts as current.
    assert CONF_DIRECTION_CHANGED_AT not in mock_entry.data


def _verdict(mock_entry):
    return read_verdict(
        {"battery_direction": {"state": "verified", "checked_days": 10, "reversed_days": 0,
                               "as_of": VERIFIED["as_of"]}},
        mock_entry.data.get(CONF_DIRECTION_CHANGED_AT))


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_at", [
    # The refused change, made after the verdict was counted.
    CHANGED_AT,
    # A change from before the verdict.
    "2026-10-12T06:00:00+00:00",
], ids=["changed_after_verdict", "changed_before_verdict"])
async def test_direction_verified_sets_the_change_stamp_back(
    hass: HomeAssistant, mock_entry, changed_at
):
    """The stamp becomes the second before the refused verdict, whatever it was: the 409
    shows the server holds that verdict current, and a later verdict is newer anyway."""
    client = _direction_client()
    client.open_replacement.side_effect = WoltaReplacementConflict(
        "direction_verified", detail=dict(VERIFIED))
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[])
    _change(mock_entry, "manual", False)
    mock_entry.data = {**mock_entry.data, CONF_DIRECTION_CHANGED_AT: changed_at}

    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)

    assert mock_entry.data[CONF_INVERT_INTERVALS] == []
    assert mock_entry.data[CONF_DIRECTION_CHANGED_AT] == "2026-10-13T04:45:09+00:00"
    assert requires_override(_verdict(mock_entry)) is True


@pytest.mark.asyncio
async def test_refusal_after_swapped_windows_gates_the_next_confirmation(
    hass: HomeAssistant, mock_entry
):
    """A change made the same morning after the verdict was counted, refused after one
    window was already swapped: the next confirmation under Battery direction shows the
    override box, so the owner can do what the refusal issue says."""
    from types import SimpleNamespace
    from homeassistant.data_entry_flow import FlowResultType
    from tests.test_config_flow import _make_mock_entry

    client = _direction_client()
    client.open_replacement.side_effect = WoltaReplacementConflict(
        "direction_verified", detail=dict(VERIFIED))
    job = {"type": "direction", "target": [[None, None]], "replacement": None,
           "done_windows": [[A, B]], "retry_at": None, "origin": "manual",
           "override": False}
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[None, None]], applied=[], job=job)
    _change(mock_entry, "manual", False)  # 08:00, after the verdict's 04:45
    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)
    assert _refused_issue(hass, mock_entry) is not None
    assert coordinator.direction_change_pending() is False

    entry = _make_mock_entry(hass, dict(mock_entry.data))
    coordinator.data = SimpleNamespace(results={"betyg": {"battery_direction": {
        "state": "verified", "form": "all", "checked_days": 10, "reversed_days": 0,
        "as_of": VERIFIED["as_of"]}}})
    entry.runtime_data = coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "battery_direction"})
    assert result["type"] == FlowResultType.MENU, result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "direction_flip_all"})
    assert result["type"] == FlowResultType.FORM, result
    assert result["step_id"] == "direction_confirm"
    assert any(str(k) == "direction_override" for k in result["data_schema"].schema)
    assert result["description_placeholders"]["gate"]


def _verdict_betyg(state: str, as_of: str, **block) -> dict:
    return {"battery_direction": {"state": state, "form": "all", "checked_days": 12,
                                  "reversed_days": 12 if state == "reversed" else 0,
                                  "as_of": as_of, **block}}


@pytest.mark.asyncio
@pytest.mark.parametrize("betyg,kept", [
    # The verdict the server refused with, read again: it still stands.
    (_verdict_betyg("verified", VERIFIED["as_of"]), True),
    (_verdict_betyg("ok", "2026-10-12T04:45:10+00:00"), True),
    # A recount after the refusal: what the owner was told may no longer hold.
    (_verdict_betyg("verified", "2026-10-14T04:45:10+00:00"), False),
    (_verdict_betyg("unknown", "2026-10-14T04:45:10+00:00"), False),
    # The fixable repair is raised (here from the refused verdict's own as_of): the two
    # must never contradict each other.
    (_verdict_betyg("reversed", VERIFIED["as_of"],
                    swap_intervals=[{"from": None, "to": None}]), False),
])
async def test_refused_issue_is_cleared_by_a_newer_verdict_or_the_repair(
    hass: HomeAssistant, mock_entry, betyg, kept
):
    client = _direction_client()
    client.open_replacement.side_effect = WoltaReplacementConflict(
        "direction_verified", detail=dict(VERIFIED))
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[])
    _change(mock_entry, "manual", False)
    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)
    assert _refused_issue(hass, mock_entry) is not None

    with _writes_entry(hass):
        coordinator._evaluate_battery_direction_issue(betyg)

    assert (_refused_issue(hass, mock_entry) is not None) is kept


@pytest.mark.asyncio
async def test_refused_issue_without_as_of_waits_for_a_verdict_after_it(
    hass: HomeAssistant, mock_entry
):
    """A refusal without as_of: a verdict counted after the issue was raised clears it,
    one counted before does not."""
    client = _direction_client()
    client.open_replacement.side_effect = WoltaReplacementConflict("direction_verified")
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[])
    _change(mock_entry, "manual", False)
    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)
    created = _refused_issue(hass, mock_entry).created

    with _writes_entry(hass):
        coordinator._evaluate_battery_direction_issue(
            _verdict_betyg("verified", (created - timedelta(hours=1)).isoformat()))
        assert _refused_issue(hass, mock_entry) is not None
        coordinator._evaluate_battery_direction_issue(
            _verdict_betyg("verified", (created + timedelta(hours=1)).isoformat()))
    assert _refused_issue(hass, mock_entry) is None


@pytest.mark.asyncio
async def test_other_conflict_on_open_still_counts_a_failure(hass: HomeAssistant, mock_entry):
    """Only direction_verified is a final no; any other 409 code on the open is counted as
    before and keeps the job."""
    client = _direction_client()
    client.open_replacement.side_effect = WoltaReplacementConflict("conflict")
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[[FROM, None]], applied=[])
    _change(mock_entry, "manual", False)

    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)

    job = coordinator._state["replacement_job"]
    assert job["job_failures"] == 1
    assert mock_entry.data[CONF_INVERT_INTERVALS] == [[FROM, None]]
    assert mock_entry.data[CONF_DIRECTION_ORIGIN] == "manual"
    assert _refused_issue(hass, mock_entry) is None


async def _next_windows(coordinator, target) -> list[tuple[datetime, datetime]]:
    """Every window a direction job towards `target` would swap now, as the runner asks
    _direction_next for them one by one."""
    job = coordinator._new_replacement_job("direction", intervals.dump(target))
    windows: list = []
    profile = {"data_since": D_SINCE}
    while isinstance(spec := await coordinator._direction_next(job, D_NOW, profile), dict):
        windows.append((datetime.fromisoformat(spec["window_start"]),
                        datetime.fromisoformat(spec["window_end"])))
        job["done_windows"].append([spec["window_start"], spec["window_end"]])
    return windows


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [
    [],
    [(None, None)],
    [(datetime.fromisoformat(FROM), None)],
    [(None, datetime.fromisoformat(A))],
    [(None, datetime.fromisoformat(A)), (datetime.fromisoformat(B), None)],
])
async def test_direction_effect_matches_direction_next(
    hass: HomeAssistant, mock_entry, target
):
    """The preview for a change is exactly what the direction job would then swap, read
    with the data_since of the last profile the coordinator fetched."""
    applied = [[None, A], [B, None]]
    client = _direction_client()
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=applied, applied=applied)
    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)  # nothing to swap: just the profile read
    coordinator._state["last_uploaded_ts"] = D_BOOKMARK

    effect = coordinator.direction_effect(target)

    assert effect == await _next_windows(coordinator, target)
    if target == []:
        assert effect == [(datetime(2025, 10, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
                          (datetime(2026, 6, 1, tzinfo=UTC), D_END)]


@pytest.mark.asyncio
async def test_direction_effect_none_without_bookmark(hass: HomeAssistant, mock_entry):
    """None while the bookmark or the server's data_since is unknown, and when the server
    said it holds no rows although this client has a bookmark (a value read before the
    first upload went through)."""
    client = _direction_client()
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[], applied=[])
    target = [(None, None)]

    assert coordinator.direction_effect(target) is None  # no profile read yet

    await coordinator.async_check_profile_sync()
    assert coordinator.direction_effect(target) == [(datetime(2025, 10, 1, tzinfo=UTC), D_END)]

    coordinator._state.pop("last_uploaded_ts")
    assert coordinator.direction_effect(target) is None

    coordinator._state["last_uploaded_ts"] = D_BOOKMARK
    client.get_profile.return_value = {**BASE_PROFILE, "data_since": None}
    await coordinator.async_check_profile_sync()
    assert coordinator.direction_effect(target) is None

    client.get_profile.return_value = dict(BASE_PROFILE)  # an older server: no data_since
    await coordinator.async_check_profile_sync()
    assert coordinator.direction_effect(target) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("put_response", [{"data_since": D_SINCE}, {}, None],
                         ids=["put_with_data_since", "put_without", "put_no_body"])
async def test_first_upload_makes_no_rows_unknown_or_takes_the_put_data_since(
    hass: HomeAssistant, mock_entry, put_response
):
    """A new entry: its first profile GET says the server holds no rows, then the first
    upload goes through. "Nothing stored changes" is no longer true: the preview takes the
    data_since of the PUT response, or is unknown (None / MISSING) until the next profile
    GET brings it, so neither Battery direction nor a Reconfigure role swap goes ahead on
    the old value."""
    from custom_components.wolta import coverage
    client = _direction_client(data_since=None)
    client.put_data.return_value = put_response
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[], applied=[], bookmark=None)
    target = [(None, None)]

    await coordinator.async_check_profile_sync()
    assert coordinator.has_uploaded() is False
    assert coordinator.replacement_floor() is None  # nothing uploaded: nothing rebuilt
    with _writes_entry(hass):
        await _cycle(coordinator, D_NOW)
    client.put_data.assert_awaited()
    assert coordinator.has_uploaded() is True

    effect = coordinator.direction_effect(target)
    if put_response and "data_since" in put_response:
        floor = coordinator._replacement_floor(datetime.fromisoformat(D_SINCE))
        assert coordinator.replacement_floor() == floor
        assert effect and effect[0][0] == floor
    else:
        assert effect is None
        assert coordinator.replacement_floor() is coverage.MISSING

    # The next profile GET brings the real value.
    client.get_profile.return_value = {**BASE_PROFILE, "data_since": D_SINCE}
    await coordinator.async_check_profile_sync()
    assert coordinator.direction_effect(target)
    assert coordinator.replacement_floor() == coordinator._replacement_floor(
        datetime.fromisoformat(D_SINCE))


@pytest.mark.asyncio
async def test_replacement_floor_known_only_with_data_since(hass: HomeAssistant, mock_entry):
    """The floor Reconfigure names before a role swap, as the entity job plans it
    (_plan_since): _replacement_floor of the latest profile's data_since; MISSING while that
    is unknown, and when the server said it holds no rows although this client has uploaded;
    None when it holds none and nothing is uploaded (nothing is rebuilt)."""
    from custom_components.wolta import coverage
    client = _direction_client()
    coordinator, _seen = await _direction_coordinator(
        hass, mock_entry, client, desired=[], applied=[])
    assert coordinator.replacement_floor() is coverage.MISSING  # no profile read yet

    await coordinator.async_check_profile_sync()
    assert coordinator.replacement_floor() == coordinator._replacement_floor(
        datetime.fromisoformat(D_SINCE))

    client.get_profile.return_value = {**BASE_PROFILE, "data_since": None}
    await coordinator.async_check_profile_sync()
    assert coordinator.replacement_floor() is coverage.MISSING  # a bookmark is held
    coordinator._state.pop("last_uploaded_ts")
    assert coordinator.replacement_floor() is None
    coordinator._state["last_uploaded_ts"] = D_BOOKMARK

    client.get_profile.return_value = dict(BASE_PROFILE)  # an older server: no data_since
    await coordinator.async_check_profile_sync()
    assert coordinator.replacement_floor() is coverage.MISSING


@pytest.mark.asyncio
async def test_role_swap_entity_job_rebuilds_from_floor(hass: HomeAssistant, mock_entry):
    """Characterization: swapping the charge and discharge sensors under Reconfigure is a
    sensor change of both battery streams. The entity job rebuilds both battery fields
    from the replacement floor - the whole stored history this client uploaded - not just
    from some recent date."""
    client = _direction_client(E_SINCE)
    coordinator, _starts = await _entity_coordinator(
        hass, mock_entry, client,
        data_updates={CONF_BATT_IN: ["sensor.batt_out"], CONF_BATT_OUT: ["sensor.batt_in"]})
    long_ago = datetime(2024, 1, 1, tzinfo=UTC)

    await _entity_cycle(coordinator, {
        "sensor.batt_in": long_ago, "sensor.batt_out": long_ago,
        "sensor.grid_in": long_ago, "sensor.grid_out": long_ago, "sensor.solar": long_ago,
    })

    floor = coordinator._replacement_floor(datetime.fromisoformat(E_SINCE))
    first = client.open_replacement.call_args_list[0].kwargs
    assert first["kind"] == "rows"
    assert first["fields"] == ["batt_charged_kwh", "batt_discharged_kwh"]
    assert first["window_start"] == floor
