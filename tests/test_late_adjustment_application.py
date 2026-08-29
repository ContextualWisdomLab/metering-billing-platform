"""Tests for the append-only late-adjustment application acknowledgement."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
import unittest
from unittest import mock
from uuid import uuid4

from metering_billing import (
    LateAdjustmentApplicationService,
    LateAdjustmentRatingService,
    LateAdjustmentPresentmentService,
    create_billing_period,
    create_http_app,
    create_late_adjustment,
    validate_late_adjustment_application,
    validate_late_adjustment_rating,
    validate_late_adjustment_presentment,
)
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO, seed_ledger


def _store_open_target_period(ledger, period_id) -> None:
    """Give the in-memory reference the lifecycle state used by application tests."""
    ledger.insert_billing_period(
        create_billing_period(
            TENANT_ONE,
            date(2026, 8, 1),
            date(2026, 9, 1),
            opened_by="operator:period",
            opened_at=datetime(2026, 8, 1, tzinfo=UTC),
            period_id=period_id,
        )
    )


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
        _store_open_target_period(ledger, adjustment.target_period_id)
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
                    authorization_reference="change:other",
                )
            ),
            stored,
        )
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_application(
                replace(
                    stored,
                    late_adjustment_application_id=uuid4(),
                    adjustment_amount=Decimal("1.0"),
                )
            )
        self.assertEqual(
            ledger.insert_late_adjustment_application(
                replace(stored, late_adjustment_application_id=uuid4())
            ),
            stored,
        )
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_application(stored)
        missing_source = replace(adjustment, late_adjustment_id=uuid4(), source_reference="provider:application-missing-source", source_payload_hash="sha256:" + "b" * 64)
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_application(
                replace(stored, late_adjustment_application_id=uuid4(), late_adjustment_id=missing_source.late_adjustment_id)
            )
        cross_tenant_source = replace(adjustment, late_adjustment_id=uuid4(), source_reference="provider:application-cross-tenant", source_payload_hash="sha256:" + "c" * 64)
        ledger.insert_late_adjustment(TENANT_TWO, cross_tenant_source)
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_application(
                replace(stored, late_adjustment_application_id=uuid4(), late_adjustment_id=cross_tenant_source.late_adjustment_id)
            )
        target_mismatch_source = replace(adjustment, late_adjustment_id=uuid4(), source_reference="provider:application-target-mismatch", source_payload_hash="sha256:" + "d" * 64)
        ledger.insert_late_adjustment(TENANT_ONE, target_mismatch_source)
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_application(
                replace(stored, late_adjustment_application_id=uuid4(), late_adjustment_id=target_mismatch_source.late_adjustment_id, target_period_id=uuid4())
            )
        amount_mismatch_source = replace(adjustment, late_adjustment_id=uuid4(), source_reference="provider:application-amount-mismatch", source_payload_hash="sha256:" + "e" * 64)
        ledger.insert_late_adjustment(TENANT_ONE, amount_mismatch_source)
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_application(
                replace(stored, late_adjustment_application_id=uuid4(), late_adjustment_id=amount_mismatch_source.late_adjustment_id, adjustment_amount=Decimal("9.0"))
            )
        currency_mismatch_source = replace(adjustment, late_adjustment_id=uuid4(), currency_code="EUR", source_reference="provider:application-currency-mismatch", source_payload_hash="sha256:" + "f" * 64)
        ledger.insert_late_adjustment(TENANT_ONE, currency_mismatch_source)
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_application(
                replace(stored, late_adjustment_application_id=uuid4(), late_adjustment_id=currency_mismatch_source.late_adjustment_id)
            )
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
        _store_open_target_period(ledger, adjustment.target_period_id)
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

    def test_application_rejects_target_closed_after_adjustment_recording(self) -> None:
        """A target must still be open when the application fact is written."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "1.25",
            "USD",
            "provider:application-closed-target",
            "sha256:" + "e" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        _store_open_target_period(ledger, adjustment.target_period_id)
        self.assertIsNone(
            ledger.get_billing_period("urn:cwl:missing", adjustment.target_period_id)
        )
        self.assertIsNone(ledger.get_billing_period(TENANT_ONE, uuid4()))
        target = ledger.get_billing_period(TENANT_ONE, adjustment.target_period_id)
        assert target is not None
        with self.assertRaises(ValueError):
            ledger.insert_billing_period(replace(target, period_end=date(2026, 9, 2)))
        ledger.insert_billing_period(
            target.advance(
                "soft_closed",
                actor_reference="operator:period",
                authorization_reference="change:closed-target",
                reason="close target before application",
                transitioned_at=datetime(2026, 8, 18, tzinfo=UTC),
            )
        )
        with self.assertRaises(ValueError):
            ledger.insert_billing_period(
                replace(target, status="open", transitions=())
            )
        result = LateAdjustmentApplicationService(ledger).apply_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            applied_by="operator:alice",
            authorization_reference="change:128",
        )
        self.assertEqual(result.late_adjustment_application_outcome_code, "rejected")
        self.assertEqual(
            result.rejection_reason_code, "late_adjustment_target_period_not_open"
        )
        self.assertEqual(ledger.late_adjustment_applications, {})

    def test_rating_rejects_first_fact_after_target_closes(self) -> None:
        """A closed target accepts neither a first application rating nor a new fact."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "1.25",
            "USD",
            "provider:rating-closed-target",
            "sha256:" + "f" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        _store_open_target_period(ledger, adjustment.target_period_id)
        LateAdjustmentApplicationService(ledger).apply_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            applied_by="operator:alice",
            authorization_reference="change:closed-rating",
        )
        target = ledger.get_billing_period(TENANT_ONE, adjustment.target_period_id)
        assert target is not None
        ledger.insert_billing_period(
            target.advance(
                "soft_closed",
                actor_reference="operator:period",
                authorization_reference="change:closed-rating",
                reason="close target before rating",
                transitioned_at=datetime(2026, 8, 18, tzinfo=UTC),
            )
        )
        result = LateAdjustmentRatingService(ledger).rate_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            rated_by="operator:alice",
            authorization_reference="change:closed-rating-fact",
        )
        self.assertEqual(result.late_adjustment_rating_outcome_code, "rejected")
        self.assertEqual(
            result.rejection_reason_code, "late_adjustment_target_period_not_open"
        )
        self.assertEqual(validate_late_adjustment_rating(result.as_contract_dict()), ())
        self.assertEqual(ledger.late_adjustment_ratings, {})

    def test_rating_consumes_application_without_rewriting_original_rating_run(self) -> None:
        """Rating records the signed delta separately and is replay-safe."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "reversal",
            "-7.250",
            "USD",
            "provider:rating-001",
            "sha256:" + "c" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        _store_open_target_period(ledger, adjustment.target_period_id)
        application_result = LateAdjustmentApplicationService(
            ledger,
            clock=lambda: datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC),
        ).apply_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            applied_by="operator:alice",
            authorization_reference="change:123",
        )
        application = ledger.get_late_adjustment_application(
            application_result.late_adjustment_application_id
        )
        assert application is not None
        service = LateAdjustmentRatingService(
            ledger,
            clock=lambda: datetime(2026, 8, 29, 1, 3, 3, tzinfo=UTC),
        )
        for invalid_request in (
            ("urn:cwl:missing", adjustment.late_adjustment_id, "operator:alice", "change:124"),
            (TENANT_ONE, "not-a-uuid", "operator:alice", "change:124"),
            (TENANT_ONE, adjustment.late_adjustment_id, 1, "change:124"),
            (TENANT_ONE, adjustment.late_adjustment_id, " ", "change:124"),
            (TENANT_ONE, adjustment.late_adjustment_id, "operator:alice", 1),
            (TENANT_ONE, adjustment.late_adjustment_id, "operator:alice", " "),
        ):
            with self.subTest(invalid_request=invalid_request):
                result = service.rate_late_adjustment(
                    invalid_request[0],
                    invalid_request[1],  # type: ignore[arg-type]
                    rated_by=invalid_request[2],
                    authorization_reference=invalid_request[3],
                )
                self.assertEqual(result.late_adjustment_rating_outcome_code, "rejected")
        self.assertEqual(
            service.rate_late_adjustment(
                TENANT_TWO,
                adjustment.late_adjustment_id,
                rated_by="operator:alice",
                authorization_reference="change:124",
            ).rejection_reason_code,
            "late_adjustment_not_found",
        )
        accepted = service.rate_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            rated_by="operator:alice",
            authorization_reference="change:124",
        )
        self.assertEqual(accepted.late_adjustment_rating_outcome_code, "accepted")
        self.assertEqual(accepted.adjustment_amount, Decimal("-7.250"))
        self.assertEqual(validate_late_adjustment_rating(accepted.as_contract_dict()), ())
        replay = service.rate_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            rated_by="operator:other",
            authorization_reference="change:125",
        )
        self.assertEqual(replay.late_adjustment_rating_outcome_code, "duplicate_replay")
        self.assertEqual(replay.late_adjustment_rating_id, accepted.late_adjustment_rating_id)
        self.assertEqual(len(ledger.late_adjustment_ratings), 1)
        self.assertEqual(
            LateAdjustmentPresentmentService(ledger)
            .present_late_adjustment(TENANT_ONE, adjustment.late_adjustment_id)
            .next_operator_action,
            "record_invoice_adjustment",
        )
        stored = ledger.get_late_adjustment_rating(accepted.late_adjustment_rating_id)
        assert stored is not None
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_rating(stored)
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_rating(
                replace(
                    stored,
                    late_adjustment_rating_id=uuid4(),
                    late_adjustment_id=uuid4(),
                    late_adjustment_application_id=uuid4(),
                )
            )
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_rating(
                replace(stored, late_adjustment_rating_id=uuid4(), late_adjustment_application_id=uuid4())
            )
        for invalid in (
            replace(stored, late_adjustment_rating_status="pending"),
            replace(stored, currency_code="usd"),
            replace(stored, adjustment_amount=Decimal("0")),
            replace(stored, adjustment_amount=Decimal("1" * 41)),
            replace(stored, rated_by=" "),
            replace(stored, authorization_reference=" "),
        ):
            with self.assertRaises(ValueError):
                ledger.insert_late_adjustment_rating(invalid)
        self.assertEqual(
            ledger.insert_late_adjustment_rating(
                replace(
                    stored,
                    late_adjustment_rating_id=uuid4(),
                    rated_by="operator:other",
                    authorization_reference="change:other",
                )
            ),
            stored,
        )
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_rating(
                replace(stored, late_adjustment_rating_id=uuid4(), target_period_id=uuid4())
            )
        missing_application_adjustment = replace(
            adjustment,
            late_adjustment_id=uuid4(),
            source_reference="provider:rating-missing-application",
            source_payload_hash="sha256:" + "d" * 64,
        )
        ledger.insert_late_adjustment(TENANT_ONE, missing_application_adjustment)
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_rating(
                replace(
                    stored,
                    late_adjustment_rating_id=uuid4(),
                    late_adjustment_id=missing_application_adjustment.late_adjustment_id,
                    late_adjustment_application_id=uuid4(),
                )
            )
        application_mismatch_adjustment = replace(
            adjustment,
            late_adjustment_id=uuid4(),
            source_reference="provider:rating-application-mismatch",
            source_payload_hash="sha256:" + "e" * 64,
        )
        ledger.insert_late_adjustment(TENANT_ONE, application_mismatch_adjustment)
        mismatch_application_result = LateAdjustmentApplicationService(
            ledger
        ).apply_late_adjustment(
            TENANT_ONE,
            application_mismatch_adjustment.late_adjustment_id,
            applied_by="operator:alice",
            authorization_reference="change:application-mismatch",
        )
        mismatch_application = ledger.get_late_adjustment_application(
            mismatch_application_result.late_adjustment_application_id
        )
        assert mismatch_application is not None
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_rating(
                replace(
                    stored,
                    late_adjustment_rating_id=uuid4(),
                    late_adjustment_application_id=(
                        mismatch_application.late_adjustment_application_id
                    ),
                    late_adjustment_id=application_mismatch_adjustment.late_adjustment_id,
                    adjustment_amount=Decimal("1.0"),
                )
            )
        source_mismatch_adjustment = replace(
            adjustment,
            late_adjustment_id=uuid4(),
            source_reference="provider:rating-source-mismatch",
            source_payload_hash="sha256:" + "f" * 64,
        )
        ledger.insert_late_adjustment(TENANT_ONE, source_mismatch_adjustment)
        source_mismatch_application = replace(
            application,
            late_adjustment_application_id=uuid4(),
            late_adjustment_id=source_mismatch_adjustment.late_adjustment_id,
        )
        ledger.insert_late_adjustment_application(source_mismatch_application)
        ledger.late_adjustments[source_mismatch_adjustment.late_adjustment_id] = replace(
            source_mismatch_adjustment, adjustment_amount=Decimal("6.0")
        )
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_rating(
                replace(
                    stored,
                    late_adjustment_rating_id=uuid4(),
                    late_adjustment_application_id=(
                        source_mismatch_application.late_adjustment_application_id
                    ),
                    late_adjustment_id=source_mismatch_adjustment.late_adjustment_id,
                )
            )
        self.assertEqual(
            ledger.insert_late_adjustment_rating(
                replace(stored, late_adjustment_rating_id=uuid4())
            ),
            stored,
        )
        target = ledger.get_billing_period(TENANT_ONE, adjustment.target_period_id)
        assert target is not None
        ledger.insert_billing_period(
            target.advance(
                "soft_closed",
                actor_reference="operator:period",
                authorization_reference="change:close-after-rating",
                reason="close target after rating",
                transitioned_at=datetime(2026, 8, 30, tzinfo=UTC),
            )
        )
        self.assertEqual(
            ledger.insert_late_adjustment_rating(
                replace(stored, late_adjustment_rating_id=uuid4())
            ),
            stored,
        )
        self.assertTrue(validate_late_adjustment_rating(None))
        self.assertTrue(
            validate_late_adjustment_rating(
                accepted.as_contract_dict() | {"adjustment_amount": "0"}
            )
        )
        self.assertTrue(
            validate_late_adjustment_rating(
                accepted.as_contract_dict() | {"adjustment_amount": "not-a-decimal"}
            )
        )
        self.assertTrue(
            validate_late_adjustment_rating(
                accepted.as_contract_dict() | {"adjustment_amount": None}
            )
        )
        with self.assertRaises(ValueError):
            replace(accepted, rated_by=None).as_contract_dict()
        with self.assertRaises(ValueError):
            replace(accepted, late_adjustment_rating_id=None).as_contract_dict()

    def test_http_rating_requires_application_and_is_idempotent(self) -> None:
        """The nested rating command exposes the application gate over HTTP."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "3.25",
            "USD",
            "provider:rating-002",
            "sha256:" + "d" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        _store_open_target_period(ledger, adjustment.target_period_id)
        app = create_http_app(ledger)
        path = f"/v1/late-adjustments/{adjustment.late_adjustment_id}/ratings"
        payload = {
            "tenant_reference": TENANT_ONE,
            "rated_by": "operator:alice",
            "authorization_reference": "change:126",
        }
        status, body = invoke_http(app, "POST", path, payload)
        self.assertEqual(status, 422)
        self.assertEqual(
            body["rejection_reason_code"], "late_adjustment_application_not_found"
        )
        self.assertEqual(body["next_operator_action"], "apply_late_adjustment")
        status, body = invoke_http(app, "POST", path, {})
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "tenant_not_found")
        LateAdjustmentApplicationService(ledger).apply_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            applied_by="operator:alice",
            authorization_reference="change:127",
        )
        status, body = invoke_http(app, "POST", path, payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["next_operator_action"], "record_invoice_adjustment")
        self.assertEqual(validate_late_adjustment_rating(body), ())
        status, body = invoke_http(
            app,
            "POST",
            path,
            payload | {"card_pan": "4111111111111111"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")

        status, replay = invoke_http(app, "POST", path, payload)
        self.assertEqual(status, 200)
        self.assertEqual(replay["late_adjustment_rating_outcome_code"], "duplicate_replay")
        method_status, method_body = invoke_http(app, "GET", path)
        self.assertEqual(method_status, 422)
        self.assertEqual(method_body["rejection_reason_code"], "request_invalid")
        with mock.patch(
            "metering_billing.http_app.LateAdjustmentRatingService.rate_late_adjustment",
            side_effect=ValueError("broken"),
        ):
            status, body = invoke_http(app, "POST", path, payload)
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "request_invalid")

    def test_http_rating_rejects_first_fact_after_target_closes(self) -> None:
        """The HTTP command exposes the closed-target rating rejection."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "3.25",
            "USD",
            "provider:http-rating-closed-target",
            "sha256:" + "a" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        _store_open_target_period(ledger, adjustment.target_period_id)
        application = LateAdjustmentApplicationService(ledger).apply_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            applied_by="operator:alice",
            authorization_reference="change:http-closed-rating",
        )
        self.assertEqual(application.late_adjustment_application_outcome_code, "accepted")
        target = ledger.get_billing_period(TENANT_ONE, adjustment.target_period_id)
        assert target is not None
        ledger.insert_billing_period(
            target.advance(
                "soft_closed",
                actor_reference="operator:period",
                authorization_reference="change:http-closed-rating",
                reason="close target before rating",
                transitioned_at=datetime(2026, 8, 18, tzinfo=UTC),
            )
        )
        path = f"/v1/late-adjustments/{adjustment.late_adjustment_id}/ratings"
        status, body = invoke_http(
            create_http_app(ledger),
            "POST",
            path,
            {
                "tenant_reference": TENANT_ONE,
                "rated_by": "operator:alice",
                "authorization_reference": "change:http-closed-rating-fact",
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            body["rejection_reason_code"], "late_adjustment_target_period_not_open"
        )
        self.assertEqual(validate_late_adjustment_rating(body), ())


if __name__ == "__main__":
    unittest.main()
