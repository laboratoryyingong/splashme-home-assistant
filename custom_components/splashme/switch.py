"""Switch entities for SplashMe."""

import asyncio
from collections.abc import Iterable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SplashMeConfigEntry, is_lan_entry
from .const import OPT_LAN_PUMP_SPEED_SETPOINT
from .coordinator import SplashMeAux, SplashMeDeviceSnapshot
from .entity import SplashMeDeviceEntity
from .lan import (
    CHLORINATOR_TYPE_CODES,
    DEFAULT_PUMP_SPEED,
    LIQUID_CHLORINE_TYPE_CODES,
    MAIN_PUMP_TYPE_CODE as LAN_MAIN_PUMP_TYPE_CODE,
    PH_DOSER_TYPE_CODES,
    POOL_HEATER_TYPE_CODES,
    SOLAR_TYPE_CODES,
    SPA_HEATER_TYPE_CODES,
    SplashMeLanCoordinator,
    SplashMeLanEntity,
)
from .slots import SplashMeLanSlotEntities

MAIN_PUMP_TYPE_CODE = 31
MIN_PUMP_SPEED = 40
STATE_CONFIRM_ATTEMPTS = 6
STATE_CONFIRM_INTERVAL_SECONDS = 2
POOL_PRESSURE_CLEANER = 3
POOL_IN_FLOOR_CLEANING = 4
CLEANER_TYPE_CODES = frozenset({POOL_PRESSURE_CLEANER, POOL_IN_FLOOR_CLEANING})
PRIMARY_SPA_TYPE_CODES = (20, 21, 33, 34)
SPA_SENTINEL_KEY = "__spa__"
HEATER_KINDS = {"pool": POOL_HEATER_TYPE_CODES, "spa": SPA_HEATER_TYPE_CODES, "solar": SOLAR_TYPE_CODES}
HEATER_LABELS = {"pool": "Pool Heater", "spa": "Spa Heater", "solar": "Solar Heater"}
HEATER_ICONS = {"pool": "mdi:fire", "spa": "mdi:hot-tub", "solar": "mdi:solar-power-variant"}
# Dosing enable flags: kind -> (equipment that needs it, settings object, JSON key, label, icon).
DOSING_KINDS = {
    "ph": (PH_DOSER_TYPE_CODES, "ph", "ph_dosing", "pH Dosing Enabled", "mdi:flask-outline"),
    "chlorinator": (CHLORINATOR_TYPE_CODES, "chlorine", "orp_controlled", "Chlorinator Enabled", "mdi:water-plus"),
    "liquid": (LIQUID_CHLORINE_TYPE_CODES, "chlorine", "liquid_chlorine_dosing", "Liquid Chlorine Enabled", "mdi:water-plus"),
}


def _find_primary_spa(aux_list: Iterable[SplashMeAux]) -> SplashMeAux | None:
    """Return the primary Spa mode entry."""
    spa_aux = [aux for aux in aux_list if aux.category == "spa"]
    if not spa_aux:
        return None

    for type_code in PRIMARY_SPA_TYPE_CODES:
        for aux in spa_aux:
            if aux.aux_type_code == type_code:
                return aux

    return spa_aux[0]


