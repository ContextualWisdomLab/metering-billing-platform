# Data Model

## Initial normalized records

- `tenant_account`: tenant authority boundary.
- `billing_account`: commercial payer and invoice grouping.
- `billing_principal`: human, service, agent, workflow, or runtime attribution subject.
- `credential_record`: opaque fingerprint and issuer reference; no plaintext secret.
- `credential_assignment`: effective-dated link among credential, principal, and billing account.
- `meter_definition`: versioned unit and aggregation rule.
- `meter_quality_rule`: billable, analytics-only, or manual-review disposition by quality.
- `usage_event`: idempotent source fact identified by tenant-scoped `source_event_key` and by `(tenant_account_id, event_payload_hash, event_contract_version)`. The producer `event_id` is stored as `producer_event_id`, not as the internal primary key.
- `usage_measurement`: normalized meter quantity and quality, constrained to an explicit meter-specific quality rule.
- `usage_ingestion_receipt`: append-only accepted, replay, or rejected outcome for every ingest attempt, including rejected cross-tenant and schema failures.
- `rate_card`: tenant-scoped commercial price-book header identified by `(tenant_account_id, rate_card_name)`.
- `rate_card_version`: append-only published price list for one card. Identity is `(tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version)`.
- `rate_card_line`: exact flat `unit_amount` for one `metric_code` on one published version. Currency must match the version.
- `rating_run`: append-only invoice-intent total for one tenant, half-open window, rate card, and usage snapshot. Presentment projects a statement from this row; it does not add a snapshot table.
- `rating_line`: append-only invoice-intent line for one billing account and meter inside a rating run.
- `invoice_draft`: append-only draft-only commercial document for one tenant and rating run. Presentment projects a statement from this row plus tax, credit, and collection facts; it does not add a snapshot table. Draft status stays `draft` after commercial issue.
- `invoice_draft_line`: append-only draft line copied from a rating line.
- `issued_invoice`: append-only commercial invoice snapshot issued from one tenant draft. Identity is `(tenant_account_id, invoice_draft_id)`. `issued_invoice_id` is the opaque generated invoice identifier, not a statutory number.
- `issued_invoice_line`: append-only commercial line frozen from one invoice-draft line.
- `issued_invoice_void`: append-only commercial void of one unused issued invoice. Identity is `(tenant_account_id, issued_invoice_id)`. `issued_invoice_void_id` is the opaque generated identifier, not a statutory number.
- `journal_proposal`: append-only balanced accounting-journal proposal for one tenant invoice draft, payment receipt, credit adjustment, collection write-off, leftover refund, leftover apply, or issued-invoice void. AIS pulls these rows; query does not add a second table.
- `journal_proposal_line`: append-only debit-or-credit line using a semantic account role.
- `collection_case`: commercial collection case for one tenant invoice draft; receipts update outstanding and may mark the case settled; an unused issued-invoice void may mark the case `voided`.
- `collection_dunning_event`: append-only commercial reminder that does not capture money.
- `payment_intent`: provider-neutral payment initiation projection for one collection case; cancellation updates current status.
- `payment_receipt`: append-only commercial receipt applied against one projected payment intent.
- `posting_receipt_observation`: append-only commercial observation of one AIS posting receipt. AIS `receipt_id` is an external reference, not the internal primary key. AIS outbox drain reuses this table and does not add a drain row.
- `credit_adjustment`: append-only commercial credit against one tenant invoice draft. The paired journal proposal reuses `journal_proposal`.
- `tax_rate_schedule`: tenant-scoped tax-rate header identified by `(tenant_account_id, tax_code)`.
- `tax_rate_version`: append-only published tax rate. Identity is `(tenant_account_id, tax_rate_schedule_id, source_payload_hash, tax_rate_contract_version)`.
- `tax_assessment`: append-only commercial tax on one tenant invoice draft. `tax_inclusive_amount` drives collection outstanding and the AR journal debit when present.
- `tenant_api_credential`: append-only HTTP API credential for one tenant. Stores `credential_prefix` and a keyed `credential_secret_hash` only; never the plaintext secret. Status is `active` or `revoked`. Presentment is a metadata read of this row and never returns the hash or secret.
- `webhook_subscription`: tenant-scoped https callback. Stores `webhook_secret_prefix` and a keyed `webhook_secret_hash` only; never the plaintext secret. Status is `active` or `revoked`.
- `webhook_outbox_event`: append-only commercial fact (`journal_proposal.validated`, `payment_receipt.applied`, `credit_adjustment.recorded`, `invoice.issued`, `invoice.voided`, `credit_note.issued`, `credit_note.applied`, `collection.settled`, `write_off.recorded`, `unapplied_cash.applied`, `refund.recorded`, `dispute.held`, `dispute.released`) identified by `(tenant_account_id, event_type_code, source_id, payload_hash)`. Presentment is a metadata read of this row and never returns `payload_json` or the webhook secret. `GET /v1/webhook-outbox-events/{outbox_event_id}` projects one stored event. `GET /v1/webhook-outbox-events` lists `{webhook_outbox_events, next_cursor}` ordered by `enqueued_at` then `outbox_event_id`. This is the Billing commercial webhook outbox, not the AIS posting-receipt outbox.
- `webhook_delivery_attempt`: append-only POST attempt against one outbox event and subscription. Identity is `delivery_attempt_id`. Presentment is a read of this row plus the parent `webhook_outbox_event` event type and `source_id`; it never returns `payload_json` or the webhook secret.
- `provider_account`: provider and role registration.
- `provider_capability`: effective-dated supported capability.
- `provider_object_mapping`: provider-neutral internal-to-external mapping.
- `accounting_export_record`: proposal lifecycle and payload integrity.
- `outbox_event`: transactional publication record.

