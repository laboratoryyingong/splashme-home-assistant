"""SplashMe command protocol v2: signed envelope, state frame, actions, queries.

Pure Python port of the firmware contract (splashme_esp32
docs/protocol_v2_app_guide.md) and the backend reference implementation
(backend/internal/pv2). No Home Assistant imports so it can be unit tested
standalone. All integers on the wire are little-endian.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import struct
import time

from cryptography.hazmat.primitives import cmac
from cryptography.hazmat.primitives.ciphers import algorithms

ENVELOPE_VERSION = 0x01
CMAC_LEN = 4
_ENVELOPE_MIN = 1 + 4 + CMAC_LEN

SECTION_A = 0x01
SECTION_B = 0x02
SECTION_TAIL = 0x04

AUX_MODE_OFF = 0
AUX_MODE_MANUAL = 99


class PV2Error(ValueError):
    """Malformed or unverifiable v2 data."""


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def cmac4(key: bytes, data: bytes) -> bytes:
    """AES-128-CMAC (RFC 4493) truncated to 4 bytes."""
    mac = cmac.CMAC(algorithms.AES(key))
    mac.update(data)
    return mac.finalize()[:CMAC_LEN]


@dataclass(frozen=True, slots=True)
class Envelope:
    """A verified envelope."""

    ver: int
    counter: int
    payload: bytes


def wrap(key: bytes, counter: int, payload: bytes) -> bytes:
    """Sign payload into [ver][counter u32][payload][cmac4]."""
    head = struct.pack("<BI", ENVELOPE_VERSION, counter & 0xFFFFFFFF) + payload
    return head + cmac4(key, head)


def unwrap(key: bytes, buf: bytes) -> Envelope:
    """Verify an envelope and return its contents."""
    if len(buf) < _ENVELOPE_MIN:
        raise PV2Error(f"envelope too short ({len(buf)} bytes)")
    if buf[0] != ENVELOPE_VERSION:
        raise PV2Error(f"unsupported envelope version 0x{buf[0]:02x}")
    body, tag = buf[:-CMAC_LEN], buf[-CMAC_LEN:]
    if not hmac.compare_digest(cmac4(key, body), tag):
        raise PV2Error("CMAC mismatch")
    return Envelope(ver=buf[0], counter=struct.unpack_from("<I", buf, 1)[0], payload=body[5:])


def unwrap_unverified(buf: bytes) -> Envelope:
    """Split an envelope without checking the CMAC.

    Only for probing a device before its key is known; never trust the
    payload for control decisions.
    """
    if len(buf) < _ENVELOPE_MIN:
        raise PV2Error(f"envelope too short ({len(buf)} bytes)")
    if buf[0] != ENVELOPE_VERSION:
        raise PV2Error(f"unsupported envelope version 0x{buf[0]:02x}")
    return Envelope(ver=buf[0], counter=struct.unpack_from("<I", buf, 1)[0], payload=buf[5:-CMAC_LEN])


class Counter:
    """Strictly increasing action counter.

    The device drops any inbound frame whose counter is <= the last accepted
    one, and the app and backend share that baseline. Both use unix seconds,
    so writers interleave naturally and nothing needs persisting across
    restarts; two writes in one second are spaced by bumping past the last.
    """

    def __init__(self) -> None:
        """Initialize the counter."""
        self._last = 0

    def next(self) -> int:
        """Return the next counter value."""
        self._last = max(self._last + 1, int(time.time()))
        return self._last


# ---------------------------------------------------------------------------
# Bounded little-endian reader
# ---------------------------------------------------------------------------


class _Reader:
    """Reads past the section limit yield None, which keeps decoding
    forward compatible: older frames give missing trailing fields, newer
    frames carry extra bytes the caller skips via the length prefix."""

    __slots__ = ("buf", "off", "limit")

    def __init__(self, buf: bytes, off: int = 0, limit: int | None = None) -> None:
        self.buf = buf
        self.off = off
        self.limit = len(buf) if limit is None else limit

    def _take(self, fmt: str, size: int) -> int | None:
        if self.off + size > self.limit:
            return None
        value = struct.unpack_from(fmt, self.buf, self.off)[0]
        self.off += size
        return value

    def u8(self) -> int | None:
        return self._take("<B", 1)

    def u16(self) -> int | None:
        return self._take("<H", 2)

    def i16(self) -> int | None:
        return self._take("<h", 2)

    def u32(self) -> int | None:
        return self._take("<I", 4)

    def i32(self) -> int | None:
        return self._take("<i", 4)

    def string(self) -> str | None:
        """[len u8][utf8]."""
        n = self.u8()
        if n is None or self.off + n > self.limit:
            return None
        s = self.buf[self.off : self.off + n].decode("utf-8", "replace")
        self.off += n
        return s


def _u(value: int | None) -> int:
    return 0 if value is None else value


# ---------------------------------------------------------------------------
# State frame
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Telemetry:
    """Section A, scaled to engineering units (docs §3.3)."""

    flags: int
    water_temp: float  # °C
    ambient_temp: float  # °C
    ph: float
    orp: int  # mV
    flow: int  # L/min
    pressure: int  # kPa
    pump_speed: int  # %
    pump_power: int  # W
    drive_temp: int  # °C
    main_pump: int  # 0 off / 1-20 schedule / 99 manual
    san: int
    flags2: int
    aux_mode: tuple[int, ...]  # per slot: 0 off / 1-20 schedule / 99 manual
    aux_trump: frozenset[int]  # slots under manual override
    pump_cooldown_remain: int  # seconds
    pump_prime_remain: int  # seconds

    def flag(self, bit: int) -> bool:
        """Return bit `bit` of `flags`."""
        return bool(self.flags & (1 << bit))

    def flag2(self, bit: int) -> bool:
        """Return bit `bit` of `flags2`."""
        return bool(self.flags2 & (1 << bit))

    @property
    def dry_run(self) -> int:
        """Dry-run state, flags bits 13-14 (firmware status & 0x3; 3 = normal)."""
        return (self.flags >> 13) & 0x3

    def expander_connected(self, board: int) -> bool:
        """Return whether LoRa expander board 1-4 is online (flags 10/11, flags2 7/8)."""
        return {
            1: self.flag(FLAG_EXPANDER_1_CONN),
            2: self.flag(FLAG_EXPANDER_2_CONN),
            3: self.flag2(FLAG2_EXPANDER_3_CONN),
            4: self.flag2(FLAG2_EXPANDER_4_CONN),
        }[board]


# `flags` bit numbers (docs §3.6)
FLAG_HEATER = 0
FLAG_SOLAR = 1
FLAG_SPA_HEATER = 2
FLAG_PH_SWITCH = 3
FLAG_ORP_SWITCH = 4
FLAG_CHEM_STABLE = 5
FLAG_EXPANDER_1_CONN = 10
FLAG_EXPANDER_2_CONN = 11
FLAG_FLOW_OK = 15
# `flags2` bit numbers
FLAG2_PUMP_COOLING = 0
FLAG2_PUMP_PRIMING = 1
FLAG2_WIFI_UP = 2
FLAG2_ETH_UP = 3
FLAG2_MQTT_UP = 4
FLAG2_INTERNET_OK = 5
FLAG2_HAS_SPA = 6
FLAG2_EXPANDER_3_CONN = 7
FLAG2_EXPANDER_4_CONN = 8

# Slot map for the 4-expander firmware (docs §3.3, N=37): 0-11 local outputs,
# then five slots per LoRa expander board. 22-25 are reserved and never
# assigned; 26 is the RS485 mineral chlorinator ozone virtual slot.
AUX_COUNT_4_EXPANDERS = 37
EXPANDER_SLOTS: dict[int, range] = {
    1: range(12, 17),
    2: range(17, 22),
    3: range(27, 32),
    4: range(32, 37),
}
ECOCLEAR_VIRTUAL_SLOT = 26


def _decode_section_a(r: _Reader) -> Telemetry:
    flags = _u(r.u16())
    water_temp = _u(r.u16()) / 10
    ambient_temp = _u(r.u16()) / 100
    ph = _u(r.u8()) / 10
    orp = _u(r.u16())
    r.u16()  # tds: placeholder, ignored
    flow = _u(r.u16())
    pressure = _u(r.u16())
    r.u16()  # prime_pressure
    pump_speed = _u(r.u8())
    pump_power = _u(r.u16())
    r.u16()  # pump_current
    drive_temp = _u(r.u8())
    main_pump = _u(r.u8())
    san = _u(r.u16())
    for _ in range(4):  # cost[4]
        r.i32()

    flags2 = 0
    aux_mode: list[int] = []
    trump: set[int] = set()
    cooldown = prime = 0
    f2 = r.u16()
    if f2 is not None:
        flags2 = f2
        n = r.u8()
        if n is not None:
            for _ in range(n):
                m = r.u8()
                if m is None:
                    break
                aux_mode.append(m)
            for b in range((n + 7) // 8):
                byte = r.u8()
                if byte is None:
                    break
                for bit in range(8):
                    slot = b * 8 + bit
                    if slot < n and byte & (1 << bit):
                        trump.add(slot)
            cooldown = _u(r.u16())
            prime = _u(r.u16())

    return Telemetry(
        flags=flags,
        water_temp=water_temp,
        ambient_temp=ambient_temp,
        ph=ph,
        orp=orp,
        flow=flow,
        pressure=pressure,
        pump_speed=pump_speed,
        pump_power=pump_power,
        drive_temp=drive_temp,
        main_pump=main_pump,
        san=san,
        flags2=flags2,
        aux_mode=tuple(aux_mode),
        aux_trump=frozenset(trump),
        pump_cooldown_remain=cooldown,
        pump_prime_remain=prime,
    )


@dataclass(frozen=True, slots=True)
class Config:
    """Section B numeric config (docs §3.4); raw wire values."""

    desired_water_temp: int
    spa_desired_temp: int
    solar_heater_selected: bool  # cfg_flags bit 0
    pool_heater_selected: bool  # cfg_flags bit 1
    spa_heater_selected: bool  # cfg_flags bit 2
    heater_cooldown: int
    desired_ph_level: int  # ×10
    acid_drum_volume: int
    remain_acid_volume: int
    desired_orp_mineral: int
    desired_orp_liquid: int
    chlorine_drum_volume: int
    remain_chlorine_volume: int
    sched_enabled: int  # bit i = schedule i enabled
    fw_version: str
    aux_type: tuple[int, ...]  # AuxTypeCode per slot


def _decode_section_b(r: _Reader) -> Config:
    r.u8()  # pool_size
    r.u16()  # electricity_cost
    r.u8()  # max_run_current
    r.u16()  # pump_prime_time_100
    for _ in range(6):  # pump_start_speed .. cleaner_pump_speed
        r.u8()
    r.u16()  # backwash_pressure
    r.u8()  # flow_retry_time
    r.u8()  # flow_retry_cycles
    r.u16()  # min_flow
    r.i16()  # min_pressure
    desired_water_temp = _u(r.u8())
    spa_desired_temp = _u(r.u8())
    cfg_flags = _u(r.u8())
    heater_cooldown = _u(r.u16())
    r.u16()  # spa_heater_cooldown
    r.u8()  # solar_hysteresis_temp
    r.u16()  # solar_hysteresis_time
    r.u16()  # ambient_hysteresis_time
    r.u16()  # solar_collector_flush_time

    r.u8()  # ph_dosing
    r.u16()  # ph_dosing_time
    desired_ph_level = _u(r.u8())
    acid_drum_volume = _u(r.u32())
    remain_acid_volume = _u(r.u32())
    r.u8()  # ph_max_dose_per_day
    r.u16()  # ph_dose_intervals
    r.u16()  # ph_dose_rate

    r.u8()  # orp_flags
    desired_orp_mineral = _u(r.u16())
    r.u32()  # mineral_max_duration_preday
    desired_orp_liquid = _u(r.u16())
    r.u16()  # liquid_dose_time
    chlorine_drum_volume = _u(r.u32())
    remain_chlorine_volume = _u(r.u32())
    r.u16()  # liquid_dose_rate

    r.u16()  # chlsave_sp
    r.u16()  # ozonesave_sp
    r.u8()  # pool_light_mode
    r.u8()  # spa_light_mode
    r.i16()  # water_temp_offset
    r.i16()  # ambient_temp_offset
    sched_enabled = _u(r.u32())
    fw = f"{_u(r.u8())}.{_u(r.u8())}.{_u(r.u8())}"
    aux_type: list[int] = []
    for _ in range(_u(r.u8())):
        t = r.u8()
        if t is None:
            break
        aux_type.append(t)

    return Config(
        desired_water_temp=desired_water_temp,
        spa_desired_temp=spa_desired_temp,
        solar_heater_selected=bool(cfg_flags & 0x01),
        pool_heater_selected=bool(cfg_flags & 0x02),
        spa_heater_selected=bool(cfg_flags & 0x04),
        heater_cooldown=heater_cooldown,
        desired_ph_level=desired_ph_level,
        acid_drum_volume=acid_drum_volume,
        remain_acid_volume=remain_acid_volume,
        desired_orp_mineral=desired_orp_mineral,
        desired_orp_liquid=desired_orp_liquid,
        chlorine_drum_volume=chlorine_drum_volume,
        remain_chlorine_volume=remain_chlorine_volume,
        sched_enabled=sched_enabled,
        fw_version=fw,
        aux_type=tuple(aux_type),
    )


@dataclass(frozen=True, slots=True)
class AuxTimer:
    """One running aux countdown from the frame tail."""

    slot: int
    remain_sec: int
    duration_sec: int


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded state frame; sections absent from the mask are None."""

    ver: int
    section_mask: int
    telemetry: Telemetry | None
    config: Config | None
    timers: tuple[AuxTimer, ...] = ()


