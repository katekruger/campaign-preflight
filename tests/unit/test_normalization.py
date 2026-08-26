"""Tests for the normalization primitives every provider and rule depends on."""

from __future__ import annotations

from datetime import time

import pytest

from campaign_preflight.normalization import (
    canonical_header,
    coerce_bool,
    coerce_int,
    domain_of,
    email_is_syntactically_valid,
    extract_urls,
    find_template_tokens,
    has_control_characters,
    hash_ref,
    is_formula_injection,
    malformed_urls,
    neutralize_formula,
    normalize_domain,
    normalize_email,
    normalize_text,
    parse_clock_time,
    strip_control_characters,
)


class TestEmail:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Ana@Corp.Example.COM", "ana@corp.example.com"),
            ("  ana@corp.example.com  ", "ana@corp.example.com"),
            ("<ana@corp.example.com>", "ana@corp.example.com"),
            ("", None),
            ("   ", None),
            (None, None),
        ],
    )
    def test_normalize(self, raw, expected) -> None:
        assert normalize_email(raw) == expected

    def test_normalization_is_idempotent(self) -> None:
        once = normalize_email("Ana@Corp.Example.COM")
        assert normalize_email(once) == once

    def test_plus_tags_and_dots_are_preserved(self) -> None:
        assert normalize_email("a.n.a+q3@corp.example.com") == "a.n.a+q3@corp.example.com"

    def test_domain_of(self) -> None:
        assert domain_of("Ana@Corp.Example.com") == "corp.example.com"
        assert domain_of("not-an-email") is None


class TestDomain:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://www.Corp.example.com/path?q=1", "corp.example.com"),
            ("WWW.CORP.EXAMPLE.COM", "corp.example.com"),
            ("corp.example.com:8443", "corp.example.com"),
            ("corp.example.com.", "corp.example.com"),
            ("", None),
            (None, None),
        ],
    )
    def test_normalize_domain(self, raw, expected) -> None:
        assert normalize_domain(raw) == expected


class TestText:
    def test_bom_and_crlf_are_removed(self) -> None:
        assert normalize_text("\ufeffHello\r\nWorld") == "Hello\nWorld"

    def test_empty_becomes_none(self) -> None:
        assert normalize_text("   ") is None
        assert normalize_text(None) is None

    @pytest.mark.parametrize("char", ["\u200b", "\u202e", "\x00", "\x1f", "\ufeff"])
    def test_control_characters_are_detected(self, char: str) -> None:
        assert has_control_characters(f"ab{char}cd")
        assert strip_control_characters(f"ab{char}cd") == "abcd"

    @pytest.mark.parametrize("text", ["normal", "tabs\tand\nnewlines", "accénts", "日本語"])
    def test_ordinary_text_is_not_flagged(self, text: str) -> None:
        assert not has_control_characters(text)

    def test_hash_ref_is_stable_and_case_insensitive(self) -> None:
        assert hash_ref("Ana@Corp.example.com") == hash_ref("ana@corp.example.com")
        assert len(hash_ref("a@b.com")) == 16

    def test_hash_ref_does_not_contain_the_input(self) -> None:
        assert "ana" not in hash_ref("ana@corp.example.com")


class TestFormulaInjection:
    @pytest.mark.parametrize("value", ["=1+1", "+A1", "@SUM(1)", "\t=cmd", "-cmd|'/c calc'!A1"])
    def test_dangerous_values(self, value: str) -> None:
        assert is_formula_injection(value)
        assert neutralize_formula(value).startswith("'")

    @pytest.mark.parametrize("value", ["-5", "+1.5", "-0.25", "Corp Ltd", "", "a=b"])
    def test_safe_values(self, value: str) -> None:
        assert not is_formula_injection(value)
        assert neutralize_formula(value) == value


class TestTemplateTokens:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Hi {{first_name}}", ["first_name"]),
            ("Hi {% first_name %}", ["first_name"]),
            ("Hi ${first_name}", ["first_name"]),
            ("Hi {{ first_name | there }}", ["first_name"]),
            ("{{a}} and {{b}} and {{a}}", ["a", "b"]),
            ("no tokens here", []),
            (r"escaped \{{a}}", []),
            ("doubled {{{{a}}}}", []),
            (None, []),
        ],
    )
    def test_find_tokens(self, text, expected) -> None:
        assert find_template_tokens(text) == expected


