"""A Lovelace dashboard per local device, owned by the integration.

The dashboard is registered as a storage-mode Lovelace panel when the LAN
entry loads and deleted when the entry is removed, so uninstalling the
integration leaves nothing behind in the sidebar. The layout is generated
from the device's entities; once the user edits it in the UI it is left
alone (the generated version is recognised by its hash).

Two layouts exist: the full one with dosing, chemistry, schedules and trends
for whoever maintains the pool, and a basic one with just the pump, heating,
lights, other outputs and the water temperature. The entry's options pick one
for the Pool dashboard, or a basic Pool for everyone plus a full Pool Admin
dashboard that only administrators see.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import logging
from typing import Any

from homeassistant.components import frontend
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.components.lovelace.const import (
    CONF_REQUIRE_ADMIN,
    CONF_SHOW_IN_SIDEBAR,
    CONF_TITLE,
    CONF_URL_PATH,
    LOVELACE_DATA,
    MODE_STORAGE,
)
from homeassistant.components.lovelace.dashboard import ConfigNotFound, LovelaceStorage
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ICON, CONF_ID, CONF_MODE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later

from .const import (
    CONF_DEVICE_ID,
    CONF_MAC,
    DASHBOARD_BASIC,
    DASHBOARD_BASIC_USERS_FULL_ADMINS,
    DASHBOARD_FULL,
    DASHBOARD_FULL_ADMINS_ONLY,
    DOMAIN,
    OPT_CREATE_DASHBOARD,
    OPT_DASHBOARD_ADMIN_ONLY,
    OPT_DASHBOARD_MODE,
)
from .lan import (
    CHLORINATOR_TYPE_CODES,
    LIQUID_CHLORINE_TYPE_CODES,
    MAIN_PUMP_TYPE_CODE,
    PH_DOSER_TYPE_CODES,
    POOL_HEATER_TYPE_CODES,
    SOLAR_TYPE_CODES,
    SPA_HEATER_TYPE_CODES,
    SplashMeLanAux,
    SplashMeLanCoordinator,
    SplashMeLanData,
    lan_device_identifier,
    unfitted_keys,
)

_LOGGER = logging.getLogger(__name__)

OPT_DASHBOARD_HASH = "dashboard_generated_hash"
DASHBOARD_ICON = "mdi:pool"
# Renaming the device renames all its entity IDs in a burst: rebuild once after it.
RENAME_REBUILD_DELAY = 2  # seconds


def _short_id(entry: ConfigEntry) -> str:
    mac = (entry.data.get(CONF_MAC) or entry.entry_id).replace(":", "")
    return mac[-6:].lower()


def dashboard_url_path(entry: ConfigEntry) -> str:
    """Sidebar URL of the entry's dashboard (must contain a hyphen)."""
    return f"splashme-{_short_id(entry)}"


def _device_label(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """The name the user gave the device in HA, else as the app shows it, e.g. SplashMe_24DCC32B9888."""
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, lan_device_identifier(entry)), entry.entry_id)
    if device is not None and device.name_by_user:
        return device.name_by_user
    device_id = entry.data.get(CONF_DEVICE_ID) or ""
    return f"SplashMe_{device_id.replace('_', '')}" if device_id else entry.title


def dashboard_mode(entry: ConfigEntry) -> str:
    """The dashboard choice; "administrators only" from v0.1.11 is the full layout for admins."""
    mode = entry.options.get(OPT_DASHBOARD_MODE)
    if mode:
        return mode
    return DASHBOARD_FULL_ADMINS_ONLY if entry.options.get(OPT_DASHBOARD_ADMIN_ONLY) else DASHBOARD_FULL


def _wanted_dashboards(entry: ConfigEntry) -> list[tuple[bool, str]]:
    """(admin dashboard?, layout) for each dashboard the options ask for."""
    mode = dashboard_mode(entry)
    if mode == DASHBOARD_BASIC:
        return [(False, DASHBOARD_BASIC)]
    if mode == DASHBOARD_BASIC_USERS_FULL_ADMINS:
        return [(False, DASHBOARD_BASIC), (True, DASHBOARD_FULL)]
    return [(False, DASHBOARD_FULL)]


