# Data Model

## Initial normalized records

- `tenant_account`: tenant authority boundary.
- `billing_account`: commercial payer and invoice grouping.
- `billing_principal`: human, service, agent, workflow, or runtime attribution subject.
- `credential_record`: opaque fingerprint and issuer reference; no plaintext secret.
- `credential_assignment`: effective-dated link among credential, principal, and billing account. Intervals for one credential are half-open and non-overlapping; adjacent assignments are valid.
- `meter_definition`: versioned unit and aggregation rule.
- `meter_quality_rule`: billable, analytics-only, or manual-review disposition by quality.
- `usage_event`: idempotent source fact identified by tenant-scoped `source_event_key` and by `(tenant_account_id, event_payload_hash, event_contract_version)`. The producer `event_id` is stored as `producer_event_id`, not as the internal primary key.
- `usage_measurement`: normalized meter quantity and quality, constrained to an explicit meter-specific quality rule.
- `usage_ingestion_receipt`: append-only accepted, replay, or rejected outcome for every ingest attempt, including rejected cross-tenant and schema failures.
- `rate_card`: tenant-scoped commercial price-book header identified by `(tenant_account_id, rate_card_name)`.
- `rate_card_version`: append-only published price list for one card. Identity is `(tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version)`.
- `rate_card_line`: exact flat `unit_amount` for one `metric_code` on one published version. Currency must match the version.
- `rating_run`: append-only invoice-intent total for one tenant, half-open window, rate-card header plus pinned version number, and usage snapshot. Presentment projects a statement from this row; it does not add a snapshot table.
- `rating_line`: append-only invoice-intent line for one billing account and meter inside a rating run.
- `invoice_draft`: append-only draft-only commercial document for one tenant and rating run. Presentment projects a statement from this row plus tax, credit, and collection facts; it does not add a snapshot table. Draft status stays `draft` after commercial issue.
- `invoice_draft_line`: append-only draft line copied from a rating line.
- `issued_invoice`: append-only commercial invoice snapshot issued from one tenant draft. Identity is `(tenant_account_id, invoice_draft_id)`. `issued_invoice_id` is the opaque generated invoice identifier, not a statutory number.
- `issued_invoice_line`: append-only commercial line frozen from one invoice-draft line.
- `issued_invoice_void`: append-only commercial void of one unused issued invoice. Identity is `(tenant_account_id, issued_invoice_id)`. `issued_invoice_void_id` is the opaque generated identifier, not a statutory number.
- `journal_proposal`: append-only balanced accounting-journal proposal for one tenant invoice draft, payment receipt, credit adjustment, collection write-off, leftover refund, leftover apply, issued-invoice void, or issued-credit-note void. AIS pulls these rows; query does not add a second table. `PostgresUsageLedger` persists invoice-draft, cash, credit, write-off, leftover, leftover-apply, leftover-refund, unused invoice-void, and unused credit-note-void journals so GET presentment survives process restart. `operator_console` Storybook presents one validated morning cash journal with exact received amount.
- `journal_proposal_line`: append-only debit-or-credit line using a semantic account role. Amounts are Exact Decimal. Compose and insert fail closed when a debit or credit cannot be represented with six fractional digits without changing the value; trailing zeros that do not change the value remain postable.
- `collection_case`: commercial collection case for one tenant invoice draft; receipts update outstanding and may mark the case settled; an unused issued-invoice void may mark the case `voided`.
- `collection_dunning_event`: append-only commercial reminder that does not capture money.
- `payment_intent`: provider-neutral payment initiation projection for one collection case; cancellation updates current status.
- `payment_receipt`: append-only commercial receipt applied against one projected payment intent.
- `posting_receipt_observation`: append-only commercial observation of one AIS posting receipt. AIS `receipt_id` is an external reference, not the internal primary key. AIS outbox drain reuses this table and does not add a drain row.
- `credit_adjustment`: append-only commercial credit against one tenant invoice draft. The paired journal proposal reuses `journal_proposal`.
- `issued_credit_note`: append-only commercial credit-note snapshot issued from one tenant credit. Identity is `(tenant_account_id, credit_adjustment_id)`. Internal primary key is `issued_credit_note_id`. Status is `issued` only. `PostgresUsageLedger` persists the issued row; statutory numbers, credit-note lines, and `tax_assessment_id` are not stored on the snapshot.
- `issued_credit_note_void`: append-only commercial void of one unused issued credit note. Identity is `(tenant_account_id, issued_credit_note_id)`. Internal primary key is `issued_credit_note_void_id`. Status is `recorded` only. `PostgresUsageLedger` persists the unused void; statutory numbers and journals are not stored on the void.
- `credit_note_application`: append-only application of one issued credit note onto one open same-tenant collection case. Identity is `(tenant_account_id, issued_credit_note_id)`. Internal primary key is `credit_note_application_id`. Status is `applied` only. `PostgresUsageLedger` persists the applied row; remaining outstanding and journals are not stored on the application.
- `spend_budget`: append-only commercial budget for one tenant billing account, half-open window, and currency. Identity is `(tenant_account_id, billing_account_id, window_started_at, window_ended_at, currency_code, source_payload_hash, spend_budget_contract_version)`. Internal primary key is `spend_budget_id`. Status is `published` only and is stored as `spend_budget_status`. `PostgresUsageLedger` persists the published row; evaluation snapshots, utilization, and remaining/over are not stored. The row is not a spend reservation.
- `tax_rate_schedule`: tenant-scoped tax-rate header identified by `(tenant_account_id, tax_code)`.
- `tax_rate_version`: append-only published tax rate. Identity is `(tenant_account_id, tax_rate_schedule_id, source_payload_hash, tax_rate_contract_version)`.
- `tax_assessment`: append-only commercial tax on one tenant invoice draft. `tax_inclusive_amount` drives collection outstanding and the AR journal debit when present.
- `tenant_api_credential`: append-only HTTP API credential for one tenant. Stores `credential_prefix` and a keyed `credential_secret_hash` only; never the plaintext secret. Status is `active` or `revoked`. Presentment is a metadata read of this row and never returns the hash or secret.
- `webhook_subscription`: tenant-scoped https callback. Stores `webhook_secret_prefix` and a keyed `webhook_secret_hash` only; never the plaintext secret. The one-time secret is process-local for delivery. Status is `active` or `revoked`.
- `webhook_outbox_event`: append-only commercial fact (`journal_proposal.validated`, `payment_receipt.applied`, `credit_adjustment.recorded`, `invoice.issued`, `invoice.voided`, `credit_note.issued`, `credit_note.voided`, `credit_note.applied`, `collection.settled`, `write_off.recorded`, `unapplied_cash.applied`, `refund.recorded`, `dispute.held`, `dispute.released`, `spend_budget.published`, `spend_budget.over`, `spend_budget.approaching`) identified by `(tenant_account_id, event_type_code, source_id, payload_hash)`. Presentment is a metadata read of this row and never returns `payload_json` or the webhook secret. `GET /v1/webhook-outbox-events/{outbox_event_id}` projects one stored event. `GET /v1/webhook-outbox-events` lists `{webhook_outbox_events, next_cursor}` ordered by `enqueued_at` then `outbox_event_id`. This is the Billing commercial webhook outbox, not the AIS posting-receipt outbox.
- `webhook_delivery_attempt`: append-only POST attempt against one outbox event and subscription. Identity is `delivery_attempt_id`; PostgreSQL derives the tenant key from the parent outbox row and enforces composite foreign keys. Presentment is a read of this row plus the parent `webhook_outbox_event` event type and `source_id`; it never returns `payload_json` or the webhook secret.
- `provider_account`: provider and role registration.
- `provider_capability`: effective-dated supported capability.
- `provider_object_mapping`: provider-neutral internal-to-external mapping.
- `accounting_export_record`: proposal lifecycle and payload integrity. `proposal_reference` is unique within a tenant.
- `outbox_event`: transactional publication record.

