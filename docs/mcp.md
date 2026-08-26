# MCP server

Campaign Preflight ships a local, read-only MCP server. Point an agent at it and
it can answer "is this campaign safe to launch?" — and nothing else.

```bash
uv run campaign-preflight-mcp     # stdio
```

## Why read-only matters here

Giving an agent a provider API key gives it the ability to activate a campaign,
import leads, and send email. This server is the alternative: the agent gets the
analysis and none of the authority.

The guarantee is enforced, not asserted:

- The server **refuses to start** if any tool has a mutating verb in its name,
  or if any tool description does not begin with `READ-ONLY`.
- No tool accepts an API key. Credentials come from the server process's
  environment.
- File access is limited to the exact paths a caller passes. No directory is
  scanned, walked, or globbed.
- Underneath, the Instantly provider's transport allowlist blocks every write at
  the HTTP layer.

`tests/unit/test_mcp_safety.py` asserts the exposed tool list against an
explicit set, so a new tool cannot appear without a deliberate edit and review.

## Tools

| Tool | What it does |
|---|---|
| `preflight_demo` | Runs the bundled synthetic demo. No credentials, no network. |
| `preflight_files` | Checks a campaign from local files. |
| `preflight_instantly_campaign` | Inspects a live Instantly campaign. |
| `list_preflight_rules` | Lists the rule catalogue, optionally by category. |
| `explain_preflight_rule` | Explains one rule and its options. |
| `validate_preflight_config` | Validates a config file without running checks. |

There is no tool to activate, launch, send, create, update, move, patch, delete,
or approve anything. There never will be; that is what the startup assertion is
for.

### `preflight_files`

| Argument | Required | Notes |
|---|---|---|
| `campaign_path` | yes | Campaign YAML or JSON. |
| `leads_path` | yes | Leads CSV. |
| `senders_path` | no | Sender YAML/JSON with health data. |
| `suppressions_path` | no | Suppressions CSV. |
| `evidence_path` | no | Evidence JSON bundle. |
| `config_path` | no | Rules configuration. |
| `output_format` | no | `structured` (default), `terminal`, `json`, `markdown`. |
| `max_samples` | no | Affected records per finding. Clamped to 25. |

Omitting an optional input does not silently produce a clean result. It is
reported as an unavailable capability, and any rule depending on it returns
`UNKNOWN`.

### `preflight_instantly_campaign`

| Argument | Required | Notes |
|---|---|---|
| `campaign_id` | yes | Instantly campaign UUID. |
| `config_path` | no | Rules configuration. |
| `max_samples` | no | Clamped to 25. |
| `lead_limit` | no | Default 5,000. |

`INSTANTLY_API_KEY` must be set in the server's environment. If it is missing,
the tool returns a structured error explaining that the key cannot be passed as
an argument.

## What a preflight tool returns

```json
{
  "report_id": "cpf-3f9a1c0e8b2d",
  "readiness": "NOT_READY",
  "score": 68,
  "confidence": "MEDIUM",
  "score_explanation": "100 - (campaign.stop_on_reply FAIL/BLOCKER -30, ...) = 68",
  "counts": { "leads": 180, "senders": 3, "blockers": 2, "warnings": 4, "unknown": 1 },
  "blockers": [ { "rule_id": "suppression.contact_listed", "summary": "...", "remediation": "..." } ],
  "warnings": [ ... ],
  "unknown_checks": [ ... ],
  "unknown_checks_note": "UNKNOWN means the check could not run. It is not a pass. ...",
  "recommended_remediations": [ "[suppression.contact_listed] Remove these contacts ..." ],
  "limitations": [ ... ],
  "redacted": true,
  "snapshot_note": "Point-in-time snapshot. Campaign state may change after this check ran.",
  "disclaimer": "Campaign Preflight is a read-only linter. ..."
}
```

`report_id` is derived from the findings, not the clock, so re-running an
unchanged check returns the same id. An agent can use that to tell "nothing
moved" from "something changed".

Output is redacted by default: mailbox local parts are masked, domains are kept
(a domain is what makes a suppression finding actionable), and affected-record
samples are bounded.

## Registering with Claude Code

```bash
claude mcp add campaign-preflight -- uv run --directory /path/to/campaign-preflight campaign-preflight-mcp
```

With an Instantly key:

```bash
claude mcp add campaign-preflight \
  --env INSTANTLY_API_KEY=your-key-here \
  -- uv run --directory /path/to/campaign-preflight campaign-preflight-mcp
```

## Registering with Claude Desktop

Edit `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "campaign-preflight": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/campaign-preflight",
        "campaign-preflight-mcp"
      ],
      "env": {
        "INSTANTLY_API_KEY": "your-key-here"
      }
    }
  }
}
```

If you installed the package instead of running from a checkout:

```json
{
  "mcpServers": {
    "campaign-preflight": {
      "command": "campaign-preflight-mcp",
      "env": { "INSTANTLY_API_KEY": "your-key-here" }
    }
  }
}
```

Restart Claude Desktop after editing. The MCP extra is required:

```bash
pip install "campaign-preflight[mcp]"
```

## Asking for a check

> Check campaign 01a03960-aa51-777b-8a74-c93b2883a947 before I activate it.

> Run the campaign preflight demo and show me what a blocker looks like.

> Preflight the files in ./campaigns/q4/ — campaign.yaml, leads.csv, and
> suppressions.csv — and tell me what would stop a launch.

> Explain the `senders.health_below_threshold` rule and how to configure it.

## Troubleshooting

**The server will not start.** It fails closed on a safety violation. The error
names the offending tool. This should only ever happen in a fork that added a
tool.

**Everything comes back `UNKNOWN`.** The key is missing, or lacks scope. Check
`limitations` in the response — it names the capability and the reason.

**Logs are interleaved with output.** They should not be: stdio transport owns
stdout, so all logging goes to stderr. Set `CAMPAIGN_PREFLIGHT_LOG_LEVEL=DEBUG`
for more.

**The SDK version.** The server supports both the 1.x `FastMCP` and the 2.x
`MCPServer` class names and resolves whichever is installed.
