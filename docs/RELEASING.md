# 发布流程

本项目用 GitHub Actions 把测试、构建和 PyPI 发布串起来。

## 一次发布的完整步骤

```bash
# 1. 确认工作区干净，测试通过
uv run --extra dev pytest -q

# 2. 改版本号（会同步 pyproject.toml 和 memotalk/__init__.py）
scripts/release.sh 0.1.1 --dry-run   # 先看一遍将要发生什么
scripts/release.sh 0.1.1             # 真正执行：改版本、跑测试、提交、打 tag、推送

# 3. 建 GitHub release —— 这一步触发 publish.yml，把包传到 PyPI
gh release create v0.1.1 --generate-notes

# 4. 确认
pip install memotalk-sdk
```

## 三个 workflow

| 文件 | 触发 | 作用 |
| :-- | :-- | :-- |
| `.github/workflows/ci.yml` | push / PR | 5 个 Python 版本跑测试、构建并验证安装、ruff 查缺陷级问题 |
| `.github/workflows/publish.yml` | GitHub release 发布 / 手动触发 | 构建 sdist + wheel 并上传 PyPI |

CI 是 publish 的前置条件：分支保护要求 `CI` workflow 通过后才能合并。

## PyPI 认证：两种方式

publish.yml 同时支持两种，按需选一种。

### 方式一：Trusted Publishing（推荐）

不需要保存任何 token。

1. 在 PyPI 上为 `memotalk-sdk` 建立一个 **pending** 的 publisher，填：
   - 仓库：`floatboatai/memotalk-sdk`
   - workflow 文件名：`publish.yml`
   - 环境名：`pypi`
2. 第一次发版时 PyPI 会发一封确认邮件，点头之后 pending 变 active。

publish.yml 已经带上了 `id-token: write` 权限和 `environment: pypi`，不用改。

### 方式二：API token

在 PyPI 的 API token 页面创建一个 token（scope 选 `pypi`），然后：

```bash
gh secret set PYPI_API_TOKEN --repo floatboatai/memotalk-sdk
```

workflow 会自动检测到这个 secret 并改用 token 方式；secret 不存在时走 OIDC。

## 先发到 TestPyPI 试水

```bash
gh workflow run publish.yml -f dry_run=true
```

这会把包传到 TestPyPI，可以随便试装：

```bash
pip install --index-url https://test.pypi.org/simple/ memotalk-sdk
```

## 版本号的一致性

版本号出现在两处，`pyproject.toml` 和 `memotalk/__init__.py`。`tests/test_commands.py::VersionTests` 会断言两者相同，`scripts/release.sh` 也会同时改两处——CI 会拦住漏改的情况。

## 首次发布前的检查清单

- [x] `memotalk-sdk` 这个名字在 PyPI 上未被占用
- [x] wheel 和 sdist 能构建，且 wheel 内不含本地路径
- [x] 干净 venv 里 `import memotalk` 和 `memotalk --version` 都正常
- [ ] 建立 PyPI publisher 或写入 `PYPI_API_TOKEN` secret
- [ ] 建好名为 `pypi` 的 GitHub environment
- [ ] `gh release create v0.1.0 --generate-notes` 触发首次发布