## Temporal rule

Assignments and capabilities use `valid_from`, `valid_to`, and `recorded_at`. Closing an interval supersedes a fact; it does not erase history. Composite foreign keys bind credentials, principals, billing accounts, and usage to the same tenant. PostgreSQL migration `0036` enforces the credential half-open non-overlap rule with an exclusion constraint and keeps proposal references unique per tenant. Migration `0037` stores tenant and credential URNs so a future durable resolver can preserve the same identity as the reference ledger; existing rows are backfilled deterministically. Migration `0038` adds canonical billing-account and meter references to rating and invoice lines, pins rating-run version numbers, and carries the durable path through collection cases, payment receipts, credit adjustments, collection write-offs, exact-zero collection settlements, cash/credit proposals, and webhook delivery metadata. Migration `0039` stores `spend_budget_status` as `published` only on the existing tenant-scoped `spend_budget` row so a published commercial budget survives process restart.

## Monetary rule

Database numeric values use exact `numeric` types. API amounts use canonical decimal strings. Binary floating-point types are forbidden for quantities that affect billing or accounting.

## Future extensions

Subsequent migrations add contracts, spend reservations, provider webhooks, disputes, and reconciliation exceptions without changing the initial identity, usage, rating-run, invoice-draft, issued-invoice, journal-proposal, collection-case, payment-intent, payment-receipt, posting-receipt-observation, credit-adjustment, spend-budget, rate-card-catalog, tax-assessment, credit-tax-unwind, tenant-api-credential, webhook-outbox, unapplied-cash, unapplied-cash-application, unapplied-cash-refund, refund-journal-proposal, leftover-journal-proposal, or apply-journal-proposal keys. A published `spend_budget` is a commercial control fact, not a reservation.

## Usage identity

A stored usage row is identified twice: by `(tenant_account_id, source_event_key)` and by `(tenant_account_id, event_payload_hash, event_contract_version)`.  Measurements remain in their own table and reference the event and meter definition.  Time-window reads filter `occurred_at` and never leak another tenant's rows.

## Rating identity

A stored rating run is identified by `(tenant_account_id, window_started_at, window_ended_at, rate_card_id, rate_card_version, usage_snapshot_hash)`.  The run pins the published version so a later catalog publish cannot rewrite earlier invoice-intent money.  Lines reference the run, tenant, billing account, canonical billing-account reference, and meter definition/code/unit.  Money columns use exact `numeric` types.

## Rate-card identity

A stored rate-card header is identified by `(tenant_account_id, rate_card_name)`.  Internal primary key is `rate_card_id`.  A stored version is identified by `(tenant_account_id, rate_card_id, source_payload_hash, rate_card_contract_version)` and also by `(tenant_account_id, rate_card_id, version_number)`.  Lines reference the version and tenant, carry unique `metric_code` values, and store exact `unit_amount` values greater than zero.  A published version is never updated.

## Invoice-draft identity

A stored invoice draft is identified by `(tenant_account_id, rating_run_id)` and carries the rating run's `usage_snapshot_hash`.  Status is `draft` only.  Lines reference the draft, tenant, billing account, and meter definition.

## Journal-proposal identity

