"""The rule registry and every bundled rule.

Importing this package registers all rules. Rules are grouped by category into
one module each; see ``docs/rules.md`` for the catalogue.
"""

from .base import (
    Rule,
    all_rules,
    clear_registry,
    get_rule,
    known_rule_ids,
    register,
    rules_for_category,
)

__all__ = [
    "Rule",
    "all_rules",
    "clear_registry",
    "get_rule",
    "known_rule_ids",
    "register",
    "rules_for_category",
]
