"""Exception hierarchy and process exit codes for Campaign Preflight.

Every exception here is safe to print: none of them ever carry an API key or a
raw provider response. :func:`redact_secrets` is applied to any message that may
have passed through provider code, so an accidental interpolation of a token
still cannot reach a terminal, a log line, or a report file.
"""

from __future__ import annotations

import re
from enum import IntEnum

__all__ = [
    "ConfigurationError",
    "ExitCode",
    "InputError",
    "InternalError",
    "PreflightError",
    "ProviderAuthError",
    "ProviderCapabilityError",
    "ProviderError",
    "redact_secrets",
]


class ExitCode(IntEnum):
    """Documented process exit codes. See ``docs/ci.md``."""

    READY = 0
    READY_WITH_WARNINGS = 1
    NOT_READY = 2
    INCOMPLETE = 3
    CONFIG_ERROR = 4
    PROVIDER_ERROR = 5
    INTERNAL_ERROR = 6


# Patterns that look like credentials. Deliberately broad: a false positive
# costs a few masked characters, a false negative leaks a key.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Authorization: Bearer <token>" in any casing, with or without the header.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-=+/]{8,}"),
    re.compile(r"(?i)\bauthorization\b\s*[:=]\s*\S+"),
    # Key-ish assignments, including prefixed names such as INSTANTLY_API_KEY.
    # The leading [A-Za-z0-9_]* absorbs any prefix; backtracking lets the
    # credential word itself match at the end of the identifier.
    re.compile(
        r"(?i)\b[A-Za-z0-9_]*"
        r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|password)\b"
        r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._\-=+/]{4,}"
    ),
    # Instantly v2 keys are long base64url strings ending in "==". No trailing
    # \b: the character after "=" is often a quote, and two non-word characters
    # form no boundary.
    re.compile(r"(?<![A-Za-z0-9\-_])[A-Za-z0-9\-_]{24,}=="),
)

REDACTION = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Mask anything that looks like a credential in ``text``.

    Applied to every provider-facing error message and to structured log output.
    """
    if not text:
        return text
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTION, out)
    return out


class PreflightError(Exception):
    """Base class for every error Campaign Preflight raises deliberately."""

    exit_code: ExitCode = ExitCode.INTERNAL_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.raw_message = redact_secrets(message)
        self.hint = redact_secrets(hint) if hint else None
        super().__init__(self.raw_message if not hint else f"{self.raw_message} ({self.hint})")

    def __str__(self) -> str:  # pragma: no cover - trivial
        return redact_secrets(super().__str__())


class ConfigurationError(PreflightError):
    """The rule configuration file is invalid, unreadable, or unsupported."""

    exit_code = ExitCode.CONFIG_ERROR


class InputError(PreflightError):
    """A user-supplied file is missing, malformed, or too large to process."""

    exit_code = ExitCode.CONFIG_ERROR


class ProviderError(PreflightError):
    """A provider could not be reached or returned something unusable."""

    exit_code = ExitCode.PROVIDER_ERROR

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        status: int | None = None,
        endpoint: str | None = None,
    ) -> None:
        self.status = status
        self.endpoint = endpoint
        super().__init__(message, hint=hint)


class ProviderAuthError(ProviderError):
    """Credentials are missing, invalid, expired, or lack the required scope."""


class ProviderCapabilityError(ProviderError):
    """The provider cannot supply a capability this run needed.

    Raised only when a capability was explicitly requested and hard-required.
    Ordinary missing capabilities become ``UNKNOWN`` rule results instead.
    """


class InternalError(PreflightError):
    """An unexpected failure inside Campaign Preflight itself."""

    exit_code = ExitCode.INTERNAL_ERROR
