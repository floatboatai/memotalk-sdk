"""Command builders - one function per hardware atom.

Every builder is pure: it returns the bytes to write and nothing else. It does
not know which channel the frame has to go to - routing lives one layer up, in
``client.py``, which passes ``write=``/``listen=`` explicitly at every call
site. 0x80 is written to FFE1 and answered on FFE2; 0x02 is written to FFF1
and answered on FFE2; everything else goes to FFF1 and is answered on FFF2.
"""

from __future__ import annotations

import datetime as _dt
import struct
from typing import Optional

from .codec import bcd_bytes, build_frame
from .constants import Cmd, Limits, RecAction, SettingType, SyncOp

__all__ = [
    "read_battery",
    "read_record_state",
    "read_ftp_host",
    "read_ftp_port",
    "read_ftp_user",
    "read_ftp_password",
    "read_firmware_version",
    "read_wifi_ssid",
    "read_wifi_password",
    "read_has_import_file",
    "read_free_space",
    "query_file_list",
    "read_device_sn",
    "read_setting",
    "sync_rtc",
    "control_recording",
    "sync_audio_file",
    "set_wifi_ap",
    "delete_file",
    "clear_all_files",
    "set_realtime_asr",
    "write_setting",
]

# --- reads ------------------------------------------------------------------
def read_battery() -> bytes:
    """0x01 - battery level; doubles as the keep-alive heartbeat."""
    return build_frame(Cmd.BATTERY)


def read_record_state() -> bytes:
    """0x02 - recording state, real MAC and current file id. Answers on FFE2."""
    return build_frame(Cmd.RECORD_STATE)


def read_ftp_host() -> bytes:
    """0x03 - dynamic FTP IP. Only valid after the hotspot is up."""
    return build_frame(Cmd.FTP_HOST)


def read_ftp_port() -> bytes:
    """0x04 - dynamic FTP port, ASCII digits."""
    return build_frame(Cmd.FTP_PORT)


def read_ftp_user() -> bytes:
    """0x05 - FTP username."""
    return build_frame(Cmd.FTP_USER)


def read_ftp_password() -> bytes:
    """0x06 - FTP password, XOR 0xFF obfuscated."""
    return build_frame(Cmd.FTP_PASSWORD)


def read_firmware_version() -> bytes:
    """0x09 - three ASCII digits."""
    return build_frame(Cmd.FIRMWARE)


def read_wifi_ssid() -> bytes:
    """0x0B - hotspot SSID."""
    return build_frame(Cmd.WIFI_SSID)


def read_wifi_password() -> bytes:
    """0x0C - hotspot WPA2 password, XOR 0xFF obfuscated."""
    return build_frame(Cmd.WIFI_PASSWORD)


def read_has_import_file() -> bytes:
    """0x0D - whether an imported file exists; 0xFF means Wi-Fi unsupported."""
    return build_frame(Cmd.HAS_IMPORT_FILE)


def read_free_space() -> bytes:
    """0x0F - remaining storage percentage."""
    return build_frame(Cmd.FREE_SPACE)


def query_file_list() -> bytes:
    """0x13 - local recording list, answered as a 0xFF multi-packet stream."""
    return build_frame(Cmd.FILE_LIST)


def read_device_sn() -> bytes:
    """0x14 - 16-byte serial, storage format and realtime-ASR state."""
    return build_frame(Cmd.DEVICE_SN)


def read_setting(setting_type: SettingType) -> bytes:
    """0xB1 - read one configurable item."""
    return build_frame(Cmd.SETTING_READ, bytes([int(setting_type)]))


# --- handshake and control --------------------------------------------------
def sync_rtc(dt: Optional[_dt.datetime] = None) -> bytes:
    """0x91 - 7-byte pure BCD time sync. Must be sent within 1s of connecting."""
    return build_frame(Cmd.RTC_SYNC, bcd_bytes(dt or _dt.datetime.now()))


