"""Diagnostics support for Wolta (profile token and plant id redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_INVERT_INTERVALS, CONF_LINK_TOKEN, CONF_PLANT_ID, CONF_TOKEN

# CONF_PLANT_ID is redacted alongside the token: it is the key the backend uses to recognise
# this plant, so anyone holding it can re-onboard as this plant and take over its row. Users
# routinely attach diagnostics dumps to GitHub issues and forum posts.
# CONF_LINK_TOKEN (spec 2026-08-24) is a lesser-privileged credential than the owner token –
# read + scoped economy/tariff writes, never delete/mint/share – but still live and worth
# keeping out of a shared dump.
TO_REDACT = [CONF_TOKEN, CONF_PLANT_ID, CONF_LINK_TOKEN]


def _replacement_summary(entry: ConfigEntry, state: dict[str, Any]) -> dict[str, Any]:
    """Where the server-side correction of stored history stands.

    Desired battery-direction intervals (entry data) against the applied ones (what the
    stored rows were built with), the running job's type, phase, replacement kind/window/
    fields, committed-window count and retry time, and any per-type backoff. Only plain
    values the user could read in the dump anyway; replacement ids are random per attempt
    and carry no credential.
    """
    job = state.get("replacement_job")
    job_info: dict[str, Any] | None = None
    if isinstance(job, dict):
        rep = job.get("replacement")
        rep_info: dict[str, Any] | None = None
        if isinstance(rep, dict):
            rep_info = {
                key: rep.get(key)
                for key in ("id", "kind", "phase", "window_start", "window_end", "fields",
                            "reason")
            }
        done = job.get("done_windows")
        job_info = {
            "type": job.get("type"),
            "replacement": rep_info,
            "done_windows": len(done) if isinstance(done, list) else 0,
            "retry_at": job.get("retry_at"),
            "failures": job.get("failures"),
            "job_failures": job.get("job_failures"),
            "plan_failures": job.get("plan_failures"),
        }
    backoff = state.get("job_backoff")
    return {
        "desired_invert_intervals": entry.data.get(CONF_INVERT_INTERVALS),
        "applied_invert_intervals": state.get("applied_invert_intervals"),
        "job": job_info,
        "job_backoff": dict(backoff) if isinstance(backoff, dict) else None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (token redacted)."""
    coordinator = getattr(entry, "runtime_data", None)
    state: dict[str, Any] = dict(getattr(coordinator, "_state", {}) or {})
    data = getattr(coordinator, "data", None)
    # Quarters the latest regular read dropped as incomplete, and the sensors behind it.
    completeness = getattr(coordinator, "_last_completeness", None)
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "coordinator_state": state,
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "results": getattr(data, "results", None),
        "completeness": dict(completeness) if isinstance(completeness, dict) else None,
        "replacement": _replacement_summary(entry, state),
    }
