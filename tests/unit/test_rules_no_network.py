"""Source-level guard for README's "no DNS, no SMTP" claim.

README's "What it does not do" section promises: "It does not verify
mailboxes. Address checks are syntax only. No DNS, no SMTP." There is no
behavioural way to test the *absence* of a network call short of
intercepting every socket a test run might open, so this asserts it
directly against the source of every rule module instead -- the same
pattern ``deliverability-guard`` uses for claims where behavioural testing
is impractical.

The assertion is deliberately ``not in``-shaped, never ``in``-shaped: a
docstring mention of "dns" would make this fail loudly, a false failure
that is easy to notice and fix. The opposite shape -- asserting a forbidden
pattern *is* present somewhere plausible -- can pass against code that has
been rewritten to route around the very thing it claims to check, because a
stale comment or docstring still mentions it. That exact mistake made an
equivalent guard in a sibling repo vacuous.

Scoped to the whole ``rules`` package, not just ``contacts.py`` -- the
claim in the README is about the tool, not one module.
"""

from __future__ import annotations

import importlib
import inspect
import re

import pytest

# Matches an actual DNS/SMTP call site, not prose: `socket.` (module-qualified
# use, so "the socket library" in a docstring wouldn't match), a `dns.` module
# reference, or the `smtplib`/`dnspython` module names as whole words.
NETWORK_LOOKUP_PATTERN = re.compile(r"\bsocket\.|\bdns\.|\bsmtplib\b|\bdnspython\b")

# The modules `rules/base.py::_ensure_loaded()` imports to populate the
# registry -- the complete set of rule category modules that ship today.
RULE_MODULE_NAMES = (
    "campaign",
    "contacts",
    "copy",
    "personalization",
    "schedule",
    "senders",
    "suppression",
)


@pytest.mark.parametrize("module_name", RULE_MODULE_NAMES)
def test_rule_module_performs_no_dns_or_smtp_lookup(module_name: str) -> None:
    module = importlib.import_module(f"campaign_preflight.rules.{module_name}")
    source = inspect.getsource(module)
    match = NETWORK_LOOKUP_PATTERN.search(source)
    assert match is None, (
        f"campaign_preflight.rules.{module_name} appears to perform a network "
        f"lookup ({match.group() if match else ''!r}). README promises address "
        "checks are syntax only -- no DNS, no SMTP."
    )
