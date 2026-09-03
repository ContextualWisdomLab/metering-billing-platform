"""Tenant API credential HTTP presentment tests for metadata-only reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest import mock
from uuid import UUID, uuid4

from metering_billing import (
    TenantApiCredentialPresentmentService,
    TenantApiCredentialService,
    create_http_app,
)
from metering_billing.contracts import validate_tenant_api_credential_presentment
from metering_billing.errors import TenantApiCredentialPresentmentQueryError
from metering_billing.tenant_api_credential_presentment import next_operator_action
from metering_billing.usage_ledger import StoredTenantApiCredential, generate_record_id
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import seed_rated_ledger


ISSUED_MORNING = datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
ISSUED_EVENING = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)


def persist_credential(
    ledger,
    tenant_reference,
    *,
    issued_at,
    credential_status="active",
    revoked_at=None,
    credential_label="operator_key",
    credential_prefix="cwlak_fake001",
    hash_suffix="a",
):
    """Persist one stored #22 credential without minting a recoverable secret."""
    tenant = ledger.require_tenant(tenant_reference)
    return ledger.insert_tenant_api_credential(
        StoredTenantApiCredential(
            tenant_api_credential_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            tenant_api_credential_contract_version=1,
            credential_label=credential_label,
            credential_prefix=credential_prefix,
            credential_secret_hash="hmac-sha256:" + (hash_suffix * 64),
            credential_status=credential_status,
            issued_at=issued_at,
            revoked_at=revoked_at,
        )
    )