A stored journal proposal is identified by `(tenant_account_id, invoice_draft_id, source_payload_hash, proposal_contract_version)` for invoice-draft exports, by `(tenant_account_id, payment_receipt_id, source_payload_hash, proposal_contract_version)` for cash exports, by `(tenant_account_id, credit_adjustment_id)` for credit exports, by `(tenant_account_id, collection_write_off_id)` for write-off exports, by `(tenant_account_id, unapplied_cash_refund_id)` for leftover-refund exports, by `(tenant_account_id, unapplied_cash_id)` for leftover-park exports, by `(tenant_account_id, unapplied_cash_application_id)` for leftover-apply exports, by `(tenant_account_id, issued_invoice_void_id)` for issued-invoice-void exports, and by `(tenant_account_id, issued_credit_note_void_id)` for issued-credit-note-void exports.  `PostgresUsageLedger` persists the invoice-draft journal so GET presentment survives process restart.  Replay of the same tenant, draft, source hash, and contract version does not insert a second row.  A crash after insert and before the existing `journal_proposal.validated` outbox enqueue is healed by the next replay.  Credit accept already writes that credit row; `POST /v1/credit-adjustments/{credit_adjustment_id}/journal-proposals` composes or replays it.  Lines reference the proposal and tenant, carry unique `line_number` values, and must balance in the transaction currency.  Untaxed draft lines use semantic `accounts_receivable` and `usage_revenue` roles.  Taxed draft lines add semantic `tax_payable`.  Cash lines use semantic `cash_receipt` and `accounts_receivable` roles.  Credit lines use semantic `usage_revenue` and `accounts_receivable` roles.  Write-off lines use semantic `write_off_expense` and `accounts_receivable` roles.  Refund lines use semantic `unapplied_cash` and `cash_receipt` roles.  Leftover-park lines use semantic `cash_receipt` and `unapplied_cash` roles.  Leftover-apply lines use semantic `unapplied_cash` and `accounts_receivable` roles.  Issued-invoice-void lines use semantic `usage_revenue`, optional `tax_payable`, and `accounts_receivable` roles.  Issued-credit-note-void lines use semantic `accounts_receivable`, `usage_revenue`, and optional `tax_payable` roles.  Status is proposal-only.  Statutory account IDs and posted journals are not stored here.

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

## Spend-budget identity

A stored spend budget is identified by `(tenant_account_id, billing_account_id, window_started_at, window_ended_at, currency_code, source_payload_hash, spend_budget_contract_version)`.  Internal primary key is the opaque generated `spend_budget_id`.  The hash covers the billing account, ISO 4217 currency, exact `budget_amount` greater than zero, UTC window instants, and contract version.  Status is `published` only and is persisted as `spend_budget_status`.  A later distinct amount or hash for the same account, window, and currency inserts a new row.  A published budget is never updated.  `PostgresUsageLedger` writes the published row with tenant-scoped composite foreign keys and exact `numeric` amounts.  Replay of the same identity does not insert a second row.  Rejected publish writes zero rows.  First successful publish appends one `webhook_outbox_event` with `event_type_code` `spend_budget.published` and `source_id` `spend_budget_id`.  The outbox `data` is a thin reference plus hash and omits rated spend, remaining/over, and utilization.  The row invents no rated-spend comparison, journal, reservation, or statutory number.  `GET /v1/spend-budgets/{spend_budget_id}` projects the stored row.  `GET /v1/spend-budgets` lists `{spend_budgets, next_cursor}` ordered by `published_at` then `spend_budget_id`.  Spend-budget evaluation presentment also does not add a table.  `GET /v1/spend-budgets/{spend_budget_id}/evaluation` projects the stored `spend_budget` against already-rated exclusive-account spend for the same window and currency.  `remaining_amount` and `over_amount` are complementary non-negative exact amounts.  `utilization_status` stays `under`, `at`, or `over`.  The read writes no evaluation row.  First observation with `utilization_status=over` appends one `webhook_outbox_event` with `event_type_code` `spend_budget.over` and `source_id` `spend_budget_id`.  The over envelope `data` is a thin reference plus hash and exact `over_amount`.  Remaining and rated lines are omitted.  Replay of the same source does not insert a second over row.  under and at write zero over-signal rows.  The write does not persist an evaluation snapshot.  `GET /v1/spend-budgets/{spend_budget_id}/over-signal` projects the live over-signal envelope plus zero or one stored `spend_budget.over` webhook-outbox presentment.  The read writes no outbox row.  First observation with `utilization_status=at` the documented `budget_amount` appends one `webhook_outbox_event` with `event_type_code` `spend_budget.approaching` and `source_id` `spend_budget_id`.  The approaching envelope `data` is a thin reference plus hash and exact `remaining_amount`.  Over and rated lines are omitted.  Replay of the same source does not insert a second approaching row.  under and over write zero approaching-signal rows.  The write does not persist an evaluation snapshot.  `GET /v1/spend-budgets/{spend_budget_id}/approaching-signal` projects the live approaching-signal envelope plus zero or one stored `spend_budget.approaching` webhook-outbox presentment.  The read writes no outbox row.

## Invoice-presentment projection

Presentment does not add a table.  `GET /v1/invoice-drafts/{invoice_draft_id}` projects `invoice_draft`, optional `tax_assessment`, accepted `credit_adjustment` rows, optional `collection_case`, and `invoice_draft_line` quantities.  `amount_due` is `max(0, tax_inclusive_or_draft_total - sum(credit_amount))`.

## Issued-invoice identity

