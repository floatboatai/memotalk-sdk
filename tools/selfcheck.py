#!/usr/bin/env python3
"""三层真机回归：链路 / 静态指令逐条比对 / 动态流程 / 热点凭据 / 勘误反向验证。

设计原则：
1. 只读为主。绝不 0x9A 清空，绝不删除用户既有录音。
2. 破坏性指令只验证「保护是否生效」（confirm=False 必须在本地就抛错，不发包）。
3. 录音测试会创建新文件，结束前只删除**本次自己创建**的那些。
4. 不自动连接设备热点：那会切断主机网络，进而中断会话。

用法：python tools/selfcheck.py --out report.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memotalk import MemoTalkClient, commands, parsers
from memotalk.codec import build_frame, verify_crc
from memotalk.constants import Channel, Cmd, SettingType, SyncOp
from memotalk.errors import (
    ConfirmationRequired,
    FileNotFoundOnDevice,
    MemoTalkError,
    MemoTalkTimeout,
    ProtocolError,
)

RESULTS: list[dict] = []
MP3_SYNC = b"\xff\xf3\x48\xc4"


def record(stage: str, name: str, status: str, detail: str = "", evidence=None) -> None:
    entry = {"stage": stage, "check": name, "status": status,
             "detail": detail, "at": time.strftime("%H:%M:%S")}
    if evidence is not None:
        entry["evidence"] = evidence
    RESULTS.append(entry)
    mark = {"PASS": "[ OK ]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "WARN": "[WARN]"}[status]
    print(f"{mark} {stage:<4} {name:<36} {detail}")


def summarize() -> dict:
    counts: dict[str, int] = {}
    for r in RESULTS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


def code_predicate(*codes: int):
    wanted = {int(c) for c in codes} | {int(Cmd.ERROR)}

    def predicate(frame: bytes) -> bool:
        return len(frame) >= 3 and frame[0] == 0x5A and frame[2] in wanted

    return predicate


async def send_and_parse(client, frame, code, parser, label, *,
                         write=Channel.CONTROL, listen=Channel.CONTROL, timeout=5.0):
    """发原始帧 -> 校验 CRC -> 解析，并把原始 hex 留作证据。"""
    raw = await client.raw.request(
        frame, predicate=code_predicate(code), write=write, listen=listen, timeout=timeout
    )
    hexstr = raw.hex().upper()
    if raw[2] == int(Cmd.ERROR):
        record("L2", label, "FAIL", f"device returned error 0x{raw[3]:02X}", {"frame": hexstr})
        return None
    if not verify_crc(raw):
        record("L2", label, "FAIL", "CRC mismatch on device frame", {"frame": hexstr})
        return None
    parsed = parser(raw)
    record("L2", label, "PASS", f"{parsed}", {"frame": hexstr})
    return parsed


# ---------------------------------------------------------------------------
# L1 - link layer
# ---------------------------------------------------------------------------
async def stage_l1(client: MemoTalkClient) -> None:
    tgt = client.transport.target
    record("L1", "device discovered", "PASS",
           f"{getattr(tgt, 'name', '?')} @ {getattr(tgt, 'address', '?')}")

    if not client.is_connected:
        record("L1", "ble link established", "FAIL", "not connected")
        return
    record("L1", "ble link established", "PASS", "is_connected=True")

    record("L1", "0x91 rtc handshake", "PASS" if client.transport.handshake_done else "FAIL",
           "ack confirmed within 1s" if client.transport.handshake_done else "no 0x91 ack")

    counter = client.transport.subscribe(
        lambda f: len(f) >= 3 and f[0] == 0x5A and f[2] == int(Cmd.BATTERY),
        channel=Channel.CONTROL, maxsize=64,
    )
    try:
        await asyncio.sleep(9.0)
        hits = 0
        while True:
            try:
                await counter.get(timeout=0.2)
                hits += 1
            except asyncio.TimeoutError:
                break
        record("L1", "keepalive auto ping", "PASS" if hits >= 2 else "FAIL",
               f"{hits} x 0x01 in 9s (configured interval 4s)", {"hits": hits})
    finally:
        client.transport.unsubscribe(counter)


# ---------------------------------------------------------------------------
# L2 - 每条读指令：原始帧 vs 库解析
# ---------------------------------------------------------------------------
async def stage_l2(client: MemoTalkClient) -> None:
    out = await send_and_parse(client, commands.read_battery(), Cmd.BATTERY,
                               parsers.parse_battery, "0x01 battery")
    if out:
        api = await client.battery()
        ok = api == out and 0 <= api.level <= 10 and api.percent == api.level * 10
        record("L2", "0x01 api == raw parse", "PASS" if ok else "FAIL", f"{api}")

    out = await send_and_parse(client, commands.read_firmware_version(), Cmd.FIRMWARE,
                               parsers.parse_firmware, "0x09 firmware")
    if out:
        api = await client.firmware()
        ok = api.raw == out.raw and api.raw.isdigit() and len(api.raw) == 3
        record("L2", "0x09 api == raw parse", "PASS" if ok else "FAIL", f"V{api.version}")

    out = await send_and_parse(client, commands.read_device_sn(), Cmd.DEVICE_SN,
                               parsers.parse_device_identity, "0x14 sn+format")
    if out:
        api = await client.identity()
        ok = api.serial == out.serial and len(api.serial) == 16
        record("L2", "0x14 api == raw parse", "PASS" if ok else "FAIL",
               f"{api.serial} {api.format_name}")

    out = await send_and_parse(client, commands.read_free_space(), Cmd.FREE_SPACE,
                               parsers.parse_free_space, "0x0F free space")
    if out:
        api = await client.free_space()
        record("L2", "0x0F api == raw parse",
               "PASS" if api.percent == out.percent and out.success else "FAIL",
               f"{api.percent}%")

    try:
        await send_and_parse(client, commands.read_has_import_file(), Cmd.HAS_IMPORT_FILE,
                             parsers.parse_has_import_file, "0x0D import flag")
    except ProtocolError as exc:
        record("L2", "0x0D import flag", "SKIP", f"device reports unsupported: {exc}")

    out = await send_and_parse(client, commands.read_record_state(), Cmd.RECORD_STATE,
                               parsers.parse_record_state, "0x02 record state (FFE2)",
                               write=Channel.CONTROL, listen=Channel.RECORD)
    if out:
        api = await client.record.state()
        ok = api.state == out.state and api.mac == out.mac and len(api.mac) == 12
        record("L2", "0x02 api == raw parse", "PASS" if ok else "FAIL",
               f"{api.state.name} mac={api.mac}")

    for st, label in (
        (SettingType.SLEEP_TIMEOUT_SEC, "0xB1 sleep timeout"),
        (SettingType.MAX_RECORD_MINUTES, "0xB1 max record"),
        (SettingType.USB_MASS_STORAGE, "0xB1 usb switch"),
    ):
        parsed = await send_and_parse(
            client, commands.read_setting(st), Cmd.SETTING_READ,
            lambda f, _st=st: parsers.parse_setting(f, _st), label,
        )
        if parsed:
            api = await client.settings.read(st)
            record("L2", f"{label} api == raw",
                   "PASS" if api.value == parsed.value else "FAIL", f"{api.value}")

    t0 = time.time()
    entries = await client.files.list()
    elapsed = (time.time() - t0) * 1000
    bad = [e for e in entries if not e.name.lower().endswith(".mp3") or e.size <= 0]
    record("L2", "0x13 file list", "PASS" if not bad else "FAIL",
           f"{len(entries)} files in {elapsed:.0f}ms" if entries else "0 files (empty-list frame ok)",
           {"files": [{"name": e.name, "size": e.size, "dur": e.duration_sec,
                       "fmt": e.format_name, "at": str(e.recorded_at)} for e in entries]})
    if entries:
        first = min(entries, key=lambda e: e.size)
        record("L2", "0x13 entry well-formed", "PASS",
               f"{first.name} size={first.size} dur={first.duration_sec}s at={first.recorded_at}")

    try:
        raw = await client.raw.request(
            build_frame(0x77), predicate=code_predicate(0x77), timeout=5
        )
        if raw[2] == int(Cmd.ERROR):
            record("L2", "0x00 error frame", "PASS",
                   f"undefined cmd rejected with 0x{raw[3]:02X}", {"frame": raw.hex().upper()})
        else:
            record("L2", "0x00 error frame", "WARN", "device answered an undefined command")
    except MemoTalkError as exc:
        record("L2", "0x00 error frame", "WARN", f"no error frame: {exc}")


# ---------------------------------------------------------------------------
# L3 - dynamic flows
# ---------------------------------------------------------------------------
async def stage_l3(client: MemoTalkClient) -> None:
    created: list[str] = []

    # 0xA4 off -> 0xA8 level stream
    try:
        await client.record.set_realtime(False)
        state = await client.record.start()
        created.append(state.filename)
        record("L3", "0x80 start (level mode)", "PASS",
               f"state={state.state.name} file={state.filename[-24:]}")

        levels: list[float] = []

        async def consume():
            async for lv in client.stream.level():
                levels.append(lv.db)
                if len(levels) >= 60:
                    return

        task = asyncio.create_task(consume())
        await asyncio.sleep(3.0)
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        await client.record.stop()

        if levels:
            lo, hi = min(levels), max(levels)
            sane = all(-121.0 <= v <= 1.0 for v in levels)
            record("L3", "0xA8 level stream", "PASS" if sane else "FAIL",
                   f"{len(levels)} frames, {lo:.1f}..{hi:.1f} dB",
                   {"count": len(levels), "min": lo, "max": hi})
        else:
            record("L3", "0xA8 level stream", "FAIL", "0 frames in 3s")
    except MemoTalkError as exc:
        record("L3", "0x80 start (level mode)", "FAIL", f"{type(exc).__name__}: {exc}")

    # 0xA4 on -> 0x90 mp3 stream
    try:
        await client.record.set_realtime(True)
        state = await client.record.start()
        created.append(state.filename)
        record("L3", "0x80 start (mp3 mode)", "PASS", f"file={state.filename[-24:]}")

        chunks: list[bytes] = []

        async def consume_audio():
            async for chunk in client.stream.audio():
                chunks.append(chunk)
                if len(chunks) >= 200:
                    return

        task = asyncio.create_task(consume_audio())
        await asyncio.sleep(3.0)
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        await client.record.stop()
        await client.record.set_realtime(False)

        total = sum(len(c) for c in chunks)
        sized = bool(chunks) and all(len(c) == 288 for c in chunks)
        sync_ok = bool(chunks) and all(c[:4] == MP3_SYNC for c in chunks)
        record("L3", "0x90 mp3 stream", "PASS" if sized and sync_ok else "FAIL",
               f"{len(chunks)} frames / {total} bytes, 288B={sized}, mp3sync={sync_ok}",
               {"count": len(chunks), "bytes": total,
                "first_frame": chunks[0][:16].hex() if chunks else ""})
    except MemoTalkError as exc:
        record("L3", "0x80 start (mp3 mode)", "FAIL", f"{type(exc).__name__}: {exc}")

    entries = await client.files.list()
    by_name = {e.name for e in entries}
    landed = [n for n in created if n in by_name]
    record("L3", "recorded files persisted", "PASS" if landed else "WARN",
           f"{len(landed)}/{len(created)} visible in 0x13")

    # 0x84 download: byte exact + deterministic
    if entries:
        target = min(entries, key=lambda e: e.size)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                t0 = time.time()
                p1 = await client.files.download(target, tmp, overwrite=True)
                dt = (time.time() - t0) * 1000
                data1 = p1.read_bytes()
                sha1 = hashlib.sha256(data1).hexdigest()
                p2 = await client.files.download(target, tmp, overwrite=True)
                sha2 = hashlib.sha256(p2.read_bytes()).hexdigest()

                size_ok = len(data1) == target.size
                det = sha1 == sha2
                mp3_ok = MP3_SYNC in data1[:2048]
                record("L3", "0x84 download",
                       "PASS" if size_ok and det and mp3_ok else "FAIL",
                       f"{len(data1)}B/{target.size}B in {dt:.0f}ms, deterministic={det}, mp3sync={mp3_ok}",
                       {"sha256": sha1, "declared": target.size, "got": len(data1),
                        "ms": round(dt), "head": data1[:32].hex()})
        except (MemoTalkError, OSError) as exc:
            record("L3", "0x84 download", "FAIL", f"{type(exc).__name__}: {exc}")

    # --- 0x84 failure frame: real hardware shape must be parsed correctly ---
    missing = "NOT_EXIST_000000.mp3"
    try:
        raw_fail = await client.raw.request(
            commands.sync_audio_file(missing, 0, SyncOp.START),
            predicate=code_predicate(Cmd.FILE_SYNC),
            timeout=5.0,
        )
        ack = parsers.parse_sync_ack(raw_fail)
        shape_ok = (
            not ack.exists
            and ack.error_code == 0x04
            and ack.filename == missing
            and ack.total_packets == 0
            and len(raw_fail) == len(missing) + 9
        )
        record(
            "L3", "0x84 failure frame parsed", "PASS" if shape_ok else "FAIL",
            f"exists={ack.exists} err=0x{ack.error_code:02X} packets={ack.total_packets} "
            f"size={len(raw_fail)} (expect name+9={len(missing) + 9})",
            {"frame": raw_fail.hex().upper(), "len": len(raw_fail), "name": ack.filename},
        )
    except MemoTalkError as exc:
        record("L3", "0x84 failure frame parsed", "FAIL", f"{type(exc).__name__}: {exc}")

    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            await client.files.download(missing, tmp, size=128, ack_timeout=2.0)
        record("L3", "0x84 missing -> not found", "FAIL", "download unexpectedly succeeded")
    except FileNotFoundOnDevice:
        record("L3", "0x84 missing -> not found", "PASS",
               f"FileNotFoundOnDevice in {time.time() - t0:.1f}s")
    except MemoTalkTimeout:
        record("L3", "0x84 missing -> not found", "FAIL", "still a bare timeout")
    except Exception as exc:  # noqa: BLE001
        record("L3", "0x84 missing -> not found", "FAIL", f"{type(exc).__name__}: {exc}")

    # safety guard: 0x9A must refuse before touching the device
    try:
        await client.files.clear_all(confirm=False)
        record("L3", "0x9A guard", "FAIL", "clear_all(confirm=False) did NOT raise")
    except ConfirmationRequired:
        record("L3", "0x9A guard", "PASS", "refused locally, nothing sent to device")

    # cleanup: only what this run created
    for name in landed:
        try:
            await client.files.delete(name)
            record("L3", f"0x92 cleanup ...{name[-16:]}", "PASS", "deleted own test recording")
        except MemoTalkError as exc:
            record("L3", f"0x92 cleanup ...{name[-16:]}", "WARN", f"{type(exc).__name__}: {exc}")

    after = await client.files.list()
    gone = all(n not in {e.name for e in after} for n in landed)
    record("L3", "0x13 reflects deletion", "PASS" if gone else "FAIL",
           f"{len(after)} files remain")


# ---------------------------------------------------------------------------
# L4 - hotspot + credentials (never joins the AP)
# ---------------------------------------------------------------------------
async def stage_l4(client: MemoTalkClient) -> None:
    try:
        ack = await client.wifi.enable(ready_delay=2.0)
        record("L4", "0x87 hotspot on", "PASS" if ack.success else "FAIL", f"state={ack.state}")

        creds = await client.wifi.credentials()
        record("L4", "0x0B/0x0C credentials", "PASS",
               f"ssid={creds.ssid!r} pwd_len={len(creds.password)}")

        ftp = await client.wifi.ftp()
        ip_ok = ftp.host.count(".") == 3 and ftp.host != "0.0.0.0"
        record("L4", "0x03-0x06 ftp config", "PASS" if ip_ok else "FAIL",
               f"{ftp.address} user={ftp.user!r} pwd_len={len(ftp.password)} ready={ftp.ready}")
    except MemoTalkError as exc:
        record("L4", "0x87 hotspot on", "FAIL", f"{type(exc).__name__}: {exc}")

    # close is retried AND verified through 0x03
    try:
        await client.wifi.disable()
        report = client.wifi.last_close_report or {}
        confirmed = bool(report.get("confirmed"))
        record("L4", "0x87 hotspot off", "PASS" if confirmed else "FAIL",
               f"confirmed={confirmed} attempts={report.get('attempts')}")
    except MemoTalkError as exc:
        record("L4", "0x87 hotspot off", "FAIL", f"{type(exc).__name__}: {exc}")

    # independent re-check, plus prove the device is back in recording mode
    try:
        still_on = await client.wifi.hotspot_state()
        record("L4", "0x03 confirms off", "PASS" if not still_on else "FAIL",
               f"hotspot_state={still_on}")
    except MemoTalkError as exc:
        record("L4", "0x03 confirms off", "WARN", f"{type(exc).__name__}: {exc}")

    try:
        state = await client.record.state()
        record("L4", "recording mode restored", "PASS",
               f"0x02 answers again: {state.state.name}")
    except MemoTalkError as exc:
        record("L4", "recording mode restored", "FAIL", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# E1 - 用真实副作用反向验证 ERRATA 结论
# ---------------------------------------------------------------------------
async def stage_errata(client: MemoTalkClient) -> None:
    st = SettingType.SLEEP_TIMEOUT_SEC
    cur = (await client.settings.read(st)).value
    record("E1", "current sleep timeout", "PASS", f"{cur}s")

    pred = code_predicate(Cmd.SETTING_WRITE)
    corrected = commands.write_setting(st, cur)
    raw_a = await client.raw.request(corrected, predicate=pred, timeout=5)
    if raw_a[2] == int(Cmd.SETTING_WRITE) and raw_a[3] == 0x01:
        record("E1", "library frame accepted", "PASS",
               f"{corrected.hex().upper()} -> result=1", {"frame": corrected.hex().upper()})
    else:
        record("E1", "library frame accepted", "FAIL",
               f"{corrected.hex().upper()} -> 0x{raw_a[3]:02X}",
               {"frame": corrected.hex().upper()})

    if cur == 300:
        doc_frame = bytes.fromhex("5A06B0002C01C790")
        raw_b = await client.raw.request(doc_frame, predicate=pred, timeout=5)
        if raw_b[2] == int(Cmd.ERROR):
            record("E1", "spec frame rejected", "PASS",
                   f"{doc_frame.hex().upper()} -> error 0x{raw_b[3]:02X} => ERRATA E1 confirmed",
                   {"frame": doc_frame.hex().upper(), "error": f"0x{raw_b[3]:02X}"})
        else:
            record("E1", "spec frame rejected", "WARN",
                   "device accepted a wrong-CRC frame: CRC not enforced on 0xB0")
    else:
        record("E1", "spec frame rejected", "SKIP", f"current value {cur}s != 300s case")

    await client.raw.request(commands.write_setting(st, cur), predicate=pred, timeout=5)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="l1,l2,l3,l4,e1")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    want = {s.strip().lower() for s in args.stages.split(",")}

    print("=" * 86)
    print("MemoTalk S1 / AI-PEN hardware regression")
    print("=" * 86)

    try:
        client = await MemoTalkClient(
            prefer="MemoTalk S1", keepalive_interval=4.0, scan_timeout=8.0
        ).connect()
    except Exception as exc:
        record("L1", "connect", "FAIL", f"{type(exc).__name__}: {exc}")
        print("\nTOTAL:", summarize())
        return 2

    try:
        if "l1" in want:
            await stage_l1(client)
        if "l2" in want:
            await stage_l2(client)
        if "l3" in want:
            await stage_l3(client)
        if "l4" in want:
            await stage_l4(client)
        if "e1" in want:
            await stage_errata(client)
    finally:
        await client.close()

    counts = summarize()
    print("-" * 86)
    print("TOTAL:", counts)
    if args.out:
        Path(args.out).write_text(
            json.dumps({"counts": counts, "results": RESULTS}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("report ->", args.out)
    return 0 if counts.get("FAIL", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
