"""Translation-file parity guard.

Home Assistant loads runtime strings for a CUSTOM integration ONLY from
``translations/<lang>.json``; ``strings.json`` is the authoring source and is never
read at runtime. A field added to ``strings.json`` but forgotten in a translation
file therefore renders as its raw key (e.g. ``external_control_entity``) with no
description for every user in that locale - which is exactly what happened to the
external-control picker in ``en.json``.

This is a mechanical guard: it compares LEAF KEY PATHS, not values, so it fires on a
dropped key without pretending to check translation quality.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "wolta"
_STRINGS = _COMPONENT / "strings.json"
_TRANSLATIONS = _COMPONENT / "translations"


def _leaf_paths(node, prefix: str = "") -> set[str]:
    """Every dotted path down to a non-dict value."""
    if not isinstance(node, dict):
        return {prefix}
    out: set[str] = set()
    for key, value in node.items():
        out |= _leaf_paths(value, f"{prefix}.{key}" if prefix else key)
    return out


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _translation_files() -> list[Path]:
    return sorted(_TRANSLATIONS.glob("*.json"))


def test_translation_files_exist():
    """Guard the guard: a glob that silently matches nothing would make every
    parametrised case below vanish instead of failing."""
    assert _translation_files(), "no translations/*.json found"


@pytest.mark.parametrize("path", _translation_files(), ids=lambda p: p.name)
def test_translation_has_same_keys_as_strings(path: Path):
    """Every key in strings.json must exist in each translations/<lang>.json."""
    expected = _leaf_paths(_load(_STRINGS))
    actual = _leaf_paths(_load(path))

    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    assert not missing, f"{path.name} is missing keys from strings.json: {missing}"
    assert not extra, f"{path.name} has keys strings.json does not: {extra}"


@pytest.mark.parametrize("lang, key, text", [
    ("en", "soc_unavailable", "The selected state-of-charge sensor isn't available right now. "
                              "Check that it exists and has a value."),
    ("sv", "soc_unavailable", "Den valda sensorn för laddningsnivå är inte tillgänglig just nu. "
                              "Kontrollera att den finns och har ett värde."),
    ("en", "soc_id_too_long", "The sensor's entity ID is longer than 128 characters. "
                              "Rename the entity and try again."),
    ("sv", "soc_id_too_long", "Sensorns entitets-id är längre än 128 tecken. "
                              "Byt namn på entiteten och försök igen."),
])
def test_soc_picker_errors_are_translated(lang: str, key: str, text: str) -> None:
    """The two SoC picker errors config_flow._soc_error can return render as text, in
    both the setup and the reconfigure flow (both read config.error)."""
    data = json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    assert data["config"]["error"][key] == text


@pytest.mark.parametrize("lang", ["en", "sv"])
def test_soc_field_is_described_as_required(lang: str) -> None:
    """SoC is required (spec 2026-10-20-soc-krav): the field text says why and what the
    upload does, and neither the label nor the text calls it optional. The box for "no
    SoC sensor" is labelled in both the setup and the reconfigure step."""
    data = json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    optional = "optional" if lang == "en" else "valfri"
    for step in ("entities", "reconfigure"):
        node = data["config"]["step"][step]
        desc = node["data_description"]["soc_entities"]
        assert "365" in desc, (step, desc)
        for text in (desc, node["data"]["soc_entities"]):
            assert optional not in text.lower(), (step, text)
        assert node["data"]["soc_absent"], step


@pytest.mark.parametrize("lang, needle", [("en", "prefilled"), ("sv", "förifyll")])
def test_eff_beskrivningen_sager_att_faltet_ar_forifyllt(lang: str, needle: str) -> None:
    """Efterpost plan C: eff-fältet i Configure → Battery renderas med serverns lagrade värde,
    men beskrivningen sa inget om det – ägaren kunde tro att 0,84 var ett förslag att fylla i."""
    data = json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    desc = data["options"]["step"]["settings"]["sections"]["battery"]["data_description"]["eff"]
    assert needle in desc.lower()


