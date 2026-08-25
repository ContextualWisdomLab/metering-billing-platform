import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { renderAmountDue } from "../src/amount_due.js";
import { renderInvoiceStatement, NEXT_OPERATOR_ACTION } from "../src/invoice_statement.js";
import { renderLineTable } from "../src/line_table.js";
import { renderStatusChip, resolveStatementStatus } from "../src/status_chip.js";
import { renderTenantPin } from "../src/tenant_pin.js";
import {
  renderCollectionCase,
  COLLECTION_CUSTOMER_COPY,
  nextOperatorActionCopy,
} from "../src/collection_case.js";
import {
  renderPaymentIntent,
  PAYMENT_INTENT_CUSTOMER_COPY,
  nextOperatorActionCopy as nextPaymentIntentActionCopy,
} from "../src/payment_intent.js";
import {
  renderPaymentReceipt,
  PAYMENT_RECEIPT_CUSTOMER_COPY,
  nextOperatorActionCopy as nextPaymentReceiptActionCopy,
} from "../src/payment_receipt.js";
import {
  renderCreditAdjustment,
  CREDIT_ADJUSTMENT_CUSTOMER_COPY,
  nextOperatorActionCopy as nextCreditAdjustmentActionCopy,
} from "../src/credit_adjustment.js";
import {
  renderRateCard,
  RATE_CARD_CUSTOMER_COPY,
  nextOperatorActionCopy as nextRateCardActionCopy,
} from "../src/rate_card.js";
import {
  renderUsageEvent,
  USAGE_EVENT_CUSTOMER_COPY,
  nextOperatorActionCopy as nextUsageEventActionCopy,
} from "../src/usage_event.js";
import {
  renderRatingRun,
  RATING_RUN_CUSTOMER_COPY,
  nextOperatorActionCopy as nextRatingRunActionCopy,
} from "../src/rating_run.js";
import {
  renderTaxAssessment,
  TAX_ASSESSMENT_CUSTOMER_COPY,
  nextOperatorActionCopy as nextTaxAssessmentActionCopy,
} from "../src/tax_assessment.js";
import {
  renderPostingReceiptObservation,
  POSTING_RECEIPT_OBSERVATION_CUSTOMER_COPY,
  nextOperatorActionCopy as nextPostingReceiptObservationActionCopy,
} from "../src/posting_receipt_observation.js";
import {
  renderWebhookDelivery,
  WEBHOOK_DELIVERY_CUSTOMER_COPY,
  nextOperatorActionCopy as nextWebhookDeliveryActionCopy,
} from "../src/webhook_delivery.js";
import {
  renderTenantApiCredential,
  TENANT_API_CREDENTIAL_CUSTOMER_COPY,
  nextOperatorActionCopy as nextTenantApiCredentialActionCopy,
} from "../src/tenant_api_credential.js";
import {
  renderWebhookSubscription,
  WEBHOOK_SUBSCRIPTION_CUSTOMER_COPY,
  nextOperatorActionCopy as nextWebhookSubscriptionActionCopy,
} from "../src/webhook_subscription.js";
import {
  renderDunningNotice,
  DUNNING_NOTICE_CUSTOMER_COPY,
  nextOperatorActionCopy as nextDunningNoticeActionCopy,
} from "../src/dunning_notice.js";
import {
  renderWebhookOutboxEvent,
  WEBHOOK_OUTBOX_EVENT_CUSTOMER_COPY,
  nextOperatorActionCopy as nextWebhookOutboxEventActionCopy,
} from "../src/webhook_outbox_event.js";
import {
  renderIssuedInvoice,
  ISSUED_INVOICE_CUSTOMER_COPY,
  nextOperatorActionCopy as nextIssuedInvoiceActionCopy,
} from "../src/issued_invoice.js";
import {
  renderIssuedCreditNote,
  ISSUED_CREDIT_NOTE_CUSTOMER_COPY,
  nextOperatorActionCopy as nextIssuedCreditNoteActionCopy,
} from "../src/issued_credit_note.js";
import {
  renderCreditNoteApplication,
  CREDIT_NOTE_APPLICATION_CUSTOMER_COPY,
  nextOperatorActionCopy as nextCreditNoteApplicationActionCopy,
} from "../src/credit_note_application.js";
import {
  renderCollectionCaseSettlement,
  COLLECTION_CASE_SETTLEMENT_CUSTOMER_COPY,
  nextOperatorActionCopy as nextCollectionCaseSettlementActionCopy,
} from "../src/collection_case_settlement.js";
import {
  renderCollectionAging,
  COLLECTION_AGING_CUSTOMER_COPY,
} from "../src/collection_aging.js";
import {
  renderAccountStatement,
  ACCOUNT_STATEMENT_CUSTOMER_COPY,
} from "../src/account_statement.js";
import {
  renderSpendBudget,
  SPEND_BUDGET_CUSTOMER_COPY,
  nextOperatorActionCopy as nextSpendBudgetActionCopy,
} from "../src/spend_budget.js";
import {
  renderRatedSpend,
  RATED_SPEND_CUSTOMER_COPY,
} from "../src/rated_spend.js";
import {
  renderBudgetStatus,
  BUDGET_STATUS_CUSTOMER_COPY,
  BUDGET_STATUS_TENANT_REFERENCE,
} from "../src/budget_status.js";
import {
  renderSpendBudgetOver,
  SPEND_BUDGET_OVER_CUSTOMER_COPY,
  SPEND_BUDGET_OVER_TENANT_REFERENCE,
} from "../src/spend_budget_over.js";

const fixturesDirectory = join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures");

function loadFixture(fileName) {
  return JSON.parse(readFileSync(join(fixturesDirectory, fileName), "utf8"));
}

test("taxed partial credit renders exact-decimal amount due and next action", () => {
  const statement = loadFixture("taxed_partial_credit.json");
  const html = renderInvoiceStatement(statement);
  assert.match(html, /99\.00 USD/);
  assert.match(html, /Collect or credit/);
  assert.equal(NEXT_OPERATOR_ACTION, "Collect or credit");
  assert.equal(resolveStatementStatus(statement), "due");
  assert.match(renderAmountDue(statement), />99\.00 USD</);
  assert.match(renderLineTable(statement), />100\.00</);
  assert.match(renderStatusChip(statement), /due/);
  assert.match(renderTenantPin(statement), /urn:cwl:tenant_001/);
  assert.equal(typeof statement.amount_due, "string");
  assert.notEqual(typeof statement.amount_due, "number");
});

