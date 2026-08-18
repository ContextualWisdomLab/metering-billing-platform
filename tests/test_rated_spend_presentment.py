"""Rated-spend tests for already-rated product totals on one billing account."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from metering_billing import (
    InvoiceDraftService,
    RatedSpendPresentmentService,
    UsageIngestionService,
    UsageRatingService,
    compute_source_payload_hash,
    create_http_app,
    format_exact_decimal,
)
from metering_billing.contracts import validate_rated_spend_presentment
from metering_billing.errors import ExactDecimalError, RatedSpendPresentmentQueryError
from metering_billing.time_window import TimeWindow
from metering_billing.usage_ledger import (
    StoredInvoiceDraft,
    StoredInvoiceDraftLine,
    generate_record_id,
)
from test_account_statement_presentment import ACCOUNT_THREE, _account_id
from test_http_app import invoke_http
from test_usage_ingestion import (
    ACCOUNT_ONE,
    CATALOG_START,
    CREDENTIAL_ONE,
    PRINCIPAL_ONE,
    TENANT_ONE,
    TENANT_TWO,
    make_event,
)
from test_usage_rating import (
    DAY_WINDOW,
    KNOWN_MORNING_TOTAL,
    MORNING_WINDOW,
    ingest_known_batch,
    seed_rated_ledger,
)


AS_OF = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
PRODUCT_ONE = "contextual_orchestrator"
PRODUCT_TWO = "contextual_memory"
PROJECT_ONE = "urn:cwl:tenant_001:project:metering"
PROJECT_TWO = "urn:cwl:tenant_001:project:memory"
CREDENTIAL_ALT = "urn:cwl:tenant_001:credential_record:019d7004"
PRINCIPAL_ALT = "urn:cwl:tenant_001:billing_principal:019d7005"


def _rate_known_morning():
    """Ingest the known morning batch and persist one rating run."""
    ingest = ingest_known_batch()
    rated = UsageRatingService(ingest.ledger, clock=lambda: AS_OF).rate_usage_window(
        TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
    )
    return ingest.ledger, rated


def _spend_path(billing_account_id) -> str:
    """Return the nested rated-spend path for one billing account."""
    return f"/v1/billing-accounts/{billing_account_id}/rated-spend"


def _morning_query(**extra: str) -> dict[str, str]:
    """Return the known morning half-open window as query fields."""
    return {
        "window_started_at": "2026-08-16T10:00:00Z",
        "window_ended_at": "2026-08-16T11:00:00Z",
        **extra,
    }


def _rate_morning_events(*events: dict[str, object]):
    """Ingest caller-supplied morning events and persist one rating run."""
    ingest = UsageIngestionService(seed_rated_ledger())
    ingest.ingest_usage_batch(events)
    rated = UsageRatingService(ingest.ledger, clock=lambda: AS_OF).rate_usage_window(
        TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
    )
    return ingest.ledger, rated


def _known_morning_event(
    *,
    event_id: str,
    source_event_key: str,
    occurred_at: str,
    quantity: str,
    **overrides: object,
) -> dict[str, object]:
    """Return one known-morning token event, optionally attributed to a project."""
    return make_event(
        event_id=event_id,
        source_event_key=source_event_key,
        occurred_at=occurred_at,
        measurements=[
            {
                "meter_code": "gen_ai_output_token",
                "quantity": quantity,
                "unit_code": "token",
                "quality_code": "provider_reported",
            }
        ],
        **overrides,
    )


def _event_without_credential(**kwargs: object) -> dict[str, object]:
    """Return one morning event with credential_reference omitted, not a sentinel."""
    event = _known_morning_event(**kwargs)
    event.pop("credential_reference", None)
    event["source_payload_hash"] = compute_source_payload_hash(event)
    return event


def _seed_ledger_with_alt_credential():
    """Register a second same-tenant credential so mixed-credential spend can fail closed."""
    ledger = seed_rated_ledger()
    ledger.register_credential_record(TENANT_ONE, CREDENTIAL_ALT, "api_key", "fingerprint-alt")
    ledger.register_credential_assignment(
        TENANT_ONE, CREDENTIAL_ALT, PRINCIPAL_ONE, ACCOUNT_ONE, CATALOG_START
    )
    return ledger


def _seed_ledger_with_alt_principal():
    """Register a second same-tenant principal so mixed-principal spend can fail closed."""
    ledger = seed_rated_ledger()
    ledger.register_billing_principal(
        TENANT_ONE, PRINCIPAL_ALT, "github_workflow", CATALOG_START
    )
    ledger.register_credential_record(TENANT_ONE, CREDENTIAL_ALT, "api_key", "fingerprint-alt")
    ledger.register_credential_assignment(
        TENANT_ONE, CREDENTIAL_ALT, PRINCIPAL_ALT, ACCOUNT_ONE, CATALOG_START
    )
    return ledger


def _orphan_exclusive_principals(ledger, billing_account_id) -> None:
    """Point exclusive-account usage at a missing principal so grouping fails closed."""
    for event_id, event in list(ledger.usage_events.items()):
        if event.billing_account_id == billing_account_id:
            ledger.usage_events[event_id] = replace(
                event, billing_principal_id=uuid4()
            )


class RatedSpendPresentmentTests(unittest.TestCase):
    """Verify already-rated spend stays exact, product-grouped, and read-only."""

    def test_known_morning_spend_groups_by_product_without_rerating(self) -> None:
        """Stored morning rating lines become one USD product row."""
        ledger, rated = _rate_known_morning()
        prior_runs = len(ledger.rating_runs)
        prior_drafts = len(ledger.invoice_drafts)
        presented = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW
        )
        replay = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW
        )
        self.assertEqual(presented.as_contract_dict(), replay.as_contract_dict())
        self.assertEqual(presented.tenant_reference, TENANT_ONE)
        self.assertEqual(presented.billing_account_id, _account_id(ledger))
        self.assertEqual(presented.billing_account_reference, ACCOUNT_ONE)
        self.assertEqual(presented.window_started_at, MORNING_WINDOW.window_started_at)
        self.assertEqual(presented.window_ended_at, MORNING_WINDOW.window_ended_at)
        self.assertEqual(len(presented.products), 1)
        row = presented.products[0]
        self.assertEqual(row.currency_code, "USD")
        self.assertEqual(row.product_code, PRODUCT_ONE)
        self.assertEqual(row.rated_amount, rated.rated_total_amount)
        self.assertEqual(row.rated_amount, KNOWN_MORNING_TOTAL)
        payload = presented.as_contract_dict()
        self.assertEqual(validate_rated_spend_presentment(payload), ())
        self.assertEqual(payload["products"][0]["rated_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL))
        self.assertIsInstance(payload["products"][0]["rated_amount"], str)
        self.assertNotIsInstance(payload["products"][0]["rated_amount"], float)
        self.assertNotIn("group_by", payload)
        self.assertNotIn("project_reference", payload["products"][0])
        self.assertNotIn("credential_reference", payload["products"][0])
        self.assertNotIn("billing_principal_reference", payload["products"][0])
        self.assertNotIn("card_pan", payload)
        self.assertNotIn("statutory_account_id", payload)
        by_product = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="product"
        )
        self.assertEqual(presented.as_contract_dict(), by_product.as_contract_dict())
        self.assertNotIn("project_reference", by_product.as_contract_dict()["products"][0])
        self.assertNotIn("credential_reference", by_product.as_contract_dict()["products"][0])
        self.assertNotIn(
            "billing_principal_reference", by_product.as_contract_dict()["products"][0]
        )
        self.assertEqual(len(ledger.rating_runs), prior_runs)
        self.assertEqual(len(ledger.invoice_drafts), prior_drafts)
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_exclusive_draft_does_not_double_count_and_unrated_usage_is_omitted(self) -> None:
        """A later draft uses the same stored line amount; unrated events add nothing."""
        ledger, rated = _rate_known_morning()
        InvoiceDraftService(ledger).draft_invoice(TENANT_ONE, rated.rating_run_id)
        drafted = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW
        )
        self.assertEqual(drafted.products[0].rated_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(drafted.products), 1)
        unrated = UsageIngestionService(seed_rated_ledger())
        unrated.ingest_usage_batch(
            (
                make_event(
                    event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf701",
                    source_event_key="unrated_morning_token",
                    occurred_at="2026-08-16T10:30:00Z",
                ),
            )
        )
        empty = RatedSpendPresentmentService(unrated.ledger).present_rated_spend(
            TENANT_ONE, _account_id(unrated.ledger), MORNING_WINDOW
        )
        self.assertEqual(empty.products, ())
        self.assertEqual(validate_rated_spend_presentment(empty.as_contract_dict()), ())
        self.assertEqual(len(unrated.ledger.rating_runs), 0)

    def test_other_window_other_account_and_mixed_facts_stay_omitted(self) -> None:
        """A day window, another account, mixed products, and mixed drafts invent no spend."""
        ledger, rated = _rate_known_morning()
        day = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), DAY_WINDOW
        )
        self.assertEqual(day.products, ())
        later = TimeWindow.from_iso8601("2026-08-16T11:00:00Z", "2026-08-16T12:00:00Z")
        later_spend = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), later
        )
        self.assertEqual(later_spend.products, ())
        ledger.register_billing_account(TENANT_ONE, ACCOUNT_THREE)
        other = ledger.billing_accounts[ACCOUNT_THREE]
        other_spend = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, other.billing_account_id, MORNING_WINDOW
        )
        self.assertEqual(other_spend.products, ())
        mixed_product = UsageIngestionService(seed_rated_ledger())
        mixed_product.ingest_usage_batch(
            (
                make_event(),
                make_event(
                    event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf702",
                    source_event_key="memory_morning_token",
                    product_code=PRODUCT_TWO,
                    occurred_at="2026-08-16T10:40:00Z",
                    measurements=[
                        {
                            "meter_code": "gen_ai_output_token",
                            "quantity": "10",
                            "unit_code": "token",
                            "quality_code": "provider_reported",
                        }
                    ],
                ),
            )
        )
        UsageRatingService(mixed_product.ledger).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        mixed_rows = RatedSpendPresentmentService(mixed_product.ledger).present_rated_spend(
            TENANT_ONE, _account_id(mixed_product.ledger), MORNING_WINDOW
        )
        self.assertEqual(mixed_rows.products, ())
        tenant = ledger.require_tenant(TENANT_ONE)
        account = ledger.billing_accounts[ACCOUNT_ONE]
        mixed_id = generate_record_id()
        own_line = StoredInvoiceDraftLine(
            invoice_draft_line_id=generate_record_id(),
            invoice_draft_id=mixed_id,
            tenant_account_id=tenant.tenant_account_id,
            billing_account_id=account.billing_account_id,
            billing_account_reference=account.billing_account_reference,
            meter_definition_id=ledger.meter_definitions[0].meter_definition_id,
            meter_code="gen_ai_output_token",
            unit_code="token",
            rated_quantity=Decimal("1"),
            unit_price_amount=Decimal("15"),
            line_total_amount=Decimal("15"),
            line_number=1,
        )
        other_line = replace(
            own_line,
            invoice_draft_line_id=generate_record_id(),
            billing_account_id=other.billing_account_id,
            billing_account_reference=other.billing_account_reference,
            line_total_amount=Decimal("5"),
            unit_price_amount=Decimal("5"),
            line_number=2,
        )
        ledger.insert_invoice_draft(
            StoredInvoiceDraft(
                invoice_draft_id=mixed_id,
                tenant_account_id=tenant.tenant_account_id,
                rating_run_id=rated.rating_run_id,
                usage_snapshot_hash="sha256:" + ("c" * 64),
                currency_code="USD",
                invoice_draft_status="draft",
                drafted_total_amount=Decimal("20"),
                recorded_at=AS_OF,
                invoice_draft_lines=(own_line, other_line),
            ),
            (own_line, other_line),
        )
        after_mixed = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, account.billing_account_id, MORNING_WINDOW
        )
        self.assertEqual(after_mixed.products[0].rated_amount, KNOWN_MORNING_TOTAL)
        self.assertNotEqual(after_mixed.products[0].rated_amount, Decimal("15"))

    def test_group_by_project_uses_usage_project_and_omits_unattributed(self) -> None:
        """Project grouping reads usage URNs and never invents a project or split."""
        ledger, _rated = _rate_known_morning()
        unattributed = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="project"
        )
        self.assertEqual(unattributed.products, ())
        self.assertEqual(validate_rated_spend_presentment(unattributed.as_contract_dict()), ())
        self.assertNotIn("group_by", unattributed.as_contract_dict())
        attributed_ledger, attributed = _rate_morning_events(
            _known_morning_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf710",
                source_event_key="project_morning_output",
                occurred_at="2026-08-16T10:27:42.482Z",
                quantity="1810",
                project_reference=PROJECT_ONE,
            ),
            _known_morning_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf711",
                source_event_key="project_morning_followup",
                occurred_at="2026-08-16T10:28:10.000Z",
                quantity="42.5",
                project_reference=PROJECT_ONE,
            ),
        )
        product_rows = RatedSpendPresentmentService(attributed_ledger).present_rated_spend(
            TENANT_ONE, _account_id(attributed_ledger), MORNING_WINDOW
        )
        project_rows = RatedSpendPresentmentService(attributed_ledger).present_rated_spend(
            TENANT_ONE, _account_id(attributed_ledger), MORNING_WINDOW, group_by="project"
        )
        replay = RatedSpendPresentmentService(attributed_ledger).present_rated_spend(
            TENANT_ONE, _account_id(attributed_ledger), MORNING_WINDOW, group_by="project"
        )
        self.assertEqual(project_rows.as_contract_dict(), replay.as_contract_dict())
        self.assertEqual(len(product_rows.products), 1)
        self.assertNotIn("project_reference", product_rows.as_contract_dict()["products"][0])
        self.assertEqual(product_rows.products[0].rated_amount, attributed.rated_total_amount)
        self.assertEqual(product_rows.products[0].rated_amount, KNOWN_MORNING_TOTAL)
        self.assertEqual(len(project_rows.products), 1)
        row = project_rows.products[0]
        self.assertEqual(row.currency_code, "USD")
        self.assertEqual(row.product_code, PRODUCT_ONE)
        self.assertEqual(row.project_reference, PROJECT_ONE)
        self.assertEqual(row.rated_amount, KNOWN_MORNING_TOTAL)
        payload = project_rows.as_contract_dict()
        self.assertEqual(validate_rated_spend_presentment(payload), ())
        self.assertEqual(payload["products"][0]["project_reference"], PROJECT_ONE)
        self.assertEqual(payload["products"][0]["rated_amount"], format_exact_decimal(KNOWN_MORNING_TOTAL))
        self.assertNotIn("group_by", payload)
        self.assertNotIn("credential_reference", payload["products"][0])
        self.assertNotIn("billing_principal_reference", payload["products"][0])
        mixed_ledger, mixed = _rate_morning_events(
            _known_morning_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf712",
                source_event_key="project_mixed_metering",
                occurred_at="2026-08-16T10:27:42.482Z",
                quantity="1810",
                project_reference=PROJECT_ONE,
            ),
            _known_morning_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf713",
                source_event_key="project_mixed_memory",
                occurred_at="2026-08-16T10:28:10.000Z",
                quantity="42.5",
                project_reference=PROJECT_TWO,
            ),
        )
        mixed_product = RatedSpendPresentmentService(mixed_ledger).present_rated_spend(
            TENANT_ONE, _account_id(mixed_ledger), MORNING_WINDOW, group_by="product"
        )
        mixed_project = RatedSpendPresentmentService(mixed_ledger).present_rated_spend(
            TENANT_ONE, _account_id(mixed_ledger), MORNING_WINDOW, group_by="project"
        )
        self.assertEqual(mixed_product.products[0].rated_amount, mixed.rated_total_amount)
        self.assertNotIn("project_reference", mixed_product.as_contract_dict()["products"][0])
        self.assertEqual(mixed_project.products, ())
        partial_ledger, partial = _rate_morning_events(
            _known_morning_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf714",
                source_event_key="project_partial_attributed",
                occurred_at="2026-08-16T10:27:42.482Z",
                quantity="1810",
                project_reference=PROJECT_ONE,
            ),
            _known_morning_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf715",
                source_event_key="project_partial_plain",
                occurred_at="2026-08-16T10:28:10.000Z",
                quantity="42.5",
            ),
        )
        partial_project = RatedSpendPresentmentService(partial_ledger).present_rated_spend(
            TENANT_ONE, _account_id(partial_ledger), MORNING_WINDOW, group_by="project"
        )
        self.assertEqual(len(partial_project.products), 1)
        self.assertEqual(partial_project.products[0].project_reference, PROJECT_ONE)
        self.assertEqual(partial_project.products[0].rated_amount, partial.rated_total_amount)
        self.assertEqual(len(attributed_ledger.rating_runs), 1)
        self.assertEqual(len(attributed_ledger.invoice_drafts), 0)
        self.assertEqual(len(attributed_ledger.journal_proposals), 0)
        with self.assertRaises(RatedSpendPresentmentQueryError) as unknown_group:
            RatedSpendPresentmentService(ledger).present_rated_spend(
                TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="cost_center"
            )
        self.assertEqual(unknown_group.exception.rejection_reason_code, "request_invalid")

    def test_group_by_credential_uses_usage_credential_and_omits_unattributed(self) -> None:
        """Credential grouping reads usage URNs and never invents a credential or split."""
        ledger, rated = _rate_known_morning()
        by_product = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="product"
        )
        by_project = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="project"
        )
        by_credential = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="credential"
        )
        replay = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="credential"
        )
        self.assertEqual(by_credential.as_contract_dict(), replay.as_contract_dict())
        self.assertEqual(len(by_product.products), 1)
        self.assertNotIn("credential_reference", by_product.as_contract_dict()["products"][0])
        self.assertNotIn("project_reference", by_product.as_contract_dict()["products"][0])
        self.assertEqual(by_project.products, ())
        self.assertEqual(len(by_credential.products), 1)
        row = by_credential.products[0]
        self.assertEqual(row.currency_code, "USD")
        self.assertEqual(row.product_code, PRODUCT_ONE)
        self.assertEqual(row.credential_reference, CREDENTIAL_ONE)
        self.assertIsNone(row.project_reference)
        self.assertEqual(row.rated_amount, rated.rated_total_amount)
        self.assertEqual(row.rated_amount, KNOWN_MORNING_TOTAL)
        payload = by_credential.as_contract_dict()
        self.assertEqual(validate_rated_spend_presentment(payload), ())
        self.assertEqual(payload["products"][0]["credential_reference"], CREDENTIAL_ONE)
        self.assertNotIn("project_reference", payload["products"][0])
        self.assertNotIn("billing_principal_reference", payload["products"][0])
        self.assertNotIn("group_by", payload)
        unattributed_ledger, _unattributed = _rate_morning_events(
            _event_without_credential(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf720",
                source_event_key="credential_morning_plain_output",
                occurred_at="2026-08-16T10:27:42.482Z",
                quantity="1810",
            ),
            _event_without_credential(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf721",
                source_event_key="credential_morning_plain_followup",
                occurred_at="2026-08-16T10:28:10.000Z",
                quantity="42.5",
            ),
        )
        unattributed = RatedSpendPresentmentService(unattributed_ledger).present_rated_spend(
            TENANT_ONE, _account_id(unattributed_ledger), MORNING_WINDOW, group_by="credential"
        )
        self.assertEqual(unattributed.products, ())
        mixed = UsageIngestionService(_seed_ledger_with_alt_credential())
        mixed.ingest_usage_batch(
            (
                _known_morning_event(
                    event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf722",
                    source_event_key="credential_mixed_one",
                    occurred_at="2026-08-16T10:27:42.482Z",
                    quantity="1810",
                ),
                _known_morning_event(
                    event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf723",
                    source_event_key="credential_mixed_alt",
                    occurred_at="2026-08-16T10:28:10.000Z",
                    quantity="42.5",
                    credential_reference=CREDENTIAL_ALT,
                ),
            )
        )
        mixed_rated = UsageRatingService(mixed.ledger, clock=lambda: AS_OF).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        mixed_product = RatedSpendPresentmentService(mixed.ledger).present_rated_spend(
            TENANT_ONE, _account_id(mixed.ledger), MORNING_WINDOW, group_by="product"
        )
        mixed_credential = RatedSpendPresentmentService(mixed.ledger).present_rated_spend(
            TENANT_ONE, _account_id(mixed.ledger), MORNING_WINDOW, group_by="credential"
        )
        self.assertEqual(mixed_product.products[0].rated_amount, mixed_rated.rated_total_amount)
        self.assertNotIn("credential_reference", mixed_product.as_contract_dict()["products"][0])
        self.assertEqual(mixed_credential.products, ())
        partial_ledger, partial = _rate_morning_events(
            _known_morning_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf724",
                source_event_key="credential_partial_attributed",
                occurred_at="2026-08-16T10:27:42.482Z",
                quantity="1810",
            ),
            _event_without_credential(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf725",
                source_event_key="credential_partial_plain",
                occurred_at="2026-08-16T10:28:10.000Z",
                quantity="42.5",
            ),
        )
        partial_credential = RatedSpendPresentmentService(partial_ledger).present_rated_spend(
            TENANT_ONE, _account_id(partial_ledger), MORNING_WINDOW, group_by="credential"
        )
        self.assertEqual(len(partial_credential.products), 1)
        self.assertEqual(partial_credential.products[0].credential_reference, CREDENTIAL_ONE)
        self.assertEqual(partial_credential.products[0].rated_amount, partial.rated_total_amount)
        self.assertEqual(len(ledger.rating_runs), 1)
        self.assertEqual(len(ledger.invoice_drafts), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_group_by_principal_uses_usage_principal_and_omits_mixed(self) -> None:
        """Principal grouping reads stored URNs and never invents a principal or split."""
        ledger, rated = _rate_known_morning()
        by_product = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="product"
        )
        by_project = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="project"
        )
        by_credential = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="credential"
        )
        by_principal = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="principal"
        )
        replay = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="principal"
        )
        self.assertEqual(by_principal.as_contract_dict(), replay.as_contract_dict())
        self.assertEqual(len(by_product.products), 1)
        self.assertNotIn("billing_principal_reference", by_product.as_contract_dict()["products"][0])
        self.assertNotIn("project_reference", by_product.as_contract_dict()["products"][0])
        self.assertNotIn("credential_reference", by_product.as_contract_dict()["products"][0])
        self.assertEqual(by_project.products, ())
        self.assertEqual(by_credential.products[0].credential_reference, CREDENTIAL_ONE)
        self.assertIsNone(by_credential.products[0].billing_principal_reference)
        self.assertEqual(len(by_principal.products), 1)
        row = by_principal.products[0]
        self.assertEqual(row.currency_code, "USD")
        self.assertEqual(row.product_code, PRODUCT_ONE)
        self.assertEqual(row.billing_principal_reference, PRINCIPAL_ONE)
        self.assertIsNone(row.project_reference)
        self.assertIsNone(row.credential_reference)
        self.assertEqual(row.rated_amount, rated.rated_total_amount)
        self.assertEqual(row.rated_amount, KNOWN_MORNING_TOTAL)
        payload = by_principal.as_contract_dict()
        self.assertEqual(validate_rated_spend_presentment(payload), ())
        self.assertEqual(payload["products"][0]["billing_principal_reference"], PRINCIPAL_ONE)
        self.assertNotIn("project_reference", payload["products"][0])
        self.assertNotIn("credential_reference", payload["products"][0])
        self.assertNotIn("group_by", payload)
        ledger.register_billing_account(TENANT_ONE, ACCOUNT_THREE)
        other = ledger.billing_accounts[ACCOUNT_THREE]
        source = next(
            event
            for event in ledger.usage_events.values()
            if event.billing_account_id == _account_id(ledger)
        )
        foreign_id = generate_record_id()
        ledger.usage_events[foreign_id] = replace(
            source,
            usage_event_id=foreign_id,
            billing_account_id=other.billing_account_id,
        )
        skipped_other = RatedSpendPresentmentService(ledger).present_rated_spend(
            TENANT_ONE, _account_id(ledger), MORNING_WINDOW, group_by="principal"
        )
        self.assertEqual(skipped_other.products[0].billing_principal_reference, PRINCIPAL_ONE)
        self.assertEqual(skipped_other.products[0].rated_amount, KNOWN_MORNING_TOTAL)
        mixed = UsageIngestionService(_seed_ledger_with_alt_principal())
        mixed.ingest_usage_batch(
            (
                _known_morning_event(
                    event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf726",
                    source_event_key="principal_mixed_one",
                    occurred_at="2026-08-16T10:27:42.482Z",
                    quantity="1810",
                ),
                _known_morning_event(
                    event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf727",
                    source_event_key="principal_mixed_alt",
                    occurred_at="2026-08-16T10:28:10.000Z",
                    quantity="42.5",
                    billing_principal_reference=PRINCIPAL_ALT,
                    credential_reference=CREDENTIAL_ALT,
                ),
            )
        )
        mixed_rated = UsageRatingService(mixed.ledger, clock=lambda: AS_OF).rate_usage_window(
            TENANT_ONE, MORNING_WINDOW, 1, rate_card_code="cwl_standard"
        )
        mixed_product = RatedSpendPresentmentService(mixed.ledger).present_rated_spend(
            TENANT_ONE, _account_id(mixed.ledger), MORNING_WINDOW, group_by="product"
        )
        mixed_principal = RatedSpendPresentmentService(mixed.ledger).present_rated_spend(
            TENANT_ONE, _account_id(mixed.ledger), MORNING_WINDOW, group_by="principal"
        )
        self.assertEqual(mixed_product.products[0].rated_amount, mixed_rated.rated_total_amount)
        self.assertNotIn(
            "billing_principal_reference", mixed_product.as_contract_dict()["products"][0]
        )
        self.assertEqual(mixed_principal.products, ())
        orphaned_ledger, orphaned_rated = _rate_known_morning()
        _orphan_exclusive_principals(orphaned_ledger, _account_id(orphaned_ledger))
        orphaned_product = RatedSpendPresentmentService(orphaned_ledger).present_rated_spend(
            TENANT_ONE, _account_id(orphaned_ledger), MORNING_WINDOW, group_by="product"
        )
        orphaned_principal = RatedSpendPresentmentService(orphaned_ledger).present_rated_spend(
            TENANT_ONE, _account_id(orphaned_ledger), MORNING_WINDOW, group_by="principal"
        )
        self.assertEqual(orphaned_product.products[0].rated_amount, orphaned_rated.rated_total_amount)
        self.assertEqual(orphaned_principal.products, ())
        self.assertEqual(len(ledger.rating_runs), 1)
        self.assertEqual(len(ledger.invoice_drafts), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_http_reads_rated_spend_without_writing_money(self) -> None:
        """GET /v1/billing-accounts/{id}/rated-spend is a tenant-scoped read."""
        ledger, _rated = _rate_known_morning()
        billing_account_id = _account_id(ledger)
        tenant_two = ledger.require_tenant(TENANT_TWO)
        two_account = next(
            account
            for account in ledger.billing_accounts.values()
            if account.tenant_account_id == tenant_two.tenant_account_id
        )
        app = create_http_app(ledger)
        status, body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_rated_spend_presentment(body), ())
        self.assertEqual(body["billing_account_id"], str(billing_account_id))
        self.assertEqual(body["products"][0]["product_code"], PRODUCT_ONE)
        self.assertEqual(
            body["products"][0]["rated_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        forbidden_status, forbidden_body = invoke_http(
            app,
            "GET",
            _spend_path(two_account.billing_account_id),
            query=_morning_query(),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(forbidden_status, 403)
        self.assertEqual(forbidden_body["rejection_reason_code"], "billing_account_forbidden")
        missing_status, missing_body = invoke_http(
            app,
            "GET",
            _spend_path(uuid4()),
            query=_morning_query(),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing_body["rejection_reason_code"], "billing_account_not_found")
        no_tenant_status, no_tenant_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(),
        )
        self.assertEqual(no_tenant_status, 422)
        self.assertEqual(no_tenant_body["rejection_reason_code"], "tenant_not_found")
        unknown_tenant_status, unknown_tenant_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(),
            headers={"X-CWL-Tenant-Reference": "urn:cwl:missing_tenant"},
        )
        self.assertEqual(unknown_tenant_status, 422)
        self.assertEqual(unknown_tenant_body["rejection_reason_code"], "tenant_not_found")
        illegal_status, illegal_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query={
                "window_started_at": "2026-08-16T11:00:00Z",
                "window_ended_at": "2026-08-16T10:00:00Z",
            },
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(illegal_status, 422)
        self.assertEqual(illegal_body["rejection_reason_code"], "request_invalid")
        missing_window_status, missing_window_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(missing_window_status, 422)
        self.assertEqual(missing_window_body["rejection_reason_code"], "request_invalid")
        method_status, _method_body = invoke_http(
            app,
            "POST",
            _spend_path(billing_account_id),
            {"tenant_reference": TENANT_ONE, **_morning_query()},
        )
        self.assertEqual(method_status, 422)
        self.assertEqual(len(ledger.rating_runs), 1)
        self.assertEqual(len(ledger.invoice_drafts), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)
        _, issue_body = invoke_http(
            app,
            "POST",
            "/v1/tenant-api-credentials",
            {"tenant_reference": TENANT_ONE, "credential_label": "operator_key"},
        )
        gated_status, gated_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(gated_status, 422)
        self.assertEqual(gated_body["rejection_reason_code"], "api_credential_missing")
        keyed_status, keyed_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(),
            headers={
                "X-CWL-Tenant-Reference": TENANT_ONE,
                "Authorization": f"Bearer {issue_body['api_credential_secret']}",
            },
        )
        self.assertEqual(keyed_status, 200)
        self.assertEqual(
            keyed_body["products"][0]["rated_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        product_status, product_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(group_by="product"),
            headers={
                "X-CWL-Tenant-Reference": TENANT_ONE,
                "Authorization": f"Bearer {issue_body['api_credential_secret']}",
            },
        )
        self.assertEqual(product_status, 200)
        self.assertEqual(product_body, keyed_body)
        self.assertNotIn("project_reference", product_body["products"][0])
        self.assertNotIn("credential_reference", product_body["products"][0])
        self.assertNotIn("billing_principal_reference", product_body["products"][0])
        project_status, project_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(group_by="project"),
            headers={
                "X-CWL-Tenant-Reference": TENANT_ONE,
                "Authorization": f"Bearer {issue_body['api_credential_secret']}",
            },
        )
        self.assertEqual(project_status, 200)
        self.assertEqual(validate_rated_spend_presentment(project_body), ())
        self.assertEqual(project_body["products"], [])
        credential_status, credential_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(group_by="credential"),
            headers={
                "X-CWL-Tenant-Reference": TENANT_ONE,
                "Authorization": f"Bearer {issue_body['api_credential_secret']}",
            },
        )
        self.assertEqual(credential_status, 200)
        self.assertEqual(validate_rated_spend_presentment(credential_body), ())
        self.assertEqual(credential_body["products"][0]["credential_reference"], CREDENTIAL_ONE)
        self.assertNotIn("project_reference", credential_body["products"][0])
        self.assertEqual(
            credential_body["products"][0]["rated_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        principal_status, principal_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(group_by="principal"),
            headers={
                "X-CWL-Tenant-Reference": TENANT_ONE,
                "Authorization": f"Bearer {issue_body['api_credential_secret']}",
            },
        )
        self.assertEqual(principal_status, 200)
        self.assertEqual(validate_rated_spend_presentment(principal_body), ())
        self.assertEqual(
            principal_body["products"][0]["billing_principal_reference"], PRINCIPAL_ONE
        )
        self.assertNotIn("project_reference", principal_body["products"][0])
        self.assertNotIn("credential_reference", principal_body["products"][0])
        self.assertEqual(
            principal_body["products"][0]["rated_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        unknown_group_status, unknown_group_body = invoke_http(
            app,
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(group_by="cost_center"),
            headers={
                "X-CWL-Tenant-Reference": TENANT_ONE,
                "Authorization": f"Bearer {issue_body['api_credential_secret']}",
            },
        )
        self.assertEqual(unknown_group_status, 422)
        self.assertEqual(unknown_group_body["rejection_reason_code"], "request_invalid")
        attributed_ledger, _attributed = _rate_morning_events(
            _known_morning_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf716",
                source_event_key="http_project_morning",
                occurred_at="2026-08-16T10:27:42.482Z",
                quantity="1810",
                project_reference=PROJECT_ONE,
            ),
            _known_morning_event(
                event_id="019d7b92-1aa0-7a7f-b61c-962c0f4bf717",
                source_event_key="http_project_followup",
                occurred_at="2026-08-16T10:28:10.000Z",
                quantity="42.5",
                project_reference=PROJECT_ONE,
            ),
        )
        attributed_app = create_http_app(attributed_ledger)
        attributed_status, attributed_body = invoke_http(
            attributed_app,
            "GET",
            _spend_path(_account_id(attributed_ledger)),
            query=_morning_query(group_by="project"),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(attributed_status, 200)
        self.assertEqual(validate_rated_spend_presentment(attributed_body), ())
        self.assertEqual(attributed_body["products"][0]["project_reference"], PROJECT_ONE)
        self.assertEqual(
            attributed_body["products"][0]["rated_amount"],
            format_exact_decimal(KNOWN_MORNING_TOTAL),
        )
        self.assertEqual(len(ledger.rating_runs), 1)
        self.assertEqual(len(ledger.invoice_drafts), 0)
        self.assertEqual(len(ledger.journal_proposals), 0)

    def test_resolver_and_corrupt_line_fail_closed(self) -> None:
        """Hollow tenant resolve raises; IEEE rated lines cannot become spend."""
        ledger, rated = _rate_known_morning()
        billing_account_id = _account_id(ledger)
        service = RatedSpendPresentmentService(ledger)
        with mock.patch.object(service.ledger, "resolve_tenant", return_value=(None, None)):
            with self.assertRaisesRegex(ValueError, "tenant resolution succeeded"):
                service.present_rated_spend(TENANT_ONE, billing_account_id, MORNING_WINDOW)
        stored_run = ledger.rating_runs[rated.rating_run_id]
        corrupt_line = replace(stored_run.rating_lines[0], line_total_amount=0.003705)  # type: ignore[arg-type]
        ledger.rating_runs[rated.rating_run_id] = replace(stored_run, rating_lines=(corrupt_line,))
        with self.assertRaises(ExactDecimalError):
            service.present_rated_spend(TENANT_ONE, billing_account_id, MORNING_WINDOW)
        corrupt_status, corrupt_body = invoke_http(
            create_http_app(ledger),
            "GET",
            _spend_path(billing_account_id),
            query=_morning_query(),
            headers={"X-CWL-Tenant-Reference": TENANT_ONE},
        )
        self.assertEqual(corrupt_status, 422)
        self.assertEqual(corrupt_body["rejection_reason_code"], "request_invalid")
        empty = RatedSpendPresentmentService()
        with self.assertRaises(RatedSpendPresentmentQueryError) as missing_tenant:
            empty.present_rated_spend(TENANT_ONE, uuid4(), MORNING_WINDOW)
        self.assertEqual(missing_tenant.exception.rejection_reason_code, "tenant_not_found")
        self.assertEqual(
            TimeWindow.from_iso8601("2026-08-16T10:00:00Z", "2026-08-16T11:00:00Z"),
            MORNING_WINDOW,
        )


if __name__ == "__main__":
    unittest.main()
