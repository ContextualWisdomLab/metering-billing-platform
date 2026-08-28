use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};

use cwl_metering_producer::{
    build_usage_cloud_event, build_usage_event, CorrectionLineage, DeliveryError, FileUsageOutbox,
    UsageDeliveryReceipt, UsageDeliveryResponse, UsageEvent, UsageEventInput,
};
use serde_json::Value;

fn fixture() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../schemas/examples/usage-event-v1-conformance.json");
    serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
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

fn path(suffix: &str, event: &UsageEvent) -> PathBuf {
    std::env::temp_dir().join(format!("cwl-coverage-{suffix}-{}.json", event.event_id))
}

static CURRENT_DIRECTORY_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

struct CurrentDirectoryGuard {
    previous: PathBuf,
    isolated: PathBuf,
    relative_file: PathBuf,
}

impl Drop for CurrentDirectoryGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(self.isolated.join(&self.relative_file));
        let _ = fs::remove_file(self.isolated.join(self.relative_file.with_extension("tmp")));
        let _ = std::env::set_current_dir(&self.previous);
        let _ = fs::remove_dir(&self.isolated);
    }
}

fn receipt(event: &UsageEvent, outcome: &str) -> UsageDeliveryReceipt {
    UsageDeliveryReceipt {
        source_event_key: event.source_event_key.clone(),
        event_contract_version: Some(event.event_contract_version),
        source_payload_hash: Some(event.source_payload_hash.clone()),
        tenant_reference: Some(event.tenant_reference.clone()),
        ingestion_outcome_code: outcome.into(),
        rejection_reason_code: None,
    }
}

#[test]
fn covers_conformance_and_cloud_event() {
    let vector = fixture();
    let event = build_usage_event(input_from_fixture(&vector)).unwrap();
    assert_eq!(serde_json::to_value(&event).unwrap(), vector["event"]);
    assert_eq!(
        cwl_metering_producer::canonical_source_payload_json(&event).unwrap(),
        vector["canonical_source_payload_json"]
    );
    assert_eq!(event.source_payload_hash, vector["source_payload_hash"]);
    let cloud_event = build_usage_cloud_event(&event, "urn:cwl:producer:coverage").unwrap();
    assert_eq!(cloud_event.specversion, "1.0");
    assert_eq!(cloud_event.id, event.event_id);
    assert_eq!(cloud_event.subject, event.source_event_key);
    assert_eq!(cloud_event.data, event);
    assert!(build_usage_cloud_event(&event, "").is_err());
    let mut invalid_cloud_event = event.clone();
    invalid_cloud_event.occurred_at = "not-a-timestamp".into();
    assert!(build_usage_cloud_event(&invalid_cloud_event, "urn:cwl:producer:coverage").is_err());

    let mut invalid_payload = event.clone();
    invalid_payload.available_at = Some("not-a-timestamp".into());
    assert!(cwl_metering_producer::canonical_source_payload_json(&invalid_payload).is_err());
    invalid_payload.available_at = event.available_at.clone();
    invalid_payload.occurred_at = "not-a-timestamp".into();
    assert!(cwl_metering_producer::canonical_source_payload_json(&invalid_payload).is_err());
    invalid_payload.occurred_at = event.occurred_at.clone();
    invalid_payload.measurements[0].quantity = "not-a-decimal".into();
    assert!(cwl_metering_producer::canonical_source_payload_json(&invalid_payload).is_err());
    let mut no_meter_version = event.clone();
    no_meter_version.measurements[0].meter_version = None;
    assert!(cwl_metering_producer::canonical_source_payload_json(&no_meter_version).is_ok());

    let mut whole_second = input_from_fixture(&vector);
    whole_second.occurred_at = "2026-08-16T10:27:42.000Z".into();
    assert!(build_usage_event(whole_second).is_ok());
    let mut digit_code = input_from_fixture(&vector);
    digit_code.product_code = "product_2".into();
    assert!(build_usage_event(digit_code).is_ok());
    let mut dimensions = input_from_fixture(&vector);
    dimensions.dimensions = Some(BTreeMap::from([
        ("provider_code".into(), "openai".into()),
        ("workflow_code".into(), "workflow_code".into()),
        ("role_code".into(), "operator".into()),
        ("orchestration_mode_code".into(), "sync".into()),
        ("backend_code".into(), "rust".into()),
        ("model_code".into(), "gpt-4o-mini".into()),
        ("document_job_reference".into(), "urn:cwl:job:01".into()),
        ("shard_reference".into(), "urn:cwl:shard:01".into()),
        ("run_reference".into(), "urn:cwl:run:01".into()),
        ("seed_reference".into(), "urn:cwl:seed:01".into()),
    ]));
    assert!(build_usage_event(dimensions).is_ok());
    let mut too_many_dimensions = input_from_fixture(&vector);
    let mut dimensions = BTreeMap::new();
    for index in 0..11 {
        dimensions.insert(format!("unknown_{index}"), "value".into());
    }
    too_many_dimensions.dimensions = Some(dimensions);
    assert!(build_usage_event(too_many_dimensions).is_err());

    let mut minimal = input_from_fixture(&vector);
    minimal.credential_reference = None;
    minimal.cost_center_reference = None;
    minimal.project_reference = None;
    minimal.repository_reference = None;
    minimal.trace_reference = None;
    minimal.correlation_reference = None;
    minimal.causation_reference = None;
    minimal.available_at = None;
    minimal.correction_lineage = Some(CorrectionLineage {
        prior_event_id: minimal.event_id.clone(),
        relationship_code: "corrects".into(),
        reason_code: None,
    });
    minimal.operation_code = None;
    minimal.dimensions = None;
    assert!(build_usage_event(minimal).is_ok());
    let mut no_correction = input_from_fixture(&vector);
    no_correction.correction_lineage = None;
    assert!(build_usage_event(no_correction).is_ok());
}