test("untaxed morning fixture keeps the known exact product string", () => {
  const statement = loadFixture("untaxed_morning.json");
  const html = renderInvoiceStatement(statement);
  assert.match(html, /0\.003705/);
  assert.match(html, /1852\.5/);
  assert.match(html, /0\.000002/);
  assert.equal(resolveStatementStatus(statement), "draft");
  assert.equal(typeof statement.invoice_lines[0].quantity, "string");
});

test("settled fixture shows zero due as a string", () => {
  const statement = loadFixture("settled_statement.json");
  const html = renderInvoiceStatement(statement);
  assert.match(html, /0\.00 USD/);
  assert.equal(resolveStatementStatus(statement), "settled");
  assert.match(renderStatusChip(statement), /settled/);
});

test("open collection case shows outstanding and collect or credit", () => {
  const statement = loadFixture("open_collection_case.json");
  const html = renderCollectionCase(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /Collect or credit/);
  assert.match(html, /Open the collection case, then collect or credit/);
  assert.equal(COLLECTION_CUSTOMER_COPY, "Open the collection case, then collect or credit.");
  assert.equal(nextOperatorActionCopy("collect"), "Collect or credit");
  assert.equal(nextOperatorActionCopy("credit"), "Credit");
  assert.equal(nextOperatorActionCopy("wait"), "Wait");
  assert.equal(typeof statement.collection_outstanding, "string");
});

test("dunning collection case keeps first_notice and outstanding as a string", () => {
  const statement = loadFixture("dunning_collection_case.json");
  const html = renderCollectionCase(statement);
  assert.match(html, /100\.00 USD/);
  assert.match(html, /dunning/);
  assert.equal(statement.last_dunning_notice_code, "first_notice");
  assert.equal(typeof statement.collection_outstanding, "string");
});

test("settled collection case waits", () => {
  const statement = loadFixture("settled_collection_case.json");
  const html = renderCollectionCase(statement);
  assert.match(html, /0\.00 USD/);
  assert.match(html, /Wait/);
  assert.equal(statement.next_operator_action, "wait");
});

test("projected payment intent shows amount and record the receipt", () => {
  const statement = loadFixture("projected_payment_intent.json");
  const html = renderPaymentIntent(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /Record the receipt/);
  assert.match(html, /Create a projected payment intent, then record the receipt/);
  assert.equal(
    PAYMENT_INTENT_CUSTOMER_COPY,
    "Create a projected payment intent, then record the receipt.",
  );
  assert.equal(nextPaymentIntentActionCopy("record_receipt"), "Record the receipt");
  assert.equal(nextPaymentIntentActionCopy("wait"), "Wait");
  assert.equal(typeof statement.payment_amount, "string");
});

test("cancelled payment intent waits", () => {
  const statement = loadFixture("cancelled_payment_intent.json");
  const html = renderPaymentIntent(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /Wait/);
  assert.equal(statement.next_operator_action, "wait");
});

test("full payment receipt shows received amount and drain or wait", () => {
  const statement = loadFixture("applied_full_payment_receipt.json");
  const html = renderPaymentReceipt(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /Drain or wait for AIS/);
  assert.match(html, /Record the receipt; the cash journal is already validated for AIS to pull/);
  assert.equal(
    PAYMENT_RECEIPT_CUSTOMER_COPY,
    "Record the receipt; the cash journal is already validated for AIS to pull.",
  );
  assert.equal(nextPaymentReceiptActionCopy("drain_or_wait"), "Drain or wait for AIS");
  assert.equal(nextPaymentReceiptActionCopy("record_receipt"), "Record the receipt");
  assert.equal(typeof statement.received_amount, "string");
});

test("partial payment receipt keeps remaining outstanding as a string", () => {
  const statement = loadFixture("applied_partial_payment_receipt.json");
  const html = renderPaymentReceipt(statement);
  assert.match(html, /0\.001 USD/);
  assert.match(html, /Record the receipt/);
  assert.equal(statement.next_operator_action, "record_receipt");
  assert.equal(typeof statement.remaining_outstanding_amount, "string");
});

test("recorded morning credit shows amount and wait", () => {
  const statement = loadFixture("recorded_morning_credit.json");
  const html = renderCreditAdjustment(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /Wait for AIS/);
  assert.match(html, /Record the credit; AIS pulls the validated journal/);
  assert.equal(
    CREDIT_ADJUSTMENT_CUSTOMER_COPY,
    "Record the credit; AIS pulls the validated journal.",
  );
  assert.equal(nextCreditAdjustmentActionCopy("wait"), "Wait for AIS");
  assert.equal(nextCreditAdjustmentActionCopy("credit"), "Record the credit");
  assert.equal(typeof statement.credit_amount, "string");
});

test("taxed credit keeps exclusive and tax as strings", () => {
  const statement = loadFixture("recorded_taxed_credit.json");
  const html = renderCreditAdjustment(statement);
  assert.match(html, /11\.00 USD/);
  assert.equal(statement.next_operator_action, "wait");
  assert.equal(typeof statement.tax_exclusive_amount, "string");
  assert.equal(typeof statement.tax_amount, "string");
});

test("published standard rate card shows unit price and rate a window", () => {
  const statement = loadFixture("published_standard_rate.json");
  const html = renderRateCard(statement);
  assert.match(html, /0\.000002 USD/);
  assert.match(html, /Rate a window/);
  assert.match(html, /Publish a rate card, then rate a window against that version/);
  assert.equal(
    RATE_CARD_CUSTOMER_COPY,
    "Publish a rate card, then rate a window against that version.",
  );
  assert.equal(nextRateCardActionCopy("rate_window"), "Rate a window");
  assert.equal(nextRateCardActionCopy("publish"), "Publish a rate card");
  assert.equal(typeof statement.lines[0].unit_amount, "string");
});

test("published premium rate card keeps unit amount as a string", () => {
  const statement = loadFixture("published_premium_rate.json");
  const html = renderRateCard(statement);
  assert.match(html, /0\.000005 USD/);
  assert.equal(statement.next_operator_action, "rate_window");
  assert.equal(typeof statement.lines[0].unit_amount, "string");
});

test("stored morning usage shows quantity and rate a window", () => {
  const statement = loadFixture("stored_morning_usage.json");
  const html = renderUsageEvent(statement);
  assert.match(html, /1810 token/);
  assert.match(html, /Rate a window/);
  assert.match(html, /Ingest usage, then rate a window against a published card/);
  assert.equal(
    USAGE_EVENT_CUSTOMER_COPY,
    "Ingest usage, then rate a window against a published card.",
  );
  assert.equal(nextUsageEventActionCopy("rate_window"), "Rate a window");
  assert.equal(nextUsageEventActionCopy("ingest"), "Ingest usage");
  assert.equal(typeof statement.measurements[0].quantity, "string");
});

