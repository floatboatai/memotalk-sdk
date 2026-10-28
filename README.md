# memotalk-sdk

[![CI](https://github.com/floatboatai/memotalk-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/floatboatai/memotalk-sdk/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/memotalk-sdk.svg)](https://pypi.org/project/memotalk-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/memotalk-sdk.svg)](https://pypi.org/project/memotalk-sdk/)

MemoTalk S1 / AI-PEN 录音笔的 Python 客户端库。补齐了原 SKILL 缺失的**传输层**与**通用应答解析层**，把 22 条硬件指令封装成 6 组类型化的原子能力。

- 纯协议层无第三方依赖；仅在连接真实设备时需要 `bleak`
- 全部应答解析为 `dataclass`，无需手工切片 `pkt[3:-2]`
- 内置设备模拟器，无硬件即可开发与测试
- 154 个单元测试，断言基于真机抓包

---

## 安装

从 PyPI 安装：

```bash
pip install memotalk-sdk
```

从源码安装（开发用）：

```bash
pip install -e ".[dev]"
```

或直接从目录使用：

```bash
export PYTHONPATH=/path/to/memotalk-sdk
```

依赖：`bleak >= 0.21`（仅连接真实设备时需要）。

---

## 快速开始

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

### 录音 + 实时波形

```python
async with MemoTalkClient() as dev:
    await dev.record.set_realtime(False)         # 0xA4=0 -> 推 0xA8 电平
    await dev.record.start()                     # 0x80(0x01)

    async for level in dev.stream.level():
        print(f"{level.db:6.1f} dB  {'#' * int(level.normalized * 30)}")

    await dev.record.stop()
```

### 边录边转写

```python
async with MemoTalkClient() as dev:
    await dev.record.set_realtime(True)          # 0xA4=1 -> 推 0x90 MP3
    await dev.record.start()

    async for chunk in dev.stream.audio():       # 288 字节 = 2 个 MP3 帧 = 72ms
        feed_asr(chunk)                          # 送入流式识别

    await dev.record.stop()
    await dev.record.set_realtime(False)
```

### Wi-Fi 快传（热点保证关闭）

```python
async with MemoTalkClient() as dev:
    async with dev.wifi.session() as net:        # 0x87 开 -> 读凭据 -> ... -> 0x87 关
        print(net.ssid, net.password)
        print(net.ftp_config.address)

        net.join_hotspot()                       # macOS 自动入网；其它平台抛 CapabilityUnavailable
        ftp = net.open_ftp()                     # PASV 登录
        for name in net.list_audio_files(ftp):
            net.download(ftp, name, "./downloads")
        ftp.quit()
```

即使 `with` 块内抛出异常，热点仍会被关闭。

### 配置

```python
async with MemoTalkClient() as dev:
    print(await dev.settings.read_all())         # DeviceSettings(...)
    await dev.settings.sleep_timeout(600)        # 0xB0，范围 15~7200 或 0=默认
    await dev.settings.max_record_minutes(120)   # 1~480
    await dev.settings.usb_mass_storage(True)    # 0/1
```

### 原始指令入口

```python
frame = build_frame(0x77)                        # 任何协议码
reply = await dev.raw.request(frame, code=0x77, timeout=2.0)
```

---

## API 地图

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

## 命令行

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

# 没有硬件时用模拟器
memotalk --simulate status
```

---

## 分层结构

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

### 关键设计点

**帧长推断**是低 MTU 平台正常工作的前提。设备有三种帧型（标准 `2+len`、`0x90` 的 3 字节长度 → 295、`0x84` 固定 328），`codec.expected_frame_length()` 可由任意前缀推断应等待的字节数，`FrameReassembler` 据此自动拼接。

**请求-应答关联**通过谓词订阅实现，订阅在写入**之前**注册，因此不会遗漏快速应答。同时匹配 `0x00` 错误帧，将设备拒绝映射为类型化异常。

**丢包续传**：`0x84` 传输超时后，由 `missing_sequences()` 计算首个缺失包号，携带该包号重发 `0x84`，最多重试 N 次。

---

## 测试

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

其中 `test_parsers.py` 的全部 hex 字面量均取自协议文档中的真机抓包。

---

## 已知边界

- **已在真机验证**（MemoTalk S1 / 固件 206 / 2026-10-22）：链路、握手、保活、读指令逐条比对（`0x01`/`0x02`/`0x09`/`0x0D`/`0x0F`/`0x13`/`0x14`/`0xB1`×3）、录音、`0xA8`/`0x90` 实时流、`0x84` 下载与失败帧、`0x92` 删除、`0x87` 与凭据读取。三个 `docs/reg_*.json` 合计 39 PASS / 2 WARN / 0 FAIL；`0x9A` 从未发给设备，明细见 [docs/HARDWARE-REGRESSION.md](docs/HARDWARE-REGRESSION.md)。
- **FTP 实际传输未验证**：连入设备热点会切断主机网络，只验证了凭据可获得。
- 自动连热点仅 macOS。
- 不做 MP3 解码、不做 ASR、不做 U 盘挂载。
- `0x07` 指令在协议中仅有编号、无定义，未实现。

应用中必须处理的三个硬件事实：

1. 设备**不校验 CRC**，错误帧会被静默执行，因此不应依赖 CRC 保证操作正确。
2. 设备**不发送 `0x00` 错误帧**，失败表现为超时，因此超时后需判断该操作是否已经生效。
3. `0x87` 存在过渡期锁：中断 enable 会使设备短暂拒绝录音，因此关闭热点需容错重试并回读确认。

协议文档的缺陷与硬件实测发现见 [docs/ERRATA.md](docs/ERRATA.md)。

---

## 文档

| 文档 | 内容 |
| :-- | :-- |
| [CAPABILITY-MATRIX.md](CAPABILITY-MATRIX.md) | 硬件原子能力 ↔ 库原子能力的逐条对照 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |
| [docs/CODE-STRUCTURE.md](docs/CODE-STRUCTURE.md) | 分层结构与移植注意事项 |
| [docs/ERRATA.md](docs/ERRATA.md) | 协议文档的缺陷与真机实测发现 |
| [docs/HARDWARE-REGRESSION.md](docs/HARDWARE-REGRESSION.md) | 真机回归明细与原始 JSON |
| [docs/RELEASING.md](docs/RELEASING.md) | 发布到 PyPI 的流程 |

---

## 许可

MIT，版权人 Floatboat 与 MemoTalk。见 [LICENSE](LICENSE)。
