"""Custom evaluation cases for the decision engine (WS-D).

Small synthetic fixtures (fictional people, example.org / example.net addresses) under
code/evaluation/custom_cases_data/ are pushed through the same functions main.py uses:
evidence registry -> identity resolution -> request intent -> planner.audit_case. Each case has a
hand-written expected output; the runner compares status and the exact ordered step list.

    python code/evaluation/custom_cases.py        # prints PASS/FAIL per case, exit code 1 on any failure
    python -m pytest code/tests/test_custom_cases.py -q

Cases
    a_history_email      request uses an email that only the audit log knows (changed in March)
    b_tombstone_pii      account tombstone still carries name / email / phone -> REDACT, not DELETE
    c_order_flow_restore active connector + restorable backup + leftover search entry: flow and
                         restore are handled before the dependent delete
    d_replica_only       primary profile table is clean, the failover replica still holds the account
    e1_audit_before      search entry seen absent before audit_at; a later re-index file is ignored
    e2_audit_after       same person, later audit_at: the re-index file is now eligible -> INCOMPLETE
    f_marketing_only     "remove me from marketing lists only" -> NOT_APPLICABLE
    g_negated_deletion   "I am not asking for deletion" -> NOT_APPLICABLE
    h_export_link        closed account, old download-your-data export still live -> deletion, INCOMPLETE
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from ghost import identity, intent, planner, state  # noqa: E402
from ghost.activity import ActivityIndex  # noqa: E402
from ghost.evidence import registry  # noqa: E402
from ghost.llm import LLMClient  # noqa: E402
from ghost.loaders import load_rules  # noqa: E402
from ghost.models import AuditEvent, Case, EvidenceFile, Identity, parse_ts  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "custom_cases_data"

IDENTITIES = [
    Identity("user_901", "Ines Valcourt", "ines.valcourt@example.org", "+44 7700 900101", "a" * 64,
             parse_ts("2026-03-01T09:00:00Z")),
    Identity("user_902", "Tomas Brandt", "tomas.brandt@example.org", "+49 151 2345 9002", "b" * 64,
             parse_ts("2026-01-10T09:00:00Z")),
    Identity("user_903", "Asha Mwangi", "asha.mwangi@example.org", "+254 700 009 003", "c" * 64,
             parse_ts("2026-01-10T09:00:00Z")),
    Identity("user_904", "Lucia Ferro", "lucia.ferro@example.org", "+39 347 000 9004", "d" * 64,
             parse_ts("2026-01-10T09:00:00Z")),
    Identity("user_905", "Dev Raman", "dev.raman@example.org", "+91 98000 09005", "e" * 64,
             parse_ts("2026-01-10T09:00:00Z")),
    Identity("user_906", "Mara Okafor", "mara.okafor@example.org", "+234 800 000 9006", "f" * 64,
             parse_ts("2026-01-10T09:00:00Z")),
    Identity("user_907", "Pia Lund", "pia.lund@example.org", "+47 400 09 007", "1" * 64,
             parse_ts("2026-01-10T09:00:00Z")),
    Identity("user_908", "Omar Haddou", "omar.haddou@example.org", "+33 6 00 00 90 08", "2" * 64,
             parse_ts("2026-01-10T09:00:00Z")),
    Identity("user_9104", "Karl Ostrom", "karl.ostrom@example.org", "+46 70 000 9104", "3" * 64,
             parse_ts("2026-01-10T09:00:00Z")),
]

AUDIT_LOG = [
    AuditEvent("audit_9901", "user_901", parse_ts("2026-03-01T09:00:00Z"), "email",
               "ines.v.old@example.net", "ines.valcourt@example.org"),
]

MANIFEST = [
    EvidenceFile("custom_ev_a", "evidence/logs/lookup_trace_a.log", "fixture", parse_ts("2026-09-15T10:05:00Z")),
    EvidenceFile("custom_ev_b", "evidence/records/snapshot_bundle_b.json", "fixture", parse_ts("2026-09-15T12:00:00Z")),
    EvidenceFile("custom_ev_c", "evidence/logs/lookup_trace_c.log", "fixture", parse_ts("2026-09-15T11:04:00Z")),
    EvidenceFile("custom_ev_d", "evidence/databases/accounts_failover_d.sql", "fixture", parse_ts("2026-09-15T12:00:00Z")),
    EvidenceFile("custom_ev_e1", "evidence/logs/lookup_trace_e1.log", "fixture", parse_ts("2026-09-10T10:02:00Z")),
    EvidenceFile("custom_ev_e2", "evidence/logs/lookup_trace_e2.log", "fixture", parse_ts("2026-09-20T10:02:00Z")),
    EvidenceFile("custom_ev_h", "evidence/logs/lookup_trace_h.log", "fixture", parse_ts("2026-09-15T11:42:00Z")),
]

DELETE_TEXT = "Please permanently delete my account and the personal data attached to it."


def _case(cid, email, requested, reported, audit, text=DELETE_TEXT, status="COMPLETE") -> Case:
    return Case(cid, email, parse_ts(requested), status, parse_ts(reported), parse_ts(audit), text)


@dataclass
class CustomCase:
    case: Case
    expected_status: str
    expected_steps: list[tuple]        # (phase, operation, system_id, resource_id, evidence_id)
    purpose: str = ""
    expected_intent: bool = True
    extra: dict = field(default_factory=dict)


def F(op, sys_, res, ev):
    return ("FINDING", op, sys_, res, ev)


def R(op, sys_, res):
    return ("REMEDIATION", op, sys_, res, "")


def V(op, sys_, res, ev=""):
    return ("VERIFICATION", op, sys_, res, ev)


CASES = [
    CustomCase(
        _case("a_history_email", "ines.v.old@example.net", "2026-09-10T09:00:00Z", "2026-09-12T09:00:00Z",
              "2026-09-15T12:00:00Z"),
        "COMPLETE",
        [V("VERIFY_RECORD_ABSENT", "account_service", "account_9001", "custom_ev_a"),
         V("VERIFY_RECORD_PRESENT", "security_service", "audit_record_9001", "custom_ev_a")],
        purpose="identity resolved only through the audit log; evidence looked the account up by the old email",
    ),
    CustomCase(
        _case("b_tombstone_pii", "tomas.brandt@example.org", "2026-09-10T09:00:00Z", "2026-09-12T09:00:00Z",
              "2026-09-15T12:00:00Z"),
        "INCOMPLETE",
        [F("REPORT_EXPOSED_PII", "identity_service", "tombstone_9002", "custom_ev_b"),
         R("REDACT_RECORD", "identity_service", "tombstone_9002"),
         V("VERIFY_RECORD_ABSENT", "account_service", "account_9002"),
         V("VERIFY_RECORD_REDACTED", "identity_service", "tombstone_9002")],
        purpose="ACCOUNT_TOMBSTONE rule is REDACT: keep the user_id reference, strip name/email/phone",
    ),
    CustomCase(
        _case("c_order_flow_restore", "asha.mwangi@example.org", "2026-09-10T09:00:00Z", "2026-09-12T09:00:00Z",
              "2026-09-15T12:00:00Z"),
        "INCOMPLETE",
        [F("REPORT_REMAINING_RECORD", "search_service", "search_9003", "custom_ev_c"),
         F("REPORT_ACTIVE_DATA_FLOW", "integration_service", "data_flow_9003", "custom_ev_c"),
         F("REPORT_RESTORE_RISK", "backup_service", "backup_9003", "custom_ev_c"),
         R("DISABLE_DATA_FLOW", "integration_service", "data_flow_9003"),
         R("BLOCK_RESTORE", "backup_service", "backup_9003"),
         R("DELETE_RECORD", "search_service", "search_9003"),
         V("VERIFY_RECORD_ABSENT", "account_service", "account_9003"),
         V("VERIFY_RECORD_ABSENT", "search_service", "search_9003"),
         V("VERIFY_DATA_FLOW_DISABLED", "integration_service", "data_flow_9003"),
         V("VERIFY_RESTORE_BLOCKED", "backup_service", "backup_9003")],
        purpose="recreation paths are closed before the dependent record is deleted",
    ),
    CustomCase(
        _case("d_replica_only", "lucia.ferro@example.org", "2026-09-10T09:00:00Z", "2026-09-12T09:00:00Z",
              "2026-09-15T12:00:00Z"),
        "INCOMPLETE",
        [F("REPORT_REMAINING_RECORD", "account_service", "account_9004", "custom_ev_d"),
         R("DELETE_RECORD", "account_service", "account_9004"),
         V("VERIFY_RECORD_ABSENT", "account_service", "account_9004")],
        purpose="a copy surviving only in the failover replica is still a remaining record",
    ),
    CustomCase(
        _case("e1_audit_before", "dev.raman@example.org", "2026-09-05T09:00:00Z", "2026-09-07T09:00:00Z",
              "2026-09-12T12:00:00Z"),
        "COMPLETE",
        [V("VERIFY_RECORD_ABSENT", "account_service", "account_9005", "custom_ev_e1")],
        purpose="the 2026-09-20 re-index file is after audit_at and must not change the answer",
    ),
    CustomCase(
        _case("e2_audit_after", "dev.raman@example.org", "2026-09-05T09:00:00Z", "2026-09-07T09:00:00Z",
              "2026-09-21T12:00:00Z"),
        "INCOMPLETE",
        [F("REPORT_REMAINING_RECORD", "search_service", "search_9005", "custom_ev_e2"),
         R("DELETE_RECORD", "search_service", "search_9005"),
         V("VERIFY_RECORD_ABSENT", "account_service", "account_9005"),
         V("VERIFY_RECORD_ABSENT", "search_service", "search_9005")],
        purpose="same evidence, later audit_at: the re-indexed entry is now in scope",
    ),
    CustomCase(
        _case("f_marketing_only", "pia.lund@example.org", "2026-09-10T09:00:00Z", "2026-09-12T09:00:00Z",
              "2026-09-15T12:00:00Z",
              text="Please remove me from marketing lists only. I still want receipts and service messages.",
              status="NOT_STARTED"),
        "NOT_APPLICABLE", [], purpose="marketing opt-out is not a deletion request", expected_intent=False,
    ),
    CustomCase(
        _case("g_negated_deletion", "omar.haddou@example.org", "2026-09-10T09:00:00Z", "2026-09-12T09:00:00Z",
              "2026-09-15T12:00:00Z",
              text="I am not asking for deletion. Please correct the spelling of my surname on my profile.",
              status="NOT_STARTED"),
        "NOT_APPLICABLE", [], purpose="negated deletion vocabulary", expected_intent=False,
    ),
    CustomCase(
        _case("h_export_link", "mara.okafor@example.org", "2026-09-10T09:00:00Z", "2026-09-12T09:00:00Z",
              "2026-09-15T12:00:00Z",
              text=("I closed my account, but an old download-your-data link still works. "
                    "Please delete that export file as well.")),
        "INCOMPLETE",
        [F("REPORT_REMAINING_RECORD", "export_service", "export_9006", "custom_ev_h"),
         R("DELETE_RECORD", "export_service", "export_9006"),
         V("VERIFY_RECORD_ABSENT", "account_service", "account_9006"),
         V("VERIFY_RECORD_ABSENT", "export_service", "export_9006")],
        purpose="mentions export / download but asks for deletion of the export file",
    ),
]


def build_world():
    llm = LLMClient(offline=True)
    extraction = registry.extract_all_detailed(MANIFEST, llm, dataset_dir=DATA_DIR, write_flags=False)
    activity_index = ActivityIndex([])
    pool = []
    for cc in CASES:
        subj = identity.resolve_subject(cc.case.email, IDENTITIES, AUDIT_LOG, as_of=cc.case.audit_at)
        if subj is not None and intent.rule_verdict(cc.case.request_text).label != "NOT_DELETION":
            pool.append(state.PoolEntry(cc.case, subj))
    ctx = state.AuditContext.build(extraction, activity_index, pool=pool)
    return llm, extraction, activity_index, ctx


def run_case(cc: CustomCase, world=None) -> tuple[str, list[tuple], bool]:
    llm, extraction, activity_index, ctx = world or build_world()
    intent_result = intent.classify_requests([cc.case], llm)[cc.case.case_id]
    if not intent_result.is_deletion:
        return "NOT_APPLICABLE", [], intent_result.is_deletion
    subj = identity.resolve_subject(cc.case.email, IDENTITIES, AUDIT_LOG, as_of=cc.case.audit_at)
    if subj is None:
        status, steps = planner.decide_unknown_subject(cc.case)
    else:
        status, steps, _notes = planner.audit_case(cc.case, subj, extraction, activity_index, load_rules(), ctx)
    got = [(s.phase, s.operation, s.system_id, s.resource_id, s.evidence_id) for s in steps]
    return status, got, intent_result.is_deletion


def main() -> int:
    world = build_world()
    failures = 0
    for cc in CASES:
        status, got, is_del = run_case(cc, world)
        ok = status == cc.expected_status and got == cc.expected_steps and is_del == cc.expected_intent
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {cc.case.case_id:<22} {status:<15} steps={len(got):<3} {cc.purpose}")
        if not ok:
            print(f"      expected {cc.expected_status}: {cc.expected_steps}")
            print(f"      got      {status}: {got}")
    print(f"{len(CASES) - failures}/{len(CASES)} custom cases pass")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
