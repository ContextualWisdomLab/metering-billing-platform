BEGIN;

ALTER TABLE billing_core.spend_budget
    ADD COLUMN spend_budget_status text NOT NULL DEFAULT 'published';

ALTER TABLE billing_core.spend_budget
    ADD CONSTRAINT spend_budget_status_published
        CHECK (spend_budget_status = 'published');

COMMIT;
