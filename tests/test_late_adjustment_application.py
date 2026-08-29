"""Tests for the append-only late-adjustment application acknowledgement."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import unittest
from unittest import mock
from uuid import uuid4

from metering_billing import (
    LateAdjustmentApplicationService,
    LateAdjustmentPresentmentService,
    create_http_app,
    create_late_adjustment,
    validate_late_adjustment_application,
    validate_late_adjustment_presentment,
)
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO, seed_ledger


class LateAdjustmentApplicationTests(unittest.TestCase):
    """Verify tenant-safe application, replay, and presentment transition."""

    def test_application_is_exact_idempotent_and_moves_next_action_to_rating(
        self,
    ) -> None:
        """Applying a signed fact records no second row and preserves its amount."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "-12.50",
            "USD",
            "provider:application-001",
            "sha256:" + "a" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        service = LateAdjustmentApplicationService(
            ledger, clock=lambda: datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC)
        )

        self.assertEqual(
            service.apply_late_adjustment(
                "urn:cwl:missing",
                adjustment.late_adjustment_id,
                applied_by="operator:alice",
                authorization_reference="change:123",
            ).rejection_reason_code,
            "tenant_not_found",
        )
        self.assertEqual(
            service.apply_late_adjustment(
                TENANT_ONE,
                "not-a-uuid",  # type: ignore[arg-type]
                applied_by="operator:alice",
                authorization_reference="change:123",
            ).rejection_reason_code,
            "late_adjustment_not_found",
        )
        self.assertEqual(
            service.apply_late_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                applied_by=" ",
                authorization_reference="change:123",
            ).rejection_reason_code,
            "actor_reference_invalid",
        )

        accepted = service.apply_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            applied_by="operator:alice",
            authorization_reference="change:123",
        )
        self.assertEqual(accepted.late_adjustment_application_outcome_code, "accepted")
        self.assertEqual(accepted.adjustment_amount, adjustment.adjustment_amount)
        self.assertEqual(
            validate_late_adjustment_application(accepted.as_contract_dict()), ()
        )

        replay = service.apply_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            applied_by="operator:alice",
            authorization_reference="change:123",
        )
        self.assertEqual(
            replay.late_adjustment_application_outcome_code, "duplicate_replay"
        )
        self.assertEqual(
            replay.late_adjustment_application_id,
            accepted.late_adjustment_application_id,
        )
        self.assertEqual(
            len(ledger.late_adjustment_applications),
            1,
        )
        projected = LateAdjustmentPresentmentService(ledger).present_late_adjustment(
            TENANT_ONE, adjustment.late_adjustment_id
        )
        self.assertEqual(projected.next_operator_action, "rate_late_adjustment")
        self.assertEqual(
            validate_late_adjustment_presentment(projected.as_contract_dict()), ()
        )

        stored = ledger.get_late_adjustment_application(
            accepted.late_adjustment_application_id
        )
        assert stored is not None
        for invalid in (
            replace(stored, late_adjustment_application_status="pending"),
            replace(stored, currency_code="usd"),
            replace(stored, adjustment_amount=Decimal("0")),
            replace(stored, adjustment_amount=Decimal("1" * 41)),
            replace(stored, applied_by=" "),
            replace(stored, authorization_reference=" "),
        ):
            with self.assertRaises(ValueError):
                ledger.insert_late_adjustment_application(invalid)
        self.assertEqual(
            ledger.insert_late_adjustment_application(
                replace(
                    stored,
                    late_adjustment_application_id=uuid4(),
                    applied_by="operator:other",
                    authorization_reference="change:456",
                    applied_at=datetime(2026, 8, 29, 1, 2, 4, tzinfo=UTC),
                )
            ),
            stored,
        )
        self.assertEqual(
            ledger.insert_late_adjustment_application(
                replace(stored, late_adjustment_application_id=uuid4())
            ),
            stored,
        )
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_application(
                replace(
                    stored,
                    late_adjustment_application_id=uuid4(),
                    adjustment_amount=Decimal("-12.51"),
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_application(stored)
        for field in (
            "late_adjustment_application_id",
            "late_adjustment_id",
            "target_period_id",
            "adjustment_amount",
            "currency_code",
            "applied_by",
            "authorization_reference",
            "applied_at",
            "late_adjustment_application_status",
        ):
            broken = replace(accepted, **{field: None})
            with self.assertRaises(ValueError):
                broken.as_contract_dict()
        self.assertTrue(validate_late_adjustment_application(None))
        self.assertTrue(
            validate_late_adjustment_application(
                accepted.as_contract_dict() | {"adjustment_amount": "0"}
            )
        )
        self.assertTrue(
            validate_late_adjustment_application(
                accepted.as_contract_dict() | {"adjustment_amount": "not-a-decimal"}
            )
        )

    def test_http_application_and_rejection_contracts_fail_closed(self) -> None:
        """The nested command preserves tenant and audit-reference boundaries."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "late_usage",
            "2.5",
            "USD",
            "provider:application-002",
            "sha256:" + "b" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        app = create_http_app(ledger)
        path = f"/v1/late-adjustments/{adjustment.late_adjustment_id}/applications"
        status, body = invoke_http(
            app,
            "POST",
            path,
            {"tenant_reference": TENANT_ONE, "applied_by": "operator:alice"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            body["rejection_reason_code"], "authorization_reference_invalid"
        )
        self.assertEqual(validate_late_adjustment_application(body), ())

        status, body = invoke_http(
            app,
            "POST",
            path,
            {
                "tenant_reference": TENANT_ONE,
                "applied_by": "operator:alice",
                "authorization_reference": "change:456",
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")

        status, body = invoke_http(app, "POST", path, {})
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "tenant_not_found")

        status, body = invoke_http(
            app,
            "POST",
            path,
            {
                "tenant_reference": TENANT_TWO,
                "applied_by": "operator:alice",
                "authorization_reference": "change:456",
            },
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["rejection_reason_code"], "late_adjustment_not_found")

        status, body = invoke_http(
            app,
            "POST",
            path,
            {
                "tenant_reference": TENANT_ONE,
                "applied_by": "operator:alice",
                "authorization_reference": "change:456",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["next_operator_action"], "rate_late_adjustment")
        self.assertEqual(validate_late_adjustment_application(body), ())

        with mock.patch(
            "metering_billing.http_app.LateAdjustmentApplicationService.apply_late_adjustment",
            side_effect=ValueError("broken"),
        ):
            status, body = invoke_http(
                create_http_app(ledger),
                "POST",
                path,
                {
                    "tenant_reference": TENANT_ONE,
                    "applied_by": "operator:alice",
                    "authorization_reference": "change:789",
                },
            )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")

        method_status, method_body = invoke_http(app, "GET", path)
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")


if __name__ == "__main__":
    unittest.main()
