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
    """Version must start at 0.1.0."""
    manifest_path = REPO_ROOT / "custom_components" / "wolta" / "manifest.json"
    with open(manifest_path) as f:
        data = json.load(f)
    assert data["version"] == "0.1.0"


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
