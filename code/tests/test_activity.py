"""Tests for ghost/activity.py on synthetic rows plus a real-CSV smoke check."""
from __future__ import annotations

import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from ghost.activity import ActivityIndex, system_for  # noqa: E402
from ghost.models import ActivityEvent, Subject, parse_ts  # noqa: E402


def row(aid, t, etype, src, tgt, res, result, details):
    return ActivityEvent(aid, parse_ts(t), etype, src, tgt, res, result, details)


EVENTS = [
    row("activity_5", "2026-09-02T10:00:00Z", "DISABLE_DATA_FLOW", "deletion_worker", "integration_service",
        "data_flow_7", "SUCCESS", "Policy action completed for data_flow_7."),
    row("activity_4", "2026-09-01T10:00:00Z", "CHECK_DATA_FLOW", "deletion_worker", "integration_service",
        "data_flow_7", "SUCCESS", "Connector status check confirmed that data flow data_flow_7 remains enabled."),
    row("activity_6", "2026-09-01T11:00:00Z", "CHECK_RESTORE_ACCESS", "deletion_worker", "backup_service",
        "backup_3", "SUCCESS", "Restore-access check confirmed that archive backup_3 can still be restored."),
    row("activity_7", "2026-09-03T11:00:00Z", "BLOCK_RESTORE", "deletion_worker", "backup_service",
        "backup_3", "SUCCESS", "Policy action completed for backup_3."),
    row("activity_8", "2026-08-01T11:00:00Z", "SYNC_RECORD", "integration_service", "search_service",
        "data_flow_7", "SUCCESS", "Active connector repopulated downstream resource search_9."),
    row("activity_9", "2026-08-02T11:00:00Z", "RESTORE_BACKUP", "backup_service", "search_service",
        "backup_3", "SUCCESS", "Restore job repopulated downstream resource search_9."),
    row("activity_10", "2026-07-01T00:00:00Z", "LINK_RESOURCE_IDENTITY", "data_catalog", "search_service",
        "search_9", "SUCCESS", "Data catalog linked search_9 (search_9) to Ada@Example.com and user_1."),
    row("activity_11", "2026-08-05T00:00:00Z", "RETAIN_RECORD", "security_service", "archive_service",
        "audit_record_4", "SUCCESS", "Security-event archival processed audit_record_4."),
    row("activity_12", "2026-08-06T00:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service",
        "account_2", "SUCCESS", "Deletion worker submitted primary profile account_2 to account_service."),
    row("activity_13", "2026-08-07T00:00:00Z", "SYNC_RECORD", "account_service", "account_replication_cluster",
        "account_2", "SUCCESS", "Replication worker reported its final checkpoint for account_2."),
    row("activity_14", "2026-08-01T00:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service",
        "account_99", "SUCCESS", "Deletion worker submitted primary profile account_99 to account_service."),
]
ADA = Subject(user_id="user_1", emails={"ada@example.com"})
OTHER = Subject(user_id="user_2", emails={"zed@example.com"})


def test_system_mapping():
    assert system_for("audit_record_4") == ("security_service", "SECURITY_AUDIT_LOG")
    assert system_for("data_flow_7")[0] == "integration_service"
    assert system_for("tombstone_1")[0] == "identity_service"
    assert system_for("email_delivery_3")[0] == "email_delivery_service"


def test_links_and_one_hop_expansion():
    ix = ActivityIndex(EVENTS)
    linked = ix.resources_linked_to(ADA)
    assert ("search_service", "search_9") in linked
    assert ("integration_service", "data_flow_7") in linked  # repopulated search_9
    assert ("backup_service", "backup_3") in linked
    assert ix.resources_linked_to(OTHER) == set()


def test_states_and_time_cutoff():
    ix = ActivityIndex(EVENTS)
    obs = ix.observations_for(ADA, parse_ts("2026-09-01T12:00:00Z"))
    by = {(o.evidence_id, o.resource_id): o for o in obs}
    assert by[("activity_4", "data_flow_7")].state_hint == "ACTIVE"
    assert by[("activity_6", "backup_3")].state_hint == "ACTIVE"
    assert by[("activity_9", "backup_3")].state_hint == "ACTIVE"
    assert by[("activity_9", "backup_3")].extra["downstream"] == "search_9"
    assert by[("activity_8", "data_flow_7")].state_hint == "UNKNOWN"
    assert by[("activity_10", "search_9")].user_ids == {"user_1"}
    assert "activity_5" not in {o.evidence_id for o in obs}  # after as_of
    later = ix.observations_for(ADA, parse_ts("2026-10-01T00:00:00Z"))
    states = {o.evidence_id: o.state_hint for o in later}
    assert states["activity_5"] == "DISABLED" and states["activity_7"] == "BLOCKED"
    keys = [(o.captured_at, int(o.evidence_id.split("_")[1])) for o in later]
    assert keys == sorted(keys)


def test_extra_resources_from_other_evidence():
    ix = ActivityIndex(EVENTS)
    obs = ix.observations_for(OTHER, parse_ts("2026-10-01T00:00:00Z"),
                              resources=[("account_service", "account_2"), "audit_record_4"])
    by = {o.evidence_id: o for o in obs}
    assert set(by) == {"activity_11", "activity_12", "activity_13"}
    assert by["activity_11"].state_hint == "PRESENT" and by["activity_11"].system_id == "security_service"
    assert by["activity_12"].state_hint == "UNKNOWN"  # a reported delete is only a claim
    assert by["activity_13"].extra["replica"] == "account_replication_cluster"


def test_real_activity_smoke():
    from ghost.identity import resolve_subject
    from ghost.loaders import load_activity, load_audit_log, load_identities, load_sample_cases

    ix = ActivityIndex(load_activity())
    ids, aud = load_identities(), load_audit_log()
    for c in load_sample_cases():
        s = resolve_subject(c.email, ids, aud, c.audit_at)
        for o in ix.observations_for(s, c.audit_at):
            assert o.captured_at <= c.audit_at and o.evidence_id.startswith("activity_")
