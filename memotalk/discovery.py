"""Peripheral discovery.

Two strategies, in order:

1. Normal BLE advertising scan, matching the advertised name or either service
   UUID. This works on every platform.
2. macOS/iOS only: a peripheral already connected by the OS stops advertising,
   so we ask CoreBluetooth for connected peripherals on FFF0/FFE0. This path
   uses a private bleak attribute and is therefore best-effort by design.
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional, Sequence, Tuple

from .constants import NAME_HINTS

__all__ = ["find_device", "find_devices", "describe"]

log = logging.getLogger("memotalk.discovery")


def _uuid_matches(uuids: Sequence[str]) -> bool:
    wanted = {"fff0", "ffe0"}
    for raw in uuids:
        value = str(raw).lower()
        for service in wanted:
            if value.startswith(f"0000{service}") or value.startswith(service):
                return True
    return False


def _name_matches(name: str, hints: Sequence[str]) -> bool:
    lowered = (name or "").lower()
    return any(h in lowered for h in hints)


async def find_devices(
    timeout: float = 3.5,
    *,
    name_hints: Sequence[str] = NAME_HINTS,
    match_service_uuid: bool = True,
) -> List[Tuple[object, str, List[str]]]:
    """Return every plausible peripheral as `(BLEDevice, name, service_uuids)`."""
    try:
        from bleak import BleakScanner
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("bleak is required for device discovery") from exc

    found: List[Tuple[object, str, List[str]]] = []
    try:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except TypeError:  # very old bleak without return_adv
        discovered = {d.address: (d, None) for d in await BleakScanner.discover(timeout=timeout)}

    for _address, pair in (discovered or {}).items():
        device, adv = pair if isinstance(pair, tuple) else (pair, None)
        name = getattr(device, "name", None) or getattr(adv, "local_name", None) or ""
        uuids = [str(u) for u in (getattr(adv, "service_uuids", None) or [])]
        if _name_matches(name, name_hints) or (match_service_uuid and _uuid_matches(uuids)):
            found.append((device, name, uuids))

    found.sort(key=lambda item: (0 if _name_matches(item[1], name_hints) else 1, item[1]))
    return found


async def find_device(
    timeout: float = 3.5,
    *,
    name_hints: Sequence[str] = NAME_HINTS,
    match_service_uuid: bool = True,
    prefer: Optional[str] = None,
):
    """Return one usable BLEDevice, or None.

    `prefer` pins a specific name or address when several recorders are nearby.
    """
    candidates = await find_devices(
        timeout, name_hints=name_hints, match_service_uuid=match_service_uuid
    )
    if prefer:
        lowered = prefer.lower()
        for device, name, _uuids in candidates:
            address = getattr(device, "address", "") or ""
            if lowered in (name or "").lower() or lowered == address.lower():
                return device

    if candidates:
        device, name, _uuids = candidates[0]
        log.debug("found %s (%s)", name, getattr(device, "address", "?"))
        return device

    if sys.platform == "darwin":
        retrieved = _retrieve_connected_macos()
        if retrieved is not None:
            return retrieved

    return None


def _retrieve_connected_macos():
    """Ask CoreBluetooth for peripherals the OS already holds (macOS only)."""
    try:
        import CoreBluetooth  # noqa: F401  (only importable on macOS)
        from bleak import BleakScanner
        from bleak.backends.device import BLEDevice
    except Exception:
        return None

    try:
        manager = BleakScanner._backend._manager  # private, hence best-effort
        central = manager.central_manager
    except Exception:
        return None

    for service in ("FFF0", "FFE0"):
        try:
            cb_uuid = CoreBluetooth.CBUUID.UUIDWithString_(service)
            peripherals = central.retrieveConnectedPeripheralsWithServices_([cb_uuid])
        except Exception:
            continue
        for peripheral in peripherals or []:
            try:
                name = str(peripheral.name() or "MemoTalk S1")
                uuid = str(peripheral.identifier().UUIDString())
                log.info("recovered OS-connected peripheral %s", name)
                return BLEDevice(address=uuid, name=name, details=(peripheral, manager))
            except Exception:
                continue
    return None


def describe(device) -> str:
    """Human readable label that also works for the simulator."""
    if device is None:
        return "?"
    name = getattr(device, "name", None) or type(device).__name__
    address = getattr(device, "address", None) or "?"
    return f"{name} ({address})"
