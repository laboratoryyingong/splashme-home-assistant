"""Number entities for SplashMe chemistry settings."""

from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SplashMeConfigEntry, is_lan_entry
from .const import OPT_LAN_PUMP_SPEED_SETPOINT
from .entity import SplashMeDeviceEntity
from .lan import DEFAULT_PUMP_SPEED, SplashMeLanCoordinator, SplashMeLanEntity
from .slots import SplashMeLanSlotEntities


@dataclass(frozen=True, slots=True)
class SplashMeNumberSpec:
    """One writable chemistry setting."""

    field: str
    name: str
    kind: str  # "ph" or "chlorine"
    unit: str | None
    min_value: float
    max_value: float
    step: float
    scale: float  # raw = value * scale
    icon: str


NUMBER_SPECS: tuple[SplashMeNumberSpec, ...] = (
    SplashMeNumberSpec(
        field="desired_ph_level",
        name="Desired pH",
        kind="ph",
        unit="pH",
        min_value=6.5,
        max_value=8.0,
        step=0.1,
        scale=10,
        icon="mdi:flask-outline",
    ),
    SplashMeNumberSpec(
        field="desired_orp_level_mineral",
        name="Desired ORP (Mineral)",
        kind="chlorine",
        unit="mV",
        min_value=400,
        max_value=900,
        step=10,
        scale=1,
        icon="mdi:chart-bell-curve-cumulative",
    ),
    SplashMeNumberSpec(
        field="desired_orp_level_liquid",
        name="Desired ORP (Liquid)",
        kind="chlorine",
        unit="mV",
        min_value=400,
        max_value=900,
        step=10,
        scale=1,
        icon="mdi:chart-bell-curve-cumulative",
    ),
    SplashMeNumberSpec(
        field="acid_drum_volume",
        name="Acid Drum Volume",
        kind="ph",
        unit="L",
        min_value=0,
        max_value=1000,
        step=0.01,
        scale=1000,  # device stores mL (mobile app convention)
        icon="mdi:barrel-outline",
    ),
    SplashMeNumberSpec(
        field="chlorine_drum_volume",
        name="Chlorine Drum Volume",
        kind="chlorine",
        unit="L",
        min_value=0,
        max_value=1000,
        step=0.01,
        scale=1000,  # device stores mL (mobile app convention)
        icon="mdi:barrel",
    ),
)


SINGLE_SPEED_PUMP_BRAND = "Single Speed"
PUMP_SPEED_MIN = 40
PUMP_SPEED_MAX = 100


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SplashMeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SplashMe numbers."""
    if is_lan_entry(entry):
        await _async_setup_lan_numbers(hass, entry, async_add_entities)
        return
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SplashMeSettingsNumber(coordinator, device, spec)
        for device in coordinator.data.devices
        for spec in NUMBER_SPECS
    )

    # Pump speed control only exists for variable speed pumps; pump identity
    # arrives with the device snapshot, so add these lazily.
    known_pump_numbers: set[str] = set()

    @callback
    def _async_add_pump_speed_numbers() -> None:
        entities = []
        for device in coordinator.data.devices:
            if device.device_id in known_pump_numbers:
                continue
            snapshot = coordinator.data.snapshots.get(device.device_id)
            if snapshot is None or snapshot.pump is None:
                continue
            # Wait until the brand is actually known: a failed pumpInfo poll
            # leaves it empty and must not create a speed control that later
            # turns out to belong to a single speed pump.
            if not snapshot.pump.pump_brand:
                continue
            if snapshot.pump.pump_brand == SINGLE_SPEED_PUMP_BRAND:
                known_pump_numbers.add(device.device_id)  # never variable; stop checking
                continue
            known_pump_numbers.add(device.device_id)
            entities.append(SplashMePumpSpeedNumber(coordinator, entry, device))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_add_pump_speed_numbers))
    _async_add_pump_speed_numbers()


async def _async_setup_lan_numbers(hass, entry, async_add_entities) -> None:
    """Chemistry setpoints, heating targets, plus the pump speed slider for variable speed pumps."""
    coordinator: SplashMeLanCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        SplashMeLanSettingsNumber(coordinator, entry, spec) for spec in NUMBER_SPECS
    )

    # One target temperature per body of water that has something to heat it.
    def _heating_targets(data):
        wanted = {}
        if data.has_pool_heating:
            wanted["heating_pool_target"] = "pool"
        if data.has_spa_heating:
            wanted["heating_spa_target"] = "spa"
        return wanted

    heating = SplashMeLanSlotEntities(
        hass, entry, coordinator, "heating_", _heating_targets,
        lambda kind: SplashMeLanTargetTempNumber(coordinator, entry, kind),
    )
    await heating.async_sync()
    entry.runtime_data.slot_entities.append(heating)

    # The pump reads as "Single Speed" until the device has identified it
    # (a while after boot), so add the slider as soon as a variable speed
    # model shows up.
    added = False

    @callback
    def _async_add_pump_speed_number() -> None:
        nonlocal added
        pump = coordinator.data.pump
        if added or pump is None or not pump.model or pump.brand == SINGLE_SPEED_PUMP_BRAND:
            return
        added = True
        async_add_entities([SplashMeLanPumpSpeedNumber(coordinator, entry)])

    entry.async_on_unload(coordinator.async_add_listener(_async_add_pump_speed_number))
    _async_add_pump_speed_number()


class SplashMeLanSettingsNumber(SplashMeLanEntity, NumberEntity):
    """Writable chemistry setting on a LAN device (signed cmd passthrough)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, entry, spec: SplashMeNumberSpec) -> None:
        """Initialize the number."""
        super().__init__(coordinator, entry)
        self._spec = spec
        self._attr_unique_id = f"{self.unique_id_base}_{spec.field}"
        self._attr_name = spec.name
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_native_min_value = spec.min_value
        self._attr_native_max_value = spec.max_value
        self._attr_native_step = spec.step
        self._attr_icon = spec.icon

    def _settings(self) -> dict | None:
        data = self.coordinator.data
        return data.ph_settings if self._spec.kind == "ph" else data.chlorine_settings

    @property
    def available(self) -> bool:
        """Settings must be present to read or write."""
        return super().available and self._settings() is not None

    @property
    def native_value(self) -> float | None:
        """Return the current setting value."""
        settings = self._settings()
        raw = None if settings is None else settings.get(self._spec.field)
        return None if raw is None else raw / self._spec.scale

    async def async_set_native_value(self, value: float) -> None:
        """Write the new setting value."""
        changes = {self._spec.field: round(value * self._spec.scale)}
        if self._spec.kind == "ph":
            await self.coordinator.async_update_ph_settings(changes)
        else:
            await self.coordinator.async_update_chlorine_settings(changes)


