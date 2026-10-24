"""Score an output.csv against a truth CSV, or check the output contract.

Examples (from the repo root):
  python code/evaluation/main.py
  python code/evaluation/main.py --predictions output_sample.csv --subset holdout
  python code/evaluation/main.py --case case_05
  python code/evaluation/main.py --markdown --json .cache/runs/metrics.json
  python code/evaluation/main.py --validate output.csv --requests dataset/deletion_requests.csv

Exit codes: 0 normally; 1 when --validate finds contract errors; 2 when an input file is missing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_CODE_DIR = Path(__file__).resolve().parents[1]
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from evaluation.scoring import (  # noqa: E402
    DATASET_DIR,
    REPO_ROOT,
    diff_case,
    format_markdown,
    format_text,
    is_error,
    load_evidence_index,
    load_holdout_ids,
    load_output_csv,
    score,
    split_subsets,
    validate_contract,
)


def _resolve(p: str) -> Path:
    """Relative paths resolve against the current directory first, then the repo root."""
    path = Path(p)
    if path.is_absolute() or path.exists():
        return path
    return REPO_ROOT / path


def _validate(pred_path: Path, requests_path: Path) -> int:
    if not requests_path.is_file():
        print(f"requests file not found: {requests_path}", file=sys.stderr)
        return 2
    problems = validate_contract(pred_path, requests_path, load_evidence_index(DATASET_DIR))
    errors = [p for p in problems if is_error(p)]
    warnings = [p for p in problems if not is_error(p)]
    for p in errors + warnings:
        print(p)
    print(f"contract check: {pred_path} vs {requests_path.name}: "
          f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score predictions against truth for Ghost Data Still Exists.")
    ap.add_argument("--predictions", default=".cache/runs/sample_output.csv")
    ap.add_argument("--truth", default="dataset/sample_output.csv")
    ap.add_argument("--subset", choices=("all", "dev", "holdout"), default="all")
    ap.add_argument("--case", help="print a side-by-side diff for one case_id")
    ap.add_argument("--json", dest="json_path", help="write metrics JSON here")
    ap.add_argument("--markdown", action="store_true", help="print a Markdown metrics table")
    ap.add_argument("--per-case", action="store_true", help="print the per-case table")
    ap.add_argument("--validate", help="contract check only for this output CSV")
    ap.add_argument("--requests", default="dataset/deletion_requests.csv",
                    help="requests CSV used by --validate")
    args = ap.parse_args(argv)

    if args.validate:
        return _validate(_resolve(args.validate), _resolve(args.requests))

    pred_path, truth_path = _resolve(args.predictions), _resolve(args.truth)
    for label, p in (("predictions", pred_path), ("truth", truth_path)):
        if not p.is_file():
            print(f"{label} file not found: {p}", file=sys.stderr)
            return 2
    pred = load_output_csv(pred_path)
    truth = load_output_csv(truth_path)

    if args.case:
        print(diff_case(pred, truth, args.case))
        return 0

    subsets = split_subsets(truth, load_holdout_ids())
    is_sample = bool(subsets["holdout"])  # truth contains the configured holdout cases
    if args.subset == "all" and is_sample:
        names = ["all", "dev", "holdout"]
    else:
        names = [args.subset]
    blocks = {name: score(pred, truth, subsets[name]) for name in names}

    print(f"predictions: {pred_path}")
    print(f"truth:       {truth_path}")
    for name in names:
        print()
        print(format_text(f"{name} ({len(subsets[name])} cases)", blocks[name], per_case=args.per_case))
    if args.markdown:
        print()
        print(format_markdown(blocks))
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"predictions": str(pred_path), "truth": str(truth_path), "subsets": blocks}
        with out.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"\nmetrics written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
