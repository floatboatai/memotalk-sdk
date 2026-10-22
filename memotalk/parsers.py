"""Typed parsers for every response the device can emit.

The original skill only parsed 0x13 / 0x84 / the FFE2 pushes and left the rest
to ad-hoc `pkt[3:-2]` slicing. Here every documented frame has exactly one
parser, and each one validates length before touching bytes.
"""

from __future__ import annotations

import struct
from typing import Callable, Dict, Union

from .codec import frame_payload, require_crc, xor_ff_decode
from .constants import (
    Cmd,
    DeviceErrorCode,
    RecordResult,
    RecordStateCode,
    SettingType,
    SyncOp,
)
from .errors import ProtocolError
from .models import (
    AckResult,
    AudioLevel,
    BatteryInfo,
    DeviceIdentity,
    FirmwareInfo,
    FreeSpace,
    FtpConfig,
    RecordState,
    SettingValue,
    SyncAck,
    SyncDataPacket,
    WifiCredentials,
    _bcd_to_datetime,
)

__all__ = [
    "ParsedResponse",
    "parse_response",
    "parse_battery",
    "parse_firmware",
    "parse_device_identity",
    "parse_free_space",
    "parse_has_import_file",
    "parse_record_state",
    "parse_ack_result",
    "parse_simple_ack",
    "parse_setting",
    "parse_delete_result",
    "parse_text",
    "parse_wifi_credentials",
    "parse_ftp",
    "parse_audio_level",
    "parse_audio_stream",
    "parse_sync_packet",
    "parse_sync_ack",
    "parse_error",
    "PARSERS",
]

ParsedResponse = Union[
    BatteryInfo,
    FirmwareInfo,
    DeviceIdentity,
    FreeSpace,
    RecordState,
    AckResult,
    SettingValue,
    AudioLevel,
    SyncAck,
    SyncDataPacket,
    WifiCredentials,
    FtpConfig,
    bool,
    str,
    float,
    bytes,
]


def _need(frame: bytes, minimum: int, what: str) -> None:
    if len(frame) < minimum:
        raise ProtocolError(
            f"{what} frame too short: {len(frame)} bytes "
            f"(expected >= {minimum}) -> {frame.hex().upper()}"
        )


def _verify(frame: bytes) -> bytes:
    """Strict CRC gate used by the command parsers (pushes excluded)."""
    require_crc(frame)
    return frame


# --- reads ------------------------------------------------------------------
def parse_battery(frame: bytes) -> BatteryInfo:
    """`5A 04 01 [0..10] CRC` -> level and percent."""
    _verify(frame)
    _need(frame, 6, "0x01 battery")
    level = frame[3]
    if level > 10:
        level = 10
    return BatteryInfo(level=level, percent=level * 10)


def parse_firmware(frame: bytes) -> FirmwareInfo:
    """`5A 06 09 "206" CRC` -> FirmwareInfo(raw='206')."""
    _verify(frame)
    _need(frame, 8, "0x09 firmware")
    return FirmwareInfo(raw=frame_payload(frame).decode("ascii", errors="replace"))


def parse_device_identity(frame: bytes) -> DeviceIdentity:
    """`5A 15 14 [16B SN] [1B fmt] [1B asr] CRC`."""
    _verify(frame)
    _need(frame, 22, "0x14 device identity")
    return DeviceIdentity(
        serial=frame[3:19].decode("ascii", errors="replace").rstrip("\x00"),
        format_code=frame[19],
        realtime_asr_enabled=bool(frame[20]),
    )


def parse_free_space(frame: bytes) -> FreeSpace:
    """`5A 05 0F [status] [percent] CRC`."""
    _verify(frame)
    _need(frame, 7, "0x0F free space")
    return FreeSpace(success=frame[3] == 0x01, percent=frame[4])


def parse_has_import_file(frame: bytes) -> bool:
    """0x00 -> False, 0x01 -> True, 0xFF -> raises (Wi-Fi unsupported)."""
    _verify(frame)
    _need(frame, 6, "0x0D has import file")
    value = frame[3]
    if value == 0xFF:
        raise ProtocolError("device reports Wi-Fi fast transfer is not supported")
    return value == 0x01


def parse_record_state(frame: bytes) -> RecordState:
    """20-byte state answer, shared by the 0x02 read and the 0x80 control ack."""
    _verify(frame)
    _need(frame, 20, "0x02/0x80 record state")
    mac = frame[5:11].hex().upper()
    bcd = bytes(frame[11:18])
    bcd_time = bcd.hex().upper()
    raw_result = frame[4]
    try:
        result = RecordResult(raw_result)
    except ValueError:
        result = RecordResult.FAILED
    try:
        state = RecordStateCode(frame[3])
    except ValueError:
        state = RecordStateCode.STOPPED
    return RecordState(
        code=frame[2],
        state=state,
        result=result,
        mac=mac,
        bcd_time=bcd_time,
        filename=f"{mac}_{bcd_time}.mp3",
        recorded_at=_bcd_to_datetime(bcd),
        raw=bytes(frame),
    )


