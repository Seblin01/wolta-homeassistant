"""Shared pytest fixtures for Wolta integration tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Project root (contains custom_components/wolta).
# The HA loader discovers custom integrations by importing the `custom_components`
# package, so the project root must be on sys.path before any test module is loaded.
_PROJECT_ROOT = str(Path(__file__).parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture
def hass_config_dir() -> str:
    """Point HA config dir at the project root.

    Home Assistant's loader also looks for custom integrations under
    <config_dir>/custom_components, so pointing at the project root provides
    a second discovery path.
    """
    return _PROJECT_ROOT


@pytest.fixture
def enable_custom_integrations(hass):  # noqa: ANN001
    """Enable custom integrations for tests that use the hass fixture.

    pytest-homeassistant-custom-component caches the custom-components list in
    hass.data at startup (before sys.path is updated). Popping the cache key
    forces a fresh re-scan that picks up ``custom_components/wolta``.

    Request this fixture in every test (or test file) that calls
    ``hass.config_entries.flow.async_init(DOMAIN, ...)``.
    """
    from homeassistant import loader

    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)


# pytest-homeassistant-custom-component provides the `aioclient_mock` fixture
# automatically; no additional conftest work is needed for it.
