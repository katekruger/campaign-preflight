## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The problem being solved. Link an issue if there is one. -->

## What could this break

<!--
The section reviewers actually read. Be specific and be honest.

Worth calling out:
- Does it change the result of an existing rule on input that used to pass?
- Does it change the score, a severity, or a readiness verdict?
- Does it change the JSON report shape? (If so, the schema version needs a bump.)
- Does it add anything to the read-only allowlist or the MCP tool surface?
- Does it add a runtime dependency? (It must not — see below.)

"Nothing" is a fine answer when it is true.
-->

## Checks

- [ ] `uv run ruff format .` and `uv run ruff check .` pass
- [ ] `uv run mypy` passes
- [ ] `uv run pytest` passes
- [ ] Tests cover the new behaviour, **including the case where required data is missing**
- [ ] `uv run python scripts/generate_rules_doc.py` re-run, if a rule changed
- [ ] `uv run python scripts/build_plugin.py` re-run, if `src/` changed
- [ ] Docs updated, if behaviour visible to a user changed

## Invariants

These are load-bearing. Tick each one you did not break, or explain below.

- [ ] **No runtime dependency was added.** The package must keep running on a
      bare Python 3.9 with nothing installed — that is what makes the Cowork
      plugin work.
- [ ] **Missing data still returns `UNKNOWN`, never `PASS`.** A rule that
      reports success on absent data defeats the purpose of the tool.
- [ ] **Nothing new can write to a provider.** No addition to the read-only
      allowlist, no MCP tool that mutates external state.
- [ ] **No credential can reach output.** Errors, logs, and reports stay
      scrubbed regardless of `--redact`.
- [ ] **No real contact data** was added to fixtures. Reserved example domains
      only.

## Notes for the reviewer

<!-- Anything you are unsure about, or would like a second opinion on. -->
