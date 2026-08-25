# Consumed contracts

Files in this directory are **consumer copies** of contracts owned by another CWL authority.

`accounting-posting-receipt.schema.json` is published by the Accounting Information Platform (AIS). Billing validates AIS HTTP responses against this copy. It does not own `posting_status_code`, does not treat the file as a Billing schema, and does not flip `proposal_status` when a receipt arrives.

The AIS `$id` and `x-cwl-authority` fields stay unchanged so operators can see who publishes the fact.
