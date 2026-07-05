"""Button platform for the Wolta integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WoltaRateLimitError
from .const import DOMAIN
from .coordinator import WoltaCoordinator


class WoltaRecomputeButton(CoordinatorEntity[WoltaCoordinator], ButtonEntity):
    """Button that immediately triggers a server-side Wolta recompute."""

    _attr_has_entity_name = True
    _attr_name = "Räkna om"

    def __init__(
        self,
        coordinator: WoltaCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id}_recompute"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Wolta",
            manufacturer="Wolta",
            entry_type="service",
        )

    async def async_press(self) -> None:
        """Handle button press — trigger server-side recompute."""
        try:
            await self.coordinator.async_trigger_recompute()
        except WoltaRateLimitError as err:
            raise HomeAssistantError(
                "Wolta begränsar just nu förfrågningar – försök igen senare "
                f"(retry after {err.retry_after}s)."
            ) from err


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wolta button from a config entry."""
    coordinator: WoltaCoordinator = entry.runtime_data
    async_add_entities([WoltaRecomputeButton(coordinator=coordinator, entry=entry)])
