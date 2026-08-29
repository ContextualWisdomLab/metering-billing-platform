"""Reality-based tests for period lifecycle, FX snapshots, and reconciliation lines."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from metering_billing import (
    BillingPeriod,
    BillingPeriodStatus,
    BillingPeriodTransition,
    FxRate,
    FxRateType,
    PeriodCloseValidationError,
    ReconciliationException,
    ReconciliationExceptionCode,
    ReconciliationLine,
    ReconciliationLineStatus,
    assess_reconciliation_line,
    convert_currency_amount,
    create_billing_period,
    create_fx_rate,
    validate_billing_period,
    validate_fx_conversion,
    validate_fx_rate,
    validate_reconciliation_line,
)


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
            provider_currency_code="EUR",
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
        )
        self.assertEqual(settlement.exceptions[0].exception_code, ReconciliationExceptionCode.SETTLEMENT_MISMATCH)
        self.assertEqual(validate_reconciliation_line(price.as_contract_dict()), ())

    def test_reconciliation_rejects_bad_amounts_and_inconsistent_documents(self) -> None:
        """Provider fees cannot be negative and frozen line invariants are checked."""
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line("not-a-uuid", "provider_account:001", "USD", "1", "1", "1", assessed_at=OPENED_AT)  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line(PERIOD_ID, "", "USD", "1", "1", "1", assessed_at=OPENED_AT)
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line(PERIOD_ID, "provider_account:001", "usd", "1", "1", "1", assessed_at=OPENED_AT)
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line(PERIOD_ID, "provider_account:001", "USD", 1.0, "1", "1", assessed_at=OPENED_AT)  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line(PERIOD_ID, "provider_account:001", "USD", "1", "1", "1", provider_fee_amount="-1", assessed_at=OPENED_AT)
        with self.assertRaises(PeriodCloseValidationError):
            assess_reconciliation_line(PERIOD_ID, "provider_account:001", "USD", "1", "1", "1", assessed_at=datetime(2026, 8, 1))
        matched = assess_reconciliation_line(PERIOD_ID, "provider_account:001", "USD", "1", "1", "1", assessed_at=OPENED_AT)
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
            ReconciliationException("bad", "inspect")  # type: ignore[arg-type]
        with self.assertRaises(PeriodCloseValidationError):
            ReconciliationException(ReconciliationExceptionCode.PRICE_MISMATCH, "")
        with self.assertRaises(PeriodCloseValidationError):
            replace(matched, internal_currency_code="usd")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
