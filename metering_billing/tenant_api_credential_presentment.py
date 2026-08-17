"""Tenant-scoped API-credential presentment from stored metadata.

The service is a read path:

1. Resolve the tenant.
2. Load that tenant's stored ``tenant_api_credential``.
3. Project identity, prefix, status, timestamps, and the next action.
4. Return metadata.  Do not mint, revoke, or reconstruct a secret.

RFC 9110 treats GET as a safe, idempotent read (Fielding et al., 2022).
List pages use a deterministic cursor rather than a mutable offset
(Google, 2024).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from metering_billing.errors import TenantApiCredentialPresentmentQueryError
from metering_billing.time_window import parse_iso8601_datetime
from metering_billing.usage_ledger import MemoryUsageLedger, StoredTenantApiCredential


TENANT_API_CREDENTIAL_PRESENTMENT_CONTRACT_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAXIMUM_PAGE_LIMIT = 100
OPERATOR_ACTION_WAIT = "wait"
OPERATOR_ACTION_ISSUE = "issue"


def next_operator_action(*, credential_status: str) -> str:
    """Return wait for an active key, otherwise issue a replacement."""
    if credential_status == "active":
        return OPERATOR_ACTION_WAIT
    if credential_status == "revoked":
        return OPERATOR_ACTION_ISSUE
    raise TenantApiCredentialPresentmentQueryError("request_invalid")


@dataclass(frozen=True)
class TenantApiCredentialPresentmentResult:
    """Buyer-facing projection of one stored tenant API credential."""

    tenant_api_credential_id: UUID
    tenant_reference: str
    credential_label: str
    credential_prefix: str
    credential_status: str
    tenant_api_credential_contract_version: int
    issued_at: datetime
    revoked_at: datetime | None
    next_operator_action: str

    def as_contract_dict(self) -> dict[str, object]:
        """Return the closed JSON object published in the presentment schema."""
        payload: dict[str, object] = {
            "tenant_api_credential_presentment_contract_version": (
                TENANT_API_CREDENTIAL_PRESENTMENT_CONTRACT_VERSION
            ),
            "tenant_api_credential_id": str(self.tenant_api_credential_id),
            "tenant_reference": self.tenant_reference,
            "credential_label": self.credential_label,
            "credential_prefix": self.credential_prefix,
            "credential_status": self.credential_status,
            "tenant_api_credential_contract_version": (
                self.tenant_api_credential_contract_version
            ),
            "issued_at": _format_issued_at(self.issued_at),
            "next_operator_action": self.next_operator_action,
        }
        if self.revoked_at is not None:
            payload["revoked_at"] = _format_issued_at(self.revoked_at)
        return payload

    def as_summary_dict(self) -> dict[str, object]:
        """Return the list-item envelope used by collection GET."""
        return {
            "tenant_api_credential_id": str(self.tenant_api_credential_id),
            "credential_label": self.credential_label,
            "credential_prefix": self.credential_prefix,
            "credential_status": self.credential_status,
            "issued_at": _format_issued_at(self.issued_at),
            "next_operator_action": self.next_operator_action,
        }


@dataclass(frozen=True)
class TenantApiCredentialPresentmentPage:
    """One tenant-scoped page of credential metadata summaries."""

    tenant_api_credentials: tuple[TenantApiCredentialPresentmentResult, ...]
    next_cursor: str | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return ``{tenant_api_credentials, next_cursor}`` with summaries."""
        return {
            "tenant_api_credentials": [
                item.as_summary_dict() for item in self.tenant_api_credentials
            ],
            "next_cursor": self.next_cursor,
        }