class SplashMeLanTargetTempNumber(SplashMeLanEntity, NumberEntity):
    """Pool or spa target temperature; the firmware heats towards it."""

    _attr_native_min_value = 0
    _attr_native_max_value = 40
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_icon = "mdi:thermometer"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, entry, kind: str) -> None:
        """Initialize for "pool" or "spa"."""
        super().__init__(coordinator, entry)
        self._kind = kind
        self._attr_unique_id = f"{self.unique_id_base}_heating_{kind}_target"
        self._attr_name = f"{kind.title()} Target Temperature"

    @property
    def available(self) -> bool:
        """Config section must have been received."""
        return super().available and self.coordinator.data.config is not None

    @property
    def native_value(self) -> float | None:
        """Return the target from the config section."""
        config = self.coordinator.data.config
        if config is None:
            return None
        return float(config.desired_water_temp if self._kind == "pool" else config.spa_desired_temp)

    async def async_set_native_value(self, value: float) -> None:
        """Write the new target."""
        await self.coordinator.async_set_heating({f"desire_{self._kind}_temp": int(value)})


class SplashMeLanPumpSpeedNumber(SplashMeLanEntity, NumberEntity):
    """Speed setpoint for a variable speed pump on a LAN device.

    While the pump runs the new speed is sent immediately; while stopped it
    is stored and applied when the pump switch next turns on.
    """

    _attr_native_min_value = PUMP_SPEED_MIN
    _attr_native_max_value = PUMP_SPEED_MAX
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:fan-chevron-up"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry) -> None:
        """Initialize the pump speed number."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self.unique_id_base}_pump_speed_setpoint"
        self._attr_name = "Filter Pump Speed"

    def _running_speed(self) -> int:
        telemetry = self.coordinator.data.telemetry
        return telemetry.pump_speed if telemetry else 0

    @property
    def native_value(self) -> float:
        """Running: the live speed; stopped: the stored setpoint."""
        if speed := self._running_speed():
            return float(speed)
        return float(self._entry.options.get(OPT_LAN_PUMP_SPEED_SETPOINT) or DEFAULT_PUMP_SPEED)

    async def async_set_native_value(self, value: float) -> None:
        """Store the setpoint; push to the device only while the pump runs."""
        speed = int(value)
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, OPT_LAN_PUMP_SPEED_SETPOINT: speed}
        )
        pump = self.coordinator.data.main_pump
        if pump is not None and self._running_speed():
            await self.coordinator.async_set_aux(pump.slot, True, pump_speed=speed)
        self.async_write_ha_state()


class SplashMeSettingsNumber(SplashMeDeviceEntity, NumberEntity):
    """Writable chemistry setting backed by the pH/chlorine settings object."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, device, spec: SplashMeNumberSpec) -> None:
        """Initialize the number."""
        super().__init__(coordinator, device)
        self._spec = spec
        self._attr_unique_id = f"{device.device_id}_{spec.field}"
        self._attr_name = spec.name
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_native_min_value = spec.min_value
        self._attr_native_max_value = spec.max_value
        self._attr_native_step = spec.step
        self._attr_icon = spec.icon

    def _settings(self) -> dict | None:
        snapshot = self.coordinator.data.snapshots.get(self.device.device_id)
        if snapshot is None:
            return None
        return (
            snapshot.ph_settings
            if self._spec.kind == "ph"
            else snapshot.chlorine_settings
        )

    @property
    def available(self) -> bool:
        """Settings must be present to read or write."""
        return self._settings() is not None

    @property
    def native_value(self) -> float | None:
        """Return the current setting value."""
        settings = self._settings()
        if settings is None:
            return None
        raw = settings.get(self._spec.field)
        return None if raw is None else raw / self._spec.scale

    async def async_set_native_value(self, value: float) -> None:
        """Write the new setting value."""
        raw = round(value * self._spec.scale)
        changes = {self._spec.field: raw}
        if self._spec.kind == "ph":
            await self.coordinator.async_update_ph_settings(
                self.device.device_id, changes
            )
        else:
            await self.coordinator.async_update_chlorine_settings(
                self.device.device_id, changes
            )


