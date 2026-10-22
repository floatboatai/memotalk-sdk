# 两端设计：硬件原子能力 ↔ 库原子能力

本文档说明一件事：**底层硬件能做什么，顶层库就应暴露什么；两者之间的映射必须可逐条查证。**

设计只有三条原则：

1. **不丢能力**：硬件每一条业务指令，顶层都有一个可调用的入口。不存在「因库未实现而无法做到」的缺口。
2. **不造能力**：库不引入协议之外的行为，也不宣称支持硬件并不具备的能力（例如非 macOS 环境的自动入网）。
3. **只为必然成对的动作做组合**：仅当分开调用必然出错时，才允许合并为单个 API（开热点 + 读凭据 + 关热点；请求 + 收包 + 补包）。

---

## 一、下层：硬件原子能力（25 个协议码）

### 1.1 物理通道

| 服务 | 特征 | 方向 | 职责 | 硬约束 |
| :-- | :-- | :-- | :-- | :-- |
| `FFF0` | `FFF1` | 写 | 除 `0x80` 外的**全部**指令 | `write-without-response` |
| `FFF0` | `FFF2` | 通知 | 指令应答 + `0x13` 文件列表 | `notify` |
| `FFE0` | `FFE1` | 写 | **仅** `0x80` 录音控制 | `write-without-response` |
| `FFE0` | `FFE2` | 通知 | `0x02`/`0x80` 状态、`0xA8` 电平、`0x90` 语音、`0x84` 数据包 | `notify` |

> **唯一的路由例外**：`0x02` 写入 `FFF1`，应答却从 `FFE2` 返回。这是全协议唯一一处「写通道 ≠ 听通道」。

### 1.2 三种帧格式（低 MTU 拆包的根源）

| 帧型 | 长度规则 | 总长 | 使用者 |
| :-- | :-- | :-: | :-- |
| 标准控制帧 | `2 + len`（1B 长度） | 6 ~ 242 | 全部指令与应答 |
| `0x90` 实时帧 | `4 + (3B LE 长度 0x000123)` | **295** | 实时 MP3 语音流 |
| `0x84` 数据包 | 固定 | **328** | BLE 文件同步 |

外加 `0xFF` 文件列表流：**每包自定界**，不带总长度前缀，靠 `0x13`/`0x1D`/`0x2D` 标记推进。

### 1.3 22 条可发指令（硬件动作全集）

| # | 码 | 硬件原子动作 | 通道（写→听） | 应答解析的关键点 |
| :-: | :-: | :-- | :-- | :-- |
| 1 | `0x01` | 读电量（兼心跳） | FFF1→FFF2 | 1B，0~10 档 |
| 2 | `0x02` | 读录音状态 + 真实 MAC | FFF1→**FFE2** | 20B，含 13B 文件 ID |
| 3 | `0x03` | 读动态 FTP IP | FFF1→FFF2 | UTF-8，**须先开热点** |
| 4 | `0x04` | 读动态 FTP 端口 | FFF1→FFF2 | UTF-8 数字串 |
| 5 | `0x05` | 读 FTP 账号 | FFF1→FFF2 | UTF-8 |
| 6 | `0x06` | 读 FTP 密码 | FFF1→FFF2 | XOR 0xFF，**须先开热点** |
| 7 | `0x09` | 读固件版本 | FFF1→FFF2 | 3B ASCII |
| 8 | `0x0B` | 读热点 SSID | FFF1→FFF2 | UTF-8 |
| 9 | `0x0C` | 读热点密码 | FFF1→FFF2 | XOR 0xFF |
| 10 | `0x0D` | 有无导入文件 | FFF1→FFF2 | `0xFF` = 不支持 WiFi |
| 11 | `0x0F` | 剩余空间 | FFF1→FFF2 | 状态 + 百分比 |
| 12 | `0x13` | 列录音文件 | FFF1→FFF2 | `0xFF` 多包 + 空列表特例 |
| 13 | `0x14` | SN + 存储格式 + 转录态 | FFF1→FFF2 | 16B SN + 1B + 1B |
| 14 | `0xB1` | 读配置项 | FFF1→FFF2 | 3 种类型，两种布局 |
| 15 | `0x91` | RTC 对时握手 | FFF1→FFF2 | **1 秒内必发**，7B 纯 BCD |
| 16 | `0x80` | 录音启/停/暂停/恢复 | **FFE1**→FFE2 | 20B，结果可能是"存储满/电量低" |
| 17 | `0x84` | BLE 拉文件流 | FFF1→FFF2 + FFE2 | ack 的尾部字段定长 |
| 18 | `0x87` | 热点开关 | FFF1→FFF2 | `[state][result]` |
| 19 | `0x92` | 删单个文件 | FFF1→FFF2 | 失败码 4/5/6/7 |
| 20 | `0x9A` | 清空全部 | FFF1→FFF2 | **不可逆** |
| 21 | `0xA4` | 实时流二选一开关 | FFF1→FFF2 | `[state][result]` |
| 22 | `0xB0` | 写配置项 | FFF1→FFF2 | 单 result 字节 |

