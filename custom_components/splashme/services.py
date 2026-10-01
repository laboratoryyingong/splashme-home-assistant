"""SplashMe services: schedule create/update/delete.

Schedule enable/disable is a switch entity; full CRUD needs parameters HA has
no native UI for, so it is exposed as services (dev tools / automations).
"""

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from . import pv2
from .const import CONF_DEVICE_ID, DOMAIN
from .lan import SplashMeLanCoordinator

ATTR_DEVICE_ID = "device_id"
ATTR_SCHEDULE_NUM = "schedule_num"

SERVICE_CREATE_SCHEDULE = "create_schedule"
SERVICE_UPDATE_SCHEDULE = "update_schedule"
SERVICE_DELETE_SCHEDULE = "delete_schedule"

# Times are minutes since midnight (0-1439). day_of_week is 7 bools Mon..Sun.
_SCHEDULE_FIELDS = {
    vol.Required(ATTR_DEVICE_ID): cv.string,
    vol.Required("start_time"): vol.All(int, vol.Range(min=0, max=1439)),
    vol.Required("end_time"): vol.All(int, vol.Range(min=0, max=1439)),
    vol.Required("day_of_week"): vol.All([cv.boolean], vol.Length(min=7, max=7)),
    vol.Optional("schedule_name", default=""): cv.string,
    vol.Optional("aux_slot"): int,
    vol.Optional("pump_speed"): vol.All(int, vol.Range(min=40, max=100)),
    vol.Optional("pool_volume"): int,
}

CREATE_SCHEMA = vol.Schema(_SCHEDULE_FIELDS)
UPDATE_SCHEMA = vol.Schema(
    {**_SCHEDULE_FIELDS, vol.Required(ATTR_SCHEDULE_NUM): int}
)
DELETE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_DEVICE_ID): cv.string, vol.Required(ATTR_SCHEDULE_NUM): int}
)


def _find_coordinator(hass: HomeAssistant, device_id: str):
    """Return the coordinator (LAN or cloud) that owns device_id."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        coordinator = getattr(runtime, "coordinator", None)
        if coordinator is None or getattr(coordinator, "data", None) is None:
            continue
        if isinstance(coordinator, SplashMeLanCoordinator):
            if entry.data.get(CONF_DEVICE_ID) == device_id:
                return coordinator
            continue
        if any(d.device_id == device_id for d in coordinator.data.devices):
            return coordinator
    raise HomeAssistantError(f"No SplashMe device {device_id} found")


def _lan_schedule_fields(coordinator: SplashMeLanCoordinator, call: ServiceCall) -> pv2.ScheduleFields:
    """Build the v2 schedule payload; defaults to the filter pump slot."""
    data = coordinator.data
    slot = call.data.get("aux_slot")
    if slot is None:
        pump = data.main_pump
        if pump is None:
            raise HomeAssistantError("aux_slot is required: no filter pump slot found")
        slot = pump.slot
    aux = data.aux_by_slot(slot)
    if aux is None:
        raise HomeAssistantError(f"Unknown aux slot {slot}")
    # day_of_week is Mon..Sun; the device mask uses bit 0 = Monday.
    week_day = sum(1 << i for i, on in enumerate(call.data["day_of_week"]) if on)
    return pv2.ScheduleFields(
        slot=slot,
        aux_type=aux.type_code,
        turnover=0,
        speed=call.data.get("pump_speed", 0),
        start_min=call.data["start_time"],
        end_min=call.data["end_time"],
        week_day=week_day,
        name=call.data.get("schedule_name", ""),
    )


def _schedule_body(call: ServiceCall) -> dict:
    body = {
        "start_time": call.data["start_time"],
        "end_time": call.data["end_time"],
        "day_of_week": call.data["day_of_week"],
        "schedule_name": call.data.get("schedule_name", ""),
    }
    for opt in ("aux_slot", "pump_speed", "pool_volume"):
        if opt in call.data:
            body[opt] = call.data[opt]
    return body


async def async_register_services(hass: HomeAssistant) -> None:
    """Register SplashMe schedule services (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_CREATE_SCHEDULE):
        return

    async def _create(call: ServiceCall) -> None:
        coordinator = _find_coordinator(hass, call.data[ATTR_DEVICE_ID])
        if isinstance(coordinator, SplashMeLanCoordinator):
            await coordinator.async_create_schedule(_lan_schedule_fields(coordinator, call))
            return
        await coordinator.async_create_schedule(
            call.data[ATTR_DEVICE_ID], _schedule_body(call)
        )

    async def _update(call: ServiceCall) -> None:
        coordinator = _find_coordinator(hass, call.data[ATTR_DEVICE_ID])
        if isinstance(coordinator, SplashMeLanCoordinator):
            await coordinator.async_update_schedule(
                call.data[ATTR_SCHEDULE_NUM], _lan_schedule_fields(coordinator, call)
            )
            return
        body = _schedule_body(call)
        body["schedule_num"] = call.data[ATTR_SCHEDULE_NUM]
        await coordinator.async_update_schedule(call.data[ATTR_DEVICE_ID], body)

    async def _delete(call: ServiceCall) -> None:
        coordinator = _find_coordinator(hass, call.data[ATTR_DEVICE_ID])
        if isinstance(coordinator, SplashMeLanCoordinator):
            await coordinator.async_delete_schedule(call.data[ATTR_SCHEDULE_NUM])
            return
        await coordinator.async_delete_schedule(
            call.data[ATTR_DEVICE_ID], call.data[ATTR_SCHEDULE_NUM]
        )

    hass.services.async_register(DOMAIN, SERVICE_CREATE_SCHEDULE, _create, CREATE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UPDATE_SCHEDULE, _update, UPDATE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_SCHEDULE, _delete, DELETE_SCHEMA)
