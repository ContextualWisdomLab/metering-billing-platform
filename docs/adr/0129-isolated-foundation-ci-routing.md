# ADR 0129: Route Foundation CI to the isolated Linux runner pool

## Status

Accepted for the local candidate. Runtime capacity and access must be verified
before claiming CI acceptance. This does not authorize changes to runner
registration, group access, production hosts, or central review workflow
custody.

## Context

The repository's Foundation CI previously selected `ubuntu-latest`. That made
the repository contract gate depend on GitHub-hosted capacity and the
organization billing state. The job runs PR-controlled tests, a PostgreSQL 18
service container, migrations, full statement and branch coverage, repository
validation, and compilation.

The required isolated pool is a separate custody boundary. Generic
self-hosted labels are not enough and must not cause PR-controlled source to
run on central review or control hosts.

The organization-owned `.github` repository and its reusable workflow ref were
not accessible during this change. No central workflow name or ref is invented.
This repository therefore keeps its complete Foundation job local while
routing its execution to the approved isolated self-hosted group. Central
required workflows remain a separate organization-owned path.

## Decision

Select the named `CWL CI isolated` group together with exact labels
`self-hosted`, `Linux`, `X64`, and `cwlab-ci-isolated`. The group and labels
are joint requirements. Preserve PostgreSQL 18 service health checks,
hash-locked dependency installation, pinned action identities,
credential-free checkout, branch coverage collection, 100% enforcement,
repository validation, compilation, and the existing job timeout.

Do not run PR-controlled source on central review or control runners to bypass
unavailable isolated capacity. Route actual group registration, repository
access, clean per-job worker lifetime, and runtime canary verification to the
existing operator/coordinator.

Managed CodeQL and GitHub Code Quality are not repository-authored workflows.
They cannot be disabled or moved by editing this repository's YAML. Their
organization or repository settings owner must decide whether to disable them
or route them to an approved self-hosted CodeQL-capable pool.

## Verification

The regression test fails on the hosted `ubuntu-latest` mapping and passes
only with both the named group and exact labels while all existing acceptance
commands remain. Local contract checks are separate from fresh runner
execution. Hosted readiness requires an actual eligible runner allocation,
executed steps, and successful checks; an online runner inventory alone is not
acceptance.

## Consequences

Without operator-owned group capacity, a fresh job fails closed or queues. Do
not describe that as repaired runtime execution. No billing behavior,
credential, shared runner ACL, protection rule, or remote deployment change
is included.
