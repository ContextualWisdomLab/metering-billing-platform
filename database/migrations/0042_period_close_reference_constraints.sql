BEGIN;

ALTER TABLE billing_core.billing_period
    ADD CONSTRAINT billing_period_opened_by_not_blank
    CHECK (btrim(opened_by) <> '');

ALTER TABLE billing_core.billing_period_transition
    ADD CONSTRAINT billing_period_transition_actor_not_blank
    CHECK (btrim(actor_reference) <> ''),
    ADD CONSTRAINT billing_period_transition_authorization_not_blank
    CHECK (btrim(authorization_reference) <> ''),
    ADD CONSTRAINT billing_period_transition_reason_not_blank
    CHECK (btrim(transition_reason) <> '');

ALTER TABLE billing_core.fx_rate
    ADD CONSTRAINT fx_rate_source_not_blank
    CHECK (btrim(rate_source) <> '');

ALTER TABLE billing_core.reconciliation_line
    ADD CONSTRAINT reconciliation_line_provider_account_not_blank
    CHECK (btrim(provider_account_reference) <> '');

ALTER TABLE billing_core.reconciliation_exception
    ADD CONSTRAINT reconciliation_exception_next_action_not_blank
    CHECK (btrim(next_action) <> '');

ALTER TABLE billing_core.reconciliation_resolution
    ADD CONSTRAINT reconciliation_resolution_owner_not_blank
    CHECK (btrim(owner_reference) <> ''),
    ADD CONSTRAINT reconciliation_resolution_reason_not_blank
    CHECK (btrim(resolution_reason) <> ''),
    ADD CONSTRAINT reconciliation_resolution_evidence_not_blank
    CHECK (btrim(evidence_reference) <> ''),
    ADD CONSTRAINT reconciliation_resolution_maker_not_blank
    CHECK (btrim(maker_reference) <> ''),
    ADD CONSTRAINT reconciliation_resolution_checker_not_blank
    CHECK (btrim(checker_reference) <> '');

COMMIT;
