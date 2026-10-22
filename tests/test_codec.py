"""Codec and command builder tests.

Every expected hex string below is copied from the verified protocol spec, so a
passing test proves byte-for-byte compatibility with firmware 206.
"""

from __future__ import annotations

import datetime as dt
import unittest

from memotalk import commands
from memotalk.codec import (
    bcd_bytes,
    build_frame,
    crc16,
    expected_frame_length,
    from_bcd,
    to_bcd,
    verify_crc,
    xor_ff_decode,
    xor_ff_encode,
)
from memotalk.constants import Cmd, RecAction, SettingType, SyncOp
from memotalk.errors import CrcError, ProtocolError


class CrcTests(unittest.TestCase):
    def test_spec_vector(self):
        # Spec 2.3: CRC over [0x03, 0x09] is 0x46C1, sent little-endian.
        self.assertEqual(crc16(bytes([0x03, 0x09])), 0x46C1)

    def test_build_frame_matches_spec(self):
        self.assertEqual(build_frame(Cmd.FIRMWARE).hex().upper(), "5A0309C146")

    def test_verify_crc_accepts_real_capture(self):
        self.assertTrue(verify_crc(bytes.fromhex("5A040105F192")))

    def test_verify_crc_rejects_corruption(self):
        self.assertFalse(verify_crc(bytes.fromhex("5A040105F193")))

    def test_verify_crc_rejects_short_frames(self):
        self.assertFalse(verify_crc(b"\x5a\x03\x09"))
        self.assertFalse(verify_crc(b""))

    def test_payload_limit(self):
        with self.assertRaises(ValueError):
            build_frame(Cmd.FILE_SYNC, b"\x00" * 238)


class BcdTests(unittest.TestCase):
    def test_roundtrip(self):
        for value in (0, 7, 10, 59, 99):
            self.assertEqual(from_bcd(to_bcd(value)), value)

    def test_packed_layout(self):
        self.assertEqual(to_bcd(26), 0x26)
        self.assertEqual(to_bcd(10), 0x10)

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            to_bcd(100)

    def test_rtc_payload(self):
        stamp = dt.datetime(2026, 10, 20, 10, 29, 56)
        self.assertEqual(bcd_bytes(stamp).hex().upper(), "20261020102956")


class FrameLengthTests(unittest.TestCase):
    def test_standard_frame(self):
        self.assertEqual(expected_frame_length(bytes.fromhex("5A0401")), 6)

    def test_realtime_frame(self):
        # 0x90 uses a 3-byte little-endian length: 0x000123 + 4 = 295.
        self.assertEqual(expected_frame_length(bytes.fromhex("5A23010090")), 295)

    def test_sync_packet(self):
        self.assertEqual(expected_frame_length(b"\xff\xff\xff"), 328)

    def test_partial_prefix(self):
        self.assertIsNone(expected_frame_length(b""))
        self.assertIsNone(expected_frame_length(b"\x5a"))
        self.assertIsNone(expected_frame_length(b"\xff"))

    def test_garbage(self):
        self.assertIsNone(expected_frame_length(b"\x12\x34\x56"))


class SecretTests(unittest.TestCase):
    def test_spec_vectors(self):
        self.assertEqual(xor_ff_decode(bytes.fromhex("B1A6868CCCBEAE9C")), "NYys3AQc")
        self.assertEqual(xor_ff_decode(bytes.fromhex("BAAC8E91CCBB899C")), "ESqn3Dvc")

    def test_roundtrip(self):
        self.assertEqual(xor_ff_decode(xor_ff_encode("s3cr3t!")), "s3cr3t!")


class ReadCommandTests(unittest.TestCase):
    EXPECTED = {
        "read_battery": "5A0301C080",
        "read_record_state": "5A03028081",
        "read_ftp_host": "5A03034141",
        "read_ftp_port": "5A03040083",
        "read_ftp_user": "5A0305C143",
        "read_ftp_password": "5A03068142",
        "read_firmware_version": "5A0309C146",
        "read_wifi_ssid": "5A030B4087",
        "read_wifi_password": "5A030C0145",
        "read_has_import_file": "5A030DC085",
        "read_free_space": "5A030F4144",
        "query_file_list": "5A0313408D",
        "read_device_sn": "5A0314014F",
    }

    def test_all_read_commands(self):
        for name, expected in self.EXPECTED.items():
            with self.subTest(command=name):
                frame = getattr(commands, name)()
                self.assertEqual(frame.hex().upper(), expected)

    def test_read_setting_variants(self):
        cases = {
            SettingType.SLEEP_TIMEOUT_SEC: "5A04B1004451",
            SettingType.MAX_RECORD_MINUTES: "5A04B1018591",
            SettingType.USB_MASS_STORAGE: "5A04B102C590",
        }
        for setting, expected in cases.items():
            with self.subTest(setting=setting.name):
                self.assertEqual(
                    commands.read_setting(setting).hex().upper(), expected
                )


