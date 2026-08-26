"""Deterministic normalization helpers shared by every provider and rule.

Two goals:

1. Given the same input bytes, produce the same normalized values every time --
   snapshot tests and diffable reports depend on it.
2. Never destroy information. Normalization returns *additional* derived fields
   (``normalized_email``) and leaves the original alongside, so a rule can report
   what the user actually wrote.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import time
from typing import Any, Final

__all__ = [
    "normalize_email",
    "email_is_syntactically_valid",
    "domain_of",
    "normalize_domain",
    "normalize_text",
    "strip_control_characters",
    "has_control_characters",
    "is_formula_injection",
    "neutralize_formula",
    "hash_ref",
    "find_template_tokens",
    "find_unresolved_tokens",
    "extract_urls",
    "malformed_urls",
    "parse_clock_time",
    "canonical_header",
    "coerce_bool",
    "coerce_int",
    "collapse_whitespace",
    "FREE_EMAIL_DOMAINS",
    "ROLE_LOCAL_PARTS",
    "PLACEHOLDER_VALUES",
]

BOM: Final = "﻿"

# ---------------------------------------------------------------------------
# Email and domain
# ---------------------------------------------------------------------------

# Deliberately pragmatic rather than RFC 5322-complete: one '@', a non-empty
# local part with no spaces or control characters, and a dotted domain whose
# labels are non-empty and do not start or end with a hyphen. Unicode letters
# are allowed so internationalized addresses are not falsely rejected.
_EMAIL_RE: Final = re.compile(
    r"^(?P<local>[^\s@,;<>\"'\\\[\]()]{1,64})@(?P<domain>[^\s@,;<>\"'\\\[\]()]{1,255})$"
)
_DOMAIN_LABEL_RE: Final = re.compile(r"^[^\W_](?:[\w-]*[^\W_])?$", re.UNICODE)


def normalize_email(value: str | None) -> str | None:
    """Lowercase, NFKC-fold, and trim an address for comparison purposes.

    Used for duplicate detection and suppression matching. The original string is
    always preserved on the model; this is the comparison key only. Gmail dot and
    plus-tag folding is deliberately NOT applied -- it is provider-specific and
    would silently merge addresses the user considers distinct.
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip().strip("<>").strip()
    if not text:
        return None
    return text.lower()


def email_is_syntactically_valid(value: str | None) -> bool:
    """True when ``value`` is plausibly a deliverable address.

    Syntax only. This makes no claim about whether the mailbox exists --
    Campaign Preflight never verifies addresses over the network.
    """
    if not value:
        return False
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if has_control_characters(text):
        return False
    match = _EMAIL_RE.match(text)
    if not match:
        return False
    domain = match.group("domain")
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return False
    labels = domain.split(".")
    if len(labels) < 2 or not labels[-1] or len(labels[-1]) < 2:
        return False
    return all(_DOMAIN_LABEL_RE.match(label) for label in labels)


def domain_of(email: str | None) -> str | None:
    """The domain half of an address, normalized, or ``None``."""
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        return None
    domain = normalized.rsplit("@", 1)[1]
    return domain or None


