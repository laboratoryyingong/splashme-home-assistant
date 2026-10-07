"""Button entities for SplashMe."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SplashMeConfigEntry, is_lan_entry
from .dashboard import async_register_dashboard
from .entity import SplashMeDeviceEntity
from .lan import SplashMeLanEntity
from .lights import LIGHT_ACTIONS, LightAction
from .slots import SplashMeLanSlotEntities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SplashMeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SplashMe buttons."""
    coordinator = entry.runtime_data.coordinator
    if is_lan_entry(entry):
        async_add_entities(
            [
                SplashMeLanRefreshButton(coordinator, entry),
                SplashMeLanResetVolumeButton(coordinator, entry, "acid"),
                SplashMeLanResetVolumeButton(coordinator, entry, "chlorine"),
                SplashMeLanResetDashboardButton(coordinator, entry),
            ]
        )
        # Brand-specific light actions (next colour / sync / brightness),
        # following light slot and brand changes.
        slots = SplashMeLanSlotEntities(
            hass,
            entry,
            coordinator,
            "light_",
            lambda data: {
                f"light_{aux.slot}_{action.key}": (aux.slot, action)
                for aux in data.aux
                if aux.is_light
                for action in LIGHT_ACTIONS.get(data.light_type(aux.slot), ())
            },
            lambda spec: SplashMeLanLightActionButton(coordinator, entry, *spec),
        )
        await slots.async_sync()
        entry.runtime_data.slot_entities.append(slots)
        return
    entities: list[ButtonEntity] = [
        SplashMeRefreshDeviceDataButton(coordinator, device)
        for device in coordinator.data.devices
    ]
    entities.extend(
        SplashMeResetVolumeButton(coordinator, device, kind)
        for device in coordinator.data.devices
        for kind in ("acid", "chlorine")
    )
    async_add_entities(entities)


class SplashMeRefreshDeviceDataButton(SplashMeDeviceEntity, ButtonEntity):
    """Refresh detail data for a specific SplashMe device."""

    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, device) -> None:
        """Initialize the refresh button."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_refresh_device_data"
        self._attr_name = "Refresh"

    @property
    def available(self) -> bool:
        """Return whether the device is online and can be refreshed."""
        return any(
            device.device_id == self.device.device_id and device.online
            for device in self.coordinator.data.devices
        )

    async def async_press(self) -> None:
        """Refresh device detail data on demand."""
        if not self.available:
            raise HomeAssistantError("Device is offline")

        await self.coordinator.async_refresh_device_snapshot(self.device.device_id)


class SplashMeResetVolumeButton(SplashMeDeviceEntity, ButtonEntity):
    """Reset the remaining acid/chlorine volume back to the drum capacity.

    Used after swapping in a full drum.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:barrel"

    def __init__(self, coordinator, device, kind: str) -> None:
        """Initialize the button."""
        super().__init__(coordinator, device)
        self._kind = kind
        self._attr_unique_id = f"{device.device_id}_reset_{kind}_volume"
        self._attr_name = (
            "Reset Acid Volume" if kind == "acid" else "Reset Chlorine Volume"
        )

    async def async_press(self) -> None:
        """Trigger the volume reset."""
        if self._kind == "acid":
            await self.coordinator.async_update_ph_settings(
                self.device.device_id, {"reset_acid_volume": True}
            )
        else:
            await self.coordinator.async_update_chlorine_settings(
                self.device.device_id, {"reset_chlorine_volume": True}
            )


class SplashMeLanRefreshButton(SplashMeLanEntity, ButtonEntity):
    """Poll the LAN device now."""

    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry) -> None:
        """Initialize the refresh button."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self.unique_id_base}_refresh_device_data"
        self._attr_name = "Refresh"

    async def async_press(self) -> None:
        """Request a poll."""
        await self.coordinator.async_request_refresh()


class SplashMeLanResetVolumeButton(SplashMeLanEntity, ButtonEntity):
    """Reset the remaining acid/chlorine volume back to the drum capacity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:barrel"

    def __init__(self, coordinator, entry, kind: str) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry)
        self._kind = kind
        self._attr_unique_id = f"{self.unique_id_base}_reset_{kind}_volume"
        self._attr_name = "Reset Acid Volume" if kind == "acid" else "Reset Chlorine Volume"

    async def async_press(self) -> None:
        """Trigger the volume reset."""
        if self._kind == "acid":
            await self.coordinator.async_update_ph_settings({"reset_acid_volume": True})
        else:
            await self.coordinator.async_update_chlorine_settings({"reset_chlorine_volume": True})


class SplashMeLanResetDashboardButton(SplashMeLanEntity, ButtonEntity):
    """Put the Pool dashboard back to the generated layout, dropping edits made in the UI.

    An edited dashboard is never regenerated; after a reset it follows the
    equipment and integration updates again.
    """

    _attr_icon = "mdi:view-dashboard"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self.unique_id_base}_reset_dashboard"
        self._attr_name = "Reset Pool Dashboard"

    async def async_press(self) -> None:
        """Regenerate the dashboard."""
        await async_register_dashboard(self.hass, self._entry, self.coordinator, reset=True)


class SplashMeLanLightActionButton(SplashMeLanEntity, ButtonEntity):
    """One-shot light command: next colour, save, sync, brightness step."""

    _attr_icon = "mdi:palette"

    def __init__(self, coordinator, entry, slot: int, action: LightAction) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry)
        self._slot = slot
        self._action = action
        self._attr_unique_id = f"{self.unique_id_base}_light_{slot}_{action.key}"

    @property
    def name(self) -> str:
        """Return the light's current name plus the action."""
        aux = self.coordinator.data.aux_by_slot(self._slot)
        light_name = aux.display_name if aux else f"Light {self._slot}"
        return f"{light_name} {self._action.name}"

    async def async_press(self) -> None:
        """Run the light sequence."""
        await self.coordinator.async_set_light_colour(
            self._slot, self._action.colour, settle_seconds=self._action.settle_seconds
        )
