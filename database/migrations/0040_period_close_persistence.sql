BEGIN;

CREATE TABLE billing_core.billing_period (
    period_id uuid PRIMARY KEY,
    tenant_account_id uuid NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    opened_at timestamptz NOT NULL,
    opened_by text NOT NULL,
    period_contract_version integer NOT NULL CHECK (period_contract_version >= 1),
    UNIQUE (tenant_account_id, period_id),
    FOREIGN KEY (tenant_account_id)
        REFERENCES billing_core.tenant_account (tenant_account_id),
    CHECK (period_start < period_end),
    CHECK (opened_by <> '')
);

CREATE TABLE billing_core.billing_period_transition (
    transition_id uuid PRIMARY KEY,
    tenant_account_id uuid NOT NULL,
    period_id uuid NOT NULL,
    transition_number integer NOT NULL CHECK (transition_number > 0),
    from_status text NOT NULL,
    to_status text NOT NULL,
    actor_reference text NOT NULL,
    authorization_reference text NOT NULL,
    transition_reason text NOT NULL,
    transitioned_at timestamptz NOT NULL,
    UNIQUE (tenant_account_id, period_id, transition_id),
    UNIQUE (tenant_account_id, period_id, transition_number),
    FOREIGN KEY (tenant_account_id, period_id)
        REFERENCES billing_core.billing_period (tenant_account_id, period_id),
    CHECK (from_status || ':' || to_status IN (
        'open:soft_closed',
        'soft_closed:reconciled',
        'reconciled:invoiced',
        'invoiced:hard_closed'
    )),
    CHECK (actor_reference <> ''),
    CHECK (authorization_reference <> ''),
    CHECK (transition_reason <> '')
);

CREATE TABLE billing_core.fx_rate (
    fx_rate_id uuid PRIMARY KEY,
    rate_source text NOT NULL,
    rate_type text NOT NULL CHECK (rate_type IN ('spot', 'accounting', 'provider')),
    base_currency text NOT NULL CHECK (base_currency ~ '^[A-Z]{3}$'),
    quote_currency text NOT NULL CHECK (quote_currency ~ '^[A-Z]{3}$'),
    fx_rate_value numeric NOT NULL CHECK (fx_rate_value > 0),
    rate_precision integer NOT NULL CHECK (rate_precision >= 0),
    effective_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    fx_rate_contract_version integer NOT NULL CHECK (fx_rate_contract_version >= 1),
    CHECK (rate_source <> '')
);

CREATE TABLE billing_core.fx_conversion (
    fx_conversion_id uuid PRIMARY KEY,
    fx_rate_id uuid NOT NULL REFERENCES billing_core.fx_rate (fx_rate_id),
    source_amount numeric NOT NULL,
    source_currency text NOT NULL CHECK (source_currency ~ '^[A-Z]{3}$'),
    quote_amount numeric NOT NULL,
    quote_currency text NOT NULL CHECK (quote_currency ~ '^[A-Z]{3}$'),
    quote_minor_units integer NOT NULL CHECK (quote_minor_units BETWEEN 0 AND 4),
    fx_rate_value numeric NOT NULL CHECK (fx_rate_value > 0),
    rate_precision integer NOT NULL CHECK (rate_precision >= 0),
    rounding_mode text NOT NULL CHECK (rounding_mode = 'ROUND_HALF_UP'),
    converted_at timestamptz NOT NULL,
    fx_conversion_contract_version integer NOT NULL CHECK (fx_conversion_contract_version >= 1)
);

CREATE TABLE billing_core.reconciliation_line (
    reconciliation_line_id uuid PRIMARY KEY,
    tenant_account_id uuid NOT NULL,
    period_id uuid NOT NULL,
    provider_account_reference text NOT NULL,
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    internal_currency_code text NOT NULL CHECK (internal_currency_code ~ '^[A-Z]{3}$'),
    provider_currency_code text NOT NULL CHECK (provider_currency_code ~ '^[A-Z]{3}$'),
    cash_currency_code text NOT NULL CHECK (cash_currency_code ~ '^[A-Z]{3}$'),
    internal_expected_amount numeric NOT NULL,
    provider_actual_amount numeric NOT NULL,
    cash_actual_amount numeric NOT NULL,
    provider_fee_amount numeric NOT NULL CHECK (provider_fee_amount >= 0),
    withheld_tax_amount numeric NOT NULL CHECK (withheld_tax_amount >= 0),
    reserve_amount numeric NOT NULL CHECK (reserve_amount >= 0),
    expected_cash_amount numeric NOT NULL,
    reconciliation_line_status text NOT NULL CHECK (reconciliation_line_status IN ('matched', 'exception')),
    assessed_at timestamptz NOT NULL,
    reconciliation_line_contract_version integer NOT NULL CHECK (reconciliation_line_contract_version >= 1),
    UNIQUE (tenant_account_id, reconciliation_line_id),
    FOREIGN KEY (tenant_account_id, period_id)
        REFERENCES billing_core.billing_period (tenant_account_id, period_id),
    CHECK (provider_account_reference <> ''),
    CHECK (expected_cash_amount = provider_actual_amount - provider_fee_amount - withheld_tax_amount - reserve_amount)
);

CREATE TABLE billing_core.reconciliation_exception (
    reconciliation_line_id uuid NOT NULL REFERENCES billing_core.reconciliation_line (reconciliation_line_id),
    exception_number integer NOT NULL CHECK (exception_number > 0),
    exception_code text NOT NULL CHECK (exception_code IN (
        'currency_mismatch',
        'price_mismatch',
        'provider_fee_mismatch',
        'settlement_mismatch'
    )),
    next_action text NOT NULL,
    PRIMARY KEY (reconciliation_line_id, exception_number),
    UNIQUE (reconciliation_line_id, exception_code),
    CHECK (next_action <> '')
);

COMMIT;