class TenantApiCredentialPresentmentTests(unittest.TestCase):
    """Verify metadata GET, list paging, and fail-closed secret isolation."""

    def test_stored_active_projects_wait_without_secret_or_hash(self) -> None:
        """An active stored key shows wait and never reconstructs the secret."""
        ledger = seed_rated_ledger()
        stored = persist_credential(ledger, TENANT_ONE, issued_at=ISSUED_MORNING)
        first = TenantApiCredentialPresentmentService(ledger).present_tenant_api_credential(
            TENANT_ONE, stored.tenant_api_credential_id
        )
        second = TenantApiCredentialPresentmentService(ledger).present_tenant_api_credential(
            TENANT_ONE, stored.tenant_api_credential_id
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.tenant_api_credential_id, stored.tenant_api_credential_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.credential_label, "operator_key")
        self.assertEqual(first.credential_prefix, "cwlak_fake001")
        self.assertEqual(first.credential_status, "active")
        self.assertEqual(first.tenant_api_credential_contract_version, 1)
        self.assertEqual(first.issued_at, ISSUED_MORNING)
        self.assertIsNone(first.revoked_at)
        self.assertEqual(first.next_operator_action, "wait")
        payload = first.as_contract_dict()
        self.assertEqual(validate_tenant_api_credential_presentment(payload), ())
        self.assertNotIn("revoked_at", payload)
        self.assertNotIn("api_credential_secret", payload)
        self.assertNotIn("credential_secret_hash", payload)
        self.assertNotIn("hmac-sha256:", str(payload))
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("card_pan", payload)

    def test_stored_revoked_projects_issue(self) -> None:
        """A revoked stored key keeps revoked_at and asks to issue a new key."""
        ledger = seed_rated_ledger()
        stored = persist_credential(
            ledger,
            TENANT_ONE,
            issued_at=ISSUED_MORNING,
            credential_status="revoked",
            revoked_at=ISSUED_EVENING,
            hash_suffix="b",
            credential_prefix="cwlak_fake002",
        )
        presented = TenantApiCredentialPresentmentService(ledger).present_tenant_api_credential(
            TENANT_ONE, stored.tenant_api_credential_id
        )
        payload = presented.as_contract_dict()
        self.assertEqual(presented.next_operator_action, "issue")
        self.assertEqual(presented.credential_status, "revoked")
        self.assertEqual(presented.revoked_at, ISSUED_EVENING)
        self.assertNotIn("api_credential_secret", payload)
        self.assertNotIn("credential_secret_hash", payload)
        self.assertEqual(validate_tenant_api_credential_presentment(payload), ())

    def test_http_get_item_and_paged_list_keep_auth(self) -> None:
        """GET item and list page stored metadata; active keys still require auth."""
        ledger = seed_rated_ledger()
        times = iter((ISSUED_MORNING, ISSUED_EVENING))
        issuer = TenantApiCredentialService(ledger, clock=lambda: next(times))
        first = issuer.issue_credential(TENANT_ONE, "morning_key")
        second = issuer.issue_credential(TENANT_ONE, "evening_key")
        secret = second.api_credential_secret
        app = create_http_app(ledger)
        missing_key_status, missing_key_body = invoke_http(
            app,
            "GET",
            f"/v1/tenant-api-credentials/{first.tenant_api_credential_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(missing_key_status, 422)
        self.assertEqual(missing_key_body["rejection_reason_code"], "api_credential_missing")
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/tenant-api-credentials/{first.tenant_api_credential_id}",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["tenant_api_credential_id"], str(first.tenant_api_credential_id))
        self.assertEqual(body["credential_prefix"], first.credential_prefix)
        self.assertEqual(body["credential_status"], "active")
        self.assertEqual(body["next_operator_action"], "wait")
        self.assertNotIn("api_credential_secret", body)
        self.assertNotIn("credential_secret_hash", body)
        self.assertNotIn(secret, str(body))
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/tenant-api-credentials/{first.tenant_api_credential_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE, "X-CWL-Api-Key": secret},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/tenant-api-credentials",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"tenant_api_credentials", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["tenant_api_credentials"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["tenant_api_credentials"][0]
        self.assertEqual(
            set(first_summary),
            {
                "tenant_api_credential_id",
                "credential_label",
                "credential_prefix",
                "credential_status",
                "issued_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/tenant-api-credentials",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["tenant_api_credentials"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["tenant_api_credential_id"],
            second_body["tenant_api_credentials"][0]["tenant_api_credential_id"],
        }
        self.assertEqual(
            listed_ids,
            {
                str(first.tenant_api_credential_id),
                str(second.tenant_api_credential_id),
            },
        )
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/tenant-api-credentials",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["tenant_api_credentials"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_issue_and_revoke_stay_and_refuse_card_data(self) -> None:
        """POST issue and revoke stay #22; PAN and secrets are refused."""
        ledger = seed_rated_ledger()
        app = create_http_app(ledger)
        issue_refused, issue_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(issue_refused, 422)
        self.assertEqual(issue_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.tenant_api_credentials), 0)
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "credential_label": "operator_key"},
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["tenant_api_credential_outcome_code"], "accepted")
        self.assertIn("api_credential_secret", accepted_body)
        secret = accepted_body["api_credential_secret"]
        credential_id = accepted_body["tenant_api_credential_id"]
        revoke_refused, revoke_body = invoke_http(
            app,
            "POST",
            f"/v1/tenant-api-credentials/{credential_id}/revoke",
            {"tenant_reference": TENANT_ONE, "cvc": "123"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(revoke_refused, 422)
        self.assertEqual(revoke_body["rejection_reason_code"], "request_invalid")
        stored_active = ledger.get_tenant_api_credential(UUID(credential_id))
        assert stored_active is not None
        self.assertEqual(stored_active.credential_status, "active")
        revoke_status, revoke_accepted = invoke_http(
            app,
            "POST",
            f"/v1/tenant-api-credentials/{credential_id}/revoke",
            {"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(revoke_status, 200)
        self.assertEqual(revoke_accepted["credential_status"], "revoked")
        self.assertNotIn("api_credential_secret", revoke_accepted)

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant ids stay 404 with no hash."""
        ledger = seed_rated_ledger()
        stored = persist_credential(
            ledger,
            TENANT_ONE,
            issued_at=ISSUED_MORNING,
            credential_status="revoked",
            revoked_at=ISSUED_EVENING,
        )
        tenant_one_secret = TenantApiCredentialService(ledger).issue_credential(
            TENANT_ONE, "recovery_key"
        ).api_credential_secret
        tenant_two_secret = TenantApiCredentialService(ledger).issue_credential(
            TENANT_TWO, "operator_key"
        ).api_credential_secret
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/tenant-api-credentials/{stored.tenant_api_credential_id}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/tenant-api-credentials/{stored.tenant_api_credential_id}",
            query={"tenant_reference": TENANT_TWO},
            headers={"Authorization": f"Bearer {tenant_two_secret}"},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "api_credential_not_found")
        self.assertNotIn("credential_prefix", other_body)
        self.assertNotIn("credential_secret_hash", other_body)
        self.assertNotIn("api_credential_secret", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/tenant-api-credentials/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {tenant_one_secret}"},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "api_credential_not_found")
        with self.assertRaises(TenantApiCredentialPresentmentQueryError) as crossed:
            TenantApiCredentialPresentmentService(ledger).present_tenant_api_credential(
                TENANT_TWO, stored.tenant_api_credential_id
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "api_credential_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and actions."""
        self.assertEqual(next_operator_action(credential_status="active"), "wait")
        self.assertEqual(next_operator_action(credential_status="revoked"), "issue")
        with self.assertRaises(TenantApiCredentialPresentmentQueryError):
            next_operator_action(credential_status="posted")
        ledger = seed_rated_ledger()
        stored = persist_credential(
            ledger,
            TENANT_ONE,
            issued_at=ISSUED_MORNING,
            credential_status="revoked",
            revoked_at=ISSUED_EVENING,
        )
        secret = TenantApiCredentialService(ledger).issue_credential(
            TENANT_ONE, "recovery_key"
        ).api_credential_secret
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/tenant-api-credentials",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/tenant-api-credentials",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            f"/v1/tenant-api-credentials/{stored.tenant_api_credential_id}",
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.TenantApiCredentialPresentmentService.list_tenant_api_credentials",
            side_effect=TenantApiCredentialPresentmentQueryError("api_credential_not_found"),
        ):
            not_found_status, not_found_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/tenant-api-credentials",
                query={"tenant_reference": TENANT_ONE},
                headers={"Authorization": f"Bearer {secret}"},
            )
        self.assertEqual(not_found_status, 404)
        self.assertEqual(not_found_body["rejection_reason_code"], "api_credential_not_found")
        with mock.patch(
            "metering_billing.http_app.TenantApiCredentialPresentmentService.list_tenant_api_credentials",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/tenant-api-credentials",
                query={"tenant_reference": TENANT_ONE},
                headers={"Authorization": f"Bearer {secret}"},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = TenantApiCredentialPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(TenantApiCredentialPresentmentQueryError):
            empty.list_tenant_api_credentials(TENANT_ONE)
        service = TenantApiCredentialPresentmentService(ledger)
        listed = service.list_tenant_api_credentials(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.tenant_api_credentials), 2)
        with self.assertRaises(TenantApiCredentialPresentmentQueryError):
            service.list_tenant_api_credentials(TENANT_ONE, page_limit=True)
        with self.assertRaises(TenantApiCredentialPresentmentQueryError):
            service.list_tenant_api_credentials(TENANT_ONE, page_limit=101)
        with self.assertRaises(TenantApiCredentialPresentmentQueryError):
            service.list_tenant_api_credentials(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(TenantApiCredentialPresentmentQueryError):
            service.list_tenant_api_credentials(TENANT_ONE, page_limit="abc")
        with self.assertRaises(TenantApiCredentialPresentmentQueryError):
            service.list_tenant_api_credentials(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(app, "GET", "/v1/tenant-api-credentials")
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        default_limit = service.list_tenant_api_credentials(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.tenant_api_credentials), 2)
        empty_limit = service.list_tenant_api_credentials(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.tenant_api_credentials), 2)
        self.assertEqual(
            service.list_tenant_api_credentials(TENANT_ONE, page_limit=50).next_cursor,
            None,
        )
        with self.assertRaises(TenantApiCredentialPresentmentQueryError):
            service.present_tenant_api_credential(TENANT_ONE, uuid4())
        with self.assertRaises(TenantApiCredentialPresentmentQueryError):
            service.present_tenant_api_credential("", stored.tenant_api_credential_id)
        self.assertEqual(listed.tenant_api_credentials[0].issued_at, ISSUED_MORNING)
        with mock.patch(
            "metering_billing.http_app.TenantApiCredentialService.issue_credential",
            side_effect=ValueError("closed"),
        ):
            issue_value_status, issue_value_body = invoke_http(
                create_http_app(seed_rated_ledger()),
                "POST",
                "/v1/tenant-api-credentials",
                {"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(issue_value_status, 422)
        self.assertEqual(issue_value_body["rejection_reason_code"], "request_invalid")