## Temporal rule

Assignments and capabilities use `valid_from`, `valid_to`, and `recorded_at`. Closing an interval supersedes a fact; it does not erase history. Composite foreign keys bind credentials, principals, billing accounts, and usage to the same tenant.

## Monetary rule

Database numeric values use exact `numeric` types. API amounts use canonical decimal strings. Binary floating-point types are forbidden for quantities that affect billing or accounting.

## Future extensions

Subsequent migrations add contracts, spend reservations, provider webhooks, disputes, and reconciliation exceptions without changing the initial identity, usage, rating-run, invoice-draft, issued-invoice, journal-proposal, collection-case, payment-intent, payment-receipt, posting-receipt-observation, credit-adjustment, rate-card-catalog, tax-assessment, credit-tax-unwind, tenant-api-credential, webhook-outbox, unapplied-cash, unapplied-cash-application, unapplied-cash-refund, refund-journal-proposal, leftover-journal-proposal, or apply-journal-proposal keys.

## Usage identity

A stored usage row is identified twice: by `(tenant_account_id, source_event_key)` and by `(tenant_account_id, event_payload_hash, event_contract_version)`.  Measurements remain in their own table and reference the event and meter definition.  Time-window reads filter `occurred_at` and never leak another tenant's rows.

## Rating identity

A stored rating run is identified by `(tenant_account_id, window_started_at, window_ended_at, rate_card_version_id, usage_snapshot_hash)`.  The run pins the published version so a later catalog publish cannot rewrite earlier invoice-intent money.  Lines reference the run, tenant, billing account, and meter definition.  Money columns use exact `numeric` types.

## Rate-card identity

A stored rate-card header is identified by `(tenant_account_id, rate_card_name)`.  Internal primary key is `rate_card_id`.  A stored version is identified by `(tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version)` and also by `(tenant_account_id, rate_card_id, version_number)`.  Lines reference the version and tenant, carry unique `metric_code` values, and store exact `unit_amount` values greater than zero.  A published version is never updated.

## Invoice-draft identity

A stored invoice draft is identified by `(tenant_account_id, rating_run_id)` and carries the rating run's `usage_snapshot_hash`.  Status is `draft` only.  Lines reference the draft, tenant, billing account, and meter definition.

## Journal-proposal identity

