"""End-to-end client tests against the in-memory simulator.

These exercise the whole stack: client -> transport -> dispatch -> parser ->
typed model, including the retry path and the guaranteed Wi-Fi off-switch.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path

from memotalk import MemoTalkClient
from memotalk.codec import build_frame
from memotalk.constants import Cmd, RecordStateCode, SettingType
from memotalk.errors import (
    ConfirmationRequired,
    ConnectionLost,
    DeleteFailed,
    DeviceErrorResponse,
    FileNotFoundOnDevice,
    MemoTalkTimeout,
)
from memotalk.fake import FakeMemoTalk, FakeTransport, make_file_entry

FILE_NAME = "FE1DDF64551E_20261011233717.mp3"


class ClientTestCase(unittest.IsolatedAsyncioTestCase):
    files = None

    async def asyncSetUp(self):
        self.device = FakeMemoTalk(files=list(self.files or []))
        self.transport = FakeTransport(self.device)
        self.client = await MemoTalkClient(transport=self.transport).connect()

    async def asyncTearDown(self):
        await self.client.close()


class StatusTests(ClientTestCase):
    async def test_handshake_ran_on_connect(self):
        self.assertIn(0x91, [frame[2] for _ch, frame in self.device.writes])

    async def test_battery(self):
        info = await self.client.battery()
        self.assertEqual(info.level, 5)
        self.assertEqual(info.percent, 50)

    async def test_firmware(self):
        info = await self.client.firmware()
        self.assertEqual(info.version, "2.0.6")

    async def test_identity(self):
        ident = await self.client.identity()
        self.assertEqual(ident.serial, "L0m0J06000074305")
        self.assertEqual(ident.format_name, "MP3")

    async def test_free_space(self):
        self.assertEqual((await self.client.free_space()).percent, 99)

    async def test_info_snapshot(self):
        info = await self.client.info()
        self.assertEqual(info.battery.percent, 50)
        self.assertEqual(info.firmware.raw, "206")
        self.assertEqual(info.free_space.percent, 99)
        self.assertEqual(info.record.state, RecordStateCode.STOPPED)
        self.assertEqual(info.settings.sleep_timeout_sec, 300)
        self.assertEqual(info.settings.max_record_minutes, 300)
        self.assertFalse(info.settings.usb_mass_storage)


class FileListTests(ClientTestCase):
    files = [make_file_entry(FILE_NAME, size=1000, duration_sec=5)]

    async def test_list_returns_entries(self):
        entries = await self.client.files.list()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, FILE_NAME)
        self.assertEqual(entries[0].size, 1000)
        self.assertEqual(entries[0].duration_sec, 5)
        self.assertEqual(entries[0].format_name, "MP3")

    async def test_latest(self):
        latest = await self.client.files.latest()
        self.assertEqual(latest.name, FILE_NAME)

    async def test_download_roundtrip(self):
        entry = (await self.client.files.list())[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = await self.client.files.download(entry, tmp)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), self.device._file_bytes(entry))
            self.assertEqual(path.stat().st_size, 1000)

    async def test_download_refuses_overwrite(self):
        entry = (await self.client.files.list())[0]
        with tempfile.TemporaryDirectory() as tmp:
            await self.client.files.download(entry, tmp)
            with self.assertRaises(FileExistsError):
                await self.client.files.download(entry, tmp)
            # overwrite=True succeeds
            await self.client.files.download(entry, tmp, overwrite=True)

    async def test_download_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundOnDevice):
                await self.client.files.download("ghost.mp3", tmp, size=100)

    async def test_delete_existing(self):
        self.assertTrue(await self.client.files.delete(FILE_NAME))
        self.assertEqual(self.device.files, [])
        self.assertEqual(await self.client.files.list(), [])

    async def test_delete_missing_raises(self):
        with self.assertRaises(DeleteFailed):
            await self.client.files.delete("ghost.mp3")

    async def test_clear_all_requires_confirm(self):
        with self.assertRaises(ConfirmationRequired):
            await self.client.files.clear_all()

    async def test_clear_all_confirmed(self):
        self.assertTrue(await self.client.files.clear_all(confirm=True))
        self.assertEqual(self.device.files, [])


class EmptyDeviceTests(ClientTestCase):
    async def test_empty_list(self):
        self.assertEqual(await self.client.files.list(), [])

    async def test_clear_all_on_empty_device(self):
        self.assertTrue(await self.client.files.clear_all(confirm=True))


class DownloadResumeTests(ClientTestCase):
    """The flagship behaviour: a dropped 0x84 packet must not fail the transfer."""

    files = [make_file_entry(FILE_NAME, size=1000, duration_sec=5)]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.device.drop_sequences = {1}

    async def test_resumes_after_dropped_packet(self):
        entry = (await self.client.files.list())[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = await self.client.files.download(
                entry, tmp, packet_timeout=0.2, ack_timeout=2.0, max_retries=3
            )
            self.assertEqual(path.read_bytes(), self.device._file_bytes(entry))
        self.assertGreaterEqual(self.device.sync_attempts, 2)


class RecordingTests(ClientTestCase):
    async def test_start_and_stop(self):
        started = await self.client.record.start()
        self.assertEqual(started.state, RecordStateCode.RECORDING)
        self.assertTrue(started.success)
        self.assertTrue(started.is_recording)
        self.assertTrue(started.filename.endswith(".mp3"))

        stopped = await self.client.record.stop()
        self.assertEqual(stopped.state, RecordStateCode.STOPPED)

    async def test_pause_and_resume(self):
        await self.client.record.start()
        paused = await self.client.record.pause()
        self.assertEqual(paused.state, RecordStateCode.PAUSED)
        resumed = await self.client.record.resume()
        self.assertEqual(resumed.state, RecordStateCode.RESUMED)

    async def test_realtime_switch(self):
        ack = await self.client.record.set_realtime(True)
        self.assertTrue(ack.success)
        self.assertTrue(self.device.realtime_asr)
        await self.client.record.set_realtime(False)
        self.assertFalse(self.device.realtime_asr)

    async def test_stream_level(self):
        levels = []

        async def consume():
            async for level in self.client.stream.level():
                levels.append(level.db)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        self.transport.emit_level(-12.5)
        self.transport.emit_level(-30.0)
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        self.assertEqual(len(levels), 2)
        self.assertAlmostEqual(levels[0], -12.5, places=3)
        self.assertAlmostEqual(levels[1], -30.0, places=3)

    async def test_stream_audio(self):
        chunks = []

        async def consume():
            async for chunk in self.client.stream.audio():
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        payload = bytes(range(96)) * 3
        self.transport.emit_audio(payload)
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 288)
        self.assertEqual(chunks[0], payload)


class SettingsTests(ClientTestCase):
    async def test_read_all(self):
        settings = await self.client.settings.read_all()
        self.assertEqual(settings.sleep_timeout_sec, 300)
        self.assertEqual(settings.max_record_minutes, 300)
        self.assertFalse(settings.usb_mass_storage)

    async def test_write_sleep_timeout(self):
        self.assertTrue(await self.client.settings.sleep_timeout(600))
        self.assertEqual(self.device.settings[int(SettingType.SLEEP_TIMEOUT_SEC)], 600)
        self.assertEqual(await self.client.settings.sleep_timeout(), 600)

    async def test_write_max_record(self):
        await self.client.settings.max_record_minutes(120)
        self.assertEqual(self.device.settings[int(SettingType.MAX_RECORD_MINUTES)], 120)

    async def test_write_usb(self):
        await self.client.settings.usb_mass_storage(True)
        self.assertEqual(self.device.settings[int(SettingType.USB_MASS_STORAGE)], 1)
        self.assertTrue(await self.client.settings.usb_mass_storage())


class WifiTests(ClientTestCase):
    async def test_enable_reads_credentials_then_disables(self):
        await self.client.wifi.enable(ready_delay=0)
        self.assertTrue(self.device.wifi_on)

        creds = await self.client.wifi.credentials()
        self.assertEqual(creds.ssid, "MemoTalk-S1-TEST")
        self.assertEqual(creds.password, "TESTwifi")

        ftp = await self.client.wifi.ftp()
        self.assertTrue(ftp.ready)
        self.assertEqual(ftp.host, "192.168.4.1")
        self.assertEqual(ftp.port, 2121)
        self.assertEqual(ftp.user, "fulltech")
        self.assertEqual(ftp.password, "ESqn3Dvc")

        await self.client.wifi.disable()
        self.assertFalse(self.device.wifi_on)

    async def test_session_turns_hotspot_off(self):
        async with self.client.wifi.session(ready_delay=0) as net:
            self.assertEqual(net.ssid, "MemoTalk-S1-TEST")
            self.assertTrue(net.ftp_config.ready)
            self.assertTrue(self.device.wifi_on)
        self.assertFalse(self.device.wifi_on)

    async def test_session_turns_hotspot_off_on_error(self):
        with self.assertRaises(RuntimeError):
            async with self.client.wifi.session(ready_delay=0):
                raise RuntimeError("boom")
        self.assertFalse(self.device.wifi_on)

    async def test_ftp_not_ready_before_hotspot(self):
        from memotalk.client import NetworkSession
        from memotalk.models import WifiCredentials

        ftp = await self.client.wifi.ftp()
        self.assertFalse(ftp.ready)
        session = NetworkSession(
            credentials=WifiCredentials(ssid="s", password="p"), ftp_config=ftp
        )
        with self.assertRaises(ConnectionError):
            session.open_ftp()


class ErrorMappingTests(ClientTestCase):
    async def test_unknown_command_raises_device_error(self):
        with self.assertRaises(DeviceErrorResponse) as ctx:
            await self.client._call(build_frame(0x77), 0x77)
        self.assertEqual(ctx.exception.code, 0x04)

    async def test_dead_link_raises_connection_lost(self):
        await self.transport.disconnect()
        with self.assertRaises(ConnectionLost):
            await self.client.files.list()


class RawAccessTests(ClientTestCase):
    async def test_raw_request(self):
        reply = await self.client.raw.request(
            build_frame(0x01), code=0x01, timeout=2.0
        )
        self.assertEqual(reply[2], 0x01)
        self.assertEqual(reply[3], 5)

    async def test_client_repr(self):
        self.assertIn("connected", repr(self.client))


class WifiCloseVerificationTests(ClientTestCase):
    """Firmware 206 quirks: an ack can lie, and silence can mean "still on"."""

    async def test_ack_failure_but_confirmed_off(self):
        await self.client.wifi.enable(ready_delay=0)
        self.device.wifi_close_ack_success = False
        ack = await self.client.wifi.disable()
        self.assertTrue(ack.success)
        self.assertFalse(self.device.wifi_on)
        self.assertTrue(self.client.wifi.last_close_report["confirmed"])

    async def test_retries_through_silence(self):
        await self.client.wifi.enable(ready_delay=0)
        self.device.wifi_ignore_close = 1
        ack = await self.client.wifi.disable(timeout=1.0, attempts=3)
        self.assertTrue(ack.success)
        self.assertFalse(self.device.wifi_on)
        report = self.client.wifi.last_close_report
        self.assertTrue(report["confirmed"])
        self.assertGreaterEqual(report["attempts"], 2)

    async def test_hotspot_state_probe(self):
        self.assertFalse(await self.client.wifi.hotspot_state())
        await self.client.wifi.enable(ready_delay=0)
        self.assertTrue(await self.client.wifi.hotspot_state())
        await self.client.wifi.disable()
        self.assertFalse(await self.client.wifi.hotspot_state())

    async def test_session_records_confirmed_close(self):
        async with self.client.wifi.session(ready_delay=0) as net:
            self.assertTrue(self.device.wifi_on)
            self.assertEqual(net.ssid, "MemoTalk-S1-TEST")
        self.assertFalse(self.device.wifi_on)
        self.assertTrue(self.client.wifi.last_close_report["confirmed"])

    async def test_session_cleans_up_when_enable_fails(self):
        original = self.transport.write

        async def flaky(channel, frame):
            if len(frame) >= 4 and frame[2] == int(Cmd.WIFI_AP) and frame[3] == 0x01:
                await original(channel, frame)  # device really did switch it on
                raise MemoTalkTimeout(Cmd.WIFI_AP)
            return await original(channel, frame)

        self.transport.write = flaky
        try:
            with self.assertRaises(MemoTalkTimeout):
                async with self.client.wifi.session(ready_delay=0):
                    pass
        finally:
            self.transport.write = original
        self.assertFalse(self.device.wifi_on)


class DownloadSilenceTests(ClientTestCase):
    """A missing 0x84 target must surface as "not found", not as a timeout."""

    files = [make_file_entry(FILE_NAME, size=1000)]

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.device.silent_on_missing_file = True

    async def test_missing_file_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundOnDevice):
                await self.client.files.download(
                    "ghost.mp3", tmp, size=100, ack_timeout=0.6
                )

    async def test_existing_file_still_downloads(self):
        entry = (await self.client.files.list())[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = await self.client.files.download(entry, tmp)
            self.assertEqual(path.read_bytes(), self.device._file_bytes(entry))


if __name__ == "__main__":
    unittest.main()
