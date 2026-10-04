# ADR 0129: Route Foundation CI to the isolated Linux runner pool

## Status

Accepted for the local candidate. Runtime capacity and access must be verified
before claiming hosted acceptance. This does not authorize changes to runner
registration, group access, production hosts, or central review workflow custody.

## Context

PR 182's Foundation CI and managed default CodeQL jobs failed before any step
started. Their actual check annotations report an account lock caused by a
billing issue; the earlier source-bound 720-test/100% local gate is independent
of that platform denial. A REST rate-limit denial also occurred, but normal Git
and GraphQL reads still worked. These are distinct failures, not source defects.

The user's standing CI direction is self-hosted execution. Current online org
runners belong to restricted central review/control or GPU groups. The Default
runner group has zero members. Merely adding generic self-hosted labels would
not establish eligible capacity and must not cause PR-controlled tests to run
on privileged central reviewer hosts.

## Decision

Select the named `CWL CI isolated` group together with exact labels
`self-hosted`, `Linux`, `X64`, and `cwlab-ci-isolated`. The group and labels are
joint requirements, not alternatives. Preserve PostgreSQL 18 service health
checks, hash-locked dependency installation, pinned action identities,
credential-free checkout, branch coverage collection, 100% enforcement,
repository validation, compilation and the existing job timeout.

Do not run PR-controlled source on central review/control runners to bypass
unavailable isolated capacity. Route actual group registration, repository
access, clean per-job worker lifetime and runtime canary verification to the
existing operator/coordinator. Managed default CodeQL remains a separate
configuration owner path; this workflow edit neither disables it nor claims it
passed. The original failed checks remain historical evidence.

## Verification

The regression first failed on hosted routing, then separately failed on a
generic self-hosted selection without the isolated group. It passes only with
both the named group and labels while all existing acceptance commands remain.
Local contract and whitespace checks are separate from fresh hosted execution.
The final successor requires current-head independent review and tests before
normal publication. Hosted readiness requires real eligible runner allocation,
executed steps and successful checks, not an online runner inventory alone.

## Consequences

Without the operator-owned group/capacity, a fresh job will fail closed or queue.
Do not describe that as repaired runtime execution. No billing domain behavior,
credential, shared runner ACL, protection rule or remote deployment changes.
