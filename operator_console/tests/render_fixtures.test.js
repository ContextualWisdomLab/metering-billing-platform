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
  assert.match(html, /Record the receipt, then drain or wait for AIS to pull the cash journal/);
  assert.equal(
    PAYMENT_RECEIPT_CUSTOMER_COPY,
    "Record the receipt, then drain or wait for AIS to pull the cash journal.",
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
    () => renderLineTable({ invoice_lines: [{ line_number: 1, metric_code: "x", quantity: 1, unit_amount: "1.00", line_amount: "1.00" }] }),
    TypeError,
  );
});
