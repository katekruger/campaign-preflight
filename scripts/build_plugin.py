#!/usr/bin/env python3
"""Vendor the engine into the Cowork plugin and package it as a .plugin file.

The plugin ships a copy of ``src/campaign_preflight`` so it can run with no
install step. A copy is a drift risk, so this script is the only thing allowed
to write it, and ``--check`` verifies the copy matches the source. CI runs the
check, which makes drift a build failure rather than a surprise.

    uv run python scripts/build_plugin.py            # sync + package
    uv run python scripts/build_plugin.py --check    # verify the copy is current
    uv run python scripts/build_plugin.py --no-zip   # sync only
"""

from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "src" / "campaign_preflight"
PLUGIN_DIR = REPO_ROOT / "plugin" / "campaign-preflight"
VENDORED = PLUGIN_DIR / "engine" / "campaign_preflight"
OUTPUT = REPO_ROOT / "dist" / "campaign-preflight.plugin"

# Never vendored: caches, compiled files, and editor droppings.
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_NAMES = {".DS_Store"}


def _wanted(path: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return False
    return path.suffix not in EXCLUDE_SUFFIXES and path.name not in EXCLUDE_NAMES


def _source_files() -> list[Path]:
    return sorted(p for p in SOURCE.rglob("*") if p.is_file() and _wanted(p))


def check_sync() -> list[str]:
    """Differences between the source package and the vendored copy."""
    problems: list[str] = []
    if not VENDORED.is_dir():
        return [f"vendored engine is missing: {VENDORED.relative_to(REPO_ROOT)}"]

    expected = {p.relative_to(SOURCE) for p in _source_files()}
    actual = {
        p.relative_to(VENDORED) for p in VENDORED.rglob("*") if p.is_file() and _wanted(p)
    }
    for missing in sorted(expected - actual):
        problems.append(f"missing from the plugin: {missing}")
    for extra in sorted(actual - expected):
        problems.append(f"stale file in the plugin: {extra}")
    for shared in sorted(expected & actual):
        if not filecmp.cmp(SOURCE / shared, VENDORED / shared, shallow=False):
            problems.append(f"out of date in the plugin: {shared}")
    return problems


def sync() -> int:
    """Replace the vendored copy with the current source package."""
    if VENDORED.exists():
        shutil.rmtree(VENDORED)
    VENDORED.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SOURCE,
        VENDORED,
        ignore=shutil.ignore_patterns(*EXCLUDE_DIRS, "*.pyc", "*.pyo", ".DS_Store"),
    )
    count = len(list(VENDORED.rglob("*.py")))
    print(f"Vendored {count} modules into {VENDORED.relative_to(REPO_ROOT)}")
    return count


def validate() -> list[str]:
    """Structural checks equivalent to what `claude plugin validate` would do."""
    import json

    problems: list[str] = []
    manifest_path = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
    if not manifest_path.is_file():
        return ["missing .claude-plugin/plugin.json"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"plugin.json is not valid JSON: {exc}"]

    name = manifest.get("name", "")
    if not name:
        problems.append("plugin.json has no 'name'")
    elif not all(c.islower() or c.isdigit() or c == "-" for c in name):
        problems.append(f"plugin name must be kebab-case, got {name!r}")

    skills_dir = PLUGIN_DIR / "skills"
    if not skills_dir.is_dir():
        problems.append("no skills/ directory")
    else:
        for entry in sorted(skills_dir.iterdir()):
            if entry.is_dir() and not (entry / "SKILL.md").is_file():
                problems.append(f"skill {entry.name} has no SKILL.md")

    mcp_path = PLUGIN_DIR / ".mcp.json"
    if mcp_path.is_file():
        try:
            json.loads(mcp_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f".mcp.json is not valid JSON: {exc}")

    for launcher in ("bin/preflight", "bin/preflight-mcp"):
        path = PLUGIN_DIR / launcher
        if not path.is_file():
            problems.append(f"missing launcher: {launcher}")
        elif not path.stat().st_mode & 0o111:
            problems.append(f"launcher is not executable: {launcher}")

    return problems


def smoke_test() -> list[str]:
    """Run the packaged launcher exactly as Cowork would."""
    problems: list[str] = []
    launcher = PLUGIN_DIR / "bin" / "preflight"
    for args, expect in (
        (["version"], "campaign-preflight"),
        (["demo", "--quiet"], "CAMPAIGN PREFLIGHT"),
        (["rules", "list"], "rule(s)."),
    ):
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(launcher), *args], capture_output=True, text=True, timeout=120
        )
        if expect not in result.stdout:
            problems.append(
                f"`preflight {' '.join(args)}` did not produce expected output "
                f"(exit {result.returncode}): {result.stderr.strip()[:200]}"
            )
    return problems


def package() -> Path:
    """Zip the plugin directory into a .plugin file."""
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        OUTPUT.unlink()
    files = sorted(p for p in PLUGIN_DIR.rglob("*") if p.is_file() and _wanted(p))
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(PLUGIN_DIR))
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"Packaged {len(files)} files -> {OUTPUT.relative_to(REPO_ROOT)} ({size_kb:.0f} KB)")
    return OUTPUT


def main() -> int:
    if "--check" in sys.argv:
        problems = check_sync() + validate()
        if problems:
            print("Plugin is not current:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print(
                "\nRegenerate with: uv run python scripts/build_plugin.py", file=sys.stderr
            )
            return 1
        print("Plugin is in sync with src/ and structurally valid.")
        return 0

    sync()

    problems = validate()
    if problems:
        print("Plugin structure is invalid:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Structure valid: manifest, skills, launchers, .mcp.json")

    problems = smoke_test()
    if problems:
        print("Packaged plugin failed its smoke test:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Smoke test passed: version, demo, rules list all run from the launcher")

    if "--no-zip" not in sys.argv:
        package()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