A stored issued invoice is identified by `(tenant_account_id, invoice_draft_id)`.  Internal primary key is the opaque generated `issued_invoice_id`.  The hash covers the draft, contract version, rating run, usage snapshot, currency, exclusive/tax/inclusive totals, and issued lines.  Status is `issued` only.  `due_at` is optional.  The snapshot does not store a statutory invoice number, fiscal signature, or customer PII.  First successful issue appends one `webhook_outbox_event` with `event_type_code` `invoice.issued` and `source_id` `issued_invoice_id`.  The outbox `data` is a thin reference plus hash and omits issued lines.  Issued-invoice and presentment line envelopes are version 2; presentment upgrades a stored historical v1 line snapshot to the v2 envelope without rewriting the stored invoice.  Issuance preserves exact representable totals before the `numeric(38,12)` check and caps the projected issued lines at 10,000.  `GET /v1/issued-invoices/{issued_invoice_id}` projects the stored row and, when a stored `tax_assessment` for the same draft still matches the frozen exclusive/tax/inclusive amounts, optional `tax_assessment_id`.  The issued row does not persist that identifier.  `GET /v1/issued-invoices` lists `{issued_invoices, next_cursor}` ordered by `issued_at` then `issued_invoice_id`.

## Issued-invoice-void identity

A stored issued-invoice void is identified by `(tenant_account_id, issued_invoice_id)`.  Internal primary key is the opaque generated `issued_invoice_void_id`.  The hash covers the issued invoice, draft, currency, inclusive amount, and contract version.  Status is `recorded` only.  Voided amount is the issued tax-inclusive amount.  Remaining outstanding after accept is exact zero.  One issued invoice voids at most once.  The issued snapshot stays `issued`.  First successful void appends one `webhook_outbox_event` with `event_type_code` `invoice.voided` and `source_id` `issued_invoice_void_id`.  The outbox `data` is a thin reference plus hash and omits remaining outstanding and collection-case status.  The void row invents no payment receipt, credit note, write-off, refund, settlement, or statutory number.  An unused open or dunning case closes as `voided`.  An explicit later compose appends one `journal_proposal` with `issued_invoice_void_id` and semantic `usage_revenue` / optional `tax_payable` / `accounts_receivable` lines.  `PostgresUsageLedger` writes that unused invoice-void journal with tenant-scoped composite foreign keys and exact `numeric` amounts.  Replay of the same tenant and unused invoice-void does not insert a second row.  The proposal binds the original invoice journal by Billing `proposal_id` / stored invoice-draft journal identity only.  `GET /v1/issued-invoice-voids/{issued_invoice_void_id}` projects the stored row plus current remaining outstanding.  `GET /v1/issued-invoice-voids` lists `{issued_invoice_voids, next_cursor}` ordered by `voided_at` then `issued_invoice_void_id`.  `operator_console` Storybook presents one unused issued-invoice void as an exact-decimal inclusive voided string.

## Issued-credit-note identity

A stored issued credit note is identified by `(tenant_account_id, credit_adjustment_id)`.  Internal primary key is the opaque generated `issued_credit_note_id`.  The hash covers the credit, draft, optional issued invoice, versions, currency, exclusive/tax/inclusive credit totals, and the credit source hash.  Status is `issued` only.  `issued_invoice_id` is optional and stored only when already traceable.  The snapshot copies the closed `credit_reason_code` and invents no lines, statutory credit-note number, fiscal signature, or customer PII.  `PostgresUsageLedger` writes the issued row with tenant-scoped composite foreign keys and exact `numeric` amounts.  Replay of the same identity does not insert a second row.  Rejected issue writes zero rows.  First successful issue appends one `webhook_outbox_event` with `event_type_code` `credit_note.issued` and `source_id` `issued_credit_note_id`.  The outbox `data` is a thin reference plus hash and omits lines.  `GET /v1/issued-credit-notes/{issued_credit_note_id}` projects the stored row and, when a stored `tax_assessment` for the same draft still reproduces the frozen exclusive/tax split, optional `tax_assessment_id`.  The issued row does not persist that identifier.  `GET /v1/issued-credit-notes` lists `{issued_credit_notes, next_cursor}` ordered by `issued_at` then `issued_credit_note_id`.

## Issued-credit-note-void identity

A stored issued-credit-note void is identified by `(tenant_account_id, issued_credit_note_id)`.  Internal primary key is the opaque generated `issued_credit_note_void_id`.  The hash covers the issued note, credit, draft, currency, inclusive amount, and contract version.  Status is `recorded` only.  Voided amount is the issued tax-inclusive credit.  One issued credit note voids at most once.  The issued snapshot stays `issued`.  `PostgresUsageLedger` writes the unused void with tenant-scoped composite foreign keys and exact `numeric` amounts.  Replay of the same identity does not insert a second row.  Rejected void writes zero rows.  First successful void appends one `webhook_outbox_event` with `event_type_code` `credit_note.voided` and `source_id` `issued_credit_note_void_id`.  The outbox `data` is a thin reference plus hash and omits remaining outstanding, collection identity, lines, and AIS ids.  The void row invents no VAT register, NTS filing, statutory number, or AIS posting.  Collection remaining is unchanged because the note cannot have been applied.  After a void exists, apply fail-closes as `issued_credit_note_voided`.  An explicit later compose appends one `journal_proposal` with `issued_credit_note_void_id` and semantic `accounts_receivable` / `usage_revenue` / optional `tax_payable` lines.  `PostgresUsageLedger` persists that unused credit-note-void journal so GET presentment survives process restart.  The proposal binds the original credit journal by Billing `proposal_id` plus `credit_adjustment_id` / `issued_credit_note_id` only and fails closed if that original is missing.  `GET /v1/issued-credit-note-voids/{issued_credit_note_void_id}` projects the stored row.  `GET /v1/issued-credit-note-voids` lists `{issued_credit_note_voids, next_cursor}` ordered by `voided_at` then `issued_credit_note_void_id`.  `operator_console` Storybook presents one unused issued-credit-note void as an exact-decimal string.

