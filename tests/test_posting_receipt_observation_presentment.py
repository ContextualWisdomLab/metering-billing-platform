"""Posting-receipt observation HTTP presentment tests for tenant-scoped reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest import mock
from urllib.parse import quote
from uuid import uuid4

from metering_billing import (
    PostingReceiptObservationPresentmentService,
    create_http_app,
)
from metering_billing.contracts import validate_posting_receipt_observation_presentment
from metering_billing.errors import PostingReceiptObservationPresentmentQueryError
from metering_billing.posting_receipt_observation_presentment import next_operator_action
from metering_billing.usage_ledger import StoredPostingReceiptObservation, generate_record_id
from test_http_app import invoke_http
from test_posting_receipt_observation import (
    ScriptedAisClient,
    _lookup_result,
    make_ais_receipt,
    persist_known_ar_and_cash_proposals,
)
from test_usage_ingestion import TENANT_ONE, TENANT_TWO


def insert_observation(
    ledger,
    tenant_reference,
    idempotency_key,
    *,
    observed_at,
    posting_status_code="posted",
    source_proposal_id=None,
):
    """Persist one stored #16 observation without calling AIS."""
    tenant = ledger.require_tenant(tenant_reference)
    stored = ledger.insert_posting_receipt_observation(
        StoredPostingReceiptObservation(
            posting_receipt_observation_id=generate_record_id(),
            tenant_account_id=tenant.tenant_account_id,
            receipt_id=generate_record_id(),
            receipt_contract_version=1,
            idempotency_key=idempotency_key,
            source_proposal_id=source_proposal_id or generate_record_id(),
            source_payload_hash="sha256:" + ("a" * 64),
            legal_entity_reference="urn:cwl:entity_001",
            accounting_book_reference="urn:cwl:book_primary",
            accounting_policy_version="policy-2026.1",
            posting_rule_version="rule-2026.1",
            posting_status_code=posting_status_code,
            recorded_at="2026-08-17T18:00:00Z",
            fiscal_period_reference=None,
            journal_reference=None,
            reversal_of_journal_reference=None,
            hold_reason_code=None,
            rejection_reason_code=None,
            posted_at="2026-08-17T18:05:00Z" if posting_status_code == "posted" else None,
            line_count=2 if posting_status_code == "posted" else None,
            transaction_currency="USD",
            functional_currency="USD",
            observed_at=observed_at,
        )
    )
    return stored


