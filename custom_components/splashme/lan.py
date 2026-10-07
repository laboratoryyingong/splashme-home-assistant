"""Local LAN client, coordinator, and entity base for SplashMe devices.

Devices with firmware >= 2.5.41 run an HTTP server on port 8080 and announce
themselves via mDNS (_splashme._tcp). All communication is plain HTTP on the
local network; the cloud is only contacted once, during setup, to fetch the
device's protocol v2 envelope key.

Everything goes through the device's protocol v2 bridge (`POST
/sm/2/lan/pv2`): `op=state` returns the whole state frame (telemetry +
numeric config) without a signed request, `op=a` runs signed actions (aux,
schedule, and the `cmd` passthrough to any v1 command) and `op=q` runs
signed queries. No v1 JSON command is used directly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
import json
import logging
import socket
from typing import Any

import aiohttp
from aiohttp import ClientError, ClientSession

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from . import pv2
from .lights import (
    COLOUR_SETTLE_SECONDS,
    LIGHT_UNKNOWN,
    light_group,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_MAC,
    DOMAIN,
    LAN_PORT,
    LAN_SCAN_INTERVAL_SECONDS,
    LAN_TIMEOUT_SECONDS,
    MANUFACTURER,
)

_LOGGER = logging.getLogger(__name__)

# Brief outages are normal (AP OTA, WiFi TLS handshake): keep serving the last
# data for a few failed polls before marking entities unavailable.
LAN_MAX_CONSECUTIVE_FAILURES = 3
# Schedules and dosing settings change rarely: query them every N polls.
SLOW_QUERY_EVERY = 4
# Aux writes are queued on the device; read back once after this delay.
WRITE_CONFIRM_DELAY_SECONDS = 2

# Aux slots that are dosing equipment (chlorinators, dosers): run by the
# chemistry logic, shown read-only, never as switches.
DOSING_TYPE_CODES = frozenset({5, 6, 7, 8})
# Pressure cleaner, in-floor cleaning, ioniser, ozone, UV: run with the filter
# pump. Switching one on makes the firmware start the pump; losing flow
# switches them all off.
PUMP_LINKED_TYPE_CODES = frozenset({3, 4, 9, 10, 11})
PH_DOSER_TYPE_CODES = frozenset({6})
CHLORINATOR_TYPE_CODES = frozenset({5, 7})  # salt, mineral: run under ORP control
LIQUID_CHLORINE_TYPE_CODES = frozenset({8})
# Heating equipment is run by the firmware's heating logic, never switched by
# hand: the user picks which sources may heat and the target temperatures.
POOL_HEATER_TYPE_CODES = frozenset({2, 32})  # pool heater, spa & pool heater
SPA_HEATER_TYPE_CODES = frozenset({19, 32})  # spa heater, spa & pool heater
SOLAR_TYPE_CODES = frozenset({17, 18})  # solar dedicated, solar filter pump
HEATING_TYPE_CODES = POOL_HEATER_TYPE_CODES | SPA_HEATER_TYPE_CODES | SOLAR_TYPE_CODES
# Return, suction, extra 1, extra 2: the firmware interlocks them, so any one
# of them switched sets spa mode for all (this is the order it prefers).
SPA_ACTUATOR_TYPE_CODES_ORDERED = (20, 21, 33, 34)
SPA_ACTUATOR_TYPE_CODES = frozenset(SPA_ACTUATOR_TYPE_CODES_ORDERED)
EMPTY_SLOT_TYPE_CODE = 0
MAIN_PUMP_TYPE_CODE = 31
DEFAULT_PUMP_SPEED = 40


class LanError(HomeAssistantError):
    """The device rejected or could not serve a request."""


class SplashMeLanClient:
    """HTTP client for a device's LAN server.

    The device serves one connection at a time, so requests are serialized
    behind a lock.
    """

    def __init__(
        self, session: ClientSession, host: str, key: bytes | None = None, port: int = LAN_PORT
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.host = host
        self._port = port
        self._key = key
        self._lock = asyncio.Lock()
        self._counter = pv2.Counter()

    async def _async_post(self, command: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST one command and return the raw JSON response."""
        async with self._lock:
            async with self._session.post(
                f"http://{self.host}:{self._port}/sm/2/lan/{command}",
                json=body,
                timeout=aiohttp.ClientTimeout(total=LAN_TIMEOUT_SECONDS),
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)

    # -- protocol v2 --------------------------------------------------------

    def _require_key(self) -> bytes:
        if self._key is None:
            raise LanError("Device envelope key is not configured")
        return self._key

    async def _async_pv2(self, op: str, name: str = "", payload: bytes | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"op": op, "name": name, "env": ""}
        if payload is not None:
            body["env"] = pv2.wrap(self._require_key(), self._counter.next(), payload).hex()
        data = await self._async_post("pv2", body)
        if data.get("ok") is False:
            raise LanError(f"Device refused pv2 {op} {name}: {data.get('err') or 'not ok'}")
        return data

    def _decode_env(self, data: dict[str, Any]) -> bytes | None:
        env_hex = data.get("env")
        if not env_hex:
            return None
        try:
            return pv2.unwrap(self._require_key(), bytes.fromhex(env_hex)).payload
        except (ValueError, pv2.PV2Error) as err:
            raise LanError(f"Bad device envelope: {err}") from err

    async def async_state(self) -> pv2.Frame:
        """Fetch a full state frame (reads need no signed request)."""
        payload = self._decode_env(await self._async_pv2("state"))
        if payload is None:
            raise LanError("Device returned no state frame")
        return pv2.decode_frame(payload)

    async def async_probe(self) -> pv2.Frame:
        """Connectivity probe: a state frame read without verifying the envelope.

        Works before the device key is known (setup, rediscovery); the
        result is only used for reachability and the firmware version.
        """
        env_hex = (await self._async_pv2("state")).get("env")
        if not env_hex:
            raise LanError("Device returned no state frame")
        try:
            return pv2.decode_frame(pv2.unwrap_unverified(bytes.fromhex(env_hex)).payload)
        except (ValueError, pv2.PV2Error) as err:
            raise LanError(f"Bad device envelope: {err}") from err

    async def async_action(self, name: str, payload: bytes) -> pv2.Frame | None:
        """Send a signed action; return the confirmation state frame if any."""
        raw = self._decode_env(await self._async_pv2("a", name, payload))
        return None if raw is None else pv2.decode_frame(raw)

    async def async_query(self, name: str, payload: bytes = b"") -> bytes:
        """Send a signed query and return the raw response payload."""
        raw = self._decode_env(await self._async_pv2("q", name, payload))
        if raw is None:
            raise LanError(f"Device returned no answer for query {name}")
        return raw

    async def async_query_json(self, name: str) -> dict[str, Any]:
        """Run a v1 GET command through the signed query passthrough."""
        raw = await self.async_query(name)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as err:
            raise LanError(f"Query {name} returned invalid JSON") from err
        return data.get("d") if isinstance(data, dict) and "d" in data else data


