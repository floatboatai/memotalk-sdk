<div align="center">

# 🎙️ memotalk-sdk

**MemoTalk S1 / AI-PEN voice recorders — fully unlocked for Python.**

22 raw hardware commands distilled into 6 typed capability groups.<br>
Scan, connect, record, stream live audio, download — in ~10 lines.

[![CI](https://github.com/floatboatai/memotalk-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/floatboatai/memotalk-sdk/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/memotalk-sdk.svg)](https://pypi.org/project/memotalk-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/memotalk-sdk.svg)](https://pypi.org/project/memotalk-sdk/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-154%20passing-brightgreen)](tests/)

**[English](README.md)** · [简体中文](README.zh-CN.md)

</div>

---

## ✨ Highlights

- **🔌 Zero-dependency protocol layer.** `bleak` is only needed when talking to real hardware.
- **📦 Every reply is a `dataclass`.** No more hand-rolling `pkt[3:-2]` slices — batteries, file lists and settings arrive typed and validated.
- **🎮 Built-in device simulator.** Develop and test the full stack with no pen in hand.
- **🧪 154 unit tests, byte-level asserts from real captures.** The parser suite is literally a replay of genuine device traffic.
- **📡 Live audio & level streaming.** Real-time dB meter or 72 ms MP3 chunks while recording — ready for streaming ASR.
- **🚀 Wi-Fi fast transfer.** One context manager opens the hotspot, reads FTP credentials and hands you a live FTP session.

---

## 📥 Installation

```bash
pip install memotalk-sdk
```

Development install:

```bash
pip install -e ".[dev]"
```

> Dependency: `bleak >= 0.21` — only when connecting to a real device.

---

## 🚀 Quick Start

```python
import asyncio
from memotalk import MemoTalkClient

async def main():
    async with MemoTalkClient() as dev:          # scan + connect + time sync + keepalive
        print(await dev.battery())               # BatteryInfo(level=5, percent=50)

        for entry in await dev.files.list():
            print(entry.name, entry.size, entry.duration_sec)

        path = await dev.files.download((await dev.files.latest()).name, "./downloads")
        print("saved", path)

asyncio.run(main())
```

### 🎚️ Record + live level meter

```python
async with MemoTalkClient() as dev:
    await dev.record.set_realtime(False)         # 0xA4=0 -> pushes 0xA8 levels
    await dev.record.start()

    async for level in dev.stream.level():
        print(f"{level.db:6.1f} dB  {'#' * int(level.normalized * 30)}")

    await dev.record.stop()
```

### 🗣️ Record + live transcription

```python
async with MemoTalkClient() as dev:
    await dev.record.set_realtime(True)          # 0xA4=1 -> pushes 0x90 MP3
    await dev.record.start()

    async for chunk in dev.stream.audio():       # 288 bytes = 2 MP3 frames = 72 ms
        feed_asr(chunk)                          # straight into streaming ASR

    await dev.record.stop()
    await dev.record.set_realtime(False)
```

### 📶 Wi-Fi fast transfer

```python
async with MemoTalkClient() as dev:
    async with dev.wifi.session() as net:        # 0x87 on -> read creds -> 0x87 off
        print(net.ssid, net.password)
        print(net.ftp_config.address)

        net.join_hotspot()                       # auto-join on macOS
        ftp = net.open_ftp()                     # PASV login
        for name in net.list_audio_files(ftp):
            net.download(ftp, name, "./downloads")
        ftp.quit()
```

The hotspot is always closed on exit — even on exceptions.

### ⚙️ Settings

```python
async with MemoTalkClient() as dev:
    print(await dev.settings.read_all())         # DeviceSettings(...)
    await dev.settings.sleep_timeout(600)        # 15~7200, or 0 = default
    await dev.settings.max_record_minutes(120)   # 1~480
    await dev.settings.usb_mass_storage(True)
```

### 🔧 Escape hatch

```python
frame = build_frame(0x77)                        # any protocol code
reply = await dev.raw.request(frame, code=0x77, timeout=2.0)
```

---

## 🗺️ API Map

| Group | Methods |
| :-- | :-- |
| Status | `battery()` `firmware()` `identity()` `free_space()` `has_import_file()` `info()` `sync_time()` |
| Record | `record.start()` `pause()` `resume()` `stop()` `state()` `set_realtime()` |
| Stream | `stream.level()` `stream.audio()` `stream.states()` `stream.packets()` |
| Files | `files.list()` `latest()` `download()` `download_all()` `delete()` `clear_all()` `sync_control()` |
| Settings | `settings.read()` `read_all()` `write()` `sleep_timeout()` `max_record_minutes()` `usb_mass_storage()` |
| Network | `wifi.enable()` `disable()` `credentials()` `ftp()` `session()` |
| Raw | `raw.send()` `request()` `expect()` |

Full hardware ↔ SDK mapping: [CAPABILITY-MATRIX.md](CAPABILITY-MATRIX.md)

---

## 💻 CLI

```bash
memotalk scan
memotalk status --files
memotalk list
memotalk download --all --out ./downloads
memotalk record --seconds 5            # live level meter
memotalk record --seconds 5 --realtime # count received MP3 bytes
memotalk wifi --ls --download FILE.mp3
memotalk config --sleep 600 --usb 1
memotalk raw --hex 5A0301C080 --expect 01

# no hardware? use the simulator
memotalk --simulate status
```

---

## 🧱 Architecture

```
memotalk/
├── codec.py         CRC16 / 0x5A framing / length inference / BCD / XOR-FF
├── commands.py      22 pure command builders + channel routing table
├── parsers.py       every reply -> typed model (incl. 0x00 error frames)
├── assemblers.py    frame reassembler / 0x13 listing / 0x84 audio stream
├── transport.py     connect / write / request-reply correlation / keepalive
├── client.py        the 6 capability groups
├── discovery.py     scanning + macOS already-connected recovery
├── fake.py          device simulator
└── cli.py           command line
```

**Length inference.** The device uses three frame shapes (standard `2+len`, `0x90`'s 3-byte length → 295, `0x84`'s fixed 328). `codec.expected_frame_length()` derives the expected byte count from any prefix, so `FrameReassembler` stitches frames automatically — even on low-MTU platforms.

**Request-reply correlation.** Predicate subscriptions are registered *before* writing, so fast replies are never missed. `0x00` error frames are matched too and surface as typed exceptions.

**Loss-resilient transfer.** On `0x84` timeouts, `missing_sequences()` pinpoints the first missing packet and the transfer resumes from it — automatically, with bounded retries.

---

## 🧪 Testing

```bash
# uv (recommended; pytest lives in the dev extra)
uv run --extra dev pytest -q

# or stdlib
python -m unittest discover -s tests -v
```

| File | Covers |
| :-- | :-- |
| `test_codec.py` | CRC, framing, BCD, byte-level asserts for all 22 commands, range validation |
| `test_commands.py` | builders ↔ sendable codes 1:1, write-channel routing, version consistency |
| `test_parsers.py` | 24 real device captures → typed models |
| `test_assemblers.py` | split / out-of-order / lost packets / CRC errors / padding truncation |
| `test_client.py` | simulator-driven end-to-end flows |

Every hex literal in `test_parsers.py` comes from real device captures.

---

## 📚 Documentation

| Doc | Contents |
| :-- | :-- |
| [CAPABILITY-MATRIX.md](CAPABILITY-MATRIX.md) | Hardware atomic capability ↔ SDK capability, line by line |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [docs/CODE-STRUCTURE.md](docs/CODE-STRUCTURE.md) | Layered structure & porting notes |
| [docs/ERRATA.md](docs/ERRATA.md) | Protocol doc quirks & real-hardware findings |
| [docs/HARDWARE-REGRESSION.md](docs/HARDWARE-REGRESSION.md) | Hardware regression details & raw JSON |
| [docs/RELEASING.md](docs/RELEASING.md) | PyPI release process |

---

## 🛠️ Migrating from `memotalk-ble-skill`

The original SKILL stays put as a **protocol dictionary + reference scripts**; this package is the **installable client** built on top of it. `cmd_*` names were dropped — `cmd_read_battery()` is now `read_battery()`, so migrations are just a prefix away.

---

## ⭐ Show your support

If memotalk-sdk saves you an afternoon of BLE reverse-engineering, give it a star — it helps others find it.

---

## 📄 License

MIT — Floatboat & MemoTalk. See [LICENSE](LICENSE).
