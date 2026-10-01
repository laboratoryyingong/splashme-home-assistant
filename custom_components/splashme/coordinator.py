"""Data coordinator for SplashMe."""

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
import logging
import time
from typing import Any

from aiohttp import ClientError, ClientResponseError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SplashMeApiClient, SplashMeUserInfo
from .const import (
    COORDINATOR_NAME,
    INSTALLER_ROLE,
    OPT_PUMP_SPEED_SETPOINTS,
    OPT_SELECTED_DEVICE_ID,
    OPT_SELECTED_SITE_ID,
    SCAN_INTERVAL_SECONDS,
    SITES_REFRESH_INTERVAL_SECONDS,
)


@dataclass(frozen=True, slots=True)
class SplashMeAux:
    """Aux state for a device."""

    key: str
    category: str
    aux_tag: str
    aux_type_code: int
    aux_status: bool
    aux_slot_num: int
    pump_speed: int
    actual_flow_rate: int
    aux_timer: int | None
    aux_timer_remain_sec: int | None

    @property
    def display_name(self) -> str:
        """Return the preferred display name."""
        return self.aux_tag or f"{self.category.replace('_', ' ').title()} {self.aux_slot_num}"


@dataclass(frozen=True, slots=True)
class SplashMePump:
    """Pump information for a device."""

    pump_type: str | None
    pump_brand: str | None
    pump_model: str | None
    motor_status: int | None
    actual_flowrate: int | None
    actual_speed: int | None
    # Heater cool-down: the firmware keeps the main pump running after the
    # heater switches off; cooldown_left is the remaining time in seconds.
    cooldown: bool | None
    cooldown_left: int | None


@dataclass(frozen=True, slots=True)
class SplashMeChemistry:
    """Chemistry information for a device."""

    ph_value: int | None
    orp_value: int | None
    desired_ph_value: int | None
    desired_orp_value: int | None


@dataclass(frozen=True, slots=True)
class SplashMeTemperature:
    """Temperature information for a device."""

    water_temp: float | None
    ambient_temp: float | None


@dataclass(frozen=True, slots=True)
class SplashMeDeviceSnapshot:
    """Full snapshot for a device."""

    pump: SplashMePump | None
    chemistry: SplashMeChemistry | None
    temperature: SplashMeTemperature | None
    aux: tuple[SplashMeAux, ...]
    # Raw settings objects from the chemistry card; treat as read-only.
    # Kept whole because writes must send every field back (the firmware
    # zeroes missing fields).
    ph_settings: dict[str, Any] | None = None
    chlorine_settings: dict[str, Any] | None = None
    # Dashboard telemetry (live dosing relay states, heater flags, ...).
    dashboard: dict[str, Any] | None = None
    # Device schedules (raw dicts from scheduleInfo); None until first fetch.
    schedules: tuple[dict[str, Any], ...] | None = None


@dataclass(frozen=True, slots=True)
class SplashMeDevice:
    """Device metadata."""

    site_id: str
    site_name: str
    object_id: str
    device_id: str
    tag: str
    address: str
    online: bool

    @property
    def display_name(self) -> str:
        """Return the preferred display name."""
        return self.tag or self.address or self.device_id

    @property
    def select_label(self) -> str:
        """Return the device label used in selectors."""
        return f"{self.display_name} ({self.device_id})"


@dataclass(frozen=True, slots=True)
class SplashMeSite:
    """Site metadata."""

    object_id: str
    name: str
    city: str
    street: str
    devices: tuple[SplashMeDevice, ...]

    @property
    def display_name(self) -> str:
        """Return the preferred display name."""
        return self.name or self.street or self.city or self.object_id


@dataclass(frozen=True, slots=True)
class SplashMeCoordinatorData:
    """Coordinator state."""

    role: str | None
    sites: tuple[SplashMeSite, ...]
    devices: tuple[SplashMeDevice, ...]
    selected_site_id: str | None
    selected_device_id: str | None
    snapshots: dict[str, SplashMeDeviceSnapshot | None]


