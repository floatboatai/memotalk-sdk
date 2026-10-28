"""Parser tests driven by real captures from the protocol spec.

Each hex literal below is a frame the vendor recorded on hardware, so these
tests assert the parser against reality rather than against my own encoder.
"""

from __future__ import annotations

import datetime as dt
import unittest

from memotalk import parsers
from memotalk.codec import build_frame
from memotalk.constants import Cmd, DeviceErrorCode, RecordResult, RecordStateCode, SettingType, SyncOp
from memotalk.errors import DeviceErrorResponse, InvalidParameter, ProtocolError

# --- captured frames --------------------------------------------------------
BATTERY = "5A040105F192"
FIRMWARE = "5A06093230369A45"
IDENTITY = "5A15144C306D304A30363030303037343330350000B26A"
FREE_SPACE = "5A050F01637152"
HAS_IMPORT = "5A040D01F551"
RECORD_STATE = "5A12020001FE1DDF64551E2025060700000011CA"
SSID = "5A130B4D656D6F54616C6B2D53312D4A7A59560D56"
WIFI_PASSWORD = "5A0B0CB1A6868CCCBEAE9C7554"
FTP_HOST = "5A0E033139322E3136382E342E31E4BA"
FTP_PORT = "5A0704323132317BAF"
FTP_USER = "5A0B0566756C6C7465636827E3"
FTP_PASSWORD = "5A0B06BAAC8E91CCBB899C06FC"
SETTING_SLEEP = "5A07B100012C01F176"
SETTING_MAXREC = "5A07B101012C01F08A"
SETTING_USB_OFF = "5A06B10201002B6C"
SETTING_USB_ON = "5A06B1020101EAAC"
ACK_WIFI_ON = "5A058701017091"
ACK_WIFI_OFF = "5A058700017101"
ACK_CLEAR = "5A049A019B61"
ACK_RTC = "5A0491019C51"
ACK_SETTING = "5A04B0018401"
ACK_A4_ON = "5A05A40101815B"
ACK_A4_OFF = "5A05A4000180CB"
DELETE_OK = "5A0492019CA1"
DELETE_FAIL = "5A05920004A0C6"
AUDIO_LEVEL = "5A07A8688872C1190E"
SYNC_ACK = "5A288401" + "FE1DDF64551E_20261011233717.mp3".encode().hex().upper() + "0000020D007AF5"


def h(text: str) -> bytes:
    return bytes.fromhex(text)


class ReadParserTests(unittest.TestCase):
    def test_battery(self):
        info = parsers.parse_battery(h(BATTERY))
        self.assertEqual(info.level, 5)
        self.assertEqual(info.percent, 50)

    def test_firmware(self):
        info = parsers.parse_firmware(h(FIRMWARE))
        self.assertEqual(info.raw, "206")
        self.assertEqual(info.version, "2.0.6")

    def test_identity(self):
        info = parsers.parse_device_identity(h(IDENTITY))
        self.assertEqual(info.serial, "L0m0J06000074305")
        self.assertEqual(info.format_code, 0x00)
        self.assertEqual(info.format_name, "MP3")
        self.assertFalse(info.realtime_asr_enabled)

    def test_free_space(self):
        info = parsers.parse_free_space(h(FREE_SPACE))
        self.assertTrue(info.success)
        self.assertEqual(info.percent, 99)

    def test_has_import_file(self):
        self.assertTrue(parsers.parse_has_import_file(h(HAS_IMPORT)))


class RecordStateTests(unittest.TestCase):
    def test_captured_frame(self):
        state = parsers.parse_record_state(h(RECORD_STATE))
        self.assertEqual(state.state, RecordStateCode.STOPPED)
        self.assertEqual(state.result, RecordResult.SUCCESS)
        self.assertEqual(state.mac, "FE1DDF64551E")
        self.assertEqual(state.bcd_time, "20250607000000")
        self.assertEqual(state.filename, "FE1DDF64551E_20250607000000.mp3")
        self.assertEqual(state.recorded_at, dt.datetime(2025, 6, 7, 0, 0, 0))
        self.assertTrue(state.success)
        self.assertFalse(state.is_recording)

    def test_failure_result(self):
        frame = build_frame(Cmd.RECORD_CONTROL, bytes([0x01, 0xFF]) + bytes.fromhex("FE1DDF64551E") + bytes(7))
        state = parsers.parse_record_state(frame)
        self.assertEqual(state.result, RecordResult.STORAGE_FULL)
        self.assertFalse(state.success)


