"""Typed value objects returned by the parsers.

These are frozen dataclasses: a value, once parsed, describes one instant of
device state and is never mutated in place.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .constants import RecordResult, RecordStateCode, SettingType, SyncOp

__all__ = [
    "BatteryInfo", "FirmwareInfo", "DeviceIdentity", "FreeSpace",
    "RecordState", "WifiCredentials", "FtpConfig", "AudioFileEntry",
    "SettingValue", "DeviceSettings", "SyncAck", "AudioLevel",
    "SyncDataPacket", "DeviceInfo", "AckResult",
]


def _bcd_to_datetime(bcd: bytes) -> Optional[_dt.datetime]:
    """Decode the 7-byte pure-BCD stamp [century, yy, mm, dd, hh, mm, ss]."""
    if len(bcd) != 7:
        return None

    def dec(b: int) -> int:
        return (b >> 4) * 10 + (b & 0x0F)

    try:
        year = dec(bcd[0]) * 100 + dec(bcd[1])
        return _dt.datetime(
            year, dec(bcd[2]), dec(bcd[3]), dec(bcd[4]), dec(bcd[5]), dec(bcd[6])
        )
    except ValueError:
        return None


@dataclass(frozen=True)
class AckResult:
    """Generic `[state] [result]` acknowledgement (0x87 / 0xA4)."""

    code: int
    state: int
    success: bool
    raw: bytes = b""

    def __bool__(self) -> bool:
        return self.success


@dataclass(frozen=True)
class BatteryInfo:
    """0x01 - level is 0..10, percent is the derived 0..100."""

    level: int
    percent: int


@dataclass(frozen=True)
class FirmwareInfo:
    """0x09 - three ASCII digits, '206' meaning V2.0.6."""

    raw: str

    @property
    def major(self) -> int:
        return int(self.raw[0]) if len(self.raw) > 0 and self.raw[0].isdigit() else 0

    @property
    def minor(self) -> int:
        return int(self.raw[1]) if len(self.raw) > 1 and self.raw[1].isdigit() else 0

    @property
    def patch(self) -> int:
        return int(self.raw[2]) if len(self.raw) > 2 and self.raw[2].isdigit() else 0

    @property
    def version(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class DeviceIdentity:
    """0x14 - serial number, storage format and realtime-ASR switch state."""

    serial: str
    format_code: int
    realtime_asr_enabled: bool

    @property
    def format_name(self) -> str:
        return "MP3" if self.format_code == 0x00 else f"FMT({self.format_code})"


@dataclass(frozen=True)
class FreeSpace:
    """0x0F - status byte plus remaining percentage."""

    success: bool
    percent: int


@dataclass(frozen=True)
class RecordState:
    """0x02 read answer or 0x80 control ack (identical 20-byte layout)."""

    code: int
    state: RecordStateCode
    result: RecordResult
    mac: str
    bcd_time: str
    filename: str
    recorded_at: Optional[_dt.datetime] = None
    raw: bytes = b""

    @property
    def success(self) -> bool:
        return self.result == RecordResult.SUCCESS

    @property
    def is_recording(self) -> bool:
        return self.state in (RecordStateCode.RECORDING, RecordStateCode.RESUMED)


@dataclass(frozen=True)
class WifiCredentials:
    """0x0B + 0x0C."""

    ssid: str
    password: str


@dataclass(frozen=True)
class FtpConfig:
    """0x03 + 0x04 + 0x05 + 0x06."""

    host: str
    port: int
    user: str
    password: str

    @property
    def ready(self) -> bool:
        """A hotspot that is not up yet reports 0.0.0.0 and an unusable payload."""
        return bool(self.host) and self.host != "0.0.0.0" and self.port > 0

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class AudioFileEntry:
    """One entry from the 0x13 file list."""

    name: str
    format_code: int
    timestamp: int
    size: int
    duration_sec: int
    crc: int = 0xFFFF

    @property
    def format_name(self) -> str:
        return "MP3" if self.format_code == 0x00 else f"FMT({self.format_code})"

    @property
    def recorded_at(self) -> Optional[_dt.datetime]:
        try:
            return _dt.datetime.fromtimestamp(self.timestamp)
        except (OSError, OverflowError, ValueError):
            return None


@dataclass(frozen=True)
class SettingValue:
    """0xB1 read answer for one setting type."""

    type: SettingType
    success: bool
    value: int


@dataclass
class DeviceSettings:
    """The three configurable knobs, assembled from three 0xB1 reads."""

    sleep_timeout_sec: Optional[int] = None
    max_record_minutes: Optional[int] = None
    usb_mass_storage: Optional[bool] = None
    raw: Dict[int, SettingValue] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncAck:
    """0x84 control answer: does the file exist, and how many packets to expect."""

    exists: bool
    filename: str
    error_code: int
    total_packets: int
    operation: Optional[SyncOp]
    raw: bytes = b""


@dataclass(frozen=True)
class AudioLevel:
    """0xA8 - instantaneous microphone level in dBFS (-120.0 .. 0.0)."""

    db: float

    @property
    def normalized(self) -> float:
        """Map -60..0 dB onto 0.0..1.0 for waveform bars (clamped)."""
        if self.db <= -60.0:
            return 0.0
        if self.db >= 0.0:
            return 1.0
        return (self.db + 60.0) / 60.0


@dataclass(frozen=True)
class SyncDataPacket:
    """One 328-byte 0x84 payload packet."""

    seq: int
    payload: bytes
    crc_ok: bool
    raw: bytes = b""


@dataclass
class DeviceInfo:
    """Composite snapshot produced by `client.info()`."""

    battery: BatteryInfo
    firmware: FirmwareInfo
    identity: DeviceIdentity
    free_space: FreeSpace
    has_import_file: Optional[bool]
    record: RecordState
    settings: DeviceSettings
    files: Optional[List[AudioFileEntry]] = None
