"""The command layer must stay complete: one builder per sendable command code.

The protocol has 25 codes. Three of them never travel from app to device
(0x00 is the device's error frame, 0xA8 / 0x90 are device pushes), so exactly
22 codes are sendable and each of them must have exactly one builder.

This test is what makes "the library does not drop capabilities" executable
instead of a convention nobody enforces.
"""

from __future__ import annotations

import inspect
import unittest

from memotalk import commands
from memotalk.constants import Cmd, RecAction, SettingType, SyncOp

# Codes the device never receives: its own error frame and the two pushes.
NOT_SENDABLE = frozenset({int(Cmd.ERROR), int(Cmd.AUDIO_LEVEL), int(Cmd.AUDIO_STREAM)})

# builder name -> (code, sample arguments)
BUILDERS = {
    "read_battery": (Cmd.BATTERY, ()),
    "read_record_state": (Cmd.RECORD_STATE, ()),
    "read_ftp_host": (Cmd.FTP_HOST, ()),
    "read_ftp_port": (Cmd.FTP_PORT, ()),
    "read_ftp_user": (Cmd.FTP_USER, ()),
    "read_ftp_password": (Cmd.FTP_PASSWORD, ()),
    "read_firmware_version": (Cmd.FIRMWARE, ()),
    "read_wifi_ssid": (Cmd.WIFI_SSID, ()),
    "read_wifi_password": (Cmd.WIFI_PASSWORD, ()),
    "read_has_import_file": (Cmd.HAS_IMPORT_FILE, ()),
    "read_free_space": (Cmd.FREE_SPACE, ()),
    "query_file_list": (Cmd.FILE_LIST, ()),
    "read_device_sn": (Cmd.DEVICE_SN, ()),
    "read_setting": (Cmd.SETTING_READ, (SettingType.SLEEP_TIMEOUT_SEC,)),
    "sync_rtc": (Cmd.RTC_SYNC, ()),
    "control_recording": (Cmd.RECORD_CONTROL, (RecAction.START,)),
    "sync_audio_file": (Cmd.FILE_SYNC, ("A.mp3", 0, SyncOp.START)),
    "set_wifi_ap": (Cmd.WIFI_AP, (True,)),
    "delete_file": (Cmd.DELETE_FILE, ("A.mp3",)),
    "clear_all_files": (Cmd.CLEAR_ALL, (True,)),
    "set_realtime_asr": (Cmd.REALTIME_ASR, (True,)),
    "write_setting": (Cmd.SETTING_WRITE, (SettingType.SLEEP_TIMEOUT_SEC, 300)),
}


class BuilderCoverageTests(unittest.TestCase):
    def test_every_sendable_code_has_a_builder(self):
        sendable = {int(c) for c in Cmd} - NOT_SENDABLE
        covered = {int(code) for code, _ in BUILDERS.values()}
        self.assertEqual(sendable - covered, set())
        self.assertEqual(covered - sendable, set())

    def test_no_duplicate_codes(self):
        codes = [int(code) for code, _ in BUILDERS.values()]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(codes), 22)

    def test_public_api_matches_the_builder_table(self):
        """Every exported builder is covered by the table, and vice versa."""
        exported = {n for n in commands.__all__ if not n.startswith("_")}
        self.assertEqual(exported, set(BUILDERS))

    def test_each_builder_emits_its_own_code(self):
        for name, (code, args) in BUILDERS.items():
            frame = getattr(commands, name)(*args)
            self.assertTrue(frame.startswith(b"\x5a"), f"{name} is not a 0x5A frame")
            self.assertEqual(
                frame[2], int(code), f"{name} built a {frame[2]:02X} frame, want {int(code):02X}"
            )

    def test_no_legacy_aliases_remain(self):
        """The cmd_* aliases were removed; make sure they do not come back."""
        self.assertFalse([n for n in dir(commands) if n.startswith("cmd_")])


class RoutingTests(unittest.TestCase):
    """Routing is hardcoded per call site in client.py, not table driven.

    These lock the two exceptions that a caller is most likely to get wrong.
    """

    def test_no_routing_tables_are_exported(self):
        import memotalk

        self.assertFalse(hasattr(commands, "WRITE_CHANNEL"))
        self.assertFalse(hasattr(commands, "RESPONSE_CHANNEL"))
        self.assertFalse(hasattr(memotalk, "WRITE_CHANNEL"))

    def test_builtin_signatures_are_stable(self):
        """Guards against a builder silently changing its argument order."""
        sigs = {
            "read_setting": ["setting_type"],
            "sync_rtc": ["dt"],
            "control_recording": ["action"],
            "sync_audio_file": ["filename", "start_packet", "operation"],
            "delete_file": ["filename"],
            "clear_all_files": ["confirm"],
            "set_realtime_asr": ["enable"],
            "write_setting": ["setting_type", "value"],
        }
        for name, params in sigs.items():
            got = list(inspect.signature(getattr(commands, name)).parameters)
            self.assertEqual(got, params, name)


class VersionTests(unittest.TestCase):
    """The version lives in two places; they must not drift apart."""

    def test_package_version_matches_pyproject(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        self.assertIsNotNone(match, "no version in pyproject.toml")
        import memotalk

        self.assertEqual(memotalk.__version__, match.group(1))


if __name__ == "__main__":
    unittest.main()
