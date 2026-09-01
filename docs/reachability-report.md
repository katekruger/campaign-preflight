# Reachability and divergence report

Originally report only. Verified at HEAD (`a8aad91` / same tree as
`4dc2ab2`) on 1 September 2026, against `README.md`, `CONTRIBUTING.md`, and a
real subprocess drive of every entry point this repo ships. `AGENTS.md` and
`tests/unit/test_mcp_safety.py` were not touched to produce this report.

**Status update:** every item this report found (§1.1, §1.2, §1.3, and both
halves of the incidental §1.5) has since been closed, across two follow-up
sessions. The findings below are preserved exactly as originally written — a
report that erases what it found is worth less than one that shows the
trail — with a **Resolved** line added under each closed item naming the
commit that closed it. See the Resolution log at the end for the full list.

Every claim below was checked from outside the module it describes — its
test suite, its CI config, or a fresh subprocess — not by re-reading the
function that makes the claim. That is the failure mode this report exists
to catch: five sibling-repo examples (`segment-mcp`'s logging claim,
`deliverability-guard`'s "Implemented" drivers, its dry-run claim, its
`BreakerStateStore` docstring, and its reopened `PAUSED` path) were each true
of the code their author was looking at and false of the running system.

---

## 1. Divergences found between docs and reality

Five findings. None of these are dangerous — this is a read-only tool and
nothing here weakens that guarantee — but each is a claim a reader would
reasonably rely on that the code does not currently back up.

### 1.1 README's architecture section names the wrong data-modeling tool

> "The context is a frozen Pydantic model, so 'a rule never mutates its
> input' is enforced by the type system rather than by review."
> — `README.md`, Architecture section, directly under the mermaid diagram.

`src/campaign_preflight/models.py`'s own module docstring says the opposite:

> "Implemented with frozen dataclasses and no third-party dependencies."

`grep -n '^import\|^from' src/campaign_preflight/models.py` shows only
`dataclasses`, `json`, `datetime`, `enum`, `typing` — no Pydantic import
anywhere in the package. If the codebase actually depended on Pydantic,
`security.yml`'s AST walk (every `src/` import must resolve to the standard
library, or to the one allowed `httpx` extra) and `release.yml`'s bare-3.9
`--no-deps` venv install would both fail on every push. They don't, because
the claim is wrong, not the guard. The freezing guarantee itself is real —
`@dataclass(frozen=True)` appears throughout `models.py` — only the tool
named for it is wrong.

**Fix (not applied):** replace "frozen Pydantic model" with "frozen
dataclass" in that one sentence.

