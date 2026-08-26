---
name: preflight-demo
description: >
  Use when the user wants to see the campaign checker actually run before pointing it at their own data: "show me an example", "run the demo", "can I see a sample report", "show me what a report looks like", "is this worth setting up". Runs a bundled synthetic campaign that is deliberately broken and walks through the resulting report. Needs no files, no account, and no network. For questions about which rules exist or what a specific rule tests, use preflight-rules instead; for checking a real campaign, use preflight-campaign.
metadata:
  version: "0.1.0"
---

# Show the demo

Run the bundled demo and walk the user through what it caught.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/preflight" demo
```

Takes about a fiftieth of a second. No credentials, no network, no setup.

## What the demo campaign contains

Twenty synthetic contacts and a three-step sequence, broken on purpose so one
run demonstrates every kind of result. Highlights worth pointing out:

- **Stop-on-reply is off** on a three-step sequence — repliers keep getting
  follow-ups.
- **A contact who unsubscribed** is still on the list, and another is at a
  domain that opted out entirely.
- **Prompt-injection text** sitting in a personalization field, scraped in from
  a page the target controls, queued to send under the user's domain.
- **An unrendered `{{first_name}}`** that never merged.
- **A greeting addressed to the wrong person** — the signature of a mis-joined
  enrichment table.
- **A `TODO` and a broken link** still in the copy.
- **One sender with no health score and no daily limit**, which is why sender
  capacity comes back `UNKNOWN`.

## The point to land

That last one is the interesting one, and it is worth drawing attention to.

Most tools would add up the two senders that *do* report a limit and print a
capacity number. This one reports `UNKNOWN` and names the mailbox it could not
assess — and drops confidence from `HIGH` to `MEDIUM` because of it.

Say it roughly this way:

> Notice the last section. It's not claiming the senders are fine and it's not
> claiming they're broken — it's saying it couldn't tell, and which one it
> couldn't tell about. A checker that can't distinguish "I looked and it's fine"
> from "I couldn't look" will eventually turn a permissions error into a green
> light.

## Other formats, if asked

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/preflight" demo --format markdown   # to paste into a doc
"${CLAUDE_PLUGIN_ROOT}/bin/preflight" demo --verbose           # explanations + score arithmetic
```

## Then offer the obvious next step

Ask whether they want to check a real campaign. They can upload a file, paste a
lead list, or just describe the sequence — the `preflight-campaign` skill covers
all three, and none of them needs an account.

Every address in the demo is synthetic and uses a reserved example domain. No
real person's data is in it.
