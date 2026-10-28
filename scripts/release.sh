#!/usr/bin/env bash
# Cut a release: bump the version, tag, and let the publish workflow do the rest.
#
# Usage:
#   scripts/release.sh 0.1.1              # bump, tag, push
#   scripts/release.sh 0.1.1 --dry-run    # show what would happen
#
# Steps:
#   1. refuse if the working tree is dirty
#   2. update pyproject.toml and memotalk/__init__.py
#   3. run the test suite
#   4. commit, tag v<version>, push
#   5. (optional) create the GitHub release, which triggers .github/workflows/publish.yml
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="${1:-}"
DRY_RUN=0
[[ "${2:-}" == "--dry-run" ]] && DRY_RUN=1

if [[ -z "$VERSION" ]]; then
  echo "usage: scripts/release.sh <version> [--dry-run]" >&2
  exit 2
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "version must look like 1.2.3, got: $VERSION" >&2
  exit 2
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "working tree is dirty; commit or stash first" >&2
  git status --short
  exit 1
fi

if git rev-parse "v$VERSION" >/dev/null 2>&1; then
  echo "tag v$VERSION already exists" >&2
  exit 1
fi

echo "==> bumping to $VERSION"
sed -i.bak "s/^version = \"[^\"]*\"/version = \"$VERSION\"/" pyproject.toml
rm -f pyproject.toml.bak
sed -i.bak "s/^__version__ = \"[^\"]*\"/__version__ = \"$VERSION\"/" memotalk/__init__.py
rm -f memotalk/__init__.py.bak

echo "==> running tests"
uv run --extra dev pytest -q

if [[ "$DRY_RUN" == "1" ]]; then
  echo "==> dry run, stopping here"
  git diff --stat
  git checkout -- pyproject.toml memotalk/__init__.py
  exit 0
fi

echo "==> committing"
git add pyproject.toml memotalk/__init__.py
git commit -m "release: v$VERSION"

git tag -a "v$VERSION" -m "v$VERSION"
git push origin main
git push origin "v$VERSION"

echo "==> tagged v$VERSION"
echo "now create the GitHub release to publish to PyPI:"
echo "  gh release create v$VERSION --generate-notes"
