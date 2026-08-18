# Product Requirements Document

## Product outcome

Organizations can attribute AI-platform and CWL-product usage to a billing account, principal, credential, project, and cost center; apply versioned commercial rules; control spend; explain charges; and project those results to replaceable collection providers.

## Primary users

- Platform operators configure meters, prices, budgets, provider accounts, and reconciliation.
- Finance operations review invoice intent, collections, refunds, settlement, and accounting exports.
- Customer administrators inspect usage and spend by product, project, principal, and credential.
- Product services emit usage without implementing price or accounting logic.

## Required product properties

1. At-least-once event delivery produces at-most-once monetary effects.
2. Estimated usage is not automatically billable.
3. Price, contract, and meter changes do not rewrite historical rating outcomes.
4. Provider customer and subscription identifiers stay behind mapping records.
5. Payment, refund, dispute, and settlement facts remain provider-sticky after creation.
6. Every invoice line is explainable down to its usage evidence.
7. Accounting exports are proposals and cannot claim statutory posting.

## First commercial vertical

```text
contextual-orchestrator usage
-> canonical usage event
-> billability decision
-> deterministic aggregate
-> invoice intent
-> manual enterprise invoice or Lemon Squeezy projection
-> payment and settlement evidence
-> reconciliation
-> accounting journal proposal
```

## Tax-assessment acceptance

- A known tenant publishes one `tax_rate_schedule` and one immutable `tax_rate_version` whose `tax_rate` is an exact decimal in `[0, 1]`.
- A second publish of the same tenant, `tax_code`, rate, and contract version returns the same `tax_rate_version` as `duplicate_replay`.
- A later distinct rate increments the version. A published version is never edited.
- Assessing a stored draft stores `tax_exclusive_amount` as the drafted subtotal, half-even `tax_amount` in the currency minor units, and `tax_inclusive_amount` as exclusive plus tax.
- A taxed journal proposal debits `accounts_receivable` inclusive, credits `usage_revenue` exclusive, and credits `tax_payable` tax. An untaxed draft keeps the two-line AR/revenue proposal.
- Collection outstanding uses the inclusive amount when an assessment exists. Assess after a case is open fails closed.
- Another tenant cannot list, fetch, or assess the first tenant's rate or assessment.
- Missing tenant, float rates, rates outside `[0, 1]`, unknown `tax_code`, unknown currency exponents, and zero drafts fail closed.
- Operators publish a tax rate, assess the draft, then propose the journal and let AIS pull. AIS must map `tax_payable`. This slice does not add an OSS engine, exemptions, or UI.
- `POST /v1/tax-assessments` remains the #19 assess command keyed on `invoice_draft_id` and `tax_rate_version`. PAN, CVC, and provider secrets are refused.
- A known stored tax assessment presents one tenant-scoped statement with `invoice_draft_id`, exact `tax_exclusive_amount`, `tax_amount`, `tax_inclusive_amount`, stored `tax_code`/`tax_rate`, and `next_operator_action` (`propose_journal`).
- `GET /v1/tax-assessments/{tax_assessment_id}` stays the existing #19 item read. HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/tax-assessments` lists summaries as `{tax_assessments, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{assessed_at}|{tax_assessment_id}`.

## Rate-card catalog acceptance

- A known tenant publishes one `rate_card` and one immutable `rate_card_version` whose lines carry exact `unit_amount` values greater than zero.
- A second publish of the same tenant, card name, canonical line hash, and contract version returns the same `rate_card_version` as `duplicate_replay`.
- A later distinct line set increments the version. A published version is never edited.
- Another tenant cannot list, fetch, or rate the first tenant's card or version.
- Missing tenant, empty lines, zero or negative `unit_amount`, float money, currency mismatch, and single-word metric or card names fail closed.
- Operators publish a rate card, then rate a window against that version. This slice does not apply tax, discounts, or tiered prices.

## Windowed-rating acceptance

- A known stored-usage set in a half-open ISO 8601 window produces one exact invoice-intent money total equal to quantity times the published `unit_amount`.
- Equivalent decimal and UTC spellings (`1` vs `1.0`, `Z` vs `+00:00`) remain one stored fact and therefore one rated quantity.
- Rating requires a persisted same-tenant `rate_card_version`. An unknown or cross-tenant version rejects.
- A billable meter missing from the published card fails closed and does not invent a price.
- A second rate of the same tenant, window, rate-card version, and usage snapshot returns the same `rating_run_id` and totals.
- Another tenant's usage is invisible to the rated total.
- Estimated, reconstructed, and other non-billable qualities stay stored and stay out of invoice-intent money when `meter_quality_rule` says so.
- Rating does not create an invoice draft, a payment-provider command, or a posted accounting journal.
- `POST /v1/rating-runs` remains the #7 rate-a-window command. Replay of the same tenant, window, rate-card version, and usage snapshot returns the same `rating_run_id`. PAN, CVC, and provider secrets are refused.
- A known stored rating run presents one tenant-scoped statement with window bounds, `rate_card_version`, exact `rated_total_amount`, rating lines, and `next_operator_action` (`draft_invoice`).
- `GET /v1/rating-runs/{rating_run_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/rating-runs` lists summaries as `{rating_runs, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{recorded_at}|{rating_run_id}`.

## Journal-proposal acceptance

- A known invoice draft produces one balanced exact-decimal `accounting_journal_proposal` whose debit total equals its credit total.
- A second propose of the same tenant, `invoice_draft_id`, source-payload hash, and contract version returns the same `proposal_id`.
- Another tenant cannot see or propose from the first tenant's draft.
- Missing drafts, zero draft totals, float money, and unbalanced lines fail closed.
- Status stays inside the proposal lifecycle and is never `posted`. Operators hand the proposal to AIS.

## Collection-case acceptance

- A known invoice draft opens one collection case whose outstanding equals the exact draft total.
- A second open of the same tenant and `invoice_draft_id` returns the same `collection_case_id`.
- Another tenant cannot see or collect the first tenant's case.
- Dunning events append commercial reminders (`first_notice`, `overdue_notice`) without capturing money.
- Missing drafts, cross-tenant IDs, float money, and zero outstanding fail closed.
- Status stays `open` or `dunning` until a receipt settles remaining outstanding to zero.

## Payment-intent acceptance

- A known collection case projects one payment intent whose amount equals the exact case outstanding.
- A second project of the same tenant, `collection_case_id`, source-payload hash, and contract version returns the same `payment_intent_id`.
- Another tenant cannot see or project the first tenant's case.
- Missing cases, cross-tenant IDs, float money, and zero amounts fail closed.
- Status stays `projected`, `cancelled`, or `rejected`. Operators next record a commercial receipt or cancel the intent.

## Payment-settlement acceptance

- A known projected intent records one payment receipt whose exact amount is applied against that intent.
- A full receipt of the intent amount zeros collection outstanding and marks the case `settled`.
- A partial receipt leaves residual outstanding and leaves the case `open` or `dunning`.
- A second receipt of the same tenant, `payment_intent_id`, received amount, source-payload hash, and contract version returns the same `payment_receipt_id`.
- Another tenant cannot see or settle the first tenant's intent.
- Cancel flips a projected intent to `cancelled` without writing a receipt or changing outstanding. Cancel replay is idempotent. A cancelled intent cannot later receive a receipt.
- Missing intents, cross-tenant IDs, float money, zero or negative amounts, over-application, and non-projected intents fail closed.
- Status stays `applied`. Accept and duplicate replay idempotently propose the existing cash journal. Operators record the receipt; the cash journal is already validated for AIS to pull.

