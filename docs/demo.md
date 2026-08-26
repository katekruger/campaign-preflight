# The demo

```bash
campaign-preflight demo
```

No API key. No network. No configuration. It runs in about 20 milliseconds and
prints a full readiness report for a bundled synthetic campaign.

## What is in it

The demo campaign is deliberately broken, in specific and instructive ways, so
one command shows you every kind of result the tool produces.

**Campaign configuration**
- Daily limit of 400, above the default blocker threshold of 250
- Stop-on-reply disabled — a three-step sequence that keeps emailing repliers
- Scheduled on `Europe/Berlin`, which will not match a US-target config
- A Saturday sending window
- A 07:00 start, before recipient-friendly hours

**Contacts** (20 synthetic leads)
- An exact duplicate and a case-only duplicate
- A malformed address (`dana.whitfield@@meridian..example.com`)
- A contact on the suppression list, and one at a suppressed domain
- A missing first name, a missing company, a role address (`info@`)
- A free-email-domain contact
- Placeholder values that survived an import (`TBD`, `Acme Inc`)
- A zero-width character and a spreadsheet formula in a company name

**Personalization**
- An unrendered `{{first_name}}` token that never merged
- A greeting addressed to the wrong person
- **Prompt-injection text scraped in from a target's own page**

**Copy**
- `TODO` markers left in two steps
- A broken link (`htp:/broken-link.example`)
- Two steps that are byte-identical copies of each other

**Evidence and claims**
- A claim whose number appears nowhere in its cited evidence
- A claim with no evidence attached at all
- Evidence older than the 180-day default
- Evidence with an empty excerpt
- Evidence joined to a contact that does not exist

**Senders**
- One healthy mailbox (score 94)
- One below threshold (score 41, warmup in an issue state)
- **One with no score and no daily limit at all** — which is why
  `senders.aggregate_capacity` comes back `UNKNOWN` rather than guessing

## The point of that last one

Most linters would sum the two senders that *do* report a limit and call it
capacity. Campaign Preflight will not, because a partial sum understates
capacity and could turn a real shortfall into a pass. It reports `UNKNOWN` and
names the sender it could not assess.

That distinction — between "we checked and it is fine" and "we could not check"
— is the reason this tool exists.

## All the data is synthetic

Every address uses an RFC 2606 reserved domain (`example.com`, `.invalid`). No
address belongs to a real person. The demo files carry comments explaining why
each defect is there, so they double as a worked example of the input format:

- [`src/campaign_preflight/demo/campaign.yaml`](../src/campaign_preflight/demo/campaign.yaml)
- [`src/campaign_preflight/demo/leads.csv`](../src/campaign_preflight/demo/leads.csv)
- [`src/campaign_preflight/demo/suppressions.csv`](../src/campaign_preflight/demo/suppressions.csv)
- [`src/campaign_preflight/demo/evidence.json`](../src/campaign_preflight/demo/evidence.json)

`tests/unit/test_demo_offline.py` asserts that no non-reserved domain and no
credential-shaped string can appear in any of them.

## Other formats

```bash
campaign-preflight demo --format markdown        # paste into a PR
campaign-preflight demo --format json | jq .     # machine-readable
campaign-preflight demo --verbose                # explanations and score derivation
campaign-preflight demo --quiet                  # the verdict line only
```

## A 90-second walkthrough

A script for a screen recording. Times are approximate.

**0:00 — the problem**

> Every outbound team has shipped a campaign with a mistake in it. A contact who
> unsubscribed still got emailed. A sequence kept following up after someone
> replied. A merge field never merged and two hundred people got "Hi
> {{first_name}}."
>
> You find out after it sends.

**0:15 — the tool**

```bash
campaign-preflight demo
```

> Campaign Preflight is a linter for outbound campaigns. It reads a campaign,
> its leads, its senders, and its suppression list, and tells you what would go
> wrong. It is read-only — it cannot activate anything.

**0:30 — read the output**

> Not ready. Eight blockers.
>
> One contact is on the suppression list. Stop-on-reply is off on a three-step
> sequence. There is prompt-injection text sitting in a personalization field —
> that came in from a scraped page and it is queued to go out under your domain.

**0:50 — the honest part**

> Scroll down. Sender capacity is `UNKNOWN` — one mailbox reports no daily
> limit, so the total cannot be summed. Most tools would add up the ones they
> have and call it a number. This one tells you it does not know.
>
> That is why confidence is `MEDIUM` and not `HIGH`.

**1:05 — your own files**

```bash
campaign-preflight check --campaign campaign.yaml --leads leads.csv --fail-on blocker
```

> Same checks, your files, no account needed. Exit code 2 on a blocker, so it
> drops straight into CI.

**1:20 — the agent**

> And it runs as a read-only MCP server. Point Claude at a live Instantly
> campaign and ask "is this safe to launch?" — it gets the analysis and no
> ability to launch it.

**1:30 — close**

> `pipx install campaign-preflight`, then `campaign-preflight demo`.
