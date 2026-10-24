"""Ghost Data Still Exists - deletion-audit pipeline entry point.

Usage (from the repository root, any OS):
    python code/main.py                      # all 518 cases -> output.csv
    python code/main.py --cases sample       # 32 public samples -> .cache/runs/sample_output.csv
    python code/main.py --cases dev          # 24 development samples
    python code/main.py --cases holdout      # 8 held-out samples
    python code/main.py --cases all --limit 20
    python code/main.py --offline            # never call the LLM (cached extractions only)
    python code/main.py --clear-cache        # cold run: wipe .cache first

Pipeline (all deterministic except the two LLM extraction points, which are cached):
    load dataset -> extract evidence (files + activity + audit log) -> classify request intent
    -> per case: resolve subject -> reconstruct state at audit_at -> apply policy -> plan steps -> write csv
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost import activity as activity_mod  # noqa: E402
from ghost import identity, intent, planner  # noqa: E402
from ghost.config import CACHE_DIR, CONFIG, OUTPUT_PATH, REPO_ROOT  # noqa: E402
from ghost.evidence import registry  # noqa: E402
from ghost.llm import LLMClient, usage_summary  # noqa: E402
from ghost.loaders import (  # noqa: E402
    load_activity,
    load_audit_log,
    load_cases,
    load_identities,
    load_manifest,
    load_rules,
    load_sample_cases,
)
from ghost.models import CaseResult, case_sort_key  # noqa: E402
from ghost.writer import write_output  # noqa: E402


def select_cases(which: str, limit: int | None):
    if which == "all":
        cases = load_cases()
    else:
        samples = load_sample_cases()
        holdout = set(CONFIG.get("sample_holdout_cases", []))
        if which == "sample":
            cases = samples
        elif which == "dev":
            cases = [c for c in samples if c.case_id not in holdout]
        elif which == "holdout":
            cases = [c for c in samples if c.case_id in holdout]
        else:
            raise SystemExit(f"unknown --cases {which}")
    cases = sorted(cases, key=lambda c: case_sort_key(c.case_id))
    return cases[:limit] if limit else cases


def run(args: argparse.Namespace) -> Path:
    started = time.time()
    if args.clear_cache and CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    llm = LLMClient(run_id=run_id, offline=args.offline)

    cases = select_cases(args.cases, args.limit)
    identities = load_identities()
    audit_log = load_audit_log()
    activity_events = load_activity()
    rules = load_rules()
    manifest = load_manifest()
    print(f"[run {run_id}] cases={len(cases)} identities={len(identities)} audit={len(audit_log)} "
          f"activity={len(activity_events)} evidence_files={len(manifest)}")

    # 1. Evidence -> observations (files are read once; LLM only for images / unreadable PDFs, cached)
    extraction = registry.extract_all_detailed(manifest, llm)
    print(f"[evidence] observations={len(extraction.observations)} coverage={len(extraction.coverage)} "
          f"from {len(manifest)} files; skipped={len(extraction.skipped)} llm_files={len(extraction.llm_files)} "
          f"injection_flags={len(extraction.injection_flags)}; llm calls={llm.calls} cache hits={llm.cache_hits}")
    if extraction.skipped:
        print(f"[evidence] skipped (no LLM / parse error): {', '.join(extraction.skipped)}")
    activity_index = activity_mod.ActivityIndex(activity_events)

    # 2. Request intent (deterministic rules + cached LLM for the rest)
    intents = intent.classify_requests(cases, llm)

    # 3. Per-case decision
    results: list[CaseResult] = []
    for case in cases:
        intent_result = intents[case.case_id]
        if not intent_result.is_deletion:
            results.append(CaseResult(case.case_id, "NOT_APPLICABLE", [], notes=[intent_result.reason]))
            continue
        subject = identity.resolve_subject(case.email, identities, audit_log, as_of=case.audit_at)
        if subject is None:
            # Unknown person: nothing to audit, but the request still authorizes deletion.
            status, steps = planner.decide_unknown_subject(case)
            results.append(CaseResult(case.case_id, status, steps, notes=["subject not found"]))
            continue
        status, steps, notes = planner.audit_case(case, subject, extraction, activity_index, rules)
        results.append(CaseResult(case.case_id, status, steps, notes=notes))

    # 4. Write
    if args.out:
        out_path = Path(args.out)
    elif args.cases == "all" and not args.limit:
        out_path = OUTPUT_PATH
    else:
        out_path = CACHE_DIR / "runs" / f"{args.cases}_output.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_output(out_path, results)

    summary = usage_summary(run_id=run_id)
    meta = {
        "run_id": run_id,
        "cases": args.cases,
        "n_cases": len(results),
        "output": str(out_path),
        "seconds": round(time.time() - started, 1),
        "statuses": {s: sum(1 for r in results if r.deletion_status == s)
                     for s in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")},
        "usage": summary["total"],
    }
    (CACHE_DIR / "last_run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (CACHE_DIR / "runs").mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "runs" / f"run_{run_id}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    shown = out_path.relative_to(REPO_ROOT) if out_path.is_relative_to(REPO_ROOT) else out_path
    print(f"[done] {shown} rows={len(results)} statuses={meta['statuses']} "
          f"llm_calls={summary['total']['calls']} cost=${summary['total']['cost_usd']:.4f} in {meta['seconds']}s")
    return out_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases", default="all", choices=["all", "sample", "dev", "holdout"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", default=None, help="override output path")
    p.add_argument("--offline", action="store_true", help="never call the LLM; use cache only")
    p.add_argument("--clear-cache", action="store_true", help="delete .cache/ first (cold run)")
    args = p.parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
