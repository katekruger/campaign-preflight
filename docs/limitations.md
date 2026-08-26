# Limitations

What Campaign Preflight does not do, cannot do, and does not claim to do. Read
this before you rely on a `READY`.

## It does not guarantee deliverability

Campaign Preflight checks configuration and data. Whether your email reaches an
inbox depends on domain reputation, SPF/DKIM/DMARC alignment, content history,
recipient-side filtering, and the mood of a mailbox provider's classifier — none
of which this tool measures or can measure.

A `READY` verdict means "no configured check found a problem." It does not mean
"this will land."

Where a provider exposes its own health score, that score is reported as-is. No
deliverability number is ever derived, estimated, or invented. If a provider has
no score, the sender rules return `UNKNOWN`.

## It does not provide legal advice

Campaign Preflight does not determine whether your outreach is lawful under
GDPR, CAN-SPAM, CASL, PECR, or any other regime. It is not a compliance product
and its output is not a compliance artifact.

Several rules read lists you configure — `restricted_regions`,
`internal_domains`, `competitor_domains`, `customer_domains`, `opt_out_phrases`.
These encode **your organization's outreach policy**, decided by you. The tool
checks your campaign against your policy. It has no opinion on whether your
policy is sufficient.

`copy.opt_out_language` checks for phrases you listed. It does not assess
whether your opt-out mechanism is legally adequate.

## It does not replace provider-native safeguards

Your sending platform has its own suppression handling, bounce protection,
warmup logic, and rate limiting. Campaign Preflight reads some of that state; it
does not duplicate or supersede it. Keep those safeguards on.

## Results depend on what the provider will tell you

Every check declares the data it needs. When that data is unavailable — no file
supplied, an endpoint the API key cannot reach, a provider that does not expose
the field — the check returns `UNKNOWN`.

`UNKNOWN` is **not** a pass. A campaign with unknown critical checks is
`INCOMPLETE`, and the report says which capability was missing and why. This is
the single most important thing to understand about the output:

| Situation | Result |
|---|---|
| Suppression list read, nobody matched | `PASS` |
| No suppression list supplied | `UNKNOWN` + run is `INCOMPLETE` |
| Suppression endpoint returned 403 | `UNKNOWN` + run is `INCOMPLETE` |
| Zero leads in the campaign | `FAIL` |
| Lead endpoint unreachable | `UNKNOWN` |

A tool that collapsed these into a single "looks fine" would be worse than no
tool at all.

## Results are a point-in-time snapshot

Every report carries `generated_at` and a snapshot note. The campaign can be
edited the moment after the check completes: a lead imported, a sender paused, a
suppression added. Campaign Preflight is read-only, so it cannot lock anything
in place.

Run it as close to activation as you can, and treat the report as evidence of
what was true at that timestamp.

## Heuristic rules are judgement calls

Some rules encode a heuristic rather than a fact. They are marked `heuristic` in
the rule catalogue, labelled in every rendered report, and are never `BLOCKER`
severity by default:

- `personalization.duplicate_across_contacts` — some campaigns legitimately
  reuse a line
- `personalization.company_mismatch` and `first_name_mismatch` — token matching,
  which will produce false positives
- `personalization.sensitive_inference` — term matching over a small list
- `copy.excessive_length`, `copy.excessive_links`,
  `copy.generation_artifacts` — style signals, not measurements

Treat these as a prompt to read the copy, not as a verdict about it.

### Spam-word folklore is deliberately absent

There is no rule that flags "free", "act now", or "limited time". Those lists
are not evidence of anything, and implementing them would train you to ignore
the tool.

## Claim checking is deliberately narrow

`personalization.unsupported_claim` compares numbers in a claim against the text
of its cited evidence. That is a string check, not a judgement about truth:

- A claim can be **true** and still fail, if the evidence excerpt is too thin to
  contain the figure.
- A claim can be **false** and still pass, if the wrong number happens to appear
  in the evidence.

With no evidence supplied at all, the rule returns `UNKNOWN`. It will not accuse
your copy of fabrication on no information.

## Optional LLM assessment is probabilistic

An optional evaluator interface exists for semantic entailment. It is
**disabled by default** and no lead data leaves your machine unless you
explicitly configure it.

When enabled, any result is labelled `MODEL_ASSESSED`, never `FACT`, and is
recorded with the provider, model, prompt version, timestamp, score, and
explanation. Full lead datasets are never sent; the number of claims that can
leave the machine is capped by `evidence.max_claims_evaluated`.

## Email validation is syntax only

`contacts.email_syntax` checks that an address is well-formed. It performs no
DNS lookup, no MX check, no SMTP probe, and no mailbox verification. A
syntactically valid address can still bounce. Catch-all status is not
determinable and is never claimed.

## Duplicate detection is conservative

Duplicates are matched on the normalized address (lowercased, Unicode-folded).
Gmail-style dot and plus-tag folding is **not** applied, because it is
provider-specific and would silently merge addresses you may consider distinct.
`ana+q3@corp.example.com` and `ana@corp.example.com` are two contacts here.

## Scale bounds

| Limit | Value | Why |
|---|---|---|
| Leads CSV | 256 MB / 1,000,000 rows | Beyond this you want a database, not a linter |
| Campaign file | 8 MB | |
| Config file | 2 MB | |
| Single CSV field | 1 MB | |
| Instantly pagination | 100 pages (10,000 records) per endpoint | Bounded by design |
| Instantly senders inspected | 200 | Reported when exceeded |
| Affected-record samples | `settings.max_samples`, default 5, max 100 | A 100k campaign must not emit 100k lines |

Exceeding a bound is always reported, never silent.

## What it will never do

- Activate, pause, resume, or schedule a campaign
- Create, update, move, merge, or delete a lead
- Add to or remove from a suppression list
- Send, reply to, or forward an email
- Modify anything in your sending platform

The Instantly provider enforces this at the transport layer with an explicit
read-only allowlist. The MCP server refuses to start if a tool with a mutating
name is registered. Both are covered by tests that treat a failure as a security
incident.

## Reporting a gap

If a check produced a wrong result — or, worse, a `PASS` where the data was
missing — that is a bug worth filing. Open an issue with the rule id and a
minimal reproduction.
