# Instantly.ai provider

Campaign Preflight can inspect a live Instantly campaign through the official
v2 API. It reads. It never writes.

```bash
export INSTANTLY_API_KEY="..."
campaign-preflight instantly --campaign-id 01a03960-aa51-777b-8a74-c93b2883a947
```

## Getting a key

Instantly v2 keys are generated in the app under **Settings → Integrations →
API**. A v1 key will not work: v2 is a separate API with its own keys and its
own scopes.

The key is read from `INSTANTLY_API_KEY` and only from there. There is
deliberately no `--api-key` flag: a key passed on the command line ends up in
shell history, in `ps` output, and in CI logs.

## What it reads

Exactly these endpoints, and nothing else:

| Endpoint | Used for |
|---|---|
| `GET /api/v2/campaigns/{id}` | Campaign settings, schedule, sequence, sender list |
| `POST /api/v2/leads/list` | Leads in the campaign (paginated) |
| `GET /api/v2/accounts/{email}` | Sending account state and warmup score |
| `GET /api/v2/block-lists-entries` | Workspace block list (paginated) |
| `GET /api/v2/campaigns/analytics` | Lead totals, used to detect a truncated read |
| `GET /api/v2/workspaces/current` | Reachability check only |

A full run against a single-sender campaign issues five requests.

### Why one of them is a POST

`POST /api/v2/leads/list` is a **read**. Instantly models lead listing as a POST
because the filter set is too large for a query string. It is the only non-GET
entry on the allowlist, and the test suite asserts that a second one never
appears.

## How the write barrier works

Every request passes through `ReadOnlyTransport`, an `httpx` transport that
matches `(method, path)` against an explicit allowlist and raises
`ReadOnlyViolation` on anything else — **before** the request leaves the
process.

The check sits at the transport layer, below the client and below the provider,
so a future code change that adds a `PATCH` call fails loudly at runtime instead
of quietly editing somebody's campaign. Two further guards run at import time:
the allowlist cannot contain `PUT`, `PATCH`, `DELETE`, `HEAD`, or `OPTIONS`, and
`POST` is permitted for exactly one path.

`tests/contract/test_instantly_transport.py` exercises the full
method × path matrix and a list of every documented mutating endpoint —
campaign create/patch/delete/activate/pause, lead add/move/merge/delete,
account pause/resume/patch, block-list bulk-create/bulk-delete, email
reply/forward, webhook create/delete. All are blocked.

## Field mapping

| Campaign Preflight | Instantly v2 |
|---|---|
| `campaign.status` | `status` (integer enum, mapped to a label) |
| `campaign.daily_limit` | `daily_limit` |
| `campaign.stop_on_reply` | `stop_on_reply` |
| `campaign.schedule` | `campaign_schedule.schedules[]` |
| `campaign.steps` | `sequences[].steps[].variants[]`, flattened |
| `campaign.sender_emails` | `email_list[]` |
| `sender.health_score` | `stat_warmup_score` |
| `sender.status_label` | `status` (integer enum) |
| `sender.warmup_status` | `warmup_status` (integer enum) |
| `suppression.value` | `bl_value`, with `is_domain` |

### Documented enums

Campaign status: `-99` account suspended, `-2` bounce protect, `-1` accounts
unhealthy, `0` draft, `1` active, `2` paused, `3` completed, `4` running
subsequences.

Account status: `1` active, `2` paused, `3` maintenance, `-1` connection error,
`-2` soft bounce error, `-3` sending error.

Warmup status: `0` paused, `1` active, `-1` banned, `-2` spam folder unknown,
`-3` permanent suspension.

An integer outside these maps becomes `unknown:<value>` and any rule reading it
returns `UNKNOWN`. Nothing is guessed.

### What is deliberately not mapped

The v2 lead `status` enum's labels are not documented. Rather than invent them,
prior contact is derived from `timestamp_last_contact`, which **is** documented:
a non-null timestamp means contacted. That is a fact rather than an inference.

`null` is never coerced to `false`. Instantly returns `null` for settings that
were never configured, and turning "we do not know whether stop-on-reply is on"
into "stop-on-reply is off" would be a fabricated finding. A `null`
`stop_on_reply` produces `UNKNOWN`.

## Scopes, and what happens without them

Instantly issues scoped keys. Campaign Preflight degrades honestly rather than
failing:

| Missing scope | Effect |
|---|---|
| `campaigns:read` | The run is `INCOMPLETE`; nothing else can be evaluated. |
| `leads:read` | All contact and personalization checks become `UNKNOWN`. |
| `accounts:read` | Sender health and capacity become `UNKNOWN`. Senders still appear, without scores. |
| `block_list_entries:read` | Suppression checks become `UNKNOWN` and the run is `INCOMPLETE`. |

An `UNKNOWN` is never a `PASS`. A campaign whose suppression check could not run
is reported as `INCOMPLETE`, not as safe.

## Failure handling

| Condition | Behaviour |
|---|---|
| `400` | Reported as a failed capability with the API's message. |
| `401` / `403` | Reported as a **permissions** capability failure. |
| `402` | Plan restriction; treated as a permissions failure. |
| `404` | Campaign not found; capability failure, run is `INCOMPLETE`. |
| `429` | Retried up to 3 times honouring `Retry-After`, then reported. |
| `5xx` | Retried up to 3 times with jittered exponential backoff. |
| Network timeout | Retried, then reported. Connect 10s, read 30s. |
| Malformed JSON | Reported as a failed capability, never a crash. |
| Unexpected shape | Reported as a failed capability. |
| Repeated pagination cursor | The walk stops, results are marked partial. |
| Page cap reached | 100 pages (10,000 records); results marked partial. |

Only reads are retried, and retries are always bounded. Sender lookups run at a
concurrency of 3.

## Secrets

The key is sent as an `Authorization: Bearer` header and is never formatted into
a message. Every error string additionally passes through the redaction filter,
so even a provider that echoes your key back in an error body cannot get it into
a report, a log line, or an exception. There is a test for exactly that:
`test_a_key_echoed_by_the_provider_is_scrubbed`.

## Limits

- `--lead-limit` defaults to 5,000. Raise it for larger campaigns; the page cap
  is 10,000 records per list endpoint.
- Campaigns with more than 200 attached senders have the first 200 inspected,
  and the report says so.
- Results are a point-in-time snapshot. The campaign can change the moment after
  the check completes.

## Testing without an account

Every Instantly code path is exercised against `httpx.MockTransport`. You can do
the same:

```python
import httpx
from campaign_preflight.providers.instantly_provider import InstantlyProvider

provider = InstantlyProvider(
    "test-key",
    transport=httpx.MockTransport(lambda r: httpx.Response(200, json={...})),
)
```

Or point the real client at a local server:

```bash
export INSTANTLY_BASE_URL="http://localhost:8080"
```

## Reference

Official documentation: <https://developer.instantly.ai/>
