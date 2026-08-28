# CWL metering producer SDK for TypeScript

This package builds the closed canonical usage event and its CloudEvents 1.0
envelope. It uses exact-decimal quantity strings, deterministic UTC
canonicalization, Node's standard SHA-256 implementation, and the same
conformance vector as the Python and Rust references.

It does not calculate prices, persist credentials, or include sensitive
content. `FileUsageOutbox` provides durable local buffering: enqueue validated
events, call `flush(sender, batchSize, maxAttempts)`, and remove only
hash-matched accepted or duplicate-replay receipts. Rejected events remain
dead-lettered until `replayDeadLetter` is called. The sender owns HTTP,
credentials, and scheduling; `httpUsageIngestionTransport` targets the
platform's existing batch route.
