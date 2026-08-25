"""HTTP journal-proposal query tests for an AIS pull without mutation."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from uuid import uuid4

from metering_billing.accounting_export import AccountingExportService
from metering_billing.contracts import validate_journal_proposal
from metering_billing.errors import JournalProposalQueryError
from metering_billing.http_app import create_http_app
from test_http_app import invoke_http
from test_usage_ingestion import TENANT_ONE, TENANT_TWO, known_event_batch
from test_usage_rating import seed_rated_ledger


def persist_known_proposals_over_http() -> tuple[object, str, str, str, str]:
    """Write the known morning commercial path and return both proposal ids."""
    app = create_http_app(seed_rated_ledger())
    invoke_http(
        app,
        "POST",
        "/v1/usage-events",
        {"tenant_reference": TENANT_ONE, "events": list(known_event_batch())},
    )
    _, rating_body = invoke_http(
        app,
        "POST",
        "/v1/rating-runs",
        {
            "tenant_reference": TENANT_ONE,
            "window_started_at": "2026-08-16T10:00:00Z",
            "window_ended_at": "2026-08-16T11:00:00Z",
            "rate_card_version": 1,
        },
    )
    _, draft_body = invoke_http(
        app,
        "POST",
        "/v1/invoice-drafts",
        {"tenant_reference": TENANT_ONE, "rating_run_id": rating_body["rating_run_id"]},
    )
    _, journal_body = invoke_http(
        app,
        "POST",
        "/v1/journal-proposals",
        {"tenant_reference": TENANT_ONE, "invoice_draft_id": draft_body["invoice_draft_id"]},
    )
    _, case_body = invoke_http(
        app,
        "POST",
        "/v1/collection-cases",
        {"tenant_reference": TENANT_ONE, "invoice_draft_id": draft_body["invoice_draft_id"]},
    )
    invoke_http(
        app,
        "POST",
        f"/v1/collection-cases/{case_body['collection_case_id']}/dunning-events",
        {"tenant_reference": TENANT_ONE, "dunning_notice_code": "first_notice"},
    )
    _, intent_body = invoke_http(
        app,
        "POST",
        "/v1/payment-intents",
        {"tenant_reference": TENANT_ONE, "collection_case_id": case_body["collection_case_id"]},
    )
    _, receipt_body = invoke_http(
        app,
        "POST",
        "/v1/payment-receipts",
        {
            "tenant_reference": TENANT_ONE,
            "payment_intent_id": intent_body["payment_intent_id"],
            "received_amount": "0.0037050",
        },
    )
    _, cash_body = invoke_http(
        app,
        "POST",
        "/v1/cash-journal-proposals",
        {"tenant_reference": TENANT_ONE, "payment_receipt_id": receipt_body["payment_receipt_id"]},
    )
    return (
        app,
        str(journal_body["proposal_id"]),
        str(cash_body["proposal_id"]),
        str(journal_body["proposal_status"]),
        str(cash_body["proposal_status"]),
    )


class JournalProposalQueryTests(unittest.TestCase):
    """Verify AIS can pull persisted proposals without flipping status."""

    def test_ais_pull_lists_and_fetches_ar_and_cash_without_mutation(self) -> None:
        """A second-tenant-isolated GET must return both shared-store proposals."""
        app, ar_proposal_id, cash_proposal_id, ar_status, cash_status = (
            persist_known_proposals_over_http()
        )
        self.assertEqual(ar_status, "validated")
        self.assertEqual(cash_status, "validated")

        list_status, list_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(list_status, 200)
        items = list_body["journal_proposals"]
        self.assertEqual(len(items), 2)
        self.assertIsNone(list_body["next_cursor"])
        listed_ids = [item["proposal_id"] for item in items]
        self.assertEqual(set(listed_ids), {ar_proposal_id, cash_proposal_id})
        proposed_keys = [(item["proposed_at"], item["proposal_id"]) for item in items]
        self.assertEqual(proposed_keys, sorted(proposed_keys))
        for item in items:
            self.assertEqual(validate_journal_proposal(item), ())
            self.assertEqual(item["proposal_status"], "validated")
            self.assertNotEqual(item["proposal_status"], "posted")
            self.assertNotEqual(item["proposal_status"], "exported")
            self.assertIsInstance(item["lines"][0]["debit_amount"], str)
            self.assertNotIsInstance(item["lines"][0]["debit_amount"], float)

        first_id = items[0]["proposal_id"]
        get_status, get_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{first_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body, items[0])
        self.assertEqual(validate_journal_proposal(get_body), ())

        after_status, after_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(after_status, 200)
        self.assertEqual(
            [item["proposal_status"] for item in after_body["journal_proposals"]],
            ["validated", "validated"],
        )
        self.assertEqual(after_body["journal_proposals"][0]["proposal_id"], ar_proposal_id)
        replay_get_status, replay_get_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{ar_proposal_id}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(replay_get_status, 200)
        self.assertEqual(replay_get_body["proposal_status"], ar_status)
        self.assertEqual(replay_get_body["proposal_status"], "validated")

        other_list_status, other_list_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_list_status, 200)
        self.assertEqual(other_list_body["journal_proposals"], [])
        self.assertIsNone(other_list_body["next_cursor"])
        other_get_status, other_get_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{ar_proposal_id}",
            query={"tenant_reference": TENANT_TWO},
        )
        self.assertEqual(other_get_status, 404)
        self.assertEqual(other_get_body["rejection_reason_code"], "proposal_not_found")
        self.assertNotIn("proposal_id", other_get_body)
        self.assertNotIn("lines", other_get_body)

    def test_empty_list_and_cursor_pages_are_deterministic(self) -> None:
        """An unused tenant is empty; page_limit=1 walks AR then cash by time."""
        empty_app = create_http_app(seed_rated_ledger())
        empty_status, empty_body = invoke_http(
            empty_app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty_body["journal_proposals"], [])
        self.assertIsNone(empty_body["next_cursor"])

        app, ar_proposal_id, cash_proposal_id, _, _ = persist_known_proposals_over_http()
        first_status, first_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE, "page_limit": "1"},
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(len(first_body["journal_proposals"]), 1)
        self.assertIsNotNone(first_body["next_cursor"])
        first_item = first_body["journal_proposals"][0]
        self.assertIn(first_item["proposal_id"], {ar_proposal_id, cash_proposal_id})

        second_status, second_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={
                "tenant_reference": TENANT_ONE,
                "page_limit": "1",
                "cursor": str(first_body["next_cursor"]),
            },
        )
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second_body["journal_proposals"]), 1)
        self.assertIsNone(second_body["next_cursor"])
        second_item = second_body["journal_proposals"][0]
        self.assertNotEqual(second_item["proposal_id"], first_item["proposal_id"])
        self.assertEqual(
            {first_item["proposal_id"], second_item["proposal_id"]},
            {ar_proposal_id, cash_proposal_id},
        )
        self.assertLess(
            (first_item["proposed_at"], first_item["proposal_id"]),
            (second_item["proposed_at"], second_item["proposal_id"]),
        )

        status_filter, filtered = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE, "proposal_status": "validated"},
        )
        self.assertEqual(status_filter, 200)
        self.assertEqual(len(filtered["journal_proposals"]), 2)
        draft_status, draft_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE, "proposal_status": "draft"},
        )
        self.assertEqual(draft_status, 200)
        self.assertEqual(draft_body["journal_proposals"], [])

        later = (
            datetime.fromisoformat(second_item["proposed_at"].replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).astimezone(UTC).isoformat().replace("+00:00", "Z")
        after_status, after_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE, "proposed_after": later},
        )
        self.assertEqual(after_status, 200)
        self.assertEqual(after_body["journal_proposals"], [])
        inclusive_status, inclusive_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={
                "tenant_reference": TENANT_ONE,
                "proposed_after": first_item["proposed_at"],
            },
        )
        self.assertEqual(inclusive_status, 200)
        self.assertEqual(len(inclusive_body["journal_proposals"]), 2)

    def test_query_fails_closed_on_missing_tenant_illegal_filter_and_unknown_id(self) -> None:
        """Missing tenant and illegal filters are 422; unknown ids stay 404."""
        app, ar_proposal_id, _, _, _ = persist_known_proposals_over_http()
        missing_status, missing_body = invoke_http(app, "GET", "/v1/journal-proposals")
        self.assertEqual(missing_status, 422)
        self.assertEqual(missing_body["rejection_reason_code"], "tenant_not_found")

        posted_status, posted_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE, "proposal_status": "posted"},
        )
        self.assertEqual(posted_status, 422)
        self.assertEqual(posted_body["rejection_reason_code"], "request_invalid")

        limit_status, limit_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE, "page_limit": "0"},
        )
        self.assertEqual(limit_status, 422)
        self.assertEqual(limit_body["rejection_reason_code"], "request_invalid")

        cursor_status, cursor_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE, "cursor": "not-a-cursor"},
        )
        self.assertEqual(cursor_status, 422)
        self.assertEqual(cursor_body["rejection_reason_code"], "request_invalid")

        after_status, after_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE, "proposed_after": "not-a-timestamp"},
        )
        self.assertEqual(after_status, 422)
        self.assertEqual(after_body["rejection_reason_code"], "request_invalid")

        unknown_status, unknown_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{uuid4()}",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown_body["rejection_reason_code"], "proposal_not_found")

        missing_item_tenant_status, missing_item_tenant_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{ar_proposal_id}",
        )
        self.assertEqual(missing_item_tenant_status, 422)
        self.assertEqual(missing_item_tenant_body["rejection_reason_code"], "tenant_not_found")

        cash_route_status, cash_route_body = invoke_http(
            app,
            "GET",
            "/v1/cash-journal-proposals",
            query={"tenant_reference": TENANT_ONE},
        )
        self.assertEqual(cash_route_status, 422)
        self.assertEqual(cash_route_body["rejection_reason_code"], "request_invalid")

        unknown_tenant_status, unknown_tenant_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": "urn:cwl:tenant_missing"},
        )
        self.assertEqual(unknown_tenant_status, 422)
        self.assertEqual(unknown_tenant_body["rejection_reason_code"], "tenant_not_found")

        unknown_item_tenant_status, unknown_item_tenant_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{ar_proposal_id}",
            query={"tenant_reference": "urn:cwl:tenant_missing"},
        )
        self.assertEqual(unknown_item_tenant_status, 422)
        self.assertEqual(unknown_item_tenant_body["rejection_reason_code"], "tenant_not_found")

        for method, path in (
            ("PUT", "/v1/journal-proposals"),
            ("PUT", f"/v1/journal-proposals/{ar_proposal_id}"),
        ):
            status, body = invoke_http(app, method, path, query={"tenant_reference": TENANT_ONE})
            self.assertEqual(status, 422, msg=f"{method} {path}")
            self.assertEqual(body["rejection_reason_code"], "request_invalid")

        for illegal_limit in ("abc", "101", "1.5"):
            status, body = invoke_http(
                app,
                "GET",
                "/v1/journal-proposals",
                query={"tenant_reference": TENANT_ONE, "page_limit": illegal_limit},
            )
            self.assertEqual(status, 422, msg=illegal_limit)
            self.assertEqual(body["rejection_reason_code"], "request_invalid")

        empty_filters_status, empty_filters_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={
                "tenant_reference": TENANT_ONE,
                "proposal_status": "",
                "proposed_after": "",
                "cursor": "",
                "page_limit": "",
            },
        )
        self.assertEqual(empty_filters_status, 200)
        self.assertEqual(len(empty_filters_body["journal_proposals"]), 2)

        service = AccountingExportService(seed_rated_ledger())
        with self.assertRaises(JournalProposalQueryError):
            service.list_journal_proposals(TENANT_ONE, page_limit=True)
        with self.assertRaises(JournalProposalQueryError):
            service.list_journal_proposals(TENANT_ONE, page_limit=1.5)
        empty_page = service.list_journal_proposals(TENANT_ONE, page_limit=2)
        self.assertEqual(empty_page.journal_proposals, ())
        self.assertIsNone(empty_page.next_cursor)

        seeded = create_http_app(seed_rated_ledger())
        with mock.patch(
            "metering_billing.http_app.AccountingExportService.list_journal_proposals",
            side_effect=ValueError("query decode failed"),
        ):
            boom_status, boom_body = invoke_http(
                seeded,
                "GET",
                "/v1/journal-proposals",
                query={"tenant_reference": TENANT_ONE},
            )
        self.assertEqual(boom_status, 422)
        self.assertEqual(boom_body["rejection_reason_code"], "request_invalid")

    def test_tenant_header_pins_reads_and_writes_without_statutory_ids(self) -> None:
        """AIS may pin X-CWL-Tenant-Reference; mismatch is 422; roles stay semantic."""
        app, ar_proposal_id, cash_proposal_id, _, _ = persist_known_proposals_over_http()
        header = {"X-CWL-Tenant-Reference": TENANT_ONE}

        header_list_status, header_list_body = invoke_http(
            app, "GET", "/v1/journal-proposals", headers=header
        )
        self.assertEqual(header_list_status, 200)
        self.assertEqual(len(header_list_body["journal_proposals"]), 2)
        for item in header_list_body["journal_proposals"]:
            roles = {line["account_role_code"] for line in item["lines"]}
            self.assertTrue(roles <= {"accounts_receivable", "usage_revenue", "cash_receipt"})
            self.assertNotIn("110200", json.dumps(item))
            self.assertNotIn("110100", json.dumps(item))
            self.assertNotIn("410100", json.dumps(item))

        matching_status, matching_body = invoke_http(
            app,
            "GET",
            f"/v1/journal-proposals/{ar_proposal_id}",
            query={"tenant_reference": TENANT_ONE},
            headers=header,
        )
        self.assertEqual(matching_status, 200)
        self.assertEqual(matching_body["proposal_id"], ar_proposal_id)
        self.assertEqual(matching_body["proposal_status"], "validated")

        mismatch_status, mismatch_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_TWO},
            headers=header,
        )
        self.assertEqual(mismatch_status, 422)
        self.assertEqual(mismatch_body["rejection_reason_code"], "request_invalid")

        empty_header_status, empty_header_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
            headers={"X-CWL-Tenant-Reference": ""},
        )
        self.assertEqual(empty_header_status, 200)
        self.assertEqual(len(empty_header_body["journal_proposals"]), 2)

        write_status, write_body = invoke_http(
            app,
            "POST",
            "/v1/journal-proposals",
            {"invoice_draft_id": "not-a-uuid"},
            headers=header,
        )
        self.assertEqual(write_status, 422)
        self.assertEqual(write_body["rejection_reason_code"], "request_invalid")

        rating_app = create_http_app(seed_rated_ledger())
        invoke_http(
            rating_app,
            "POST",
            "/v1/usage-events",
            {"tenant_reference": TENANT_ONE, "events": list(known_event_batch())},
        )
        header_write_status, header_write_body = invoke_http(
            rating_app,
            "POST",
            "/v1/rating-runs",
            {
                "window_started_at": "2026-08-16T10:00:00Z",
                "window_ended_at": "2026-08-16T11:00:00Z",
                "rate_card_version": 1,
            },
            headers=header,
        )
        self.assertEqual(header_write_status, 200)
        self.assertEqual(header_write_body["rating_outcome_code"], "accepted")

        mismatch_write_status, mismatch_write_body = invoke_http(
            rating_app,
            "POST",
            "/v1/rating-runs",
            {
                "tenant_reference": TENANT_TWO,
                "window_started_at": "2026-08-16T10:00:00Z",
                "window_ended_at": "2026-08-16T11:00:00Z",
                "rate_card_version": 1,
            },
            headers=header,
        )
        self.assertEqual(mismatch_write_status, 422)
        self.assertEqual(mismatch_write_body["rejection_reason_code"], "request_invalid")
        self.assertNotIn(cash_proposal_id, mismatch_write_body)

        non_string_header_status, non_string_header_body = invoke_http(
            app,
            "GET",
            "/v1/journal-proposals",
            query={"tenant_reference": TENANT_ONE},
            extra_environ={"HTTP_X_CWL_TENANT_REFERENCE": 1},
        )
        self.assertEqual(non_string_header_status, 200)
        self.assertEqual(len(non_string_header_body["journal_proposals"]), 2)