## Credit-note-application identity

A stored credit-note application is identified by `(tenant_account_id, issued_credit_note_id)`.  Internal primary key is the opaque generated `credit_note_application_id`.  The hash covers the issued note, collection case, invoice draft, optional issued invoice, currency, exact applied amount, and both contract versions.  Status is `applied` only.  Applied amount is the issued tax-inclusive credit.  One issued credit note applies at most once.  `PostgresUsageLedger` writes the applied row with tenant-scoped composite foreign keys and exact `numeric` amounts.  Replay of the same identity does not insert a second row or reduce outstanding again.  Rejected apply writes zero rows.  The row invents no statutory credit-note number, journal, tax unwind, settlement, or payment receipt.  First successful apply appends one `webhook_outbox_event` with `event_type_code` `credit_note.applied` and `source_id` `credit_note_application_id`.  The outbox `data` is a thin reference plus hash and omits remaining outstanding because that amount is not stored on the application.  `GET /v1/credit-note-applications/{credit_note_application_id}` projects the stored row plus current remaining outstanding.  `GET /v1/credit-note-applications` lists `{credit_note_applications, next_cursor}` ordered by `applied_at` then `credit_note_application_id`.

## Collection-case-settlement identity

A stored collection-case settlement is identified by `(tenant_account_id, collection_case_id)`.  Internal primary key is the opaque generated `collection_case_settlement_id`.  The hash covers the case, invoice draft, optional issued invoice, currency, exact-zero remaining, and contract version.  Status is `settled` only.  Remaining outstanding is exact zero.  One case settles at most once through this command.  The row invents no payment receipt, write-off, journal, or tax unwind.  First successful settle appends one `webhook_outbox_event` with `event_type_code` `collection.settled` and `source_id` `collection_case_settlement_id`.  The outbox `data` is a thin reference plus hash.  `GET /v1/collection-case-settlements/{collection_case_settlement_id}` projects the stored row plus current remaining outstanding.  `GET /v1/collection-case-settlements` lists `{collection_case_settlements, next_cursor}` ordered by `settled_at` then `collection_case_settlement_id`.  `operator_console` Storybook presents one leftover write-off settle-when-zero as an exact-decimal zero remaining string.

## Collection-write-off identity

A stored collection dispute is identified by `(tenant_account_id, collection_case_id)`.  Internal primary key is the opaque generated `collection_dispute_id`.  The hash covers the case, invoice draft, optional issued invoice, currency, remaining snapshot, and contract version.  Status is `held` until an in-place release flips it to `released` and stores `released_at`.  Remaining outstanding is a snapshot of current remaining and is not changed.  One case holds at most once through this command.  `PostgresUsageLedger` writes the collection-dispute row with tenant-scoped composite foreign keys and exact `numeric` amounts.  Replay of the same identity does not insert a second row.  Rejected hold writes zero rows.  Case status becomes `disputed` while held and returns to `open` or `dunning` on release.  First successful hold appends one `webhook_outbox_event` with `event_type_code` `dispute.held` and `source_id` `collection_dispute_id`.  First successful release appends one `webhook_outbox_event` with `event_type_code` `dispute.released` and the same `source_id`.  The outbox `data` is a thin reference plus hash and uses remaining outstanding at hold or release, not later-mutated case remaining.  The hold row invents no payment receipt, credit note, settlement, write-off, void, or journal.  A later hold of the same case fail-closes after release.  `operator_console` Storybook presents one held dispute and one released/fail-close remaining snapshot as exact-decimal strings.

A stored collection write-off is identified by `(tenant_account_id, collection_case_id)`.  Internal primary key is the opaque generated `collection_write_off_id`.  The hash covers the case, invoice draft, optional issued invoice, currency, write-off amount, exact-zero remaining, and contract version.  Status is `recorded` only.  Write-off amount is the remaining inclusive amount at accept time.  Remaining outstanding after accept is exact zero.  One case writes off at most once through this command.  The write-off row invents no payment receipt, credit note, settlement, or tax unwind.  Case status stays `open` or `dunning`.  First successful write-off appends one `webhook_outbox_event` with `event_type_code` `write_off.recorded` and `source_id` `collection_write_off_id`.  The outbox `data` is a thin reference plus hash and uses stored exact-zero remaining, not later-mutated case remaining.  An explicit later compose appends one `journal_proposal` with `collection_write_off_id` and semantic `write_off_expense` / `accounts_receivable` lines.  `PostgresUsageLedger` writes that write-off journal with tenant-scoped composite foreign keys and exact `numeric` amounts.  Replay of the same identity does not insert a second row or mutate remaining.  A crash after insert and before the existing `journal_proposal.validated` outbox enqueue is healed by the next replay.  Rejected compose writes zero journal rows.  `GET /v1/collection-write-offs/{collection_write_off_id}` projects the stored row plus current remaining outstanding.  `GET /v1/collection-write-offs` lists `{collection_write_offs, next_cursor}` ordered by `written_off_at` then `collection_write_off_id`.  `operator_console` Storybook presents one recorded leftover remaining write-off as exact-decimal strings.

## Unapplied-cash identity

