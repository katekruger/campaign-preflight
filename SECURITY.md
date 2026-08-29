# Security policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting:
<https://github.com/katekruger/campaign-preflight/security/advisories/new>

Please include:

- What the issue is and why it matters
- A minimal reproduction
- Affected version (`campaign-preflight version`)
- Your assessment of the impact

You will get an acknowledgement within 3 working days and an assessment within
10. We will keep you updated through to a fix, and credit you in the release
notes unless you would rather we did not.

## What counts as a vulnerability here

Campaign Preflight is a local, read-only tool. The things that would be serious:

### Critical

- **Any write reaching a provider.** The Instantly transport allowlist is the
  control; a bypass is a critical bug. So is an MCP tool that mutates external
  state.
- **A credential appearing in output** — a report, a log line, an exception, a
  test snapshot, or a file on disk.
- **Arbitrary code execution** from a campaign file, a leads CSV, a config file,
  or a provider response.

### High

- **A `PASS` where data was missing.** The engine must return `UNKNOWN` when a
  required capability is unavailable. A rule that reports success on absent data
  is a high-severity bug, because it is exactly the failure this tool exists to
  prevent.
- **Path traversal** — reading a file the user did not name.
- **Redaction bypass** — contact data escaping into a report that claims to be
  redacted.
- **Formula injection surviving into generated output.**

### Moderate

- Denial of service through crafted input (unbounded memory, an infinite
  pagination loop, a catastrophic regex).
- A `BLOCKER` finding that a configuration change can silently suppress.

## Not vulnerabilities

- A rule producing a false positive or false negative on judgement-call content.
  Heuristic rules are labelled as such. File a normal issue.
- Campaign Preflight not detecting a problem it makes no claim to detect. See
  [docs/limitations.md](docs/limitations.md).
- A campaign failing to deliver. The tool does not guarantee deliverability.
- An `UNKNOWN` result. That is the design working.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

Pre-1.0, security fixes land on the latest minor release only.

## Handling your data

- **Credentials** are read from the environment and never accepted as CLI
  arguments or MCP tool arguments. They are set once as an HTTP header and never
  formatted into a message. Every error string and every rendered report passes
  through a redaction filter unconditionally — `--no-redact` disables PII
  masking, never secret masking.
- **Contact data** stays on your machine. Mailbox local parts are masked in
  output by default; domains are kept because a domain is what makes a finding
  actionable. Affected-record samples are bounded.
- **Nothing is sent to an LLM** unless you explicitly configure the optional
  evaluator, and `validate-config` warns when a config enables it.
- **Report files** are written with owner-only permissions (`0600`), `O_NOFOLLOW`,
  to a temporary file that is then renamed.
- **No telemetry.** The tool makes no network request other than to the provider
  you point it at.

## Threat model

Documented in [docs/architecture.md](docs/architecture.md#threat-model), covering
credential leakage, malicious CSV content, spreadsheet formula injection, prompt
injection in lead research, path traversal, oversized input, provider response
poisoning, sensitive data in reports, and confusing an incomplete check with a
successful one.

## Security testing

Run before reporting, to confirm the control you think is broken actually is:

```bash
uv run pytest tests/contract/test_instantly_transport.py   # the write barrier
uv run pytest tests/unit/test_mcp_safety.py                # MCP tool surface
uv run pytest tests/unit/test_redaction.py                 # secret scrubbing
uv run pytest tests/unit/test_registry.py -k missing       # missing data never passes
```
