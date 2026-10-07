"""A Lovelace dashboard per local device, owned by the integration.

The dashboard is registered as a storage-mode Lovelace panel when the LAN
entry loads and deleted when the entry is removed, so uninstalling the
integration leaves nothing behind in the sidebar. The layout is generated
from the device's entities; once the user edits it in the UI it is left
alone (the generated version is recognised by its hash).
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

from .const import CONF_DEVICE_ID, CONF_MAC, DOMAIN
from .lan import (
    CHLORINATOR_TYPE_CODES,
    LIQUID_CHLORINE_TYPE_CODES,
    MAIN_PUMP_TYPE_CODE,
    PH_DOSER_TYPE_CODES,
    POOL_HEATER_TYPE_CODES,
    SOLAR_TYPE_CODES,
    SPA_HEATER_TYPE_CODES,
    SplashMeLanCoordinator,
    SplashMeLanData,
    lan_device_identifier,
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
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, lan_device_identifier(entry))})
    if device is not None and device.name_by_user:
        return device.name_by_user
    device_id = entry.data.get(CONF_DEVICE_ID) or ""
    return f"SplashMe_{device_id.replace('_', '')}" if device_id else entry.title


def _dashboard_item(entry: ConfigEntry) -> dict[str, Any]:
    return {
        CONF_ID: f"splashme_{_short_id(entry)}",
        CONF_URL_PATH: dashboard_url_path(entry),
        CONF_TITLE: "Pool",
        CONF_ICON: DASHBOARD_ICON,
        CONF_REQUIRE_ADMIN: False,
        CONF_SHOW_IN_SIDEBAR: True,
        CONF_MODE: MODE_STORAGE,
    }


def _config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(config, sort_keys=True).encode()).hexdigest()


async def async_register_dashboard(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SplashMeLanCoordinator, *, reset: bool = False
) -> None:
    """Create or refresh the entry's dashboard and its sidebar panel.

    reset: overwrite a dashboard the user edited, so it is generated again.
    """
    item = _dashboard_item(entry)
    url_path = item[CONF_URL_PATH]
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
        require_admin=False,
        update=url_path in hass.data.get(DATA_PANELS, {}),
    )

    generated = build_dashboard_config(hass, entry, coordinator)
    try:
        current = await storage.async_load(False)
    except ConfigNotFound:
        current = None
    if current is not None and not reset:
        if _config_hash(current) != entry.options.get(OPT_DASHBOARD_HASH):
            # Edited by the user in the UI: keep their version.
            return
        if _config_hash(current) == _config_hash(generated):
            return
    await storage.async_save(generated)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, OPT_DASHBOARD_HASH: _config_hash(generated)}
    )
    _LOGGER.info("Dashboard /%s generated for %s", url_path, entry.title)


async def async_remove_dashboard(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the entry's dashboard, its stored config and sidebar panel."""
    item = _dashboard_item(entry)
    url_path = item[CONF_URL_PATH]
    lovelace = hass.data.get(LOVELACE_DATA)
    storage = lovelace.dashboards.pop(url_path, None) if lovelace else None
    frontend.async_remove_panel(hass, url_path, warn_if_unknown=False)
    if not isinstance(storage, LovelaceStorage):
        storage = LovelaceStorage(hass, item)
    await storage.async_delete()
    _LOGGER.info("Dashboard /%s removed with %s", url_path, entry.title)


