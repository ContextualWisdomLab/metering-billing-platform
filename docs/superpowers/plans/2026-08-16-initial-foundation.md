# Metering Billing Platform Initial Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Establish a reviewable, testable initial repository contract that separates metering and billing authority from statutory accounting authority.

**Architecture:** Use versioned JSON Schema contracts and a normalized PostgreSQL migration as the first public boundary. Keep repository validation in a small dependency-free Python module so the empty repository can prove its contracts before runtime services or provider SDKs are introduced.

**Tech Stack:** Python 3.13 repository tooling, JSON Schema Draft 2020-12, PostgreSQL 18-compatible SQL, GitHub Actions with commit-pinned actions.

## Global Constraints

- All database object names contain at least two `snake_case` words.
- Database design is third-normal-form oriented; provider IDs are mapped, never embedded in core records.
- Accounting journal exports are proposals and cannot represent posted journals.
- No cardholder data, PAT plaintext, prompt text, response text, or provider secret is stored in contracts.
- Repository tooling must achieve 100% statement and branch coverage.
- No unpinned GitHub Action reference is permitted.

---

### Task 1: Repository contract tests

**Files:**
- Create: `tests/test_repository_contracts.py`
- Create: `scripts/validate_repository.py`

**Interfaces:**
- Produces: `validate_repository(repository_root: pathlib.Path) -> tuple[str, ...]`

- [x] **Step 1: Write tests for required files, valid schemas, SQL naming, accounting status, and clean source text.**
- [x] **Step 2: Run `python3 -m unittest discover -s tests -p 'test_*.py'` and verify import failure because `scripts.validate_repository` does not exist.**
- [x] **Step 3: Implement the minimum validator that returns a tuple of errors and never mutates repository content.**
- [x] **Step 4: Re-run the tests and require a clean pass.**
- [x] **Step 5: Run branch coverage and require 100%.**

### Task 2: Versioned contracts and normalized migration

**Files:**
- Create: `schemas/usage-event.schema.json`
- Create: `schemas/provider-capability.schema.json`
- Create: `schemas/accounting-journal-proposal.schema.json`
- Create: `database/migrations/0001_initial_billing_core.sql`

**Interfaces:**
- Produces: Draft 2020-12 schema contracts and a PostgreSQL 18-compatible migration.

- [x] **Step 1: Add positive and negative schema fixtures to the repository-contract tests.**
- [x] **Step 2: Verify the new assertions fail while schemas and migration are absent.**
- [x] **Step 3: Add schemas with closed objects, decimal values encoded as strings, idempotency keys, quality codes, and proposal-only accounting states.**
- [x] **Step 4: Add the initial 3NF migration with provider-neutral mappings and append-only event records.**
- [x] **Step 5: Run tests and coverage to green.**
- [x] **Step 6: Add tenant-scoped composite foreign keys and meter-specific quality-rule enforcement after self-review.**

### Task 3: Architecture and governance baseline

**Files:**
- Create: `README.md`
- Create: `docs/PRD.md`
- Create: `docs/TRD.md`
- Create: `docs/ARCHITECTURE.md`
- Create: `docs/DATA_MODEL.md`
- Create: `docs/ACCOUNTING_BOUNDARY.md`
- Create: `docs/adr/0001-commercial-authority.md`
- Create: `docs/adr/0002-accounting-boundary.md`
- Create: `AGENTS.md`
- Create: `CLAUDE.md`
- Create: `CHANGELOG.md`

**Interfaces:**
- Produces: human-readable authority, integration, and development contracts.

- [x] **Step 1: Extend required-file tests to cover governance and architecture documents.**
- [x] **Step 2: Verify tests fail for missing documents.**
- [x] **Step 3: Add concise documents with no placeholders and with explicit next-action language.**
- [x] **Step 4: Run tests and coverage to green.**

### Task 4: Exact-head CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.gitignore`

**Interfaces:**
- Produces: `Repository contracts` GitHub check.

- [x] **Step 1: Extend validator tests to reject mutable action tags.**
- [x] **Step 2: Verify the test fails before the workflow exists.**
- [x] **Step 3: Add a minimum-permission, concurrency-controlled workflow with commit-pinned checkout and setup-python actions.**
- [x] **Step 4: Run the same commands locally that CI will run.**
- [x] **Step 5: Commit and open a draft pull request for independent review.**


## Verification Evidence

- The first contract-test run failed because `scripts.validate_repository` did not exist, proving the initial red state.
- The live local suite executes 53 tests.
- `scripts` and `metering_billing` record 1104 statements and 374 branches at 100% coverage.
- `python scripts/validate_repository.py .` reports a valid repository.
- `python -m compileall -q scripts tests metering_billing` completes without errors.