#[test]
fn covers_invalid_contract_boundaries() {
    let vector = fixture();
    let base = input_from_fixture(&vector);
    let mut invalid = base.clone();
    invalid.event_id = "not-a-uuid".into();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.event_contract_version = 0;
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.producer_contract_version = 0;
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.source_event_key.clear();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.source_event_key = "x".repeat(257);
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.tenant_reference = "not-a-reference".into();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.tenant_reference.clear();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.credential_reference = Some("not-a-reference".into());
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.trace_reference = Some(String::new());
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.product_code = "A".into();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.product_code = "Bad".into();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.product_code = "a".repeat(65);
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.product_code = "ba!".into();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.operation_code = Some("A".into());
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.occurred_at = "not-a-timestamp".into();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.occurred_at = "2026-08-16T10:27:42.1234567Z".into();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.available_at = Some("not-a-timestamp".into());
    assert!(build_usage_event(invalid).is_err());
    for lineage in [
        CorrectionLineage {
            prior_event_id: "not-a-uuid".into(),
            relationship_code: "corrects".into(),
            reason_code: None,
        },
        CorrectionLineage {
            prior_event_id: base.event_id.clone(),
            relationship_code: "invalid".into(),
            reason_code: None,
        },
        CorrectionLineage {
            prior_event_id: base.event_id.clone(),
            relationship_code: "corrects".into(),
            reason_code: Some("A".into()),
        },
    ] {
        let mut invalid = base.clone();
        invalid.correction_lineage = Some(lineage);
        assert!(build_usage_event(invalid).is_err());
    }
    let mut invalid = base.clone();
    invalid.measurements.clear();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.measurements = vec![base.measurements[0].clone(); 65];
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.measurements[0].meter_version = Some(0);
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.measurements[0].quality_code = "invalid".into();
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.measurements[0].meter_code = "A".into();
    assert!(build_usage_event(invalid).is_err());
    for quantity in ["", "1", ".", "01", "1a", "1.", "1.a"] {
        let mut invalid = base.clone();
        invalid.measurements[0].quantity = if quantity == "1" {
            "1".repeat(40)
        } else {
            quantity.into()
        };
        assert!(build_usage_event(invalid).is_err());
    }
    let mut invalid = base.clone();
    invalid.measurements[0].unit_code = "A".into();
    assert!(build_usage_event(invalid).is_err());

    let mut invalid = base.clone();
    invalid.dimensions = Some(BTreeMap::from([("unknown".into(), "value".into())]));
    assert!(build_usage_event(invalid).is_err());
    for model_code in ["", "!a", "a!"] {
        let mut invalid = base.clone();
        invalid.dimensions = Some(BTreeMap::from([("model_code".into(), model_code.into())]));
        assert!(build_usage_event(invalid).is_err());
    }
    let mut invalid = base.clone();
    invalid.dimensions = Some(BTreeMap::from([("model_code".into(), "a".repeat(129))]));
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.dimensions = Some(BTreeMap::from([("provider_code".into(), "A".into())]));
    assert!(build_usage_event(invalid).is_err());
    let mut invalid = base.clone();
    invalid.dimensions = Some(BTreeMap::from([(
        "document_job_reference".into(),
        "not-a-reference".into(),
    )]));
    assert!(build_usage_event(invalid).is_err());

    let mut tampered: UsageEvent = serde_json::from_value(vector["event"].clone()).unwrap();
    tampered.source_payload_hash =
        "sha256:0000000000000000000000000000000000000000000000000000000000000000".into();
    assert!(build_usage_cloud_event(&tampered, "urn:cwl:producer:coverage").is_err());
}

