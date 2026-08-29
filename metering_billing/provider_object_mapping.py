"""Keep provider object identifiers behind an explicit sticky mapping boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime


PROVIDER_OBJECT_MAPPING_CONTRACT_VERSION = 1
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_OBJECT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,99}$")


class ProviderObjectMappingError(ValueError):
    """Raised when a provider object mapping cannot be trusted or changed."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _aware_utc(value: datetime, reason_code: str) -> datetime:
    """Normalize an aware instant and reject ambiguous local timestamps."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderObjectMappingError(reason_code)
    return value.astimezone(UTC)


def _reference(value: str, reason_code: str, maximum: int) -> str:
    """Validate a bounded identifier without silently rewriting it."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= maximum
        or any(character in "\x00\r\n" for character in value)
    ):
        raise ProviderObjectMappingError(reason_code)
    return value


def _overlap(
    first_from: datetime,
    first_to: datetime | None,
    second_from: datetime,
    second_to: datetime | None,
) -> bool:
    """Return whether two mapping intervals overlap."""
    return (first_to is None or second_from < first_to) and (
        second_to is None or first_from < second_to
    )


@dataclass(frozen=True)
class ProviderObjectMapping:
    """An effective-dated internal-to-provider object reference."""

    provider_account_reference: str
    provider_code: str
    internal_object_type: str
    internal_object_reference: str
    provider_object_type: str
    provider_object_reference: str
    valid_from: datetime
    valid_to: datetime | None = None
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Reject invalid identifiers, intervals, and timestamps."""
        account = _reference(
            self.provider_account_reference, "provider_account_reference_invalid", 200
        )
        if not isinstance(self.provider_code, str) or _CODE_PATTERN.fullmatch(self.provider_code) is None:
            raise ProviderObjectMappingError("provider_code_invalid")
        for value, reason_code in (
            (self.internal_object_type, "internal_object_type_invalid"),
            (self.provider_object_type, "provider_object_type_invalid"),
        ):
            if not isinstance(value, str) or _OBJECT_TYPE_PATTERN.fullmatch(value) is None:
                raise ProviderObjectMappingError(reason_code)
        internal_reference = _reference(
            self.internal_object_reference, "internal_object_reference_invalid", 200
        )
        provider_reference = _reference(
            self.provider_object_reference, "provider_object_reference_invalid", 200
        )
        valid_from = _aware_utc(self.valid_from, "valid_from_invalid")
        valid_to = (
            None if self.valid_to is None else _aware_utc(self.valid_to, "valid_to_invalid")
        )
        if valid_to is not None and valid_to <= valid_from:
            raise ProviderObjectMappingError("valid_interval_invalid")
        recorded_at = _aware_utc(self.recorded_at, "recorded_at_invalid")
        object.__setattr__(self, "provider_account_reference", account)
        object.__setattr__(self, "internal_object_reference", internal_reference)
        object.__setattr__(self, "provider_object_reference", provider_reference)
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_to", valid_to)
        object.__setattr__(self, "recorded_at", recorded_at)

    def as_contract_dict(self) -> dict[str, object]:
        """Render the mapping without credentials or provider payloads."""
        payload: dict[str, object] = {
            "provider_object_mapping_contract_version": PROVIDER_OBJECT_MAPPING_CONTRACT_VERSION,
            "provider_account_reference": self.provider_account_reference,
            "provider_code": self.provider_code,
            "internal_object_type": self.internal_object_type,
            "internal_object_reference": self.internal_object_reference,
            "provider_object_type": self.provider_object_type,
            "provider_object_reference": self.provider_object_reference,
            "valid_from": self.valid_from.isoformat().replace("+00:00", "Z"),
            "recorded_at": self.recorded_at.isoformat().replace("+00:00", "Z"),
        }
        if self.valid_to is not None:
            payload["valid_to"] = self.valid_to.isoformat().replace("+00:00", "Z")
        return payload


def _now_utc() -> datetime:
    """Return an aware timestamp for mapping construction defaults."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class _StoredMapping:
    """Internal mapping record with replacement history preserved."""

    mapping: ProviderObjectMapping


