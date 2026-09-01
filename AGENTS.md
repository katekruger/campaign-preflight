# AGENTS.md

Working notes for anyone — human or agent — changing this repository. These are
the conventions that are easy to break by accident because they look like
mistakes until you know why they are there.

`CLAUDE.md` at the repo root is a one-line `@AGENTS.md` include, kept only
because Claude Code auto-loads `CLAUDE.md` as project context and does not
(yet) do the same for `AGENTS.md`. It is *not* shipped as plugin context — see
the plugin-manifest warning under CI workflows below; a plugin install never
sees it. Edit this file, never `CLAUDE.md` directly.

## What this is

`campaign-preflight`, a read-only linter for outbound email campaigns. It ships
three ways: a Python CLI, an MCP server, and a Claude plugin.

**Naming.** The repository and the plugin share one name: `campaign-preflight`.
Use it verbatim in prose, headings, install commands, and URLs.

## Non-negotiables — this tool cannot write, and must never be able to

This is a pre-send safety tool. Its entire value is that it can be pointed at
a real campaign and a real API key without any risk of the campaign changing.
No change may weaken that, however small or however good the reason looks.
Concretely, no change may:

- **Add a code path that writes to a provider.** There is no `PATCH`/`PUT`/
  `DELETE`/mutating-`POST` call anywhere in this codebase, and there must
  never be one. `ReadOnlyTransport` in
  `src/campaign_preflight/providers/instantly_transport.py` matches every
  outbound `(method, path)` against `READ_ONLY_ALLOWLIST` — an explicit tuple
  of compiled patterns — and raises `ReadOnlyViolation` on anything else,
  *before the request leaves the process*. Exactly one entry is a `POST`
  (`/api/v2/leads/list`, a filter-body read modelled as a POST); the test
  suite asserts no second `POST` entry ever appears. Adding a provider call
  means adding an allowlist entry that a reviewer can see is a read — never
  widening the match to something broader like a whole path prefix.
- **Let an MCP tool's name, description, or annotations imply a write.**
  `_assert_read_only()` in `src/campaign_preflight/mcp/server.py` runs at
  server startup — not only in tests — and refuses to start if any tool name
  contains a mutating verb (`MUTATING_VERBS`), if its description doesn't
  start with the literal `READ-ONLY.` prefix, or if it isn't annotated
  `readOnlyHint: True`. This is a fail-closed guard specifically so a mistake
  in a fork can't ship a write tool to somebody's Cowork or Claude Desktop.
  **Its three failure branches have no test coverage today** — see Testing
  conventions below; treat that as a known gap to close, not evidence the
  branches are unimportant.
- **Let missing or unreadable data produce a `PASS`.** Every provider read
  returns data *plus the reason it does or does not exist* (`ProviderResult`
  in `providers/base.py`: `ok`, `failed`, `forbidden`, `unsupported`,
  `misconfigured`). Every rule declares the `Capability` values it
  `requires`, and `engine.evaluate()` short-circuits a rule to `UNKNOWN`
  *before calling it* if a required capability isn't available
  (`_missing_capabilities()`). Rules cannot opt out of this — there is no
  parameter that lets a rule run anyway and guess. There are four verdicts,
  not two, and `INCOMPLETE` is a first-class outcome, not an error state.
- **Let a credential reach a log, a report, an exception, or a PR comment.**
  The Instantly API key is read from the environment only — no CLI flag, no
  MCP tool argument accepts one (`test_no_tool_accepts_a_credential_argument`
  enforces the latter) — set once as an `Authorization` header, and never
  formatted into a message. `redact_secrets()` (`errors.py`) runs over every
  provider-facing error string, every log line, and every rendered report,
  **unconditionally**; `--no-redact` on the CLI disables contact-mailbox
  masking only, never credential masking.
- **Accept a raw campaign edit, activation call, or send trigger as a
  feature request.** This tool inspects; it does not act. If a request is
  "and then also send it" or "and then also pause it," the answer is no, not
  a design discussion — see What this repo deliberately does not do.

If a change touches any of the above, treat it as a security change, not a
feature: it needs a test that would fail without the change (the existing
suite already has a home for these — see `test_instantly_transport.py`,
`test_mcp_safety.py`, and `test_registry.py::test_missing_required_data_never_passes`),
and it needs a person, not just CI, to have looked at the diff.

## Layout

```
.claude-plugin/     plugin manifest and marketplace manifest
skills/             the three skills, one directory each
bin/                launchers the MCP server and CLI run through
src/                the Python package: rules, engine, providers, reporters
tests/              unit, integration, contract
docs/               rules catalogue, configuration, MCP, CI, limitations, architecture
examples/           three worked campaigns, one per verdict
scripts/            generators and the plugin packager
```