def _dashboard_item(entry: ConfigEntry, admin: bool = False) -> dict[str, Any]:
    """The Pool dashboard, or the Pool Admin one that goes with a basic Pool."""
    suffix = "-admin" if admin else ""
    return {
        CONF_ID: f"splashme_{_short_id(entry)}{suffix.replace('-', '_')}",
        CONF_URL_PATH: f"{dashboard_url_path(entry)}{suffix}",
        CONF_TITLE: "Pool Admin" if admin else "Pool",
        CONF_ICON: DASHBOARD_ICON,
        CONF_REQUIRE_ADMIN: admin or dashboard_mode(entry) == DASHBOARD_FULL_ADMINS_ONLY,
        CONF_SHOW_IN_SIDEBAR: True,
        CONF_MODE: MODE_STORAGE,
    }


def _hash_option(admin: bool) -> str:
    return f"{OPT_DASHBOARD_HASH}_admin" if admin else OPT_DASHBOARD_HASH


def _present(hass: HomeAssistant, url_path: str) -> bool:
    return url_path in hass.data[LOVELACE_DATA].dashboards or url_path in hass.data.get(DATA_PANELS, {})


def _config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(config, sort_keys=True).encode()).hexdigest()


async def async_register_dashboard(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SplashMeLanCoordinator, *, reset: bool = False
) -> None:
    """Create or refresh the entry's dashboard(s) and their sidebar panels.

    reset: overwrite a dashboard the user edited, so it is generated again.
    """
    wanted = _wanted_dashboards(entry) if entry.options.get(OPT_CREATE_DASHBOARD, True) else []
    # Drop what an earlier version or setting created and the options no longer ask for.
    for admin in (False, True):
        item = _dashboard_item(entry, admin)
        if not any(a == admin for a, _layout in wanted) and _present(hass, item[CONF_URL_PATH]):
            await _async_remove_one(hass, item)
    for admin, layout in wanted:
        await _async_register_one(hass, entry, coordinator, admin, layout, reset)


async def _async_register_one(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SplashMeLanCoordinator, admin: bool, layout: str, reset: bool
) -> None:
    item = _dashboard_item(entry, admin)
    url_path = item[CONF_URL_PATH]
    hash_option = _hash_option(admin)
    dashboards = hass.data[LOVELACE_DATA].dashboards
    storage = dashboards.get(url_path)
    if not isinstance(storage, LovelaceStorage):
        storage = LovelaceStorage(hass, item)
        dashboards[url_path] = storage
    frontend.async_register_built_in_panel(
        hass,
        "lovelace",
        sidebar_title=item[CONF_TITLE],
        sidebar_icon=item[CONF_ICON],
        frontend_url_path=url_path,
        config={"mode": MODE_STORAGE},
        require_admin=item[CONF_REQUIRE_ADMIN],
        update=url_path in hass.data.get(DATA_PANELS, {}),
    )

    generated = build_dashboard_config(hass, entry, coordinator, layout)
    try:
        current = await storage.async_load(False)
    except ConfigNotFound:
        current = None
    if current is not None and not reset:
        if _config_hash(current) != entry.options.get(hash_option):
            # Edited by the user in the UI: keep their version.
            return
        if _config_hash(current) == _config_hash(generated):
            return
    await storage.async_save(generated)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, hash_option: _config_hash(generated)}
    )
    _LOGGER.info("Dashboard /%s (%s layout) generated for %s", url_path, layout, entry.title)


