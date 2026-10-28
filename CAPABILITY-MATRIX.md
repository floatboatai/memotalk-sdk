# 两端设计：硬件原子能力 ↔ 库原子能力

本文档说明一件事：**底层硬件能做什么，顶层库就应暴露什么；两者之间的映射必须可逐条查证。**

设计只有三条原则：

1. **不丢能力**：硬件每一条业务指令，顶层都有一个可调用的入口。不存在「因库未实现而无法做到」的缺口。
2. **不造能力**：库不引入协议之外的行为，也不宣称支持硬件并不具备的能力（例如非 macOS 环境的自动入网）。
3. **组合必须说明理由，并分两类**：
   - **安全性组合**：分开调用必然出错，才允许合并为单个 API，如开热点 + 读凭据 + 关热点、请求 + 收包 + 补包。
   - **便利性组合**：只是把若干原子调用串起来，不增加新语义，每一步仍可单独调用，如 `info()`、`read_all()`。

---

## 一、下层：硬件原子能力（25 个协议码 = 22 可发 + 2 只推送 + 1 错误帧）

> 码表以 `memotalk/constants.py` 的 `Cmd` 枚举为准。`0xFF`（文件列表包头）与 `FF FF FF`（数据包头）是帧内标记，不计入。

### 1.1 物理通道

| 服务 | 特征 | 方向 | 职责 | 硬约束 |
| :-- | :-- | :-- | :-- | :-- |
| `FFF0` | `FFF1` | 写 | 除 `0x80` 外的**全部**指令 | `write-without-response` |
| `FFF0` | `FFF2` | 通知 | 除 `0x02`/`0x80` 外的全部指令应答、`0x13` 文件列表、`0x84` ack、`0x00` 错误帧（也可能从 FFE2 返回） | `notify` |
| `FFE0` | `FFE1` | 写 | **仅** `0x80` 录音控制 | `write-without-response` |
| `FFE0` | `FFE2` | 通知 | `0x02`/`0x80` 状态、`0xA8` 电平、`0x90` 语音、`0x84` 数据包 | `notify` |

> **跨服务的两处例外**：
> - `0x02` 写入 `FFF1`，**应答帧**却从 `FFE2` 返回，这是唯一跨服务的指令应答。
> - `0x84` 写入 `FFF1`，ack 回到 `FFF2`，但**数据包**从 `FFE2` 推送。

### 1.2 三种帧格式（低 MTU 拆包的根源）

| 帧型 | 长度规则 | 总长 | 使用者 |
| :-- | :-- | :-: | :-- |
| 标准控制帧 | `5A` + `len`(1B) + `cmd` + payload + CRC16 LE；总长 `2 + len` | 5 ~ 242 | 全部指令与应答 |
| `0x90` 实时帧 | `5A` + 3B LE 长度 `0x000123`(291) + `90` + 288B + 2B；总长 `4 + 291` | **295** | 实时 MP3 语音流 |
| `0x84` 数据包 | 3B 头 + 3B 序号 + 320B 数据 + 2B CRC | **328** | BLE 文件同步 |

> 标准帧下限 5 对应无 payload 的请求（如读电量 `5A 03 01 C0 80`），设备应答最短为 6B。上限 242 来自 `FRAME_MAX_PAYLOAD = 237`，只在发送端校验。

外加 `0xFF` 文件列表流：**每包自定界**。包内第 1-2 字节有长度字段，但语义有歧义（不等于整帧长度，见 ERRATA）。列表靠 `0x13`/`0x1D`/`0x2D` 标记推进，不做 CRC 校验。完成判定有三个条件：条目数达到声明值、收到 `0x2D` 尾包、声明条目数为 0；单包时另有「包长 ≥ 声明长度」的兜底。

### 1.3 22 条可发指令（硬件动作全集）

