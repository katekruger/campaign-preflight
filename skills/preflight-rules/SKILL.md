---
name: preflight-rules
description: >
  Use when the user asks about the rules themselves rather than about a campaign: "what rules are there", "which checks does it run", "why did it flag this", "how do I turn that check off", "that threshold is wrong for us", or names a rule id such as campaign.daily_volume. Also use when the user disagrees with a finding from an earlier check and wants it explained or retuned. Covers the rule catalogue, per-rule behaviour, and configuration. To watch the checker run on sample data, use preflight-demo instead.
metadata:
  version: "0.1.0"
---

# Explain and tune the checks

## What it checks

76 rules across seven categories. To list them:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/preflight" rules list
"${CLAUDE_PLUGIN_ROOT}/bin/preflight" rules list --category senders
```

Categories: `campaign`, `contacts`, `suppression`, `personalization`, `copy`,
`schedule`, `senders`.

Summarize rather than dumping all 76 unless the user asks for the full list:

| Category | Catches |
|---|---|
| Campaign | Stop-on-reply off, volume too high, no sending window, incoherent dates |
| Contacts | Malformed addresses, duplicates, role inboxes, placeholder values, spreadsheet formula injection |
| Suppression | Unsubscribed contacts and domains, existing customers, internal addresses, competitors |
| Personalization | Unrendered merge fields, wrong name or company, unsupported claims, prompt injection |
| Copy | Empty subject, broken links, TODO markers, missing opt-out language, duplicate steps |
| Schedule | Invalid timezone, weekend sending, zero active days, DST transitions |
| Senders | Unhealthy mailboxes, error states, volume exceeding capacity |

## Why did it flag this?

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/preflight" rules explain campaign.daily_volume
```

That prints what the rule checks, what data it needs, its default severity, and
every option with its default. Use it before answering — do not describe a rule
from memory.

Re-running the original check with `--verbose` shows the reasoning behind each
specific finding and the score arithmetic.

## "That threshold is wrong for us"

This is a legitimate and common response. Build them a config file rather than
telling them to ignore the finding.

```yaml
version: 1

settings:
  target_timezone: America/New_York
  required_variables: [first_name, company_name]
  allow_weekend_sending: true          # if they genuinely send weekends
  internal_domains: [ourcompany.example.com]
  customer_domains: [bigcustomer.example.com]

rules:
  campaign.daily_volume:
    warning_above: 300                 # their real limits
    blocker_above: 800
  senders.health_below_threshold:
    minimum_score: 70
  contacts.missing_job_title:
    enabled: false                     # turn a rule off entirely
  contacts.free_email_domain:
    severity: INFO                     # report it, but stop deducting points
```

Then validate it and pass it on the next run:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/preflight" validate-config <path>
```

Validation is strict on purpose: an unknown rule id or an unknown option is a
hard error, not a warning. A typo that silently disables a safety check is worse
than no config at all.

## "It flagged something that isn't a problem"

Some rules are judgement calls, marked `(heuristic)` in the output: duplicate
personalization, wrong-company and wrong-name detection, sensitive-topic
detection, copy length, link count, generation artifacts. These produce false
positives by design.

Tell the user that plainly, and offer either `severity: INFO` or `enabled:
false` for that rule. Do not defend a heuristic finding as if it were a fact.

If they disagree with a **non-heuristic** rule, that is more interesting —
either their situation is genuinely different (configure it) or the rule is
wrong (worth reporting at
<https://github.com/katekruger/campaign-preflight/issues>).

## What it deliberately does not check

Worth saying when the user expects it:

- **No spam-word list.** "Free" and "act now" are not evidence of anything, and
  flagging them would train people to ignore the tool.
- **No deliverability prediction.** It reports the provider's own health score
  when there is one and `UNKNOWN` when there is not. It never estimates.
- **No legal determination.** Region, domain, and opt-out rules compare a
  campaign against the user's *configured policy*. They say nothing about what
  any law requires.
- **No mailbox verification.** Address checks are syntax only — no DNS, no SMTP
  probe. A valid-looking address can still bounce.
