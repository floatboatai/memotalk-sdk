"""Stateful assemblers for the two multi-packet streams.

Three machines:

* ``FrameReassembler``  - glue fragmented BLE notifications back into frames.
* ``FileListAssembler`` - rebuild the 0xFF multi-packet 0x13 list.
* ``AudioSyncAssembler``- collect 0x84 packets, track gaps, assemble the file.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .codec import expected_frame_length
from .constants import (
    FILE_LIST_FIRST,
    FILE_LIST_HEADER,
    FILE_LIST_LAST,
    FILE_LIST_MORE,
    SYNC_PACKET_HEADER,
)
from .errors import ProtocolError
from .models import AudioFileEntry, SyncAck
from .parsers import parse_sync_ack, parse_sync_packet

__all__ = ["FrameReassembler", "FileListAssembler", "AudioSyncAssembler"]


class FrameReassembler:
    """Turn a byte stream from one characteristic into whole frames.

    On hosts with a large MTU (macOS/iOS: 512) every notification is already a
    complete frame. On small-MTU hosts the 295-byte 0x90 frame and the 328-byte
    0x84 packet arrive split; this machine knows both lengths and stitches them.
    """

    def __init__(self, kind: str = "record") -> None:
        if kind not in ("control", "record"):
            raise ValueError("kind must be 'control' or 'record'")
        self.kind = kind
        self._buf = bytearray()
        self.dropped_bytes = 0

    def reset(self) -> None:
        self._buf.clear()

    @property
    def pending(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> List[bytes]:
        if data:
            self._buf.extend(data)
        frames: List[bytes] = []

        while self._buf:
            head = self._buf[0]

            # 0x13 list packets are self-delimiting per notification, not
            # length-prefixed per packet, so they pass straight through.
            if self.kind == "control" and head == FILE_LIST_HEADER:
                frames.append(bytes(self._buf))
                self._buf.clear()
                break

            if head not in (0x5A, 0xFF):
                # Lost sync: drop one byte and rescan.
                del self._buf[0]
                self.dropped_bytes += 1
                continue

            total = expected_frame_length(bytes(self._buf))
            if total is None:
                if len(self._buf) < 3:
                    break
                # 0xFF lead that is not a 0x84 packet header -> resync.
                del self._buf[0]
                self.dropped_bytes += 1
                continue

            if len(self._buf) < total:
                break

            frames.append(bytes(self._buf[:total]))
            del self._buf[:total]

        return frames


class FileListAssembler:
    """Rebuild the 0x13 answer.

    Completion is decided by *parsing success*, not by byte counts: as soon as
    the documented number of entries has been decoded the list is complete.
    That makes the single-packet, multi-packet and empty-list cases uniform.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.expected_total_len = 0
        self.file_count = 0
        self.buffer = bytearray()
        self.is_complete = False
        self.files: List[AudioFileEntry] = []

    def feed(self, pkt: bytes) -> bool:
        if len(pkt) < 4 or pkt[0] != FILE_LIST_HEADER:
            return False

        total_len = (pkt[1] << 8) | pkt[2]
        marker = pkt[3]

        if marker == FILE_LIST_FIRST:
            self.reset()
            self.expected_total_len = total_len
            if len(pkt) < 6:
                return False
            self.file_count = (pkt[4] << 8) | pkt[5]
            if self.file_count == 0:
                self.is_complete = True
                return True
            self.buffer.extend(pkt[6:])
            if self._try_parse():
                self.is_complete = True
                return True
            if len(pkt) >= total_len:
                # Device claims this single packet is the whole answer.
                self.is_complete = True
                return True
            return False

        if marker == FILE_LIST_MORE:
            self.buffer.extend(pkt[4:])
            self._try_parse()
            return False

        if marker == FILE_LIST_LAST:
            self.buffer.extend(pkt[4:-2] if len(pkt) >= 6 else pkt[4:])
            self._try_parse()
            self.is_complete = True
            return True

        return False

    def _try_parse(self) -> bool:
        self._parse_entries()
        return self.file_count > 0 and len(self.files) == self.file_count

    def _parse_entries(self) -> None:
        """Entry layout: 1B name_len, name, 1B fmt, 1B rsv, 4B ts, 4B size, 4B dur, 2B crc."""
        self.files = []
        stream = bytes(self.buffer)
        idx = 0
        total = len(stream)

        while idx < total and len(self.files) < self.file_count:
            name_len = stream[idx]
            meta_at = idx + 1 + name_len
            if meta_at + 16 > total:
                break
            name = stream[idx + 1 : meta_at].decode("ascii", errors="replace")
            fmt, _rsv, ts, size, dur, fcrc = _unpack_entry(stream[meta_at : meta_at + 16])
            self.files.append(
                AudioFileEntry(
                    name=name,
                    format_code=fmt,
                    timestamp=ts,
                    size=size,
                    duration_sec=dur,
                    crc=fcrc,
                )
            )
            idx = meta_at + 16