| # | 码 | 硬件原子动作 | 通道（写→听） | 应答解析的关键点 |
| :-: | :-: | :-- | :-- | :-- |
| 1 | `0x01` | 读电量（兼心跳） | FFF1→FFF2 | 1B，0~10 档 |
| 2 | `0x02` | 读录音状态 + 真实 MAC | FFF1→**FFE2** | 20B，含 13B 文件 ID |
| 3 | `0x03` | 读动态 FTP IP | FFF1→FFF2 | UTF-8；热点关闭时也应答，值为 `0.0.0.0`（可作开关探测） |
| 4 | `0x04` | 读动态 FTP 端口 | FFF1→FFF2 | UTF-8 数字串 |
| 5 | `0x05` | 读 FTP 账号 | FFF1→FFF2 | UTF-8 |
| 6 | `0x06` | 读 FTP 密码 | FFF1→FFF2 | XOR 0xFF |
| 7 | `0x09` | 读固件版本 | FFF1→FFF2 | 3B ASCII |
| 8 | `0x0B` | 读热点 SSID | FFF1→FFF2 | UTF-8 |
| 9 | `0x0C` | 读热点密码 | FFF1→FFF2 | XOR 0xFF |
| 10 | `0x0D` | 有无导入文件 | FFF1→FFF2 | `0xFF` = 不支持 WiFi |
| 11 | `0x0F` | 剩余空间 | FFF1→FFF2 | 状态 + 百分比 |
| 12 | `0x13` | 列录音文件 | FFF1→FFF2 | `0xFF` 多包 + 空列表特例 |
| 13 | `0x14` | SN + 存储格式 + 转录态 | FFF1→FFF2 | 16B SN + 1B + 1B |
| 14 | `0xB1` | 读配置项 | FFF1→FFF2 | 3 种类型，两种布局 |
| 15 | `0x91` | RTC 对时握手 | FFF1→FFF2 | 连接（两路 notify 订阅完成）后 **1 秒内必发**，7B 纯 BCD |
| 16 | `0x80` | 录音启/停/暂停/恢复 | **FFE1**→FFE2 | 20B，结果可能是"存储满/电量低" |
| 17 | `0x84` | BLE 拉文件流 | FFF1→FFF2（ack）+ FFE2（数据包） | ack 尾部有两种布局：文件存在 5B、不存在 3B（ERRATA E11） |
| 18 | `0x87` | 热点开关 | FFF1→FFF2 | `[state][result]` |
| 19 | `0x92` | 删单个文件 | FFF1→FFF2 | 失败码 4/5/6/7 |
| 20 | `0x9A` | 清空全部 | FFF1→FFF2 | **不可逆** |
| 21 | `0xA4` | 实时流二选一开关 | FFF1→FFF2 | `[state][result]` |
| 22 | `0xB0` | 写配置项 | FFF1→FFF2 | 单 result 字节 |

> `0x03`~`0x06`、`0x0B`、`0x0C` 只有在热点（`0x87`）开启后才返回有效值。

### 1.4 FFE2 上的推送

**只推送、不可请求（2 条）**

| 码 | 内容 | 触发条件 | 形状 |
| :-: | :-- | :-- | :-- |
| `0xA8` | 麦克风电平 | 录音中且 `0xA4=0` | 9B，IEEE754 LE，`-120~0 dB`，真机实测约 16.7Hz（50 帧 / 3s） |
| `0x90` | 实时 MP3 | 录音中且 `0xA4=1` | 295B，其中 288B 音频 ≈ 72ms（见下方注） |

**可请求、同时可能主动推送（已计入 1.3）**

| 码 | 内容 | 推送场景 | 形状 |
| :-: | :-- | :-- | :-- |
| `0x02` | 录音状态 | 录音期间状态变更 | 20B |
| `0x80` | 录音控制应答 | 每次 `0x80` 后，可能重复 | 20B |

> - `0x02`/`0x80` 的主动推送和 `0x80` 的重复应答，目前出自协议文档和代码注释，真机回归里**没有专门的实测记录**。
> - 288B 的帧划分：真机同步头 `FF F3 48 C4` 解码为 MPEG-2 Layer III、32kbps、16kHz、单声道，单帧 = 72×32000/16000 = 144B（36ms），即 288B = **2×144B = 72ms**。协议文档另称「4 秒约 102 帧」（约 14 帧/秒），与 72ms/块（约 13.9 块/秒）基本一致，两处「帧率」指的是不同的东西。

### 1.5 `0x00` 异常帧（协议定义的错误语义）

| 码 | 含义 | 库中的异常类 |
| :-: | :-- | :-- |
| `0x00` | 超时 | `DeviceErrorResponse` |
| `0x01` | 长度超范围 | `DeviceErrorResponse` |
| `0x02` | 校验错 | `DeviceErrorResponse` |
| `0x03` | 禁止操作（如快传期间录音） | `OperationForbidden` |
| `0x04` + 原命令 | 无效命令 | `DeviceErrorResponse` |
| `0x05` + 原参数 | 无效参数 | `InvalidParameter` |

