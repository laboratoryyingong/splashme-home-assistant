"""Select entities for SplashMe."""

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SplashMeConfigEntry
from .entity import SplashMeHubEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SplashMeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SplashMe select entities."""
    del hass
    coordinator = entry.runtime_data.coordinator
    entities: list[SelectEntity] = [SplashMeDeviceSelect(coordinator, entry.entry_id)]
    if coordinator.site_select_visible:
        entities.insert(0, SplashMeSiteSelect(coordinator, entry.entry_id))
    async_add_entities(entities)


class SplashMeSiteSelect(SplashMeHubEntity, SelectEntity):
    """Select the active SplashMe site."""

    _attr_icon = "mdi:home-switch-outline"

    def __init__(self, coordinator, entry_id: str) -> None:
        """Initialize the site selector."""
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_selected_site"
        self._attr_name = "Selected Site"

    @property
    def current_option(self) -> str | None:
        """Return the current site."""
        return self.coordinator.selected_site_label

    @property
    def options(self) -> list[str]:
        """Return available sites."""
        return self.coordinator.site_options

    async def async_select_option(self, option: str) -> None:
        """Select a new site."""
        await self.coordinator.async_select_site(option)


class SplashMeDeviceSelect(SplashMeHubEntity, SelectEntity):
    """Select the active SplashMe device."""

    _attr_icon = "mdi:pool"

    def __init__(self, coordinator, entry_id: str) -> None:
        """Initialize the device selector."""
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_selected_device"
        self._attr_name = "Selected Device"

    @property
    def current_option(self) -> str | None:
        """Return the current device."""
        return self.coordinator.selected_device_label

    @property
    def options(self) -> list[str]:
        """Return available devices."""
        return self.coordinator.device_options

    async def async_select_option(self, option: str) -> None:
        """Select a new device."""
        await self.coordinator.async_select_device(option)
