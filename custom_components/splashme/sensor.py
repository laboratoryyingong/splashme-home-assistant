"""Sensor entities for SplashMe."""

from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SplashMeConfigEntry, is_lan_entry
from . import pv2
from .coordinator import SplashMeDeviceSnapshot
from .entity import SplashMeDeviceEntity
from .lan import SplashMeLanEntity

PUMP_SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="pump_brand",
        name="Pump Brand",
        icon="mdi:pump",
    ),
    SensorEntityDescription(
        key="pump_actual_speed",
        name="Pump Speed",
        native_unit_of_measurement="rpm",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan",
    ),
    SensorEntityDescription(
        key="pump_actual_flowrate",
        name="Pump Flow Rate",
        icon="mdi:waves-arrow-right",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="pump_cooldown_left",
        name="Heater Cooldown Remaining",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:fan-clock",
    ),
)

CHEMISTRY_SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="chemistry_ph",
        name="pH",
        native_unit_of_measurement="pH",
        icon="mdi:flask-outline",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="chemistry_orp",
        name="ORP",
        native_unit_of_measurement="mV",
        icon="mdi:chart-bell-curve-cumulative",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="acid_remaining",
        name="Acid Remaining",
        native_unit_of_measurement="L",
        icon="mdi:barrel-outline",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="chlorine_remaining",
        name="Chlorine Remaining",
        native_unit_of_measurement="L",
        icon="mdi:barrel",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="ph_doses_today",
        name="pH Doses Today",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="chlorine_doses_today",
        name="Chlorine Doses Today",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)

DASHBOARD_SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="dashboard_pressure",
        name="Pressure",
        native_unit_of_measurement="kPa",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge",
    ),
    SensorEntityDescription(
        key="dashboard_pump_mode",
        name="Pump Mode",
        icon="mdi:pump",
    ),
)


def _ml_to_l(value: Any) -> float | None:
    """Convert a millilitre wire value to litres."""
    return None if value is None else value / 1000


def _pump_mode_label(value: Any) -> str | None:
    """Map main_pump_status (0 off / 1-20 schedule# / 99 manual) to a label."""
    if value is None:
        return None
    mode = int(value)
    if mode == 0:
        return "Off"
    if mode == 99:
        return "Manual"
    return f"Schedule {mode}"


TEMPERATURE_SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="temperature_water",
        name="Water Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:pool-thermometer",
    ),
    SensorEntityDescription(
        key="temperature_ambient",
        name="Ambient Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer",
    ),
)


def _flow_settled(snapshot: SplashMeDeviceSnapshot) -> bool:
    # Without flow the probes sit in still water: pH, ORP and water temperature are
    # only valid once the controller reports the chemistry stable. No dashboard data:
    # take the reading.
    return (snapshot.dashboard or {}).get("chemistry_stable") is not False


# Held while the pump is off (see HoldsLastReading).
HELD_VALUE_KEYS = {"chemistry_ph", "chemistry_orp", "temperature_water"}