@pytest.mark.parametrize("lang", ["en", "sv"])
def test_soc_required_repair_translates_every_error_its_flow_returns(lang: str) -> None:
    """The repair's picker returns soc_picker.soc_error's keys plus soc_required
    (soc_conflict cannot occur there: the picker never has the box); a fix flow
    reads errors from ITS OWN block, not config.error."""
    data = json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    errors = data["issues"]["soc_required"]["fix_flow"]["error"]
    shared = ("soc_unavailable", "soc_not_measurement", "soc_id_too_long", "soc_too_many")
    for key in ("soc_required", *shared):
        assert errors.get(key), key
    for key in shared:
        assert errors[key] == data["config"]["error"][key], key


# Data mode (backend spec 2026-10-16): the grade is computed against the capacity and
# efficiency measured from the state of charge, and a plant without one gets a grade of
# its own that is not compared with others. The old "does not change your grade" was true
# only while the SoC was merely collected; it must not come back.
_SOC_STALE_SV = ("påverkar inte ditt betyg", "Ändrar inte ditt betyg")


@pytest.mark.parametrize("lang", ["en", "sv"])
def test_soc_texts_no_longer_claim_the_grade_is_unaffected(lang: str) -> None:
    data = json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    texts = [data["issues"]["soc_required"]["fix_flow"]["step"]["init"]["description"],
             data["config"]["step"]["entities"]["data_description"]["soc_entities"],
             data["config"]["step"]["reconfigure"]["data_description"]["soc_entities"]]
    for text in texts:
        assert "not change your grade" not in text, text
        assert not any(n in text for n in _SOC_STALE_SV), text


@pytest.mark.parametrize("lang, needles", [
    ("en", ("compared with other plants", "365 days", "coming version")),
    ("sv", ("jämföras med andra anläggningar", "365 dagars", "kommande version"))])
def test_soc_required_repair_says_what_is_needed_and_what_happens_without_it(
        lang: str, needles: tuple[str, ...]) -> None:
    data = json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    text = data["issues"]["soc_required"]["fix_flow"]["step"]["init"]["description"]
    for needle in needles:
        assert needle in text, (needle, text)


@pytest.mark.parametrize("path", [_STRINGS, *_translation_files()], ids=lambda p: p.name)
def test_soc_required_repair_has_menu_steps_and_aborts(path: Path) -> None:
    """The fix flow's two menu options, its picker step and the two aborts it can end in
    (repairs.SocRequiredRepairFlow) all render as text; the old soc_missing is gone."""
    data = _load(path)
    assert "soc_missing" not in data["issues"]
    flow = data["issues"]["soc_required"]["fix_flow"]
    assert set(flow["step"]["init"]["menu_options"]) == {"pick_sensors", "soc_absent"}
    assert flow["step"]["pick_sensors"]["data"]["soc_entities"]
    assert {"failed", "entry_not_found"} <= set(flow["abort"])
    for key in ("soc_required", "soc_conflict"):
        assert data["config"]["error"][key], key


@pytest.mark.parametrize("path", [_STRINGS, *_translation_files()], ids=lambda p: p.name)
def test_statistics_missing_names_the_sensors(path: Path) -> None:
    """The repair's only placeholder is {entities}; a text without it would hide which
    sensor went silent - the one thing the owner needs to act on."""
    issue = _load(path)["issues"]["statistics_missing"]
    assert "{entities}" in issue["description"]
    assert "{" not in issue["title"]


@pytest.mark.parametrize("path", [_STRINGS, *_translation_files()], ids=lambda p: p.name)
def test_upload_stalled_names_the_time_and_the_sensors(path: Path) -> None:
    """The stall repair says since when nothing was measured ({since}) and which sensors
    were picked ({entities}); the title carries no placeholder."""
    issue = _load(path)["issues"]["upload_stalled"]
    assert "{since}" in issue["description"]
    assert "{entities}" in issue["description"]
    assert "{" not in issue["title"]


@pytest.mark.parametrize("path, needle", [
    (_STRINGS, "Wolta only sends quarters in which every selected sensor has a value"),
    (_TRANSLATIONS / "en.json",
     "Wolta only sends quarters in which every selected sensor has a value"),
    (_TRANSLATIONS / "sv.json",
     "Wolta skickar bara kvartar där varje vald sensor har ett eget mätvärde"),
], ids=lambda v: v.name if isinstance(v, Path) else "")
def test_energy_empty_no_longer_promises_a_grade_without_the_stream(
    path: Path, needle: str
) -> None:
    """With complete quarters an empty stream stops the whole upload; the old text said
    the grade was computed without that stream, which is no longer true."""
    text = _load(path)["issues"]["energy_stream_no_statistics"]["description"]
    assert needle in text
    assert "without that stream" not in text
    assert "utan den strömmen" not in text


