#!/usr/bin/env bash
#
# Bump the version everywhere it appears, driven by .version-bump.json.
#
#   scripts/bump-version.sh 0.2.0     apply
#   scripts/bump-version.sh --check   verify every file already agrees
#
# Fails loudly on a missing path or a pattern that no longer matches, because a
# half-applied bump is worse than no bump: the manifest and the package would
# disagree and nothing would say so.
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG=".version-bump.json"
[ -f "$CONFIG" ] || { echo "error: $CONFIG not found" >&2; exit 1; }

python3 - "$@" <<'PY'
import json, pathlib, re, sys

CONFIG = pathlib.Path(".version-bump.json")
config = json.loads(CONFIG.read_text(encoding="utf-8"))
current = config["version"]

args = [a for a in sys.argv[1:] if a]
check_only = "--check" in args
positional = [a for a in args if not a.startswith("-")]

if not check_only and not positional:
    print(f"current version: {current}", file=sys.stderr)
    print("usage: scripts/bump-version.sh <new-version> | --check", file=sys.stderr)
    sys.exit(2)

new = current if check_only else positional[0]
if not check_only and not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", new):
    print(f"error: {new!r} is not a semantic version", file=sys.stderr)
    sys.exit(1)

problems, updated, agreed = [], [], []

for entry in config["files"]:
    path = pathlib.Path(entry["path"])
    if not path.is_file():
        problems.append(f"{path}: listed in {CONFIG} but does not exist")
        continue

    text = path.read_text(encoding="utf-8")
    pattern = re.compile(entry["pattern"], re.MULTILINE)
    match = pattern.search(text)
    if not match:
        problems.append(
            f"{path}: pattern {entry['pattern']!r} matched nothing. "
            f"The file changed shape; update {CONFIG}."
        )
        continue

    found = match.group(1)
    if check_only:
        (agreed if found == current else problems).append(
            f"{path}: {found}" if found == current
            else f"{path}: has {found}, {CONFIG} says {current}"
        )
        continue

    replacement = entry["template"].replace("{version}", new)
    path.write_text(pattern.sub(replacement.replace("\\", "\\\\"), text, count=1), encoding="utf-8")
    updated.append(f"{path}: {found} -> {new}")

if problems:
    print("FAILED:", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    sys.exit(1)

if check_only:
    print(f"all {len(agreed)} version fields agree on {current}")
    for line in agreed:
        print(f"  {line}")
    sys.exit(0)

config["version"] = new
CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"bumped {current} -> {new} in {len(updated)} file(s):")
for line in updated:
    print(f"  {line}")

if config.get("generated"):
    print("\nGenerated copies still to refresh:")
    for entry in config["generated"]:
        print(f"  {entry['path']}")
if config.get("after_bump"):
    print("\nNext:")
    for step in config["after_bump"]:
        print(f"  {step}")
PY