def control_recording(action: RecAction) -> bytes:
    """0x80 - start / pause / resume / stop. Written to FFE1, answered on FFE2."""
    act = RecAction(int(action))  # validates the action code
    return build_frame(Cmd.RECORD_CONTROL, bytes([int(act)]))


def sync_audio_file(filename: str, start_packet: int = 0, operation: SyncOp = SyncOp.START) -> bytes:
    """0x84 - start / cancel / pause / resume a BLE file transfer from a packet index."""
    name = filename.encode("ascii")
    if not name:
        raise ValueError("filename must not be empty")
    if len(name) + 4 > Limits.FRAME_MAX_PAYLOAD:
        raise ValueError(f"filename too long ({len(name)} bytes)")
    if not 0 <= start_packet <= 0xFFFFFF:
        raise ValueError("start_packet must fit in 3 bytes (0..0xFFFFFF)")
    op = SyncOp(int(operation))  # validates the operation code
    payload = name + start_packet.to_bytes(3, "big") + bytes([int(op)])
    return build_frame(Cmd.FILE_SYNC, payload)


def set_wifi_ap(enable: bool) -> bytes:
    """0x87 - hotspot on/off. Always switch it off when the transfer ends."""
    return build_frame(Cmd.WIFI_AP, b"\x01" if enable else b"\x00")


def delete_file(filename: str) -> bytes:
    """0x92 - delete one recording by exact, case-sensitive ASCII name."""
    name = filename.encode("ascii")
    if not 1 <= len(name) <= Limits.DELETE_FILENAME_MAX:
        raise ValueError(
            f"filename must be 1..{Limits.DELETE_FILENAME_MAX} bytes, got {len(name)}"
        )
    return build_frame(Cmd.DELETE_FILE, name)


def clear_all_files(confirm: bool = False) -> bytes:
    """0x9A - irreversible wipe. Refuses unless confirm=True."""
    if not confirm:
        from .errors import ConfirmationRequired

        raise ConfirmationRequired(
            "clearing every recording is irreversible; pass confirm=True explicitly"
        )
    return build_frame(Cmd.CLEAR_ALL)


def set_realtime_asr(enable: bool) -> bytes:
    """0xA4 - choose the FFE2 payload: 0x90 MP3 stream on, 0xA8 level meter off."""
    return build_frame(Cmd.REALTIME_ASR, b"\x01" if enable else b"\x00")


def write_setting(setting_type: SettingType, value: int) -> bytes:
    """0xB0 - write one configurable item, with range validation."""
    st = SettingType(int(setting_type))
    if st is SettingType.SLEEP_TIMEOUT_SEC:
        if value != 0 and not (
            Limits.SLEEP_TIMEOUT_MIN <= value <= Limits.SLEEP_TIMEOUT_MAX
        ):
            raise ValueError(
                f"sleep timeout must be 0 or {Limits.SLEEP_TIMEOUT_MIN}..{Limits.SLEEP_TIMEOUT_MAX}s"
            )
        return build_frame(Cmd.SETTING_WRITE, b"\x00" + struct.pack("<H", value))
    if st is SettingType.MAX_RECORD_MINUTES:
        if not (Limits.MAX_RECORD_MINUTES_MIN <= value <= Limits.MAX_RECORD_MINUTES_MAX):
            raise ValueError(
                f"max record minutes must be {Limits.MAX_RECORD_MINUTES_MIN}.."
                f"{Limits.MAX_RECORD_MINUTES_MAX}"
            )
        return build_frame(Cmd.SETTING_WRITE, b"\x01" + struct.pack("<H", value))
    if st is SettingType.USB_MASS_STORAGE:
        if value not in (0, 1):
            raise ValueError("usb mass storage must be 0 (off) or 1 (on)")
        return build_frame(Cmd.SETTING_WRITE, bytes([0x02, value]))
    raise ValueError(f"unsupported setting type: 0x{int(st):02X}")