@pytest.mark.parametrize("path", [_STRINGS, *_translation_files()], ids=lambda p: p.name)
@pytest.mark.parametrize("key", ["replacement_waiting_direction", "replacement_waiting_entities"])
def test_replacement_waiting_names_the_retry_time(path: Path, key: str) -> None:
    """The repair for a full archive has one placeholder, {retry_at}: when the correction
    is tried again. The title carries none."""
    issue = _load(path)["issues"][key]
    assert "{retry_at}" in issue["description"]
    assert "{" not in issue["title"]
    assert "fix_flow" not in issue


@pytest.mark.parametrize("path, needles", [
    (_STRINGS, ("temporarily stopped accepting corrections", "automatically",
                "don't need to do anything")),
    (_TRANSLATIONS / "en.json", ("temporarily stopped accepting corrections",
                                 "automatically", "don't need to do anything")),
    (_TRANSLATIONS / "sv.json", ("tillfälligt", "automatiskt",
                                 "behöver inte göra något")),
], ids=lambda v: v.name if isinstance(v, Path) else "")
def test_replacement_waiting_says_nothing_needs_to_be_done(
    path: Path, needles: tuple[str, ...]
) -> None:
    for key in ("replacement_waiting_direction", "replacement_waiting_entities"):
        text = _load(path)["issues"][key]["description"]
        for needle in needles:
            assert needle in text, (key, needle)


@pytest.mark.parametrize("path", [_STRINGS, *_translation_files()], ids=lambda p: p.name)
def test_grid_fee_implausible_issue_names_the_limit(path: Path) -> None:
    issue = _load(path)["issues"]["grid_fee_implausible"]
    assert issue["title"]
    assert "{limit}" in issue["description"]


@pytest.mark.parametrize("lang, needle", [
    ("en", "excluding VAT and energy tax"), ("sv", "exklusive moms och energiskatt")])
def test_grid_fee_field_says_without_vat_and_energy_tax(lang: str, needle: str) -> None:
    data = json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    text = data["options"]["step"]["settings"]["sections"]["tariffs"]["data_description"]["grid_var_ore"]
    assert needle in text, text


@pytest.mark.parametrize("path", [_STRINGS, *_translation_files()], ids=lambda p: p.name)
def test_control_system_required_repair_has_the_pick_step_and_aborts(path: Path) -> None:
    """The fix flow opens on the form (repairs.ControlSystemRequiredRepairFlow): its two
    fields, the error it returns, and the two aborts it can end in all render as text.
    A fix flow reads errors from its own block, not config.error."""
    issue = _load(path)["issues"]["control_system_required"]
    assert issue["title"]
    pick = issue["fix_flow"]["step"]["pick"]
    assert pick["title"] and pick["description"]
    assert pick["data"]["control_system"] and pick["data"]["control_system_name"]
    assert pick["data_description"]["control_system_name"]
    assert issue["fix_flow"]["error"]["control_system_name_required"]
    assert {"failed", "entry_not_found"} <= set(issue["fix_flow"]["abort"])
    # No menu and no ignore: the answer is required (HA's own Ignore stays).
    assert "init" not in issue["fix_flow"]["step"]


@pytest.mark.parametrize("lang, needles", [
    ("en", ("same control system", "wolta.se")),
    ("sv", ("samma styrsystem", "wolta.se"))])
def test_control_system_required_repair_says_why_and_where_to_change_it(
        lang: str, needles: tuple[str, ...]) -> None:
    data = json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    issue = data["issues"]["control_system_required"]
    text = issue["fix_flow"]["step"]["pick"]["description"]
    for needle in needles:
        assert needle in text, (needle, text)
    # The issue's own text is what the repairs list shows before the flow opens.
    assert "wolta.se" in issue["description"]


@pytest.mark.parametrize("lang", ["en", "sv"])
def test_control_system_required_repair_reuses_the_onboarding_name_error(lang: str) -> None:
    """One rule, one wording: the repair's name error is the setup step's."""
    data = json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    errors = data["issues"]["control_system_required"]["fix_flow"]["error"]
    assert errors["control_system_name_required"] == data["config"]["error"][
        "control_system_name_required"]
