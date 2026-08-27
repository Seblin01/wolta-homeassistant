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
