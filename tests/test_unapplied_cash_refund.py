"""Unapplied-cash refund tests for returning parked leftover to the payer."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    UnappliedCashApplicationService,
    UnappliedCashRefundPresentmentService,
    UnappliedCashRefundService,
    UnappliedCashService,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import (
    validate_unapplied_cash_refund,
    validate_unapplied_cash_refund_presentment,
)
from metering_billing.errors import (
    UnappliedCashApplicationRejectionReasonCode,
    UnappliedCashRefundOutcomeCode,
    UnappliedCashRefundPresentmentQueryError,
    UnappliedCashRefundRejectionReasonCode,
)
from metering_billing.unapplied_cash_refund import (
    UNAPPLIED_CASH_REFUND_CONTRACT_VERSION,
    UnappliedCashRefundResult,
    _format_refunded_at,
    _rejected,
)
from metering_billing.usage_ledger import (
    StoredUnappliedCash,
    StoredUnappliedCashRefund,
    generate_record_id,
)
from test_http_app import invoke_http
from test_unapplied_cash import LEFTOVER, PARKED_MORNING, _second_receipt_on_same_ledger
from test_unapplied_cash_application import park_leftover_and_open_second_case
from test_usage_ingestion import TENANT_ONE, TENANT_TWO


REFUNDED_MORNING = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
REFUNDED_EVENING = datetime(2026, 8, 18, 23, 0, tzinfo=UTC)


def _park_leftover():
    """Park leftover on the morning receipt without applying it to another case."""
    ledger, parked, _collection, _source_case_id, payment_receipt_id = (
        park_leftover_and_open_second_case()
    )
    return ledger, parked, payment_receipt_id


class UnappliedCashRefundTests(unittest.TestCase):
    """Verify parked leftover refunds once as a commercial fact only."""

    def test_refund_full_parked_amount_once_without_psp_or_webhook(self) -> None:
        """Full leftover refunds once; replay returns the stored id."""
        ledger, parked, payment_receipt_id = _park_leftover()
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        prior_receipts = len(ledger.payment_receipts)
        prior_write_offs = len(ledger.collection_write_offs)
        prior_settlements = len(ledger.collection_case_settlements)
        prior_applications = len(ledger.unapplied_cash_applications)
        first = UnappliedCashRefundService(
            ledger, clock=lambda: REFUNDED_MORNING
        ).refund_unapplied_cash(TENANT_ONE, parked.unapplied_cash_id)
        second = UnappliedCashRefundService(
            ledger, clock=lambda: REFUNDED_EVENING
        ).refund_unapplied_cash(TENANT_ONE, parked.unapplied_cash_id)
        self.assertEqual(
            first.unapplied_cash_refund_outcome_code,
            UnappliedCashRefundOutcomeCode.ACCEPTED,
        )
        self.assertEqual(
            second.unapplied_cash_refund_outcome_code,
            UnappliedCashRefundOutcomeCode.DUPLICATE_REPLAY,
        )
        self.assertEqual(first.unapplied_cash_refund_id, second.unapplied_cash_refund_id)
        self.assertEqual(first.unapplied_cash_id, parked.unapplied_cash_id)
        self.assertEqual(first.payment_receipt_id, payment_receipt_id)
        self.assertEqual(first.currency_code, "USD")
        self.assertEqual(first.refund_amount, LEFTOVER)
        self.assertEqual(first.unapplied_amount, LEFTOVER)
        self.assertEqual(first.unapplied_cash_refund_status, "recorded")
        self.assertEqual(first.unapplied_cash_status, "parked")
        self.assertEqual(first.next_operator_action, "wait")
        self.assertEqual(first.refunded_at, REFUNDED_MORNING)
        self.assertEqual(second.refunded_at, REFUNDED_MORNING)
        self.assertTrue(first.source_payload_hash.startswith("sha256:"))
        payload = first.as_contract_dict()
        self.assertEqual(validate_unapplied_cash_refund(payload), ())
        self.assertIsInstance(payload["refund_amount"], str)
        self.assertNotIsInstance(payload["refund_amount"], float)
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("legal_invoice_number", payload)
        self.assertNotIn("statutory_account_id", payload)
        stored_leftover = ledger.get_unapplied_cash(parked.unapplied_cash_id)
        self.assertEqual(stored_leftover.unapplied_cash_status, "parked")
        self.assertEqual(stored_leftover.unapplied_amount, LEFTOVER)
        self.assertEqual(len(ledger.unapplied_cash_refunds), 1)
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        self.assertEqual(len(ledger.payment_receipts), prior_receipts)
        self.assertEqual(len(ledger.collection_write_offs), prior_write_offs)
        self.assertEqual(len(ledger.collection_case_settlements), prior_settlements)
        self.assertEqual(len(ledger.unapplied_cash_applications), prior_applications)
        presented = UnappliedCashRefundPresentmentService(ledger).present_unapplied_cash_refund(
            TENANT_ONE, first.unapplied_cash_refund_id
        )
        self.assertEqual(presented.refund_amount, LEFTOVER)
        self.assertEqual(presented.next_operator_action, "wait")
        self.assertEqual(
            validate_unapplied_cash_refund_presentment(presented.as_contract_dict()), ()
        )

    def test_supplied_amount_must_equal_parked_leftover(self) -> None:
        """Omitting amount refunds the parked leftover; a mismatch refuses."""
        ledger, parked, _receipt_id = _park_leftover()
        matched = UnappliedCashRefundService(ledger).refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, refund_amount=LEFTOVER
        )
        self.assertEqual(
            matched.unapplied_cash_refund_outcome_code,
            UnappliedCashRefundOutcomeCode.ACCEPTED,
        )
        self.assertEqual(matched.refund_amount, LEFTOVER)

    def test_fail_closed_when_already_applied_or_not_parked(self) -> None:
        """Applied leftover and non-parked leftover refuse without writing a refund."""
        ledger, parked, collection, _source_case_id, _receipt_id = (
            park_leftover_and_open_second_case()
        )
        UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        applied = UnappliedCashRefundService(ledger).refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id
        )
        self.assertEqual(
            applied.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_ALREADY_APPLIED,
        )
        self.assertEqual(len(ledger.unapplied_cash_refunds), 0)
        other_ledger, other_parked, _receipt_id = _park_leftover()
        stored = other_ledger.get_unapplied_cash(other_parked.unapplied_cash_id)
        self.assertIsInstance(stored, StoredUnappliedCash)
        other_ledger.unapplied_cash[other_parked.unapplied_cash_id] = replace(
            stored, unapplied_cash_status="applied"
        )
        not_parked = UnappliedCashRefundService(other_ledger).refund_unapplied_cash(
            TENANT_ONE, other_parked.unapplied_cash_id
        )
        self.assertEqual(
            not_parked.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_NOT_PARKED,
        )

    def test_apply_after_refund_fail_closes(self) -> None:
        """Refund uniqueness consumes leftover; later apply refuses."""
        ledger, parked, collection, _source_case_id, _receipt_id = (
            park_leftover_and_open_second_case()
        )
        refunded = UnappliedCashRefundService(ledger).refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id
        )
        self.assertEqual(
            refunded.unapplied_cash_refund_outcome_code,
            UnappliedCashRefundOutcomeCode.ACCEPTED,
        )
        applied = UnappliedCashApplicationService(ledger).apply_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, collection.collection_case_id
        )
        self.assertEqual(
            applied.rejection_reason_code,
            UnappliedCashApplicationRejectionReasonCode.UNAPPLIED_CASH_ALREADY_REFUNDED,
        )
        self.assertEqual(len(ledger.unapplied_cash_applications), 0)

    def test_fail_closed_on_zero_negative_mismatch_and_currency(self) -> None:
        """Zero, negative, mismatched, and currency leftover refunds refuse."""
        ledger, parked, _receipt_id = _park_leftover()
        service = UnappliedCashRefundService(ledger)
        zero = service.refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, refund_amount=Decimal("0")
        )
        self.assertEqual(
            zero.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.REFUND_AMOUNT_ZERO,
        )
        negative = service.refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, refund_amount=Decimal("-1")
        )
        self.assertEqual(
            negative.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.REFUND_AMOUNT_NEGATIVE,
        )
        mismatch = service.refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, refund_amount=Decimal("1.00")
        )
        self.assertEqual(
            mismatch.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.REFUND_AMOUNT_MISMATCH,
        )
        currency = service.refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, currency_code="EUR"
        )
        self.assertEqual(
            currency.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.CURRENCY_MISMATCH,
        )
        self.assertEqual(len(ledger.unapplied_cash_refunds), 0)

    def test_missing_and_cross_tenant_inputs_fail_closed(self) -> None:
        """A tenant cannot refund another tenant's leftover."""
        ledger, parked, _receipt_id = _park_leftover()
        service = UnappliedCashRefundService(ledger)
        missing_tenant = service.refund_unapplied_cash(
            "urn:cwl:missing", parked.unapplied_cash_id
        )
        self.assertEqual(
            missing_tenant.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.TENANT_NOT_FOUND,
        )
        missing_leftover = service.refund_unapplied_cash(TENANT_ONE, generate_record_id())
        self.assertEqual(
            missing_leftover.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND,
        )
        crossed = service.refund_unapplied_cash(TENANT_TWO, parked.unapplied_cash_id)
        self.assertEqual(
            crossed.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND,
        )
        self.assertEqual(len(ledger.unapplied_cash_refunds), 0)

    def test_resolver_ieee_and_orphaned_replay_fail_closed(self) -> None:
        """Hollow resolve raises; IEEE leftover and orphaned replay refuse."""
        ledger, parked, _receipt_id = _park_leftover()
        service = UnappliedCashRefundService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.refund_unapplied_cash(TENANT_ONE, parked.unapplied_cash_id)
        floated = service.refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, refund_amount=0.001  # type: ignore[arg-type]
        )
        self.assertEqual(
            floated.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.REQUEST_INVALID,
        )
        nan_amount = service.refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, refund_amount=Decimal("NaN")
        )
        self.assertEqual(
            nan_amount.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.REQUEST_INVALID,
        )
        infinite = service.refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id, refund_amount=Decimal("Infinity")
        )
        self.assertEqual(
            infinite.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.REQUEST_INVALID,
        )
        accepted = UnappliedCashRefundService(
            ledger, clock=lambda: REFUNDED_MORNING
        ).refund_unapplied_cash(TENANT_ONE, parked.unapplied_cash_id)
        del ledger.unapplied_cash[parked.unapplied_cash_id]
        orphaned = UnappliedCashRefundService(ledger).refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id
        )
        self.assertEqual(
            orphaned.rejection_reason_code,
            UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND,
        )
        self.assertEqual(accepted.unapplied_cash_refund_outcome_code.value, "accepted")

    def test_http_refunds_lists_and_refuses_pan(self) -> None:
        """POST refunds leftover; GET item and list never capture or post."""
        ledger, parked, _receipt_id = _park_leftover()
        prior_journals = len(ledger.journal_proposals)
        prior_outbox = len(ledger.webhook_outbox_events)
        app = create_http_app(ledger)
        refused_status, refused_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash/{parked.unapplied_cash_id}/refunds",
            {"tenant_reference": TENANT_ONE, "card_pan": "4111111111111111"},
        )
        self.assertEqual(refused_status, 422)
        self.assertEqual(refused_body["rejection_reason_code"], "request_invalid")
        accepted_status, accepted_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash/{parked.unapplied_cash_id}/refunds",
            {
                "tenant_reference": TENANT_ONE,
                "refund_amount": format_exact_decimal(LEFTOVER),
                "currency_code": "USD",
            },
        )
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted_body["unapplied_cash_refund_outcome_code"], "accepted")
        self.assertEqual(accepted_body["refund_amount"], format_exact_decimal(LEFTOVER))
        refund_id = accepted_body["unapplied_cash_refund_id"]
        replay_status, replay_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash/{parked.unapplied_cash_id}/refunds",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay_body["unapplied_cash_refund_outcome_code"], "duplicate_replay")
        self.assertEqual(replay_body["unapplied_cash_refund_id"], refund_id)
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/unapplied-cash-refunds/{refund_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["unapplied_cash_refund_id"], refund_id)
        self.assertEqual(validate_unapplied_cash_refund_presentment(get_body), ())
        other_status, other_body = invoke_http(
            app,
            "GET",
            f"/v1/unapplied-cash-refunds/{refund_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_status, 404)
        self.assertEqual(other_body["rejection_reason_code"], "unapplied_cash_refund_not_found")
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash-refunds",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(len(list_body["unapplied_cash_refunds"]), 1)
        self.assertIsNone(list_body["next_cursor"])
        method_status, _method_body = invoke_http(
            app,
            "POST",
            "/v1/unapplied-cash-refunds",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(method_status, 422)
        number_status, number_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash/{parked.unapplied_cash_id}/refunds",
            {"tenant_reference": TENANT_ONE, "refund_amount": 1},
        )
        self.assertEqual(number_status, 422)
        self.assertEqual(number_body["rejection_reason_code"], "request_invalid")
        currency_status, currency_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash/{parked.unapplied_cash_id}/refunds",
            {"tenant_reference": TENANT_ONE, "currency_code": 840},
        )
        self.assertEqual(currency_status, 422)
        self.assertEqual(currency_body["rejection_reason_code"], "request_invalid")
        self.assertEqual(len(ledger.journal_proposals), prior_journals)
        self.assertEqual(len(ledger.webhook_outbox_events), prior_outbox)
        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash-refunds",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")
        missing_tenant_status, missing_tenant_body = invoke_http(
            app,
            "GET",
            f"/v1/unapplied-cash-refunds/{refund_id}",
        )
        self.assertEqual(missing_tenant_status, 422)
        self.assertEqual(missing_tenant_body["rejection_reason_code"], "tenant_not_found")
        unreadable_status, unreadable_body = invoke_http(
            app,
            "POST",
            f"/v1/unapplied-cash/{parked.unapplied_cash_id}/refunds",
            {"tenant_reference": TENANT_ONE, "refund_amount": "not-a-decimal"},
        )
        self.assertEqual(unreadable_status, 422)
        self.assertEqual(unreadable_body["rejection_reason_code"], "request_invalid")
        nested_method_status, nested_method_body = invoke_http(
            app,
            "GET",
            f"/v1/unapplied-cash/{parked.unapplied_cash_id}/refunds",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(nested_method_status, 405)
        self.assertEqual(nested_method_body["rejection_reason_code"], "method_not_allowed")
        item_method_status, _item_method = invoke_http(
            app,
            "PUT",
            f"/v1/unapplied-cash-refunds/{refund_id}",
            {"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(item_method_status, 422)
        with mock.patch(
            "metering_billing.http_app.UnappliedCashRefundPresentmentService.present_unapplied_cash_refund",
            side_effect=ValueError("boom"),
        ):
            get_boom_status, get_boom = invoke_http(
                app,
                "GET",
                f"/v1/unapplied-cash-refunds/{refund_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(get_boom_status, 422)
        self.assertEqual(get_boom["rejection_reason_code"], "request_invalid")
        page_status, page_body = invoke_http(
            app,
            "GET",
            "/v1/unapplied-cash-refunds",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(page_status, 200)
        self.assertEqual(len(page_body["unapplied_cash_refunds"]), 1)

    def test_presentment_list_and_query_fail_closed(self) -> None:
        """List pages stay tenant-scoped; illegal cursor and hollow resolve fail."""
        ledger, first_parked, _receipt_id = _park_leftover()
        second_receipt_id = _second_receipt_on_same_ledger(ledger)
        second_parked = UnappliedCashService(ledger).park_unapplied_cash(
            TENANT_ONE, second_receipt_id, unapplied_amount=LEFTOVER
        )
        UnappliedCashRefundService(ledger, clock=lambda: REFUNDED_MORNING).refund_unapplied_cash(
            TENANT_ONE, first_parked.unapplied_cash_id
        )
        UnappliedCashRefundService(ledger, clock=lambda: REFUNDED_EVENING).refund_unapplied_cash(
            TENANT_ONE, second_parked.unapplied_cash_id
        )
        page = UnappliedCashRefundPresentmentService(ledger).list_unapplied_cash_refunds(
            TENANT_ONE, page_limit=1
        )
        self.assertEqual(len(page.unapplied_cash_refunds), 1)
        self.assertIsNotNone(page.next_cursor)
        next_page = UnappliedCashRefundPresentmentService(ledger).list_unapplied_cash_refunds(
            TENANT_ONE, cursor=page.next_cursor, page_limit=1
        )
        self.assertEqual(len(next_page.unapplied_cash_refunds), 1)
        self.assertNotEqual(
            page.unapplied_cash_refunds[0].unapplied_cash_refund_id,
            next_page.unapplied_cash_refunds[0].unapplied_cash_refund_id,
        )
        service = UnappliedCashRefundPresentmentService(ledger)
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError) as raised:
            service.list_unapplied_cash_refunds(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(raised.exception.rejection_reason_code, "request_invalid")
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError) as missing:
            service.present_unapplied_cash_refund(TENANT_ONE, uuid4())
        self.assertEqual(missing.exception.rejection_reason_code, "unapplied_cash_refund_not_found")
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.present_unapplied_cash_refund(TENANT_ONE, uuid4())
        empty = UnappliedCashRefundPresentmentService()
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError) as tenant_missing:
            empty.present_unapplied_cash_refund(TENANT_ONE, uuid4())
        self.assertEqual(tenant_missing.exception.rejection_reason_code, "tenant_not_found")
        self.assertIsNone(
            UnappliedCashRefundPresentmentService(ledger)
            .list_unapplied_cash_refunds(TENANT_ONE)
            .next_cursor
        )
        self.assertEqual(
            len(service.list_unapplied_cash_refunds(TENANT_ONE, page_limit="").unapplied_cash_refunds),
            2,
        )
        self.assertEqual(
            len(service.list_unapplied_cash_refunds(TENANT_ONE, page_limit="1").unapplied_cash_refunds),
            1,
        )
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError):
            service.list_unapplied_cash_refunds(TENANT_ONE, page_limit=True)
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError):
            service.list_unapplied_cash_refunds(TENANT_ONE, page_limit=object())
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError):
            service.list_unapplied_cash_refunds(TENANT_ONE, page_limit="ten")
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError):
            service.list_unapplied_cash_refunds(TENANT_ONE, page_limit=0)
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError):
            service.list_unapplied_cash_refunds(TENANT_ONE, page_limit=101)
        first_refund_id = page.unapplied_cash_refunds[0].unapplied_cash_refund_id
        stored_refund = ledger.get_unapplied_cash_refund(first_refund_id)
        self.assertIsNotNone(stored_refund)
        del ledger.unapplied_cash[stored_refund.unapplied_cash_id]
        with self.assertRaises(UnappliedCashRefundPresentmentQueryError) as orphaned:
            service.present_unapplied_cash_refund(TENANT_ONE, first_refund_id)
        self.assertEqual(
            orphaned.exception.rejection_reason_code, "unapplied_cash_refund_not_found"
        )

    def test_contract_and_result_helpers_fail_closed(self) -> None:
        """Accepted refund stays exact; rejected refund stays sparse."""
        ledger, parked, _receipt_id = _park_leftover()
        accepted = UnappliedCashRefundService(ledger).refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id
        )
        self.assertEqual(validate_unapplied_cash_refund(accepted.as_contract_dict()), ())
        rejected = _rejected(UnappliedCashRefundRejectionReasonCode.UNAPPLIED_CASH_NOT_FOUND)
        self.assertEqual(validate_unapplied_cash_refund(rejected.as_contract_dict()), ())
        missing_reason = UnappliedCashRefundResult(
            unapplied_cash_refund_outcome_code=UnappliedCashRefundOutcomeCode.REJECTED,
            unapplied_cash_refund_contract_version=1,
            unapplied_cash_refund_id=None,
            unapplied_cash_id=None,
            payment_receipt_id=None,
            payment_intent_id=None,
            collection_case_id=None,
            tenant_reference=None,
            currency_code=None,
            refund_amount=None,
            unapplied_amount=None,
            unapplied_cash_refund_status=None,
            unapplied_cash_status=None,
            refunded_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        self.assertEqual(
            missing_reason.as_contract_dict()["rejection_reason_code"],
            "unapplied_cash_not_found",
        )
        self.assertNotIn("unapplied_cash_refund_id", rejected.as_contract_dict())
        missing_id = {
            "unapplied_cash_refund_contract_version": 1,
            "unapplied_cash_refund_outcome_code": "accepted",
        }
        self.assertTrue(validate_unapplied_cash_refund(missing_id))
        self.assertTrue(validate_unapplied_cash_refund(["not-an-object"]))
        unknown = {
            "unapplied_cash_refund_contract_version": 1,
            "unapplied_cash_refund_outcome_code": "posted",
        }
        self.assertTrue(validate_unapplied_cash_refund(unknown))
        missing_outcome = {"unapplied_cash_refund_contract_version": 1}
        self.assertTrue(validate_unapplied_cash_refund(missing_outcome))
        legal = dict(accepted.as_contract_dict())
        legal["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_unapplied_cash_refund(legal))
        rejected_legal = rejected.as_contract_dict()
        rejected_legal["legal_invoice_number"] = "INV-1"
        self.assertTrue(validate_unapplied_cash_refund(rejected_legal))
        rejected_credit = rejected.as_contract_dict()
        rejected_credit["legal_credit_note_number"] = "CN-1"
        self.assertTrue(validate_unapplied_cash_refund(rejected_credit))
        rejected_missing = {
            "unapplied_cash_refund_contract_version": 1,
            "unapplied_cash_refund_outcome_code": "rejected",
        }
        self.assertTrue(validate_unapplied_cash_refund(rejected_missing))
        zero_amount = dict(accepted.as_contract_dict())
        zero_amount["refund_amount"] = "0"
        self.assertTrue(validate_unapplied_cash_refund(zero_amount))
        bad_amount = dict(accepted.as_contract_dict())
        bad_amount["refund_amount"] = "not-decimal"
        self.assertTrue(validate_unapplied_cash_refund(bad_amount))
        int_amount = dict(accepted.as_contract_dict())
        int_amount["refund_amount"] = 1
        self.assertTrue(validate_unapplied_cash_refund(int_amount))
        presentment = (
            UnappliedCashRefundPresentmentService(ledger)
            .present_unapplied_cash_refund(TENANT_ONE, accepted.unapplied_cash_refund_id)
            .as_contract_dict()
        )
        self.assertEqual(validate_unapplied_cash_refund_presentment(presentment), ())
        self.assertTrue(validate_unapplied_cash_refund_presentment(["not-an-object"]))
        missing_presentment = dict(presentment)
        del missing_presentment["refund_amount"]
        self.assertTrue(validate_unapplied_cash_refund_presentment(missing_presentment))
        zero_presentment = dict(presentment)
        zero_presentment["refund_amount"] = "0"
        self.assertTrue(validate_unapplied_cash_refund_presentment(zero_presentment))
        wait_mismatch = dict(presentment)
        wait_mismatch["next_operator_action"] = "collect"
        self.assertTrue(validate_unapplied_cash_refund_presentment(wait_mismatch))
        forbidden = dict(presentment)
        forbidden["card_pan"] = "4111111111111111"
        self.assertTrue(validate_unapplied_cash_refund_presentment(forbidden))
        float_amount = dict(presentment)
        float_amount["refund_amount"] = 0.001
        self.assertTrue(validate_unapplied_cash_refund_presentment(float_amount))
        bad_presentment = dict(presentment)
        bad_presentment["refund_amount"] = "not-decimal"
        self.assertTrue(validate_unapplied_cash_refund_presentment(bad_presentment))
        outcome_presentment = dict(presentment)
        outcome_presentment["unapplied_cash_refund_outcome_code"] = "accepted"
        self.assertTrue(validate_unapplied_cash_refund_presentment(outcome_presentment))
        unsupported = UnappliedCashRefundResult(
            unapplied_cash_refund_outcome_code="posted",  # type: ignore[arg-type]
            unapplied_cash_refund_contract_version=1,
            unapplied_cash_refund_id=None,
            unapplied_cash_id=None,
            payment_receipt_id=None,
            payment_intent_id=None,
            collection_case_id=None,
            tenant_reference=None,
            currency_code=None,
            refund_amount=None,
            unapplied_amount=None,
            unapplied_cash_refund_status=None,
            unapplied_cash_status=None,
            refunded_at=None,
            source_payload_hash=None,
            next_operator_action="wait",
            rejection_reason_code=None,
        )
        with self.assertRaisesRegex(ValueError, "unsupported unapplied cash refund outcome"):
            unsupported.as_contract_dict()
        self.assertTrue(_format_refunded_at(REFUNDED_MORNING).endswith("Z"))
        with self.assertRaisesRegex(ValueError, "accepted unapplied cash refund must include refunded_at"):
            _format_refunded_at(None)

    def test_ledger_insert_fail_closed_branches(self) -> None:
        """Ledger insert refuses invalid status, leftover, and duplicate identity."""
        ledger, parked, _receipt_id = _park_leftover()
        accepted = UnappliedCashRefundService(ledger).refund_unapplied_cash(
            TENANT_ONE, parked.unapplied_cash_id
        )
        leftover = ledger.get_unapplied_cash(parked.unapplied_cash_id)
        self.assertIsNotNone(leftover)
        second_receipt_id = _second_receipt_on_same_ledger(ledger)
        second_parked = UnappliedCashService(ledger).park_unapplied_cash(
            TENANT_ONE, second_receipt_id, unapplied_amount=LEFTOVER
        )
        second = ledger.get_unapplied_cash(second_parked.unapplied_cash_id)
        invalid_status = StoredUnappliedCashRefund(
            unapplied_cash_refund_id=generate_record_id(),
            tenant_account_id=leftover.tenant_account_id,
            unapplied_cash_id=second.unapplied_cash_id,
            payment_receipt_id=second.payment_receipt_id,
            payment_intent_id=second.payment_intent_id,
            collection_case_id=second.collection_case_id,
            unapplied_cash_refund_contract_version=UNAPPLIED_CASH_REFUND_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("ab" * 32),
            currency_code="USD",
            refund_amount=LEFTOVER,
            unapplied_amount=LEFTOVER,
            unapplied_cash_refund_status="parked",
            refunded_at=REFUNDED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "unapplied_cash_refund_status must be recorded"):
            ledger.insert_unapplied_cash_refund(invalid_status)
        zero_row = StoredUnappliedCashRefund(
            unapplied_cash_refund_id=generate_record_id(),
            tenant_account_id=leftover.tenant_account_id,
            unapplied_cash_id=second.unapplied_cash_id,
            payment_receipt_id=second.payment_receipt_id,
            payment_intent_id=second.payment_intent_id,
            collection_case_id=second.collection_case_id,
            unapplied_cash_refund_contract_version=UNAPPLIED_CASH_REFUND_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("cd" * 32),
            currency_code="USD",
            refund_amount=Decimal("0"),
            unapplied_amount=LEFTOVER,
            unapplied_cash_refund_status="recorded",
            refunded_at=REFUNDED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "positive exact decimal"):
            ledger.insert_unapplied_cash_refund(zero_row)
        zero_leftover = StoredUnappliedCashRefund(
            unapplied_cash_refund_id=generate_record_id(),
            tenant_account_id=leftover.tenant_account_id,
            unapplied_cash_id=second.unapplied_cash_id,
            payment_receipt_id=second.payment_receipt_id,
            payment_intent_id=second.payment_intent_id,
            collection_case_id=second.collection_case_id,
            unapplied_cash_refund_contract_version=UNAPPLIED_CASH_REFUND_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("99" * 32),
            currency_code="USD",
            refund_amount=LEFTOVER,
            unapplied_amount=Decimal("0"),
            unapplied_cash_refund_status="recorded",
            refunded_at=REFUNDED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "unapplied cash amount must be a positive exact decimal"):
            ledger.insert_unapplied_cash_refund(zero_leftover)
        duplicate_id = StoredUnappliedCashRefund(
            unapplied_cash_refund_id=accepted.unapplied_cash_refund_id,
            tenant_account_id=leftover.tenant_account_id,
            unapplied_cash_id=second.unapplied_cash_id,
            payment_receipt_id=second.payment_receipt_id,
            payment_intent_id=second.payment_intent_id,
            collection_case_id=second.collection_case_id,
            unapplied_cash_refund_contract_version=UNAPPLIED_CASH_REFUND_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("ef" * 32),
            currency_code="USD",
            refund_amount=LEFTOVER,
            unapplied_amount=LEFTOVER,
            unapplied_cash_refund_status="recorded",
            refunded_at=REFUNDED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "unapplied_cash_refund_id already stored"):
            ledger.insert_unapplied_cash_refund(duplicate_id)
        duplicate_identity = StoredUnappliedCashRefund(
            unapplied_cash_refund_id=generate_record_id(),
            tenant_account_id=leftover.tenant_account_id,
            unapplied_cash_id=parked.unapplied_cash_id,
            payment_receipt_id=leftover.payment_receipt_id,
            payment_intent_id=leftover.payment_intent_id,
            collection_case_id=leftover.collection_case_id,
            unapplied_cash_refund_contract_version=UNAPPLIED_CASH_REFUND_CONTRACT_VERSION,
            source_payload_hash="sha256:" + ("11" * 32),
            currency_code="USD",
            refund_amount=LEFTOVER,
            unapplied_amount=LEFTOVER,
            unapplied_cash_refund_status="recorded",
            refunded_at=REFUNDED_MORNING,
        )
        with self.assertRaisesRegex(ValueError, "immutable and cannot be replaced"):
            ledger.insert_unapplied_cash_refund(duplicate_identity)


if __name__ == "__main__":
    unittest.main()
