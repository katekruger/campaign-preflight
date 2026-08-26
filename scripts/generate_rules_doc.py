#!/usr/bin/env python3
"""Regenerate ``docs/rules.md`` from the live rule registry.

Run with ``--check`` to verify the committed file is current; CI does exactly
that, so the catalogue cannot drift away from the code.

    uv run python scripts/generate_rules_doc.py          # write
    uv run python scripts/generate_rules_doc.py --check  # verify
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from campaign_preflight.models import RuleCategory  # noqa: E402
from campaign_preflight.rules import all_rules  # noqa: E402

TARGET = REPO_ROOT / "docs" / "rules.md"

CATEGORY_BLURBS: dict[RuleCategory, str] = {
    RuleCategory.CAMPAIGN: (
        "Configuration-level checks. These read the campaign object only and "
        "catch the settings that silently break a launch."
    ),
    RuleCategory.CONTACTS: (
        "Contact-data quality. Most are ratio-driven: one bad row is noise, a "
        "quarter of the list is a broken import."
    ),
    RuleCategory.SUPPRESSION: (
        "Who should not be contacted. Nothing here is a compliance check -- the "
        "domain and region lists encode **your organization's outreach policy**, "
        "which you configure. See [limitations.md](limitations.md)."
    ),
    RuleCategory.PERSONALIZATION: (
        "Per-contact personalization, and the evidence behind any factual claim. "
        "Claim checking is conservative: with no evidence supplied it returns "
        "UNKNOWN rather than accusing your copy of fabrication."
    ),
    RuleCategory.COPY: (
        "The campaign copy itself. Spam-word folklore is deliberately not "
        "implemented; what is here is either structural or an explicit heuristic."
    ),
    RuleCategory.SCHEDULE: (
        "When the campaign sends. Timezones are validated against the system "
        "IANA database; an unresolvable zone is UNKNOWN, never assumed valid."
    ),
    RuleCategory.SENDERS: (
        "Sender readiness. No deliverability number is ever invented: if the "
        "provider does not expose a health score, these rules say so."
    ),
}


def build() -> str:
    rules = all_rules()
    lines: list[str] = [
        "<!--",
        "  GENERATED FILE - do not edit by hand.",
        "  Regenerate with: uv run python scripts/generate_rules_doc.py",
        "-->",
        "",
        "# Rule catalogue",
        "",
        f"Campaign Preflight ships **{len(rules)} rules** across "
        f"{len(RuleCategory)} categories.",
        "",
        "## How to read this",
        "",
        "- **Severity** is the default. Any rule's severity can be overridden per",
        "  campaign in your config file.",
        "- **Requires** lists the provider capabilities a rule needs. If any of them",
        "  is unavailable, the rule returns `UNKNOWN` — never `PASS`. That is the",
        "  central safety property of the engine: missing data is not good news.",
        "- **Heuristic** marks a rule that encodes a judgement call rather than a",
        "  verifiable fact. Heuristic rules are never blockers by default and are",
        "  labelled as heuristics everywhere they appear in output.",
        "",
        "Every rule can be inspected from the command line:",
        "",
        "```bash",
        "campaign-preflight rules explain contacts.duplicate_email",
        "```",
        "",
        "## Statuses",
        "",
        "| Status | Meaning |",
        "|---|---|",
        "| `PASS` | The rule ran and found nothing wrong. |",
        "| `WARN` | The rule found something worth a human look. |",
        "| `FAIL` | The rule found a defect. A `FAIL` at `BLOCKER` severity forces `NOT_READY`. |",
        "| `UNKNOWN` | The rule could not run. **This is not a pass.** |",
        "| `NOT_APPLICABLE` | The rule does not apply to this campaign or is not configured. |",
        "",
        "## Severities",
        "",
        "| Severity | Effect |",
        "|---|---|",
        "| `BLOCKER` | A `FAIL` always produces `NOT_READY`, whatever the score. |",
        "| `HIGH` | A `FAIL` produces `NOT_READY` unless `scoring.high_failure_blocks` is off. |",
        "| `MEDIUM` | Deducts from the score. |",
        "| `LOW` | Deducts a little from the score. |",
        "| `INFO` | Reported, deducts nothing. |",
        "",
    ]

    for category in RuleCategory:
        selected = [r for r in rules if r.category is category]
        if not selected:
            continue
        lines += [
            f"## {category.value.title()} ({len(selected)})",
            "",
            CATEGORY_BLURBS[category],
            "",
            "| Rule | Severity | Requires | Checks |",
            "|---|---|---|---|",
        ]
        for rule in selected:
            requires = ", ".join(f"`{c.value}`" for c in rule.requires) or "—"
            summary = rule.title
            if rule.heuristic:
                summary += " _(heuristic)_"
            lines.append(f"| `{rule.rule_id}` | {rule.severity.value} | {requires} | {summary} |")
        lines.append("")

    lines += [
        "## Configuring a rule",
        "",
        "Every rule accepts `enabled` and `severity`. Most accept thresholds of",
        "their own. `rules explain` prints the exact options and their defaults:",
        "",
        "```yaml",
        "version: 1",
        "rules:",
        "  campaign.daily_volume:",
        "    enabled: true",
        "    warning_above: 100",
        "    blocker_above: 250",
        "",
        "  contacts.missing_first_name:",
        "    enabled: true",
        "    warning_ratio: 0.05",
        "    blocker_ratio: 0.25",
        "",
        "  senders.health_below_threshold:",
        "    enabled: true",
        "    minimum_score: 80",
        "```",
        "",
        "An unknown rule id or an unknown option is a hard configuration error, not",
        "a warning. A typo in a safety config that silently does nothing is worse",
        "than no config at all.",
        "",
        "See [configuration.md](configuration.md) for the full schema.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    content = build()
    if "--check" in sys.argv:
        if not TARGET.is_file():
            print(f"{TARGET} is missing; run this script without --check", file=sys.stderr)
            return 1
        if TARGET.read_text(encoding="utf-8") != content:
            print(
                f"{TARGET} is out of date. Regenerate with:\n"
                f"  uv run python scripts/generate_rules_doc.py",
                file=sys.stderr,
            )
            return 1
        print(f"{TARGET} is up to date.")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(content, encoding="utf-8")
    print(f"Wrote {TARGET} ({len(content.splitlines())} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
