"""Shared data model. Every workstream codes against these types.

Nothing in here reads files or calls an LLM. Keep it dependency-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Output vocabulary (problem_statement.md / AGENTS.md section 6.4)
# ---------------------------------------------------------------------------

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")

# operation -> (phase, state). Order inside each phase is the required sort order.
OPERATIONS: dict[str, tuple[str, str]] = {
    # findings
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    # remediation
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    # verification
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
OPERATION_ORDER: dict[str, int] = {op: i for i, op in enumerate(OPERATIONS)}

# system_id values seen in the sample answers and activity stream.
KNOWN_SYSTEMS = (
    "account_service",
    "search_service",
    "support_platform",
    "analytics_platform",
    "security_service",
    "integration_service",
    "backup_service",
    "export_service",
    "identity_service",
    "marketing_platform",
    "cache_service",
    "email_delivery_service",
    "legal_service",
)

# deletion_rules.csv data categories.
CATEGORIES = (
    "USER_PROFILE",
    "SECONDARY_IDENTIFIER",
    "SEARCH_INDEX_ENTRY",
    "CACHE_ENTRY",
    "SUPPORT_MESSAGE",
    "SUPPORT_ATTACHMENT",
    "ANALYTICS_PROFILE",
    "PRODUCT_TELEMETRY",
    "USER_EXPORT",
    "MARKETING_PROFILE",
    "EMAIL_DELIVERY_LOG",
    "SECURITY_AUDIT_LOG",
    "LEGAL_HOLD_RECORD",
    "BACKUP_ARCHIVE",
    "ACCOUNT_TOMBSTONE",
    "DATA_FLOW",  # not a rule category, but a resource kind we must track
)

# Normalized state hints an extractor may attach to an Observation.
STATE_HINTS = (
    "PRESENT",      # record exists with direct identifiers
    "ABSENT",       # record confirmed gone (404 / NOT_FOUND / missing from snapshot that covers it)
    "REDACTED",     # record exists, direct identifiers removed
    "ANONYMIZED",   # record exists, unlinked from the user
    "ACTIVE",       # data flow enabled / restore allowed / backup active
    "DISABLED",     # data flow disabled
    "BLOCKED",      # restore blocked
    "UNKNOWN",      # extractor could not tell
)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_ts(value: str) -> datetime:
    """Parse ISO-8601 timestamps like 2026-09-15T11:44:55Z into aware UTC datetimes."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def case_sort_key(case_id: str) -> tuple:
    """Natural sort: case_33 < case_100. Falls back to plain string."""
    prefix, _, num = case_id.rpartition("_")
    if num.isdigit():
        return (prefix, int(num))
    return (case_id, -1)


def resource_sort_key(resource_id: str) -> tuple:
    """Natural sort for resource ids (account_005 < account_4248)."""
    prefix, _, num = resource_id.rpartition("_")
    if num.isdigit():
        return (prefix, int(num), resource_id)
    return (resource_id, -1, resource_id)


# ---------------------------------------------------------------------------
# Dataset rows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    case_id: str
    email: str
    requested_at: datetime
    reported_status: str
    reported_at: datetime
    audit_at: datetime
    request_text: str


@dataclass(frozen=True)
class Identity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: datetime


@dataclass(frozen=True)
class AuditEvent:
    audit_id: str
    user_id: str
    event_time: datetime
    field_name: str  # email | phone | email_hash
    old_value: str
    new_value: str


@dataclass(frozen=True)
class ActivityEvent:
    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str  # SUCCESS | FAILED | PARTIAL
    details: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str  # DELETE | REDACT | ANONYMIZE | RETAIN


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    file_path: str  # relative to dataset/
    origin: str
    captured_at: datetime


# ---------------------------------------------------------------------------
# Derived objects
# ---------------------------------------------------------------------------

@dataclass
class Subject:
    """Everything we know a person has ever been called in the systems."""
    user_id: str
    emails: set[str] = field(default_factory=set)        # lower-cased
    phones: set[str] = field(default_factory=set)        # normalized: digits only, keep leading +
    email_hashes: set[str] = field(default_factory=set)  # lower-cased
    names: set[str] = field(default_factory=set)         # as written
    # identifier value -> audit_id that introduced/retired it (for citations)
    audit_refs: dict[str, str] = field(default_factory=dict)


@dataclass
class IntentResult:
    case_id: str
    is_deletion: bool
    method: str            # "rule" | "llm" | "llm+rule"
    reason: str = ""
    injection_suspected: bool = False


@dataclass
class Observation:
    """One fact about one resource, as seen in one piece of evidence.

    evidence_id : file_id, activity_id or audit_id that proves it (participant-visible)
    captured_at : when the fact was true (manifest captured_at, or the snapshot time inside the file
                  if the file carries its own earlier capture time - never later than the manifest)
    system_id   : output vocabulary (see KNOWN_SYSTEMS)
    resource_id : e.g. account_4248, search_052, data_flow_9522, backup_706, audit_record_9394
    category    : one of CATEGORIES when known, else ""
    state_hint  : one of STATE_HINTS
    identifiers : values found on the record, already normalized like Subject fields
    extra       : raw fields worth keeping (table name, status columns, retention_until, ...)
    """
    evidence_id: str
    captured_at: datetime
    system_id: str
    resource_id: str
    category: str
    state_hint: str
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    email_hashes: set[str] = field(default_factory=set)
    user_ids: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    extra: dict = field(default_factory=dict)
    source_path: str = ""

    def mentions(self, subject: Subject) -> bool:
        return bool(
            (self.emails & subject.emails)
            or (self.phones & subject.phones)
            or (self.email_hashes & subject.email_hashes)
            or (subject.user_id and subject.user_id in self.user_ids)
        )


@dataclass
class ResourceState:
    """Best-known state of one resource for one case at audit_at."""
    system_id: str
    resource_id: str
    category: str
    state: str               # one of STATE_HINTS
    evidence_id: str         # the observation that establishes the state
    captured_at: datetime
    exposes_pii: bool = False
    linked_user_ids: set[str] = field(default_factory=set)
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    step_no: int
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str

    def to_json_dict(self) -> dict:
        return {
            "step_no": self.step_no,
            "phase": self.phase,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation,
            "state": self.state,
            "evidence_id": self.evidence_id,
        }


@dataclass
class CaseResult:
    case_id: str
    deletion_status: str
    steps: list[Step]
    notes: list[str] = field(default_factory=list)  # free-form, for reports / viewer; never graded


# ---------------------------------------------------------------------------
# Normalizers (use everywhere so matching is consistent)
# ---------------------------------------------------------------------------

def norm_email(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def norm_phone(value: Optional[str]) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    digits = "".join(ch for ch in v if ch.isdigit())
    return ("+" if v.startswith("+") else "") + digits


def norm_hash(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def norm_name(value: Optional[str]) -> str:
    return " ".join((value or "").split()).lower()