**The repo root is the plugin.** `.claude-plugin/plugin.json` and
`.claude-plugin/marketplace.json` both sit at the root, and the skills are
discovered from `skills/` there. There is no vendored second copy of the Python
package; `bin/preflight` puts `src/` on `PYTHONPATH` directly. If you find a
`plugin/` directory, something has gone wrong.

`scripts/build_plugin.py` no longer generates anything — it smoke-tests the
launchers and zips the tree into `dist/campaign-preflight.plugin`.

## src/ and bin/ — the CLI and MCP surface

`bin/preflight` and `bin/preflight-mcp` are POSIX shell launchers, not Python
entry points. Each finds a Python 3.9+ interpreter on the host (`python3`,
then a few absolute fallbacks, then `python`), puts `src/` on `PYTHONPATH`,
and `exec`s `python -m campaign_preflight.cli` or `campaign_preflight.mcp.server`.
No venv, no pip install, no build step between editing `src/` and running the
launcher — that's the point of having no runtime dependencies. `uv run
campaign-preflight`/`campaign-preflight-mcp` (declared under
`[project.scripts]` in `pyproject.toml`) is the local-dev equivalent; both
paths import the same `src/`, but if you change how either program is
invoked, check `bin/*` and `[project.scripts]` together.

The CLI (`cli.py`) has six subcommands — `demo`, `check`, `instantly`, `rules`
(`list`/`explain`), `validate-config`, `version` — and six documented exit
codes (0 ready, 1 ready-with-warnings, 2 not-ready, 3 incomplete, 4
input/config error, 5 provider/auth error, 6 unexpected internal error;
`campaign-preflight --help` is authoritative). The MCP server exposes exactly
six tools (`preflight_demo`, `preflight_files`, `preflight_instantly_campaign`,
`list_preflight_rules`, `explain_preflight_rule`, `validate_preflight_config`)
over stdio — `mcp/protocol.py` implements the JSON-RPC framing,
`mcp/server.py` builds the tool registry and the read-only gate above,
`mcp/formatting.py` shapes tool output. Inside the plugin, skills invoke the
CLI as `"${CLAUDE_PLUGIN_ROOT}/bin/preflight" <subcommand>` (see any
`skills/*/SKILL.md`) — `${CLAUDE_PLUGIN_ROOT}` is supplied by the plugin host
at run time, not set by this repo. `.mcp.json` registers the MCP server the
same way, via `${CLAUDE_PLUGIN_ROOT}/bin/preflight-mcp`.

## skills/ — packaging and constraints

Three skills, each a self-contained directory under `skills/` with its own
`SKILL.md` frontmatter; `preflight-campaign` additionally has a `references/`
subdirectory for longer material it links to rather than inlines. There is no
build or packaging step for skills — `.claude-plugin/plugin.json` points at
the repo root, and the plugin host discovers `skills/*/SKILL.md` directly
from the working tree. Renaming a skill directory or moving a `SKILL.md` *is*
the deployment; there's nothing to regenerate afterward.

**Frontmatter.** The spec requires only `description`; the directory name is
authoritative for the skill's identity.

- `description` is **required**. Open with a "Use when" construction,
  describe *triggers* rather than the workflow, third person, under 1024
  characters.
- `name` is **optional**. Where present it must match the directory name. Do
  not add it where it is absent and do not strip it where it exists.
- `metadata.version` is a local convention — not part of the documented skill
  schema, but it validates clean and this repo uses it. Keep it in sync;
  `.version-bump.json` lists all three and `./scripts/bump-version.sh --check`
  catches drift.

**Trigger boundaries matter more than trigger counts.** The three skills sit
close together and were once ambiguous — `preflight-demo` claimed "what does
this check" while `preflight-rules` claimed "what does it check". Before
editing any description, read the other two and confirm no phrase in one
would plausibly match another:

| Skill | Owns |
|---|---|
| `preflight-campaign` | checking a real campaign the user supplies |
| `preflight-demo` | watching the checker run on bundled sample data |
| `preflight-rules` | which rules exist, what each tests, how to retune them |

What a change to a skill must not break:

- **The `${CLAUDE_PLUGIN_ROOT}/bin/preflight` invocation.** Skills call the
  shell launcher, not `python -m` or `uv run` — those assume a dev
  environment a plugin install won't have.
- **Non-overlapping trigger descriptions** — see the table above.
- **The plugin manifest's warning staying the single expected one.** CI's
  "Validate the plugin manifest" step asserts that `claude plugin validate
  .claude-plugin/plugin.json` produces *only* the known "CLAUDE.md at the
  plugin root" warning (see CI workflows below). A skill authoring mistake
  that produces a second warning fails that check, not a skill-specific one.

## Conventions that look like mistakes

**No runtime dependencies.** The package imports nothing outside the standard
library. `httpx` is an optional extra behind a lazy import, needed only for
the live Instantly provider. This is load-bearing: the plugin runs on
whatever `python3` the user already has, with no install step. Adding a
dependency breaks that. If you need a library, implement the subset you
need — see `_yaml.py`, which replaces PyYAML and is differentially tested
against it.

**Python 3.9 floor.** Deliberate, and deliberately different from the other
plugins in this account. 3.9 is the oldest interpreter the plugin can
encounter on a user's machine, and since the package has no dependencies
there is nothing forcing it higher. The CI matrix runs 3.9 through 3.13 and a
bare-interpreter job that installs nothing at all. Do not raise the floor to
match another repo.

Practical consequence: no `match` statements, no `X | Y` outside annotations,
no `dataclass(slots=)` or `kw_only=`. `mypy` targets 3.10 because it dropped
3.9 support; `ruff`'s `target-version = "py39"` is what actually holds the
line.

## Commands

```bash
uv sync --all-extras            # install (dev toolchain only; the package needs nothing)

uv run pytest                   # full suite, ~6s
uv run ruff format --check .    # format check
uv run ruff check .             # lint
uv run mypy                     # typecheck, strict

uv run campaign-preflight demo  # the bundled synthetic campaign
claude plugin validate . --strict
```

Two things have `--check` modes, and CI runs both, so drift is a build
failure rather than a surprise:

```bash
uv run python scripts/generate_rules_doc.py --check   # docs/rules.md
./scripts/bump-version.sh --check                     # version agreement
```

`docs/rules.md` is generated from the live rule registry (76 rules across
seven categories: campaign, contacts, suppression, personalization, copy,
schedule, senders). Never hand-edit it; regenerate after adding or changing
a rule.

## Testing conventions

The full suite, three directories, each with a distinct job:

| Directory | Contains |
|---|---|
| `tests/unit/` | Rules, normalization, config, scoring, redaction, reporting, MCP safety, properties |
| `tests/integration/` | CLI, engine, performance |
| `tests/contract/` | Provider behaviour against mocked HTTP |

Full detail, including the fixture-builder pattern (`make_context()` in
`tests/helpers.py`), is in `CONTRIBUTING.md` — this section is the pointer,
that one is the reference.

Treat these as security tests, not unit tests, when you touch anything near
them: `test_instantly_transport.py` (the write barrier), `test_mcp_safety.py`
(the agent-facing tool surface), and
`test_registry.py::test_missing_required_data_never_passes` (the core
`UNKNOWN`-not-`PASS` guarantee). All three also run again, independently, in
`.github/workflows/security.yml`'s `read-only-boundary` job — a change that
passes CI's `test` job but not that job is still broken.

**A known coverage gap, not yet closed.** `test_mcp_safety.py` verifies the
properties of the *correctly-built* tool list (no mutating verb present,
every description starts with `READ-ONLY.`, etc.) but never constructs a
deliberately malformed tool and asserts `_assert_read_only()` actually raises
on it. The three `raise PreflightError(...)` branches inside that function
(`mcp/server.py`, currently around lines 501, 507, 512) have zero coverage
from the test suite or from any documented CLI/MCP usage — verified directly
against `coverage.xml`, not inferred. This is the fail-closed guard named in
Non-negotiables above; its own failure paths are the part most worth adding
a test for next.

## CI workflows

Three workflows, each gating something different:

| Workflow | Gates |
|---|---|
| `ci.yml` | Lint (`ruff format --check`, `ruff check`, `mypy`) on every push/PR; the full test matrix across Python 3.9–3.13, each also uploading coverage, with a `3.12`-only step enforcing per-file coverage floors on six safety-critical modules (`scoring.py` 95%, `engine.py` 85%, `instantly_transport.py` 95%, `redaction.py` 95%, `errors.py` 85%, `_yaml.py` 85% — notably **not** `mcp/server.py`, see the coverage gap above); the demo's documented exit codes (0/2/3); `docs/rules.md` freshness; the plugin and marketplace manifests validating cleanly (with the one expected CLAUDE.md warning) |
| `release.yml` | Runs only on a `v*` tag or manual dispatch. Re-verifies lint/type/test, confirms the tag matches `__version__`, confirms `CHANGELOG.md` has a matching section, builds the wheel/sdist, installs the wheel with `--no-deps` into a bare 3.9 venv to prove the no-runtime-dependencies claim, packages the plugin bundle, and publishes a GitHub release. PyPI publication is a separate job gated `if: false` until a trusted-publisher environment exists — do not flip that without setting up the "pypi" environment first. |
| `security.yml` | CodeQL, a dependency-graph advisory check, `pip-audit` over the locked dev toolchain, an AST walk proving every `src/` import resolves to the standard library (plus the one allowed `httpx` extra), a secret/PII scan, a check that fixture data uses only RFC 2606 reserved domains, confirmation `.env` is never tracked, and the read-only-boundary tests above. Runs on push, PR, and a weekly schedule so a newly-disclosed advisory in the dev toolchain surfaces on its own. |

## Releasing

The version appears in six files. `.version-bump.json` lists all of them.

```bash
./scripts/bump-version.sh 0.2.0
# add a CHANGELOG.md section for the new version
uv run campaign-preflight version
git tag v0.2.0
```

The bump script fails loudly if a listed path is missing or a pattern stops
matching, because a half-applied bump leaves the manifest and the package
disagreeing with nothing to say so. `release.yml` then independently
re-verifies the tag against `__version__` and that `CHANGELOG.md` has a
matching section before it will build or publish anything — see CI workflows
above.

## Adding a rule

Rules are small and pure: they read a frozen context, return one
`RuleResult`, and do nothing else. Subclass `Rule`, decorate with
`@register`, and declare `requires` **honestly** — that declaration is what
makes the `UNKNOWN` guarantee work. Then regenerate `docs/rules.md`.

`tests/unit/test_registry.py` holds every rule to the shared contract
automatically: metadata completeness, determinism, no mutation of the
context, a remediation on every actionable result, and `UNKNOWN` rather than
`PASS` whenever a required capability is missing.

Rules that will not be accepted: spam-word lists, invented deliverability
metrics, and legal determinations. The reasoning is in `CONTRIBUTING.md`.

## Fixture data

Every address in this repository is synthetic and uses an RFC 2606 reserved
domain. `.gitignore` deliberately blocks `/leads.csv` and `/suppressions.csv`
at the root so real contact data cannot be committed by accident. CI fails if
a non-reserved domain appears in fixture data.

Never commit real contact data, a real campaign export, or an API key.

## What this repo deliberately does not do

Each of these has come up before as "why doesn't it just...". The answer is
usually the Non-negotiables section above.

- **It does not send, activate, edit, or pause a campaign.** Every tool this
  server exposes is read-only by construction, not by convention — there is
  no code path to a provider mutation to accidentally call.
- **It does not score deliverability or invent a metric a provider doesn't
  expose.** If Instantly doesn't return it, the rule returns `UNKNOWN`, not
  an estimate. See "Rules that will not be accepted" above and in
  `CONTRIBUTING.md`.
- **It does not make legal determinations.** Region and consent rules check a
  campaign against policy the *user* configured; they assert nothing about
  what the law actually requires.
- **It does not filter on spam-word lists.** "Free" and "act now" are not
  evidence of anything, and rules built on them would just teach users to
  route around a checklist instead of fixing the campaign.
- **It does not require a network connection, an API key, or an account to
  be useful.** The CLI, the demo, and two of the three providers (`csv`,
  `fixture`) work fully offline. Only the live Instantly provider needs
  credentials, and it's an optional extra.
- **It does not vendor a second copy of `src/` into a `plugin/` directory.**
  See Layout above — the repo root *is* the plugin, and `bin/preflight` runs
  `src/` directly. If a `plugin/` directory reappears, something regressed.

## Architectural decisions

There are no ADRs in this repository yet (`docs/decisions/` does not exist).
When a genuinely load-bearing, non-obvious decision needs one — the kind of
thing this file currently just asserts without recording the "why we didn't
do it the other way" reasoning, e.g. the Python 3.9 floor, the no-runtime-
dependencies rule, or the read-only allowlist design — write it as a MADR
4.0.0 record under `docs/decisions/` rather than adding another paragraph
here. `pipeline-waterfall`'s `docs/decisions/` is the reference for format
and numbering in this account. Don't manufacture ADRs to fill the directory;
only decisions with real alternatives-considered reasoning behind them belong
there.

## Related safety tooling

`deliverability-guard` (also in this account) addresses pre-send safety from
the sending-reputation side — bounce/complaint-rate throttling — while this
repo addresses it from the campaign-content side (suppressions, merge
fields, opt-out language, schedules). They don't share code and neither
depends on the other, but if you're working on pre-send safety in one, check
whether the change has a counterpart in the other.