test("stored partial token usage keeps quantity as a string", () => {
  const statement = loadFixture("stored_partial_token_usage.json");
  const html = renderUsageEvent(statement);
  assert.match(html, /42\.5 token/);
  assert.equal(statement.next_operator_action, "rate_window");
  assert.equal(typeof statement.measurements[0].quantity, "string");
});

test("rated morning window shows total and draft an invoice", () => {
  const statement = loadFixture("rated_morning_window.json");
  const html = renderRatingRun(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /Draft an invoice/);
  assert.match(html, /Rate a window, then draft an invoice/);
  assert.equal(RATING_RUN_CUSTOMER_COPY, "Rate a window, then draft an invoice.");
  assert.equal(nextRatingRunActionCopy("draft_invoice"), "Draft an invoice");
  assert.equal(nextRatingRunActionCopy("rate"), "Rate a window");
  assert.equal(typeof statement.rated_total_amount, "string");
});

test("rated partial window keeps total as a string", () => {
  const statement = loadFixture("rated_partial_window.json");
  const html = renderRatingRun(statement);
  assert.match(html, /0\.000085 USD/);
  assert.equal(statement.next_operator_action, "draft_invoice");
  assert.equal(typeof statement.rated_total_amount, "string");
});

test("assessed morning vat shows inclusive and propose the journal", () => {
  const statement = loadFixture("assessed_morning_vat.json");
  const html = renderTaxAssessment(statement);
  assert.match(html, /110\.00 USD/);
  assert.match(html, /Propose the journal/);
  assert.match(
    html,
    /Publish a tax rate, assess the draft, then propose the journal and let AIS pull/,
  );
  assert.equal(
    TAX_ASSESSMENT_CUSTOMER_COPY,
    "Publish a tax rate, assess the draft, then propose the journal and let AIS pull.",
  );
  assert.equal(nextTaxAssessmentActionCopy("propose_journal"), "Propose the journal");
  assert.equal(nextTaxAssessmentActionCopy("assess"), "Assess the draft");
  assert.equal(typeof statement.tax_inclusive_amount, "string");
});

test("assessed partial vat keeps inclusive as a string", () => {
  const statement = loadFixture("assessed_partial_vat.json");
  const html = renderTaxAssessment(statement);
  assert.match(html, /22\.00 USD/);
  assert.equal(statement.next_operator_action, "propose_journal");
  assert.equal(typeof statement.tax_inclusive_amount, "string");
});

test("observed posted morning shows posted and wait", () => {
  const statement = loadFixture("observed_posted_morning.json");
  const html = renderPostingReceiptObservation(statement);
  assert.match(html, /posted/);
  assert.match(html, />Wait</);
  assert.match(
    html,
    /Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty/,
  );
  assert.equal(
    POSTING_RECEIPT_OBSERVATION_CUSTOMER_COPY,
    "Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty.",
  );
  assert.equal(nextPostingReceiptObservationActionCopy("wait"), "Wait");
  assert.equal(nextPostingReceiptObservationActionCopy("pull"), "Store the observation");
  assert.equal(statement.posting_status_code, "posted");
});

test("delivered morning webhook shows wait and never leaks a secret", () => {
  const statement = loadFixture("delivered_morning.json");
  const html = renderWebhookDelivery(statement);
  assert.match(html, /019d7b92-1aa0-7a7f-b61c-962c0f4bf8a0/);
  assert.match(html, />Wait</);
  assert.match(
    html,
    /Register an https callback, then run deliveries; AIS may keep polling/,
  );
  assert.equal(
    WEBHOOK_DELIVERY_CUSTOMER_COPY,
    "Register an https callback, then run deliveries; AIS may keep polling.",
  );
  assert.equal(nextWebhookDeliveryActionCopy("wait"), "Wait");
  assert.equal(nextWebhookDeliveryActionCopy("run_deliveries"), "Run deliveries");
  assert.equal(nextWebhookDeliveryActionCopy("unknown"), "Run deliveries");
  assert.equal(statement.next_operator_action, "wait");
  assert.ok(!("webhook_secret" in statement));
  assert.ok(!("payload_json" in statement));
  assert.ok(!("delivery_status" in statement));
});

test("active operator key shows prefix and wait", () => {
  const statement = loadFixture("active_operator_key.json");
  const html = renderTenantApiCredential(statement);
  assert.match(html, /cwlak_fake001/);
  assert.match(html, />Wait</);
  assert.match(html, /Issue a key, then send it on every \/v1 call; revoke when leaked/);
  assert.equal(
    TENANT_API_CREDENTIAL_CUSTOMER_COPY,
    "Issue a key, then send it on every /v1 call; revoke when leaked.",
  );
  assert.equal(nextTenantApiCredentialActionCopy("wait"), "Wait");
  assert.equal(nextTenantApiCredentialActionCopy("issue"), "Issue a key");
  assert.equal(nextTenantApiCredentialActionCopy("unknown"), "Issue a key");
  assert.equal(statement.next_operator_action, "wait");
  assert.ok(!("api_credential_secret" in statement));
  assert.ok(!("credential_secret_hash" in statement));
});

test("revoked leaked key shows issue", () => {
  const statement = loadFixture("revoked_leaked_key.json");
  const html = renderTenantApiCredential(statement);
  assert.match(html, /cwlak_fake002/);
  assert.match(html, /revoked/);
  assert.match(html, /Issue a key/);
  assert.equal(statement.next_operator_action, "issue");
});

test("active https callback shows run deliveries and never leaks a secret", () => {
  const statement = loadFixture("active_https_callback.json");
  const html = renderWebhookSubscription(statement);
  assert.match(html, /https:\/\/hooks\.example\.test\/cwl/);
  assert.match(html, /Run deliveries/);
  assert.match(
    html,
    /Register an https callback, then run deliveries; AIS may keep polling/,
  );
  assert.equal(
    WEBHOOK_SUBSCRIPTION_CUSTOMER_COPY,
    "Register an https callback, then run deliveries; AIS may keep polling.",
  );
  assert.equal(nextWebhookSubscriptionActionCopy("run_deliveries"), "Run deliveries");
  assert.equal(nextWebhookSubscriptionActionCopy("register"), "Register a callback");
  assert.equal(nextWebhookSubscriptionActionCopy("unknown"), "Register a callback");
  assert.equal(statement.next_operator_action, "run_deliveries");
  assert.ok(!("webhook_secret" in statement));
  assert.ok(!("webhook_secret_hash" in statement));
  assert.ok(!("webhook_secret_prefix" in statement));
  assert.ok(!("payload_json" in statement));
});