> 错误帧的 `payload`（`frame[4:-2]`，不含 CRC）承载回显内容，`DeviceErrorResponse.echoed` 与之相同（0x04 回显原命令，0x05 回显原参数）。

> ⚠️ **真机上很少见到这种帧**：对非法指令、坏 CRC 等，设备实测都是**不应答**（见 4.3 第 2 条）。调用方处理错误应以 `MemoTalkTimeout` 为主，不能只捕获上表中的异常。

---

## 二、上层：库的原子能力

顶层并非将 22 个函数改名，而是按**能力域**分组，使调用方无需接触原始字节。

```
client
├── connect() / close() / async with   is_connected / transport
├── info()                    ← 便利性组合快照
├── battery / firmware / identity / free_space / has_import_file / sync_time
├── record     录音生命周期
├── stream     主动推送流
├── files      文件资产
├── settings   设备配置
├── wifi       热点与 FTP（含事务边界）→ session() 产出 NetworkSession
└── raw        原始指令入口
```

### 2.1 完整映射矩阵

| 上层 API | 底层码 | 返回类型 | 库承担的责任 |
| :-- | :-: | :-- | :-- |
| `client.battery()` | `0x01` | `BatteryInfo(level, percent)` | 换算 ×10% |
| `client.firmware()` | `0x09` | `FirmwareInfo`（字段 `raw`，属性 `version`） | `"206"` → `2.0.6` |
| `client.identity()` | `0x14` | `DeviceIdentity` | SN 去填充、格式名 |
| `client.free_space()` | `0x0F` | `FreeSpace` | 状态位语义化 |
| `client.has_import_file()` | `0x0D` | `bool` | `0xFF` → 抛异常 |
| `client.sync_time(when=None)` | `0x91` | `bool` | BCD 编码；`auto_handshake=True`（默认）时连接后自动调用一次：超时/传输错误只记 warning，CRC 等 `ProtocolError` 会让 `connect()` 抛出，设备回失败时静默返回 `False`；若设备回 `0x00` 错误帧，会被当成应答解析，不抛类型化异常 |
| `record.state()` | `0x02` | `RecordState` | **正确的听通道 FFE2** |
| `record.start/pause/resume/stop()` | `0x80` | `RecordState` | **写 FFE1**；非成功结果（失败/存储满/电量低）转 `RecordingRejected`。**不去重重复应答** |
| `record.set_realtime(bool)` | `0xA4` | `AckResult` | 切换 FFE2 推送 `0x90`/`0xA8`；**不检查 `success`、不抛异常**，调用方须自查 |
| `stream.level()` | `0xA8` | `AsyncIterator[AudioLevel]` | 拆包 + 浮点解码 + `normalized` |
| `stream.audio()` | `0x90` | `AsyncIterator[bytes]` | 拆包 + 截出 288B |
| `stream.states()` | `0x02`/`0x80` | `AsyncIterator[RecordState]` | 与指令应答共用解复用；会收到 `0x80` 的重复应答 |
| `stream.packets(predicate, channel)` | 任意推送 | `AsyncIterator[bytes]` | 原始帧订阅 |
| `files.list()` | `0x13` | `List[AudioFileEntry]` | 三段分包 + 空列表 |
| `files.latest()` | `0x13` | `Optional[AudioFileEntry]` | 按时间戳取最新一项，空列表返回 `None`（便利） |
| `files.download(target, dest_dir=None, *, size, max_retries=3, packet_timeout, ack_timeout, progress, overwrite=False)` | `0x84` | `Path` | ack 尾解析 + 乱序归位 + **缺包/CRC 错包自动续传**（`max_retries=3`）+ 按 size 截断；ack 超时时用 `0x13` 区分 `FileNotFoundOnDevice` 与 `MemoTalkTimeout`；期间暂停保活 |
| `files.download_all(dest_dir=None, *, progress)` | `0x13` + `0x84`×N | `List[Path]` | 串行下载全部（便利）；**强制 `overwrite=True`** |
| `files.delete()` | `0x92` | `bool` | 文件名非法 → `DeviceRejected`；失败码 → `DeleteFailed` |
| `files.clear_all(confirm=)` | `0x9A` | `bool` | **强制二次确认**：未确认时本地抛 `ConfirmationRequired`，不发包 |
| `files.sync_control(filename, operation, start_packet=0)` | `0x84` | `SyncAck` | 手动暂停/取消的入口 |
| `settings.read(setting_type)` | `0xB1` | `SettingValue` | 两种布局自动判别 |
| `settings.read_all()` | `0xB1`×3 | `DeviceSettings` | 单项失败跳过，该字段保持 `None`（便利） |
| `settings.write(setting_type, value)` | `0xB0` | `bool` | 取值范围前置校验，越界抛 **`ValueError`**（不属于 `MemoTalkError`）；设备拒绝时返回 `False`，不抛 |
| `settings.sleep_timeout(seconds=None)` | `0xB1`/`0xB0` | `int` | getter/setter 合一；0 或 15~7200 秒 |
| `settings.max_record_minutes(minutes=None)` | `0xB1`/`0xB0` | `int` | getter/setter 合一；1~480 分钟 |
| `settings.usb_mass_storage(enabled=None)` | `0xB1`/`0xB0` | `bool` | getter/setter 合一 |
| `wifi.enable(*, ready_delay=1.5, timeout=20.0)` | `0x87(1)` | `AckResult` | 被拒 → `DeviceRejected`；成功后**固定等待** `ready_delay` 秒供 DHCP/FTP 就绪（不轮询）；默认超时 20s |
| `wifi.disable(*, timeout=20.0, attempts=3, verify=True)` | `0x87(0)` | `AckResult` | 逐次重试，每次用 `0x03` 回读确认已关；结果记入 `wifi.last_close_report`；最终未确认抛 `DeviceRejected` |
| `wifi.hotspot_state()` | `0x03` | `bool` | 地址非 `0.0.0.0` 即视为开启 |
| `wifi.credentials()` | `0x0B`+`0x0C` | `WifiCredentials` | XOR 解密；仅热点开启时有意义 |
| `wifi.ftp()` | `0x03`~`0x06` | `FtpConfig` | 端口转 int（失败为 0）、`ready` 判定 |
| `wifi.session(*, ready_delay=1.5, timeout=20.0, close_timeout=20.0)` | `0x87(1)`…`0x87(0)` | 异步上下文管理器，`async with` 得到 `NetworkSession` | enable 抛 `MemoTalkError` 时也会补关（`disable(attempts=2)`，最多试 2 次）；任务被取消时不补关；正常/异常退出都在 `finally` 中关闭（见 2.2 的保证边界） |
| `NetworkSession.credentials` / `ftp_config` / `ssid` / `password` | — | `WifiCredentials` / `FtpConfig` / `str` | 会话期间读到的凭据快照 |
| `NetworkSession.open_ftp()` | FTP | `ftplib.FTP` | `ready` 为假抛 `ConnectionError`；PASV 登录 |
| `NetworkSession.list_audio_files(ftp)` / `download(ftp, filename, dest_dir)` | FTP | `List[str]` / `Path` | FTP 侧列表与下载（**实际传输未经真机验证**） |
| `NetworkSession.join_hotspot(iface="en0", wait=15.0)` | — | `Optional[str]` | **仅 macOS**（`networksetup`）；其他平台抛 `CapabilityUnavailable` |
| `raw.send(frame, channel)` | 任意 | `None` | 只写不等 |
| `raw.request(...)` / `raw.expect(...)` | 任意 | `bytes` | 无；可写任意通道，绕过所有路由保护 |

