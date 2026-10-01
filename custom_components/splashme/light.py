"""Light entities for LAN-connected SplashMe devices.

A pool light is a relay on an aux slot; on/off goes through the signed aux
action like any aux, colour changes run the firmware's brand-specific relay
pulse sequence through the signed cmd passthrough. Brands with absolute colour
selection expose the colours as effects; the rest are plain on/off lights
with buttons (see button.py).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SplashMeConfigEntry, is_lan_entry
from .lan import SplashMeLanCoordinator, SplashMeLanEntity
from .lights import COLOUR_TABLES, COLOUR_UNSET
from .slots import SplashMeLanSlotEntities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SplashMeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one light per light slot (LAN entries only), following reassignment."""
    del async_add_entities
    if not is_lan_entry(entry):
        return
    coordinator: SplashMeLanCoordinator = entry.runtime_data.coordinator
    slots = SplashMeLanSlotEntities(
        hass,
        entry,
        coordinator,
        "light_",
        lambda data: {f"light_{aux.slot}": aux.slot for aux in data.aux if aux.is_light},
        lambda slot: SplashMeLanLight(coordinator, entry, slot),
    )
    await slots.async_sync()
    entry.runtime_data.slot_entities.append(slots)


class SplashMeLanLight(SplashMeLanEntity, LightEntity):
    """One pool/spa light slot."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, coordinator: SplashMeLanCoordinator, entry, slot: int) -> None:
        """Initialize the light."""
        super().__init__(coordinator, entry)
        self._slot = slot
        self._attr_unique_id = f"{self.unique_id_base}_light_{slot}"
        self._optimistic_on: bool | None = None
        self._optimistic_effect: str | None = None
        self._apply_brand()

    def _aux(self):
        return self.coordinator.data.aux_by_slot(self._slot)

    @property
    def name(self) -> str:
        """Return the slot's current name (renamed in the app at any time)."""
        aux = self._aux()
        return aux.display_name if aux else f"Light {self._slot}"

    def _apply_brand(self) -> None:
        """Expose the colour table as effects when the brand supports it."""
        table = COLOUR_TABLES.get(self.coordinator.data.light_type(self._slot))
        if table:
            self._attr_supported_features = LightEntityFeature.EFFECT
            self._attr_effect_list = list(table.values())
        else:
            self._attr_supported_features = LightEntityFeature(0)
            self._attr_effect_list = None

    @property
    def available(self) -> bool:
        """Return whether the slot still exists."""
        return super().available and self._aux() is not None

    @property
    def is_on(self) -> bool:
        """Return the relay state (optimistic while a sequence runs)."""
        if self._optimistic_on is not None:
            return self._optimistic_on
        aux = self._aux()
        return bool(aux and aux.is_on)

    @property
    def effect(self) -> str | None:
        """Return the last colour the device stored for this light's group."""
        if self._optimistic_effect is not None:
            return self._optimistic_effect
        table = COLOUR_TABLES.get(self.coordinator.data.light_type(self._slot))
        colour = self.coordinator.data.light_colour(self._slot)
        if not table or colour in (None, COLOUR_UNSET):
            return None
        return table.get(colour)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the brand protocol and raw colour index."""
        aux = self._aux()
        return {
            "light_type": self.coordinator.data.light_type(self._slot),
            "light_color": self.coordinator.data.light_colour(self._slot),
            "manual_override": bool(aux and aux.trump),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop optimistic state once fresh data arrives, unless a sequence runs."""
        if not self.coordinator.light_busy(self._slot):
            self._optimistic_on = None
            self._optimistic_effect = None
        self._apply_brand()
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, or pick a colour (which also turns the light on)."""
        effect = kwargs.get(ATTR_EFFECT)
        table = COLOUR_TABLES.get(self.coordinator.data.light_type(self._slot))
        if effect and table:
            colour = next((idx for idx, name in table.items() if name == effect), None)
            if colour is None:
                return
            await self.coordinator.async_set_light_colour(self._slot, colour)
            self._optimistic_effect = effect
        else:
            await self.coordinator.async_set_aux(self._slot, True)
        self._optimistic_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the relay off."""
        del kwargs
        await self.coordinator.async_set_aux(self._slot, False)
        self._optimistic_on = False
        self.async_write_ha_state()
