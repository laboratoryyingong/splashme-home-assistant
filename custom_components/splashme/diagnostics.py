"""Diagnostics support for SplashMe."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import SplashMeConfigEntry, is_lan_entry
from .account import is_account_entry
from .const import CONF_ENVELOPE_KEY, DOMAIN

TO_REDACT = {
    CONF_ENVELOPE_KEY,
    "address",
    "device_id",
    "email",
    "family_name",
    "given_name",
    "name",
    "object_id",
    "preferred_username",
    "serial_number",
    "site_id",
    "sub",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SplashMeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    del hass
    if is_lan_entry(entry):
        return _lan_diagnostics(entry)
    if is_account_entry(entry):
        return {
            "user_info": async_redact_data(asdict(entry.runtime_data.user_info), TO_REDACT),
            "paired_devices": len(entry.runtime_data.paired_device_ids),
        }
    coordinator = entry.runtime_data.coordinator
    return {
        "user_info": async_redact_data(asdict(entry.runtime_data.user_info), TO_REDACT),
        "selected_site_id": async_redact_data(
            {"selected_site_id": coordinator.data.selected_site_id}, TO_REDACT
        ),
        "selected_device_id": async_redact_data(
            {"selected_device_id": coordinator.data.selected_device_id}, TO_REDACT
        ),
        "devices": async_redact_data(
            [
                {
                    "site_id": device.site_id,
                    "object_id": device.object_id,
                    "device_id": device.device_id,
                    "tag": device.tag,
                    "address": device.address,
                    "online": device.online,
                }
                for device in coordinator.data.devices
            ],
            TO_REDACT,
        ),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: SplashMeConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a single SplashMe device."""
    del hass
    if is_lan_entry(entry):
        return _lan_diagnostics(entry)
    device_id = next(
        identifier[1] for identifier in device.identifiers if identifier[0] == DOMAIN
    )
    coordinator = entry.runtime_data.coordinator
    snapshot = coordinator.data.snapshots.get(device_id)
    matched_device = next(
        current_device
        for current_device in coordinator.data.devices
        if current_device.device_id == device_id
    )

    return {
        "device": async_redact_data(
            {
                "site_id": matched_device.site_id,
                "object_id": matched_device.object_id,
                "device_id": matched_device.device_id,
                "tag": matched_device.tag,
                "address": matched_device.address,
                "online": matched_device.online,
            },
            TO_REDACT,
        ),
        "snapshot": None
        if snapshot is None
        else {
            "pump": None if snapshot.pump is None else asdict(snapshot.pump),
            "chemistry": None if snapshot.chemistry is None else asdict(snapshot.chemistry),
            "temperature": None
            if snapshot.temperature is None
            else asdict(snapshot.temperature),
            "aux": [asdict(aux) for aux in snapshot.aux],
        },
    }


def _lan_diagnostics(entry: SplashMeConfigEntry) -> dict[str, Any]:
    """Diagnostics for a LAN entry: entry data plus the decoded state."""
    data = entry.runtime_data.coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "telemetry": None if data.telemetry is None else asdict(data.telemetry),
        "config": None if data.config is None else asdict(data.config),
        "aux": [asdict(aux) for aux in data.aux],
        "pump": None if data.pump is None else asdict(data.pump),
        "schedules": [asdict(s) for s in data.schedules],
    }