> 三个 getter/setter 包装（`sleep_timeout` / `max_record_minutes` / `usb_mass_storage`）作 setter 时**忽略 `write()` 的返回值**，原样返回传入值；设备拒绝写入时调用方无法从返回值得知，需要再读一次确认。

### 2.2 组合 API（对应原则 3）

**安全性组合**：分开调用必然出错，这是仅有的两个。

| 组合 API | 为什么必须合并 | 保证边界 |
| :-- | :-- | :-- |
| `wifi.session()` | 开热点后忘记关 → 设备无法录音且极耗电。用 `try/finally` 把「尝试关闭」变成语言级保证；enable 中途失败也会补关（最多试 2 次）。 | 只保证**一定会尝试关**。关闭最终失败时异常被吞掉，需查 `wifi.last_close_report` 确认结果。即使确认已关，设备仍可能拒绝录音数十秒（见 4.3 第 3 条）。 |
| `files.download()` | `0x84` 只管发 ack 和推包，丢包是常态。合并「发请求 → 收包 → 算缺包 → 从缺口重传 → 截断落盘」。 | 重试 `max_retries` 次后仍不完整，抛 `ProtocolError`。 |

**便利性组合**：只是串行调用，每一步都可以单独调用，不增加新语义。

| 组合 API | 构成 |
| :-- | :-- |
| `client.info()` | 默认 9 次串行读取：`0x01`、`0x09`、`0x14`、`0x0F`、`0x02`、`0xB1`×3、`0x0D`；`include_files=True` 时再加 `0x13`，共 10 次。`0x0D` 不支持时字段为 `None`。 |
| `settings.read_all()` | `0xB1`×3，单项失败跳过、字段为 `None` |
| `settings.sleep_timeout / max_record_minutes / usb_mass_storage` | `read` / `write` 的 getter/setter 包装 |
| `files.latest()` / `files.download_all()` | `list()` + 选取 / 循环 `download()` |

