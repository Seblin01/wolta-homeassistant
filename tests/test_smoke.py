"""Smoke tests for the Wolta integration scaffold."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def test_manifest_valid_json():
    """Manifest must be valid JSON."""
    manifest_path = REPO_ROOT / "custom_components" / "wolta" / "manifest.json"
    with open(manifest_path) as f:
        data = json.load(f)
    assert data["domain"] == "wolta"


def test_manifest_required_keys():
    """Manifest must contain all required keys."""
    manifest_path = REPO_ROOT / "custom_components" / "wolta" / "manifest.json"
    with open(manifest_path) as f:
        data = json.load(f)

    required_keys = {
        "domain", "name", "codeowners", "config_flow",
        "documentation", "iot_class", "version",
    }
    for key in required_keys:
        assert key in data, f"Missing required key: {key}"


def test_manifest_version():
    """Version must be present and a valid semver (no hardcoded value — bumps must not
    break this test; hassfest already requires the key)."""
    import re

    manifest_path = REPO_ROOT / "custom_components" / "wolta" / "manifest.json"
    with open(manifest_path) as f:
        data = json.load(f)
    assert re.fullmatch(r"\d+\.\d+\.\d+", data["version"]), data["version"]


def test_domain_in_const_py():
    """DOMAIN constant in const.py must equal 'wolta'."""
    const_path = REPO_ROOT / "custom_components" / "wolta" / "const.py"
    src = const_path.read_text()
    # Parse without importing (avoids homeassistant dep)
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("DOMAIN"):
            _, _, val = line.partition("=")
            assert val.strip().strip('"').strip("'") == "wolta"
            return
    raise AssertionError("DOMAIN not found in const.py")


def test_hacs_json():
    """hacs.json must be valid and specify correct homeassistant floor."""
    hacs_path = REPO_ROOT / "hacs.json"
    with open(hacs_path) as f:
        data = json.load(f)
    assert data["name"] == "Wolta"
    assert data["homeassistant"] == "2025.12.0"


def test_readme_attribute_version_is_actually_shipped():
    """The README advertises the `flex_compensation` attribute as "vX.Y.Z+". If the
    manifest still carries an older version, nobody's HACS offers the update and the
    promise is empty - the exact way a released tag gets re-used by accident."""
    import re

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"`flex_compensation` attribute \(v(\d+)\.(\d+)\.(\d+)\+\)", readme)
    assert m, "README no longer states which version ships the flex_compensation attribute"
    promised = tuple(int(g) for g in m.groups())

    manifest_path = REPO_ROOT / "custom_components" / "wolta" / "manifest.json"
    with open(manifest_path) as f:
        shipped = tuple(int(x) for x in json.load(f)["version"].split("."))

    assert shipped >= promised, (
        f"README promises the attribute from v{'.'.join(map(str, promised))} but the "
        f"manifest ships v{'.'.join(map(str, shipped))} - bump the manifest"
    )


def test_readme_does_not_promise_removing_sensor_months_on_the_web():
    """The compensation card locks BOTH fields on a `source: "sensor"` row, replaces
    its Remove button with a label, and buildFlexPatch filters sensor rows out of both
    sides of its comparison. There is therefore no route on wolta.se that removes a
    month the integration wrote, and the README must not send anyone looking for one."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "remove those on wolta.se" not in readme.lower(), (
        "README still points at a removal path for sensor-written months that the "
        "compensation card does not offer"
    )
    assert "no Remove button" in readme, (
        "README no longer says the sensor rows cannot be removed from the card"
    )
