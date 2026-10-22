# 仓库代码结构整理 · 四语言（Python / JS / TS / Kotlin）落地基线

> 本文回答两个问题：**现在这套代码是怎么切的**，以及 **切法是否已经能支撑另外三种语言**。
> 结论是：分层是对的，但**分层边界现在只存在于目录习惯里，没有存在于代码契约里**。补上契约，四种语言才可能字节级一致。

盘基于当前工作副本实测（140 个用例全绿，3.5s），行数由 `wc -l` 统计。

---

## 0. 结论先行

1. **可移植性是分层的，不是整体的。** 3968 行包体里，**1657 行（42%）是纯协议核**——零 IO、零 asyncio、输入 bytes 输出值对象，这部分在四种语言里必须逐字节一致。剩下 58% 是链路绑定与组合逻辑，本来就该每种语言重写一遍。
2. **最大的结构债不是"代码写在哪个目录"，而是"同一份知识写了四处"。** 25 个协议码同时出现在 `constants.py`、`commands.py`、`parsers.py`、`CAPABILITY-MATRIX.md`；乘以 4 种语言就是 16 处。`test_codec.py:256` 的 `CoverageGuardTests` 已经在用测试防这件事，说明风险已被感知——但守护手段选错了，应该守的是**数据**。
3. **移植前必须先做一刀：`BleTransport` 现在既是接口又是实现**（`fake.py:394` 的 `FakeTransport` 直接继承它）。接口不独立，TS 的 Web Bluetooth / Node noble、Kotlin 的 `BluetoothGatt` 就没有对标的锚点。

---

## 1. 现状盘点

### 1.1 目录与体量

```
memotalk-sdk/
├── CAPABILITY-MATRIX.md      189  能力映射表（下层 25 码 ↔ 上层 API）
├── README.md                 207  使用文档 + 分层示意（第 145-158 行）
├── pyproject.toml             31  setuptools 打包，唯一入口 memotalk.cli:main
│
├── memotalk/                3968  库本体（13 个模块）
│   ├── __init__.py           156  公开面 re-export（46 个符号）
│   ├── constants.py          147  ★ 硬件事实：UUID / Cmd / 枚举 / Limits / 帧标记
│   ├── codec.py              172  ★ CRC16 · 0x5A 组帧 · 帧长推断 · BCD · XOR-FF
│   ├── commands.py           277  ★ 22 个纯构建函数 + 通道路由表 + 22 行兼容别名
│   ├── parsers.py            409  ★ 24 个解析函数 + 8 个帧谓词 + PARSERS 派发表
│   ├── assemblers.py         253  ★ 3 个状态机：拆包重组 / 0x13 列表 / 0x84 收包
│   ├── models.py             245  ★ 15 个值对象（13 frozen + 2 可变组合体）
│   ├── errors.py             154  ★ 16 类异常层级 + 0x00 帧映射
│   ├── transport.py          377  BLE 链路：连接 / 写 / 关联 / 保活 / 断连
│   ├── discovery.py          141  扫描 + macOS 已连外设找回（私有 API，best-effort）
│   ├── client.py             875  6 组能力 API + FTP 客户端 + macOS subprocess
│   ├── cli.py                324  argparse 参考消费者
│   └── fake.py               438  设备模拟器（FakeMemoTalk 390 + FakeTransport 48）
│
├── tests/                   1217  140 个用例
│   ├── test_codec.py         282  字节级断言 + CoverageGuardTests（协议面守卫）
│   ├── test_parsers.py       276  24 条真机抓包 → 值对象
│   ├── test_assemblers.py    267  拆包 / 乱序 / 丢包 / CRC 错 / 填充截断
│   └── test_client.py        392  模拟器驱动端到端
│
├── tools/selfcheck.py        502  真机三层回归（只读为主，自建录音自行清理）
│
└── docs/
    ├── ERRATA.md             227  11 条协议勘误 E1-E11（真机实证）
    ├── HARDWARE-REGRESSION.md 228  L3/L4 复验 12 PASS / 0 FAIL
    └── reg_l12.json / reg_l3.json / reg_l4e1.json   回归结果快照
```