test("revoked https callback shows register", () => {
  const statement = loadFixture("revoked_https_callback.json");
  const html = renderWebhookSubscription(statement);
  assert.match(html, /https:\/\/hooks\.example\.test\/cwl-revoked/);
  assert.match(html, /revoked/);
  assert.match(html, /Register a callback/);
  assert.equal(statement.next_operator_action, "register");
});

test("first notice morning shows collect and never invents a send channel", () => {
  const statement = loadFixture("first_notice_morning.json");
  const html = renderDunningNotice(statement);
  assert.match(html, /first_notice/);
  assert.match(html, /Collect or credit/);
  assert.match(html, /Record the commercial reminder, then collect or credit/);
  assert.equal(
    DUNNING_NOTICE_CUSTOMER_COPY,
    "Record the commercial reminder, then collect or credit.",
  );
  assert.equal(nextDunningNoticeActionCopy("collect"), "Collect or credit");
  assert.equal(nextDunningNoticeActionCopy("wait"), "Wait");
  assert.equal(nextDunningNoticeActionCopy("unknown"), "Collect or credit");
  assert.equal(statement.next_operator_action, "collect");
  assert.ok(!("recipient" in statement));
  assert.ok(!("delivery_status" in statement));
  assert.ok(!("body" in statement));
});

test("overdue notice evening shows collect", () => {
  const statement = loadFixture("overdue_notice_evening.json");
  const html = renderDunningNotice(statement);
  assert.match(html, /overdue_notice/);
  assert.match(html, /Collect or credit/);
  assert.equal(statement.next_operator_action, "collect");
});

test("pending journal outbox event shows run deliveries and never leaks a body", () => {
  const statement = loadFixture("pending_journal_validated.json");
  const html = renderWebhookOutboxEvent(statement);
  assert.match(html, /journal_proposal\.validated/);
  assert.match(html, /Run deliveries/);
  assert.match(
    html,
    /Register an https callback, then run deliveries; AIS may keep polling/,
  );
  assert.equal(
    WEBHOOK_OUTBOX_EVENT_CUSTOMER_COPY,
    "Register an https callback, then run deliveries; AIS may keep polling.",
  );
  assert.equal(nextWebhookOutboxEventActionCopy("run_deliveries"), "Run deliveries");
  assert.equal(nextWebhookOutboxEventActionCopy("wait"), "Wait");
  assert.equal(nextWebhookOutboxEventActionCopy("unknown"), "Run deliveries");
  assert.equal(statement.next_operator_action, "run_deliveries");
  assert.ok(!("payload_json" in statement));
  assert.ok(!("webhook_secret" in statement));
  assert.ok(!("signature" in statement));
});

test("delivered receipt outbox event shows wait", () => {
  const statement = loadFixture("delivered_receipt_applied.json");
  const html = renderWebhookOutboxEvent(statement);
  assert.match(html, /payment_receipt\.applied/);
  assert.match(html, />Wait</);
  assert.equal(statement.next_operator_action, "wait");
  assert.equal(statement.delivery_status, "delivered");
});

test("failed callback webhook shows run deliveries", () => {
  const statement = loadFixture("failed_callback.json");
  const html = renderWebhookDelivery(statement);
  assert.match(html, /webhook_http_error/);
  assert.match(html, /Run deliveries/);
  assert.equal(statement.next_operator_action, "run_deliveries");
  const actionOnly = renderWebhookDelivery({
    delivery_attempt_id: statement.delivery_attempt_id,
    tenant_reference: statement.tenant_reference,
    next_operator_action: "run_deliveries",
  });
  assert.match(actionOnly, /run_deliveries/);
});

test("observed held receipt keeps AIS held status", () => {
  const statement = loadFixture("observed_held_receipt.json");
  const html = renderPostingReceiptObservation(statement);
  assert.match(html, /held/);
  assert.equal(statement.next_operator_action, "wait");
  assert.equal(statement.posting_status_code, "held");
});

test("issued untaxed morning shows collect and known exact product", () => {
  const statement = loadFixture("issued_untaxed_morning.json");
  const html = renderIssuedInvoice(statement);
  assert.match(html, /0\.003705/);
  assert.match(html, /1852\.5/);
  assert.match(html, /Collect or credit/);
  assert.match(html, /Issue invoice, then collect or credit/);
  assert.equal(ISSUED_INVOICE_CUSTOMER_COPY, "Issue invoice, then collect or credit.");
  assert.equal(nextIssuedInvoiceActionCopy("collect"), "Collect or credit");
  assert.equal(nextIssuedInvoiceActionCopy("wait"), "Issue invoice");
  assert.equal(statement.next_operator_action, "collect");
  assert.equal(typeof statement.tax_inclusive_amount, "string");
  assert.ok(!("invoice_number" in statement));
  assert.ok(!("legal_invoice_number" in statement));
  assert.ok(!("card_pan" in statement));
});

test("issued taxed hundred freezes inclusive 110.00", () => {
  const statement = loadFixture("issued_taxed_hundred.json");
  const html = renderIssuedInvoice(statement);
  assert.match(html, /110\.00 USD/);
  assert.match(html, /issued/);
  assert.equal(statement.tax_inclusive_amount, "110.00");
  assert.equal(statement.next_operator_action, "collect");
});

test("issued morning credit note shows wait and known exact product", () => {
  const statement = loadFixture("issued_morning_credit_note.json");
  const html = renderIssuedCreditNote(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /Wait for AIS/);
  assert.match(html, /Issue the credit note; the validated journal remains available for AIS/);
  assert.equal(
    ISSUED_CREDIT_NOTE_CUSTOMER_COPY,
    "Issue the credit note; the validated journal remains available for AIS.",
  );
  assert.equal(nextIssuedCreditNoteActionCopy("wait"), "Wait for AIS");
  assert.equal(nextIssuedCreditNoteActionCopy("issue"), "Issue the credit note");
  assert.equal(statement.next_operator_action, "wait");
  assert.equal(typeof statement.tax_inclusive_amount, "string");
  assert.ok(!("credit_note_number" in statement));
  assert.ok(!("legal_credit_note_number" in statement));
  assert.ok(!("issued_credit_note_lines" in statement));
  assert.ok(!("card_pan" in statement));
});

