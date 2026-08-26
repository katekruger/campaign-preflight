"""Rule contract and registry.

A rule is a small, pure function of the context: it reads
:class:`~campaign_preflight.models.PreflightContext`, returns exactly one
:class:`~campaign_preflight.models.RuleResult`, and does nothing else. It never
calls a provider, never touches the filesystem, and cannot mutate its input --
the context is a frozen model.

The engine, not the rule, decides what happens when required data is missing:
if any capability in :attr:`Rule.requires` is not ``SUPPORTED_OK``, the rule is
short-circuited to ``UNKNOWN``. That is the mechanism behind the invariant
"missing data never becomes PASS".
"""

from __future__ import annotations

import abc
from typing import Any, ClassVar

from ..config import PreflightConfig, RuleOptions
from ..models import (
    Capability,
    CapabilityStatus,
    PreflightContext,
    RuleCategory,
    RuleResult,
    RuleStatus,
    Severity,
)

__all__ = [
    "Rule",
    "all_rules",
    "clear_registry",
    "get_rule",
    "known_rule_ids",
    "register",
    "rules_for_category",
]

_REGISTRY: dict[str, Rule] = {}


class Rule(abc.ABC):
    """Base class for every check."""

    rule_id: ClassVar[str] = ""
    version: ClassVar[str] = "1.0.0"
    title: ClassVar[str] = ""
    category: ClassVar[RuleCategory]
    severity: ClassVar[Severity] = Severity.MEDIUM
    requires: ClassVar[tuple[Capability, ...]] = ()
    description: ClassVar[str] = ""
    remediation: ClassVar[str] = ""
    heuristic: ClassVar[bool] = False
    """Set on rules that encode a judgement call rather than a hard fact.

    Heuristic rules are labelled as such everywhere they appear, and should not
    be given BLOCKER severity by default.
    """

    options_model: ClassVar[type[RuleOptions]] = RuleOptions

    # -- contract ----------------------------------------------------------

    @abc.abstractmethod
    def evaluate(
        self,
        ctx: PreflightContext,
        options: RuleOptions,
        config: PreflightConfig,
    ) -> RuleResult:
        """Return this rule's single result. Must not mutate ``ctx``."""

    # -- result helpers ----------------------------------------------------

    def build(
        self,
        status: RuleStatus,
        summary: str,
        *,
        severity: Severity | None = None,
        explanation: str = "",
        affected: int = 0,
        samples: tuple[str, ...] | list[str] = (),
        evidence: tuple[str, ...] | list[str] = (),
        remediation: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RuleResult:
        """Construct a RuleResult carrying this rule's identity."""
        return RuleResult(
            rule_id=self.rule_id,
            rule_version=self.version,
            title=self.title,
            category=self.category,
            severity=severity or self.severity,
            status=status,
            summary=summary,
            explanation=explanation or self.description,
            affected_record_count=affected,
            affected_record_samples=tuple(samples),
            evidence=tuple(evidence),
            remediation=(
                remediation
                if remediation is not None
                else (self.remediation if status in _ACTIONABLE else "")
            ),
            metadata=dict(metadata or {}),
            heuristic=self.heuristic,
        )

    def passed(self, summary: str, **kwargs: Any) -> RuleResult:
        return self.build(RuleStatus.PASS, summary, **kwargs)

    def warn(self, summary: str, **kwargs: Any) -> RuleResult:
        return self.build(RuleStatus.WARN, summary, **kwargs)

    def failed(self, summary: str, **kwargs: Any) -> RuleResult:
        return self.build(RuleStatus.FAIL, summary, **kwargs)

    def unknown(self, summary: str, **kwargs: Any) -> RuleResult:
        kwargs.setdefault("remediation", "")
        return self.build(RuleStatus.UNKNOWN, summary, **kwargs)

    def not_applicable(self, summary: str, **kwargs: Any) -> RuleResult:
        kwargs.setdefault("remediation", "")
        return self.build(RuleStatus.NOT_APPLICABLE, summary, **kwargs)

    # -- shared utilities --------------------------------------------------

    @staticmethod
    def sample(labels: list[str], limit: int) -> tuple[str, ...]:
        """A bounded, deterministic sample of affected-record labels.

        Sorted so two runs over the same data produce identical reports, and
        capped so a 100k-lead campaign cannot emit 100k lines.
        """
        if limit <= 0:
            return ()
        return tuple(sorted(set(labels))[:limit])

    @staticmethod
    def ratio(count: int, total: int) -> float:
        return (count / total) if total else 0.0

    def missing_capability_result(
        self, ctx: PreflightContext, capability: Capability
    ) -> RuleResult:
        """The UNKNOWN result used when a required capability is unavailable."""
        status = ctx.capability_status(capability)
        detail = ctx.capability_detail(capability) or "no detail supplied"
        reason = _CAPABILITY_REASONS.get(status, "is unavailable")
        return self.unknown(
            f"Not checked: {capability.value} data {reason}.",
            explanation=(
                f"This rule needs {capability.value} data. The provider reported "
                f"{status.value}: {detail}. Treating this as UNKNOWN rather than a "
                f"pass, because an unchecked campaign is not a safe campaign."
            ),
            metadata={"capability": capability.value, "capability_status": status.value},
        )


_ACTIONABLE = frozenset({RuleStatus.FAIL, RuleStatus.WARN})

_CAPABILITY_REASONS: dict[CapabilityStatus, str] = {
    CapabilityStatus.SUPPORTED_FAILED: "could not be retrieved",
    CapabilityStatus.UNSUPPORTED: "is not available from this provider",
    CapabilityStatus.UNAVAILABLE_PERMISSIONS: "is blocked by the current credentials",
    CapabilityStatus.UNAVAILABLE_CONFIG: "was not supplied",
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def register(rule_cls: type[Rule]) -> type[Rule]:
    """Class decorator that adds a rule to the global registry."""
    if not rule_cls.rule_id:
        raise ValueError(f"{rule_cls.__name__} must define a rule_id")
    if not rule_cls.title:
        raise ValueError(f"{rule_cls.rule_id} must define a title")
    if rule_cls.rule_id in _REGISTRY:
        raise ValueError(f"duplicate rule id: {rule_cls.rule_id}")
    _REGISTRY[rule_cls.rule_id] = rule_cls()
    return rule_cls


def _ensure_loaded() -> None:
    """Import every rule module exactly once, populating the registry."""
    if _REGISTRY:
        return
    from . import (  # noqa: F401  - imported for their registration side effects
        campaign,
        contacts,
        copy,
        personalization,
        schedule,
        senders,
        suppression,
    )


def all_rules() -> tuple[Rule, ...]:
    """Every registered rule, ordered by id so output is deterministic."""
    _ensure_loaded()
    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))


def get_rule(rule_id: str) -> Rule:
    _ensure_loaded()
    try:
        return _REGISTRY[rule_id]
    except KeyError:
        raise KeyError(f"unknown rule id: {rule_id}") from None


def known_rule_ids() -> frozenset[str]:
    _ensure_loaded()
    return frozenset(_REGISTRY)


def rules_for_category(category: RuleCategory) -> tuple[Rule, ...]:
    return tuple(r for r in all_rules() if r.category is category)


def clear_registry() -> None:
    """Test hook. Never call this from library code."""
    _REGISTRY.clear()
