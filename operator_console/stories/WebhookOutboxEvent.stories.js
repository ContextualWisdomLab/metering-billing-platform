import { renderWebhookOutboxEvent } from "../src/webhook_outbox_event.js";
import pendingJournalValidated from "../fixtures/pending_journal_validated.json";
import deliveredReceiptApplied from "../fixtures/delivered_receipt_applied.json";

export default {
  title: "WebhookOutboxEvent",
};

export const PendingJournalValidated = {
  render: () => renderWebhookOutboxEvent(pendingJournalValidated),
};

export const DeliveredReceiptApplied = {
  render: () => renderWebhookOutboxEvent(deliveredReceiptApplied),
};