A stored unapplied-cash row is identified by `(tenant_account_id, payment_receipt_id)`.  Internal primary key is the opaque generated `unapplied_cash_id`.  The hash covers the receipt, leftover amount, received and applied snapshots, currency, and contract version.  Status is `parked` only.  Leftover amount is a positive exact decimal that does not exceed the stored receipt.  One receipt parks leftover at most once.  `PostgresUsageLedger` writes the parked row with tenant-scoped composite foreign keys and exact `numeric` amounts.  Replay of the same identity does not insert a second row or mutate remaining.  Rejected park writes zero rows.  The row invents no webhook, write-off, settlement, or credit note.  Receipt amount and case remaining stay unchanged.  An explicit later compose appends one `journal_proposal` with `unapplied_cash_id` and semantic `cash_receipt` / `unapplied_cash` lines.  `PostgresUsageLedger` persists that leftover journal so GET presentment survives process restart.  Apply leftover through `unapplied_cash_application`.  `GET /v1/unapplied-cash/{unapplied_cash_id}` projects the stored row.  `GET /v1/unapplied-cash` lists `{unapplied_cash, next_cursor}` ordered by `parked_at` then `unapplied_cash_id`.  `operator_console` Storybook presents one parked leftover as an exact-decimal string.

## Unapplied-cash-application identity

A stored unapplied-cash application is identified by `(tenant_account_id, unapplied_cash_id)`.  Internal primary key is the opaque generated `unapplied_cash_application_id`.  The hash covers the leftover, target case, receipt, currency, applied amount, parked amount snapshot, and contract version.  Status is `applied` only.  Applied amount is the full parked leftover.  One leftover applies at most once.  `PostgresUsageLedger` writes the leftover-apply row with tenant-scoped composite foreign keys and exact `numeric` amounts.  Replay of the same identity does not insert a second row or reduce remaining again once remaining already excludes the applied amount.  A crash after insert and before outstanding reduction is healed by the next replay.  Rejected apply writes zero rows.  Outstanding is reduced by the exact applied inclusive amount.  Case status stays `open` or `dunning` even when remaining becomes exact zero.  The parked leftover row stays `parked`.  First successful apply appends one `webhook_outbox_event` with `event_type_code` `unapplied_cash.applied` and `source_id` `unapplied_cash_application_id`.  The outbox `data` is a thin reference plus hash and omits remaining outstanding because that amount is not stored on the application.  The row invents no write-off, settlement, or credit note.  An explicit later compose appends one `journal_proposal` with `unapplied_cash_application_id` and semantic `unapplied_cash` / `accounts_receivable` lines.  `PostgresUsageLedger` persists that leftover-apply journal so GET presentment survives process restart.  Apply fail-closes when a refund already exists.  `GET /v1/unapplied-cash-applications/{unapplied_cash_application_id}` projects the stored row plus current remaining outstanding.  `GET /v1/unapplied-cash-applications` lists `{unapplied_cash_applications, next_cursor}` ordered by `applied_at` then `unapplied_cash_application_id`.  `operator_console` Storybook presents that leftover-apply residual.

## Unapplied-cash-refund identity

A stored unapplied-cash refund is identified by `(tenant_account_id, unapplied_cash_id)`.  Internal primary key is the opaque generated `unapplied_cash_refund_id`.  The hash covers the leftover, receipt, currency, refund amount, parked amount snapshot, and contract version.  Status is `recorded` only.  Refund amount is the full parked leftover.  One leftover refunds at most once.  `PostgresUsageLedger` writes the leftover-refund row with tenant-scoped composite foreign keys and exact `numeric` amounts.  Replay of the same identity does not insert a second row.  Rejected refund writes zero rows.  The parked leftover row stays `parked`.  First successful refund appends one `webhook_outbox_event` with `event_type_code` `refund.recorded` and `source_id` `unapplied_cash_refund_id`.  The outbox `data` is a thin reference plus hash and omits payment-intent id, collection-case id, parked leftover snapshot, and leftover status.  An explicit later compose appends one `journal_proposal` with `unapplied_cash_refund_id` and semantic `unapplied_cash` / `cash_receipt` lines.  `PostgresUsageLedger` persists that leftover-refund journal so GET presentment survives process restart.  The row invents no write-off, settlement, credit note, or PSP capture.  `GET /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}` projects the stored row plus current leftover status.  `GET /v1/unapplied-cash-refunds` lists `{unapplied_cash_refunds, next_cursor}` ordered by `refunded_at` then `unapplied_cash_refund_id`.  `operator_console` Storybook presents that leftover-refund remaining snapshot.

## Collection-case-presentment projection

