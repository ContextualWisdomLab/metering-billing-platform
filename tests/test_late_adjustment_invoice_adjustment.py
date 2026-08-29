"""Tests for composing rated late adjustments into invoice intent."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
import unittest
from uuid import uuid4
from unittest import mock

from metering_billing import (
    InvoiceDraftService,
    IssuedInvoiceService,
    LateAdjustmentApplicationService,
    LateAdjustmentInvoiceAdjustmentService,
    LateAdjustmentPresentmentService,
    LateAdjustmentRatingService,
    MemoryUsageLedger,
    UsageRatingService,
    create_billing_period,
    create_http_app,
    create_late_adjustment,
    validate_late_adjustment_invoice_adjustment,
)
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO
from test_usage_rating import MORNING_WINDOW, ingest_known_batch
from metering_billing.usage_ledger import StoredLateAdjustmentInvoiceAdjustment


def prepare_invoice_adjustment():
    """Build a rated late adjustment and one unissued same-currency draft."""
    ingest = ingest_known_batch()
    ledger = ingest.ledger
    rating = UsageRatingService(ledger).rate_usage_window(
        TENANT_ONE, MORNING_WINDOW, 1
    )
    draft = InvoiceDraftService(ledger).draft_invoice(TENANT_ONE, rating.rating_run_id)
    source = create_billing_period(
        TENANT_ONE,
        date(2026, 7, 1),
        date(2026, 8, 1),
        opened_by="operator:period",
        opened_at=datetime(2026, 7, 1, tzinfo=UTC),
        period_id=uuid4(),
    ).advance(
        "soft_closed",
        actor_reference="operator:period",
        authorization_reference="approval:period",
        reason="close source",
        transitioned_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    target = create_billing_period(
        TENANT_ONE,
        date(2026, 8, 1),
        date(2026, 9, 1),
        opened_by="operator:period",
        opened_at=datetime(2026, 8, 1, tzinfo=UTC),
        period_id=uuid4(),
    )
    ledger.insert_billing_period(source)
    ledger.insert_billing_period(target)
    adjustment = create_late_adjustment(
        source.period_id,
        target.period_id,
        "correction",
        "-12.50",
        "USD",
        "provider:late-invoice-001",
        "sha256:" + "a" * 64,
        datetime(2026, 8, 2, tzinfo=UTC),
        late_adjustment_id=uuid4(),
    )
    ledger.insert_late_adjustment(TENANT_ONE, adjustment)
    LateAdjustmentApplicationService(ledger).apply_late_adjustment(
        TENANT_ONE,
        adjustment.late_adjustment_id,
        applied_by="operator:finance",
        authorization_reference="approval:apply",
    )
    LateAdjustmentRatingService(ledger).rate_late_adjustment(
        TENANT_ONE,
        adjustment.late_adjustment_id,
        rated_by="operator:finance",
        authorization_reference="approval:rate",
    )
    return ledger, adjustment, draft


def stored_candidate(ledger, adjustment, draft):
    """Build a valid direct-ledger composition candidate for boundary tests."""
    tenant = ledger.require_tenant(TENANT_ONE)
    rating = ledger.find_late_adjustment_rating(
        tenant.tenant_account_id, adjustment.late_adjustment_id
    )
    assert rating is not None
    return StoredLateAdjustmentInvoiceAdjustment(
        late_adjustment_invoice_adjustment_id=uuid4(),
        tenant_account_id=tenant.tenant_account_id,
        late_adjustment_rating_id=rating.late_adjustment_rating_id,
        late_adjustment_application_id=rating.late_adjustment_application_id,
        late_adjustment_id=rating.late_adjustment_id,
        invoice_draft_id=draft.invoice_draft_id,
        target_period_id=rating.target_period_id,
        adjustment_amount=rating.adjustment_amount,
        currency_code=rating.currency_code,
        recorded_by="operator:test",
        authorization_reference="approval:test",
        recorded_at=datetime(2026, 8, 3, tzinfo=UTC),
        source_payload_hash="sha256:" + "b" * 64,
        late_adjustment_invoice_adjustment_contract_version=1,
        late_adjustment_invoice_adjustment_status="recorded",
    )


class LateAdjustmentInvoiceAdjustmentTests(unittest.TestCase):
    """Verify exact composition, replay, isolation, and issued-draft safety."""

    def test_composes_rated_delta_without_rewriting_draft(self) -> None:
        """The signed rating becomes one immutable invoice-intent fact."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        service = LateAdjustmentInvoiceAdjustmentService(
            ledger,
            clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
        )
        accepted = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:finance",
            authorization_reference="approval:invoice-adjustment",
        )
        self.assertEqual(
            accepted.late_adjustment_invoice_adjustment_outcome_code, "accepted"
        )
        self.assertEqual(accepted.adjustment_amount, Decimal("-12.50"))
        self.assertEqual(validate_late_adjustment_invoice_adjustment(accepted.as_contract_dict()), ())
        self.assertEqual(len(ledger.late_adjustment_invoice_adjustments), 1)
        stored_draft = ledger.get_invoice_draft(draft.invoice_draft_id)
        self.assertIsNotNone(stored_draft)
        assert stored_draft is not None
        self.assertEqual(stored_draft.drafted_total_amount, draft.drafted_total_amount)
        self.assertEqual(len(stored_draft.invoice_draft_lines), len(draft.invoice_draft_lines))
        self.assertEqual(
            LateAdjustmentPresentmentService(ledger)
            .present_late_adjustment(TENANT_ONE, adjustment.late_adjustment_id)
            .next_operator_action,
            "issue_invoice",
        )
        replay = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:other",
            authorization_reference="approval:other",
        )
        self.assertEqual(
            replay.late_adjustment_invoice_adjustment_outcome_code,
            "duplicate_replay",
        )
        self.assertEqual(
            replay.late_adjustment_invoice_adjustment_id,
            accepted.late_adjustment_invoice_adjustment_id,
        )

    def test_requires_rating_and_rejects_other_tenant_or_issued_draft(self) -> None:
        """Composition cannot bypass rating, tenant scope, or invoice immutability."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        service = LateAdjustmentInvoiceAdjustmentService(ledger)
        self.assertEqual(
            service.record_invoice_adjustment(
                TENANT_TWO,
                adjustment.late_adjustment_id,
                draft.invoice_draft_id,
                recorded_by="operator:finance",
                authorization_reference="approval:invoice-adjustment",
            ).rejection_reason_code,
            "late_adjustment_not_found",
        )
        IssuedInvoiceService(ledger).issue_invoice(TENANT_ONE, draft.invoice_draft_id)
        rejected = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:finance",
            authorization_reference="approval:invoice-adjustment",
        )
        self.assertEqual(rejected.rejection_reason_code, "invoice_already_issued")

    def test_http_command_is_tenant_scoped_and_schema_valid(self) -> None:
        """The nested command accepts the same result through the HTTP adapter."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        status, body = invoke_http(
            create_http_app(ledger),
            "POST",
            f"/v1/late-adjustments/{adjustment.late_adjustment_id}/invoice-adjustments",
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(draft.invoice_draft_id),
                "recorded_by": "operator:http",
                "authorization_reference": "approval:http",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_late_adjustment_invoice_adjustment(body), ())
        self.assertEqual(body["next_operator_action"], "issue_invoice")
        path = f"/v1/late-adjustments/{adjustment.late_adjustment_id}/invoice-adjustments"
        status, body = invoke_http(
            create_http_app(ledger),
            "POST",
            path,
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": str(draft.invoice_draft_id),
                "recorded_by": "operator:http",
                "authorization_reference": "approval:http",
                "card_pan": "4111111111111111",
            },
        )
        self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))
        status, body = invoke_http(
            create_http_app(ledger),
            "POST",
            path,
            {
                "tenant_reference": TENANT_ONE,
                "invoice_draft_id": "not-a-uuid",
                "recorded_by": "operator:http",
                "authorization_reference": "approval:http",
            },
        )
        self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))
        with mock.patch.object(
            LateAdjustmentInvoiceAdjustmentService,
            "record_invoice_adjustment",
            side_effect=ValueError("unexpected persistence error"),
        ):
            status, body = invoke_http(
                create_http_app(ledger),
                "POST",
                path,
                {
                    "tenant_reference": TENANT_ONE,
                    "invoice_draft_id": str(draft.invoice_draft_id),
                    "recorded_by": "operator:http",
                    "authorization_reference": "approval:http",
                },
            )
        self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))
        status, body = invoke_http(create_http_app(ledger), "GET", path)
        self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))

    def test_service_rejects_each_precondition_and_formats_sparse_contracts(self) -> None:
        """Every rejected command remains sparse and points to one next action."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        service = LateAdjustmentInvoiceAdjustmentService(ledger)
        cases = (
            ("tenant_not_found", ("urn:cwl:missing", adjustment.late_adjustment_id, draft.invoice_draft_id, "operator:x", "approval:x")),
            ("late_adjustment_not_found", (TENANT_ONE, uuid4(), draft.invoice_draft_id, "operator:x", "approval:x")),
            ("actor_reference_invalid", (TENANT_ONE, adjustment.late_adjustment_id, draft.invoice_draft_id, " ", "approval:x")),
            ("authorization_reference_invalid", (TENANT_ONE, adjustment.late_adjustment_id, draft.invoice_draft_id, "operator:x", None)),
            ("invoice_draft_not_found", (TENANT_ONE, adjustment.late_adjustment_id, "not-a-uuid", "operator:x", "approval:x")),
        )
        for reason, (tenant, late_id, draft_id, recorded_by, authorization) in cases:
            with self.subTest(reason=reason):
                result = service.record_invoice_adjustment(
                    tenant,
                    late_id,
                    draft_id,
                    recorded_by=recorded_by,
                    authorization_reference=authorization,
                )
                self.assertEqual(result.rejection_reason_code.value, reason)
                self.assertEqual(
                    result.as_contract_dict()[
                        "late_adjustment_invoice_adjustment_outcome_code"
                    ],
                    "rejected",
                )
        self.assertEqual(
            service.record_invoice_adjustment(
                TENANT_ONE,
                "not-a-uuid",
                draft.invoice_draft_id,
                recorded_by="operator:x",
                authorization_reference="approval:x",
            ).rejection_reason_code.value,
            "late_adjustment_not_found",
        )

        no_rating_ledger, no_rating_adjustment, no_rating_draft = prepare_invoice_adjustment()
        no_rating_tenant = no_rating_ledger.require_tenant(TENANT_ONE)
        no_rating = no_rating_ledger.find_late_adjustment_rating(
            no_rating_tenant.tenant_account_id, no_rating_adjustment.late_adjustment_id
        )
        assert no_rating is not None
        no_rating_ledger.late_adjustment_ratings.pop(no_rating.late_adjustment_rating_id)
        no_rating_ledger.late_adjustment_rating_index.pop(
            (no_rating_tenant.tenant_account_id, no_rating_adjustment.late_adjustment_id)
        )
        self.assertEqual(
            LateAdjustmentInvoiceAdjustmentService(no_rating_ledger)
            .record_invoice_adjustment(
                TENANT_ONE,
                no_rating_adjustment.late_adjustment_id,
                no_rating_draft.invoice_draft_id,
                recorded_by="operator:x",
                authorization_reference="approval:x",
            )
            .rejection_reason_code.value,
            "late_adjustment_rating_not_found",
        )

        draft_missing = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            uuid4(),
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        self.assertEqual(draft_missing.rejection_reason_code.value, "invoice_draft_not_found")
        stored_draft = ledger.get_invoice_draft(draft.invoice_draft_id)
        assert stored_draft is not None
        ledger.invoice_drafts[draft.invoice_draft_id] = replace(
            stored_draft, currency_code="EUR"
        )
        currency_mismatch = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        self.assertEqual(currency_mismatch.rejection_reason_code.value, "currency_mismatch")
        self.assertEqual(
            service.record_invoice_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                uuid4(),
                recorded_by="operator:x",
                authorization_reference="approval:x",
            ).rejection_reason_code.value,
            "invoice_draft_not_found",
        )

        malformed = replace(
            service.record_invoice_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                draft.invoice_draft_id,
                recorded_by="operator:x",
                authorization_reference="approval:x",
            ),
            rejection_reason_code=None,
        )
        self.assertEqual(
            malformed.as_contract_dict()["rejection_reason_code"], "invoice_draft_not_found"
        )

    def test_service_rejects_composition_identity_conflicts(self) -> None:
        """One rated identity cannot be attached to a second invoice draft."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        service = LateAdjustmentInvoiceAdjustmentService(ledger)
        self.assertEqual(
            service.record_invoice_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                draft.invoice_draft_id,
                recorded_by="operator:x",
                authorization_reference="approval:x",
            ).late_adjustment_invoice_adjustment_outcome_code,
            "accepted",
        )
        conflict = service.record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            uuid4(),
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        self.assertEqual(
            conflict.rejection_reason_code.value,
            "late_adjustment_invoice_adjustment_identity_conflict",
        )

    def test_memory_insert_validates_and_replays_immutable_composition(self) -> None:
        """The in-memory authority enforces the same immutable fact boundaries."""
        ledger, adjustment, draft = prepare_invoice_adjustment()
        candidate = stored_candidate(ledger, adjustment, draft)
        self.assertIsNone(ledger.get_late_adjustment_invoice_adjustment(uuid4()))
        for bad in (
            replace(candidate, late_adjustment_invoice_adjustment_status="pending"),
            replace(candidate, currency_code="US"),
            replace(candidate, adjustment_amount=Decimal("0")),
            replace(candidate, adjustment_amount=Decimal("NaN")),
            replace(candidate, adjustment_amount=Decimal("1E+40")),
            replace(candidate, recorded_by=" "),
            replace(candidate, authorization_reference=" "),
            replace(candidate, source_payload_hash="invalid"),
        ):
            with self.assertRaises(ValueError):
                ledger.insert_late_adjustment_invoice_adjustment(bad)
        stored = ledger.insert_late_adjustment_invoice_adjustment(candidate)
        self.assertEqual(stored, candidate)
        replay = ledger.insert_late_adjustment_invoice_adjustment(
            replace(
                candidate,
                late_adjustment_invoice_adjustment_id=uuid4(),
                recorded_by="operator:replay",
                authorization_reference="approval:replay",
                recorded_at=datetime(2026, 8, 4, tzinfo=UTC),
            )
        )
        self.assertEqual(replay, stored)
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_invoice_adjustment(
                replace(candidate, late_adjustment_invoice_adjustment_id=uuid4(), invoice_draft_id=uuid4())
            )
        with self.assertRaises(ValueError):
            ledger.insert_late_adjustment_invoice_adjustment(candidate)

        for variant in ("missing_rating", "missing_draft", "evidence", "issued"):
            test_ledger, test_adjustment, test_draft = prepare_invoice_adjustment()
            test_candidate = stored_candidate(test_ledger, test_adjustment, test_draft)
            if variant == "missing_rating":
                test_candidate = replace(test_candidate, late_adjustment_rating_id=uuid4())
            elif variant == "missing_draft":
                test_candidate = replace(test_candidate, invoice_draft_id=uuid4())
            elif variant == "evidence":
                test_candidate = replace(test_candidate, adjustment_amount=Decimal("-12.51"))
            else:
                IssuedInvoiceService(test_ledger).issue_invoice(
                    TENANT_ONE, test_draft.invoice_draft_id
                )
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                test_ledger.insert_late_adjustment_invoice_adjustment(test_candidate)

    def test_service_maps_insert_race_and_contract_validation_edges(self) -> None:
        """The service remains fail-closed when issuance wins between prechecks."""
        ledger, adjustment, draft = prepare_invoice_adjustment()

        class IssuanceRaceLedger:
            """Simulate an issued-invoice race after the service precheck."""

            def __init__(self, delegate: MemoryUsageLedger) -> None:
                self.delegate = delegate

            def find_issued_invoice(self, *_args):
                return None

            def insert_late_adjustment_invoice_adjustment(self, *_args):
                raise ValueError("invoice draft already has an issued invoice")

            def __getattr__(self, name):
                return getattr(self.delegate, name)

        raced = LateAdjustmentInvoiceAdjustmentService(IssuanceRaceLedger(ledger)).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        self.assertEqual(raced.rejection_reason_code.value, "invoice_already_issued")

        class UnexpectedLedger(IssuanceRaceLedger):
            """Preserve unexpected persistence errors for the caller."""

            def insert_late_adjustment_invoice_adjustment(self, *_args):
                raise ValueError("unexpected persistence error")

        with self.assertRaisesRegex(ValueError, "unexpected persistence error"):
            LateAdjustmentInvoiceAdjustmentService(UnexpectedLedger(ledger)).record_invoice_adjustment(
                TENANT_ONE,
                adjustment.late_adjustment_id,
                draft.invoice_draft_id,
                recorded_by="operator:x",
                authorization_reference="approval:x",
            )

        accepted = LateAdjustmentInvoiceAdjustmentService(ledger).record_invoice_adjustment(
            TENANT_ONE,
            adjustment.late_adjustment_id,
            draft.invoice_draft_id,
            recorded_by="operator:x",
            authorization_reference="approval:x",
        )
        with self.assertRaises(ValueError):
            replace(accepted, source_payload_hash=None).as_contract_dict()
        self.assertTrue(validate_late_adjustment_invoice_adjustment(None))
        self.assertTrue(
            validate_late_adjustment_invoice_adjustment(
                accepted.as_contract_dict() | {"adjustment_amount": "0"}
            )
        )
        self.assertTrue(
            validate_late_adjustment_invoice_adjustment(
                accepted.as_contract_dict() | {"adjustment_amount": "not-a-decimal"}
            )
        )
        self.assertTrue(
            validate_late_adjustment_invoice_adjustment(
                accepted.as_contract_dict() | {"adjustment_amount": Decimal("-1")}
            )
        )