class SplashMePumpSpeedNumber(SplashMeDeviceEntity, NumberEntity):
    """Speed setpoint for a variable speed pump.

    Changing the slider while the pump is stopped only stores the setpoint
    (applied when the pump switch next turns on); while running it is sent
    to the device immediately. Actual RPM stays on the Pump Speed sensor.
    """

    _attr_native_min_value = PUMP_SPEED_MIN
    _attr_native_max_value = PUMP_SPEED_MAX
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:fan-chevron-up"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry, device) -> None:
        """Initialize the pump speed number."""
        super().__init__(coordinator, device)
        self._entry = entry
        self._attr_unique_id = f"{device.device_id}_pump_speed_setpoint"
        # 命名紧跟 "Filter Pump"：设备页 Controls 按字母序排列，
        # 这样速度滑条恰好排在泵开关的下一行（两者强相关）。
        self._attr_name = "Filter Pump Speed"

    def _main_pump_aux(self):
        snapshot = self.coordinator.data.snapshots.get(self.device.device_id)
        if snapshot is None:
            return None
        for aux in snapshot.aux:
            if aux.aux_type_code == 31:  # main pump slot
                return aux
        return None

    def _pump_running(self) -> bool:
        """True only when the motor actually spins (actual_speed > 0);
        the slot's aux_status can stay set on a stopped VSD pump."""
        snapshot = self.coordinator.data.snapshots.get(self.device.device_id)
        pump = snapshot.pump if snapshot else None
        if pump is not None and pump.actual_speed is not None:
            return pump.actual_speed > 0
        aux = self._main_pump_aux()
        return bool(aux and aux.aux_status)

    @property
    def available(self) -> bool:
        """Available while the device snapshot (and its pump) is known."""
        snapshot = self.coordinator.data.snapshots.get(self.device.device_id)
        return snapshot is not None and snapshot.pump is not None

    @property
    def native_value(self) -> float | None:
        """Running: the pump's live speed setting; stopped: the stored setpoint."""
        aux = self._main_pump_aux()
        if aux is not None and self._pump_running() and aux.pump_speed:
            return float(aux.pump_speed)
        setpoint = self.coordinator.pump_speed_setpoint(self.device.device_id)
        if setpoint is not None:
            return float(setpoint)
        if aux is not None and aux.pump_speed:
            return float(aux.pump_speed)
        return float(PUMP_SPEED_MIN)

    async def async_set_native_value(self, value: float) -> None:
        """Store the setpoint; push to the device only while the pump runs."""
        speed = int(value)
        self.coordinator.set_pump_speed_setpoint(self.device.device_id, speed)

        aux = self._main_pump_aux()
        if aux is not None and self._pump_running():
            await self._entry.runtime_data.api.async_set_pump_info(
                self.device.device_id,
                aux_slot_num=aux.aux_slot_num,
                enabled=True,
                pump_speed=speed,
            )
            await self.coordinator.async_refresh_device_snapshot(self.device.device_id)
        else:
            self.async_write_ha_state()