## Cash-journal acceptance

- A known payment receipt produces one balanced exact-decimal `accounting_journal_proposal` that debits `cash_receipt` and credits `accounts_receivable`.
- A second propose of the same tenant, `payment_receipt_id`, source-payload hash, and contract version returns the same `proposal_id`.
- Another tenant cannot see or propose from the first tenant's receipt.
- Missing receipts, cross-tenant IDs, float money, and zero or negative amounts fail closed.
- Collection outstanding is not changed. Status stays inside the proposal lifecycle and is never `posted`. AIS pulls validated proposals.

## HTTP accept-surface acceptance

- Buyers and AIS can POST the already-built commercial path as JSON without importing in-process Python services.
- Every write requires `tenant_reference`. Money stays exact-decimal strings.
- After a tenant has one or more active API credentials, every `/v1` call except credential issue requires a matching bearer or `X-CWL-Api-Key` secret. Zero active keys keep the tenant pin (bootstrap window).
- HTTP 200 means `accepted` or `duplicate_replay`. HTTP 422 means `rejected` or an unreadable request. HTTP 404 is an unknown route or an unknown/cross-tenant credential.
- HTTP does not post journals, store a card PAN, or add Stripe, Adyen, or Toss.

## Journal-proposal query acceptance

- AIS can GET persisted proposals as the published `accounting_journal_proposal` contract.
- Every read requires a tenant via optional `X-CWL-Tenant-Reference` or `tenant_reference`. If both are present they must match. Another tenant cannot list or fetch the first tenant's proposals.
- Optional filters are `proposal_status` (`draft|validated|exported|rejected` only), inclusive `proposed_after`, and a bounded `cursor` / `page_limit`.
- Cash, AR, and credit proposals share `journal_proposal` and appear in the same list. There is no cash-specific GET route.
- HTTP 200 is a successful read. HTTP 422 is a missing tenant or illegal filter. HTTP 404 is an unknown route or unknown/cross-tenant proposal.
- Query never mutates `proposal_status` and never emits `posted`. AIS pulls validated proposals and owns `posting_receipt`.

## Posting-receipt observation acceptance

- An operator can pull an AIS posting receipt for the published invoice and cash idempotency keys and store one `posting_receipt_observation`.
- A replay of the same tenant, key, and receipt returns the same observation as `duplicate_replay`.
- AIS 403 is cross-tenant, writes zero rows, and is not retried as another tenant.
- AIS 404 is `not_yet_accepted`: accept the proposal on AIS, then retry. Billing does not invent a receipt.
- `posting_status_code` values `posted`, `held`, `rejected`, and `reversed` store as observations. Billing `proposal_status` stays `validated`.
- GET of a stored observation is tenant-scoped, returns 404 across tenants, and does not call AIS.
- `POST /v1/posting-receipt-observations` remains the #16 pull keyed on `idempotency_key`. PAN, CVC, and provider secrets are refused.
- A known stored observation presents one tenant-scoped statement with `posting_receipt_observation_id`, `source_proposal_id`, `idempotency_key`, AIS `posting_status_code`, hashes, timestamps, and `next_operator_action` (`wait`).
- `GET /v1/posting-receipt-observations/{idempotency_key}` stays the existing #16 item read. HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/posting-receipt-observations` lists summaries as `{posting_receipt_observations, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{observed_at}|{posting_receipt_observation_id}`.
- Missing tenant, missing key, illegal `posting_status_code`, tenant mismatch, float JSON, and transport failure fail closed.

## Invoice-presentment acceptance

- A known stored invoice draft presents one tenant-scoped statement with `tax_exclusive_amount`, `tax_amount`, `tax_inclusive_amount`, `credited_amount`, and `amount_due`.
- Tax fields are zero when no assessment exists. Inclusive equals exclusive plus tax when assessed.
- `credited_amount` is the sum of accepted credits. `amount_due` is inclusive minus credits and never below zero.
- Line items project `metric_code`, `quantity`, `unit_amount`, and `line_amount` as exact-decimal strings.
- When a collection case exists the statement includes `collection_case_id` and `collection_outstanding`.
- A second presentment of the same tenant and `invoice_draft_id` returns the same amounts.
- `GET /v1/invoice-drafts/{invoice_draft_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/invoice-drafts` lists summaries `{invoice_draft_id, amount_due, currency_code, drafted_at}` with `{invoice_drafts, next_cursor}`.
- Missing tenant, float money, and unknown ids fail closed.
- Operators open the draft statement, then collect or credit. HTTP presentment does not add PDF or email.
- `operator_console` Storybook renders that statement with tokenized amount due, line table, status chip, and tenant pin. Fixtures are taxed plus partial credit, untaxed, and settled. Amounts stay exact-decimal strings. Customer copy is amount due and the next operator action: collect or credit. There is no login wall, Stripe, AIS call, or production SPA.

## Issued-invoice acceptance

- A known stored invoice draft issues one append-only commercial `issued_invoice` whose currency, lines, and tax-exclusive/tax/inclusive totals match the draft or its tax assessment.
- A second issue of the same tenant and `invoice_draft_id` returns the same `issued_invoice_id` as `duplicate_replay`. A later `due_at` is ignored.
- `issued_invoice_id` is an opaque generated identifier. The path does not invent sequential or statutory numbering, QR/fiscal signatures, Peppol clearance, or jurisdiction-specific compliance claims.
- Optional `due_at` is stored only when the caller supplies a valid timezone-aware instant. Drafts have no due terms today.
- `POST /v1/invoice-drafts/{invoice_draft_id}/issued-invoices` is the nested issue command. PAN, CVC, and provider secrets are refused. Missing tenant is HTTP 422. Unknown or cross-tenant drafts reject `invoice_draft_not_found`.
- A known stored issued invoice presents one tenant-scoped statement with identity, draft source, frozen totals, lines, `issued_at`, optional `due_at`, optional stored `tax_assessment_id` when the draft assessment amounts still match those totals, and `next_operator_action` (`collect`).
- `GET /v1/issued-invoices/{issued_invoice_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/issued-invoices` lists summaries as `{issued_invoices, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{issued_at}|{issued_invoice_id}`.
- Invoice-draft, tax-assessment, journal-proposal, collection, payment, and AIS contracts stay unchanged. `invoice_draft_status` stays `draft`. `proposal_status` stays `validated`. First successful issue enqueues one existing #24 `invoice.issued` outbox event. No payment capture or AIS call is added. HMAC, SSRF, and delivery contracts stay unchanged.
- Operators issue invoice, then collect or credit. `operator_console` Storybook adds one `IssuedInvoice` story. There is no login wall, Stripe, AIS call, or production SPA.

## Issued-invoice-void acceptance

