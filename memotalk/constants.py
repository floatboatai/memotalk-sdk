"""Wire constants: GATT routing, command codes, enums and protocol limits.

Everything here is a *fact about the hardware*, transcribed from the verified
protocol spec (firmware baseline 206 / V2.0.6). The library never invents codes.
"""

from __future__ import annotations

from enum import Enum, IntEnum

__all__ = [
    "UUID_SVC_FFF0", "UUID_CHAR_FFF1_WRITE", "UUID_CHAR_FFF2_NOTIFY",
    "UUID_SVC_FFE0", "UUID_CHAR_FFE1_WRITE", "UUID_CHAR_FFE2_NOTIFY",
    "SERVICE_UUIDS", "NAME_HINTS", "Channel", "Cmd", "RecAction", "SyncOp",
    "SettingType", "RecordResult", "RecordStateCode", "DeviceErrorCode",
    "FileErrorCode", "Limits", "FILE_LIST_HEADER", "FILE_LIST_FIRST",
    "FILE_LIST_MORE", "FILE_LIST_LAST", "SYNC_PACKET_HEADER",
    "SYNC_PACKET_TOTAL", "SYNC_PAYLOAD_SIZE", "AUDIO_STREAM_TOTAL",
    "REALTIME_FRAME_SIZE", "REALTIME_PAYLOAD",
]

# --- GATT -------------------------------------------------------------------
UUID_SVC_FFF0 = "0000fff0-0000-1000-8000-00805f9b34fb"
UUID_CHAR_FFF1_WRITE = "0000fff1-0000-1000-8000-00805f9b34fb"
UUID_CHAR_FFF2_NOTIFY = "0000fff2-0000-1000-8000-00805f9b34fb"

UUID_SVC_FFE0 = "0000ffe0-0000-1000-8000-00805f9b34fb"
UUID_CHAR_FFE1_WRITE = "0000ffe1-0000-1000-8000-00805f9b34fb"
UUID_CHAR_FFE2_NOTIFY = "0000ffe2-0000-1000-8000-00805f9b34fb"

SERVICE_UUIDS = (UUID_SVC_FFF0, UUID_SVC_FFE0)
NAME_HINTS = ("memotalk", "ai-pen", "ai pen")


class Channel(str, Enum):
    """Logical write channels. The mapping to characteristics is fixed by hardware."""

    CONTROL = "control"   # FFF1 - everything except 0x80
    RECORD = "record"     # FFE1 - 0x80 recording control only


# --- command / response codes ----------------------------------------------
class Cmd(IntEnum):
    ERROR = 0x00
    BATTERY = 0x01
    RECORD_STATE = 0x02
    FTP_HOST = 0x03
    FTP_PORT = 0x04
    FTP_USER = 0x05
    FTP_PASSWORD = 0x06
    FIRMWARE = 0x09
    WIFI_SSID = 0x0B
    WIFI_PASSWORD = 0x0C
    HAS_IMPORT_FILE = 0x0D
    FREE_SPACE = 0x0F
    FILE_LIST = 0x13
    DEVICE_SN = 0x14
    RECORD_CONTROL = 0x80          # write FFE1, notify FFE2
    FILE_SYNC = 0x84
    WIFI_AP = 0x87
    RTC_SYNC = 0x91
    DELETE_FILE = 0x92
    CLEAR_ALL = 0x9A
    REALTIME_ASR = 0xA4
    SETTING_WRITE = 0xB0
    SETTING_READ = 0xB1
    AUDIO_LEVEL = 0xA8             # device -> app push
    AUDIO_STREAM = 0x90            # device -> app push


class RecAction(IntEnum):
    STOP = 0x00
    START = 0x01
    PAUSE = 0x02
    RESUME = 0x03


class SyncOp(IntEnum):
    START = 0x00
    CANCEL = 0x01
    PAUSE = 0x02
    RESUME = 0x03


class SettingType(IntEnum):
    SLEEP_TIMEOUT_SEC = 0x00
    MAX_RECORD_MINUTES = 0x01
    USB_MASS_STORAGE = 0x02


class RecordResult(IntEnum):
    FAILED = 0x00
    SUCCESS = 0x01
    STORAGE_FULL = 0xFF
    LOW_BATTERY = 0xFE


class RecordStateCode(IntEnum):
    STOPPED = 0
    RECORDING = 1
    PAUSED = 2
    RESUMED = 3


class DeviceErrorCode(IntEnum):
    """0x00 error response payload codes (spec section 3.1)."""

    TIMEOUT = 0x00
    LENGTH_OUT_OF_RANGE = 0x01
    CRC_MISMATCH = 0x02
    OPERATION_FORBIDDEN = 0x03
    INVALID_COMMAND = 0x04
    INVALID_PARAMETER = 0x05


class FileErrorCode(IntEnum):
    """0x92 delete failure codes."""

    NOT_FOUND = 0x04
    BAD_PATH = 0x05
    BAD_FILENAME = 0x06
    PERMISSION = 0x07


class Limits:
    FRAME_MAX_PAYLOAD = 237
    DELETE_FILENAME_MAX = 63
    SLEEP_TIMEOUT_MIN = 15
    SLEEP_TIMEOUT_MAX = 7200
    SLEEP_TIMEOUT_DEFAULT = 300
    MAX_RECORD_MINUTES_MIN = 1
    MAX_RECORD_MINUTES_MAX = 480


# --- stream framing markers -------------------------------------------------
FILE_LIST_HEADER = 0xFF
FILE_LIST_FIRST = 0x13
FILE_LIST_MORE = 0x1D
FILE_LIST_LAST = 0x2D

SYNC_PACKET_HEADER = b"\xff\xff\xff"
SYNC_PACKET_TOTAL = 328          # 3B header + 3B seq + 320B data + 2B CRC
SYNC_PAYLOAD_SIZE = 320

REALTIME_FRAME_SIZE = 295        # 0x90 frame total length
AUDIO_STREAM_TOTAL = REALTIME_FRAME_SIZE
REALTIME_PAYLOAD = slice(5, 293)  # 288 bytes = 2 x 144B MPEG-2 L3 frames (72ms)