test("applied morning credit note shows applied amount and residual wait", () => {
  const statement = loadFixture("applied_morning_credit_note.json");
  const html = renderCreditNoteApplication(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /applied/);
  assert.match(html, /Apply the issued credit note, then collect the residual/);
  assert.equal(
    CREDIT_NOTE_APPLICATION_CUSTOMER_COPY,
    "Apply the issued credit note, then collect the residual.",
  );
  assert.equal(nextCreditNoteApplicationActionCopy("wait"), "Wait");
  assert.equal(nextCreditNoteApplicationActionCopy("collect"), "Collect");
  assert.equal(statement.next_operator_action, "wait");
  assert.equal(typeof statement.applied_amount, "string");
  assert.equal(typeof statement.remaining_outstanding_amount, "string");
  assert.ok(!("legal_credit_note_number" in statement));
  assert.ok(!("credit_note_application_outcome_code" in statement));
  assert.ok(!("card_pan" in statement));
});

test("settled account statement shows zero remaining and exact applied credit", () => {
  const statement = loadFixture("settled_account_statement.json");
  const html = renderAccountStatement(statement);
  assert.match(html, /0\.00 USD/);
  assert.match(html, /110\.00 USD/);
  assert.match(html, /settled/);
  assert.match(html, /Open the account statement, then collect, credit, park, apply, or refund/);
  assert.equal(
    ACCOUNT_STATEMENT_CUSTOMER_COPY,
    "Open the account statement, then collect, credit, park, apply, or refund.",
  );
  assert.equal(statement.currencies[0].voided_invoice_total, "0");
  assert.equal(statement.currencies[0].voided_credit_total, "0");
  assert.equal(typeof statement.currencies[0].open_collection_remaining, "string");
  assert.equal(typeof statement.currencies[0].applied_credit_total, "string");
  assert.match(renderTenantPin(statement), /urn:cwl:tenant_001/);
  assert.ok(!("card_pan" in statement));
  assert.ok(!("statutory_account_id" in statement));
  assert.ok(!("proposal_status" in statement));
});

test("voided account statement keeps inclusive voids unmixed and does not rewrite issued", () => {
  const statement = loadFixture("voided_account_statement.json");
  const html = renderAccountStatement(statement);
  assert.match(html, /110\.00 USD/);
  assert.match(html, /11\.00 USD/);
  assert.equal(statement.currencies[0].issued_invoice_total, "110.00");
  assert.equal(statement.currencies[0].voided_invoice_total, "110.00");
  assert.equal(statement.currencies[0].voided_credit_total, "11.00");
  assert.equal(statement.currencies[0].applied_credit_total, "0");
  assert.equal(statement.currencies[0].currency_code, "USD");
  assert.equal(statement.currencies.length, 1);
  assert.equal(typeof statement.currencies[0].voided_invoice_total, "string");
  assert.equal(typeof statement.currencies[0].voided_credit_total, "string");
  assert.notEqual(typeof statement.currencies[0].voided_invoice_total, "number");
  assert.ok(!("card_pan" in statement));
  assert.ok(!("journal_entry_id" in statement));
});

test("morning aging shows exact USD buckets without mixing currencies", () => {
  const statement = loadFixture("morning_collection_aging.json");
  const html = renderCollectionAging(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /1\.25 USD/);
  assert.match(html, /8\.00 USD/);
  assert.match(html, /Open the aging statement, then collect or credit/);
  assert.equal(
    COLLECTION_AGING_CUSTOMER_COPY,
    "Open the aging statement, then collect or credit.",
  );
  assert.equal(typeof statement.currencies[0].current.outstanding_amount, "string");
  assert.ok(!("collection_case_id" in statement));
  assert.ok(!("card_pan" in statement));
  assert.ok(!("statutory_account_id" in statement));
});

test("settled morning zero shows exact-zero remaining and wait", () => {
  const statement = loadFixture("settled_morning_zero.json");
  const html = renderCollectionCaseSettlement(statement);
  assert.match(html, /0 USD/);
  assert.match(html, /settled/);
  assert.match(html, /Settle the zero-outstanding case, then wait/);
  assert.equal(
    COLLECTION_CASE_SETTLEMENT_CUSTOMER_COPY,
    "Settle the zero-outstanding case, then wait.",
  );
  assert.equal(nextCollectionCaseSettlementActionCopy("wait"), "Wait");
  assert.equal(nextCollectionCaseSettlementActionCopy("settle"), "Settle");
  assert.equal(statement.next_operator_action, "wait");
  assert.equal(typeof statement.remaining_outstanding_amount, "string");
  assert.ok(!("write_off_amount" in statement));
  assert.ok(!("collection_case_settlement_outcome_code" in statement));
  assert.ok(!("card_pan" in statement));
});

test("issued taxed credit note freezes inclusive 11.00", () => {
  const statement = loadFixture("issued_taxed_credit_note.json");
  const html = renderIssuedCreditNote(statement);
  assert.match(html, /11\.00 USD/);
  assert.match(html, /issued/);
  assert.equal(statement.tax_exclusive_amount, "10.00");
  assert.equal(statement.tax_amount, "1.00");
  assert.equal(statement.tax_inclusive_amount, "11.00");
  assert.equal(statement.next_operator_action, "wait");
  assert.equal(statement.issued_invoice_id, "019d7b92-1aa0-7a7f-b61c-962c0f4bfd11");
});

test("published under budget shows remaining and wait", () => {
  const statement = loadFixture("published_under_budget.json");
  const html = renderSpendBudget(statement);
  assert.match(html, /99\.996295 USD/);
  assert.match(html, /100\.00 USD/);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, />Wait</);
  assert.match(html, /oc-status-chip--settled/);
  assert.match(html, /under/);
  assert.match(html, /Publish a commercial spend budget, then wait/);
  assert.equal(
    SPEND_BUDGET_CUSTOMER_COPY,
    "Publish a commercial spend budget, then wait.",
  );
  assert.equal(nextSpendBudgetActionCopy("wait"), "Wait");
  assert.equal(nextSpendBudgetActionCopy("unknown"), "Wait");
  assert.equal(statement.next_operator_action, "wait");
  assert.equal(statement.utilization_status, "under");
  assert.equal(statement.spend_budget_status, "published");
  assert.equal(typeof statement.budget_amount, "string");
  assert.equal(typeof statement.rated_amount, "string");
  assert.equal(typeof statement.remaining_amount, "string");
  assert.equal(typeof statement.over_amount, "string");
  assert.notEqual(typeof statement.budget_amount, "number");
  assert.match(renderTenantPin(statement), /urn:cwl:tenant_001/);
  assert.match(renderAmountDue({
    amount_due: statement.remaining_amount,
    currency_code: statement.currency_code,
  }), /99\.996295 USD/);
  assert.match(renderStatusChip({
    amount_due: "0",
    status_label: statement.utilization_status,
  }), /oc-status-chip--settled/);
  assert.ok(!("card_pan" in statement));
  assert.ok(!("retained_earnings" in statement));
  assert.ok(!("statutory_account_id" in statement));
  assert.ok(!("journal_entry_id" in statement));
});

