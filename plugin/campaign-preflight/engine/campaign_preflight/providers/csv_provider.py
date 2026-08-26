"""File-backed provider: campaign YAML/JSON plus leads/suppressions CSV.

This is the provider that works with no account, no key, and no network. It is
also the strictest reader in the package, because a CSV from a stranger is the
most hostile input Campaign Preflight accepts.

Reading rules:

* Rows are streamed, never slurped, so a 100k-lead file stays flat in memory.
* A malformed row is *kept* and reported, never silently dropped -- a discarded
  bad row would look like a clean campaign.
* Source row numbers survive into the report so a finding is actionable.
* Every failure mode maps to an explicit capability status, so "no suppression
  file supplied" is distinguishable from "suppression file unreadable".
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .. import _yaml
from ..config import safe_resolve
from ..errors import InputError
from ..models import (
    Campaign,
    CampaignSchedule,
    CampaignStep,
    Capability,
    Lead,
    PersonalizationClaim,
    Sender,
    SendingWindow,
    SourceEvidence,
    SuppressionEntry,
)
from ..normalization import (
    BOM,
    canonical_header,
    coerce_bool,
    coerce_int,
    domain_of,
    normalize_domain,
    normalize_email,
    normalize_text,
    parse_clock_time,
)
from .base import CampaignProvider, ProviderResult, failed, misconfigured, ok, unsupported

__all__ = ["CSVProvider", "load_campaign_document", "parse_campaign", "MAX_INPUT_BYTES"]

# 256 MB of CSV is ~1M leads; beyond that the user wants a database, not a linter.
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_CAMPAIGN_BYTES = 8 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_FIELD_BYTES = 1_000_000

SUPPORTED_CAMPAIGN_VERSIONS = frozenset({1})

_DAY_NAMES: dict[str, int] = {
    "sun": 0, "sunday": 0,
    "mon": 1, "monday": 1,
    "tue": 2, "tues": 2, "tuesday": 2,
    "wed": 3, "weds": 3, "wednesday": 3,
    "thu": 4, "thur": 4, "thurs": 4, "thursday": 4,
    "fri": 5, "friday": 5,
    "sat": 6, "saturday": 6,
}

# Python's csv module defaults to a 128 KB field limit; a single oversized cell
# would otherwise abort the whole read. Raise it to our own explicit bound so we
# can report the row instead of crashing.
csv.field_size_limit(MAX_FIELD_BYTES)


# ---------------------------------------------------------------------------
# Campaign document
# ---------------------------------------------------------------------------


def load_campaign_document(path: Path | str) -> dict[str, Any]:
    """Read and parse a campaign YAML or JSON file."""
    resolved = safe_resolve(path)
    if not resolved.is_file():
        raise InputError(f"campaign file not found: {path}")
    size = resolved.stat().st_size
    if size > MAX_CAMPAIGN_BYTES:
        raise InputError(f"campaign file is {size} bytes, above {MAX_CAMPAIGN_BYTES}")
    try:
        text = resolved.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(f"campaign file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise InputError(f"campaign file could not be read: {path} ({exc.strerror})") from exc
    try:
        document = _yaml.safe_load(text)
    except _yaml.YamlError as exc:
        raise InputError(f"campaign file is not valid YAML/JSON: {path} ({exc})") from exc
    if document is None:
        raise InputError(f"campaign file is empty: {path}")
    if not isinstance(document, dict):
        raise InputError(
            f"campaign file must contain a mapping, got {type(document).__name__}: {path}"
        )
    version = document.get("version", 1)
    if version not in SUPPORTED_CAMPAIGN_VERSIONS:
        supported = ", ".join(str(v) for v in sorted(SUPPORTED_CAMPAIGN_VERSIONS))
        raise InputError(
            f"unsupported campaign schema version {version!r} in {path}",
            hint=f"supported versions: {supported}",
        )
    return document


def _parse_days(value: Any, warnings: list[str]) -> frozenset[int]:
    """Accept ``[mon, tue]``, ``[1,2]``, or Instantly's ``{"1": true}`` map."""
    if value is None:
        return frozenset()
    days: set[int] = set()
    if isinstance(value, dict):
        for key, enabled in value.items():
            if coerce_bool(enabled) is not True:
                continue
            index = coerce_int(key)
            if index is not None and 0 <= index <= 6:
                days.add(index)
            else:
                warnings.append(f"schedule: ignoring unrecognized day key {key!r}")
        return frozenset(days)
    if isinstance(value, str):
        value = [part for part in value.replace(";", ",").split(",") if part.strip()]
    if not isinstance(value, list):
        warnings.append(f"schedule: ignoring unrecognized days value {value!r}")
        return frozenset()
    for item in value:
        index = coerce_int(item)
        if index is not None and 0 <= index <= 6:
            days.add(index)
            continue
        name = str(item).strip().lower()
        if name in _DAY_NAMES:
            days.add(_DAY_NAMES[name])
        else:
            warnings.append(f"schedule: ignoring unrecognized day {item!r}")
    return frozenset(days)


