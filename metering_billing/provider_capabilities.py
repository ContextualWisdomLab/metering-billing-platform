"""Capability-based provider selection without provider-specific core fields.

The registry is intentionally a small in-process policy boundary.  It selects
an adapter manifest before an external command is created; it does not call a
provider, store provider objects, or make a provider catalog authoritative.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock


PROVIDER_ROLES = frozenset(
    {
        "merchant_of_record",
        "payment_processor",
        "payment_gateway",
        "payment_orchestrator",
        "tax_service",
        "invoicing_service",
        "metering_service",
        "manual_collection",
    }
)
PROVIDER_CAPABILITIES = frozenset(
    {
        "hosted_checkout",
        "subscription_collection",
        "metered_usage_push",
        "custom_invoice_line",
        "prepaid_credit",
        "customer_portal",
        "partial_refund",
        "dispute_management",
        "tax_document",
        "wire_transfer",
        "multi_currency",
        "korean_billing_key",
        "settlement_export",
    }
)
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_JURISDICTION_PATTERN = re.compile(r"^[A-Z]{2}(?:-[A-Z0-9]{1,3})?$")


class ProviderCapabilityError(ValueError):
    """Raised when a provider manifest or route request is unsafe."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class ProviderRoutingError(ProviderCapabilityError):
    """Raised when no healthy provider satisfies a route request."""


