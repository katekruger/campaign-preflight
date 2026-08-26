# Translating a report into plain language

The user is a GTM or ops person, not a CLI user. Give them the verdict, the
things that would actually hurt, and what to do — not a wall of 76 checks.

## Structure the answer this way

1. **The verdict, in one line.** "Not ready — two things would cause real
   problems if this sent today."
2. **Blockers, each in a sentence a human would say.** Lead with consequence,
   not rule id.
3. **Warnings, grouped and summarized.** Do not enumerate all of them if there
   are many.
4. **What could not be checked**, if anything. This is not optional — see below.
5. **The next action.** One or two concrete things to fix.

Mention rule ids only if the user asks, or if they will want to configure one.

## Translating findings

Say the consequence, not the mechanism.

| Finding | Say this |
|---|---|
| `campaign.stop_on_reply` fails | "Stop-on-reply is off. Anyone who replies will keep getting the follow-ups — that's the one most likely to cost you a deal." |
| `suppression.contact_listed` fails | "Three people on this list have unsubscribed. They'd be emailed again." |
| `personalization.unresolved_token` fails | "Four contacts would receive a literal `{{first_name}}` instead of their name." |
| `personalization.prompt_injection` fails | "One contact's research field contains text telling an AI to ignore its instructions. That came from a page the target controls, and it's queued to go out under your domain." |
| `copy.placeholder_text` fails | "There's still a TODO in step 2." |
| `senders.health_below_threshold` fails | "Neither sending mailbox is healthy enough — sending from these will hurt your domain." |
| `contacts.duplicate_email` fails | "Six people would get this twice." |
| `senders.aggregate_capacity` fails | "You're asking for 300/day but the attached mailboxes total 120/day." |
| `schedule.weekend_sending` warns | "It's set to send Saturdays." |
| `copy.opt_out_language` fails | "No opt-out language in any step." |

## Talking about UNKNOWN

This is where it is easiest to mislead someone, so be careful.

`UNKNOWN` means the check **could not run**. Never describe it as a pass, and
never let it vanish from the summary because it isn't a "finding".

**Do say:**
- "I couldn't check whether anyone here has unsubscribed — no suppression list
  was provided. That's the gap I'd close before sending."
- "Sender capacity is unverifiable: one mailbox didn't report a daily limit, so
  I can't total them up."
- "This is *incomplete* rather than clean — nothing looks wrong, but two things
  I'd want verified couldn't be."

**Do not say:**
- "No suppression problems found." ← implies a check ran
- "Everything passed." ← when some checks did not run
- "Looks good!" ← on an `INCOMPLETE` verdict

An `INCOMPLETE` verdict deserves the same prominence as `NOT_READY`. It is not a
soft pass.

## Heuristic findings

Some rules are judgement calls and are labelled `(heuristic)` in the output:
duplicate personalization, wrong-company detection, wrong-name detection,
sensitive-topic detection, copy length, link count, generation artifacts.

Present these as "worth a look", not as defects. They produce false positives by
design and the user's judgement beats the tool's.

## The score

The score is `100` minus weighted deductions and is fully derivable — `--verbose`
prints the arithmetic. Useful things to know:

- A blocker always means `NOT_READY`, whatever the score says. A campaign can
  score 70 and still be not-ready.
- `UNKNOWN` deducts **nothing** from the score. It lowers *confidence* instead,
  so a provider outage does not look like a bad campaign.
- Report the score alongside the confidence level, never alone. "68/100 with LOW
  confidence" means something quite different from "68/100 with HIGH".

## Limits to state when relevant

Do not oversell the result. If the user seems to be treating a `READY` as a
guarantee, say plainly:

- It does not guarantee deliverability. It checks configuration and data, not
  inbox placement, and never invents a deliverability score.
- It does not give legal advice. Region, domain, and opt-out checks compare the
  campaign against **the user's own configured policy**, not against GDPR,
  CAN-SPAM, CASL, or anything else.
- It is a point-in-time snapshot. The campaign can change a minute later.
- It never activates anything, so it cannot enforce a fix.