### 1.2 依赖方向

单向、无环，自下而上六层：

```
                    cli.py ──┐
                             ├──> client.py ──> discovery.py
                    fake.py ─┘        │              │
                                      ├──> transport.py ──> assemblers.py
                                      │                        │
                                      ├──> parsers.py ─────────┤
                                      │        │               │
                                      └──> commands.py ──> codec.py
                                               │            │
                                               └──> constants.py
                                                        │
                                              errors.py ┘
```

两条唯一的"反向"引用，都是潜在移植坑：

| 位置 | 现象 | 影响 |
| :-- | :-- | :-- |
| `parsers.py:36` | `from .models import _bcd_to_datetime` —— 导入**私有名** | Kotlin 的 `private`、TS 的 `#field` 会直接编译失败，必须提为包内可见 |
| `parsers.py:269` | `parse_sync_packet` 函数体内 `from .codec import crc16` | 隐式依赖，倒查调用链时才看得见 |

### 1.3 三条真实数据流

```
① 指令写入（22 条指令共用）
   client.RecordController.start()
     → commands.control_recording(RecAction.START)     纯函数，返回 bytes
     → codec.build_frame(0x80, [01])                   5A 03 80 01 CRC16
     → WRITE_CHANNEL[0x80] = Channel.RECORD            ← 全协议唯一路由例外
     → transport.write(FFE1, frame)                    write-without-response

② 应答读取
   notify(FFF2 | FFE2) → transport._on_notify
     → FrameReassembler.feed()                         按三种帧型推断总长并粘合
     → _dispatch() → Subscription.matches(predicate)   订阅在 write 之前注册
     → client._call()                                  先判 0x00 错误帧
     → parsers.parse_*() → models dataclass

③ 多包流（两种独立机制，不可混用）
   0x13 文件列表：0xFF 自定界包 → FileListAssembler → List[AudioFileEntry]
                 完成判据是"解析出的条目数 == 声明数"，不是字节数
   0x84 文件传输：328B 定长包 → AudioSyncAssembler → missing_sequences()
                 → 从首个缺口重发 → 按 expected_size 截断落盘
```

---

## 2. 可移植性分层（本文的核心）

按**是否需要宿主能力**切，而不是按业务部门切。这是四种语言共用一套设计的前提。

| 层 | 模块 | 行数 | 跨语言要求 | 语言差异 |
| :-- | :-- | --: | :-- | :-- |
| **A · 纯协议核** | constants · codec · commands · parsers · assemblers · models · errors | **1657** | **字节级一致**，同一输入必须同一输出 | 仅字面量语法。`struct.pack("<H")` → `DataView.setUint16(0, v, true)` / `ByteBuffer.order(LITTLE_ENDIAN)` |
| **B · 链路绑定** | transport · discovery | 518 | **接口一致**，实现各写各的 | `bleak` ↔ Web Bluetooth / `@abandonware/noble` ↔ `BluetoothGatt` |
| **C · 组合与宿主** | client · cli | 1199 | **语义一致**，惯用法各写各的 | `asynccontextmanager` ↔ TS `AsyncDisposable` ↔ Kotlin `use {}` / `coroutineScope` |
| **D · 验证资产** | fake · tests · selfcheck · docs | 438+1217+502+851 | **物理共享**，不复制 | 无（JSON 与 Markdown 天然中立） |

### 2.1 这条边界不是新发明的，它本来就在

现有分层刚好对齐三种语言各自"平台无关 / 平台相关"的切法，说明当初的分层是对的：

| 语言 | A 层落在 | B 层落在 |
| :-- | :-- | :-- |
| Python | 普通模块 | `bleak`（跨平台） |
| TS | `core/`（Node 与浏览器都能跑） | `transport-web`（Web Bluetooth） / `transport-node`（noble） |
| Kotlin | `commonMain`（纯 JVM 也能跑） | `androidMain`（`BluetoothGatt`，JVM 无 BLE） |

Kotlin 侧这条尤其关键：**协议核可以是纯 JVM 制品**（无 Android 依赖），只有 transport 必须是 Android。不做这个切分，`memotalk-core` 就无法在服务端做离线解析、回放与批量转写。

