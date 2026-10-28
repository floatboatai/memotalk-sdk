# Changelog

本文件记录 `memotalk-sdk` 的用户可见变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-10-29

首个公开版本。

### 新增

- **传输层**：`BleTransport` 负责扫描、连接、写入、应答关联、保活心跳与断连检测，修复了原 SKILL 缺失的这一层。
- **通用应答解析层**：`parsers.py` 把每种应答映射为类型化 `dataclass`（24 个解析函数 + 8 个帧谓词），调用方无需手工切片 `pkt[3:-2]`。
- **22 个纯命令构建函数**：`commands.py` 每个函数对应一个硬件原子动作，只返回待写入的字节，不含任何副作用。
- **6 组能力 API**：`MemoTalkClient` 的 `record` / `stream` / `files` / `settings` / `wifi` / `raw`，外加 `info()` 快照。
- **结构性安全护栏**：
  - `wifi.session()` 是异步上下文管理器，退出时保证尝试关闭热点并回读确认；
  - `files.clear_all()` 未显式 `confirm=True` 时本地拒绝，不发包；
  - `files.download()` 默认不覆盖已存在的本地文件。
- **多包流重组**：`FileListAssembler` 处理 `0x13` 的自定界列表包，`AudioSyncAssembler` 处理 `0x84` 的定长数据包并按缺口续传。
- **内置设备模拟器** `FakeMemoTalk` / `FakeTransport`，无硬件即可开发与测试。
- **命令行工具** `memotalk`，覆盖扫描、状态、列表、下载、删除、热点等常用操作。

### 已知边界

- FTP 实际传输未在真机验证（连入设备热点会切断主机网络，仅验证了凭据可获得）。
- 自动连热点仅支持 macOS。
- 不做 MP3 解码、不做 ASR、不做 U 盘挂载。
- `0x07` 指令在协议中仅有编号、无定义，未实现。
- `0x9A`（清空全部）从未在真机上执行过。

### 移除

- `cmd_*` 兼容别名（`cmd_read_battery()` 等）。统一改为 `read_battery()`、`read_record_state()` 等动词开头的名字；迁移时去掉 `cmd_` 前缀即可。
- `commands.WRITE_CHANNEL` / `RESPONSE_CHANNEL` 路由表。这两张表从未被任何代码读取，实际路由由 `client.py` 各调用点的 `write=` / `listen=` 显式指定，保留它们只会误导读者以为路由是表驱动的。

[0.1.0]: https://github.com/floatboatai/memotalk-sdk/releases/tag/v0.1.0
