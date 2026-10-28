"""Assembler tests: notify defragmentation and both multi-packet streams."""

from __future__ import annotations

import unittest

from memotalk.assemblers import AudioSyncAssembler, FileListAssembler, FrameReassembler
from memotalk.constants import SYNC_PACKET_TOTAL
from memotalk.errors import ProtocolError
from memotalk.fake import build_file_list_frames, build_sync_ack, build_sync_packet, make_file_entry


class FrameReassemblerTests(unittest.TestCase):
    def test_standard_frames_in_one_notification(self):
        stream = bytes.fromhex("5A040105F192") + bytes.fromhex("5A0491019C51")
        frames = FrameReassembler("control").feed(stream)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].hex().upper(), "5A040105F192")
        self.assertEqual(frames[1].hex().upper(), "5A0491019C51")

    def test_split_across_notifications(self):
        frame = bytes.fromhex("5A040105F192")
        machine = FrameReassembler("control")
        self.assertEqual(machine.feed(frame[:3]), [])
        self.assertEqual(machine.pending, 3)
        out = machine.feed(frame[3:])
        self.assertEqual(len(out), 1)
        self.assertEqual(machine.pending, 0)

    def test_file_list_passthrough(self):
        pkt = bytes.fromhex("FF000513 0000300D".replace(" ", ""))
        frames = FrameReassembler("control").feed(pkt)
        self.assertEqual(frames, [pkt])

    def test_realtime_frame_split(self):
        """The 295-byte 0x90 frame is the one that fragments on small-MTU hosts."""
        chunk = bytes(range(96)) * 3
        frame = b"\x5a" + b"\x23\x01\x00" + b"\x90" + chunk + b"\x00\x00"
        machine = FrameReassembler("record")
        out = []
        for start in range(0, len(frame), 100):
            out.extend(machine.feed(frame[start : start + 100]))
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), 295)
        self.assertEqual(out[0][5:293], chunk)

    def test_sync_packet_split(self):
        frame = build_sync_packet(7, b"\xaa" * 320)
        self.assertEqual(len(frame), SYNC_PACKET_TOTAL)
        machine = FrameReassembler("record")
        out = []
        for start in range(0, len(frame), 64):
            out.extend(machine.feed(frame[start : start + 64]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], frame)

    def test_resync_after_garbage(self):
        machine = FrameReassembler("control")
        frames = machine.feed(b"\x00\x11" + bytes.fromhex("5A040105F192"))
        self.assertEqual(len(frames), 1)
        self.assertEqual(machine.dropped_bytes, 2)

    def test_bad_kind(self):
        with self.assertRaises(ValueError):
            FrameReassembler("nope")


class FileListAssemblerTests(unittest.TestCase):
    def test_empty_list_real_capture(self):
        asm = FileListAssembler()
        self.assertTrue(asm.feed(bytes.fromhex("FF000513 0000300D".replace(" ", ""))))
        self.assertEqual(asm.files, [])
        self.assertTrue(asm.is_complete)

    def test_single_packet_list(self):
        entries = [
            make_file_entry("FE1DDF64551E_20261011233717.mp3", size=4096, duration_sec=12),
            make_file_entry("FE1DDF64551E_20261012090000.mp3", size=3200, duration_sec=8),
        ]
        frames = build_file_list_frames(entries)
        # Two 31-char names fit in one 120-byte packet.
        self.assertEqual(len(frames), 1)

        asm = FileListAssembler()
        self.assertTrue(asm.feed(frames[0]))
        self.assertEqual(len(asm.files), 2)
        self.assertEqual(asm.files[0].name, entries[0].name)
        self.assertEqual(asm.files[0].size, 4096)
        self.assertEqual(asm.files[0].duration_sec, 12)
        self.assertEqual(asm.files[1].size, 3200)

    def test_multi_packet_list(self):
        entries = [
            make_file_entry("FE1DDF64551E_2026101123371%d.mp3" % i, size=1000 + i)
            for i in range(6)
        ]
        frames = build_file_list_frames(entries, chunk=40)
        self.assertGreater(len(frames), 2)
        markers = [f[3] for f in frames]
        self.assertEqual(markers[0], 0x13)
        self.assertEqual(markers[-1], 0x2D)
        self.assertTrue(all(m == 0x1D for m in markers[1:-1]))

        asm = FileListAssembler()
        result = False
        for frame in frames:
            result = asm.feed(frame)
        self.assertTrue(result)
        self.assertTrue(asm.is_complete)
        self.assertEqual([e.name for e in asm.files], [e.name for e in entries])

    def test_incomplete_then_complete(self):
        entries = [make_file_entry("x%d.mp3" % i) for i in range(5)]
        frames = build_file_list_frames(entries, chunk=40)
        asm = FileListAssembler()
        self.assertFalse(asm.feed(frames[0]))
        self.assertFalse(asm.is_complete)
        for frame in frames[1:]:
            asm.feed(frame)
        self.assertTrue(asm.is_complete)
        self.assertEqual(len(asm.files), 5)

    def test_ignores_foreign_frames(self):
        asm = FileListAssembler()
        self.assertFalse(asm.feed(bytes.fromhex("5A040105F192")))


