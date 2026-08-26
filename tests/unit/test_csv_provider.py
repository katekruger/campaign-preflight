"""CSV and campaign-file parsing, including the hostile-input cases.

The governing rule under test: a malformed row is reported, never silently
dropped. A discarded bad row would make a broken list look clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from campaign_preflight.errors import InputError
from campaign_preflight.models import CapabilityStatus
from campaign_preflight.providers.csv_provider import (
    CSVProvider,
    load_campaign_document,
    parse_campaign,
    read_evidence,
    read_leads,
    read_senders,
    read_suppressions,
)

HEADER = "email,first_name,last_name,company_name,company_domain,job_title\n"


def write(tmp_path: Path, name: str, content: str | bytes) -> Path:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


class TestLeadsCsv:
    def test_reads_a_simple_file(self, tmp_path: Path) -> None:
        path = write(tmp_path, "leads.csv", HEADER + "ana@corp.example.com,Ana,Diaz,Corp,corp.example.com,VP\n")
        leads, warnings, truncated = read_leads(path)
        assert len(leads) == 1
        assert leads[0].email == "ana@corp.example.com"
        assert leads[0].source_row == 2
        assert not warnings and not truncated

    def test_header_aliases_are_resolved(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "leads.csv",
            "E-Mail Address,First Name,Company,Website\nana@corp.example.com,Ana,Corp,https://corp.example.com\n",
        )
        leads, _, _ = read_leads(path)
        assert leads[0].first_name == "Ana"
        assert leads[0].company_name == "Corp"
        assert leads[0].company_domain == "corp.example.com"

    def test_unknown_columns_become_custom_variables(self, tmp_path: Path) -> None:
        path = write(tmp_path, "leads.csv", "email,Funding Stage\nana@corp.example.com,Series B\n")
        leads, _, _ = read_leads(path)
        assert leads[0].custom_variables == {"funding_stage": "Series B"}

    def test_utf8_bom_is_handled(self, tmp_path: Path) -> None:
        content = ("﻿" + HEADER + "ana@corp.example.com,Ana,Diaz,Corp,corp.example.com,VP\n")
        path = write(tmp_path, "leads.csv", content.encode("utf-8"))
        leads, _, _ = read_leads(path)
        assert len(leads) == 1 and leads[0].first_name == "Ana"

    def test_windows_line_endings_are_handled(self, tmp_path: Path) -> None:
        content = HEADER.replace("\n", "\r\n") + "ana@corp.example.com,Ana,Diaz,Corp,corp.example.com,VP\r\n"
        path = write(tmp_path, "leads.csv", content)
        leads, _, _ = read_leads(path)
        assert len(leads) == 1 and leads[0].job_title == "VP"

    def test_blank_rows_are_reported_not_silently_skipped(self, tmp_path: Path) -> None:
        path = write(tmp_path, "leads.csv", HEADER + "\n" + "ana@corp.example.com,Ana,Diaz,Corp,c.example.com,VP\n")
        leads, warnings, _ = read_leads(path)
        assert len(leads) == 1
        assert any("blank row" in w for w in warnings)

    def test_short_row_is_kept_and_reported(self, tmp_path: Path) -> None:
        path = write(tmp_path, "leads.csv", HEADER + "ana@corp.example.com,Ana\n")
        leads, warnings, _ = read_leads(path)
        assert len(leads) == 1, "a short row must not vanish"
        assert leads[0].company_name is None
        assert any("header has 6" in w for w in warnings)

    def test_long_row_is_kept_and_reported(self, tmp_path: Path) -> None:
        path = write(tmp_path, "leads.csv", HEADER + "a@c.example.com,A,B,C,d.example.com,VP,extra,more\n")
        leads, warnings, _ = read_leads(path)
        assert len(leads) == 1
        assert any("extra field" in w for w in warnings)

    def test_duplicate_columns_are_reported_and_folded(self, tmp_path: Path) -> None:
        path = write(tmp_path, "leads.csv", "email,Company,company_name\na@c.example.com,,Corp Two\n")
        leads, warnings, _ = read_leads(path)
        assert any("duplicate column" in w for w in warnings)
        assert leads[0].company_name == "Corp Two", "first non-empty value wins"

    def test_blank_header_is_reported(self, tmp_path: Path) -> None:
        path = write(tmp_path, "leads.csv", "email,,name\na@c.example.com,x,y\n")
        _, warnings, _ = read_leads(path)
        assert any("blank header" in w for w in warnings)

    def test_malformed_emails_are_preserved(self, tmp_path: Path) -> None:
        path = write(tmp_path, "leads.csv", HEADER + "not-an-email,Ana,Diaz,Corp,c.example.com,VP\n")
        leads, _, _ = read_leads(path)
        assert leads[0].email == "not-an-email"
        assert leads[0].normalized_email == "not-an-email"

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(InputError, match="not found"):
            read_leads(tmp_path / "nope.csv")

    def test_empty_file(self, tmp_path: Path) -> None:
        with pytest.raises(InputError, match="is empty"):
            read_leads(write(tmp_path, "leads.csv", ""))

    def test_header_only_file_yields_no_leads(self, tmp_path: Path) -> None:
        leads, _, _ = read_leads(write(tmp_path, "leads.csv", HEADER))
        assert leads == []

    def test_no_email_column_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(InputError, match="no recognizable email column"):
            read_leads(write(tmp_path, "leads.csv", "name,company\nAna,Corp\n"))

    def test_invalid_utf8_is_an_error(self, tmp_path: Path) -> None:
        path = write(tmp_path, "leads.csv", b"email\n\xff\xfe\x00bad\n")
        with pytest.raises(UnicodeDecodeError):
            read_leads(path)

    def test_row_numbers_match_a_spreadsheet(self, tmp_path: Path) -> None:
        rows = "".join(f"p{i}@c{i}.example.com,P,Q,C,c{i}.example.com,VP\n" for i in range(3))
        leads, _, _ = read_leads(write(tmp_path, "leads.csv", HEADER + rows))
        assert [lead.source_row for lead in leads] == [2, 3, 4]

    def test_very_long_field_is_read_not_crashed(self, tmp_path: Path) -> None:
        long_value = "x" * 200_000
        path = write(tmp_path, "leads.csv", HEADER + f'a@c.example.com,Ana,Diaz,"{long_value}",c.example.com,VP\n')
        leads, _, _ = read_leads(path)
        assert len(leads[0].company_name or "") == 200_000

    def test_quoted_embedded_newlines(self, tmp_path: Path) -> None:
        path = write(tmp_path, "leads.csv", HEADER + 'a@c.example.com,Ana,Diaz,"Corp\nTwo",c.example.com,VP\n')
        leads, _, _ = read_leads(path)
        assert leads[0].company_name == "Corp\nTwo"


class TestSuppressionsCsv:
    def test_value_column(self, tmp_path: Path) -> None:
        path = write(tmp_path, "s.csv", "value,is_domain\na@c.example.com,false\nblocked.example.com,true\n")
        entries, _ = read_suppressions(path)
        assert entries[0].value == "a@c.example.com" and not entries[0].is_domain
        assert entries[1].value == "blocked.example.com" and entries[1].is_domain

    def test_domain_inferred_from_missing_at_sign(self, tmp_path: Path) -> None:
        entries, _ = read_suppressions(write(tmp_path, "s.csv", "value\nblocked.example.com\n"))
        assert entries[0].is_domain

    def test_email_and_domain_columns(self, tmp_path: Path) -> None:
        entries, _ = read_suppressions(write(tmp_path, "s.csv", "email,domain\na@c.example.com,\n,x.example.com\n"))
        assert len(entries) == 2
        assert entries[1].is_domain

    def test_no_value_column(self, tmp_path: Path) -> None:
        with pytest.raises(InputError, match="no value/email/domain column"):
            read_suppressions(write(tmp_path, "s.csv", "foo,bar\n1,2\n"))

    def test_values_are_normalized(self, tmp_path: Path) -> None:
        entries, _ = read_suppressions(write(tmp_path, "s.csv", "value\n  A@Corp.Example.COM \n"))
        assert entries[0].value == "a@corp.example.com"


class TestCampaignDocument:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(InputError, match="not found"):
            load_campaign_document(tmp_path / "nope.yaml")

    def test_empty_file(self, tmp_path: Path) -> None:
        with pytest.raises(InputError, match="is empty"):
            load_campaign_document(write(tmp_path, "c.yaml", "# just a comment\n"))

    def test_unsupported_version(self, tmp_path: Path) -> None:
        with pytest.raises(InputError, match="unsupported campaign schema version"):
            load_campaign_document(write(tmp_path, "c.yaml", "version: 99\ncampaign: {}\n"))

    def test_non_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(InputError, match="must contain a mapping"):
            load_campaign_document(write(tmp_path, "c.yaml", "- a\n- b\n"))

    def test_day_names_and_numbers_both_parse(self) -> None:
        by_name, _ = parse_campaign(
            {"campaign": {"schedule": {"windows": [{"days": ["mon", "fri"]}]}}}
        )
        by_number, _ = parse_campaign(
            {"campaign": {"schedule": {"windows": [{"days": [1, 5]}]}}}
        )
        assert by_name.schedule.windows[0].days == by_number.schedule.windows[0].days

    def test_instantly_style_day_map_parses(self) -> None:
        campaign, _ = parse_campaign(
            {"campaign": {"schedule": {"schedules": [{"days": {"1": True, "6": False}}]}}}
        )
        assert campaign.schedule.windows[0].days == frozenset({1})

    def test_unparseable_time_is_reported_not_guessed(self) -> None:
        campaign, warnings = parse_campaign(
            {"campaign": {"schedule": {"windows": [{"start": "nine am", "end": "17:00"}]}}}
        )
        assert campaign.schedule.windows[0].start is None
        assert any("unparseable start time" in w for w in warnings)

    def test_variants_are_flattened(self) -> None:
        campaign, _ = parse_campaign(
            {
                "campaign": {
                    "steps": [
                        {"variants": [{"subject": "A", "body": "1"}, {"subject": "B", "body": "2"}]}
                    ]
                }
            }
        )
        assert len(campaign.steps) == 2
        assert {s.variant_index for s in campaign.steps} == {0, 1}
        assert {s.index for s in campaign.steps} == {0}

    def test_stop_on_reply_null_stays_null(self) -> None:
        campaign, _ = parse_campaign({"campaign": {"stop_on_reply": None}})
        assert campaign.stop_on_reply is None

    def test_unparseable_stop_on_reply_stays_null(self) -> None:
        campaign, _ = parse_campaign({"campaign": {"stop_on_reply": "maybe"}})
        assert campaign.stop_on_reply is None


class TestSendersAndEvidence:
    def test_senders_file(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "senders.yaml",
            "senders:\n  - email: a@example.com\n    health_score: 90\n    daily_limit: 50\n",
        )
        senders, _ = read_senders(path)
        assert senders[0].health_score == 90.0 and senders[0].daily_limit == 50

    def test_sender_without_email_is_reported(self, tmp_path: Path) -> None:
        path = write(tmp_path, "senders.yaml", "senders:\n  - name: Nobody\n")
        senders, warnings = read_senders(path)
        assert senders == []
        assert any("missing email" in w for w in warnings)

    def test_evidence_bundle(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "evidence.json",
            '{"evidence": [{"evidence_id": "e1", "excerpt": "x", "retrieved_at": "2026-01-01T00:00:00Z"}],'
            ' "claims": [{"claim_id": "c1", "lead_ref": "L-1", "text": "x", "evidence_ids": ["e1"]}]}',
        )
        evidence, claims, _ = read_evidence(path)
        assert evidence[0].retrieved_at is not None
        assert evidence[0].retrieved_at.tzinfo is not None
        assert claims[0].evidence_ids == ("e1",)

    def test_malformed_evidence_json(self, tmp_path: Path) -> None:
        with pytest.raises(InputError, match="not valid JSON"):
            read_evidence(write(tmp_path, "e.json", "{nope"))

    def test_claim_without_lead_ref_is_reported(self, tmp_path: Path) -> None:
        path = write(tmp_path, "e.json", '{"claims": [{"claim_id": "c1", "text": "x"}]}')
        _, claims, warnings = read_evidence(path)
        assert claims == []
        assert any("missing lead_ref" in w for w in warnings)


class TestProviderCapabilities:
    async def test_missing_optional_inputs_are_config_unavailable(self, tmp_path: Path) -> None:
        campaign = write(tmp_path, "c.yaml", "version: 1\ncampaign:\n  id: x\n  name: X\n")
        leads = write(tmp_path, "l.csv", HEADER + "a@c.example.com,A,B,C,c.example.com,VP\n")
        provider = CSVProvider(campaign_path=campaign, leads_path=leads)
        assert (await provider.list_suppressions()).status is CapabilityStatus.UNAVAILABLE_CONFIG
        assert (await provider.list_evidence()).status is CapabilityStatus.UNAVAILABLE_CONFIG

    async def test_unreadable_leads_file_is_a_failure_not_an_empty_list(
        self, tmp_path: Path
    ) -> None:
        """The distinction the whole capability model exists to preserve."""
        campaign = write(tmp_path, "c.yaml", "version: 1\ncampaign:\n  id: x\n")
        provider = CSVProvider(campaign_path=campaign, leads_path=tmp_path / "missing.csv")
        result = await provider.list_campaign_leads()
        assert result.status is CapabilityStatus.SUPPORTED_FAILED
        assert result.data is None

    async def test_empty_lead_list_is_a_success(self, tmp_path: Path) -> None:
        campaign = write(tmp_path, "c.yaml", "version: 1\ncampaign:\n  id: x\n")
        leads = write(tmp_path, "l.csv", HEADER)
        provider = CSVProvider(campaign_path=campaign, leads_path=leads)
        result = await provider.list_campaign_leads()
        assert result.status is CapabilityStatus.SUPPORTED_OK
        assert result.data == []