def _find_main_pump(aux_list: Iterable[SplashMeAux]) -> SplashMeAux | None:
    """Return the main pump entry."""
    for aux in aux_list:
        if aux.category == "pump_devices" and aux.aux_type_code == MAIN_PUMP_TYPE_CODE:
            return aux

    for aux in aux_list:
        if aux.aux_type_code == MAIN_PUMP_TYPE_CODE:
            return aux

    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SplashMeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SplashMe switches."""
    if is_lan_entry(entry):
        await _async_setup_lan_switches(hass, entry, async_add_entities)
        return

    coordinator = entry.runtime_data.coordinator
    known_aux_keys: dict[str, set[str]] = {}
    known_schedules: dict[str, set[int]] = {}

    @callback
    def _async_add_missing_switches() -> None:
        entities: list[SwitchEntity] = []

        for device in coordinator.data.devices:
            snapshot = coordinator.data.snapshots.get(device.device_id)
            if snapshot is None:
                continue

            device_keys = known_aux_keys.setdefault(device.device_id, set())
            for aux in snapshot.aux:
                if aux.category == "spa":
                    continue
                if aux.key in device_keys:
                    continue
                device_keys.add(aux.key)
                entities.append(SplashMeAuxSwitch(coordinator, entry, device, aux))

            if _find_primary_spa(snapshot.aux) is not None and SPA_SENTINEL_KEY not in device_keys:
                device_keys.add(SPA_SENTINEL_KEY)
                entities.append(SplashMeSpaSwitch(coordinator, entry, device))

            # One switch per device schedule (added as schedules appear).
            sched_nums = known_schedules.setdefault(device.device_id, set())
            for sched in snapshot.schedules or ():
                num = sched.get("schedule_num")
                if num is None or num in sched_nums:
                    continue
                sched_nums.add(num)
                entities.append(SplashMeScheduleSwitch(coordinator, device, num))

        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_add_missing_switches))
    _async_add_missing_switches()


async def _async_setup_lan_switches(hass, entry, async_add_entities) -> None:
    """Aux switches per slot (following reassignment), plus one switch per defined schedule."""
    coordinator: SplashMeLanCoordinator = entry.runtime_data.coordinator
    slots = SplashMeLanSlotEntities(
        hass,
        entry,
        coordinator,
        "aux_",
        lambda data: {f"aux_{aux.slot}": aux.slot for aux in data.aux if aux.is_switchable},
        lambda slot: SplashMeLanAuxSwitch(coordinator, entry, slot),
    )
    await slots.async_sync()
    entry.runtime_data.slot_entities.append(slots)

    # Heating sources the firmware may use, one enable switch each.
    heating = SplashMeLanSlotEntities(
        hass,
        entry,
        coordinator,
        "heating_",
        lambda data: {
            f"heating_{kind}": kind
            for kind, codes in HEATER_KINDS.items()
            if data.has_equipment(codes)
        },
        lambda kind: SplashMeLanHeaterSwitch(coordinator, entry, kind),
    )
    await heating.async_sync()
    entry.runtime_data.slot_entities.append(heating)

    # Dosing enable flags from the pH / chlorine settings, one per assigned doser.
    dosing = SplashMeLanSlotEntities(
        hass,
        entry,
        coordinator,
        "dosing_",
        lambda data: {
            f"dosing_{kind}": kind
            for kind, (codes, *_rest) in DOSING_KINDS.items()
            if data.has_equipment(codes)
        },
        lambda kind: SplashMeLanDosingSwitch(coordinator, entry, kind),
    )
    await dosing.async_sync()
    entry.runtime_data.slot_entities.append(dosing)

    # One Spa Mode switch for all spa actuators (the firmware moves them together).
    spa = SplashMeLanSlotEntities(
        hass,
        entry,
        coordinator,
        "spa_mode",
        lambda data: {"spa_mode": None} if data.spa_actuators else {},
        lambda _: SplashMeLanSpaSwitch(coordinator, entry),
    )
    await spa.async_sync()
    entry.runtime_data.slot_entities.append(spa)

    # One switch per defined schedule; deleted ones leave the registry.
    schedules = SplashMeLanSlotEntities(
        hass,
        entry,
        coordinator,
        "schedule_",
        lambda data: {f"schedule_{s.index}": s.index for s in data.schedules if s.defined},
        lambda index: SplashMeLanScheduleSwitch(coordinator, entry, index),
    )
    await schedules.async_sync()
    entry.runtime_data.slot_entities.append(schedules)


class SplashMeAuxSwitch(SplashMeDeviceEntity, SwitchEntity):
    """Switch mapped to a SplashMe aux."""

    _attr_icon = "mdi:toggle-switch-variant"

    def __init__(
        self,
        coordinator,
        entry: SplashMeConfigEntry,
        device,
        aux: SplashMeAux,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, device)
        self._entry = entry
        self.aux = aux
        self._attr_unique_id = f"{device.device_id}_{aux.key}_switch"
        self._attr_name = aux.display_name

    @property
    def is_on(self) -> bool | None:
        """Return the current aux state."""
        aux = self._get_aux()
        return None if aux is None else self._reports_on(aux)

    def _reports_on(self, aux: SplashMeAux) -> bool:
        """Return whether the device reports this aux as actually running.

        For a variable speed main pump the slot's aux_status can stay set
        while the motor is stopped; the honest signal is the live pump speed
        (documented as 0 when stopped).
        """
        if self._is_main_pump(aux):
            snapshot = self._get_snapshot()
            pump = snapshot.pump if snapshot else None
            if (
                pump is not None
                and (pump.pump_brand or "") != "Single Speed"
                and pump.actual_speed is not None
            ):
                return pump.actual_speed > 0
        return aux.aux_status

    def _get_snapshot(self) -> SplashMeDeviceSnapshot | None:
        """Return the latest device snapshot."""
        return self.coordinator.data.snapshots.get(self.device.device_id)

    def _get_aux(self) -> SplashMeAux | None:
        """Return the latest aux state."""
        snapshot = self._get_snapshot()
        if snapshot is None:
            return None
        for aux in snapshot.aux:
            if aux.key == self.aux.key:
                return aux
        return None

    def _get_command_aux(self) -> SplashMeAux:
        """Return the aux used for commands."""
        aux = self._get_aux()
        if aux is None:
            raise HomeAssistantError("SplashMe device data is unavailable")
        return aux

    def _get_main_pump(self) -> SplashMeAux | None:
        """Return the main pump entry."""
        snapshot = self._get_snapshot()
        return None if snapshot is None else _find_main_pump(snapshot.aux)

    def _get_primary_spa(self) -> SplashMeAux | None:
        """Return the primary Spa mode entry."""
        snapshot = self._get_snapshot()
        return None if snapshot is None else _find_primary_spa(snapshot.aux)

    def _get_cleaner_aux(self) -> list[SplashMeAux]:
        """Return linked cleaner entries."""
        snapshot = self._get_snapshot()
        if snapshot is None:
            return []
        return [
            aux
            for aux in snapshot.aux
            if aux.category == "pump_linked_devices"
            and aux.aux_type_code in CLEANER_TYPE_CODES
        ]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the aux on."""
        del kwargs
        await self._async_set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the aux off."""
        del kwargs
        await self._async_set_state(False)

    async def _async_set_state(self, enabled: bool) -> None:
        """Set the aux state."""
        aux = self._get_command_aux()

        if self._is_main_pump(aux):
            await self._async_set_main_pump_state(aux, enabled)
        elif self._is_pump_linked(aux):
            await self._async_set_pump_linked_state(aux, enabled)
        else:
            await self._async_set_regular_aux_state(aux, enabled)

        await self._async_wait_for_state(enabled)

    async def _async_wait_for_state(self, enabled: bool) -> SplashMeAux | None:
        """Poll the backend until the requested state is reflected."""
        for attempt in range(1, STATE_CONFIRM_ATTEMPTS + 1):
            await self.coordinator.async_refresh_device_snapshot(self.device.device_id)
            refreshed_aux = self._get_aux()
            if refreshed_aux is not None and self._reports_on(refreshed_aux) == enabled:
                return refreshed_aux
            if attempt < STATE_CONFIRM_ATTEMPTS:
                await asyncio.sleep(STATE_CONFIRM_INTERVAL_SECONDS)

        raise HomeAssistantError(
            f"SplashMe device did not confirm {'on' if enabled else 'off'} state in time"
        )

    def _is_main_pump(self, aux: SplashMeAux) -> bool:
        """Return whether the aux is the main pump."""
        return aux.category == "pump_devices" and aux.aux_type_code == MAIN_PUMP_TYPE_CODE

    def _is_pump_linked(self, aux: SplashMeAux) -> bool:
        """Return whether the aux is pump-linked."""
        return aux.category == "pump_linked_devices"

    async def _async_set_regular_aux_state(self, aux: SplashMeAux, enabled: bool) -> None:
        """Toggle a regular aux."""
        await self._entry.runtime_data.api.async_set_aux(
            self.device.device_id,
            aux_type_code=aux.aux_type_code,
            aux_slot_num=aux.aux_slot_num,
            enabled=enabled,
        )

    async def _async_set_main_pump_state(self, aux: SplashMeAux, enabled: bool) -> None:
        """Toggle the main pump."""
        if not enabled:
            await self._async_turn_off_pump_linked_devices()
            await self._async_turn_off_spa()

        await self._entry.runtime_data.api.async_set_pump_info(
            self.device.device_id,
            aux_slot_num=aux.aux_slot_num,
            enabled=enabled,
            pump_speed=self._pump_start_speed(aux),
        )

    def _pump_start_speed(self, pump: SplashMeAux) -> int:
        """Return the speed to start the pump with: the user's setpoint
        (Pump Speed Setpoint number) when set, else the device's own value."""
        setpoint = self.coordinator.pump_speed_setpoint(self.device.device_id)
        if setpoint is not None:
            return max(setpoint, MIN_PUMP_SPEED)
        return max(pump.pump_speed, MIN_PUMP_SPEED)

    async def _async_set_pump_linked_state(self, aux: SplashMeAux, enabled: bool) -> None:
        """Toggle a pump-linked device."""
        if enabled:
            if aux.aux_type_code in CLEANER_TYPE_CODES:
                await self._async_turn_off_spa()
            await self._async_ensure_main_pump_on()

        await self._async_set_regular_aux_state(aux, enabled)

    async def _async_ensure_main_pump_on(self) -> None:
        """Turn on the main pump when required."""
        pump = self._get_main_pump()
        if pump is None or pump.aux_status:
            return

        await self._entry.runtime_data.api.async_set_pump_info(
            self.device.device_id,
            aux_slot_num=pump.aux_slot_num,
            enabled=True,
            pump_speed=self._pump_start_speed(pump),
        )

    async def _async_turn_off_pump_linked_devices(self) -> None:
        """Turn off all pump-linked devices."""
        snapshot = self._get_snapshot()
        if snapshot is None:
            return

        for aux in snapshot.aux:
            if aux.category != "pump_linked_devices" or not aux.aux_status:
                continue
            await self._async_set_regular_aux_state(aux, False)

    async def _async_turn_off_spa(self) -> None:
        """Turn off Spa mode when needed."""
        spa = self._get_primary_spa()
        if spa is None or not spa.aux_status:
            return
        await self._async_set_regular_aux_state(spa, False)


