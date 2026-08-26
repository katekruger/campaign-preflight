"""Typed domain models for Campaign Preflight.

Everything downstream of a provider speaks these models, so the rule engine never
sees a provider-shaped dict. Provider quirks (Instantly's integer status enums,
CSV header aliases, ``null`` in required-looking fields) are resolved during
normalization, and anything that could not be resolved is represented explicitly
rather than defaulted away.

Implemented with frozen dataclasses and no third-party dependencies: Campaign
Preflight ships inside a Cowork plugin, where the only guaranteed runtime is the
system ``python3`` with nothing installed alongside it.

Every model is frozen, so "a rule never mutates its input" is enforced by the
runtime rather than by convention. All datetimes are timezone-aware UTC.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field, fields
from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "REPORT_SCHEMA_VERSION",
    "utcnow",
    "as_tuple",
    "as_frozenset",
    "to_builtin",
    "RuleStatus",
    "Severity",
    "SEVERITY_ORDER",
    "Readiness",
    "Confidence",
    "RuleCategory",
    "CapabilityStatus",
    "Capability",
    "CapabilityReport",
    "SourceEvidence",
    "PersonalizationClaim",
    "SendingWindow",
    "CampaignSchedule",
    "CampaignStep",
    "Sender",
    "Lead",
    "SuppressionEntry",
    "ProviderMetadata",
    "Campaign",
    "PreflightContext",
    "RuleResult",
    "ScoreDeduction",
    "ScoreBreakdown",
    "PreflightReport",
]

REPORT_SCHEMA_VERSION = "1.0.0"
"""Version of the JSON report envelope. Bumped on any breaking field change."""


def utcnow() -> datetime:
    """Timezone-aware current UTC time (the only clock this package reads)."""
    return datetime.now(timezone.utc)


def as_tuple(value: Any) -> Tuple[Any, ...]:
    """Coerce a sequence to a tuple so a model field cannot be mutated in place."""
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, (list, set, frozenset)):
        return tuple(value)
    return (value,)


def as_frozenset(value: Any) -> frozenset:
    if value is None:
        return frozenset()
    if isinstance(value, frozenset):
        return value
    if isinstance(value, (set, list, tuple)):
        return frozenset(value)
    return frozenset({value})


def _freeze(instance: Any, name: str, value: Any) -> None:
    """Assign to a frozen dataclass field during ``__post_init__``."""
    object.__setattr__(instance, name, value)


def to_builtin(value: Any) -> Any:
    """Recursively convert a model into JSON-safe builtins.

    Used by the reporters and by the test that proves a rule did not mutate the
    context it was handed.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_builtin(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): to_builtin(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_builtin(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(to_builtin(v) for v in value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    return value


def to_json(value: Any) -> str:
    """Deterministic JSON for a model. Key order is sorted, so it is comparable."""
    return json.dumps(to_builtin(value), sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RuleStatus(str, Enum):
    """Outcome of a single rule.

    ``UNKNOWN`` is load-bearing: it means the rule could not be evaluated, which
    is never the same thing as passing.
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Severity(str, Enum):
    """How much a failing rule matters."""

    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    BLOCKER = "BLOCKER"


SEVERITY_ORDER: Dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.BLOCKER: 4,
}


class Readiness(str, Enum):
    """The overall verdict."""

    READY = "READY"
    READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
    NOT_READY = "NOT_READY"
    INCOMPLETE = "INCOMPLETE"


class Confidence(str, Enum):
    """How much of the intended check surface actually ran."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RuleCategory(str, Enum):
    """Rule grouping, used for report sections and ``rules list`` filtering."""

    CAMPAIGN = "campaign"
    CONTACTS = "contacts"
    SUPPRESSION = "suppression"
    PERSONALIZATION = "personalization"
    COPY = "copy"
    SCHEDULE = "schedule"
    SENDERS = "senders"


class CapabilityStatus(str, Enum):
    """Why a provider did or did not supply a piece of data.

    ``SUPPORTED_OK`` with an empty list means "genuinely nothing there".
    Every other value means "we do not know what is there", and rules that
    depend on the capability must return ``UNKNOWN``.
    """

    SUPPORTED_OK = "SUPPORTED_OK"
    SUPPORTED_FAILED = "SUPPORTED_FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    UNAVAILABLE_PERMISSIONS = "UNAVAILABLE_PERMISSIONS"
    UNAVAILABLE_CONFIG = "UNAVAILABLE_CONFIG"

    @property
    def is_ok(self) -> bool:
        return self is CapabilityStatus.SUPPORTED_OK


class Capability(str, Enum):
    """A unit of data a rule may require from a provider."""

    CAMPAIGN = "campaign"
    LEADS = "leads"
    SENDERS = "senders"
    SENDER_HEALTH = "sender_health"
    SUPPRESSIONS = "suppressions"
    EVIDENCE = "evidence"
    ANALYTICS = "analytics"


@dataclass(frozen=True)
class CapabilityReport:
    """Per-capability outcome, carried into the report's ``limitations``."""

    capability: Capability
    status: CapabilityStatus
    detail: Optional[str] = None
    record_count: Optional[int] = None

    @property
    def is_ok(self) -> bool:
        return self.status.is_ok


# ---------------------------------------------------------------------------
# Evidence and claims
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceEvidence:
    """A citation backing a personalization claim.

    ``lead_ref`` is either a lead id or a hashed email; raw emails are never
    required here so evidence files can be shared without exposing contacts.
    """

    evidence_id: str
    lead_ref: Optional[str] = None
    source_url: Optional[str] = None
    title: Optional[str] = None
    retrieved_at: Optional[datetime] = None
    excerpt: str = ""
    content_hash: Optional[str] = None
    company_name: Optional[str] = None

    def __post_init__(self) -> None:
        if self.retrieved_at is not None and self.retrieved_at.tzinfo is None:
            _freeze(self, "retrieved_at", self.retrieved_at.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class PersonalizationClaim:
    """A factual assertion made about a lead inside personalization text."""

    claim_id: str
    lead_ref: str
    text: str
    evidence_ids: Tuple[str, ...] = ()
    numeric_values: Tuple[str, ...] = ()
    source_field: str = "personalization"

    def __post_init__(self) -> None:
        _freeze(self, "evidence_ids", as_tuple(self.evidence_ids))
        _freeze(self, "numeric_values", as_tuple(self.numeric_values))


# ---------------------------------------------------------------------------
# Campaign structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendingWindow:
    """One named sending window within a campaign schedule."""

    name: str = "default"
    start: Optional[time] = None
    end: Optional[time] = None
    days: frozenset = field(default_factory=frozenset)
    """Active weekday numbers, ISO-style 0=Sunday .. 6=Saturday."""
    timezone_name: Optional[str] = None
    raw_timezone: Optional[str] = None

    def __post_init__(self) -> None:
        _freeze(self, "days", as_frozenset(self.days))

    @property
    def crosses_midnight(self) -> bool:
        return self.start is not None and self.end is not None and self.end <= self.start


@dataclass(frozen=True)
class CampaignSchedule:
    """When the campaign is allowed to send."""

    start_date: Optional[date] = None
    end_date: Optional[date] = None
    windows: Tuple[SendingWindow, ...] = ()
    timezone_name: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _freeze(self, "windows", as_tuple(self.windows))


@dataclass(frozen=True)
class CampaignStep:
    """One step of the sequence. Variants are flattened into subject/body pairs."""

    index: int
    step_type: str = "email"
    delay: Optional[float] = None
    delay_unit: Optional[str] = None
    subject: str = ""
    body: str = ""
    variant_index: int = 0
    disabled: bool = False


@dataclass(frozen=True)
class Sender:
    """A sending mailbox attached to the campaign."""

    email: str
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    status_label: Optional[str] = None
    status_is_error: Optional[bool] = None
    daily_limit: Optional[int] = None
    health_score: Optional[float] = None
    warmup_status: Optional[str] = None
    setup_pending: Optional[bool] = None
    provider: Optional[str] = None
    raw_status: Any = None


@dataclass(frozen=True)
class Lead:
    """A normalized contact. Every optional field may legitimately be absent."""

    id: Optional[str] = None
    email: Optional[str] = None
    normalized_email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None
    company_domain: Optional[str] = None
    job_title: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    personalization: Optional[str] = None
    custom_variables: Dict[str, str] = field(default_factory=dict)
    assigned_sender: Optional[str] = None
    source_row: Optional[int] = None
    source_name: Optional[str] = None
    suppressed: Optional[bool] = None
    status_label: Optional[str] = None

    @property
    def label(self) -> str:
        """A stable human reference used in affected-record samples."""
        return self.email or self.id or (f"row {self.source_row}" if self.source_row else "?")

    @property
    def email_domain(self) -> Optional[str]:
        if self.normalized_email and "@" in self.normalized_email:
            return self.normalized_email.rsplit("@", 1)[1]
        return None


@dataclass(frozen=True)
class SuppressionEntry:
    """One suppression-list entry: an address, a domain, or both."""

    value: str
    is_domain: bool = False
    reason: Optional[str] = None
    source: Optional[str] = None


@dataclass(frozen=True)
class ProviderMetadata:
    """Which provider produced this context, and what it could see."""

    name: str
    version: Optional[str] = None
    base_url: Optional[str] = None
    read_only: bool = True
    capabilities: Tuple[CapabilityReport, ...] = ()
    errors: Tuple[str, ...] = ()
    fetched_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        _freeze(self, "capabilities", as_tuple(self.capabilities))
        _freeze(self, "errors", as_tuple(self.errors))

    def capability(self, cap: Capability) -> Optional[CapabilityReport]:
        for report in self.capabilities:
            if report.capability is cap:
                return report
        return None


@dataclass(frozen=True)
class Campaign:
    """The campaign under inspection, provider-agnostic."""

    id: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    raw_status: Any = None
    timezone_name: Optional[str] = None
    schedule: Optional[CampaignSchedule] = None
    daily_limit: Optional[int] = None
    stop_on_reply: Optional[bool] = None
    stop_on_auto_reply: Optional[bool] = None
    steps: Tuple[CampaignStep, ...] = ()
    sender_emails: Tuple[str, ...] = ()
    custom_variables: Dict[str, Any] = field(default_factory=dict)
    lead_count_hint: Optional[int] = None
    provider_metadata: Optional[ProviderMetadata] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _freeze(self, "steps", as_tuple(self.steps))
        _freeze(self, "sender_emails", as_tuple(self.sender_emails))


# ---------------------------------------------------------------------------
# Engine input / output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightContext:
    """Everything a rule is allowed to look at.

    Frozen by construction, so "a rule never mutates its input" is enforced by
    the runtime rather than by convention.
    """

    provider: ProviderMetadata
    campaign: Optional[Campaign] = None
    leads: Tuple[Lead, ...] = ()
    senders: Tuple[Sender, ...] = ()
    suppressions: Tuple[SuppressionEntry, ...] = ()
    evidence: Tuple[SourceEvidence, ...] = ()
    claims: Tuple[PersonalizationClaim, ...] = ()
    analytics: Dict[str, Any] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=utcnow)
    input_warnings: Tuple[str, ...] = ()
    lead_total_hint: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("leads", "senders", "suppressions", "evidence", "claims", "input_warnings"):
            _freeze(self, name, as_tuple(getattr(self, name)))

    def capability_status(self, cap: Capability) -> CapabilityStatus:
        report = self.provider.capability(cap)
        return report.status if report else CapabilityStatus.UNSUPPORTED

    def capability_detail(self, cap: Capability) -> Optional[str]:
        report = self.provider.capability(cap)
        return report.detail if report else None

    def has(self, cap: Capability) -> bool:
        return self.capability_status(cap).is_ok

    def state_signature(self) -> str:
        """A deterministic serialization, used to prove a rule did not mutate it."""
        return to_json(self)


@dataclass(frozen=True)
class RuleResult:
    """The single output of a single rule."""

    rule_id: str
    rule_version: str
    title: str
    category: RuleCategory
    severity: Severity
    status: RuleStatus
    summary: str
    explanation: str = ""
    affected_record_count: int = 0
    affected_record_samples: Tuple[str, ...] = ()
    evidence: Tuple[str, ...] = ()
    remediation: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    heuristic: bool = False
    """True when the rule encodes a judgement call rather than a hard fact."""

    def __post_init__(self) -> None:
        _freeze(self, "affected_record_samples", as_tuple(self.affected_record_samples))
        _freeze(self, "evidence", as_tuple(self.evidence))

    @property
    def is_blocking(self) -> bool:
        return self.status is RuleStatus.FAIL and self.severity is Severity.BLOCKER

    def with_severity(self, severity: Severity) -> "RuleResult":
        return dataclasses.replace(self, severity=severity)


@dataclass(frozen=True)
class ScoreDeduction:
    """One line item in the score arithmetic, so the total is auditable."""

    rule_id: str
    status: RuleStatus
    severity: Severity
    points: float
    reason: str


@dataclass(frozen=True)
class ScoreBreakdown:
    """The full, printable derivation of the readiness score."""

    starting_score: float = 100.0
    deductions: Tuple[ScoreDeduction, ...] = ()
    final_score: int = 100
    confidence: Confidence = Confidence.HIGH
    excluded_rule_ids: Tuple[str, ...] = ()
    critical_unknown_rule_ids: Tuple[str, ...] = ()
    explanation: str = ""

    def __post_init__(self) -> None:
        for name in ("deductions", "excluded_rule_ids", "critical_unknown_rule_ids"):
            _freeze(self, name, as_tuple(getattr(self, name)))


@dataclass(frozen=True)
class PreflightReport:
    """The complete, serializable result of one preflight run."""

    tool_version: str
    generated_at: datetime
    provider: str
    readiness: Readiness
    score: int
    score_breakdown: ScoreBreakdown
    confidence: Confidence
    report_schema_version: str = REPORT_SCHEMA_VERSION
    provider_read_only: bool = True
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    campaign_status: Optional[str] = None
    lead_count: int = 0
    lead_count_is_partial: bool = False
    sender_count: int = 0
    suppression_count: int = 0
    results: Tuple[RuleResult, ...] = ()
    blocker_count: int = 0
    warning_count: int = 0
    failure_count: int = 0
    unknown_count: int = 0
    passed_count: int = 0
    not_applicable_count: int = 0
    limitations: Tuple[str, ...] = ()
    provider_errors: Tuple[str, ...] = ()
    redacted: bool = True
    duration_seconds: float = 0.0
    snapshot_note: str = (
        "Point-in-time snapshot. Campaign state may change after this check ran."
    )

    def __post_init__(self) -> None:
        for name in ("results", "limitations", "provider_errors"):
            _freeze(self, name, as_tuple(getattr(self, name)))

    def results_by_status(self, status: RuleStatus) -> Tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.status is status)

    @property
    def blockers(self) -> Tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.is_blocking)

    @property
    def warnings(self) -> Tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.status is RuleStatus.WARN)

    @property
    def failures(self) -> Tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.status is RuleStatus.FAIL)

    @property
    def unknowns(self) -> Tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.status is RuleStatus.UNKNOWN)