VALUE_FNS: dict[str, Any] = {
    "pump_brand": lambda snapshot: snapshot.pump.pump_brand if snapshot.pump else None,
    "pump_actual_speed": lambda snapshot: snapshot.pump.actual_speed if snapshot.pump else None,
    "pump_actual_flowrate": lambda snapshot: (
        snapshot.pump.actual_flowrate if snapshot.pump else None
    ),
    # Firmware omits pump_cooldown_left when no cool-down is running.
    "pump_cooldown_left": lambda snapshot: (
        None
        if snapshot.pump is None
        else (snapshot.pump.cooldown_left or 0) if snapshot.pump.cooldown else 0
    ),
    "chemistry_ph": lambda snapshot: (
        snapshot.chemistry.ph_value / 10
        if snapshot.chemistry
        and snapshot.chemistry.ph_value is not None
        and _flow_settled(snapshot)
        else None
    ),
    "chemistry_orp": lambda snapshot: (
        snapshot.chemistry.orp_value
        if snapshot.chemistry and _flow_settled(snapshot)
        else None
    ),
    "temperature_water": lambda snapshot: (
        snapshot.temperature.water_temp
        if snapshot.temperature and _flow_settled(snapshot)
        else None
    ),
    "temperature_ambient": lambda snapshot: (
        snapshot.temperature.ambient_temp if snapshot.temperature else None
    ),
    # Drum volumes travel in mL (mobile app convention); show litres.
    "acid_remaining": lambda snapshot: _ml_to_l(
        (snapshot.ph_settings or {}).get("remain_acid_volume")
    ),
    "chlorine_remaining": lambda snapshot: _ml_to_l(
        (snapshot.chlorine_settings or {}).get("remain_chlorine_volume")
    ),
    "ph_doses_today": lambda snapshot: (
        (snapshot.ph_settings or {}).get("current_dose_today")
    ),
    "chlorine_doses_today": lambda snapshot: (
        (snapshot.chlorine_settings or {}).get("current_dose_today")
    ),
    "dashboard_pressure": lambda snapshot: (
        (snapshot.dashboard or {}).get("actual_pressure")
    ),
    "dashboard_pump_mode": lambda snapshot: _pump_mode_label(
        (snapshot.dashboard or {}).get("main_pump_status")
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SplashMeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SplashMe sensors."""
    del hass
    if is_lan_entry(entry):
        coordinator = entry.runtime_data.coordinator
        entities: list[SensorEntity] = [
            (SplashMeLanHeldSensor if key in LAN_HELD_KEYS else SplashMeLanSensor)(
                coordinator, entry, key, name, unit, device_class, icon, fn
            )
            for key, name, unit, device_class, icon, fn in LAN_SENSORS
        ]
        entities.extend(
            SplashMeLanSensor(coordinator, entry, key, name, None, None, icon, fn, measurement=False)
            for key, name, icon, fn in LAN_TEXT_SENSORS
        )
        async_add_entities(entities)
        return

    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = []
    for device in coordinator.data.devices:
        entities.extend(
            (SplashMeHeldValueSensor if description.key in HELD_VALUE_KEYS else SplashMeValueSensor)(
                coordinator, device, description
            )
            for description in (
                PUMP_SENSOR_DESCRIPTIONS
                + CHEMISTRY_SENSOR_DESCRIPTIONS
                + TEMPERATURE_SENSOR_DESCRIPTIONS
                + DASHBOARD_SENSOR_DESCRIPTIONS
            )
        )

    async_add_entities(entities)


class HoldsLastReading(RestoreSensor):
    """Keeps showing the last valid reading while the pump is off.

    The value function returns None until the controller reports the chemistry
    stable (flow held for 2 minutes). Until then the reading taken before the pump
    stopped stays, across restarts too; unknown only before the first valid one.
    """

    _held: Any = None

    async def async_added_to_hass(self) -> None:
        """Restore the held reading, then take the live one if it is valid."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._held = last.native_value
        self._hold()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._hold()
        super()._handle_coordinator_update()

    def _hold(self) -> None:
        if (value := super().native_value) is not None:
            self._held = value

    @property
    def native_value(self) -> Any:
        """Return the latest valid reading."""
        return self._held


class SplashMeValueSensor(SplashMeDeviceEntity, SensorEntity):
    """Sensor backed by the device snapshot."""

    entity_description: SensorEntityDescription

    def __init__(self, coordinator, device, description: SensorEntityDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self.entity_description = description
        self._value_fn = VALUE_FNS[description.key]
        self._attr_unique_id = f"{device.device_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the current value."""
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return self._value_fn(snapshot)

    @property
    def _snapshot(self) -> SplashMeDeviceSnapshot | None:
        """Return the latest snapshot."""
        return self.coordinator.data.snapshots.get(self.device.device_id)


class SplashMeHeldValueSensor(HoldsLastReading, SplashMeValueSensor):
    """Cloud reading that is only valid with flow (pH, ORP, water temperature)."""


# LAN sensors read straight off the state frame: (key, name, unit, device_class, icon, value fn)
def _lan_ambient(data):
    # 0 means the ambient/solar sensor is not fitted.
    t = data.telemetry
    return None if t is None or t.ambient_temp == 0 else t.ambient_temp


def _lan_settled(data, field):
    # Without flow the probes sit in still water: the reading is only valid once
    # the controller says the chemistry is stable (flow held for 2 minutes).
    t = data.telemetry
    return getattr(t, field) if t is not None and t.flag(pv2.FLAG_CHEM_STABLE) else None


# Held while the pump is off (see HoldsLastReading); flow and pressure stay live.
LAN_HELD_KEYS = {"water_temp", "actual_ph", "actual_orp"}

LAN_SENSORS: tuple[tuple[str, str, str | None, SensorDeviceClass | None, str | None, Any], ...] = (
    ("water_temp", "Water Temperature", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, "mdi:pool-thermometer",
     lambda d: _lan_settled(d, "water_temp")),
    ("solar_temp", "Ambient Temperature", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, "mdi:thermometer", _lan_ambient),
    ("actual_ph", "pH", "pH", None, "mdi:flask-outline", lambda d: _lan_settled(d, "ph")),
    ("actual_orp", "ORP", "mV", None, "mdi:chart-bell-curve-cumulative", lambda d: _lan_settled(d, "orp")),
    ("actual_flow_rate", "Flow Rate", "L/min", None, "mdi:waves-arrow-right", lambda d: d.telemetry.flow if d.telemetry else None),
    ("actual_pressure", "Pressure", "kPa", SensorDeviceClass.PRESSURE, "mdi:gauge", lambda d: d.telemetry.pressure if d.telemetry else None),
    ("actual_pump_speed", "Pump Speed", "%", None, "mdi:fan", lambda d: d.telemetry.pump_speed if d.telemetry else None),
    ("pump_power", "Pump Power", "W", SensorDeviceClass.POWER, "mdi:flash", lambda d: d.telemetry.pump_power if d.telemetry else None),
    ("pump_cooldown_left", "Heater Cooldown Remaining", UnitOfTime.SECONDS, SensorDeviceClass.DURATION, "mdi:fan-clock",
     lambda d: d.telemetry.pump_cooldown_remain if d.telemetry else None),
    # Drum volumes travel in mL (mobile app convention); show litres.
    ("acid_remaining", "Acid Remaining", "L", None, "mdi:barrel-outline", lambda d: d.config.remain_acid_volume / 1000 if d.config else None),
    ("chlorine_remaining", "Chlorine Remaining", "L", None, "mdi:barrel", lambda d: d.config.remain_chlorine_volume / 1000 if d.config else None),
    ("ph_doses_today", "pH Doses Today", None, None, "mdi:counter", lambda d: (d.ph_settings or {}).get("current_dose_today")),
    ("chlorine_doses_today", "Chlorine Doses Today", None, None, "mdi:counter", lambda d: (d.chlorine_settings or {}).get("current_dose_today")),
)

# Non-numeric LAN sensors (no state class)
LAN_TEXT_SENSORS: tuple[tuple[str, str, str, Any], ...] = (
    ("pump_mode", "Pump Mode", "mdi:pump", lambda d: _pump_mode_label(d.telemetry.main_pump) if d.telemetry else None),
    ("pump_brand", "Pump Brand", "mdi:pump", lambda d: d.pump.brand if d.pump else None),
    ("pump_model", "Pump Model", "mdi:pump", lambda d: d.pump.model if d.pump else None),
)


class SplashMeLanSensor(SplashMeLanEntity, SensorEntity):
    """Sensor backed by the LAN state frame."""

    def __init__(
        self,
        coordinator,
        entry,
        key: str,
        name: str,
        unit: str | None,
        device_class: SensorDeviceClass | None,
        icon: str | None,
        value_fn: Any,
        measurement: bool = True,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._value_fn = value_fn
        self._attr_unique_id = f"{self.unique_id_base}_{key}"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        if device_class is not None:
            self._attr_device_class = device_class
        if icon is not None:
            self._attr_icon = icon
        if measurement:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Any:
        """Return the current value."""
        return self._value_fn(self.coordinator.data)


class SplashMeLanHeldSensor(HoldsLastReading, SplashMeLanSensor):
    """LAN reading that is only valid with flow (pH, ORP, water temperature)."""
