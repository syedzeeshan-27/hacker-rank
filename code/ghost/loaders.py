"""CSV loaders for everything under dataset/. Plain csv module, UTF-8, Windows-safe."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .config import DATASET_DIR
from .models import (
    ActivityEvent,
    AuditEvent,
    Case,
    EvidenceFile,
    Identity,
    Rule,
    Step,
    parse_ts,
)


def _rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_cases(path: Path | None = None) -> list[Case]:
    path = path or DATASET_DIR / "deletion_requests.csv"
    return [
        Case(
            case_id=r["case_id"],
            email=r["email"],
            requested_at=parse_ts(r["requested_at"]),
            reported_status=r["reported_status"],
            reported_at=parse_ts(r["reported_at"]),
            audit_at=parse_ts(r["audit_at"]),
            request_text=r["request_text"],
        )
        for r in _rows(path)
    ]


def load_sample_cases() -> list[Case]:
    return load_cases(DATASET_DIR / "sample_deletion_requests.csv")


def load_sample_output(path: Path | None = None) -> dict[str, tuple[str, list[Step]]]:
    """case_id -> (deletion_status, steps)."""
    path = path or DATASET_DIR / "sample_output.csv"
    out: dict[str, tuple[str, list[Step]]] = {}
    for r in _rows(path):
        steps = [
            Step(
                step_no=int(s["step_no"]),
                phase=s["phase"],
                system_id=s["system_id"],
                resource_id=s["resource_id"],
                operation=s["operation"],
                state=s["state"],
                evidence_id=s.get("evidence_id", ""),
            )
            for s in json.loads(r["steps_json"] or "[]")
        ]
        out[r["case_id"]] = (r["deletion_status"], steps)
    return out


def load_identities() -> list[Identity]:
    return [
        Identity(
            user_id=r["user_id"],
            name=r["name"],
            email=r["email"],
            phone=r["phone"],
            email_hash=r["email_hash"],
            updated_at=parse_ts(r["updated_at"]),
        )
        for r in _rows(DATASET_DIR / "user_identities.csv")
    ]


def load_audit_log() -> list[AuditEvent]:
    return [
        AuditEvent(
            audit_id=r["audit_id"],
            user_id=r["user_id"],
            event_time=parse_ts(r["event_time"]),
            field_name=r["field_name"],
            old_value=r["old_value"],
            new_value=r["new_value"],
        )
        for r in _rows(DATASET_DIR / "user_audit_log.csv")
    ]


def load_activity() -> list[ActivityEvent]:
    return [
        ActivityEvent(
            activity_id=r["activity_id"],
            event_time=parse_ts(r["event_time"]),
            event_type=r["event_type"],
            source_system=r["source_system"],
            target_system=r["target_system"],
            resource_id=r["resource_id"],
            reported_result=r["reported_result"],
            details=r["details"],
        )
        for r in _rows(DATASET_DIR / "system_activity.csv")
    ]


def load_rules() -> list[Rule]:
    return [
        Rule(
            rule_id=r["rule_id"],
            data_category=r["data_category"],
            condition=r["condition"],
            required_action=r["required_action"],
        )
        for r in _rows(DATASET_DIR / "deletion_rules.csv")
    ]


def load_manifest() -> list[EvidenceFile]:
    return [
        EvidenceFile(
            file_id=r["file_id"],
            file_path=r["file_path"],
            origin=r["origin"],
            captured_at=parse_ts(r["captured_at"]),
        )
        for r in _rows(DATASET_DIR / "evidence_files.csv")
    ]