test("published at budget shows exact-zero remaining and wait", () => {
  const statement = loadFixture("published_at_budget.json");
  const html = renderSpendBudget(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, />Wait</);
  assert.match(html, /oc-status-chip--settled/);
  assert.match(html, /at/);
  assert.equal(statement.utilization_status, "at");
  assert.equal(statement.remaining_amount, "0");
  assert.equal(statement.over_amount, "0");
  assert.equal(statement.next_operator_action, "wait");
  assert.equal(typeof statement.budget_amount, "string");
  assert.match(renderTenantPin(statement), /urn:cwl:tenant_001/);
  assert.match(renderStatusChip({
    amount_due: statement.remaining_amount,
    status_label: statement.utilization_status,
  }), /oc-status-chip--settled/);
  assert.ok(!("card_pan" in statement));
});

test("published over budget shows complementary over amount and wait", () => {
  const statement = loadFixture("published_over_budget.json");
  const html = renderSpendBudget(statement);
  assert.match(html, /0\.002705 USD/);
  assert.match(html, /0\.001 USD/);
  assert.match(html, />Wait</);
  assert.match(html, /oc-status-chip--due/);
  assert.match(html, /over/);
  assert.equal(statement.utilization_status, "over");
  assert.equal(statement.remaining_amount, "0");
  assert.equal(statement.over_amount, "0.002705");
  assert.equal(statement.next_operator_action, "wait");
  assert.equal(typeof statement.over_amount, "string");
  assert.match(renderTenantPin(statement), /urn:cwl:tenant_001/);
  assert.match(renderStatusChip({
    amount_due: statement.over_amount,
    status_label: statement.utilization_status,
  }), /oc-status-chip--due/);
  assert.ok(!("card_pan" in statement));
});

test("product-grouped morning rated spend shows exact amount and draft-invoice copy", () => {
  const statement = loadFixture("rated_spend_morning_product.json");
  const html = renderRatedSpend(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /contextual_orchestrator/);
  assert.match(html, /oc-status-chip--due/);
  assert.match(
    html,
    /Inspect rated product, project, credential, principal, or cost-center spend, then draft an invoice/,
  );
  assert.equal(
    RATED_SPEND_CUSTOMER_COPY,
    "Inspect rated product, project, credential, principal, or cost-center spend, then draft an invoice.",
  );
  assert.equal(statement.products[0].rated_amount, "0.003705");
  assert.equal(typeof statement.products[0].rated_amount, "string");
  assert.notEqual(typeof statement.products[0].rated_amount, "number");
  assert.ok(!("project_reference" in statement.products[0]));
  assert.ok(!("group_by" in statement));
  assert.ok(!("next_operator_action" in statement));
  assert.match(renderTenantPin(statement), /urn:cwl:tenant_001/);
  assert.match(renderAmountDue({
    amount_due: statement.products[0].rated_amount,
    currency_code: statement.products[0].currency_code,
  }), /0\.003705 USD/);
  assert.match(renderStatusChip({
    amount_due: statement.products[0].rated_amount,
    status_label: statement.products[0].product_code,
  }), /oc-status-chip--due/);
  assert.ok(!("card_pan" in statement));
  assert.ok(!("retained_earnings" in statement));
  assert.ok(!("statutory_account_id" in statement));
  assert.ok(!("journal_entry_id" in statement));
});

test("account budget status lists under, at, and over on one tenant and waits", () => {
  const statement = loadFixture("account_budget_status_under_over.json");
  const html = renderBudgetStatus(statement);
  assert.match(html, /99\.996295 USD/);
  assert.match(html, /100\.00 USD/);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /0\.002705 USD/);
  assert.match(html, /0\.001 USD/);
  assert.match(html, /oc-status-chip--settled/);
  assert.match(html, /oc-status-chip--due/);
  assert.match(html, /under/);
  assert.match(html, /at/);
  assert.match(html, /over/);
  assert.match(html, />Wait</);
  assert.match(html, /Open the account budget status, then wait/);
  assert.equal(BUDGET_STATUS_CUSTOMER_COPY, "Open the account budget status, then wait.");
  assert.equal(BUDGET_STATUS_TENANT_REFERENCE, "urn:cwl:tenant_001");
  assert.equal(statement.next_cursor, null);
  assert.equal(statement.budget_statuses.length, 3);
  assert.equal(statement.budget_statuses[0].utilization_status, "under");
  assert.equal(statement.budget_statuses[1].utilization_status, "at");
  assert.equal(statement.budget_statuses[2].utilization_status, "over");
  assert.equal(statement.budget_statuses[0].next_operator_action, "wait");
  assert.equal(typeof statement.budget_statuses[0].remaining_amount, "string");
  assert.notEqual(typeof statement.budget_statuses[0].remaining_amount, "number");
  assert.ok(!("tenant_reference" in statement));
  assert.ok(!("rated_amount" in statement));
  assert.ok(!("items" in statement));
  assert.ok(!("cursor" in statement));
  assert.match(html, /urn:cwl:tenant_001/);
  assert.match(renderTenantPin({ tenant_reference: BUDGET_STATUS_TENANT_REFERENCE }), /urn:cwl:tenant_001/);
  assert.match(renderAmountDue({
    amount_due: statement.budget_statuses[0].remaining_amount,
    currency_code: statement.budget_statuses[0].currency_code,
  }), /99\.996295 USD/);
  assert.match(renderStatusChip({
    amount_due: "0",
    status_label: statement.budget_statuses[0].utilization_status,
  }), /oc-status-chip--settled/);
  assert.match(renderStatusChip({
    amount_due: statement.budget_statuses[2].over_amount,
    status_label: statement.budget_statuses[2].utilization_status,
  }), /oc-status-chip--due/);
  assert.ok(!("card_pan" in statement));
  assert.ok(!("retained_earnings" in statement));
  assert.ok(!("statutory_account_id" in statement));
  assert.ok(!("journal_entry_id" in statement));
});

