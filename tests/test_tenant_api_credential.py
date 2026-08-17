"""Tenant API credential tests for issue-once secrets and HTTP auth."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from unittest import mock
from uuid import uuid4

from metering_billing import (
    TenantApiCredentialService,
    create_http_app,
)
from metering_billing.contracts import validate_tenant_api_credential
from metering_billing.errors import (
    TenantApiCredentialOutcomeCode,
    TenantApiCredentialQueryError,
    TenantApiCredentialRejectionReasonCode,
)
from metering_billing.tenant_api_credential import (
    DEFAULT_CREDENTIAL_PEPPER,
    TenantApiCredentialResult,
    hash_api_credential_secret,
)
from metering_billing.usage_ledger import StoredTenantApiCredential, generate_record_id
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import seed_rated_ledger


class TenantApiCredentialTests(unittest.TestCase):
    """Verify bootstrap, keyed hashes, revoke, and tenant-isolated HTTP auth."""

    def test_issue_returns_secret_once_and_never_replays_the_same_secret(self) -> None:
        """A second issue of the same label mints a new secret and a new row."""
        ledger = seed_rated_ledger()
        service = TenantApiCredentialService(ledger, credential_pepper="test_pepper_one")
        first = service.issue_credential(TENANT_ONE, "operator_key")
        second = service.issue_credential(TENANT_ONE, "operator_key")
        self.assertEqual(first.tenant_api_credential_outcome_code, TenantApiCredentialOutcomeCode.ACCEPTED)
        self.assertEqual(second.tenant_api_credential_outcome_code, TenantApiCredentialOutcomeCode.ACCEPTED)
        self.assertIsNotNone(first.api_credential_secret)
        self.assertIsNotNone(second.api_credential_secret)
        self.assertNotEqual(first.api_credential_secret, second.api_credential_secret)
        self.assertNotEqual(first.tenant_api_credential_id, second.tenant_api_credential_id)
        self.assertTrue(first.api_credential_secret.startswith(first.credential_prefix))
        stored = ledger.get_tenant_api_credential(first.tenant_api_credential_id)
        assert stored is not None
        self.assertEqual(
            stored.credential_secret_hash,
            hash_api_credential_secret(first.api_credential_secret, "test_pepper_one"),
        )
        self.assertNotIn(first.api_credential_secret, stored.credential_secret_hash)
        payload = first.as_contract_dict()
        self.assertEqual(validate_tenant_api_credential(payload), ())
        self.assertIn("api_credential_secret", payload)
        listed = service.list_credentials(TENANT_ONE)
        list_payload = listed.as_contract_dict()
        self.assertEqual(len(list_payload["tenant_api_credentials"]), 2)
        for item in list_payload["tenant_api_credentials"]:
            self.assertNotIn("api_credential_secret", item)
            self.assertNotIn("credential_secret_hash", item)
            self.assertEqual(
                set(item),
                {
                    "tenant_api_credential_id",
                    "credential_label",
                    "credential_prefix",
                    "credential_status",
                    "issued_at",
                },
            )

    def test_http_bootstrap_then_requires_key_and_keeps_healthz_open(self) -> None:
        """Zero keys keep the tenant pin; after issue, /v1 needs the secret."""
        ledger = seed_rated_ledger()
        app = create_http_app(ledger)
        bootstrap_status, bootstrap_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(bootstrap_status, 200)
        self.assertEqual(bootstrap_body["journal_proposals"], [])
        issue_status, issue_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "credential_label": "operator_key"},
        )
        self.assertEqual(issue_status, 200)
        secret = issue_body["api_credential_secret"]
        self.assertTrue(secret.startswith(issue_body["credential_prefix"]))
        self.assertEqual(validate_tenant_api_credential(issue_body), ())
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "api_credential_missing")
        valid_status, valid_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(valid_status, 200)
        self.assertEqual(valid_body["journal_proposals"], [])
        header_status, _header_body = invoke_http(
            app,
            "GET",
            "/v1/tenant-api-credentials",
            query={"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Api-Key": secret},
        )
        self.assertEqual(header_status, 200)
        list_missing_status, list_missing_body = invoke_http(
            app,
            "GET",
            "/v1/tenant-api-credentials",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "api_credential_missing")
        health_status, health_body = invoke_http(app, "GET", "/healthz")
        self.assertEqual(health_status, 200)
        self.assertEqual(health_body, {"status": "ok"})
        second_issue_status, second_issue_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "credential_label": "operator_key"},
        )
        self.assertEqual(second_issue_status, 200)
        self.assertNotEqual(second_issue_body["api_credential_secret"], secret)

    def test_revoked_and_cross_tenant_keys_fail_closed(self) -> None:
        """Revoked keys and tenant-A keys cannot read tenant B."""
        ledger = seed_rated_ledger()
        app = create_http_app(ledger)
        _, one_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "credential_label": "operator_key"},
        )
        one_secret = one_body["api_credential_secret"]
        credential_id = one_body["tenant_api_credential_id"]
        revoke_status, revoke_body = invoke_http(
            app,
            "POST",
            f"/v1/tenant-api-credentials/{credential_id}/revoke",
            {"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {one_secret}"},
        )
        self.assertEqual(revoke_status, 200)
        self.assertEqual(revoke_body["credential_status"], "revoked")
        self.assertNotIn("api_credential_secret", revoke_body)
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/tenant-api-credentials/{credential_id}/revoke",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["credential_status"], "revoked")
        bootstrap_again_status, bootstrap_again_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(bootstrap_again_status, 200)
        self.assertEqual(bootstrap_again_body["journal_proposals"], [])
        revoked_status, revoked_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {one_secret}"},
        )
        self.assertEqual(revoked_status, 422)
        self.assertEqual(revoked_body["rejection_reason_code"], "api_credential_invalid")
        _, two_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_TWO, "credential_label": "operator_key"},
        )
        two_secret = two_body["api_credential_secret"]
        crossed_status, crossed_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_TWO},
            headers={"Authorization": f"Bearer {one_secret}"},
        )
        self.assertEqual(crossed_status, 422)
        self.assertIn(crossed_body["rejection_reason_code"], {"api_credential_invalid", "request_invalid"})
        other_pin_status, other_pin_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {two_secret}"},
        )
        self.assertEqual(other_pin_status, 422)
        self.assertEqual(other_pin_body["rejection_reason_code"], "request_invalid")
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_TWO},
            headers={"Authorization": "Bearer not-a-stored-secret"},
        )
        self.assertEqual(unknown_status, 422)
        self.assertEqual(unknown_body["rejection_reason_code"], "api_credential_invalid")

    def test_fail_closed_labels_headers_and_missing_tenant(self) -> None:
        """Invalid labels, header mismatch, and missing tenants reject."""
        ledger = seed_rated_ledger()
        service = TenantApiCredentialService(ledger)
        rejected = service.issue_credential(TENANT_ONE, "key")
        self.assertEqual(rejected.tenant_api_credential_outcome_code, TenantApiCredentialOutcomeCode.REJECTED)
        self.assertEqual(
            rejected.rejection_reason_code,
            TenantApiCredentialRejectionReasonCode.CREDENTIAL_LABEL_INVALID,
        )
        missing_tenant = service.issue_credential("", "operator_key")
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            TenantApiCredentialRejectionReasonCode.TENANT_NOT_FOUND,
        )
        with self.assertRaises(TenantApiCredentialQueryError) as missing_list:
            service.list_credentials("")
        self.assertEqual(missing_list.exception.rejection_reason_code, "tenant_not_found")
        issued = service.issue_credential(TENANT_ONE)
        self.assertEqual(issued.credential_label, "operator_key")
        with self.assertRaises(TenantApiCredentialQueryError):
            service.revoke_credential(TENANT_TWO, issued.tenant_api_credential_id)
        with self.assertRaises(TenantApiCredentialQueryError):
            service.revoke_credential(TENANT_ONE, uuid4())
        app = create_http_app(ledger)
        secret = issued.api_credential_secret
        mismatch_status, mismatch_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
            headers={
                "Authorization": f"Bearer {secret}",
                "X-CWL-Api-Key": "other-secret",
            },
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")
        both_status, _both_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
            headers={
                "Authorization": f"Bearer {secret}",
                "X-CWL-Api-Key": secret,
            },
        )
        self.assertEqual(both_status, 200)
        bad_scheme_status, bad_scheme_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Basic {secret}"},
        )
        self.assertEqual(bad_scheme_status, 422)
        self.assertEqual(bad_scheme_body["rejection_reason_code"], "api_credential_invalid")
        empty = TenantApiCredentialService()
        self.assertIsNotNone(empty.ledger)
        with mock.patch.dict(os.environ, {"CWL_API_CREDENTIAL_PEPPER": "env_pepper"}):
            env_service = TenantApiCredentialService(seed_rated_ledger())
            env_issued = env_service.issue_credential(TENANT_ONE, "operator_key")
            self.assertTrue(
                env_issued.as_contract_dict()["api_credential_secret"].startswith(
                    env_issued.credential_prefix
                )
            )
        self.assertTrue(DEFAULT_CREDENTIAL_PEPPER)
        method_status, method_body = invoke_http(app, "PUT", "/v1/tenant-api-credentials")
        self.assertEqual(method_status, 422)
        unknown_revoke_status, unknown_revoke_body = invoke_http(
            app,
            "POST",
            f"/v1/tenant-api-credentials/{uuid4()}/revoke",
            {"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(unknown_revoke_status, 404)
        self.assertEqual(unknown_revoke_body["rejection_reason_code"], "api_credential_not_found")
        with mock.patch(
            "metering_billing.http_app.TenantApiCredentialService.list_credentials",
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
        with self.assertRaises(ValueError):
            TenantApiCredentialService(ledger, credential_pepper="")
        with self.assertRaises(ValueError):
            hash_api_credential_secret(0.1, "pepper")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            hash_api_credential_secret("secret", "")
        empty_bearer_status, empty_bearer_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": "Bearer "},
        )
        self.assertEqual(empty_bearer_status, 422)
        self.assertEqual(empty_bearer_body["rejection_reason_code"], "api_credential_invalid")
        put_revoke_status, put_revoke_body = invoke_http(
            app, "PUT", f"/v1/tenant-api-credentials/{issued.tenant_api_credential_id}/revoke"
        )
        self.assertEqual(put_revoke_status, 422)
        missing_list_status, missing_list_body = invoke_http(app, "GET", "/v1/tenant-api-credentials")
        self.assertEqual(missing_list_status, 422)
        self.assertEqual(missing_list_body["rejection_reason_code"], "tenant_not_found")
        tenant = ledger.require_tenant(TENANT_ONE)
        with self.assertRaises(ValueError):
            ledger.insert_tenant_api_credential(
                StoredTenantApiCredential(
                    tenant_api_credential_id=generate_record_id(),
                    tenant_account_id=tenant.tenant_account_id,
                    tenant_api_credential_contract_version=1,
                    credential_label="operator_key",
                    credential_prefix="cwlak_xxxxxx",
                    credential_secret_hash="sha256:" + ("a" * 64),
                    credential_status="active",
                    issued_at=datetime(2026, 8, 17, 22, 0, tzinfo=UTC),
                    revoked_at=None,
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_tenant_api_credential(
                StoredTenantApiCredential(
                    tenant_api_credential_id=generate_record_id(),
                    tenant_account_id=tenant.tenant_account_id,
                    tenant_api_credential_contract_version=1,
                    credential_label="operator_key",
                    credential_prefix="cwlak_xxxxxx",
                    credential_secret_hash="hmac-sha256:" + ("b" * 64),
                    credential_status="posted",
                    issued_at=datetime(2026, 8, 17, 22, 0, tzinfo=UTC),
                    revoked_at=None,
                )
            )
        stored = ledger.get_tenant_api_credential(issued.tenant_api_credential_id)
        assert stored is not None
        with self.assertRaises(ValueError):
            ledger.insert_tenant_api_credential(stored)
        with self.assertRaises(ValueError):
            ledger.insert_tenant_api_credential(
                StoredTenantApiCredential(
                    tenant_api_credential_id=generate_record_id(),
                    tenant_account_id=tenant.tenant_account_id,
                    tenant_api_credential_contract_version=1,
                    credential_label="operator_key",
                    credential_prefix="cwlak_yyyyyy",
                    credential_secret_hash=stored.credential_secret_hash,
                    credential_status="active",
                    issued_at=datetime(2026, 8, 17, 22, 0, tzinfo=UTC),
                    revoked_at=None,
                )
            )
        with self.assertRaises(ValueError):
            ledger.revoke_tenant_api_credential(uuid4(), datetime(2026, 8, 17, 22, 0, tzinfo=UTC))
        rejected_payload = service.issue_credential("urn:cwl:missing_tenant", "operator_key").as_contract_dict()
        self.assertEqual(rejected_payload["rejection_reason_code"], "tenant_not_found")
        self.assertNotIn("api_credential_secret", rejected_payload)
        self.assertNotEqual(validate_tenant_api_credential([]), ())
        self.assertIn(
            "$: persisted hashes must not appear on the HTTP contract",
            validate_tenant_api_credential(
                {
                    "tenant_api_credential_contract_version": 1,
                    "tenant_api_credential_outcome_code": "accepted",
                    "tenant_api_credential_id": str(issued.tenant_api_credential_id),
                    "tenant_reference": TENANT_ONE,
                    "credential_label": "operator_key",
                    "credential_prefix": issued.credential_prefix,
                    "credential_status": "active",
                    "issued_at": "2026-08-17T22:00:00Z",
                    "credential_secret_hash": stored.credential_secret_hash,
                }
            ),
        )
        self.assertIn(
            "$: rejected credentials must include rejection_reason_code",
            validate_tenant_api_credential(
                {
                    "tenant_api_credential_contract_version": 1,
                    "tenant_api_credential_outcome_code": "rejected",
                    "api_credential_secret": "should-not-be-here",
                }
            ),
        )
        with self.assertRaises(ValueError):
            hash_api_credential_secret("", "pepper")
        rejected_default = TenantApiCredentialResult(
            tenant_api_credential_outcome_code="rejected",
            tenant_api_credential_contract_version=1,
            tenant_api_credential_id=None,
            tenant_reference=None,
            credential_label=None,
            credential_prefix=None,
            api_credential_secret=None,
            credential_status=None,
            issued_at=None,
            rejection_reason_code=None,
        )
        self.assertEqual(
            rejected_default.as_contract_dict()["rejection_reason_code"],
            "tenant_not_found",
        )
        first_revoke = ledger.revoke_tenant_api_credential(
            issued.tenant_api_credential_id, datetime(2026, 8, 17, 22, 1, tzinfo=UTC)
        )
        second_revoke = ledger.revoke_tenant_api_credential(
            issued.tenant_api_credential_id, datetime(2026, 8, 17, 22, 2, tzinfo=UTC)
        )
        self.assertEqual(second_revoke.credential_status, "revoked")
        self.assertEqual(second_revoke.revoked_at, first_revoke.revoked_at)
        header_issue_status, header_issue_body = invoke_http(
            create_http_app(seed_rated_ledger()),
            "POST",
            "/v1/tenant-api-credentials",
            {},
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(header_issue_status, 200)
        self.assertIn("api_credential_secret", header_issue_body)
        invalid_label_status, invalid_label_body = invoke_http(
            create_http_app(seed_rated_ledger()),
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "credential_label": "key"},
        )
        self.assertEqual(invalid_label_status, 422)
        self.assertEqual(invalid_label_body["rejection_reason_code"], "credential_label_invalid")
        array_status, array_body = invoke_http(
            create_http_app(seed_rated_ledger()),
            "POST",
            "/v1/tenant-api-credentials",
            b"[]",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(array_status, 422)
        self.assertEqual(array_body["rejection_reason_code"], "request_invalid")
        empty_issue_status, empty_issue_body = invoke_http(
            create_http_app(seed_rated_ledger()),
            "POST",
            "/v1/tenant-api-credentials",
        )
        self.assertEqual(empty_issue_status, 422)
        self.assertEqual(empty_issue_body["rejection_reason_code"], "request_invalid")
        unknown_tenant_key_status, unknown_tenant_key_body = invoke_http(
            create_http_app(),
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": "Bearer not-a-stored-secret"},
        )
        self.assertEqual(unknown_tenant_key_status, 422)
        self.assertEqual(unknown_tenant_key_body["rejection_reason_code"], "api_credential_invalid")
        gone_ledger = seed_rated_ledger()
        gone_service = TenantApiCredentialService(gone_ledger)
        gone_issued = gone_service.issue_credential(TENANT_ONE)
        del gone_ledger.tenant_accounts[TENANT_ONE]
        with self.assertRaises(TenantApiCredentialQueryError) as gone:
            gone_service.authorize_request(TENANT_ONE, gone_issued.api_credential_secret)
        self.assertEqual(gone.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(TenantApiCredentialQueryError) as empty_secret:
            service.authorize_request(TENANT_ONE, "")
        self.assertEqual(empty_secret.exception.rejection_reason_code, "api_credential_invalid")