def _unpack_entry(raw: bytes):
    import struct

    return struct.unpack(">BBIIIH", raw)


class AudioSyncAssembler:
    """Collect 0x84 data packets, tolerate reordering and loss, then assemble."""

    def __init__(self, filename: str = "", expected_size: Optional[int] = None) -> None:
        self.filename = filename
        self.expected_size = expected_size
        self.total_packets: Optional[int] = None
        self.ack: Optional[SyncAck] = None
        self.chunks: Dict[int, bytes] = {}
        self.crc_errors: List[int] = []
        self.duplicates = 0

    @property
    def received(self) -> int:
        return len(self.chunks)

    @property
    def is_complete(self) -> bool:
        return self.total_packets is not None and len(self.chunks) >= self.total_packets

    def feed_ack(self, frame: bytes) -> SyncAck:
        ack = parse_sync_ack(frame)
        # Only a success ack echoes the name reliably; a failure ack may echo it
        # truncated on some firmware, so a mismatch there is not an error.
        if ack.exists and self.filename and ack.filename and ack.filename != self.filename:
            raise ProtocolError(
                f"0x84 ack is for {ack.filename!r}, expected {self.filename!r}"
            )
        self.ack = ack
        if ack.exists:
            self.total_packets = ack.total_packets
            if not self.filename:
                self.filename = ack.filename
        return ack

    def feed_packet(self, frame: bytes) -> bool:
        """Insert one packet. Returns True once every packet is present."""
        if frame[:3] != SYNC_PACKET_HEADER:
            raise ProtocolError("not a 0x84 data packet")
        pkt = parse_sync_packet(frame)
        if not pkt.crc_ok:
            self.crc_errors.append(pkt.seq)
            return self.is_complete
        if pkt.seq in self.chunks:
            self.duplicates += 1
        self.chunks[pkt.seq] = pkt.payload
        return self.is_complete

    def missing_sequences(self) -> List[int]:
        if self.total_packets is None:
            return []
        return [s for s in range(self.total_packets) if s not in self.chunks]

    def next_retry_from(self) -> Optional[int]:
        """First missing packet index, i.e. where a resume should start."""
        missing = self.missing_sequences()
        return missing[0] if missing else None

    def assemble(self) -> bytes:
        if self.total_packets is None:
            raise ProtocolError("total_packets unknown - 0x84 ack was never parsed")
        missing = self.missing_sequences()
        if missing:
            preview = ", ".join(str(s) for s in missing[:8])
            more = "" if len(missing) <= 8 else f" (+{len(missing) - 8} more)"
            raise ProtocolError(f"{len(missing)} packet(s) still missing: {preview}{more}")
        raw = b"".join(self.chunks[i] for i in range(self.total_packets))
        if self.expected_size is not None and len(raw) > self.expected_size:
            raw = raw[: self.expected_size]
        return raw