- A known unused same-tenant issued invoice voids once. `voided_amount` is the issued tax-inclusive amount in the same currency. Status is `recorded`. The issued snapshot stays `issued`.
- A second void of the same tenant and `issued_invoice_id` returns the same `issued_invoice_void_id` as `duplicate_replay` and never re-closes a collection case.
- `issued_invoice_void_id` is an opaque generated identifier. The path does not invent sequential or statutory numbering, a journal, refund, write-off rewrite, statement rewrite, or AIS call.
- First successful void enqueues one existing #24 `invoice.voided` outbox event. `source_id` is `issued_invoice_void_id`. Replay does not enqueue a second row. Rejected void writes zero outbox rows. HMAC, SSRF, and delivery contracts stay unchanged.
- Fail closed when the related collection case has a payment receipt, credit-note apply, unapplied-cash apply, or write-off; remaining no longer equals the issued amount; the case is already settled; currency mismatches; the invoice is missing or cross-tenant; or the tenant is missing.
- An unused open or dunning case closes as `voided` at exact-zero remaining. `settled` is not reused. Settle-when-zero then fail-closes as `collection_case_voided`.
- `POST /v1/issued-invoices/{issued_invoice_id}/voids` is the nested void command. PAN, CVC, and provider secrets are refused. Missing tenant is HTTP 422.
- A known stored void presents one tenant-scoped statement with identity, voided amount, remaining outstanding, `voided_at`, and `next_operator_action` (`wait`).
- `GET /v1/issued-invoice-voids/{issued_invoice_void_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/issued-invoice-voids` lists summaries as `{issued_invoice_voids, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{voided_at}|{issued_invoice_void_id}`.
- Issued-invoice, collection, payment, credit, write-off, leftover, settlement, statement, and AIS contracts stay unchanged except the additive `voided` collection-case status and settle fail-closed on that status.
- Operators void an unused issue. Existing subscriptions opt in by including `invoice.voided`. There is no login wall, Stripe, AIS call, journal, second webhook system, or production SPA.

## Issued-credit-note acceptance

- A known stored credit adjustment issues one append-only commercial `issued_credit_note` whose currency and tax-exclusive/tax/inclusive totals match the stored credit.
- A second issue of the same tenant and `credit_adjustment_id` returns the same `issued_credit_note_id` as `duplicate_replay`.
- `issued_credit_note_id` is an opaque generated identifier. The path does not invent sequential or statutory numbering, QR/fiscal signatures, Peppol clearance, or jurisdiction-specific compliance claims.
- `issued_invoice_id` is stored only when an issued invoice already exists for the same draft. The field is omitted when absent.
- The snapshot copies the closed `credit_reason_code`. It invents no credit-note lines and no PII.
- `POST /v1/credit-adjustments/{credit_adjustment_id}/issued-credit-notes` is the nested issue command. PAN, CVC, and provider secrets are refused. Missing tenant is HTTP 422. Unknown or cross-tenant credits reject `credit_adjustment_not_found`.
- A known stored issued credit note presents one tenant-scoped statement with identity, credit source, frozen totals, `issued_at`, optional stored `tax_assessment_id` when the draft assessment still reproduces those exclusive/tax amounts, and `next_operator_action` (`wait`).
- `GET /v1/issued-credit-notes/{issued_credit_note_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/issued-credit-notes` lists summaries as `{issued_credit_notes, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{issued_at}|{issued_credit_note_id}`.
- Credit-adjustment, tax-unwind, journal-proposal, invoice, collection, payment, and AIS contracts stay unchanged. `credit_adjustment` stays `recorded`. `proposal_status` stays `validated`. First successful issue enqueues one existing #24 `credit_note.issued` outbox event. No payment capture or AIS call is added. HMAC, SSRF, and delivery contracts stay unchanged.
- Operators issue the credit note; the validated journal remains available for AIS. `operator_console` Storybook adds one `IssuedCreditNote` story. There is no login wall, Stripe, AIS call, or production SPA.

## Issued-credit-note-void acceptance

- A known unused same-tenant issued credit note voids once. `voided_amount` is the issued tax-inclusive credit in the same currency. Status is `recorded`. The issued snapshot stays `issued`.
- A second void of the same tenant and `issued_credit_note_id` returns the same `issued_credit_note_void_id` as `duplicate_replay`.
- `issued_credit_note_void_id` is an opaque generated identifier. The path does not invent sequential or statutory numbering, a journal, VAT register, NTS filing, 연말정산, statutory account, negative invoice, or AIS call.
- First successful void enqueues one existing #24 `credit_note.voided` outbox event. `source_id` is `issued_credit_note_void_id`. Replay does not enqueue a second row. Rejected void writes zero outbox rows. HMAC, SSRF, and delivery contracts stay unchanged.
- Fail closed when the note has already been applied; the note is missing or cross-tenant; currency mismatches; or the tenant is missing.
- Collection remaining is unchanged because the note cannot have been applied. After a void exists, apply fail-closes as `issued_credit_note_voided`.
- `POST /v1/issued-credit-notes/{issued_credit_note_id}/voids` is the nested void command. PAN, CVC, and provider secrets are refused. Missing tenant is HTTP 422.
- A known stored void presents one tenant-scoped statement with identity, voided amount, `voided_at`, and `next_operator_action` (`wait`).
- `GET /v1/issued-credit-note-voids/{issued_credit_note_void_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/issued-credit-note-voids` lists summaries as `{issued_credit_note_voids, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{voided_at}|{issued_credit_note_void_id}`.
- Issued-credit-note, credit-note-application, collection, payment, and AIS contracts stay unchanged except the additive apply fail-closed on a voided unused note.
- Operators void an unused issued credit note. Existing subscriptions opt in by including `credit_note.voided`. There is no login wall, Stripe, AIS call, journal, second webhook system, or production SPA.

## Credit-note-application acceptance

- A known stored issued credit note applies once onto one open same-tenant collection case and reduces `collection_outstanding` by the exact issued tax-inclusive amount.
- A second apply of the same tenant and `issued_credit_note_id` returns the same `credit_note_application_id` as `duplicate_replay` and never double-reduces.
- `credit_note_application_id` is an opaque generated identifier. The path does not invent statutory numbering, a journal, tax unwind, payment capture, AIS call, write-off, or settlement.
- Currency mismatch, settled case, remaining that would go negative, invoice mismatch (draft, or issued invoice when stored), and a voided unused note fail closed.
- `POST /v1/collection-cases/{collection_case_id}/credit-note-applications` is the nested apply command. PAN, CVC, and provider secrets are refused. Missing tenant is HTTP 422.
- A known stored application presents one tenant-scoped statement with identity, applied amount, remaining outstanding, `applied_at`, and `next_operator_action` (`collect` or `wait`).
- `GET /v1/credit-note-applications/{credit_note_application_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/credit-note-applications` lists summaries as `{credit_note_applications, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{applied_at}|{credit_note_application_id}`.
- Issued-credit-note, credit-adjustment, collection, payment, journal, and AIS contracts stay unchanged. `proposal_status` stays `validated`. Payment receipts stay unchanged. First successful apply enqueues one existing #24 `credit_note.applied` outbox event. HMAC, SSRF, and delivery contracts stay unchanged.
- Operators apply the issued credit note, then collect the residual. `operator_console` Storybook adds one `CreditNoteApplication` story. There is no login wall, Stripe, AIS call, or production SPA.

## Collection-case-settlement acceptance

