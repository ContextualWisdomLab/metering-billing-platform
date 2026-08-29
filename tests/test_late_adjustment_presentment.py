"""Late-adjustment HTTP presentment tests for the stored commercial fact."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import unittest
from unittest import mock
from uuid import uuid4

from metering_billing import (
    LateAdjustmentPresentmentService,
    create_http_app,
    create_late_adjustment,
    validate_late_adjustment_presentment,
)
from metering_billing.errors import LateAdjustmentPresentmentQueryError
from metering_billing.late_adjustment_presentment import next_operator_action
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO, seed_ledger


RECORDED_AT = datetime(2026, 8, 17, 21, 0, tzinfo=UTC)


def make_adjustment(
    *, source_reference: str, recorded_at: datetime, amount: str = "12.50"
):
    """Build one valid late adjustment for the in-memory reference ledger."""
    return create_late_adjustment(
        uuid4(),
        uuid4(),
        "correction",
        amount,
        "USD",
        source_reference,
        "sha256:" + source_reference.encode().hex().ljust(64, "0")[:64],
        recorded_at,
        late_adjustment_id=uuid4(),
    )


class LateAdjustmentPresentmentTests(unittest.TestCase):
    """Verify exact projections, pagination, and tenant isolation."""

    def test_projection_and_http_list_are_exact_and_actionable(self) -> None:
        """Stored signed evidence is projected without applying or posting it."""
        ledger = seed_ledger()
        first = make_adjustment(
            source_reference="provider:late_001", recorded_at=RECORDED_AT
        )
        second = make_adjustment(
            source_reference="provider:late_002",
            recorded_at=datetime(2026, 8, 17, 22, 0, tzinfo=UTC),
            amount="-2.25",
        )
        self.assertEqual(ledger.insert_late_adjustment(TENANT_ONE, first), first)
        self.assertEqual(ledger.insert_late_adjustment(TENANT_ONE, second), second)
        service = LateAdjustmentPresentmentService(ledger)
        projected = service.present_late_adjustment(
            TENANT_ONE, first.late_adjustment_id
        )
        self.assertEqual(projected.adjustment_amount, first.adjustment_amount)
        self.assertEqual(projected.next_operator_action, "apply_late_adjustment")
        payload = projected.as_contract_dict()
        self.assertEqual(validate_late_adjustment_presentment(payload), ())
        self.assertTrue(validate_late_adjustment_presentment(None))
        self.assertTrue(
            validate_late_adjustment_presentment(payload | {"adjustment_amount": "0"})
        )
        self.assertTrue(
            validate_late_adjustment_presentment(
                payload | {"next_operator_action": "wait"}
            )
        )
        self.assertTrue(
            validate_late_adjustment_presentment(payload | {"adjustment_amount": 1})
        )
        self.assertTrue(
            validate_late_adjustment_presentment(
                payload | {"adjustment_amount": "not-a-decimal"}
            )
        )
        self.assertNotIn("status", payload)
        self.assertNotIn("journal_proposal_id", payload)

        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/late-adjustments/{first.late_adjustment_id}",
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/late-adjustments",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(set(list_body), {"late_adjustments", "next_cursor"})
        self.assertEqual(len(list_body["late_adjustments"]), 1)
        next_status, next_body = invoke_http(
            app,
            "GET",
            "/v1/late-adjustments",
            query={
                "tenant_reference": TENANT_ONE,
                "cursor": list_body["next_cursor"],
            },
        )
        self.assertEqual(next_status, 200)
        self.assertEqual(len(next_body["late_adjustments"]), 1)
        self.assertIsNone(next_body["next_cursor"])
        self.assertEqual(next_operator_action(), "apply_late_adjustment")

    def test_replay_conflict_and_tenant_isolation_fail_closed(self) -> None:
        """The reference adapter preserves one source identity per tenant."""
        ledger = seed_ledger()
        adjustment = make_adjustment(
            source_reference="provider:late_003", recorded_at=RECORDED_AT
        )
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        replay = replace(adjustment, late_adjustment_id=uuid4())
        self.assertEqual(ledger.insert_late_adjustment(TENANT_ONE, replay), adjustment)
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment(
                TENANT_ONE,
                make_adjustment(
                    source_reference=adjustment.source_reference,
                    recorded_at=adjustment.recorded_at,
                    amount="13.50",
                ),
            )
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment(TENANT_TWO, adjustment)
        self.assertIsNone(
            ledger.get_late_adjustment(TENANT_TWO, adjustment.late_adjustment_id)
        )
        self.assertIsNone(
            ledger.get_late_adjustment("urn:cwl:missing", adjustment.late_adjustment_id)
        )
        self.assertEqual(ledger.list_late_adjustments(TENANT_TWO), ())
        self.assertEqual(ledger.list_late_adjustments("urn:cwl:missing"), ())
        with self.assertRaises(LateAdjustmentPresentmentQueryError) as error:
            LateAdjustmentPresentmentService(ledger).present_late_adjustment(
                TENANT_TWO, adjustment.late_adjustment_id
            )
        self.assertEqual(
            error.exception.rejection_reason_code, "late_adjustment_not_found"
        )

    def test_invalid_query_inputs_and_http_errors_fail_closed(self) -> None:
        """Invalid cursors, bounds, tenants, and methods never expose evidence."""
        ledger = seed_ledger()
        adjustment = make_adjustment(
            source_reference="provider:late_004", recorded_at=RECORDED_AT
        )
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        service = LateAdjustmentPresentmentService(ledger)
        with self.assertRaises(LateAdjustmentPresentmentQueryError):
            service.present_late_adjustment(TENANT_ONE, "not-a-uuid")  # type: ignore[arg-type]
        with self.assertRaises(LateAdjustmentPresentmentQueryError):
            service.list_late_adjustments(TENANT_ONE, cursor="not-a-cursor")
        self.assertEqual(
            len(
                service.list_late_adjustments(TENANT_ONE, page_limit=1).late_adjustments
            ),
            1,
        )
        with self.assertRaises(LateAdjustmentPresentmentQueryError):
            service.list_late_adjustments(TENANT_ONE, page_limit="0")
        with self.assertRaises(LateAdjustmentPresentmentQueryError):
            service.list_late_adjustments(TENANT_ONE, page_limit="not-a-number")
        with self.assertRaises(LateAdjustmentPresentmentQueryError):
            service.list_late_adjustments(TENANT_ONE, page_limit=True)
        with self.assertRaises(LateAdjustmentPresentmentQueryError):
            service.list_late_adjustments("urn:cwl:missing")

        app = create_http_app(ledger)
        missing_status, missing_body = invoke_http(
            app, "GET", f"/v1/late-adjustments/{adjustment.late_adjustment_id}"
        )
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")
        invalid_status, invalid_body = invoke_http(
            app,
            "GET",
            "/v1/late-adjustments",
            query={"tenant_reference": TENANT_ONE, "page_limit": "101"},
        )
        self.assertEqual(invalid_status, 422)
        self.assertEqual(invalid_body["rejection_reason_code"], "request_invalid")
        collection_method_status, collection_method_body = invoke_http(
            app, "PUT", "/v1/late-adjustments"
        )
        self.assertEqual(collection_method_status, 422)
        self.assertEqual(
            collection_method_body["rejection_reason_code"], "request_invalid"
        )
        method_status, method_body = invoke_http(
            app, "PUT", f"/v1/late-adjustments/{adjustment.late_adjustment_id}"
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.LateAdjustmentPresentmentService.present_late_adjustment",
            side_effect=ValueError("closed"),
        ):
            value_status, value_body = invoke_http(
                create_http_app(ledger),
                "GET",
                f"/v1/late-adjustments/{adjustment.late_adjustment_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(value_status, 422)
        self.assertEqual(value_body["rejection_reason_code"], "request_invalid")