class CredentialParserTests(unittest.TestCase):
    def test_ssid(self):
        self.assertEqual(parsers.parse_text(h(SSID)), "MemoTalk-S1-JzYV")

    def test_wifi_password_is_obfuscated(self):
        self.assertEqual(parsers.parse_secret(h(WIFI_PASSWORD)), "NYys3AQc")

    def test_ftp_fields(self):
        self.assertEqual(parsers.parse_text(h(FTP_HOST)), "192.168.4.1")
        self.assertEqual(parsers.parse_text(h(FTP_PORT)), "2121")
        self.assertEqual(parsers.parse_text(h(FTP_USER)), "fulltech")
        self.assertEqual(parsers.parse_secret(h(FTP_PASSWORD)), "ESqn3Dvc")

    def test_composite_ftp(self):
        cfg = parsers.parse_ftp(h(FTP_HOST), h(FTP_PORT), h(FTP_USER), h(FTP_PASSWORD))
        self.assertTrue(cfg.ready)
        self.assertEqual(cfg.address, "192.168.4.1:2121")

    def test_not_ready_hotspot(self):
        dead = build_frame(Cmd.FTP_HOST, b"0.0.0.0")
        cfg = parsers.parse_ftp(dead, build_frame(Cmd.FTP_PORT, b"0"), h(FTP_USER), h(FTP_PASSWORD))
        self.assertFalse(cfg.ready)

    def test_composite_wifi(self):
        creds = parsers.parse_wifi_credentials(h(SSID), h(WIFI_PASSWORD))
        self.assertEqual(creds.ssid, "MemoTalk-S1-JzYV")
        self.assertEqual(creds.password, "NYys3AQc")


class SettingParserTests(unittest.TestCase):
    def test_numeric_settings(self):
        sleep = parsers.parse_setting(h(SETTING_SLEEP), SettingType.SLEEP_TIMEOUT_SEC)
        self.assertTrue(sleep.success)
        self.assertEqual(sleep.value, 300)

        maxrec = parsers.parse_setting(h(SETTING_MAXREC), SettingType.MAX_RECORD_MINUTES)
        self.assertEqual(maxrec.value, 300)

    def test_boolean_setting(self):
        off = parsers.parse_setting(h(SETTING_USB_OFF), SettingType.USB_MASS_STORAGE)
        self.assertEqual(off.value, 0)
        on = parsers.parse_setting(h(SETTING_USB_ON), SettingType.USB_MASS_STORAGE)
        self.assertEqual(on.value, 1)

    def test_type_mismatch_is_rejected(self):
        with self.assertRaises(ProtocolError):
            parsers.parse_setting(h(SETTING_SLEEP), SettingType.MAX_RECORD_MINUTES)


class AckParserTests(unittest.TestCase):
    def test_ack_result(self):
        on = parsers.parse_ack_result(h(ACK_WIFI_ON))
        self.assertEqual(on.state, 0x01)
        self.assertTrue(on.success)
        self.assertTrue(bool(on))
        # Off is still a *successful* command: state 0, result 1.
        off = parsers.parse_ack_result(h(ACK_WIFI_OFF))
        self.assertEqual(off.state, 0x00)
        self.assertTrue(off.success)

    def test_simple_acks(self):
        for frame in (ACK_CLEAR, ACK_RTC, ACK_SETTING):
            with self.subTest(frame=frame):
                self.assertTrue(parsers.parse_simple_ack(h(frame)))

    def test_realtime_ack(self):
        self.assertTrue(parsers.parse_ack_result(h(ACK_A4_ON)).state == 1)
        self.assertTrue(parsers.parse_ack_result(h(ACK_A4_OFF)).state == 0)

    def test_delete(self):
        self.assertIs(parsers.parse_delete_result(h(DELETE_OK)), True)
        self.assertEqual(parsers.parse_delete_result(h(DELETE_FAIL)), 0x04)


class PushParserTests(unittest.TestCase):
    def test_audio_level(self):
        level = parsers.parse_audio_level(h(AUDIO_LEVEL))
        self.assertAlmostEqual(level.db, -15.158, places=2)
        self.assertGreaterEqual(level.normalized, 0.0)
        self.assertLessEqual(level.normalized, 1.0)

    def test_level_normalization(self):
        from memotalk.models import AudioLevel

        self.assertEqual(AudioLevel(-120.0).normalized, 0.0)
        self.assertEqual(AudioLevel(0.0).normalized, 1.0)
        self.assertAlmostEqual(AudioLevel(-30.0).normalized, 0.5)

    def test_audio_stream(self):
        chunk = bytes(range(96)) * 3
        frame = b"\x5a" + b"\x23\x01\x00" + b"\x90" + chunk + b"\x00\x00"
        self.assertEqual(len(frame), 295)
        self.assertEqual(parsers.parse_audio_stream(frame), chunk)

    def test_wrong_length_rejected(self):
        with self.assertRaises(ProtocolError):
            parsers.parse_audio_stream(b"\x5a\x23\x01\x00\x90" + b"\x00" * 10)