#[test]
fn covers_outbox_edges_and_receipt_binding() {
    let vector = fixture();
    let event = build_usage_event(input_from_fixture(&vector)).unwrap();
    let mut invalid = input_from_fixture(&vector);
    invalid.event_id = "not-a-uuid".into();
    assert_eq!(
        build_usage_event(invalid).unwrap_err().to_string(),
        "event_id must be a UUID"
    );

    let parent_file = std::env::temp_dir().join(format!("cwl-parent-{}", event.event_id));
    fs::write(&parent_file, b"file").unwrap();
    assert!(FileUsageOutbox::open(parent_file.join("child.json")).is_err());
    let _ = fs::remove_file(&parent_file);
    let directory = std::env::temp_dir().join(format!("cwl-directory-{}", event.event_id));
    fs::create_dir(&directory).unwrap();
    assert!(FileUsageOutbox::open(&directory).is_err());
    fs::remove_dir(&directory).unwrap();
    let invalid_json = std::env::temp_dir().join(format!("cwl-invalid-{}", event.event_id));
    fs::write(&invalid_json, b"{").unwrap();
    assert!(FileUsageOutbox::open(&invalid_json).is_err());
    let _ = fs::remove_file(&invalid_json);

    let blocked_path = std::env::temp_dir().join(format!("cwl-blocked-{}", event.event_id));
    fs::create_dir_all(blocked_path.with_extension("tmp")).unwrap();
    assert!(FileUsageOutbox::open(&blocked_path).is_err());
    fs::remove_dir(blocked_path.with_extension("tmp")).unwrap();

    let _current_directory_lock = CURRENT_DIRECTORY_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap();
    let previous_directory = std::env::current_dir().unwrap();
    let isolated_directory = std::env::temp_dir().join(format!("cwl-relative-{}", event.event_id));
    fs::create_dir(&isolated_directory).unwrap();
    std::env::set_current_dir(&isolated_directory).unwrap();
    let relative = PathBuf::from(format!("cwl-relative-{}.json", event.event_id));
    let _relative_guard = CurrentDirectoryGuard {
        previous: previous_directory,
        isolated: isolated_directory,
        relative_file: relative.clone(),
    };
    let _ = fs::remove_file(&relative);

    let flush_parent = std::env::temp_dir().join(format!("cwl-flush-parent-{}", event.event_id));
    fs::create_dir(&flush_parent).unwrap();
    let flush_path = flush_parent.join("outbox.json");
    let mut flush_error = FileUsageOutbox::open(&flush_path).unwrap();
    flush_error.enqueue(event.clone()).unwrap();
    fs::remove_file(&flush_path).unwrap();
    fs::remove_dir(&flush_parent).unwrap();
    assert!(flush_error
        .flush(1, 2, |_| Err(DeliveryError::Transient("offline".into())))
        .is_err());

    let rename_parent = std::env::temp_dir().join(format!("cwl-rename-parent-{}", event.event_id));
    fs::create_dir(&rename_parent).unwrap();
    let rename_path = rename_parent.join("outbox.json");
    let mut rename_error = FileUsageOutbox::open(&rename_path).unwrap();
    rename_error.enqueue(event.clone()).unwrap();
    fs::remove_file(&rename_path).unwrap();
    fs::create_dir(&rename_path).unwrap();
    assert!(rename_error
        .flush(1, 2, |_| Err(DeliveryError::Transient("offline".into())))
        .is_err());
    fs::remove_file(rename_path.with_extension("tmp")).unwrap();
    fs::remove_dir(&rename_path).unwrap();
    fs::remove_dir(rename_parent).unwrap();
    let mut outbox = FileUsageOutbox::open(&relative).unwrap();
    let mut invalid_event = event.clone();
    invalid_event.occurred_at = "not-a-timestamp".into();
    assert!(outbox.enqueue(invalid_event).is_err());
    assert_eq!(outbox.pending_count(), 0);
    assert_eq!(outbox.dead_letter_count(), 0);
    assert!(outbox.flush(0, 1, |_| unreachable!()).is_err());
    assert!(outbox.flush(1, 0, |_| unreachable!()).is_err());
    assert_eq!(
        outbox
            .flush(1, 1, |_| unreachable!())
            .unwrap()
            .attempted_count,
        0
    );
    outbox.enqueue(event.clone()).unwrap();
    outbox.enqueue(event.clone()).unwrap();
    let mut different = input_from_fixture(&vector);
    different.source_event_key = "producer-reference:workflow-381:other".into();
    let mut different_event = build_usage_event(different).unwrap();
    different_event.event_id = event.event_id.clone();
    assert!(outbox.enqueue(different_event).is_err());
    assert_eq!(FileUsageOutbox::open(&relative).unwrap().pending_count(), 1);
    assert_eq!(
        outbox
            .flush(1, 3, |_| Err(DeliveryError::Permanent(
                "bad request".into()
            )))
            .unwrap()
            .dead_lettered_count,
        1
    );
    assert!(outbox.replay_dead_letter("unknown").is_err());
    let _ = fs::remove_file(&relative);

    let transient_path = path("transient", &event);
    let _ = fs::remove_file(&transient_path);
    let mut transient = FileUsageOutbox::open(&transient_path).unwrap();
    transient.enqueue(event.clone()).unwrap();
    assert_eq!(
        transient
            .flush(1, 2, |_| Err(DeliveryError::Transient("offline".into())))
            .unwrap()
            .retried_count,
        1
    );
    assert_eq!(
        transient
            .flush(1, 2, |_| Err(DeliveryError::Transient("offline".into())))
            .unwrap()
            .dead_lettered_count,
        1
    );
    let _ = fs::remove_file(transient_path);

    let duplicate_path = path("duplicate", &event);
    let _ = fs::remove_file(&duplicate_path);
    let mut duplicate = FileUsageOutbox::open(&duplicate_path).unwrap();
    duplicate.enqueue(event.clone()).unwrap();
    let duplicate_receipt = receipt(&event, "accepted");
    assert_eq!(
        duplicate
            .flush(1, 3, |_| Ok(UsageDeliveryResponse {
                event_receipts: vec![duplicate_receipt.clone(), duplicate_receipt]
            }))
            .unwrap()
            .retried_count,
        1
    );
    assert_eq!(
        duplicate
            .flush(1, 3, |_| Ok(UsageDeliveryResponse {
                event_receipts: vec![]
            }))
            .unwrap()
            .retried_count,
        1
    );
    let _ = fs::remove_file(duplicate_path);

    let accepted_path = path("accepted", &event);
    let _ = fs::remove_file(&accepted_path);
    let mut accepted = FileUsageOutbox::open(&accepted_path).unwrap();
    accepted.enqueue(event.clone()).unwrap();
    assert_eq!(
        accepted
            .flush(1, 2, |_| Ok(UsageDeliveryResponse {
                event_receipts: vec![receipt(&event, "accepted")]
            }))
            .unwrap()
            .accepted_count,
        1
    );
    let _ = fs::remove_file(accepted_path);

    let replay_path = path("replay", &event);
    let _ = fs::remove_file(&replay_path);
    let mut replay = FileUsageOutbox::open(&replay_path).unwrap();
    replay.enqueue(event.clone()).unwrap();
    assert_eq!(
        replay
            .flush(1, 2, |_| Ok(UsageDeliveryResponse {
                event_receipts: vec![receipt(&event, "duplicate_replay")]
            }))
            .unwrap()
            .duplicate_replay_count,
        1
    );
    let _ = fs::remove_file(replay_path);

    let rejected_path = path("rejected", &event);
    let _ = fs::remove_file(&rejected_path);
    let mut rejected = FileUsageOutbox::open(&rejected_path).unwrap();
    rejected.enqueue(event.clone()).unwrap();
    let rejected_result = rejected
        .flush(1, 2, |_| {
            Ok(UsageDeliveryResponse {
                event_receipts: vec![receipt(&event, "rejected")],
            })
        })
        .unwrap();
    assert_eq!(rejected_result.rejected_count, 1);
    assert_eq!(rejected.dead_letter_count(), 1);
    rejected.replay_dead_letter(&event.event_id).unwrap();
    assert_eq!(rejected.pending_count(), 1);
    let _ = fs::remove_file(rejected_path);

    let stale_path = path("stale", &event);
    let _ = fs::remove_file(&stale_path);
    let mut stale = FileUsageOutbox::open(&stale_path).unwrap();
    let mut second_input = input_from_fixture(&vector);
    second_input.event_id = "019d7b92-1aa0-7a7f-b61c-962c0f4bf6ad".into();
    second_input.source_event_key = "producer-reference:workflow-381:stale-02".into();
    let second = build_usage_event(second_input).unwrap();
    let mut third_input = input_from_fixture(&vector);
    third_input.event_id = "019d7b92-1aa0-7a7f-b61c-962c0f4bf6ae".into();
    third_input.source_event_key = "producer-reference:workflow-381:stale-03".into();
    let third = build_usage_event(third_input).unwrap();
    stale.enqueue(event.clone()).unwrap();
    stale.enqueue(second.clone()).unwrap();
    stale.enqueue(third.clone()).unwrap();
    let mut wrong_hash = receipt(&event, "accepted");
    wrong_hash.source_payload_hash = Some("sha256:wrong".into());
    let mut wrong_version = receipt(&second, "duplicate_replay");
    wrong_version.event_contract_version = Some(second.event_contract_version + 1);
    let mut wrong_tenant = receipt(&third, "rejected");
    wrong_tenant.tenant_reference = None;
    let stale_result = stale
        .flush(3, 2, |_| {
            Ok(UsageDeliveryResponse {
                event_receipts: vec![wrong_hash, wrong_version, wrong_tenant],
            })
        })
        .unwrap();
    assert_eq!(stale_result.retried_count, 3);
    assert_eq!(stale.pending_count(), 3);
    let _ = fs::remove_file(stale_path);
}
