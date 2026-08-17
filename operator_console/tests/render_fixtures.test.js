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
    () => renderLineTable({ invoice_lines: [{ line_number: 1, metric_code: "x", quantity: 1, unit_amount: "1.00", line_amount: "1.00" }] }),
    TypeError,
  );
});