@dataclass(frozen=True, slots=True)
class SplashMeLanAux:
    """One output slot, assembled from the state frame and aux_names."""

    slot: int
    type_code: int
    name: str
    mode: int  # 0 off / 1-20 schedule / 99 manual
    trump: bool
    timer_remain_sec: int | None
    is_virtual: bool = False  # no relay behind it (RS485 ozone slot)

    @property
    def is_on(self) -> bool:
        """Return whether the slot is energised."""
        return self.mode != pv2.AUX_MODE_OFF

    @property
    def display_name(self) -> str:
        """Return the preferred display name."""
        return self.name or f"Aux {self.slot}"

    @property
    def is_light(self) -> bool:
        """Return whether this slot drives a pool/spa light."""
        return light_group(self.type_code) is not None

    @property
    def is_pump_linked(self) -> bool:
        """Return whether this slot only runs together with the filter pump."""
        return self.type_code in PUMP_LINKED_TYPE_CODES

    @property
    def is_switchable(self) -> bool:
        """Return whether this slot should get a switch entity (lights get a light).

        Dosing and heating slots are controlled by the firmware (their state
        shows in the heater/solar/dosing sensors). Spa actuators always move
        together and get one "Spa Mode" switch instead. The RS485 mineral
        chlorinator ozone slot (26 on 37-slot firmware) is virtual: it has no
        relay and is driven by the minerals settings, so it is shown but never
        switched.
        """
        return (
            self.type_code != EMPTY_SLOT_TYPE_CODE
            and self.type_code not in DOSING_TYPE_CODES
            and self.type_code not in HEATING_TYPE_CODES
            and self.type_code not in SPA_ACTUATOR_TYPE_CODES
            and not self.is_light
            and not self.is_virtual
        )