@callback
def async_track_renames(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SplashMeLanCoordinator
) -> None:
    """Rebuild the dashboard when the user renames the device or its entity IDs.

    The layout stores entity IDs, so without this it would point at IDs that no
    longer exist until the integration reloads. A dashboard the user edited is
    still left alone by async_register_dashboard.
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
        if data["action"] != "update" or "old_entity_id" not in data:
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


def _entity_map(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str]:
    """Map unique-id key (after the device prefix) -> entity_id."""
    prefix = f"{lan_device_identifier(entry)}_"
    out: dict[str, str] = {}
    for ent in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if ent.unique_id.startswith(prefix):
            out[ent.unique_id[len(prefix) :]] = ent.entity_id
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
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SplashMeLanCoordinator
) -> dict[str, Any]:
    """Generate the sections layout from the device's current entities."""
    ent = _entity_map(hass, entry)
    data = coordinator.data
    # Dosing and heating state entities exist whatever is fitted: only show those
    # of equipment assigned to an output.
    unfitted = _unfitted(data)
    ent = {key: entity_id for key, entity_id in ent.items() if key not in unfitted}
    aux = list(data.aux) if data is not None else []
    pump = next((a for a in aux if a.type_code == MAIN_PUMP_TYPE_CODE), None)
    # A heater on a general output gets no target temperature (the controller only
    # switches it), but its name gives it away: "Heater", "Heat Pump".
    aux_heaters = [
        a for a in aux
        if a is not pump and not a.is_pump_linked and f"aux_{a.slot}" in ent
        and "heat" in a.display_name.lower()
    ]

    def tiles(*specs: tuple[str, str | None]) -> list[dict[str, Any]]:
        return [_tile(ent[key], name) for key, name in specs if key in ent]

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
    if heating_tabs:
        heating = [_status_card("custom:splashme-heater-card", ent, {
            "water_entity": "water_temp", "ambient_entity": "solar_temp",
        }) | {"tabs": heating_tabs}]
    else:
        # The heater card shows these otherwise.
        heating = tiles(("water_temp", "Water Temperature"), ("solar_temp", "Ambient Temperature"))
    heating += [_toggle_tile(ent[f"aux_{a.slot}"], a.display_name) for a in aux_heaters]
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
    schedules = [
        ent[key] for key in sorted(ent, key=_schedule_sort) if key.startswith("schedule_")
    ]
    if schedules:
        water += [
            _heading("Schedules", "mdi:calendar-clock"),
            {"type": "entities", "entities": schedules},
        ]

    # -- Equipment -----------------------------------------------------------
    equipment: list[dict[str, Any]] = [_heading("Equipment", "mdi:pool-thermometer")]
    if pump is not None and f"aux_{pump.slot}" in ent:
        equipment.append(_toggle_tile(ent[f"aux_{pump.slot}"], "Filter Pump"))
    if "spa_mode" in ent:
        equipment.append(_toggle_tile(ent["spa_mode"], "Spa Mode"))
    if "pump_speed_setpoint" in ent:
        equipment.append(
            _tile(ent["pump_speed_setpoint"], "Filter Pump Speed",
                  features=[{"type": "numeric-input", "style": "slider"}])
        )
    equipment += tiles(("pump_cooldown", "Heater Cooldown"), ("pump_cooldown_left", "Cooldown Remaining"))
    for a in aux:
        if a.is_light and f"light_{a.slot}" in ent:
            equipment.append(_toggle_tile(ent[f"light_{a.slot}"], a.display_name))
    for a in aux:
        if (a is not pump and not a.is_light and not a.is_pump_linked and a not in aux_heaters
                and f"aux_{a.slot}" in ent):
            equipment.append(_toggle_tile(ent[f"aux_{a.slot}"], a.display_name))
    linked = [a for a in aux if a.is_pump_linked and f"aux_{a.slot}" in ent]
    if linked:
        equipment.append(_heading("Runs With The Pump", "mdi:link-variant"))
        equipment += [
            _toggle_tile(ent[f"aux_{a.slot}"], a.display_name, icon="mdi:link-variant") for a in linked
        ]
    if "actual_pump_speed" in ent or "actual_flow_rate" in ent:
        equipment = [
            _heading("Filtration", "mdi:pump"),
            _status_card("custom:splashme-filtration-card", ent, {
                "speed_entity": "actual_pump_speed", "flow_entity": "actual_flow_rate",
                "pressure_entity": "actual_pressure", "type_entity": "pump_brand",
                "mode_entity": "pump_mode",
                "pump_entity": f"aux_{pump.slot}" if pump is not None else None,
            }) | {"linked": [{"name": a.display_name, "entity": ent[f"aux_{a.slot}"]} for a in linked]},
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
    graphs = [
        history("Chemistry", ("actual_ph", "actual_orp")),
        history("Temperature", ("water_temp", "solar_temp")),
        history("Pump", ("actual_pump_speed", "actual_flow_rate", "actual_pressure")),
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


def _unfitted(data: SplashMeLanData | None) -> set[str]:
    """Entity keys of dosing and heating equipment that no output is assigned to."""

    def fitted(codes: frozenset[int]) -> bool:
        return data is not None and data.has_equipment(codes)

    keys: set[str] = set()
    if not fitted(PH_DOSER_TYPE_CODES):
        keys |= {"acid_remaining", "acid_drum_volume", "reset_acid_volume", "ph_doses_today", "ph_switch_status"}
    if not fitted(LIQUID_CHLORINE_TYPE_CODES):  # only liquid chlorine comes in drums
        keys |= {"chlorine_remaining", "chlorine_drum_volume", "reset_chlorine_volume", "chlorine_doses_today"}
    if not fitted(CHLORINATOR_TYPE_CODES | LIQUID_CHLORINE_TYPE_CODES):
        keys.add("orp_switch_status")
    if not fitted(POOL_HEATER_TYPE_CODES):
        keys.add("heater_state")
    if not fitted(SPA_HEATER_TYPE_CODES):
        keys.add("spa_heater_state")
    if not fitted(SOLAR_TYPE_CODES):
        keys.add("solar_state")
    if not fitted(POOL_HEATER_TYPE_CODES | SPA_HEATER_TYPE_CODES):  # the pump cools a heater after it stops
        keys |= {"pump_cooldown", "pump_cooldown_left"}
    return keys


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