A stored journal proposal is identified by `(tenant_account_id, invoice_draft_id, source_payload_hash, proposal_contract_version)` for invoice-draft exports, by `(tenant_account_id, payment_receipt_id, source_payload_hash, proposal_contract_version)` for cash exports, by `(tenant_account_id, credit_adjustment_id)` for credit exports, by `(tenant_account_id, collection_write_off_id)` for write-off exports, by `(tenant_account_id, unapplied_cash_refund_id)` for leftover-refund exports, by `(tenant_account_id, unapplied_cash_id)` for leftover-park exports, and by `(tenant_account_id, unapplied_cash_application_id)` for leftover-apply exports.  Credit accept already writes that credit row; `POST /v1/credit-adjustments/{credit_adjustment_id}/journal-proposals` composes or replays it.  Lines reference the proposal and tenant, carry unique `line_number` values, and must balance in the transaction currency.  Untaxed draft lines use semantic `accounts_receivable` and `usage_revenue` roles.  Taxed draft lines add semantic `tax_payable`.  Cash lines use semantic `cash_receipt` and `accounts_receivable` roles.  Credit lines use semantic `usage_revenue` and `accounts_receivable` roles.  Write-off lines use semantic `write_off_expense` and `accounts_receivable` roles.  Refund lines use semantic `unapplied_cash` and `cash_receipt` roles.  Leftover-park lines use semantic `cash_receipt` and `unapplied_cash` roles.  Leftover-apply lines use semantic `unapplied_cash` and `accounts_receivable` roles.  Status is proposal-only.  Statutory account IDs and posted journals are not stored here.

## Collection-case identity

A stored collection case is identified by `(tenant_account_id, invoice_draft_id)`.  Outstanding starts as `tax_inclusive_amount` when a tax assessment exists, otherwise the exact invoice-draft total.  Status is `open` or `dunning` until applied receipts, commercial credits recorded against an already-open case, a later `credit_note_application`, or an explicit `collection_case_settlement` reduce outstanding to zero and mark the case `settled`.  An unused issued-invoice void may close the case as `voided` at exact-zero remaining; `settled` is not reused.  A commercial `collection_dispute` may hold an open or dunning case as `disputed` without changing remaining outstanding; `settled` and `voided` are not reused.  Dunning events reference the case and tenant, carry unique notice codes and event numbers, and never capture payment or post journals.  New dunning fails closed while the case is `disputed`.

## Payment-intent identity

A stored payment intent is identified by `(tenant_account_id, collection_case_id, source_payload_hash, payment_intent_contract_version)`.  The hash covers the case outstanding, currency, and stored status snapshot.  Status is `projected`, `cancelled`, or `rejected` only.  Provider charge IDs and card PAN are not stored.

## Payment-receipt identity

A stored payment receipt is identified by `(tenant_account_id, payment_intent_id, source_payload_hash, settlement_contract_version)`.  The hash covers the intent amount, currency, status, and received amount.  Status is `applied` only.  Provider charge IDs and card PAN are not stored.  Accept reuses the existing cash `journal_proposal` identity `(tenant_account_id, payment_receipt_id, source_payload_hash, proposal_contract_version)`.

## Posting-receipt observation identity

A stored posting-receipt observation is identified by `(tenant_account_id, idempotency_key)` plus `source_payload_hash` and AIS `receipt_id`.  Internal primary key is `posting_receipt_observation_id`.  `posting_status_code` is AIS-owned (`posted`, `held`, `rejected`, `reversed`) and is not mapped onto journal `proposal_status`.

## Credit-adjustment identity

A stored credit adjustment is identified by `(tenant_account_id, invoice_draft_id, source_payload_hash, credit_adjustment_contract_version)`.  Internal primary key is `credit_adjustment_id`.  The hash covers the draft, exact credit amount, closed reason, currency, and the tax split when the draft is taxed.  Status is `recorded` only.  Remaining adjustable is the tax-inclusive amount when an assessment exists, otherwise the draft total, minus prior accepted credits.  If a collection case exists, outstanding is reduced by the same inclusive amount and cannot go negative.  `tax_exclusive_amount` plus `tax_amount` equals `credit_amount`.  A taxed journal debits `usage_revenue` and `tax_payable` and credits `accounts_receivable`.

## Invoice-presentment projection