class AudioSyncAssemblerTests(unittest.TestCase):
    DATA = bytes(range(256)) * 4  # 1024 bytes -> 4 packets

    def _ack(self, total=4):
        return build_sync_ack("rec.mp3", True, total, operation=0)

    def test_happy_path(self):
        asm = AudioSyncAssembler("rec.mp3", expected_size=len(self.DATA))
        ack = asm.feed_ack(self._ack())
        self.assertTrue(ack.exists)
        self.assertEqual(asm.total_packets, 4)

        for seq in range(4):
            chunk = self.DATA[seq * 320 : (seq + 1) * 320]
            asm.feed_packet(build_sync_packet(seq, chunk))
        self.assertTrue(asm.is_complete)
        self.assertEqual(asm.assemble(), self.DATA)

    def test_out_of_order_arrival(self):
        asm = AudioSyncAssembler("rec.mp3", expected_size=len(self.DATA))
        asm.feed_ack(self._ack())
        for seq in (2, 0, 3, 1):
            chunk = self.DATA[seq * 320 : (seq + 1) * 320]
            asm.feed_packet(build_sync_packet(seq, chunk))
        self.assertTrue(asm.is_complete)
        self.assertEqual(asm.assemble(), self.DATA)

    def test_missing_packets_reported(self):
        asm = AudioSyncAssembler("rec.mp3", expected_size=len(self.DATA))
        asm.feed_ack(self._ack())
        for seq in (0, 1):
            asm.feed_packet(build_sync_packet(seq, self.DATA[seq * 320 : (seq + 1) * 320]))
        self.assertFalse(asm.is_complete)
        self.assertEqual(asm.missing_sequences(), [2, 3])
        self.assertEqual(asm.next_retry_from(), 2)
        with self.assertRaises(ProtocolError):
            asm.assemble()

    def test_crc_error_is_not_stored(self):
        asm = AudioSyncAssembler("rec.mp3")
        asm.feed_ack(self._ack())
        broken = bytearray(build_sync_packet(0, b"\x00" * 320))
        broken[-1] ^= 0xFF
        asm.feed_packet(bytes(broken))
        self.assertEqual(asm.crc_errors, [0])
        self.assertEqual(asm.received, 0)

    def test_truncates_padding(self):
        asm = AudioSyncAssembler("rec.mp3", expected_size=len(self.DATA))
        asm.feed_ack(self._ack())
        for seq in range(4):
            asm.feed_packet(build_sync_packet(seq, self.DATA[seq * 320 : (seq + 1) * 320]))
        raw = asm.assemble()
        self.assertEqual(len(raw), 1024)
        self.assertEqual(raw, self.DATA)

    def test_duplicate_packets(self):
        asm = AudioSyncAssembler("rec.mp3")
        asm.feed_ack(self._ack())
        pkt = build_sync_packet(0, self.DATA[:320])
        asm.feed_packet(pkt)
        asm.feed_packet(pkt)
        self.assertEqual(asm.received, 1)
        self.assertEqual(asm.duplicates, 1)

    def test_ack_filename_mismatch(self):
        asm = AudioSyncAssembler("other.mp3")
        with self.assertRaises(ProtocolError):
            asm.feed_ack(self._ack())

    # Real capture: a missing file answers with the short failure tail.
    FAIL_20 = "5A1B84004E4F545F45584953545F3030303030302E6D7033000400B600"

    def test_failure_ack_is_accepted(self):
        asm = AudioSyncAssembler("NOT_EXIST_000000.mp3")
        ack = asm.feed_ack(bytes.fromhex(self.FAIL_20))
        self.assertFalse(ack.exists)
        self.assertEqual(ack.error_code, 0x04)
        self.assertEqual(ack.filename, "NOT_EXIST_000000.mp3")
        # A failure must not invent a packet total, or is_complete flips true.
        self.assertIsNone(asm.total_packets)
        self.assertFalse(asm.is_complete)

    def test_failure_ack_tolerates_other_filename(self):
        # Failure acks may echo a shortened name on some firmware -> no raise.
        asm = AudioSyncAssembler("something_else.mp3")
        ack = asm.feed_ack(bytes.fromhex(self.FAIL_20))
        self.assertFalse(ack.exists)


class SimulatorFidelityTests(unittest.TestCase):
    """The simulator must emit byte-identical frames to firmware 206."""

    def test_failure_ack_matches_hardware(self):
        from memotalk.constants import SyncOp
        from memotalk.fake import build_sync_ack

        self.assertEqual(
            build_sync_ack("NOT_EXIST_000000.mp3", False, 0, SyncOp.START, 4).hex().upper(),
            "5A1B84004E4F545F45584953545F3030303030302E6D7033000400B600",
        )
        self.assertEqual(
            build_sync_ack("AB.mp3", False, 0, SyncOp.START, 4).hex().upper(),
            "5A0D840041422E6D70330004000669",
        )

    def test_success_ack_matches_captured_frame(self):
        from memotalk.constants import SyncOp
        from memotalk.fake import build_sync_ack

        expected = (
            "5A288401"
            + "FE1DDF64551E_20261011233717.mp3".encode().hex().upper()
            + "0000020D007AF5"
        )
        self.assertEqual(
            build_sync_ack("FE1DDF64551E_20261011233717.mp3", True, 525, SyncOp.START)
            .hex()
            .upper(),
            expected,
        )

    def test_first_file_list_frame_is_the_real_empty_capture(self):
        from memotalk.fake import build_file_list_frames

        self.assertEqual(build_file_list_frames([])[0].hex().upper(), "FF00051300006468")

    def test_both_empty_list_variants_parse(self):
        # Doc and hardware disagree on the last two bytes; neither is a CRC16,
        # so the assembler must accept both.
        for hexstr in ("FF0005130000300D", "FF00051300006468"):
            with self.subTest(frame=hexstr):
                asm = FileListAssembler()
                self.assertTrue(asm.feed(bytes.fromhex(hexstr)))
                self.assertEqual(asm.files, [])


if __name__ == "__main__":
    unittest.main()