### 1.4 3 条只推送、不可请求的码

| 码 | 内容 | 触发条件 | 形状 |
| :-: | :-- | :-- | :-- |
| `0x02` | 录音状态变更 | 录音期间 | 20B |
| `0x80` | 录音控制应答 | 每次 `0x80` 后 | 20B，可能重复 1~2 次 |
| `0xA8` | 麦克风电平 | 录音中且 `0xA4=0` | 9B，IEEE754 LE，`-120~0 dB`，~10Hz |
| `0x90` | 实时 MP3 | 录音中且 `0xA4=1` | 295B = 288B 音频 = 3×96B 帧 = 72ms |

### 1.5 `0x00` 异常帧（硬件唯一的错误语义）

| 码 | 含义 |
| :-: | :-- |
| `0x00` | 超时 |
| `0x01` | 长度超范围 |
| `0x02` | 校验错 |
| `0x03` | 禁止操作（如快传期间录音） |
| `0x04` + 原命令 | 无效命令 |
| `0x05` + 原参数 | 无效参数 |

---

## 二、上层：库的原子能力

顶层并非将 22 个函数改名，而是按**能力域**分组，使调用方无需接触原始字节。

```
client
├── info()                    ← 组合快照
├── battery / firmware / identity / free_space / has_import_file / sync_time
├── record     录音生命周期
├── stream     主动推送流
├── files      文件资产
├── settings   设备配置
├── wifi       热点与 FTP（含事务边界）
└── raw        原始指令入口
```

### 2.1 完整映射矩阵

| 上层 API | 底层码 | 返回类型 | 库承担的责任 |
| :-- | :-: | :-- | :-- |
| `client.battery()` | `0x01` | `BatteryInfo(level, percent)` | 换算 ×10% |
| `client.firmware()` | `0x09` | `FirmwareInfo(raw, version)` | `"206"` → `2.0.6` |
| `client.identity()` | `0x14` | `DeviceIdentity` | SN 去填充、格式名 |
| `client.free_space()` | `0x0F` | `FreeSpace` | 状态位语义化 |
| `client.has_import_file()` | `0x0D` | `bool` | `0xFF` → 抛异常 |
| `client.sync_time()` | `0x91` | `bool` | BCD 编码 + 连接时自动调用 |
| `record.state()` | `0x02` | `RecordState` | **正确的听通道 FFE2** |
| `record.start/pause/resume/stop()` | `0x80` | `RecordState` | **写 FFE1**；非成功结果转 `RecordingRejected` |
| `record.set_realtime(bool)` | `0xA4` | `AckResult` | 布尔 → 0x90/0xA8 路由说明 |
| `stream.level()` | `0xA8` | `AsyncIterator[AudioLevel]` | 拆包 + 浮点解码 + `normalized` |
| `stream.audio()` | `0x90` | `AsyncIterator[bytes]` | 拆包 + 截出 288B |
| `stream.states()` | `0x02`/`0x80` | `AsyncIterator[RecordState]` | 与指令应答共用解复用 |
| `files.list()` | `0x13` | `List[AudioFileEntry]` | 三段分包 + 空列表 |
| `files.download()` | `0x84` | `Path` | ack 尾解析 + 乱序归位 + **缺包自动续传** + 按 size 截断 |
| `files.delete()` | `0x92` | `bool` | 失败码 → `DeleteFailed` |
| `files.clear_all(confirm=)` | `0x9A` | `bool` | **强制二次确认** |
| `files.sync_control()` | `0x84` | `SyncAck` | 手动暂停/取消的入口 |
| `settings.read(type)` | `0xB1` | `SettingValue` | 两种布局自动判别 |
| `settings.read_all()` | `0xB1`×3 | `DeviceSettings` | 单项失败不影响整体 |
| `settings.write(type, v)` | `0xB0` | `bool` | 取值范围前置校验 |
| `settings.sleep_timeout(sec=None)` | `0xB1`/`0xB0` | `int` | getter/setter 合一 |
| `wifi.enable()` | `0x87(1)` | `AckResult` | 含 DHCP 就绪等待 |
| `wifi.disable()` | `0x87(0)` | `AckResult` | — |
| `wifi.credentials()` | `0x0B`+`0x0C` | `WifiCredentials` | XOR 解密 |
| `wifi.ftp()` | `0x03`~`0x06` | `FtpConfig` | 端口转 int、`ready` 判定 |
| `wifi.session()` | `0x87(1)`…`0x87(0)` | `AsyncIterator[NetworkSession]` | **异常也必定关热点** |
| `raw.send/request/expect` | 任意 | `bytes` | 无 |

