"""Unapplied-cash tests for leftover parking, replay, and fail-closed reads."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    CollectionCaseService,
    PaymentIntentService,
    PaymentSettlementService,
    UnappliedCashPresentmentService,
    UnappliedCashService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_unapplied_cash,
    validate_unapplied_cash_presentment,
)
from metering_billing.errors import (
    PaymentSettlementRejectionReasonCode,
    UnappliedCashOutcomeCode,
    UnappliedCashPresentmentQueryError,
    UnappliedCashRejectionReasonCode,
)
from metering_billing.unapplied_cash import (
    UNAPPLIED_CASH_CONTRACT_VERSION,
    UnappliedCashResult,
    _format_parked_at,
    _rejected,
)
from metering_billing.usage_ledger import StoredUnappliedCash, generate_record_id
from test_http_app import invoke_http
from test_payment_intent import open_known_morning_case
from test_payment_receipt_presentment import apply_known_morning_receipt
from test_tax_assessment import insert_commercial_draft
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import KNOWN_MORNING_TOTAL


PARKED_MORNING = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
PARKED_EVENING = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
LEFTOVER = Decimal("0.001")
OVERAGE = Decimal("1.00")


class UnappliedCashTests(unittest.TestCase):
    """Verify leftover parks once against a stored receipt without rewriting #12."""

    def test_park_leftover_once_without_rewriting_receipt_or_case(self) -> None:
        """A supplied leftover parks once; replay returns the stored id."""
        ledger, payment_receipt_id, payment_intent_id, collection_case_id = (
            apply_known_morning_receipt()
        )
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_receipts = len(ledger.payment_receipts)
        prior_write_offs = len(ledger.collection_write_offs)
        prior_settlements = len(ledger.collection_case_settlements)
        remaining_before = ledger.collection_cases[collection_case_id].outstanding_amount
        first = UnappliedCashService(
            ledger, clock=lambda: PARKED_MORNING
        ).park_unapplied_cash(TENANT_ONE, payment_receipt_id, unapplied_amount=LEFTOVER)
        second = UnappliedCashService(
            ledger, clock=lambda: PARKED_EVENING
        ).park_unapplied_cash(TENANT_ONE, payment_receipt_id, unapplied_amount=LEFTOVER)
        self.assertEqual(first.unapplied_cash_outcome_code, UnappliedCashOutcomeCode.ACCEPTED)
        self.assertEqual(
            second.unapplied_cash_outcome_code, UnappliedCashOutcomeCode.DUPLICATE_REPLAY
        )
        self.assertEqual(first.unapplied_cash_id, second.unapplied_cash_id)
        self.assertEqual(first.payment_receipt_id, payment_receipt_id)
        self.assertEqual(first.payment_intent_id, payment_intent_id)
        self.assertEqual(first.collection_case_id, collection_case_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.unapplied_amount, LEFTOVER)
        self.assertEqual(first.received_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.applied_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(first.unapplied_cash_status, "parked")
        self.assertEqual(first.next_operator_action, "wait")
        self.assertEqual(first.parked_at, PARKED_MORNING)
        self.assertEqual(second.parked_at, PARKED_MORNING)
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        payload = first.as_contract_dict()
        self.assertEqual(validate_unapplied_cash(payload), ())
        self.assertIsInstance(payload["unapplied_amount"], str)
        self.assertNotIsInstance(payload["unapplied_amount"], float)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("legal_invoice_number", payload)
        self.assertNotIn("statutory_account_id", payload)
        stored_receipt = ledger.get_payment_receipt(payment_receipt_id)
        self.assertEqual(stored_receipt.received_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(
            ledger.collection_cases[collection_case_id].outstanding_amount, remaining_before
        )
        self.assertEqual(len(ledger.unapplied_cash), 1)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_write_offs), prior_write_offs)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        presented = UnappliedCashPresentmentService(ledger).present_unapplied_cash(
            TENANT_ONE, first.unapplied_cash_id
        )
        self.assertEqual(presented.unapplied_amount, LEFTOVER)
        self.assertEqual(presented.next_operator_action, "wait")
        self.assertEqual(validate_unapplied_cash_presentment(presented.as_contract_dict()), ())

    def test_twelve_still_rejects_overpay_and_omit_is_already_consumed(self) -> None:
        """#12 still fail-closes overpay; omitted leftover is already consumed."""
        ledger, payment_intent_id, _collection_case_id = _project_morning_intent()
        overpay = PaymentSettlementService(ledger).record_payment_receipt(
            TENANT_ONE, payment_intent_id, OVERAGE
        )
        self.assertEqual(
            overpay.rejection_reason_code,
            PaymentSettlementRejectionReasonCode.PAYMENT_AMOUNT_EXCEEDS_OUTSTANDING,
        )
        ledger, payment_receipt_id, _intent_id, _case_id = apply_known_morning_receipt()
        omitted = UnappliedCashService(ledger).park_unapplied_cash(
            TENANT_ONE, payment_receipt_id
        )
        self.assertEqual(
            omitted.rejection_reason_code,
            UnappliedCashRejectionReasonCode.PAYMENT_RECEIPT_ALREADY_CONSUMED,
        )
        self.assertEqual(len(ledger.unapplied_cash), 0)

    def test_fail_closed_on_zero_negative_exceeds_and_currency(self) -> None:
        """Zero, negative, oversized leftover, and currency mismatch refuse."""
        ledger, payment_receipt_id, _intent_id, _case_id = apply_known_morning_receipt()
        service = UnappliedCashService(ledger)
        zero = service.park_unapplied_cash(
            TENANT_ONE, payment_receipt_id, unapplied_amount=Decimal("0")
        )
        self.assertEqual(
            zero.rejection_reason_code, UnappliedCashRejectionReasonCode.UNAPPLIED_AMOUNT_ZERO
        )
        negative = service.park_unapplied_cash(
            TENANT_ONE, payment_receipt_id, unapplied_amount=Decimal("-1")
        )
        self.assertEqual(
            negative.rejection_reason_code,
            UnappliedCashRejectionReasonCode.UNAPPLIED_AMOUNT_NEGATIVE,
        )
        exceeds = service.park_unapplied_cash(
            TENANT_ONE, payment_receipt_id, unapplied_amount=OVERAGE
        )
        self.assertEqual(
            exceeds.rejection_reason_code,
            UnappliedCashRejectionReasonCode.UNAPPLIED_AMOUNT_EXCEEDS_RECEIPT,
        )
        currency = service.park_unapplied_cash(
            TENANT_ONE, payment_receipt_id, unapplied_amount=LEFTOVER, currency_code="EUR"
        )
        self.assertEqual(
            currency.rejection_reason_code, UnappliedCashRejectionReasonCode.CURRENCY_MISMATCH
        )
        self.assertEqual(len(ledger.unapplied_cash), 0)

    def test_missing_and_cross_tenant_receipts_fail_closed(self) -> None:
        """A tenant cannot park leftover on another tenant's receipt."""
        ledger, payment_receipt_id, _intent_id, _case_id = apply_known_morning_receipt()
        service = UnappliedCashService(ledger)
        missing_tenant = service.park_unapplied_cash(
            "urn:cwl:missing", payment_receipt_id, unapplied_amount=LEFTOVER
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            UnappliedCashRejectionReasonCode.TENANT_NOT_FOUND,
        )
        missing_receipt = service.park_unapplied_cash(
            TENANT_ONE, generate_record_id(), unapplied_amount=LEFTOVER
        )
        self.assertEqual(
            missing_receipt.rejection_reason_code,
            UnappliedCashRejectionReasonCode.PAYMENT_RECEIPT_NOT_FOUND,
        )
        crossed = service.park_unapplied_cash(
            TENANT_TWO, payment_receipt_id, unapplied_amount=LEFTOVER
        )
        self.assertEqual(
            crossed.rejection_reason_code,
            UnappliedCashRejectionReasonCode.PAYMENT_RECEIPT_NOT_FOUND,
        )
        self.assertEqual(len(ledger.unapplied_cash), 0)

    def test_resolver_and_ieee_fail_closed(self) -> None:
        """Hollow tenant resolve raises; IEEE leftover cannot be parked."""
        ledger, payment_receipt_id, _intent_id, _case_id = apply_known_morning_receipt()
        service = UnappliedCashService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.park_unapplied_cash(
                    TENANT_ONE, payment_receipt_id, unapplied_amount=LEFTOVER
                )
        floated = service.park_unapplied_cash(
            TENANT_ONE, payment_receipt_id, unapplied_amount=0.001  # type: ignore[arg-type]
        )
        self.assertEqual(
            floated.rejection_reason_code, UnappliedCashRejectionReasonCode.REQUEST_INVALID
        )
        nan_amount = service.park_unapplied_cash(
            TENANT_ONE, payment_receipt_id, unapplied_amount=Decimal("NaN")
        )
        self.assertEqual(
            nan_amount.rejection_reason_code, UnappliedCashRejectionReasonCode.REQUEST_INVALID
        )
        infinite = service.park_unapplied_cash(
            TENANT_ONE, payment_receipt_id, unapplied_amount=Decimal("Infinity")
        )
        self.assertEqual(
            infinite.rejection_reason_code, UnappliedCashRejectionReasonCode.REQUEST_INVALID
        )
        self.assertEqual(len(ledger.unapplied_cash), 0)

    def test_http_parks_and_lists_without_writing_money(self) -> None:
        """POST parks leftover; GET item and list never capture or post."""
        ledger, payment_receipt_id, _intent_id, _case_id = apply_known_morning_receipt()
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/payment-receipts/{payment_receipt_id}/unapplied-cash",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        omitted_status, omitted_body = invoke_http(
            app,
            "POST",
            f"/v1/payment-receipts/{payment_receipt_id}/unapplied-cash",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(omitted_status, 422)
        self.assertEqual(
            omitted_body["rejection_reason_code"], "payment_receipt_already_consumed"
        )
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/payment-receipts/{payment_receipt_id}/unapplied-cash",
            {
                "tenant_reference": TENANT_ONE,
                "unapplied_amount": format_exact_decimal(LEFTOVER),
                "currency_code": "USD",
            },
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["unapplied_cash_outcome_code"], "accepted")
        self.assertEqual(accepted_body["unapplied_amount"], format_exact_decimal(LEFTOVER))
        unapplied_cash_id = accepted_body["unapplied_cash_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/payment-receipts/{payment_receipt_id}/unapplied-cash",
            {
                "tenant_reference": TENANT_ONE,
                "unapplied_amount": format_exact_decimal(LEFTOVER),
            },
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["unapplied_cash_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["unapplied_cash_id"], unapplied_cash_id)
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/unapplied-cash/{unapplied_cash_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["unapplied_cash_id"], unapplied_cash_id)
        self.assertEqual(validate_unapplied_cash_presentment(get_body), ())
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/unapplied-cash/{unapplied_cash_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "unapplied_cash_not_found")
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(len(list_body["unapplied_cash"]), 1)
        self.assertIsNone(list_body["next_cursor"])
        method_status, _method_body = invoke_http(
            app,
            "POST",
            "/v1/unapplied-cash",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 422)
        number_status, number_body = invoke_http(
            app,
            "POST",
            f"/v1/payment-receipts/{payment_receipt_id}/unapplied-cash",
            {"tenant_reference": TENANT_ONE, "unapplied_amount": 1},
        )
        self.assertEqual(number_status, 422)
        self.assertEqual(number_body["rejection_reason_code"], "request_invalid")
        currency_status, currency_body = invoke_http(
            app,
            "POST",
            f"/v1/payment-receipts/{payment_receipt_id}/unapplied-cash",
            {
                "tenant_reference": TENANT_ONE,
                "unapplied_amount": format_exact_decimal(LEFTOVER),
                "currency_code": 840,
            },
        )
        self.assertEqual(currency_status, 422)
        self.assertEqual(currency_body["rejection_reason_code"], "request_invalid")
        missing_tenant_status, missing_tenant_body = invoke_http(
            app,
            "GET",
            f"/v1/unapplied-cash/{unapplied_cash_id}",
        )
        self.assertEqual(missing_tenant_status, 422)
        self.assertEqual(missing_tenant_body["rejection_reason_code"], "tenant_not_found")
        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        _, issue_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "credential_label": "operator_key"},
        )
        gated_status, gated_body = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(gated_status, 422)
        self.assertEqual(gated_body["rejection_reason_code"], "api_credential_missing")
        keyed_status, keyed_body = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash",
            query={"tenant_reference": TENANT_ONE},
            headers={"Authorization": f"Bearer {issue_body['api_credential_secret']}"},
        )
        self.assertEqual(keyed_status, 200)
        self.assertEqual(keyed_body["unapplied_cash"][0]["unapplied_cash_id"], unapplied_cash_id)

    def test_presentment_list_and_query_fail_closed(self) -> None:
        """List pages stay tenant-scoped; illegal cursor and hollow resolve fail."""
        ledger, first_id, _intent_id, _case_id = apply_known_morning_receipt()
        second_id = _second_receipt_on_same_ledger(ledger)
        UnappliedCashService(ledger, clock=lambda: PARKED_MORNING).park_unapplied_cash(
            TENANT_ONE, first_id, unapplied_amount=LEFTOVER
        )
        UnappliedCashService(ledger, clock=lambda: PARKED_EVENING).park_unapplied_cash(
            TENANT_ONE, second_id, unapplied_amount=LEFTOVER
        )
        page = UnappliedCashPresentmentService(ledger).list_unapplied_cash(
            TENANT_ONE, page_limit=1
        )
        self.assertEqual(len(page.unapplied_cash), 1)
        self.assertIsNotNone(page.next_cursor)
        next_page = UnappliedCashPresentmentService(ledger).list_unapplied_cash(
            TENANT_ONE, cursor=page.next_cursor, page_limit=1
        )
        self.assertEqual(len(next_page.unapplied_cash), 1)
        self.assertNotEqual(
            page.unapplied_cash[0].unapplied_cash_id, next_page.unapplied_cash[0].unapplied_cash_id
        )
        service = UnappliedCashPresentmentService(ledger)
        with self.assertRaises(UnappliedCashPresentmentQueryError) as raised:
            service.list_unapplied_cash(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(raised.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(UnappliedCashPresentmentQueryError) as missing:
            service.present_unapplied_cash(TENANT_ONE, uuid4())
        self.assertEqual(missing.exception.rejection_reason_code, "unapplied_cash_not_found")
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.present_unapplied_cash(TENANT_ONE, uuid4())
        empty = UnappliedCashPresentmentService()
        with self.assertRaises(UnappliedCashPresentmentQueryError) as tenant_missing:
            empty.present_unapplied_cash(TENANT_ONE, uuid4())
        self.assertEqual(tenant_missing.exception.rejection_reason_code, "tenant_not_found")
        self.assertIsNone(
            UnappliedCashPresentmentService(ledger).list_unapplied_cash(TENANT_ONE).next_cursor
        )
        self.assertEqual(
            len(service.list_unapplied_cash(TENANT_ONE, page_limit="").unapplied_cash),
            2,
        )
        with self.assertRaises(UnappliedCashPresentmentQueryError):
            service.list_unapplied_cash(TENANT_ONE, page_limit=True)
        with self.assertRaises(UnappliedCashPresentmentQueryError):
            service.list_unapplied_cash(TENANT_ONE, page_limit=object())
        with self.assertRaises(UnappliedCashPresentmentQueryError):
            service.list_unapplied_cash(TENANT_ONE, page_limit="ten")
        with self.assertRaises(UnappliedCashPresentmentQueryError):
            service.list_unapplied_cash(TENANT_ONE, page_limit=0)
        with self.assertRaises(UnappliedCashPresentmentQueryError):
            service.list_unapplied_cash(TENANT_ONE, page_limit=101)

    def test_contract_and_result_helpers_fail_closed(self) -> None:
        """Accepted leftover stays exact; rejected leftover stays sparse."""
        ledger, payment_receipt_id, _intent_id, _case_id = apply_known_morning_receipt()
        accepted = UnappliedCashService(ledger).park_unapplied_cash(
            TENANT_ONE, payment_receipt_id, unapplied_amount=LEFTOVER
        )
        self.assertEqual(validate_unapplied_cash(accepted.as_contract_dict()), ())
        rejected = _rejected(UnappliedCashRejectionReasonCode.PAYMENT_RECEIPT_NOT_FOUND)
        self.assertEqual(validate_unapplied_cash(rejected.as_contract_dict()), ())
        self.assertNotIn("unapplied_cash_id", rejected.as_contract_dict())
        missing_id = {
            "unapplied_cash_contract_version": 1,
            "unapplied_cash_outcome_code": "accepted",
        }
        self.assertTrue(validate_unapplied_cash(missing_id))
        self.assertTrue(validate_unapplied_cash(["not-an-object"]))
        unknown = {
            "unapplied_cash_contract_version": 1,
            "unapplied_cash_outcome_code": "posted",
        }
        self.assertTrue(validate_unapplied_cash(unknown))
        missing_outcome = {"unapplied_cash_contract_version": 1}
        self.assertTrue(validate_unapplied_cash(missing_outcome))
        legal = dict(accepted.as_contract_dict())
        legal["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_unapplied_cash(legal))
        rejected_legal = rejected.as_contract_dict()
        rejected_legal["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_unapplied_cash(rejected_legal))
        rejected_missing = {
            "unapplied_cash_contract_version": 1,
            "unapplied_cash_outcome_code": "rejected",
        }
        self.assertTrue(validate_unapplied_cash(rejected_missing))
        zero_amount = dict(accepted.as_contract_dict())
        zero_amount["unapplied_amount"] = "0"
        self.assertTrue(validate_unapplied_cash(zero_amount))
        bad_amount = dict(accepted.as_contract_dict())
        bad_amount["unapplied_amount"] = "not-decimal"
        self.assertTrue(validate_unapplied_cash(bad_amount))
        int_amount = dict(accepted.as_contract_dict())
        int_amount["unapplied_amount"] = 1
        self.assertTrue(validate_unapplied_cash(int_amount))
        presentment = UnappliedCashPresentmentService(ledger).present_unapplied_cash(
            TENANT_ONE, accepted.unapplied_cash_id
        ).as_contract_dict()
        self.assertEqual(validate_unapplied_cash_presentment(presentment), ())
        self.assertTrue(validate_unapplied_cash_presentment(["not-an-object"]))
        missing_presentment = dict(presentment)
        del missing_presentment["unapplied_amount"]
        self.assertTrue(validate_unapplied_cash_presentment(missing_presentment))
        zero_presentment = dict(presentment)
        zero_presentment["unapplied_amount"] = "0"
        self.assertTrue(validate_unapplied_cash_presentment(zero_presentment))
        wait_mismatch = dict(presentment)
        wait_mismatch["next_operator_action"] = "apply"
        self.assertTrue(validate_unapplied_cash_presentment(wait_mismatch))
        forbidden = dict(presentment)
        forbidden["card_pan"] = "4111111111111111"
        self.assertTrue(validate_unapplied_cash_presentment(forbidden))
        float_amount = dict(presentment)
        float_amount["unapplied_amount"] = 0.001
        self.assertTrue(validate_unapplied_cash_presentment(float_amount))
        bad_presentment = dict(presentment)
        bad_presentment["unapplied_amount"] = "not-decimal"
        self.assertTrue(validate_unapplied_cash_presentment(bad_presentment))
        unsupported = UnappliedCashResult(
            unapplied_cash_outcome_code="posted",  # type: ignore[arg-type]
            unapplied_cash_contract_version=1,
            unapplied_cash_id=None,
            payment_receipt_id=None,
            payment_intent_id=None,
            collection_case_id=None,
            tenant_reference=None,
            currency_code=None,
            unapplied_amount=None,
            received_amount=None,
            applied_amount=None,
            unapplied_cash_status=None,
            parked_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(ValueError, "unsupported unapplied cash outcome"):
            unsupported.as_contract_dict()
        self.assertTrue(_format_parked_at(PARKED_MORNING).endswith("Z"))
        with self.assertRaisesRegex(ValueError, "accepted unapplied cash must include parked_at"):
            _format_parked_at(None)

    def test_ledger_insert_fail_closed_branches(self) -> None:
        """Ledger insert refuses invalid status, leftover, and duplicate identity."""
        ledger, payment_receipt_id, payment_intent_id, collection_case_id = (
            apply_known_morning_receipt()
        )
        receipt = ledger.get_payment_receipt(payment_receipt_id)
        self.assertIsNotNone(receipt)
        accepted = UnappliedCashService(ledger).park_unapplied_cash(
            TENANT_ONE, payment_receipt_id, unapplied_amount=LEFTOVER
        )
        self.assertEqual(accepted.unapplied_cash_outcome_code, UnappliedCashOutcomeCode.ACCEPTED)
        second_receipt_id = _second_receipt_on_same_ledger(ledger)
        second_receipt = ledger.get_payment_receipt(second_receipt_id)
        self.assertIsNotNone(second_receipt)
        invalid_status = StoredUnappliedCash(
            unapplied_cash_id=generate_record_id(),
            tenant_account_id=receipt.tenant_account_id,
            payment_receipt_id=second_receipt_id,
            payment_intent_id=second_receipt.payment_intent_id,
            collection_case_id=second_receipt.collection_case_id,
            unapplied_cash_contract_version=UNAPPLIED_CASH_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("ab" * 32),
            currency_code="USD",
            unapplied_amount=LEFTOVER,
            received_amount=second_receipt.received_amount,
            applied_amount=second_receipt.received_amount,
            unapplied_cash_status="applied",
            parked_at=PARKED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "unapplied_cash_status must be parked"):
            ledger.insert_unapplied_cash(invalid_status)
        zero_row = StoredUnappliedCash(
            unapplied_cash_id=generate_record_id(),
            tenant_account_id=receipt.tenant_account_id,
            payment_receipt_id=second_receipt_id,
            payment_intent_id=second_receipt.payment_intent_id,
            collection_case_id=second_receipt.collection_case_id,
            unapplied_cash_contract_version=UNAPPLIED_CASH_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("cd" * 32),
            currency_code="USD",
            unapplied_amount=Decimal("0"),
            received_amount=second_receipt.received_amount,
            applied_amount=second_receipt.received_amount,
            unapplied_cash_status="parked",
            parked_at=PARKED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "positive exact decimal"):
            ledger.insert_unapplied_cash(zero_row)
        exceeds_row = StoredUnappliedCash(
            unapplied_cash_id=generate_record_id(),
            tenant_account_id=receipt.tenant_account_id,
            payment_receipt_id=second_receipt_id,
            payment_intent_id=second_receipt.payment_intent_id,
            collection_case_id=second_receipt.collection_case_id,
            unapplied_cash_contract_version=UNAPPLIED_CASH_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("ef" * 32),
            currency_code="USD",
            unapplied_amount=Decimal("21.00"),
            received_amount=second_receipt.received_amount,
            applied_amount=second_receipt.received_amount,
            unapplied_cash_status="parked",
            parked_at=PARKED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "cannot exceed the stored receipt"):
            ledger.insert_unapplied_cash(exceeds_row)
        duplicate_id = StoredUnappliedCash(
            unapplied_cash_id=accepted.unapplied_cash_id,
            tenant_account_id=receipt.tenant_account_id,
            payment_receipt_id=second_receipt_id,
            payment_intent_id=second_receipt.payment_intent_id,
            collection_case_id=second_receipt.collection_case_id,
            unapplied_cash_contract_version=UNAPPLIED_CASH_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("11" * 32),
            currency_code="USD",
            unapplied_amount=LEFTOVER,
            received_amount=second_receipt.received_amount,
            applied_amount=second_receipt.received_amount,
            unapplied_cash_status="parked",
            parked_at=PARKED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "unapplied_cash_id already stored"):
            ledger.insert_unapplied_cash(duplicate_id)
        duplicate_identity = StoredUnappliedCash(
            unapplied_cash_id=generate_record_id(),
            tenant_account_id=receipt.tenant_account_id,
            payment_receipt_id=payment_receipt_id,
            payment_intent_id=payment_intent_id,
            collection_case_id=collection_case_id,
            unapplied_cash_contract_version=UNAPPLIED_CASH_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("22" * 32),
            currency_code="USD",
            unapplied_amount=LEFTOVER,
            received_amount=receipt.received_amount,
            applied_amount=receipt.received_amount,
            unapplied_cash_status="parked",
            parked_at=PARKED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "immutable and cannot be replaced"):
            ledger.insert_unapplied_cash(duplicate_identity)


def _project_morning_intent():
    """Project one morning intent without applying a receipt."""
    ledger, collection_case_id = open_known_morning_case()
    intent = PaymentIntentService(ledger).project_payment_intent(TENANT_ONE, collection_case_id)
    if intent.payment_intent_id is None:
        raise AssertionError("morning path must project a payment intent")
    return ledger, intent.payment_intent_id, collection_case_id


def _second_receipt_on_same_ledger(ledger):
    """Apply a second same-tenant receipt without creating a second ledger."""
    draft_id = insert_commercial_draft(ledger, TENANT_ONE, "USD", Decimal("20.00"))
    case = CollectionCaseService(ledger).open_collection_case(TENANT_ONE, draft_id)
    intent = PaymentIntentService(ledger).project_payment_intent(
        TENANT_ONE, case.collection_case_id
    )
    if intent.payment_intent_id is None:
        raise AssertionError("second path must project a payment intent")
    receipt = PaymentSettlementService(ledger).record_payment_receipt(
        TENANT_ONE, intent.payment_intent_id, Decimal("20.00")
    )
    if receipt.payment_receipt_id is None:
        raise AssertionError("second path must apply a payment receipt")
    return receipt.payment_receipt_id


if __name__ == "__main__":
    unittest.main()
