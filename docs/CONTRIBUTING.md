# Contributing and repository operation

This file holds contributor, agent, and CI procedure for the Metering Billing
Platform. The product boundary and independent-run contract live in the root
[README](../README.md).

## Writer boundaries

These rules mirror the root `AGENTS.md` / `CLAUDE.md` and apply to any change
that claims commercial or accounting behavior:

- Preserve the billing-versus-accounting boundary. This repository may export
  an `accounting_journal_proposal`. It must not own posted journals, charts,
  books, fiscal close, or financial statements.
- Never use a provider object ID as an internal primary key.
- Never let a webhook directly grant entitlement or post accounting.
- Do not store card data, PAT plaintext, prompt text, response text, or
  provider secrets.
- Use exact decimals for money and billable quantities. Binary floating-point
  types are forbidden for those values.
- Keep normalized facts relational; raw provider payloads belong in immutable
  object storage with hashes and retention references.
- Use two-or-more-word `snake_case` database identifiers.
- Historical facts are corrected by compensating records, not in-place
  mutation.
- Document every public API and every accounting or monetary invariant.
- Update architecture docs, ADRs, and `CHANGELOG.md` when authority or
  behavior changes.

Do not treat "may be composed by Naruon" as a defect. Naruon is the CWL
composition hub. Independent run must still not require Naruon or any sibling
checkout.

## What is in scope for a documentation-only change

A README or `docs/` change may describe:

- product intent that already exists on the default branch;
- commands that exist and have been run;
- a sibling call shape only when a schema or API in this repository defines it.

It must not invent endpoints, authentication, papers, certifications, or
readiness.

## Exact-head CI

The default branch carries the repository-local workflow
(`.github/workflows/ci.yml`, "Foundation CI"), which:

- checks out the exact commit under test with a commit-pinned
  `actions/checkout`;
- installs Python 3.13 with a commit-pinned `actions/setup-python`;
- installs hash-locked quality tooling from `requirements-quality.txt` and
  runtime dependencies from `requirements-runtime.txt`;
- starts a PostgreSQL 18 service container and applies every checked-in
  migration through `scripts/migrate_postgres.py`;
- runs the unit suite under branch coverage, fails under 100% statement and
  branch coverage, runs `scripts/validate_repository.py .`, and compiles
  `scripts`, `tests`, and `metering_billing`.

Mutable GitHub Action tags (`@v4`, `@main`) are rejected by the repository
validator. Review and merge evidence must match the current head commit; an
approval or check on a previous SHA is not evidence for a later push.

Organization required workflows from `ContextualWisdomLab/.github`
(OpenCode review, Noema review, Strix, Semgrep, Trivy, OSV, Scorecard, and
related jobs) also run on pull requests. Those jobs judge the current head
SHA. A cancelled or superseded check is a queue or evidence state, not a
source-code finding.

## Successor heads and PR stacking

- If a pull request is behind its base, an update creates a **successor
  head**. The new SHA must pass review and checks again.
- Stacked follow-on work targets the default branch once its predecessor has
  merged; independent work targets the default branch directly.
- Do not force-push over a head that already has check or review evidence;
  prefer a new commit so the successor head is obvious.
- When a stacked base branch disappears during a release-train merge, recreate
  the PR against the default branch rather than reopening against history.

## Review gates

Merge readiness is judged by current-head review evidence: CodeRabbit (or its
authoritative skip/limit notice), Devin review findings triaged to resolution,
the central OpenCode/Noema review workflows, and the Foundation CI result.
Review comments are artifacts, not the product specification; if a review
finds a real contract or doc defect, fix the source document.

## Local validation

See [docs/doctoring/VALIDATION.md](doctoring/VALIDATION.md) for the exact
offline command sequence, including the dedicated PostgreSQL 18 test database
requirement.
