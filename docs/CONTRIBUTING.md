# Contributing and repository operation

This file holds contributor, agent, and CI procedure. It is not the buyer/operator product story. The product boundary and independent-run contract live in the root [README](../README.md).

## Writer boundaries

These rules are already stated in the draft foundation `AGENTS.md` / `CLAUDE.md` on [PR #1](https://github.com/ContextualWisdomLab/metering-billing-platform/pull/1). They apply to any change that claims commercial or accounting behavior:

- Preserve the billing-versus-accounting boundary. This repository may export an `accounting_journal_proposal`. It must not own posted journals, charts, books, fiscal close, or financial statements.
- Never use a provider object ID as an internal primary key.
- Never let a webhook directly grant entitlement or post accounting.
- Do not store card data, PAT plaintext, prompt text, response text, or provider secrets.
- Use exact decimals for money and billable quantities. Binary floating-point types are forbidden for those values.
- Keep normalized facts relational; raw provider payloads belong in immutable object storage when that milestone exists.
- Use two-or-more-word `snake_case` database identifiers.
- Historical facts are corrected by compensating records, not in-place mutation.
- Document every public API and every accounting or monetary invariant when those APIs exist.
- Update architecture, ADRs, and CHANGELOG when authority or behavior changes.

Do not treat “may be composed by Naruon” as a defect. Naruon is the CWL composition hub. Independent run must still not require Naruon or any sibling checkout.

## What is in scope for a documentation-only change

A README or `docs/` change may describe:

- product intent that already exists in this repository or in an open pull request against it;
- the default-branch bootstrap state;
- sibling call shape only when a schema or API in this repository defines it;
- local commands that exist on a named branch and have been run, or that are documented as absent.

It must not invent endpoints, authentication, papers, certifications, or readiness.

## Exact-head CI

The foundation branch defines a repository-local workflow (`.github/workflows/ci.yml`) that:

- checks out the exact commit under test with a commit-pinned `actions/checkout`;
- installs Python 3.13 with a commit-pinned `actions/setup-python`;
- installs hash-locked quality tooling from `requirements-quality.txt`;
- runs unit tests under branch coverage, fails under 100% statement and branch coverage, runs `scripts/validate_repository.py`, and compiles `scripts` and `tests`.

That workflow is not on the default branch until the foundation merges. Mutable GitHub Action tags (`@v4`, `@main`) are rejected by the foundation validator.

On PR #1 head `bd87bde`, the documented offline block succeeds on the foundation branch. Those files are not on `develop` until the foundation merges.

Organization required workflows from `ContextualWisdomLab/.github` (OpenCode, Strix, merge scheduler, and related jobs) may also run on pull requests. Those jobs judge the current head SHA. A cancelled or superseded check is a queue or evidence blocker, not a source-code finding.

## Successor heads and PR stacking

- Review and merge evidence must match the current head commit (`--match-head-commit` / exact-head review). An approval or check on a previous SHA is not evidence for a later push.
- If a pull request is behind its base, an update creates a **successor head**. The new SHA must pass review and required checks again. Being behind is an update request, not a merge signal.
- Stacked follow-on work should wait for the predecessor head it actually depends on, or should target the default branch when the change is independent. This README rewrite is independent of the foundation implementation PR and must not silently rewrite that PR's contracts.
- Do not force-push over a head that already has required-check or review evidence unless the branch policy explicitly requires a rebase; prefer a new commit so the successor head is obvious.

## Do not merge from this file or from the README

Human or agent writers do not merge pull requests in this repository as part of ordinary documentation or foundation work. Merge readiness is a separate operator decision.

Keep implementation PRs in Draft until the exact PR head has the repository's required checks and independent review. A draft state is not hidden approval.

## Approve-gate language (not a product claim)

OpenCode approval in the CWL org is evidence-gated: the review must name changed files, structural evidence, a change-flow DAG, and an observed verification result. That gate is CI/review procedure.

It is **not** a statement that the Metering Billing Platform is commercially ready, certified, or callable.

A GitHub Actions-authored review is not OpenCode approval evidence. Pending CodeRabbit or required-check evidence is a wait state, not a hard product blocker and not a substitute for the README.

## CodeRabbit and review summaries

CodeRabbit, OpenCode, and similar review comments are review artifacts. They are not the product specification, not the MSA, and not buyer-facing readiness.

Do not copy a review summary into the root README as if it were shipped capability. If a review finds a real contract or doc defect, fix the source document.

## Next implementation increment (foundation PR, not this change)

After the foundation contracts merge, the foundation README's stated next increment is immutable usage ingestion and idempotent deduplication, before any payment-provider adapter. That work is out of scope for a buyer/operator README rewrite.