class TenantApiCredentialPresentmentService:
    """Read-only projector of stored tenant API credential metadata."""

    def __init__(self, ledger: MemoryUsageLedger | None = None) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger

    def present_tenant_api_credential(
        self, tenant_reference: str, tenant_api_credential_id: UUID
    ) -> TenantApiCredentialPresentmentResult:
        """Return one same-tenant stored credential, or fail closed.

        A missing or cross-tenant identifier is indistinguishable.  The read
        does not mint, revoke, or reconstruct a secret.
        """
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_tenant_api_credential(tenant_api_credential_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise TenantApiCredentialPresentmentQueryError("api_credential_not_found")
        return self._project_credential(tenant.tenant_reference, stored)

    def list_tenant_api_credentials(
        self,
        tenant_reference: str,
        cursor: str | None = None,
        page_limit: object | None = None,
    ) -> TenantApiCredentialPresentmentPage:
        """Return one tenant page of credential summaries without secrets.

        Order is ``issued_at`` then ``tenant_api_credential_id``.
        The envelope is ``tenant_api_credentials`` plus ``next_cursor``.
        """
        tenant = self._require_tenant(tenant_reference)
        cursor_key = _parse_page_cursor(cursor)
        limit = _parse_page_limit(page_limit)
        stored_rows = sorted(
            self.ledger.list_tenant_api_credentials(tenant.tenant_account_id),
            key=lambda credential: (credential.issued_at, credential.tenant_api_credential_id),
        )
        matched: list[StoredTenantApiCredential] = []
        for stored in stored_rows:
            if cursor_key is not None and (
                stored.issued_at,
                stored.tenant_api_credential_id,
            ) <= cursor_key:
                continue
            matched.append(stored)
        page_rows = matched[:limit]
        next_cursor = None
        if len(matched) > limit:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(last.issued_at, last.tenant_api_credential_id)
        return TenantApiCredentialPresentmentPage(
            tenant_api_credentials=tuple(
                self._project_credential(tenant.tenant_reference, stored) for stored in page_rows
            ),
            next_cursor=next_cursor,
        )

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed without leaking other tenants."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise TenantApiCredentialPresentmentQueryError("tenant_not_found")
        assert tenant is not None
        return tenant

    def _project_credential(
        self, tenant_reference: str, stored: StoredTenantApiCredential
    ) -> TenantApiCredentialPresentmentResult:
        """Project one stored credential using only persisted metadata."""
        return TenantApiCredentialPresentmentResult(
            tenant_api_credential_id=stored.tenant_api_credential_id,
            tenant_reference=tenant_reference,
            credential_label=stored.credential_label,
            credential_prefix=stored.credential_prefix,
            credential_status=stored.credential_status,
            tenant_api_credential_contract_version=stored.tenant_api_credential_contract_version,
            issued_at=stored.issued_at,
            revoked_at=stored.revoked_at,
            next_operator_action=next_operator_action(
                credential_status=stored.credential_status
            ),
        )


def _format_issued_at(issued_at: datetime) -> str:
    """Render an issue timestamp as a timezone-aware ISO 8601 instant."""
    return issued_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_page_limit(page_limit: object | None) -> int:
    """Bound page size to a positive integer no greater than the maximum."""
    if page_limit is None or page_limit == "":
        return DEFAULT_PAGE_LIMIT
    if isinstance(page_limit, bool) or not isinstance(page_limit, (int, str)):
        raise TenantApiCredentialPresentmentQueryError("request_invalid")
    if isinstance(page_limit, str):
        if not page_limit.isdigit():
            raise TenantApiCredentialPresentmentQueryError("request_invalid")
        parsed = int(page_limit)
    else:
        parsed = page_limit
    if parsed < 1 or parsed > MAXIMUM_PAGE_LIMIT:
        raise TenantApiCredentialPresentmentQueryError("request_invalid")
    return parsed


def _encode_page_cursor(issued_at: datetime, tenant_api_credential_id: UUID) -> str:
    """Encode the keyset cursor as issued_at then credential id."""
    return f"{_format_issued_at(issued_at)}|{tenant_api_credential_id}"


def _parse_page_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    """Decode a keyset cursor or reject an unreadable token."""
    if cursor is None or cursor == "":
        return None
    try:
        issued_text, credential_text = cursor.split("|", 1)
        return parse_iso8601_datetime(issued_text), UUID(credential_text)
    except (TypeError, ValueError) as error:
        raise TenantApiCredentialPresentmentQueryError("request_invalid") from error
