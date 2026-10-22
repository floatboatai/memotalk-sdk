"""BLE transport: the layer the original skill never shipped.

Responsibilities, and only these:

* open/close the link and subscribe both notify characteristics
* route a write to the correct characteristic (FFF1 vs FFE1)
* rebuild frames from fragmented notifications
* correlate a write with its answer, by code or by predicate, under a deadline
* fan out unsolicited pushes (0xA8 / 0x90 / record state) to subscribers
* keep the peripheral awake while idle
* surface disconnects as `ConnectionLost` instead of a hang

No protocol knowledge beyond "where do the bytes go" lives here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Callable, List, Optional

from . import commands, parsers
from .assemblers import FrameReassembler
from .constants import (
    UUID_CHAR_FFE1_WRITE,
    UUID_CHAR_FFE2_NOTIFY,
    UUID_CHAR_FFF1_WRITE,
    UUID_CHAR_FFF2_NOTIFY,
    Channel,
    Cmd,
)
from .errors import ConnectionLost, MemoTalkTimeout, TransportError

__all__ = ["Subscription", "BleTransport"]

Predicate = Callable[[bytes], bool]

log = logging.getLogger("memotalk.transport")


class Subscription:
    """A filtered, bounded queue over one notify channel."""

    __slots__ = ("predicate", "channel", "_queue", "_closed", "_maxsize", "dropped")

    def __init__(self, predicate: Optional[Predicate], channel: Optional[Channel], maxsize: int = 256):
        self.predicate = predicate
        self.channel = channel
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._closed = False
        self._maxsize = maxsize
        self.dropped = 0

    def matches(self, frame: bytes, channel: Channel) -> bool:
        if self._closed:
            return False
        if self.channel is not None and self.channel != channel:
            return False
        if self.predicate is None:
            return True
        try:
            return bool(self.predicate(frame))
        except Exception:  # a bad predicate must not kill the notify callback
            return False

    def offer(self, frame: bytes) -> None:
        if self._closed:
            return
        if self._maxsize and self._queue.qsize() >= self._maxsize:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(frame)

    async def get(self, timeout: Optional[float] = None) -> bytes:
        if timeout is None:
            return await self._queue.get()
        return await asyncio.wait_for(self._queue.get(), timeout)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _code_predicate(code: Optional[int], include_error: bool = True) -> Predicate:
    def predicate(frame: bytes) -> bool:
        if len(frame) < 3 or frame[0] != 0x5A:
            return False
        if code is not None and frame[2] == int(code):
            return True
        return include_error and frame[2] == int(Cmd.ERROR)

    return predicate


class BleTransport:
    """Async BLE link to one MemoTalk S1 / AI-PEN peripheral."""

    #: True when the transport must be told which peripheral to open.
    needs_target = True

    def __init__(
        self,
        *,
        keepalive_interval: float = 30.0,
        auto_handshake: bool = True,
        connect_timeout: float = 12.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.keepalive_interval = keepalive_interval
        self.auto_handshake = auto_handshake
        self.connect_timeout = connect_timeout
        self.log = logger or log

        self._client = None
        self._target = None
        self._connected = False
        self._disconnect_reason: Optional[str] = None
        self._subscriptions: List[Subscription] = []
        self._reassemblers = {
            Channel.CONTROL: FrameReassembler("control"),
            Channel.RECORD: FrameReassembler("record"),
        }
        self._keepalive_task: Optional[asyncio.Task] = None
        self._keepalive_stop: Optional[asyncio.Event] = None
        self._busy = 0
        self.handshake_done = False

    # -- introspection -------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            return bool(client.is_connected)
        except Exception:
            return self._connected

    @property
    def target(self):
        return self._target

    # -- lifecycle -----------------------------------------------------------
    async def connect(self, target=None, *, timeout: Optional[float] = None) -> None:
        """Connect to a BLEDevice, a MAC address, or a device name hint."""
        if isinstance(target, str) and len(target.split(":")) == 6:
            address: Optional[str] = target
            device = None
        else:
            address = None
            device = target

        try:
            from bleak import BleakClient
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise TransportError("bleak is required for real BLE transport") from exc

        self._target = target
        self._disconnect_reason = None
        self._connected = False
        self._keepalive_stop = asyncio.Event()

        kwargs = {"timeout": timeout or self.connect_timeout}
        handle = device if device is not None else address
        try:
            client = BleakClient(handle, disconnected_callback=self._on_disconnected, **kwargs)
        except TypeError:  # older bleak signature without disconnected_callback
            client = BleakClient(handle, **kwargs)

        self._client = client
        try:
            await client.connect()
            await client.start_notify(UUID_CHAR_FFF2_NOTIFY, self._make_notify_handler(Channel.CONTROL))
            await client.start_notify(UUID_CHAR_FFE2_NOTIFY, self._make_notify_handler(Channel.RECORD))
        except Exception as exc:
            self._client = None
            raise TransportError(f"failed to open BLE link: {exc}") from exc

        self._connected = True
        self.log.debug("connected to %s", target)

        if self.auto_handshake:
            try:
                await self.handshake()
            except (MemoTalkTimeout, TransportError) as exc:
                self.log.warning("0x91 handshake did not confirm in time: %s", exc)

        self._start_keepalive()

    def _on_disconnected(self, client) -> None:
        self._connected = False
        self._disconnect_reason = "peripheral disconnected"
        self.log.debug("peripheral disconnected")

    async def disconnect(self) -> None:
        await self._stop_keepalive()
        self._connected = False
        for sub in list(self._subscriptions):
            sub.close()
        self._subscriptions.clear()
        client, self._client = self._client, None
        if client is None:
            return
        for uuid in (UUID_CHAR_FFF2_NOTIFY, UUID_CHAR_FFE2_NOTIFY):
            with contextlib.suppress(Exception):
                await client.stop_notify(uuid)
        with contextlib.suppress(Exception):
            await client.disconnect()

    async def __aenter__(self) -> "BleTransport":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # -- notifications -------------------------------------------------------
    def _make_notify_handler(self, channel: Channel):
        def handler(_characteristic, data) -> None:
            try:
                self._on_notify(channel, bytes(data))
            except Exception:  # pragma: no cover - never raise into bleak
                self.log.exception("notify handler failed")

        return handler

    def _on_notify(self, channel: Channel, data: bytes) -> None:
        for frame in self._reassemblers[channel].feed(data):
            self._dispatch(channel, frame)

    def _dispatch(self, channel: Channel, frame: bytes) -> None:
        # Iterate a copy: a subscriber may close itself while being served.
        for sub in list(self._subscriptions):
            if sub.matches(frame, channel):
                sub.offer(frame)

    def publish(self, channel: Channel, frame: bytes) -> None:
        """Inject a frame as if the device had sent it (used by simulators and replay)."""
        self._dispatch(channel, frame)

    # -- subscriptions -------------------------------------------------------
    def subscribe(
        self,
        predicate: Optional[Predicate] = None,
        *,
        channel: Optional[Channel] = None,
        maxsize: int = 256,
    ) -> Subscription:
        sub = Subscription(predicate, channel, maxsize=maxsize)
        self._subscriptions.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        with contextlib.suppress(ValueError):
            self._subscriptions.remove(sub)

    # -- writes --------------------------------------------------------------
    async def write(self, channel: Channel, frame: bytes) -> None:
        if not self.is_connected or self._client is None:
            raise ConnectionLost(
                self._disconnect_reason or "not connected; call connect() first"
            )
        uuid = UUID_CHAR_FFE1_WRITE if channel == Channel.RECORD else UUID_CHAR_FFF1_WRITE
        try:
            await self._client.write_gatt_char(uuid, frame, response=False)
        except Exception as exc:
            if not self.is_connected:
                raise ConnectionLost(self._disconnect_reason or "link dropped") from exc
            raise TransportError(f"write to {channel.value} failed: {exc}") from exc

    # -- request / response --------------------------------------------------
    async def request(
        self,
        frame: bytes,
        *,
        code: Optional[int] = None,
        predicate: Optional[Predicate] = None,
        write_channel: Channel = Channel.CONTROL,
        listen_channel: Optional[Channel] = None,
        timeout: float = 2.0,
    ) -> bytes:
        """Write `frame`, then wait for the first answer that matches.

        The subscription is registered *before* the write so a fast answer can
        never be missed.
        """
        listen = listen_channel or write_channel
        pred = predicate or _code_predicate(code)
        sub = self.subscribe(pred, channel=listen, maxsize=1)
        try:
            await self.write(write_channel, frame)
            return await sub.get(timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MemoTalkTimeout(code) from exc
        finally:
            self.unsubscribe(sub)

    async def expect(
        self,
        predicate: Predicate,
        *,
        channel: Channel = Channel.RECORD,
        timeout: float = 2.0,
    ) -> bytes:
        """Wait for the next frame matching `predicate`, without writing."""
        sub = self.subscribe(predicate, channel=channel, maxsize=1)
        try:
            return await sub.get(timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MemoTalkTimeout() from exc
        finally:
            self.unsubscribe(sub)

    # -- handshake -----------------------------------------------------------
    async def handshake(self, when=None, *, timeout: float = 2.0) -> bool:
        """0x91 RTC sync. The device must receive this within 1s of connecting."""
        ack = await self.request(commands.sync_rtc(when), code=Cmd.RTC_SYNC, timeout=timeout)
        ok = len(ack) >= 6 and parsers.parse_simple_ack(ack)
        self.handshake_done = bool(ok)
        return bool(ok)

    # -- keep-alive ----------------------------------------------------------
    def _start_keepalive(self) -> None:
        if self.keepalive_interval <= 0:
            return
        if self._keepalive_stop is None:
            self._keepalive_stop = asyncio.Event()
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name="memotalk-keepalive"
            )

    async def _stop_keepalive(self) -> None:
        if self._keepalive_stop is not None:
            self._keepalive_stop.set()
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _keepalive_loop(self) -> None:
        assert self._keepalive_stop is not None
        # The peripheral sleeps after ~300s idle; stay well inside that window.
        while True:
            try:
                await asyncio.wait_for(
                    self._keepalive_stop.wait(), timeout=self.keepalive_interval
                )
                return
            except asyncio.TimeoutError:
                pass
            if self._busy or not self.is_connected:
                continue
            try:
                await self.request(
                    commands.read_battery(), code=Cmd.BATTERY, timeout=3.0
                )
            except Exception:
                if not self.is_connected:
                    return

    @contextlib.asynccontextmanager
    async def activity(self):
        """Suppress the keep-alive probe while a transfer owns the link."""
        self._busy += 1
        try:
            yield
        finally:
            self._busy -= 1
