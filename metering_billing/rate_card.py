"""Tenant-scoped versioned rate-card catalog.

The service is the buyer-facing price-list path:

1. Resolve the tenant.
2. Accept a named card, a currency, and one or more flat metric lines.
3. Persist an append-only ``rate_card_version`` whose unit amounts are exact.
4. Replay the same tenant, card name, canonical line hash, and contract version.

A published version is never edited.  Publishing different lines on the same
card name creates the next version.  Rating later resolves that persisted
version; this service does not invent a hidden default price (TM Forum, 2024).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from metering_billing.errors import (
    ExactDecimalError,
    RateCardOutcomeCode,
    RateCardQueryError,
    RateCardRejectionReasonCode,
)
from metering_billing.exact_decimal import format_exact_decimal, parse_exact_decimal
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredRateCard,
    StoredRateCardLine,
    StoredRateCardVersion,
    generate_record_id,
)


Clock = Callable[[], datetime]
RATE_CARD_CONTRACT_VERSION = 1
METRIC_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9_]+$")
RATE_CARD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9_]+$")
CURRENCY_CODE_PATTERN = re.compile(r"^[A-Z]{3}$")


def parse_unit_amount(value: Any) -> Decimal:
    """Parse a flat unit price as an exact positive decimal.

    Binary floating-point values are rejected at this boundary so a catalog
    cannot smuggle IEEE inexact money into later rating.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise ExactDecimalError("unit amount must be an exact decimal")
    if isinstance(value, Decimal):
        parsed = parse_exact_decimal(format_exact_decimal(value))
    else:
        parsed = parse_exact_decimal(value)
    if parsed <= 0:
        raise ExactDecimalError("unit amount must be greater than zero")
    return parsed


def compute_rate_card_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical published lines."""
    canonical_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class RateCardLineResult:
    """One published flat unit price for a metric code."""

    metric_code: str
    unit_amount: Decimal
    currency_code: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the rate-card schema."""
        return {
            "metric_code": self.metric_code,
            "unit_amount": format_exact_decimal(self.unit_amount),
            "currency_code": self.currency_code,
        }