**Resolved:** [`86c9c62`](https://github.com/katekruger/campaign-preflight/commit/86c9c62)
— `README.md` now says "frozen dataclass."

### 1.2 README's PyPI install instruction does not currently work

> "### As a CLI
> ```bash
> pipx install campaign-preflight
> ```"
> — `README.md`, Install section, presented as a working option alongside
> the plugin and from-checkout paths.

`.github/workflows/release.yml` has never published this package to PyPI:

```yaml
# PyPI publication is deliberately NOT automatic. Enable it by creating a
# "pypi" environment with a trusted publisher, then removing the `if: false`.
# Until then a release produces GitHub artifacts only.
pypi:
  name: Publish to PyPI
  if: false
```

Running the documented command today produces "No matching distribution
found for campaign-preflight," not an install. This isn't a stale claim
about something that used to work — the job has `if: false` and the comment
says publication was deliberately never turned on.

**Fix (not applied):** remove the `pipx install` block until the `pypi`
environment exists, or mark it "not yet available."

**Resolved:** [`d9178f3`](https://github.com/katekruger/campaign-preflight/commit/d9178f3)
— removed the `pipx install` block from both `README.md` and the GitHub
release notes `release.yml` itself generates (which had the identical
problem, discovered while closing this out); the from-checkout path, which
works today, stays. Publishing to PyPI is deferred to its own session, per
the report's own recommendation.

### 1.3 An absolute claim with no test that would fail if it stopped being true

> "It does not verify mailboxes. Address checks are syntax only. No DNS, no
> SMTP." — `README.md`, "What it does not do."

`grep -rn "dns\|smtp\|DNS\|SMTP" tests/ src/` returns nothing. There is no
test asserting that a contacts rule never performs a DNS lookup or an SMTP
handshake — nothing would fail today if a future rule added one. Contrast
with every other absolute claim swept in §1.5 below, which each have a named
test. This one currently has none. Nothing indicates the claim is false
today; it is a divergence in waiting, and it is reported rather than
silently patched with a new test, per this session's ground rules.

**Proposed pattern (not applied):** `deliverability-guard` closed an
analogous gap with a source-level assertion where behavioural testing is
impractical — `assert "CampaignRef" not in inspect.getsource(breaker_module)`.
The same shape would work here:
`assert not re.search(r"\bsocket\.|dns\.|smtplib", inspect.getsource(contacts_module))`
against `src/campaign_preflight/rules/contacts.py`. Crude, but it would fail
loudly the day someone "improves" address checking with a real lookup.

**Resolved:** [`818c946`](https://github.com/katekruger/campaign-preflight/commit/818c946)
— added `tests/unit/test_rules_no_network.py`, a `not in`-shaped source
guard swept across the whole `rules/` package (not just `contacts.py`, per
the report's own suggestion that the claim is about the tool, not one
module). Proved it bites before committing: temporarily added a
`socket.gethostbyname()` call to `EmailSyntax.evaluate()`, confirmed the new
test failed, then reverted the mutation — `contacts.py` carries no diff in
that commit, only the new test file.

### 1.4 CONTRIBUTING.md and README.md's tested claims check out

Everything else swept for `never`/`always`/`cannot`/`guaranteed` in both
files has a real test or CI gate behind it — see the table in §1.5.
`CONTRIBUTING.md`'s own "Test | Protects" table was checked against the
filesystem directly (`ls tests/unit/test_registry.py
tests/contract/test_instantly_transport.py tests/unit/test_mcp_safety.py
tests/unit/test_redaction.py tests/unit/test_demo_offline.py` — all five
exist, and `test_missing_required_data_never_passes` is a real function in
`test_registry.py`, not a stale reference). No divergence found there.

### 1.5 Incidental: `AGENTS.md`'s own "known gap" text is stale (originally
out of this report's scope — see Resolved below)

`AGENTS.md` was excluded from this round's divergence check by the ground
rules, and it was not edited to produce this report. Noted here only because
it was already surfaced and verified in a prior pass, and remains true at
this HEAD: `AGENTS.md`'s Non-negotiables and Testing conventions sections
still describe `_assert_read_only()`'s three failure branches as having
"zero coverage from the test suite" and its own test count as "1,851
tests." `tests/unit/test_mcp_safety.py` already exercises all three
branches (`pytest.raises(PreflightError, match="mutating name")`,
`match="does not declare itself READ-ONLY"`, `match="not annotated
readOnlyHint"`), and `uv run pytest --collect-only -q` currently sums to
1,896, not 1,851. This is a divergence inside `AGENTS.md` itself, not
`README.md`/`CONTRIBUTING.md`, so it is out of scope for §1's mandate — it
is recorded here rather than silently dropped, and rather than fixed.

**Resolved, in two parts:**

- [`5469630`](https://github.com/katekruger/campaign-preflight/commit/5469630)
  — the stale test count is gone from both mentions, deleted rather than
  re-pinned to a fresh number: "the full suite" is as informative as a count
  and doesn't rot on the next commit, which is the same number going stale a
  second time. At the time this closed, the other half of this finding — the
  "known coverage gap" text describing `_assert_read_only()`'s failure
  branches as untested, which `tests/unit/test_mcp_safety.py` already
  contradicted — was deliberately left open: it was not in that round's four
  named items, and `tests/unit/test_mcp_safety.py` was not to be touched, so
  it was recorded here rather than silently folded into "resolved."
- [`90b51f7`](https://github.com/katekruger/campaign-preflight/commit/90b51f7)
  — the remaining half. Both the Non-negotiables sentence and the matching
  "known coverage gap" paragraph in Testing conventions now say the three
  `raise PreflightError(...)` branches are covered and name
  `tests/unit/test_mcp_safety.py`, instead of restating a count or line
  numbers. `AGENTS.md` also gained a one-sentence convention at the top —
  point at the file that holds the truth rather than restate a checkable
  fact in prose — since this was the second time the file had gone stale
  about itself in as many rounds. `tests/unit/test_mcp_safety.py` itself was
  not touched. A sweep for other stale self-descriptions in `AGENTS.md`
  found none: the "76 rules" count and the CI coverage-floor percentages
  both still match `all_rules()` and `ci.yml` exactly.

### Absolute-claims sweep

`grep -noiE '\b(never|always|cannot|can.t|guarantee[ds]?)\b' README.md
CONTRIBUTING.md` — every hit, and what backs it:

| # | Claim (file:line) | Backed by |
|---|---|---|
| 1 | README:19,26–27,32 "never writes... cannot activate anything" | `test_instantly_transport.py` (full method×path matrix), `test_mcp_safety.py` |
| 2 | README:31 "does not guarantee deliverability... never invents a score" | `rules/senders.py` `UNHEALTHY_WARMUP`/`UNKNOWN` design; no score-generating code path exists to test against |
| 3 | README:47 "does not verify mailboxes... cannot [do DNS/SMTP]" | **resolved — `tests/unit/test_rules_no_network.py`, see §1.3** |
| 4 | README:53 "does not replace your provider's safeguards" | advisory prose, not a runtime claim — no test expected |
| 5 | README:187 "cannot tell those apart is worse than no checker" | rhetorical, not a claim about this codebase |
| 6 | README:230 "none needs an account" | `test_demo_offline.py` (`no_network`, `no_credentials` fixtures) |
| 7 | README:299 "no spam-word rule... shipping that list would train you to ignore it" | `test_rules_copy.py::test_no_spam_word_folklore_is_implemented` |
| 8 | README:352 "allowlist cannot contain PUT/PATCH/DELETE/HEAD/OPTIONS" | `test_instantly_transport.py::test_no_mutating_method_is_allowlisted`, `::test_a_mutating_method_is_rejected` |
| 9 | README:364–372 "will never... activate/pause/create/delete/send" | same transport matrix, plus `test_mcp_safety.py`'s tool-surface assertions |
| 10 | README:391 "`--fail-on` ... never changes the verdict itself" | `test_cli.py::test_fail_on_none_always_exits_zero` and neighbouring `--fail-on` tests exercise exit code vs. `report.readiness` separately |
| 11 | README:411 "a blocker always produces NOT_READY... numeric score cannot override it" | `test_scoring.py::test_blocker_always_produces_not_ready`, `::test_high_numeric_score_cannot_override_a_blocker` |
| 12 | README:441 "frozen Pydantic model... never mutates" | mutation-freedom is real and tested (`test_registry.py`'s no-mutation contract) — **the tool name is wrong, see §1.1** |
| 13 | README:455–456 "secrets scrubbed unconditionally... cannot get it into a report" | `test_redaction.py::test_auth_error_never_echoes_the_key`, `::test_scrubbing_survives_no_redact` |
| 14 | README:461 "there is a test for exactly that" | same — `test_auth_error_never_echoes_the_key` |
| 15 | README:534 "never coerced to a pass" (describing `segment-mcp`, a different repo) | not a claim about this codebase |
| 16 | CONTRIBUTING:77 "`UNKNOWN` (never `PASS`) whenever a required capability is missing" | `test_registry.py::test_missing_required_data_never_passes` |
| 17 | CONTRIBUTING:100 "never return an empty list when you could not look" | advisory guidance for provider authors, not a single testable runtime invariant — no direct test, low risk (structural: `ProviderResult` has no "empty means unknown" footgun) |
| 18 | CONTRIBUTING:136 "never" (table cell, "the demo needing no network and no key") | `test_demo_offline.py` |
| 19 | CONTRIBUTING:145 "never commit real contact data... or an API key" | contributor policy; enforced by `security.yml`'s secret/PII scan and RFC 2606-domain fixture check, per `AGENTS.md`'s CI workflows table |

Only row 3 has no backing test. Everything else either has a named test or
is plainly not a claim about runtime behavior (rhetorical framing, a
description of a different repo, or contributor-facing advice with no single
invariant to assert).

---

## 2. Module reachability

### Method

Every `.py` file under `src/campaign_preflight/` (34 files) instrumented
with `coverage run --parallel-mode --branch --source=campaign_preflight`,
against **real subprocesses only** — never the test suite, and never an
in-process `sys.modules` census (which would prove nothing here: `pytest`
collection already imports most of the tree before a single test runs).

Two entry points were driven, matching what `AGENTS.md` documents as the
only two ways this package runs in production:

- **CLI** (`python -m campaign_preflight.cli`, the module `bin/preflight`
  execs): `demo` in all three output formats, `check` against all three
  bundled examples (`clean`, `risky`, `incomplete` — one per verdict),
  `rules list`, `rules list --category ... --json`, `rules explain`,
  `validate-config`, `version`, and `instantly` with a dummy API key so
  `InstantlyProvider` and `ReadOnlyTransport` actually construct and run
  before the network call fails. 12 subprocess invocations.
- **MCP server** (`python -m campaign_preflight.mcp.server`, the module
  `bin/preflight-mcp` execs): one subprocess fed real newline-delimited
  JSON-RPC over stdin — `initialize`, `notifications/initialized`,
  `tools/list`, `ping`, and all six registered tools (`preflight_demo`,
  `preflight_files`, `preflight_instantly_campaign`, `list_preflight_rules`,
  `explain_preflight_rule`, `validate_preflight_config`).

`bin/preflight` and `bin/preflight-mcp` themselves are POSIX shell, not
Python — `coverage run` against the shell script directly fails with a
`SyntaxError` (confirmed, then discarded as a non-finding: there is no
Python in the launcher to instrument). Driving `-m campaign_preflight.cli`
and `-m campaign_preflight.mcp.server` directly is the same code path the
launchers `exec` into, per `AGENTS.md`'s own description of `bin/*`. This
repo has no `__main__.py` package-entry trap of the kind `n8n-operator`
found — `pyproject.toml`'s `[project.scripts]` points straight at
`campaign_preflight.cli:main` and `campaign_preflight.mcp.server:main`, both
of which ran, under `if __name__ == "__main__":`, in every subprocess above.

Coverage data from all 13 subprocesses combined with `coverage combine`,
then reported with `coverage report --source=campaign_preflight` — which
lists every file under that package whether or not it was ever imported, so
an unreached module shows up as a 0%-covered row rather than being silently
absent from the report.

### Result: zero unreached modules

`diff` between `find src -name '*.py'` (34 files) and the files
`coverage report` lists (34 rows) is empty. Every module was imported and
executed at least one real function body under a real subprocess drive of
both entry points.

| Module | Reached | Via | Note |
|---|---|---|---|
| `__init__.py` | yes | CLI, MCP | package init, both |
| `_yaml.py` | yes | CLI, MCP | `load_config`, campaign/leads parsing |
| `cli.py` | yes | **CLI only** | 0% under the MCP-only run — expected, it's the CLI's own module |
| `config.py` | yes | CLI, MCP | `load_config`/`validate_preflight_config` |
| `engine.py` | yes | CLI, MCP | `run_preflight`, all `check`/`demo`/`instantly`/`preflight_*` |
| `errors.py` | yes | CLI, MCP | redaction, every error path |
| `mcp/__init__.py` | yes | **MCP only** | 0% under the CLI-only run — expected |
| `mcp/formatting.py` | yes | **MCP only** | shapes every tool response |
| `mcp/protocol.py` | yes | **MCP only** | JSON-RPC framing for every request sent |
| `mcp/server.py` | yes | **MCP only** | tool registry, `_assert_read_only()` at startup |
| `models.py` | yes | CLI, MCP | every report |
| `normalization.py` | yes | CLI, MCP | `check`/`demo`, `preflight_files`/`preflight_demo` |
| `providers/__init__.py` | yes | CLI, MCP | |
| `providers/base.py` | yes | CLI, MCP | `ProviderResult` on every read |
| `providers/csv_provider.py` | yes | CLI, MCP | `check`, `preflight_files` |
| `providers/fixture_provider.py` | yes | CLI, MCP | `demo`, `preflight_demo` |
| `providers/instantly_provider.py` | yes | CLI, MCP | `instantly`, `preflight_instantly_campaign` (fails at the network call with a dummy key, but constructs the provider and reaches the transport first — see §3) |
| `providers/instantly_transport.py` | yes | CLI, MCP | `ReadOnlyTransport` runs before the failing network attempt |
| `reporting/__init__.py` | yes | CLI, MCP | |
| `reporting/csv_export.py` | yes | CLI, MCP | `check --affected-csv` (MCP path only touches it lightly — no MCP tool exposes CSV export) |
| `reporting/json_report.py` | yes | CLI, MCP | `--format json`, every MCP tool response |
| `reporting/markdown.py` | yes | CLI, MCP | `--format markdown` (MCP barely touches it — no MCP tool renders markdown) |
| `reporting/redaction.py` | yes | CLI, MCP | every report and error string |
| `reporting/terminal.py` | yes | CLI, MCP | CLI default output (MCP barely touches it — MCP responses are JSON, not terminal-rendered) |
| `rules/__init__.py` | yes | CLI, MCP | `all_rules()`/`get_rule()` |
| `rules/base.py` | yes | CLI, MCP | `engine.evaluate()` |
| `rules/campaign.py` | yes | CLI, MCP | every check run |
| `rules/contacts.py` | yes | CLI, MCP | same |
| `rules/copy.py` | yes | CLI, MCP | same |
| `rules/personalization.py` | yes | CLI, MCP | same |
| `rules/schedule.py` | yes | CLI, MCP | same |
| `rules/senders.py` | yes | CLI, MCP | same |
| `rules/suppression.py` | yes | CLI, MCP | same |
| `scoring.py` | yes | CLI, MCP | `engine.evaluate()` |

Four modules (`mcp/__init__.py`, `mcp/formatting.py`, `mcp/protocol.py`,
`mcp/server.py`) are reached **exclusively** through the MCP server and show
0% under a CLI-only drive. One module (`cli.py`) is reached **exclusively**
through the CLI and shows 0% under an MCP-only drive. Every other module is
shared core, reached by both surfaces independently — meaning the CLI and
MCP entry points are not redundant with each other for reachability
purposes; both were necessary to reach all 34 files, and both did.

Combined statement coverage from this drive alone (13 subprocesses, not the
test suite) ranged from 100% (`errors.py`, `scoring.py`, several
`__init__.py` files) down to 37% (`providers/instantly_provider.py` —
expected, since the one `instantly` run fails at the network call and never
exercises pagination, retries, or the campaign cache). Low coverage on a
reached module is a different finding from an unreached module; this report
found none of the latter.

---

## 3. Process-shape check: does this repo persist anything durable?

**No.** This repo persists nothing durable across process invocations, so
there is no live-path-versus-rebuild-from-storage pair that could disagree,
and no process-restart test exists because none is needed. This is the
shortest possible answer, and it closes the question — but here is the
evidence rather than just the assertion:

```
grep -rn "open(\|\.write(\|os\.replace\|tempfile\|pickle\|shelve\|sqlite" \
  src/campaign_preflight
```

turns up exactly four writers/readers, all one-shot and none read back by a
later invocation:

- `cli.py`'s `--output` report file (write-once, per invocation, to a path
  the *user* names)
- `reporting/csv_export.py`'s `--affected-csv` (same)
- `reporting/json_report.py`'s one-time read of the bundled JSON Schema file
  (a static asset shipped with the package, not written by any run)
- `providers/csv_provider.py`'s `open()` on the campaign/leads/suppressions
  files the user supplies as *input* — read, not written

The one candidate for cross-run state is `InstantlyProvider._campaign_cache`
(`providers/instantly_provider.py:156,349,519,526`) — a plain `dict`
assigned in `__init__`. It is scoped to a single provider instance within a
single process, and that instance is constructed fresh by `cmd_instantly()`
on every CLI invocation and by `preflight_instantly_campaign()` on every MCP
tool call. Nothing writes it to disk; it does not survive process exit, so
it cannot diverge from anything on the next run — there is nothing on the
next run to diverge from.

`grep -rliE "subprocess|restart" tests/` returns one file
(`tests/unit/test_yaml.py`), which matched on an unrelated YAML
deserialization security test (`!!python/object/apply:subprocess.check_output`)
— not a process-restart test. No process-restart test exists in this repo,
and this section is the record of having checked rather than assumed that.

**If a caching layer is ever added ahead of the `instantly` provider** (e.g.
to avoid re-fetching a campaign across MCP tool calls in the same session),
this is the section that would need a process-restart test, and this
sentence should stop being true. Until then, there is nothing to test.

---

## Summary

- §1: four real divergences in `README.md`, plus one incidental finding
  already known and out of this section's scope in `AGENTS.md`. Nineteen
  absolute claims swept across `README.md` and `CONTRIBUTING.md`; eighteen
  have a named test or are not runtime claims, one (§1.3, mailbox
  verification) was unguarded.
- §2: zero unreached modules, confirmed by real subprocess evidence across
  both shipped entry points, with per-surface attribution showing the CLI
  and MCP entry points are each necessary (four MCP-exclusive modules, one
  CLI-exclusive module, the rest shared).
- §3: this repo persists nothing durable across process invocations. No
  process-restart test exists, and none is currently needed.

Nothing in this report was fixed to produce it. `AGENTS.md` and
`tests/unit/test_mcp_safety.py` were not modified to produce it.

## Resolution log

All items this report found have since been closed, across two follow-up
sessions. Findings are preserved above exactly as originally written; only a
**Resolved** line was added under each.

| Item | Finding | Commit | What changed |
|---|---|---|---|
| §1.1 | README names Pydantic, code uses frozen dataclasses | [`86c9c62`](https://github.com/katekruger/campaign-preflight/commit/86c9c62) | `README.md`: "frozen Pydantic model" → "frozen dataclass" |
| §1.2 | `pipx install` doesn't work, PyPI publish is gated off | [`d9178f3`](https://github.com/katekruger/campaign-preflight/commit/d9178f3) | Removed the `pipx` block from `README.md` **and** `release.yml`'s generated release notes (the same defect, found in a second place while closing this out); the working from-checkout path stays |
| §1.3 | "No DNS, no SMTP" claim was unguarded | [`818c946`](https://github.com/katekruger/campaign-preflight/commit/818c946) | Added `tests/unit/test_rules_no_network.py`, a source-level `not in`-shaped guard across all of `rules/`, proved to bite by mutation before the mutation was reverted |
| §1.5, part one | `AGENTS.md`'s "1,851 tests" was stale | [`5469630`](https://github.com/katekruger/campaign-preflight/commit/5469630) | Both mentions changed to "the full suite" — deleted rather than re-pinned, so it can't go stale a third time. §1.5's other half (the "known coverage gap" text) was deliberately left open at this point — not in that round's four named items |
| §1.5, part two | `AGENTS.md`'s "known coverage gap" text was stale — `test_mcp_safety.py` already covered the gap it described as open | [`90b51f7`](https://github.com/katekruger/campaign-preflight/commit/90b51f7) | Both the Non-negotiables sentence and the Testing conventions paragraph now say the branches are covered and name the test file, instead of a count or line numbers. Added a one-sentence convention to `AGENTS.md` — point at the file that holds the truth, don't restate a checkable fact — since this was the second time the file had gone stale about itself. Swept the rest of the file: "76 rules" and the CI coverage-floor percentages both still check out, left as-is |

Everything else in this report — the zero-unreached-module result (§2), the
process-persistence finding (§3), and the fifteen absolute claims that
already had a test — required no action and is unchanged.
