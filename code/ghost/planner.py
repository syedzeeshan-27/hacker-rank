"""WS-D: the decision engine. Entry point used by main.py: audit_case.

Contract
--------
audit_case(case, subject, extraction, activity_index, rules, context=None) -> (status, list[Step], notes)
    extraction      : ghost.evidence.ExtractionResult (observations + coverage for ALL files; the engine
                      drops anything delivered after case.audit_at itself)
    activity_index  : ghost.activity.ActivityIndex
    rules           : deletion_rules.csv rows
    context         : optional state.AuditContext (indexes + request pool). When omitted, a default one is
                      built once from the same extraction / activity index plus the dataset's request
                      queue (sample + evaluation requests) and cached.
    returns status in {"COMPLETE", "INCOMPLETE"}, the ordered steps, and free-text notes (why).
    Never returns NOT_APPLICABLE (main.py handles intent first).

Decision rules (plain English version in code/evaluation/notes/ws_d_engine.md)
-----------------------------------------------------------------------------
1. state.reconstruct gives every resource tied to the subject and its state at audit_at.
2. The subject's account record is ONE account: the account of the deletion run reconstructed for this
   request when the evidence or the run cadence ties it, else the identifier-linked account with the newest
   fact, else the run picked by timing / elimination, else the account id implied by a zero-padded user id
   (identity.derived_account_id). Other account ids (old lookups) are ignored.
3. policy.required_treatment maps each resource's category to DELETE / REDACT / ANONYMIZE / RETAIN /
   BLOCK_RESTORE / DISABLE through deletion_rules.csv.
4. Per resource, (treatment, state) gives findings + remediation + verification:
       DELETE     PRESENT|REDACTED|ANONYMIZED -> REPORT_REMAINING_RECORD, DELETE_RECORD, VERIFY_RECORD_ABSENT
                  ABSENT                      -> VERIFY_RECORD_ABSENT
       REDACT     PRESENT                     -> REPORT_EXPOSED_PII, REDACT_RECORD, VERIFY_RECORD_REDACTED
                  REDACTED|ANONYMIZED         -> VERIFY_RECORD_REDACTED
       ANONYMIZE  PRESENT|REDACTED            -> REPORT_EXPOSED_PII, ANONYMIZE_RECORD, VERIFY_RECORD_ANONYMIZED
                  ANONYMIZED                  -> VERIFY_RECORD_ANONYMIZED
       RETAIN     PRESENT|REDACTED|ANONYMIZED -> VERIFY_RECORD_PRESENT
       BLOCK_RESTORE ACTIVE -> REPORT_RESTORE_RISK, BLOCK_RESTORE, VERIFY_RESTORE_BLOCKED; BLOCKED -> VERIFY_RESTORE_BLOCKED
       DISABLE    ACTIVE -> REPORT_ACTIVE_DATA_FLOW, DISABLE_DATA_FLOW, VERIFY_DATA_FLOW_DISABLED; DISABLED -> VERIFY_DATA_FLOW_DISABLED
   A resource with no eligible state gives no step, except the account record (INCOMPLETE: future
   VERIFY_RECORD_ABSENT) and a retained record (INCOMPLETE: future VERIFY_RECORD_PRESENT): those future
   checks need no evidence.
   Non-account records that are already ABSENT are verified only when the deletion run processed them
   (the run row names them) or a finding / remediation touches them.
5. Status: INCOMPLETE when there is at least one finding, else COMPLETE. COMPLETE keeps only the
   verification steps whose state is proven by cited evidence (latest eligible file, activity id only
   when no file shows the fact). INCOMPLETE: findings cite evidence, remediation and verification use "".
6. Order: FINDING -> REMEDIATION -> VERIFICATION; inside a phase models.OPERATION_ORDER, then system_id,
   then resource_id (models.resource_sort_key); steps numbered from 1.

decide_unknown_subject(case) -> (status, steps)
    Deletion authorized but the email matches no identity: there is no record we can tie to the person,
    so there is nothing to remediate and nothing evidence-backed to verify. We return ("COMPLETE", [])
    and a note; it never triggers on the shipped data (all 518 + 32 emails resolve).
"""
from __future__ import annotations

from typing import Optional

from . import policy, state
from .activity import ActivityIndex
from .evidence import ExtractionResult
from .models import (
    OPERATION_ORDER,
    OPERATIONS,
    PHASES,
    Case,
    ResourceState,
    Rule,
    Step,
    Subject,
    norm_name,
    resource_sort_key,
)

RECORD_PRESENT_STATES = ("PRESENT", "REDACTED", "ANONYMIZED")