class PostingReceiptObservationPresentmentTests(unittest.TestCase):
    """Verify stored-observation GET, list envelope, and fail-closed isolation."""

    def test_stored_observation_projects_status_and_wait(self) -> None:
        """A stored posted observation shows AIS status and wait, not proposal_status."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        stored = insert_observation(
            ledger,
            TENANT_ONE,
            str(ar_proposal.idempotency_key),
            observed_at="2026-08-17T21:00:00Z",
            source_proposal_id=ar_proposal.proposal_id,
        )
        first = PostingReceiptObservationPresentmentService(ledger).present_posting_receipt_observation(
            TENANT_ONE, stored.idempotency_key
        )
        second = PostingReceiptObservationPresentmentService(ledger).present_posting_receipt_observation(
            TENANT_ONE, stored.idempotency_key
        )
        self.assertEqual(first.as_contract_dict(), second.as_contract_dict())
        self.assertEqual(first.posting_receipt_observation_id, stored.posting_receipt_observation_id)
        self.assertEqual(first.tenant_reference, TENANT_ONE)
        self.assertEqual(first.source_proposal_id, ar_proposal.proposal_id)
        self.assertEqual(first.idempotency_key, stored.idempotency_key)
        self.assertEqual(first.posting_status_code, "posted")
        self.assertEqual(first.next_operator_action, "wait")
        payload = first.as_contract_dict()
        self.assertEqual(validate_posting_receipt_observation_presentment(payload), ())
        self.assertNotIn("items", payload)
        self.assertNotIn("cursor", payload)
        self.assertNotIn("posting_receipt_observation_outcome_code", payload)
        self.assertNotIn("proposal_status", payload)
        self.assertNotIn("card_pan", payload)
        self.assertEqual(ledger.get_journal_proposal(ar_proposal.proposal_id).proposal_status, "validated")

    def test_http_get_keeps_item_and_adds_list_envelope(self) -> None:
        """Item GET stays #16; list uses {posting_receipt_observations, next_cursor}."""
        ledger, ar_proposal, cash_proposal = persist_known_ar_and_cash_proposals()
        first = insert_observation(
            ledger,
            TENANT_ONE,
            str(ar_proposal.idempotency_key),
            observed_at="2026-08-17T21:00:00Z",
            source_proposal_id=ar_proposal.proposal_id,
        )
        second = insert_observation(
            ledger,
            TENANT_ONE,
            str(cash_proposal.idempotency_key),
            observed_at="2026-08-17T22:00:00Z",
            source_proposal_id=cash_proposal.proposal_id,
        )
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/posting-receipt-observations/{quote(first.idempotency_key, safe='')}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["posting_receipt_observation_id"], str(first.posting_receipt_observation_id))
        self.assertEqual(body["posting_status_code"], "posted")
        self.assertEqual(body["idempotency_key"], first.idempotency_key)
        self.assertEqual(body["posting_receipt_observation_outcome_code"], "accepted")
        self.assertNotIn("proposal_status", body)
        replay_status, replay_body = invoke_http(
            app,
            "GET",
            f"/v1/posting-receipt-observations/{quote(first.idempotency_key, safe='')}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body, body)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/posting-receipt-observations",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"posting_receipt_observations", "next_cursor"})
        self.assertNotIn("items", list_body)
        self.assertNotIn("cursor", list_body)
        self.assertEqual(len(list_body["posting_receipt_observations"]), 1)
        self.assertIsNotNone(list_body["next_cursor"])
        first_summary = list_body["posting_receipt_observations"][0]
        self.assertEqual(
            set(first_summary),
            {
                "posting_receipt_observation_id",
                "idempotency_key",
                "posting_status_code",
                "observed_at",
                "next_operator_action",
            },
        )
        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/posting-receipt-observations",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(list_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["posting_receipt_observations"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        listed_ids = {
            first_summary["posting_receipt_observation_id"],
            second_body["posting_receipt_observations"][0]["posting_receipt_observation_id"],
        }
        self.assertEqual(
            listed_ids,
            {
                str(first.posting_receipt_observation_id),
                str(second.posting_receipt_observation_id),
            },
        )
        ledger.register_tenant(TENANT_TWO)
        empty_status, empty_body = invoke_http(
            app,
            "GET",
            "/v1/posting-receipt-observations",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["posting_receipt_observations"], [])
        self.assertIsNone(empty_body["next_cursor"])

    def test_http_post_stays_the_existing_pull_and_refuses_card_data(self) -> None:
        """POST stays the #16 pull; PAN and secrets are refused."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        key = str(ar_proposal.idempotency_key)
        receipt = make_ais_receipt(
            tenant_reference=TENANT_ONE,
            idempotency_key=key,
            source_proposal_id=str(ar_proposal.proposal_id),
            source_payload_hash=str(ar_proposal.source_payload_hash),
        )
        app = create_http_app(ledger, ais_client=ScriptedAisClient([_lookup_result(200, receipt)]))
        payload = {"tenant_reference": TENANT_ONE, "idempotency_key": key}
        status, body = invoke_http(
            app,
            "POST",
            "/v1/posting-receipt-observations",
            {**payload, "card_pan": "4111111111111111"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.posting_receipt_observations), 0)
        accepted_status, accepted_body = invoke_http(
            app, "POST", "/v1/posting-receipt-observations", payload
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["posting_receipt_observation_outcome_code"], "accepted")
        self.assertIn("posting_receipt_observation_id", accepted_body)
        self.assertEqual(ledger.get_journal_proposal(ar_proposal.proposal_id).proposal_status, "validated")

    def test_cross_tenant_and_unknown_reads_are_404_without_leak(self) -> None:
        """Missing tenant is 422; unknown or cross-tenant keys stay 404 with no status."""
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        stored = insert_observation(
            ledger,
            TENANT_ONE,
            str(ar_proposal.idempotency_key),
            observed_at="2026-08-17T21:00:00Z",
            source_proposal_id=ar_proposal.proposal_id,
        )
        ledger.register_tenant(TENANT_TWO)
        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            f"/v1/posting-receipt-observations/{quote(stored.idempotency_key, safe='')}",
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/posting-receipt-observations/{quote(stored.idempotency_key, safe='')}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "observation_not_found")
        self.assertNotIn("posting_status_code", other_body)
        self.assertNotIn("proposal_status", other_body)
        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/posting-receipt-observations/{quote('missing-key', safe='')}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "observation_not_found")
        with self.assertRaises(PostingReceiptObservationPresentmentQueryError) as crossed:
            PostingReceiptObservationPresentmentService(ledger).present_posting_receipt_observation(
                TENANT_TWO, stored.idempotency_key
            )
        self.assertEqual(crossed.exception.rejection_reason_code, "observation_not_found")

    def test_list_filters_and_helpers_fail_closed(self) -> None:
        """Illegal list filters stay 422; helpers cover page bounds and wait."""
        self.assertEqual(next_operator_action(), "wait")
        ledger, ar_proposal, _cash = persist_known_ar_and_cash_proposals()
        stored = insert_observation(
            ledger,
            TENANT_ONE,
            str(ar_proposal.idempotency_key),
            observed_at="2026-08-17T21:00:00Z",
            source_proposal_id=ar_proposal.proposal_id,
        )
        app = create_http_app(ledger)
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/posting-receipt-observations",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/posting-receipt-observations",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        method_status, method_body = invoke_http(
            app,
            "PUT",
            f"/v1/posting-receipt-observations/{quote(stored.idempotency_key, safe='')}",
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        collection_method_status, collection_method_body = invoke_http(
            app, "PUT", "/v1/posting-receipt-observations"
        )
        self.assertEqual(collection_method_status, 422)
        self.assertEqual(collection_method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.PostingReceiptObservationPresentmentService.list_posting_receipt_observations",
            side_effect=PostingReceiptObservationPresentmentQueryError("observation_not_found"),
        ):
            not_found_status, not_found_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/posting-receipt-observations",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(not_found_status, 404)
        self.assertEqual(not_found_body["rejection_reason_code"], "observation_not_found")
        with mock.patch(
            "metering_billing.http_app.PostingReceiptObservationPresentmentService.list_posting_receipt_observations",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                "/v1/posting-receipt-observations",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
        empty = PostingReceiptObservationPresentmentService()
        self.assertIsNotNone(empty.ledger)
        with self.assertRaises(PostingReceiptObservationPresentmentQueryError):
            empty.list_posting_receipt_observations(TENANT_ONE)
        service = PostingReceiptObservationPresentmentService(ledger)
        listed = service.list_posting_receipt_observations(TENANT_ONE, cursor="")
        self.assertEqual(len(listed.posting_receipt_observations), 1)
        self.assertEqual(
            len(
                ledger.list_posting_receipt_observations(
                    ledger.require_tenant(TENANT_ONE).tenant_account_id
                )
            ),
            1,
        )
        self.assertEqual(len(ledger.list_posting_receipt_observations()), 1)
        with self.assertRaises(PostingReceiptObservationPresentmentQueryError):
            service.list_posting_receipt_observations(TENANT_ONE, page_limit=True)
        with self.assertRaises(PostingReceiptObservationPresentmentQueryError):
            service.list_posting_receipt_observations(TENANT_ONE, page_limit=101)
        with self.assertRaises(PostingReceiptObservationPresentmentQueryError):
            service.list_posting_receipt_observations(TENANT_ONE, page_limit=1.5)
        with self.assertRaises(PostingReceiptObservationPresentmentQueryError):
            service.list_posting_receipt_observations(TENANT_ONE, page_limit="abc")
        with self.assertRaises(PostingReceiptObservationPresentmentQueryError):
            service.list_posting_receipt_observations(TENANT_ONE, page_limit="0")
        list_missing_status, list_missing_body = invoke_http(
            app, "GET", "/v1/posting-receipt-observations"
        )
        self.assertEqual(list_missing_status, 422)
        self.assertEqual(list_missing_body["rejection_reason_code"], "tenant_not_found")
        default_limit = service.list_posting_receipt_observations(TENANT_ONE, page_limit=None)
        self.assertEqual(len(default_limit.posting_receipt_observations), 1)
        empty_limit = service.list_posting_receipt_observations(TENANT_ONE, page_limit="")
        self.assertEqual(len(empty_limit.posting_receipt_observations), 1)
        self.assertEqual(
            service.list_posting_receipt_observations(TENANT_ONE, page_limit=50).next_cursor,
            None,
        )
        with self.assertRaises(PostingReceiptObservationPresentmentQueryError):
            service.present_posting_receipt_observation(TENANT_ONE, "missing-key")
        with self.assertRaises(PostingReceiptObservationPresentmentQueryError):
            service.present_posting_receipt_observation(TENANT_ONE, "")
        with self.assertRaises(PostingReceiptObservationPresentmentQueryError):
            service.present_posting_receipt_observation("", stored.idempotency_key)
        self.assertEqual(listed.posting_receipt_observations[0].posting_status_code, "posted")
        self.assertEqual(
            listed.posting_receipt_observations[0].observed_at,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
        )
        held = insert_observation(
            ledger,
            TENANT_ONE,
            "held-key",
            observed_at="2026-08-17T23:00:00Z",
            posting_status_code="held",
        )
        held_presentment = service.present_posting_receipt_observation(TENANT_ONE, held.idempotency_key)
        self.assertNotIn("posted_at", held_presentment.as_contract_dict())
        self.assertEqual(held_presentment.posting_status_code, "held")
        unused = uuid4()
        self.assertIsNone(unused if unused in ledger.posting_receipt_observations else None)