class SplashMeSpaSwitch(SplashMeAuxSwitch):
    """Single switch representing Spa mode."""

    def __init__(
        self,
        coordinator,
        entry: SplashMeConfigEntry,
        device,
    ) -> None:
        """Initialize the Spa switch."""
        super().__init__(
            coordinator,
            entry,
            device,
            SplashMeAux(
                key=SPA_SENTINEL_KEY,
                category="spa",
                aux_tag="spa",
                aux_type_code=0,
                aux_status=False,
                aux_slot_num=0,
                pump_speed=0,
                actual_flow_rate=0,
                aux_timer=None,
                aux_timer_remain_sec=None,
            ),
        )
        self._attr_unique_id = f"{device.device_id}_spa_switch"
        self._attr_name = "spa"

    def _get_aux(self) -> SplashMeAux | None:
        """Return the primary Spa mode entry."""
        return self._get_primary_spa()

    async def _async_set_state(self, enabled: bool) -> None:
        """Set the Spa mode state."""
        spa = self._get_command_aux()

        if enabled:
            await self._async_turn_off_cleaners()
            await self._async_ensure_main_pump_on()

        await self._async_set_regular_aux_state(spa, enabled)
        await self.coordinator.async_request_refresh()

    async def _async_turn_off_cleaners(self) -> None:
        """Turn off linked cleaners before enabling Spa."""
        for aux in self._get_cleaner_aux():
            if not aux.aux_status:
                continue
            await self._async_set_regular_aux_state(aux, False)


