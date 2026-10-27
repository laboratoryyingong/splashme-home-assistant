"""Climate entities for LAN-connected SplashMe devices: pool and spa heating (#18).

One thermostat per body of water that something can heat, so HomeKit and
Google Home show a dial with the water temperature. Heat / off is the
"selected" flag of the body's heater (or of solar when that is all it has);
a shared pool & spa heater runs when either flag is set, so both thermostats
then read and write the pair together. The firmware only heats while the
filter pump runs and the water is below the target. The controller has one water probe: it reads the spa while spa mode
is on and the pool otherwise, so each thermostat keeps the last reading taken
while its own water was circulating.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SplashMeConfigEntry, is_lan_entry, pv2
from .const import HEATING_TARGET_MAX, HEATING_TARGET_MIN
from .lan import (
    POOL_HEATER_TYPE_CODES,
    SHARED_HEATER_TYPE_CODES,
    SPA_HEATER_TYPE_CODES,
    SplashMeLanCoordinator,
    SplashMeLanData,
    SplashMeLanEntity,
)
from .slots import SplashMeLanSlotEntities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SplashMeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a thermostat for the pool and the spa when something can heat them."""
    del async_add_entities
    if not is_lan_entry(entry):
        return
    coordinator: SplashMeLanCoordinator = entry.runtime_data.coordinator

    def _wanted(data: SplashMeLanData) -> dict[str, str]:
        wanted = {}
        if data.has_pool_heating:
            wanted["climate_pool"] = "pool"
        if data.has_spa_heating:
            wanted["climate_spa"] = "spa"
        return wanted

    slots = SplashMeLanSlotEntities(
        hass, entry, coordinator, "climate_", _wanted,
        lambda kind: SplashMeLanClimate(coordinator, entry, kind),
    )
    await slots.async_sync()
    entry.runtime_data.slot_entities.append(slots)


def heat_sources(data: SplashMeLanData, kind: str) -> tuple[str, ...]:
    """The heaters whose "selected" flags heat / off drives: the body's own, else solar.

    A shared heater heats the pool while either flag is set, so it is both flags.
    """
    if data.has_equipment(SHARED_HEATER_TYPE_CODES):
        return ("pool", "spa")
    codes = POOL_HEATER_TYPE_CODES if kind == "pool" else SPA_HEATER_TYPE_CODES
    return (kind,) if data.has_equipment(codes) else ("solar",)


def circulating(data: SplashMeLanData, kind: str) -> bool:
    """Whether the water probe and the heaters are on this body right now."""
    return data.spa_on if kind == "spa" else not data.spa_on


def water_reading(data: SplashMeLanData, kind: str) -> float | None:
    """This body's water temperature, when the probe is reading it and has settled."""
    t = data.telemetry
    if t is None or not t.flag(pv2.FLAG_CHEM_STABLE) or not circulating(data, kind):
        return None
    return t.water_temp


def heating(data: SplashMeLanData, kind: str) -> bool:
    """Whether a heater or the solar pump is running for this body."""
    t = data.telemetry
    if t is None or not circulating(data, kind):
        return False
    heater = pv2.FLAG_SPA_HEATER if kind == "spa" else pv2.FLAG_HEATER
    return t.flag(heater) or t.flag(pv2.FLAG_SOLAR)


class SplashMeLanClimate(SplashMeLanEntity, ClimateEntity):
    """Pool or spa heating as a thermostat."""

    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_min_temp = HEATING_TARGET_MIN
    _attr_max_temp = HEATING_TARGET_MAX

    def __init__(self, coordinator: SplashMeLanCoordinator, entry, kind: str) -> None:
        """Initialize for "pool" or "spa"."""
        super().__init__(coordinator, entry)
        self._kind = kind
        self._attr_unique_id = f"{self.unique_id_base}_climate_{kind}"
        self._attr_name = f"{kind.title()} Heating"
        self._attr_icon = "mdi:hot-tub" if kind == "spa" else "mdi:pool-thermometer"
        self._current: float | None = None
        self._take_reading()

    def _take_reading(self) -> None:
        if self.coordinator.data is not None and (value := water_reading(self.coordinator.data, self._kind)) is not None:
            self._current = value

    @callback
    def _handle_coordinator_update(self) -> None:
        self._take_reading()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Config section must have been received."""
        return super().available and self.coordinator.data.config is not None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Heat while a heating source is selected."""
        data = self.coordinator.data
        if data.config is None:
            return None
        selected = any(getattr(data.config, f"{s}_heater_selected") for s in heat_sources(data, self._kind))
        return HVACMode.HEAT if selected else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """Heating while a heater or the solar pump runs for this body, else idle."""
        if self.hvac_mode is None:
            return None
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.HEATING if heating(self.coordinator.data, self._kind) else HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        """The last settled reading taken while this body was circulating."""
        return self._current

    @property
    def target_temperature(self) -> float | None:
        """Return the target from the config section."""
        config = self.coordinator.data.config
        if config is None:
            return None
        return float(config.desired_water_temp if self._kind == "pool" else config.spa_desired_temp)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Write the new target, and the mode when one is given with it."""
        changes: dict[str, Any] = {}
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            changes[f"desire_{self._kind}_temp"] = int(round(temperature))
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            changes.update(self._selected(mode == HVACMode.HEAT))
        if changes:
            await self.coordinator.async_set_heating(changes)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Select or deselect the heating source."""
        await self.coordinator.async_set_heating(self._selected(hvac_mode == HVACMode.HEAT))

    def _selected(self, on: bool) -> dict[str, bool]:
        return {f"is_{s}_heater_selected": on for s in heat_sources(self.coordinator.data, self._kind)}
