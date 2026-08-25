# ADR 0026: Compose Cash Journal On Payment-Receipt Accept

**Status:** Accepted

## Context

#12 persists an applied `payment_receipt` and reduces collection outstanding.  #13 already proposes a cash journal from that receipt with `{tenant}:cash_receipt:{payment_receipt_id}:{source_payload_hash}:v{version}`, debit `cash_receipt`, credit `accounts_receivable`, and `proposal_status` `validated`.  #24 already enqueues `journal_proposal.validated` from that propose.  #28 presents the receipt and tells operators to record it, then wait for AIS.  AIS still had nothing to pull until a second `POST /v1/cash-journal-proposals`.  Helland (2012) requires that a replay of the same propose command return the same stored identity.

This repository is not the statutory accounting authority.  Compose must reuse the existing #13 shape.  It must not invent a second journal, call AIS, flip `proposal_status`, capture cards, or emit statutory IDs.

## Decision

- On `PaymentSettlementService.record_payment_receipt` accept, and on duplicate replay of an already-applied receipt, call the existing `AccountingExportService.propose_cash_journal`.
- Keep `POST /v1/cash-journal-proposals` as a manual replay path.  Prefer the receipt write so operators do not need a second call.
- Do not change cash identity, line roles, or the AIS idempotency key.
- Replay of the same tenant, receipt, hash, and contract version writes no second proposal and does not enqueue a second `journal_proposal.validated` row.
- Do not add a presentment field.  Customer copy is: record the receipt; the cash journal is already validated for AIS to pull.

## Consequences

- AIS can pull the cash proposal from `GET /v1/journal-proposals` immediately after receipt accept.
- Manual cash propose remains idempotent replay.
- Receipt status stays `applied`.  Journal status stays `validated`.
