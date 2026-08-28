# CWL metering producer SDK for TypeScript

This package builds the closed canonical usage event and its CloudEvents 1.0
envelope. It uses exact-decimal quantity strings, deterministic UTC
canonicalization, Node's standard SHA-256 implementation, and the same
conformance vector as the Python and Rust references.

It does not calculate prices, persist credentials, include sensitive content,
or perform ingestion. Durable buffering and partial-result delivery belong to
the producer outbox boundary.