# (treatment, state) -> (finding op, remediation op, verification op); None = no step of that phase
_PLAN: dict[tuple[str, str], tuple[Optional[str], Optional[str], Optional[str]]] = {}
for _st in RECORD_PRESENT_STATES:
    _PLAN[("DELETE", _st)] = ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT")
    _PLAN[("RETAIN", _st)] = (None, None, "VERIFY_RECORD_PRESENT")
_PLAN[("DELETE", "ABSENT")] = (None, None, "VERIFY_RECORD_ABSENT")
_PLAN[("REDACT", "PRESENT")] = ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED")
_PLAN[("REDACT", "REDACTED")] = (None, None, "VERIFY_RECORD_REDACTED")
_PLAN[("REDACT", "ANONYMIZED")] = (None, None, "VERIFY_RECORD_REDACTED")
_PLAN[("ANONYMIZE", "PRESENT")] = ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED")
_PLAN[("ANONYMIZE", "REDACTED")] = ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED")
_PLAN[("ANONYMIZE", "ANONYMIZED")] = (None, None, "VERIFY_RECORD_ANONYMIZED")
_PLAN[("BLOCK_RESTORE", "ACTIVE")] = ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED")
_PLAN[("BLOCK_RESTORE", "BLOCKED")] = (None, None, "VERIFY_RESTORE_BLOCKED")
_PLAN[("DISABLE", "ACTIVE")] = ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED")
_PLAN[("DISABLE", "DISABLED")] = (None, None, "VERIFY_DATA_FLOW_DISABLED")

# ---------------------------------------------------------------------------------------------
# Default context (built once per extraction / activity index)
# ---------------------------------------------------------------------------------------------

_DEFAULT_CTX: dict[tuple[int, int], state.AuditContext] = {}


def default_context(extraction: ExtractionResult, activity_index: ActivityIndex) -> state.AuditContext:
    key = (id(extraction), id(activity_index))
    ctx = _DEFAULT_CTX.get(key)
    if ctx is not None:
        return ctx
    pool, ambiguous = _dataset_pool()
    ctx = state.AuditContext.build(extraction, activity_index, pool=pool, ambiguous_names=ambiguous)
    _DEFAULT_CTX.clear()
    _DEFAULT_CTX[key] = ctx
    return ctx


def _dataset_pool():
    """Request queue from the dataset: every sample + evaluation request whose text authorizes deletion
    (rule verdict, no LLM; uncertain texts count as deletion requests for run bookkeeping), with its
    resolved subject. Only request inputs are used, never answers."""
    from . import identity, intent
    from .loaders import load_audit_log, load_cases, load_identities, load_sample_cases

    try:
        identities, audit_log = load_identities(), load_audit_log()
        cases = load_sample_cases() + load_cases()
    except (OSError, KeyError):
        return [], frozenset()
    counts: dict[str, int] = {}
    for ident in identities:
        n = norm_name(ident.name)
        if n:
            counts[n] = counts.get(n, 0) + 1
    ambiguous = frozenset(n for n, k in counts.items() if k > 1)
    pool = []
    seen = set()
    for c in cases:
        if c.case_id in seen:
            continue
        seen.add(c.case_id)
        if intent.rule_verdict(c.request_text).label == "NOT_DELETION":
            continue
        subj = identity.resolve_subject(c.email, identities, audit_log, as_of=c.audit_at)
        if subj is not None:
            pool.append(state.PoolEntry(c, subj))
    return pool, ambiguous


# ---------------------------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------------------------

def _pick_account(states: dict[tuple[str, str], ResourceState], run: state.RunChoice) -> Optional[tuple[str, str]]:
    accounts = [k for k in states if k[0] == "account_service"]
    if not accounts:
        return None
    if run.account_id and ("account_service", run.account_id) in states and run.how.split("+")[0] in ("evidence", "cadence"):
        return ("account_service", run.account_id)
    identified = [k for k in accounts if any(r in ("identifier", "catalog")
                                             for r in states[k].extra.get("link_reasons", []))]
    if identified:
        def fact_key(k):
            st = states[k]
            known = st.state != "UNKNOWN"
            return (known, st.captured_at, resource_sort_key(k[1]))
        return max(identified, key=fact_key)
    if run.account_id and ("account_service", run.account_id) in states:
        return ("account_service", run.account_id)
    return sorted(accounts, key=lambda k: resource_sort_key(k[1]))[0]


def _sort_key(step_phase: str, op: str, system_id: str, resource_id: str) -> tuple:
    return (PHASES.index(step_phase), OPERATION_ORDER[op], system_id, resource_sort_key(resource_id))


