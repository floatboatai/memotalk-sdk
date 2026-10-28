<div align="center">

# 🎙️ memotalk-sdk

**MemoTalk S1 / AI-PEN 录音笔，为 Python 完全解锁。**

22 条硬件指令浓缩为 6 组类型化能力。<br>
扫描、连接、录音、实时流式取声、下载 —— 10 行代码搞定。

[![CI](https://github.com/floatboatai/memotalk-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/floatboatai/memotalk-sdk/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/memotalk-sdk.svg)](https://pypi.org/project/memotalk-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/memotalk-sdk.svg)](https://pypi.org/project/memotalk-sdk/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-154%20passing-brightgreen)](tests/)

[English](README.md) · **[简体中文](README.zh-CN.md)**

</div>

---

## ✨ 亮点

- **🔌 纯协议层，零依赖。** 只有连接真机时才需要 `bleak`。
- **📦 应答全部是 `dataclass`。** 告别手工切片 `pkt[3:-2]`——电量、文件列表、配置到手即类型化、已校验。
- **🎮 内置设备模拟器。** 手边没有笔，照样开发、测试完整链路。
- **🧪 154 个单元测试，断言来自真机抓包。** 解析测试套件就是真机流量的逐字节重放。
- **📡 实时音频与电平流。** 录音时实时 dB 电平条或 72ms MP3 分片——直接对接流式 ASR。
- **🚀 Wi-Fi 快传。** 一个上下文管理器开热点、读 FTP 凭据，递给你一条活的 FTP 连接。

---

## 📥 安装

```bash
pip install memotalk-sdk
```

源码安装（开发用）：

```bash
pip install -e ".[dev]"
```

> 依赖：`bleak >= 0.21` —— 仅连接真实设备时需要。

---

## 🚀 快速开始

```python
import asyncio
from memotalk import MemoTalkClient

async def main():
    async with MemoTalkClient() as dev:          # 扫描 + 连接 + 0x91 对时 + 保活
        print(await dev.battery())               # BatteryInfo(level=5, percent=50)

        for entry in await dev.files.list():
            print(entry.name, entry.size, entry.duration_sec)

        path = await dev.files.download((await dev.files.latest()).name, "./downloads")
        print("saved", path)

asyncio.run(main())
```

### 🎚️ 录音 + 实时波形

```python
async with MemoTalkClient() as dev:
    await dev.record.set_realtime(False)         # 0xA4=0 -> 推 0xA8 电平
    await dev.record.start()                     # 0x80(0x01)

    async for level in dev.stream.level():
        print(f"{level.db:6.1f} dB  {'#' * int(level.normalized * 30)}")

    await dev.record.stop()
```

### 🗣️ 边录边转写

```python
async with MemoTalkClient() as dev:
    await dev.record.set_realtime(True)          # 0xA4=1 -> 推 0x90 MP3
    await dev.record.start()

    async for chunk in dev.stream.audio():       # 288 字节 = 2 个 MP3 帧 = 72ms
        feed_asr(chunk)                          # 直接送入流式识别

    await dev.record.stop()
    await dev.record.set_realtime(False)
```

### 📶 Wi-Fi 快传

```python
async with MemoTalkClient() as dev:
    async with dev.wifi.session() as net:        # 0x87 开 -> 读凭据 -> 0x87 关
        print(net.ssid, net.password)
        print(net.ftp_config.address)

        net.join_hotspot()                       # macOS 自动入网
        ftp = net.open_ftp()                     # PASV 登录
        for name in net.list_audio_files(ftp):
            net.download(ftp, name, "./downloads")
        ftp.quit()
```

无论 `with` 块内发生什么，热点都会被可靠关闭。

### ⚙️ 配置

```python
async with MemoTalkClient() as dev:
    print(await dev.settings.read_all())         # DeviceSettings(...)
    await dev.settings.sleep_timeout(600)        # 0xB0，范围 15~7200 或 0=默认
    await dev.settings.max_record_minutes(120)   # 1~480
    await dev.settings.usb_mass_storage(True)    # 0/1
```

### 🔧 原始指令入口

```python
frame = build_frame(0x77)                        # 任何协议码
reply = await dev.raw.request(frame, code=0x77, timeout=2.0)
```

---

## 🗺️ API 地图

| 组 | 方法 |
| :-- | :-- |
| 状态 | `battery()` `firmware()` `identity()` `free_space()` `has_import_file()` `info()` `sync_time()` |
| 录音 | `record.start()` `pause()` `resume()` `stop()` `state()` `set_realtime()` |
| 流 | `stream.level()` `stream.audio()` `stream.states()` `stream.packets()` |
| 文件 | `files.list()` `latest()` `download()` `download_all()` `delete()` `clear_all()` `sync_control()` |
| 配置 | `settings.read()` `read_all()` `write()` `sleep_timeout()` `max_record_minutes()` `usb_mass_storage()` |
| 网络 | `wifi.enable()` `disable()` `credentials()` `ftp()` `session()` |
| 原始 | `raw.send()` `request()` `expect()` |

完整的两端映射见 [CAPABILITY-MATRIX.md](CAPABILITY-MATRIX.md)。

---

## 💻 命令行

```bash
memotalk scan
memotalk status --files
memotalk list
memotalk download --all --out ./downloads
memotalk record --seconds 5            # 打印实时电平
memotalk record --seconds 5 --realtime # 统计收到的 MP3 字节
memotalk wifi --ls --download FILE.mp3
memotalk config --sleep 600 --usb 1
memotalk raw --hex 5A0301C080 --expect 01

# 没有硬件？用模拟器
memotalk --simulate status
```

---

## 🧱 分层结构

```
memotalk/
├── codec.py         CRC16 / 0x5A 组帧 / 帧长推断 / BCD / XOR-FF
├── commands.py      22 个纯指令构建函数 + 通道路由表
├── parsers.py       全部应答 → 类型化模型（含 0x00 错误帧）
├── assemblers.py    帧重组器 / 0x13 列表 / 0x84 音频流
├── transport.py     连接 / 写 / 请求-应答关联 / 保活 / 断连
├── client.py        6 组能力
├── discovery.py     扫描 + macOS 已连接外设找回
├── fake.py          设备模拟器
└── cli.py           命令行
```

**帧长推断。** 设备有三种帧型（标准 `2+len`、`0x90` 的 3 字节长度 → 295、`0x84` 固定 328），`codec.expected_frame_length()` 可由任意前缀推断应等待的字节数，`FrameReassembler` 据此自动拼接——低 MTU 平台照样稳。

**请求-应答关联。** 谓词订阅在写入**之前**注册，快速应答绝不遗漏；`0x00` 错误帧同样被匹配，设备拒绝直接映射为类型化异常。

**丢包续传。** `0x84` 传输超时后，`missing_sequences()` 定位首个缺失包号并自动从该包续传，重试有界。

---

## 🧪 测试

```bash
# uv（推荐，pytest 在 dev extra 里）
uv run --extra dev pytest -q

# 或标准库
python -m unittest discover -s tests -v
```

| 文件 | 覆盖 |
| :-- | :-- |
| `test_codec.py` | CRC、组帧、BCD、22 条指令字节级断言、范围校验 |
| `test_commands.py` | 22 个 builder 与可发码严格一一对应、写通道归属、版本一致性 |
| `test_parsers.py` | 24 条真机抓包 → 类型化模型 |
| `test_assemblers.py` | 拆包、乱序、丢包、CRC 错、填充截断 |
| `test_client.py` | 模拟器驱动的端到端流程 |

`test_parsers.py` 的全部 hex 字面量均取自真机抓包。

---

## 📚 文档

| 文档 | 内容 |
| :-- | :-- |
| [CAPABILITY-MATRIX.md](CAPABILITY-MATRIX.md) | 硬件原子能力 ↔ 库原子能力的逐条对照 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |
| [docs/CODE-STRUCTURE.md](docs/CODE-STRUCTURE.md) | 分层结构与移植注意事项 |
| [docs/ERRATA.md](docs/ERRATA.md) | 协议文档的缺陷与真机实测发现 |
| [docs/HARDWARE-REGRESSION.md](docs/HARDWARE-REGRESSION.md) | 真机回归明细与原始 JSON |
| [docs/RELEASING.md](docs/RELEASING.md) | 发布到 PyPI 的流程 |

---

## 🛠️ 从 `memotalk-ble-skill` 迁移

原 SKILL 保持原位，继续充当**协议字典 + 参考脚本**；本库是在其之上构建的**可安装客户端**。`cmd_*` 命名已统一为动词开头——`cmd_read_battery()` 即 `read_battery()`，迁移只需去掉前缀。

---

## ⭐ 支持一下

如果 memotalk-sdk 帮你省下了一个下午的蓝牙逆向时间，欢迎点个 Star，让更多人看到它。

---

## 📄 许可

MIT —— Floatboat 与 MemoTalk。见 [LICENSE](LICENSE)。
