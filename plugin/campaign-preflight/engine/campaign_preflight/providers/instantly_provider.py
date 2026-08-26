"""Read-only Instantly.ai v2 provider.

Endpoints and field names here were taken from the official v2 reference at
https://developer.instantly.ai/ and are listed in ``docs/instantly.md`` with the
exact path each check depends on. Nothing is inferred: where the documentation
does not define an enum's labels (lead ``status``, for example), this module
declines to invent them and derives what it can from documented fields instead.

Safety properties:

* Every request goes through :class:`ReadOnlyTransport`. No write is reachable.
* Retries apply to reads only, are bounded, and honour ``Retry-After``.
* Errors never carry the API key: the key lives in a header this module never
  formats into a message, and error text is passed through ``redact_secrets``.
* Pagination stops on a repeated cursor, an empty page, or a page cap, so a
  looping provider cannot spin forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
from typing import Any, Final

import httpx

from .. import __version__
from ..errors import ProviderAuthError, ProviderError, redact_secrets
from ..models import (
    Campaign,
    CampaignSchedule,
    CampaignStep,
    Capability,
    Lead,
    Sender,
    SendingWindow,
    SuppressionEntry,
)
from ..normalization import (
    coerce_int,
    normalize_domain,
    normalize_email,
    normalize_text,
    parse_clock_time,
)
from .base import (
    CampaignProvider,
    ProviderResult,
    failed,
    forbidden,
    misconfigured,
    ok,
    unsupported,
)
from .instantly_transport import ReadOnlyTransport, ReadOnlyViolation

__all__ = ["ACCOUNT_STATUS", "CAMPAIGN_STATUS", "WARMUP_STATUS", "InstantlyProvider"]

logger = logging.getLogger("campaign_preflight.instantly")

DEFAULT_BASE_URL: Final = "https://api.instantly.ai"
API_PREFIX: Final = "/api/v2"
MAX_PAGE_SIZE: Final = 100  # documented ceiling on v2 list endpoints
DEFAULT_PAGE_CAP: Final = 100  # 100 pages x 100 rows = 10,000 records
MAX_RETRIES: Final = 3
MAX_RETRY_SLEEP: Final = 30.0
SENDER_CONCURRENCY: Final = 3
MAX_SENDER_LOOKUPS: Final = 200

USER_AGENT: Final = (
    f"campaign-preflight/{__version__} "
    f"(+https://github.com/katekruger/campaign-preflight; read-only)"
)

# Documented v2 enums. Values outside these maps are reported verbatim as
# "unknown:<value>" so an unrecognized state never silently becomes a known one.
CAMPAIGN_STATUS: Final[dict[int, str]] = {
    -99: "account_suspended",
    -2: "bounce_protect",
    -1: "accounts_unhealthy",
    0: "draft",
    1: "active",
    2: "paused",
    3: "completed",
    4: "running_subsequences",
}
ACCOUNT_STATUS: Final[dict[int, str]] = {
    1: "active",
    2: "paused",
    3: "maintenance",
    -1: "connection_error",
    -2: "soft_bounce_error",
    -3: "sending_error",
}
WARMUP_STATUS: Final[dict[int, str]] = {
    0: "paused",
    1: "active",
    -1: "banned",
    -2: "spam_folder_unknown",
    -3: "permanent_suspension",
}
_ACCOUNT_ERROR_STATUSES: Final = frozenset({-1, -2, -3})


def _label(value: Any, table: dict[int, str]) -> str | None:
    """Map a documented integer enum to its label, marking unknown values."""
    if value is None:
        return None
    try:
        return table.get(int(value)) or f"unknown:{value}"
    except (TypeError, ValueError):
        return f"unknown:{value}"


class InstantlyProvider(CampaignProvider):
    """Read-only access to one Instantly workspace."""

    name = "instantly"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
        page_cap: int = DEFAULT_PAGE_CAP,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ProviderAuthError(
                "INSTANTLY_API_KEY is empty",
                hint="export INSTANTLY_API_KEY before running the instantly command",
            )
        self.base_url = base_url.rstrip("/")
        self.version = __version__
        self.page_cap = page_cap
        self.max_retries = max_retries
        self.warnings: list[str] = []
        self._guard = ReadOnlyTransport(transport)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=self._guard,
            timeout=timeout or httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            follow_redirects=False,
        )
        self._campaign_cache: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_env(cls, *, transport: httpx.AsyncBaseTransport | None = None) -> InstantlyProvider:
        """Build a provider from the environment. Never accepts a key as an argument path."""
        return cls(
            api_key=os.environ.get("INSTANTLY_API_KEY", ""),
            base_url=os.environ.get("INSTANTLY_BASE_URL", DEFAULT_BASE_URL),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- HTTP ---------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """One retried request. Returns decoded JSON or raises ProviderError."""
        full_path = f"{API_PREFIX}{path}"
        clean_params = {k: v for k, v in params.items() if v is not None} if params else None

        last_error: ProviderError | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.request(
                    method.upper(), full_path, params=clean_params, json=json
                )
            except ReadOnlyViolation:
                raise
            except httpx.TimeoutException as exc:
                last_error = ProviderError(f"request to {full_path} timed out", endpoint=full_path)
                logger.warning(
                    "instantly request timed out",
                    extra={"endpoint": full_path, "attempt": attempt},
                )
                if attempt < self.max_retries:
                    await self._backoff(attempt)
                    continue
                raise last_error from exc
            except httpx.HTTPError as exc:
                message = redact_secrets(str(exc))
                last_error = ProviderError(
                    f"connection error on {full_path}: {message}", endpoint=full_path
                )
                if attempt < self.max_retries:
                    await self._backoff(attempt)
                    continue
                raise last_error from exc

            if response.is_success:
                if not response.content:
                    return None
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderError(
                        f"malformed JSON in the response from {full_path}",
                        hint="the provider returned a body that is not valid JSON",
                        status=response.status_code,
                        endpoint=full_path,
                    ) from exc

            error = self._error_for(response, full_path)
            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if retryable and attempt < self.max_retries:
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            raise error

        raise last_error or ProviderError(f"request to {full_path} failed", endpoint=full_path)

    def _error_for(self, response: httpx.Response, endpoint: str) -> ProviderError:
        """Build a clean error. The API key is structurally unable to appear here."""
        message = _error_message(response)
        status = response.status_code
        if status in {401, 403}:
            return ProviderAuthError(
                f"Instantly returned {status} on {endpoint}: {message}",
                hint=("the API key is invalid, expired, or lacks the scope this endpoint needs"),
                status=status,
                endpoint=endpoint,
            )
        hints = {
            400: "the request was rejected as malformed",
            402: "the workspace has no active plan for this endpoint",
            404: "the resource does not exist, or the key cannot see it",
            422: "request validation failed",
            429: "rate limited",
        }
        return ProviderError(
            f"Instantly returned {status} on {endpoint}: {message}",
            hint=hints.get(status),
            status=status,
            endpoint=endpoint,
        )

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        """Sleep before a retry. Honours Retry-After, jittered, always bounded."""
        delay = min(2.0 ** (attempt - 1), MAX_RETRY_SLEEP)
        if retry_after:
            # A date-formatted Retry-After falls back to the exponential backoff.
            with contextlib.suppress(ValueError):
                delay = min(float(retry_after), MAX_RETRY_SLEEP)
        # Jitter avoids a synchronized retry storm when several checks run at once.
        await asyncio.sleep(delay + random.uniform(0, 0.25))  # noqa: S311 - not crypto

    async def _paginate(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Walk cursor pagination. Returns ``(items, truncated)``.

        Stops on: the caller's limit, an empty page, a missing cursor, a cursor
        that repeats (a provider-side loop), or the page cap.
        """
        per_page = MAX_PAGE_SIZE if limit is None else max(1, min(MAX_PAGE_SIZE, limit))
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        for page in range(self.page_cap):
            if method.upper() == "GET":
                page_params = {**(params or {}), "limit": per_page}
                if cursor:
                    page_params["starting_after"] = cursor
                payload = await self._request("GET", path, params=page_params)
            else:
                body = {**(json or {}), "limit": per_page}
                if cursor:
                    body["starting_after"] = cursor
                payload = await self._request(method, path, json=body)

            if isinstance(payload, dict):
                batch = payload.get("items") or []
                next_cursor = payload.get("next_starting_after")
            elif isinstance(payload, list):
                batch, next_cursor = payload, None
            else:
                batch, next_cursor = [], None

            items.extend(item for item in batch if isinstance(item, dict))

            if limit is not None and len(items) >= limit:
                return items[:limit], bool(next_cursor)
            if not batch or not next_cursor:
                return items, False
            cursor_key = str(next_cursor)
            if cursor_key in seen_cursors:
                self.warnings.append(
                    f"{path}: pagination cursor repeated after {page + 1} page(s); "
                    f"stopping to avoid a loop. Results may be incomplete."
                )
                return items, True
            seen_cursors.add(cursor_key)
            cursor = cursor_key

        self.warnings.append(
            f"{path}: stopped at the {self.page_cap}-page cap; results are partial."
        )
        return items, True

    def _capability_result(self, capability: Capability, exc: ProviderError) -> ProviderResult[Any]:
        """Map a provider error onto the right capability status."""
        if isinstance(exc, ProviderAuthError) or exc.status in {401, 402, 403}:
            return forbidden(capability, str(exc))
        return failed(capability, str(exc))

    # -- campaign -----------------------------------------------------------

    async def get_campaign(self, campaign_id: str | None = None) -> ProviderResult[Campaign]:
        if not campaign_id:
            return misconfigured(Capability.CAMPAIGN, "no campaign id was supplied")
        try:
            payload = await self._request("GET", f"/campaigns/{campaign_id}")
        except ProviderError as exc:
            return self._capability_result(Capability.CAMPAIGN, exc)
        if not isinstance(payload, dict):
            return failed(
                Capability.CAMPAIGN,
                f"expected a campaign object, got {type(payload).__name__}",
            )
        self._campaign_cache[campaign_id] = payload
        return ok(
            Capability.CAMPAIGN,
            self._parse_campaign(payload),
            detail=f"GET {API_PREFIX}/campaigns/{{id}}",
        )

    def _parse_campaign(self, payload: dict[str, Any]) -> Campaign:
        schedule_raw = payload.get("campaign_schedule")
        schedule = self._parse_schedule(schedule_raw if isinstance(schedule_raw, dict) else {})
        email_list = payload.get("email_list")
        senders = tuple(e for e in (normalize_email(v) for v in email_list or []) if e)
        return Campaign(
            id=normalize_text(payload.get("id")),
            name=normalize_text(payload.get("name")),
            status=_label(payload.get("status"), CAMPAIGN_STATUS),
            raw_status=payload.get("status"),
            timezone_name=schedule.timezone_name,
            schedule=schedule,
            daily_limit=coerce_int(payload.get("daily_limit")),
            stop_on_reply=_nullable_bool(payload.get("stop_on_reply")),
            stop_on_auto_reply=_nullable_bool(payload.get("stop_on_auto_reply")),
            steps=self._parse_sequences(payload.get("sequences")),
            sender_emails=senders,
            custom_variables=_as_dict(payload.get("custom_variables")),
            provider_metadata=None,
            raw={},
        )

    def _parse_schedule(self, raw: dict[str, Any]) -> CampaignSchedule:
        from datetime import datetime

        def as_date(value: Any) -> Any:
            if not value:
                return None
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
            except ValueError:
                self.warnings.append(f"campaign_schedule: unparseable date {value!r}")
                return None

        windows: list[SendingWindow] = []
        entries = raw.get("schedules")
        if entries is not None and not isinstance(entries, list):
            self.warnings.append("campaign_schedule.schedules was not a list; ignoring")
            entries = None
        for index, entry in enumerate(entries or []):
            if not isinstance(entry, dict):
                continue
            timing = _as_dict(entry.get("timing"))
            days_raw = _as_dict(entry.get("days"))
            days = {
                day
                for key, enabled in days_raw.items()
                if enabled is True and (day := coerce_int(key)) is not None and 0 <= day <= 6
            }
            windows.append(
                SendingWindow(
                    name=normalize_text(entry.get("name")) or f"window {index + 1}",
                    start=parse_clock_time(timing.get("from")),
                    end=parse_clock_time(timing.get("to")),
                    days=frozenset(days),
                    timezone_name=normalize_text(entry.get("timezone")),
                    raw_timezone=normalize_text(entry.get("timezone")),
                )
            )
        first_zone = next((w.timezone_name for w in windows if w.timezone_name), None)
        return CampaignSchedule(
            start_date=as_date(raw.get("start_date")),
            end_date=as_date(raw.get("end_date")),
            windows=tuple(windows),
            timezone_name=first_zone,
            raw={},
        )

    def _parse_sequences(self, raw: Any) -> tuple[CampaignStep, ...]:
        if not isinstance(raw, list):
            return ()
        steps: list[CampaignStep] = []
        position = 0
        for sequence in raw:
            if not isinstance(sequence, dict):
                continue
            for step in sequence.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                delay = step.get("delay")
                try:
                    delay_value = float(delay) if delay is not None else None
                except (TypeError, ValueError):
                    delay_value = None
                variants = step.get("variants")
                if not isinstance(variants, list) or not variants:
                    variants = [{}]
                for variant_index, variant in enumerate(variants):
                    if not isinstance(variant, dict):
                        continue
                    steps.append(
                        CampaignStep(
                            index=position,
                            step_type=str(step.get("type") or "email"),
                            delay=delay_value,
                            delay_unit=normalize_text(step.get("delay_unit")),
                            subject=str(variant.get("subject") or ""),
                            body=str(variant.get("body") or ""),
                            variant_index=variant_index,
                            disabled=variant.get("v_disabled") is True,
                        )
                    )
                position += 1
        return tuple(steps)

    # -- leads --------------------------------------------------------------

    async def list_campaign_leads(
        self, campaign_id: str | None = None, *, limit: int | None = None
    ) -> ProviderResult[list[Lead]]:
        if not campaign_id:
            return misconfigured(Capability.LEADS, "no campaign id was supplied")
        try:
            # POST is the documented shape of this LIST endpoint. See
            # providers/instantly_transport.py for why it is allowlisted.
            rows, truncated = await self._paginate(
                "/leads/list", method="POST", json={"campaign": campaign_id}, limit=limit
            )
        except ProviderError as exc:
            return self._capability_result(Capability.LEADS, exc)
        leads = [self._parse_lead(row) for row in rows]
        return ok(
            Capability.LEADS,
            leads,
            detail=f"POST {API_PREFIX}/leads/list ({len(leads)} rows)",
            partial=truncated,
        )

    def _parse_lead(self, row: dict[str, Any]) -> Lead:
        email = normalize_text(row.get("email"))
        custom = _as_dict(row.get("payload"))
        variables = {str(k): str(v) for k, v in custom.items() if v not in (None, "")}
        # The v2 lead `status` enum's labels are not documented, so no label is
        # guessed. `timestamp_last_contact` IS documented, so prior contact is
        # derived from it instead -- a fact rather than an inference.
        contacted = row.get("timestamp_last_contact")
        return Lead(
            id=normalize_text(row.get("id")),
            email=email,
            normalized_email=normalize_email(email),
            first_name=normalize_text(row.get("first_name")),
            last_name=normalize_text(row.get("last_name")),
            company_name=normalize_text(row.get("company_name")),
            company_domain=normalize_domain(row.get("company_domain") or row.get("website")),
            job_title=normalize_text(row.get("job_title")),
            country=None,
            region=None,
            personalization=normalize_text(row.get("personalization")),
            custom_variables=variables,
            assigned_sender=normalize_email(row.get("last_contacted_from")),
            source_row=None,
            source_name="instantly",
            suppressed=None,
            status_label="contacted" if contacted else "not_contacted",
        )

    # -- senders ------------------------------------------------------------

    async def list_campaign_senders(
        self, campaign_id: str | None = None
    ) -> ProviderResult[list[Sender]]:
        if not campaign_id:
            return misconfigured(Capability.SENDERS, "no campaign id was supplied")
        payload = self._campaign_cache.get(campaign_id)
        if payload is None:
            try:
                fetched = await self._request("GET", f"/campaigns/{campaign_id}")
            except ProviderError as exc:
                return self._capability_result(Capability.SENDERS, exc)
            payload = fetched if isinstance(fetched, dict) else {}
            self._campaign_cache[campaign_id] = payload

        emails = [e for e in (normalize_email(v) for v in payload.get("email_list") or []) if e]
        if not emails:
            return ok(
                Capability.SENDERS,
                [],
                detail="the campaign's email_list is empty",
            )
        if len(emails) > MAX_SENDER_LOOKUPS:
            self.warnings.append(
                f"campaign has {len(emails)} senders; only the first "
                f"{MAX_SENDER_LOOKUPS} were inspected"
            )
            emails = emails[:MAX_SENDER_LOOKUPS]

        senders, errors = await self._fetch_accounts(emails)
        if errors and len(errors) == len(emails):
            first = errors[0]
            if "401" in first or "403" in first or "402" in first:
                return forbidden(Capability.SENDERS, first)
            return failed(Capability.SENDERS, first)
        if errors:
            self.warnings.append(
                f"{len(errors)} of {len(emails)} sending accounts could not be read"
            )
        return ok(
            Capability.SENDERS,
            senders,
            detail=f"GET {API_PREFIX}/accounts/{{email}} x{len(emails)}",
            partial=bool(errors),
        )

    async def _fetch_accounts(self, emails: list[str]) -> tuple[list[Sender], list[str]]:
        """Fetch account records with bounded concurrency."""
        semaphore = asyncio.Semaphore(SENDER_CONCURRENCY)

        async def fetch(email: str) -> tuple[Sender, str | None]:
            async with semaphore:
                try:
                    payload = await self._request("GET", f"/accounts/{email}")
                except ProviderError as exc:
                    # Keep the sender: the campaign genuinely has it attached.
                    # It just has no health data, which the rules report honestly.
                    return Sender(email=email), str(exc)
            if not isinstance(payload, dict):
                return Sender(email=email), f"unexpected response shape for {email}"
            return self._parse_sender(payload, email), None

        results = await asyncio.gather(*(fetch(email) for email in emails))
        senders = [sender for sender, _ in results]
        errors = [error for _, error in results if error]
        return senders, errors

    def _parse_sender(self, payload: dict[str, Any], fallback_email: str) -> Sender:
        raw_status = payload.get("status")
        status_int: int | None
        try:
            status_int = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            status_int = None
        name_parts = [payload.get("first_name"), payload.get("last_name")]
        display = " ".join(str(p) for p in name_parts if p) or None
        return Sender(
            email=normalize_email(payload.get("email")) or fallback_email,
            display_name=normalize_text(display),
            enabled=(status_int == 1) if status_int is not None else None,
            status_label=_label(raw_status, ACCOUNT_STATUS),
            status_is_error=(
                status_int in _ACCOUNT_ERROR_STATUSES if status_int is not None else None
            ),
            daily_limit=coerce_int(payload.get("daily_limit")),
            # Instantly's own warmup score. Not derived, not estimated.
            health_score=_as_float(payload.get("stat_warmup_score")),
            warmup_status=_label(payload.get("warmup_status"), WARMUP_STATUS),
            setup_pending=_nullable_bool(payload.get("setup_pending")),
            provider=normalize_text(str(payload.get("provider_code")))
            if payload.get("provider_code") is not None
            else None,
            raw_status=raw_status,
        )

    async def get_sender_health(self, senders: list[Sender]) -> ProviderResult[list[Sender]]:
        """Health rides along with the account record, so no extra request is made."""
        if not senders:
            return unsupported(Capability.SENDER_HEALTH, "no senders to report health for")
        scored = sum(1 for s in senders if s.health_score is not None)
        if scored == 0:
            return unsupported(
                Capability.SENDER_HEALTH,
                "Instantly returned no stat_warmup_score for any attached account",
            )
        return ok(
            Capability.SENDER_HEALTH,
            senders,
            detail=f"stat_warmup_score present for {scored} of {len(senders)} accounts",
            partial=scored < len(senders),
        )

    # -- suppressions -------------------------------------------------------

    async def list_suppressions(self) -> ProviderResult[list[SuppressionEntry]]:
        try:
            rows, truncated = await self._paginate("/block-lists-entries")
        except ProviderError as exc:
            return self._capability_result(Capability.SUPPRESSIONS, exc)
        entries = []
        for row in rows:
            value = normalize_text(row.get("bl_value"))
            if not value:
                continue
            is_domain = row.get("is_domain") is True or "@" not in value
            entries.append(
                SuppressionEntry(
                    value=(normalize_domain(value) if is_domain else normalize_email(value))
                    or value.lower(),
                    is_domain=bool(is_domain),
                    reason=None,
                    source="instantly block list",
                )
            )
        return ok(
            Capability.SUPPRESSIONS,
            entries,
            detail=f"GET {API_PREFIX}/block-lists-entries ({len(entries)} entries)",
            partial=truncated,
        )

    # -- analytics ----------------------------------------------------------

    async def get_campaign_analytics_context(
        self, campaign_id: str | None = None
    ) -> ProviderResult[dict[str, Any]]:
        if not campaign_id:
            return misconfigured(Capability.ANALYTICS, "no campaign id was supplied")
        try:
            payload = await self._request("GET", "/campaigns/analytics", params={"id": campaign_id})
        except ProviderError as exc:
            return self._capability_result(Capability.ANALYTICS, exc)
        rows = payload if isinstance(payload, list) else [payload]
        row = next((r for r in rows if isinstance(r, dict)), None)
        if row is None:
            return failed(Capability.ANALYTICS, "analytics returned no rows for this campaign")
        return ok(
            Capability.ANALYTICS,
            {
                "leads_count": coerce_int(row.get("leads_count")),
                "contacted_count": coerce_int(row.get("contacted_count")),
                "emails_sent_count": coerce_int(row.get("emails_sent_count")),
                "bounced_count": coerce_int(row.get("bounced_count")),
                "unsubscribed_count": coerce_int(row.get("unsubscribed_count")),
                "campaign_status": row.get("campaign_status"),
            },
            detail=f"GET {API_PREFIX}/campaigns/analytics",
        )

    # -- health -------------------------------------------------------------

    async def health_check(self) -> ProviderResult[dict[str, Any]]:
        try:
            payload = await self._request("GET", "/workspaces/current")
        except ProviderError as exc:
            return self._capability_result(Capability.CAMPAIGN, exc)
        workspace = payload if isinstance(payload, dict) else {}
        return ok(
            Capability.CAMPAIGN,
            {
                "provider": self.name,
                "reachable": True,
                "workspace_id": workspace.get("id"),
            },
        )


def _as_dict(value: Any) -> dict[str, Any]:
    """A mapping, or an empty one. Never None, so the model field stays typed."""
    return dict(value) if isinstance(value, dict) else {}


def _nullable_bool(value: Any) -> bool | None:
    """Preserve the difference between ``false`` and ``null``.

    Instantly returns ``null`` for settings that were never configured. Coercing
    that to ``False`` would turn "we do not know whether stop-on-reply is on"
    into "stop-on-reply is off", which is a fabricated finding.
    """
    return value if isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _error_message(response: httpx.Response) -> str:
    """Extract a readable message from an error body, redacted and bounded."""
    try:
        body = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return redact_secrets(text[:300]) if text else (response.reason_phrase or "unknown error")
    if isinstance(body, dict):
        for key in ("message", "error", "detail", "error_message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return redact_secrets(value[:300])
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return redact_secrets(value["message"][:300])
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return redact_secrets("; ".join(str(e) for e in errors)[:300])
    return redact_secrets(str(body)[:300])
