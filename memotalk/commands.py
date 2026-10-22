"""Command builders - one function per hardware atom.

Every builder is pure: it returns the bytes to write and nothing else. Channel
routing is declared here as data (`WRITE_CHANNEL` / `RESPONSE_CHANNEL`) so the
transport and the client can never disagree about where a frame goes.
"""

from __future__ import annotations

import datetime as _dt
import struct
from typing import Optional

from .codec import bcd_bytes, build_frame
from .constants import Channel, Cmd, Limits, RecAction, SettingType, SyncOp

__all__ = [
    "WRITE_CHANNEL",
    "RESPONSE_CHANNEL",
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
    # aliases matching the original skill SDK names
    "cmd_read_battery",
    "cmd_read_record_state",
    "cmd_read_ftp_host",
    "cmd_read_ftp_port",
    "cmd_read_ftp_user",
    "cmd_read_ftp_password",
    "cmd_read_firmware_version",
    "cmd_read_wifi_ssid",
    "cmd_read_wifi_password",
    "cmd_read_has_import_file",
    "cmd_read_free_space",
    "cmd_query_file_list",
    "cmd_read_device_sn_and_format",
    "cmd_read_setting",
    "cmd_sync_rtc_time",
    "cmd_control_recording",
    "cmd_sync_audio_file",
    "cmd_set_wifi_ap",
    "cmd_delete_file",
    "cmd_clear_all_files",
    "cmd_set_realtime_asr",
    "cmd_write_setting",
]

# Which characteristic each command must be written to. 0x80 is the sole
# exception and it is the single most common integration bug.
WRITE_CHANNEL = {
    Cmd.BATTERY: Channel.CONTROL,
    Cmd.RECORD_STATE: Channel.CONTROL,
    Cmd.FTP_HOST: Channel.CONTROL,
    Cmd.FTP_PORT: Channel.CONTROL,
    Cmd.FTP_USER: Channel.CONTROL,
    Cmd.FTP_PASSWORD: Channel.CONTROL,
    Cmd.FIRMWARE: Channel.CONTROL,
    Cmd.WIFI_SSID: Channel.CONTROL,
    Cmd.WIFI_PASSWORD: Channel.CONTROL,
    Cmd.HAS_IMPORT_FILE: Channel.CONTROL,
    Cmd.FREE_SPACE: Channel.CONTROL,
    Cmd.FILE_LIST: Channel.CONTROL,
    Cmd.DEVICE_SN: Channel.CONTROL,
    Cmd.SETTING_READ: Channel.CONTROL,
    Cmd.RTC_SYNC: Channel.CONTROL,
    Cmd.FILE_SYNC: Channel.CONTROL,
    Cmd.WIFI_AP: Channel.CONTROL,
    Cmd.DELETE_FILE: Channel.CONTROL,
    Cmd.CLEAR_ALL: Channel.CONTROL,
    Cmd.REALTIME_ASR: Channel.CONTROL,
    Cmd.SETTING_WRITE: Channel.CONTROL,
    Cmd.RECORD_CONTROL: Channel.RECORD,
}

# Where the answer arrives. 0x02 and 0x80 answer on FFE2 even though 0x02 is
# written to FFF1 - the one asymmetric route in the protocol.
RESPONSE_CHANNEL = {
    Cmd.RECORD_STATE: Channel.RECORD,
    Cmd.RECORD_CONTROL: Channel.RECORD,
}


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


# --- backward compatible aliases (same names as the original skill) ---------
cmd_read_battery = read_battery
cmd_read_record_state = read_record_state
cmd_read_ftp_host = read_ftp_host
cmd_read_ftp_port = read_ftp_port
cmd_read_ftp_user = read_ftp_user
cmd_read_ftp_password = read_ftp_password
cmd_read_firmware_version = read_firmware_version
cmd_read_wifi_ssid = read_wifi_ssid
cmd_read_wifi_password = read_wifi_password
cmd_read_has_import_file = read_has_import_file
cmd_read_free_space = read_free_space
cmd_query_file_list = query_file_list
cmd_read_device_sn_and_format = read_device_sn
cmd_read_setting = read_setting
cmd_sync_rtc_time = sync_rtc
cmd_control_recording = control_recording
cmd_sync_audio_file = sync_audio_file
cmd_set_wifi_ap = set_wifi_ap
cmd_delete_file = delete_file
cmd_clear_all_files = clear_all_files
cmd_set_realtime_asr = set_realtime_asr
cmd_write_setting = write_setting