class ProviderObjectMappingRegistry:
    """Record and resolve sticky mappings without implicit provider failover."""

    def __init__(self) -> None:
        self._mappings: list[_StoredMapping] = []

    def record(self, mapping: ProviderObjectMapping) -> ProviderObjectMapping:
        """Record a mapping only when its internal and external intervals are free."""
        if not isinstance(mapping, ProviderObjectMapping):
            raise ProviderObjectMappingError("mapping_invalid")
        for stored in self._mappings:
            current = stored.mapping
            same_internal = (
                current.provider_account_reference == mapping.provider_account_reference
                and current.internal_object_type == mapping.internal_object_type
                and current.internal_object_reference == mapping.internal_object_reference
            )
            same_external = (
                current.provider_account_reference == mapping.provider_account_reference
                and current.provider_object_type == mapping.provider_object_type
                and current.provider_object_reference == mapping.provider_object_reference
            )
            if (same_internal or same_external) and _overlap(
                current.valid_from, current.valid_to, mapping.valid_from, mapping.valid_to
            ):
                raise ProviderObjectMappingError(
                    "internal_mapping_conflict" if same_internal else "external_mapping_conflict"
                )
        self._mappings.append(_StoredMapping(mapping))
        return mapping

    def replace(
        self,
        mapping: ProviderObjectMapping,
        *,
        effective_from: datetime,
    ) -> ProviderObjectMapping:
        """Explicitly close the active mapping before recording its replacement."""
        if not isinstance(mapping, ProviderObjectMapping):
            raise ProviderObjectMappingError("mapping_invalid")
        instant = _aware_utc(effective_from, "valid_from_invalid")
        active = self._find_internal(
            mapping.provider_account_reference,
            mapping.internal_object_type,
            mapping.internal_object_reference,
            instant,
        )
        if active is None or instant <= active.valid_from:
            raise ProviderObjectMappingError("replacement_target_missing")
        if mapping.valid_from != instant:
            raise ProviderObjectMappingError("replacement_time_mismatch")
        if mapping.provider_code != active.provider_code:
            raise ProviderObjectMappingError("replacement_provider_mismatch")
        self._mappings.remove(_StoredMapping(active))
        closed = ProviderObjectMapping(
            active.provider_account_reference,
            active.provider_code,
            active.internal_object_type,
            active.internal_object_reference,
            active.provider_object_type,
            active.provider_object_reference,
            active.valid_from,
            instant,
            active.recorded_at,
        )
        self._mappings.append(_StoredMapping(closed))
        try:
            return self.record(mapping)
        except ProviderObjectMappingError:
            self._mappings.remove(_StoredMapping(closed))
            self._mappings.append(_StoredMapping(active))
            raise

    def resolve_internal(
        self,
        provider_account_reference: str,
        internal_object_type: str,
        internal_object_reference: str,
        *,
        at: datetime | None = None,
    ) -> ProviderObjectMapping:
        """Resolve one internal object or fail closed without provider fallback."""
        instant = _aware_utc(at or _now_utc(), "routing_time_invalid")
        mapping = self._find_internal(
            provider_account_reference,
            internal_object_type,
            internal_object_reference,
            instant,
        )
        if mapping is None:
            raise ProviderObjectMappingError("mapping_not_found")
        return mapping

    def resolve_external(
        self,
        provider_account_reference: str,
        provider_object_type: str,
        provider_object_reference: str,
        *,
        at: datetime | None = None,
    ) -> ProviderObjectMapping:
        """Resolve one provider object or fail closed."""
        instant = _aware_utc(at or _now_utc(), "routing_time_invalid")
        for stored in self._mappings:
            mapping = stored.mapping
            if (
                mapping.provider_account_reference == provider_account_reference
                and mapping.provider_object_type == provider_object_type
                and mapping.provider_object_reference == provider_object_reference
                and mapping.valid_from <= instant
                and (mapping.valid_to is None or instant < mapping.valid_to)
            ):
                return mapping
        raise ProviderObjectMappingError("mapping_not_found")

    def all_mappings(self) -> tuple[ProviderObjectMapping, ...]:
        """Return mapping history in deterministic effective order."""
        return tuple(
            stored.mapping
            for stored in sorted(
                self._mappings,
                key=lambda item: (
                    item.mapping.provider_account_reference,
                    item.mapping.internal_object_type,
                    item.mapping.internal_object_reference,
                    item.mapping.valid_from,
                ),
            )
        )

    def _find_internal(
        self,
        provider_account_reference: str,
        internal_object_type: str,
        internal_object_reference: str,
        instant: datetime,
    ) -> ProviderObjectMapping | None:
        """Find an active internal mapping after the caller validates time."""
        for stored in self._mappings:
            mapping = stored.mapping
            if (
                mapping.provider_account_reference == provider_account_reference
                and mapping.internal_object_type == internal_object_type
                and mapping.internal_object_reference == internal_object_reference
                and mapping.valid_from <= instant
                and (mapping.valid_to is None or instant < mapping.valid_to)
            ):
                return mapping
        return None