def decode_frame(payload: bytes) -> Frame:
    """Parse a raw (unwrapped) state frame per docs §3.2."""
    if len(payload) < 2:
        raise PV2Error(f"frame too short ({len(payload)} bytes)")
    ver, mask = payload[0], payload[1]
    off = 2
    telemetry = config = None
    timers: list[AuxTimer] = []

    if mask & SECTION_A:
        if off >= len(payload):
            raise PV2Error("truncated before section A length")
        length = payload[off]
        off += 1
        if off + length > len(payload):
            raise PV2Error("section A overruns frame")
        telemetry = _decode_section_a(_Reader(payload, off, off + length))
        off += length
    if mask & SECTION_B:
        if off + 2 > len(payload):
            raise PV2Error("truncated before section B length")
        length = struct.unpack_from("<H", payload, off)[0]
        off += 2
        if off + length > len(payload):
            raise PV2Error("section B overruns frame")
        config = _decode_section_b(_Reader(payload, off, off + length))
        off += length
    if mask & SECTION_TAIL:
        if off >= len(payload):
            raise PV2Error("truncated before tail length")
        length = payload[off]
        off += 1
        if off + length > len(payload):
            raise PV2Error("tail overruns frame")
        r = _Reader(payload, off, off + length)
        for _ in range(_u(r.u8())):
            slot, remain, duration = r.u8(), r.u16(), r.u16()
            if slot is None or remain is None or duration is None:
                break
            timers.append(AuxTimer(slot, remain, duration))

    return Frame(ver=ver, section_mask=mask, telemetry=telemetry, config=config, timers=tuple(timers))


