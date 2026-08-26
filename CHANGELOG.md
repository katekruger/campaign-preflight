# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The **report JSON schema** is versioned separately from the package; see
`report_schema_version` in any JSON report.

## [Unreleased]

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

[Unreleased]: https://github.com/katekruger/campaign-preflight/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/katekruger/campaign-preflight/releases/tag/v0.1.0
