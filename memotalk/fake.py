"""In-memory device simulator and transport.

Every answer is built with the same `build_frame` the client uses, then handed
to the *real* transport dispatch path. This makes the library testable without
hardware, and lets an application build its UI before the recorder arrives.

It reproduces the frames captured from firmware 206 (see the protocol spec),
including the irregular cases: the 0x02/0x80 answers on FFE2, the 0xFF
multi-packet file list, and the 328-byte 0x84 packet stream.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import struct
from typing import Dict, List, Optional, Sequence, Tuple

from .codec import bcd_bytes, build_frame, crc16_le, xor_ff_encode
from .constants import (
    Channel,
    Cmd,
    RecordResult,
    RecordStateCode,
    SettingType,
    SyncOp,
)
from .errors import ConnectionLost
from .models import AudioFileEntry
from .transport import BleTransport

__all__ = ["FakeMemoTalk", "FakeTransport", "make_file_entry"]

MAC_BYTES = bytes.fromhex("FE1DDF64551E")
# Captured from firmware 206. The trailing two bytes are NOT a CRC16:
# same content, doc says 30 0D, the device sends 64 68, and the real
# CRC16 would be D5 09 (see docs/ERRATA.md E11).
EMPTY_FILE_LIST = bytes.fromhex("FF00051300006468")


def make_file_entry(
    name: str,
    size: int = 4096,
    duration_sec: int = 10,
    timestamp: Optional[int] = None,
    format_code: int = 0x00,
) -> AudioFileEntry:
    return AudioFileEntry(
        name=name,
        format_code=format_code,
        timestamp=timestamp if timestamp is not None else int(_dt.datetime(2026, 10, 20, 10, 30).timestamp()),
        size=size,
        duration_sec=duration_sec,
        crc=0xFFFF,
    )


def _encode_entry(entry: AudioFileEntry) -> bytes:
    name = entry.name.encode("ascii")
    return (
        bytes([len(name)])
        + name
        + bytes([entry.format_code, 0x00])
        + struct.pack(">I", entry.timestamp)
        + struct.pack(">I", entry.size)
        + struct.pack(">I", entry.duration_sec)
        + struct.pack(">H", 0xFFFF)
    )


def build_file_list_frames(
    entries: Sequence[AudioFileEntry], chunk: int = 120
) -> List[bytes]:
    """Build the 0xFF multi-packet 0x13 answer, or the real 8-byte empty frame."""
    if not entries:
        return [EMPTY_FILE_LIST]

    body = b"".join(_encode_entry(e) for e in entries)
    total = 8 + len(body)
    count = len(entries)

    if total <= chunk:
        frame = b"\xff" + total.to_bytes(2, "big") + bytes([0x13]) + count.to_bytes(2, "big") + body
        return [frame + crc16_le(frame)]

    frames: List[bytes] = []
    first_cap = chunk - 6
    cont_cap = chunk - 4
    head, rest = body[:first_cap], body[first_cap:]
    frames.append(
        b"\xff" + total.to_bytes(2, "big") + bytes([0x13]) + count.to_bytes(2, "big") + head
    )
    while len(rest) > cont_cap:
        frames.append(b"\xff" + total.to_bytes(2, "big") + bytes([0x1D]) + rest[:cont_cap])
        rest = rest[cont_cap:]
    tail = b"\xff" + total.to_bytes(2, "big") + bytes([0x2D]) + rest
    frames.append(tail + crc16_le(tail))
    return frames


def build_sync_ack(
    filename: str, exists: bool, total_packets: int, operation: SyncOp, error_code: int = 0
) -> bytes:
    """Byte-compatible with firmware 206.

    Success and failure acks use different tails (see docs/ERRATA.md E11):
    success carries err + 3B packet count + op, failure carries status + err + op.
    """
    name = filename.encode("ascii")
    if exists:
        payload = (
            bytes([0x01])
            + name
            + bytes([error_code])
            + int(total_packets).to_bytes(3, "big")
            + bytes([int(operation)])
        )
    else:
        payload = bytes([0x00]) + name + bytes([0x00, error_code or 0x04, int(operation)])
    return build_frame(Cmd.FILE_SYNC, payload)


def build_sync_packet(seq: int, chunk: bytes) -> bytes:
    if len(chunk) > 320:
        raise ValueError("chunk exceeds 320 bytes")
    chunk = chunk.ljust(320, b"\x00")
    body = b"\xff\xff\xff" + int(seq).to_bytes(3, "big") + chunk
    return body + crc16_le(body)


class FakeMemoTalk:
    """A deterministic stand-in for the recorder."""

    name = "MemoTalk S1 (simulated)"
    address = "SIM:00:00:00:00:01"

    def __init__(
        self,
        *,
        serial: str = "L0m0J06000074305",
        firmware: str = "206",
        battery: int = 5,
        files: Optional[Sequence[AudioFileEntry]] = None,
        ssid: str = "MemoTalk-S1-TEST",
        wifi_password: str = "TESTwifi",
        ftp_host: str = "192.168.4.1",
        ftp_port: int = 2121,
        ftp_user: str = "fulltech",
        ftp_password: str = "ESqn3Dvc",
        sleep_timeout: int = 300,
        max_record_minutes: int = 300,
        usb_mass_storage: int = 0,
        free_space_percent: int = 99,
        has_import_file: bool = False,
        firmware_unsupported_error: Optional[int] = None,
    ) -> None:
        self.serial = serial
        self.firmware = firmware
        self.battery = battery
        self.files: List[AudioFileEntry] = list(files or [])
        self.ssid = ssid
        self.wifi_password = wifi_password
        self.ftp_host = ftp_host
        self.ftp_port = ftp_port
        self.ftp_user = ftp_user
        self.ftp_password = ftp_password
        self.free_space_percent = free_space_percent
        self.has_import_file = has_import_file
        self.settings: Dict[int, int] = {
            int(SettingType.SLEEP_TIMEOUT_SEC): sleep_timeout,
            int(SettingType.MAX_RECORD_MINUTES): max_record_minutes,
            int(SettingType.USB_MASS_STORAGE): usb_mass_storage,
        }
        self.realtime_asr = False
        self.wifi_on = False
        self.record_state = RecordStateCode.STOPPED
        self.record_result = RecordResult.SUCCESS
        self.last_filename = ""
        self.last_bcd = bcd_bytes(_dt.datetime.now())
        self.writes: List[Tuple[Channel, bytes]] = []
        self.firmware_unsupported_error = firmware_unsupported_error
        # Fault injection: these packet indexes are dropped on the first 0x84
        # attempt only, which is how the resume path gets exercised.
        self.drop_sequences: set = set()
        self.sync_attempts = 0
        # Hardware-shaped faults, all observed on firmware 206:
        self.wifi_close_ack_success = True   # False => ack says failed while AP is off
        self.wifi_ignore_close = 0           # N => swallow the first N close commands
        self.silent_on_missing_file = False  # True  => no ack for a missing 0x84 target

    # -- helpers -------------------------------------------------------------
    def _record_frame(self, cmd: int, state: RecordStateCode, result: RecordResult) -> bytes:
        payload = bytes([int(state), int(result)]) + MAC_BYTES + self.last_bcd
        return build_frame(cmd, payload)

    def _file_bytes(self, entry: AudioFileEntry) -> bytes:
        # Deterministic pseudo-audio so tests can assert exact reassembly.
        seed = sum(entry.name.encode("ascii")) & 0xFF
        return bytes(((seed + i * 7) & 0xFF) for i in range(entry.size))

    def level_frame(self, db: float) -> bytes:
        return build_frame(Cmd.AUDIO_LEVEL, struct.pack("<f", db))

    @staticmethod
    def audio_frame(chunk: bytes) -> bytes:
        if len(chunk) != 288:
            raise ValueError("0x90 payload must be exactly 288 bytes")
        return b"\x5a" + b"\x23\x01\x00" + bytes([Cmd.AUDIO_STREAM]) + chunk + b"\x00\x00"

    # -- write handler -------------------------------------------------------
    def on_write(self, channel: Channel, frame: bytes) -> List[Tuple[Channel, bytes]]:
        self.writes.append((channel, frame))
        if len(frame) < 4 or frame[0] != 0x5A:
            return [(Channel.CONTROL, build_frame(Cmd.ERROR, b"\x02"))]

        code = frame[2]
        payload = bytes(frame[3:-2])
        handler = getattr(self, f"_cmd_{code:02x}", None)
        if handler is None:
            # 0x04 = invalid command, echoing the offending code.
            return [(Channel.CONTROL, build_frame(Cmd.ERROR, bytes([0x04, code])))]
        return handler(payload, channel)

    # -- 0x01 battery --------------------------------------------------------
    def _cmd_01(self, payload: bytes, channel: Channel):
        return [(Channel.CONTROL, build_frame(Cmd.BATTERY, bytes([self.battery])))]

    # -- 0x02 state ----------------------------------------------------------
    def _cmd_02(self, payload: bytes, channel: Channel):
        return [(Channel.RECORD, self._record_frame(Cmd.RECORD_STATE, self.record_state, RecordResult.SUCCESS))]

    # -- 0x03..0x06 FTP ------------------------------------------------------
    def _cmd_03(self, payload: bytes, channel: Channel):
        host = self.ftp_host if self.wifi_on else "0.0.0.0"
        return [(Channel.CONTROL, build_frame(Cmd.FTP_HOST, host.encode()))]

    def _cmd_04(self, payload: bytes, channel: Channel):
        port = str(self.ftp_port) if self.wifi_on else "0"
        return [(Channel.CONTROL, build_frame(Cmd.FTP_PORT, port.encode()))]

    def _cmd_05(self, payload: bytes, channel: Channel):
        return [(Channel.CONTROL, build_frame(Cmd.FTP_USER, self.ftp_user.encode()))]

    def _cmd_06(self, payload: bytes, channel: Channel):
        raw = xor_ff_encode(self.ftp_password) if self.wifi_on else b"\x68\xfe"
        return [(Channel.CONTROL, build_frame(Cmd.FTP_PASSWORD, raw))]

    # -- 0x09 firmware -------------------------------------------------------
    def _cmd_09(self, payload: bytes, channel: Channel):
        return [(Channel.CONTROL, build_frame(Cmd.FIRMWARE, self.firmware.encode()))]

    # -- 0x0B / 0x0C hotspot -------------------------------------------------
    def _cmd_0b(self, payload: bytes, channel: Channel):
        return [(Channel.CONTROL, build_frame(Cmd.WIFI_SSID, self.ssid.encode()))]

    def _cmd_0c(self, payload: bytes, channel: Channel):
        return [(Channel.CONTROL, build_frame(Cmd.WIFI_PASSWORD, xor_ff_encode(self.wifi_password)))]

    # -- 0x0D / 0x0F ---------------------------------------------------------
    def _cmd_0d(self, payload: bytes, channel: Channel):
        return [(Channel.CONTROL, build_frame(Cmd.HAS_IMPORT_FILE, b"\x01" if self.has_import_file else b"\x00"))]

    def _cmd_0f(self, payload: bytes, channel: Channel):
        return [(Channel.CONTROL, build_frame(Cmd.FREE_SPACE, bytes([0x01, self.free_space_percent])))]

    # -- 0x13 file list ------------------------------------------------------
    def _cmd_13(self, payload: bytes, channel: Channel):
        return [(Channel.CONTROL, f) for f in build_file_list_frames(self.files)]

    # -- 0x14 identity -------------------------------------------------------
    def _cmd_14(self, payload: bytes, channel: Channel):
        sn = self.serial.encode()[:16].ljust(16, b"\x00")
        body = sn + bytes([0x00, 0x01 if self.realtime_asr else 0x00])
        return [(Channel.CONTROL, build_frame(Cmd.DEVICE_SN, body))]

    # -- 0x80 recording ------------------------------------------------------
    def _cmd_80(self, payload: bytes, channel: Channel):
        action = payload[0] if payload else 0xFF
        mapping = {
            0x01: RecordStateCode.RECORDING,
            0x02: RecordStateCode.PAUSED,
            0x03: RecordStateCode.RESUMED,
            0x00: RecordStateCode.STOPPED,
        }
        if action not in mapping:
            return [(Channel.RECORD, self._record_frame(Cmd.RECORD_CONTROL, self.record_state, RecordResult.FAILED))]
        self.record_state = mapping[action]
        if action == 0x01:
            self.last_bcd = bcd_bytes(_dt.datetime.now())
        return [(Channel.RECORD, self._record_frame(Cmd.RECORD_CONTROL, self.record_state, self.record_result))]

    # -- 0x84 file sync ------------------------------------------------------
    def _cmd_84(self, payload: bytes, channel: Channel):
        if len(payload) < 4:
            return [(Channel.CONTROL, build_frame(Cmd.ERROR, b"\x05"))]
        operation = payload[-1]
        start = int.from_bytes(payload[-4:-1], "big")
        name = payload[:-4].decode("ascii", errors="replace")
        entry = next((f for f in self.files if f.name == name), None)

        try:
            op = SyncOp(operation)
        except ValueError:
            return [(Channel.CONTROL, build_frame(Cmd.ERROR, b"\x05"))]

        if entry is None:
            if self.silent_on_missing_file:
                return []  # firmware 206 answers nothing at all
            return [(Channel.CONTROL, build_sync_ack(name, False, 0, op, error_code=0x04))]

        self.sync_attempts += 1
        data = self._file_bytes(entry)
        total = (len(data) + 319) // 320
        responses: List[Tuple[Channel, bytes]] = [
            (Channel.CONTROL, build_sync_ack(name, True, total, op))
        ]
        if op in (SyncOp.START, SyncOp.RESUME):
            self.last_filename = name
            for seq in range(start, total):
                if self.sync_attempts == 1 and seq in self.drop_sequences:
                    continue
                chunk = data[seq * 320 : (seq + 1) * 320]
                responses.append((Channel.RECORD, build_sync_packet(seq, chunk)))
        return responses

    # -- 0x87 hotspot --------------------------------------------------------
    def _cmd_87(self, payload: bytes, channel: Channel):
        enable = bool(payload and payload[0] == 0x01)
        if enable:
            self.wifi_on = True
            return [(Channel.CONTROL, build_frame(Cmd.WIFI_AP, bytes([0x01, 0x01])))]
        if self.wifi_ignore_close > 0:
            self.wifi_ignore_close -= 1
            return []  # silent while the state machine is transitioning
        self.wifi_on = False
        result = 0x01 if self.wifi_close_ack_success else 0x00
        return [(Channel.CONTROL, build_frame(Cmd.WIFI_AP, bytes([0x00, result])))]

    # -- 0x91 RTC ------------------------------------------------------------
    def _cmd_91(self, payload: bytes, channel: Channel):
        if len(payload) == 7:
            self.last_bcd = bytes(payload)
        return [(Channel.CONTROL, build_frame(Cmd.RTC_SYNC, b"\x01"))]

    # -- 0x92 delete ---------------------------------------------------------
    def _cmd_92(self, payload: bytes, channel: Channel):
        name = payload.decode("ascii", errors="replace")
        before = len(self.files)
        self.files = [f for f in self.files if f.name != name]
        if len(self.files) == before:
            return [(Channel.CONTROL, build_frame(Cmd.DELETE_FILE, bytes([0x00, 0x04])))]
        return [(Channel.CONTROL, build_frame(Cmd.DELETE_FILE, b"\x01"))]

    # -- 0x9A clear ----------------------------------------------------------
    def _cmd_9a(self, payload: bytes, channel: Channel):
        self.files.clear()
        return [(Channel.CONTROL, build_frame(Cmd.CLEAR_ALL, b"\x01"))]

    # -- 0xA4 realtime -------------------------------------------------------
    def _cmd_a4(self, payload: bytes, channel: Channel):
        self.realtime_asr = bool(payload and payload[0] == 0x01)
        return [
            (
                Channel.CONTROL,
                build_frame(Cmd.REALTIME_ASR, bytes([0x01 if self.realtime_asr else 0x00, 0x01])),
            )
        ]

    # -- 0xB0 write setting --------------------------------------------------
    def _cmd_b0(self, payload: bytes, channel: Channel):
        if not payload:
            return [(Channel.CONTROL, build_frame(Cmd.ERROR, b"\x05"))]
        setting = payload[0]
        if setting in (0x00, 0x01):
            value = struct.unpack("<H", payload[1:3])[0]
        else:
            value = payload[1]
        self.settings[setting] = value
        return [(Channel.CONTROL, build_frame(Cmd.SETTING_WRITE, b"\x01"))]

    # -- 0xB1 read setting ---------------------------------------------------
    def _cmd_b1(self, payload: bytes, channel: Channel):
        setting = payload[0] if payload else 0xFF
        if setting not in self.settings:
            return [(Channel.CONTROL, build_frame(Cmd.ERROR, b"\x05"))]
        value = self.settings[setting]
        if setting == 0x02:
            body = bytes([setting, 0x01, value])
        else:
            body = bytes([setting, 0x01]) + struct.pack("<H", value)
        return [(Channel.CONTROL, build_frame(Cmd.SETTING_READ, body))]


class FakeTransport(BleTransport):
    """Drives `FakeMemoTalk` through the real dispatch code path."""

    needs_target = False

    def __init__(
        self,
        device: Optional[FakeMemoTalk] = None,
        *,
        auto_handshake: bool = True,
    ) -> None:
        super().__init__(keepalive_interval=0, auto_handshake=auto_handshake)
        self.device = device or FakeMemoTalk()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, target=None, *, timeout=None) -> None:
        self._target = target or self.device
        self._connected = True
        self._keepalive_stop = asyncio.Event()
        if self.auto_handshake:
            await self.handshake()

    async def disconnect(self) -> None:
        self._connected = False
        for sub in list(self._subscriptions):
            sub.close()
        self._subscriptions.clear()

    async def write(self, channel: Channel, frame: bytes) -> None:
        if not self._connected:
            raise ConnectionLost("fake link is down")
        loop = asyncio.get_running_loop()
        for out_channel, response in self.device.on_write(channel, frame):
            loop.call_soon(self._dispatch, out_channel, response)

    # convenience for stream tests
    def emit_level(self, db: float) -> None:
        self.publish(Channel.RECORD, self.device.level_frame(db))

    def emit_audio(self, chunk: bytes) -> None:
        self.publish(Channel.RECORD, self.device.audio_frame(chunk))
