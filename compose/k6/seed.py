"""Seed one demo tenant catalog for the compose k6 baseline run.

The durable PostgreSQL system of record never invents tenants, billing
accounts, principals, credential records, or meters, so the load test seeds
the minimal same-tenant catalog through this one-shot script before any
``/v1`` call runs.  The script is idempotent: every registration is an
insert-or-return, so re-running it against an already-seeded database is a
safe no-op.

Run it inside the project network:

    docker compose -f compose/docker-compose.yml run --rm \
        -e METERING_BILLING_POSTGRES_DSN="$METERING_BILLING_POSTGRES_DSN" \
        billing_api python /app/compose/k6/seed.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from metering_billing.postgres_usage_ledger import PostgresUsageLedger


CATALOG_START = datetime(2026, 1, 1, tzinfo=UTC)
TENANT_REFERENCE = "urn:cwl:k6_tenant_001"
ACCOUNT_REFERENCE = f"{TENANT_REFERENCE}:billing_account:k6_account_001"
PRINCIPAL_REFERENCE = f"{TENANT_REFERENCE}:billing_principal:k6_principal_001"
CREDENTIAL_REFERENCE = f"{TENANT_REFERENCE}:credential_record:k6_credential_001"


def seed_demo_tenant(dsn: str) -> None:
    """Register the minimal demo-tenant catalog rows the baseline reads."""
    ledger = PostgresUsageLedger.connect(dsn)
    try:
        ledger.register_tenant(TENANT_REFERENCE)
        ledger.register_billing_account(TENANT_REFERENCE, ACCOUNT_REFERENCE)
        ledger.register_billing_principal(
            TENANT_REFERENCE, PRINCIPAL_REFERENCE, "github_workflow", CATALOG_START
        )
        ledger.register_credential_record(
            TENANT_REFERENCE,
            CREDENTIAL_REFERENCE,
            "api_key",
            CREDENTIAL_REFERENCE,
        )
        ledger.register_credential_assignment(
            TENANT_REFERENCE,
            CREDENTIAL_REFERENCE,
            PRINCIPAL_REFERENCE,
            ACCOUNT_REFERENCE,
            CATALOG_START,
        )
        meter = ledger.register_meter_definition(
            "gen_ai_output_token", 1, "token", "sum", CATALOG_START
        )
        ledger.register_meter_quality_rule(
            meter.meter_definition_id, "provider_reported", "billable"
        )
    finally:
        ledger.close()


def main() -> int:
    """Seed from ``METERING_BILLING_POSTGRES_DSN`` and report success."""
    dsn = os.environ.get("METERING_BILLING_POSTGRES_DSN")
    if not dsn:
        raise SystemExit("METERING_BILLING_POSTGRES_DSN must be set to seed the demo tenant")
    seed_demo_tenant(dsn)
    print(f"seeded demo tenant {TENANT_REFERENCE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
