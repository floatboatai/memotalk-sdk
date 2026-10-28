"""memotalk - MemoTalk S1 / AI-PEN BLE recorder client.

Layers, bottom to top:

    codec        bytes <-> fields (CRC16, framing, BCD, XOR-FF)
    commands     22 pure command builders
    parsers      every response mapped to a typed model
    assemblers   multi-packet streams (0x13 list, 0x84 file, notify glue)
    transport    connect / write / correlate / keep-alive / disconnect
    client       six capability groups over the transport
    fake         device simulator for offline development and tests

Typical use::

    import asyncio
    from memotalk import MemoTalkClient

    async def main():
        async with MemoTalkClient() as dev:
            print(await dev.battery())
            for entry in await dev.files.list():
                print(entry.name, entry.size)

    asyncio.run(main())
"""

from __future__ import annotations

from .assemblers import AudioSyncAssembler, FileListAssembler, FrameReassembler
from .client import MemoTalkClient, NetworkSession
from .codec import build_frame, crc16, verify_crc, xor_ff_decode, xor_ff_encode
from .constants import (
    AUDIO_STREAM_TOTAL,
    SERVICE_UUIDS,
    SYNC_PACKET_TOTAL,
    Channel,
    Cmd,
    DeviceErrorCode,
    FileErrorCode,
    RecAction,
    RecordResult,
    RecordStateCode,
    SettingType,
    SyncOp,
)
from .discovery import find_device, find_devices
from .errors import (
    CapabilityUnavailable,
    ConfirmationRequired,
    ConnectionLost,
    CrcError,
    DeleteFailed,
    DeviceErrorResponse,
    DeviceNotFound,
    DeviceRejected,
    FileNotFoundOnDevice,
    MemoTalkError,
    MemoTalkTimeout,
    ProtocolError,
    RecordingRejected,
    TransportError,
)
from .fake import FakeMemoTalk, FakeTransport
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
from .parsers import parse_response
from .transport import BleTransport, Subscription

__version__ = "0.1.0"
SKILL_COMPAT_VERSION = "V0.1"

__all__ = [
    "__version__",
    "SKILL_COMPAT_VERSION",
    # client
    "MemoTalkClient",
    "NetworkSession",
    # transport / discovery
    "BleTransport",
    "Subscription",
    "find_device",
    "find_devices",
    # codec / protocol
    "build_frame",
    "crc16",
    "verify_crc",
    "xor_ff_decode",
    "xor_ff_encode",
    "parse_response",
    # assemblers
    "FrameReassembler",
    "FileListAssembler",
    "AudioSyncAssembler",
    # constants
    "Channel",
    "Cmd",
    "RecAction",
    "SyncOp",
    "SettingType",
    "RecordResult",
    "RecordStateCode",
    "DeviceErrorCode",
    "FileErrorCode",
    "SERVICE_UUIDS",
    "AUDIO_STREAM_TOTAL",
    "SYNC_PACKET_TOTAL",
    # models
    "AckResult",
    "AudioFileEntry",
    "AudioLevel",
    "BatteryInfo",
    "DeviceIdentity",
    "DeviceInfo",
    "DeviceSettings",
    "FirmwareInfo",
    "FreeSpace",
    "FtpConfig",
    "RecordState",
    "SettingValue",
    "SyncAck",
    "WifiCredentials",
    # errors
    "MemoTalkError",
    "DeviceNotFound",
    "TransportError",
    "ConnectionLost",
    "MemoTalkTimeout",
    "ProtocolError",
    "CrcError",
    "DeviceErrorResponse",
    "DeviceRejected",
    "RecordingRejected",
    "FileNotFoundOnDevice",
    "DeleteFailed",
    "ConfirmationRequired",
    "CapabilityUnavailable",
    # simulator
    "FakeMemoTalk",
    "FakeTransport",
]