def _parse_date(value: Any, field_name: str, warnings: list[str]) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        warnings.append(f"campaign.{field_name}: could not parse date {value!r}")
        return None


def _parse_schedule(raw: Any, fallback_tz: str | None, warnings: list[str]) -> CampaignSchedule:
    if raw is None:
        return CampaignSchedule(timezone_name=fallback_tz)
    if not isinstance(raw, dict):
        warnings.append("campaign.schedule: expected a mapping; ignoring")
        return CampaignSchedule(timezone_name=fallback_tz)

    schedule_tz = normalize_text(raw.get("timezone")) or fallback_tz
    entries = raw.get("windows")
    if entries is None:
        entries = raw.get("schedules")  # Instantly's own key
    windows: list[SendingWindow] = []
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        warnings.append("campaign.schedule.windows: expected a list; ignoring")
        entries = []

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            warnings.append(f"campaign.schedule.windows[{index}]: expected a mapping; ignoring")
            continue
        timing = entry.get("timing") if isinstance(entry.get("timing"), dict) else {}
        raw_start = entry.get("start", timing.get("from"))
        raw_end = entry.get("end", timing.get("to"))
        raw_tz = normalize_text(entry.get("timezone")) or schedule_tz
        start = parse_clock_time(raw_start)
        end = parse_clock_time(raw_end)
        if raw_start not in (None, "") and start is None:
            warnings.append(
                f"campaign.schedule.windows[{index}]: unparseable start time {raw_start!r}"
            )
        if raw_end not in (None, "") and end is None:
            warnings.append(
                f"campaign.schedule.windows[{index}]: unparseable end time {raw_end!r}"
            )
        windows.append(
            SendingWindow(
                name=normalize_text(entry.get("name")) or f"window {index + 1}",
                start=start,
                end=end,
                days=_parse_days(entry.get("days"), warnings),
                timezone_name=raw_tz,
                raw_timezone=normalize_text(entry.get("timezone")),
            )
        )

    return CampaignSchedule(
        start_date=_parse_date(raw.get("start_date"), "schedule.start_date", warnings),
        end_date=_parse_date(raw.get("end_date"), "schedule.end_date", warnings),
        windows=tuple(windows),
        timezone_name=schedule_tz,
        raw=raw,
    )


