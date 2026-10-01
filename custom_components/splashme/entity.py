"""Entity helpers for SplashMe."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import SplashMeDataCoordinator, SplashMeDevice


class SplashMeCoordinatorEntity(CoordinatorEntity[SplashMeDataCoordinator]):
    """Base entity tied to the SplashMe coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SplashMeDataCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)


def _device_model(device: SplashMeDevice) -> str:
    """Return the device model label shown in Home Assistant."""
    return f"Pool Controller ({'Online' if device.online else 'Offline'})"


class SplashMeHubEntity(SplashMeCoordinatorEntity):
    """Entity owned by the integration itself."""

    def __init__(self, coordinator: SplashMeDataCoordinator, entry_id: str) -> None:
        """Initialize the hub entity."""
        super().__init__(coordinator)
        del entry_id


class SplashMeDeviceEntity(SplashMeCoordinatorEntity):
    """Entity backed by a specific SplashMe device."""

    def __init__(self, coordinator: SplashMeDataCoordinator, device: SplashMeDevice) -> None:
        """Initialize the device entity."""
        super().__init__(coordinator)
        self.device = device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer=MANUFACTURER,
            name=device.display_name,
            model=_device_model(device),
            serial_number=device.device_id,
        )

    @property
    def available(self) -> bool:
        """Return availability based on the latest snapshot."""
        return self.coordinator.data.snapshots.get(self.device.device_id) is not None