### 2.2 每语言同一个文件骨架

移植的对齐单位是**文件名**，不是目录层级。同一个职责在四种语言里必须同名同层，这样任何一次改动都能机械地推到四边：

```
core/       constants  codec  commands  parsers  assemblers  models  errors
runtime/    transport  discovery
api/        client
dev/        fake  cli
```

Python 现有文件名已经满足（`codec.py` / `transport.py` / `client.py` / `fake.py` / `cli.py`），所以 **A/B/C/D 的目录化只是加一层 `core/ runtime/ api/ dev/`，不改任何文件名**。

---

## 3. 跨语言必须共享的三个唯一事实源

同一个知识只允许写一次。这三份东西一旦分开写，四种语言必然漂移，且漂移只在真机上暴露。

### 3.1 `spec/` — 协议表（数据，不是代码）

现在 25 个协议码的知识分散在四处：

| 知识 | 当前位置 |
| :-- | :-- |
| 码值 | `constants.py:43-68` |
| 写通道 / 听通道 | `commands.py:69-99` |
| 解析函数 + 最小长度 | `parsers.py:83-398` |
| 人类可读说明 | `CAPABILITY-MATRIX.md:42-...` |

`test_codec.py:256` 的 `CoverageGuardTests` 正是在防这四处不同步——它断言"每个 `Cmd` 要么有 parser 要么在豁免集合里"。**这个测试是结构债的症状，不是解药。**

改为单一 YAML，各语言生成或手写枚举，生成物与 YAML 用 CI 校验：

```yaml
- code: 0x01
  name: BATTERY
  direction: app->dev
  write_channel: control      # FFF1
  listen_channel: control     # FFF2
  response_min_len: 6
  parser: battery
  verified: 2026-10-22        # 真机逐条比对通过
- code: 0x02
  name: RECORD_STATE
  write_channel: control
  listen_channel: record      # ← 全协议唯一非对称路由
```

### 3.2 `vectors/` — 黄金向量（140 个用例的真正内容）

现在字节级事实以 hex 字面量**内嵌在 Python 测试代码里**（`test_parsers.py` 24 条真机抓包、`test_codec.py` 22 条指令断言）。换语言时它们全部不可用，只能重抄一遍。

抽成 JSON 后，四种语言的测试跑同一批数据，"一致"才有可验证的定义：

```
vectors/
├── codec.json          build_frame 输入 → 期望完整帧 hex · CRC 中间值 · BCD
├── parsers.json        应答帧 hex → 期望字段值（真机抓包，带来源标注）
├── assemblers.json     分包序列 → 重组结果 / dropped_bytes / 缺失序号
├── simulate.json       FakeMemoTalk：写入 hex → 推送帧序列
└── README.md           新增一条向量需要标注什么（来源、固件、日期）
```

`simulate.json` 是这里最容易被低估的一项：`fake.py` 的 438 行本质是**第二套设备实现**。它现在是跨语言一致性最可靠的判据——四种语言的客户端都对着同一份回放向量跑，比对着文档跑可靠得多。**不要再让每种语言各写一个模拟器。**

### 3.3 `errors` 语义表

16 个异常类（`errors.py`）承载的是**语义**：`StorageFull` ≠ `Timeout`。TS 的 `Error` 子类、Kotlin 的 `sealed class` 都要映射到同一组名字与同一组触发条件。名字必须一致，否则跨语言的调用方行为会分叉。

---

## 4. 阻碍多语言的具体问题

按修复优先级排序，全部有定位。