- A known stored open collection case whose remaining outstanding is exact zero settles once and flips status to `settled`.
- A second settle of the same tenant and `collection_case_id` returns the same `collection_case_settlement_id` as `duplicate_replay` and never double-settles.
- `collection_case_settlement_id` is an opaque generated identifier. The path does not invent statutory numbering, a journal, tax unwind, payment receipt, write-off, AIS call, or a new webhook event type.
- Outstanding that is not zero, an already-settled case, a voided case, and a tenant mismatch fail closed.
- `POST /v1/collection-cases/{collection_case_id}/settlements` is the nested settle command. PAN, CVC, and provider secrets are refused. Missing tenant is HTTP 422.
- A known stored settlement presents one tenant-scoped statement with identity, exact-zero remaining, `settled_at`, and `next_operator_action` (`wait`).
- `GET /v1/collection-case-settlements/{collection_case_settlement_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/collection-case-settlements` lists summaries as `{collection_case_settlements, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{settled_at}|{collection_case_settlement_id}`.
- Payment-receipt, credit-note-application, collection-open, journal, and AIS contracts stay unchanged. `proposal_status` stays `validated`. Implicit #12/#17/#45 settle-when-zero on positive apply paths stays. First successful settle enqueues one existing #24 `collection.settled` outbox event. HMAC, SSRF, and delivery contracts stay unchanged. Cases already settled by #12/#45 without a settlement row are not backfilled.
- Operators settle the zero-outstanding case, then wait. `operator_console` Storybook adds one `CollectionCaseSettlement` story. There is no login wall, Stripe, AIS call, or production SPA.

## Collection-dispute-hold acceptance

- A known stored open or dunning collection case holds once. Remaining outstanding stays the current exact-decimal snapshot. Case status becomes `disputed`. Hold status is `held`.
- A second hold of the same tenant and `collection_case_id` returns the same `collection_dispute_id` as `duplicate_replay` and never changes remaining outstanding.
- `collection_dispute_id` is an opaque generated identifier. The path does not invent a journal, webhook, write-off, settlement, void rewrite, statutory numbering, or AIS call.
- Missing case, already-settled case, already-voided case, already-disputed case without a stored row, currency mismatch, and missing tenant fail closed.
- New dunning fails closed as `collection_case_disputed`. Replay of a notice that already existed before the hold stays `#10` `duplicate_replay`.
- Payment receipt, credit apply, leftover apply, write-off, settle-when-zero, and void fail closed while held.
- `POST /v1/collection-cases/{collection_case_id}/disputes` is the nested hold command. PAN, CVC, and provider secrets are refused. Missing tenant is HTTP 422.
- A known stored hold presents one tenant-scoped statement with identity, remaining snapshot, `held_at`, and `next_operator_action` (`wait`).
- `GET /v1/collection-disputes/{collection_dispute_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/collection-disputes` lists summaries as `{collection_disputes, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{held_at}|{collection_dispute_id}`.
- Write-off, void, settle, receipt, credit-apply, leftover-apply, journal, and AIS contracts stay unchanged except additive fail-closed `collection_case_disputed` reasons.
- Operators hold the disputed case, then wait. There is no Storybook story in this slice. There is no login wall, Stripe, AIS call, or production SPA.

## Collection-dispute-held webhook acceptance

- First successful hold enqueues one existing #24 `dispute.held` outbox event. `source_id` is `collection_dispute_id`. Replay does not enqueue a second row. Rejected hold writes zero outbox rows. HMAC, SSRF, and delivery contracts stay unchanged.
- Envelope `data` is a thin reference plus hash: ids, contract version, hash, currency, exact remaining outstanding at hold, `held_at`, `collection_dispute_status`, and optional `issued_invoice_id`. Collection-case status, operator action, outcome codes, PII, PAN, secrets, statutory identifiers, and dispute-reason blobs are omitted.
- Existing subscriptions opt in by including `dispute.held`. `dispute.released` is not added.
- Operators hold a disputed case. There is no login wall, Stripe, AIS call, journal, second webhook system, or production SPA.

## Collection-dispute-release acceptance

- A known stored held collection dispute releases once. Remaining outstanding stays the current exact-decimal snapshot. Dispute status becomes `released`. Case status returns to `open`, or to `dunning` when stored notices already exist.
- A second release of the same tenant and `collection_dispute_id` returns the same `collection_dispute_id` as `duplicate_replay` and never changes remaining outstanding.
- The path does not invent a second hold row, journal, webhook, write-off, settlement, void rewrite, statutory numbering, or AIS call.
- Missing dispute, not-held dispute, missing case, already-settled case, already-voided case, currency mismatch, and missing tenant fail closed.
- After release, dunning, payment receipt, credit apply, leftover apply, write-off, settle-when-zero, and void follow the existing open-case rules. A later hold of the same case fail-closes as `collection_dispute_released`.
- `POST /v1/collection-disputes/{collection_dispute_id}/releases` is the nested release command. PAN, CVC, and provider secrets are refused. Missing tenant is HTTP 422.
- A known stored release presents one tenant-scoped statement with identity, remaining snapshot, `released_at`, and `next_operator_action` (`wait`).
- `GET /v1/collection-dispute-releases/{collection_dispute_id}` is HTTP 200 for the same tenant. Cross-tenant, unknown, or still-held is HTTP 404 with no leak.
- `GET /v1/collection-dispute-releases` lists summaries as `{collection_dispute_releases, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{released_at}|{collection_dispute_id}`.
- First successful release enqueues one existing #24 `dispute.released` outbox event. `source_id` is `collection_dispute_id` on the same hold row. Replay does not enqueue a second row. Rejected or not-released rows write zero outbox rows. HMAC, SSRF, and delivery contracts stay unchanged.
- Envelope `data` is a thin reference plus hash: ids, contract version, hash, currency, exact remaining outstanding at release, `released_at`, `collection_dispute_status`, and optional `issued_invoice_id`. Collection-case status, operator action, outcome codes, `held_at`, PII, PAN, secrets, statutory identifiers, and dispute-reason blobs are omitted.
- Existing subscriptions opt in by including `dispute.released`.
- Hold, write-off, void, settle, receipt, credit-apply, leftover-apply, journal, and AIS contracts stay unchanged except additive fail-closed `collection_dispute_released` on a later hold.
- Operators release the hold, then collect or dunn. There is no Storybook story in this slice. There is no login wall, Stripe, AIS call, or production SPA.

## Collection-write-off acceptance

- A known stored open collection case whose remaining outstanding is strictly positive writes off once and zeros remaining without flipping status.
- A second write-off of the same tenant and `collection_case_id` returns the same `collection_write_off_id` as `duplicate_replay` and never re-zeros outstanding.
- `collection_write_off_id` is an opaque generated identifier. The write-off command does not compose a journal. Statutory numbering, tax unwind, payment receipt, credit note, settlement command, and AIS call stay out of this path.
- Missing case, already-settled case, remaining already zero, negative remaining, currency mismatch, and a supplied amount that does not equal remaining fail closed. Body may omit amount.
- `POST /v1/collection-cases/{collection_case_id}/write-offs` is the nested write-off command. PAN, CVC, and provider secrets are refused. Missing tenant is HTTP 422.
- A known stored write-off presents one tenant-scoped statement with identity, exact write-off amount, exact-zero remaining, `written_off_at`, and `next_operator_action` (`settle`).
- `GET /v1/collection-write-offs/{collection_write_off_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/collection-write-offs` lists summaries as `{collection_write_offs, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{written_off_at}|{collection_write_off_id}`.
- Payment-receipt, issued-credit-note, credit-note-application, collection-settlement, journal, and AIS contracts stay unchanged. `proposal_status` stays `validated`. #46 remains the explicit settle-when-zero command. First successful write-off enqueues one existing #24 `write_off.recorded` outbox event. HMAC, SSRF, and delivery contracts stay unchanged.
- Operators write off leftover remaining, compose the journal, then settle. There is no Storybook story in this slice. There is no login wall, Stripe, AIS call, or production SPA.

