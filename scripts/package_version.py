#!/usr/bin/env python3
"""Print the version recorded in pyproject.toml.

Used by the release workflow to check that the git tag matches the
package version. Kept as a file rather than an inline `python -c` so
that no shell quoting is involved.
"""
import pathlib
import re
import sys

TEXT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def main() -> int:
    match = re.search(
        r'^version\s*=\s*"([^"]+)"', TEXT.read_text(encoding="utf-8"), re.M
    )
    if not match:
        print("no version found in pyproject.toml", file=sys.stderr)
        return 1
    print(match.group(1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
