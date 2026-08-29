# Provider outage, throttling, or webhook backlog incident

**Status:** tabletop-ready; provider and network evidence is required before release.

## Owner

Provider-integration on-call owns the adapter; billing on-call owns immutable
commercial facts and customer impact.

## Severity and escalation

Use SEV-1 for a provider outage affecting collection or settlement integrity;
use SEV-2 for bounded throttling or webhook delay. Escalate to the provider
account owner without sharing customer secrets.

## Customer communication

Tell customers that provider confirmation is delayed while usage and internal
intent facts remain separate. Give the next update time and avoid promising a
provider-side status.

## Recovery objective

Record provider-specific RPO/RTO and measured queue-drain time. Do not infer a
provider SLA from local retry success.

## Evidence preservation

Keep status code, retry-after value, adapter release, event ID, source hash,
attempt number, and UTC timestamps. Store no authorization header or raw
provider body.

## Detection

Watch transport errors, rate-limit responses, webhook verification failures,
outbox age, dead letters, and provider receipt lag separately.

## Containment

Apply the adapter's bounded backoff and stop unbounded retries. Keep accepted
usage and payment intent facts; quarantine only the affected provider action.

## Diagnosis

Distinguish DNS/TLS, authentication, 4xx contract rejection, 429 throttling,
5xx outage, webhook signature failure, and a local queue/worker fault. Check
tenant and credential routing before retrying.

## Recovery

Resume with stable idempotency keys and bounded batches after the provider
health signal recovers. Reconcile provider receipts and local attempts before
marking a commercial action complete.

## Validation receipt

Record the outage interval, queue age, retry count, receipt matches, command
exit statuses, and secret-free evidence checksums. Run the repository validator
from the exact release checkout.

## Exit and RCA

Exit when backlog is within the measured envelope and every unresolved item has
an explicit dead-letter or retry owner. Attach provider escalation and a chaos
or tabletop follow-up.
