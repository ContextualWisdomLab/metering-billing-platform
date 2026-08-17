# Contributing

This repository is the CWL commercial usage and billing authority. It is provider-neutral and accounting-aware. It is not the statutory accounting authority.

Before changing behavior:

1. Identify the authoritative fact being changed.
2. Add a failing test for monetary and idempotency behavior.
3. Preserve immutable history through correction or reversal records.
4. Keep provider integration behind capability-specific ports.
5. Export accounting proposals without claiming legal posting.

Update architecture, ADRs, [CHANGELOG.md](CHANGELOG.md), and [docs/doctoring/STANDARD_TRACEABILITY.md](docs/doctoring/STANDARD_TRACEABILITY.md) when authority or a claimed standard mapping changes. Cite only standards already claimed in the product docs. Do not introduce a source that the architecture does not use.

## Local validation

Install the hash-locked quality dependency, run the unit suite, enforce 100% statement and branch coverage, and validate repository contracts. Copy-paste command dumps are in [docs/doctoring/VALIDATION.md](docs/doctoring/VALIDATION.md).

```bash
python3 -m pip install --only-binary=:all: --require-hashes -r requirements-quality.txt
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage run --branch -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage report --fail-under=100 --show-missing
python3 scripts/validate_repository.py
```

The validator is offline and dependency-free so exact-head CI can check the checked-in contracts without downloading transitive packages.

## Exact-head CI

Foundation CI checks out the exact pull-request or push head and pins GitHub Actions to full commit SHAs. Mutable tags such as `@v4` are rejected by repository-contract tests.

The workflow:

- installs Python 3.13 and the hash-locked `coverage` wheel;
- runs branch coverage over `scripts` and fails under 100%;
- runs `python scripts/validate_repository.py .`;
- compiles `scripts` and `tests`.

Do not add an unpinned Action, a network-dependent schema validator, or a coverage exemption. Production statement and branch coverage of repository tooling must remain 100%.

## Successor pull-request order

The current milestone is the contract foundation: schemas, the initial PostgreSQL core, and offline validation. Implement the next increment in this order:

1. Immutable usage ingestion and idempotent deduplication, including CloudEvents-compatible envelopes for source and event identity.
2. Deterministic rating, credits, and spend reservation.
3. Invoice-intent generation with explainable lines.
4. A payment-provider adapter behind capability-specific ports. Do not add an adapter before ingestion and deduplication exist.
5. Settlement reconciliation and journal-proposal export against live accounting receipts.

Do not use a provider object ID as an internal primary key. Do not let a webhook grant entitlement or post accounting. Prompt text, response text, card data, PAT plaintext, and provider secrets stay out of contracts and storage.