Presentment does not add a table.  `GET /v1/invoice-drafts/{invoice_draft_id}` projects `invoice_draft`, optional `tax_assessment`, accepted `credit_adjustment` rows, optional `collection_case`, and `invoice_draft_line` quantities.  `amount_due` is `max(0, tax_inclusive_or_draft_total - sum(credit_amount))`.

## Issued-invoice identity

A stored issued invoice is identified by `(tenant_account_id, invoice_draft_id)`.  Internal primary key is the opaque generated `issued_invoice_id`.  The hash covers the draft, contract version, rating run, usage snapshot, currency, exclusive/tax/inclusive totals, and issued lines.  Status is `issued` only.  `due_at` is optional.  The snapshot does not store a statutory invoice number, fiscal signature, or customer PII.  First successful issue appends one `webhook_outbox_event` with `event_type_code` `invoice.issued` and `source_id` `issued_invoice_id`.  The outbox `data` is a thin reference plus hash and omits issued lines.  `GET /v1/issued-invoices/{issued_invoice_id}` projects the stored row.  `GET /v1/issued-invoices` lists `{issued_invoices, next_cursor}` ordered by `issued_at` then `issued_invoice_id`.

## Issued-invoice-void identity

A stored issued-invoice void is identified by `(tenant_account_id, issued_invoice_id)`.  Internal primary key is the opaque generated `issued_invoice_void_id`.  The hash covers the issued invoice, draft, currency, inclusive amount, and contract version.  Status is `recorded` only.  Voided amount is the issued tax-inclusive amount.  Remaining outstanding after accept is exact zero.  One issued invoice voids at most once.  The issued snapshot stays `issued`.  First successful void appends one `webhook_outbox_event` with `event_type_code` `invoice.voided` and `source_id` `issued_invoice_void_id`.  The outbox `data` is a thin reference plus hash and omits remaining outstanding and collection-case status.  The void row invents no payment receipt, credit note, write-off, refund, settlement, or statutory number.  An unused open or dunning case closes as `voided`.  An explicit later compose appends one `journal_proposal` with `issued_invoice_void_id` and semantic `usage_revenue` / optional `tax_payable` / `accounts_receivable` lines.  The proposal binds the original invoice journal by Billing `proposal_id` / stored invoice-draft journal identity only.  `GET /v1/issued-invoice-voids/{issued_invoice_void_id}` projects the stored row plus current remaining outstanding.  `GET /v1/issued-invoice-voids` lists `{issued_invoice_voids, next_cursor}` ordered by `voided_at` then `issued_invoice_void_id`.

## Issued-credit-note identity

A stored issued credit note is identified by `(tenant_account_id, credit_adjustment_id)`.  Internal primary key is the opaque generated `issued_credit_note_id`.  The hash covers the credit, draft, optional issued invoice, versions, currency, exclusive/tax/inclusive credit totals, and the credit source hash.  Status is `issued` only.  `issued_invoice_id` is optional and stored only when already traceable.  The snapshot copies the closed `credit_reason_code` and invents no lines, statutory credit-note number, fiscal signature, or customer PII.  First successful issue appends one `webhook_outbox_event` with `event_type_code` `credit_note.issued` and `source_id` `issued_credit_note_id`.  The outbox `data` is a thin reference plus hash and omits lines.  `GET /v1/issued-credit-notes/{issued_credit_note_id}` projects the stored row.  `GET /v1/issued-credit-notes` lists `{issued_credit_notes, next_cursor}` ordered by `issued_at` then `issued_credit_note_id`.

## Credit-note-application identity

A stored credit-note application is identified by `(tenant_account_id, issued_credit_note_id)`.  Internal primary key is the opaque generated `credit_note_application_id`.  The hash covers the issued note, collection case, invoice draft, optional issued invoice, currency, exact applied amount, and both contract versions.  Status is `applied` only.  Applied amount is the issued tax-inclusive credit.  One issued credit note applies at most once.  The row invents no statutory credit-note number, journal, tax unwind, settlement, or payment receipt.  First successful apply appends one `webhook_outbox_event` with `event_type_code` `credit_note.applied` and `source_id` `credit_note_application_id`.  The outbox `data` is a thin reference plus hash and omits remaining outstanding because that amount is not stored on the application.  `GET /v1/credit-note-applications/{credit_note_application_id}` projects the stored row plus current remaining outstanding.  `GET /v1/credit-note-applications` lists `{credit_note_applications, next_cursor}` ordered by `applied_at` then `credit_note_application_id`.

