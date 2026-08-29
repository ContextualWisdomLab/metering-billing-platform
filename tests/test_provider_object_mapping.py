"""Tests for provider-sticky internal-to-external object mappings."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from metering_billing import (
    ProviderObjectMapping,
    ProviderObjectMappingError,
    ProviderObjectMappingRegistry,
    validate_provider_object_mapping,
)


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def mapping(**overrides: object) -> ProviderObjectMapping:
    """Build one valid mapping with deterministic identity fields."""
    values: dict[str, object] = {
        "provider_account_reference": "provider_account_001",
        "provider_code": "lemon_squeezy",
        "internal_object_type": "payment_intent",
        "internal_object_reference": "payment_intent_001",
        "provider_object_type": "subscriptions",
        "provider_object_reference": "subscription_001",
        "valid_from": NOW,
        "recorded_at": NOW,
    }
    values.update(overrides)
    return ProviderObjectMapping(**values)


class ProviderObjectMappingTests(unittest.TestCase):
    """Verify validation, resolution, conflict handling, and replacement."""

    def test_contract_and_effective_interval(self) -> None:
        """A mapping normalizes instants and publishes only safe references."""
        stored = mapping(
            valid_from=datetime(2026, 8, 29, 8, tzinfo=UTC),
            valid_to=NOW + timedelta(days=1),
        )
        self.assertEqual(validate_provider_object_mapping(stored.as_contract_dict()), ())
        self.assertEqual(validate_provider_object_mapping(None), ("$: expected object",))
        self.assertEqual(stored.as_contract_dict()["valid_from"], "2026-08-29T08:00:00Z")
        self.assertEqual(stored.as_contract_dict()["valid_to"], "2026-08-30T12:00:00Z")
        self.assertNotIn("valid_to", mapping().as_contract_dict())
        defaulted = ProviderObjectMapping(
            "provider_account_001",
            "lemon_squeezy",
            "payment_intent",
            "payment_intent_002",
            "subscriptions",
            "subscription_002",
            NOW,
        )
        self.assertIsNotNone(defaulted.recorded_at.tzinfo)

    def test_mapping_validation_fails_closed(self) -> None:
        """Malformed mapping trust-boundary values expose stable reason codes."""
        cases = (
            ({"provider_account_reference": None}, "provider_account_reference_invalid"),
            ({"provider_account_reference": ""}, "provider_account_reference_invalid"),
            ({"provider_account_reference": "x\n"}, "provider_account_reference_invalid"),
            ({"provider_code": "Lemon"}, "provider_code_invalid"),
            ({"internal_object_type": "PaymentIntent"}, "internal_object_type_invalid"),
            ({"provider_object_type": "payment-intent"}, "provider_object_type_invalid"),
            ({"internal_object_reference": ""}, "internal_object_reference_invalid"),
            ({"provider_object_reference": "\x00"}, "provider_object_reference_invalid"),
            ({"valid_from": datetime(2026, 8, 29, 12)}, "valid_from_invalid"),
            ({"valid_to": datetime(2026, 8, 29, 12)}, "valid_to_invalid"),
            ({"valid_to": NOW - timedelta(seconds=1)}, "valid_interval_invalid"),
            ({"recorded_at": datetime(2026, 8, 29, 12)}, "recorded_at_invalid"),
        )
        for overrides, reason_code in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ProviderObjectMappingError) as error:
                    mapping(**overrides)
                self.assertEqual(error.exception.reason_code, reason_code)

    def test_registry_is_sticky_and_rejects_collisions(self) -> None:
        """Resolution is provider-sticky and missing history never falls back."""
        registry = ProviderObjectMappingRegistry()
        first = mapping()
        registry.record(first)
        self.assertIs(
            registry.resolve_internal(
                "provider_account_001",
                "payment_intent",
                "payment_intent_001",
                at=NOW,
            ),
            first,
        )
        self.assertIs(
            registry.resolve_external(
                "provider_account_001", "subscriptions", "subscription_001", at=NOW
            ),
            first,
        )
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.record(first)
        self.assertEqual(error.exception.reason_code, "internal_mapping_conflict")
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.record(mapping(internal_object_reference="payment_intent_002"))
        self.assertEqual(error.exception.reason_code, "external_mapping_conflict")
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.record(
                mapping(
                    provider_code="manual_enterprise",
                    provider_object_type="orders",
                    provider_object_reference="order_002",
                    valid_from=NOW + timedelta(days=1),
                )
            )
        self.assertEqual(error.exception.reason_code, "internal_mapping_conflict")
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.resolve_internal("provider_account_001", "payment_intent", "missing")
        self.assertEqual(error.exception.reason_code, "mapping_not_found")
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.resolve_external("provider_account_001", "orders", "missing")
        self.assertEqual(error.exception.reason_code, "mapping_not_found")
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.record("not-a-mapping")
        self.assertEqual(error.exception.reason_code, "mapping_invalid")
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.resolve_internal("provider_account_001", "payment_intent", "payment_intent_001", at=datetime(2026, 8, 29, 12))
        self.assertEqual(error.exception.reason_code, "routing_time_invalid")
        self.assertEqual(registry.all_mappings(), (first,))

    def test_explicit_replacement_closes_old_interval(self) -> None:
        """Replacement is explicit and preserves old resolution history."""
        registry = ProviderObjectMappingRegistry()
        first = mapping()
        registry.record(first)
        effective_from = NOW + timedelta(days=1)
        replacement = mapping(
            provider_object_type="orders",
            provider_object_reference="order_001",
            valid_from=effective_from,
        )
        self.assertIs(registry.replace(replacement, effective_from=effective_from), replacement)
        old_mapping = registry.resolve_internal(
            "provider_account_001", "payment_intent", "payment_intent_001", at=NOW
        )
        self.assertEqual(old_mapping.provider_object_reference, first.provider_object_reference)
        self.assertEqual(old_mapping.valid_to, effective_from)
        self.assertEqual(registry.resolve_internal("provider_account_001", "payment_intent", "payment_intent_001", at=effective_from), replacement)
        old_external = registry.resolve_external(
            "provider_account_001", "subscriptions", "subscription_001", at=NOW
        )
        self.assertEqual(old_external.provider_object_reference, first.provider_object_reference)
        self.assertEqual(old_external.valid_to, effective_from)
        self.assertEqual(tuple(item.valid_to for item in registry.all_mappings()), (effective_from, None))

    def test_replacement_rejects_unsafe_requests_and_rolls_back(self) -> None:
        """Failed replacements leave the prior open mapping unchanged."""
        registry = ProviderObjectMappingRegistry()
        first = mapping()
        registry.record(first)
        later = NOW + timedelta(days=1)
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.replace("not-a-mapping", effective_from=later)
        self.assertEqual(error.exception.reason_code, "mapping_invalid")
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.replace(mapping(valid_from=NOW), effective_from=NOW)
        self.assertEqual(error.exception.reason_code, "replacement_target_missing")
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.replace(mapping(valid_from=NOW + timedelta(days=2)), effective_from=later)
        self.assertEqual(error.exception.reason_code, "replacement_time_mismatch")
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.replace(mapping(provider_code="manual_enterprise", valid_from=later), effective_from=later)
        self.assertEqual(error.exception.reason_code, "replacement_provider_mismatch")
        other = mapping(
            internal_object_reference="payment_intent_002",
            provider_object_reference="order_001",
            provider_object_type="orders",
        )
        registry.record(other)
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.replace(
                mapping(provider_object_type="orders", provider_object_reference="order_001", valid_from=later),
                effective_from=later,
            )
        self.assertEqual(error.exception.reason_code, "external_mapping_conflict")
        self.assertIs(
            registry.resolve_internal(
                "provider_account_001",
                "payment_intent",
                "payment_intent_001",
                at=NOW,
            ),
            first,
        )

    def test_replacement_rejects_retroactive_overlap(self) -> None:
        """A scheduled replacement cannot be shadowed by an earlier one."""
        registry = ProviderObjectMappingRegistry()
        first = mapping()
        registry.record(first)
        scheduled_at = NOW + timedelta(days=2)
        scheduled = mapping(
            provider_object_type="orders",
            provider_object_reference="order_001",
            valid_from=scheduled_at,
        )
        registry.replace(scheduled, effective_from=scheduled_at)
        retroactive_at = NOW + timedelta(days=1)
        with self.assertRaises(ProviderObjectMappingError) as error:
            registry.replace(
                mapping(
                    provider_object_type="invoices",
                    provider_object_reference="invoice_001",
                    valid_from=retroactive_at,
                ),
                effective_from=retroactive_at,
            )
        self.assertEqual(error.exception.reason_code, "internal_mapping_conflict")
        resolved = registry.resolve_internal(
            "provider_account_001",
            "payment_intent",
            "payment_intent_001",
            at=retroactive_at,
        )
        self.assertEqual(resolved.provider_object_reference, first.provider_object_reference)
        self.assertEqual(resolved.valid_to, scheduled_at)

    def test_contract_rejects_runtime_only_mapping_invariants(self) -> None:
        """Published validation stays aligned with runtime reference rules."""
        body = mapping().as_contract_dict()
        for field in (
            "provider_account_reference",
            "internal_object_reference",
            "provider_object_reference",
        ):
            with self.subTest(field=field):
                self.assertEqual(
                    validate_provider_object_mapping(dict(body, **{field: "bad\nref"})),
                    (f"$: {field} must not contain control characters",),
                )
        self.assertEqual(
            validate_provider_object_mapping(
                dict(
                    body,
                    valid_from="2026-08-29T12:00:00Z",
                    valid_to="2026-08-29T12:00:00Z",
                )
            ),
            ("$: valid_to must be after valid_from",),
        )
        self.assertEqual(
            validate_provider_object_mapping(
                dict(
                    body,
                    valid_from="2026-08-29T12:00:00Z",
                    valid_to="2026-08-29T11:00:00Z",
                )
            ),
            ("$: valid_to must be after valid_from",),
        )


if __name__ == "__main__":
    unittest.main()