## Write-off-journal acceptance

- A known stored collection write-off produces one balanced exact-decimal `accounting_journal_proposal` that debits `write_off_expense` and credits `accounts_receivable`.
- A second propose of the same tenant and `collection_write_off_id` returns the same `proposal_id` as `duplicate_replay` and does not grow the store.
- Another tenant cannot see or propose from the first tenant's write-off.
- Missing write-offs, cross-tenant IDs, currency mismatch, float money, and zero or negative amounts fail closed.
- Collection outstanding is not changed. Status stays inside the proposal lifecycle and is never `posted`. AIS pulls validated proposals through existing GET journal-proposal routes.
- `POST /v1/collection-write-offs/{collection_write_off_id}/journal-proposals` is the explicit compose because #49 shipped without compose. PAN, CVC, and provider secrets are refused.
- Invoice-draft, cash, credit, payment-receipt, settlement, and `write_off.recorded` contracts stay unchanged.

## Tenant-API-credential acceptance

- An operator can issue one append-only `tenant_api_credential` for a known tenant. The secret is returned once (prefix plus secret). The ledger stores only a keyed HMAC.
- A second issue of the same tenant, optional two-or-more-word `snake_case` `credential_label`, and contract version mints a new secret and a new row. It is not a silent duplicate of the first secret.
- `revoke_credential` is idempotent. Revoked and unknown keys fail closed as `api_credential_invalid`.
- After one or more active keys exist, every `/v1` write and GET except credential issue requires `Authorization: Bearer <secret>` or `X-CWL-Api-Key: <secret>` whose tenant equals `X-CWL-Tenant-Reference` / `tenant_reference`. A mismatch is HTTP 422.
- Zero active keys keep the existing tenant pin (bootstrap window). AIS `X-CWL-Tenant-Reference` pull stays up until a key is issued for that tenant.
- `GET /healthz` stays unauthenticated.
- `POST /v1/tenant-api-credentials` may use the tenant pin alone. PAN, CVC, and provider secrets are refused on issue and revoke.
- A known stored `tenant_api_credential` presents one tenant-scoped metadata statement with `tenant_api_credential_id`, `credential_label`, `credential_prefix`, `credential_status`, timestamps, contract version, and `next_operator_action` (`wait` while active, otherwise `issue`).
- `GET /v1/tenant-api-credentials/{tenant_api_credential_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak. GET never reconstructs a secret.
- `GET /v1/tenant-api-credentials` lists summaries as `{tenant_api_credentials, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{issued_at}|{tenant_api_credential_id}`.
- `POST /v1/tenant-api-credentials/{id}/revoke` revokes. Presentment never returns `api_credential_secret`, `credential_secret_hash`, a verifier, or a bearer token.
- Missing tenant, missing key after bootstrap closes, unknown or revoked key, and cross-tenant key use fail closed.
- Operators issue a key, then send it on every `/v1` call; revoke when leaked. This slice does not log the secret, put it on AIS contracts, change journal/tax/credit/presentment shapes, or start a web UI.

## Webhook-outbox acceptance

- An operator can register one tenant-scoped `webhook_subscription` for a known tenant. `callback_url` must be https. http is allowed only for localhost tests.
- Replay of the same tenant, callback URL, event-type set, and contract version returns the same `webhook_subscription_id` as `duplicate_replay` and does not mint a second secret.
- The secret is returned once. `GET /v1/webhook-subscriptions/{webhook_subscription_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak. GET never reconstructs a secret.
- `GET /v1/webhook-subscriptions` lists summaries as `{webhook_subscriptions, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{issued_at}|{webhook_subscription_id}`.
- Presentment never returns `webhook_secret`, `webhook_secret_hash`, `webhook_secret_prefix`, a signature key, `payload_json`, or a signed body. `POST /v1/webhook-subscriptions/{id}/revoke` stays the #24 revoke. PAN, CVC, and provider secrets are refused on register and revoke.
- When a journal proposal is validated, a payment receipt is applied, a credit is recorded, an invoice is issued, a credit note is issued, a credit note is applied, a collection case is settled, leftover remaining is written off, parked leftover is applied, unused parked leftover is refunded, a collection case is held as disputed, or a held dispute is released, one `webhook_outbox_event` is appended. Replay of the commercial fact does not enqueue a second row.
- Event types in this slice are `journal_proposal.validated`, `payment_receipt.applied`, `credit_adjustment.recorded`, `invoice.issued`, `invoice.voided`, `credit_note.issued`, `credit_note.voided`, `credit_note.applied`, `collection.settled`, `write_off.recorded`, `unapplied_cash.applied`, `refund.recorded`, `dispute.held`, and `dispute.released`. Journal, payment, and credit payloads wrap the published contract. `invoice.issued`, `invoice.voided`, `credit_note.issued`, `credit_note.voided`, `credit_note.applied`, `collection.settled`, `write_off.recorded`, `unapplied_cash.applied`, `refund.recorded`, `dispute.held`, and `dispute.released` `data` are thin references plus hash and omit lines, PAN, secrets, and statutory identifiers.
- `WebhookDeliveryService.deliver_due_events` and `POST /v1/webhook-deliveries` POST JSON to active same-tenant subscriptions and sign the raw body with `X-CWL-Webhook-Signature: sha256=<hex>`. PAN, CVC, and provider secrets are refused on the write.
- Delivery attempts are append-only. Success marks the outbox event delivered. Later explicit runs may retry. There is no scheduler.
- A known stored `webhook_delivery_attempt` presents one tenant-scoped statement with `delivery_attempt_id`, `webhook_subscription_id`, `event_type_code`, `source_id`, `attempt_number`, stored HTTP status or failure, timestamps, and `next_operator_action` (`wait` after stored success, otherwise `run_deliveries`).
- `GET /v1/webhook-deliveries/{delivery_attempt_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak. GET never resends.
- `GET /v1/webhook-deliveries` lists summaries as `{webhook_deliveries, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{attempted_at}|{delivery_attempt_id}`.
- Presentment never returns `webhook_secret`, `webhook_secret_hash`, `payload_json`, or a signed raw body. It does not invent `delivery_status`.
- Revoked subscriptions are not POSTed. Missing tenant, insecure production callbacks, unknown event types, and secret leakage on list JSON fail closed.
- AIS pull stays bootstrap. Operators register an https callback, then run deliveries; AIS may keep polling. This slice does not flip `proposal_status`, call AIS posting-receipt, or emit statutory IDs.

## Usage-event-presentment acceptance

- `POST /v1/usage-events` remains the #5 ingest. Replay of the same tenant, source-event key, payload hash, and contract version returns the same `usage_event_id`. PAN, CVC, and provider secrets are refused.
- A known stored usage event presents one tenant-scoped statement with `source_event_key`, `event_payload_hash`, `occurred_at`, `recorded_at`, measurement quantities, and `next_operator_action` (`rate_window`).
- `GET /v1/usage-events/{usage_event_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/usage-events` lists summaries as `{usage_events, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{recorded_at}|{usage_event_id}`.
- Operators ingest usage, then rate a window against a published card. HTTP presentment does not invent an ingest shape or call AIS.
- `operator_console` Storybook renders that event with tokenized quantity and status chip. Fixtures are stored-morning and stored-partial-token.

## Rate-card-presentment acceptance

- `POST /v1/rate-cards` remains the #18 write. Replay of the same tenant, card name, canonical lines, and contract version returns the same `rate_card_version`. PAN, CVC, and provider secrets are refused.
- A known stored rate card presents one tenant-scoped statement with `rate_card_name`, `currency_code`, latest `rate_card_version`, flat `lines`, and `next_operator_action` (`rate_window`).
- `GET /v1/rate-cards/{rate_card_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/rate-cards` lists summaries as `{rate_cards, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{created_at}|{rate_card_id}`.
- Operators publish a rate card, then rate a window against that version. HTTP presentment does not invent a catalog or call AIS.
- `operator_console` Storybook renders that card with tokenized unit price and status chip. Fixtures are published-standard and published-premium.

## Credit-adjustment-presentment acceptance

- `POST /v1/credit-adjustments` remains the #17 write against `invoice_draft_id`. Amount is the exact `credit_amount`. Replay of the same tenant, draft, amount, reason, and contract version returns the same `credit_adjustment_id`. PAN, CVC, and provider secrets are refused.
- A known stored credit presents one tenant-scoped statement with `credit_amount`, stored `tax_exclusive_amount` and `tax_amount`, `credit_adjustment_status` (`recorded`), and `next_operator_action` (`wait`).
- `GET /v1/credit-adjustments/{credit_adjustment_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/credit-adjustments` lists summaries as `{credit_adjustments, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{recorded_at}|{credit_adjustment_id}`.
- Operators record the credit; AIS pulls the validated journal. HTTP presentment does not invent a journal or call AIS.
- `operator_console` Storybook renders that credit with tokenized amount due and status chip. Fixtures are recorded-morning and recorded-taxed.

## Payment-receipt-presentment acceptance

- `POST /v1/payment-receipts` applies one #12 receipt against a projected `payment_intent_id`. Amount is the exact `received_amount`. Currency comes from the intent. Replay of the same tenant, intent snapshot, amount, and contract version returns the same `payment_receipt_id`. PAN, CVC, and provider secrets are refused.
- Accept persists the receipt, reduces collection outstanding, enqueues #24 `payment_receipt.applied`, and composes the existing #13 cash journal. `POST /v1/cash-journal-proposals` remains a manual replay with `{tenant}:cash_receipt:{payment_receipt_id}:{source_payload_hash}:v{version}`.
- A known stored payment receipt presents one tenant-scoped statement with `received_amount`, `remaining_outstanding_amount`, `payment_receipt_status` (`applied`), and `next_operator_action` (`record_receipt` or `drain_or_wait`).
- `GET /v1/payment-receipts/{payment_receipt_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/payment-receipts` lists summaries as `{payment_receipts, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100.
- Missing tenant, illegal cursor, and illegal page_limit fail closed.
- Operators record the receipt; the cash journal is already validated for AIS to pull. HTTP presentment does not capture cards or call AIS.
- `operator_console` Storybook renders that receipt with tokenized amount due and status chip. Fixtures are applied-full and applied-partial.

## Unapplied-cash acceptance

- `POST /v1/payment-receipts/{payment_receipt_id}/unapplied-cash` parks leftover remittance against one same-tenant stored receipt. Replay of the same tenant and receipt returns the same `unapplied_cash_id`. PAN, CVC, and provider secrets are refused.
- #12 still rejects overpay. Omitting leftover fail-closes as `payment_receipt_already_consumed`. A supplied leftover must be a positive exact decimal that does not exceed the stored receipt.
- A known stored leftover presents one tenant-scoped statement with `unapplied_amount`, receipt snapshots, `unapplied_cash_status` (`parked`), and `next_operator_action` (`wait`).
- `GET /v1/unapplied-cash/{unapplied_cash_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/unapplied-cash` lists summaries as `{unapplied_cash, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{parked_at}|{unapplied_cash_id}`.
- Missing tenant, illegal cursor, and illegal page_limit fail closed.
- Operators park leftover against a stored receipt. HTTP does not apply leftover to another case, capture cards, or call AIS. Journal compose is an explicit later command.

## Unapplied-cash-application acceptance

- `POST /v1/collection-cases/{collection_case_id}/unapplied-cash-applications` applies one parked leftover onto one same-tenant open collection case. Replay of the same tenant and leftover returns the same `unapplied_cash_application_id`. PAN, CVC, and provider secrets are refused.
- The apply uses the full parked amount. Omitting `applied_amount` uses the parked leftover. A supplied amount must equal the parked leftover.
- Outstanding is reduced by the exact applied inclusive amount. Remaining zero does not settle the case. Next operator action is `collect`, `settle`, or `wait`.
- A known stored application presents one tenant-scoped statement with `applied_amount`, current remaining, `unapplied_cash_application_status` (`applied`), and `next_operator_action`.
- `GET /v1/unapplied-cash-applications/{unapplied_cash_application_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/unapplied-cash-applications` lists summaries as `{unapplied_cash_applications, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{applied_at}|{unapplied_cash_application_id}`.
- Missing tenant, illegal cursor, and illegal page_limit fail closed.
- Operators apply parked leftover, then collect the residual or settle at exact zero. First successful apply enqueues one existing #24 `unapplied_cash.applied` outbox event. HTTP does not auto-settle, capture cards, or call AIS. Journal compose is an explicit later command.

## Unapplied-cash-refund acceptance

- `POST /v1/unapplied-cash/{unapplied_cash_id}/refunds` records one commercial refund of parked leftover. Replay of the same tenant and leftover returns the same `unapplied_cash_refund_id` as `duplicate_replay`.
- The refund uses the full parked amount. Omitting `refund_amount` uses the parked leftover. A supplied amount must equal the parked leftover.
- The parked leftover row stays `parked`. Refund uniqueness consumes it. Apply fail-closes when a refund already exists.
- A known stored refund presents one tenant-scoped statement with `refund_amount`, parked leftover snapshot, `unapplied_cash_refund_status` (`recorded`), and `next_operator_action` (`wait`).
- `GET /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/unapplied-cash-refunds` lists summaries as `{unapplied_cash_refunds, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{refunded_at}|{unapplied_cash_refund_id}`.
- Missing tenant, leftover already applied, leftover not parked, currency mismatch, zero/negative leftover, mismatched amount, IEEE leftover, illegal cursor, and illegal page_limit fail closed.
- Operators refund unused parked leftover as a commercial fact. First successful refund enqueues one existing #24 `refund.recorded` outbox event. HTTP does not capture cards, call a PSP, or call AIS. Journal compose is an explicit later command.

## Refund-journal acceptance

- A known stored leftover refund produces one balanced exact-decimal `accounting_journal_proposal` that debits `unapplied_cash` and credits `cash_receipt`.
- A second propose of the same tenant and `unapplied_cash_refund_id` returns the same `proposal_id` as `duplicate_replay` and does not grow the store.
- Another tenant cannot see or propose from the first tenant's refund.
- Missing refunds, cross-tenant IDs, currency mismatch, float money, and zero or negative amounts fail closed.
- Leftover and refund rows are not changed. Status stays inside the proposal lifecycle and is never `posted`. AIS pulls validated proposals through existing GET journal-proposal routes.
- `POST /v1/unapplied-cash-refunds/{unapplied_cash_refund_id}/journal-proposals` is the explicit compose because #57 shipped without compose. PAN, CVC, and provider secrets are refused.
- Invoice-draft, cash, credit, write-off, payment-receipt, and `refund.recorded` contracts stay unchanged.

## Unapplied-cash-journal acceptance

- A known stored parked leftover produces one balanced exact-decimal `accounting_journal_proposal` that debits `cash_receipt` and credits `unapplied_cash`.
- A second propose of the same tenant and `unapplied_cash_id` returns the same `proposal_id` as `duplicate_replay` and does not grow the store.
- Another tenant cannot see or propose from the first tenant's leftover.
- Missing leftovers, cross-tenant IDs, leftover not parked, currency mismatch, float money, and zero or negative amounts fail closed.
- Leftover, refund, and payment-receipt rows are not changed. Status stays inside the proposal lifecycle and is never `posted`. AIS pulls validated proposals through existing GET journal-proposal routes.
- `POST /v1/unapplied-cash/{unapplied_cash_id}/journal-proposals` is the explicit compose because #54 shipped without compose. PAN, CVC, and provider secrets are refused.
- Invoice-draft, cash, credit, write-off, refund-journal, and payment-receipt contracts stay unchanged.

## Unapplied-cash-application-journal acceptance

- A known stored leftover apply produces one balanced exact-decimal `accounting_journal_proposal` that debits `unapplied_cash` and credits `accounts_receivable`.
- A second propose of the same tenant and `unapplied_cash_application_id` returns the same `proposal_id` as `duplicate_replay` and does not grow the store.
- Another tenant cannot see or propose from the first tenant's application.
- Missing applications, cross-tenant IDs, currency mismatch, float money, and zero or negative amounts fail closed.
- Leftover, apply, refund, and payment-receipt rows are not changed. Status stays inside the proposal lifecycle and is never `posted`. AIS pulls validated proposals through existing GET journal-proposal routes.
- `POST /v1/unapplied-cash-applications/{unapplied_cash_application_id}/journal-proposals` is the explicit compose because #55 shipped without compose. PAN, CVC, and provider secrets are refused.
- Invoice-draft, cash, credit, write-off, refund-journal, leftover-park-journal, and payment-receipt contracts stay unchanged.

## Payment-intent-presentment acceptance

- `POST /v1/payment-intents` projects one #11 intent from a stored `collection_case_id`. Amount and currency come from the case. Replay of the same tenant, case snapshot, and contract version returns the same `payment_intent_id`. PAN, CVC, and provider secrets are refused.
- A known stored payment intent presents one tenant-scoped statement with `payment_amount`, `payment_intent_status` (`projected` / `cancelled` / `rejected`), and `next_operator_action` (`record_receipt` or `wait`).
- `GET /v1/payment-intents/{payment_intent_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/payment-intents` lists summaries as `{payment_intents, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100.
- Missing tenant, illegal cursor, and illegal page_limit fail closed.
- Operators create a projected payment intent, then record the receipt. HTTP presentment does not capture cards or call AIS.
- `operator_console` Storybook renders that intent with tokenized amount due and status chip. Fixtures are projected and cancelled.

## Collection-case-presentment acceptance

- A known stored collection case presents one tenant-scoped statement with `collection_outstanding`, `collection_case_status` (`open` / `dunning` / `settled`), last/next dunning notice codes when those rows exist, and `next_operator_action` (`collect`, `credit`, or `wait`).
- `GET /v1/collection-cases/{collection_case_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/collection-cases` lists summaries as `{collection_cases, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100.
- Missing tenant, illegal cursor, and illegal page_limit fail closed.
- Operators open the collection case, then collect or credit. HTTP presentment does not capture cards or call AIS.
- `operator_console` Storybook renders that case with tokenized amount due and status chip. Fixtures are open, dunning, and settled.

## Collection-aging-presentment acceptance

- A known tenant presents one aging statement with `current`, `days_1_30`, `days_31_60`, `days_61_90`, and `days_90_plus` buckets grouped by `currency_code`. Each bucket carries case count and exact inclusive outstanding. Currencies are never mixed in one sum.
- Only stored `open` or `dunning` cases with positive remaining are aged. Settled cases and exact-zero remaining are omitted.
- Due date is issued-invoice `due_at` when stored, otherwise `collection_case.opened_at`. `current` is not yet due or due today. Positive days are 1-30, 31-60, 61-90, then 90+.
- `GET /v1/collection-aging` is HTTP 200 for a known tenant. Missing tenant is HTTP 422 with no leak.
- Operators open the aging statement, then collect or credit. HTTP presentment does not capture cards, write money, or call AIS.
- `operator_console` Storybook renders those buckets with tokenized amount due. The fixture is morning USD aging.

## Account-statement-presentment acceptance

- A known tenant and `billing_account_id` present one statement grouped by `currency_code` with exact inclusive `issued_invoice_total`, `open_collection_remaining`, `applied_credit_total`, `write_off_total`, `parked_unapplied_cash`, and `refunded_unapplied_cash`. Currencies are never mixed in one sum.
- Money is attributed only through invoice-draft lines exclusive to that billing account. Mixed-account and lineless drafts are omitted.
- `GET /v1/billing-accounts/{billing_account_id}/statement` is HTTP 200 for the same tenant. Missing account is HTTP 404. Cross-tenant account is HTTP 403. Missing tenant is HTTP 422.
- Operators open the account statement, then collect, credit, park, apply, or refund. HTTP presentment does not capture cards, write money, or call AIS.

## Dunning-event-presentment acceptance

- A known stored `collection_dunning_event` presents one tenant-scoped statement with `dunning_event_id`, `collection_case_id`, `dunning_event_number`, `dunning_notice_code`, `occurred_at`, and `next_operator_action` (`wait` when the parent case is settled, otherwise `collect`).
- `GET /v1/dunning-events/{dunning_event_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/dunning-events` lists summaries as `{dunning_events, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{occurred_at}|{dunning_event_id}`.
- `POST /v1/collection-cases/{collection_case_id}/dunning-events` stays the #10 record. PAN, CVC, and provider secrets are refused on that write.
- Presentment never returns recipient PII, channel, provider id, delivery status, or notice body. It does not invent a send engine.
- Operators record the commercial reminder, then collect or credit.

## Webhook-outbox-event-presentment acceptance

- A known stored `webhook_outbox_event` presents one tenant-scoped statement with `outbox_event_id`, `event_type_code`, `source_id`, `payload_hash`, `occurred_at`, `enqueued_at`, `delivery_status`, `attempted_delivery_count`, and `next_operator_action` (`run_deliveries` while pending, otherwise `wait`).
- `GET /v1/webhook-outbox-events/{outbox_event_id}` is HTTP 200 for the same tenant. Cross-tenant or unknown is HTTP 404 with no leak.
- `GET /v1/webhook-outbox-events` lists summaries as `{webhook_outbox_events, next_cursor}`. Never `items` or `cursor`. `page_limit` defaults to 50 and maxes at 100. Cursor is `{enqueued_at}|{outbox_event_id}`.
- Commercial-fact enqueue and `POST /v1/webhook-deliveries` stay the #24 write path. PAN, CVC, and provider secrets are refused on that write.
- Presentment never returns `payload_json`, raw body, webhook secret, hash, prefix, signature, or callback auth. GET never publishes, sends, retries, or marks delivered.
- This is the Billing commercial webhook outbox, not the AIS posting-receipt outbox. Known event types, HMAC `X-CWL-Webhook-Signature: sha256=<hex>`, SSRF policy, and secret one-time return stay unchanged.
- Operators inspect the commercial webhook backlog, then run deliveries.

## AIS-outbox-drain acceptance

- An operator can drain AIS `posting_receipt` outbox events for a known tenant through `AisOutboxDrainService.drain_ais_outbox` or `POST /v1/ais-outbox-drains`.
- Empty unpublished `outbox_events` is success and performs zero receipt GETs.
- Matching uses equality against URNs constructed from our stored `proposal_id`: `urn:cwl:accounting:posting_receipt:{proposal_id}` and `urn:cwl:accounting:general_journal:{proposal_id}`. Billing does not parse `payload_reference`.
- Receipt lookup stays `GET /posting-receipts?idempotency_key=` with the stored Billing key (`invoice_draft`, `cash_receipt`, or `credit_adjustment` as published). The payload URN is never the query.
- A successful or existing observation for the matched proposal POSTs `/outbox-events/{outbox_event_id}/publish`. AIS 403 is cross-tenant and is not retried as another tenant. AIS 404 does not invent a row.
- `journal_reversal` and `period_close` are not drained. `proposal_status` stays `validated`.
- Missing tenant, unconfigured AIS, insecure `AIS_BASE_URL`, invalid outbox envelopes, and transport failure fail closed. There is no scheduler.
- Drain AIS outbox, then store the receipt observation; AIS may keep being polled only when the outbox is non-empty.

## Credit-adjustment acceptance

- A known invoice draft records one commercial credit whose exact amount does not exceed remaining adjustable consideration.
- A full credit of the draft total zeros remaining adjustable. If a collection case exists, outstanding is reduced by the same amount and remaining zero marks the case `settled`.
- A partial credit leaves residual adjustable consideration and, when a case exists, residual outstanding.
- A second credit of the same tenant, `invoice_draft_id`, amount, reason, source-payload hash, and contract version returns the same `credit_adjustment_id` and `proposal_id`.
- Another tenant cannot see or credit the first tenant's draft.
- A taxed credit splits inclusive `credit_amount` proportionally: `credit_tax_amount = round_half_even(credit_amount * tax_amount / tax_inclusive_amount)`. Exclusive plus tax equals the credit. A full inclusive credit reconstructs the original exclusive and tax.
- The paired journal proposal for a taxed credit debits `usage_revenue` and `tax_payable` and credits `accounts_receivable`. Untaxed credits stay two-line revenue/AR. Status stays `validated` and is never `posted`.
- Closed reasons are `rating_correction`, `goodwill`, and `billing_error`. Unknown codes fail closed.
- Missing drafts, cross-tenant IDs, float money, zero or negative amounts, over-remaining amounts, credits that exceed case outstanding, and invalid tax splits fail closed.
- Operators record the credit; AIS pulls the validated three-line unwind. This slice does not call AIS, post, emit journal-reversals, refund-to-card, or chargeback.

## Credit-journal-proposal acceptance

- A stored credit adjustment composes or replays one balanced credit `accounting_journal_proposal` through `AccountingExportService.propose_credit_journal`.
- Identity is `(tenant_account_id, credit_adjustment_id)`. Credit accept already writes the first row. A second compose returns the same `proposal_id` as `duplicate_replay`.
- Untaxed lines debit semantic `usage_revenue` and credit semantic `accounts_receivable` for the exact inclusive credit. Taxed credits reuse the existing `tax_payable` unwind on the same journal.
- `POST /v1/credit-adjustments/{credit_adjustment_id}/journal-proposals` is the explicit compose. AIS pull stays `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
- `proposal_status` stays `validated` and is never `posted`. Missing or cross-tenant credits, currency mismatch, and zero or negative amounts fail closed.
- Operators record the credit, then POST compose if needed. This slice does not call AIS, invent a second journal store, webhook, write-off, settlement, payment, or statutory account ID.

## Void-journal-proposal acceptance

- A stored issued-invoice void composes one balanced reverse `accounting_journal_proposal` through `AccountingExportService.propose_void_journal`.
- Identity is `(tenant_account_id, issued_invoice_void_id)`. A second compose returns the same `proposal_id` as `duplicate_replay`.
- Untaxed lines debit semantic `usage_revenue` and credit semantic `accounts_receivable` for the exact inclusive voided amount. Taxed unused issues also debit `tax_payable` on the same journal.
- The original invoice journal is bound by Billing `proposal_id` / stored invoice-draft journal identity only. The payload never includes `journal_entry_id` or a statutory account ID.
- `POST /v1/issued-invoice-voids/{issued_invoice_void_id}/journal-proposals` is the explicit compose. AIS pull stays `GET /v1/journal-proposals` and `GET /v1/journal-proposals/{proposal_id}`.
- `proposal_status` stays `validated` and is never `posted`. Missing or cross-tenant voids, currency mismatch, and zero or negative amounts fail closed.
- Operators void an unused issued invoice, then POST compose. This slice does not call AIS, invent a webhook, PSP, write-off, settlement rewrite, negative invoice, or statutory account ID.

## Usage-ingestion acceptance

- A known event batch stores one usage set; replaying the same batch returns `duplicate_replay` and does not grow that set.
- A replay with the same source-event key and a different source-payload hash or contract version is rejected.
- A usage event cannot attribute a billing account, principal, or credential from another tenant.
- Measurement quantities persist as exact decimals, never as binary floating-point values.
- Time-window queries return only the tenant's events whose `occurred_at` lies in `[window_started_at, window_ended_at)`.
- Ingestion does not create a posted accounting journal.

## Initial milestone acceptance

- All published schemas declare Draft 2020-12 and pass the repository's offline conformance fixtures.
- Prompt and response text are rejected from usage events.
- `posted` is rejected from accounting proposal status.
- Accounting proposals fail semantic validation when line numbers repeat or debit and credit totals differ.
- Attribution and usage references are tenant-scoped by composite foreign keys.
- SQL object names satisfy the two-word `snake_case` rule.
- Mutable GitHub Action tags are rejected.
- Repository tooling, the usage-ingestion package, the windowed-rating package, invoice-draft, accounting-export, collection-case, payment-intent, payment-settlement, cash-journal export, the HTTP accept surface, journal-proposal query, posting-receipt observation, credit adjustment, the versioned rate-card catalog, tax assessment, tax-payable unwind, invoice-draft presentment, collection-case presentment, payment-intent presentment, payment-receipt presentment, tenant API credentials, operator-console fixture checks, webhook outbox, AIS outbox drain, tax-assessment presentment, posting-receipt observation presentment, webhook-delivery presentment, tenant-api-credential presentment, webhook-subscription presentment, dunning-event presentment, and webhook-outbox-event presentment reach 100% statement and branch coverage.