async def async_remove_dashboard(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the entry's dashboards, their stored config and sidebar panels."""
    for admin in (False, True):
        await _async_remove_one(hass, _dashboard_item(entry, admin))


async def _async_remove_one(hass: HomeAssistant, item: dict[str, Any]) -> None:
    url_path = item[CONF_URL_PATH]
    lovelace = hass.data.get(LOVELACE_DATA)
    storage = lovelace.dashboards.pop(url_path, None) if lovelace else None
    frontend.async_remove_panel(hass, url_path, warn_if_unknown=False)
    if not isinstance(storage, LovelaceStorage):
        storage = LovelaceStorage(hass, item)
    await storage.async_delete()
    _LOGGER.info("Dashboard /%s removed", url_path)


@callback
def async_track_renames(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SplashMeLanCoordinator
) -> None:
    """Rebuild the dashboard when the user renames the device or its entities.

    The layout stores entity IDs and names (a name with "heat" also puts an output
    under Heating), so without this it would point at IDs that no longer exist,
    or show old names, until the integration reloads. A dashboard the user
    edited is still left alone by async_register_dashboard.
    """
    identifier = (DOMAIN, lan_device_identifier(entry))
    cancel: CALLBACK_TYPE | None = None

    @callback
    def _rebuild(_now: datetime) -> None:
        nonlocal cancel
        cancel = None
        entry.async_create_task(hass, async_register_dashboard(hass, entry, coordinator))

    @callback
    def _schedule(_event: Event) -> None:
        nonlocal cancel
        if cancel is not None:
            cancel()
        cancel = async_call_later(hass, RENAME_REBUILD_DELAY, _rebuild)

    @callback
    def _entity_renamed(data: er.EventEntityRegistryUpdatedData) -> bool:
        if data["action"] != "update" or ("old_entity_id" not in data and "name" not in data["changes"]):
            return False
        ent = er.async_get(hass).async_get(data["entity_id"])
        return ent is not None and ent.config_entry_id == entry.entry_id

    @callback
    def _device_renamed(data: dr.EventDeviceRegistryUpdatedData) -> bool:
        if data["action"] != "update" or "name_by_user" not in data["changes"]:
            return False
        device = dr.async_get(hass).async_get(data["device_id"])
        return device is not None and identifier in device.identifiers

    @callback
    def _cancel() -> None:
        if cancel is not None:
            cancel()

    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _schedule, event_filter=_entity_renamed)
    )
    entry.async_on_unload(
        hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _schedule, event_filter=_device_renamed)
    )
    entry.async_on_unload(_cancel)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def _registry_entries(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, er.RegistryEntry]:
    """Map unique-id key (after the device prefix) -> entity registry entry."""
    prefix = f"{lan_device_identifier(entry)}_"
    out: dict[str, er.RegistryEntry] = {}
    for ent in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if ent.unique_id.startswith(prefix):
            out[ent.unique_id[len(prefix) :]] = ent
    return out


def _tile(entity: str, name: str | None = None, **extra: Any) -> dict[str, Any]:
    card: dict[str, Any] = {"type": "tile", "entity": entity}
    if name:
        card["name"] = name
    card.update(extra)
    return card


def _toggle_tile(entity: str, name: str | None = None, **extra: Any) -> dict[str, Any]:
    return _tile(entity, name, features=[{"type": "toggle"}], **extra)


def _heading(text: str, icon: str) -> dict[str, Any]:
    return {"type": "heading", "heading": text, "icon": icon}


