# ADR 0125: Capability-based provider routing and webhook verification

## Status

Accepted for the provider-foundation slice.

## Context

Billing must choose a provider from the capability, currency, jurisdiction,
contract, and tenant-policy facts of a commercial operation. Provider object
identifiers must remain behind the existing mapping boundary. Lemon Squeezy
webhooks also arrive as signed JSON whose raw bytes are the trust boundary;
parsing an unverified body would allow untrusted fields to influence routing.

## Decision

- Publish a closed `provider-capability` manifest with effective half-open
  intervals and optional routing dimensions.
- Select only manifests that satisfy every requested capability and dimension.
  Health callbacks are optional and fail closed on false or on an exception;
  deterministic provider-code and effective-date ordering breaks ties.
- Ship manifests for Lemon Squeezy as a merchant-of-record capability set and
  for manual enterprise collection as a wire-transfer capability set. These
  declarations do not call either provider or create an external transaction.
- Verify the Lemon Squeezy HMAC-SHA256 signature over the exact request body
  before JSON parsing, then emit only event name, resource type, and resource
  reference for asynchronous processing. Raw payloads, credentials, and PII
  do not enter the normalized contract.

## Consequences

Capability routing is testable without network access and can preserve provider
stickiness when a later adapter records a mapping. The current slice does not
implement provider command ports, raw-artifact object storage, KMS-backed
secrets, manual maker-checker settlement, reconciliation, or production
provider sandbox runs; issue #86 remains open for those controls.

## References

- Lemon Squeezy. (n.d.). *Sync with webhooks*. Retrieved August 29, 2026, from
  https://docs.lemonsqueezy.com/guides/developer-guide/webhooks
- Lemon Squeezy. (n.d.). *Usage-based billing*. Retrieved August 29, 2026, from
  https://docs.lemonsqueezy.com/help/products/usage-based-billing
