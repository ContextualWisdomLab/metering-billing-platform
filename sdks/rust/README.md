# CWL metering producer SDK for Rust

This package builds the closed canonical usage event and its CloudEvents 1.0
envelope. It uses exact-decimal quantity strings, deterministic UTC
canonicalization, and a SHA-256 source-payload hash compatible with the Python
reference and the checked-in conformance vector.

It does not calculate prices, persist credentials, include sensitive content,
or perform ingestion. Durable buffering and partial-result delivery belong to
the producer outbox boundary.
