/** Exact-decimal helpers for presentment money.  Floats fail closed. */

export const EXACT_DECIMAL_PATTERN = /^(0|[1-9][0-9]*)(\.[0-9]+)?$/;

/**
 * Return the canonical decimal string, or throw when the value is inexact.
 *
 * @param {unknown} value
 * @param {string} fieldName
 * @returns {string}
 */
export function assertExactDecimalString(value, fieldName) {
  if (typeof value !== "string" || !EXACT_DECIMAL_PATTERN.test(value)) {
    throw new TypeError(`${fieldName} must be a canonical exact-decimal string`);
  }
  return value;
}

/**
 * Escape text for HTML attribute and element content.
 *
 * @param {unknown} value
 * @returns {string}
 */
export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
