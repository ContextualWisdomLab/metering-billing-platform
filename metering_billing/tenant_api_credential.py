"""Tenant-scoped HTTP API credentials with issue-once secrets.

The service is the buyer-facing auth path:

1. Resolve the tenant.
2. Mint a secret, return prefix plus secret once, and persist only a keyed hash.
3. Authorize later ``/v1`` calls against an active hash.
4. Revoke without deleting history.

NIST SP 800-63B requires verifier secrets to be stored as a salted or keyed
hash, never in recoverable form (National Institute of Standards and
Technology, 2020).  OWASP API authentication treats leaked keys as
revocable bearer credentials (OWASP, 2023).  SOC 2 CC6 requires logical
access control before a shippable HTTP surface (AICPA, 2017).

Issue a key, then send it on every ``/v1`` call; revoke when leaked.  A tenant
with zero active credentials stays in the bootstrap window so AIS can keep
pulling with ``X-CWL-Tenant-Reference`` until a key is issued.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable
from uuid import UUID

from metering_billing.errors import (
    TenantApiCredentialOutcomeCode,
    TenantApiCredentialQueryError,
    TenantApiCredentialRejectionReasonCode,
)
from metering_billing.usage_ledger import (
    MemoryUsageLedger,
    StoredTenantApiCredential,
    generate_record_id,
)


Clock = Callable[[], datetime]
TENANT_API_CREDENTIAL_CONTRACT_VERSION = 1
DEFAULT_CREDENTIAL_LABEL = "operator_key"
DEFAULT_CREDENTIAL_PEPPER = "cwl_bootstrap_pepper"
CREDENTIAL_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9_]+$")
SECRET_PREFIX_LENGTH = 12


def hash_api_credential_secret(secret: str, pepper: str) -> str:
    """Return ``hmac-sha256:<hex>`` for one presented or minted secret.

    The pepper is a verifier key, not a stored password salt.  The plaintext
    secret is never persisted.
    """
    if not isinstance(secret, str) or not secret:
        raise ValueError("api credential secret must be a non-empty string")
    if not isinstance(pepper, str) or not pepper:
        raise ValueError("credential pepper must be a non-empty string")
    digest = hmac.new(pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def mint_api_credential_secret() -> tuple[str, str]:
    """Return ``(credential_prefix, api_credential_secret)`` for one issue."""
    secret = f"cwlak_{secrets.token_urlsafe(32)}"
    return secret[:SECRET_PREFIX_LENGTH], secret


def parse_credential_label(value: object | None) -> str:
    """Return a two-or-more-word snake_case label, or reject the name."""
    if value is None or value == "":
        return DEFAULT_CREDENTIAL_LABEL
    if not isinstance(value, str) or CREDENTIAL_LABEL_PATTERN.fullmatch(value) is None:
        raise TenantApiCredentialQueryError("credential_label_invalid")
    return value


@dataclass(frozen=True)
class TenantApiCredentialResult:
    """Buyer-facing result of issuing, listing, or revoking one credential."""

    tenant_api_credential_outcome_code: TenantApiCredentialOutcomeCode
    tenant_api_credential_contract_version: int
    tenant_api_credential_id: UUID | None
    tenant_reference: str | None
    credential_label: str | None
    credential_prefix: str | None
    api_credential_secret: str | None
    credential_status: str | None
    issued_at: datetime | None
    rejection_reason_code: TenantApiCredentialRejectionReasonCode | None

    def as_contract_dict(self) -> dict[str, object]:
        """Return the issue contract, including the secret only when minted."""
        outcome = self.tenant_api_credential_outcome_code
        outcome_text = (
            outcome.value if isinstance(outcome, TenantApiCredentialOutcomeCode) else str(outcome)
        )
        payload: dict[str, object] = {
            "tenant_api_credential_contract_version": self.tenant_api_credential_contract_version,
            "tenant_api_credential_outcome_code": outcome_text,
        }
        if outcome_text == TenantApiCredentialOutcomeCode.REJECTED:
            payload["rejection_reason_code"] = (
                self.rejection_reason_code.value
                if self.rejection_reason_code is not None
                else "tenant_not_found"
            )
            return payload
        payload["tenant_api_credential_id"] = str(self.tenant_api_credential_id)
        payload["tenant_reference"] = self.tenant_reference
        payload["credential_label"] = self.credential_label
        payload["credential_prefix"] = self.credential_prefix
        payload["credential_status"] = self.credential_status
        payload["issued_at"] = _format_issued_at(self.issued_at)
        if self.api_credential_secret is not None:
            payload["api_credential_secret"] = self.api_credential_secret
        return payload

    def as_metadata_dict(self) -> dict[str, object]:
        """Return list/revoke metadata.  Never includes the secret or hash."""
        return {
            "tenant_api_credential_id": str(self.tenant_api_credential_id),
            "credential_label": self.credential_label,
            "credential_prefix": self.credential_prefix,
            "credential_status": self.credential_status,
            "issued_at": _format_issued_at(self.issued_at),
        }


@dataclass(frozen=True)
class TenantApiCredentialPage:
    """One tenant-scoped list of credential metadata rows."""

    tenant_api_credentials: tuple[TenantApiCredentialResult, ...]

    def as_contract_dict(self) -> dict[str, object]:
        """Return metadata only.  Secrets and hashes are omitted."""
        return {
            "tenant_api_credentials": [
                item.as_metadata_dict() for item in self.tenant_api_credentials
            ]
        }


class TenantApiCredentialService:
    """Issue, list, revoke, and authorize tenant-scoped HTTP API credentials."""

    def __init__(
        self,
        ledger: MemoryUsageLedger | None = None,
        clock: Clock | None = None,
        credential_pepper: str | None = None,
    ) -> None:
        self.ledger = MemoryUsageLedger() if ledger is None else ledger
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))
        if credential_pepper is None:
            credential_pepper = os.environ.get("CWL_API_CREDENTIAL_PEPPER") or DEFAULT_CREDENTIAL_PEPPER
        if not credential_pepper:
            raise ValueError("credential pepper must be a non-empty string")
        self._pepper = credential_pepper

    def issue_credential(
        self, tenant_reference: str, credential_label: object | None = None
    ) -> TenantApiCredentialResult:
        """Mint one active credential and return the secret once.

        A second issue of the same tenant, label, and contract version always
        mints a new secret.  The persisted row stores only the keyed hash.
        """
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            return _rejected(TenantApiCredentialRejectionReasonCode.TENANT_NOT_FOUND)
        assert tenant is not None
        try:
            label = parse_credential_label(credential_label)
        except TenantApiCredentialQueryError:
            return _rejected(TenantApiCredentialRejectionReasonCode.CREDENTIAL_LABEL_INVALID)
        prefix, secret = mint_api_credential_secret()
        stored = self.ledger.insert_tenant_api_credential(
            StoredTenantApiCredential(
                tenant_api_credential_id=generate_record_id(),
                tenant_account_id=tenant.tenant_account_id,
                tenant_api_credential_contract_version=TENANT_API_CREDENTIAL_CONTRACT_VERSION,
                credential_label=label,
                credential_prefix=prefix,
                credential_secret_hash=hash_api_credential_secret(secret, self._pepper),
                credential_status="active",
                issued_at=self._clock(),
                revoked_at=None,
            )
        )
        return _from_stored(stored, tenant.tenant_reference, secret, TenantApiCredentialOutcomeCode.ACCEPTED)

    def list_credentials(self, tenant_reference: str) -> TenantApiCredentialPage:
        """Return metadata for one tenant.  Secrets and hashes are omitted."""
        tenant = self._require_tenant(tenant_reference)
        return TenantApiCredentialPage(
            tenant_api_credentials=tuple(
                _from_stored(stored, tenant.tenant_reference, None, TenantApiCredentialOutcomeCode.ACCEPTED)
                for stored in self.ledger.list_tenant_api_credentials(tenant.tenant_account_id)
            )
        )

    def revoke_credential(
        self, tenant_reference: str, tenant_api_credential_id: UUID
    ) -> TenantApiCredentialResult:
        """Revoke one same-tenant credential.  A second revoke is a replay."""
        tenant = self._require_tenant(tenant_reference)
        stored = self.ledger.get_tenant_api_credential(tenant_api_credential_id)
        if stored is None or stored.tenant_account_id != tenant.tenant_account_id:
            raise TenantApiCredentialQueryError("api_credential_not_found")
        if stored.credential_status == "revoked":
            return _from_stored(
                stored, tenant.tenant_reference, None, TenantApiCredentialOutcomeCode.DUPLICATE_REPLAY
            )
        updated = self.ledger.revoke_tenant_api_credential(
            stored.tenant_api_credential_id, self._clock()
        )
        return _from_stored(
            updated, tenant.tenant_reference, None, TenantApiCredentialOutcomeCode.ACCEPTED
        )

    def authorize_request(
        self, tenant_reference: str, presented_secret: str | None
    ) -> None:
        """Require an active same-tenant key once any active key exists.

        A presented secret is always verified.  Unknown and revoked keys are
        indistinguishable.  A key whose tenant does not match the pin is
        ``request_invalid``.
        """
        tenant = self._require_tenant(tenant_reference)
        if presented_secret is not None:
            try:
                secret_hash = hash_api_credential_secret(presented_secret, self._pepper)
            except ValueError as error:
                raise TenantApiCredentialQueryError("api_credential_invalid") from error
            stored = self.ledger.find_tenant_api_credential_by_hash(secret_hash)
            if stored is None or stored.credential_status != "active":
                raise TenantApiCredentialQueryError("api_credential_invalid")
            if stored.tenant_account_id != tenant.tenant_account_id:
                raise TenantApiCredentialQueryError("request_invalid")
            return
        if self.ledger.list_active_tenant_api_credentials(tenant.tenant_account_id):
            raise TenantApiCredentialQueryError("api_credential_missing")

    def _require_tenant(self, tenant_reference: str):
        """Resolve the tenant or fail closed."""
        tenant, tenant_error = self.ledger.resolve_tenant(tenant_reference)
        if tenant_error is not None:
            raise TenantApiCredentialQueryError("tenant_not_found")
        assert tenant is not None
        return tenant


def _rejected(reason_code: TenantApiCredentialRejectionReasonCode) -> TenantApiCredentialResult:
    """Build a rejected result without minting a secret."""
    return TenantApiCredentialResult(
        tenant_api_credential_outcome_code=TenantApiCredentialOutcomeCode.REJECTED,
        tenant_api_credential_contract_version=TENANT_API_CREDENTIAL_CONTRACT_VERSION,
        tenant_api_credential_id=None,
        tenant_reference=None,
        credential_label=None,
        credential_prefix=None,
        api_credential_secret=None,
        credential_status=None,
        issued_at=None,
        rejection_reason_code=reason_code,
    )


def _from_stored(
    stored: StoredTenantApiCredential,
    tenant_reference: str,
    secret: str | None,
    outcome: TenantApiCredentialOutcomeCode,
) -> TenantApiCredentialResult:
    """Project a persisted credential.  ``secret`` is set only on issue."""
    return TenantApiCredentialResult(
        tenant_api_credential_outcome_code=outcome,
        tenant_api_credential_contract_version=stored.tenant_api_credential_contract_version,
        tenant_api_credential_id=stored.tenant_api_credential_id,
        tenant_reference=tenant_reference,
        credential_label=stored.credential_label,
        credential_prefix=stored.credential_prefix,
        api_credential_secret=secret,
        credential_status=stored.credential_status,
        issued_at=stored.issued_at,
        rejection_reason_code=None,
    )


def _format_issued_at(issued_at: datetime | None) -> str:
    """Render ``issued_at`` as a timezone-aware ISO 8601 instant."""
    assert issued_at is not None
    return issued_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