def build_dashboard_config(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SplashMeLanCoordinator, layout: str = DASHBOARD_FULL
) -> dict[str, Any]:
    """Generate the sections layout (full or basic) from the device's current entities."""
    entries = _registry_entries(hass, entry)
    ent = {key: e.entity_id for key, e in entries.items()}
    data = coordinator.data

    def aux_name(a: SplashMeLanAux, key: str) -> str:
        """An output's name: the one given to its entity in Home Assistant, else the controller's."""
        reg = entries.get(key)
        return (reg.name if reg is not None else None) or a.display_name

    # Dosing and heating state entities exist whatever is fitted: only show those
    # of equipment assigned to an output.
    unfitted = unfitted_keys(data)
    ent = {key: entity_id for key, entity_id in ent.items() if key not in unfitted}
    # Only a variable speed pump gets the speed slider. A single speed pump only
    # reads 0 or 100 %: show it on or off instead of a speed.
    variable_speed = "pump_speed_setpoint" in ent
    aux = list(data.aux) if data is not None else []
    pump = next((a for a in aux if a.type_code == MAIN_PUMP_TYPE_CODE), None)
    # A heater on a general output gets no target temperature (the controller only
    # switches it), but its name gives it away: "Heater", "Heat Pump".
    aux_heaters = [
        a for a in aux
        if a is not pump and not a.is_pump_linked and f"aux_{a.slot}" in ent
        and "heat" in aux_name(a, f"aux_{a.slot}").lower()
    ]

    def tiles(*specs: tuple[str, str | None]) -> list[dict[str, Any]]:
        return [_tile(ent[key], name) for key, name in specs if key in ent]

    heating_tabs = [
        tab for tab in (
            _heater_tab(ent, "Pool", "heating_pool_target", (
                ("Pool Heater", "heating_pool", "heater_state"),
                ("Solar Heater", "heating_solar", "solar_state"),
            )),
            _heater_tab(ent, "Spa", "heating_spa_target", (
                ("Spa Heater", "heating_spa", "spa_heater_state"),
                ("Solar Heater", "heating_solar", "solar_state"),
            )),
        ) if tab
    ]
    aux_heater_tiles = [_toggle_tile(ent[f"aux_{a.slot}"], aux_name(a, f"aux_{a.slot}")) for a in aux_heaters]
    light_tiles = [
        _toggle_tile(ent[f"light_{a.slot}"], aux_name(a, f"light_{a.slot}"))
        for a in aux if a.is_light and f"light_{a.slot}" in ent
    ]
    pump_tiles: list[dict[str, Any]] = []
    if pump is not None and f"aux_{pump.slot}" in ent:
        pump_tiles.append(_toggle_tile(ent[f"aux_{pump.slot}"], "Filter Pump"))
    if "spa_mode" in ent:
        pump_tiles.append(_toggle_tile(ent["spa_mode"], "Spa Mode"))
    if "pump_speed_setpoint" in ent:
        pump_tiles.append(
            _tile(ent["pump_speed_setpoint"], "Filter Pump Speed",
                  features=[{"type": "numeric-input", "style": "slider"}])
        )

    if layout == DASHBOARD_BASIC:
        # -- Basic: what a household switches day to day, nothing to maintain the pool with --
        if heating_tabs:
            heating = [_heater_card(ent, heating_tabs)]
        else:
            heating = tiles(("water_temp", "Water Temperature"))
        heating += aux_heater_tiles
        if heating_tabs or aux_heaters:
            heating.insert(0, _heading("Heating", "mdi:fire"))
        else:
            heating.insert(0, _heading("Temperature", "mdi:thermometer"))
        outputs = [
            _toggle_tile(ent[f"aux_{a.slot}"], aux_name(a, f"aux_{a.slot}"))
            for a in aux
            if a is not pump and not a.is_light and a not in aux_heaters and f"aux_{a.slot}" in ent
        ]
        equipment = [_heading("Equipment", "mdi:pool-thermometer"), *pump_tiles, *light_tiles, *outputs]
        return {
            "title": _device_label(hass, entry),
            "views": [{
                "title": "Pool",
                "path": "pool",
                "type": "sections",
                "max_columns": 2,
                "sections": [{"type": "grid", "cards": equipment}, {"type": "grid", "cards": heating}],
            }],
        }

    # -- Dosing drums ------------------------------------------------------
    drums: list[dict[str, Any]] = []
    for kind, label, color in (("acid", "Acid", "#ef6c00"), ("chlorine", "Chlorine", None)):
        if f"{kind}_remaining" not in ent:
            continue
        card: dict[str, Any] = {
            "type": "custom:splashme-tank-card",
            "name": label,
            "entity": ent[f"{kind}_remaining"],
            "grid_options": {"columns": 6, "rows": "auto"},
        }
        if f"{kind}_drum_volume" in ent:
            card["capacity_entity"] = ent[f"{kind}_drum_volume"]
        if f"reset_{kind}_volume" in ent:
            card["reset_entity"] = ent[f"reset_{kind}_volume"]
        if color:
            card["color"] = color
        drums.append(card)
    drums += tiles(("ph_doses_today", "pH Doses Today"), ("chlorine_doses_today", "Chlorine Doses Today"))
    if drums:
        drums.insert(0, _heading("Dosing Drums", "mdi:barrel"))

    # -- Heating (temperature dials, or the temperatures without heating equipment) --
    if heating_tabs:
        heating = [_heater_card(ent, heating_tabs)]
    else:
        # The heater card shows these otherwise.
        heating = tiles(("water_temp", "Water Temperature"), ("solar_temp", "Ambient Temperature"))
    heating += aux_heater_tiles
    if heating:
        if heating_tabs or aux_heaters:
            heading = _heading("Heating", "mdi:fire")
        else:
            heading = _heading("Temperature", "mdi:thermometer")
        drums = [heading, *heating, *drums]

    # -- Water quality, targets, schedules ----------------------------------
    water: list[dict[str, Any]] = [_heading("Water Quality", "mdi:water-check")]
    if "actual_ph" in ent or "actual_orp" in ent:
        water.append(_status_card("custom:splashme-chemistry-card", ent, {
            "stable_entity": "chemistry_stable",
            "pump_entity": f"aux_{pump.slot}" if pump is not None else None,
            "orp_entity": "actual_orp", "orp_target_entity": _orp_target_key(data),
            "orp_dosing_entity": "orp_switch_status",
            "orp_enable_entity": "dosing_liquid" if "dosing_liquid" in ent else "dosing_chlorinator",
            "ph_entity": "actual_ph", "ph_target_entity": "desired_ph_level",
            "ph_dosing_entity": "ph_switch_status",
            "ph_enable_entity": "dosing_ph",
        }))
    else:
        water += tiles(
            ("actual_ph", "pH"), ("actual_orp", "ORP"),
            ("ph_switch_status", "pH Dosing"), ("orp_switch_status", "Chlorine Dosing"),
            ("chemistry_stable", "Chemistry Stable"),
        )
    # Temperatures show in the Heating (or Temperature) section.
    water += tiles(("connectivity", "Connectivity"), ("dry_run", "Dry Run"))
    if "actual_ph" not in ent and "actual_orp" not in ent:
        # No chemistry card to edit them in.
        targets = tiles(
            ("desired_ph_level", "Target pH"),
            ("desired_orp_level_mineral", "Target ORP (Mineral)"),
            ("desired_orp_level_liquid", "Target ORP (Liquid)"),
        )
        if targets:
            water += [_heading("Targets", "mdi:target"), *targets]
    # Linked-device records are folded into their pump schedule, as in the app.
    schedules = [
        ent[key]
        for key in sorted(ent, key=_schedule_sort)
        if key.startswith("schedule_") and data.linked_to_schedule(int(key.removeprefix("schedule_"))) is None
    ]
    if schedules:
        water += [
            _heading("Schedules", "mdi:calendar-clock"),
            {"type": "entities", "entities": schedules},
        ]

    # -- Equipment -----------------------------------------------------------
    equipment: list[dict[str, Any]] = [_heading("Equipment", "mdi:pool-thermometer"), *pump_tiles]
    equipment += tiles(("pump_cooldown", "Heater Cooldown"), ("pump_cooldown_left", "Cooldown Remaining"))
    equipment += light_tiles
    for a in aux:
        if (a is not pump and not a.is_light and not a.is_pump_linked and a not in aux_heaters
                and f"aux_{a.slot}" in ent):
            equipment.append(_toggle_tile(ent[f"aux_{a.slot}"], aux_name(a, f"aux_{a.slot}")))
    linked = [a for a in aux if a.is_pump_linked and f"aux_{a.slot}" in ent]
    if linked:
        equipment.append(_heading("Runs With The Pump", "mdi:link-variant"))
        equipment += [
            _toggle_tile(ent[f"aux_{a.slot}"], aux_name(a, f"aux_{a.slot}"), icon="mdi:link-variant") for a in linked
        ]
    if "actual_pump_speed" in ent or "actual_flow_rate" in ent:
        equipment = [
            _heading("Filtration", "mdi:pump"),
            _status_card("custom:splashme-filtration-card", ent, {
                "speed_entity": "actual_pump_speed" if variable_speed else None,
                "flow_entity": "actual_flow_rate",
                "status_entity": "pump_status", "prime_entity": "pump_prime_left",
                "pressure_entity": "actual_pressure", "type_entity": "pump_brand",
                "mode_entity": "pump_mode",
                "pump_entity": f"aux_{pump.slot}" if pump is not None else None,
            }) | {"linked": [{"name": aux_name(a, f"aux_{a.slot}"), "entity": ent[f"aux_{a.slot}"]} for a in linked]},
            *equipment,
        ]
    # -- Trends (history graphs on their own view, apart from the controls) --
    def history(title: str, keys: tuple[str, ...], hours: int = 24) -> dict[str, Any] | None:
        entities = [ent[key] for key in keys if key in ent]
        if not entities:
            return None
        return {
            "type": "history-graph",
            "title": title,
            "hours_to_show": hours,
            "entities": entities,
            # The section spans 3 view columns = 36 grid columns: two graphs per row.
            "grid_options": {"columns": 18, "rows": "auto"},
        }

    activity_keys = ["heater_state", "solar_state", "spa_heater_state", "ph_switch_status",
                     "orp_switch_status", "spa_mode", "pump_cooldown"]
    if pump is not None:
        activity_keys.insert(0, f"aux_{pump.slot}")
    activity_keys += [f"aux_{a.slot}" for a in aux_heaters]
    if variable_speed:
        pump_series: tuple[str, ...] = ("actual_pump_speed",)
    elif pump is not None:
        pump_series = (f"aux_{pump.slot}",)  # single speed: on/off
    else:
        pump_series = ()
    graphs = [
        history("Chemistry", ("actual_ph", "actual_orp")),
        history("Temperature", ("water_temp", "solar_temp")),
        history("Pump", (*pump_series, "actual_flow_rate", "actual_pressure")),
        history("Activity", tuple(activity_keys)),
    ]
    trends = [g for g in graphs if g]

    views: list[dict[str, Any]] = [
        {
            "title": "Controls",
            "path": "pool",
            "type": "sections",
            "max_columns": 3,
            "sections": [
                {"type": "grid", "cards": drums},
                {"type": "grid", "cards": water},
                {"type": "grid", "cards": equipment},
            ],
        }
    ]
    if trends:
        views.append({
            "title": "Trends",
            "path": "trends",
            "type": "sections",
            "max_columns": 3,
            "sections": [{"type": "grid", "column_span": 3, "cards": trends}],
        })
    return {"title": _device_label(hass, entry), "views": views}


