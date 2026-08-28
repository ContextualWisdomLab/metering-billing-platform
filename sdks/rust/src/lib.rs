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
    pub quantity: String,
    pub unit_code: String,
    pub quality_code: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UsageEvent {
    pub event_id: String,
    pub event_contract_version: u32,
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
    pub product_code: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub operation_code: Option<String>,
    pub occurred_at: String,
    pub measurements: Vec<Measurement>,
    pub source_payload_hash: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UsageEventInput {
    pub event_id: String,
    pub event_contract_version: u32,
    pub source_event_key: String,
    pub tenant_reference: String,
    pub billing_account_reference: String,
    pub billing_principal_reference: String,
    pub credential_reference: Option<String>,
    pub cost_center_reference: Option<String>,
    pub project_reference: Option<String>,
    pub product_code: String,
    pub operation_code: Option<String>,
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

/// Builds one closed, validated event and computes its canonical source hash.
pub fn build_usage_event(input: UsageEventInput) -> Result<UsageEvent, ProducerContractError> {
    validate_input(&input)?;
    let mut event = UsageEvent {
        event_id: input.event_id,
        event_contract_version: input.event_contract_version,
        source_event_key: input.source_event_key,
        tenant_reference: input.tenant_reference,
        billing_account_reference: input.billing_account_reference,
        billing_principal_reference: input.billing_principal_reference,
        credential_reference: input.credential_reference,
        cost_center_reference: input.cost_center_reference,
        project_reference: input.project_reference,
        product_code: input.product_code,
        operation_code: input.operation_code,
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
    ] {
        if let Some(value) = value {
            validate_reference(name, value)?;
        }
    }
    validate_code("product_code", &input.product_code, 64)?;
    if let Some(operation_code) = &input.operation_code {
        validate_code("operation_code", operation_code, 64)?;
    }
    canonical_timestamp(&input.occurred_at)?;
    if input.measurements.is_empty() || input.measurements.len() > 64 {
        return Err(ProducerContractError(
            "measurements must contain between 1 and 64 objects".into(),
        ));
    }
    for measurement in &input.measurements {
        validate_code("meter_code", &measurement.meter_code, 96)?;
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
        source_event_key: event.source_event_key.clone(),
        tenant_reference: event.tenant_reference.clone(),
        billing_account_reference: event.billing_account_reference.clone(),
        billing_principal_reference: event.billing_principal_reference.clone(),
        credential_reference: event.credential_reference.clone(),
        cost_center_reference: event.cost_center_reference.clone(),
        project_reference: event.project_reference.clone(),
        product_code: event.product_code.clone(),
        operation_code: event.operation_code.clone(),
        occurred_at: event.occurred_at.clone(),
        measurements: event.measurements.clone(),
    };
    validate_input(&input)
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
    if value.is_empty() || value.len() > maximum {
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
            source_event_key: event.source_event_key,
            tenant_reference: event.tenant_reference,
            billing_account_reference: event.billing_account_reference,
            billing_principal_reference: event.billing_principal_reference,
            credential_reference: event.credential_reference,
            cost_center_reference: event.cost_center_reference,
            project_reference: event.project_reference,
            product_code: event.product_code,
            operation_code: event.operation_code,
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
    }

    #[test]
    fn matches_python_whole_second_timestamp_conformance() {
        let vector = fixture();
        let mut input = input_from_fixture(&vector);
        input.occurred_at = "2026-08-28T01:02:03Z".into();

        let event = build_usage_event(input).unwrap();
        assert!(canonical_source_payload_json(&event)
            .unwrap()
            .contains("\"occurred_at\":\"2026-08-28T01:02:03Z\""));
        assert_eq!(
            event.source_payload_hash,
            "sha256:37cd41c8b30b0d334539bce29f69a642468540cf7b5f96fc80ef5bbd5f85e6ad"
        );
    }
}