def audit_case(case: Case, subject: Subject, extraction: ExtractionResult, activity_index: ActivityIndex,
               rules: list[Rule], context: Optional[state.AuditContext] = None
               ) -> tuple[str, list[Step], list[str]]:
    ctx = context or default_context(extraction, activity_index)
    def violation_states(category: str) -> frozenset:
        """States for which (policy treatment, state) yields a finding under the loaded rules."""
        treatment = policy.required_treatment(category, None, rules, as_of=case.audit_at)
        return frozenset(st for (tr, st), plan in _PLAN.items() if tr == treatment and plan[0])

    states, links, run = state.reconstruct_detailed(case, subject, extraction, activity_index, ctx,
                                                    violation_states_for=violation_states)
    notes: list[str] = []
    if run.head_id:
        notes.append(f"deletion run {run.head_id} ({run.account_id}) via {run.how}")
    elif run.candidates:
        notes.append(f"deletion run ambiguous: {len(run.candidates)} candidates, no account claimed from timing")

    account_key = _pick_account(states, run)
    run_resources = {r for (s, r), lk in links.items() if "run" in lk.reasons}

    findings: list[tuple] = []        # (op, system, resource, evidence)
    remediation: list[tuple] = []
    verification: list[tuple] = []    # (op, system, resource, evidence or "", proven: bool)
    for key in sorted(states, key=lambda k: (k[0], resource_sort_key(k[1]))):
        st = states[key]
        system_id, resource_id = key
        if system_id == "account_service" and key != account_key:
            continue
        st.extra["subject_user_ids"] = [subject.user_id]
        treatment = policy.required_treatment(st.category, st, rules, as_of=case.audit_at)
        if not treatment:
            notes.append(f"{resource_id}: no rule for category {st.category or '?'}")
            continue
        if st.state == "UNKNOWN":
            if key == account_key and treatment == "DELETE":
                verification.append(("VERIFY_RECORD_ABSENT", system_id, resource_id, "", False))
            elif treatment == "RETAIN":
                verification.append(("VERIFY_RECORD_PRESENT", system_id, resource_id, "", False))
            continue
        if treatment == "RETAIN" and st.state not in RECORD_PRESENT_STATES:
            # a retained record must stay available: always a (future) check, never deleted
            verification.append(("VERIFY_RECORD_PRESENT", system_id, resource_id, "", False))
            continue
        plan = _PLAN.get((treatment, st.state))
        if plan is None:
            notes.append(f"{resource_id}: {treatment} with state {st.state} needs no step")
            continue
        f_op, r_op, v_op = plan
        if f_op is None and treatment == "DELETE" and key != account_key and resource_id not in run_resources:
            # already-absent record that this request's deletion run never touched: not part of the audit
            continue
        if f_op:
            findings.append((f_op, system_id, resource_id, st.evidence_id))
        if r_op:
            remediation.append((r_op, system_id, resource_id, ""))
        if v_op:
            verification.append((v_op, system_id, resource_id, st.evidence_id, True))

    status = "INCOMPLETE" if findings else "COMPLETE"
    rows: list[tuple[str, str, str, str, str]] = []
    if status == "INCOMPLETE":
        for op, s, r, ev in findings:
            rows.append(("FINDING", op, s, r, ev))
        for op, s, r, _ev in remediation:
            rows.append(("REMEDIATION", op, s, r, ""))
        for op, s, r, _ev, _p in verification:
            rows.append(("VERIFICATION", op, s, r, ""))
    else:
        for op, s, r, ev, proven in verification:
            if proven and ev:
                rows.append(("VERIFICATION", op, s, r, ev))
            else:
                notes.append(f"{r}: no evidence-backed state for {op}; omitted from COMPLETE plan")
    # dedupe (same op on the same resource) and order
    uniq: dict[tuple, tuple] = {}
    for ph, op, s, r, ev in rows:
        uniq.setdefault((ph, op, s, r), (ph, op, s, r, ev))
    ordered = sorted(uniq.values(), key=lambda x: _sort_key(x[0], x[1], x[2], x[3]))
    steps = []
    for i, (ph, op, s, r, ev) in enumerate(ordered, start=1):
        assert OPERATIONS[op][0] == ph
        steps.append(Step(step_no=i, phase=ph, system_id=s, resource_id=r, operation=op,
                          state=OPERATIONS[op][1], evidence_id=ev))
    return status, steps, notes


def decide_unknown_subject(case: Case) -> tuple[str, list[Step]]:
    return "COMPLETE", []
