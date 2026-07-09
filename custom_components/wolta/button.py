"""Button platform for the Wolta integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WoltaApiError, WoltaRateLimitError
from .const import CONF_TOKEN, DOMAIN, profile_url
from .coordinator import WoltaCoordinator


class WoltaRecomputeButton(CoordinatorEntity[WoltaCoordinator], ButtonEntity):
    """Button that immediately triggers a server-side Wolta recompute."""

    _attr_has_entity_name = True
    _attr_translation_key = "recompute"

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
            configuration_url=profile_url(entry.data[CONF_TOKEN]),
        )

    async def async_press(self) -> None:
        """Trigger a server-side recompute if allowed, and ALWAYS refresh the shown
        results so the latest grade appears (the grade only changes ~weekly, so most
        presses just need a refresh)."""
        try:
            await self.coordinator.async_trigger_recompute()
        except WoltaRateLimitError:
            # Already recomputed recently (cooldown, max 1/day) – no new computation needed.
            # Just fetch fresh results so the user sees the latest grade, without an error dialog.
            await self.coordinator.async_request_refresh()
            return
        except WoltaApiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="recompute_failed",
            ) from err
        # Recompute queued → fetch fresh results right away (the coordinator's fast polling
        # takes over until the computation is done).
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wolta button from a config entry."""
    coordinator: WoltaCoordinator = entry.runtime_data
    async_add_entities([WoltaRecomputeButton(coordinator=coordinator, entry=entry)])
