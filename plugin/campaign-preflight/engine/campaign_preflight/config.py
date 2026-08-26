"""Versioned, strictly-validated configuration for the rule engine.

A config file looks like this::

    version: 1
    settings:
      target_timezone: America/New_York
      required_variables: [first_name, company_name]
    rules:
      campaign.daily_volume:
        enabled: true
        warning_above: 100
        blocker_above: 250

Validation is intentionally unforgiving. An unknown rule id, an unknown option
inside a rule, or an unsupported ``version`` is a hard error -- a silently
ignored typo in a safety config is worse than no config at all.

Implemented with dataclasses and the bundled YAML parser, so the package has no
third-party dependencies.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Type, TypeVar

from . import _yaml
from .errors import ConfigurationError, InputError
from .models import Severity, as_tuple

__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "RuleOptions",
    "GlobalSettings",
    "ScoringConfig",
    "EvidenceConfig",
    "PreflightConfig",
    "load_config",
    "load_config_document",
    "safe_resolve",
    "option_defaults",
]

CONFIG_SCHEMA_VERSION = 1
SUPPORTED_CONFIG_VERSIONS = frozenset({1})

# A campaign config or rule config should never be megabytes. Refusing early
# beats an out-of-memory parse of a hostile file.
MAX_CONFIG_BYTES = 2 * 1024 * 1024

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Coercion and validation helpers
# ---------------------------------------------------------------------------


def _coerce(value: Any, annotation: Any, where: str) -> Any:
    """Coerce a parsed YAML value to the declared field type, or raise.

    Deliberately narrow: it accepts the conversions a YAML document legitimately
    produces (a list where a tuple is declared, an int where a float is) and
    rejects everything else rather than guessing.
    """
    text = str(annotation)

    if "Optional" in text or "None" in text:
        if value is None:
            return None
        inner = text.replace("Optional[", "").rstrip("]")
        return _coerce(value, inner, where)

    if text.startswith(("Tuple", "tuple")):
        if not isinstance(value, (list, tuple)):
            raise ConfigurationError(f"{where}: expected a list, got {type(value).__name__}")
        return tuple(str(v) for v in value)

    if text.startswith(("Dict", "dict", "Mapping")):
        if not isinstance(value, dict):
            raise ConfigurationError(f"{where}: expected a mapping, got {type(value).__name__}")
        return dict(value)

    if "Severity" in text:
        if isinstance(value, Severity):
            return value
        try:
            return Severity(str(value).strip().upper())
        except ValueError:
            allowed = ", ".join(s.value for s in Severity)
            raise ConfigurationError(f"{where}: unknown severity {value!r}; allowed: {allowed}")

    if "bool" in text:
        if isinstance(value, bool):
            return value
        raise ConfigurationError(f"{where}: expected true or false, got {value!r}")

    if "float" in text:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigurationError(f"{where}: expected a number, got {value!r}")
        return float(value)

    if "int" in text:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError(f"{where}: expected a whole number, got {value!r}")
        return value

    if "str" in text:
        if not isinstance(value, str):
            raise ConfigurationError(f"{where}: expected text, got {type(value).__name__}")
        return value

    return value


def _build(model: Type[T], payload: Mapping[str, Any], where: str) -> T:
    """Construct a config dataclass from a mapping, rejecting unknown keys."""
    if not isinstance(payload, Mapping):
        raise ConfigurationError(f"{where}: expected a mapping, got {type(payload).__name__}")

    known = {f.name: f for f in fields(model)}  # type: ignore[arg-type]
    unknown = sorted(set(payload) - set(known))
    if unknown:
        suggestion = _closest(unknown[0], frozenset(known))
        hint = f"did you mean '{suggestion}'?" if suggestion else f"allowed: {', '.join(known)}"
        raise ConfigurationError(f"{where}: unknown option '{unknown[0]}'", hint=hint)

    values: Dict[str, Any] = {}
    for name, value in payload.items():
        values[name] = _coerce(value, known[name].type, f"{where}.{name}")
    return model(**values)  # type: ignore[call-arg]


def _closest(value: str, candidates: frozenset) -> Optional[str]:
    import difflib

    matches = difflib.get_close_matches(value, sorted(candidates), n=1, cutoff=0.7)
    return matches[0] if matches else None


def option_defaults(model: type) -> Dict[str, Any]:
    """The default value of every option on a rule's options model."""
    defaults: Dict[str, Any] = {}
    for f in fields(model):  # type: ignore[arg-type]
        if f.default is not dataclasses.MISSING:
            defaults[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            defaults[f.name] = f.default_factory()  # type: ignore[misc]
        else:  # pragma: no cover - every option has a default
            defaults[f.name] = None
    return defaults


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleOptions:
    """Base options every rule accepts. Rules subclass this to add their own."""

    enabled: bool = True
    severity: Optional[Severity] = None
    """Override the rule's declared severity. ``None`` keeps the default."""


@dataclass(frozen=True)
class GlobalSettings:
    """Organization policy that several rules read.

    These encode *your* outreach policy, not law. Campaign Preflight makes no
    legal-compliance claim; see ``docs/limitations.md``.
    """

    target_timezone: Optional[str] = None
    """The timezone you expect the campaign to send in. Used by
    ``schedule.timezone_mismatch``; unset means the check is not applicable."""

    business_hours_start: str = "08:00"
    business_hours_end: str = "18:00"
    allow_weekend_sending: bool = False

    required_variables: Tuple[str, ...] = ("first_name",)
    """Template variables every lead must have a value for."""

    internal_domains: Tuple[str, ...] = ()
    competitor_domains: Tuple[str, ...] = ()
    customer_domains: Tuple[str, ...] = ()
    restricted_regions: Tuple[str, ...] = ()
    """Region or country codes your organization has chosen not to contact."""

    allow_free_email_domains: bool = False
    allow_role_addresses: bool = False

    opt_out_phrases: Tuple[str, ...] = (
        "unsubscribe",
        "opt out",
        "opt-out",
        "stop receiving",
        "no longer wish",
        "reply stop",
        "remove me",
    )

    max_samples: int = 5
    """How many affected records appear in a report. Bounded on purpose."""

    def __post_init__(self) -> None:
        if not 0 <= self.max_samples <= 100:
            raise ConfigurationError(
                f"settings.max_samples must be between 0 and 100, got {self.max_samples}"
            )
        for name in ("internal_domains", "competitor_domains", "customer_domains"):
            cleaned = tuple(
                sorted({d.strip().lower().lstrip("@") for d in getattr(self, name) if d.strip()})
            )
            object.__setattr__(self, name, cleaned)
        object.__setattr__(
            self,
            "restricted_regions",
            tuple(sorted({r.strip().upper() for r in self.restricted_regions if r.strip()})),
        )
        for name in ("required_variables", "opt_out_phrases"):
            object.__setattr__(self, name, as_tuple(getattr(self, name)))


@dataclass(frozen=True)
class ScoringConfig:
    """Weights for the readiness score. Published, not hidden."""

    fail_weights: Dict[Severity, float] = field(
        default_factory=lambda: {
            Severity.BLOCKER: 30.0,
            Severity.HIGH: 15.0,
            Severity.MEDIUM: 7.0,
            Severity.LOW: 3.0,
            Severity.INFO: 0.0,
        }
    )
    warn_weights: Dict[Severity, float] = field(
        default_factory=lambda: {
            Severity.BLOCKER: 10.0,
            Severity.HIGH: 6.0,
            Severity.MEDIUM: 3.0,
            Severity.LOW: 1.0,
            Severity.INFO: 0.0,
        }
    )
    high_failure_blocks: bool = True
    """When true, any HIGH-severity FAIL forces NOT_READY."""

    critical_rules: Tuple[str, ...] = (
        "campaign.exists",
        "campaign.has_steps",
        "campaign.has_senders",
        "campaign.has_leads",
        "suppression.contact_listed",
        "senders.health_below_threshold",
    )
    """Rules whose ``UNKNOWN`` makes the whole run INCOMPLETE."""

    unknown_confidence_penalty: float = 1.0
    """Confidence, not score, degrades per UNKNOWN. See ``docs/rules.md``."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "critical_rules", as_tuple(self.critical_rules))
        for name in ("fail_weights", "warn_weights"):
            raw = getattr(self, name)
            resolved: Dict[Severity, float] = {}
            for key, value in raw.items():
                severity = key if isinstance(key, Severity) else Severity(str(key).upper())
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ConfigurationError(
                        f"scoring.{name}.{severity.value}: expected a number, got {value!r}"
                    )
                if value < 0:
                    raise ConfigurationError(
                        f"scoring.{name}.{severity.value}: weight cannot be negative"
                    )
                resolved[severity] = float(value)
            # Unspecified severities keep their default weight.
            defaults = (
                _DEFAULT_FAIL_WEIGHTS if name == "fail_weights" else _DEFAULT_WARN_WEIGHTS
            )
            object.__setattr__(self, name, {**defaults, **resolved})


_DEFAULT_FAIL_WEIGHTS: Dict[Severity, float] = {
    Severity.BLOCKER: 30.0,
    Severity.HIGH: 15.0,
    Severity.MEDIUM: 7.0,
    Severity.LOW: 3.0,
    Severity.INFO: 0.0,
}
_DEFAULT_WARN_WEIGHTS: Dict[Severity, float] = {
    Severity.BLOCKER: 10.0,
    Severity.HIGH: 6.0,
    Severity.MEDIUM: 3.0,
    Severity.LOW: 1.0,
    Severity.INFO: 0.0,
}


@dataclass(frozen=True)
class EvidenceConfig:
    """Claim/evidence checking. Off-network by default."""

    max_age_days: int = 180
    evaluator: str = "disabled"
    """``disabled`` | ``fixture`` | ``openai_compatible``. Default sends nothing."""

    evaluator_model: Optional[str] = None
    evaluator_prompt_version: str = "v1"
    max_claims_evaluated: int = 25
    """Hard cap on how many claims may ever leave the machine."""

    def __post_init__(self) -> None:
        if self.max_age_days < 1:
            raise ConfigurationError("evidence.max_age_days must be at least 1")
        if self.max_claims_evaluated < 0:
            raise ConfigurationError("evidence.max_claims_evaluated cannot be negative")
        allowed = {"disabled", "fixture", "openai_compatible"}
        if self.evaluator not in allowed:
            raise ConfigurationError(
                f"evidence.evaluator must be one of {', '.join(sorted(allowed))}, "
                f"got {self.evaluator!r}"
            )


@dataclass(frozen=True)
class PreflightConfig:
    """The fully-resolved configuration for one run."""

    version: int = CONFIG_SCHEMA_VERSION
    settings: GlobalSettings = field(default_factory=GlobalSettings)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    rules: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    source_path: Optional[str] = None

    def __post_init__(self) -> None:
        if self.version not in SUPPORTED_CONFIG_VERSIONS:
            supported = ", ".join(str(s) for s in sorted(SUPPORTED_CONFIG_VERSIONS))
            raise ConfigurationError(
                f"unsupported config version {self.version!r}; supported: {supported}"
            )
        # Accept mappings supplied as plain dicts (the common case from YAML).
        for name, model in (
            ("settings", GlobalSettings),
            ("scoring", ScoringConfig),
            ("evidence", EvidenceConfig),
        ):
            value = getattr(self, name)
            if isinstance(value, dict):
                object.__setattr__(self, name, _build(model, value, name))

    def raw_options(self, rule_id: str) -> Dict[str, Any]:
        return dict(self.rules.get(rule_id, {}))

    def options_for(self, rule_id: str, options_model: type) -> RuleOptions:
        """Build a validated options object for one rule."""
        return _build(options_model, self.raw_options(rule_id), f"rules.{rule_id}")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def safe_resolve(path: "Path | str") -> Path:
    """Resolve a user-supplied path, refusing to traverse a symlink chain.

    Campaign Preflight only ever reads paths the user typed, so this is a
    defense-in-depth measure rather than a sandbox: it stops a symlink planted
    inside a shared examples directory from redirecting a read elsewhere.
    """
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:  # pragma: no cover - platform dependent
        raise InputError(f"path could not be resolved: {path} ({exc})") from exc
    if candidate.is_symlink() and os.environ.get("CAMPAIGN_PREFLIGHT_ALLOW_SYMLINKS") != "1":
        raise InputError(
            f"refusing to follow symlink: {path}",
            hint="set CAMPAIGN_PREFLIGHT_ALLOW_SYMLINKS=1 if this is intended",
        )
    return resolved


def read_document(path: "Path | str", *, what: str, max_bytes: int = MAX_CONFIG_BYTES) -> Any:
    """Read and parse a YAML/JSON document with size and encoding guards."""
    resolved = safe_resolve(path)
    if not resolved.is_file():
        raise InputError(f"{what} not found: {path}")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise InputError(
            f"{what} is {size} bytes, above the {max_bytes}-byte limit",
            hint="split the file if this is legitimate",
        )
    try:
        text = resolved.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(f"{what} is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise InputError(f"{what} could not be read: {path} ({exc.strerror})") from exc
    try:
        return _yaml.safe_load(text)
    except _yaml.YamlError as exc:
        raise InputError(f"{what} is not valid YAML/JSON: {path} ({exc})") from exc


def load_config_document(document: Any, *, source: Optional[str] = None) -> PreflightConfig:
    """Validate an already-parsed config mapping.

    Rule ids are checked against the live registry, so a typo like
    ``contacts.missing_firstname`` fails loudly instead of doing nothing.
    """
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ConfigurationError(
            f"config must be a mapping at the top level, got {type(document).__name__}"
        )

    known = {f.name for f in fields(PreflightConfig)} - {"source_path"}
    unknown = sorted(set(document) - known)
    if unknown:
        suggestion = _closest(unknown[0], frozenset(known))
        hint = f"did you mean '{suggestion}'?" if suggestion else f"allowed: {', '.join(sorted(known))}"
        raise ConfigurationError(f"unknown configuration key '{unknown[0]}'", hint=hint)

    version = document.get("version", CONFIG_SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ConfigurationError(f"config version must be a whole number, got {version!r}")

    rules = document.get("rules", {})
    if not isinstance(rules, dict):
        raise ConfigurationError(
            f"'rules' must map rule ids to options, got {type(rules).__name__}"
        )
    for rule_id, options in rules.items():
        if not isinstance(options, dict):
            raise ConfigurationError(
                f"rules.{rule_id}: expected a mapping of options, got "
                f"{type(options).__name__}"
            )

    config = PreflightConfig(
        version=version,
        settings=_build(GlobalSettings, document.get("settings", {}) or {}, "settings"),
        scoring=_build(ScoringConfig, document.get("scoring", {}) or {}, "scoring"),
        evidence=_build(EvidenceConfig, document.get("evidence", {}) or {}, "evidence"),
        rules={str(k): dict(v) for k, v in rules.items()},
        source_path=source,
    )

    _validate_rule_ids(config)
    return config


def _validate_rule_ids(config: PreflightConfig) -> None:
    """Reject unknown rule ids and unknown per-rule options."""
    from .rules import get_rule, known_rule_ids  # local import: avoids a cycle

    known = known_rule_ids()
    for rule_id in sorted(config.rules):
        if rule_id not in known:
            suggestion = _closest(rule_id, known)
            hint = f"did you mean '{suggestion}'?" if suggestion else None
            raise ConfigurationError(f"unknown rule id '{rule_id}'", hint=hint)
        # Round-trips the options through the rule's own model so unknown keys
        # and out-of-range values surface at load time, not mid-run.
        config.options_for(rule_id, get_rule(rule_id).options_model)


def load_config(path: "Path | str | None") -> PreflightConfig:
    """Load a config file, or return defaults when ``path`` is ``None``."""
    if path is None:
        return PreflightConfig()
    document = read_document(path, what="config file")
    return load_config_document(document, source=str(path))