class SplashMeLanAuxSwitch(SplashMeLanEntity, SwitchEntity):
    """Aux slot switch for a LAN-connected device.

    The device only queues the request: update optimistically, then let the
    read-back the coordinator schedules 2 s later (and the regular poll)
    correct the state.
    """

    def __init__(
        self, coordinator: SplashMeLanCoordinator, entry, slot: int
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry)
        self._slot = slot
        self._attr_unique_id = f"{self.unique_id_base}_aux_{slot}"
        self._optimistic_state: bool | None = None

    def _aux(self):
        return self.coordinator.data.aux_by_slot(self._slot)

    @property
    def name(self) -> str:
        """Return the slot's current name (renamed in the app at any time)."""
        aux = self._aux()
        return aux.display_name if aux else f"Aux {self._slot}"

    @property
    def is_on(self) -> bool:
        """Return the switch state (optimistic until the next poll)."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        aux = self._aux()
        if aux is None:
            return False
        # The pump slot's aux_mode stays "manual" while the motor is stopped;
        # the frame's main_pump status (0 off / schedule / 99 manual) is the truth.
        if aux.type_code == LAN_MAIN_PUMP_TYPE_CODE:
            telemetry = self.coordinator.data.telemetry
            return telemetry is not None and telemetry.main_pump != 0
        return aux.is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the slot mode and any running countdown."""
        aux = self._aux()
        if aux is None:
            return {}
        return {
            "mode": _aux_mode_label(aux.mode),
            "manual_override": aux.trump,
            "timer_remaining_seconds": aux.timer_remain_sec,
            # Cleaners, ioniser, ozone and UV: the firmware starts the pump for
            # them and stops them when flow is lost.
            "runs_with_pump": aux.is_pump_linked,
        }

    @property
    def available(self) -> bool:
        """Return whether the slot still exists."""
        return super().available and self._aux() is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop the optimistic state once fresh data arrives."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def _async_set_state(self, state: bool) -> None:
        aux = self._aux()
        pump_speed = None
        if aux is not None and aux.type_code == LAN_MAIN_PUMP_TYPE_CODE and state:
            pump_speed = self._entry.options.get(OPT_LAN_PUMP_SPEED_SETPOINT) or DEFAULT_PUMP_SPEED
        await self.coordinator.async_set_aux(self._slot, state, pump_speed=pump_speed)
        self._optimistic_state = state
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the aux on."""
        del kwargs
        await self._async_set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the aux off."""
        del kwargs
        await self._async_set_state(False)


