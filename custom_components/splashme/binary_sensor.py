"""Binary sensor entities for SplashMe."""

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SplashMeConfigEntry, is_lan_entry
from . import pv2
from .entity import SplashMeDeviceEntity
from .lan import SplashMeLanEntity
from .slots import SplashMeLanSlotEntities

# LAN binary sensors off the state frame flags: (key, name, device_class, icon, value fn)
LAN_BINARY_SENSORS: tuple[tuple[str, str, BinarySensorDeviceClass | None, str | None, Any], ...] = (
    ("heater_state", "Heater", BinarySensorDeviceClass.RUNNING, None, lambda t: t.flag(pv2.FLAG_HEATER)),
    ("solar_state", "Solar Heating", BinarySensorDeviceClass.RUNNING, None, lambda t: t.flag(pv2.FLAG_SOLAR)),
    ("spa_heater_state", "Spa Heater", BinarySensorDeviceClass.RUNNING, None, lambda t: t.flag(pv2.FLAG_SPA_HEATER)),
    ("ph_switch_status", "pH Dosing", BinarySensorDeviceClass.RUNNING, None, lambda t: t.flag(pv2.FLAG_PH_SWITCH)),
    ("orp_switch_status", "Chlorine Dosing", BinarySensorDeviceClass.RUNNING, None, lambda t: t.flag(pv2.FLAG_ORP_SWITCH)),
    ("chemistry_stable", "Chemistry Stable", None, "mdi:flask-round-bottom", lambda t: t.flag(pv2.FLAG_CHEM_STABLE)),
    # The frame packs SystemInfo.dryrun_status & 0x3: 3 = off/normal, 1 = retries
    # exceeded, and "dry running" (4) wraps to 0 — so anything but 3 is a problem.
    ("dry_run", "Dry Run", BinarySensorDeviceClass.PROBLEM, None, lambda t: t.dry_run != 3),
    ("pump_cooldown", "Heater Cooldown", BinarySensorDeviceClass.RUNNING, "mdi:fan-clock", lambda t: t.flag2(pv2.FLAG2_PUMP_COOLING)),
    ("pump_priming", "Pump Priming", BinarySensorDeviceClass.RUNNING, "mdi:water-pump", lambda t: t.flag2(pv2.FLAG2_PUMP_PRIMING)),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SplashMeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SplashMe binary sensors."""
    coordinator = entry.runtime_data.coordinator
    if is_lan_entry(entry):
        entities: list[BinarySensorEntity] = [
            SplashMeLanBinarySensor(coordinator, entry, key, name, device_class, icon, fn)
            for key, name, device_class, icon, fn in LAN_BINARY_SENSORS
        ]
        entities.append(SplashMeLanConnectivityBinarySensor(coordinator, entry))
        async_add_entities(entities)
        # One connectivity sensor per LoRa expander board that has equipment
        # assigned (flags bits 10/11 for boards 1-2, flags2 bits 7/8 for 3-4),
        # following slot assignment.
        slots = SplashMeLanSlotEntities(
            hass,
            entry,
            coordinator,
            "expander_",
            lambda data: {
                f"expander_{board}": board
                for board in pv2.EXPANDER_SLOTS
                if data.expander_present(board)
            },
            lambda board: SplashMeLanBinarySensor(
                coordinator,
                entry,
                f"expander_{board}",
                f"Expander {board}",
                BinarySensorDeviceClass.CONNECTIVITY,
                "mdi:access-point",
                lambda t: t.expander_connected(board),
            ),
        )
        await slots.async_sync()
        entry.runtime_data.slot_entities.append(slots)
        return

    entities: list[BinarySensorEntity] = [
        SplashMeConnectivityBinarySensor(coordinator, device)
        for device in coordinator.data.devices
    ]
    for device in coordinator.data.devices:
        entities.extend(
            SplashMeDashboardBinarySensor(coordinator, device, key, name, device_class)
            for key, name, device_class in CLOUD_DASHBOARD_BINARY_SENSORS
        )
        entities.append(SplashMeDashboardDryRunBinarySensor(coordinator, device))
        entities.append(SplashMePumpCooldownBinarySensor(coordinator, device))
    async_add_entities(entities)


class SplashMeConnectivityBinarySensor(SplashMeDeviceEntity, BinarySensorEntity):
    """Expose device connectivity state."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, device) -> None:
        """Initialize the connectivity sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_connectivity"
        self._attr_name = "Connectivity"

    @property
    def is_on(self) -> bool:
        """Return whether the device is online."""
        for device in self.coordinator.data.devices:
            if device.device_id == self.device.device_id:
                return device.online
        return False

    @property
    def available(self) -> bool:
        """Return whether the device is still present in the coordinator."""
        return any(
            device.device_id == self.device.device_id
            for device in self.coordinator.data.devices
        )


class SplashMeLanBinarySensor(SplashMeLanEntity, BinarySensorEntity):
    """Binary sensor backed by a state frame flag of a LAN device."""

    def __init__(
        self,
        coordinator,
        entry,
        key: str,
        name: str,
        device_class: BinarySensorDeviceClass | None,
        icon: str | None,
        value_fn: Any,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._value_fn = value_fn
        self._attr_unique_id = f"{self.unique_id_base}_{key}"
        self._attr_name = name
        if device_class is not None:
            self._attr_device_class = device_class
        if icon is not None:
            self._attr_icon = icon

    @property
    def is_on(self) -> bool | None:
        """Return the flag."""
        telemetry = self.coordinator.data.telemetry
        return None if telemetry is None else bool(self._value_fn(telemetry))


class SplashMeLanConnectivityBinarySensor(SplashMeLanEntity, BinarySensorEntity):
    """Whether the LAN device is answering polls (stays available when it is not)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, entry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self.unique_id_base}_connectivity"
        self._attr_name = "Connectivity"

    @property
    def available(self) -> bool:
        """Always available so an offline device reads as off, not unknown."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether the last poll succeeded."""
        return self.coordinator.last_update_success


