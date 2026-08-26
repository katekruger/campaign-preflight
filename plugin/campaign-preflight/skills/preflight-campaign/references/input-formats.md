# Input formats

Two files are required: a campaign file and a leads CSV. Three more are
optional, and their absence changes the answer — an omitted suppression list
means the suppression checks return `UNKNOWN`, not `PASS`.

## Leads CSV

Only an email column is strictly required. Everything else improves coverage.

```csv
email,first_name,last_name,company_name,company_domain,job_title,country,personalization,status
ana@acme.example.com,Ana,Diaz,Acme Co,acme.example.com,VP Operations,US,Acme opened a second facility.,not_contacted
```

### Column names are resolved generously

Write the header the way the user's export already writes it. All of these
resolve to the same field:

| Field | Accepted headers |
|---|---|
| `email` | Email, E-Mail Address, email_address, work_email, workEmail |
| `first_name` | First Name, firstName, fname, given_name |
| `last_name` | Last Name, lastName, surname, family_name |
| `company_name` | Company, Account, Organization, company_name |
| `company_domain` | Domain, Website, URL, company_website |
| `job_title` | Title, Job Title, Position, Role |
| `country` | Country, country_code |
| `region` | Region, State, Territory |
| `personalization` | Personalization, Icebreaker, Opener, custom_message |
| `assigned_sender` | Sender, sending_account, from_email |
| `status` | Status |
| `suppressed` | Suppressed, do_not_contact, dnc |

Any header that is not recognized becomes a **custom variable**, usable in copy
as `{{that_header}}`. Nothing is dropped.

### Rows that look wrong are kept

A short row, a long row, a malformed address — all are parsed, kept, and
reported with their spreadsheet row number. Discarding a bad row would make a
broken list look clean.

### Two columns worth adding if the user has them

- **`status`** — with values like `contacted` / `not_contacted`, this enables
  the "already contacted in this campaign" check. Without it that check is
  `UNKNOWN`.
- **`suppressed`** — `true`/`false`, if the export already carries a
  do-not-contact flag.

## Campaign file

YAML or JSON. This is the full shape; omit anything the user does not know.

```yaml
version: 1

campaign:
  id: q4-outbound
  name: Q4 Outbound
  status: draft              # draft | paused | scheduled | active | completed
  timezone: America/New_York
  daily_limit: 80
  stop_on_reply: true        # omit entirely if unknown -- do not guess
  stop_on_auto_reply: true

  schedule:
    start_date: 2026-09-01   # optional
    end_date: 2026-12-31     # optional
    timezone: America/New_York
    windows:
      - name: Weekday business hours
        start: "09:00"       # quote times, or YAML reads them oddly
        end: "17:00"
        days: [mon, tue, wed, thu, fri]

  senders:
    - email: dana@example.com
      enabled: true
      status: active         # active | paused | connection_error | ...
      health_score: 92       # provider's own score; omit if unknown
      daily_limit: 60
      warmup_status: active

  custom_variables:
    product_name: Northwind Analytics

  steps:
    - type: email
      delay: 0
      delay_unit: days
      subject: "{{first_name}}, a question about {{company_name}}"
      body: |
        Hi {{first_name}},

        {{personalization}}

        Worth fifteen minutes?

        Reply "unsubscribe" and I will remove you.
    - type: email
      delay: 4
      subject: ""            # empty on a follow-up threads the reply; this is fine
      body: |
        Following up once, {{first_name}}.

        Reply "unsubscribe" to opt out.
```

### Minimum viable campaign file

If the user only described a single email and nothing else:

```yaml
version: 1
campaign:
  name: Untitled Campaign
  status: draft
  steps:
    - type: email
      subject: "Subject line here"
      body: |
        Body here.
```

Everything absent becomes an honest `UNKNOWN` or `NOT_APPLICABLE`.

### Fields that most change the verdict

| Field | Why it matters |
|---|---|
| `stop_on_reply: false` | **Blocker.** Repliers keep getting follow-ups. |
| `daily_limit` | Above 250 is a blocker by default, above 100 a warning. |
| `senders[].health_score` | Below 80 fails; all below 80 is a blocker. |
| `senders[].daily_limit` | Needed to check the campaign fits sender capacity. |
| `schedule.windows[].days` | Weekend days warn unless configured otherwise. |
| `timezone` | Must be a valid IANA name (`America/New_York`, not `EST`). |

## Suppressions CSV (optional but important)

```csv
value,is_domain,reason
someone@example.com,false,unsubscribed 2026-02-11
blockeddomain.example.com,true,domain-level opt-out
```

`value` may also be named `email`, `domain`, `bl_value`, `entry`, or `address`.
A value with no `@` is treated as a domain.

**Without this file, the tool cannot tell whether anyone on the list has
unsubscribed.** That makes the run `INCOMPLETE`. Say so plainly rather than
letting it read as a clean bill of health.

## Sender file (optional)

Only needed if senders are not already declared inline in the campaign file.

```yaml
senders:
  - email: dana@example.com
    enabled: true
    status: active
    health_score: 92
    daily_limit: 60
    warmup_status: active
```

## Evidence file (optional)

Only relevant when personalization makes factual claims that should be checked
against sources.

```json
{
  "version": 1,
  "evidence": [
    {
      "evidence_id": "ev-001",
      "lead_ref": "L-001",
      "source_url": "https://acme.example.com/news",
      "title": "Acme opens second facility",
      "retrieved_at": "2026-08-02T14:11:00Z",
      "excerpt": "Acme Co today opened a second facility with 40 staff.",
      "company_name": "Acme Co"
    }
  ],
  "claims": [
    {
      "claim_id": "cl-001",
      "lead_ref": "L-001",
      "text": "Acme opened a second facility with 40 staff.",
      "evidence_ids": ["ev-001"],
      "numeric_values": ["40"]
    }
  ]
}
```

Without evidence, claim checks return `UNKNOWN`. The tool will not accuse copy
of being fabricated on no information.

## Configuration file (optional)

Turns on the policy checks that depend on the user's own lists.

```yaml
version: 1
settings:
  target_timezone: America/New_York
  required_variables: [first_name, company_name]
  internal_domains: [ourcompany.example.com]
  customer_domains: [existingcustomer.example.com]
  competitor_domains: [rival.example.com]
  allow_weekend_sending: false
rules:
  campaign.daily_volume:
    warning_above: 100
    blocker_above: 250
  senders.health_below_threshold:
    minimum_score: 80
```

Unconfigured policy lists make their rules `NOT_APPLICABLE` — the tool does not
invent a policy the user never set.
