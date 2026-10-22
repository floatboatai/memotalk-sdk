"""Typed exception hierarchy.

Design rule: a caller should never have to inspect a raw byte to decide what
went wrong. Every documented device failure mode has its own class.
"""

from __future__ import annotations

from typing import Optional

from .constants import DeviceErrorCode, RecordResult

__all__ = [
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
    "OperationForbidden",
    "InvalidParameter",
    "ConfirmationRequired",
    "CapabilityUnavailable",
    "device_error_from_frame",
]


class MemoTalkError(Exception):
    """Base class for every error raised by this package."""


# --- discovery / transport --------------------------------------------------
class DeviceNotFound(MemoTalkError):
    """No MemoTalk S1 / AI-PEN peripheral was found within the scan window."""


class TransportError(MemoTalkError):
    """BLE link level failure (adapter off, write refused, notify not started)."""


class ConnectionLost(TransportError):
    """The peripheral disconnected while an operation was in flight."""


class MemoTalkTimeout(MemoTalkError):
    """The device did not answer the command within the deadline."""

    def __init__(self, code: Optional[int] = None, message: str = ""):
        self.code = code
        label = f"0x{code:02X}" if code is not None else "command"
        super().__init__(message or f"no response for {label} before timeout")


# --- framing ----------------------------------------------------------------
class ProtocolError(MemoTalkError):
    """A frame violates the 0x5A framing rules."""


class CrcError(ProtocolError):
    """CRC16 mismatch on a received frame."""


# --- device level rejections ------------------------------------------------
_DEVICE_ERROR_TEXT = {
    DeviceErrorCode.TIMEOUT: "device reported timeout",
    DeviceErrorCode.LENGTH_OUT_OF_RANGE: "device reported length out of range",
    DeviceErrorCode.CRC_MISMATCH: "device reported CRC mismatch",
    DeviceErrorCode.OPERATION_FORBIDDEN: "device refused: operation not allowed right now",
    DeviceErrorCode.INVALID_COMMAND: "device reported invalid command",
    DeviceErrorCode.INVALID_PARAMETER: "device reported invalid parameter",
}


class DeviceErrorResponse(MemoTalkError):
    """The device answered with a 0x00 error frame (spec 3.1)."""

    def __init__(self, code: int, payload: bytes = b""):
        self.code = int(code)
        self.payload = payload
        try:
            text = _DEVICE_ERROR_TEXT[DeviceErrorCode(self.code)]
        except ValueError:
            text = f"device error 0x{self.code:02X}"
        # 0x04 / 0x05 echo the offending command + params after the code byte.
        self.echoed = payload[1:]
        super().__init__(text)

    @classmethod
    def from_frame(cls, frame: bytes) -> "DeviceErrorResponse":
        if len(frame) < 4 or frame[0] != 0x5A:
            raise ProtocolError(f"not an error frame: {frame.hex().upper()}")
        return cls(frame[3], bytes(frame[4:-2]))


class OperationForbidden(DeviceErrorResponse):
    """0x03 - typically raised while Wi-Fi fast transfer is active."""


class InvalidParameter(DeviceErrorResponse):
    """0x05 - the device rejected the parameter value."""


class DeviceRejected(MemoTalkError):
    """A well-formed ack whose result field is not 'success'."""

    def __init__(self, message: str, *, code: Optional[int] = None, frame: Optional[bytes] = None):
        self.code = code
        self.frame = frame
        super().__init__(message)


class RecordingRejected(DeviceRejected):
    """0x80 ack carried a failure result (storage full / low battery / failed)."""

    def __init__(self, result: RecordResult, frame: Optional[bytes] = None):
        reason = {
            RecordResult.FAILED: "recording command failed",
            RecordResult.STORAGE_FULL: "storage is full",
            RecordResult.LOW_BATTERY: "battery too low to record",
        }.get(result, f"unexpected result 0x{int(result):02X}")
        self.result = result
        super().__init__(reason, code=int(result), frame=frame)


class FileNotFoundOnDevice(DeviceRejected):
    """0x84 ack reported the requested file does not exist."""


class DeleteFailed(DeviceRejected):
    """0x92 returned failure, with the documented file error code."""


class ConfirmationRequired(MemoTalkError):
    """0x9A (clear all) requires an explicit confirm=True."""


class CapabilityUnavailable(MemoTalkError):
    """The host platform cannot perform this operation (e.g. join Wi-Fi on Windows)."""


def device_error_from_frame(frame: bytes) -> DeviceErrorResponse:
    """Map a 0x00 frame to the most specific exception class available."""
    err = DeviceErrorResponse.from_frame(frame)
    if err.code == DeviceErrorCode.OPERATION_FORBIDDEN:
        return OperationForbidden(err.code, err.payload)
    if err.code == DeviceErrorCode.INVALID_PARAMETER:
        return InvalidParameter(err.code, err.payload)
    return err