def _aware_utc(value: datetime, reason_code: str) -> datetime:
    """Normalize an aware instant without accepting ambiguous local time."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderCapabilityError(reason_code)
    return value.astimezone(UTC)


def _unique_codes(
    values: Iterable[str], allowed: frozenset[str] | None, reason_code: str
) -> tuple[str, ...]:
    """Validate bounded, non-empty code collections while preserving order."""
    try:
        items = tuple(values)
    except TypeError as error:
        raise ProviderCapabilityError(reason_code) from error
    try:
        unique = len(set(items))
    except TypeError as error:
        raise ProviderCapabilityError(reason_code) from error
    if not items or unique != len(items):
        raise ProviderCapabilityError(reason_code)
    if any(not isinstance(item, str) for item in items):
        raise ProviderCapabilityError(reason_code)
    if allowed is not None and any(item not in allowed for item in items):
        raise ProviderCapabilityError(reason_code)
    return items


def _scoped_codes(
    values: Iterable[str], pattern: re.Pattern[str], reason_code: str
) -> tuple[str, ...]:
    """Validate optional routing dimensions and preserve deterministic order."""
    try:
        items = tuple(values)
    except TypeError as error:
        raise ProviderCapabilityError(reason_code) from error
    try:
        unique = len(set(items))
    except TypeError as error:
        raise ProviderCapabilityError(reason_code) from error
    if unique != len(items):
        raise ProviderCapabilityError(reason_code)
    if any(not isinstance(item, str) or pattern.fullmatch(item) is None for item in items):
        raise ProviderCapabilityError(reason_code)
    return items


def _intervals_overlap(
    first_from: datetime,
    first_to: datetime | None,
    second_from: datetime,
    second_to: datetime | None,
) -> bool:
    """Return whether two half-open effective intervals overlap."""
    return (first_to is None or second_from < first_to) and (
        second_to is None or first_from < second_to
    )


@dataclass(frozen=True)
class ProviderCapabilityManifest:
    """Versioned capabilities and routing dimensions for one provider."""

    provider_code: str
    provider_roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    effective_from: datetime
    effective_to: datetime | None = None
    supported_currency_codes: tuple[str, ...] = ()
    jurisdiction_codes: tuple[str, ...] = ()
    contract_type_codes: tuple[str, ...] = ()
    tenant_policy_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject ambiguous or unsupported provider declarations."""
        if not isinstance(self.provider_code, str) or _CODE_PATTERN.fullmatch(self.provider_code) is None:
            raise ProviderCapabilityError("provider_code_invalid")
        roles = _unique_codes(self.provider_roles, PROVIDER_ROLES, "provider_role_invalid")
        capabilities = _unique_codes(
            self.capabilities, PROVIDER_CAPABILITIES, "capability_invalid"
        )
        effective_from = _aware_utc(self.effective_from, "effective_from_invalid")
        effective_to = (
            None
            if self.effective_to is None
            else _aware_utc(self.effective_to, "effective_to_invalid")
        )
        if effective_to is not None and effective_to <= effective_from:
            raise ProviderCapabilityError("effective_interval_invalid")
        currencies = _scoped_codes(
            self.supported_currency_codes, _CURRENCY_PATTERN, "currency_code_invalid"
        )
        jurisdictions = _scoped_codes(
            self.jurisdiction_codes, _JURISDICTION_PATTERN, "jurisdiction_code_invalid"
        )
        contracts = _scoped_codes(
            self.contract_type_codes, _CODE_PATTERN, "contract_type_code_invalid"
        )
        policies = _scoped_codes(
            self.tenant_policy_codes, _CODE_PATTERN, "tenant_policy_code_invalid"
        )
        object.__setattr__(self, "provider_roles", roles)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "effective_from", effective_from)
        object.__setattr__(self, "effective_to", effective_to)
        object.__setattr__(self, "supported_currency_codes", currencies)
        object.__setattr__(self, "jurisdiction_codes", jurisdictions)
        object.__setattr__(self, "contract_type_codes", contracts)
        object.__setattr__(self, "tenant_policy_codes", policies)

    def as_contract_dict(self) -> dict[str, object]:
        """Render the provider-capability contract without credentials."""
        payload: dict[str, object] = {
            "provider_code": self.provider_code,
            "provider_roles": list(self.provider_roles),
            "capabilities": list(self.capabilities),
            "effective_from": self.effective_from.isoformat().replace("+00:00", "Z"),
        }
        if self.effective_to is not None:
            payload["effective_to"] = self.effective_to.isoformat().replace("+00:00", "Z")
        if self.supported_currency_codes:
            payload["supported_currency_codes"] = list(self.supported_currency_codes)
        if self.jurisdiction_codes:
            payload["jurisdiction_codes"] = list(self.jurisdiction_codes)
        if self.contract_type_codes:
            payload["contract_type_codes"] = list(self.contract_type_codes)
        if self.tenant_policy_codes:
            payload["tenant_policy_codes"] = list(self.tenant_policy_codes)
        return payload

    def supports(
        self,
        capability: str,
        *,
        at: datetime,
        provider_role_code: str | None = None,
    ) -> bool:
        """Return whether one capability is active for the requested role/time."""
        if not isinstance(capability, str) or (
            provider_role_code is not None and not isinstance(provider_role_code, str)
        ):
            return False
        instant = _aware_utc(at, "routing_time_invalid")
        return (
            self.effective_from <= instant
            and (self.effective_to is None or instant < self.effective_to)
            and capability in self.capabilities
            and (provider_role_code is None or provider_role_code in self.provider_roles)
        )

    def matches(self, request: "ProviderRouteRequest") -> bool:
        """Return whether this manifest satisfies every route dimension."""
        if not all(
            self.supports(
                capability,
                at=request.at,
                provider_role_code=request.provider_role_code,
            )
            for capability in request.required_capabilities
        ):
            return False
        dimensions = (
            (request.currency_code, self.supported_currency_codes),
            (request.jurisdiction_code, self.jurisdiction_codes),
            (request.contract_type_code, self.contract_type_codes),
            (request.tenant_policy_code, self.tenant_policy_codes),
        )
        return all(
            value is None or not supported or value in supported
            for value, supported in dimensions
        )


