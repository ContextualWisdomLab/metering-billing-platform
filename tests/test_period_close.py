"""Reality-based tests for period lifecycle, FX snapshots, and reconciliation lines."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from uuid import UUID, uuid4

import metering_billing.contracts as contracts_module
import metering_billing.period_close as period_close_module
from metering_billing import (
    BillingPeriod,
    BillingPeriodStatus,
    BillingPeriodTransition,
    FxRate,
    FxRateType,
    PeriodCloseValidationError,
    ReconciliationException,
    ReconciliationExceptionCode,
    ReconciliationEvidence,
    ReconciliationLine,
    ReconciliationLineStatus,
    ReconciliationResolution,
    ReconciliationResolutionStatus,
    assess_reconciliation_line,
    convert_currency_amount,
    create_billing_period,
    create_fx_rate,
    validate_billing_period,
    validate_fx_conversion,
    validate_fx_rate,
    validate_reconciliation_line,
    validate_reconciliation_evidence,
    validate_reconciliation_resolution,
)
from scripts.validate_repository import validate_schema_instance


OPENED_AT = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
PERIOD_ID = UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bf620")


def make_period() -> BillingPeriod:
    """Return one deterministic open period for lifecycle tests."""
    return create_billing_period(
        "urn:cwl:tenant_001",
        date(2026, 8, 1),
        date(2026, 9, 1),
        opened_by="operator:finance_001",
        opened_at=OPENED_AT,
        period_id=PERIOD_ID,
    )


def make_rate() -> FxRate:
    """Return one high-precision USD-to-KRW rate fixture."""
    return create_fx_rate(
        "provider:fx_001",
        FxRateType.PROVIDER,
        "USD",
        "KRW",
        "1350.1234",
        4,
        datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
        datetime(2026, 8, 31, 15, 1, tzinfo=UTC),
        fx_rate_id=UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bf621"),
    )


class BillingPeriodTests(unittest.TestCase):
    """Verify append-only lifecycle behavior and its contract projection."""

    def test_period_advances_once_per_state_and_preserves_prior_snapshot(self) -> None:
        """The full lifecycle is monotonic and the original object stays open."""
        period = make_period()
        original = period
        for offset, status in enumerate(
            (
                BillingPeriodStatus.SOFT_CLOSED,
                BillingPeriodStatus.RECONCILED,
                BillingPeriodStatus.INVOICED,
                BillingPeriodStatus.HARD_CLOSED,
            ),
            start=1,
        ):
            period = period.advance(
                status,
                actor_reference=f"operator:finance_{offset:03d}",
                authorization_reference=f"approval:period_{offset:03d}",
                reason=f"period transition {offset}",
                transitioned_at=OPENED_AT + timedelta(hours=offset),
                transition_id=UUID(f"019d7b92-1aa0-7a7f-b61c-962c0f4bf6{20 + offset:02d}"),
            )
        self.assertEqual(original.status, BillingPeriodStatus.OPEN)
        self.assertEqual(period.status, BillingPeriodStatus.HARD_CLOSED)
        self.assertEqual(len(period.transitions), 4)
        self.assertEqual(validate_billing_period(period.as_contract_dict()), ())
        self.assertEqual(period.as_contract_dict()["period_status"], "hard_closed")

    def test_period_rejects_invalid_identity_dates_history_and_mutation(self) -> None:
        """Invalid periods and non-forward transitions fail without creating state."""
        with self.assertRaises(PeriodCloseValidationError):
            create_billing_period(
                "tenant_001",
                date(2026, 8, 1),
                date(2026, 9, 1),
                opened_by="operator:finance_001",
                opened_at=OPENED_AT,
            )
        with self.assertRaises(PeriodCloseValidationError):
            create_billing_period(
                "urn:cwl:tenant_001",
                datetime(2026, 8, 1, tzinfo=UTC),  # type: ignore[arg-type]
                date(2026, 9, 1),
                opened_by="operator:finance_001",
                opened_at=OPENED_AT,
            )
        with self.assertRaises(PeriodCloseValidationError):
            create_billing_period(
                "urn:cwl:tenant_001",
                "2026-08-01",  # type: ignore[arg-type]
                date(2026, 9, 1),
                opened_by="operator:finance_001",
                opened_at=OPENED_AT,
            )
        with self.assertRaises(PeriodCloseValidationError):
            create_billing_period(
                "urn:cwl:tenant_001",
                date(2026, 9, 1),
                date(2026, 8, 1),
                opened_by="operator:finance_001",
                opened_at=OPENED_AT,
            )
        with self.assertRaises(PeriodCloseValidationError):
            create_billing_period(
                "urn:cwl:tenant_001",
                date(2026, 8, 1),
                date(2026, 9, 1),
                opened_by="operator:finance_001",
                opened_at=datetime(2026, 8, 1),
            )
        with self.assertRaises(PeriodCloseValidationError):
            create_billing_period(
                "urn:cwl:tenant_001",
                date(2026, 8, 1),
                date(2026, 9, 1),
                opened_by="operator:finance_001",
                opened_at=OPENED_AT,
                period_id="not-a-uuid",  # type: ignore[arg-type]
            )

        with self.assertRaises(PeriodCloseValidationError):
            BillingPeriodTransition(
                uuid4(),
                BillingPeriodStatus.OPEN,
                BillingPeriodStatus.HARD_CLOSED,
                "operator:finance_001",
                "approval:period_001",
                "skip states",
                OPENED_AT,
            )
        with self.assertRaises(PeriodCloseValidationError):
            BillingPeriodTransition(
                uuid4(),
                "unsupported",  # type: ignore[arg-type]
                BillingPeriodStatus.SOFT_CLOSED,
                "operator:finance_001",
                "approval:period_001",
                "bad state",
                OPENED_AT,
            )
        valid_transition = BillingPeriodTransition(
            uuid4(),
            BillingPeriodStatus.SOFT_CLOSED,
            BillingPeriodStatus.RECONCILED,
            "operator:finance_001",
            "approval:period_001",
            "reconcile",
            OPENED_AT + timedelta(hours=1),
        )
        with self.assertRaises(PeriodCloseValidationError):
            BillingPeriod(
                PERIOD_ID,
                "urn:cwl:tenant_001",
                date(2026, 8, 1),
                date(2026, 9, 1),
                OPENED_AT,
                "operator:finance_001",
                BillingPeriodStatus.RECONCILED,
                (valid_transition,),
            )
        with self.assertRaises(PeriodCloseValidationError):
            replace(make_period(), transitions=[valid_transition])  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(make_period(), transitions=(object(),))  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(make_period(), status="unsupported")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(
                make_period(),
                status=BillingPeriodStatus.RECONCILED,
                transitions=(
                    BillingPeriodTransition(
                        uuid4(),
                        BillingPeriodStatus.OPEN,
                        BillingPeriodStatus.SOFT_CLOSED,
                        "operator:finance_001",
                        "approval:period_001",
                        "soft close",
                        OPENED_AT + timedelta(hours=1),
                    ),
                ),
            )
        with self.assertRaises(PeriodCloseValidationError):
            make_period().advance(
                BillingPeriodStatus.HARD_CLOSED,
                actor_reference="operator:finance_001",
                authorization_reference="approval:period_001",
                reason="skip states",
                transitioned_at=OPENED_AT + timedelta(hours=1),
            )
        first_transition = make_period().advance(
            BillingPeriodStatus.SOFT_CLOSED,
            actor_reference="operator:finance_001",
            authorization_reference="approval:period_001",
            reason="soft close",
            transitioned_at=OPENED_AT + timedelta(hours=1),
            transition_id=UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bf630"),
        )
        with self.assertRaises(PeriodCloseValidationError):
            first_transition.advance(
                BillingPeriodStatus.RECONCILED,
                actor_reference="operator:finance_002",
                authorization_reference="approval:period_002",
                reason="reconcile",
                transitioned_at=OPENED_AT + timedelta(hours=2),
                transition_id=first_transition.transitions[0].transition_id,
            )
        duplicate_transition = first_transition.as_contract_dict()
        duplicate_transition["period_status"] = "reconciled"
        duplicate_transition["transitions"].append(
            {
                **duplicate_transition["transitions"][0],
                "from_status": "soft_closed",
                "to_status": "reconciled",
            }
        )
        self.assertTrue(validate_billing_period(duplicate_transition))
        with self.assertRaises(PeriodCloseValidationError):
            make_period().advance(
                "unsupported",  # type: ignore[arg-type]
                actor_reference="operator:finance_001",
                authorization_reference="approval:period_001",
                reason="bad state",
                transitioned_at=OPENED_AT + timedelta(hours=1),
            )
        hard_closed = make_period().advance(
            BillingPeriodStatus.SOFT_CLOSED,
            actor_reference="operator:finance_001",
            authorization_reference="approval:period_001",
            reason="soft close",
            transitioned_at=OPENED_AT + timedelta(hours=1),
        )
        hard_closed = hard_closed.advance(
            BillingPeriodStatus.RECONCILED,
            actor_reference="operator:finance_001",
            authorization_reference="approval:period_002",
            reason="reconcile",
            transitioned_at=OPENED_AT + timedelta(hours=2),
        )
        hard_closed = hard_closed.advance(
            BillingPeriodStatus.INVOICED,
            actor_reference="operator:finance_001",
            authorization_reference="approval:period_003",
            reason="invoice",
            transitioned_at=OPENED_AT + timedelta(hours=3),
        )
        hard_closed = hard_closed.advance(
            BillingPeriodStatus.HARD_CLOSED,
            actor_reference="operator:finance_001",
            authorization_reference="approval:period_004",
            reason="hard close",
            transitioned_at=OPENED_AT + timedelta(hours=4),
        )
        with self.assertRaises(PeriodCloseValidationError):
            hard_closed.advance(
                BillingPeriodStatus.HARD_CLOSED,
                actor_reference="operator:finance_001",
                authorization_reference="approval:period_005",
                reason="mutate",
                transitioned_at=OPENED_AT + timedelta(hours=5),
            )

    def test_period_rejects_bad_transition_fields_and_time_order(self) -> None:
        """Authorization, reason, identifier, and timestamp fields are mandatory."""
        for kwargs in (
            {"transition_id": "not-a-uuid"},
            {"actor_reference": ""},
            {"authorization_reference": ""},
            {"reason": ""},
            {"transitioned_at": datetime(2026, 8, 1)},
        ):
            values = {
                "transition_id": uuid4(),
                "from_status": BillingPeriodStatus.OPEN,
                "to_status": BillingPeriodStatus.SOFT_CLOSED,
                "actor_reference": "operator:finance_001",
                "authorization_reference": "approval:period_001",
                "reason": "soft close",
                "transitioned_at": OPENED_AT + timedelta(hours=1),
            }
            values.update(kwargs)
            with self.assertRaises(PeriodCloseValidationError):
                BillingPeriodTransition(**values)  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            BillingPeriodTransition(
                uuid4(),
                BillingPeriodStatus.OPEN,
                BillingPeriodStatus.OPEN,
                "operator:finance_001",
                "approval:period_001",
                "same state",
                OPENED_AT,
            )
        valid = BillingPeriodTransition(
            uuid4(),
            BillingPeriodStatus.OPEN,
            BillingPeriodStatus.SOFT_CLOSED,
            "operator:finance_001",
            "approval:period_001",
            "soft close",
            OPENED_AT - timedelta(seconds=1),
        )
        with self.assertRaises(PeriodCloseValidationError):
            replace(make_period(), status=BillingPeriodStatus.SOFT_CLOSED, transitions=(valid,))


class FxContractTests(unittest.TestCase):
    """Verify exact rates, frozen conversions, and minor-unit behavior."""

    def test_rate_and_conversion_are_schema_valid_and_replayable(self) -> None:
        """A conversion carries the exact rate used and only target-scale rounding."""
        rate = make_rate()
        self.assertEqual(validate_fx_rate(rate.as_contract_dict()), ())
        conversion = convert_currency_amount(
            "0.01",
            "USD",
            0,
            rate,
            fx_conversion_id=UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bf622"),
        )
        self.assertEqual(conversion.quote_amount, Decimal("14"))
        self.assertEqual(conversion.rate, rate.rate)
        self.assertEqual(conversion.fx_rate_id, rate.fx_rate_id)
        self.assertEqual(validate_fx_conversion(conversion.as_contract_dict()), ())
        self.assertEqual(conversion.as_contract_dict()["rounding_mode"], "ROUND_HALF_UP")

    def test_conversion_supports_two_three_four_decimals_and_negative_adjustment(self) -> None:
        """Currency scales are explicit and signed adjustments remain exact."""
        rate = create_fx_rate(
            "provider:fx_002",
            FxRateType.SPOT,
            "USD",
            "EUR",
            "1.005",
            3,
            OPENED_AT,
            OPENED_AT,
        )
        self.assertEqual(convert_currency_amount("1", "USD", 2, rate).quote_amount, Decimal("1.01"))
        self.assertEqual(convert_currency_amount("1", "USD", 3, rate).quote_amount, Decimal("1.005"))
        self.assertEqual(convert_currency_amount("1", "USD", 4, rate).quote_amount, Decimal("1.0050"))
        self.assertEqual(convert_currency_amount("-1", "USD", 2, rate).quote_amount, Decimal("-1.01"))

    def test_large_conversion_rounds_only_after_exact_multiplication(self) -> None:
        """Large but schema-valid fixed-point values do not inherit context rounding."""
        rate = create_fx_rate(
            "provider:fx_large",
            FxRateType.PROVIDER,
            "USD",
            "EUR",
            "1234567890123456789012345678.123456789",
            9,
            OPENED_AT,
            OPENED_AT,
        )
        source = Decimal("9999999999")
        with localcontext() as context:
            context.prec = 100
            expected = (source * rate.rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        conversion = convert_currency_amount(source, "USD", 0, rate)
        self.assertEqual(conversion.quote_amount, expected)
        self.assertEqual(validate_fx_conversion(conversion.as_contract_dict()), ())

    def test_rate_and_conversion_reject_bad_inputs(self) -> None:
        """No float, invalid currency, zero rate, or under-specified precision crosses the boundary."""
        with self.assertRaises(PeriodCloseValidationError):
            create_fx_rate("", FxRateType.SPOT, "USD", "EUR", "1", 0, OPENED_AT, OPENED_AT)
        with self.assertRaises(PeriodCloseValidationError):
            create_fx_rate("provider:fx", "bad", "USD", "EUR", "1", 0, OPENED_AT, OPENED_AT)  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            create_fx_rate("provider:fx", FxRateType.SPOT, "usd", "EUR", "1", 0, OPENED_AT, OPENED_AT)
        with self.assertRaises(PeriodCloseValidationError):
            create_fx_rate("provider:fx", FxRateType.SPOT, "USD", "EUR", "0", 0, OPENED_AT, OPENED_AT)
        with self.assertRaises(PeriodCloseValidationError):
            create_fx_rate("provider:fx", FxRateType.SPOT, "USD", "EUR", -1, 0, OPENED_AT, OPENED_AT)  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            create_fx_rate("provider:fx", FxRateType.SPOT, "USD", "EUR", "1.2345", 3, OPENED_AT, OPENED_AT)
        with self.assertRaises(PeriodCloseValidationError):
            create_fx_rate("provider:fx", FxRateType.SPOT, "USD", "EUR", "1", True, OPENED_AT, OPENED_AT)  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            create_fx_rate("provider:fx", FxRateType.SPOT, "USD", "EUR", "1", 0, datetime(2026, 8, 1), OPENED_AT)
        with self.assertRaises(PeriodCloseValidationError):
            FxRate("not-a-uuid", "provider:fx", FxRateType.SPOT, "USD", "EUR", Decimal("1"), 0, OPENED_AT, OPENED_AT)  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            convert_currency_amount("1", "EUR", 2, make_rate())
        with self.assertRaises(PeriodCloseValidationError):
            convert_currency_amount("1", "USD", 5, make_rate())
        with self.assertRaises(PeriodCloseValidationError):
            convert_currency_amount(1.0, "USD", 2, make_rate())  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            convert_currency_amount("1", "USD", 2, object())  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            convert_currency_amount(True, "USD", 2, make_rate())  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            convert_currency_amount("1e2", "USD", 2, make_rate())

    def test_conversion_value_validation_rejects_broken_frozen_documents(self) -> None:
        """A manually constructed result cannot disagree with its exact metadata."""
        rate = make_rate()
        conversion = convert_currency_amount("1", "USD", 2, rate)
        with self.assertRaises(PeriodCloseValidationError):
            replace(conversion, fx_rate_id="not-a-uuid")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(conversion, source_currency="usd")
        with self.assertRaises(PeriodCloseValidationError):
            replace(conversion, quote_minor_units=5)
        with self.assertRaises(PeriodCloseValidationError):
            replace(conversion, rate=Decimal("0"))
        with self.assertRaises(PeriodCloseValidationError):
            replace(conversion, rate_precision=3)
        with self.assertRaises(PeriodCloseValidationError):
            replace(conversion, quote_amount=Decimal("1.234"))
        with self.assertRaises(PeriodCloseValidationError):
            replace(conversion, quote_amount=Decimal("1.00"))
        with self.assertRaises(PeriodCloseValidationError):
            create_fx_rate(
                "provider:fx",
                FxRateType.SPOT,
                "USD",
                "EUR",
                "1." + "0" * 38,
                38,
                OPENED_AT,
                OPENED_AT,
            )
        with self.assertRaises(PeriodCloseValidationError):
            replace(conversion, converted_at=datetime(2026, 8, 1))


class ReconciliationTests(unittest.TestCase):
    """Verify three-way comparisons retain independent money components."""

    def test_matched_line_keeps_fees_withholding_and_reserve_separate(self) -> None:
        """Provider actual less deductions must equal the cash actual exactly."""
        line = assess_reconciliation_line(
            PERIOD_ID,
            "provider_account:001",
            "USD",
            "100.00",
            "100.00",
            "94.00",
            provider_fee_amount="3.00",
            withheld_tax_amount="1.00",
            reserve_amount="2.00",
            assessed_at=OPENED_AT,
            internal_currency_code="USD",
            provider_currency_code="USD",
            cash_currency_code="USD",
            reconciliation_line_id=UUID("019d7b92-1aa0-7a7f-b61c-962c0f4bf623"),
        )
        self.assertIsInstance(line, ReconciliationLine)
        self.assertEqual(line.status, ReconciliationLineStatus.MATCHED)
        self.assertEqual(line.exceptions, ())
        self.assertEqual(line.expected_cash_amount, Decimal("94.00"))
        self.assertEqual(validate_reconciliation_line(line.as_contract_dict()), ())

    def test_exception_codes_are_deterministic_and_actionable(self) -> None:
        """Price, currency, fee, and settlement defects remain distinct."""
        price = assess_reconciliation_line(
            PERIOD_ID,
            "provider_account:001",
            "USD",
            "100",
            "99",
            "97",
            provider_fee_amount="2",
            assessed_at=OPENED_AT,
            internal_currency_code="USD",
            provider_currency_code="EUR",
            cash_currency_code="USD",
        )
        self.assertEqual(
            tuple(exception.exception_code for exception in price.exceptions),
            (ReconciliationExceptionCode.CURRENCY_MISMATCH, ReconciliationExceptionCode.PRICE_MISMATCH),
        )
        fee = assess_reconciliation_line(
            PERIOD_ID,
            "provider_account:001",
            "USD",
            "100",
            "100",
            "100",
            provider_fee_amount="3",
            assessed_at=OPENED_AT,
            internal_currency_code="USD",
            provider_currency_code="USD",
            cash_currency_code="USD",
        )
        self.assertEqual(fee.exceptions[0].exception_code, ReconciliationExceptionCode.PROVIDER_FEE_MISMATCH)
        settlement = assess_reconciliation_line(
            PERIOD_ID,
            "provider_account:001",
            "USD",
            "100",
            "100",
            "96",
            provider_fee_amount="3",
            assessed_at=OPENED_AT,
            internal_currency_code="USD",
            provider_currency_code="USD",
            cash_currency_code="USD",
        )
        self.assertEqual(settlement.exceptions[0].exception_code, ReconciliationExceptionCode.SETTLEMENT_MISMATCH)
        self.assertEqual(validate_reconciliation_line(price.as_contract_dict()), ())

    def test_exception_vocabulary_covers_the_required_provider_cases(self) -> None:
        """Every minimum issue code remains constructible and serializable."""
        codes = tuple(ReconciliationExceptionCode)
        self.assertEqual(
            codes,
            (
                ReconciliationExceptionCode.QUANTITY_MISMATCH,
                ReconciliationExceptionCode.PRICE_MISMATCH,
                ReconciliationExceptionCode.TAX_MISMATCH,
                ReconciliationExceptionCode.CURRENCY_MISMATCH,
                ReconciliationExceptionCode.PAYMENT_MISSING,
                ReconciliationExceptionCode.DUPLICATE_CHARGE,
                ReconciliationExceptionCode.REFUND_MISMATCH,
                ReconciliationExceptionCode.DISPUTE_MISMATCH,
                ReconciliationExceptionCode.SETTLEMENT_MISMATCH,
                ReconciliationExceptionCode.PROVIDER_FEE_MISMATCH,
                ReconciliationExceptionCode.CASH_TIMING_DIFFERENCE,
                ReconciliationExceptionCode.UNMAPPED_PROVIDER_OBJECT,
            ),
        )
        self.assertEqual(
            tuple(
                ReconciliationException(code, period_close_module._NEXT_ACTIONS[code]).as_contract_dict()
                for code in codes
            ),
            tuple(
                {"exception_code": code.value, "next_action": period_close_module._NEXT_ACTIONS[code]}
                for code in codes
            ),
        )
        expanded_line = replace(
            assess_reconciliation_line(
                PERIOD_ID,
                "provider_account:001",
                "USD",
                "100",
                "99",
                "97",
                provider_fee_amount="2",
                assessed_at=OPENED_AT,
                internal_currency_code="USD",
                provider_currency_code="USD",
                cash_currency_code="USD",
            ),
            exceptions=(
                ReconciliationException(
                    ReconciliationExceptionCode.TAX_MISMATCH,
                    period_close_module._NEXT_ACTIONS[ReconciliationExceptionCode.TAX_MISMATCH],
                ),
            ),
        )
        self.assertEqual(validate_reconciliation_line(expanded_line.as_contract_dict()), ())

    def test_raw_contract_validators_apply_domain_invariants(self) -> None:
        """Schema-valid documents cannot bypass lifecycle, arithmetic, or FX invariants."""
        period = make_period().as_contract_dict()
        period["period_status"] = "hard_closed"
        self.assertTrue(validate_billing_period(period))
        reconciled = make_period().advance(
            BillingPeriodStatus.SOFT_CLOSED,
            actor_reference="operator:finance_001",
            authorization_reference="approval:period_001",
            reason="soft close",
            transitioned_at=OPENED_AT + timedelta(hours=1),
        ).advance(
            BillingPeriodStatus.RECONCILED,
            actor_reference="operator:finance_002",
            authorization_reference="approval:period_002",
            reason="reconcile",
            transitioned_at=OPENED_AT + timedelta(hours=2),
        )
        invalid_transition = reconciled.as_contract_dict()
        invalid_transition["transitions"][1]["from_status"] = "open"
        self.assertTrue(
            validate_schema_instance(
                contracts_module.load_json_schema("billing-period.schema.json"),
                invalid_transition,
            )
        )

        rate = make_rate().as_contract_dict()
        rate["rate"] = "0"
        self.assertTrue(validate_fx_rate(rate))

        conversion = convert_currency_amount("1", "USD", 2, make_rate()).as_contract_dict()
        conversion["quote_amount"] = "1.00"
        self.assertTrue(validate_fx_conversion(conversion))

        line = assess_reconciliation_line(
            PERIOD_ID,
            "provider_account:001",
            "USD",
            "1",
            "1",
            "1",
            assessed_at=OPENED_AT,
            internal_currency_code="USD",
            provider_currency_code="USD",
            cash_currency_code="USD",
        ).as_contract_dict()
        line["expected_cash_amount"] = "2"
        self.assertTrue(validate_reconciliation_line(line))
        self.assertTrue(validate_billing_period({}))
        self.assertTrue(validate_fx_rate([]))
        self.assertTrue(validate_fx_conversion({}))
        self.assertTrue(validate_reconciliation_line({}))

    def test_reconciliation_rejects_bad_amounts_and_inconsistent_documents(self) -> None:
        """Provider fees cannot be negative and frozen line invariants are checked."""
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line("not-a-uuid", "provider_account:001", "USD", "1", "1", "1", assessed_at=OPENED_AT, internal_currency_code="USD", provider_currency_code="USD", cash_currency_code="USD")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line(PERIOD_ID, "", "USD", "1", "1", "1", assessed_at=OPENED_AT, internal_currency_code="USD", provider_currency_code="USD", cash_currency_code="USD")
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line(PERIOD_ID, "provider_account:001", "usd", "1", "1", "1", assessed_at=OPENED_AT, internal_currency_code="USD", provider_currency_code="USD", cash_currency_code="USD")
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line(PERIOD_ID, "provider_account:001", "USD", 1.0, "1", "1", assessed_at=OPENED_AT, internal_currency_code="USD", provider_currency_code="USD", cash_currency_code="USD")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line(PERIOD_ID, "provider_account:001", "USD", "1", "1", "1", provider_fee_amount="-1", assessed_at=OPENED_AT, internal_currency_code="USD", provider_currency_code="USD", cash_currency_code="USD")
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line(PERIOD_ID, "provider_account:001", "USD", "1", "1", "1", assessed_at=datetime(2026, 8, 1), internal_currency_code="USD", provider_currency_code="USD", cash_currency_code="USD")
        matched = assess_reconciliation_line(PERIOD_ID, "provider_account:001", "USD", "1", "1", "1", assessed_at=OPENED_AT, internal_currency_code="USD", provider_currency_code="USD", cash_currency_code="USD")
        with self.assertRaises(PeriodCloseValidationError):
            replace(matched, reconciliation_line_id="not-a-uuid")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(matched, period_id="not-a-uuid")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(matched, expected_cash_amount=Decimal("2"))
        with self.assertRaises(PeriodCloseValidationError):
            replace(matched, status=ReconciliationLineStatus.EXCEPTION)
        with self.assertRaises(PeriodCloseValidationError):
            replace(matched, exceptions=[ReconciliationException(ReconciliationExceptionCode.PRICE_MISMATCH, "inspect")])  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(matched, exceptions=(object(),), status=ReconciliationLineStatus.EXCEPTION)  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(matched, status="bad")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(
                matched,
                status=ReconciliationLineStatus.EXCEPTION,
                exceptions=(
                    ReconciliationException(
                        ReconciliationExceptionCode.PRICE_MISMATCH,
                        period_close_module._NEXT_ACTIONS[ReconciliationExceptionCode.PRICE_MISMATCH],
                    ),
                    ReconciliationException(
                        ReconciliationExceptionCode.PRICE_MISMATCH,
                        period_close_module._NEXT_ACTIONS[ReconciliationExceptionCode.PRICE_MISMATCH],
                    ),
                ),
            )
        with self.assertRaises(PeriodCloseValidationError):
            ReconciliationException("bad", "inspect")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            ReconciliationException(ReconciliationExceptionCode.PRICE_MISMATCH, "")
        with self.assertRaises(PeriodCloseValidationError):
            replace(matched, internal_currency_code="usd")
        with self.assertRaises(PeriodCloseValidationError):
            replace(
                matched,
                exceptions=(
                    ReconciliationException(
                        ReconciliationExceptionCode.PRICE_MISMATCH,
                        period_close_module._NEXT_ACTIONS[ReconciliationExceptionCode.PRICE_MISMATCH],
                    ),
                ),
                status=ReconciliationLineStatus.EXCEPTION,
                provider_currency_code="EUR",
            )

    def test_reconciliation_resolution_requires_distinct_maker_checker(self) -> None:
        """A resolution carries owner, reason, evidence, and two approvers."""
        resolution = ReconciliationResolution(
            resolution_id=uuid4(),
            reconciliation_line_id=PERIOD_ID,
            exception_code=ReconciliationExceptionCode.PRICE_MISMATCH,
            resolution_status=ReconciliationResolutionStatus.RESOLVED,
            owner_reference="operator:finance_001",
            resolution_reason="corrected provider quantity",
            evidence_reference="urn:cwl:evidence:correction-001",
            maker_reference="operator:finance_001",
            checker_reference="operator:finance_002",
            resolved_at=OPENED_AT,
        )
        self.assertEqual(validate_reconciliation_resolution(resolution.as_contract_dict()), ())
        with self.assertRaises(PeriodCloseValidationError):
            replace(
                resolution,
                maker_reference="operator:finance_002",
                checker_reference="operator:finance_002",
            )
        with self.assertRaises(PeriodCloseValidationError):
            replace(resolution, resolution_id="not-a-uuid")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(resolution, exception_code="bad")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(resolution, resolution_status="bad")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(resolution, resolved_at=datetime.now(UTC) + timedelta(days=1))
        invalid = resolution.as_contract_dict()
        invalid["maker_reference"] = invalid["checker_reference"]
        self.assertTrue(validate_reconciliation_resolution(invalid))
        invalid["resolution_status"] = "bad"
        self.assertTrue(validate_reconciliation_resolution(invalid))

    def test_resolution_contract_exports_are_public(self) -> None:
        """Resolution schema and validator remain available from the contract module."""
        self.assertIn("RECONCILIATION_RESOLUTION_SCHEMA_NAME", contracts_module.__all__)
        self.assertIn("validate_reconciliation_resolution", contracts_module.__all__)

    def test_reconciliation_evidence_requires_a_hash_and_source_reference(self) -> None:
        """Evidence keeps a typed exception linked to immutable source content."""
        evidence = ReconciliationEvidence(
            evidence_id=uuid4(),
            reconciliation_line_id=PERIOD_ID,
            exception_code=ReconciliationExceptionCode.TAX_MISMATCH,
            evidence_kind="provider_tax_document",
            evidence_reference="urn:cwl:evidence:provider-tax-001",
            evidence_sha256="sha256:" + "a" * 64,
            captured_by="operator:finance_001",
            captured_at=OPENED_AT,
        )
        self.assertEqual(validate_reconciliation_evidence(evidence.as_contract_dict()), ())
        with self.assertRaises(PeriodCloseValidationError):
            replace(evidence, evidence_id="not-a-uuid")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(evidence, reconciliation_line_id="not-a-uuid")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(evidence, exception_code="bad")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(evidence, evidence_sha256="not-a-hash")
        with self.assertRaises(PeriodCloseValidationError):
            replace(evidence, evidence_sha256=None)  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            replace(evidence, captured_at=datetime.now(UTC) + timedelta(days=1))
        invalid = evidence.as_contract_dict()
        invalid["evidence_sha256"] = "not-a-hash"
        self.assertTrue(validate_reconciliation_evidence(invalid))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
