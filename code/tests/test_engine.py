"""Unit tests for the decision engine (state / policy / planner) on synthetic objects."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from ghost import planner, policy, state  # noqa: E402
from ghost.activity import ActivityIndex  # noqa: E402
from ghost.evidence.registry import Coverage, ExtractionResult  # noqa: E402
from ghost.loaders import load_rules  # noqa: E402
from ghost.models import (  # noqa: E402
    OPERATION_ORDER,
    ActivityEvent,
    Case,
    Observation,
    ResourceState,
    Rule,
    Subject,
    fmt_ts,
    parse_ts,
)

T = parse_ts
RULES = load_rules()
SUBJ = Subject(user_id="user_77", emails={"kai@example.org"}, phones={"+15550001"}, names={"Kai Moss", "kai moss"})


def case(audit="2026-09-15T12:00:00Z", requested="2026-09-01T09:00:00Z", reported="2026-09-02T12:00:00Z",
         cid="case_x") -> Case:
    return Case(cid, "kai@example.org", T(requested), "COMPLETE", T(reported), T(audit), "Please delete my account.")


def obs(ev, at, system, rid, cat, st, *, filed=None, emails=(), users=(), extra=None) -> Observation:
    ex = dict(extra or {})
    if filed and filed != at:
        ex["manifest_captured_at"] = filed
    return Observation(evidence_id=ev, captured_at=T(at), system_id=system, resource_id=rid, category=cat,
                       state_hint=st, emails=set(emails), user_ids=set(users), extra=ex)


def run(observations, coverage=(), events=(), c=None, pool=None):
    c = c or case()
    ext = ExtractionResult(observations=list(observations), coverage=list(coverage))
    act = ActivityIndex(list(events))
    ctx = state.AuditContext.build(ext, act, pool=pool or [])
    return planner.audit_case(c, SUBJ, ext, act, RULES, ctx)


def tuples(steps):
    return [(s.phase, s.operation, s.system_id, s.resource_id, s.evidence_id) for s in steps]


# ---------------------------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------------------------

def test_policy_follows_rule_table():
    expected = {
        "USER_PROFILE": "DELETE", "SECONDARY_IDENTIFIER": "DELETE", "SEARCH_INDEX_ENTRY": "DELETE",
        "CACHE_ENTRY": "DELETE", "USER_EXPORT": "DELETE", "MARKETING_PROFILE": "DELETE",
        "SUPPORT_MESSAGE": "REDACT", "SUPPORT_ATTACHMENT": "DELETE", "ANALYTICS_PROFILE": "ANONYMIZE",
        "PRODUCT_TELEMETRY": "ANONYMIZE", "EMAIL_DELIVERY_LOG": "REDACT", "SECURITY_AUDIT_LOG": "RETAIN",
        "LEGAL_HOLD_RECORD": "RETAIN", "BACKUP_ARCHIVE": "BLOCK_RESTORE", "ACCOUNT_TOMBSTONE": "REDACT",
        "DATA_FLOW": "DISABLE",
    }
    for cat, action in expected.items():
        assert policy.required_treatment(cat, None, RULES) == action, cat
    assert policy.required_treatment("SOMETHING_ELSE", None, RULES) == ""


def test_policy_is_driven_by_the_table_not_a_dict():
    custom = [Rule("r1", "SEARCH_INDEX_ENTRY", "Always", "ANONYMIZE")]
    assert policy.required_treatment("SEARCH_INDEX_ENTRY", None, custom) == "ANONYMIZE"
    assert policy.required_treatment("USER_PROFILE", None, custom) == ""


def test_retention_expired_means_no_rule():
    st = ResourceState("security_service", "audit_record_1", "SECURITY_AUDIT_LOG", "PRESENT", "ev", T("2026-09-01T00:00:00Z"),
                       extra={"retention_until": "2026-01-01T00:00:00Z"})
    assert policy.required_treatment("SECURITY_AUDIT_LOG", st, RULES, as_of=T("2026-09-15T00:00:00Z")) == ""
    st.extra["retention_until"] = "04 May 2033"
    assert policy.required_treatment("SECURITY_AUDIT_LOG", st, RULES, as_of=T("2026-09-15T00:00:00Z")) == "RETAIN"


# ---------------------------------------------------------------------------------------------
# ordering and citations
# ---------------------------------------------------------------------------------------------

def _messy_world():
    at = "2026-09-15T10:00:00Z"
    return [
        obs("ev_1", at, "account_service", "account_5", "USER_PROFILE", "ABSENT", emails=["kai@example.org"]),
        obs("ev_1", at, "search_service", "search_10", "SEARCH_INDEX_ENTRY", "PRESENT", emails=["kai@example.org"]),
        obs("ev_1", at, "search_service", "search_9", "SEARCH_INDEX_ENTRY", "PRESENT", emails=["kai@example.org"]),
        obs("ev_1", at, "support_platform", "support_3", "SUPPORT_MESSAGE", "PRESENT", emails=["kai@example.org"]),
        obs("ev_1", at, "analytics_platform", "analytics_4", "ANALYTICS_PROFILE", "REDACTED", users=["user_77"]),
        obs("ev_1", at, "integration_service", "data_flow_2", "DATA_FLOW", "ACTIVE", users=["user_77"]),
        obs("ev_1", at, "backup_service", "backup_8", "BACKUP_ARCHIVE", "ACTIVE", users=["user_77"]),
        obs("ev_1", at, "security_service", "audit_record_6", "SECURITY_AUDIT_LOG", "PRESENT", users=["user_77"]),
    ]


def test_incomplete_plan_order_and_empty_evidence_for_future_steps():
    status, steps, _ = run(_messy_world())
    assert status == "INCOMPLETE"
    assert [s.step_no for s in steps] == list(range(1, len(steps) + 1))
    phases = [s.phase for s in steps]
    assert phases == sorted(phases, key=["FINDING", "REMEDIATION", "VERIFICATION"].index)
    for ph in ("FINDING", "REMEDIATION", "VERIFICATION"):
        ops = [OPERATION_ORDER[s.operation] for s in steps if s.phase == ph]
        assert ops == sorted(ops)
    # natural resource order: search_9 before search_10
    finds = [(s.operation, s.resource_id) for s in steps if s.phase == "FINDING"]
    assert finds[:2] == [("REPORT_REMAINING_RECORD", "search_9"), ("REPORT_REMAINING_RECORD", "search_10")]
    # flow and restore are closed before records are changed
    rem = [s.operation for s in steps if s.phase == "REMEDIATION"]
    assert rem[:2] == ["DISABLE_DATA_FLOW", "BLOCK_RESTORE"]
    assert all(s.evidence_id == "ev_1" for s in steps if s.phase == "FINDING")
    assert all(s.evidence_id == "" for s in steps if s.phase != "FINDING")
    # analytics with only a user reference left is still attributable -> anonymize
    assert ("FINDING", "REPORT_EXPOSED_PII", "analytics_platform", "analytics_4", "ev_1") in tuples(steps)
    assert ("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_6", "") in tuples(steps)


def test_complete_case_cites_latest_eligible_file():
    world = [
        obs("ev_old", "2026-09-10T10:00:00Z", "account_service", "account_5", "USER_PROFILE", "ABSENT", emails=["kai@example.org"]),
        obs("ev_new", "2026-09-14T10:00:00Z", "account_service", "account_5", "USER_PROFILE", "ABSENT", emails=["kai@example.org"]),
        obs("ev_new", "2026-09-14T10:00:00Z", "support_platform", "support_3", "SUPPORT_MESSAGE", "REDACTED", users=["user_77"]),
    ]
    status, steps, _ = run(world)
    assert status == "COMPLETE"
    assert tuples(steps) == [
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_5", "ev_new"),
        ("VERIFICATION", "VERIFY_RECORD_REDACTED", "support_platform", "support_3", "ev_new"),
    ]


# ---------------------------------------------------------------------------------------------
# audit_at filtering
# ---------------------------------------------------------------------------------------------

def test_file_delivered_after_audit_is_never_cited_even_with_older_inner_time():
    world = [
        obs("ev_ok", "2026-09-15T09:00:00Z", "account_service", "account_5", "USER_PROFILE", "ABSENT", emails=["kai@example.org"]),
        # bundle delivered 09-17 holding a snapshot from 09-15 11:00 (before audit_at)
        obs("ev_late", "2026-09-15T11:00:00Z", "account_service", "account_5", "USER_PROFILE", "PRESENT",
            filed="2026-09-17T12:00:00Z", emails=["kai@example.org"]),
    ]
    status, steps, _ = run(world)
    assert status == "COMPLETE"
    assert tuples(steps) == [("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_5", "ev_ok")]


def test_later_evidence_cannot_flip_the_answer():
    world = [
        obs("ev_a", "2026-09-10T09:00:00Z", "account_service", "account_5", "USER_PROFILE", "ABSENT", emails=["kai@example.org"]),
        obs("ev_b", "2026-09-20T09:00:00Z", "search_service", "search_1", "SEARCH_INDEX_ENTRY", "PRESENT", emails=["kai@example.org"]),
    ]
    assert run(world, c=case(audit="2026-09-15T12:00:00Z"))[0] == "COMPLETE"
    assert run(world, c=case(audit="2026-09-21T12:00:00Z"))[0] == "INCOMPLETE"


def test_coverage_absence_only_for_deletable_records_without_explicit_facts():
    cov = [Coverage("ev_snap", T("2026-09-14T12:00:00Z"), "account_service", "account_service/customer_profiles", set()),
           Coverage("ev_snap", T("2026-09-14T12:00:00Z"), "security_service", "security_service/events", set())]
    world = [obs("ev_link", "2026-08-01T00:00:00Z", "security_service", "audit_record_6", "SECURITY_AUDIT_LOG",
                 "PRESENT", users=["user_77"])]
    ev = [ActivityEvent("activity_1", T("2026-09-02T11:00:00Z"), "DELETE_RECORD", "deletion_worker",
                        "account_service", "account_5", "SUCCESS", "Deletion worker submitted primary profile account_5.")]
    status, steps, _ = run(world, coverage=cov, events=ev)
    assert status == "COMPLETE"
    # the account (from the run window) is proven absent by the snapshot; the retained audit record is
    # not made "absent" by a snapshot slice that omits it
    assert tuples(steps) == [
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_5", "ev_snap"),
        ("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_6", "ev_link"),
    ]


def test_replica_copy_after_request_keeps_record_present():
    world = [
        obs("ev_sql", "2026-09-10T00:00:00Z", "account_service", "account_5", "USER_PROFILE", "PRESENT",
            emails=["kai@example.org"], users=["user_77"], extra={"copy_role": "failover_replica"}),
        obs("ev_api", "2026-09-12T00:00:00Z", "account_service", "account_5", "USER_PROFILE", "ABSENT", emails=["kai@example.org"]),
    ]
    status, steps, _ = run(world)
    assert status == "INCOMPLETE"
    assert tuples(steps)[0] == ("FINDING", "REPORT_REMAINING_RECORD", "account_service", "account_5", "ev_sql")


# ---------------------------------------------------------------------------------------------
# deletion-run reconstruction
# ---------------------------------------------------------------------------------------------

def _ev(aid, t, etype, target, rid):
    return ActivityEvent(aid, T(t), etype, "deletion_worker", target, rid, "SUCCESS", f"Policy action completed for {rid}.")


def test_run_cadence_links_identifier_free_followups():
    head = _ev("activity_10", "2026-09-02T11:00:00Z", "DELETE_RECORD", "account_service", "account_5")
    t0 = head.event_time
    first = t0 + timedelta(minutes=30, seconds=41)       # whole minutes + run phase
    events = [
        head,
        _ev("activity_11", fmt_ts(first), "ANONYMIZE_RECORD", "analytics_platform", "analytics_4"),
        _ev("activity_12", fmt_ts(first + timedelta(seconds=449)), "REDACT_RECORD", "support_platform", "support_3"),
        # a neighbouring run's row one second off the phase: not linked
        _ev("activity_13", fmt_ts(t0 + timedelta(minutes=50, seconds=42)), "ANONYMIZE_RECORD", "analytics_platform", "analytics_99"),
    ]
    world = [
        obs("ev_1", "2026-09-15T10:00:00Z", "support_platform", "support_3", "SUPPORT_MESSAGE", "REDACTED", users=["user_77"]),
        obs("ev_1", "2026-09-15T10:00:00Z", "analytics_platform", "analytics_4", "ANALYTICS_PROFILE", "ANONYMIZED"),
        obs("ev_1", "2026-09-15T10:00:00Z", "analytics_platform", "analytics_99", "ANALYTICS_PROFILE", "ANONYMIZED"),
        obs("ev_1", "2026-09-15T10:00:00Z", "account_service", "account_5", "USER_PROFILE", "ABSENT"),
    ]
    status, steps, notes = run(world, events=events)
    assert status == "COMPLETE"
    assert tuples(steps) == [
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_5", "ev_1"),
        ("VERIFICATION", "VERIFY_RECORD_REDACTED", "support_platform", "support_3", "ev_1"),
        ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "analytics_platform", "analytics_4", "ev_1"),
    ]
    assert any("via cadence" in n for n in notes)


def test_run_row_on_someone_elses_record_is_dropped():
    head = _ev("activity_10", "2026-09-02T11:00:00Z", "DELETE_RECORD", "account_service", "account_5")
    first = head.event_time + timedelta(minutes=30, seconds=41)
    events = [head,
              _ev("activity_11", fmt_ts(first), "DELETE_RECORD", "search_service", "search_1"),
              _ev("activity_12", fmt_ts(first + timedelta(seconds=390)), "REDACT_RECORD", "support_platform", "support_3")]
    world = [
        obs("ev_1", "2026-09-15T10:00:00Z", "support_platform", "support_3", "SUPPORT_MESSAGE", "REDACTED", users=["user_77"]),
        obs("ev_1", "2026-09-15T10:00:00Z", "search_service", "search_1", "SEARCH_INDEX_ENTRY", "PRESENT",
            emails=["other@example.org"], users=["user_12"]),
    ]
    status, steps, _ = run(world, events=events)
    assert status == "COMPLETE"
    assert "search_1" not in {s.resource_id for s in steps}


def test_ambiguous_run_window_makes_no_account_claim():
    events = [_ev("activity_1", "2026-09-02T11:00:00Z", "DELETE_RECORD", "account_service", "account_5"),
              _ev("activity_2", "2026-09-02T11:10:00Z", "DELETE_RECORD", "account_service", "account_6")]
    world = [obs("ev_1", "2026-09-15T10:00:00Z", "security_service", "audit_record_6", "SECURITY_AUDIT_LOG",
                 "PRESENT", users=["user_77"])]
    status, steps, notes = run(world, events=events)
    assert status == "COMPLETE"
    assert [s.resource_id for s in steps] == ["audit_record_6"]
    assert any("ambiguous" in n for n in notes)


def test_unknown_subject():
    assert planner.decide_unknown_subject(case()) == ("COMPLETE", [])


# ---------------------------------------------------------------------------------------------
# linkage vs state cutoff, derived account, deletion queues
# ---------------------------------------------------------------------------------------------

def test_linkage_may_use_a_later_file_but_state_and_citation_come_from_eligible_files():
    # the only file naming the person on audit_record_9 is delivered after audit_at; an older file
    # shows the record present but carries no identifier
    world = [
        obs("ev_old", "2026-09-10T09:00:00Z", "security_service", "audit_record_9", "SECURITY_AUDIT_LOG", "PRESENT"),
        obs("ev_new", "2026-09-20T09:00:00Z", "security_service", "audit_record_9", "SECURITY_AUDIT_LOG", "PRESENT",
            users=["user_77"]),
        obs("ev_old", "2026-09-10T09:00:00Z", "account_service", "account_5", "USER_PROFILE", "ABSENT",
            emails=["kai@example.org"]),
    ]
    status, steps, _ = run(world, c=case(audit="2026-09-15T12:00:00Z"))
    assert status == "COMPLETE"
    assert tuples(steps) == [
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_5", "ev_old"),
        ("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_9", "ev_old"),
    ]
    saved = state.LINKAGE_IGNORES_AUDIT_CUTOFF
    try:
        state.LINKAGE_IGNORES_AUDIT_CUTOFF = False
        _, strict_steps, _ = run(world, c=case(audit="2026-09-15T12:00:00Z"))
        assert [s.resource_id for s in strict_steps] == ["account_5"]
    finally:
        state.LINKAGE_IGNORES_AUDIT_CUTOFF = saved


def test_a_later_file_can_show_a_run_row_belongs_to_someone_else():
    head = _ev("activity_10", "2026-09-02T11:00:00Z", "DELETE_RECORD", "account_service", "account_5")
    first = head.event_time + timedelta(minutes=30, seconds=41)
    events = [head, _ev("activity_11", fmt_ts(first), "DELETE_RECORD", "search_service", "search_1")]
    world = [
        obs("ev_1", "2026-09-14T10:00:00Z", "search_service", "search_1", "SEARCH_INDEX_ENTRY", "ABSENT"),
        # delivered after audit_at: the document is another person's
        obs("ev_late", "2026-09-30T10:00:00Z", "search_service", "search_1", "SEARCH_INDEX_ENTRY", "PRESENT",
            emails=["other@example.org"], users=["user_12"]),
        obs("ev_1", "2026-09-14T10:00:00Z", "account_service", "account_5", "USER_PROFILE", "ABSENT"),
    ]
    status, steps, _ = run(world, events=events)
    assert status == "COMPLETE"
    assert "search_1" not in {s.resource_id for s in steps}


def test_derived_account_is_used_only_when_nothing_else_supplies_one():
    subj = Subject(user_id="user_07", emails={"pri@example.org"}, names={"Pri Raman", "pri raman"})
    c = case(audit="2026-09-15T12:00:00Z")
    cov = [Coverage("ev_snap", T("2026-09-14T12:00:00Z"), "account_service", "customer_profiles_primary",
                    {"account_900"}, origin="failover_capture")]
    world = [obs("ev_snap", "2026-09-14T12:00:00Z", "account_service", "account_900", "USER_PROFILE", "PRESENT",
                 emails=["someone@example.org"], extra={"status": {"account_status": "ACTIVE"}})]
    ext = ExtractionResult(observations=world, coverage=cov)
    act = ActivityIndex([])
    ctx = state.AuditContext.build(ext, act)
    status, steps, _ = planner.audit_case(c, subj, ext, act, RULES, ctx)
    assert status == "COMPLETE"
    assert tuples(steps) == [("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_007", "ev_snap")]
    # an account tied by evidence wins over the derived one
    world2 = world + [obs("ev_snap", "2026-09-14T12:00:00Z", "account_service", "account_4321", "USER_PROFILE",
                          "ABSENT", emails=["pri@example.org"])]
    ext2 = ExtractionResult(observations=world2, coverage=cov)
    ctx2 = state.AuditContext.build(ext2, act)
    _, steps2, _ = planner.audit_case(c, subj, ext2, act, RULES, ctx2)
    assert [s.resource_id for s in steps2] == ["account_4321"]


def test_a_deletion_queue_never_implies_absence():
    # the "inventory" origin only ever lists accounts that are pending deletion: it is a work queue,
    # so not being listed in it proves nothing; a real enumeration from another origin does
    cov = [Coverage("ev_queue", T("2026-09-14T12:00:00Z"), "account_service", "customer_profiles",
                    {"account_8"}, origin="inventory_capture"),
           Coverage("ev_enum", T("2026-09-12T12:00:00Z"), "account_service", "customer_profiles_primary",
                    {"account_8", "account_9"}, origin="failover_capture")]
    world = [
        obs("ev_queue", "2026-09-14T12:00:00Z", "account_service", "account_8", "USER_PROFILE", "PRESENT",
            emails=["x@example.org"], extra={"status": {"account_status": "DELETION_PENDING"}}),
        obs("ev_enum", "2026-09-12T12:00:00Z", "account_service", "account_8", "USER_PROFILE", "PRESENT",
            emails=["x@example.org"], extra={"status": {"account_status": "ACTIVE"}}),
        obs("ev_enum", "2026-09-12T12:00:00Z", "account_service", "account_9", "USER_PROFILE", "PRESENT",
            emails=["y@example.org"], extra={"status": {"account_status": "ACTIVE"}}),
    ]
    ev = [ActivityEvent("activity_1", T("2026-09-02T11:00:00Z"), "DELETE_RECORD", "deletion_worker",
                        "account_service", "account_5", "SUCCESS", "Deletion worker submitted account_5.")]
    status, steps, _ = run(world, coverage=cov, events=ev)
    assert status == "COMPLETE"
    assert tuples(steps) == [("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_5", "ev_enum")]
