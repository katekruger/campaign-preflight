<!--
  GENERATED FILE - do not edit by hand.
  Regenerate with: uv run python scripts/generate_rules_doc.py
-->

# Rule catalogue

Campaign Preflight ships **76 rules** across 7 categories.

## How to read this

- **Severity** is the default. Any rule's severity can be overridden per
  campaign in your config file.
- **Requires** lists the provider capabilities a rule needs. If any of them
  is unavailable, the rule returns `UNKNOWN` — never `PASS`. That is the
  central safety property of the engine: missing data is not good news.
- **Heuristic** marks a rule that encodes a judgement call rather than a
  verifiable fact. Heuristic rules are never blockers by default and are
  labelled as heuristics everywhere they appear in output.

Every rule can be inspected from the command line:

```bash
campaign-preflight rules explain contacts.duplicate_email
```

## Statuses

| Status | Meaning |
|---|---|
| `PASS` | The rule ran and found nothing wrong. |
| `WARN` | The rule found something worth a human look. |
| `FAIL` | The rule found a defect. A `FAIL` at `BLOCKER` severity forces `NOT_READY`. |
| `UNKNOWN` | The rule could not run. **This is not a pass.** |
| `NOT_APPLICABLE` | The rule does not apply to this campaign or is not configured. |

## Severities

| Severity | Effect |
|---|---|
| `BLOCKER` | A `FAIL` always produces `NOT_READY`, whatever the score. |
| `HIGH` | A `FAIL` produces `NOT_READY` unless `scoring.high_failure_blocks` is off. |
| `MEDIUM` | Deducts from the score. |
| `LOW` | Deducts a little from the score. |
| `INFO` | Reported, deducts nothing. |

## Campaign (10)

Configuration-level checks. These read the campaign object only and catch the settings that silently break a launch.

| Rule | Severity | Requires | Checks |
|---|---|---|---|
| `campaign.daily_volume` | HIGH | `campaign` | Daily sending volume is within configured limits |
| `campaign.date_coherence` | HIGH | `campaign` | Campaign start and end dates are coherent |
| `campaign.exists` | BLOCKER | `campaign` | Campaign is readable |
| `campaign.has_leads` | BLOCKER | `leads` | Campaign has leads |
| `campaign.has_senders` | BLOCKER | `senders` | Campaign has at least one sender attached |
| `campaign.has_steps` | BLOCKER | `campaign` | Campaign has at least one step |
| `campaign.schedule_windows` | BLOCKER | `campaign` | Campaign schedule has at least one sending window |
| `campaign.start_in_past` | MEDIUM | `campaign` | Campaign does not start in the past |
| `campaign.status_suitable` | MEDIUM | `campaign` | Campaign status is suitable for preflight |
| `campaign.stop_on_reply` | HIGH | `campaign` | Stop-on-reply is enabled |

## Contacts (15)

Contact-data quality. Most are ratio-driven: one bad row is noise, a quarter of the list is a broken import.

| Rule | Severity | Requires | Checks |
|---|---|---|---|
| `contacts.control_characters` | MEDIUM | `leads` | Contact fields are free of control and bidi characters |
| `contacts.duplicate_company_contact` | LOW | `leads` | No repeated person-at-company combinations |
| `contacts.duplicate_email` | HIGH | `leads` | No exact duplicate email addresses |
| `contacts.duplicate_normalized_email` | MEDIUM | `leads` | No duplicate addresses after normalization |
| `contacts.email_syntax` | HIGH | `leads` | Email addresses are syntactically valid |
| `contacts.field_length` | LOW | `leads` | Contact fields are not excessively long |
| `contacts.formula_injection` | MEDIUM | `leads` | Contact fields are free of spreadsheet formula injection |
| `contacts.free_email_domain` | LOW | `leads` | Contacts use business addresses |
| `contacts.invalid_region` | LOW | `leads` | Country and region values are usable |
| `contacts.missing_company_domain` | LOW | `leads` | Contacts have a company domain |
| `contacts.missing_company_name` | MEDIUM | `leads` | Contacts have a company name |
| `contacts.missing_first_name` | MEDIUM | `leads` | Contacts have a first name |
| `contacts.missing_job_title` | LOW | `leads` | Contacts have a job title |
| `contacts.placeholder_values` | MEDIUM | `leads` | No placeholder values in contact fields |
| `contacts.role_address` | MEDIUM | `leads` | Contacts are individuals, not shared inboxes |

## Suppression (8)

Who should not be contacted. Nothing here is a compliance check -- the domain and region lists encode **your organization's outreach policy**, which you configure. See [limitations.md](limitations.md).

| Rule | Severity | Requires | Checks |
|---|---|---|---|
| `suppression.capability_unavailable` | HIGH | — | Suppression data was available |
| `suppression.competitor_domain` | MEDIUM | `leads` | No contact is at a competitor domain |
| `suppression.contact_listed` | BLOCKER | `leads`, `suppressions` | No contact is on the suppression list |
| `suppression.domain_listed` | BLOCKER | `leads`, `suppressions` | No contact is on a suppressed domain |
| `suppression.duplicate_in_campaign` | MEDIUM | `leads` | No contact is already present in the campaign |
| `suppression.existing_customer` | HIGH | `leads` | No contact is at an existing-customer domain |
| `suppression.internal_domain` | BLOCKER | `leads` | No contact is at an internal domain |
| `suppression.restricted_region` | HIGH | `leads` | No contact is in a restricted region |

## Personalization (13)

Per-contact personalization, and the evidence behind any factual claim. Claim checking is conservative: with no evidence supplied it returns UNKNOWN rather than accusing your copy of fabrication.

