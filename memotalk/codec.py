"""Frame codec: CRC16, 0x5A framing, BCD time, credential de-obfuscation.

This module is pure: no IO, no asyncio, no BLE. It is the only place that knows
how bytes are shaped on the wire.
"""

from __future__ import annotations

import struct
from typing import Optional

from .constants import (
    AUDIO_STREAM_TOTAL,
    Limits,
    SYNC_PACKET_HEADER,
    SYNC_PACKET_TOTAL,
)
from .errors import CrcError, ProtocolError

__all__ = [
    "CRC16_TABLE",
    "crc16",
    "crc16_le",
    "build_frame",
    "verify_crc",
    "frame_code",
    "frame_payload",
    "expected_frame_length",
    "to_bcd",
    "from_bcd",
    "bcd_bytes",
    "bcd_to_str",
    "xor_ff_decode",
    "xor_ff_encode",
]

# Nibble lookup table from the vendor SDK. Initial value 0xFFFF.
CRC16_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)


def crc16(data: bytes, init: int = 0xFFFF) -> int:
    """Standard nibble-table CRC16, computed over everything after the 0x5A header."""
    crc = init
    for b in data:
        crc = CRC16_TABLE[(b ^ crc) & 0x0F] ^ (crc >> 4)
        crc = CRC16_TABLE[((b >> 4) ^ crc) & 0x0F] ^ (crc >> 4)
    return crc & 0xFFFF


def crc16_le(data: bytes) -> bytes:
    """CRC16 packed little-endian, as it appears on the wire."""
    return struct.pack("<H", crc16(data))


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    """Build `5A <len> <cmd> <payload> <crc_lo> <crc_hi>`.

    `len` counts cmd + payload + crc, so total frame size is `2 + len`.
    """
    if len(payload) > Limits.FRAME_MAX_PAYLOAD:
        raise ValueError(
            f"payload too long ({len(payload)} bytes, max {Limits.FRAME_MAX_PAYLOAD})"
        )
    body = bytes([1 + len(payload) + 2, int(cmd)]) + payload
    return b"\x5a" + body + crc16_le(body)


def verify_crc(frame: bytes) -> bool:
    """True when the trailing CRC16 matches. False for malformed frames."""
    if len(frame) < 5 or frame[0] != 0x5A:
        return False
    return crc16(frame[1:-2]) == struct.unpack("<H", frame[-2:])[0]


def require_crc(frame: bytes) -> None:
    """Raise CrcError when the frame is malformed or corrupt."""
    if len(frame) < 5 or frame[0] != 0x5A:
        raise ProtocolError(f"not a 0x5A frame: {frame[:8].hex().upper()}...")
    if not verify_crc(frame):
        raise CrcError(f"CRC mismatch on frame {frame.hex().upper()}")


def frame_code(frame: bytes) -> int:
    """Command / response / push code (byte 2)."""
    if len(frame) < 3:
        raise ProtocolError(f"frame too short: {frame.hex().upper()}")
    return frame[2]


def frame_payload(frame: bytes) -> bytes:
    """Payload between the code byte and the CRC."""
    if len(frame) < 5:
        raise ProtocolError(f"frame too short: {frame.hex().upper()}")
    return bytes(frame[3:-2])


def expected_frame_length(buf: bytes) -> Optional[int]:
    """Total wire length of the frame starting at buf[0], or None if undecidable.

    Three distinct framings exist on this device, and a fragmented notify gives
    us no help in telling them apart. This function resolves all three:

    * ``FF FF FF ...``  -> fixed 328-byte 0x84 audio packet
    * ``5A 23 01 00 90``-> 0x90 realtime frame, 3-byte LE length (291) => 295 total
    * ``5A <len> ...``  -> standard control frame, 1-byte length => 2 + len
    """
    if not buf:
        return None

    if buf[0] == 0x5A:
        # 0x90 uses a 3-byte little-endian length; the byte pattern is unique.
        if len(buf) >= 4 and buf[1] == 0x23 and buf[2] == 0x01 and buf[3] == 0x00:
            return AUDIO_STREAM_TOTAL
        if len(buf) < 2:
            return None
        return 2 + buf[1]

    if buf[0] == 0xFF:
        if len(buf) < 3:
            return None
        if buf[:3] == SYNC_PACKET_HEADER:
            return SYNC_PACKET_TOTAL
        return None

    return None


# --- BCD time ---------------------------------------------------------------
def to_bcd(value: int) -> int:
    """Encode 0..99 into packed BCD."""
    if not 0 <= value <= 99:
        raise ValueError(f"BCD value out of range: {value}")
    return ((value // 10) << 4) | (value % 10)


def from_bcd(byte: int) -> int:
    """Decode one packed-BCD byte."""
    return (byte >> 4) * 10 + (byte & 0x0F)


def bcd_bytes(dt) -> bytes:
    """7-byte pure BCD stamp: [century, year, month, day, hour, minute, second]."""
    return bytes(
        [
            to_bcd(dt.year // 100),
            to_bcd(dt.year % 100),
            to_bcd(dt.month),
            to_bcd(dt.day),
            to_bcd(dt.hour),
            to_bcd(dt.minute),
            to_bcd(dt.second),
        ]
    )


def bcd_to_str(bcd: bytes) -> str:
    """Render a 7-byte BCD stamp as `20 26 10 20 10 30 00` (uppercase hex)."""
    return bcd.hex().upper()


# --- credential obfuscation -------------------------------------------------
def xor_ff_decode(payload: bytes) -> str:
    """Decode 0x06 / 0x0C: every byte XOR 0xFF, then UTF-8."""
    return bytes(b ^ 0xFF for b in payload).decode("utf-8", errors="replace")


def xor_ff_encode(plaintext: str) -> bytes:
    """Inverse of `xor_ff_decode` (used by the simulator and by tests)."""
    return bytes(b ^ 0xFF for b in plaintext.encode("utf-8"))
