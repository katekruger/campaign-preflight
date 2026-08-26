"""Redaction applied to every rendered report.

Redaction is on by default and is applied at render time, not at collection
time, so ``--no-redact`` does not require re-running the check.

What is masked:

* Email local parts (``a***y@northwind.example.com``). Domains survive, because
  a domain is what makes a suppression or free-domain finding actionable, and a
  domain is not personal data in the way a mailbox is.
* Anything matching a credential pattern, always -- ``--no-redact`` does not
  turn this off. There is no legitimate reason for an API key to appear in a
  report.
"""

from __future__ import annotations

import re

from ..errors import redact_secrets

__all__ = ["redact_email", "redact_text", "redact_samples"]

_EMAIL_IN_TEXT = re.compile(r"(?<![\w.+*-])([\w.+-]{1,64})@([\w.-]{1,255}\.[A-Za-z]{2,})")


def _mask_local(local: str) -> str:
    """Keep the first and last character, mask the middle.

    Already-masked values are returned unchanged, so rendering a report twice --
    or rendering a summary that quotes an already-rendered line -- does not eat
    another character each pass.
    """
    if "*" in local:
        return local
    if len(local) <= 2:
        return "*" * len(local)
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}"


def redact_email(value: str) -> str:
    """Mask the local part of an address, keeping the domain readable."""
    match = _EMAIL_IN_TEXT.fullmatch(value.strip())
    if not match:
        return value
    return f"{_mask_local(match.group(1))}@{match.group(2)}"


# A malformed address ("a@@b..c", "00;@x.y") will not match the well-formed
# pattern above, and a malformed address is exactly what a syntax finding
# reports. This fallback masks everything up to the last "@" in any token
# containing one. The local-part class is deliberately permissive -- it errs
# toward masking a character too many rather than leaking a mailbox -- and
# _mask_local's already-masked guard keeps the two passes idempotent.
_ADDRESS_LIKE = re.compile(r"""(?<![\w.+*-])([^\s@<>"']{1,64})(@+)([^\s,;<>"']{1,255})""")


def _mask_match(match: re.Match[str]) -> str:
    return f"{_mask_local(match.group(1))}{match.group(2)}{match.group(3)}"


def redact_text(value: str, *, redacted: bool = True) -> str:
    """Mask addresses inside free text. Credentials are masked unconditionally."""
    text = redact_secrets(value)
    if not redacted:
        return text
    text = _EMAIL_IN_TEXT.sub(
        lambda m: f"{_mask_local(m.group(1))}@{m.group(2)}", text
    )
    return _ADDRESS_LIKE.sub(_mask_match, text)


def redact_samples(
    samples: tuple[str, ...] | list[str], *, redacted: bool = True, limit: int | None = None
) -> tuple[str, ...]:
    """Redact and bound a list of affected-record labels."""
    values = [redact_text(s, redacted=redacted) for s in samples]
    if limit is not None and limit >= 0:
        values = values[:limit]
    return tuple(values)
