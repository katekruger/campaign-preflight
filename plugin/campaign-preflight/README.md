# Campaign Preflight — Cowork plugin

Catches problems in an outbound email campaign **before it sends**.

Ask Claude to check a campaign and it will look for suppressed contacts,
duplicates, unrendered merge fields, missing opt-out language, unhealthy
senders, bad schedules, and prompt-injection text pulled in from lead research —
then tell you in plain language what would go wrong.

**No account. No API key. No setup.**

## What you can say

> Check this campaign before I send it.

> Here's my lead list — anything wrong with it?  *(paste or upload)*

> I'm sending a 3-email sequence to 200 people, 80 a day, weekdays 9-5 Eastern.
> Is that okay?

> What does this actually check?

> Show me an example.

## The three ways in

| You have | What happens |
|---|---|
| **A file** (uploaded, or on disk) | Checked directly. |
| **A pasted list or some copy** | Written to a scratch file, checked, then cleaned up. |
| **Only a description** | Claude builds the campaign file from what you say, shows you what it built, and checks that. |

Anything you don't know is left blank rather than guessed — a blank field comes
back as "couldn't check", which is the honest answer.

## The four verdicts

| Verdict | Meaning |
|---|---|
| **READY** | Nothing found. |
| **READY WITH WARNINGS** | Nothing blocking, but things worth a look. |
| **NOT READY** | At least one thing would cause real damage. |
| **INCOMPLETE** | Nothing looks wrong, but something couldn't be verified. |

That last one is the point of the tool. A checker that can't tell *"I looked and
it's fine"* from *"I couldn't look"* will eventually turn a permissions error
into a green light. This one keeps them separate, everywhere.

So if you don't hand it a suppression list, it won't say "no suppression
problems found." It'll say it couldn't check whether anyone has unsubscribed —
and mark the whole run incomplete.

## Read-only, structurally

The plugin can inspect a campaign. It cannot activate, edit, pause, import, or
send one.

That isn't a promise in a docstring:

- The MCP server **refuses to start** if any tool has a mutating verb in its
  name or fails to declare itself read-only.
- Every tool is annotated `readOnlyHint: true`, `destructiveHint: false`.
- If you ever connect it to a live sending platform, every outbound request is
  matched against an explicit allowlist and blocked *before it leaves the
  process*.

## What it checks

76 deterministic rules across seven areas:

| Area | Examples |
|---|---|
| **Campaign** | Stop-on-reply off, volume too high, no sending window |
| **Contacts** | Malformed addresses, duplicates, role inboxes, placeholder values |
| **Suppression** | Unsubscribed people and domains, existing customers, competitors |
| **Personalization** | Unrendered `{{first_name}}`, wrong name or company, prompt injection |
| **Copy** | Empty subject, broken links, leftover TODOs, missing opt-out |
| **Schedule** | Invalid timezone, weekend sending, zero active days |
| **Senders** | Unhealthy mailboxes, error states, volume over capacity |

Ask "what does it check?" for the full catalogue, or "why did it flag that?" for
any single finding.

There is deliberately **no spam-word list**. "Free" and "act now" aren't
evidence of anything, and flagging them would just train you to ignore the tool.

## What it doesn't do

- **Doesn't guarantee deliverability.** It checks configuration and data, not
  inbox placement, and never invents a deliverability score.
- **Doesn't give legal advice.** Region and opt-out checks compare your campaign
  against *your own configured policy* — not GDPR, CAN-SPAM, or CASL.
- **Doesn't verify mailboxes.** Address checks are syntax only. No DNS, no SMTP.
- **Doesn't replace your sending platform's safeguards.** Keep those on.
- **Doesn't send anything anywhere.** Your data stays on your machine.

Results are a point-in-time snapshot. A campaign that passed at 09:00 can be
edited at 09:05.

## Privacy

Contact addresses are masked in output by default
(`m**********s@stonebridge.example.com`) — domains are kept, because a domain is
what makes a finding actionable. Scratch files are deleted after a check. Nothing
is transmitted anywhere.

## Requirements

Python 3.9 or newer — already present on macOS and Linux. Nothing to install:
the engine has zero third-party dependencies and ships inside the plugin.

## Optional: a live Instantly campaign

Not required, and everything above works without it. If you do want to check a
live campaign, set `INSTANTLY_API_KEY` in the plugin's environment and install
the one optional dependency (`httpx`). The key is read from the environment
only — it can never be passed as a tool argument.

## Source

<https://github.com/katekruger/campaignpreflightplugin> — MIT licensed, 1,400+ tests.