| # | 问题 | 证据 | 为什么挡住四语言 |
| :-: | :-- | :-- | :-- |
| 1 | 协议表写四处 | `constants.py:43` / `commands.py:69` / `parsers.py:376` / `CAPABILITY-MATRIX.md:42` | 乘 4 语言 = 16 处同步点 |
| 2 | transport 既是接口又是实现 | `transport.py:102` 被 `fake.py:394` 继承 | 三种 BLE 栈没有可对标的抽象契约 |
| 3 | `client.py` 875 行混三件事 | 能力 API 190-595 · FTP 599-680 · macOS `subprocess` 653-680 | 平台代码长在协议库最顶层，非 Python 实现无法切分 |
| 4 | `_bcd_to_datetime` 跨模块私有导入 | `parsers.py:36` ↔ `models.py:23` | Kotlin/TS 严格可见性下直接编译失败 |
| 5 | 22 行纯别名 `cmd_*` | `commands.py:256-277` | 纯历史包袱，四语言各抄一遍是 88 行噪音 |
| 6 | ~~命名三套~~ **已统一为 `memotalk-sdk`**（2026-10-22） | `pyproject.toml:6` · `README.md:1` · `cli.py:235` | 制品名已统一；命名空间 `memotalk` 按规则保留，见 §5.1 |
| 7 | 无 `.git` / 无 `.gitignore` / 无 `LICENSE`，`__pycache__` 与 `.egg-info` 留在目录里 | 目录实测 | 多语言单仓的前置条件；`egg-info` 是构建残留，不该进库 |

README 第 145-158 行还有一份与 `__init__.py:3-11` 平行的分层示意——同一事实的第三处副本，建议改为链接本文。

---

## 5. 目标仓库布局

```
memotalk-sdk/                       ← 仓库名收敛为一个（决策点 1）
├── spec/
│   ├── protocol.yaml               25 码 · 通道路由 · 帧型 · Limits
│   ├── errors.yaml                 错误码 → 语义 → 异常名
│   └── CAPABILITY-MATRIX.md        人类可读的能力映射（保留）
│
├── vectors/                        四种语言共用的黄金数据
│   ├── codec.json  parsers.json  assemblers.json  simulate.json
│   └── README.md
│
├── packages/
│   ├── python/
│   │   ├── pyproject.toml          name = memotalk-sdk
│   │   └── memotalk/
│   │       ├── core/               ← A 层，1657 行迁入，文件名不变
│   │       ├── runtime/            ← B 层
│   │       ├── api/                ← C 层
│   │       └── dev/                ← D 层
│   │
│   ├── ts/                         ← 源码即 TS，JS 走同包编译产物（决策点 2）
│   │   ├── package.json            @memotalk/s1
│   │   └── src/
│   │       ├── core/               A 层，Node 与浏览器通用
│   │       ├── runtime/
│   │       │   ├── transport-web.ts    Web Bluetooth
│   │       │   └── transport-node.ts   @abandonware/noble
│   │       ├── api/
│   │       └── dev/
│   │
│   └── kotlin/                     KMP：协议核纯 JVM（决策点 3）
│       ├── build.gradle.kts
│       └── src/
│           ├── commonMain/kotlin/memotalk/core/    A 层
│           ├── jvmMain/kotlin/memotalk/            CLI + 离线解析
│           └── androidMain/kotlin/memotalk/runtime/ BluetoothGatt
│
├── tests/                          Python 侧测试保留原位（或随包下移）
├── tools/selfcheck.py
├── docs/  CODE-STRUCTURE.md（本文）· ERRATA.md · HARDWARE-REGRESSION.md
├── .gitignore  LICENSE  README.md
└── AGENTS.md                       ← 可选，见决策点 4
```

### 5.1 命名规则（2026-10-22 已定）

| 概念 | 值 | 理由 |
| :-- | :-- | :-- |
| 仓库名 / 本地目录 | `memotalk-sdk` | 已执行 |
| 制品名（发行标识） | PyPI `memotalk-sdk` · npm `@memotalk/sdk` · Maven `memotalk-sdk` | 对外可检索、可安装的正式名 |
| 命名空间（导入名） | Python 包 `memotalk` · TS scope `@memotalk` · Kotlin `ai.memotalk` | `memotalk-sdk` 含连字符，不是合法 Python / Kotlin 标识符；且 `from memotalk import ...` 是与原 SKILL 兼容的既有接口 |

一句话：**制品名统一，命名空间独立**。这不是遗留的不一致，而是四种语言共同遵守的规则——`pip install memotalk-sdk` 之后 `import memotalk`，与 `pip install pillow` 之后 `import PIL` 同理。

---

