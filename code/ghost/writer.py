"""output.csv serialization. Deterministic: natural case order, sorted JSON keys, compact separators."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import CaseResult, case_sort_key

HEADER = ["case_id", "deletion_status", "steps_json"]


def steps_to_json(result: CaseResult) -> str:
    return json.dumps([s.to_json_dict() for s in result.steps], sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def write_output(path: Path, results: list[CaseResult]) -> None:
    ordered = sorted(results, key=lambda r: case_sort_key(r.case_id))
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(HEADER)
        for r in ordered:
            w.writerow([r.case_id, r.deletion_status, steps_to_json(r)])