test("account budget status next_cursor page shows the keyset token without a money widget", () => {
  const statement = loadFixture("account_budget_status_next_cursor.json");
  const html = renderBudgetStatus(statement);
  assert.match(html, /99\.996295 USD/);
  assert.match(html, /2026-08-18T15:00:00Z\|019d7b92-1aa0-7a7f-b61c-962c0f4bfe02/);
  assert.match(html, /Next cursor/);
  assert.doesNotMatch(html, /Amount due<\/span><p class="oc-amount-due__value">2026-08-18T15:00:00Z/);
  assert.equal(statement.next_cursor, "2026-08-18T15:00:00Z|019d7b92-1aa0-7a7f-b61c-962c0f4bfe02");
  assert.equal(statement.budget_statuses.length, 2);
  assert.equal(statement.budget_statuses[0].utilization_status, "under");
  assert.equal(statement.budget_statuses[1].utilization_status, "at");
  assert.equal(typeof statement.budget_statuses[0].budget_amount, "string");
  assert.match(html, /urn:cwl:tenant_001/);
  assert.ok(!("card_pan" in statement));
});

test("first-over accepted observation shows exact over amount, enqueue, and wait", () => {
  const observation = loadFixture("accepted_over_signal.json");
  const outbox = loadFixture("pending_spend_budget_over.json");
  const html = renderSpendBudgetOver(observation, [outbox]);
  assert.match(html, /0\.002705 USD/);
  assert.match(html, /0\.001 USD/);
  assert.match(html, /oc-status-chip--due/);
  assert.match(html, /over/);
  assert.match(html, /accepted/);
  assert.match(html, /spend_budget\.over/);
  assert.match(html, /019d7b92-1aa0-7a7f-b61c-962c0f4bfe03/);
  assert.match(html, />Wait</);
  assert.match(html, /Observe the spend budget over signal, then wait/);
  assert.equal(
    SPEND_BUDGET_OVER_CUSTOMER_COPY,
    "Observe the spend budget over signal, then wait.",
  );
  assert.equal(SPEND_BUDGET_OVER_TENANT_REFERENCE, "urn:cwl:tenant_001");
  assert.equal(observation.spend_budget_over_signal_outcome_code, "accepted");
  assert.equal(observation.utilization_status, "over");
  assert.equal(observation.over_amount, "0.002705");
  assert.equal(observation.budget_amount, "0.001");
  assert.equal(observation.next_operator_action, "wait");
  assert.equal(typeof observation.over_amount, "string");
  assert.equal(typeof observation.budget_amount, "string");
  assert.notEqual(typeof observation.over_amount, "number");
  assert.equal(outbox.event_type_code, "spend_budget.over");
  assert.equal(outbox.source_id, observation.spend_budget_id);
  assert.equal(outbox.delivery_status, "pending");
  assert.match(html, /urn:cwl:tenant_001/);
  assert.match(
    renderTenantPin({ tenant_reference: SPEND_BUDGET_OVER_TENANT_REFERENCE }),
    /urn:cwl:tenant_001/,
  );
  assert.match(renderAmountDue({
    amount_due: observation.over_amount,
    currency_code: observation.currency_code,
  }), /0\.002705 USD/);
  assert.match(renderAmountDue({
    amount_due: observation.budget_amount,
    currency_code: observation.currency_code,
  }), /0\.001 USD/);
  assert.match(renderStatusChip({
    amount_due: observation.over_amount,
    status_label: observation.utilization_status,
  }), /oc-status-chip--due/);
  assert.ok(!("remaining_amount" in observation));
  assert.ok(!("rated_amount" in observation));
  assert.ok(!("card_pan" in observation));
  assert.ok(!("retained_earnings" in observation));
  assert.ok(!("statutory_account_id" in observation));
  assert.ok(!("journal_entry_id" in observation));
});

test("under observation waits with zero over-signal rows", () => {
  const observation = loadFixture("accepted_under_signal.json");
  const html = renderSpendBudgetOver(observation, []);
  assert.match(html, /100\.00 USD/);
  assert.match(html, /0 USD/);
  assert.match(html, /oc-status-chip--settled/);
  assert.match(html, /under/);
  assert.match(html, /accepted/);
  assert.match(html, />Wait</);
  assert.match(html, /Over-signal rows<\/span><p class="oc-invoice-statement__id">0<\/p>/);
  assert.doesNotMatch(html, /spend_budget\.over/);
  assert.equal(observation.utilization_status, "under");
  assert.equal(observation.over_amount, "0");
  assert.equal(observation.budget_amount, "100.00");
  assert.equal(observation.next_operator_action, "wait");
  assert.equal(typeof observation.over_amount, "string");
  assert.notEqual(typeof observation.over_amount, "number");
  assert.match(html, /urn:cwl:tenant_001/);
  assert.match(renderStatusChip({
    amount_due: observation.over_amount,
    status_label: observation.utilization_status,
  }), /oc-status-chip--settled/);
  const missingRows = renderSpendBudgetOver(observation);
  assert.match(missingRows, /Over-signal rows<\/span><p class="oc-invoice-statement__id">0<\/p>/);
  assert.doesNotMatch(missingRows, /spend_budget\.over/);
  assert.ok(!("remaining_amount" in observation));
  assert.ok(!("card_pan" in observation));
});

test("duplicate replay keeps first-over-wins outbox and current over evaluation", () => {
  const observation = loadFixture("duplicate_replay_over_signal.json");
  const outbox = loadFixture("pending_spend_budget_over.json");
  const firstOver = loadFixture("accepted_over_signal.json");
  const html = renderSpendBudgetOver(observation, [outbox]);
  assert.match(html, /0\.002705 USD/);
  assert.match(html, /duplicate_replay/);
  assert.match(html, /spend_budget\.over/);
  assert.match(html, /Over-signal rows<\/span><p class="oc-invoice-statement__id">1<\/p>/);
  assert.match(html, />Wait</);
  assert.equal(observation.spend_budget_id, firstOver.spend_budget_id);
  assert.equal(observation.over_amount, firstOver.over_amount);
  assert.equal(observation.budget_amount, firstOver.budget_amount);
  assert.equal(observation.utilization_status, "over");
  assert.equal(observation.spend_budget_over_signal_outcome_code, "duplicate_replay");
  assert.equal(outbox.source_id, firstOver.spend_budget_id);
  assert.equal(typeof observation.over_amount, "string");
  assert.match(html, /urn:cwl:tenant_001/);
  assert.ok(!("remaining_amount" in observation));
  assert.ok(!("card_pan" in observation));
});

test("empty account budget status still pins the commercial tenant and waits", () => {
  const html = renderBudgetStatus({ budget_statuses: [], next_cursor: null });
  assert.match(html, /urn:cwl:tenant_001/);
  assert.match(html, />Wait</);
  assert.match(html, /oc-status-chip--settled/);
  assert.doesNotMatch(html, /Next cursor/);
  const missing = renderBudgetStatus({ next_cursor: "" });
  assert.match(missing, /urn:cwl:tenant_001/);
  assert.doesNotMatch(missing, /Next cursor/);
});

