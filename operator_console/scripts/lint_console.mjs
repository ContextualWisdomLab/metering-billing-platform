#!/usr/bin/env node
/** Lint operator-console fixtures and tokens.  Float money fails closed. */

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { EXACT_DECIMAL_PATTERN } from "../src/exact_decimal.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const moneyFields = [
  "tax_exclusive_amount",
  "tax_amount",
  "tax_inclusive_amount",
  "credited_amount",
  "amount_due",
  "collection_outstanding",
  "payment_amount",
  "received_amount",
  "remaining_outstanding_amount",
  "credit_amount",
  "rated_total_amount",
  "tax_rate",
  "budget_amount",
  "rated_amount",
  "remaining_amount",
  "over_amount",
];
const currencyMoneyFields = [
  "issued_invoice_total",
  "voided_invoice_total",
  "open_collection_remaining",
  "applied_credit_total",
  "voided_credit_total",
  "write_off_total",
  "parked_unapplied_cash",
  "refunded_unapplied_cash",
];
const lineMoneyFields = ["quantity", "unit_amount", "line_amount"];

function fail(message) {
  console.error(message);
  process.exitCode = 1;
}

const tokens = JSON.parse(readFileSync(join(root, "tokens", "design_tokens.json"), "utf8"));
for (const groupName of ["color", "spacing", "type", "radius"]) {
  if (tokens[groupName] === undefined || Object.keys(tokens[groupName]).length === 0) {
    fail(`design tokens must include a non-empty ${groupName} group`);
  }
}

const fixturesDirectory = join(root, "fixtures");
for (const fileName of readdirSync(fixturesDirectory).filter((name) => name.endsWith(".json"))) {
  const payload = JSON.parse(readFileSync(join(fixturesDirectory, fileName), "utf8"));
  for (const fieldName of moneyFields) {
    if (!(fieldName in payload)) {
      continue;
    }
    const value = payload[fieldName];
    if (typeof value !== "string" || !EXACT_DECIMAL_PATTERN.test(value)) {
      fail(`${fileName}: ${fieldName} must be an exact-decimal string`);
    }
  }
  const currencies = Array.isArray(payload.currencies) ? payload.currencies : [];
  for (const [index, currency] of currencies.entries()) {
    for (const fieldName of currencyMoneyFields) {
      if (!(fieldName in currency)) {
        continue;
      }
      const value = currency[fieldName];
      if (typeof value !== "string" || !EXACT_DECIMAL_PATTERN.test(value)) {
        fail(`${fileName}: currencies[${index}].${fieldName} must be an exact-decimal string`);
      }
    }
  }
  const invoiceLines = Array.isArray(payload.invoice_lines) ? payload.invoice_lines : [];
  for (const [index, line] of invoiceLines.entries()) {
    for (const fieldName of lineMoneyFields) {
      const value = line[fieldName];
      if (typeof value !== "string" || !EXACT_DECIMAL_PATTERN.test(value)) {
        fail(`${fileName}: invoice_lines[${index}].${fieldName} must be an exact-decimal string`);
      }
    }
  }
  const catalogLines = Array.isArray(payload.lines) ? payload.lines : [];
  for (const [index, line] of catalogLines.entries()) {
    const value = line.unit_amount;
    if (value === undefined) {
      continue;
    }
    if (typeof value !== "string" || !EXACT_DECIMAL_PATTERN.test(value)) {
      fail(`${fileName}: lines[${index}].unit_amount must be an exact-decimal string`);
    }
  }
  const measurements = Array.isArray(payload.measurements) ? payload.measurements : [];
  for (const [index, measurement] of measurements.entries()) {
    const value = measurement.quantity;
    if (value === undefined) {
      continue;
    }
    if (typeof value !== "string" || !EXACT_DECIMAL_PATTERN.test(value)) {
      fail(`${fileName}: measurements[${index}].quantity must be an exact-decimal string`);
    }
  }
  const ratingLines = Array.isArray(payload.rating_lines) ? payload.rating_lines : [];
  for (const [index, line] of ratingLines.entries()) {
    for (const fieldName of ["rated_quantity", "unit_price_amount", "line_total_amount"]) {
      const value = line[fieldName];
      if (value === undefined) {
        continue;
      }
      if (typeof value !== "string" || !EXACT_DECIMAL_PATTERN.test(value)) {
        fail(`${fileName}: rating_lines[${index}].${fieldName} must be an exact-decimal string`);
      }
    }
  }
  const products = Array.isArray(payload.products) ? payload.products : [];
  for (const [index, product] of products.entries()) {
    const value = product.rated_amount;
    if (value === undefined) {
      continue;
    }
    if (typeof value !== "string" || !EXACT_DECIMAL_PATTERN.test(value)) {
      fail(`${fileName}: products[${index}].rated_amount must be an exact-decimal string`);
    }
  }
  const budgetStatuses = Array.isArray(payload.budget_statuses) ? payload.budget_statuses : [];
  for (const [index, row] of budgetStatuses.entries()) {
    for (const fieldName of ["budget_amount", "rated_amount", "remaining_amount", "over_amount"]) {
      const value = row[fieldName];
      if (value === undefined) {
        continue;
      }
      if (typeof value !== "string" || !EXACT_DECIMAL_PATTERN.test(value)) {
        fail(
          `${fileName}: budget_statuses[${index}].${fieldName} must be an exact-decimal string`,
        );
      }
    }
  }
  const issuedLines = Array.isArray(payload.issued_invoice_lines)
    ? payload.issued_invoice_lines
    : [];
  for (const [index, line] of issuedLines.entries()) {
    for (const fieldName of ["rated_quantity", "unit_price_amount", "line_total_amount"]) {
      const value = line[fieldName];
      if (value === undefined) {
        continue;
      }
      if (typeof value !== "string" || !EXACT_DECIMAL_PATTERN.test(value)) {
        fail(
          `${fileName}: issued_invoice_lines[${index}].${fieldName} must be an exact-decimal string`,
        );
      }
    }
  }
}

const sourceDirectory = join(root, "src");
for (const fileName of readdirSync(sourceDirectory).filter((name) => name.endsWith(".js"))) {
  const source = readFileSync(join(sourceDirectory, fileName), "utf8");
  if (source.includes("parseFloat") || source.includes("Number.parseFloat")) {
    fail(`${fileName}: presentment money must not use parseFloat`);
  }
}

if (process.exitCode) {
  process.exit(process.exitCode);
}
console.log("operator console lint passed");