### 2.2 三个"只有组合才安全"的能力

这三个是唯一允许越过单指令粒度的 API，因为它们分开调用必然出错：

| 组合 API | 为什么必须合并 |
| :-- | :-- |
| `wifi.session()` | 开热点后忘记关 → 设备无法录音且极耗电。用 `try/finally` 把关闭变成语言级保证。 |
| `files.download()` | `0x84` 只管发 ack 和推包，丢包是常态。合并"发请求 → 收包 → 算缺包 → 从缺口重传 → 截断落盘"。 |
| `client.info()` | 状态表需 10 次串行读取才能拼装，调用方不应自行实现。 |

### 2.3 库替你挡住的错误（对照原 SDK）

| 风险 | 原 SDK | 本库 |
| :-- | :-- | :-- |
| 写错通道 | 只写在文档里 | `WRITE_CHANNEL` 表 + client 硬编码，无法写错 |
| `0x02` 听错通道 | 手工 `ffe2_q.get()` | `Channel.CONTROL` 写 / `Channel.RECORD` 听，显式传参 |
| 丢包 | 无处理，超时即失败 | 自动算 `missing_sequences()` 并续传 |
| 低 MTU 拆包 | 文档提示"自行拼接" | `FrameReassembler` 按三种帧长自动粘合 |
| CRC 错包 | 部分校验 | 指令应答全量校验；数据包逐包校验并记入 `crc_errors` |
| 忘记关热点 | 文档警告 | `wifi.session()` 结构性保证 |
| 误清空设备 | `confirm=True` | 保留，且 CLI 再加 `--confirm` |
| 覆盖本地文件 | 无 | 默认 `FileExistsError`，需显式 `overwrite=True` |
| 设备错误帧 | 无解析 | `0x00` 6 种错误码 → 类型化异常 |
| 保活 | 文档声称存在，代码中未实现 | 后台任务，间隔 25~60s 可配，传输期间自动暂停 |

---

## 三、明确不实现的能力

| 没做的 | 原因 | 调用方该怎么做 |
| :-- | :-- | :-- |
| 非 macOS 自动连热点 | 各平台网络 API 差异过大；表面支持而行为不确定，代价高于明确不支持 | `wifi.session()` 获取凭据，由调用方使用本平台网络 API 连接 |
| MP3 解码 / ASR | 硬件输出已是标准 MP3，交由 ffmpeg 或云端处理 | `stream.audio()` 直接送入解码器 |
| `0x07` 指令 | 协议文档仅在编号范围内提及，从未定义 | 不实现。若真机存在该指令，可用 `raw` 探测 |
| U 盘模式的实际挂载 | 只暴露开关位，不接管文件系统 | 用 `settings.usb_mass_storage()` 开关 |
| 多设备并发 | 一个 client 对一条链路 | 每台设备一个 `MemoTalkClient` |

---

## 四、验证状态

| 层 | 验证方式 | 状态 |
| :-- | :-- | :-- |
| codec / commands | 对照 24 条真机抓包逐字节断言 | ✅ 通过 |
| parsers | 真机抓包解析为类型化模型 | ✅ 通过 |
| assemblers | 分包/乱序/丢包/CRC 错/截断 | ✅ 通过 |
| transport + client | 模拟器走完整 dispatch 链路，140 个用例 | ✅ 通过 |
| 真实 BLE 硬件 | `tools/selfcheck.py`，全部通过（含 22 条指令逐条比对） | ✅ 2026-10-22 实测 |
| FTP 实际传输 | 凭据读取已验证；传输未做（会切断主机网络） | ⚠️ 部分 |

真机回归明细见 [docs/HARDWARE-REGRESSION.md](docs/HARDWARE-REGRESSION.md)。

### 真机回归发现的三个硬件事实（影响设计）

1. **固件不校验 CRC 与长度字段** → 库仍生成正确 CRC，但不将 CRC 视为安全保证。
2. **失败语义是无应答，而非 `0x00` 错误帧** → `MemoTalkTimeout` 是实际的错误信号，超时值不宜过短。
3. **`0x87` 有过渡期锁** → 关闭热点必须容错重试，并用 `0x03` 回读确认。