@dataclass(frozen=True, slots=True)
class SplashMeLanData:
    """Coordinator data for one LAN device."""

    telemetry: pv2.Telemetry | None = None
    config: pv2.Config | None = None
    timers: tuple[pv2.AuxTimer, ...] = ()
    aux_names: tuple[str, ...] = ()
    pump: pv2.PumpID | None = None
    schedules: tuple[pv2.Schedule, ...] = ()
    ph_settings: dict[str, Any] | None = None
    chlorine_settings: dict[str, Any] | None = None
    lights: dict[str, Any] | None = None  # get_pool_light_switch_mode `d`

    def _light_field(self, slot: int, field: str) -> int | None:
        aux = self.aux_by_slot(slot)
        group = light_group(aux.type_code) if aux else None
        if group is None or not self.lights:
            return None
        value = self.lights.get(f"g_{group}_{field}")
        return None if value is None else int(value)

    def light_type(self, slot: int) -> int:
        """Return the brand protocol (LightTypeMode) configured for the slot's group."""
        return self._light_field(slot, "light_type_mode") or LIGHT_UNKNOWN

    def light_colour(self, slot: int) -> int | None:
        """Return the last stored colour index for the slot's group (99 = never set)."""
        return self._light_field(slot, "light_mode")

    @property
    def aux(self) -> tuple[SplashMeLanAux, ...]:
        """Return every slot the frame describes."""
        if self.config is None:
            return ()
        modes = self.telemetry.aux_mode if self.telemetry else ()
        trump = self.telemetry.aux_trump if self.telemetry else frozenset()
        remain = {t.slot: t.remain_sec for t in self.timers}
        four_boards = len(self.config.aux_type) >= pv2.AUX_COUNT_4_EXPANDERS
        return tuple(
            SplashMeLanAux(
                slot=slot,
                type_code=type_code,
                name=self.aux_names[slot] if slot < len(self.aux_names) else "",
                mode=modes[slot] if slot < len(modes) else pv2.AUX_MODE_OFF,
                trump=slot in trump,
                timer_remain_sec=remain.get(slot),
                is_virtual=four_boards and slot == pv2.ECOCLEAR_VIRTUAL_SLOT,
            )
            for slot, type_code in enumerate(self.config.aux_type)
        )

    def has_equipment(self, type_codes: frozenset[int]) -> bool:
        """Return whether any slot is assigned one of the given types."""
        return any(a.type_code in type_codes for a in self.aux)

    @property
    def has_pool_heating(self) -> bool:
        """Return whether anything can heat the pool."""
        return self.has_equipment(POOL_HEATER_TYPE_CODES | SOLAR_TYPE_CODES)

    @property
    def has_spa_heating(self) -> bool:
        """Return whether anything can heat the spa (solar needs a spa to heat)."""
        return self.has_equipment(SPA_HEATER_TYPE_CODES) or (
            self.has_equipment(SOLAR_TYPE_CODES) and self.has_equipment(SPA_ACTUATOR_TYPE_CODES)
        )

    @property
    def spa_actuators(self) -> tuple[SplashMeLanAux, ...]:
        """Spa actuator slots, primary first."""
        return tuple(
            a
            for code in SPA_ACTUATOR_TYPE_CODES_ORDERED
            for a in self.aux
            if a.type_code == code
        )

    @property
    def spa_on(self) -> bool:
        """Spa mode as the firmware sees it: any actuator energised."""
        return any(a.is_on for a in self.spa_actuators)

    def expander_present(self, board: int) -> bool:
        """Return whether any slot of LoRa expander board 1-4 has equipment assigned."""
        if self.config is None:
            return False
        types = self.config.aux_type
        return any(
            slot < len(types) and types[slot] != EMPTY_SLOT_TYPE_CODE
            for slot in pv2.EXPANDER_SLOTS[board]
        )

    def aux_by_slot(self, slot: int) -> SplashMeLanAux | None:
        """Return the aux entry for a slot."""
        for aux in self.aux:
            if aux.slot == slot:
                return aux
        return None

    @property
    def main_pump(self) -> SplashMeLanAux | None:
        """Return the filter pump slot."""
        for aux in self.aux:
            if aux.type_code == MAIN_PUMP_TYPE_CODE:
                return aux
        return None

    @property
    def equipment_signature(self) -> tuple[Any, ...]:
        """What the slot entities and the dashboard are derived from."""
        pump = (self.pump.brand, self.pump.model) if self.pump else None
        slots = tuple(
            (a.slot, a.type_code, a.name, self.light_type(a.slot) if a.is_light else None)
            for a in self.aux
        )
        schedules = tuple((s.index, s.name) for s in self.schedules if s.defined)
        return (pump, slots, schedules)

    def schedule_by_index(self, index: int) -> pv2.Schedule | None:
        """Return the schedule with the given index."""
        for sched in self.schedules:
            if sched.index == index:
                return sched
        return None

    def with_frame(self, frame: pv2.Frame) -> SplashMeLanData:
        """Overlay a frame: sections absent from the mask keep their old value."""
        changes: dict[str, Any] = {}
        if frame.telemetry is not None:
            changes["telemetry"] = frame.telemetry
            # The timer tail travels with telemetry; absent means none running.
            changes["timers"] = frame.timers
        if frame.config is not None:
            changes["config"] = frame.config
        return replace(self, **changes)


