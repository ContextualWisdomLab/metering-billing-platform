BEGIN;

CREATE TABLE billing_core.collection_case (
    collection_case_id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_account_id uuid NOT NULL REFERENCES billing_core.tenant_account (tenant_account_id),
    invoice_draft_id uuid NOT NULL,
    currency_code text NOT NULL,
    collection_case_status text NOT NULL CHECK (collection_case_status IN ('open', 'dunning')),
    outstanding_amount numeric(38, 12) NOT NULL CHECK (outstanding_amount > 0),
    opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_account_id, invoice_draft_id),
    UNIQUE (tenant_account_id, collection_case_id),
    FOREIGN KEY (tenant_account_id, invoice_draft_id)
        REFERENCES billing_core.invoice_draft (tenant_account_id, invoice_draft_id)
);

CREATE TABLE billing_core.collection_dunning_event (
    collection_dunning_event_id uuid PRIMARY KEY DEFAULT uuidv7(),
    collection_case_id uuid NOT NULL,
    tenant_account_id uuid NOT NULL,
    dunning_event_number integer NOT NULL CHECK (dunning_event_number > 0),
    dunning_notice_code text NOT NULL CHECK (dunning_notice_code IN ('first_notice', 'overdue_notice')),
    occurred_at timestamptz NOT NULL,
    UNIQUE (collection_case_id, dunning_notice_code),
    UNIQUE (collection_case_id, dunning_event_number),
    FOREIGN KEY (tenant_account_id, collection_case_id)
        REFERENCES billing_core.collection_case (tenant_account_id, collection_case_id)
);

COMMIT;