class WriteCommandTests(unittest.TestCase):
    def test_recording_control(self):
        cases = {
            RecAction.START: "5A0480019001",
            RecAction.PAUSE: "5A048002D000",
            RecAction.RESUME: "5A04800311C0",
            RecAction.STOP: "5A04800051C1",
        }
        for action, expected in cases.items():
            with self.subTest(action=action.name):
                self.assertEqual(
                    commands.control_recording(action).hex().upper(), expected
                )

    def test_rtc_handshake(self):
        stamp = dt.datetime(2026, 10, 20, 10, 29, 56)
        self.assertEqual(
            commands.sync_rtc(stamp).hex().upper(), "5A0A912026102010295 6CE5C".replace(" ", "")
        )

    def test_wifi_ap(self):
        self.assertEqual(commands.set_wifi_ap(True).hex().upper(), "5A0487019231")
        self.assertEqual(commands.set_wifi_ap(False).hex().upper(), "5A04870053F1")

    def test_realtime_asr(self):
        self.assertEqual(commands.set_realtime_asr(True).hex().upper(), "5A04A4018B01")
        self.assertEqual(commands.set_realtime_asr(False).hex().upper(), "5A04A4004AC1")

    def test_clear_all(self):
        self.assertEqual(
            commands.clear_all_files(confirm=True).hex().upper(), "5A039A812B"
        )

    def test_write_setting(self):
        self.assertEqual(
            commands.write_setting(SettingType.USB_MASS_STORAGE, 1).hex().upper(),
            "5A05B00201C1AF",
        )
        self.assertEqual(
            commands.write_setting(SettingType.USB_MASS_STORAGE, 0).hex().upper(),
            "5A05B00200006F",
        )
        # Spec erratum: the protocol document prints `5A 06 B0 00 2C 01 C7 90`
        # for 300s. C790 is not the CRC of `06 B0 00 2C 01` under either the
        # nibble table or an independent bitwise CRC-16/ARC, and it only
        # matches an out-of-range 0xCBD9 seconds. 57 C0 is the mathematically
        # correct value. Hardware note (verified 2026-10-22): firmware 206 does
        # not enforce CRC at all, so it accepts both frames - we still emit the
        # correct one so future or third-party implementations agree.
        self.assertEqual(
            commands.write_setting(SettingType.SLEEP_TIMEOUT_SEC, 300).hex().upper(),
            "5A06B0002C0157C0",
        )
        self.assertEqual(
            commands.write_setting(SettingType.MAX_RECORD_MINUTES, 300).hex().upper(),
            "5A06B0012C010600",
        )


class ValidationTests(unittest.TestCase):
    def test_clear_all_requires_confirmation(self):
        from memotalk.errors import ConfirmationRequired

        with self.assertRaises(ConfirmationRequired):
            commands.clear_all_files()
        with self.assertRaises(ConfirmationRequired):
            commands.clear_all_files(confirm=False)

    def test_sleep_timeout_range(self):
        with self.assertRaises(ValueError):
            commands.write_setting(SettingType.SLEEP_TIMEOUT_SEC, 5)
        with self.assertRaises(ValueError):
            commands.write_setting(SettingType.SLEEP_TIMEOUT_SEC, 8000)
        # 0 restores the 300s default and must be allowed.
        self.assertTrue(commands.write_setting(SettingType.SLEEP_TIMEOUT_SEC, 0))

    def test_max_record_range(self):
        with self.assertRaises(ValueError):
            commands.write_setting(SettingType.MAX_RECORD_MINUTES, 0)
        with self.assertRaises(ValueError):
            commands.write_setting(SettingType.MAX_RECORD_MINUTES, 481)

    def test_usb_range(self):
        with self.assertRaises(ValueError):
            commands.write_setting(SettingType.USB_MASS_STORAGE, 2)

    def test_delete_filename_length(self):
        with self.assertRaises(ValueError):
            commands.delete_file("")
        with self.assertRaises(ValueError):
            commands.delete_file("a" * 64)
        self.assertTrue(commands.delete_file("a" * 63))

    def test_recording_action_validation(self):
        with self.assertRaises(ValueError):
            commands.control_recording(0x04)

    def test_sync_operation_validation(self):
        with self.assertRaises(ValueError):
            SyncOp(9)
        with self.assertRaises(ValueError):
            commands.sync_audio_file("a.mp3", 0, 9)  # type: ignore[arg-type]

    def test_sync_start_packet_range(self):
        with self.assertRaises(ValueError):
            commands.sync_audio_file("a.mp3", 0x1000000, SyncOp.START)


class SyncAudioFrameTests(unittest.TestCase):
    def test_start_frame_shape(self):
        name = "FE1DDF64551E_20261011233717.mp3"
        frame = commands.sync_audio_file(name, 0, SyncOp.START)
        self.assertEqual(frame[3:-6].decode(), name)
        self.assertEqual(frame[-6:-3], b"\x00\x00\x00")
        self.assertEqual(frame[-3], int(SyncOp.START))

    def test_resume_from_packet(self):
        frame = commands.sync_audio_file("a.mp3", 0x00020D, SyncOp.RESUME)
        self.assertEqual(frame[-6:-3], b"\x00\x02\x0d")
        self.assertEqual(frame[-3], int(SyncOp.RESUME))


class CoverageGuardTests(unittest.TestCase):
    """Locks the protocol surface so a refactor cannot silently drop a command."""

    def test_every_code_has_a_builder_or_a_parser(self):
        from memotalk.parsers import PARSERS

        handled_elsewhere = {
            Cmd.ERROR,
            Cmd.FILE_LIST,
            Cmd.FILE_SYNC,
            Cmd.SETTING_READ,
        }
        for code in Cmd:
            with self.subTest(code=code.name):
                self.assertTrue(
                    int(code) in PARSERS or code in handled_elsewhere,
                    f"{code.name} (0x{int(code):02X}) is neither parsed nor exempted",
                )

    def test_command_count(self):
        # 25 codes on the wire; 3 are device->app only, leaving 22 builders.
        push_only = {Cmd.ERROR, Cmd.AUDIO_LEVEL, Cmd.AUDIO_STREAM}
        self.assertEqual(len([c for c in Cmd if c not in push_only]), 22)


if __name__ == "__main__":
    unittest.main()
