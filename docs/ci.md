# Using Campaign Preflight in CI

Campaign Preflight is a linter, so it fits where a linter fits: in the pipeline
that runs before a campaign is imported or activated.

```bash
campaign-preflight check \
  --campaign campaigns/q4/campaign.yaml \
  --leads campaigns/q4/leads.csv \
  --suppressions ops/suppressions.csv \
  --config ops/preflight.yaml \
  --fail-on blocker
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | `READY` — nothing to fix, or nothing above your `--fail-on` threshold. |
| `1` | `READY_WITH_WARNINGS` — findings exist, none blocking. |
| `2` | `NOT_READY` — a blocker (or a `HIGH` failure) is present. |
| `3` | `INCOMPLETE` — a critical check could not run. |
| `4` | Configuration or input error (bad path, invalid config). |
| `5` | Provider or authentication error. |
| `6` | Unexpected internal error. |

Codes `4`–`6` mean the tool could not do its job. Codes `0`–`3` are verdicts
about your campaign.

## `--fail-on`

`--fail-on` raises the bar at which a verdict becomes a nonzero exit. It never
changes the verdict itself: the report always says what it found.

| Value | Exits nonzero when |
|---|---|
| `none` | Never (except on codes 4–6). Good for a report-only step. |
| `blocker` | A `BLOCKER` failure is present, or the run is `INCOMPLETE`. |
| `high` | A `BLOCKER` or `HIGH` failure is present, or the run is `INCOMPLETE`. |
| `warning` | Any failure or warning. This is the default for `check`. |

`INCOMPLETE` is not suppressed by a severity threshold, except by `--fail-on
none`. A check that could not run is a different problem from a low-severity
finding, and silencing it with a severity filter would defeat the point.

`demo` defaults to `--fail-on none` so running the demo does not fail your
shell.

## GitHub Actions

### Gate a campaign import

```yaml
name: Campaign preflight

on:
  pull_request:
    paths: ["campaigns/**"]

jobs:
  preflight:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v7
        with:
          enable-cache: true

      - name: Install
        run: uv tool install campaign-preflight

      - name: Preflight
        run: |
          campaign-preflight check \
            --campaign campaigns/q4/campaign.yaml \
            --leads campaigns/q4/leads.csv \
            --suppressions ops/suppressions.csv \
            --config ops/preflight.yaml \
            --fail-on blocker \
            --format markdown \
            --output preflight.md

      - name: Comment the report on the PR
        if: always()
        run: gh pr comment "$PR" --body-file preflight.md
        env:
          GH_TOKEN: ${{ github.token }}
          PR: ${{ github.event.pull_request.number }}
```

### Check a live campaign before activation

```yaml
      - name: Preflight the live campaign
        env:
          INSTANTLY_API_KEY: ${{ secrets.INSTANTLY_API_KEY }}
        run: |
          campaign-preflight instantly \
            --campaign-id "${{ inputs.campaign_id }}" \
            --fail-on blocker \
            --format json \
            --output preflight.json
```

The key goes in the environment. There is no `--api-key` flag, so it cannot
reach the command line, `ps` output, or a build log.

## GitLab CI

```yaml
campaign-preflight:
  image: python:3.12-slim
  script:
    - pip install campaign-preflight
    - campaign-preflight check --campaign campaign.yaml --leads leads.csv --fail-on blocker
  artifacts:
    when: always
    paths: [preflight.json]
```

## Pre-commit

```yaml
repos:
  - repo: local
    hooks:
      - id: campaign-preflight
        name: campaign preflight
        entry: campaign-preflight check --fail-on blocker --campaign campaign.yaml --leads
        language: system
        files: ^campaigns/.*\.csv$
        pass_filenames: true
```

## Machine-readable output

```bash
campaign-preflight check --campaign c.yaml --leads l.csv --format json --output report.json
```

The JSON conforms to a versioned schema shipped inside the package
(`campaign_preflight/schemas/report-1.0.0.json`), so you can validate it in your
own pipeline:

```python
import json, jsonschema
from campaign_preflight.reporting import load_schema

jsonschema.validate(json.load(open("report.json")), load_schema())
```

Output is deterministic: identical input produces byte-identical JSON. Commit
the report and the diffs mean something.

### Extracting a verdict

```bash
readiness=$(campaign-preflight check ... --format json | jq -r .readiness)
score=$(campaign-preflight check ... --format json | jq -r .score)
```

### Getting the rows to fix

```bash
campaign-preflight check ... --affected-csv rows-to-fix.csv
```

Every value written there is neutralized against spreadsheet formula injection —
this tool reports that risk, so writing an export that reintroduces it would be
indefensible.

## Privacy in CI

Reports are redacted by default: mailbox local parts are masked, domains are
kept. Report files are written with owner-only permissions.

If you post a report to a PR comment or an issue, it will be visible to everyone
with repository access. Keep the default redaction on for that. Use
`--no-redact` only when writing to somewhere you control, and note that the
report announces its own unredacted status when you do.

Credentials are scrubbed from output unconditionally — `--no-redact` turns off
PII masking, never secret masking.

## What CI cannot tell you

Results are a point-in-time snapshot. A campaign that passed preflight at
09:00 can be edited at 09:05. Gate the import, not the world.
