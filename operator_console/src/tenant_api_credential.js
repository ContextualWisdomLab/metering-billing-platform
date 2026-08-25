import { escapeHtml } from "./exact_decimal.js";
import { renderStatusChip } from "./status_chip.js";
import { renderTenantPin } from "./tenant_pin.js";

export const TENANT_API_CREDENTIAL_CUSTOMER_COPY =
  "Issue a key, then send it on every /v1 call; revoke when leaked.";

/**
 * Return the operator-facing next-action copy for one credential.
 *
 * @param {string} action
 * @returns {string}
 */
export function nextOperatorActionCopy(action) {
  if (action === "wait") {
    return "Wait";
  }
  if (action === "issue") {
    return "Issue a key";
  }
  return "Issue a key";
}

/**
 * Render one tenant API credential presentment for the operator console.
 *
 * @param {Record<string, unknown>} statement
 * @returns {string}
 */
export function renderTenantApiCredential(statement) {
  const action = String(statement.next_operator_action ?? "");
  return (
    `<article class="oc-tenant-api-credential">` +
    `<header class="oc-invoice-statement__header">` +
    `<div>` +
    `<h1 class="oc-invoice-statement__title">Tenant API credential</h1>` +
    `<p class="oc-invoice-statement__id">${escapeHtml(statement.tenant_api_credential_id)}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(statement.credential_prefix)}</p>` +
    `</div>` +
    `${renderStatusChip({
      amount_due: "0",
      status_label: statement.credential_status,
    })}` +
    `</header>` +
    `${renderTenantPin(statement)}` +
    `<section class="oc-invoice-statement__action">` +
    `<span class="oc-invoice-statement__action-label">Next operator action</span>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(nextOperatorActionCopy(action))}</p>` +
    `<p class="oc-invoice-statement__action-copy">${escapeHtml(TENANT_API_CREDENTIAL_CUSTOMER_COPY)}</p>` +
    `</section>` +
    `</article>`
  );
}
