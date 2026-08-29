"""Tests for provider routing and verified Lemon Squeezy webhook references."""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from datetime import UTC, datetime, timedelta

from metering_billing import (
    LEMON_SQUEEZY_MANIFEST,
    MANUAL_ENTERPRISE_MANIFEST,
    LemonSqueezyWebhookError,
    ProviderCapabilityError,
    ProviderCapabilityManifest,
    ProviderCapabilityRegistry,
    ProviderRouteRequest,
    ProviderRoutingError,
    validate_lemon_squeezy_webhook,
    validate_provider_capability,
    verify_lemon_squeezy_webhook,
)


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def manifest(**overrides: object) -> ProviderCapabilityManifest:
    """Build one valid manifest with every routing dimension available."""
    values: dict[str, object] = {
        "provider_code": "acme_payments",
        "provider_roles": ("payment_processor",),
        "capabilities": ("hosted_checkout", "settlement_export"),
        "effective_from": NOW - timedelta(days=1),
        "supported_currency_codes": ("USD",),
        "jurisdiction_codes": ("US", "KR-11"),
        "contract_type_codes": ("purchase_order",),
        "tenant_policy_codes": ("standard",),
    }
    values.update(overrides)
    return ProviderCapabilityManifest(**values)


def signed(body: bytes, secret: str = "test-secret") -> str:
    """Return a Lemon Squeezy-compatible raw-body signature."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class ProviderCapabilityTests(unittest.TestCase):
    """Verify manifest validation, deterministic routing, and health gates."""

    def test_manifest_contract_and_route_dimensions(self) -> None:
        """A manifest normalizes instants and routes only matching requests."""
        stored = manifest(
            effective_from=datetime(2026, 8, 29, 8, tzinfo=UTC),
            effective_to=NOW + timedelta(days=1),
        )
        body = stored.as_contract_dict()
        self.assertEqual(validate_provider_capability(body), ())
        self.assertEqual(body["effective_from"], "2026-08-29T08:00:00Z")
        self.assertEqual(body["supported_currency_codes"], ["USD"])
        self.assertTrue(stored.supports("hosted_checkout", at=NOW, provider_role_code="payment_processor"))
        self.assertFalse(stored.supports("missing", at=NOW, provider_role_code="payment_processor"))
        self.assertFalse(stored.supports([], at=NOW))
        self.assertFalse(stored.supports("hosted_checkout", at=NOW, provider_role_code=[]))
        self.assertFalse(stored.supports("hosted_checkout", at=NOW, provider_role_code="manual_collection"))
        self.assertFalse(stored.supports("hosted_checkout", at=NOW - timedelta(days=2)))
        self.assertFalse(stored.supports("hosted_checkout", at=NOW + timedelta(days=2)))
        request = ProviderRouteRequest(
            ("hosted_checkout",),
            provider_role_code="payment_processor",
            currency_code="USD",
            jurisdiction_code="US",
            contract_type_code="purchase_order",
            tenant_policy_code="standard",
            at=NOW,
        )
        self.assertTrue(stored.matches(request))
        for field, value in (
            ("currency_code", "EUR"),
            ("jurisdiction_code", "DE"),
            ("contract_type_code", "annual_commitment"),
            ("tenant_policy_code", "restricted"),
        ):
            with self.subTest(field=field):
                self.assertFalse(stored.matches(ProviderRouteRequest(("hosted_checkout",), at=NOW, **{field: value})))
        self.assertEqual(
            ProviderCapabilityManifest(
                "short_name",
                ("payment_processor",),
                ("hosted_checkout",),
                NOW,
            ).as_contract_dict(),
            {
                "provider_code": "short_name",
                "provider_roles": ["payment_processor"],
                "capabilities": ["hosted_checkout"],
                "effective_from": "2026-08-29T12:00:00Z",
            },
        )

    def test_manifest_and_route_validation_fail_closed(self) -> None:
        """Malformed trust-boundary values return stable reason codes."""
        cases = (
            ({"provider_code": "A"}, "provider_code_invalid"),
            ({"provider_roles": ()}, "provider_role_invalid"),
            ({"provider_roles": ("payment_processor", "payment_processor")}, "provider_role_invalid"),
            ({"provider_roles": ("unknown_role",)}, "provider_role_invalid"),
            ({"provider_roles": None}, "provider_role_invalid"),
            ({"provider_roles": (["payment_processor"],)}, "provider_role_invalid"),
            ({"provider_roles": (1,)}, "provider_role_invalid"),
            ({"capabilities": ()}, "capability_invalid"),
            ({"capabilities": ("unknown",)}, "capability_invalid"),
            ({"effective_from": datetime(2026, 8, 29, 12)}, "effective_from_invalid"),
            ({"effective_to": datetime(2026, 8, 29, 12)}, "effective_to_invalid"),
            ({"effective_to": NOW - timedelta(days=1)}, "effective_interval_invalid"),
            ({"supported_currency_codes": ("USD", "USD")}, "currency_code_invalid"),
            ({"supported_currency_codes": ("usd",)}, "currency_code_invalid"),
            ({"supported_currency_codes": None}, "currency_code_invalid"),
            ({"supported_currency_codes": (["USD"],)}, "currency_code_invalid"),
            ({"supported_currency_codes": (1,)}, "currency_code_invalid"),
            ({"jurisdiction_codes": ("USA",)}, "jurisdiction_code_invalid"),
            ({"contract_type_codes": ("PO",)}, "contract_type_code_invalid"),
            ({"tenant_policy_codes": ("Policy",)}, "tenant_policy_code_invalid"),
        )
        for overrides, reason_code in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ProviderCapabilityError) as error:
                    manifest(**overrides)
                self.assertEqual(error.exception.reason_code, reason_code)

        request_cases = (
            ((), "required_capability_invalid"),
            (("unknown",), "required_capability_invalid"),
            (("hosted_checkout", "hosted_checkout"), "required_capability_invalid"),
            ((["hosted_checkout"],), "required_capability_invalid"),
        )
        for capabilities, reason_code in request_cases:
            with self.assertRaises(ProviderCapabilityError) as error:
                ProviderRouteRequest(capabilities, at=NOW)
            self.assertEqual(error.exception.reason_code, reason_code)
        for field, value, reason_code in (
            ("provider_role_code", "unknown", "provider_role_invalid"),
            ("provider_role_code", ["payment_processor"], "provider_role_invalid"),
            ("currency_code", "usd", "currency_code_invalid"),
            ("jurisdiction_code", "USA", "jurisdiction_code_invalid"),
            ("contract_type_code", "PO", "contract_type_code_invalid"),
            ("tenant_policy_code", "Policy", "tenant_policy_code_invalid"),
            ("at", datetime(2026, 8, 29, 12), "routing_time_invalid"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ProviderCapabilityError) as error:
                    values = {field: value}
                    if field != "at":
                        values["at"] = NOW
                    ProviderRouteRequest(("hosted_checkout",), **values)
                self.assertEqual(error.exception.reason_code, reason_code)

    def test_registry_selects_healthy_provider_and_rejects_conflicts(self) -> None:
        """Routing honors health, effective intervals, and stable tie breaking."""
        registry = ProviderCapabilityRegistry()
        registry.register(manifest(provider_code="zeta_payments"), health_check=lambda: True)
        registry.register(
            manifest(
                provider_code="acme_payments",
                effective_from=NOW + timedelta(days=1),
                effective_to=NOW + timedelta(days=2),
            ),
            health_check=lambda: True,
        )
        registry.register(LEMON_SQUEEZY_MANIFEST)
        registry.register(MANUAL_ENTERPRISE_MANIFEST)
        request = ProviderRouteRequest(("settlement_export",), at=NOW)
        self.assertEqual(registry.select(request).provider_code, "lemon_squeezy")
        self.assertEqual(
            tuple(item.provider_code for item in registry.active_manifests(NOW)),
            ("lemon_squeezy", "manual_enterprise", "zeta_payments"),
        )
        with self.assertRaises(ProviderCapabilityError) as error:
            registry.register(manifest(provider_code="zeta_payments"))
        self.assertEqual(error.exception.reason_code, "manifest_interval_conflict")
        with self.assertRaises(ProviderCapabilityError) as error:
            registry.register(manifest(), health_check="not-callable")
        self.assertEqual(error.exception.reason_code, "health_check_invalid")
        with self.assertRaises(ProviderCapabilityError) as error:
            registry.register("not-a-manifest")
        self.assertEqual(error.exception.reason_code, "manifest_invalid")
        with self.assertRaises(ProviderRoutingError) as error:
            registry.select("not-a-request")
        self.assertEqual(error.exception.reason_code, "route_request_invalid")

        unhealthy = ProviderCapabilityRegistry()
        unhealthy.register(manifest(provider_code="unhealthy"), health_check=lambda: False)
        unhealthy.register(
            manifest(provider_code="broken"),
            health_check=lambda: (_ for _ in ()).throw(RuntimeError("health unavailable")),
        )
        with self.assertRaises(ProviderRoutingError) as error:
            unhealthy.select(ProviderRouteRequest(("hosted_checkout",), at=NOW))
        self.assertEqual(error.exception.reason_code, "no_provider_available")
        with self.assertRaises(ProviderCapabilityError) as error:
            unhealthy.active_manifests(datetime(2026, 8, 29, 12))
        self.assertEqual(error.exception.reason_code, "routing_time_invalid")


class LemonSqueezyWebhookTests(unittest.TestCase):
    """Verify raw-body authentication and PII-free normalization."""

    def test_verified_root_and_json_api_payloads(self) -> None:
        """Valid signatures permit both documented resource envelope forms."""
        root = json.dumps(
            {
                "meta": {"event_name": "order_created"},
                "type": "orders",
                "id": "order-1",
                "attributes": {"user_email": "do-not-return@example.test"},
            },
            separators=(",", ":"),
        ).encode()
        event = verify_lemon_squeezy_webhook(root, " " + signed(root).upper() + " ", "test-secret")
        self.assertEqual(event.event_name, "order_created")
        self.assertEqual(event.provider_object_type, "orders")
        self.assertEqual(event.provider_object_reference, "order-1")
        contract = event.as_contract_dict()
        self.assertEqual(validate_lemon_squeezy_webhook(contract), ())
        self.assertNotIn("attributes", contract)
        nested = json.dumps(
            {"meta": {"event_name": "subscription_payment_success"}, "data": {"type": "subscriptions", "id": "sub-1"}},
            separators=(",", ":"),
        ).encode()
        self.assertEqual(
            verify_lemon_squeezy_webhook(nested, signed(nested), "test-secret").provider_object_reference,
            "sub-1",
        )

    def test_webhook_inputs_fail_closed_without_parsing_untrusted_body(self) -> None:
        """Invalid signatures, payloads, and references expose only reason codes."""
        valid = b'{"meta":{"event_name":"order_created"},"type":"orders","id":"1"}'
        cases = (
            (None, signed(valid), "test-secret", "body_invalid"),
            (b"", signed(valid), "test-secret", "body_invalid"),
            (b"x" * (1_048_576 + 1), "", "test-secret", "body_too_large"),
            (valid, signed(valid), "", "secret_invalid"),
            (valid, None, "test-secret", "signature_invalid"),
            (valid, "short", "test-secret", "signature_invalid"),
            (valid, "g" * 64, "test-secret", "signature_invalid"),
            (valid, "0" * 64, "test-secret", "signature_invalid"),
            (b"not utf8: \xff", "0" * 64, "test-secret", "signature_invalid"),
        )
        for body, signature, secret, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(LemonSqueezyWebhookError) as error:
                    verify_lemon_squeezy_webhook(body, signature, secret)
                self.assertEqual(error.exception.reason_code, reason_code)

        signed_cases = (
            (b"not utf8: \xff", "payload_invalid"),
            (b"[]", "payload_invalid"),
            (b"{}", "event_name_invalid"),
            (b'{"meta":null}', "event_name_invalid"),
            (b'{"meta":{"event_name":""},"type":"orders","id":"1"}', "event_name_invalid"),
            (b'{"meta":{"event_name":"ok"},"data":null}', "resource_invalid"),
            (b'{"meta":{"event_name":"ok"},"type":"","id":"1"}', "resource_type_invalid"),
            (b'{"meta":{"event_name":"ok"},"type":"orders","id":""}', "resource_reference_invalid"),
        )
        for body, reason_code in signed_cases:
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(LemonSqueezyWebhookError) as error:
                    verify_lemon_squeezy_webhook(body, signed(body), "test-secret")
                self.assertEqual(error.exception.reason_code, reason_code)