class SplashMeLanSpaSwitch(SplashMeLanEntity, SwitchEntity):
    """Spa mode: all spa actuators together.

    Switching any one actuator slot makes the firmware sequence every
    actuator, take over the main pump and stop the cleaners, so one write
    to the primary actuator is the whole operation.
    """

    _attr_icon = "mdi:hot-tub"

    def __init__(self, coordinator: SplashMeLanCoordinator, entry) -> None:
        """Initialize the spa mode switch."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self.unique_id_base}_spa_mode"
        self._attr_name = "Spa Mode"
        self._optimistic_state: bool | None = None

    @property
    def available(self) -> bool:
        """Return whether a spa actuator is still assigned."""
        return super().available and bool(self.coordinator.data.spa_actuators)

    @property
    def is_on(self) -> bool:
        """Return spa mode (optimistic until the next poll)."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self.coordinator.data.spa_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose which actuator slots make up the spa."""
        return {"actuators": [a.display_name for a in self.coordinator.data.spa_actuators]}

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop the optimistic state once fresh data arrives."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def _async_set_state(self, state: bool) -> None:
        actuators = self.coordinator.data.spa_actuators
        if not actuators:
            raise HomeAssistantError("No spa actuator is assigned")
        await self.coordinator.async_set_aux(actuators[0].slot, state)
        self._optimistic_state = state
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enter spa mode."""
        del kwargs
        await self._async_set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Leave spa mode."""
        del kwargs
        await self._async_set_state(False)