def normalize_domain(value: str | None) -> str | None:
    """Reduce a URL, host, or bare domain to a comparable registrable host."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    if not text:
        return None
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@")[-1]
    text = text.split(":", 1)[0]
    # Strip whitespace again: splitting on a delimiter can expose trailing
    # spaces that were interior a moment ago, and leaving them would make this
    # function non-idempotent.
    text = text.strip().strip(".").strip()
    if text.startswith("www."):
        text = text[4:]
    return text or None


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------

# C0/C1 controls except tab, newline, carriage return; plus the Unicode
# zero-width and bidirectional-override characters used to disguise text.
_CONTROL_RE: Final = re.compile(
    "[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f-\\x9f"
    "\\u200b-\\u200f\\u2028\\u2029\\u202a-\\u202e\\u2066-\\u2069\\ufeff]"
)
_WS_RE: Final = re.compile(r"\s+")


def has_control_characters(value: str | None) -> bool:
    """True when ``value`` contains control, zero-width, or bidi characters."""
    return bool(value) and bool(_CONTROL_RE.search(str(value)))


def strip_control_characters(value: str) -> str:
    return _CONTROL_RE.sub("", value)


def collapse_whitespace(value: str) -> str:
    return _WS_RE.sub(" ", value).strip()


def normalize_text(value: Any) -> str | None:
    """Trim, NFKC-fold, and drop empty strings. BOM and CRLF safe."""
    if value is None:
        return None
    text = str(value).replace(BOM, "").replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFKC", text).strip()
    return text or None


# Leading characters that make a spreadsheet treat a cell as a formula.
_FORMULA_PREFIXES: Final = ("=", "+", "-", "@", "\t=", "\r=")


def is_formula_injection(value: str | None) -> bool:
    """True when a value would be evaluated as a formula by Excel/Sheets.

    A bare negative number (``-5``) is not flagged; a leading ``-`` followed by a
    letter or an opening paren is.
    """
    if not value:
        return False
    text = str(value).lstrip(" \t\r\n ")
    if not text:
        return False
    if not text.startswith(_FORMULA_PREFIXES):
        return False
    if text[0] in "+-" and len(text) > 1 and (text[1].isdigit() or text[1] in ".,"):
        return False  # plain signed number
    return True


def neutralize_formula(value: str) -> str:
    """Prefix a formula-like cell so spreadsheets treat it as literal text."""
    return f"'{value}" if is_formula_injection(value) else value


def hash_ref(value: str) -> str:
    """A stable, non-reversible reference for an email or id.

    Used to correlate evidence with leads without putting addresses in files
    that may be shared. Truncated to 16 hex characters: enough to avoid
    collisions at campaign scale, short enough to read.
    """
    normalized = normalize_email(value) or str(value).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Template tokens
# ---------------------------------------------------------------------------

# The three syntaxes seen across outbound tools. Escaped forms (``\{{x}}`` and
# doubled ``{{{{x}}}}``) are excluded so an intentionally literal brace is not
# reported as an unresolved token.
_TOKEN_PATTERNS: Final = (
    re.compile(r"(?<!\\)\{\{\s*([^{}]+?)\s*\}\}"),
    re.compile(r"(?<!\\)\{%\s*([^{}%]+?)\s*%\}"),
    re.compile(r"(?<!\\)\$\{\s*([^{}]+?)\s*\}"),
)
_ESCAPED_TOKEN_RE: Final = re.compile(r"\\\{\{|\{\{\{\{")


def find_template_tokens(text: str | None) -> list[str]:
    """Every template variable name referenced in ``text``, in order of first use."""
    if not text:
        return []
    cleaned = _ESCAPED_TOKEN_RE.sub(" ", str(text))
    found: list[str] = []
    for pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(cleaned):
            name = match.group(1).strip()
            # Strip fallback/filter syntax: {{first_name | there}} -> first_name
            name = re.split(r"[|]", name, maxsplit=1)[0].strip()
            if name and name not in found:
                found.append(name)
    return found


def find_unresolved_tokens(text: str | None) -> list[str]:
    """Tokens present in text that was supposed to already be rendered.

    Applied to *lead* personalization, where a remaining ``{{...}}`` means the
    merge never happened.
    """
    return find_template_tokens(text)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

# Matches proper URLs, typo'd schemes ("htp:/x", "htps://x"), and bare hosts
# ("www.x"). A one-letter scheme is excluded so a Windows path like C:/Users is
# not mistaken for a broken link.
_URL_RE: Final = re.compile(
    r"""(?ix)
    \b(
        [a-z][a-z0-9+.\-]{1,20} : /{1,3} [^\s<>"']*
      | www\. [^\s<>"']+
    )"""
)
_HREF_RE: Final = re.compile(r"""(?i)<a\b[^>]*\bhref\s*=\s*["']([^"']*)["']""")
_VALID_URL_RE: Final = re.compile(
    r"""(?ix)
    ^(?:https?://)
    (?:[^\s/?\#@:]+(?::[^\s/?\#@]*)?@)?
    (?P<host>[^\s/?\#:]+)
    (?::\d{1,5})?
    (?:[/?\#]\S*)?$
    """
)


def extract_urls(text: str | None) -> list[str]:
    """All links in ``text``, from both raw URLs and HTML ``href`` attributes."""
    if not text:
        return []
    urls: list[str] = []
    for match in _HREF_RE.finditer(str(text)):
        value = match.group(1).strip()
        if value and not value.lower().startswith(("mailto:", "tel:", "#")):
            urls.append(value)
    stripped = _HREF_RE.sub(" ", str(text))
    for match in _URL_RE.finditer(stripped):
        urls.append(match.group(1).rstrip(".,);:\"'>"))
    return urls


def malformed_urls(text: str | None) -> list[str]:
    """Links that are not usable as-is.

    Template tokens inside a URL are treated as valid, since the token is
    resolved at send time; ``copy.unresolved_tokens`` covers that case instead.
    """
    bad: list[str] = []
    for url in extract_urls(text):
        candidate = url
        if find_template_tokens(candidate):
            continue
        # mailto:/tel:/sms: are valid links that are simply not http.
        if candidate.lower().startswith(("mailto:", "tel:", "sms:", "callto:")):
            continue
        if candidate.lower().startswith("www."):
            candidate = f"https://{candidate}"
        match = _VALID_URL_RE.match(candidate)
        if not match:
            bad.append(url)
            continue
        host = match.group("host")
        if "." not in host or host.startswith(".") or host.endswith(".") or ".." in host:
            bad.append(url)
    return bad


# ---------------------------------------------------------------------------
# Clock times
# ---------------------------------------------------------------------------


def parse_clock_time(value: Any) -> time | None:
    """Parse ``HH:MM`` / ``H:MM`` / ``HH:MM:SS`` into a ``time``.

    Returns ``None`` for anything unparseable rather than guessing, so the
    schedule rules can distinguish "no window" from "malformed window".
    """
    if value is None:
        return None
    if isinstance(value, time):
        return value
    text = str(value).strip()
    if not text:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    if hour == 24 and minute == 0 and second == 0:
        return time(23, 59, 59)
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    return time(hour, minute, second)


# ---------------------------------------------------------------------------
# CSV header canonicalization
# ---------------------------------------------------------------------------

_HEADER_ALIASES: Final[dict[str, str]] = {
    "email": "email",
    "emailaddress": "email",
    "email_address": "email",
    "workemail": "email",
    "work_email": "email",
    "e_mail": "email",
    "firstname": "first_name",
    "first_name": "first_name",
    "fname": "first_name",
    "givenname": "first_name",
    "given_name": "first_name",
    "lastname": "last_name",
    "last_name": "last_name",
    "lname": "last_name",
    "surname": "last_name",
    "familyname": "last_name",
    "family_name": "last_name",
    "company": "company_name",
    "companyname": "company_name",
    "company_name": "company_name",
    "account": "company_name",
    "accountname": "company_name",
    "organization": "company_name",
    "organisation": "company_name",
    "domain": "company_domain",
    "companydomain": "company_domain",
    "company_domain": "company_domain",
    "website": "company_domain",
    "company_website": "company_domain",
    "url": "company_domain",
    "title": "job_title",
    "jobtitle": "job_title",
    "job_title": "job_title",
    "position": "job_title",
    "role": "job_title",
    "country": "country",
    "countrycode": "country",
    "country_code": "country",
    "region": "region",
    "state": "region",
    "territory": "region",
    "personalization": "personalization",
    "personalisation": "personalization",
    "custom_message": "personalization",
    "icebreaker": "personalization",
    "opener": "personalization",
    "id": "id",
    "leadid": "id",
    "lead_id": "id",
    "sender": "assigned_sender",
    "assignedsender": "assigned_sender",
    "assigned_sender": "assigned_sender",
    "sending_account": "assigned_sender",
    "from_email": "assigned_sender",
    "suppressed": "suppressed",
    "do_not_contact": "suppressed",
    "dnc": "suppressed",
    "status": "status_label",
}

KNOWN_LEAD_FIELDS: Final[frozenset[str]] = frozenset(_HEADER_ALIASES.values())


def canonical_header(header: str) -> str:
    """Map a CSV header to a canonical lead field, or a ``custom.`` variable.

    Unrecognized headers are preserved as custom variables rather than dropped,
    because campaign copy frequently references them.
    """
    cleaned = str(header).replace(BOM, "").strip()
    key = re.sub(r"[^a-z0-9]+", "_", cleaned.lower()).strip("_")
    compact = key.replace("_", "")
    if key in _HEADER_ALIASES:
        return _HEADER_ALIASES[key]
    if compact in _HEADER_ALIASES:
        return _HEADER_ALIASES[compact]
    return f"custom.{key or 'unnamed'}"


# ---------------------------------------------------------------------------
# Scalar coercion
# ---------------------------------------------------------------------------

_TRUE_VALUES: Final = frozenset({"true", "yes", "y", "1", "t", "on"})
_FALSE_VALUES: Final = frozenset({"false", "no", "n", "0", "f", "off"})


def coerce_bool(value: Any) -> bool | None:
    """Parse a boolean from CSV/YAML text. Unrecognized input yields ``None``.

    Returning ``None`` matters: an unparseable ``stop_on_reply`` must become
    ``UNKNOWN``, not ``False``.
    """
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return None


def coerce_int(value: Any) -> int | None:
    """Parse an integer, tolerating ``"1,000"`` and ``"250.0"``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip().replace(",", "").replace("_", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            as_float = float(text)
        except ValueError:
            return None
        return int(as_float) if as_float.is_integer() else None


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

FREE_EMAIL_DOMAINS: Final[frozenset[str]] = frozenset({
    "aol.com", "comcast.net", "fastmail.com", "gmail.com", "gmx.com", "gmx.de",
    "googlemail.com", "hey.com", "hotmail.co.uk", "hotmail.com", "hotmail.fr",
    "icloud.com", "inbox.com", "libero.it", "live.com", "mac.com", "mail.com",
    "mail.ru", "me.com", "msn.com", "naver.com", "orange.fr", "outlook.com",
    "pm.me", "proton.me", "protonmail.com", "qq.com", "rediffmail.com",
    "rocketmail.com", "seznam.cz", "sina.com", "t-online.de", "tutanota.com",
    "verizon.net", "wanadoo.fr", "web.de", "yahoo.co.in", "yahoo.co.jp",
    "yahoo.co.uk", "yahoo.com", "yahoo.fr", "yandex.com", "yandex.ru",
    "ymail.com", "zoho.com",
})

ROLE_LOCAL_PARTS: Final[frozenset[str]] = frozenset({
    "abuse", "accounting", "accounts", "admin", "administrator", "billing",
    "careers", "compliance", "contact", "customerservice", "enquiries",
    "enquiry", "feedback", "finance", "help", "hello", "hi", "hr", "info",
    "inquiries", "inquiry", "invoices", "it", "jobs", "legal", "mail",
    "marketing", "media", "news", "newsletter", "noreply", "no-reply",
    "office", "orders", "partners", "payments", "postmaster", "press",
    "privacy", "purchasing", "recruiting", "sales", "security", "service",
    "support", "team", "webmaster", "welcome",
})

PLACEHOLDER_VALUES: Final[frozenset[str]] = frozenset({
    "-", "--", "?", "??", "n/a", "na", "none", "nil", "null", "nan", "tbd",
    "todo", "to do", "unknown", "test", "testing", "test test", "asdf",
    "asdfasdf", "qwerty", "xxx", "xxxx", "xx", "foo", "bar", "foobar", "baz",
    "lorem", "lorem ipsum", "example", "sample", "placeholder", "your name",
    "first name", "firstname", "last name", "company", "company name",
    "acme", "acme inc", "acme corp", "john doe", "jane doe", "no name",
    "not available", "not found", "undefined", "#n/a", "#value!", "#ref!",
})