class SyncAckTests(unittest.TestCase):
    def test_captured_ack(self):
        ack = parsers.parse_sync_ack(h(SYNC_ACK))
        self.assertTrue(ack.exists)
        self.assertEqual(ack.filename, "FE1DDF64551E_20261011233717.mp3")
        self.assertEqual(ack.total_packets, 525)
        self.assertEqual(ack.operation, SyncOp.START)
        self.assertEqual(ack.error_code, 0x00)

    # Captured on hardware (firmware 206): a failure ack carries only a 3-byte
    # tail (status, error, op) instead of the success layout's 5 bytes.
    FAIL_6 = "5A0D840041422E6D70330004000669"
    FAIL_20 = "5A1B84004E4F545F45584953545F3030303030302E6D7033000400B600"
    FAIL_34 = "5A2984004141414141414141414141414141414141414141414141414141414141412E6D70330004003EF7"

    def _assert_failure(self, hexstr, expected_name):
        ack = parsers.parse_sync_ack(h(hexstr))
        self.assertFalse(ack.exists)
        self.assertEqual(ack.filename, expected_name)
        self.assertEqual(ack.error_code, 0x04)
        self.assertEqual(ack.total_packets, 0)
        self.assertEqual(ack.operation, SyncOp.START)

    def test_missing_file_ack_short_name(self):
        self._assert_failure(self.FAIL_6, "AB.mp3")

    def test_missing_file_ack_20_byte_name(self):
        self._assert_failure(self.FAIL_20, "NOT_EXIST_000000.mp3")

    def test_missing_file_ack_34_byte_name(self):
        self._assert_failure(self.FAIL_34, "A" * 30 + ".mp3")

    def test_success_and_failure_tails_differ(self):
        ok = parsers.parse_sync_ack(h(SYNC_ACK))
        bad = parsers.parse_sync_ack(h(self.FAIL_20))
        self.assertEqual(len(ok.raw), len(ok.filename.encode()) + 11)
        self.assertEqual(len(bad.raw), len(bad.filename.encode()) + 9)

    def test_failure_ack_crc_is_valid(self):
        # The tail layout is unusual, but the frames themselves are well formed.
        from memotalk.codec import verify_crc

        for hexstr in (self.FAIL_6, self.FAIL_20, self.FAIL_34):
            with self.subTest(size=len(hexstr) // 2):
                self.assertTrue(verify_crc(h(hexstr)))


class ErrorFrameTests(unittest.TestCase):
    def test_error_code(self):
        frame = build_frame(Cmd.ERROR, b"\x03")
        self.assertEqual(parsers.parse_error(frame), DeviceErrorCode.OPERATION_FORBIDDEN)

    def test_error_with_echo(self):
        frame = build_frame(Cmd.ERROR, bytes([0x04, 0x77]))
        self.assertEqual(parsers.parse_error(frame), DeviceErrorCode.INVALID_COMMAND)

    def test_error_frame_echoed_keeps_the_offending_command(self):
        # 0x04 echoes the rejected command after the error code byte.
        frame = build_frame(Cmd.ERROR, bytes([0x04, 0x77]))
        err = DeviceErrorResponse.from_frame(frame)
        self.assertEqual(err.code, 0x04)
        self.assertEqual(err.echoed, bytes([0x77]))
        self.assertEqual(err.echoed, err.payload)

    def test_error_frame_echoed_keeps_the_offending_parameters(self):
        # 0x05 echoes the rejected parameter(s) after the error code byte.
        frame = build_frame(Cmd.ERROR, bytes([0x05, 0x02, 0x00]))
        err = InvalidParameter.from_frame(frame)
        self.assertEqual(err.code, 0x05)
        self.assertEqual(err.echoed, bytes([0x02, 0x00]))


class RobustnessTests(unittest.TestCase):
    def test_crc_corruption_is_caught(self):
        broken = bytearray(h(BATTERY))
        broken[-1] ^= 0xFF
        with self.assertRaises(Exception):
            parsers.parse_battery(bytes(broken))

    def test_truncated_frame_is_caught(self):
        with self.assertRaises(ProtocolError):
            parsers.parse_battery(h(BATTERY)[:4])

    def test_unknown_code(self):
        with self.assertRaises(ProtocolError):
            parsers.parse_response(build_frame(0x77))

    def test_dispatch_registry(self):
        from memotalk.constants import Cmd as _Cmd

        self.assertEqual(len(parsers.PARSERS), 21)
        self.assertEqual(int(_Cmd.FILE_SYNC) not in parsers.PARSERS, True)


if __name__ == "__main__":
    unittest.main()