class SplashMeLanCoordinator(DataUpdateCoordinator[SplashMeLanData]):
    """Poll one LAN device (name discovery, IP communication)."""

    def __init__(
        self, hass: HomeAssistant, client: SplashMeLanClient, entry: ConfigEntry
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"splashme_lan_{entry.data.get(CONF_MAC) or client.host}",
            update_interval=timedelta(seconds=LAN_SCAN_INTERVAL_SECONDS),
        )
        self.client = client
        self.entry = entry
        self._failures = 0
        self._polls = 0
        self._confirm_task: asyncio.Task | None = None
        self._light_busy_until: dict[int, float] = {}

    # -- polling ------------------------------------------------------------

    async def _async_update_data(self) -> SplashMeLanData:
        """One state frame per poll, plus the slow queries every few polls."""
        data = self.data or SplashMeLanData()
        try:
            try:
                frame = await self.client.async_state()
            except (ClientError, TimeoutError):
                # DHCP may have moved the device: re-resolve its mDNS name.
                if not await self._async_rediscover():
                    raise
                frame = await self.client.async_state()
            previous = data.config
            data = data.with_frame(frame)
            if data.config is not None and (
                len(data.aux_names) != len(data.config.aux_type)
                or (previous is not None and previous.aux_type != data.config.aux_type)
                or data.pump is None
            ):
                # A slot was (re)assigned in the app: its name changes with it.
                data = await self._async_fetch_identity(data)
            if self._polls % SLOW_QUERY_EVERY == 0 or not data.schedules:
                data = await self._async_fetch_slow(data)
        except (ClientError, TimeoutError, LanError, pv2.PV2Error) as err:
            self._failures += 1
            if self.data is not None and self._failures < LAN_MAX_CONSECUTIVE_FAILURES:
                _LOGGER.debug(
                    "LAN poll failed (%s/%s), keeping last data: %s",
                    self._failures,
                    LAN_MAX_CONSECUTIVE_FAILURES,
                    err,
                )
                return self.data
            raise UpdateFailed(f"LAN device unreachable: {err}") from err

        self._failures = 0
        self._polls += 1
        return data

    async def _async_rediscover(self) -> bool:
        """Resolve splashme-<mac>.local and move to the new IP if it answers.

        Zeroconf handles this on hosts that see multicast; resolving the
        hostname also works where only unicast DNS forwarding is available
        (e.g. Home Assistant in a Docker bridge network).
        """
        mac = self.entry.data.get(CONF_MAC)
        if not mac:
            return False
        hostname = lan_hostname(mac)
        try:
            ip = await self.hass.async_add_executor_job(socket.gethostbyname, hostname)
        except OSError:
            return False
        if ip == self.client.host:
            return False
        old_host = self.client.host
        self.client.host = ip
        try:
            await self.client.async_probe()
        except (ClientError, TimeoutError, LanError):
            self.client.host = old_host
            return False
        _LOGGER.info("LAN device %s moved from %s to %s", hostname, old_host, ip)
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_HOST: ip}
        )
        return True

    async def _async_fetch_identity(self, data: SplashMeLanData) -> SplashMeLanData:
        names = pv2.decode_aux_names(await self.client.async_query("aux_names"))
        pump = pv2.decode_pump_id(await self.client.async_query("pump_id"))
        return replace(data, aux_names=names, pump=pump)

    async def _async_fetch_slow(self, data: SplashMeLanData) -> SplashMeLanData:
        # After a device reboot the pump reads as "Single Speed" until the
        # RS485 identification finishes; keep asking until a real model shows.
        pump = data.pump
        if pump is None or not pump.model:
            pump = pv2.decode_pump_id(await self.client.async_query("pump_id"))
        return replace(
            data,
            pump=pump,
            aux_names=pv2.decode_aux_names(await self.client.async_query("aux_names")),
            schedules=pv2.decode_schedules(await self.client.async_query("schedules")),
            ph_settings=await self.client.async_query_json("get_ph_settings"),
            chlorine_settings=await self.client.async_query_json("get_chlorine_settings"),
            lights=await self.client.async_query_json("get_pool_light_switch_mode"),
        )

    def _apply_frame(self, frame: pv2.Frame | None) -> None:
        """Publish an action's confirmation frame to entities immediately."""
        if frame is not None and self.data is not None:
            self.async_set_updated_data(self.data.with_frame(frame))

    def _schedule_confirm_refresh(self) -> None:
        """Read the state back shortly after a queued (v1) write."""
        if self._confirm_task is not None and not self._confirm_task.done():
            return

        async def _confirm() -> None:
            await asyncio.sleep(WRITE_CONFIRM_DELAY_SECONDS)
            await self.async_request_refresh()

        self._confirm_task = self.hass.async_create_task(_confirm())

    # -- writes -------------------------------------------------------------

    async def async_set_aux(self, slot: int, on: bool, pump_speed: int | None = None) -> None:
        """Switch an aux slot through the signed cmd passthrough of v2_manual_set.

        This mirrors the mobile app exactly (manual override while on, cleared
        when switched off) and carries the pump speed. The native `aux` action
        was tried on the bench: trump=0 is reverted by the scheduler within a
        second and trump=1 leaves the override flag set after switching off.
        The device queues the relay change, so a read-back is scheduled.
        """
        body: dict[str, Any] = {"aux_switch_state": on, "aux_slot_num": slot}
        if pump_speed is not None:
            body["pump_speed"] = pump_speed
        await self._async_action("cmd", pv2.encode_cmd("v2_manual_set", json.dumps(body).encode()))
        self._schedule_confirm_refresh()

    async def _async_action(self, name: str, payload: bytes) -> None:
        try:
            self._apply_frame(await self.client.async_action(name, payload))
        except (ClientError, TimeoutError) as err:
            raise LanError(f"Device unreachable for {name}: {err}") from err

    async def async_set_heating(self, changes: dict[str, Any]) -> None:
        """Change target temperatures / heater selection.

        `v2_temp_set` takes the whole set at once and zeroes what is missing,
        so the current values are read back first. Keys: desire_pool_temp,
        desire_spa_temp, is_solar_heater_selected, is_pool_heater_selected,
        is_spa_heater_selected.
        """
        try:
            current = await self.client.async_query_json("v2_temp_get")
            by_category = {
                e.get("category"): e for e in current.get("temperature") or [] if isinstance(e, dict)
            }
            pool, spa = by_category.get("pool"), by_category.get("spa")
            if pool is None or spa is None:
                raise LanError("Device returned no temperature settings")
            body = {
                "desire_pool_temp": pool.get("target_temp", 0),
                "desire_spa_temp": spa.get("target_temp", 0),
                "is_solar_heater_selected": bool(pool.get("is_solar_heater_selected")),
                "is_pool_heater_selected": bool(pool.get("is_pool_heater_selected")),
                "is_spa_heater_selected": bool(pool.get("is_spa_heater_selected")),
                "temp2_ch": current.get("temp2_ch", 0),
                "temp3_ch": current.get("temp3_ch", 0),
                **changes,
            }
            await self._async_action("cmd", pv2.encode_cmd("v2_temp_set", json.dumps(body).encode()))
            # The cmd reply carries no config section: read the result back.
            self._apply_frame(await self.client.async_state())
        except (ClientError, TimeoutError) as err:
            raise LanError(f"Device unreachable for heating settings: {err}") from err

    async def _async_refresh_schedules(self) -> None:
        if self.data is None:
            return
        schedules = pv2.decode_schedules(await self.client.async_query("schedules"))
        self.async_set_updated_data(replace(self.data, schedules=schedules))

    async def async_set_schedule_enabled(self, index: int, enabled: bool) -> None:
        """Enable or disable a schedule."""
        await self._async_action("schedule", pv2.encode_schedule_enable(index, enabled))
        await self._async_refresh_schedules()

    async def async_create_schedule(self, fields: pv2.ScheduleFields) -> None:
        """Create a schedule."""
        await self._async_action("schedule", pv2.encode_schedule_create(fields))
        await self._async_refresh_schedules()

    async def async_update_schedule(self, index: int, fields: pv2.ScheduleFields) -> None:
        """Replace a schedule's definition."""
        await self._async_action("schedule", pv2.encode_schedule_update(index, fields))
        await self._async_refresh_schedules()

    async def async_delete_schedule(self, index: int) -> None:
        """Delete a schedule."""
        await self._async_action("schedule", pv2.encode_schedule_delete(index))
        await self._async_refresh_schedules()

    # -- lights -------------------------------------------------------------

    def light_busy(self, slot: int) -> bool:
        """Return whether a colour sequence is still running on the slot."""
        return self.hass.loop.time() < self._light_busy_until.get(slot, 0)

    async def async_set_light_colour(
        self, slot: int, colour: int, settle_seconds: int = COLOUR_SETTLE_SECONDS
    ) -> None:
        """Run a brand colour sequence; the light ends up on.

        The relay pulses for up to a couple of minutes (brand dependent) and
        the device suppresses state pushes meanwhile, so the slot is marked
        busy and read back once the sequence has settled.
        """
        light_type = self.data.light_type(slot) if self.data else LIGHT_UNKNOWN
        if light_type == LIGHT_UNKNOWN:
            raise LanError(f"Light on slot {slot} has no brand protocol configured")
        body = json.dumps({"slot": slot, "light_type": light_type, "light_color": colour}).encode()
        await self._async_action("cmd", pv2.encode_cmd("set_pool_light_switch_mode", body))
        self._light_busy_until[slot] = self.hass.loop.time() + settle_seconds

        async def _settle() -> None:
            await asyncio.sleep(settle_seconds)
            try:
                lights = await self.client.async_query_json("get_pool_light_switch_mode")
            except (ClientError, TimeoutError, LanError):
                lights = None
            self._light_busy_until.pop(slot, None)
            if lights is not None and self.data is not None:
                self.async_set_updated_data(replace(self.data, lights=lights))
            await self.async_request_refresh()

        self.hass.async_create_task(_settle())

    async def _async_write_settings(
        self, command: str, current: dict[str, Any] | None, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Write a full settings object (the firmware zeroes missing fields)."""
        if not current:
            raise LanError("Settings are not available yet; wait for the next poll")
        body = json.dumps({**current, **changes}).encode()
        await self._async_action("cmd", pv2.encode_cmd(command, body))
        return await self.client.async_query_json(f"get_{command.removeprefix('set_')}")

    async def async_update_ph_settings(self, changes: dict[str, Any]) -> None:
        """Apply changes on top of the current pH settings and write them back."""
        current = self.data.ph_settings if self.data else None
        settings = await self._async_write_settings(
            "set_ph_settings", current, {"reset_acid_volume": False, **changes}
        )
        self.async_set_updated_data(replace(self.data, ph_settings=settings))

    async def async_update_chlorine_settings(self, changes: dict[str, Any]) -> None:
        """Apply changes on top of the current chlorine settings and write them back."""
        current = self.data.chlorine_settings if self.data else None
        settings = await self._async_write_settings(
            "set_chlorine_settings", current, {"reset_chlorine_volume": False, **changes}
        )
        self.async_set_updated_data(replace(self.data, chlorine_settings=settings))


def lan_hostname(mac: str) -> str:
    """mDNS hostname the firmware announces: splashme-<last 6 hex of MAC>.local."""
    return f"splashme-{mac.replace(':', '')[-6:]}.local"


def lan_device_identifier(entry: ConfigEntry) -> str:
    """Return the stable device-registry identifier for a LAN entry."""
    mac = entry.data.get(CONF_MAC)
    return f"lan_{mac}" if mac else f"lan_{entry.entry_id}"


def lan_device_info(entry: ConfigEntry, fw_version: str | None = None) -> DeviceInfo:
    """Build DeviceInfo for a LAN entry."""
    mac = entry.data.get(CONF_MAC)
    info = DeviceInfo(
        identifiers={(DOMAIN, lan_device_identifier(entry))},
        manufacturer=MANUFACTURER,
        name=entry.title,
        model="Pool Controller (Local)",
        serial_number=entry.data.get(CONF_DEVICE_ID) or mac,
    )
    if mac:
        info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
    if fw_version:
        info["sw_version"] = fw_version
    return info


class SplashMeLanEntity(CoordinatorEntity[SplashMeLanCoordinator]):
    """Base entity for LAN-connected SplashMe devices."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SplashMeLanCoordinator, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._entry = entry
        config = coordinator.data.config if coordinator.data else None
        self._attr_device_info = lan_device_info(entry, config.fw_version if config else None)

    @property
    def unique_id_base(self) -> str:
        """Return the base for entity unique ids."""
        return lan_device_identifier(self._entry)
