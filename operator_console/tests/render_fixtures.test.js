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

test("float money fails closed", () => {
  assert.throws(() => renderAmountDue({ amount_due: 99.0, currency_code: "USD" }), TypeError);
  assert.throws(
    () => renderCollectionCase({ collection_outstanding: 100.0, currency_code: "USD" }),
    TypeError,
  );
  assert.throws(
    () => renderLineTable({ invoice_lines: [{ line_number: 1, metric_code: "x", quantity: 1, unit_amount: "1.00", line_amount: "1.00" }] }),
    TypeError,
  );
});
