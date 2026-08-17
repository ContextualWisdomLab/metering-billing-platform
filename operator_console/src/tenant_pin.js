import { escapeHtml } from "./exact_decimal.js";

/**
 * Render the tokenized tenant pin.  The reference stays usable for invoicing.
 *
 * @param {{ tenant_reference: string }} statement
 * @returns {string}
 */
export function renderTenantPin(statement) {
  return (
    `<div class="oc-tenant-pin">` +
    `<span class="oc-tenant-pin__label">Tenant</span>` +
    `<span class="oc-tenant-pin__value">${escapeHtml(statement.tenant_reference)}</span>` +
    `</div>`
  );
}
