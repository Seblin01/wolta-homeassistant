"""The control-system picker's options come from the server (GET /control-systems, label_en)
with const.CONTROL_SYSTEMS as the fallback, sorted alphabetically with Other last.

Why from the server: a control system added on the backend (2026-10-29: 1KOMMA5°, Custom-built,
Darkstar, Solis) used to need a HACS release before anyone could pick it in Home Assistant, and
every installed client older than that release kept the old list. The fallback keeps the picker
working when the request fails or an older server answers without label_en."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.wolta.const import CONTROL_SYSTEMS
from custom_components.wolta.control_system_prefill import (
    async_control_system_choices,
    control_system_selector,
    fallback_choices,
    options_from_server,
    sorted_options,
)


def _row(cs_id, label_en, domains=()):
    return {"id": cs_id, "label": label_en, "label_en": label_en, "ha_domains": list(domains)}


SERVER = [
    _row("tibber", "Tibber", ["tibber"]),
    _row("brandnew", "Brand New", ["brandnew_battery"]),
    _row("custom", "Custom-built (Home Assistant, script)"),
    _row("other", "Other"),
    _row("1komma5", "1KOMMA5°"),
]


def _client(rows):
    client = MagicMock()
    client.get_control_systems = AsyncMock(return_value=rows)
    return client


def test_const_list_is_sorted_with_other_last():
    labels = [label for _, label in CONTROL_SYSTEMS]
    assert labels[-1] == "Other"
    assert labels[:-1] == sorted(labels[:-1], key=str.casefold)
    assert CONTROL_SYSTEMS == sorted_options(CONTROL_SYSTEMS)


def test_const_list_has_the_2026_10_29_systems():
    for pair in [("1komma5", "1KOMMA5°"), ("custom", "Custom-built (Home Assistant, script)"),
                 ("darkstar", "Darkstar"), ("solis", "Solis")]:
        assert pair in CONTROL_SYSTEMS
    assert len(CONTROL_SYSTEMS) == 20


def test_options_from_server_sorts_by_english_label_other_last():
    assert [c for c, _ in options_from_server(SERVER)] == [
        "1komma5", "brandnew", "custom", "tibber", "other"]


@pytest.mark.parametrize("rows", [
    None, [], "x",
    # An older server: no label_en on the rows.
    [{"id": "tibber", "label": "Tibber", "ha_domains": []},
     {"id": "other", "label": "Annat", "ha_domains": []}],
    # One bad row poisons the list: a half list would hide systems the owner may have.
    [_row("tibber", "Tibber"), {"id": "other", "label_en": "  "}],
    [_row("tibber", "Tibber"), _row("tibber", "Tibber"), _row("other", "Other")],
    # Without Other there is no way to name an unlisted system.
    [_row("tibber", "Tibber")],
])
def test_options_from_server_refuses_an_incomplete_answer(rows):
    assert options_from_server(rows) is None


@pytest.mark.asyncio
async def test_choices_take_the_servers_list_and_suggestion():
    choices = await async_control_system_choices(_client(SERVER), ["brandnew_battery"])
    assert [c for c, _ in choices.options][-1] == "other"
    assert ("brandnew", "Brand New") in choices.options
    # A server-only id is a valid default now that it is one of the options.
    assert choices.suggestion == "brandnew"


@pytest.mark.asyncio
async def test_choices_fall_back_to_const_on_an_older_server():
    rows = [{"id": "brandnew", "label": "Brand New", "ha_domains": ["brandnew_battery"]}]
    choices = await async_control_system_choices(_client(rows), ["brandnew_battery"])
    assert choices.options == CONTROL_SYSTEMS
    # Not one of the fallback's options -> no default (HA would reject it).
    assert choices.suggestion is None


def test_fallback_choices():
    choices = fallback_choices()
    assert choices.options == CONTROL_SYSTEMS and choices.suggestion is None


def test_selector_uses_the_given_options():
    selector = control_system_selector([("tibber", "Tibber"), ("other", "Other")])
    assert [o["value"] for o in selector.config["options"]] == ["tibber", "other"]
    assert [o["value"] for o in control_system_selector().config["options"]] == [
        c for c, _ in CONTROL_SYSTEMS]


def test_a_partial_server_list_is_logged_as_a_warning(caplog):
    rows = [_row("tibber", "Tibber"), {"id": "other", "label": "Annat", "ha_domains": []}]
    with caplog.at_level("WARNING"):
        assert options_from_server(rows) is None
    assert "incomplete" in caplog.text


def test_an_older_server_without_label_en_falls_back_quietly(caplog):
    rows = [{"id": "tibber", "label": "Tibber", "ha_domains": []},
            {"id": "other", "label": "Annat", "ha_domains": []}]
    with caplog.at_level("WARNING"):
        assert options_from_server(rows) is None
    assert caplog.text == ""
