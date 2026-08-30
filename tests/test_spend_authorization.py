"""Tests for exact, idempotent spend authorization lifecycle commands."""

from __future__ import annotations

import unittest
import unittest.mock as mock
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from metering_billing import (
    SpendAuthorizationQueryError,
    SpendAuthorizationResult,
    SpendAuthorizationService,
    SpendAuthorizationOutcomeCode,
    validate_spend_authorization,
    validate_spend_authorization_presentment,
)
from test_http_app import invoke_http
from test_spend_budget import TENANT_ONE, TENANT_TWO
from test_spend_budget import publish_known_budget
from metering_billing.usage_ledger import (
    StoredSpendCommitment,
    StoredSpendRelease,
    StoredSpendReservation,
)
from test_usage_ingestion import ACCOUNT_TWO


NOW = datetime(2026, 8, 16, 10, 15, tzinfo=UTC)
DEADLINE = datetime(2026, 8, 16, 10, 30, tzinfo=UTC)


class SpendAuthorizationTests(unittest.TestCase):
    """Verify conservation, replay safety, tenant isolation, and API mapping."""

    def test_request_commit_release_replay_and_conservation(self) -> None:
        """A stream can commit actual use and release only its unused remainder."""
        ledger, _, budget = publish_known_budget()
        service = SpendAuthorizationService(ledger, clock=lambda: NOW)
        first = service.request_authorization(
            TENANT_ONE,
            budget.spend_budget_id,
            Decimal("70.00"),
            "request-1",
            "worker-1",
            "inference",
            "policy-v1",
            DEADLINE,
        )
        replay = SpendAuthorizationService(ledger, clock=lambda: NOW + timedelta(seconds=1)).request_authorization(
            TENANT_ONE,
            budget.spend_budget_id,
            "70.00",
            "request-1",
            "worker-1",
            "inference",
            "policy-v1",
            DEADLINE,
        )
        self.assertEqual(first.outcome_code, SpendAuthorizationOutcomeCode.ACCEPTED)
        self.assertEqual(replay.outcome_code, SpendAuthorizationOutcomeCode.DUPLICATE_REPLAY)
        self.assertIsNotNone(first.authorization)
        authorization_id = first.authorization.spend_authorization_id
        self.assertEqual(validate_spend_authorization(first.as_contract_dict()), ())
        replay_service = SpendAuthorizationService(
            ledger, clock=lambda: NOW + timedelta(seconds=1)
        )

        committed = service.commit_authorization(
            TENANT_ONE, authorization_id, "20.00", "commit-1", "usage-1"
        )
        committed_replay = replay_service.commit_authorization(
            TENANT_ONE, authorization_id, "20.00", "commit-1", "usage-1"
        )
        self.assertEqual(committed.authorization.committed_amount, Decimal("20.00"))
        self.assertEqual(committed.authorization.authorization_status, "partially_committed")
        self.assertEqual(
            committed_replay.outcome_code, SpendAuthorizationOutcomeCode.DUPLICATE_REPLAY
        )
        self.assertEqual(committed_replay.mutation_id, committed.mutation_id)
        self.assertEqual(len(ledger.spend_commitments), 1)

        released = service.release_authorization(
            TENANT_ONE, authorization_id, "50.00", "release-1", "cancelled"
        )
        released_replay = replay_service.release_authorization(
            TENANT_ONE, authorization_id, "50.00", "release-1", "cancelled"
        )
        self.assertEqual(
            released_replay.outcome_code, SpendAuthorizationOutcomeCode.DUPLICATE_REPLAY
        )
        self.assertEqual(released_replay.mutation_id, released.mutation_id)
        self.assertEqual(released.authorization.authorization_status, "released")
        self.assertEqual(released.authorization.committed_amount, Decimal("20.00"))
        self.assertEqual(released.authorization.released_amount, Decimal("50.00"))
        presented = service.present_authorization(TENANT_ONE, authorization_id)
        self.assertEqual(validate_spend_authorization_presentment(presented), ())
        self.assertEqual(presented["remaining_amount"], "0.00")
        self.assertEqual(len(ledger.spend_authorization_transitions), 3)

    def test_denial_validation_expiry_and_tenant_isolation(self) -> None:
        """Denials are replayable, invalid writes are non-mutating, and expiry releases remainder."""
        ledger, _, budget = publish_known_budget()
        service = SpendAuthorizationService(ledger, clock=lambda: NOW)
        accepted = service.request_authorization(
            TENANT_ONE, budget.spend_budget_id, "80.00", "request-1", "worker", "batch", "v1", DEADLINE
        )
        denied = service.request_authorization(
            TENANT_ONE, budget.spend_budget_id, "20.01", "request-2", "worker", "batch", "v1", DEADLINE
        )
        self.assertEqual(denied.outcome_code, SpendAuthorizationOutcomeCode.REJECTED)
        self.assertEqual(denied.rejection_reason_code, "authorization_exposure_exceeded")
        denied_replay = service.request_authorization(
            TENANT_ONE, budget.spend_budget_id, "20.01", "request-2", "worker", "batch", "v1", DEADLINE
        )
        self.assertEqual(denied_replay.rejection_reason_code, "authorization_exposure_exceeded")
        self.assertEqual(len(ledger.spend_reservations), 1)
        self.assertEqual(validate_spend_authorization(denied.as_contract_dict()), ())

        invalids = (
            (100.0, "requested_amount_invalid"),
            ("0", "requested_amount_invalid"),
            ("1.00", "idempotency_key_invalid"),
        )
        for amount, reason in invalids:
            result = service.request_authorization(
                TENANT_ONE,
                budget.spend_budget_id,
                amount,
                "" if reason == "idempotency_key_invalid" else "invalid-" + reason,
                "worker",
                "batch",
                "v1",
                DEADLINE,
            )
            self.assertEqual(result.rejection_reason_code, reason)
        self.assertEqual(service.request_authorization("missing", budget.spend_budget_id, "1", "x", "a", "p", "v1", DEADLINE).rejection_reason_code, "tenant_not_found")
        self.assertEqual(
            service.request_authorization(
                TENANT_ONE, uuid4(), "1", "unknown", "worker", "batch", "v1", DEADLINE
            ).rejection_reason_code,
            "spend_budget_not_found",
        )
        ledger.register_tenant(TENANT_TWO)
        with self.assertRaises(SpendAuthorizationQueryError) as cross_tenant:
            service.present_authorization(TENANT_TWO, accepted.authorization.spend_authorization_id)
        self.assertEqual(cross_tenant.exception.rejection_reason_code, "spend_authorization_not_found")

        clock = [NOW]
        expiring = SpendAuthorizationService(ledger, clock=lambda: clock[0]).request_authorization(
            TENANT_ONE,
            budget.spend_budget_id,
            "10.00",
            "request-expire",
            "worker",
            "batch",
            "v1",
            DEADLINE,
        )
        clock[0] = DEADLINE + timedelta(seconds=1)
        expired = expiring.authorization
        self.assertEqual(
            ledger.apply_spend_commitment(
                expired.tenant_account_id,
                StoredSpendCommitment(
                    uuid4(), expired.tenant_account_id, expired.spend_authorization_id,
                    "late-direct", Decimal("1"), "usage-late", clock[0]
                ),
                clock[0],
            )[1],
            "authorization_expired",
        )
        expired_result = SpendAuthorizationService(ledger, clock=lambda: clock[0]).expire_authorization(
            TENANT_ONE, expired.spend_authorization_id, "expire-1"
        )
        self.assertEqual(expired_result.authorization.authorization_status, "expired")
        self.assertEqual(expired_result.authorization.released_amount, Decimal("10.00"))
        expired_replay = SpendAuthorizationService(ledger, clock=lambda: clock[0]).expire_authorization(
            TENANT_ONE, expired.spend_authorization_id, "expire-1"
        )
        self.assertEqual(expired_replay.outcome_code, SpendAuthorizationOutcomeCode.DUPLICATE_REPLAY)
        self.assertEqual(
            SpendAuthorizationService(ledger, clock=lambda: clock[0]).commit_authorization(
                TENANT_ONE, expired.spend_authorization_id, "1", "late-1", "usage-late"
            ).rejection_reason_code,
            "authorization_expired",
        )

    def test_edge_paths_keep_validation_and_ledger_state_fail_closed(self) -> None:
        """Every rejected input is non-mutating and low-level writes remain idempotent."""
        ledger, _, budget = publish_known_budget()
        service = SpendAuthorizationService(ledger, clock=lambda: NOW)
        valid = dict(
            budget_id=budget.spend_budget_id,
            amount="1.00",
            key="edge-request",
            actor="worker",
            purpose="batch",
            policy="v1",
            deadline=DEADLINE,
        )

        def request(**overrides):
            values = {**valid, **overrides}
            return service.request_authorization(
                TENANT_ONE,
                values["budget_id"],
                values["amount"],
                values["key"],
                values["actor"],
                values["purpose"],
                values["policy"],
                values["deadline"],
            )

        for overrides, reason in (
            ({"amount": "1e2"}, "requested_amount_invalid"),
            ({"amount": "0"}, "requested_amount_invalid"),
            ({"actor": ""}, "actor_reference_invalid"),
            ({"purpose": ""}, "purpose_invalid"),
            ({"policy": ""}, "policy_version_invalid"),
            ({"deadline": datetime(2026, 8, 16, 10, 30)}, "validity_window_invalid"),
            ({"deadline": datetime(2026, 8, 16, 10, tzinfo=UTC)}, "validity_window_invalid"),
            ({"deadline": datetime(2026, 8, 16, 11, 1, tzinfo=UTC)}, "validity_window_invalid"),
        ):
            result = request(**overrides, key="invalid-" + reason)
            self.assertEqual(result.rejection_reason_code, reason)

        outside = SpendAuthorizationService(
            ledger, clock=lambda: datetime(2026, 8, 16, 11, tzinfo=UTC)
        ).request_authorization(
            TENANT_ONE,
            budget.spend_budget_id,
            "1",
            "outside-window",
            "worker",
            "batch",
            "v1",
            datetime(2026, 8, 16, 11, 30, tzinfo=UTC),
        )
        self.assertEqual(outside.rejection_reason_code, "validity_window_invalid")

        tenant_two = ledger.register_tenant(TENANT_TWO)
        account_two = ledger.register_billing_account(TENANT_TWO, ACCOUNT_TWO)
        original_budget = ledger.spend_budgets[budget.spend_budget_id]
        ledger.spend_budgets[budget.spend_budget_id] = original_budget.__class__(
            **{**original_budget.__dict__, "tenant_account_id": tenant_two.tenant_account_id}
        )
        self.assertEqual(request(key="foreign-budget").rejection_reason_code, "spend_budget_not_found")
        ledger.spend_budgets[budget.spend_budget_id] = original_budget
        ledger.spend_budgets[budget.spend_budget_id] = original_budget.__class__(
            **{**original_budget.__dict__, "billing_account_id": account_two.billing_account_id}
        )
        self.assertEqual(request(key="foreign-account").rejection_reason_code, "billing_account_forbidden")
        ledger.spend_budgets[budget.spend_budget_id] = original_budget
        account_one = ledger.get_billing_account(original_budget.billing_account_id)
        self.assertIsNotNone(account_one)
        del ledger.billing_accounts[account_one.billing_account_reference]
        self.assertEqual(request(key="missing-account").rejection_reason_code, "billing_account_not_found")
        ledger.billing_accounts[account_one.billing_account_reference] = account_one

        accepted = request(key="direct-request")
        self.assertEqual(accepted.spend_authorization_outcome_code, SpendAuthorizationOutcomeCode.ACCEPTED)
        authorization = accepted.authorization
        assert authorization is not None
        tenant_id = authorization.tenant_account_id
        reservation = StoredSpendReservation(
            uuid4(), tenant_id, authorization.spend_authorization_id, authorization.requested_amount,
            authorization.idempotency_key, NOW, DEADLINE
        )
        self.assertEqual(ledger.create_spend_authorization(authorization, reservation)[1], "duplicate_replay")
        with self.assertRaises(ValueError):
            ledger.create_spend_authorization(
                authorization.__class__(**{**authorization.__dict__, "actor_reference": "other"}), reservation
            )
        with self.assertRaises(KeyError):
            ledger.create_spend_authorization(
                authorization.__class__(**{**authorization.__dict__, "spend_budget_id": uuid4(), "idempotency_key": "missing-budget"}),
                reservation,
            )

        zero_commit = StoredSpendCommitment(
            uuid4(), tenant_id, authorization.spend_authorization_id, "zero-commit", Decimal("0"), "usage", NOW
        )
        accepted_commit = ledger.apply_spend_commitment(tenant_id, zero_commit, NOW)
        replayed_commit = ledger.apply_spend_commitment(tenant_id, zero_commit, NOW)
        self.assertEqual(accepted_commit[1], "accepted")
        self.assertEqual(replayed_commit[1], "duplicate_replay")
        self.assertEqual(replayed_commit[2], accepted_commit[2])
        with self.assertRaises(ValueError):
            ledger.apply_spend_commitment(
                tenant_id,
                zero_commit.__class__(**{**zero_commit.__dict__, "committed_amount": Decimal("0.1")}),
                NOW,
            )
        self.assertEqual(
            ledger.apply_spend_commitment(
                tenant_id,
                zero_commit.__class__(**{**zero_commit.__dict__, "idempotency_key": "too-much", "committed_amount": Decimal("2")}),
                NOW,
            )[1],
            "commitment_amount_exceeded",
        )
        with self.assertRaises(KeyError):
            ledger.apply_spend_commitment(
                tenant_id,
                zero_commit.__class__(**{**zero_commit.__dict__, "spend_authorization_id": uuid4()}),
                NOW,
            )

        denied = service.request_authorization(
            TENANT_ONE, budget.spend_budget_id, "100", "denied-direct", "worker", "batch", "v1", DEADLINE
        )
        self.assertEqual(
            ledger.apply_spend_commitment(
                tenant_id,
                zero_commit.__class__(**{**zero_commit.__dict__, "spend_authorization_id": denied.authorization.spend_authorization_id, "idempotency_key": "denied-commit"}),
                NOW,
            )[1],
            "authorization_status_invalid",
        )

        zero_release = StoredSpendRelease(
            uuid4(), tenant_id, authorization.spend_authorization_id, "zero-release", Decimal("0"), "cancelled", NOW
        )
        accepted_release = ledger.apply_spend_release(tenant_id, zero_release, NOW)
        replayed_release = ledger.apply_spend_release(tenant_id, zero_release, NOW)
        self.assertEqual(accepted_release[1], "accepted")
        self.assertEqual(replayed_release[1], "duplicate_replay")
        self.assertEqual(replayed_release[2], accepted_release[2])
        with self.assertRaises(ValueError):
            ledger.apply_spend_release(
                tenant_id,
                zero_release.__class__(**{**zero_release.__dict__, "released_amount": Decimal("0.1")}),
                NOW,
            )
        self.assertEqual(
            ledger.apply_spend_release(
                tenant_id,
                zero_release.__class__(**{**zero_release.__dict__, "idempotency_key": "too-much-release", "released_amount": Decimal("2")}),
                NOW,
            )[1],
            "release_amount_exceeded",
        )
        with self.assertRaises(KeyError):
            ledger.apply_spend_release(
                tenant_id,
                zero_release.__class__(**{**zero_release.__dict__, "spend_authorization_id": uuid4()}),
                NOW,
            )

        for operation in (
            lambda: service.release_authorization(TENANT_ONE, authorization.spend_authorization_id, "1e2", "bad-release-amount", "cancelled"),
            lambda: service.release_authorization(TENANT_ONE, authorization.spend_authorization_id, "0", "bad-release-zero", "cancelled"),
            lambda: service.release_authorization(TENANT_ONE, authorization.spend_authorization_id, "1", "", "cancelled"),
            lambda: service.release_authorization(TENANT_ONE, authorization.spend_authorization_id, "1", "bad-release-reason", ""),
            lambda: service.commit_authorization(TENANT_ONE, authorization.spend_authorization_id, "1e2", "bad-commit-amount", "usage"),
            lambda: service.commit_authorization(TENANT_ONE, authorization.spend_authorization_id, "0", "bad-commit-zero", "usage"),
            lambda: service.commit_authorization(TENANT_ONE, authorization.spend_authorization_id, "1", "", "usage"),
            lambda: service.commit_authorization(TENANT_ONE, authorization.spend_authorization_id, "1", "bad-commit-ref", ""),
        ):
            self.assertEqual(operation().outcome_code, SpendAuthorizationOutcomeCode.REJECTED)
        self.assertEqual(service.release_authorization("missing", authorization.spend_authorization_id, "1", "missing-tenant-release", "x").rejection_reason_code, "tenant_not_found")
        self.assertEqual(service.commit_authorization("missing", authorization.spend_authorization_id, "1", "missing-tenant-commit", "x").rejection_reason_code, "tenant_not_found")
        self.assertEqual(service.expire_authorization("missing", authorization.spend_authorization_id, "missing-tenant-expire").rejection_reason_code, "tenant_not_found")
        self.assertEqual(service.release_authorization(TENANT_ONE, uuid4(), "1", "missing-release", "x").rejection_reason_code, "spend_authorization_not_found")
        self.assertEqual(service.commit_authorization(TENANT_ONE, uuid4(), "1", "missing-commit", "x").rejection_reason_code, "spend_authorization_not_found")
        self.assertEqual(service.expire_authorization(TENANT_ONE, uuid4(), "missing-expire").rejection_reason_code, "spend_authorization_not_found")
        self.assertEqual(service.expire_authorization(TENANT_ONE, authorization.spend_authorization_id, "early-expire").rejection_reason_code, "validity_window_invalid")
        self.assertEqual(service.expire_authorization(TENANT_ONE, authorization.spend_authorization_id, "").rejection_reason_code, "idempotency_key_invalid")

        with mock.patch.object(ledger, "create_spend_authorization", side_effect=KeyError):
            self.assertEqual(request(key="forced-missing-budget").rejection_reason_code, "spend_budget_not_found")
        with mock.patch.object(ledger, "create_spend_authorization", side_effect=ValueError):
            self.assertEqual(request(key="forced-key-conflict").rejection_reason_code, "idempotency_key_conflict")
        self.assertEqual(
            service.release_authorization(
                TENANT_ONE, denied.authorization.spend_authorization_id, "1", "denied-release", "cancelled"
            ).rejection_reason_code,
            "authorization_status_invalid",
        )
        service.commit_authorization(TENANT_ONE, authorization.spend_authorization_id, "0.5", "service-commit", "usage")
        self.assertEqual(
            service.commit_authorization(TENANT_ONE, authorization.spend_authorization_id, "0.6", "service-commit", "usage").rejection_reason_code,
            "idempotency_key_conflict",
        )
        service.release_authorization(TENANT_ONE, authorization.spend_authorization_id, "0.1", "service-release", "cancelled")
        self.assertEqual(
            service.release_authorization(TENANT_ONE, authorization.spend_authorization_id, "0.2", "service-release", "cancelled").rejection_reason_code,
            "idempotency_key_conflict",
        )
        self.assertEqual(
            service.release_authorization(TENANT_ONE, authorization.spend_authorization_id, "1", "release-too-much", "cancelled").rejection_reason_code,
            "release_amount_exceeded",
        )
        full = request(key="full-request")
        assert full.authorization is not None
        self.assertEqual(
            service.commit_authorization(TENANT_ONE, full.authorization.spend_authorization_id, "1", "full-commit", "usage").authorization.authorization_status,
            "committed",
        )
        self.assertEqual(
            SpendAuthorizationService(ledger, clock=lambda: DEADLINE + timedelta(seconds=1)).expire_authorization(
                TENANT_ONE, full.authorization.spend_authorization_id, "full-expire"
            ).rejection_reason_code,
            "authorization_status_invalid",
        )
        with self.assertRaises(SpendAuthorizationQueryError):
            service.present_authorization("missing", authorization.spend_authorization_id)

        empty = SpendAuthorizationResult("request", SpendAuthorizationOutcomeCode.REJECTED, None, None, None, None)
        self.assertEqual(empty.spend_authorization_outcome_code, SpendAuthorizationOutcomeCode.REJECTED)
        self.assertEqual(empty.as_contract_dict()["next_operator_action"], "correct_request")
        self.assertTrue(validate_spend_authorization(None))
        self.assertTrue(validate_spend_authorization_presentment(None))
        valid_body = accepted.as_contract_dict()
        missing_field = dict(valid_body)
        missing_field.pop("remaining_amount")
        self.assertTrue(validate_spend_authorization(missing_field))
        rejected_body = empty.as_contract_dict()
        rejected_body.pop("rejection_reason_code", None)
        self.assertTrue(validate_spend_authorization(rejected_body))
        invalid_decimal = dict(valid_body, remaining_amount="not-a-decimal")
        self.assertTrue(validate_spend_authorization(invalid_decimal))
        self.assertTrue(validate_spend_authorization(dict(valid_body, remaining_amount="0.01")))

        with self.assertRaises(ValueError):
            SpendAuthorizationService(ledger, clock=lambda: "not-a-datetime").request_authorization(
                TENANT_ONE, budget.spend_budget_id, "1", "bad-clock", "worker", "batch", "v1", DEADLINE
            )

    def test_http_routes_and_fail_closed_metadata(self) -> None:
        """The stdlib adapter exposes request, commitment, release, and read routes."""
        ledger, _, budget = publish_known_budget()
        app = __import__("metering_billing").create_http_app(ledger, clock=lambda: NOW)
        ledger.register_tenant(TENANT_TWO)
        request = {
            "tenant_reference": TENANT_ONE,
            "spend_budget_id": str(budget.spend_budget_id),
            "requested_amount": "30.00",
            "idempotency_key": "http-request",
            "actor_reference": "worker",
            "purpose_code": "inference",
            "policy_version": "policy-v1",
            "valid_until": DEADLINE.isoformat().replace("+00:00", "Z"),
        }
        status, body = invoke_http(app, "POST", "/v1/spend-authorizations", request)
        self.assertEqual(status, 200)
        self.assertEqual(validate_spend_authorization(body), ())
        authorization_id = body["spend_authorization_id"]
        status, body = invoke_http(
            app,
            "POST",
            f"/v1/spend-authorizations/{authorization_id}/commitments",
            {
                "tenant_reference": TENANT_ONE,
                "committed_amount": "10.00",
                "idempotency_key": "http-commit",
                "actual_usage_reference": "usage-http",
            },
        )
        self.assertEqual(status, 200)
        status, body = invoke_http(
            app,
            "POST",
            f"/v1/spend-authorizations/{authorization_id}/releases",
            {
                "tenant_reference": TENANT_ONE,
                "released_amount": "20.00",
                "idempotency_key": "http-release",
                "release_reason_code": "cancelled",
            },
        )
        self.assertEqual(status, 200)
        status, body = invoke_http(
            app,
            "GET",
            f"/v1/spend-authorizations/{authorization_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_spend_authorization_presentment(body), ())
        cross_status, cross_body = invoke_http(
            app,
            "GET",
            f"/v1/spend-authorizations/{authorization_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(cross_status, 404)
        self.assertEqual(cross_body["rejection_reason_code"], "spend_authorization_not_found")
        bad_status, bad_body = invoke_http(
            app,
            "POST",
            "/v1/spend-authorizations",
            {**request, "card_pan": "unsupported-field-value"},
        )
        self.assertEqual(bad_status, 422)
        self.assertEqual(bad_body["rejection_reason_code"], "request_invalid")
        wrong_status, _ = invoke_http(app, "GET", "/v1/spend-authorizations")
        self.assertEqual(wrong_status, 422)
        self.assertEqual(
            invoke_http(app, "GET", f"/v1/spend-authorizations/{authorization_id}/commitments")[0], 422
        )
        self.assertEqual(
            invoke_http(app, "GET", f"/v1/spend-authorizations/{authorization_id}/releases")[0], 422
        )
        self.assertEqual(
            invoke_http(app, "POST", f"/v1/spend-authorizations/{authorization_id}")[0], 422
        )
        no_tenant_status, no_tenant_body = invoke_http(
            app, "GET", f"/v1/spend-authorizations/{authorization_id}"
        )
        self.assertEqual((no_tenant_status, no_tenant_body["rejection_reason_code"]), (422, "tenant_not_found"))
        invalid_id_status, invalid_id_body = invoke_http(
            app,
            "GET",
            "/v1/spend-authorizations/00000000000000000000000000000000000-",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual((invalid_id_status, invalid_id_body["rejection_reason_code"]), (422, "request_invalid"))
        for deadline in (None, "invalid", "2026-08-16T10:30:00"):
            invalid_request = {**request, "idempotency_key": "bad-deadline-" + str(deadline), "valid_until": deadline}
            status, body = invoke_http(app, "POST", "/v1/spend-authorizations", invalid_request)
            self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))
        status, body = invoke_http(
            app,
            "POST",
            "/v1/spend-authorizations",
            {**request, "idempotency_key": "unknown-budget", "spend_budget_id": str(uuid4())},
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["rejection_reason_code"], "spend_budget_not_found")
        status, body = invoke_http(
            app,
            "POST",
            f"/v1/spend-authorizations/{uuid4()}/commitments",
            {"tenant_reference": TENANT_ONE, "committed_amount": "1", "idempotency_key": "unknown-auth", "actual_usage_reference": "usage"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["rejection_reason_code"], "spend_authorization_not_found")
        over_status, over_body = invoke_http(
            app,
            "POST",
            "/v1/spend-authorizations",
            {**request, "idempotency_key": "http-over", "requested_amount": "91.00"},
        )
        self.assertEqual((over_status, over_body["rejection_reason_code"]), (422, "authorization_exposure_exceeded"))
        with mock.patch.object(SpendAuthorizationService, "present_authorization", side_effect=ValueError):
            status, body = invoke_http(
                app,
                "GET",
                f"/v1/spend-authorizations/{authorization_id}",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))
        bad_clock_app = __import__("metering_billing").create_http_app(ledger, clock=lambda: "bad")
        status, body = invoke_http(bad_clock_app, "POST", "/v1/spend-authorizations", request)
        self.assertEqual((status, body["rejection_reason_code"]), (422, "request_invalid"))


if __name__ == "__main__":
    unittest.main()
