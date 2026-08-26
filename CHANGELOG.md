# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The **report JSON schema** is versioned separately from the package; see
`report_schema_version` in any JSON report.

## [Unreleased]

### Changed

- **The package now has zero runtime dependencies** and targets Python 3.9.
  This is what makes the Cowork plugin possible: the only interpreter
  guaranteed there is the system `python3` with nothing installed alongside it,
  so any dependency would mean the plugin does not work for its recipient.
  - pydantic v2 models became frozen dataclasses, keeping the "a rule cannot
    mutate its input" guarantee at runtime.
  - PyYAML was replaced by a bundled YAML-subset parser, differentially tested
    against PyYAML on every document the tool reads (values and types).
  - typer and rich were replaced by argparse and raw ANSI.
  - The `mcp` SDK was replaced by a direct JSON-RPC-over-stdio implementation.
  - `httpx` became an optional extra (`campaign-preflight[instantly]`) behind a
    lazy import, needed only for the live Instantly provider.
- No rule logic, scoring, or normalization changed as part of this.

### Added

- **Cowork plugin** (`plugin/campaign-preflight`, built by
  `scripts/build_plugin.py`) with three skills covering the ways a non-CLI user
  actually arrives: an uploaded file, a pasted lead list, or a campaign
  described in conversation.
- MCP tools now carry `readOnlyHint` / `destructiveHint` annotations, and the
  server refuses to start if any tool is missing them.
- `audit_allowlist()` makes the read-only transport's import-time guard a
  testable function, and rejects unanchored patterns as well as mutating methods.
- GitHub Actions for CI, security, and release; issue and pull-request
  templates; Dependabot.

### Fixed

- `_coerce` would recurse infinitely on a PEP 604 (`str | None`) annotation.
  Found by enabling the annotation modernization lint.
- URL extraction missed typo'd schemes (`htp:/example`) and schemeless hosts,
  so `copy.malformed_urls` did not fire on them.
- `copy.placeholder_text` scanned tag-stripped text, so an angle-bracket
  placeholder such as `<your company here>` was removed before it could be found.
- `copy.generation_artifacts` did not match a model preamble on the body's first
  line, because the pattern was anchored without `MULTILINE`.
- Redaction was not idempotent: masking an already-masked address consumed
  another character each pass.
- `normalize_domain` was not idempotent for values with interior whitespace.


## [0.1.0] - 2026-08-25

First release.

### Added

- **Rule engine** with 76 deterministic checks across seven categories:
  campaign configuration (10), contact data (15), suppression and eligibility
  (8), personalization (13), campaign copy (13), schedule and timezone (9), and
  sender readiness (8).
- **Capability model** that distinguishes "read succeeded and found nothing"
  from "could not read". Rules declaring a capability they cannot get are
  short-circuited to `UNKNOWN` by the engine, so missing data can never become a
  `PASS`.
- **Four readiness verdicts**: `READY`, `READY_WITH_WARNINGS`, `NOT_READY`, and
  `INCOMPLETE`, with a published 0–100 score, an itemized derivation, and a
  separate confidence level.
- **CSV/file provider** with streaming reads, header-alias resolution, duplicate
  column folding, BOM and CRLF handling, and row-level problem reporting.
  Malformed rows are kept and reported, never dropped.
- **Read-only Instantly.ai v2 provider** covering campaign retrieval, lead
  listing, sending accounts, block-list entries, and campaign analytics, with
  bounded retries, `Retry-After` handling, cursor pagination with loop
  detection, and documented enum mapping.
- **Transport-level write barrier**: every Instantly request is matched against
  an explicit read-only allowlist and blocked before it leaves the process.
  Import-time assertions forbid mutating methods in the allowlist.
- **Read-only MCP server** exposing six tools over stdio, which refuses to start
  if any tool has a mutating name or fails to declare itself read-only.
- **CLI** (`demo`, `check`, `instantly`, `rules list`, `rules explain`,
  `validate-config`, `version`) with documented exit codes and `--fail-on`
  thresholds.
- **Three report formats**: terminal, versioned JSON with a published schema,
  and Markdown suitable for a pull request. All deterministic and redacted by
  default.
- **Optional affected-record CSV export**, with spreadsheet formula injection
  neutralized in every written value.
- **Strict, versioned configuration**: unknown rule ids, unknown options, and
  out-of-range values are hard errors.
- **Bundled synthetic demo** requiring no credentials and no network,
  demonstrating passes, warnings, blockers, and checks that could not run.
- **Three worked examples**, one per verdict: clean, risky, and incomplete.
- **Documentation**: rule catalogue (generated from the registry), configuration
  reference, Instantly integration, MCP setup, CI usage, limitations, and an
  architecture document including a threat model.

### Security

- API keys are read from the environment only and are never accepted as CLI or
  MCP tool arguments.
- Credential scrubbing is applied unconditionally to all output; `--no-redact`
  disables PII masking, never secret masking.
- Contact mailboxes are masked in reports by default; domains are retained
  because they make findings actionable.
- Report files are written with owner-only permissions via an atomic rename.
- Symlinks are refused unless explicitly permitted.
- Prompt-injection text arriving through lead research is a `BLOCKER`-severity
  finding.
- Optional LLM claim evaluation is disabled by default; no data leaves the
  machine without explicit configuration.

[Unreleased]: https://github.com/katekruger/campaignpreflightplugin/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/katekruger/campaignpreflightplugin/releases/tag/v0.1.0