class TestUrls:
    def test_extracts_raw_and_href_urls(self) -> None:
        text = 'See https://a.example.com and <a href="https://b.example.com">b</a>'
        found = extract_urls(text)
        assert "https://a.example.com" in found
        assert "https://b.example.com" in found

    def test_trailing_punctuation_is_trimmed(self) -> None:
        assert extract_urls("Visit https://a.example.com.") == ["https://a.example.com"]

    @pytest.mark.parametrize(
        "url", ["https://a.example.com", "http://a.example.com/x?y=1", "www.a.example.com"]
    )
    def test_valid_urls_are_not_malformed(self, url: str) -> None:
        assert malformed_urls(f"go to {url}") == []

    @pytest.mark.parametrize("url", ["htp:/a.example.com", "https://", "http://.example.com"])
    def test_malformed_urls_are_detected(self, url: str) -> None:
        assert malformed_urls(f"go to {url}")

    def test_template_tokens_in_urls_are_left_alone(self) -> None:
        assert malformed_urls("https://{{company_domain}}/pricing") == []

    def test_mailto_is_not_a_malformed_url(self) -> None:
        assert malformed_urls("write to mailto:ana@corp.example.com") == []

    def test_windows_paths_are_not_mistaken_for_links(self) -> None:
        assert malformed_urls("open C:/Users/report.xlsx") == []


class TestClockTime:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("09:00", time(9, 0)),
            ("9:05", time(9, 5)),
            ("17:30:15", time(17, 30, 15)),
            ("24:00", time(23, 59, 59)),
            ("25:00", None),
            ("09:99", None),
            ("nine", None),
            ("", None),
            (None, None),
        ],
    )
    def test_parse(self, raw, expected) -> None:
        assert parse_clock_time(raw) == expected


class TestHeaders:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Email", "email"),
            ("E-Mail Address", "email"),
            ("First Name", "first_name"),
            ("firstName", "first_name"),
            ("Company", "company_name"),
            ("Website", "company_domain"),
            ("Job Title", "job_title"),
            ("\ufeffemail", "email"),
            ("Custom Score", "custom.custom_score"),
            ("", "custom.unnamed"),
        ],
    )
    def test_canonicalization(self, header: str, expected: str) -> None:
        assert canonical_header(header) == expected


class TestCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("true", True), ("YES", True), ("1", True), ("false", False), ("no", False), (True, True)],
    )
    def test_bool(self, raw, expected) -> None:
        assert coerce_bool(raw) is expected

    @pytest.mark.parametrize("raw", ["maybe", "", "2", None, "null"])
    def test_unparseable_bool_is_none_not_false(self, raw) -> None:
        """The distinction that keeps stop_on_reply honest."""
        assert coerce_bool(raw) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("100", 100), ("1,000", 1000), ("250.0", 250), (42, 42), (4.0, 4)],
    )
    def test_int(self, raw, expected) -> None:
        assert coerce_int(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "", None, 1.5, True])
    def test_unparseable_int_is_none(self, raw) -> None:
        assert coerce_int(raw) is None


@pytest.mark.parametrize(
    "email",
    [
        "ana@corp.example.com",
        "a@b.co",
        "first.last+tag@sub.domain.example.com",
        "user@xn--bcher-kva.example.com",
    ],
)
def test_valid_emails(email: str) -> None:
    assert email_is_syntactically_valid(email)


@pytest.mark.parametrize(
    "email",
    ["", "a", "a@", "@b.com", "a@b", "a b@c.com", "a@@b.com", "a@b..com", "a@.b.com", None],
)
def test_invalid_emails(email) -> None:
    assert not email_is_syntactically_valid(email)


def test_control_characters_make_an_email_invalid() -> None:
    assert not email_is_syntactically_valid("a\u200b@corp.example.com")