@dataclass(frozen=True)
class RateCardResult:
    """Buyer-facing result of publishing or reading one rate-card version."""

    rate_card_outcome_code: RateCardOutcomeCode
    rate_card_contract_version: int
    rate_card_id: UUID | None
    rate_card_version_id: UUID | None
    tenant_reference: str | None
    rate_card_name: str | None
    rate_card_version: int | None
    currency_code: str | None
    source_payload_hash: str | None
    published_at: datetime | None
    next_operator_action: str
    rejection_reason_code: RateCardRejectionReasonCode | None
    lines: tuple[RateCardLineResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the published card, or a sparse rejected operational result."""
        outcome = self.rate_card_outcome_code
        outcome_text = outcome.value if isinstance(outcome, RateCardOutcomeCode) else str(outcome)
        if outcome_text == RateCardOutcomeCode.REJECTED:
            return {
                "rate_card_contract_version": self.rate_card_contract_version,
                "rate_card_outcome_code": outcome_text,
                "rejection_reason_code": (
                    self.rejection_reason_code.value
                    if self.rejection_reason_code is not None
                    else RateCardRejectionReasonCode.RATE_CARD_NOT_FOUND.value
                ),
            }
        if (
            outcome_text != RateCardOutcomeCode.ACCEPTED
            and outcome_text != RateCardOutcomeCode.DUPLICATE_REPLAY
        ):
            raise ValueError(f"unsupported rate card outcome: {outcome_text}")
        if (
            self.rate_card_id is None
            or self.rate_card_version_id is None
            or self.published_at is None
            or self.rate_card_version is None
        ):
            raise ValueError("accepted rate cards must include identity and version")
        return {
            "rate_card_contract_version": self.rate_card_contract_version,
            "rate_card_outcome_code": outcome_text,
            "rate_card_id": str(self.rate_card_id),
            "rate_card_version_id": str(self.rate_card_version_id),
            "tenant_reference": self.tenant_reference,
            "rate_card_name": self.rate_card_name,
            "rate_card_version": self.rate_card_version,
            "currency_code": self.currency_code,
            "source_payload_hash": self.source_payload_hash,
            "published_at": _format_published_at(self.published_at),
            "next_operator_action": self.next_operator_action,
            "lines": [line.as_contract_dict() for line in self.lines],
        }


@dataclass(frozen=True)
class RateCardListPage:
    """Tenant-scoped list of rate-card headers."""

    tenant_reference: str
    rate_cards: tuple[dict[str, object], ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON list published for GET /v1/rate-cards."""
        return {
            "tenant_reference": self.tenant_reference,
            "rate_cards": list(self.rate_cards),
        }


@dataclass(frozen=True)
class RateCardVersionListPage:
    """Tenant-scoped list of published versions for one card."""

    tenant_reference: str
    rate_card_id: UUID
    rate_card_versions: tuple[dict[str, object], ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON list published for GET .../versions."""
        return {
            "tenant_reference": self.tenant_reference,
            "rate_card_id": str(self.rate_card_id),
            "rate_card_versions": list(self.rate_card_versions),
        }


class RateCardService:
    """Append-only tenant-scoped rate-card publisher backed by a normalized ledger."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def publish_rate_card(
        self,
        tenant_reference: str,
        rate_card_name: str,
        currency_code: str,
        lines: Sequence[Mapping[str, Any]],
    ) -> RateCardResult:
        """Publish one immutable rate-card version for a tenant.

        A replay of the same tenant, card name, canonical line hash, and
        contract version returns the stored ``rate_card_version``.  A later
        distinct line set increments the version.  Rating next uses that
        persisted version; this service never invents a default price.
        """
        if not isinstance(tenant_reference, str) or not tenant_reference:
            return _rejected(RateCardRejectionReasonCode.TENANT_NOT_FOUND)
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(RateCardRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None
        if not isinstance(rate_card_name, str) or RATE_CARD_NAME_PATTERN.fullmatch(rate_card_name) is None:
            return _rejected(RateCardRejectionReasonCode.RATE_CARD_NAME_INVALID)
        if not isinstance(currency_code, str) or CURRENCY_CODE_PATTERN.fullmatch(currency_code) is None:
            return _rejected(RateCardRejectionReasonCode.CURRENCY_CODE_INVALID)
        if not isinstance(lines, Sequence) or isinstance(lines, (str, bytes)) or not lines:
            return _rejected(RateCardRejectionReasonCode.RATE_CARD_LINES_INVALID)
        parsed_lines, line_error = _parse_lines(lines, currency_code)
        if line_error is not None:
            return _rejected(line_error)
        assert parsed_lines is not None
        source_payload_hash = compute_rate_card_payload_hash(
            _canonical_line_snapshot(rate_card_name, currency_code, parsed_lines)
        )
        existing_card = self.ledger.find_rate_card(tenant.tenant_account_id, rate_card_name)
        if existing_card is not None and existing_card.currency_code != currency_code:
            return _rejected(RateCardRejectionReasonCode.CURRENCY_MISMATCH)
        if existing_card is not None:
            existing_version = self.ledger.find_rate_card_version_by_identity(
                tenant.tenant_account_id,
                existing_card.rate_card_id,
                source_payload_hash,
                RATE_CARD_CONTRACT_VERSION,
            )
            if existing_version is not None:
                return _from_stored(
                    existing_card,
                    existing_version,
                    tenant.tenant_reference,
                    RateCardOutcomeCode.DUPLICATE_REPLAY,
                )
        stored_card = self.ledger.insert_rate_card(
            StoredRateCard(
                rate_card_id=(
                    existing_card.rate_card_id if existing_card is not None else generate_record_id()
                ),
                tenant_account_id=tenant.tenant_account_id,
                rate_card_name=rate_card_name,
                currency_code=currency_code,
                created_at=self._clock(),
            )
        )
        version_number = self.ledger.next_rate_card_version_number(
            tenant.tenant_account_id, stored_card.rate_card_id
        )
        version_id = generate_record_id()
        stored_version = self.ledger.insert_rate_card_version(
            StoredRateCardVersion(
                rate_card_version_id=version_id,
                tenant_account_id=tenant.tenant_account_id,
                rate_card_id=stored_card.rate_card_id,
                version_number=version_number,
                rate_card_contract_version=RATE_CARD_CONTRACT_VERSION,
                currency_code=currency_code,
                source_payload_hash=source_payload_hash,
                published_at=self._clock(),
                rate_card_lines=tuple(
                    StoredRateCardLine(
                        rate_card_line_id=generate_record_id(),
                        tenant_account_id=tenant.tenant_account_id,
                        rate_card_version_id=version_id,
                        metric_code=line.metric_code,
                        unit_amount=line.unit_amount,
                        currency_code=line.currency_code,
                    )
                    for line in parsed_lines
                ),
            )
        )
        return _from_stored(
            stored_card,
            stored_version,
            tenant.tenant_reference,
            RateCardOutcomeCode.ACCEPTED,
        )

    def list_rate_cards(self, tenant_reference: str) -> RateCardListPage:
        """Return tenant-scoped rate-card headers, or fail closed."""
        tenant = _require_tenant(self.ledger, tenant_reference)
        cards = []
        for card in self.ledger.list_rate_cards(tenant.tenant_account_id):
            latest = _latest_version(self.ledger, tenant.tenant_account_id, card.rate_card_id)
            cards.append(
                {
                    "rate_card_id": str(card.rate_card_id),
                    "rate_card_name": card.rate_card_name,
                    "currency_code": card.currency_code,
                    "latest_rate_card_version": (
                        latest.version_number if latest is not None else None
                    ),
                }
            )
        return RateCardListPage(tenant_reference=tenant.tenant_reference, rate_cards=tuple(cards))

    def get_rate_card(self, tenant_reference: str, rate_card_id: UUID) -> RateCardResult:
        """Return the latest published version of one same-tenant card."""
        tenant = _require_tenant(self.ledger, tenant_reference)
        if not isinstance(rate_card_id, UUID):
            raise RateCardQueryError("rate_card_not_found")
        card = self.ledger.get_rate_card(rate_card_id)
        if card is None or card.tenant_account_id != tenant.tenant_account_id:
            raise RateCardQueryError("rate_card_not_found")
        latest = _latest_version(self.ledger, tenant.tenant_account_id, card.rate_card_id)
        if latest is None:
            raise RateCardQueryError("rate_card_not_found")
        return _from_stored(card, latest, tenant.tenant_reference, RateCardOutcomeCode.ACCEPTED)

    def list_rate_card_versions(
        self, tenant_reference: str, rate_card_id: UUID
    ) -> RateCardVersionListPage:
        """Return published versions for one same-tenant card."""
        tenant = _require_tenant(self.ledger, tenant_reference)
        if not isinstance(rate_card_id, UUID):
            raise RateCardQueryError("rate_card_not_found")
        card = self.ledger.get_rate_card(rate_card_id)
        if card is None or card.tenant_account_id != tenant.tenant_account_id:
            raise RateCardQueryError("rate_card_not_found")
        versions = []
        for version in self.ledger.list_rate_card_versions(
            tenant.tenant_account_id, card.rate_card_id
        ):
            versions.append(
                _from_stored(
                    card, version, tenant.tenant_reference, RateCardOutcomeCode.ACCEPTED
                ).as_contract_dict()
            )
        return RateCardVersionListPage(
            tenant_reference=tenant.tenant_reference,
            rate_card_id=card.rate_card_id,
            rate_card_versions=tuple(versions),
        )

    def get_rate_card_version(
        self, tenant_reference: str, rate_card_version: UUID | int
    ) -> RateCardResult:
        """Return one same-tenant published version, or fail closed.

        ``rate_card_version`` may be the internal version identifier or the
        integer version number.  A missing or cross-tenant identifier is
        indistinguishable.
        """
        tenant = _require_tenant(self.ledger, tenant_reference)
        stored = _resolve_version(self.ledger, tenant.tenant_account_id, rate_card_version)
        if stored is None:
            raise RateCardQueryError("rate_card_not_found")
        card = self.ledger.get_rate_card(stored.rate_card_id)
        if card is None or card.tenant_account_id != tenant.tenant_account_id:
            raise RateCardQueryError("rate_card_not_found")
        return _from_stored(card, stored, tenant.tenant_reference, RateCardOutcomeCode.ACCEPTED)


def _parse_lines(
    lines: Sequence[Mapping[str, Any]], currency_code: str
) -> tuple[tuple[RateCardLineResult, ...] | None, RateCardRejectionReasonCode | None]:
    """Validate published lines as exact, unique, same-currency metric prices."""
    parsed: list[RateCardLineResult] = []
    seen: set[str] = set()
    for raw_line in lines:
        if not isinstance(raw_line, Mapping):
            return None, RateCardRejectionReasonCode.RATE_CARD_LINES_INVALID
        metric_code = raw_line.get("metric_code")
        if not isinstance(metric_code, str) or METRIC_CODE_PATTERN.fullmatch(metric_code) is None:
            return None, RateCardRejectionReasonCode.METRIC_CODE_INVALID
        if metric_code in seen:
            return None, RateCardRejectionReasonCode.RATE_CARD_LINES_INVALID
        seen.add(metric_code)
        line_currency = raw_line.get("currency_code", currency_code)
        if not isinstance(line_currency, str) or line_currency != currency_code:
            return None, RateCardRejectionReasonCode.CURRENCY_MISMATCH
        try:
            unit_amount = parse_unit_amount(raw_line.get("unit_amount"))
        except ExactDecimalError:
            return None, RateCardRejectionReasonCode.UNIT_AMOUNT_INVALID
        parsed.append(
            RateCardLineResult(
                metric_code=metric_code,
                unit_amount=unit_amount,
                currency_code=currency_code,
            )
        )
    ordered = tuple(sorted(parsed, key=lambda line: line.metric_code))
    return ordered, None


def _canonical_line_snapshot(
    rate_card_name: str,
    currency_code: str,
    lines: tuple[RateCardLineResult, ...],
) -> dict[str, object]:
    """Return name, currency, and sorted lines for catalog identity."""
    return {
        "rate_card_name": rate_card_name,
        "currency_code": currency_code,
        "rate_card_contract_version": RATE_CARD_CONTRACT_VERSION,
        "lines": [
            {
                "metric_code": line.metric_code,
                "unit_amount": format_exact_decimal(line.unit_amount),
                "currency_code": line.currency_code,
            }
            for line in lines
        ],
    }


def _require_tenant(ledger: MemoryUsageLedger, tenant_reference: str):
    """Return a stored tenant or raise a tenant-scoped query error."""
    if not isinstance(tenant_reference, str) or not tenant_reference:
        raise RateCardQueryError("tenant_not_found")
    tenant, tenant_error = ledger.resolve_tenant(tenant_reference)
    if tenant_error is not None:
        raise RateCardQueryError("tenant_not_found")
    assert tenant is not None
    return tenant


def _latest_version(
    ledger: MemoryUsageLedger, tenant_account_id: UUID, rate_card_id: UUID
) -> StoredRateCardVersion | None:
    """Return the highest published version number for one card, if any."""
    versions = ledger.list_rate_card_versions(tenant_account_id, rate_card_id)
    if not versions:
        return None
    return max(versions, key=lambda version: version.version_number)


def _resolve_version(
    ledger: MemoryUsageLedger,
    tenant_account_id: UUID,
    rate_card_version: UUID | int,
) -> StoredRateCardVersion | None:
    """Resolve a version by internal identifier or tenant-scoped number."""
    if isinstance(rate_card_version, UUID):
        stored = ledger.get_rate_card_version(rate_card_version)
        if stored is None or stored.tenant_account_id != tenant_account_id:
            return None
        return stored
    if isinstance(rate_card_version, bool) or not isinstance(rate_card_version, int):
        return None
    return ledger.find_rate_card_version(tenant_account_id, rate_card_version)


def _rejected(reason: RateCardRejectionReasonCode) -> RateCardResult:
    """Return a sparse rejected catalog result."""
    return RateCardResult(
        rate_card_outcome_code=RateCardOutcomeCode.REJECTED,
        rate_card_contract_version=RATE_CARD_CONTRACT_VERSION,
        rate_card_id=None,
        rate_card_version_id=None,
        tenant_reference=None,
        rate_card_name=None,
        rate_card_version=None,
        currency_code=None,
        source_payload_hash=None,
        published_at=None,
        next_operator_action="Publish a rate card, then rate a window against that version.",
        rejection_reason_code=reason,
        lines=(),
    )


def _from_stored(
    card: StoredRateCard,
    version: StoredRateCardVersion,
    tenant_reference: str,
    outcome: RateCardOutcomeCode,
) -> RateCardResult:
    """Project a persisted card and version into the buyer-facing result."""
    return RateCardResult(
        rate_card_outcome_code=outcome,
        rate_card_contract_version=version.rate_card_contract_version,
        rate_card_id=card.rate_card_id,
        rate_card_version_id=version.rate_card_version_id,
        tenant_reference=tenant_reference,
        rate_card_name=card.rate_card_name,
        rate_card_version=version.version_number,
        currency_code=version.currency_code,
        source_payload_hash=version.source_payload_hash,
        published_at=version.published_at,
        next_operator_action="Publish a rate card, then rate a window against that version.",
        rejection_reason_code=None,
        lines=tuple(
            RateCardLineResult(
                metric_code=line.metric_code,
                unit_amount=line.unit_amount,
                currency_code=line.currency_code,
            )
            for line in version.rate_card_lines
        ),
    )


def _format_published_at(published_at: datetime) -> str:
    """Return an RFC 3339 UTC timestamp for the publish instant."""
    return published_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