## Collection-case-settlement identity

A stored collection-case settlement is identified by `(tenant_account_id, collection_case_id)`.  Internal primary key is the opaque generated `collection_case_settlement_id`.  The hash covers the case, invoice draft, optional issued invoice, currency, exact-zero remaining, and contract version.  Status is `settled` only.  Remaining outstanding is exact zero.  One case settles at most once through this command.  The row invents no payment receipt, write-off, journal, or tax unwind.  First successful settle appends one `webhook_outbox_event` with `event_type_code` `collection.settled` and `source_id` `collection_case_settlement_id`.  The outbox `data` is a thin reference plus hash.  `GET /v1/collection-case-settlements/{collection_case_settlement_id}` projects the stored row plus current remaining outstanding.  `GET /v1/collection-case-settlements` lists `{collection_case_settlements, next_cursor}` ordered by `settled_at` then `collection_case_settlement_id`.

## Collection-write-off identity

A stored collection dispute is identified by `(tenant_account_id, collection_case_id)`.  Internal primary key is the opaque generated `collection_dispute_id`.  The hash covers the case, invoice draft, optional issued invoice, currency, remaining snapshot, and contract version.  Status is `held` until an in-place release flips it to `released` and stores `released_at`.  Remaining outstanding is a snapshot of current remaining and is not changed.  One case holds at most once through this command.  Case status becomes `disputed` while held and returns to `open` or `dunning` on release.  First successful hold appends one `webhook_outbox_event` with `event_type_code` `dispute.held` and `source_id` `collection_dispute_id`.  First successful release appends one `webhook_outbox_event` with `event_type_code` `dispute.released` and the same `source_id`.  The outbox `data` is a thin reference plus hash and uses remaining outstanding at hold or release, not later-mutated case remaining.  The hold row invents no payment receipt, credit note, settlement, write-off, void, or journal.  A later hold of the same case fail-closes after release.

A stored collection write-off is identified by `(tenant_account_id, collection_case_id)`.  Internal primary key is the opaque generated `collection_write_off_id`.  The hash covers the case, invoice draft, optional issued invoice, currency, write-off amount, exact-zero remaining, and contract version.  Status is `recorded` only.  Write-off amount is the remaining inclusive amount at accept time.  Remaining outstanding after accept is exact zero.  One case writes off at most once through this command.  The write-off row invents no payment receipt, credit note, settlement, or tax unwind.  Case status stays `open` or `dunning`.  First successful write-off appends one `webhook_outbox_event` with `event_type_code` `write_off.recorded` and `source_id` `collection_write_off_id`.  The outbox `data` is a thin reference plus hash and uses stored exact-zero remaining, not later-mutated case remaining.  An explicit later compose appends one `journal_proposal` with `collection_write_off_id` and semantic `write_off_expense` / `accounts_receivable` lines.  `GET /v1/collection-write-offs/{collection_write_off_id}` projects the stored row plus current remaining outstanding.  `GET /v1/collection-write-offs` lists `{collection_write_offs, next_cursor}` ordered by `written_off_at` then `collection_write_off_id`.

## Unapplied-cash identity

A stored unapplied-cash row is identified by `(tenant_account_id, payment_receipt_id)`.  Internal primary key is the opaque generated `unapplied_cash_id`.  The hash covers the receipt, leftover amount, received and applied snapshots, currency, and contract version.  Status is `parked` only.  Leftover amount is a positive exact decimal that does not exceed the stored receipt.  One receipt parks leftover at most once.  The row invents no webhook, write-off, settlement, or credit note.  Receipt amount and case remaining stay unchanged.  An explicit later compose appends one `journal_proposal` with `unapplied_cash_id` and semantic `cash_receipt` / `unapplied_cash` lines.  Apply leftover through `unapplied_cash_application`.  `GET /v1/unapplied-cash/{unapplied_cash_id}` projects the stored row.  `GET /v1/unapplied-cash` lists `{unapplied_cash, next_cursor}` ordered by `parked_at` then `unapplied_cash_id`.

