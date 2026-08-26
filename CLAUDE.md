# CLAUDE.md

Working notes for anyone — human or agent — changing this repository. These are
the conventions that are easy to break by accident because they look like
mistakes until you know why they are there.

## What this is

`campaign-preflight`, a read-only linter for outbound email campaigns. It ships
three ways: a Python CLI, an MCP server, and a Claude plugin.

**Naming.** The repository is `campaignpreflightplugin`. The plugin is
`campaign-preflight`. Use the plugin name in prose, headings, and every install
command. The repository name belongs only inside URLs. They differ because the
repo was renamed after the plugin was published; do not "fix" one to match the
other.

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

## The two ideas worth protecting

Everything else is implementation detail. These two are the product.

**1. Read-only is structural, not a promise.** There is no code path that writes
to a provider. The Instantly transport matches every request against an explicit
allowlist and raises *before the request leaves the process*
(`instantly_transport.py`). The MCP server refuses to start if a tool has a
mutating verb in its name or is missing its `readOnlyHint` annotation. Both are
covered by tests that should be treated as security tests, not unit tests.

**2. "Checked and passed" is never confused with "couldn't check."** Every
provider read returns data *plus the reason it does or does not exist*. Every
rule declares the capabilities it needs, and the engine short-circuits a rule to
`UNKNOWN` before it runs if that data is unavailable. Rules cannot opt out.
There are four verdicts, not two, and `INCOMPLETE` is a first-class outcome.

If a change would let missing data produce a `PASS`, it is wrong, regardless of
what else it improves.

## Conventions that look like mistakes

**No runtime dependencies.** The package imports nothing outside the standard
library. `httpx` is an optional extra behind a lazy import, needed only for the
live Instantly provider. This is load-bearing: the plugin runs on whatever
`python3` the user already has, with no install step. Adding a dependency breaks
that. If you need a library, implement the subset you need — see `_yaml.py`,
which replaces PyYAML and is differentially tested against it.

**Python 3.9 floor.** Deliberate, and deliberately different from the other
plugins in this account. 3.9 is the oldest interpreter the plugin can encounter
on a user's machine, and since the package has no dependencies there is nothing
forcing it higher. The CI matrix runs 3.9 through 3.13 and a bare-interpreter
job that installs nothing at all. Do not raise the floor to match another repo.

Practical consequence: no `match` statements, no `X | Y` outside annotations, no
`dataclass(slots=)` or `kw_only=`. `mypy` targets 3.10 because it dropped 3.9
support; `ruff`'s `target-version = "py39"` is what actually holds the line.

**`metadata.version` in SKILL.md frontmatter.** Not part of the documented skill
schema, but it validates clean and this repo uses it. Keep it, and keep it in
sync — `.version-bump.json` lists it. Do not strip it as unrecognized.

## SKILL.md frontmatter

The spec requires only `description`; the directory name is authoritative for
the skill's identity.

- `description` is **required**. Open with a "Use when" construction, describe
  *triggers* rather than the workflow, third person, under 1024 characters.
- `name` is **optional**. Where present it must match the directory name. Do not
  add it where it is absent and do not strip it where it exists.
- `metadata.version` is a local convention. See above.

**Trigger boundaries matter more than trigger counts.** The three skills sit
close together and were once ambiguous — `preflight-demo` claimed "what does
this check" while `preflight-rules` claimed "what does it check". Before editing
any description, read the other two and confirm no phrase in one would plausibly
match another:

| Skill | Owns |
|---|---|
| `preflight-campaign` | checking a real campaign the user supplies |
| `preflight-demo` | watching the checker run on bundled sample data |
| `preflight-rules` | which rules exist, what each tests, how to retune them |

## Commands

```bash
uv sync --all-extras            # install (dev toolchain only; the package needs nothing)

uv run pytest                   # full suite
uv run ruff format .            # format
uv run ruff check .             # lint
uv run mypy                     # typecheck, strict

uv run campaign-preflight demo  # the bundled synthetic campaign
claude plugin validate . --strict
```

Two things have `--check` modes, and CI runs both, so drift is a build failure
rather than a surprise:

```bash
uv run python scripts/generate_rules_doc.py --check   # docs/rules.md
./scripts/bump-version.sh --check                     # version agreement
```

`docs/rules.md` is generated from the live rule registry. Never hand-edit it;
regenerate after adding or changing a rule.

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
disagreeing with nothing to say so.

## Adding a rule

Rules are small and pure: they read a frozen context, return one `RuleResult`,
and do nothing else. Subclass `Rule`, decorate with `@register`, and declare
`requires` **honestly** — that declaration is what makes the `UNKNOWN` guarantee
work. Then regenerate `docs/rules.md`.

`tests/unit/test_registry.py` holds every rule to the shared contract
automatically: metadata completeness, determinism, no mutation of the context, a
remediation on every actionable result, and `UNKNOWN` rather than `PASS`
whenever a required capability is missing.

Rules that will not be accepted: spam-word lists, invented deliverability
metrics, and legal determinations. The reasoning is in `CONTRIBUTING.md`.

## Fixture data

Every address in this repository is synthetic and uses an RFC 2606 reserved
domain. `.gitignore` deliberately blocks `/leads.csv` and `/suppressions.csv` at
the root so real contact data cannot be committed by accident. CI fails if a
non-reserved domain appears in fixture data.

Never commit real contact data, a real campaign export, or an API key.