Collection presentment does not add a table.  `GET /v1/collection-cases/{collection_case_id}` projects stored `collection_case` and `collection_dunning_event` rows plus accepted credits on the same draft.  `collection_outstanding` is the exact stored outstanding.  `collection_case_status` stays `open`, `dunning`, `settled`, `voided`, or `disputed`.  Next operator action is `collect`, `credit`, or `wait`.  Collection aging presentment also does not add a table.  `GET /v1/collection-aging` projects stored `collection_case` remaining into current / 1-30 / 31-60 / 61-90 / 90+ buckets grouped by `currency_code`.  Due date is issued-invoice `due_at` when stored, otherwise `collection_case.opened_at`.  Settled cases and exact-zero remaining are omitted.  Account-statement presentment also does not add a table.  `GET /v1/billing-accounts/{billing_account_id}/statement` projects stored issued-invoice totals, unused issued-invoice voids, open collection remaining, applied credit-note amounts, unused issued-credit-note voids, write-offs, unused parked leftover, and leftover refunds grouped by `currency_code`.  `issued_invoice_total` stays the issued snapshot.  `applied_credit_total` stays applied credits only.  Attribution uses invoice-draft lines exclusive to that billing account.  Rated-spend presentment also does not add a table.  `GET /v1/billing-accounts/{billing_account_id}/rated-spend` projects stored `rating_run` and exclusive `invoice_draft` line amounts for one billing account and matching half-open window, grouped by `currency_code` and `product_code`.  Optional `group_by=project` adds stored exclusive-account `project_reference` and omits usage without that URN.  Optional `group_by=credential` adds stored exclusive-account `credential_reference` and omits usage without that URN.  Optional `group_by=principal` adds stored exclusive-account `billing_principal_reference`.  Mixed or unresolved principals omit the run.  Optional `group_by=cost_center` adds stored exclusive-account `cost_center_reference` and omits usage without that URN.  `rated_amount` is the stored rated or exclusive draft line amount.  Mixed-account and lineless drafts are omitted.  Unrated usage is omitted.  The read does not re-rate.  Spend-budget presentment also does not add a table.  `GET /v1/spend-budgets/{spend_budget_id}` projects stored `spend_budget` rows.  `budget_amount` is the exact stored amount.  `spend_budget_status` stays `published`.  Next operator action is `wait`.  Spend-budget evaluation presentment also does not add a table.  `GET /v1/spend-budgets/{spend_budget_id}/evaluation` projects that stored budget against already-rated exclusive-account spend for the same window and currency.  `remaining_amount` and `over_amount` are complementary non-negative exact amounts.  `utilization_status` stays `under`, `at`, or `over`.  Next operator action is `wait`.  Billing-account budget-status presentment also does not add a table.  `GET /v1/billing-accounts/{billing_account_id}/budget-status` lists those evaluations as `{budget_statuses, next_cursor}` ordered by `published_at` then `spend_budget_id`.  Unknown or cross-tenant budgets are omitted.  Currencies stay on their own rows.  Dunning-event presentment also does not add a table.  `GET /v1/dunning-events/{dunning_event_id}` projects one stored `collection_dunning_event`.  `GET /v1/dunning-events` lists `{dunning_events, next_cursor}` ordered by `occurred_at` then `collection_dunning_event_id`.

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

## Period-close and reconciliation foundation

The first #87 slice publishes side-effect-free contracts for later durable
period-close persistence.  `billing-period` represents one tenant period and
allows only the append-only state sequence `open`, `soft_closed`,
`reconciled`, `invoiced`, `hard_closed`; every transition carries an actor,
authorization reference, reason, and monotonic timestamp.  A hard-closed
snapshot cannot be changed through this contract. PostgreSQL now stores the
period base row and normalized transition rows; the current status is derived
from that history, and replaying a later immutable snapshot appends only new
transitions. Tenant ownership is resolved through `tenant_account_id` and
composite foreign keys.

`fx-rate` stores the source, rate type, base/quote currencies, exact rate,
declared precision, effective time, and recorded time.  `fx-conversion` copies
the rate identity and exact value into the result and rounds only at the
explicit target minor-unit scale.  This supports zero-, two-, three-, and
four-decimal currencies without summing unlike currencies or looking up a
later rate when re-exporting a closed result.

`reconciliation-line` keeps internal expected, provider actual, cash actual,
provider fee, withholding, and reserve amounts separate, and requires the
internal, provider, and cash source currencies as evidence.  Its deterministic
status reports typed quantity, price, tax, currency, payment, duplicate-charge,
refund, dispute, settlement, provider-fee, cash-timing, or unmapped-provider
exceptions with a next action. Raw contract validation re-applies the domain
lifecycle, exact arithmetic, and exception/status invariants. PostgreSQL stores
the line and exception children atomically, preserving the pinned FX snapshot
and exact `numeric` amounts. These contracts are not a FOCUS export, tax engine,
statutory invoice authority, provider connector, or period-wide reconciliation
run.

`reconciliation-resolution` is an append-only disposition linked to one stored
exception. It retains owner, reason, evidence reference, resolved-versus-waived
status, and distinct maker/checker references. Persisting a resolution does not
rewrite the original line or advance its period; a later reconciliation run
must evaluate all blocking exceptions and resolutions together.

`reconciliation-evidence` is an append-only hash-backed source reference linked
to one stored exception. It retains evidence kind, source reference, SHA-256
content digest, capturing operator, and capture instant. PostgreSQL rejects
evidence for an exception that is not present on the referenced line and keeps
reads tenant-scoped. The repository does not fetch or archive provider payloads
yet; run-level evidence completeness remains a follow-up.

`reconciliation-run` is an immutable completed-run envelope for one billing
period. Its normalized child rows preserve ordered reconciliation-line
membership and PostgreSQL requires every member to belong to the same tenant
and period. The stored exception count is a run summary; this slice does not
calculate it, resolve exceptions, or advance the period status.

`reconciliation-exception-aging` is a read-only projection of each persisted
line exception. It uses the immutable line `assessed_at` as the source instant,
an explicit `as_of` instant, UTC calendar days, and fixed current / 1-30 / 31-60
/ 61-90 / 90+ buckets. PostgreSQL returns it through the existing tenant- and
period-scoped line reads; age is never stored or used to mutate the exception.

`late_adjustment` is an append-only commercial fact with source and target
periods, one of `late_usage`, `correction`, or `reversal`, a signed exact
amount, currency, source reference, source payload hash, and contract version.
Its tenant-scoped source reference is the stable replay key; the source/target,
kind, hash, and contract version also have a unique payload identity. Identical
retries remain durable even when an opaque ID is regenerated, while changed
payloads fail closed. Composite foreign keys and migrations `0048`/`0049` triggers
require an adequately closed source, an open target beginning no earlier than
the source end, and immutable rows. The fact does not rewrite usage, rating,
or source period history and does not itself create a journal, tax document, or
FOCUS export.