def _status_card(card_type: str, ent: dict[str, str], keys: dict[str, str | None]) -> dict[str, Any]:
    """A full-width card whose options are entity ids, skipping entities that do not exist."""
    card: dict[str, Any] = {"type": card_type, "grid_options": {"columns": "full", "rows": "auto"}}
    card.update({option: ent[key] for option, key in keys.items() if key and key in ent})
    return card




def _heater_card(ent: dict[str, str], heating_tabs: list[dict[str, Any]]) -> dict[str, Any]:
    return _status_card("custom:splashme-heater-card", ent, {
        "water_entity": "water_temp", "ambient_entity": "solar_temp",
    }) | {"tabs": heating_tabs}


def _orp_target_key(data: SplashMeLanData | None) -> str:
    """The ORP target that applies: a liquid chlorine doser uses its own setpoint."""
    liquid = data is not None and data.has_equipment(LIQUID_CHLORINE_TYPE_CODES)
    return "desired_orp_level_liquid" if liquid else "desired_orp_level_mineral"


def _heater_tab(
    ent: dict[str, str], title: str, target: str, rows: tuple[tuple[str, str, str], ...]
) -> dict[str, Any] | None:
    """One heater-card tab: the target dial plus an enable row per heating source that exists."""
    if target not in ent:
        return None
    return {
        "title": title,
        "target_entity": ent[target],
        "heaters": [
            {"name": name, "enable_entity": ent[enable], "state_entity": ent.get(state)}
            for name, enable, state in rows
            if enable in ent
        ],
    }


def _schedule_sort(key: str) -> tuple[int, str]:
    if key.startswith("schedule_"):
        try:
            return (int(key.split("_", 1)[1]), key)
        except ValueError:
            return (999, key)
    return (999, key)