@dataclass(frozen=True)
class ProviderRouteRequest:
    """Buyer and policy facts used before creating an external transaction."""

    required_capabilities: tuple[str, ...]
    provider_role_code: str | None = None
    currency_code: str | None = None
    jurisdiction_code: str | None = None
    contract_type_code: str | None = None
    tenant_policy_code: str | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Validate all route facts before they reach an adapter."""
        capabilities = _unique_codes(
            self.required_capabilities, PROVIDER_CAPABILITIES, "required_capability_invalid"
        )
        if self.provider_role_code is not None and (
            not isinstance(self.provider_role_code, str)
            or self.provider_role_code not in PROVIDER_ROLES
        ):
            raise ProviderCapabilityError("provider_role_invalid")
        if self.currency_code is not None and (
            not isinstance(self.currency_code, str)
            or _CURRENCY_PATTERN.fullmatch(self.currency_code) is None
        ):
            raise ProviderCapabilityError("currency_code_invalid")
        if self.jurisdiction_code is not None and (
            not isinstance(self.jurisdiction_code, str)
            or _JURISDICTION_PATTERN.fullmatch(self.jurisdiction_code) is None
        ):
            raise ProviderCapabilityError("jurisdiction_code_invalid")
        for value, reason_code in (
            (self.contract_type_code, "contract_type_code_invalid"),
            (self.tenant_policy_code, "tenant_policy_code_invalid"),
        ):
            if value is not None and (
                not isinstance(value, str) or _CODE_PATTERN.fullmatch(value) is None
            ):
                raise ProviderCapabilityError(reason_code)
        at = _aware_utc(self.at, "routing_time_invalid")
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(self, "at", at)


@dataclass(frozen=True)
class _RegisteredProvider:
    """Internal manifest and health callback pair."""

    manifest: ProviderCapabilityManifest
    health_check: Callable[[], bool] | None


class ProviderCapabilityRegistry:
    """Select a healthy, effective provider by explicit capabilities."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._providers: list[_RegisteredProvider] = []

    def register(
        self,
        manifest: ProviderCapabilityManifest,
        health_check: Callable[[], bool] | None = None,
    ) -> None:
        """Register one manifest and reject overlapping versions."""
        if not isinstance(manifest, ProviderCapabilityManifest):
            raise ProviderCapabilityError("manifest_invalid")
        if health_check is not None and not callable(health_check):
            raise ProviderCapabilityError("health_check_invalid")
        with self._lock:
            for registered in self._providers:
                if (
                    registered.manifest.provider_code == manifest.provider_code
                    and _intervals_overlap(
                        registered.manifest.effective_from,
                        registered.manifest.effective_to,
                        manifest.effective_from,
                        manifest.effective_to,
                    )
                ):
                    raise ProviderCapabilityError("manifest_interval_conflict")
            self._providers.append(_RegisteredProvider(manifest, health_check))

    def select(self, request: ProviderRouteRequest) -> ProviderCapabilityManifest:
        """Return the deterministic first healthy manifest or fail closed."""
        if not isinstance(request, ProviderRouteRequest):
            raise ProviderRoutingError("route_request_invalid")
        candidates: list[ProviderCapabilityManifest] = []
        with self._lock:
            providers = tuple(self._providers)
        for registered in providers:
            if not registered.manifest.matches(request):
                continue
            if registered.health_check is not None:
                try:
                    healthy = bool(registered.health_check())
                except Exception:
                    healthy = False
                if not healthy:
                    continue
            candidates.append(registered.manifest)
        if not candidates:
            raise ProviderRoutingError("no_provider_available")
        return min(candidates, key=lambda item: (item.provider_code, item.effective_from))

    def active_manifests(self, at: datetime) -> tuple[ProviderCapabilityManifest, ...]:
        """Return active manifests in stable provider/effective order."""
        instant = _aware_utc(at, "routing_time_invalid")
        with self._lock:
            providers = tuple(self._providers)
        active = [
            registered.manifest
            for registered in providers
            if registered.manifest.effective_from <= instant
            and (registered.manifest.effective_to is None or instant < registered.manifest.effective_to)
        ]
        return tuple(sorted(active, key=lambda item: (item.provider_code, item.effective_from)))


LEMON_SQUEEZY_MANIFEST = ProviderCapabilityManifest(
    provider_code="lemon_squeezy",
    provider_roles=("merchant_of_record",),
    capabilities=(
        "hosted_checkout",
        "subscription_collection",
        "metered_usage_push",
        "customer_portal",
        "partial_refund",
        "dispute_management",
        "tax_document",
        "settlement_export",
    ),
    effective_from=datetime(2026, 1, 1, tzinfo=UTC),
    supported_currency_codes=("USD", "EUR", "GBP"),
)

MANUAL_ENTERPRISE_MANIFEST = ProviderCapabilityManifest(
    provider_code="manual_enterprise",
    provider_roles=("manual_collection",),
    capabilities=("custom_invoice_line", "wire_transfer", "settlement_export"),
    effective_from=datetime(2026, 1, 1, tzinfo=UTC),
    contract_type_codes=("annual_commitment", "purchase_order", "wire_transfer"),
)
