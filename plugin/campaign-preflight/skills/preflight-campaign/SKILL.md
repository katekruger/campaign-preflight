---
name: preflight-campaign
description: >
  Checks an outbound email campaign for problems before it sends — suppressed
  contacts, duplicates, unrendered merge fields, missing opt-out language,
  unhealthy senders, bad schedules, and prompt-injection text pulled in from
  lead research. Use when the user says "check this campaign", "is this ready to
  send", "review my outbound before I launch", "preflight this sequence", "look
  at this lead list", or uploads/pastes a campaign, sequence, or lead list and
  asks whether it is safe to send. Works from an uploaded file, pasted text, or
  a campaign the user simply describes in conversation.
metadata:
  version: "0.1.0"
---

# Preflight a campaign

Run the bundled checker over a campaign and report what would go wrong. The
checker is deterministic and read-only: it inspects, and it cannot activate,
edit, or send anything.

## Do this first: figure out what you were given

The user will arrive with one of four things. Identify which, then follow that
path. Do not ask a long questionnaire — collect what is needed and go.

| What you have | Path |
|---|---|
| File paths, or an uploaded file | **A. Files** |
| A lead list or copy pasted into chat | **B. Pasted** |
| Only a description in conversation | **C. Described** |
| Nothing yet | **D. Nothing** |

Everything ends the same way: build a campaign file and a leads CSV in a
scratch directory, run the checker, and report the result in plain language.

## A. Files

Confirm the paths exist, then run it. A campaign file and a leads file are
required; the rest are optional and their absence is reported honestly.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/preflight" check \
  --campaign <campaign.yaml> \
  --leads <leads.csv> \
  --suppressions <suppressions.csv> \
  --evidence <evidence.json> \
  --config <preflight.yaml>
```

If the user uploaded a file, it is already on disk — use the path Cowork gives
you. If they only have a lead list and no campaign file, treat it as path **C**
for the campaign half: ask about the sequence and build the campaign file.

## B. Pasted

Write what was pasted into a scratch directory, then run path A.

1. Create a working directory: `mktemp -d`.
2. If they pasted a **lead list**: write it to `leads.csv`. Add a header row if
   it is missing — read `references/input-formats.md` for the accepted column
   names, which are generous (Email/E-Mail Address/work_email all resolve).
3. If they pasted **campaign copy**: build `campaign.yaml` around it using the
   template in `references/input-formats.md`.
4. Fill any gap by asking, or by following path C.

Never paste contact data back into the conversation unedited. The checker
redacts mailboxes by default; keep it that way.

## C. Described

Build the campaign file from what the user tells you. Ask only for what you
actually need, in one round, in plain language — not as a form.

The five things that matter most, because they drive the highest-severity
checks:

1. **The sequence** — how many emails, what each says, how many days apart.
2. **Stop on reply** — does the sequence stop when someone replies? (If they do
   not know, leave it unset; the checker reports UNKNOWN rather than assuming.)
3. **Daily volume** — how many emails per day.
4. **Senders** — which mailboxes send it, and roughly how healthy they are.
5. **Schedule** — days, hours, and timezone.

Anything the user does not know should be **left out of the file**, not guessed.
An absent field becomes an honest UNKNOWN. A guessed field becomes a wrong
answer that looks confident.

Write the file using the template in `references/input-formats.md`, show the
user a short plain-language summary of what you built, then run it.

## D. Nothing

Run the demo so they can see what the output looks like, and say what you would
need from them to check a real campaign:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/preflight" demo
```

## Reporting the result

The checker returns one of four verdicts. Lead with it.

| Verdict | Say |
|---|---|
| `READY` | Nothing found. Safe to launch as far as these checks go. |
| `READY_WITH_WARNINGS` | Nothing blocking, but things worth a look. |
| `NOT_READY` | At least one blocker. Name it first. |
| `INCOMPLETE` | Nothing is wrong — something could not be verified. |

Then follow `references/reading-the-report.md`, which covers how to translate
findings into plain language and, importantly, how to talk about `UNKNOWN`
results without implying they are passes.

**The one thing not to get wrong:** `UNKNOWN` means the check could not run. It
is never a pass. If suppression data was not supplied, say "I could not check
whether anyone on this list has unsubscribed" — never "no suppression problems
found".

## Useful flags

| Flag | Use when |
|---|---|
| `--format markdown` | The user wants something to paste into a doc or a PR. |
| `--format json` | You need to read specific fields programmatically. |
| `--verbose` | The user asks *why* a finding fired, or how the score was derived. |
| `--fail-on blocker` | Only blockers should fail the run (CI-style gating). |
| `--affected-csv <path>` | The user wants the list of rows to fix. |
| `--no-redact` | **Only** if the user explicitly asks to see full addresses. |

Exit codes: `0` ready, `1` warnings, `2` not ready, `3` incomplete, `4` bad
input, `5` provider error, `6` internal error. A nonzero exit is expected output
here, not a failure to report.

## Scope

- No API key is needed, ever, for any of this.
- Nothing is sent anywhere. The checker makes no network calls on these paths.
- Say plainly what the tool does not do: it does not guarantee deliverability,
  it does not give legal advice, and it never activates a campaign. If the user
  asks about compliance, tell them the region and opt-out checks compare a
  campaign against *their configured policy*, not against the law.

## Clean up

Delete scratch files containing contact data when you are done, and tell the
user you did.