| Rule | Severity | Requires | Checks |
|---|---|---|---|
| `personalization.claim_without_evidence` | MEDIUM | `leads`, `evidence` | Every claim has an evidence reference |
| `personalization.company_mismatch` | HIGH | `leads` | Personalization does not name a different company _(heuristic)_ |
| `personalization.duplicate_across_contacts` | MEDIUM | `leads` | Personalization is not identical across many contacts _(heuristic)_ |
| `personalization.empty` | MEDIUM | `leads` | Personalization values are populated |
| `personalization.evidence_lead_mismatch` | HIGH | `leads`, `evidence` | Evidence is attached to the right contact |
| `personalization.excessive_length` | LOW | `leads` | Personalization is not excessively long |
| `personalization.first_name_mismatch` | HIGH | `leads` | Personalization does not greet a different person _(heuristic)_ |
| `personalization.missing_required_variable` | HIGH | `leads` | Every contact has the required personalization variables |
| `personalization.prompt_injection` | HIGH | `leads` | Personalization contains no prompt-injection text |
| `personalization.sensitive_inference` | HIGH | `leads` | Personalization avoids sensitive inferences _(heuristic)_ |
| `personalization.stale_evidence` | MEDIUM | `leads`, `evidence` | Evidence is recent enough to cite |
| `personalization.unresolved_token` | BLOCKER | `leads` | Personalization contains no unrendered template tokens |
| `personalization.unsupported_claim` | HIGH | `leads`, `evidence` | Numeric claims are supported by the supplied evidence |

## Copy (13)

The campaign copy itself. Spam-word folklore is deliberately not implemented; what is here is either structural or an explicit heuristic.

| Rule | Severity | Requires | Checks |
|---|---|---|---|
| `copy.conflicting_variables` | MEDIUM | `campaign` | Copy does not mix conflicting company or contact variables |
| `copy.empty_body` | BLOCKER | `campaign` | Every step has a body |
| `copy.empty_subject` | BLOCKER | `campaign` | Every step has a subject line |
| `copy.excessive_length` | LOW | `campaign` | Copy length is within configured limits _(heuristic)_ |
| `copy.excessive_links` | LOW | `campaign` | Steps do not carry an excessive number of links _(heuristic)_ |
| `copy.generation_artifacts` | MEDIUM | `campaign` | Copy is free of obvious generation artifacts _(heuristic)_ |
| `copy.identical_steps` | MEDIUM | `campaign` | Steps are not byte-identical to one another |
| `copy.malformed_urls` | HIGH | `campaign` | Links in the copy are well-formed |
| `copy.missing_required_variables` | LOW | `campaign` | Copy uses the required personalization variables |
| `copy.opt_out_language` | HIGH | `campaign` | Copy contains configured opt-out language |
| `copy.placeholder_text` | BLOCKER | `campaign` | Copy contains no placeholder text |
| `copy.stop_condition` | BLOCKER | `campaign` | The sequence has a working stop condition |
| `copy.unresolved_tokens` | HIGH | `campaign`, `leads` | Copy references only variables your contacts have |

## Schedule (9)

When the campaign sends. Timezones are validated against the system IANA database; an unresolvable zone is UNKNOWN, never assumed valid.

| Rule | Severity | Requires | Checks |
|---|---|---|---|
| `schedule.dst_transition` | LOW | `campaign` | No daylight-saving transition inside the sending window |
| `schedule.invalid_timezone` | BLOCKER | `campaign` | Declared timezones are valid IANA zones |
| `schedule.missing_timezone` | HIGH | `campaign` | The schedule declares a timezone |
| `schedule.no_active_days` | BLOCKER | `campaign` | The schedule has at least one active sending day |
| `schedule.outside_business_hours` | MEDIUM | `campaign` | Sending windows fall within recipient-friendly hours |
| `schedule.start_after_end` | BLOCKER | `campaign` | The schedule start date is not after its end date |
| `schedule.timezone_mismatch` | MEDIUM | `campaign` | The campaign timezone matches the configured target |
| `schedule.weekend_sending` | MEDIUM | `campaign` | Weekend sending matches configured policy |
| `schedule.window_start_after_end` | HIGH | `campaign` | Sending windows start before they end |

## Senders (8)

Sender readiness. No deliverability number is ever invented: if the provider does not expose a health score, these rules say so.

| Rule | Severity | Requires | Checks |
|---|---|---|---|
| `senders.aggregate_capacity` | HIGH | `senders` | Campaign volume fits total sender capacity |
| `senders.all_unavailable` | BLOCKER | `senders` | At least one sender is usable |
| `senders.daily_capacity` | HIGH | `senders` | No single sender is asked to exceed its daily limit |
| `senders.disabled` | HIGH | `senders` | Attached senders are enabled |
| `senders.error_state` | HIGH | `senders` | No sender is in a provider error state |
| `senders.health_below_threshold` | HIGH | `senders`, `sender_health` | Sender health meets the configured threshold |
| `senders.health_unavailable` | MEDIUM | — | Sender health data was available |
| `senders.none_attached` | BLOCKER | `senders` | At least one sender is attached |

## Configuring a rule

Every rule accepts `enabled` and `severity`. Most accept thresholds of
their own. `rules explain` prints the exact options and their defaults:

```yaml
version: 1
rules:
  campaign.daily_volume:
    enabled: true
    warning_above: 100
    blocker_above: 250

  contacts.missing_first_name:
    enabled: true
    warning_ratio: 0.05
    blocker_ratio: 0.25

  senders.health_below_threshold:
    enabled: true
    minimum_score: 80
```

An unknown rule id or an unknown option is a hard configuration error, not
a warning. A typo in a safety config that silently does nothing is worse
than no config at all.

See [configuration.md](configuration.md) for the full schema.
