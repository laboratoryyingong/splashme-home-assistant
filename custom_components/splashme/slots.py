"""Entities that follow the device's equipment configuration.

Which switches, lights, light buttons and expander sensors a LAN device
needs depends on what is assigned to its aux slots, and the user changes
that in the SplashMe app at any time. Each platform describes the entities
it wants as unique-id keys; whenever the equipment signature changes every
platform is re-synced (new keys added, gone keys removed from the entity
registry, renamed slots renamed) and the dashboard is regenerated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform, entity_registry as er

from .dashboard import async_register_dashboard
from .lan import (
    SplashMeLanCoordinator,
    SplashMeLanData,
    SplashMeLanEntity,
    lan_device_identifier,
)

_LOGGER = logging.getLogger(__name__)


class SplashMeLanSlotEntities:
    """One platform's equipment-dependent entities, keyed by unique-id suffix.

    `wanted(data)` maps each key the platform needs right now to whatever
    `factory` needs to build that entity. Must be created inside the
    platform's `async_setup_entry`.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: SplashMeLanCoordinator,
        key_prefix: str,
        wanted: Callable[[SplashMeLanData], Mapping[str, Any]],
        factory: Callable[[Any], SplashMeLanEntity],
    ) -> None:
        """Initialize the tracker for one platform."""
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._base = f"{lan_device_identifier(entry)}_"
        self._prefix = self._base + key_prefix
        self._wanted = wanted
        self._factory = factory
        self._platform = entity_platform.async_get_current_platform()
        self._live: dict[str, SplashMeLanEntity] = {}

    async def async_sync(self) -> None:
        """Add entities for new keys, drop gone ones, refresh renamed ones."""
        wanted = self._wanted(self._coordinator.data)
        registry = er.async_get(self._hass)
        for reg in er.async_entries_for_config_entry(registry, self._entry.entry_id):
            if reg.domain != self._platform.domain or not reg.unique_id.startswith(self._prefix):
                continue
            key = reg.unique_id[len(self._base) :]
            if key in wanted:
                live = self._live.get(key)
                if live is not None and reg.original_name != live.name:
                    registry.async_update_entity(reg.entity_id, original_name=live.name)
                continue
            # Slot reassigned (possibly while HA was down). A loaded entity
            # removes itself when its registry entry goes.
            registry.async_remove(reg.entity_id)
            self._live.pop(key, None)
            _LOGGER.info("Removed %s: no longer assigned on %s", reg.entity_id, self._entry.title)
        new = {key: self._factory(spec) for key, spec in wanted.items() if key not in self._live}
        if new:
            await self._platform.async_add_entities(new.values())
            self._live.update(new)


@callback
def async_track_equipment(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SplashMeLanCoordinator
) -> None:
    """Re-sync slot entities and the dashboard when the equipment changes."""
    synced = coordinator.data.equipment_signature
    task: asyncio.Task | None = None

    async def _resync(signature: tuple[Any, ...]) -> None:
        nonlocal synced
        for slots in entry.runtime_data.slot_entities:
            await slots.async_sync()
        await async_register_dashboard(hass, entry, coordinator)
        synced = signature

    @callback
    def _on_update() -> None:
        nonlocal task
        if task is not None and not task.done():
            return
        signature = coordinator.data.equipment_signature
        if signature == synced:
            return
        _LOGGER.info("Equipment changed on %s: refreshing entities and dashboard", entry.title)
        task = entry.async_create_task(hass, _resync(signature))

    entry.async_on_unload(coordinator.async_add_listener(_on_update))
