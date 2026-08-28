//! Typed Rust reference builder for the canonical CWL usage event.
//!
//! The builder owns shaping, closed-field validation, deterministic
//! canonicalization, and source-payload integrity. It deliberately does not
//! calculate prices or perform ingestion/network I/O.

use std::collections::BTreeMap;
use std::fmt::{Display, Formatter};

use chrono::{DateTime, SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use uuid::Uuid;

pub const CLOUD_EVENTS_SPECVERSION: &str = "1.0";
pub const USAGE_CLOUD_EVENT_TYPE: &str = "org.contextualwisdomlab.metering.usage.v1";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProducerContractError(String);

impl Display for ProducerContractError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ProducerContractError {}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Measurement {
    pub meter_code: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub meter_version: Option<u32>,
    pub quantity: String,
    pub unit_code: String,
    pub quality_code: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CorrectionLineage {
    pub prior_event_id: String,
    pub relationship_code: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason_code: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UsageEvent {
    pub event_id: String,
    pub event_contract_version: u32,
    pub producer_contract_version: u32,
    pub source_event_key: String,
    pub tenant_reference: String,
    pub billing_account_reference: String,
    pub billing_principal_reference: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub credential_reference: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cost_center_reference: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub project_reference: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub repository_reference: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trace_reference: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub correlation_reference: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub causation_reference: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub available_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub correction_lineage: Option<CorrectionLineage>,
    pub product_code: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub operation_code: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dimensions: Option<BTreeMap<String, String>>,
    pub occurred_at: String,
    pub measurements: Vec<Measurement>,
    pub source_payload_hash: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UsageEventInput {
    pub event_id: String,
    pub event_contract_version: u32,
    pub producer_contract_version: u32,
    pub source_event_key: String,
    pub tenant_reference: String,
    pub billing_account_reference: String,
    pub billing_principal_reference: String,
    pub credential_reference: Option<String>,
    pub cost_center_reference: Option<String>,
    pub project_reference: Option<String>,
    pub repository_reference: Option<String>,
    pub trace_reference: Option<String>,
    pub correlation_reference: Option<String>,
    pub causation_reference: Option<String>,
    pub available_at: Option<String>,
    pub correction_lineage: Option<CorrectionLineage>,
    pub product_code: String,
    pub operation_code: Option<String>,
    pub dimensions: Option<BTreeMap<String, String>>,
    pub occurred_at: String,
    pub measurements: Vec<Measurement>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UsageCloudEvent {
    pub specversion: String,
    pub id: String,
    pub source: String,
    #[serde(rename = "type")]
    pub event_type: String,
    pub subject: String,
    pub time: String,
    pub datacontenttype: String,
    pub data: UsageEvent,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeliveryError {
    Transient(String),
    Permanent(String),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UsageDeliveryReceipt {
    pub source_event_key: String,
    #[serde(default)]
    pub event_contract_version: Option<u32>,
    #[serde(default)]
    pub source_payload_hash: Option<String>,
    #[serde(default)]
    pub tenant_reference: Option<String>,
    pub ingestion_outcome_code: String,
    #[serde(default)]
    pub rejection_reason_code: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UsageDeliveryResponse {
    pub event_receipts: Vec<UsageDeliveryReceipt>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OutboxFlushResult {
    pub attempted_count: usize,
    pub accepted_count: usize,
    pub duplicate_replay_count: usize,
    pub rejected_count: usize,
    pub retried_count: usize,
    pub dead_lettered_count: usize,
    pub pending_count: usize,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum OutboxState {
    Pending,
    DeadLetter,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct OutboxRecord {
    event: UsageEvent,
    attempts: u32,
    state: OutboxState,
    #[serde(default)]
    last_error_code: Option<String>,
}

/// A durable, process-local file queue for at-least-once producer delivery.
pub struct FileUsageOutbox {
    path: PathBuf,
    records: Vec<OutboxRecord>,
}

impl FileUsageOutbox {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, ProducerContractError> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            fs::create_dir_all(parent).map_err(io_error)?;
        }
        let records = if path.exists() {
            let bytes = fs::read(&path).map_err(io_error)?;
            serde_json::from_slice(&bytes).map_err(json_error)?
        } else {
            Vec::new()
        };
        let outbox = Self { path, records };
        if !outbox.path.exists() {
            outbox.persist()?;
        }
        Ok(outbox)
    }

    pub fn enqueue(&mut self, event: UsageEvent) -> Result<(), ProducerContractError> {
        build_usage_cloud_event(&event, "urn:cwl:producer:outbox-validation")?;
        if let Some(existing) = self
            .records
            .iter()
            .find(|record| record.event.event_id == event.event_id)
        {
            let existing_bytes = serde_json::to_vec(&existing.event).map_err(json_error)?;
            let event_bytes = serde_json::to_vec(&event).map_err(json_error)?;
            if existing_bytes != event_bytes {
                return Err(ProducerContractError(
                    "event_id already has different event bytes".into(),
                ));
            }
            return Ok(());
        }
        self.records.push(OutboxRecord {
            event,
            attempts: 0,
            state: OutboxState::Pending,
            last_error_code: None,
        });
        self.persist()
    }

    pub fn replay_dead_letter(&mut self, event_id: &str) -> Result<(), ProducerContractError> {
        let record = self
            .records
            .iter_mut()
            .find(|record| {
                record.event.event_id == event_id && record.state == OutboxState::DeadLetter
            })
            .ok_or_else(|| ProducerContractError(format!("unknown dead letter: {event_id}")))?;
        record.attempts = 0;
        record.state = OutboxState::Pending;
        record.last_error_code = None;
        self.persist()
    }

    pub fn pending_count(&self) -> usize {
        self.records
            .iter()
            .filter(|record| record.state == OutboxState::Pending)
            .count()
    }

    pub fn dead_letter_count(&self) -> usize {
        self.records
            .iter()
            .filter(|record| record.state == OutboxState::DeadLetter)
            .count()
    }

    pub fn flush<F>(
        &mut self,
        batch_size: usize,
        max_attempts: u32,
        sender: F,
    ) -> Result<OutboxFlushResult, ProducerContractError>
    where
        F: FnOnce(&[UsageEvent]) -> Result<UsageDeliveryResponse, DeliveryError>,
    {
        if batch_size == 0 || max_attempts == 0 {
            return Err(ProducerContractError(
                "batch_size and max_attempts must be positive".into(),
            ));
        }
        let batch: Vec<UsageEvent> = self
            .records
            .iter()
            .filter(|record| record.state == OutboxState::Pending)
            .take(batch_size)
            .map(|record| record.event.clone())
            .collect();
        if batch.is_empty() {
            return Ok(self.result(0, 0, 0, 0, 0, 0));
        }

        let mut accepted_count = 0;
        let mut duplicate_replay_count = 0;
        let mut rejected_count = 0;
        let mut retried_count = 0;
        let mut dead_lettered_count = 0;
        let mut remove_ids = HashSet::new();
        let mut fail = |event_id: &str, code: &str, force_dead: bool| {
            if let Some(record) = self
                .records
                .iter_mut()
                .find(|record| record.event.event_id == event_id)
            {
                record.attempts += 1;
                record.last_error_code = Some(code.into());
                if force_dead || record.attempts >= max_attempts {
                    record.state = OutboxState::DeadLetter;
                    dead_lettered_count += 1;
                } else {
                    retried_count += 1;
                }
            }
        };

        match sender(&batch) {
            Err(DeliveryError::Permanent(_)) => {
                for event in &batch {
                    fail(&event.event_id, "transport_permanent", true);
                }
            }
            Err(DeliveryError::Transient(_)) => {
                for event in &batch {
                    fail(&event.event_id, "transport_transient", false);
                }
            }
            Ok(response) => {
                for event in &batch {
                    let receipt = find_receipt(&response.event_receipts, event);
                    match receipt {
                        Some(receipt)
                            if receipt.ingestion_outcome_code == "accepted"
                                && receipt_matches(receipt, event) =>
                        {
                            remove_ids.insert(event.event_id.clone());
                            accepted_count += 1;
                        }
                        Some(receipt)
                            if receipt.ingestion_outcome_code == "duplicate_replay"
                                && receipt_matches(receipt, event) =>
                        {
                            remove_ids.insert(event.event_id.clone());
                            duplicate_replay_count += 1;
                        }
                        Some(receipt) if receipt.ingestion_outcome_code == "rejected" => {
                            fail(
                                &event.event_id,
                                receipt
                                    .rejection_reason_code
                                    .as_deref()
                                    .unwrap_or("rejected"),
                                true,
                            );
                            rejected_count += 1;
                        }
                        _ => fail(&event.event_id, "invalid_delivery_receipt", false),
                    }
                }
            }
        }
        self.records
            .retain(|record| !remove_ids.contains(&record.event.event_id));
        self.persist()?;
        Ok(self.result(
            batch.len(),
            accepted_count,
            duplicate_replay_count,
            rejected_count,
            retried_count,
            dead_lettered_count,
        ))
    }

    fn result(
        &self,
        attempted_count: usize,
        accepted_count: usize,
        duplicate_replay_count: usize,
        rejected_count: usize,
        retried_count: usize,
        dead_lettered_count: usize,
    ) -> OutboxFlushResult {
        OutboxFlushResult {
            attempted_count,
            accepted_count,
            duplicate_replay_count,
            rejected_count,
            retried_count,
            dead_lettered_count,
            pending_count: self.pending_count(),
        }
    }

    fn persist(&self) -> Result<(), ProducerContractError> {
        let temporary_path = self.path.with_extension("tmp");
        let bytes = serde_json::to_vec(&self.records).map_err(json_error)?;
        let mut file = File::create(&temporary_path).map_err(io_error)?;
        file.write_all(&bytes).map_err(io_error)?;
        file.sync_all().map_err(io_error)?;
        fs::rename(temporary_path, &self.path).map_err(io_error)
    }
}

fn find_receipt<'a>(
    receipts: &'a [UsageDeliveryReceipt],
    event: &UsageEvent,
) -> Option<&'a UsageDeliveryReceipt> {
    let mut matches = receipts.iter().filter(|receipt| {
        receipt.source_event_key == event.source_event_key
            && receipt
                .tenant_reference
                .as_ref()
                .is_some_and(|tenant| tenant == &event.tenant_reference)
    });
    let receipt = matches.next()?;
    matches.next().is_none().then_some(receipt)
}

fn receipt_matches(receipt: &UsageDeliveryReceipt, event: &UsageEvent) -> bool {
    receipt.tenant_reference.as_deref() == Some(event.tenant_reference.as_str())
        && receipt.source_payload_hash.as_deref() == Some(event.source_payload_hash.as_str())
        && receipt.event_contract_version == Some(event.event_contract_version)
}

fn io_error(error: io::Error) -> ProducerContractError {
    ProducerContractError(format!("outbox I/O failed: {error}"))
}

fn json_error(error: serde_json::Error) -> ProducerContractError {
    ProducerContractError(format!("outbox JSON failed: {error}"))
}

/// Builds one closed, validated event and computes its canonical source hash.
pub fn build_usage_event(input: UsageEventInput) -> Result<UsageEvent, ProducerContractError> {
    validate_input(&input)?;
    let mut event = UsageEvent {
        event_id: input.event_id,
        event_contract_version: input.event_contract_version,
        producer_contract_version: input.producer_contract_version,
        source_event_key: input.source_event_key,
        tenant_reference: input.tenant_reference,
        billing_account_reference: input.billing_account_reference,
        billing_principal_reference: input.billing_principal_reference,
        credential_reference: input.credential_reference,
        cost_center_reference: input.cost_center_reference,
        project_reference: input.project_reference,
        repository_reference: input.repository_reference,
        trace_reference: input.trace_reference,
        correlation_reference: input.correlation_reference,
        causation_reference: input.causation_reference,
        available_at: input.available_at,
        correction_lineage: input.correction_lineage,
        product_code: input.product_code,
        operation_code: input.operation_code,
        dimensions: input.dimensions,
        occurred_at: input.occurred_at,
        measurements: input.measurements,
        source_payload_hash: String::new(),
    };
    event.source_payload_hash = compute_source_payload_hash(&event)?;
    Ok(event)
}

/// Wraps a hash-verified event in a CloudEvents 1.0 JSON-compatible envelope.
pub fn build_usage_cloud_event(
    event: &UsageEvent,
    source: &str,
) -> Result<UsageCloudEvent, ProducerContractError> {
    if source.is_empty() {
        return Err(ProducerContractError(
            "CloudEvents source must be a non-empty string".into(),
        ));
    }
    validate_event(event)?;
    let expected_hash = compute_source_payload_hash(event)?;
    if event.source_payload_hash != expected_hash {
        return Err(ProducerContractError(format!(
            "source_payload_hash must equal {expected_hash}"
        )));
    }
    Ok(UsageCloudEvent {
        specversion: CLOUD_EVENTS_SPECVERSION.into(),
        id: event.event_id.clone(),
        source: source.into(),
        event_type: USAGE_CLOUD_EVENT_TYPE.into(),
        subject: event.source_event_key.clone(),
        time: event.occurred_at.clone(),
        datacontenttype: "application/json".into(),
        data: event.clone(),
    })
}

/// Returns the byte-stable JSON payload used by all producer SDKs for hashing.
pub fn canonical_source_payload_json(event: &UsageEvent) -> Result<String, ProducerContractError> {
    let mut payload = BTreeMap::new();
    payload.insert(
        "billing_account_reference",
        serde_json::Value::String(event.billing_account_reference.clone()),
    );
    payload.insert(
        "billing_principal_reference",
        serde_json::Value::String(event.billing_principal_reference.clone()),
    );
    if let Some(dimensions) = &event.dimensions {
        payload.insert(
            "dimensions",
            serde_json::to_value(dimensions).expect("BTreeMap of strings is serializable"),
        );
    }
    insert_optional(
        &mut payload,
        "cost_center_reference",
        &event.cost_center_reference,
    );
    insert_optional(
        &mut payload,
        "credential_reference",
        &event.credential_reference,
    );
    payload.insert(
        "event_contract_version",
        serde_json::Value::from(event.event_contract_version),
    );
    payload.insert(
        "producer_contract_version",
        serde_json::Value::from(event.producer_contract_version),
    );
    let measurements = event
        .measurements
        .iter()
        .map(canonical_measurement)
        .collect::<Result<Vec<_>, _>>()?;
    payload.insert("measurements", serde_json::Value::Array(measurements));
    insert_optional(&mut payload, "operation_code", &event.operation_code);
    payload.insert(
        "product_code",
        serde_json::Value::String(event.product_code.clone()),
    );
    insert_optional(&mut payload, "project_reference", &event.project_reference);
    insert_optional(
        &mut payload,
        "repository_reference",
        &event.repository_reference,
    );
    insert_optional(&mut payload, "trace_reference", &event.trace_reference);
    insert_optional(
        &mut payload,
        "correlation_reference",
        &event.correlation_reference,
    );
    insert_optional(
        &mut payload,
        "causation_reference",
        &event.causation_reference,
    );
    if let Some(available_at) = &event.available_at {
        payload.insert(
            "available_at",
            serde_json::Value::String(canonical_timestamp(available_at)?),
        );
    }
    if let Some(correction_lineage) = &event.correction_lineage {
        let mut lineage = BTreeMap::new();
        lineage.insert(
            "prior_event_id",
            serde_json::Value::String(correction_lineage.prior_event_id.clone()),
        );
        if let Some(reason_code) = &correction_lineage.reason_code {
            lineage.insert(
                "reason_code",
                serde_json::Value::String(reason_code.clone()),
            );
        }
        lineage.insert(
            "relationship_code",
            serde_json::Value::String(correction_lineage.relationship_code.clone()),
        );
        payload.insert(
            "correction_lineage",
            serde_json::to_value(lineage).expect("lineage is serializable"),
        );
    }
    payload.insert(
        "occurred_at",
        serde_json::Value::String(canonical_timestamp(&event.occurred_at)?),
    );
    payload.insert(
        "tenant_reference",
        serde_json::Value::String(event.tenant_reference.clone()),
    );
    serde_json::to_string(&payload).map_err(|error| {
        ProducerContractError(format!(
            "usage event cannot be canonically serialized: {error}"
        ))
    })
}

fn compute_source_payload_hash(event: &UsageEvent) -> Result<String, ProducerContractError> {
    let canonical = canonical_source_payload_json(event)?;
    let digest = Sha256::digest(canonical.as_bytes());
    Ok(format!("sha256:{digest:x}"))
}

fn insert_optional(
    payload: &mut BTreeMap<&'static str, serde_json::Value>,
    name: &'static str,
    value: &Option<String>,
) {
    if let Some(value) = value {
        payload.insert(name, serde_json::Value::String(value.clone()));
    }
}

fn canonical_measurement(
    measurement: &Measurement,
) -> Result<serde_json::Value, ProducerContractError> {
    let mut value = BTreeMap::new();
    value.insert(
        "meter_code",
        serde_json::Value::String(measurement.meter_code.clone()),
    );
    if let Some(meter_version) = measurement.meter_version {
        value.insert("meter_version", serde_json::Value::from(meter_version));
    }
    value.insert(
        "quality_code",
        serde_json::Value::String(measurement.quality_code.clone()),
    );
    value.insert(
        "quantity",
        serde_json::Value::String(canonical_quantity(&measurement.quantity)?),
    );
    value.insert(
        "unit_code",
        serde_json::Value::String(measurement.unit_code.clone()),
    );
    Ok(serde_json::to_value(value).expect("BTreeMap of JSON values is serializable"))
}

fn canonical_quantity(quantity: &str) -> Result<String, ProducerContractError> {
    validate_quantity(quantity)?;
    let (integer, fraction) = quantity.split_once('.').unwrap_or((quantity, ""));
    let trimmed_fraction = fraction.trim_end_matches('0');
    if trimmed_fraction.is_empty() {
        Ok(integer.into())
    } else {
        Ok(format!("{integer}.{trimmed_fraction}"))
    }
}

fn canonical_timestamp(timestamp: &str) -> Result<String, ProducerContractError> {
    if timestamp
        .split_once('T')
        .and_then(|(_, time)| time.split_once('.'))
        .is_some_and(|(_, fraction_and_zone)| {
            fraction_and_zone
                .chars()
                .take_while(char::is_ascii_digit)
                .count()
                > 6
        })
    {
        return Err(ProducerContractError(
            "timestamps cannot contain sub-microsecond precision".into(),
        ));
    }
    let parsed = DateTime::parse_from_rfc3339(timestamp)
        .map_err(|_| ProducerContractError("occurred_at must be an RFC3339 date-time".into()))?;
    let normalized = parsed.with_timezone(&Utc);
    let format = if normalized.timestamp_subsec_micros() == 0 {
        SecondsFormat::Secs
    } else {
        SecondsFormat::Micros
    };
    Ok(normalized.to_rfc3339_opts(format, true))
}

fn validate_input(input: &UsageEventInput) -> Result<(), ProducerContractError> {
    validate_uuid(&input.event_id)?;
    if input.event_contract_version == 0 {
        return Err(ProducerContractError(
            "event_contract_version must be at least 1".into(),
        ));
    }
    if input.producer_contract_version == 0 {
        return Err(ProducerContractError(
            "producer_contract_version must be at least 1".into(),
        ));
    }
    validate_bounded_text("source_event_key", &input.source_event_key, 256)?;
    for (name, value) in [
        ("tenant_reference", &input.tenant_reference),
        (
            "billing_account_reference",
            &input.billing_account_reference,
        ),
        (
            "billing_principal_reference",
            &input.billing_principal_reference,
        ),
    ] {
        validate_reference(name, value)?;
    }
    for (name, value) in [
        ("credential_reference", &input.credential_reference),
        ("cost_center_reference", &input.cost_center_reference),
        ("project_reference", &input.project_reference),
        ("repository_reference", &input.repository_reference),
        ("correlation_reference", &input.correlation_reference),
        ("causation_reference", &input.causation_reference),
    ] {
        if let Some(value) = value {
            validate_reference(name, value)?;
        }
    }
    if let Some(trace_reference) = &input.trace_reference {
        validate_bounded_text("trace_reference", trace_reference, 256)?;
    }
    validate_code("product_code", &input.product_code, 64)?;
    if let Some(operation_code) = &input.operation_code {
        validate_code("operation_code", operation_code, 64)?;
    }
    validate_dimensions(input.dimensions.as_ref())?;
    canonical_timestamp(&input.occurred_at)?;
    if let Some(available_at) = &input.available_at {
        canonical_timestamp(available_at)?;
    }
    if let Some(lineage) = &input.correction_lineage {
        validate_correction_lineage(lineage)?;
    }
    if input.measurements.is_empty() || input.measurements.len() > 64 {
        return Err(ProducerContractError(
            "measurements must contain between 1 and 64 objects".into(),
        ));
    }
    for measurement in &input.measurements {
        validate_code("meter_code", &measurement.meter_code, 96)?;
        if matches!(measurement.meter_version, Some(0)) {
            return Err(ProducerContractError(
                "meter_version must be at least 1".into(),
            ));
        }
        validate_quantity(&measurement.quantity)?;
        validate_code("unit_code", &measurement.unit_code, 32)?;
        if !matches!(
            measurement.quality_code.as_str(),
            "provider_reported"
                | "locally_measured"
                | "deterministically_derived"
                | "estimated"
                | "reconstructed"
                | "corrected"
        ) {
            return Err(ProducerContractError(
                "quality_code is not in the published enum".into(),
            ));
        }
    }
    Ok(())
}

fn validate_event(event: &UsageEvent) -> Result<(), ProducerContractError> {
    let input = UsageEventInput {
        event_id: event.event_id.clone(),
        event_contract_version: event.event_contract_version,
        producer_contract_version: event.producer_contract_version,
        source_event_key: event.source_event_key.clone(),
        tenant_reference: event.tenant_reference.clone(),
        billing_account_reference: event.billing_account_reference.clone(),
        billing_principal_reference: event.billing_principal_reference.clone(),
        credential_reference: event.credential_reference.clone(),
        cost_center_reference: event.cost_center_reference.clone(),
        project_reference: event.project_reference.clone(),
        repository_reference: event.repository_reference.clone(),
        trace_reference: event.trace_reference.clone(),
        correlation_reference: event.correlation_reference.clone(),
        causation_reference: event.causation_reference.clone(),
        available_at: event.available_at.clone(),
        correction_lineage: event.correction_lineage.clone(),
        product_code: event.product_code.clone(),
        operation_code: event.operation_code.clone(),
        dimensions: event.dimensions.clone(),
        occurred_at: event.occurred_at.clone(),
        measurements: event.measurements.clone(),
    };
    validate_input(&input)
}

fn validate_dimensions(
    dimensions: Option<&BTreeMap<String, String>>,
) -> Result<(), ProducerContractError> {
    let Some(dimensions) = dimensions else {
        return Ok(());
    };
    if dimensions.len() > 10 {
        return Err(ProducerContractError(
            "dimensions must contain at most 10 allowlisted fields".into(),
        ));
    }
    for (name, value) in dimensions {
        match name.as_str() {
            "provider_code"
            | "workflow_code"
            | "role_code"
            | "orchestration_mode_code"
            | "backend_code" => validate_code(name, value, 64)?,
            "model_code" => {
                if value.is_empty()
                    || value.len() > 128
                    || !value
                        .chars()
                        .next()
                        .is_some_and(|character| character.is_ascii_alphanumeric())
                    || !value.chars().all(|character| {
                        character.is_ascii_alphanumeric()
                            || matches!(character, '.' | '_' | ':' | '/' | '-')
                    })
                {
                    return Err(ProducerContractError(
                        "model_code must be a bounded provider model identifier".into(),
                    ));
                }
            }
            "document_job_reference"
            | "shard_reference"
            | "run_reference"
            | "artifact_reference"
            | "configuration_reference"
            | "seed_reference" => validate_reference(name, value)?,
            _ => {
                return Err(ProducerContractError(format!(
                    "dimension {name} is not in the published allowlist"
                )))
            }
        }
    }
    Ok(())
}

fn validate_correction_lineage(lineage: &CorrectionLineage) -> Result<(), ProducerContractError> {
    validate_uuid(&lineage.prior_event_id)?;
    if !matches!(
        lineage.relationship_code.as_str(),
        "corrects" | "reverses" | "supersedes"
    ) {
        return Err(ProducerContractError(
            "correction_lineage relationship_code is not in the published enum".into(),
        ));
    }
    if let Some(reason_code) = &lineage.reason_code {
        validate_code("correction_lineage reason_code", reason_code, 64)?;
    }
    Ok(())
}

fn validate_uuid(value: &str) -> Result<(), ProducerContractError> {
    Uuid::parse_str(value)
        .map(|_| ())
        .map_err(|_| ProducerContractError("event_id must be a UUID".into()))
}

fn validate_reference(name: &str, value: &str) -> Result<(), ProducerContractError> {
    if value.is_empty() || !value.starts_with("urn:cwl:") {
        return Err(ProducerContractError(format!(
            "{name} must be a non-empty urn:cwl reference"
        )));
    }
    Ok(())
}

fn validate_bounded_text(
    name: &str,
    value: &str,
    maximum: usize,
) -> Result<(), ProducerContractError> {
    if value.is_empty() || value.chars().count() > maximum {
        return Err(ProducerContractError(format!(
            "{name} must be between 1 and {maximum} characters"
        )));
    }
    Ok(())
}

fn validate_code(name: &str, value: &str, maximum: usize) -> Result<(), ProducerContractError> {
    if value.len() < 2 || value.len() > maximum {
        return Err(ProducerContractError(format!(
            "{name} must be between 2 and {maximum} characters"
        )));
    }
    let mut chars = value.chars();
    if !chars
        .next()
        .is_some_and(|character| character.is_ascii_lowercase())
        || !chars.all(|character| {
            character.is_ascii_lowercase() || character.is_ascii_digit() || character == '_'
        })
    {
        return Err(ProducerContractError(format!(
            "{name} must be lower snake_case"
        )));
    }
    Ok(())
}

fn validate_quantity(quantity: &str) -> Result<(), ProducerContractError> {
    if quantity.is_empty() || quantity.len() > 39 {
        return Err(ProducerContractError(
            "quantity must be a non-negative exact decimal".into(),
        ));
    }
    let (integer, fraction) = quantity.split_once('.').unwrap_or((quantity, ""));
    if integer.is_empty()
        || (integer.len() > 1 && integer.starts_with('0'))
        || !integer.chars().all(|character| character.is_ascii_digit())
        || (quantity.contains('.')
            && (fraction.is_empty()
                || !fraction.chars().all(|character| character.is_ascii_digit())))
    {
        return Err(ProducerContractError(
            "quantity must be a non-negative exact decimal".into(),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::fs;
    use std::path::PathBuf;

    fn fixture() -> Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../schemas/examples/usage-event-v1-conformance.json");
        serde_json::from_str(&fs::read_to_string(path).expect("conformance fixture exists"))
            .expect("conformance fixture is JSON")
    }

    fn input_from_fixture(value: &Value) -> UsageEventInput {
        let event: UsageEvent = serde_json::from_value(value["event"].clone()).unwrap();
        UsageEventInput {
            event_id: event.event_id,
            event_contract_version: event.event_contract_version,
            producer_contract_version: event.producer_contract_version,
            source_event_key: event.source_event_key,
            tenant_reference: event.tenant_reference,
            billing_account_reference: event.billing_account_reference,
            billing_principal_reference: event.billing_principal_reference,
            credential_reference: event.credential_reference,
            cost_center_reference: event.cost_center_reference,
            project_reference: event.project_reference,
            repository_reference: event.repository_reference,
            trace_reference: event.trace_reference,
            correlation_reference: event.correlation_reference,
            causation_reference: event.causation_reference,
            available_at: event.available_at,
            correction_lineage: event.correction_lineage,
            product_code: event.product_code,
            operation_code: event.operation_code,
            dimensions: event.dimensions,
            occurred_at: event.occurred_at,
            measurements: event.measurements,
        }
    }

    #[test]
    fn matches_python_conformance_vector() {
        let vector = fixture();
        let event = build_usage_event(input_from_fixture(&vector)).unwrap();
        assert_eq!(serde_json::to_value(&event).unwrap(), vector["event"]);
        assert_eq!(
            canonical_source_payload_json(&event).unwrap(),
            vector["canonical_source_payload_json"]
        );
        assert_eq!(event.source_payload_hash, vector["source_payload_hash"]);

        let cloud_event =
            build_usage_cloud_event(&event, "urn:cwl:producer:reference-rust").unwrap();
        assert_eq!(cloud_event.specversion, "1.0");
        assert_eq!(cloud_event.id, event.event_id);
        assert_eq!(cloud_event.subject, event.source_event_key);
        assert_eq!(cloud_event.data, event);
    }

    #[test]
    fn matches_python_allowlisted_dimensions_conformance() {
        let vector = fixture();
        let mut input = input_from_fixture(&vector);
        input.dimensions = Some(BTreeMap::from([
            ("model_code".into(), "gpt-4o-mini".into()),
            ("provider_code".into(), "openai".into()),
            ("workflow_code".into(), "verified_workflow".into()),
        ]));

        let event = build_usage_event(input).unwrap();
        assert_eq!(
            event.source_payload_hash,
            "sha256:601172eebd1e5f5d840706bcf1b5833203d4b802898459c00176fd4600ebed35"
        );
    }

    #[test]
    fn matches_python_whole_second_timestamp_conformance() {
        let vector = fixture();
        let mut input = input_from_fixture(&vector);
        input.occurred_at = "2026-08-16T10:27:42.000Z".into();

        let event = build_usage_event(input).unwrap();
        assert_eq!(
            event.source_payload_hash,
            "sha256:d5d3aeda8f19e49e2db7cd70e9d1219bf131941e0142a750a8fe51d84515fa3c"
        );
    }

    #[test]
    fn rejects_tampered_hash_and_float_or_sensitive_input() {
        let vector = fixture();
        let mut event: UsageEvent = serde_json::from_value(vector["event"].clone()).unwrap();
        event.source_payload_hash =
            "sha256:0000000000000000000000000000000000000000000000000000000000000000".into();
        assert!(build_usage_cloud_event(&event, "urn:cwl:producer:test").is_err());

        let mut input = input_from_fixture(&vector);
        input.measurements[0].quantity = "1.25".into();
        assert!(build_usage_event(input.clone()).is_ok());
        input.measurements[0].quantity = "1e3".into();
        assert!(build_usage_event(input).is_err());

        let mut submicrosecond = input_from_fixture(&vector);
        submicrosecond.occurred_at = "2026-08-16T10:27:42.1234567Z".into();
        assert!(build_usage_event(submicrosecond).is_err());

        let mut unicode_key = input_from_fixture(&vector);
        unicode_key.source_event_key = "가".repeat(256);
        assert!(build_usage_event(unicode_key.clone()).is_ok());
        unicode_key.source_event_key.push('가');
        assert!(build_usage_event(unicode_key).is_err());
    }

    #[test]
    fn durable_outbox_retries_and_acknowledges_duplicate_replay() {
        let vector = fixture();
        let event = build_usage_event(input_from_fixture(&vector)).unwrap();
        let path = std::env::temp_dir().join(format!("cwl-outbox-{}.json", event.event_id));
        let _ = fs::remove_file(&path);
        let mut outbox = FileUsageOutbox::open(&path).unwrap();
        outbox.enqueue(event.clone()).unwrap();
        let first = outbox
            .flush(1, 3, |_| Err(DeliveryError::Transient("offline".into())))
            .unwrap();
        assert_eq!(first.retried_count, 1);
        assert_eq!(outbox.pending_count(), 1);
        let response = UsageDeliveryResponse {
            event_receipts: vec![UsageDeliveryReceipt {
                source_event_key: event.source_event_key.clone(),
                event_contract_version: Some(event.event_contract_version),
                source_payload_hash: Some(event.source_payload_hash.clone()),
                tenant_reference: Some(event.tenant_reference.clone()),
                ingestion_outcome_code: "duplicate_replay".into(),
                rejection_reason_code: None,
            }],
        };
        let second = outbox.flush(1, 3, |_| Ok(response)).unwrap();
        assert_eq!(second.duplicate_replay_count, 1);
        assert_eq!(outbox.pending_count(), 0);
        let _ = fs::remove_file(path);
    }

    #[test]
    fn durable_outbox_dead_letters_and_explicitly_replays_rejections() {
        let vector = fixture();
        let event = build_usage_event(input_from_fixture(&vector)).unwrap();
        let path = std::env::temp_dir().join(format!("cwl-reject-outbox-{}.json", event.event_id));
        let _ = fs::remove_file(&path);
        let mut outbox = FileUsageOutbox::open(&path).unwrap();
        outbox.enqueue(event.clone()).unwrap();
        let result = outbox
            .flush(1, 3, |_| {
                Ok(UsageDeliveryResponse {
                    event_receipts: vec![UsageDeliveryReceipt {
                        source_event_key: event.source_event_key.clone(),
                        event_contract_version: None,
                        source_payload_hash: None,
                        tenant_reference: Some(event.tenant_reference.clone()),
                        ingestion_outcome_code: "rejected".into(),
                        rejection_reason_code: Some("meter_not_found".into()),
                    }],
                })
            })
            .unwrap();
        assert_eq!(result.rejected_count, 1);
        assert_eq!(outbox.dead_letter_count(), 1);
        outbox.replay_dead_letter(&event.event_id).unwrap();
        assert_eq!(outbox.pending_count(), 1);
        let _ = fs::remove_file(path);
    }

    #[test]
    fn durable_outbox_applies_partial_receipts_per_event() {
        let vector = fixture();
        let first = build_usage_event(input_from_fixture(&vector)).unwrap();
        let mut second_input = input_from_fixture(&vector);
        second_input.event_id = "019d7b92-1aa0-7a7f-b61c-962c0f4bf6ad".into();
        second_input.source_event_key = "producer-reference:workflow-381:step-05".into();
        let second = build_usage_event(second_input).unwrap();
        let path = std::env::temp_dir().join(format!("cwl-partial-outbox-{}.json", first.event_id));
        let _ = fs::remove_file(&path);
        let mut outbox = FileUsageOutbox::open(&path).unwrap();
        outbox.enqueue(first.clone()).unwrap();
        outbox.enqueue(second).unwrap();
        let result = outbox
            .flush(2, 3, |events| {
                Ok(UsageDeliveryResponse {
                    event_receipts: vec![UsageDeliveryReceipt {
                        source_event_key: events[0].source_event_key.clone(),
                        event_contract_version: Some(events[0].event_contract_version),
                        source_payload_hash: Some(events[0].source_payload_hash.clone()),
                        tenant_reference: Some(events[0].tenant_reference.clone()),
                        ingestion_outcome_code: "accepted".into(),
                        rejection_reason_code: None,
                    }],
                })
            })
            .unwrap();
        assert_eq!(result.accepted_count, 1);
        assert_eq!(result.retried_count, 1);
        assert_eq!(outbox.pending_count(), 1);
        let _ = fs::remove_file(path);
    }

    #[test]
    fn durable_outbox_requires_a_tenant_matched_receipt() {
        let vector = fixture();
        let event = build_usage_event(input_from_fixture(&vector)).unwrap();
        let path = std::env::temp_dir().join(format!("cwl-tenant-outbox-{}.json", event.event_id));
        let _ = fs::remove_file(&path);
        let mut outbox = FileUsageOutbox::open(&path).unwrap();
        outbox.enqueue(event.clone()).unwrap();
        let result = outbox
            .flush(1, 3, |_| {
                Ok(UsageDeliveryResponse {
                    event_receipts: vec![UsageDeliveryReceipt {
                        source_event_key: event.source_event_key.clone(),
                        event_contract_version: Some(event.event_contract_version),
                        source_payload_hash: Some(event.source_payload_hash.clone()),
                        tenant_reference: None,
                        ingestion_outcome_code: "accepted".into(),
                        rejection_reason_code: None,
                    }],
                })
            })
            .unwrap();
        assert_eq!(result.accepted_count, 0);
        assert_eq!(result.retried_count, 1);
        assert_eq!(outbox.pending_count(), 1);
        let _ = fs::remove_file(path);
    }
}
