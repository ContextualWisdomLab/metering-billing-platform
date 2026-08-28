# CWL metering producer SDK for Rust

This package builds the closed canonical usage event and its CloudEvents 1.0
envelope. It uses exact-decimal quantity strings, deterministic UTC
canonicalization, and a SHA-256 source-payload hash compatible with the Python
reference and the checked-in conformance vector.

It does not calculate prices, persist credentials, or include sensitive
content. `FileUsageOutbox` provides durable local buffering: enqueue validated
events, call `flush(batch_size, max_attempts, sender)`, and remove only
hash-matched accepted or duplicate-replay receipts. Rejected events remain
dead-lettered until `replay_dead_letter` is called. The sender owns HTTP,
credentials, and scheduling. The file queue fsyncs both the replacement file
and its parent directory before acknowledging persistence.
