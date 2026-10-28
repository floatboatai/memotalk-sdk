# AGENTS.md — memotalk-sdk agent guide

Python SDK (`memotalk-sdk`) for MemoTalk S1 / AI-PEN BLE recorders. Python 3.9–3.13, packaging via setuptools + uv, no lockfile committed.

## Commands

```bash
uv sync --extra dev              # install deps (dev extras include pytest)
uv run --extra dev pytest -q     # run tests
uv run --with ruff ruff check memotalk tests   # lint (defect-level rules only, see pyproject [tool.ruff])
uv build --out-dir dist          # build sdist + wheel
```

Tests are async (`asyncio_mode = "auto"`); no hardware needed — `memotalk/fake.py` provides the fake transport.

## CI (`.github/workflows/ci.yml`)

Runs on push to `main` and every PR. Three jobs: **test** (matrix py3.9–3.13 via uv), **build** (uv build + install-and-import smoke test of the wheel), **lint** (ruff, defect-level rules only). Must stay green before release.

## Release to PyPI (`.github/workflows/release-publish.yml`)

**How to publish a new version:**

1. Bump `version` in `pyproject.toml`.
2. Push to `main`.
3. `gh release create vX.Y.Z -R floatboatai/memotalk-sdk` — the `release: published` event triggers the workflow, which builds, verifies the tag matches `pyproject.toml` version (tag `v0.1.0` ⇔ version `0.1.0`; mismatch fails the run), and uploads to PyPI.
4. Verify: `curl -s https://pypi.org/pypi/memotalk-sdk/json` shows the new version.

**Auth model — trusted publishing, no API tokens:**

- PyPI side: trusted publisher registered for GitHub `floatboatai/memotalk-sdk`, workflow `release-publish.yml`, GitHub environment `pypi`, PyPI account `Redreamality`.
- GitHub side: `pypi` environment (already exists); no `PYPI_API_TOKEN` secret set — the workflow falls back to an API token path only if that secret ever appears.
- Manual re-run of a failed publish: `gh run rerun <id> -R floatboatai/memotalk-sdk`.
- Dry run to TestPyPI: workflow_dispatch with `dry_run=true` (requires its own trusted publisher on test.pypi.org if ever needed).

**Never** put `secrets` context in a step-level `if:` in these workflows — GitHub rejects the whole file and fires a guaranteed-to-fail run on every push. Resolve secrets into `env` at job level and branch on `env` (see comments in `release-publish.yml`).

## Conventions

- Version lives in `pyproject.toml` only; `scripts/package_version.py` prints it for the release workflow.
- `memotalk` package ships `py.typed`; keep it in `[tool.setuptools.package-data]`.
- Ruff is intentionally minimal (E9, F63, F7, F82, F401, F811, F841) — no formatting rewrites.
- Workflow commits go straight to `main`; no PR ceremony for CI/release fixes.
