"""High level client: the *top* half of the design.

Six capability groups, one per hardware domain, so that every public method can
be traced back to a specific command in `commands.py`:

    client.info()      composite status snapshot
    client.record      recording lifecycle (0x80) + realtime mode select (0xA4)
    client.stream      unsolicited pushes (0xA8 level, 0x90 MP3)
    client.files       list / download / delete / clear (0x13, 0x84, 0x92, 0x9A)
    client.settings    three configurable knobs (0xB0 / 0xB1)
    client.wifi        hotspot + FTP credentials, with a guaranteed off-switch
    client.raw         escape hatch for anything not modelled here
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import ftplib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, List, Optional, Union

from . import commands, parsers
from .assemblers import AudioSyncAssembler, FileListAssembler
from .constants import (
    Channel,
    Cmd,
    RecAction,
    SettingType,
    SyncOp,
)
from .discovery import describe, find_device
from .errors import (
    CapabilityUnavailable,
    DeleteFailed,
    DeviceNotFound,
    DeviceRejected,
    FileNotFoundOnDevice,
    MemoTalkTimeout,
    MemoTalkError,
    ProtocolError,
    RecordingRejected,
    device_error_from_frame,
)
from .models import (
    AckResult,
    AudioFileEntry,
    AudioLevel,
    BatteryInfo,
    DeviceIdentity,
    DeviceInfo,
    DeviceSettings,
    FirmwareInfo,
    FreeSpace,
    FtpConfig,
    RecordState,
    SettingValue,
    SyncAck,
    WifiCredentials,
)
from .transport import BleTransport

__all__ = ["MemoTalkClient", "NetworkSession"]


class MemoTalkClient:
    """One peripheral, one link, six capability groups."""

    def __init__(
        self,
        device=None,
        *,
        address: Optional[str] = None,
        transport: Optional[BleTransport] = None,
        keepalive_interval: float = 30.0,
        auto_handshake: bool = True,
        scan_timeout: float = 3.5,
        connect_timeout: float = 12.0,
        prefer: Optional[str] = None,
        logger=None,
    ) -> None:
        self._device_arg = device
        self._address = address
        self._prefer = prefer
        self._scan_timeout = scan_timeout
        self._transport = transport or BleTransport(
            keepalive_interval=keepalive_interval,
            auto_handshake=auto_handshake,
            connect_timeout=connect_timeout,
            logger=logger,
        )

        self.record = RecordController(self)
        self.stream = StreamController(self)
        self.files = FileController(self)
        self.settings = SettingsController(self)
        self.wifi = WifiController(self)
        self.raw = RawController(self)

    # -- properties ----------------------------------------------------------
    @property
    def transport(self) -> BleTransport:
        return self._transport

    @property
    def is_connected(self) -> bool:
        return self._transport.is_connected

    # -- lifecycle -----------------------------------------------------------
    async def connect(self) -> "MemoTalkClient":
        target = self._device_arg
        if target is None:
            target = self._address
        if target is None and getattr(self._transport, "needs_target", True):
            target = await find_device(self._scan_timeout, prefer=self._prefer)
            if target is None:
                raise DeviceNotFound(
                    "no MemoTalk S1 / AI-PEN found; press the physical button to wake it "
                    "and make sure it is not connected to another host"
                )
        await self._transport.connect(target)
        return self

    async def close(self) -> None:
        await self._transport.disconnect()

    async def __aenter__(self) -> "MemoTalkClient":
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def __repr__(self) -> str:
        target = self._transport.target
        state = "connected" if self.is_connected else "disconnected"
        return f"<MemoTalkClient {state} {describe(target) if target else '?'}>"

    # -- internal call helpers ----------------------------------------------
    async def _call(
        self,
        frame: bytes,
        code: int,
        *,
        write: Channel = Channel.CONTROL,
        listen: Optional[Channel] = None,
        timeout: float = 2.0,
    ) -> bytes:
        """Write a command and return its answer, translating 0x00 error frames."""
        listen = listen or write
        expected = int(code)

        def predicate(pkt: bytes) -> bool:
            return (
                len(pkt) >= 3
                and pkt[0] == 0x5A
                and pkt[2] in (expected, int(Cmd.ERROR))
            )

        pkt = await self._transport.request(
            frame,
            predicate=predicate,
            write_channel=write,
            listen_channel=listen,
            timeout=timeout,
        )
        if pkt[2] == int(Cmd.ERROR):
            raise device_error_from_frame(pkt)
        return pkt

    async def _call_parsed(
        self,
        frame: bytes,
        code: int,
        parser: Callable[[bytes], object],
        *,
        write: Channel = Channel.CONTROL,
        listen: Optional[Channel] = None,
        timeout: float = 2.0,
    ):
        pkt = await self._call(
            frame, code, write=write, listen=listen, timeout=timeout
        )
        return parser(pkt)

    # -- composite -----------------------------------------------------------
    async def info(self, *, include_files: bool = False) -> DeviceInfo:
        """One round of status reads, assembled into a snapshot."""
        battery = await self.battery()
        firmware = await self.firmware()
        identity = await self.identity()
        free_space = await self.free_space()
        record = await self.record.state()
        settings = await self.settings.read_all()
        try:
            has_import: Optional[bool] = await self.has_import_file()
        except ProtocolError:
            has_import = None
        files = await self.files.list() if include_files else None
        return DeviceInfo(
            battery=battery,
            firmware=firmware,
            identity=identity,
            free_space=free_space,
            has_import_file=has_import,
            record=record,
            settings=settings,
            files=files,
        )

    async def battery(self) -> BatteryInfo:
        """0x01"""
        return await self._call_parsed(
            commands.read_battery(), Cmd.BATTERY, parsers.parse_battery
        )

    async def firmware(self) -> FirmwareInfo:
        """0x09"""
        return await self._call_parsed(
            commands.read_firmware_version(), Cmd.FIRMWARE, parsers.parse_firmware
        )

    async def identity(self) -> DeviceIdentity:
        """0x14"""
        return await self._call_parsed(
            commands.read_device_sn(), Cmd.DEVICE_SN, parsers.parse_device_identity
        )

    async def free_space(self) -> FreeSpace:
        """0x0F"""
        return await self._call_parsed(
            commands.read_free_space(), Cmd.FREE_SPACE, parsers.parse_free_space
        )

    async def has_import_file(self) -> bool:
        """0x0D"""
        return await self._call_parsed(
            commands.read_has_import_file(),
            Cmd.HAS_IMPORT_FILE,
            parsers.parse_has_import_file,
        )

    async def sync_time(self, when: Optional[_dt.datetime] = None) -> bool:
        """0x91 - re-run the RTC handshake (e.g. after a long idle period)."""
        return await self._transport.handshake(when)


# ---------------------------------------------------------------------------
# 0x80 / 0xA4 - recording lifecycle
# ---------------------------------------------------------------------------
class RecordController:
    def __init__(self, client: MemoTalkClient) -> None:
        self._client = client

    async def state(self, timeout: float = 2.0) -> RecordState:
        """0x02 - written to FFF1, answered on FFE2."""
        return await self._client._call_parsed(
            commands.read_record_state(),
            Cmd.RECORD_STATE,
            parsers.parse_record_state,
            write=Channel.CONTROL,
            listen=Channel.RECORD,
            timeout=timeout,
        )

    async def _send(self, action: RecAction, timeout: float = 3.0) -> RecordState:
        pkt = await self._client._call(
            commands.control_recording(action),
            Cmd.RECORD_CONTROL,
            write=Channel.RECORD,
            listen=Channel.RECORD,
            timeout=timeout,
        )
        state = parsers.parse_record_state(pkt)
        if not state.success:
            raise RecordingRejected(state.result, pkt)
        return state

    async def start(self) -> RecordState:
        """0x80 (0x01)"""
        return await self._send(RecAction.START)

    async def pause(self) -> RecordState:
        """0x80 (0x02)"""
        return await self._send(RecAction.PAUSE)

    async def resume(self) -> RecordState:
        """0x80 (0x03)"""
        return await self._send(RecAction.RESUME)

    async def stop(self) -> RecordState:
        """0x80 (0x00). Safe to call twice; the device may ack twice."""
        return await self._send(RecAction.STOP)

    async def set_realtime(self, enabled: bool) -> AckResult:
        """0xA4 - route FFE2 to 0x90 MP3 stream (True) or 0xA8 level meter (False)."""
        return await self._client._call_parsed(
            commands.set_realtime_asr(enabled),
            Cmd.REALTIME_ASR,
            parsers.parse_ack_result,
        )


# ---------------------------------------------------------------------------
# 0xA8 / 0x90 - unsolicited pushes
# ---------------------------------------------------------------------------
class StreamController:
    def __init__(self, client: MemoTalkClient) -> None:
        self._client = client

    async def _iterate(self, predicate, channel=Channel.RECORD) -> AsyncIterator[bytes]:
        transport = self._client.transport
        sub = transport.subscribe(predicate, channel=channel, maxsize=1024)
        try:
            while True:
                if not transport.is_connected:
                    return
                try:
                    frame = await sub.get(timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                yield frame
        finally:
            transport.unsubscribe(sub)

    async def level(self) -> AsyncIterator[AudioLevel]:
        """0xA8 - ~10 frames per second while recording with realtime mode off."""
        async for frame in self._iterate(parsers.is_audio_level_frame):
            yield parsers.parse_audio_level(frame)

    async def audio(self) -> AsyncIterator[bytes]:
        """0x90 - 288-byte MP3 chunks (3 x 96B frames = 72ms) for live ASR."""
        async for frame in self._iterate(parsers.is_audio_stream_frame):
            yield parsers.parse_audio_stream(frame)

    async def states(self) -> AsyncIterator[RecordState]:
        """0x02 / 0x80 state changes pushed during a recording session."""
        async for frame in self._iterate(parsers.is_record_state_frame):
            yield parsers.parse_record_state(frame)

    async def packets(self, predicate=None, channel: Channel = Channel.RECORD) -> AsyncIterator[bytes]:
        """Raw frames - the escape hatch for debugging the stream."""
        async for frame in self._iterate(predicate or (lambda _f: True), channel=channel):
            yield frame


# ---------------------------------------------------------------------------
# 0x13 / 0x84 / 0x92 / 0x9A - file assets
# ---------------------------------------------------------------------------
class FileController:
    def __init__(self, client: MemoTalkClient) -> None:
        self._client = client

    async def list(self, *, timeout: float = 4.0) -> List[AudioFileEntry]:
        """0x13 - returns [] for an empty device without raising."""
        transport = self._client.transport
        assembler = FileListAssembler()
        sub = transport.subscribe(
            parsers.is_file_list_packet, channel=Channel.CONTROL, maxsize=512
        )
        try:
            await transport.write(Channel.CONTROL, commands.query_file_list())
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while not assembler.is_complete:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise MemoTalkTimeout(Cmd.FILE_LIST)
                try:
                    pkt = await sub.get(timeout=remaining)
                except asyncio.TimeoutError:
                    raise MemoTalkTimeout(Cmd.FILE_LIST) from None
                if assembler.feed(pkt):
                    break
            return list(assembler.files)
        finally:
            transport.unsubscribe(sub)

    async def latest(self) -> Optional[AudioFileEntry]:
        entries = await self.list()
        return max(entries, key=lambda e: e.timestamp) if entries else None

    async def download(
        self,
        target: Union[str, AudioFileEntry],
        dest_dir: Union[str, Path, None] = None,
        *,
        size: Optional[int] = None,
        max_retries: int = 3,
        packet_timeout: float = 3.0,
        ack_timeout: float = 3.0,
        progress: Optional[Callable[[int, int], None]] = None,
        overwrite: bool = False,
    ) -> Path:
        """0x84 - pull one recording over BLE with gap detection and resume.

        This is the loop the original skill described but never implemented:
        on a packet timeout we ask the device to resume from the first missing
        index instead of failing the whole transfer.
        """
        transport = self._client.transport
        name = target.name if isinstance(target, AudioFileEntry) else str(target)
        if size is None and isinstance(target, AudioFileEntry):
            size = target.size
        if size is None:
            entries = await self.list()
            entry = next((e for e in entries if e.name == name), None)
            if entry is None:
                raise FileNotFoundOnDevice(f"{name!r} is not present on the device")
            size = entry.size

        dest_path = Path(dest_dir or ".") / name
        if dest_path.exists() and not overwrite:
            raise FileExistsError(f"{dest_path} already exists (pass overwrite=True)")

        assembler = AudioSyncAssembler(name, expected_size=size)
        data_sub = transport.subscribe(
            parsers.is_sync_packet, channel=Channel.RECORD, maxsize=4096
        )
        ack_sub = transport.subscribe(
            parsers.is_sync_ack, channel=Channel.CONTROL, maxsize=16
        )
        try:
            async with transport.activity():
                next_index = 0
                for attempt in range(max_retries + 1):
                    operation = SyncOp.START if assembler.received == 0 else SyncOp.RESUME
                    await transport.write(
                        Channel.CONTROL,
                        commands.sync_audio_file(name, next_index, operation),
                    )
                    try:
                        ack_frame = await ack_sub.get(timeout=ack_timeout)
                    except asyncio.TimeoutError:
                        # Firmware 206 stays completely silent when the file does
                        # not exist, instead of answering exists=0x00 (ERRATA E9).
                        # Distinguish "missing" from "slow" by asking 0x13.
                        try:
                            names = {e.name for e in await self.list()}
                        except MemoTalkError:
                            names = None
                        if names is not None and name not in names:
                            raise FileNotFoundOnDevice(
                                f"{name!r} is not present on the device"
                            ) from None
                        raise MemoTalkTimeout(Cmd.FILE_SYNC) from None
                    if ack_frame[2] == int(Cmd.ERROR):
                        raise device_error_from_frame(ack_frame)
                    ack = assembler.feed_ack(ack_frame)
                    if not ack.exists:
                        raise FileNotFoundOnDevice(f"{name!r} does not exist on the device")
                    if ack.total_packets == 0:
                        break

                    while not assembler.is_complete:
                        try:
                            frame = await data_sub.get(timeout=packet_timeout)
                        except asyncio.TimeoutError:
                            break
                        assembler.feed_packet(frame)
                        if progress and assembler.total_packets:
                            progress(assembler.received, assembler.total_packets)

                    if assembler.is_complete:
                        break
                    nxt = assembler.next_retry_from()
                    if nxt is None:
                        break
                    next_index = nxt
                else:
                    pass

            if not assembler.is_complete:
                missing = assembler.missing_sequences()
                raise ProtocolError(
                    f"transfer incomplete: {len(missing)} of "
                    f"{assembler.total_packets} packet(s) missing after retries"
                )

            raw = assembler.assemble()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(raw)
            return dest_path
        finally:
            transport.unsubscribe(data_sub)
            transport.unsubscribe(ack_sub)

    async def download_all(
        self,
        dest_dir: Union[str, Path, None] = None,
        *,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[Path]:
        """Serial download of every recording (FTP-free path)."""
        results: List[Path] = []
        for entry in await self.list():
            results.append(
                await self.download(entry, dest_dir, progress=progress, overwrite=True)
            )
        return results

    async def delete(self, filename: str, *, timeout: float = 3.0) -> bool:
        """0x92 - delete one recording by exact name."""
        try:
            frame = commands.delete_file(filename)
        except ValueError as exc:
            raise DeviceRejected(str(exc)) from exc
        pkt = await self._client._call(frame, Cmd.DELETE_FILE, timeout=timeout)
        result = parsers.parse_delete_result(pkt)
        if result is True:
            return True
        raise DeleteFailed(f"device refused to delete {filename!r} (error 0x{result:02X})", code=result, frame=pkt)

    async def clear_all(self, *, confirm: bool = False, timeout: float = 5.0) -> bool:
        """0x9A - irreversible; requires confirm=True."""
        frame = commands.clear_all_files(confirm=confirm)  # may raise ConfirmationRequired
        pkt = await self._client._call(frame, Cmd.CLEAR_ALL, timeout=timeout)
        return parsers.parse_simple_ack(pkt)

    async def sync_control(
        self, filename: str, operation: SyncOp, start_packet: int = 0, *, timeout: float = 3.0
    ) -> SyncAck:
        """0x84 control-only: pause / resume / cancel a transfer by hand."""
        pkt = await self._client._call(
            commands.sync_audio_file(filename, start_packet, operation),
            Cmd.FILE_SYNC,
            timeout=timeout,
        )
        return parsers.parse_sync_ack(pkt)


# ---------------------------------------------------------------------------
# 0xB0 / 0xB1 - settings
# ---------------------------------------------------------------------------
class SettingsController:
    def __init__(self, client: MemoTalkClient) -> None:
        self._client = client

    async def read(self, setting_type: SettingType, *, timeout: float = 2.0) -> SettingValue:
        """0xB1"""
        st = SettingType(int(setting_type))
        return await self._client._call_parsed(
            commands.read_setting(st),
            Cmd.SETTING_READ,
            lambda frame: parsers.parse_setting(frame, st),
            timeout=timeout,
        )

    async def read_all(self) -> DeviceSettings:
        settings = DeviceSettings()
        for st in SettingType:
            try:
                value = await self.read(st)
            except MemoTalkError:
                continue
            settings.raw[int(st)] = value
            if st is SettingType.SLEEP_TIMEOUT_SEC:
                settings.sleep_timeout_sec = value.value
            elif st is SettingType.MAX_RECORD_MINUTES:
                settings.max_record_minutes = value.value
            else:
                settings.usb_mass_storage = bool(value.value)
        return settings

    async def write(
        self, setting_type: SettingType, value: int, *, timeout: float = 2.0
    ) -> bool:
        """0xB0"""
        frame = commands.write_setting(setting_type, value)
        pkt = await self._client._call(frame, Cmd.SETTING_WRITE, timeout=timeout)
        return parsers.parse_simple_ack(pkt)

    async def sleep_timeout(self, seconds: Optional[int] = None, *, timeout: float = 2.0):
        """Get (no argument) or set the idle sleep timeout in seconds."""
        if seconds is None:
            return (await self.read(SettingType.SLEEP_TIMEOUT_SEC, timeout=timeout)).value
        await self.write(SettingType.SLEEP_TIMEOUT_SEC, seconds, timeout=timeout)
        return seconds

    async def max_record_minutes(self, minutes: Optional[int] = None, *, timeout: float = 2.0):
        if minutes is None:
            return (await self.read(SettingType.MAX_RECORD_MINUTES, timeout=timeout)).value
        await self.write(SettingType.MAX_RECORD_MINUTES, minutes, timeout=timeout)
        return minutes

    async def usb_mass_storage(self, enabled: Optional[bool] = None, *, timeout: float = 2.0):
        if enabled is None:
            return bool((await self.read(SettingType.USB_MASS_STORAGE, timeout=timeout)).value)
        await self.write(SettingType.USB_MASS_STORAGE, 1 if enabled else 0, timeout=timeout)
        return enabled


# ---------------------------------------------------------------------------
# 0x87 / 0x03-0x06 / 0x0B-0x0C - hotspot and FTP
# ---------------------------------------------------------------------------
@dataclass
class NetworkSession:
    """Credentials for one hotspot window. Valid only inside `client.wifi.session()`."""

    credentials: WifiCredentials
    ftp_config: FtpConfig

    @property
    def ssid(self) -> str:
        return self.credentials.ssid

    @property
    def password(self) -> str:
        return self.credentials.password

    def open_ftp(self, timeout: float = 10.0) -> ftplib.FTP:
        """Connect and log in over PASV. The host must already be on this hotspot."""
        if not self.ftp_config.ready:
            raise ConnectionError(
                f"FTP endpoint not ready ({self.ftp_config.address}); "
                "enable the hotspot first and wait for DHCP"
            )
        ftp = ftplib.FTP()
        ftp.connect(self.ftp_config.host, self.ftp_config.port, timeout=timeout)
        ftp.login(self.ftp_config.user, self.ftp_config.password)
        ftp.set_pasv(True)
        return ftp

    @staticmethod
    def list_audio_files(ftp: ftplib.FTP) -> List[str]:
        """Only real `.mp3` entries; skips `.Spotlight-V100` and friends."""
        lines: List[str] = []
        ftp.retrlines("LIST", lines.append)
        names: List[str] = []
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            name = parts[-1]
            if name.startswith(".") or not name.lower().endswith(".mp3"):
                continue
            names.append(name)
        return names

    def download(
        self, ftp: ftplib.FTP, filename: str, dest_dir: Union[str, Path, None] = None
    ) -> Path:
        """Serial RETR of one file. Keep concurrency at 1 as the SDK requires."""
        dest = Path(dest_dir or ".") / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as handle:
            ftp.retrbinary(f"RETR {filename}", handle.write, blocksize=32768)
        return dest

    def join_hotspot(self, iface: str = "en0", wait: float = 15.0) -> Optional[str]:
        """Join the device hotspot. Implemented for macOS only.

        Other platforms must join through their own network API; this method
        refuses rather than pretending.
        """
        if sys.platform != "darwin":
            raise CapabilityUnavailable(
                "automatic hotspot join is implemented for macOS only; "
                f"join {self.ssid!r} with your platform's network API instead"
            )
        import time

        subprocess.run(
            ["networksetup", "-setairportnetwork", iface, self.ssid, self.password],
            timeout=20,
            check=False,
        )
        prefix = ".".join(self.ftp_config.host.split(".")[:3]) + "."
        deadline = time.time() + wait
        while time.time() < deadline:
            out = subprocess.run(
                ["ipconfig", "getifaddr", iface], capture_output=True, text=True
            ).stdout.strip()
            if out.startswith(prefix):
                return out
            time.sleep(1.0)
        return None


class WifiController:
    """Hotspot control.

    Hardware notes (verified on firmware 206):

    * ``0x87`` is an asynchronous state transition. While it is in flight the
      device answers nothing, and an interrupted enable can leave it reporting
      the hotspot as off while still refusing to record.
    * An acknowledgement cannot be trusted: the device has been observed
      returning ``result=0`` (failure) while the hotspot was already off.

    Closing is therefore retried and then *verified* by reading ``0x03``.
    """

    def __init__(self, client: MemoTalkClient) -> None:
        self._client = client
        #: Outcome of the most recent close:
        #: ``{"confirmed": bool, "attempts": int, "error": str | None}``
        self.last_close_report: Optional[dict] = None

    async def enable(self, *, ready_delay: float = 1.5, timeout: float = 20.0) -> AckResult:
        """0x87 (0x01) plus a settle delay for DHCP and the FTP daemon.

        The default timeout is 20s: real hardware has been measured taking far
        more than 8s to answer while the access point comes up.
        """
        ack = await self._client._call_parsed(
            commands.set_wifi_ap(True), Cmd.WIFI_AP, parsers.parse_ack_result, timeout=timeout
        )
        if not ack.success:
            raise DeviceRejected("device refused to start the Wi-Fi hotspot", code=ack.state)
        if ready_delay:
            await asyncio.sleep(ready_delay)
        return ack

    async def hotspot_state(self, *, timeout: float = 6.0) -> bool:
        """True when the FTP endpoint has an address (i.e. the AP is up).

        ``0x03`` answers even while the hotspot is down, returning ``0.0.0.0``,
        which makes it a reliable on/off probe.
        """
        text = await self._client._call_parsed(
            commands.read_ftp_host(), Cmd.FTP_HOST, parsers.parse_text, timeout=timeout
        )
        value = (text or "").strip()
        return bool(value) and value != "0.0.0.0"

    async def disable(
        self, *, timeout: float = 20.0, attempts: int = 3, verify: bool = True
    ) -> AckResult:
        """0x87 (0x00), retried and confirmed via ``0x03``."""
        ack: Optional[AckResult] = None
        last_error: Optional[BaseException] = None

        for attempt in range(1, attempts + 1):
            try:
                ack = await self._client._call_parsed(
                    commands.set_wifi_ap(False),
                    Cmd.WIFI_AP,
                    parsers.parse_ack_result,
                    timeout=timeout,
                )
            except MemoTalkError as exc:
                last_error = exc
                ack = None

            if not verify:
                if ack is not None:
                    self.last_close_report = {
                        "confirmed": ack.success, "attempts": attempt, "error": None
                    }
                    return ack
                raise DeviceRejected("hotspot close got no acknowledgement") from last_error

            try:
                still_on = await self.hotspot_state()
            except MemoTalkError:
                still_on = True  # cannot prove it is off -> retry

            if not still_on:
                confirmed = AckResult(code=int(Cmd.WIFI_AP), state=0x00, success=True)
                self.last_close_report = {
                    "confirmed": True, "attempts": attempt, "error": None
                }
                return ack if (ack is not None and ack.success) else confirmed

            if attempt < attempts:
                await asyncio.sleep(1.0)

        message = f"hotspot still reachable after {attempts} close attempt(s)"
        self.last_close_report = {
            "confirmed": False,
            "attempts": attempts,
            "error": str(last_error) if last_error else message,
        }
        raise DeviceRejected(message) from last_error

    async def credentials(self, *, timeout: float = 6.0) -> WifiCredentials:
        """0x0B + 0x0C. Only meaningful while the hotspot is on."""
        ssid = await self._client._call_parsed(
            commands.read_wifi_ssid(), Cmd.WIFI_SSID, parsers.parse_text, timeout=timeout
        )
        password = await self._client._call_parsed(
            commands.read_wifi_password(),
            Cmd.WIFI_PASSWORD,
            parsers.parse_secret,
            timeout=timeout,
        )
        return WifiCredentials(ssid=ssid, password=password)

    async def ftp(self, *, timeout: float = 6.0) -> FtpConfig:
        """0x03 + 0x04 + 0x05 + 0x06. Reading before the AP is up yields 0.0.0.0."""
        host = await self._client._call_parsed(
            commands.read_ftp_host(), Cmd.FTP_HOST, parsers.parse_text, timeout=timeout
        )
        port = await self._client._call_parsed(
            commands.read_ftp_port(), Cmd.FTP_PORT, parsers.parse_text, timeout=timeout
        )
        user = await self._client._call_parsed(
            commands.read_ftp_user(), Cmd.FTP_USER, parsers.parse_text, timeout=timeout
        )
        password = await self._client._call_parsed(
            commands.read_ftp_password(), Cmd.FTP_PASSWORD, parsers.parse_secret, timeout=timeout
        )
        try:
            port_number = int(port.strip())
        except ValueError:
            port_number = 0
        return FtpConfig(
            host=host.strip(), port=port_number, user=user.strip(), password=password
        )

    @contextlib.asynccontextmanager
    async def session(
        self,
        *,
        ready_delay: float = 1.5,
        timeout: float = 20.0,
        close_timeout: float = 20.0,
    ) -> AsyncIterator[NetworkSession]:
        """Guarantee the hotspot ends up off, whatever happens inside.

        If ``enable`` fails, a best-effort close still runs: an interrupted
        enable is exactly what left hardware stuck refusing to record.
        """
        try:
            await self.enable(ready_delay=ready_delay, timeout=timeout)
        except MemoTalkError:
            with contextlib.suppress(MemoTalkError):
                await self.disable(timeout=close_timeout, attempts=2)
            raise

        self.last_close_report = None
        try:
            credentials = await self.credentials()
            ftp_config = await self.ftp()
            yield NetworkSession(credentials=credentials, ftp_config=ftp_config)
        finally:
            with contextlib.suppress(MemoTalkError):
                await self.disable(timeout=close_timeout)


# ---------------------------------------------------------------------------
# escape hatch
# ---------------------------------------------------------------------------
class RawController:
    def __init__(self, client: MemoTalkClient) -> None:
        self._client = client

    async def send(self, frame: bytes, channel: Channel = Channel.CONTROL) -> None:
        await self._client.transport.write(channel, frame)

    async def request(
        self,
        frame: bytes,
        *,
        code: Optional[int] = None,
        predicate=None,
        write: Channel = Channel.CONTROL,
        listen: Optional[Channel] = None,
        timeout: float = 2.0,
    ) -> bytes:
        return await self._client.transport.request(
            frame,
            code=code,
            predicate=predicate,
            write_channel=write,
            listen_channel=listen,
            timeout=timeout,
        )

    async def expect(self, predicate, *, channel: Channel = Channel.RECORD, timeout: float = 2.0) -> bytes:
        return await self._client.transport.expect(predicate, channel=channel, timeout=timeout)