### 2.3 库替你挡住的错误（对照 `memotalk-ble-skill`）

> 「原 SKILL」指 `memotalk-ble-skill`：一套协议字典加参考脚本（`SKILL.md`、`scripts/ble_core.py` 等），是本库的前身。

| 风险 | 原 SKILL | 本库 |
| :-- | :-- | :-- |
| 写错通道 | 只写在文档里 | 每个 client 方法硬编码 `write=` 通道（路由不查表，唯一的写通道例外是 0x80→FFE1）；只有 `raw` 能写到任意通道 |
| `0x02` 听错通道 | 手工 `ffe2_q.get()` | `Channel.CONTROL` 写 / `Channel.RECORD` 听，显式传参 |
| 丢包 | 无处理，超时即失败 | 自动算 `missing_sequences()` 并续传 |
| 低 MTU 拆包 | 文档提示"自行拼接" | `FrameReassembler` 按三种帧长自动粘合 |
| CRC 错包 | 部分校验 | 经 parser 的标准指令应答校验 CRC；**不校验**的有 `0x13` 列表包、`0xA8` 电平帧、`0x00` 错误帧、`raw.*`；数据包逐包校验，错包按缺包续传（`crc_errors` 仅记录在内部 assembler，不返回调用方） |
| 忘记关热点 | 文档警告 | `wifi.session()` 结构性保证「一定尝试关闭」+ `0x03` 回读确认（边界见 2.2） |
| 误清空设备 | `confirm=True` | 未确认时本地抛 `ConfirmationRequired`、不发包；CLI 需 `--confirm` |
| 覆盖本地文件 | 无 | `files.download()` 默认 `FileExistsError`，需显式 `overwrite=True`；**例外**：`download_all()` 与 CLI download 强制覆盖 |
| 设备错误帧 | 无解析 | `0x00` 6 种错误码 → `OperationForbidden`（0x03）、`InvalidParameter`（0x05），其余为 `DeviceErrorResponse`；真机上很少出现（见 1.5） |
| 保活 | 文档声称存在，代码中未实现 | 后台 `0x01` 心跳，默认 30s，`keepalive_interval` 可配（≤0 关闭，无范围约束）；仅 `files.download()` 期间跳过心跳 |

---

## 三、明确不实现的能力

| 没做的 | 原因 | 调用方该怎么做 |
| :-- | :-- | :-- |
| 非 macOS 自动连热点 | 各平台网络 API 差异过大；表面支持而行为不确定，代价高于明确不支持。macOS 上由 `NetworkSession.join_hotspot()` 提供（CLI `--join`） | `wifi.session()` 获取凭据，由调用方使用本平台网络 API 连接；其他平台调用 `join_hotspot()` 会抛 `CapabilityUnavailable` |
| MP3 解码 / ASR | 硬件输出已是标准 MP3，交由 ffmpeg 或云端处理 | `stream.audio()` 直接送入解码器 |
| `0x07` 指令 | 协议文档仅在编号范围内提及，从未定义 | 不实现。若真机存在该指令，可用 `raw` 探测 |
| U 盘模式的实际挂载 | 只暴露开关位，不接管文件系统 | 用 `settings.usb_mass_storage()` 开关 |
| 多设备并发 | 一个 client 对一条链路 | 每台设备一个 `MemoTalkClient` |