## Unapplied-cash-application identity

A stored unapplied-cash application is identified by `(tenant_account_id, unapplied_cash_id)`.  Internal primary key is the opaque generated `unapplied_cash_application_id`.  The hash covers the leftover, target case, receipt, currency, applied amount, parked amount snapshot, and contract version.  Status is `applied` only.  Applied amount is the full parked leftover.  One leftover applies at most once.  Outstanding is reduced by the exact applied inclusive amount.  Case status stays `open` or `dunning` even when remaining becomes exact zero.  The parked leftover row stays `parked`.  First successful apply appends one `webhook_outbox_event` with `event_type_code` `unapplied_cash.applied` and `source_id` `unapplied_cash_application_id`.  The outbox `data` is a thin reference plus hash and omits remaining outstanding because that amount is not stored on the application.  The row invents no write-off, settlement, or credit note.  An explicit later compose appends one `journal_proposal` with `unapplied_cash_application_id` and semantic `unapplied_cash` / `accounts_receivable` lines.  Apply fail-closes when a refund already exists.  `GET /v1/unapplied-cash-applications/{unapplied_cash_application_id}` projects the stored row plus current remaining outstanding.  `GET /v1/unapplied-cash-applications` lists `{unapplied_cash_applications, next_cursor}` ordered by `applied_at` then `unapplied_cash_application_id`.

## Unapplied-cash-refund identity

A stored unapplied-cash refund is identified by `(tenant_account_id, unapplied_cash_id)`.  Internal primary key is the opaque generated `unapplied_cash_refund_id`.  The hash covers the leftover, receipt, currency, refund amount, parked amount snapshot, and contract version.  Status is `recorded` only.  Refund amount is the full parked leftover.  One leftover refunds at most once.  The parked leftover row stays `parked`.  First successful refund appends one `webhook_outbox_event` with `event_type_code` `refund.recorded` and `source_id` `unapplied_cash_refund_id`.  The outbox `data` is a thin reference plus hash and omits payment-intent id, collection-case id, parked leftover snapshot, and leftover status.  An explicit later compose appends one `journal_proposal` with `unapplied_cash_refund_id` and semantic `unapplied_cash` / `cash_receipt` lines.  The row invents no write-off, settlement, credit note, or PSP capture.  `GET /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}` projects the stored row plus current leftover status.  `GET /v1/unapplied-cash-refunds` lists `{unapplied_cash_refunds, next_cursor}` ordered by `refunded_at` then `unapplied_cash_refund_id`.

## Collection-case-presentment projection

Collection presentment does not add a table.  `GET /v1/collection-cases/{collection_case_id}` projects stored `collection_case` and `collection_dunning_event` rows plus accepted credits on the same draft.  `collection_outstanding` is the exact stored outstanding.  `collection_case_status` stays `open`, `dunning`, `settled`, `voided`, or `disputed`.  Next operator action is `collect`, `credit`, or `wait`.  Collection aging presentment also does not add a table.  `GET /v1/collection-aging` projects stored `collection_case` remaining into current / 1-30 / 31-60 / 61-90 / 90+ buckets grouped by `currency_code`.  Due date is issued-invoice `due_at` when stored, otherwise `collection_case.opened_at`.  Settled cases and exact-zero remaining are omitted.  Account-statement presentment also does not add a table.  `GET /v1/billing-accounts/{billing_account_id}/statement` projects stored issued-invoice totals, open collection remaining, applied credit-note amounts, write-offs, unused parked leftover, and leftover refunds grouped by `currency_code`.  Attribution uses invoice-draft lines exclusive to that billing account.  Dunning-event presentment also does not add a table.  `GET /v1/dunning-events/{dunning_event_id}` projects one stored `collection_dunning_event`.  `GET /v1/dunning-events` lists `{dunning_events, next_cursor}` ordered by `occurred_at` then `collection_dunning_event_id`.

## Payment-intent-presentment projection

