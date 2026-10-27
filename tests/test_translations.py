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
    # hassfest: a fixable issue carries its text in fix_flow only; a top-level
    # description is mutually exclusive with fix_flow.
    assert "description" not in issue


@pytest.mark.parametrize("lang", ["en", "sv"])
def test_control_system_required_repair_reuses_the_onboarding_name_error(lang: str) -> None:
    """One rule, one wording: the repair's name error is the setup step's."""
    data = json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))
    errors = data["issues"]["control_system_required"]["fix_flow"]["error"]
    assert errors["control_system_name_required"] == data["config"]["error"][
        "control_system_name_required"]


# ---------------------------------------------------------------------------
# Inköpspris (HACS 0.53.0): reduced energy tax choice and the fixed grid fee
# ---------------------------------------------------------------------------

_TAX_MUNICIPALITIES = (
    "Norrbotten", "Västerbotten", "Jämtland", "Sollefteå", "Ånge", "Örnsköldsvik",
    "Ljusdal", "Malung-Sälen", "Mora", "Orsa", "Älvdalen", "Torsby")


def _lang(lang: str) -> dict:
    return json.loads((_TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", [_STRINGS, *_translation_files()], ids=lambda p: p.name)
def test_reduced_energy_tax_select_has_its_three_options(path: Path) -> None:
    """The selector reads its labels from selector.<translation_key>.options.<value>; a
    missing entry renders the raw value ("not_set") in the list."""
    options = _load(path)["selector"]["reduced_energy_tax"]["options"]
    assert set(options) == {"not_set", "no", "yes"}
    assert all(options.values())


@pytest.mark.parametrize("lang, needles", [
    ("en", ("Skatteverket", "9.6", "state is unknown", "Not set")),
    ("sv", ("Skatteverket", "9,6", "tillståndet är okänt", "Inte angivet"))])
def test_reduced_energy_tax_help_text_names_the_source_and_what_not_set_means(
        lang: str, needles: tuple[str, ...]) -> None:
    data = _lang(lang)
    text = data["options"]["step"]["settings"]["sections"]["tariffs"]["data_description"][
        "reduced_energy_tax"]
    for needle in needles + _TAX_MUNICIPALITIES:
        assert needle in text, (needle, text)
    assert data["selector"]["reduced_energy_tax"]["options"]["not_set"] in text


@pytest.mark.parametrize("lang, needle, fixed_word", [
    ("en", "0 if you only have a percentage", "fixed part"),
    ("sv", "0 om du bara har procent", "fasta del")])
def test_fixed_grid_fee_field_says_zero_when_only_a_percentage(
        lang: str, needle: str, fixed_word: str) -> None:
    descriptions = _lang(lang)["options"]["step"]["settings"]["sections"]["tariffs"][
        "data_description"]
    assert needle in descriptions["grid_var_ore"]
    # The percent field points at the fixed part it requires.
    assert fixed_word in descriptions["grid_var_pct"]


@pytest.mark.parametrize("path", [_STRINGS, *_translation_files()], ids=lambda p: p.name)
def test_grid_fixed_missing_issue_exists_and_is_not_fixable(path: Path) -> None:
    issue = _load(path)["issues"]["grid_fixed_missing"]
    assert issue["title"]
    assert issue["description"]
    assert "fix_flow" not in issue


@pytest.mark.parametrize("path", [_STRINGS, *_translation_files()], ids=lambda p: p.name)
def test_grid_fixed_required_error_exists_for_options_and_reauth(path: Path) -> None:
    data = _load(path)
    assert data["options"]["error"]["grid_fixed_required"]
    assert data["config"]["error"]["grid_fixed_required"]
    # The reauth form shows the field it asks for.
    assert data["config"]["step"]["reauth_confirm"]["data"]["grid_var_ore"]


# --- purchase-price copy -------------------------------------------------------
#
# A purchase-price sensor without a price is AVAILABLE with state `unknown` and a `reason`
# attribute (Home Assistant hides every attribute of an unavailable entity, and the reason
# is the point). Copy that says "unavailable" would send the user looking for the wrong
# thing, so the strings that explain the missing price must not use the word.

_ALL_COPY = [_STRINGS, *_translation_files()]
_NO_PRICE_COPY = (
    ("issues", "grid_fixed_missing", "description"),
    ("options", "step", "settings", "sections", "tariffs", "data_description",
     "reduced_energy_tax"),
)
_SETTINGS_PATH = {
    "strings.json": "Configure → Settings → Tariffs",
    "en.json": "Configure → Settings → Tariffs",
    "sv.json": "Konfigurera → Inställningar → Tariffer",
}


def _at(tree: dict, path: tuple[str, ...]) -> str:
    for key in path:
        tree = tree[key]
    return tree


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
@pytest.mark.parametrize("key", _NO_PRICE_COPY, ids=lambda k: ".".join(k[-2:]))
def test_missing_price_copy_does_not_say_unavailable(path: Path, key: tuple[str, ...]):
    text = _at(_load(path), key).lower()
    assert "unavailable" not in text, f"{path.name}: {'.'.join(key)}"
    assert "otillgänglig" not in text, f"{path.name}: {'.'.join(key)}"


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
@pytest.mark.parametrize("issue", ["grid_fee_implausible", "grid_fixed_missing"])
def test_repair_text_names_the_real_menu_path(path: Path, issue: str):
    """The tariffs live under Configure → Settings → Tariffs, not directly under Configure."""
    text = _at(_load(path), ("issues", issue, "description"))
    assert _SETTINGS_PATH[path.name] in text, f"{path.name}: {issue}"


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_options_fixed_part_error_names_the_tariffs_section(path: Path):
    """Base errors render at the top of the form, so "above" points at nothing."""
    text = _at(_load(path), ("options", "error", "grid_fixed_required"))
    assert "above" not in text.lower() and "ovan" not in text.lower(), path.name
    tariffs = _at(_load(path), ("options", "step", "settings", "sections", "tariffs", "name"))
    assert tariffs in text, path.name


def test_swedish_copy_uses_typographic_quotes():
    text = _at(_load(_TRANSLATIONS / "sv.json"), ("options", "error", "grid_fixed_required"))
    assert '"' not in text


# --- sale price (HACS 0.54.0): grid benefit fields, retailer terms, the export-split issues ----

_BENEFIT_FIELDS = ("grid_benefit_ore", "export_extra_ore", "export_extra_pct")
_BENEFIT_ERRORS = ("grid_benefit_fixed_required", "grid_benefit_zone")


def _tariffs(path: Path) -> dict:
    return _load(path)["options"]["step"]["settings"]["sections"]["tariffs"]


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_tariffs_section_has_label_and_description_for_the_export_fields(path: Path) -> None:
    tariffs = _tariffs(path)
    for field in _BENEFIT_FIELDS:
        assert tariffs["data"][field], f"{path.name}: {field} label"
        assert tariffs["data_description"][field], f"{path.name}: {field} description"


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_retailer_terms_field_says_it_is_not_the_grid_benefit(path: Path) -> None:
    """The reason the fields were split: a number in the retailer's field that is really the
    grid benefit is counted twice, so the field must point at the other one."""
    tariffs = _tariffs(path)
    label = tariffs["data"]["export_extra_ore"].lower()
    text = tariffs["data_description"]["export_extra_ore"].lower()
    benefit = {"en.json": "grid benefit", "strings.json": "grid benefit",
               "sv.json": "nätnytta"}[path.name]
    retailer = {"en.json": "retailer", "strings.json": "retailer", "sv.json": "elhandel"}[path.name]
    assert retailer in label
    assert benefit in text
    # The old catch-all wording is gone from both fields.
    for old in ("grid/export agreement", "nät-/exportavtal", "exportjustering",
                "export premium/discount"):
        assert old not in label and old not in text, f"{path.name}: {old}"


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_percent_field_is_the_grid_benefit_not_a_generic_export_compensation(path: Path) -> None:
    label = _tariffs(path)["data"]["export_extra_pct"].lower()
    needle = "nätnytta" if path.name == "sv.json" else "grid benefit"
    assert needle in label, path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_benefit_field_errors_exist_for_options(path: Path) -> None:
    errors = _load(path)["options"]["error"]
    for code in _BENEFIT_ERRORS:
        assert errors[code], f"{path.name}: {code}"


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_benefit_errors_name_the_field_they_ask_for(path: Path) -> None:
    """Field errors render next to the field, but the text still has to say which one to fill."""
    label = {"en.json": "Grid benefit", "strings.json": "Grid benefit",
             "sv.json": "Nätnytta"}[path.name]
    errors = _load(path)["options"]["error"]
    assert label in errors["grid_benefit_fixed_required"], path.name
    # The label the error quotes must be the label the field has.
    assert _tariffs(path)["data"]["grid_benefit_ore"].startswith(label), path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
@pytest.mark.parametrize("issue, placeholder", [
    ("export_split_unclear", "{export_extra}"),
    ("grid_benefit_fixed_missing", "{export_pct}"),
])
def test_export_issues_exist_are_not_fixable_and_show_the_owners_figure(
    path: Path, issue: str, placeholder: str
) -> None:
    node = _load(path)["issues"][issue]
    assert node["title"]
    assert "fix_flow" not in node
    assert placeholder in node["description"], f"{path.name}: {issue}"


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
@pytest.mark.parametrize("issue", ["export_split_unclear", "grid_benefit_fixed_missing"])
def test_export_issue_text_names_the_real_menu_path(path: Path, issue: str) -> None:
    text = _at(_load(path), ("issues", issue, "description"))
    assert _SETTINGS_PATH[path.name] in text, f"{path.name}: {issue}"


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_export_split_issue_says_the_grid_benefit_is_counted_twice(path: Path) -> None:
    text = _at(_load(path), ("issues", "export_split_unclear", "description"))
    needle = {"en.json": "counted twice", "strings.json": "counted twice",
              "sv.json": "räknas den två gånger"}[path.name]
    assert needle in text, path.name
    assert "5 öre" in text, path.name


# --- time periods and the contract history (HACS 0.54.0, spec 2026-10-27-marknadsanpassning
# Tillägg 2-4): the form edits today's contract; the periods and the history live on wolta.se ---

_CONTRACT_FIELDS = ("retail_price_model", "retail_fixed_ore")
_TARIFF_ERRORS = ("periods_require_base", "retail_fixed_invalid", "retail_fixed_surcharge",
                  "tariff_zone_se_only", "grid_benefit_history_requires_base")
# Never released (Global Constraints, "Tas bort helt"): no label, help text, error, selector or
# repair may name them.
_REMOVED_KEYS = (
    "grid_var_peak_ore", "grid_benefit_peak_ore",
    "grid_fee_peak_start_hour", "grid_fee_peak_end_hour", "grid_fee_peak_holidays",
    "grid_benefit_peak_start_hour", "grid_benefit_peak_end_hour", "grid_benefit_peak_holidays",
    "retail_price_model_since",
    "grid_var_peak_requires_base", "grid_benefit_peak_requires_base", "peak_hours_invalid",
    "retail_since_invalid", "peak_holidays", "grid_fee_peak_implausible",
)

# The labels of the web form, so the same field has the same name on both surfaces.
_LABELS = {
    "en.json": {
        "grid_var_ore": "Grid fee (transfer)",
        "grid_benefit_ore": "Grid benefit",
        "retail_price_model": "Electricity contract",
    },
    "sv.json": {
        "grid_var_ore": "Nätavgift (överföring)",
        "grid_benefit_ore": "Nätnytta",
        "retail_price_model": "Elhandelsavtal",
    },
}
_LABELS["strings.json"] = _LABELS["en.json"]
_WORDS = {
    "en.json": {"today": "from today", "period": "time period", "history": "contract history",
                "back": "back in time", "start": "start date"},
    "sv.json": {"today": "från i dag", "period": "tidsperiod", "history": "avtalshistorik",
                "back": "bakåt i tiden", "start": "startdatum"},
}
_WORDS["strings.json"] = _WORDS["en.json"]


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_tariff_labels_are_the_webs(path: Path) -> None:
    tariffs = _tariffs(path)["data"]
    for field, label in _LABELS[path.name].items():
        assert tariffs[field].startswith(label), (path.name, field, tariffs[field])


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_the_old_rest_of_the_year_label_is_gone_everywhere(path: Path) -> None:
    """The field is just "Grid benefit" now. The old label would survive in an error or a repair
    that quotes it, naming a field that no longer exists."""
    raw = path.read_text(encoding="utf-8")
    for old in ("Grid benefit, rest of the year", "Nätnytta, övrig tid"):
        assert old not in raw, (path.name, old)


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_the_removed_fields_and_texts_are_gone_from_the_copy(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    for key in _REMOVED_KEYS:
        assert f'"{key}"' not in raw, (path.name, key)
    # The high-load wording described the removed fields (the server's copy was changed the
    # same way, kontrollerns beslut 6).
    for word in ("high-load", "höglast", "Contract valid from", "Avtalet gäller från"):
        assert word not in raw, (path.name, word)


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_every_contract_field_has_a_label_and_a_description(path: Path) -> None:
    tariffs = _tariffs(path)
    for field in _CONTRACT_FIELDS:
        assert tariffs["data"][field], f"{path.name}: {field} label"
        assert tariffs["data_description"][field], f"{path.name}: {field} description"


_EXCL_VAT = {"en.json": "excluding VAT", "strings.json": "excluding VAT",
             "sv.json": "exklusive moms"}


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_the_fixed_price_says_excluding_vat(path: Path) -> None:
    assert _EXCL_VAT[path.name] in _tariffs(path)["data_description"]["retail_fixed_ore"]


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_contract_help_states_the_limits_of_the_model(path: Path) -> None:
    """Monthly price: the retailer's volume-weighting cost belongs in the markup. An hourly-price
    contract is counted per quarter-hour and can differ within the hour. A mix is not supported."""
    desc = _tariffs(path)["data_description"]
    en = path.name != "sv.json"
    model = desc["retail_price_model"].lower()
    markup = desc["surcharge_ore"].lower()
    assert ("volume weighting" if en else "volymvägning") in markup
    assert ("hourly" if en else "timpris") in model
    assert ("within the hour" if en else "inom timmen") in model
    assert ("mix" if en else "mixavtal") in model
    assert ("average" if en else "medel") in model


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_fixed_contract_help_says_the_markup_is_not_used(path: Path) -> None:
    desc = _tariffs(path)["data_description"]
    en = path.name != "sv.json"
    assert ("not used" if en else "används inte") in desc["retail_fixed_ore"].lower()


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_a_contract_change_applies_from_today_and_the_rest_is_on_wolta_se(path: Path) -> None:
    """The help text says what a save here does (a contract change applies from today) and where
    the rest is done: time periods, the contract history and corrections back in time."""
    words = _WORDS[path.name]
    tariffs = _tariffs(path)
    section = tariffs["description"]
    for word in ("today", "period", "history", "back"):
        assert words[word] in section.lower(), (path.name, word)
    assert "wolta.se" in section, path.name
    model = tariffs["data_description"]["retail_price_model"]
    assert words["today"] in model, path.name
    assert "wolta.se" in model, path.name
    assert words["today"] in tariffs["data_description"]["surcharge_ore"], path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_from_today_is_qualified_by_the_first_answer_everywhere(path: Path) -> None:
    """The section description and the markup help say "from today" too, and without the
    qualifier they contradict the contract-type help (Ruling 14: the first answers apply to all
    of the history). Each says it is after the first answer."""
    tariffs = _tariffs(path)
    needle = "efter ditt första svar" if path.name == "sv.json" else "after your first answer"
    for text in (tariffs["description"], tariffs["data_description"]["surcharge_ore"]):
        assert needle in text.lower(), (path.name, text[:60])


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_contract_help_names_both_first_answers_that_apply_to_all_history(path: Path) -> None:
    """Ruling 14 (widened by ruling 22): only two answers apply from the start of the history: the
    first contract type, while no contract in the history has one, and a markup entered for the
    first time without changing the contract type. Everything after that applies from today. Dropping either condition overstates
    or understates what a save does."""
    text = _tariffs(path)["data_description"]["retail_price_model"]
    if path.name == "sv.json":
        needles = ("hela din historik", "den första avtalstypen du väljer",
                   "så länge inget avtal i historiken har någon",
                   "ett påslag du anger för första gången utan att byta avtalstyp",
                   "Därefter gäller en ändring från i dag")
    else:
        needles = ("apply to all of your history", "the first contract type you choose",
                   "while no contract in your history has one",
                   "a markup you enter for the first time without changing the contract type",
                   "After that, a change applies from today")
    for needle in needles:
        assert needle in text, (path.name, needle)


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_markup_help_no_longer_speaks_of_a_start_date(path: Path) -> None:
    """Tillägg 3 point 3: "the markup before the start date" is gone; the history is in earlier
    contracts. A fixed price carries no markup."""
    text = _tariffs(path)["data_description"]["surcharge_ore"]
    assert _WORDS[path.name]["start"] not in text.lower(), path.name
    assert "SE1–SE4" in text, path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
@pytest.mark.parametrize("field", ["grid_var_ore", "grid_benefit_ore"])
def test_base_price_help_says_it_applies_outside_the_time_periods(path: Path, field: str) -> None:
    text = _tariffs(path)["data_description"][field]
    assert _WORDS[path.name]["period"] in text, (path.name, field)
    assert "wolta.se" in text, (path.name, field)


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_settings_description_carries_the_web_summary(path: Path) -> None:
    """The form passes the summary of the time periods and the contract history (empty without
    them), so the description must have the placeholder or the summary is never shown."""
    text = _load(path)["options"]["step"]["settings"]["description"]
    assert text.endswith("{web_summary}"), path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_contract_selector_has_plain_language_options(path: Path) -> None:
    selector = _load(path)["selector"]
    models = selector["retail_price_model"]["options"]
    if path.name == "sv.json":
        assert models == {"not_stated": "Inte angivet", "quarter": "Kvartspris",
                          "month": "Månadspris", "fixed": "Fastpris"}
    else:
        assert models == {"not_stated": "Not stated", "quarter": "Quarter-hour price",
                          "month": "Monthly price", "fixed": "Fixed price"}


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_contract_help_says_what_not_stated_means(path: Path) -> None:
    """Code review C1: the select offers "Not stated" while Wolta has no contract type stored. The
    help says what it is counted as and that the first choice applies to all of the history, with
    the option's own label."""
    data = _load(path)
    text = _tariffs(path)["data_description"]["retail_price_model"]
    label = data["selector"]["retail_price_model"]["options"]["not_stated"]
    if path.name == "sv.json":
        needle = (f"{label}: Wolta räknar med kvartspris tills du väljer, och ditt första val "
                  "gäller hela din historik.")
    else:
        needle = (f"{label}: Wolta counts quarter-hour price until you choose; your first choice "
                  "applies to all of your history.")
    assert needle in text, path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_contract_help_says_not_stated_stays_in_the_history(path: Path) -> None:
    """Server ruling 22: a markup change keeps the type not stated, in the contract history too,
    and the first type chosen while no contract has one gives every contract that type with its
    own markup. The old condition ("if Wolta has none stored for you yet") is gone: with a history
    of contracts without a type the first choice still applies to all of them."""
    text = _tariffs(path)["data_description"]["retail_price_model"]
    if path.name == "sv.json":
        needles = ("En ändring av påslaget lämnar avtalstypen inte angiven, också i avtalshistoriken",
                   "varje avtal i historiken den typen, vart och ett med sitt påslag",
                   "så länge inget avtal i historiken har någon")
        gone = "om Wolta inte har någon sparad för dig än"
    else:
        needles = ("A markup change keeps the contract type not stated, in your contract history too",
                   "every contract in the history that type, each with its own markup",
                   "while no contract in your history has one")
        gone = "if Wolta has none stored for you yet"
    for needle in needles:
        assert needle in text, (path.name, needle)
    assert gone not in text, path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_grid_benefit_help_says_price_lists_change_on_1_january(path: Path) -> None:
    """Final review M2: the grid benefit usually changes with the network operator's price list
    on 1 January, and the date of the change is entered on wolta.se."""
    text = _tariffs(path)["data_description"]["grid_benefit_ore"]
    needle = {"sv.json": "Nätföretagens prislistor ändras oftast 1 januari – ange datumet på wolta.se.",
              }.get(path.name, "Network operators' price lists usually change on 1 January – enter "
                               "the date on wolta.se.")
    assert needle in text, path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_export_split_issue_says_the_answer_corrects_all_history(path: Path) -> None:
    """Server ruling 21: the answer to the split question (the grid benefit and the corrected
    terms saved together) applies from the start, so the amount is counted once."""
    text = _at(_load(path), ("issues", "export_split_unclear", "description"))
    needle = {"sv.json": "Ditt svar rättar hela din historik: sparade tillsammans gäller nätnyttan "
                         "och de rättade villkoren från början"}.get(
        path.name, "Your answer corrects all of your history: saved together, the grid benefit "
                   "and the corrected terms apply from the start")
    assert needle in text, path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_tariff_errors_exist_for_options(path: Path) -> None:
    errors = _load(path)["options"]["error"]
    for code in _TARIFF_ERRORS:
        assert errors[code], f"{path.name}: {code}"


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_tariff_errors_name_the_field_they_ask_for(path: Path) -> None:
    """The error also shows on base, far from the field, so it quotes the label it means."""
    errors = _load(path)["options"]["error"]
    tariffs = _tariffs(path)
    data = tariffs["data"]
    labels = _LABELS[path.name]
    assert labels["grid_var_ore"] in errors["periods_require_base"], path.name
    assert labels["grid_benefit_ore"] in errors["periods_require_base"], path.name
    assert "wolta.se" in errors["periods_require_base"], path.name
    assert data["retail_fixed_ore"].split(" (")[0] in errors["retail_fixed_invalid"]
    assert tariffs["name"] in errors["retail_fixed_invalid"], path.name
    assert data["surcharge_ore"].split(" (")[0] in errors["retail_fixed_surcharge"], path.name
    assert tariffs["name"] in errors["retail_fixed_surcharge"], path.name
    assert "SE1–SE4" in errors["tariff_zone_se_only"], path.name
    history = errors["grid_benefit_history_requires_base"]
    assert labels["grid_benefit_ore"] in history, path.name
    assert tariffs["name"] in history, path.name
    assert "wolta.se" in history, path.name


def test_swedish_tariff_errors_use_typographic_quotes() -> None:
    errors = _load(_TRANSLATIONS / "sv.json")["options"]["error"]
    for code in _TARIFF_ERRORS:
        assert '"' not in errors[code], code


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_period_fee_issue_names_the_limit_and_where_the_periods_are_changed(path: Path) -> None:
    issue = _load(path)["issues"]["grid_fee_period_implausible"]
    assert issue["title"]
    assert "{limit}" in issue["description"]
    assert "{" not in issue["title"]
    assert "fix_flow" not in issue
    # The periods are not in the options form, so the repair sends the owner to wolta.se.
    assert "wolta.se" in issue["description"], path.name
    assert _WORDS[path.name]["period"] in issue["description"], path.name
    assert ("excluding VAT and energy tax" if path.name != "sv.json"
            else "exklusive moms och energiskatt") in issue["description"]


# --- help text must not point at positions or at fields hidden outside SE1-SE4 ---


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
@pytest.mark.parametrize("field", ["grid_var_ore", "grid_var_pct"])
def test_grid_fee_help_names_fields_not_positions(path: Path, field: str) -> None:
    """These two fields are shown in every zone while the contract fields after them are not:
    "the field below/above" points at the wrong thing or at nothing."""
    text = _tariffs(path)["data_description"][field].lower()
    for position in ("field below", "field above", "next field", "fältet nedan", "fältet ovan",
                     "nästa fält"):
        assert position not in text, (path.name, field, position)


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_grid_fee_help_names_the_labels_it_refers_to(path: Path) -> None:
    desc = _tariffs(path)["data_description"]
    labels = _LABELS[path.name]
    assert labels["grid_var_ore"] in desc["grid_var_pct"], path.name
    assert _tariffs(path)["data"]["grid_var_pct"].split(" (")[0] in desc["grid_var_ore"], path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_markup_help_says_where_the_contract_exists(path: Path) -> None:
    """The markup is shown in every zone, the contract only in SE1-SE4: the sentence about
    Monthly/Fixed price must say so."""
    text = _tariffs(path)["data_description"]["surcharge_ore"]
    assert "SE1–SE4" in text, path.name


def test_swedish_mix_contracts_sentence_is_plain() -> None:
    text = _tariffs(_TRANSLATIONS / "sv.json")["data_description"]["retail_price_model"]
    assert "Mixavtal (en del fast, en del spot) stöds inte." in text
    assert "mixavtal alltså" not in text


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_se_only_amount_labels_and_repair_use_ore_not_cents(path: Path) -> None:
    data = _tariffs(path)["data"]
    assert "öre/ct" not in data["retail_fixed_ore"], path.name
    assert "öre per kWh" in data["retail_fixed_ore"], path.name
    issue = _load(path)["issues"]["grid_fee_period_implausible"]["description"]
    assert "cents" not in issue, path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_fixed_part_repair_names_the_renamed_field(path: Path) -> None:
    text = _at(_load(path), ("issues", "grid_fixed_missing", "description"))
    assert _LABELS[path.name]["grid_var_ore"] in text, path.name


@pytest.mark.parametrize("path", [_STRINGS, _TRANSLATIONS / "en.json"], ids=lambda p: p.name)
def test_all_year_grid_fee_repair_keeps_cents(path: Path) -> None:
    """That repair is not zone-limited and the grid fee is in euro cents outside SE; only the
    time-period repair (SE1-SE4 only) says plain öre."""
    issues = _load(path)["issues"]
    assert "öre (or cents) per kWh" in issues["grid_fee_implausible"]["description"]
    assert "cents" not in issues["grid_fee_period_implausible"]["description"]


# --- The sale side's history (HACS 0.54.0, spec 2026-10-27-marknadsanpassning Tillägg 5): the
# form edits today's grid benefit and retailer's terms; the history is kept and corrected on
# wolta.se. The server's rulings 18 and 19 decide when a save applies from the start instead. ---

_SALE_FIELDS = ("grid_benefit_ore", "export_extra_ore", "export_extra_pct")


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
@pytest.mark.parametrize("field", _SALE_FIELDS)
def test_sale_help_says_a_change_applies_from_today_after_the_first_answer(
    path: Path, field: str
) -> None:
    text = _tariffs(path)["data_description"][field]
    needle = "efter ditt första svar" if path.name == "sv.json" else "after your first answer"
    assert needle in text.lower(), (path.name, field)
    assert _WORDS[path.name]["today"] in text, (path.name, field)
    assert "SE1–SE4" in text, (path.name, field)


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
@pytest.mark.parametrize("field", ["grid_benefit_ore", "export_extra_ore"])
def test_sale_help_says_the_history_and_corrections_are_on_wolta_se(path: Path, field: str) -> None:
    text = _tariffs(path)["data_description"][field]
    words = _WORDS[path.name]
    assert "wolta.se" in text, (path.name, field)
    assert words["back"] in text, (path.name, field)
    assert ("historik" if path.name == "sv.json" else "history") in text, (path.name, field)


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_terms_help_says_the_first_answer_other_than_zero_applies_to_all_history(
    path: Path,
) -> None:
    """Ruling 18: an empty field is stored as 0, so 0 is not an answer and the first value other
    than 0 applies to all of the history. Someone who really had 0 corrects the date on the web.
    The ruling only holds while the terms have no history list (the server's _sale_unanswered is
    reached only without entries): with one, every change is an entry dated today, also from a
    today's entry of 0. The condition is part of the sentence, as in the server's copy."""
    text = _tariffs(path)["data_description"]["export_extra_ore"]
    if path.name == "sv.json":
        needles = ("Så länge villkoren inte har någon historik på wolta.se gäller ditt första "
                   "svar som inte är 0 hela din historik",
                   "ett tomt fält sparas som 0", "hade du verkligen 0",
                   "Med en historik gäller varje ändring från i dag, också från 0")
        unconditional = "Ditt första svar som inte är 0 gäller hela din historik"
    else:
        needles = ("While your terms have no history on wolta.se, your first answer other than 0 "
                   "applies to all of your history",
                   "an empty field is stored as 0", "if you really had 0",
                   "With a history, every change applies from today, also from 0")
        unconditional = "Your first answer other than 0 applies to all of your history"
    for needle in needles:
        assert needle in text, (path.name, needle)
    assert unconditional not in text, path.name


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_terms_help_says_a_cleared_field_is_plain_spot(path: Path) -> None:
    """Ruling 10: clearing answered terms is a change to 0 (plain spot), from today in SE1-SE4.
    The server counts missing terms as 0 in every zone, so "your country's standard" named
    nothing the owner could look up."""
    text = _tariffs(path)["data_description"]["export_extra_ore"]
    if path.name == "sv.json":
        assert "Rensa fältet för ren spot (0); i SE1–SE4 gäller det från i dag" in text
        assert "landets schablon" not in text
    else:
        assert "Clear the field for plain spot (0); in SE1–SE4 it applies from today" in text
        assert "standard tariff" not in text


@pytest.mark.parametrize("doc", ["README.md", "docs/release-notes-v0.54.0.md"])
def test_docs_state_ruling_18_with_its_no_history_condition(doc: str) -> None:
    """The README and the release notes say the same as the help text and the server's copy:
    the first terms other than 0 apply to all of the history only while there is no history."""
    text = (_COMPONENT.parents[1] / doc).read_text(encoding="utf-8")
    assert "the first value other than 0, as long as the terms have no history on wolta.se" in text
    assert "with a history, every change of the terms applies from today, also from a 0" in text


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_benefit_help_says_the_block_and_only_the_first_amount_applies_to_all_history(
    path: Path,
) -> None:
    """Ruling 19: the grid benefit is judged as a block. Only the very first amount applies to
    all of the history; after it every change of the amount, the percentage or the time periods
    applies from today. With a history the base cannot be cleared."""
    text = _tariffs(path)["data_description"]["grid_benefit_ore"]
    if path.name == "sv.json":
        needles = ("beloppet, procentdelen eller tidsperioderna",
                   "bara det allra första beloppet gäller hela din historik",
                   "inte möjligt när nätnyttan har en historik")
    else:
        needles = ("this amount, the percentage or the time periods",
                   "only the very first amount applies to all of your history",
                   "not possible while the grid benefit has a history")
    for needle in needles:
        assert needle in text, (path.name, needle)


@pytest.mark.parametrize("path", _ALL_COPY, ids=lambda p: p.name)
def test_tariffs_description_names_the_sale_side_and_its_history(path: Path) -> None:
    text = _tariffs(path)["description"]
    if path.name == "sv.json":
        needles = ("elhandelsavtalet, nätnyttan eller elhandelns villkor för såld el",
                   "historiken för nätnyttan och elhandelns villkor")
    else:
        needles = ("the electricity contract, the grid benefit or the retailer's terms for sold "
                   "electricity", "the history of the grid benefit and the retailer's terms")
    for needle in needles:
        assert needle in text, (path.name, needle)