# Cloud dashboard binary sensors: (dashboard field, name, device_class)
CLOUD_DASHBOARD_BINARY_SENSORS: tuple[
    tuple[str, str, BinarySensorDeviceClass | None], ...
] = (
    ("ph_switch_status", "pH Dosing Active", BinarySensorDeviceClass.RUNNING),
    ("orp_switch_status", "Chlorine Dosing Active", BinarySensorDeviceClass.RUNNING),
    ("heater_state", "Heater", BinarySensorDeviceClass.RUNNING),
    ("solar_state", "Solar Heating", BinarySensorDeviceClass.RUNNING),
    ("spa_heater_state", "Spa Heater", BinarySensorDeviceClass.RUNNING),
    ("chemistry_stable", "Chemistry Stable", None),
)


class SplashMeDashboardBinarySensor(SplashMeDeviceEntity, BinarySensorEntity):
    """Binary sensor backed by a dashboard telemetry flag (cloud devices)."""

    def __init__(self, coordinator, device, key, name, device_class) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._key = key
        self._attr_unique_id = f"{device.device_id}_dash_{key}"
        self._attr_name = name
        if device_class is not None:
            self._attr_device_class = device_class

    def _dashboard(self) -> dict | None:
        snapshot = self.coordinator.data.snapshots.get(self.device.device_id)
        return snapshot.dashboard if snapshot else None

    @property
    def available(self) -> bool:
        """Dashboard data must be present."""
        dashboard = self._dashboard()
        return dashboard is not None and self._key in dashboard

    @property
    def is_on(self) -> bool | None:
        """Return the dashboard flag."""
        dashboard = self._dashboard()
        if dashboard is None:
            return None
        value = dashboard.get(self._key)
        return None if value is None else bool(value)


class SplashMeDashboardDryRunBinarySensor(SplashMeDeviceEntity, BinarySensorEntity):
    """Dry-run problem indicator from dashboard telemetry (>0 = problem)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_dash_dry_run"
        self._attr_name = "Dry Run"

    @property
    def available(self) -> bool:
        """Dashboard data must be present."""
        snapshot = self.coordinator.data.snapshots.get(self.device.device_id)
        return snapshot is not None and snapshot.dashboard is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether a dry-run problem is active."""
        snapshot = self.coordinator.data.snapshots.get(self.device.device_id)
        if snapshot is None or snapshot.dashboard is None:
            return None
        value = snapshot.dashboard.get("dry_run_status")
        return None if value is None else int(value) > 0


class SplashMePumpCooldownBinarySensor(SplashMeDeviceEntity, BinarySensorEntity):
    """Heater cool-down indicator: the main pump is being kept running by the
    heater cool-down timer (pumpInfo pump_cooldown)."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:fan-clock"

    def __init__(self, coordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_pump_cooldown"
        self._attr_name = "Heater Cooldown"

    def _pump(self):
        snapshot = self.coordinator.data.snapshots.get(self.device.device_id)
        return snapshot.pump if snapshot else None

    @property
    def available(self) -> bool:
        """Pump data must report the cool-down flag."""
        pump = self._pump()
        return pump is not None and pump.cooldown is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether the heater cool-down is keeping the pump on."""
        pump = self._pump()
        return None if pump is None else pump.cooldown