# ---------------------------------------------------------------------------
# Actions (docs §4)
# ---------------------------------------------------------------------------

SCHED_CREATE = 0
SCHED_UPDATE = 1
SCHED_DELETE = 2
SCHED_ENABLE = 3


def encode_aux(slot: int, on: bool, trump: bool = True) -> bytes:
    """`aux` action: {slot u8, on u8, trump u8}."""
    return bytes((slot, int(on), int(trump)))


def encode_cmd(name: str, body: bytes = b"") -> bytes:
    """`cmd` passthrough: {name_len u8, name utf8, json bytes}."""
    raw = name.encode()
    if not raw or len(raw) > 255:
        raise PV2Error("cmd: invalid command name")
    return bytes((len(raw),)) + raw + body


@dataclass(frozen=True, slots=True)
class ScheduleFields:
    """Schedule definition shared by CREATE and UPDATE."""

    slot: int
    aux_type: int
    turnover: int
    speed: int
    start_min: int
    end_min: int
    week_day: int  # 7-bit day mask
    name: str = ""

    def encode(self) -> bytes:
        """Encode the CREATE/UPDATE field block."""
        raw_name = self.name.encode()[:255]
        return (
            struct.pack(
                "<BBBBHHBB",
                self.slot,
                self.aux_type,
                self.turnover,
                self.speed,
                self.start_min,
                self.end_min,
                self.week_day,
                len(raw_name),
            )
            + raw_name
        )