def _parse_steps(raw: Any, warnings: list[str]) -> tuple[CampaignStep, ...]:
    """Flatten steps and their variants into a single ordered tuple."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        warnings.append("campaign.steps: expected a list; ignoring")
        return ()
    steps: list[CampaignStep] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            warnings.append(f"campaign.steps[{index}]: expected a mapping; ignoring")
            continue
        delay = entry.get("delay")
        delay_value: float | None
        try:
            delay_value = float(delay) if delay is not None else None
        except (TypeError, ValueError):
            warnings.append(f"campaign.steps[{index}]: unparseable delay {delay!r}")
            delay_value = None
        variants = entry.get("variants")
        if not isinstance(variants, list) or not variants:
            variants = [{"subject": entry.get("subject", ""), "body": entry.get("body", "")}]
        for v_index, variant in enumerate(variants):
            if not isinstance(variant, dict):
                warnings.append(
                    f"campaign.steps[{index}].variants[{v_index}]: expected a mapping; ignoring"
                )
                continue
            steps.append(
                CampaignStep(
                    index=index,
                    step_type=str(entry.get("type", "email")),
                    delay=delay_value,
                    delay_unit=normalize_text(entry.get("delay_unit")),
                    subject=str(variant.get("subject") or ""),
                    body=str(variant.get("body") or ""),
                    variant_index=v_index,
                    disabled=bool(coerce_bool(variant.get("v_disabled")) or False),
                )
            )
    return tuple(steps)


def parse_campaign(
    document: dict[str, Any], *, source: str | None = None
) -> tuple[Campaign, list[str]]:
    """Turn a campaign document into a :class:`Campaign` plus parse warnings."""
    warnings: list[str] = []
    body = document.get("campaign") if isinstance(document.get("campaign"), dict) else document

    timezone_name = normalize_text(body.get("timezone"))
    schedule = _parse_schedule(body.get("schedule"), timezone_name, warnings)

    sender_emails: list[str] = []
    raw_senders = body.get("senders") or body.get("email_list") or []
    if isinstance(raw_senders, str):
        raw_senders = [raw_senders]
    if isinstance(raw_senders, list):
        for entry in raw_senders:
            email = entry.get("email") if isinstance(entry, dict) else entry
            normalized = normalize_email(email)
            if normalized:
                sender_emails.append(normalized)
            elif entry:
                warnings.append(f"campaign.senders: unparseable sender entry {entry!r}")
    else:
        warnings.append("campaign.senders: expected a list; ignoring")

    custom_variables = body.get("custom_variables") or {}
    if not isinstance(custom_variables, dict):
        warnings.append("campaign.custom_variables: expected a mapping; ignoring")
        custom_variables = {}

    campaign = Campaign(
        id=normalize_text(body.get("id")),
        name=normalize_text(body.get("name")),
        status=(normalize_text(body.get("status")) or "").lower() or None,
        raw_status=body.get("status"),
        timezone_name=timezone_name or schedule.timezone_name,
        schedule=schedule,
        daily_limit=coerce_int(body.get("daily_limit")),
        stop_on_reply=coerce_bool(body.get("stop_on_reply")),
        stop_on_auto_reply=coerce_bool(body.get("stop_on_auto_reply")),
        steps=_parse_steps(body.get("steps") or body.get("sequence"), warnings),
        sender_emails=tuple(sender_emails),
        custom_variables=custom_variables,
        lead_count_hint=coerce_int(body.get("lead_count")),
        raw={"source": source} if source else {},
    )
    return campaign, warnings


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------


def _open_csv(path: Path, *, what: str) -> tuple[io.TextIOWrapper, int]:
    resolved = safe_resolve(path)
    if not resolved.is_file():
        raise InputError(f"{what} not found: {path}")
    size = resolved.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise InputError(f"{what} is {size} bytes, above the {MAX_INPUT_BYTES}-byte limit")
    if size == 0:
        raise InputError(f"{what} is empty: {path}")
    # utf-8-sig strips a BOM; newline="" lets csv handle CRLF and embedded newlines.
    handle = open(resolved, encoding="utf-8-sig", newline="")
    return handle, size


def _resolve_headers(
    raw_headers: list[str], warnings: list[str], what: str
) -> list[tuple[str, bool]]:
    """Canonicalize headers into ``(field, is_duplicate)`` pairs.

    Duplicates are reported and then folded: the first non-empty value in a row
    wins, so a file with two ``Company`` columns still yields one company name
    instead of an arbitrary one.
    """
    resolved: list[tuple[str, bool]] = []
    seen: dict[str, int] = {}
    for position, raw in enumerate(raw_headers):
        cleaned = (raw or "").replace(BOM, "").strip()
        if not cleaned:
            warnings.append(f"{what}: column {position + 1} has a blank header")
            resolved.append((f"custom.column_{position + 1}", False))
            continue
        canonical = canonical_header(cleaned)
        if canonical in seen:
            warnings.append(
                f"{what}: duplicate column '{cleaned}' maps to '{canonical}' "
                f"(already used by column {seen[canonical] + 1}); "
                f"keeping the first non-empty value per row"
            )
            resolved.append((canonical, True))
            continue
        seen[canonical] = position
        resolved.append((canonical, False))
    return resolved


def iter_lead_rows(
    path: Path | str, *, max_rows: int = MAX_ROWS
) -> Iterator[tuple[int, dict[str, str], list[str]]]:
    """Stream ``(row_number, values, row_warnings)`` from a leads CSV.

    ``row_number`` is 1-based and counts the header, so it matches what a
    spreadsheet shows. Rows are yielded one at a time: memory stays constant
    regardless of file size.
    """
    handle, _ = _open_csv(Path(path), what="leads file")
    warnings: list[str] = []
    try:
        reader = csv.reader(handle)
        try:
            raw_headers = next(reader)
        except StopIteration:
            raise InputError(f"leads file has no header row: {path}") from None
        headers = _resolve_headers(raw_headers, warnings, "leads file")
        if not any(field == "email" for field, _ in headers):
            raise InputError(
                f"leads file has no recognizable email column: {path}",
                hint=f"columns seen: {', '.join(raw_headers[:12])}",
            )
        # Header warnings ride along with the first row so callers see them once.
        pending = warnings
        width = len(headers)
        for offset, row in enumerate(reader):
            row_number = offset + 2
            if offset >= max_rows:
                pending = [
                    *pending,
                    f"leads file: stopped after {max_rows} rows; remaining rows not checked",
                ]
                yield row_number, {}, pending
                return
            row_warnings = pending
            pending = []
            if not row or all(not (cell or "").strip() for cell in row):
                yield row_number, {}, [*row_warnings, f"row {row_number}: blank row"]
                continue
            if len(row) != width:
                row_warnings = [
                    *row_warnings,
                    f"row {row_number}: has {len(row)} fields, header has {width}",
                ]
            values: dict[str, str] = {}
            for index, (field_name, is_duplicate) in enumerate(headers):
                cell = row[index] if index < len(row) else ""
                if is_duplicate:
                    if not (values.get(field_name) or "").strip() and (cell or "").strip():
                        values[field_name] = cell
                    continue
                values[field_name] = cell
            if len(row) > width:
                extra = "|".join(str(c) for c in row[width:])
                row_warnings = [
                    *row_warnings,
                    f"row {row_number}: {len(row) - width} extra field(s) ignored: {extra[:120]}",
                ]
            yield row_number, values, row_warnings
    finally:
        handle.close()


def _lead_from_row(row_number: int, values: dict[str, str], source_name: str) -> Lead:
    """Build a Lead from one CSV row. Never raises; bad data is preserved as-is."""
    custom: dict[str, str] = {}
    for key, value in values.items():
        if key.startswith("custom."):
            text = normalize_text(value)
            if text is not None:
                custom[key[len("custom.") :]] = text

    email_raw = normalize_text(values.get("email"))
    return Lead(
        id=normalize_text(values.get("id")),
        email=email_raw,
        normalized_email=normalize_email(email_raw),
        first_name=normalize_text(values.get("first_name")),
        last_name=normalize_text(values.get("last_name")),
        company_name=normalize_text(values.get("company_name")),
        # Never inferred from the email domain: a missing domain is a real finding.
        company_domain=normalize_domain(values.get("company_domain")),
        job_title=normalize_text(values.get("job_title")),
        country=normalize_text(values.get("country")),
        region=normalize_text(values.get("region")),
        personalization=normalize_text(values.get("personalization")),
        custom_variables=custom,
        assigned_sender=normalize_email(values.get("assigned_sender")),
        source_row=row_number,
        source_name=source_name,
        suppressed=coerce_bool(values.get("suppressed")),
        status_label=normalize_text(values.get("status_label")),
    )


def read_leads(path: Path | str) -> tuple[list[Lead], list[str], bool]:
    """Read a leads CSV into models. Returns ``(leads, warnings, truncated)``."""
    leads: list[Lead] = []
    warnings: list[str] = []
    truncated = False
    source_name = Path(path).name
    for row_number, values, row_warnings in iter_lead_rows(path):
        warnings.extend(row_warnings)
        if any("stopped after" in w for w in row_warnings):
            truncated = True
            break
        if not values:
            continue
        leads.append(_lead_from_row(row_number, values, source_name))
    return leads, warnings, truncated


def read_suppressions(path: Path | str) -> tuple[list[SuppressionEntry], list[str]]:
    """Read a suppressions CSV. Accepts ``value``/``email``/``domain`` columns."""
    handle, _ = _open_csv(Path(path), what="suppressions file")
    entries: list[SuppressionEntry] = []
    warnings: list[str] = []
    try:
        reader = csv.reader(handle)
        try:
            raw_headers = next(reader)
        except StopIteration:
            raise InputError(f"suppressions file has no header row: {path}") from None
        headers = [(h or "").replace(BOM, "").strip().lower() for h in raw_headers]
        aliases = {"value", "email", "domain", "bl_value", "entry", "address"}
        value_columns = [i for i, h in enumerate(headers) if h in aliases]
        if not value_columns:
            raise InputError(
                f"suppressions file has no value/email/domain column: {path}",
                hint=f"columns seen: {', '.join(raw_headers[:12])}",
            )
        reason_col = next((i for i, h in enumerate(headers) if h in {"reason", "note"}), None)
        domain_flag_col = next(
            (i for i, h in enumerate(headers) if h in {"is_domain", "domain_only"}), None
        )
        for offset, row in enumerate(reader):
            row_number = offset + 2
            if not row or all(not (cell or "").strip() for cell in row):
                continue
            raw_value = ""
            declared_domain = headers[value_columns[0]] == "domain"
            for column in value_columns:
                if column < len(row) and (row[column] or "").strip():
                    raw_value = row[column].strip()
                    declared_domain = headers[column] == "domain"
                    break
            if not raw_value:
                warnings.append(f"suppressions row {row_number}: no value")
                continue
            explicit_flag = (
                coerce_bool(row[domain_flag_col])
                if domain_flag_col is not None and domain_flag_col < len(row)
                else None
            )
            is_domain = (
                explicit_flag
                if explicit_flag is not None
                else (declared_domain or "@" not in raw_value)
            )
            value = (
                normalize_domain(raw_value) if is_domain else normalize_email(raw_value)
            ) or raw_value.strip().lower()
            entries.append(
                SuppressionEntry(
                    value=value,
                    is_domain=bool(is_domain),
                    reason=(
                        normalize_text(row[reason_col])
                        if reason_col is not None and reason_col < len(row)
                        else None
                    ),
                    source=Path(path).name,
                )
            )
    finally:
        handle.close()
    return entries, warnings


def parse_sender_entries(entries: Any, warnings: list[str]) -> list[Sender]:
    """Build Sender models from a list of dicts (or bare address strings).

    Shared by the standalone senders file and by inline ``senders:`` entries in a
    campaign file, so both forms support the same health fields.
    """
    senders: list[Sender] = []
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, list):
        warnings.append("senders: expected a list; ignoring")
        return senders
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            email = normalize_email(entry)
            if email:
                senders.append(Sender(email=email))
            else:
                warnings.append(f"senders[{index}]: unparseable address {entry!r}")
            continue
        if not isinstance(entry, dict):
            warnings.append(f"senders[{index}]: expected a mapping; ignoring")
            continue
        email = normalize_email(entry.get("email"))
        if not email:
            warnings.append(f"senders[{index}]: missing email; ignoring")
            continue
        score = entry.get("health_score", entry.get("warmup_score"))
        try:
            health = float(score) if score is not None else None
        except (TypeError, ValueError):
            warnings.append(f"senders[{index}]: unparseable health_score {score!r}")
            health = None
        senders.append(
            Sender(
                email=email,
                display_name=normalize_text(entry.get("name")),
                enabled=coerce_bool(entry.get("enabled")),
                status_label=normalize_text(entry.get("status")),
                status_is_error=coerce_bool(entry.get("error")),
                daily_limit=coerce_int(entry.get("daily_limit")),
                health_score=health,
                warmup_status=normalize_text(entry.get("warmup_status")),
                setup_pending=coerce_bool(entry.get("setup_pending")),
                provider=normalize_text(entry.get("provider")),
                raw_status=entry.get("status"),
            )
        )
    return senders


def read_senders(path: Path | str) -> tuple[list[Sender], list[str]]:
    """Read a sender YAML/JSON file."""
    resolved = safe_resolve(path)
    if not resolved.is_file():
        raise InputError(f"senders file not found: {path}")
    try:
        document = _yaml.safe_load(resolved.read_text(encoding="utf-8-sig"))
    except _yaml.YamlError as exc:
        raise InputError(f"senders file is not valid YAML/JSON: {path} ({exc})") from exc
    except OSError as exc:
        raise InputError(f"senders file could not be read: {path} ({exc.strerror})") from exc

    if isinstance(document, dict):
        entries = document.get("senders", [])
    elif isinstance(document, list):
        entries = document
    else:
        raise InputError(f"senders file must contain a list or a 'senders' key: {path}")

    warnings: list[str] = []
    senders = parse_sender_entries(entries, warnings)
    return senders, warnings


def read_evidence(
    path: Path | str,
) -> tuple[list[SourceEvidence], list[PersonalizationClaim], list[str]]:
    """Read the evidence JSON bundle (``evidence`` + optional ``claims``)."""
    resolved = safe_resolve(path)
    if not resolved.is_file():
        raise InputError(f"evidence file not found: {path}")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise InputError(f"evidence file is not valid JSON: {path} ({exc})") from exc
    except OSError as exc:
        raise InputError(f"evidence file could not be read: {path} ({exc.strerror})") from exc

    if isinstance(document, list):
        document = {"evidence": document}
    if not isinstance(document, dict):
        raise InputError(f"evidence file must contain an object or a list: {path}")

    warnings: list[str] = []
    evidence: list[SourceEvidence] = []
    for index, entry in enumerate(document.get("evidence") or []):
        if not isinstance(entry, dict):
            warnings.append(f"evidence[{index}]: expected an object; ignoring")
            continue
        retrieved = entry.get("retrieved_at")
        parsed_at: datetime | None = None
        if retrieved:
            try:
                parsed_at = datetime.fromisoformat(str(retrieved).replace("Z", "+00:00"))
                if parsed_at.tzinfo is None:
                    parsed_at = parsed_at.replace(tzinfo=timezone.utc)
            except ValueError:
                warnings.append(f"evidence[{index}]: unparseable retrieved_at {retrieved!r}")
        evidence.append(
            SourceEvidence(
                evidence_id=str(entry.get("evidence_id") or f"evidence-{index}"),
                lead_ref=normalize_text(entry.get("lead_ref") or entry.get("lead_id")),
                source_url=normalize_text(entry.get("source_url")),
                title=normalize_text(entry.get("title")),
                retrieved_at=parsed_at,
                excerpt=str(entry.get("excerpt") or ""),
                content_hash=normalize_text(entry.get("content_hash")),
                company_name=normalize_text(entry.get("company_name")),
            )
        )

    claims: list[PersonalizationClaim] = []
    for index, entry in enumerate(document.get("claims") or []):
        if not isinstance(entry, dict):
            warnings.append(f"claims[{index}]: expected an object; ignoring")
            continue
        lead_ref = normalize_text(entry.get("lead_ref") or entry.get("lead_id"))
        if not lead_ref:
            warnings.append(f"claims[{index}]: missing lead_ref; ignoring")
            continue
        raw_ids = entry.get("evidence_ids") or []
        claims.append(
            PersonalizationClaim(
                claim_id=str(entry.get("claim_id") or f"claim-{index}"),
                lead_ref=lead_ref,
                text=str(entry.get("text") or ""),
                evidence_ids=tuple(str(e) for e in raw_ids if e),
                numeric_values=tuple(str(n) for n in (entry.get("numeric_values") or [])),
                source_field=str(entry.get("source_field") or "personalization"),
            )
        )
    return evidence, claims, warnings


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class CSVProvider(CampaignProvider):
    """Read campaign inspection data from local files. No network, ever."""

    name = "csv"

    def __init__(
        self,
        campaign_path: Path | str,
        leads_path: Path | str,
        *,
        senders_path: Path | str | None = None,
        suppressions_path: Path | str | None = None,
        evidence_path: Path | str | None = None,
    ) -> None:
        self.campaign_path = Path(campaign_path)
        self.leads_path = Path(leads_path)
        self.senders_path = Path(senders_path) if senders_path else None
        self.suppressions_path = Path(suppressions_path) if suppressions_path else None
        self.evidence_path = Path(evidence_path) if evidence_path else None
        self.base_url = None
        self.warnings: list[str] = []
        self._campaign: Campaign | None = None
        self._document: dict[str, Any] | None = None
        self._claims: list[PersonalizationClaim] | None = None

    def validate_required_inputs(self) -> None:
        """Raise if a mandatory input is missing or unreadable.

        The campaign and leads files are preconditions for a run, not optional
        capabilities: a typo in a path should produce a clear input error and
        exit code 4, not a full report announcing that everything is UNKNOWN.
        Optional inputs stay capability-based, because their absence is a real
        (and reportable) state of the world.
        """
        for label, path in (
            ("campaign file", self.campaign_path),
            ("leads file", self.leads_path),
        ):
            resolved = safe_resolve(path)
            if not resolved.is_file():
                raise InputError(
                    f"{label} not found: {path}",
                    hint="check the path; both --campaign and --leads are required",
                )

    async def get_campaign(self, campaign_id: str | None = None) -> ProviderResult[Campaign]:
        try:
            document = load_campaign_document(self.campaign_path)
        except InputError as exc:
            return failed(Capability.CAMPAIGN, str(exc))
        campaign, warnings = parse_campaign(document, source=str(self.campaign_path))
        self.warnings.extend(warnings)
        self._campaign = campaign
        self._document = document
        return ok(Capability.CAMPAIGN, campaign, detail=f"read from {self.campaign_path.name}")

    async def list_campaign_leads(
        self, campaign_id: str | None = None, *, limit: int | None = None
    ) -> ProviderResult[list[Lead]]:
        try:
            leads, warnings, truncated = read_leads(self.leads_path)
        except InputError as exc:
            return failed(Capability.LEADS, str(exc))
        self.warnings.extend(warnings)
        if limit is not None and len(leads) > limit:
            leads = leads[:limit]
            truncated = True
        return ok(
            Capability.LEADS,
            leads,
            detail=f"read {len(leads)} rows from {self.leads_path.name}",
            partial=truncated,
        )

    def _inline_senders(self) -> list[Sender] | None:
        """Sender records declared inline in the campaign file, if any."""
        if self._document is None:
            return None
        body = self._document.get("campaign")
        if not isinstance(body, dict):
            body = self._document
        entries = body.get("senders")
        if entries is None:
            return None
        return parse_sender_entries(entries, self.warnings)

    async def list_campaign_senders(
        self, campaign_id: str | None = None
    ) -> ProviderResult[list[Sender]]:
        if self.senders_path is not None:
            try:
                senders, warnings = read_senders(self.senders_path)
            except InputError as exc:
                return failed(Capability.SENDERS, str(exc))
            self.warnings.extend(warnings)
            return ok(
                Capability.SENDERS, senders, detail=f"read from {self.senders_path.name}"
            )

        inline = self._inline_senders()
        if inline is None:
            if self._campaign is None:
                return misconfigured(
                    Capability.SENDERS, "no senders file supplied and campaign not loaded"
                )
            return ok(
                Capability.SENDERS,
                [],
                detail="the campaign file declares no senders",
            )
        return ok(
            Capability.SENDERS,
            inline,
            detail=f"{len(inline)} sender(s) declared in {self.campaign_path.name}",
        )

    async def get_sender_health(self, senders: list[Sender]) -> ProviderResult[list[Sender]]:
        """Health data only exists if the input supplied it -- never invented.

        A sender whose ``health_score`` is null stays null; the sender rules
        report that individual sender as UNKNOWN rather than assuming healthy.
        """
        if not senders:
            return unsupported(
                Capability.SENDER_HEALTH, "no senders to report health for"
            )
        scored = sum(1 for s in senders if s.health_score is not None)
        if scored == 0:
            return unsupported(
                Capability.SENDER_HEALTH,
                "no health_score values were supplied for any sender",
            )
        return ok(
            Capability.SENDER_HEALTH,
            senders,
            detail=f"health data supplied for {scored} of {len(senders)} sender(s)",
            partial=scored < len(senders),
        )

    async def list_suppressions(self) -> ProviderResult[list[SuppressionEntry]]:
        if self.suppressions_path is None:
            return misconfigured(
                Capability.SUPPRESSIONS,
                "no suppressions file supplied (pass --suppressions to enable these checks)",
            )
        try:
            entries, warnings = read_suppressions(self.suppressions_path)
        except InputError as exc:
            return failed(Capability.SUPPRESSIONS, str(exc))
        self.warnings.extend(warnings)
        return ok(
            Capability.SUPPRESSIONS,
            entries,
            detail=f"read {len(entries)} entries from {self.suppressions_path.name}",
        )

    async def list_evidence(self) -> ProviderResult[list[SourceEvidence]]:
        if self.evidence_path is None:
            return misconfigured(
                Capability.EVIDENCE,
                "no evidence file supplied (pass --evidence to enable claim checks)",
            )
        try:
            evidence, claims, warnings = read_evidence(self.evidence_path)
        except InputError as exc:
            return failed(Capability.EVIDENCE, str(exc))
        self.warnings.extend(warnings)
        self._claims = claims
        return ok(
            Capability.EVIDENCE,
            evidence,
            detail=f"read {len(evidence)} evidence records from {self.evidence_path.name}",
        )

    async def list_claims(self) -> ProviderResult[list[PersonalizationClaim]]:
        if self.evidence_path is None:
            return misconfigured(Capability.EVIDENCE, "no evidence file supplied")
        claims = self._claims
        if claims is None:
            try:
                _, claims, warnings = read_evidence(self.evidence_path)
            except InputError as exc:
                return failed(Capability.EVIDENCE, str(exc))
            self.warnings.extend(warnings)
        return ok(Capability.EVIDENCE, claims)

    async def health_check(self) -> ProviderResult[dict[str, Any]]:
        missing = [
            str(p)
            for p in (self.campaign_path, self.leads_path)
            if not Path(p).is_file()
        ]
        if missing:
            return failed(Capability.CAMPAIGN, f"missing input files: {', '.join(missing)}")
        return ok(Capability.CAMPAIGN, {"provider": self.name, "reachable": True})