class SplashMeDataCoordinator(DataUpdateCoordinator[SplashMeCoordinatorData]):
    """Coordinate SplashMe data fetching."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SplashMeApiClient,
        user_info: SplashMeUserInfo,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=logging.getLogger(__name__),
            name=COORDINATOR_NAME,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self._api = api
        self._role = user_info.role
        self._config_entry = config_entry
        # Restore the last selection; _build_data self-heals stale ids.
        self._selected_site_id: str | None = config_entry.options.get(OPT_SELECTED_SITE_ID)
        self._selected_device_id: str | None = config_entry.options.get(OPT_SELECTED_DEVICE_ID)
        self._last_sites_refresh_monotonic = 0.0

    def _persist_selection(self) -> None:
        """Persist the site/device selection so it survives restarts."""
        options = {
            **self._config_entry.options,
            OPT_SELECTED_SITE_ID: self._selected_site_id,
            OPT_SELECTED_DEVICE_ID: self._selected_device_id,
        }
        if options != dict(self._config_entry.options):
            self.hass.config_entries.async_update_entry(self._config_entry, options=options)

    def pump_speed_setpoint(self, device_id: str) -> int | None:
        """Return the persisted pump speed setpoint for a device."""
        setpoints = self._config_entry.options.get(OPT_PUMP_SPEED_SETPOINTS) or {}
        value = setpoints.get(device_id)
        return int(value) if value is not None else None

    def set_pump_speed_setpoint(self, device_id: str, speed: int) -> None:
        """Persist the pump speed setpoint (applied on the next pump start)."""
        setpoints = dict(self._config_entry.options.get(OPT_PUMP_SPEED_SETPOINTS) or {})
        if setpoints.get(device_id) == speed:
            return
        setpoints[device_id] = speed
        options = {**self._config_entry.options, OPT_PUMP_SPEED_SETPOINTS: setpoints}
        self.hass.config_entries.async_update_entry(self._config_entry, options=options)

    @property
    def site_select_visible(self) -> bool:
        """Return whether the site selector should be shown."""
        data = self.data
        return data is not None and (
            (data.role or "").lower() == INSTALLER_ROLE or len(data.sites) > 1
        )

    @property
    def site_options(self) -> list[str]:
        """Return site selector options."""
        data = self.data
        if data is None:
            return []
        return [site.display_name for site in data.sites]

    @property
    def selected_site_label(self) -> str | None:
        """Return the selected site label."""
        data = self.data
        if data is None or data.selected_site_id is None:
            return None
        for site in data.sites:
            if site.object_id == data.selected_site_id:
                return site.display_name
        return None

    @property
    def device_options(self) -> list[str]:
        """Return device selector options."""
        return [device.select_label for device in self._filtered_devices()]

    @property
    def selected_device_label(self) -> str | None:
        """Return the selected device label."""
        data = self.data
        if data is None or data.selected_device_id is None:
            return None
        for device in data.devices:
            if device.device_id == data.selected_device_id:
                return device.select_label
        return None

    async def async_select_site(self, option: str) -> None:
        """Select a site."""
        data = self.data
        if data is None:
            return
        for site in data.sites:
            if site.display_name != option:
                continue
            self._selected_site_id = site.object_id
            filtered_devices = self._filtered_devices(
                sites=data.sites,
                selected_site_id=site.object_id,
            )
            self._selected_device_id = filtered_devices[0].device_id if filtered_devices else None
            self._persist_selection()
            self.async_set_updated_data(self._build_data(data.sites, data.snapshots))
            await self.async_refresh_selected_device()
            return

    async def async_select_device(self, option: str) -> None:
        """Select a device."""
        data = self.data
        if data is None:
            return
        for device in self._filtered_devices():
            if device.select_label != option:
                continue
            self._selected_device_id = device.device_id
            self._persist_selection()
            self.async_set_updated_data(self._build_data(data.sites, data.snapshots))
            await self.async_refresh_selected_device()
            return

    async def _async_update_data(self) -> SplashMeCoordinatorData:
        """Fetch data from SplashMe."""
        sites = self.data.sites if self.data is not None else ()
        if self.data is None or self._should_refresh_sites():
            try:
                sites = await self._async_fetch_sites()
            except (ClientError, ClientResponseError) as err:
                raise UpdateFailed(f"Failed to fetch SplashMe sites: {err}") from err
            self._last_sites_refresh_monotonic = time.monotonic()

        try:
            online_statuses = await self._api.async_get_device_online_statuses()
        except (ClientError, ClientResponseError) as err:
            if self.data is None:
                raise UpdateFailed(f"Failed to fetch SplashMe online statuses: {err}") from err
            self.logger.warning("Failed to refresh SplashMe online statuses: %s", err)
        else:
            sites = self._apply_online_statuses(sites, online_statuses)

        snapshots = dict(self.data.snapshots) if self.data is not None else {}
        valid_device_ids = {device.device_id for site in sites for device in site.devices}
        snapshots = {
            device_id: snapshot
            for device_id, snapshot in snapshots.items()
            if device_id in valid_device_ids
        }

        # Keep the selected device's snapshot fresh on every poll; the backend
        # serves these reads from cache instantly and refreshes in background.
        selected_device = self._get_selected_or_first_device(sites)
        if selected_device is not None and selected_device.online:
            try:
                snapshots[selected_device.device_id] = await self._async_fetch_device_snapshot(
                    selected_device.device_id
                )
            except (ClientError, ClientResponseError) as err:
                self.logger.warning(
                    "Failed to refresh device snapshot for %s: %s",
                    selected_device.device_id,
                    err,
                )

        return self._build_data(sites, snapshots)

    async def async_refresh_selected_device(self) -> None:
        """Fetch fresh data for the currently selected device."""
        data = self.data
        if data is None:
            await self.async_refresh()
            return

        selected_device = self._get_selected_or_first_device(data.sites)
        if selected_device is None:
            self.async_set_updated_data(self._build_data(data.sites, data.snapshots))
            return

        await self.async_refresh_device_snapshot(selected_device.device_id)

    async def async_refresh_device_snapshot(self, device_id: str) -> None:
        """Fetch fresh detail data for a specific device."""
        data = self.data
        if data is None:
            await self.async_refresh()
            data = self.data
            if data is None:
                return

        device = next(
            (device for device in data.devices if device.device_id == device_id),
            None,
        )
        if device is None or not device.online:
            self.async_set_updated_data(self._build_data(data.sites, data.snapshots))
            return

        snapshots = dict(data.snapshots)
        snapshots[device.device_id] = await self._async_fetch_device_snapshot(device.device_id)
        self.async_set_updated_data(self._build_data(data.sites, snapshots))

    async def _async_fetch_sites(self) -> tuple[SplashMeSite, ...]:
        """Fetch sites and devices."""
        raw_sites = await self._api.async_get_user_sites()
        sites: list[SplashMeSite] = []
        for raw_site in raw_sites:
            site_id = str(raw_site.get("objectId") or "")
            site_name = str(raw_site.get("name") or "")
            devices: list[SplashMeDevice] = []
            for raw_device in raw_site.get("device_list") or []:
                device_id = str(raw_device.get("deviceId") or "")
                if not device_id:
                    continue
                devices.append(
                    SplashMeDevice(
                        site_id=site_id,
                        site_name=site_name,
                        object_id=str(raw_device.get("objectId") or ""),
                        device_id=device_id,
                        tag=str(raw_device.get("tag") or ""),
                        address=str(raw_device.get("address") or ""),
                        online=bool(raw_device.get("online")),
                    )
                )
            sites.append(
                SplashMeSite(
                    object_id=site_id,
                    name=site_name,
                    city=str(raw_site.get("city") or ""),
                    street=str(raw_site.get("street") or ""),
                    devices=tuple(devices),
                )
            )
        return tuple(sites)

    async def _async_fetch_device_snapshot(self, device_id: str) -> SplashMeDeviceSnapshot:
        """Fetch all data required for a device."""
        results = await asyncio.gather(
            self._api.async_get_pump_info(device_id),
            self._api.async_get_aux_info(device_id),
            self._api.async_get_chemistry_card(device_id),
            self._api.async_get_ambient_temp(device_id),
            self._api.async_get_dashboard_info(device_id),
            self._api.async_get_schedule_info(device_id),
            return_exceptions=True,
        )

        pump_raw = results[0] if isinstance(results[0], dict) else {}
        aux_raw = results[1] if isinstance(results[1], dict) else {}
        chemistry_card = results[2] if isinstance(results[2], dict) else {}
        temperature_raw = results[3] if isinstance(results[3], dict) else {}
        dashboard_raw = results[4] if isinstance(results[4], dict) else None
        schedules_raw = tuple(results[5]) if isinstance(results[5], list) else None

        chemistry_raw = dict(chemistry_card.get("chemistry") or {})
        ph_settings = chemistry_card.get("phSettings")
        chlorine_settings = chemistry_card.get("chlorineSettings")

        pump = SplashMePump(
            pump_type=_str_or_none(pump_raw.get("pump_type")),
            pump_brand=_str_or_none(pump_raw.get("pump_brand")),
            pump_model=_str_or_none(pump_raw.get("pump_model")),
            motor_status=_int_or_none(pump_raw.get("motor_status")),
            actual_flowrate=_int_or_none(pump_raw.get("actual_flowrate")),
            actual_speed=_int_or_none(pump_raw.get("actual_speed")),
            cooldown=(
                None if pump_raw.get("pump_cooldown") is None
                else bool(pump_raw.get("pump_cooldown"))
            ),
            cooldown_left=_int_or_none(pump_raw.get("pump_cooldown_left")),
        )
        chemistry = SplashMeChemistry(
            ph_value=_int_or_none(chemistry_raw.get("ph_value")),
            orp_value=_int_or_none(chemistry_raw.get("orp_value")),
            desired_ph_value=_int_or_none(chemistry_raw.get("desire_ph_value")),
            desired_orp_value=_int_or_none(chemistry_raw.get("desire_orp_value")),
        )
        # The device already applies calibration offsets to these readings.
        # Water temperature is returned in deci-degrees; ambient temperature in centi-degrees.
        temperature = SplashMeTemperature(
            water_temp=_scaled_value(temperature_raw.get("water_temp"), 10),
            ambient_temp=_scaled_value(temperature_raw.get("ambient_temp"), 100),
        )

        aux: list[SplashMeAux] = []
        for category in (
            "pump_devices",
            "pump_linked_devices",
            "spa_devices",
            "spa",
            "other_devices",
        ):
            for raw_aux in aux_raw.get(category) or []:
                slot_num = _int_or_default(raw_aux.get("aux_slot_num"))
                type_code = _int_or_default(raw_aux.get("aux_type_code"))
                aux.append(
                    SplashMeAux(
                        key=f"{category}:{slot_num}:{type_code}",
                        category=category,
                        aux_tag=str(raw_aux.get("aux_tag") or ""),
                        aux_type_code=type_code,
                        aux_status=bool(raw_aux.get("aux_status")),
                        aux_slot_num=slot_num,
                        pump_speed=_int_or_default(raw_aux.get("pump_speed")),
                        actual_flow_rate=_int_or_default(raw_aux.get("actual_flow_rate")),
                        aux_timer=_int_or_none(raw_aux.get("aux_timer")),
                        aux_timer_remain_sec=_int_or_none(raw_aux.get("aux_timer_remain_sec")),
                    )
                )

        return SplashMeDeviceSnapshot(
            pump=pump,
            chemistry=chemistry,
            temperature=temperature,
            aux=tuple(aux),
            ph_settings=dict(ph_settings) if isinstance(ph_settings, dict) else None,
            chlorine_settings=(
                dict(chlorine_settings) if isinstance(chlorine_settings, dict) else None
            ),
            dashboard=dashboard_raw,
            schedules=schedules_raw,
        )

    async def async_set_schedule_state(
        self, device_id: str, schedule_id: int, state: bool
    ) -> None:
        """Enable/disable a schedule, then refresh the device snapshot."""
        await self._api.async_change_schedule_state(device_id, schedule_id, state)
        await self.async_refresh_device_snapshot(device_id)

    async def async_create_schedule(self, device_id: str, body: dict[str, Any]) -> None:
        """Create a schedule, then refresh the device snapshot."""
        await self._api.async_create_schedule(device_id, body)
        await self.async_refresh_device_snapshot(device_id)

    async def async_update_schedule(self, device_id: str, body: dict[str, Any]) -> None:
        """Update a schedule, then refresh the device snapshot."""
        await self._api.async_update_schedule(device_id, body)
        await self.async_refresh_device_snapshot(device_id)

    async def async_delete_schedule(self, device_id: str, schedule_num: int) -> None:
        """Delete a schedule, then refresh the device snapshot."""
        await self._api.async_delete_schedule(device_id, schedule_num)
        await self.async_refresh_device_snapshot(device_id)

    async def async_update_ph_settings(
        self, device_id: str, changes: dict[str, Any]
    ) -> None:
        """Apply changes on top of the current pH settings and write them back.

        The firmware zeroes any field missing from the payload, so the full
        settings object from the latest snapshot is always sent.
        """
        snapshot = self.data.snapshots.get(device_id) if self.data else None
        if snapshot is None or not snapshot.ph_settings:
            raise UpdateFailed("pH settings are not available yet; refresh first")
        settings = {**snapshot.ph_settings, "reset_acid_volume": False, **changes}
        await self._api.async_set_ph_settings(device_id, settings)
        await self.async_refresh_device_snapshot(device_id)

    async def async_update_chlorine_settings(
        self, device_id: str, changes: dict[str, Any]
    ) -> None:
        """Apply changes on top of the current chlorine settings and write them back."""
        snapshot = self.data.snapshots.get(device_id) if self.data else None
        if snapshot is None or not snapshot.chlorine_settings:
            raise UpdateFailed("Chlorine settings are not available yet; refresh first")
        settings = {
            **snapshot.chlorine_settings,
            "reset_chlorine_volume": False,
            **changes,
        }
        await self._api.async_set_chlorine_settings(device_id, settings)
        await self.async_refresh_device_snapshot(device_id)

    def _build_data(
        self,
        sites: tuple[SplashMeSite, ...],
        snapshots: dict[str, SplashMeDeviceSnapshot | None],
    ) -> SplashMeCoordinatorData:
        """Build coordinator data."""
        devices = tuple(device for site in sites for device in site.devices)
        available_site_ids = {site.object_id for site in sites}
        if self._selected_site_id not in available_site_ids:
            self._selected_site_id = sites[0].object_id if sites else None

        filtered_devices = self._filtered_devices(
            sites=sites,
            selected_site_id=self._selected_site_id,
        )
        available_device_ids = {device.device_id for device in filtered_devices}
        if self._selected_device_id not in available_device_ids:
            # 默认选择：优先选在线设备（避免落到离线/装死的第一台），
            # 并持久化，否则每次重启都回退丢失（见 site/device 漂移问题）。
            default_device = next(
                (d for d in filtered_devices if d.online),
                filtered_devices[0] if filtered_devices else None,
            )
            self._selected_device_id = default_device.device_id if default_device else None
            if self._selected_device_id is not None:
                self._persist_selection()

        return SplashMeCoordinatorData(
            role=self._role,
            sites=sites,
            devices=devices,
            selected_site_id=self._selected_site_id,
            selected_device_id=self._selected_device_id,
            snapshots=snapshots,
        )

    def _apply_online_statuses(
        self,
        sites: tuple[SplashMeSite, ...],
        online_statuses: dict[str, bool],
    ) -> tuple[SplashMeSite, ...]:
        """Merge the latest online statuses into the site inventory."""
        return tuple(
            replace(
                site,
                devices=tuple(
                    replace(
                        device,
                        online=online_statuses.get(device.device_id, device.online),
                    )
                    for device in site.devices
                ),
            )
            for site in sites
        )

    def _should_refresh_sites(self) -> bool:
        """Return whether the site inventory needs a refresh."""
        return (
            time.monotonic() - self._last_sites_refresh_monotonic
            >= SITES_REFRESH_INTERVAL_SECONDS
        )

    def _get_selected_or_first_device(
        self,
        sites: tuple[SplashMeSite, ...],
    ) -> SplashMeDevice | None:
        """Return the currently selected device, or the first available one."""
        filtered_devices = self._filtered_devices(
            sites=sites,
            selected_site_id=self._selected_site_id,
        )
        if not filtered_devices:
            return None
        for device in filtered_devices:
            if device.device_id == self._selected_device_id:
                return device
        return filtered_devices[0]

    def _filtered_devices(
        self,
        *,
        sites: tuple[SplashMeSite, ...] | None = None,
        selected_site_id: str | None = None,
    ) -> list[SplashMeDevice]:
        """Return devices filtered by the current site selection."""
        sites = sites if sites is not None else (self.data.sites if self.data else ())
        selected_site_id = (
            selected_site_id if selected_site_id is not None else self._selected_site_id
        )
        if selected_site_id is None:
            return [device for site in sites for device in site.devices]
        return [
            device
            for site in sites
            if site.object_id == selected_site_id
            for device in site.devices
        ]


def _int_or_none(value: Any) -> int | None:
    """Convert a value to int when possible."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or_default(value: Any, default: int = 0) -> int:
    """Convert a value to int with a default fallback."""
    parsed = _int_or_none(value)
    return default if parsed is None else parsed


def _str_or_none(value: Any) -> str | None:
    """Convert a value to string when present."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    """Convert a value to float when possible."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scaled_value(value: Any, divisor: float) -> float | None:
    """Convert a raw numeric value and apply its display scale."""
    parsed = _float_or_none(value)
    if parsed is None:
        return None
    return parsed / divisor