## 6. 迁移路线

每步都以「**140 tests 全绿 + vectors 全绿**」为验收门槛，任一步红灯即停。

| 阶段 | 动作 | 风险 | 验收 |
| :-- | :-- | :-- | :-- |
| **P0** | 本文档 + `.gitignore` + `LICENSE` + `git init`；清理 `__pycache__` / `egg-info` | 无（纯新增） | 仓库可 clone |
| **P1** | 抽 `vectors/`：把 `test_parsers.py` 的 24 条抓包、`test_codec.py` 的 22 条断言导成 JSON，Python 测试改为读 JSON | **低**：测试重构，不改产品代码 | 140 tests 仍全绿，且断言数据来自 JSON |
| **P2** | 抽 `spec/protocol.yaml`，生成/校验 `constants.py` | 低 | 生成物与原文件字节相等 |
| **P3** | `Transport` 接口独立：`FakeTransport` 改为实现而非继承 `BleTransport` | 中：`fake.py` 与 `client.py` 均需改接线 | 140 tests 全绿 |
| **P4** | 移入 `packages/python/`，根级 `pyproject.toml` 改 workspace | 中：import 路径与打包配置 | `pip install -e .` + `memotalk scan` 可用 |
| **P5** | 第一个非 Python 实现：**TS core**（最快能跑 vectors，无 BLE 依赖） | 低 | 同一批 vectors 全绿 |
| **P6** | Kotlin `commonMain` core（同上，纯 JVM） | 低 | 同上 |
| **P7** | 三语言各自的 transport + client 推进 | 高 | 模拟器批次全绿 → 真机 batch |

顺序理由：**P1/P2 不动产品代码却创造最大杠杆**。没有 vectors，后面三种语言的"一致"无法验证，只能靠人工比对。所以先做数据，再动结构。

---

## 7. 待确认的决策点

| # | 决策 | 选项 | 建议 |
| :-: | :-- | :-- | :-- |
| 1 | 仓库名 | ~~待定~~ **已定 `memotalk-sdk`** | 已执行：本地目录 / `pyproject.toml` 制品名 / README 标题三处统一 |
| 2 | JS 与 TS 关系 | (a) TS 为源，编译出 ESM+CJS 双产物 (b) JS 单独维护一套手写 CJS | **(a)**。(b) 等于维护两套实现，vectors 会变成两套断言 |
| 3 | Kotlin 形态 | (a) KMP：commonMain 协议核 + androidMain transport + jvmMain CLI (b) 只做 Android 库 | **(a)**。协议核纯 JVM 才能做服务端离线解析，且正好落在 A/B 边界上 |
| 4 | 一致性门禁落点 | (a) CI：三语言各自跑 `vectors/`，一红全红 (b) 只写进 `AGENTS.md` 做约定 | **(a)+(b)**。CI 是硬约束，`AGENTS.md` 让后续协作不再重新讨论 |

---

## 附：与新目录的对应关系（一表查）

| 现文件 | 新位置 | 层 | 跨语言策略 |
| :-- | :-- | :-: | :-- |
| `constants.py` | `core/` | A | 由 `spec/protocol.yaml` 生成 |
| `codec.py` | `core/` | A | 手工移植，vectors 校验 |
| `commands.py` | `core/` | A | 手工移植，删 22 行别名 |
| `parsers.py` | `core/` | A | 手工移植，vectors 校验 |
| `assemblers.py` | `core/` | A | 手工移植，状态机逐用例校验 |
| `models.py` | `core/` | A | 各语言原生值类型（dataclass / interface / data class） |
| `errors.py` | `core/` | A | 异常名与触发条件必须一致 |
| `transport.py` | `runtime/` | B | 拆接口 + 各栈实现 |
| `discovery.py` | `runtime/` | B | 各平台重写 |
| `client.py` | `api/` | C | 语义对齐；FTP 与 macOS 代码外移 |
| `cli.py` | `dev/` | C | 各语言独立 |
| `fake.py` | `dev/` | D | 行为固化为 `vectors/simulate.json` |
| `tests/*.py` | `tests/` | D | 数据迁出到 `vectors/` |
