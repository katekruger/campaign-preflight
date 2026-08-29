# Contributing

Thanks for looking. Rules are small, pure, and independently testable, so
adding one is usually a class, a docstring, and a handful of tests.

## Setup

```bash
git clone https://github.com/katekruger/campaign-preflight
cd campaign-preflight
uv sync --all-extras
uv run pytest
```

Python 3.9 or newer. Development pins 3.12, but **the package must keep working
on 3.9** — that is the interpreter Cowork ships with, and the plugin depends on
it. CI tests 3.9 through 3.13.

The package has **no runtime dependencies** and adding one is a breaking change
for the plugin. If you need functionality from a library, implement the subset
you need (see `_yaml.py`) or make it an optional extra behind a lazy import (see
the Instantly provider).

## Before you push

```bash
uv run ruff format .
uv run ruff check --fix .
uv run mypy
uv run pytest --cov=campaign_preflight
uv run python scripts/generate_rules_doc.py    # if you added or changed a rule
```

CI runs all of these, plus a determinism check on the demo and JSON Schema
validation of the report.

## Adding a rule

1. Pick the module that matches the category (`rules/contacts.py`, etc.).
2. Subclass `Rule`, decorate with `@register`.
3. Declare `requires` honestly. If the rule needs data, say so — the engine will
   short-circuit to `UNKNOWN` when that data is unavailable, which is what keeps
   the whole thing trustworthy.
4. Add an `Options` model if the rule has thresholds.
5. Write tests for every branch, including the "no data" one.
6. Regenerate the catalogue: `uv run python scripts/generate_rules_doc.py`.

```python
class MyOptions(RuleOptions):
    warning_ratio: float = 0.05


@register
class MyRule(Rule):
    rule_id = "contacts.my_check"
    title = "Contacts satisfy my check"
    category = RuleCategory.CONTACTS
    severity = Severity.MEDIUM
    requires = (Capability.LEADS,)
    options_model = MyOptions
    description = "One or two sentences. This is what `rules explain` prints."
    remediation = "What the user should actually do about it."

    def evaluate(self, ctx, options, config) -> RuleResult:
        affected = [lead for lead in ctx.leads if is_bad(lead)]
        if not affected:
            return self.passed(f"All {len(ctx.leads)} contacts are fine.")
        return self.warn(
            f"{len(affected)} of {len(ctx.leads)} contacts are not.",
            affected=len(affected),
            samples=self.sample([lead.label for lead in affected], config.settings.max_samples),
        )
```

`tests/unit/test_registry.py` will automatically hold your rule to the shared
contract: metadata completeness, determinism, no mutation of the context, a
remediation on every actionable result, and `UNKNOWN` (never `PASS`) whenever a
required capability is missing.

### Rules that must not exist

- **No spam-word rules.** "Free" and "act now" are not evidence of anything.
- **No invented metrics.** If a provider does not expose a deliverability score,
  the rule returns `UNKNOWN`. It does not estimate one.
- **No legal determinations.** Region and consent rules check a campaign against
  a policy the user configured. They say nothing about what the law requires.
- **Nothing that writes.** There is no code path to a provider mutation and
  there will not be one.

If a rule is a judgement call, set `heuristic = True`, open its description with
`HEURISTIC`, and do not give it `BLOCKER` severity. The registry tests enforce
the last two.

## Adding a provider

Subclass `CampaignProvider` in `providers/`. The contract is read-only and the
important part is `ProviderResult`: return the data **and the reason it does or
does not exist**.

Never return an empty list when you could not look. `ok(cap, [])` means "there
is genuinely nothing there"; `failed(cap, ...)`, `forbidden(cap, ...)`,
`unsupported(cap, ...)`, and `misconfigured(cap, ...)` all mean "the question is
unanswered", and rules will report `UNKNOWN`.

If your provider talks to a network API, put an allowlist transport in front of
it — see `providers/instantly_transport.py` — and add a contract test that
exercises the full method × path matrix.

## Testing

| Directory | Contains |
|---|---|
| `tests/unit/` | Rules, normalization, config, scoring, redaction, reporting, MCP safety, properties |
| `tests/integration/` | CLI, engine, performance |
| `tests/contract/` | Provider behaviour against mocked HTTP |

Context builders live in `tests/helpers.py`. `make_context()` defaults every
capability to `SUPPORTED_OK`, so a test only describes what it is testing:

```python
from helpers import make_context, make_lead, run_rule


def test_something():
    ctx = make_context(leads=[make_lead(email="bad")])
    assert run_rule("contacts.email_syntax", ctx).status is RuleStatus.FAIL
```

Tests that must keep passing, and what they protect:

| Test | Protects |
|---|---|
| `test_registry.py::test_missing_required_data_never_passes` | The core safety property |
| `test_instantly_transport.py` | The write barrier |
| `test_mcp_safety.py` | The agent-facing tool surface |
| `test_redaction.py` | Credentials never reaching output |
| `test_demo_offline.py` | The demo needing no network and no key |

## Fixtures and data

Every address in this repository is synthetic and uses an RFC 2606 reserved
domain (`example.com`, `.invalid`). There is a test that fails if a
non-reserved domain appears in the demo data.

Never commit real contact data, a real campaign export, or an API key.

## Commits and pull requests

Conventional-commit prefixes, please: `feat:`, `fix:`, `docs:`, `test:`,
`refactor:`, `chore:`, `perf:`, `ci:`.

Keep pull requests focused. A new rule, a bug fix, and a refactor are three pull
requests. Fill in the template — the "what could this break" section is the part
reviewers actually read.

## Reporting bugs

Include the rule id, the input that produced it, what you expected, and what you
got. `campaign-preflight check ... --format json` output is ideal; redact it
first if it contains real contacts.

A rule that returned `PASS` when the data was missing is a **security** issue —
see [SECURITY.md](SECURITY.md).

## Code of conduct

Be decent. Assume good faith. Discuss the code, not the person.

The full terms are in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Reports go
through GitHub — [private vulnerability
reporting](https://github.com/katekruger/campaign-preflight/security/advisories/new)
for anything sensitive, an
[issue](https://github.com/katekruger/campaign-preflight/issues) otherwise.
There is deliberately no email address, here or in the code of conduct.
