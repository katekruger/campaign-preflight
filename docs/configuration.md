# Configuration

Campaign Preflight runs with sensible defaults and no config file. You add one
when your thresholds differ from ours, or when you want to switch on the checks
that depend on your own domain and region lists.

```bash
campaign-preflight check --campaign campaign.yaml --leads leads.csv --config preflight.yaml
campaign-preflight validate-config preflight.yaml
```

## Validation is strict, on purpose

An unknown rule id, an unknown option, or an out-of-range value is a **hard
error** with exit code `4`. Nothing is silently ignored.

```
$ campaign-preflight validate-config preflight.yaml
Invalid: unknown rule id 'contacts.missing_firstname' (did you mean 'contacts.missing_first_name'?)
```

A typo in a safety configuration that quietly does nothing is worse than having
no configuration at all — it leaves you believing a check is running when it is
not.

## File shape

```yaml
version: 1          # required; only version 1 exists today

settings: {...}     # organization policy several rules read
scoring: {...}      # how findings become a score and a verdict
evidence: {...}     # claim/evidence checking
rules: {...}        # per-rule enablement and thresholds
```

JSON is accepted anywhere YAML is, since YAML is a superset.

## `settings`

Policy that more than one rule consults.

| Key | Default | What it does |
|---|---|---|
| `target_timezone` | unset | The timezone you expect the campaign to send in. Drives `schedule.timezone_mismatch`; unset makes that rule `NOT_APPLICABLE`. |
| `business_hours_start` | `"08:00"` | Earliest recipient-friendly hour. |
| `business_hours_end` | `"18:00"` | Latest recipient-friendly hour. |
| `allow_weekend_sending` | `false` | When true, `schedule.weekend_sending` is skipped. |
| `required_variables` | `["first_name"]` | Template variables every contact must have a value for. |
| `internal_domains` | `[]` | Your own domains. Contacts there are a `BLOCKER`. |
| `competitor_domains` | `[]` | Competitor domains to exclude. |
| `customer_domains` | `[]` | Existing-customer domains, usually exported from your CRM. |
| `restricted_regions` | `[]` | Region or country codes your organization has chosen not to contact. |
| `allow_free_email_domains` | `false` | Set true for motions where personal addresses are expected. |
| `allow_role_addresses` | `false` | Set true if `info@`-style inboxes are intended targets. |
| `opt_out_phrases` | see below | Phrases that satisfy `copy.opt_out_language`. |
| `max_samples` | `5` | Affected records shown per finding. Bounded at 100. |

Default `opt_out_phrases`: `unsubscribe`, `opt out`, `opt-out`,
`stop receiving`, `no longer wish`, `reply stop`, `remove me`.

> `restricted_regions`, `internal_domains`, `competitor_domains`, and
> `customer_domains` express **your organization's outreach policy**. They are
> not a legal compliance determination and Campaign Preflight does not provide
> legal advice. See [limitations.md](limitations.md).

Domains are lowercased, deduplicated, and stripped of a leading `@`, so
`@Example.COM` and `example.com` are the same entry. Region codes are
uppercased.

## `scoring`

```yaml
scoring:
  fail_weights:
    BLOCKER: 30.0
    HIGH: 15.0
    MEDIUM: 7.0
    LOW: 3.0
    INFO: 0.0
  warn_weights:
    BLOCKER: 10.0
    HIGH: 6.0
    MEDIUM: 3.0
    LOW: 1.0
    INFO: 0.0
  high_failure_blocks: true
  critical_rules:
    - campaign.exists
    - campaign.has_steps
    - campaign.has_senders
    - campaign.has_leads
    - suppression.contact_listed
    - senders.health_below_threshold
```

- `high_failure_blocks` — when true, any `HIGH`-severity `FAIL` forces
  `NOT_READY`. Turn it off if you want only `BLOCKER` to stop a launch.
- `critical_rules` — rules whose `UNKNOWN` makes the whole run `INCOMPLETE`.
  These are the questions you should not launch without an answer to.

Weights are published rather than hidden because you should be able to check
the arithmetic. `--verbose` prints the full derivation.

## `evidence`

```yaml
evidence:
  max_age_days: 180
  evaluator: disabled          # disabled | fixture | openai_compatible
  evaluator_model: null
  evaluator_prompt_version: v1
  max_claims_evaluated: 25
```

`evaluator` is `disabled` by default and **no lead data leaves your machine**
unless you change it. `validate-config` prints a warning when a config enables
an external evaluator, and any model-derived result is labelled
`MODEL_ASSESSED`, never presented as fact.

## `rules`

Every rule accepts `enabled` and `severity`. Most accept thresholds of their
own. Ask the tool rather than guessing:

```bash
campaign-preflight rules explain campaign.daily_volume
```

```yaml
rules:
  campaign.daily_volume:
    enabled: true
    warning_above: 100
    blocker_above: 250

  contacts.missing_first_name:
    enabled: true
    warning_ratio: 0.05     # 5% of the list missing a first name -> WARN
    blocker_ratio: 0.25     # 25% -> FAIL

  senders.health_below_threshold:
    enabled: true
    minimum_score: 80

  copy.excessive_length:
    max_body_characters: 2000
    max_subject_characters: 100

  copy.identical_steps:
    treat_identical_body_as: warn   # or "pass" for deliberate resends

  schedule.window_start_after_end:
    allow_overnight: false

  personalization.sensitive_inference:
    extra_terms: ["works nights"]

  contacts.formula_injection:
    enabled: false          # turn a rule off entirely
```

### Severity overrides

```yaml
rules:
  contacts.free_email_domain:
    severity: INFO          # report it, but do not deduct from the score
```

A severity override applies to `FAIL` and `WARN` results. It cannot turn a
`PASS` into a finding, and it cannot turn an `UNKNOWN` into a pass.

## A complete example

```yaml
version: 1

settings:
  target_timezone: America/New_York
  business_hours_start: "08:30"
  business_hours_end: "17:30"
  allow_weekend_sending: false
  required_variables: [first_name, company_name]
  internal_domains: [acme.example.com]
  customer_domains: [bigcustomer.example.com, anothercustomer.example.com]
  competitor_domains: [rival.example.com]
  max_samples: 10

scoring:
  high_failure_blocks: true

rules:
  campaign.daily_volume:
    warning_above: 150
    blocker_above: 400
  senders.health_below_threshold:
    minimum_score: 75
  copy.excessive_links:
    max_links_per_step: 2
```

## Environment variables

| Variable | Purpose |
|---|---|
| `INSTANTLY_API_KEY` | Instantly v2 API key. **Never** accepted as a CLI argument. |
| `INSTANTLY_BASE_URL` | Override the API base URL (tests and staging). |
| `CAMPAIGN_PREFLIGHT_LOG_LEVEL` | Log level for the MCP server. Logs go to stderr. |
| `CAMPAIGN_PREFLIGHT_ALLOW_SYMLINKS` | Set to `1` to permit reading through a symlink. |

See [.env.example](../.env.example).
