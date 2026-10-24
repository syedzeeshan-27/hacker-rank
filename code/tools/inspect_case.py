"""Show what the engine can see for one case: subject identifiers, evidence observations at or before
audit_at that mention the subject, activity links, snapshot coverage, and (for sample cases) the
expected answer. Debugging / demo aid; not part of the prediction path.

    python code/tools/inspect_case.py case_26
    python code/tools/inspect_case.py case_11 --all-obs      # also list observations on linked resources
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost import identity, intent  # noqa: E402
from ghost.activity import ActivityIndex  # noqa: E402
from ghost.evidence import extract_all_detailed  # noqa: E402
from ghost.llm import LLMClient  # noqa: E402
from ghost.loaders import (  # noqa: E402
    load_activity, load_audit_log, load_cases, load_identities, load_manifest, load_sample_cases,
    load_sample_output,
)
from ghost.models import fmt_ts, resource_sort_key  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("case_id")
    p.add_argument("--all-obs", action="store_true")
    args = p.parse_args(argv)

    cases = {c.case_id: c for c in load_sample_cases() + load_cases()}
    case = cases[args.case_id]
    identities, audit_log = load_identities(), load_audit_log()
    subject = identity.resolve_subject(case.email, identities, audit_log, as_of=case.audit_at)
    llm = LLMClient(offline=True)
    intent_r = intent.classify_requests([case], llm)[case.case_id]
    extraction = extract_all_detailed(load_manifest(), llm, write_flags=False)
    act = ActivityIndex(load_activity())

    print(f"== {case.case_id}  email={case.email}  reported={case.reported_status}@{fmt_ts(case.reported_at)}  "
          f"audit_at={fmt_ts(case.audit_at)}")
    print(f"   intent: {'DELETION' if intent_r.is_deletion else 'NOT_DELETION'} ({intent_r.method}) {intent_r.reason}")
    if subject is None:
        print("   subject: NOT FOUND")
        return 0
    print(f"   subject: {subject.user_id} emails={sorted(subject.emails)} phones={sorted(subject.phones)} "
          f"hashes={[h[:10] + '..' for h in sorted(subject.email_hashes)]} names={sorted(subject.names)}")

    obs = [o for o in extraction.observations if o.captured_at <= case.audit_at]
    direct = [o for o in obs if o.mentions(subject)]
    linked = act.resources_linked_to(subject, as_of=case.audit_at)
    direct_res = {(o.system_id, o.resource_id) for o in direct if o.resource_id}
    all_res = direct_res | linked
    print(f"   observations <= audit_at: {len(obs)} total, {len(direct)} mention the subject; "
          f"activity-linked resources: {sorted(linked, key=lambda x: (x[0], resource_sort_key(x[1])))}")

    by_res: dict[tuple[str, str], list] = defaultdict(list)
    for o in obs:
        key = (o.system_id, o.resource_id)
        if o in direct or (args.all_obs and key in all_res) or (key in linked):
            by_res[key].append(o)
    for key in sorted(by_res, key=lambda x: (x[0], resource_sort_key(x[1]))):
        print(f"   - {key[0]} / {key[1]}")
        for o in sorted(by_res[key], key=lambda o: (o.captured_at, o.evidence_id)):
            ids = []
            if o.emails: ids.append("email")
            if o.phones: ids.append("phone")
            if o.email_hashes: ids.append("hash")
            if o.user_ids: ids.append("user:" + ",".join(sorted(o.user_ids)))
            if o.names: ids.append("name")
            tbl = o.extra.get("table") or o.extra.get("collection") or o.extra.get("copy_role") or ""
            print(f"       {fmt_ts(o.captured_at)} {o.evidence_id:<12} {o.state_hint:<10} {o.category:<20} "
                  f"ids={'+'.join(ids) or '-':<28} {tbl} status={o.extra.get('status', '')}")

    acts = act.observations_for(subject, as_of=case.audit_at, resources=[r for _, r in all_res])
    if acts:
        print("   activity observations:")
        for o in acts:
            print(f"       {fmt_ts(o.captured_at)} {o.evidence_id:<14} {o.state_hint:<10} {o.system_id}/{o.resource_id} "
                  f"{o.extra.get('event_type', '')} {o.extra.get('claim', '')}")

    cov = [c for c in extraction.coverage if c.captured_at <= case.audit_at and c.kind == "snapshot"]
    latest = {}
    for c in cov:
        k = (c.system_id, c.scope)
        if k not in latest or c.captured_at > latest[k].captured_at:
            latest[k] = c
    print("   latest snapshot coverage per system/scope (<= audit_at):")
    for k in sorted(latest):
        c = latest[k]
        hit = sorted(r for _, r in all_res if r in c.resource_ids)
        print(f"       {fmt_ts(c.captured_at)} {c.evidence_id:<12} {k[0]}/{k[1]} n={len(c.resource_ids)} subject_hits={hit}")

    truth = load_sample_output().get(case.case_id)
    if truth:
        print(f"   EXPECTED: {truth[0]}")
        for s in truth[1]:
            print(f"       {s.step_no:>2} {s.phase:<12} {s.operation:<26} {s.state:<10} {s.system_id}/{s.resource_id} ev={s.evidence_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
