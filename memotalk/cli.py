"""Command line entry point: `memotalk <command>`.

Thin on purpose - it is a reference consumer of the library, not a second
implementation of it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .client import MemoTalkClient
from .constants import Channel, SettingType
from .discovery import describe
from .errors import MemoTalkError
from .fake import FakeMemoTalk, FakeTransport


def _mask(secret: str) -> str:
    if not secret:
        return "(empty)"
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:2] + "*" * (len(secret) - 4) + secret[-2:]


async def _open(args) -> MemoTalkClient:
    if getattr(args, "simulate", False):
        device = FakeMemoTalk()
        device.files = []
        transport = FakeTransport(device)
        return await MemoTalkClient(transport=transport).connect()
    client = MemoTalkClient(scan_timeout=args.scan_timeout, prefer=args.prefer)
    return await client.connect()


async def _cmd_status(dev: MemoTalkClient, args) -> int:
    info = await dev.info(include_files=args.files)
    print("=== device ===")
    print(f"  name            : {describe(dev.transport.target)}")
    print(f"  serial          : {info.identity.serial}")
    print(f"  firmware        : {info.firmware.raw} -> V{info.firmware.version}")
    print(f"  battery         : {info.battery.percent}% ({info.battery.level}/10)")
    print(f"  free space      : {info.free_space.percent}%")
    print(f"  storage format  : {info.identity.format_name}")
    print(f"  realtime ASR    : {info.identity.realtime_asr_enabled}")
    print(f"  import pending  : {info.has_import_file}")
    print(f"  recording state : {info.record.state.name} ({info.record.state.value})")
    print(f"  idle sleep      : {info.settings.sleep_timeout_sec}s")
    print(f"  max per file    : {info.settings.max_record_minutes}min")
    print(f"  usb mass storage: {info.settings.usb_mass_storage}")
    if args.files:
        print(f"  files           : {len(info.files or [])}")
    return 0


async def _cmd_list(dev: MemoTalkClient, args) -> int:
    entries = await dev.files.list()
    if not entries:
        print("(no recordings on device)")
        return 0
    print(f"{'name':<40} {'size':>10} {'dur':>7}  recorded at")
    for e in sorted(entries, key=lambda x: x.timestamp, reverse=True):
        when = e.recorded_at.isoformat(sep=" ") if e.recorded_at else "?"
        print(f"{e.name:<40} {e.size:>10} {e.duration_sec:>6}s  {when}")
    return 0


async def _cmd_download(dev: MemoTalkClient, args) -> int:
    out = Path(args.out)
    if args.all or not args.name:
        paths = await dev.files.download_all(out)
        for p in paths:
            print(f"saved {p} ({p.stat().st_size} bytes)")
        return 0

    def progress(done: int, total: int) -> None:
        pct = (done / total * 100) if total else 0
        print(f"\r  packet {done}/{total} ({pct:5.1f}%)", end="", flush=True)

    path = await dev.files.download(args.name, out, progress=progress, overwrite=True)
    print(f"\nsaved {path} ({path.stat().st_size} bytes)")
    return 0


async def _cmd_delete(dev: MemoTalkClient, args) -> int:
    if not args.name:
        print("error: --name is required", file=sys.stderr)
        return 2
    await dev.files.delete(args.name)
    print(f"deleted {args.name}")
    return 0


async def _cmd_clear(dev: MemoTalkClient, args) -> int:
    if not args.confirm:
        print("refusing to wipe the device without --confirm", file=sys.stderr)
        return 2
    await dev.files.clear_all(confirm=True)
    print("all recordings cleared")
    return 0


async def _cmd_record(dev: MemoTalkClient, args) -> int:
    await dev.record.set_realtime(args.realtime)
    state = await dev.record.start()
    print(f"recording -> {state.filename}")

    async def pump():
        if args.realtime:
            async for chunk in dev.stream.audio():
                args._bytes += len(chunk)
        else:
            async for level in dev.stream.level():
                args._frames += 1
                if args.frames and args._frames <= args.frames:
                    bar = "#" * int(level.normalized * 30)
                    print(f"  {level.db:7.1f} dB |{bar:<30}|")

    task = asyncio.create_task(pump())
    try:
        await asyncio.sleep(args.seconds)
        if args.pause:
            await dev.record.pause()
            await asyncio.sleep(0.5)
            await dev.record.resume()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        final = await dev.record.stop()
        await dev.record.set_realtime(False)
        print(f"stopped -> {final.filename} ({final.state.name})")
        if args.realtime:
            print(f"received {args._bytes} bytes of live MP3")
        else:
            print(f"received {args._frames} level frames")
    return 0


async def _cmd_config(dev: MemoTalkClient, args) -> int:
    changed = False
    if args.sleep is not None:
        await dev.settings.write(SettingType.SLEEP_TIMEOUT_SEC, args.sleep)
        print(f"idle sleep timeout = {args.sleep}s")
        changed = True
    if args.max_record is not None:
        await dev.settings.write(SettingType.MAX_RECORD_MINUTES, args.max_record)
        print(f"max per file = {args.max_record}min")
        changed = True
    if args.usb is not None:
        await dev.settings.write(SettingType.USB_MASS_STORAGE, args.usb)
        print(f"usb mass storage = {args.usb}")
        changed = True
    if not changed:
        settings = await dev.settings.read_all()
        print(f"idle sleep timeout = {settings.sleep_timeout_sec}s")
        print(f"max per file = {settings.max_record_minutes}min")
        print(f"usb mass storage = {settings.usb_mass_storage}")
    return 0


async def _cmd_wifi(dev: MemoTalkClient, args) -> int:
    from .errors import CapabilityUnavailable

    async with dev.wifi.session() as net:
        print(f"ssid     : {net.ssid}")
        print(f"password : {net.password if args.show_secrets else _mask(net.password)}")
        print(f"ftp      : {net.ftp_config.address}")
        print(f"user     : {net.ftp_config.user}")
        if not args.show_secrets:
            print(f"ftp pass : {_mask(net.ftp_config.password)}")
        else:
            print(f"ftp pass : {net.ftp_config.password}")

        if args.join:
            try:
                ip = net.join_hotspot(iface=args.iface)
                print(f"joined hotspot, local ip = {ip}")
            except CapabilityUnavailable as exc:
                print(f"cannot auto-join: {exc}", file=sys.stderr)
                return 3

        if args.ls or args.download:
            ftp = net.open_ftp()
            try:
                names = net.list_audio_files(ftp)
                print(f"files on device ({len(names)}):")
                for name in names:
                    print(f"  {name}")
                if args.download:
                    path = net.download(ftp, args.download, args.out)
                    print(f"saved {path} ({path.stat().st_size} bytes)")
            finally:
                ftp.quit()
    print("hotspot closed")
    return 0


async def _cmd_raw(dev: MemoTalkClient, args) -> int:
    frame = bytes.fromhex(args.hex.replace(" ", ""))
    channel = Channel.RECORD if args.channel == "record" else Channel.CONTROL
    if args.expect:
        code = int(args.expect, 16)
        reply = await dev.raw.request(frame, code=code, timeout=args.timeout)
    else:
        await dev.raw.send(frame, channel)
        print(f"sent {frame.hex().upper()} to {channel.value}")
        return 0
    print(f"<- {reply.hex().upper()}")
    return 0


async def _cmd_scan(args) -> int:
    from .discovery import find_devices

    found = await find_devices(args.scan_timeout)
    if not found:
        print("no MemoTalk S1 / AI-PEN found (press the button to wake the device)")
        return 1
    for device, name, uuids in found:
        print(f"{name or '(unnamed)'}  {getattr(device, 'address', '?')}  {uuids}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memotalk",
        description=f"MemoTalk S1 / AI-PEN recorder client (memotalk-s1 {__version__})",
    )
    parser.add_argument("--version", action="version", version=f"memotalk-s1 {__version__}")
    parser.add_argument("--scan-timeout", type=float, default=3.5)
    parser.add_argument("--prefer", default=None, help="prefer a device name or MAC")
    parser.add_argument("--simulate", action="store_true", help="run against the built-in simulator")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="read one status snapshot")
    p.add_argument("--files", action="store_true", help="also list recordings")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("list", help="list recordings on the device")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("download", help="pull recordings over BLE (0x84)")
    p.add_argument("--name")
    p.add_argument("--all", action="store_true")
    p.add_argument("--out", default="./downloads")
    p.set_defaults(func=_cmd_download)

    p = sub.add_parser("delete", help="delete one recording (0x92)")
    p.add_argument("--name", required=True)
    p.set_defaults(func=_cmd_delete)

    p = sub.add_parser("clear-all", help="wipe every recording (0x9A)")
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(func=_cmd_clear)

    p = sub.add_parser("record", help="record for N seconds, printing level or audio")
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--realtime", action="store_true", help="consume 0x90 MP3 instead of 0xA8 level")
    p.add_argument("--frames", type=int, default=5)
    p.add_argument("--pause", action="store_true", help="pause and resume once")
    p.set_defaults(func=_cmd_record, _bytes=0, _frames=0)

    p = sub.add_parser("config", help="read or write the three settings (0xB0/0xB1)")
    p.add_argument("--sleep", type=int)
    p.add_argument("--max-record", type=int)
    p.add_argument("--usb", type=int, choices=[0, 1])
    p.set_defaults(func=_cmd_config)

    p = sub.add_parser("wifi", help="open the hotspot, print credentials, optionally fetch files")
    p.add_argument("--ls", action="store_true")
    p.add_argument("--download", default=None)
    p.add_argument("--out", default="./downloads")
    p.add_argument("--join", action="store_true", help="join the hotspot (macOS only)")
    p.add_argument("--iface", default="en0")
    p.add_argument("--show-secrets", action="store_true")
    p.set_defaults(func=_cmd_wifi)

    p = sub.add_parser("raw", help="send a raw hex frame")
    p.add_argument("--hex", required=True)
    p.add_argument("--channel", choices=["control", "record"], default="control")
    p.add_argument("--expect", default=None, help="response code in hex, e.g. 01")
    p.add_argument("--timeout", type=float, default=2.0)
    p.set_defaults(func=_cmd_raw)

    p = sub.add_parser("scan", help="list nearby peripherals")
    p.set_defaults(func=None)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "scan":
        return asyncio.run(_cmd_scan(args))

    async def runner() -> int:
        client = await _open(args)
        try:
            return await args.func(client, args)
        finally:
            await client.close()

    try:
        return asyncio.run(runner())
    except MemoTalkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
