"""Tests for the append-only late-adjustment application acknowledgement."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from decimal import Decimal
import unittest
from unittest import mock
from uuid import uuid4

from metering_billing import (
    BillingPeriodStatus,
    LateAdjustmentApplicationService,
    LateAdjustmentPresentmentService,
    create_billing_period,
    create_http_app,
    create_late_adjustment,
    validate_late_adjustment_application,
    validate_late_adjustment_presentment,
)
from metering_billing.usage_ledger import MemoryUsageLedger, StoredLateAdjustmentApplication
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO, seed_ledger


def seed_adjustment_periods(ledger, adjustment, *, target_status=BillingPeriodStatus.OPEN):
    """Seed the source and target lifecycle rows required by the memory adapter."""
    source = create_billing_period(
        TENANT_ONE,
        date(2026, 7, 1),
        date(2026, 8, 1),
        opened_by="operator:test_source",
        opened_at=datetime(2026, 8, 17, 20, 0, tzinfo=UTC),
        period_id=adjustment.source_period_id,
    ).advance(
        BillingPeriodStatus.SOFT_CLOSED,
        actor_reference="operator:test_source",
        authorization_reference="change:test_source",
        reason="close source",
        transitioned_at=datetime(2026, 8, 17, 20, 30, tzinfo=UTC),
    )
    target = create_billing_period(
        TENANT_ONE,
        date(2026, 8, 1),
        date(2026, 9, 1),
        opened_by="operator:test_target",
        opened_at=datetime(2026, 8, 17, 20, 0, tzinfo=UTC),
        period_id=adjustment.target_period_id,
    )
    if target_status != BillingPeriodStatus.OPEN:
        target = target.advance(
            target_status,
            actor_reference="operator:test_target",
            authorization_reference="change:test_target",
            reason="close target",
            transitioned_at=datetime(2026, 8, 17, 20, 30, tzinfo=UTC),
        )
    ledger.insert_billing_period(source)
    ledger.insert_billing_period(target)


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
        seed_adjustment_periods(ledger, adjustment)
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

    def test_application_clock_requires_aware_non_future_audit_time(self) -> None:
        """Injected clocks cannot create ambiguous or future audit facts."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "1.00",
            "USD",
            "provider:application-clock",
            "sha256:" + "d" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        seed_adjustment_periods(ledger, adjustment)
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        for clock, message in (
            (lambda: datetime(2026, 8, 17, 22, 0), "timezone-aware"),
            (lambda: datetime.now(UTC) + timedelta(days=1), "not be in the future"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    LateAdjustmentApplicationService(ledger, clock=clock).apply_late_adjustment(
                        TENANT_ONE,
                        adjustment.late_adjustment_id,
                        applied_by="operator:alice",
                        authorization_reference="change:clock",
                    )

    def test_memory_concurrent_applications_are_at_most_once(self) -> None:
        """Concurrent memory applications return one acceptance and replays."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "2.00",
            "USD",
            "provider:application-memory-race",
            "sha256:" + "f" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        seed_adjustment_periods(ledger, adjustment)
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        barrier = Barrier(2)

        class BarrierLedger:
            """Force both service prechecks to observe no application."""

            def __init__(self, delegate):
                self.delegate = delegate

            def find_late_adjustment_application(self, tenant_account_id, late_adjustment_id):
                result = self.delegate.find_late_adjustment_application(
                    tenant_account_id, late_adjustment_id
                )
                barrier.wait()
                return result

            def __getattr__(self, name):
                return getattr(self.delegate, name)

        shared = BarrierLedger(ledger)

        def apply_once(worker: int) -> str:
            return LateAdjustmentApplicationService(
                shared,
                clock=lambda: datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC),
            ).apply_late_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                applied_by=f"operator:memory_{worker}",
                authorization_reference=f"change:memory_{worker}",
            ).late_adjustment_application_outcome_code.value

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(apply_once, (0, 1)))
        self.assertEqual(sorted(outcomes), ["accepted", "duplicate_replay"])
        self.assertEqual(len(ledger.late_adjustment_applications), 1)

    def test_memory_application_racing_target_close_rejects_new_fact(self) -> None:
        """A target close cannot slip between application validation and insert."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "3.00",
            "USD",
            "provider:application-memory-close-race",
            "sha256:" + "1" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        seed_adjustment_periods(ledger, adjustment)
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        target = ledger.get_billing_period(TENANT_ONE, adjustment.target_period_id)
        assert target is not None
        closed_target = target.advance(
            BillingPeriodStatus.SOFT_CLOSED,
            actor_reference="operator:closer",
            authorization_reference="change:close-race",
            reason="close target",
            transitioned_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        target_checked = Event()
        target_closed = Event()

        class ClosingLedger:
            """Close the target after the service's stale open read."""

            def __init__(self, delegate):
                self.delegate = delegate

            def get_billing_period(self, tenant_reference, period_id):
                result = self.delegate.get_billing_period(tenant_reference, period_id)
                if period_id == adjustment.target_period_id:
                    target_checked.set()
                    self.assert_event(target_closed)
                return result

            @staticmethod
            def assert_event(event: Event) -> None:
                if not event.wait(timeout=5):
                    raise AssertionError("target close did not complete")

            def __getattr__(self, name):
                return getattr(self.delegate, name)

        shared = ClosingLedger(ledger)

        def close_target() -> None:
            if not target_checked.wait(timeout=5):
                raise AssertionError("application did not check target")
            ledger.insert_billing_period(closed_target)
            target_closed.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            close_future = pool.submit(close_target)
            result_future = pool.submit(
                lambda: LateAdjustmentApplicationService(
                    shared,
                    clock=lambda: datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC),
                ).apply_late_adjustment(
                    TENANT_ONE,
                    adjustment.late_adjustment_id,
                    applied_by="operator:memory_close-race",
                    authorization_reference="change:memory_close-race",
                )
            )
            close_future.result()
            result = result_future.result()
        self.assertEqual(result.rejection_reason_code, "target_period_not_open")
        self.assertEqual(len(ledger.late_adjustment_applications), 0)

    def test_memory_recording_racing_target_close_rejects_stale_fact(self) -> None:
        """A target close that acquires the lock first rejects stale recording."""
        close_started = Event()
        release_close = Event()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "3.50",
            "USD",
            "provider:recording-memory-close-race",
            "sha256:" + "2" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )

        class BlockingCloseLedger(MemoryUsageLedger):
            """Hold the lifecycle lock after the close has acquired it."""

            def _insert_billing_period(self, period):
                if period.period_id == adjustment.target_period_id and period.transitions:
                    close_started.set()
                    if not release_close.wait(timeout=5):
                        raise AssertionError("recording did not reach the lifecycle lock")
                return super()._insert_billing_period(period)

        ledger = seed_ledger(BlockingCloseLedger)
        seed_adjustment_periods(ledger, adjustment)
        target = ledger.get_billing_period(TENANT_ONE, adjustment.target_period_id)
        assert target is not None
        closed_target = target.advance(
            BillingPeriodStatus.SOFT_CLOSED,
            actor_reference="operator:closer",
            authorization_reference="change:recording-close-race",
            reason="close target",
            transitioned_at=datetime(2026, 8, 18, tzinfo=UTC),
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            close_future = pool.submit(ledger.insert_billing_period, closed_target)
            self.assertTrue(close_started.wait(timeout=5))
            recording_future = pool.submit(
                ledger.insert_late_adjustment, TENANT_ONE, adjustment
            )
            release_close.set()
            close_future.result()
            with self.assertRaises(ValueError):
                recording_future.result()
        self.assertNotIn(adjustment.late_adjustment_id, ledger.late_adjustments)

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
        seed_adjustment_periods(ledger, adjustment)
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

    def test_memory_application_rechecks_target_and_preserves_replay(self) -> None:
        """Memory application rejects missing targets and replays after closure."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "4.00",
            "USD",
            "provider:application-target-state",
            "sha256:" + "c" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        seed_adjustment_periods(ledger, adjustment)
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        target = ledger.get_billing_period(TENANT_ONE, adjustment.target_period_id)
        self.assertIsNotNone(target)
        assert target is not None
        del ledger.billing_periods[adjustment.target_period_id]
        candidate = StoredLateAdjustmentApplication(
            late_adjustment_application_id=uuid4(),
            tenant_account_id=ledger.require_tenant(TENANT_ONE).tenant_account_id,
            late_adjustment_id=adjustment.late_adjustment_id,
            target_period_id=adjustment.target_period_id,
            adjustment_amount=adjustment.adjustment_amount,
            currency_code=adjustment.currency_code,
            applied_by="operator:alice",
            authorization_reference="change:target-state",
            applied_at=datetime(2026, 8, 17, 22, 0, tzinfo=UTC),
            late_adjustment_application_contract_version=1,
            late_adjustment_application_status="applied",
        )
        with self.assertRaisesRegex(ValueError, "target period is missing"):
            ledger.insert_late_adjustment_application(candidate)
        with self.assertRaisesRegex(ValueError, "source is missing"):
            ledger.insert_late_adjustment_application(
                replace(
                    candidate,
                    late_adjustment_application_id=uuid4(),
                    late_adjustment_id=uuid4(),
                )
            )
        with self.assertRaisesRegex(ValueError, "source does not match"):
            ledger.insert_late_adjustment_application(
                replace(
                    candidate,
                    late_adjustment_application_id=uuid4(),
                    adjustment_amount=Decimal("4.01"),
                )
            )
        missing = LateAdjustmentApplicationService(ledger).apply_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            applied_by="operator:alice",
            authorization_reference="change:target-state",
        )
        self.assertEqual(missing.rejection_reason_code, "target_period_not_found")

        ledger.insert_billing_period(target)
        accepted = LateAdjustmentApplicationService(ledger).apply_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            applied_by="operator:alice",
            authorization_reference="change:target-state",
        )
        self.assertEqual(accepted.late_adjustment_application_outcome_code, "accepted")
        second_adjustment = replace(
            adjustment,
            late_adjustment_id=uuid4(),
            source_reference="provider:application-target-state-second",
        )
        ledger.insert_late_adjustment(TENANT_ONE, second_adjustment)
        closed_target = target.advance(
            BillingPeriodStatus.SOFT_CLOSED,
            actor_reference="operator:alice",
            authorization_reference="change:target-state-close",
            reason="close target",
            transitioned_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        ledger.insert_billing_period(closed_target)
        with self.assertRaisesRegex(ValueError, "target period must be open"):
            ledger.insert_late_adjustment_application(
                replace(
                    candidate,
                    late_adjustment_application_id=uuid4(),
                    late_adjustment_id=second_adjustment.late_adjustment_id,
                )
            )
        replay = LateAdjustmentApplicationService(ledger).apply_late_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            applied_by="operator:bob",
            authorization_reference="change:target-state-replay",
        )
        self.assertEqual(replay.late_adjustment_application_outcome_code, "duplicate_replay")
        self.assertEqual(replay.applied_by, "operator:alice")
        self.assertEqual(replay.authorization_reference, "change:target-state")

    def test_http_rejects_closed_target_as_contract_error(self) -> None:
        """A closed target is a domain rejection rather than a server error."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "4.00",
            "USD",
            "provider:application-http-closed",
            "sha256:" + "d" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        seed_adjustment_periods(ledger, adjustment)
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        target = ledger.get_billing_period(TENANT_ONE, adjustment.target_period_id)
        self.assertIsNotNone(target)
        assert target is not None
        ledger.insert_billing_period(
            target.advance(
                BillingPeriodStatus.SOFT_CLOSED,
                actor_reference="operator:alice",
                authorization_reference="change:http-close",
                reason="close target",
                transitioned_at=datetime(2026, 8, 18, tzinfo=UTC),
            )
        )
        status, body = invoke_http(
            create_http_app(ledger),
            "POST",
            f"/v1/late-adjustments/{adjustment.late_adjustment_id}/applications",
            {
                "tenant_reference": TENANT_ONE,
                "applied_by": "operator:alice",
                "authorization_reference": "change:http-closed",
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["rejection_reason_code"], "target_period_not_open")
        self.assertEqual(validate_late_adjustment_application(body), ())

    def test_postgres_target_trigger_errors_become_domain_rejections(self) -> None:
        """Only recognized PostgreSQL target lifecycle failures are translated."""
        ledger = seed_ledger()
        adjustment = create_late_adjustment(
            uuid4(),
            uuid4(),
            "correction",
            "4.00",
            "USD",
            "provider:application-target-trigger",
            "sha256:" + "e" * 64,
            datetime(2026, 8, 17, 21, 0, tzinfo=UTC),
            late_adjustment_id=uuid4(),
        )
        seed_adjustment_periods(ledger, adjustment)
        ledger.insert_late_adjustment(TENANT_ONE, adjustment)
        target = ledger.get_billing_period(TENANT_ONE, adjustment.target_period_id)
        assert target is not None
        service = LateAdjustmentApplicationService(ledger)
        for message, reason in (
            (
                "late adjustment application target period is missing",
                "target_period_not_found",
            ),
            (
                "late adjustment application target period must be open",
                "target_period_not_open",
            ),
        ):
            error = RuntimeError(message)
            error.diag = type("Diag", (), {"message_primary": message})()
            with mock.patch.object(
                ledger, "insert_late_adjustment_application", side_effect=error
            ):
                result = service.apply_late_adjustment(
                    TENANT_ONE,
                    adjustment.late_adjustment_id,
                    applied_by="operator:alice",
                    authorization_reference="change:trigger",
                )
            self.assertEqual(result.rejection_reason_code, reason)

        with mock.patch.object(
            ledger,
            "insert_late_adjustment_application",
            side_effect=RuntimeError("unrelated persistence failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unrelated persistence failure"):
                service.apply_late_adjustment(
                    TENANT_ONE,
                    adjustment.late_adjustment_id,
                    applied_by="operator:alice",
                    authorization_reference="change:trigger",
                )


if __name__ == "__main__":
    unittest.main()