---

## 四、验证状态

### 4.1 离线测试

运行：`uv run --extra dev pytest -q`（pytest 在可选依赖 `dev` 中，直接 `uv run pytest` 会报 `Failed to spawn: pytest`）。

| 层 | 测试文件 | 验证方式 |
| :-- | :-- | :-- |
| codec / commands | `tests/test_codec.py` | 指令构帧逐字节断言、CRC、拆帧 |
| parsers | `tests/test_parsers.py` | 真机抓包帧解析为类型化模型 |
| assemblers | `tests/test_assemblers.py` | 分包 / 乱序 / 丢包 / CRC 错 / 截断 |
| transport + client | `tests/test_client.py` | `memotalk/fake.py` 模拟器走完整 dispatch 链路 |

2026-10-28 实测四个文件全部通过。用例数以 pytest 输出为准，本文不写死。

### 4.2 真实 BLE 硬件（2026-10-22，`tools/selfcheck.py` + 手工实验）

明细见 [docs/HARDWARE-REGRESSION.md](docs/HARDWARE-REGRESSION.md)。三个 `docs/reg_*.json` 原始输出合计 **39 PASS / 2 WARN / 0 FAIL**；报告正文表格已按此校正，但复验轮次（含 1 次 FAIL）没有对应的 JSON，计数以 JSON 和复验文字为准。结论：**大部分通过，有 WARN，且有未覆盖项**，不能视为「全部通过」。

| 指令 | 真机状态 | 证据来源 |
| :-- | :-- | :-- |
| `0x91` | ✅ 连接握手 | selfcheck L1 |
| `0x01` `0x09` `0x14` `0x0F` `0x0D` `0x02` `0xB1` `0x13` | ✅ 读指令逐条比对（共 **8** 个码，不是 22 条） | selfcheck L2 |
| `0x80` | ✅ 录音启停 | selfcheck L3 |
| `0xA4`（及 `0xA8`/`0x90` 推送） | ✅ 间接验证：切换后分别收到 `0xA8`、`0x90` 流；未断言 `AckResult.success` | selfcheck L3 |
| `0x84` | ✅ 下载；文件不存在时的失败帧 → `FileNotFoundOnDevice` | selfcheck L3；失败帧仅见复验文字 |
| `0x92` | ✅ 成功删除；「文件不存在」失败码 | 成功删除：selfcheck L3。失败码：仅手工实验 |
| `0x87` `0x0B` `0x0C` `0x03`~`0x06` | ✅ 热点开关、凭据读取、`0x03` 回读确认关闭 | selfcheck L4；关闭确认见复验轮次 |
| `0xB0` | ✅ 仅通过 `raw` 发送，未经 `settings.write` API | selfcheck |
| `0x9A` | ❌ **从未发给设备**，只验证了本地 `ConfirmationRequired` 拦截（不可逆，拒绝在用户设备上测试） | selfcheck |
| `0x00` 错误帧 | ⚠️ WARN：发送未定义指令后设备无应答，没有收到错误帧 | selfcheck L2 |

**明确未验证**：
- FTP 实际传输：会切断主机网络。凭据读取已验证。
- `0x9A` 真实清空，以及坏 CRC 时的行为。
- `0x92` 错误码 5/6/7。
- 断连重连、多设备并发。
- `0x02`/`0x80` 的主动推送与重复应答。
- `0x90` 的帧划分（见 1.4）。

### 4.3 真机回归发现的三个硬件事实（影响设计）

1. **固件不校验 CRC 与长度字段** → 库仍生成正确 CRC，但不将 CRC 视为安全保证。
2. **失败语义大多是无应答，而非 `0x00` 错误帧**（例外：`0x92` 删除不存在的文件、`0x84` 请求不存在的文件会返回失败应答帧）→ `MemoTalkTimeout` 是实际的错误信号，超时值不宜过短。
3. **`0x87` 有过渡期锁** → 关闭热点必须容错重试，并用 `0x03` 回读确认（`wifi.disable()` 已实现）。**残留风险**：曾观察到 enable 被中断后，`0x03` 已回读为 `0.0.0.0`（热点已关），但 `0x80` 录音仍被拒绝，数十秒后自行恢复。也就是说，「热点已关」不等于「可以立即录音」，调用方遇到 `RecordingRejected` 时应退避重试。