test("project-grouped morning rated spend keeps the stored project URN unmixed", () => {
  const statement = loadFixture("rated_spend_morning_project.json");
  const html = renderRatedSpend(statement);
  assert.match(html, /0\.003705 USD/);
  assert.match(html, /urn:cwl:tenant_001:project:metering/);
  assert.match(html, /contextual_orchestrator/);
  assert.equal(statement.products[0].project_reference, "urn:cwl:tenant_001:project:metering");
  assert.equal(statement.products[0].rated_amount, "0.003705");
  assert.equal(typeof statement.products[0].rated_amount, "string");
  assert.ok(!("group_by" in statement));
  assert.ok(!("next_operator_action" in statement));
  assert.ok(!("credential_reference" in statement.products[0]));
  assert.match(renderTenantPin(statement), /urn:cwl:tenant_001/);
  assert.ok(!("card_pan" in statement));
});

test("float money fails closed", () => {
  assert.throws(() => renderAmountDue({ amount_due: 99.0, currency_code: "USD" }), TypeError);
  assert.throws(
    () => renderCollectionCase({ collection_outstanding: 100.0, currency_code: "USD" }),
    TypeError,
  );
  assert.throws(
    () => renderPaymentIntent({ payment_amount: 100.0, currency_code: "USD" }),
    TypeError,
  );
  assert.throws(
    () => renderPaymentReceipt({ received_amount: 100.0, remaining_outstanding_amount: "0.00" }),
    TypeError,
  );
  assert.throws(
    () => renderCreditAdjustment({ credit_amount: 11.0, currency_code: "USD" }),
    TypeError,
  );
  assert.throws(
    () => renderRateCard({ lines: [{ unit_amount: 0.000002, currency_code: "USD" }] }),
    TypeError,
  );
  assert.throws(
    () => renderUsageEvent({ measurements: [{ quantity: 1810, unit_code: "token" }] }),
    TypeError,
  );
  assert.throws(
    () => renderRatingRun({ rated_total_amount: 0.003705, currency_code: "USD" }),
    TypeError,
  );
  assert.throws(
    () => renderTaxAssessment({ tax_inclusive_amount: 110.0, currency_code: "USD" }),
    TypeError,
  );
  assert.throws(
    () => renderIssuedInvoice({ tax_inclusive_amount: 110.0, currency_code: "USD" }),
    TypeError,
  );
  assert.throws(
    () => renderIssuedCreditNote({ tax_inclusive_amount: 11.0, currency_code: "USD" }),
    TypeError,
  );
  assert.throws(
    () =>
      renderCreditNoteApplication({
        applied_amount: 0.003705,
        remaining_outstanding_amount: "0",
      }),
    TypeError,
  );
  assert.throws(
    () => renderCollectionCaseSettlement({ remaining_outstanding_amount: 0.0 }),
    TypeError,
  );
  assert.throws(
    () =>
      renderCollectionAging({
        currencies: [{ currency_code: "USD", current: { outstanding_amount: 1.25 } }],
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderAccountStatement({
        currencies: [{ currency_code: "USD", open_collection_remaining: 0.0, voided_invoice_total: "0" }],
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderSpendBudget({
        budget_amount: 100.0,
        rated_amount: "0.003705",
        remaining_amount: "99.996295",
        over_amount: "0",
        utilization_status: "under",
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderSpendBudget({
        budget_amount: "100.00",
        rated_amount: 0.003705,
        remaining_amount: "99.996295",
        over_amount: "0",
        utilization_status: "under",
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderSpendBudget({
        budget_amount: "100.00",
        rated_amount: "0.003705",
        remaining_amount: "99.996295",
        over_amount: "0",
        utilization_status: "unknown",
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderRatedSpend({
        products: [{ currency_code: "USD", product_code: "contextual_orchestrator", rated_amount: 0.003705 }],
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderBudgetStatus({
        budget_statuses: [{
          budget_amount: 100.0,
          rated_amount: "0.003705",
          remaining_amount: "99.996295",
          over_amount: "0",
          utilization_status: "under",
        }],
        next_cursor: null,
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderBudgetStatus({
        budget_statuses: [{
          budget_amount: "100.00",
          rated_amount: 0.003705,
          remaining_amount: "99.996295",
          over_amount: "0",
          utilization_status: "under",
        }],
        next_cursor: null,
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderBudgetStatus({
        budget_statuses: [{
          budget_amount: "100.00",
          rated_amount: "0.003705",
          remaining_amount: 99.996295,
          over_amount: "0",
          utilization_status: "under",
        }],
        next_cursor: null,
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderBudgetStatus({
        budget_statuses: [{
          budget_amount: "100.00",
          rated_amount: "0.003705",
          remaining_amount: "99.996295",
          over_amount: 0,
          utilization_status: "under",
        }],
        next_cursor: null,
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderBudgetStatus({
        budget_statuses: [{
          budget_amount: "100.00",
          rated_amount: "0.003705",
          remaining_amount: "99.996295",
          over_amount: "0",
          utilization_status: "unknown",
        }],
        next_cursor: null,
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderSpendBudgetOver({
        spend_budget_over_signal_outcome_code: "accepted",
        budget_amount: 0.001,
        over_amount: "0.002705",
        utilization_status: "over",
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderSpendBudgetOver({
        spend_budget_over_signal_outcome_code: "accepted",
        budget_amount: "0.001",
        over_amount: 0.002705,
        utilization_status: "over",
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderSpendBudgetOver({
        spend_budget_over_signal_outcome_code: "accepted",
        budget_amount: "0.001",
        over_amount: "0.002705",
        utilization_status: "unknown",
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderSpendBudgetOver({
        spend_budget_over_signal_outcome_code: "posted",
        budget_amount: "0.001",
        over_amount: "0.002705",
        utilization_status: "over",
      }),
    TypeError,
  );
  assert.throws(
    () =>
      renderSpendBudgetOver(
        {
          spend_budget_over_signal_outcome_code: "accepted",
          budget_amount: "0.001",
          over_amount: "0.002705",
          utilization_status: "over",
        },
        [{ event_type_code: "spend_budget.published", source_id: "019d7b92-1aa0-7a7f-b61c-962c0f4bfe03" }],
      ),
    TypeError,
  );
  assert.throws(
    () => renderLineTable({ invoice_lines: [{ line_number: 1, metric_code: "x", quantity: 1, unit_amount: "1.00", line_amount: "1.00" }] }),
    TypeError,
  );
});