Payment-intent presentment does not add a table.  `GET /v1/payment-intents/{payment_intent_id}` projects stored `payment_intent` rows.  `payment_amount` is the exact stored amount.  `payment_intent_status` stays `projected`, `cancelled`, or `rejected`.  Next operator action is `record_receipt` or `wait`.

## Payment-receipt-presentment projection

Credit-adjustment presentment does not add a table.  `GET /v1/credit-adjustments/{credit_adjustment_id}` projects stored `credit_adjustment` rows.  `credit_amount`, `tax_exclusive_amount`, and `tax_amount` are the exact stored amounts.  `credit_adjustment_status` stays `recorded`.  Next operator action is `wait`.

Rate-card presentment does not add a table.  `GET /v1/rate-cards/{rate_card_id}` projects stored `rate_card` and latest `rate_card_version` rows.  `unit_amount` values are the exact stored prices.  Next operator action is `rate_window`.

Usage-event presentment does not add a table.  `GET /v1/usage-events/{usage_event_id}` projects stored `usage_event` rows.  Measurement quantities are the exact stored amounts.  Next operator action is `rate_window`.

Rating-run presentment does not add a table.  `GET /v1/rating-runs/{rating_run_id}` projects stored `rating_run` rows.  `rated_total_amount` and line amounts are the exact stored amounts.  Next operator action is `draft_invoice`.

Tax-assessment presentment does not add a table.  `GET /v1/tax-assessments/{tax_assessment_id}` stays the existing #19 item read of stored `tax_assessment` rows.  `GET /v1/tax-assessments` lists summaries from those same rows.  `tax_exclusive_amount`, `tax_amount`, and `tax_inclusive_amount` are the exact stored amounts.  Next operator action is `propose_journal`.

Posting-receipt observation presentment does not add a table.  `GET /v1/posting-receipt-observations/{idempotency_key}` stays the existing #16 item read of stored `posting_receipt_observation` rows.  `GET /v1/posting-receipt-observations` lists summaries from those same rows.  `posting_status_code` is the exact stored AIS status.  Next operator action is `wait`.  `proposal_status` is not projected.

Payment-receipt presentment does not add a table.  `GET /v1/payment-receipts/{payment_receipt_id}` projects stored `payment_receipt` rows and the current `collection_case`.  `received_amount` is the exact stored amount.  `remaining_outstanding_amount` is the current case outstanding.  `payment_receipt_status` stays `applied`.  Next operator action is `record_receipt` or `drain_or_wait`.

## Tax-assessment identity

A stored tax-rate schedule is identified by `(tenant_account_id, tax_code)`.  A stored version is identified by `(tenant_account_id, tax_rate_schedule_id, source_payload_hash, tax_rate_contract_version)` and also by `(tenant_account_id, tax_rate_schedule_id, version_number)`.  A stored assessment is identified by `(tenant_account_id, invoice_draft_id, tax_rate_version_id, source_payload_hash, tax_assessment_contract_version)` and is unique per draft.  `tax_amount` is half-even rounded to the documented ISO 4217 minor-unit exponent.

## Tenant-API-credential identity

A stored tenant API credential is identified by `tenant_api_credential_id` and is unique on `credential_secret_hash`.  Internal primary key is `tenant_api_credential_id`.  The hash is `hmac-sha256:` plus HMAC-SHA256(pepper, secret).  The plaintext secret is never stored.  `credential_label` is two-or-more-word `snake_case`.  Status is `active` or `revoked`.  A second issue of the same tenant, label, and contract version inserts a new row with a new secret.  Revocation updates `credential_status` and `revoked_at` on the same row and does not delete history.

## Webhook-outbox identity

A stored webhook subscription is identified by `(tenant_account_id, callback_url, event_type_set, webhook_subscription_contract_version)`.  Internal primary key is `webhook_subscription_id`.  The hash is `hmac-sha256:` plus HMAC-SHA256(pepper, secret).  The plaintext secret is never stored in SQL.  Status is `active` or `revoked`.  HTTP presentment projects metadata only and never returns the secret, hash, prefix, or signed body.  A stored outbox event is identified by `(tenant_account_id, event_type_code, source_id, payload_hash)`.  Delivery attempts are unique on `(outbox_event_id, webhook_subscription_id, attempt_number)` and never update a prior attempt.