def parse_setting(frame: bytes, setting_type: SettingType) -> SettingValue:
    """0xB1 read answer. Two layouts: 2-byte LE value (0x00/0x01) or 1 byte (0x02)."""
    _verify(frame)
    st = SettingType(int(setting_type))
    _need(frame, 8, f"0xB1 setting 0x{int(st):02X}")
    if frame[3] != int(st):
        raise ProtocolError(
            f"0xB1 answered for type 0x{frame[3]:02X}, expected 0x{int(st):02X}"
        )
    success = frame[4] == 0x01
    if st is SettingType.USB_MASS_STORAGE:
        _need(frame, 8, "0xB1 usb mass storage")
        value = frame[5]
    else:
        _need(frame, 9, "0xB1 numeric setting")
        value = struct.unpack("<H", frame[5:7])[0]
    return SettingValue(type=st, success=success, value=value)


# --- acks -------------------------------------------------------------------
def parse_ack_result(frame: bytes) -> AckResult:
    """`[state] [result]` ack used by 0x87 and 0xA4."""
    _verify(frame)
    _need(frame, 7, "ack with state")
    return AckResult(code=frame[2], state=frame[3], success=frame[4] == 0x01, raw=bytes(frame))


def parse_simple_ack(frame: bytes) -> bool:
    """Single result byte ack: 0x91, 0x9A, 0xB0."""
    _verify(frame)
    _need(frame, 6, "simple ack")
    return frame[3] == 0x01


def parse_delete_result(frame: bytes) -> Union[bool, int]:
    """0x92: success -> True; failure -> the documented file error code."""
    _verify(frame)
    _need(frame, 6, "0x92 delete")
    if frame[3] == 0x01:
        return True
    _need(frame, 7, "0x92 delete failure")
    return frame[4]


# --- text / credentials -----------------------------------------------------
def parse_text(frame: bytes) -> str:
    """Plain UTF-8 payload: 0x03, 0x04, 0x05, 0x0B."""
    _verify(frame)
    return frame_payload(frame).decode("utf-8", errors="replace")


def parse_secret(frame: bytes) -> str:
    """XOR 0xFF payload: 0x06 and 0x0C."""
    _verify(frame)
    return xor_ff_decode(frame_payload(frame))


def parse_wifi_credentials(ssid_frame: bytes, password_frame: bytes) -> WifiCredentials:
    return WifiCredentials(ssid=parse_text(ssid_frame), password=parse_secret(password_frame))


def parse_ftp(
    host_frame: bytes, port_frame: bytes, user_frame: bytes, password_frame: bytes
) -> FtpConfig:
    host = parse_text(host_frame).strip()
    port_raw = parse_text(port_frame).strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 0
    return FtpConfig(
        host=host,
        port=port,
        user=parse_text(user_frame).strip(),
        password=parse_secret(password_frame),
    )


# --- pushes -----------------------------------------------------------------
def parse_audio_level(frame: bytes) -> AudioLevel:
    """`5A 07 A8 <float LE> CRC` - 9 bytes, no CRC gate (push stream)."""
    if len(frame) != 9 or frame[0] != 0x5A or frame[2] != Cmd.AUDIO_LEVEL:
        raise ProtocolError(f"not a 0xA8 level frame: {frame.hex().upper()}")
    return AudioLevel(db=struct.unpack("<f", frame[3:7])[0])


def parse_audio_stream(frame: bytes) -> bytes:
    """`5A 23 01 00 90 <288B> 00 00` - returns the 288-byte MP3 chunk."""
    if len(frame) != 295 or frame[0] != 0x5A or frame[4] != Cmd.AUDIO_STREAM:
        raise ProtocolError(f"not a 0x90 audio frame ({len(frame)} bytes)")
    return bytes(frame[5:293])


def parse_sync_packet(frame: bytes) -> SyncDataPacket:
    """328-byte 0x84 packet: FF FF FF + 3B BE seq + 320B data + 2B BE? CRC16 LE."""
    if len(frame) != 328 or frame[:3] != b"\xff\xff\xff":
        raise ProtocolError(f"not a 0x84 data packet ({len(frame)} bytes)")
    seq = (frame[3] << 16) | (frame[4] << 8) | frame[5]
    from .codec import crc16

    expected = struct.unpack("<H", frame[-2:])[0]
    return SyncDataPacket(
        seq=seq,
        payload=bytes(frame[6:-2]),
        crc_ok=crc16(frame[:-2]) == expected,
        raw=bytes(frame),
    )


