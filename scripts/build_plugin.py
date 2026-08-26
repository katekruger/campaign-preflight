#!/usr/bin/env python3
"""Package the repository as an installable ``.plugin`` bundle.

The repo root *is* the plugin, so there is nothing to assemble: this script
smoke-tests the launchers the way a client invokes them, then zips the tree.

    uv run python scripts/build_plugin.py            # smoke-test and package
    uv run python scripts/build_plugin.py --no-zip   # smoke-test only

Structural validation of the manifest is not done here. ``claude plugin
validate . --strict`` does it properly and runs in CI; a second hand-rolled
implementation would only drift from the real one.

This script previously also vendored ``src/`` into ``plugin/…/engine`` and
checked that copy for drift. The plugin now runs from ``src/`` directly, so
both of those are gone.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "dist" / "campaign-preflight.plugin"

# Everything a user does not need in order to run the plugin. Excluding these
# keeps the bundle to the plugin itself rather than the whole development repo.
EXCLUDE_DIRS = {
    ".git",
    ".github",
    ".venv",
    "dist",
    "tests",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    "htmlcov",
}
EXCLUDE_NAMES = {".DS_Store", "uv.lock", ".python-version", "coverage.xml", ".coverage"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _wanted(path: Path) -> bool:
    relative = path.relative_to(REPO_ROOT)
    if any(part in EXCLUDE_DIRS for part in relative.parts):
        return False
    return path.suffix not in EXCLUDE_SUFFIXES and path.name not in EXCLUDE_NAMES


def smoke_test() -> list[str]:
    """Run the launcher exactly as a plugin client invokes it."""
    problems: list[str] = []
    launcher = REPO_ROOT / "bin" / "preflight"
    if not launcher.is_file():
        return ["bin/preflight is missing"]
    if not launcher.stat().st_mode & 0o111:
        problems.append("bin/preflight is not executable")

    for args, expect in (
        (["version"], "campaign-preflight"),
        (["demo", "--quiet"], "CAMPAIGN PREFLIGHT"),
        (["rules", "list"], "rule(s)."),
    ):
        result = subprocess.run(  # fixed argv, no shell
            [str(launcher), *args], capture_output=True, text=True, timeout=120
        )
        if expect not in result.stdout:
            problems.append(
                f"`preflight {' '.join(args)}` did not produce expected output "
                f"(exit {result.returncode}): {result.stderr.strip()[:200]}"
            )

    mcp = REPO_ROOT / "bin" / "preflight-mcp"
    handshake = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    result = subprocess.run(  # fixed argv, no shell
        [str(mcp)], input=handshake, capture_output=True, text=True, timeout=120
    )
    if "preflight_demo" not in result.stdout:
        problems.append(
            f"the MCP server did not list its tools (exit {result.returncode}): "
            f"{result.stderr.strip()[:200]}"
        )
    return problems


def package() -> Path:
    """Zip the plugin into a ``.plugin`` bundle."""
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        OUTPUT.unlink()
    files = sorted(p for p in REPO_ROOT.rglob("*") if p.is_file() and _wanted(p))
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(REPO_ROOT))
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"Packaged {len(files)} files -> {OUTPUT.relative_to(REPO_ROOT)} ({size_kb:.0f} KB)")
    return OUTPUT


def main() -> int:
    problems = smoke_test()
    if problems:
        print("Smoke test failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Smoke test passed: version, demo, rules list, and the MCP handshake")

    if "--no-zip" not in sys.argv:
        package()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