def encode_schedule_create(fields: ScheduleFields) -> bytes:
    """`schedule` CREATE."""
    return bytes((SCHED_CREATE,)) + fields.encode()


def encode_schedule_update(sched_num: int, fields: ScheduleFields) -> bytes:
    """`schedule` UPDATE."""
    return bytes((SCHED_UPDATE, sched_num)) + fields.encode()


def encode_schedule_delete(sched_num: int) -> bytes:
    """`schedule` DELETE."""
    return bytes((SCHED_DELETE, sched_num))


def encode_schedule_enable(sched_num: int, enabled: bool) -> bytes:
    """`schedule` ENABLE."""
    return bytes((SCHED_ENABLE, sched_num, int(enabled)))


# ---------------------------------------------------------------------------
# Native query responses (docs §5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Schedule:
    """One record of the `schedules` query."""

    index: int
    enabled: bool
    aux_slot: int
    aux_type: int
    turnover: int
    speed: int
    start_min: int
    end_min: int
    week_day: int
    name: str

    @property
    def fields(self) -> ScheduleFields:
        """Return the record as UPDATE fields."""
        return ScheduleFields(
            slot=self.aux_slot,
            aux_type=self.aux_type,
            turnover=self.turnover,
            speed=self.speed,
            start_min=self.start_min,
            end_min=self.end_min,
            week_day=self.week_day,
            name=self.name,
        )