def parse_sync_ack(frame: bytes) -> SyncAck:
    """0x84 control answer.

    Firmware 206 uses **two different tail layouts**, which the protocol spec
    does not distinguish (see docs/ERRATA.md E11). Both were captured on
    hardware and are asserted in tests:

    * file exists   -> ``5A len 84 01 <name> [err] [3B BE packets] [op] CRC``
      5 tail bytes, e.g. ``... 00 00 02 0D 00 7A F5`` (525 packets).

    * file missing  -> ``5A len 84 00 <name> [status] [err] [op] CRC``
      only 3 tail bytes, e.g. ``... 00 04 00 B6 00`` (err 0x04 = not found).

    Both layouts keep the operation byte at ``frame[-3]``; slicing the success
    layout unconditionally is what previously corrupted the filename and the
    packet count.
    """
    _verify(frame)
    _need(frame, 11, "0x84 sync ack")

    exists = frame[3] == 0x01
    operation = _safe_sync_op(frame[-3])

    if exists:
        filename = frame[4:-7].decode("ascii", errors="replace")
        error_code = frame[-7]
        total_packets = int.from_bytes(frame[-6:-3], "big")
    else:
        filename = frame[4:-5].decode("ascii", errors="replace")
        error_code = frame[-4]
        total_packets = 0

    return SyncAck(
        exists=exists,
        filename=filename,
        error_code=error_code,
        total_packets=total_packets,
        operation=operation,
        raw=bytes(frame),
    )


def _safe_sync_op(value: int):
    """An unknown operation byte is tolerated; the caller decides what to do."""
    try:
        return SyncOp(value)
    except ValueError:
        return None


def parse_error(frame: bytes) -> DeviceErrorCode:
    """0x00 error frame -> typed code (may carry the echoed command after it)."""
    _need(frame, 3, "0x00 error")
    try:
        return DeviceErrorCode(frame[3])
    except (IndexError, ValueError) as exc:
        raise ProtocolError(f"malformed error frame: {frame.hex().upper()}") from exc


# --- frame predicates (used for stream subscriptions) -----------------------
def is_file_list_packet(frame: bytes) -> bool:
    return len(frame) >= 4 and frame[0] == 0xFF


def is_sync_packet(frame: bytes) -> bool:
    return len(frame) == 328 and frame[:3] == b"\xff\xff\xff"


def is_sync_ack(frame: bytes) -> bool:
    return len(frame) >= 3 and frame[0] == 0x5A and frame[2] in (
        int(Cmd.FILE_SYNC),
        int(Cmd.ERROR),
    )


def is_audio_level_frame(frame: bytes) -> bool:
    return len(frame) == 9 and frame[0] == 0x5A and frame[2] == int(Cmd.AUDIO_LEVEL)


def is_audio_stream_frame(frame: bytes) -> bool:
    # 0x90 uses a 3-byte length, so the command byte sits at offset 4.
    return len(frame) == 295 and frame[0] == 0x5A and frame[4] == int(Cmd.AUDIO_STREAM)


def is_record_state_frame(frame: bytes) -> bool:
    return (
        len(frame) == 20
        and frame[0] == 0x5A
        and frame[2] in (int(Cmd.RECORD_STATE), int(Cmd.RECORD_CONTROL))
    )


def is_error_frame(frame: bytes) -> bool:
    return len(frame) >= 4 and frame[0] == 0x5A and frame[2] == int(Cmd.ERROR)


PARSERS: Dict[int, Callable[[bytes], ParsedResponse]] = {
    int(Cmd.BATTERY): parse_battery,
    int(Cmd.FIRMWARE): parse_firmware,
    int(Cmd.DEVICE_SN): parse_device_identity,
    int(Cmd.FREE_SPACE): parse_free_space,
    int(Cmd.HAS_IMPORT_FILE): parse_has_import_file,
    int(Cmd.RECORD_STATE): parse_record_state,
    int(Cmd.RECORD_CONTROL): parse_record_state,
    int(Cmd.WIFI_SSID): parse_text,
    int(Cmd.WIFI_PASSWORD): parse_secret,
    int(Cmd.FTP_HOST): parse_text,
    int(Cmd.FTP_PORT): parse_text,
    int(Cmd.FTP_USER): parse_text,
    int(Cmd.FTP_PASSWORD): parse_secret,
    int(Cmd.WIFI_AP): parse_ack_result,
    int(Cmd.REALTIME_ASR): parse_ack_result,
    int(Cmd.RTC_SYNC): parse_simple_ack,
    int(Cmd.CLEAR_ALL): parse_simple_ack,
    int(Cmd.SETTING_WRITE): parse_simple_ack,
    int(Cmd.DELETE_FILE): parse_delete_result,
    int(Cmd.AUDIO_LEVEL): parse_audio_level,
    int(Cmd.AUDIO_STREAM): parse_audio_stream,
}


def parse_response(frame: bytes) -> ParsedResponse:
    """Dispatch a frame to its parser by command code."""
    if len(frame) < 3:
        raise ProtocolError(f"frame too short: {frame.hex().upper()}")
    code = frame[2]
    parser = PARSERS.get(code)
    if parser is None:
        raise ProtocolError(f"no parser registered for code 0x{code:02X}")
    return parser(frame)