`LateAdjustmentPresentmentService` projects that fact as a tenant-scoped item
or recorded-at/ID keyset page. `LateAdjustmentApplicationService` stores one
append-only `late_adjustment_application` per tenant and late-adjustment ID,
including the equal target period, signed exact amount, currency, actor,
authorization reference, and application instant. Presentment exposes
`apply_late_adjustment` before that row exists and `rate_late_adjustment`
afterward. A new application locks and rechecks the target period's latest
append-only status and requires `open`; replay bypasses that new-fact guard and
returns the first writer's immutable audit fields. The application row has
composite source/target foreign keys and immutable update/delete triggers; it
does not mutate the late adjustment. The memory reference adapter stores the
same billing-period aggregate and enforces source/target tenant, lifecycle, and
ordering invariants before storing a late-adjustment fact. Migration `0051`
rechecks concurrent application replays after the source lock and rejects future
application audit timestamps.
The list read passes the decoded cursor and `limit + 1` to the ledger, and
PostgreSQL evaluates one tenant-scoped ordered keyset query so the page never
scans or hydrates the complete history; application and rating existence are
loaded with one bulk lookup each for the bounded page.
Application audit timestamps are timezone-aware and not future-dated. The
memory adapter serializes recording, application, and target-period lifecycle writes while
preserving the same replay identity.

`LateAdjustmentRatingService` consumes that application and stores one
append-only `late_adjustment_rating` per tenant and late-adjustment ID. It copies
the application target, signed amount, and currency and adds the rating actor,
authorization reference, and instant. Migrations `0051` and `0053` protect the
application/source/target links, exact-value equality, replay identity, current
target openness for first ratings, and update/delete immutability. Migration
`0053` preserves already-stored rating replays after target closure. This is a rating-consumption fact, not a synthetic
`rating_run`. `LateAdjustmentInvoiceAdjustmentService` then stores one
tenant-scoped `late_adjustment_invoice_adjustment` per rated adjustment, linked
to an unissued invoice draft and copying the signed exact delta, target period,
application, rating, audit, source-hash, and single billing-account evidence. Migration `0054` protects
the tenant-scoped links, currency/evidence equality, replay identity, issued
draft boundary, and update/delete immutability. It does not rewrite the draft,
issue the invoice, calculate tax, or call a provider. Migration `0055` adds
`line_type` and the tenant-scoped composition link to `issued_invoice_line`.
Usage lines remain non-negative; a late-adjustment line carries the signed
non-zero delta, selected billing-account reference, and immutable composition identity. Migration `0056` persists and validates that account evidence,
backfills only unambiguous legacy drafts, and rejects ambiguous new drafts.
Migration `0057` adds one shared `BEFORE INSERT` trigger to collection cases,
tax assessments, credit adjustments, and journal proposals. It locks the
tenant-scoped invoice draft and rejects a new downstream row when an
immutable `late_adjustment_invoice_adjustment` already exists. Migration
`0058` adds the reverse composition trigger: it takes the same lock and rejects
direct composition after a downstream fact while allowing an existing
composition identity to replay. These are the database counterparts to the
service guards and make ordering safe for direct PostgreSQL persistence and
concurrent writers. Migration `0059` fails closed if a legacy version-1
composition lacks billing-account evidence; otherwise it upgrades only the
contract metadata to version 2 and adds an exact-version check constraint.
Application and direct PostgreSQL writes use the same version-2 constant.
Migration `0060` rejects composition amounts that cannot round-trip through
`numeric(38,12)`, validates late-adjustment issued lines against their
composition draft/amount/payer, and lets a post-issue collection row copy only
the frozen issued inclusive total.
Migration `0061` adds deferred PostgreSQL checks requiring every composition
linked to an issued draft to have one matching late-adjustment line and to be
included in the issued exclusive total.
Migration `0062` adds database immutability triggers for issued invoices and
issued lines and removes the `line_type` default, so direct issued-line writes
must provide the explicit version-2 type.
`IssuedInvoiceService`
locks the draft before consuming these facts and adjusts an untaxed issued
total exactly. If a tax assessment already exists, issuance rejects until a
tax reassessment path exists; it never reuses a stale tax snapshot. The issued
payload hash and presentment include the signed lines. Composition is rejected
after collection, journal, tax, or credit facts capture the draft, and a zero
resulting issue is rejected. Collection after issuance copies the frozen issued
inclusive total; collection before issuance, journal,
provider export, and statutory tax treatment remain downstream boundaries.

The PostgreSQL reconciliation command appends the `soft_closed` to `reconciled`
transition only for the latest completed run of that period. Its exception
summary must equal the run's persisted exception rows, and every exception must
have at least one resolved or waived resolution. The run and resolution facts
remain immutable; the validation and transition append share one transaction.

## Tenant-API-credential identity

A stored tenant API credential is identified by `tenant_api_credential_id` and is unique on `credential_secret_hash`.  Internal primary key is `tenant_api_credential_id`.  The hash is `hmac-sha256:` plus HMAC-SHA256(pepper, secret).  The plaintext secret is never stored.  `credential_label` is two-or-more-word `snake_case`.  Status is `active` or `revoked`.  A second issue of the same tenant, label, and contract version inserts a new row with a new secret.  Revocation updates `credential_status` and `revoked_at` on the same row and does not delete history.

## Webhook-outbox identity

A stored webhook subscription is identified by `(tenant_account_id, callback_url, event_type_set, webhook_subscription_contract_version)`.  Internal primary key is `webhook_subscription_id`.  The hash is `hmac-sha256:` plus HMAC-SHA256(pepper, secret).  The plaintext secret is never stored in SQL.  Status is `active` or `revoked`.  HTTP presentment projects metadata only and never returns the secret, hash, prefix, or signed body.  A stored outbox event is identified by `(tenant_account_id, event_type_code, source_id, payload_hash)`.  Delivery attempts are unique on `(outbox_event_id, webhook_subscription_id, attempt_number)` and never update a prior attempt.