@dataclass(frozen=True, slots=True)
class PumpID:
    """The `pump_id` query response."""

    type: str
    brand: str
    model: str


def decode_aux_names(payload: bytes) -> tuple[str, ...]:
    """`count u8` + count × [len u8][utf8]."""
    r = _Reader(payload)
    count = r.u8()
    if count is None:
        raise PV2Error("aux_names: empty payload")
    names: list[str] = []
    for i in range(count):
        s = r.string()
        if s is None:
            raise PV2Error(f"aux_names: truncated at slot {i}")
        names.append(s)
    return tuple(names)


def decode_pump_id(payload: bytes) -> PumpID:
    """Three length-delimited strings: type, brand, model."""
    r = _Reader(payload)
    parts = [r.string() for _ in range(3)]
    if any(p is None for p in parts):
        raise PV2Error("pump_id: truncated")
    return PumpID(*parts)  # type: ignore[arg-type]


def decode_schedules(payload: bytes) -> tuple[Schedule, ...]:
    """`count u8` + per schedule a 10-byte record then [len u8][name]."""
    r = _Reader(payload)
    count = r.u8()
    if count is None:
        raise PV2Error("schedules: empty payload")
    out: list[Schedule] = []
    for i in range(count):
        if r.off + 10 > r.limit:
            raise PV2Error(f"schedules: truncated record {i}")
        status, slot, aux_type, turnover, speed = (r.u8() for _ in range(5))
        start_min, end_min = r.u16(), r.u16()
        week_day = r.u8()
        name = r.string()
        if name is None:
            raise PV2Error(f"schedules: truncated name {i}")
        out.append(
            Schedule(
                index=i,
                enabled=bool(status),
                aux_slot=_u(slot),
                aux_type=_u(aux_type),
                turnover=_u(turnover),
                speed=_u(speed),
                start_min=_u(start_min),
                end_min=_u(end_min),
                week_day=_u(week_day),
                name=name,
            )
        )
    return tuple(out)