class SplashMeLanDosingSwitch(SplashMeLanEntity, SwitchEntity):
    """Whether a doser may dose: the enable flag in the pH / chlorine settings."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SplashMeLanCoordinator, entry, kind: str) -> None:
        """Initialize for "ph", "chlorinator" or "liquid"."""
        super().__init__(coordinator, entry)
        _codes, self._settings_kind, self._key, name, icon = DOSING_KINDS[kind]
        self._attr_unique_id = f"{self.unique_id_base}_dosing_{kind}"
        self._attr_name = name
        self._attr_icon = icon

    def _settings(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        return data.ph_settings if self._settings_kind == "ph" else data.chlorine_settings

    @property
    def available(self) -> bool:
        """Settings must have been read."""
        return super().available and self._settings() is not None

    @property
    def is_on(self) -> bool | None:
        """Return the enable flag."""
        settings = self._settings()
        return None if settings is None else bool(settings.get(self._key))

    async def _async_set(self, enabled: bool) -> None:
        if self._settings_kind == "ph":
            await self.coordinator.async_update_ph_settings({self._key: enabled})
        else:
            await self.coordinator.async_update_chlorine_settings({self._key: enabled})

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable dosing."""
        del kwargs
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable dosing."""
        del kwargs
        await self._async_set(False)


class SplashMeLanHeaterSwitch(SplashMeLanEntity, SwitchEntity):
    """Whether the firmware may use a heating source (pool / spa / solar heater).

    The relay itself stays under firmware control; this is the "selected"
    flag the app shows next to each heater.
    """

    def __init__(self, coordinator: SplashMeLanCoordinator, entry, kind: str) -> None:
        """Initialize for "pool", "spa" or "solar"."""
        super().__init__(coordinator, entry)
        self._kind = kind
        self._attr_unique_id = f"{self.unique_id_base}_heating_{kind}"
        self._attr_name = HEATER_LABELS[kind]
        self._attr_icon = HEATER_ICONS[kind]

    @property
    def available(self) -> bool:
        """Config section must have been received."""
        return super().available and self.coordinator.data.config is not None

    @property
    def is_on(self) -> bool | None:
        """Return the selection flag from the config section."""
        config = self.coordinator.data.config
        if config is None:
            return None
        return getattr(config, f"{self._kind}_heater_selected")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Allow this heating source."""
        del kwargs
        await self.coordinator.async_set_heating({f"is_{self._kind}_heater_selected": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disallow this heating source."""
        del kwargs
        await self.coordinator.async_set_heating({f"is_{self._kind}_heater_selected": False})


def _aux_mode_label(mode: int) -> str:
    """Map an aux/pump mode (0 off / 1-20 schedule # / 99 manual) to a label."""
    if mode == 0:
        return "Off"
    if mode == 99:
        return "Manual"
    return f"Schedule {mode}"


class SplashMeLanScheduleSwitch(SplashMeLanEntity, SwitchEntity):
    """Enable/disable one schedule on a LAN device (signed v2 action)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: SplashMeLanCoordinator, entry, index: int) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry)
        self._index = index
        self._attr_unique_id = f"{self.unique_id_base}_schedule_{index}"

    def _schedule(self):
        return self.coordinator.data.schedule_by_index(self._index)

    @property
    def name(self) -> str:
        """Follow the schedule's current name (slots get reused)."""
        sched = self._schedule()
        name = sched.name if sched else ""
        return f"Schedule {self._index + 1}" + (f" {name}" if name else "")

    @property
    def available(self) -> bool:
        """Only schedules that are actually defined get a live switch."""
        sched = self._schedule()
        return super().available and sched is not None and sched.defined

    @property
    def is_on(self) -> bool:
        """Return the enabled bit from the freshest source (the state frame)."""
        config = self.coordinator.data.config
        if config is not None:
            return bool(config.sched_enabled & (1 << self._index))
        sched = self._schedule()
        return bool(sched and sched.enabled)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the schedule definition."""
        sched = self._schedule()
        if sched is None:
            return {}
        return {
            "schedule_num": sched.index,
            "aux_slot": sched.aux_slot,
            "start_time": _minutes_to_hhmm(sched.start_min),
            "end_time": _minutes_to_hhmm(sched.end_min),
            "days": [
                label for i, label in enumerate(_WEEKDAY_LABELS) if sched.week_day & (1 << i)
            ],
            "pump_speed": sched.speed,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the schedule."""
        del kwargs
        await self.coordinator.async_set_schedule_enabled(self._index, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the schedule."""
        del kwargs
        await self.coordinator.async_set_schedule_enabled(self._index, False)


def _minutes_to_hhmm(value) -> str | None:
    """Convert a minutes-since-midnight int to HH:MM."""
    if value is None:
        return None
    try:
        m = int(value)
    except (TypeError, ValueError):
        return None
    if m < 0:
        return None
    return f"{m // 60:02d}:{m % 60:02d}"


_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class SplashMeScheduleSwitch(SplashMeDeviceEntity, SwitchEntity):
    """Enable/disable one device schedule; details exposed as attributes."""

    _attr_icon = "mdi:calendar-clock"
    # Group under the device's Configuration card, apart from manual controls.
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, device, schedule_num: int) -> None:
        """Initialize the schedule switch."""
        super().__init__(coordinator, device)
        self._schedule_num = schedule_num
        self._attr_unique_id = f"{device.device_id}_schedule_{schedule_num}"

    def _schedule(self) -> dict | None:
        snapshot = self.coordinator.data.snapshots.get(self.device.device_id)
        if snapshot is None or snapshot.schedules is None:
            return None
        for s in snapshot.schedules:
            if s.get("schedule_num") == self._schedule_num:
                return s
        return None

    @property
    def name(self) -> str:
        sched = self._schedule()
        label = (sched or {}).get("schedule_name") or f"Schedule {self._schedule_num}"
        return f"Schedule: {label}"

    @property
    def available(self) -> bool:
        return self._schedule() is not None

    @property
    def is_on(self) -> bool | None:
        sched = self._schedule()
        return None if sched is None else bool(sched.get("schedule_state"))

    @property
    def extra_state_attributes(self) -> dict:
        sched = self._schedule() or {}
        days = sched.get("week_days") or []
        active_days = [
            _WEEKDAY_LABELS[i] for i, on in enumerate(days)
            if on and i < len(_WEEKDAY_LABELS)
        ]
        attrs = {
            "schedule_num": self._schedule_num,
            "start_time": _minutes_to_hhmm(sched.get("start_time")),
            "end_time": _minutes_to_hhmm(sched.get("end_time")),
            "days": active_days,
            "schedule_name": sched.get("schedule_name"),
        }
        if sched.get("pump_speed"):
            attrs["pump_speed"] = sched.get("pump_speed")
        if sched.get("aux_slot") is not None:
            attrs["aux_slot"] = sched.get("aux_slot")
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        del kwargs
        await self.coordinator.async_set_schedule_state(
            self.device.device_id, self._schedule_num, True
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        del kwargs
        await self.coordinator.async_set_schedule_state(
            self.device.device_id, self._schedule_num, False
        )
