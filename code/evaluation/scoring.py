"""Scoring and contract checks for output.csv files.

Everything here is driven by the files passed in at runtime (a predictions CSV, a truth CSV, the
requests CSV). Nothing embeds per-case expectations. Output is deterministic: case ids are walked
in natural order, dict keys are sorted, floats are plain Python floats.

Terminology used across the metrics:
  step tuple  (step_no, phase, system_id, resource_id, operation, state, evidence_id)
  step body   the same without step_no -> (phase, system_id, resource_id, operation, state, evidence_id)
  step key    (system_id, resource_id, operation) -> what a step is about, ignoring state/evidence
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Union

_CODE_DIR = Path(__file__).resolve().parents[1]
if str(_CODE_DIR) not in sys.path:  # allow `python code/evaluation/main.py` and plain imports
    sys.path.insert(0, str(_CODE_DIR))

from ghost.models import (  # noqa: E402
    OPERATION_ORDER,
    OPERATIONS,
    PHASES,
    STATUSES,
    Step,
    case_sort_key,
    parse_ts,
    resource_sort_key,
)

REPO_ROOT = _CODE_DIR.parent
DATASET_DIR = REPO_ROOT / "dataset"
CONFIG_PATH = _CODE_DIR / "config.json"

STEP_FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
BODY_FIELDS = STEP_FIELDS[1:]
OUTPUT_COLUMNS = ("case_id", "deletion_status", "steps_json")
EVIDENCE_ID_RE = re.compile(r"^(evidence|activity|audit)_\d{2,}$")
MISSING = "MISSING"  # pseudo-status for a case with no prediction row

Pred = dict[str, tuple[str, list[Step]]]
PathLike = Union[str, Path]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def _to_int(value) -> int:
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return -1


def _step_from_dict(d: dict) -> Step:
    return Step(
        step_no=_to_int(d.get("step_no")),
        phase=str(d.get("phase", "") or ""),
        system_id=str(d.get("system_id", "") or ""),
        resource_id=str(d.get("resource_id", "") or ""),
        operation=str(d.get("operation", "") or ""),
        state=str(d.get("state", "") or ""),
        evidence_id=str(d.get("evidence_id", "") or ""),
    )


def parse_steps_json(text: str) -> list[Step]:
    """Lenient parse used for scoring. Invalid JSON or non-list -> []. Non-dict items are skipped."""
    try:
        data = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [_step_from_dict(d) for d in data if isinstance(d, dict)]


def load_output_csv(path: PathLike) -> Pred:
    """case_id -> (deletion_status, steps). Missing file -> {}. First row wins on duplicate case_id."""
    p = Path(path)
    if not p.is_file():
        return {}
    _, rows = _read_csv(p)
    out: Pred = {}
    for r in rows:
        cid = (r.get("case_id") or "").strip()
        if not cid or cid in out:
            continue
        out[cid] = ((r.get("deletion_status") or "").strip(), parse_steps_json(r.get("steps_json") or ""))
    return out


def load_request_ids(requests_csv_path: PathLike) -> list[str]:
    _, rows = _read_csv(Path(requests_csv_path))
    return [r["case_id"].strip() for r in rows]


def load_request_audit_at(requests_csv_path: PathLike) -> dict[str, datetime]:
    _, rows = _read_csv(Path(requests_csv_path))
    out: dict[str, datetime] = {}
    for r in rows:
        try:
            out[r["case_id"].strip()] = parse_ts(r["audit_at"])
        except (KeyError, ValueError):
            continue
    return out


def load_evidence_index(dataset_dir: PathLike = DATASET_DIR) -> dict[str, datetime]:
    """Every citable id (file_id, activity_id, audit_id) -> the time it was captured / happened.

    Missing files are skipped, so this degrades to {} outside a full checkout.
    """
    d = Path(dataset_dir)
    index: dict[str, datetime] = {}
    for name, id_col, ts_col in (
        ("evidence_files.csv", "file_id", "captured_at"),
        ("system_activity.csv", "activity_id", "event_time"),
        ("user_audit_log.csv", "audit_id", "event_time"),
    ):
        p = d / name
        if not p.is_file():
            continue
        _, rows = _read_csv(p)
        for r in rows:
            try:
                index[r[id_col].strip()] = parse_ts(r[ts_col])
            except (KeyError, ValueError):
                continue
    return index


# ---------------------------------------------------------------------------
# Subsets
# ---------------------------------------------------------------------------

def load_holdout_ids(config_path: PathLike = CONFIG_PATH) -> list[str]:
    p = Path(config_path)
    if not p.is_file():
        return []
    with p.open("r", encoding="utf-8") as fh:
        return list(json.load(fh).get("sample_holdout_cases", []))


def split_subsets(case_ids: Iterable[str], holdout_ids: Optional[Iterable[str]] = None) -> dict[str, list[str]]:
    """{'all','dev','holdout'} -> naturally sorted case ids. holdout = listed ids present in case_ids."""
    ids = sorted(set(case_ids), key=case_sort_key)
    hold = set(load_holdout_ids() if holdout_ids is None else holdout_ids)
    return {
        "all": ids,
        "dev": [c for c in ids if c not in hold],
        "holdout": [c for c in ids if c in hold],
    }


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------

def _err(msg: str) -> str:
    return "ERROR: " + msg


def _warn(msg: str) -> str:
    return "WARNING: " + msg


def is_error(problem: str) -> bool:
    return problem.startswith("ERROR:")


def _within_phase_key(d: dict) -> tuple:
    return (
        OPERATION_ORDER.get(str(d.get("operation")), len(OPERATION_ORDER)),
        str(d.get("system_id", "")),
        resource_sort_key(str(d.get("resource_id", ""))),
    )


def _check_steps(cid: str, status: str, raw_steps: list, audit_at: Optional[datetime],
                 evidence_index: Optional[dict[str, datetime]]) -> list[str]:
    probs: list[str] = []
    if status == "NOT_APPLICABLE":
        if raw_steps:
            probs.append(_err(f"{cid}: NOT_APPLICABLE must have steps_json=[] (got {len(raw_steps)} steps)"))
        return probs
    if not raw_steps and status in ("COMPLETE", "INCOMPLETE"):
        probs.append(_warn(f"{cid}: {status} with no steps"))

    last_phase_idx = -1
    seen = Counter()
    for i, s in enumerate(raw_steps):
        where = f"{cid} step[{i}]"
        if not isinstance(s, dict):
            probs.append(_err(f"{where}: not a JSON object"))
            continue
        keys = set(s.keys())
        missing = [f for f in STEP_FIELDS if f not in keys]
        extra = sorted(keys - set(STEP_FIELDS))
        if missing:
            probs.append(_err(f"{where}: missing fields {missing}"))
        if extra:
            probs.append(_err(f"{where}: unexpected fields {extra}"))
        sn = s.get("step_no")
        if isinstance(sn, bool) or not isinstance(sn, int):
            probs.append(_err(f"{where}: step_no must be a JSON integer (got {sn!r})"))
        elif sn != i + 1:
            probs.append(_err(f"{where}: step_no {sn}, expected {i + 1} (consecutive from 1)"))
        for f in BODY_FIELDS:
            if f in s and not isinstance(s[f], str):
                probs.append(_err(f"{where}: {f} must be a string (got {s[f]!r})"))
        phase, op, state = s.get("phase"), s.get("operation"), s.get("state")
        ev = s.get("evidence_id") if isinstance(s.get("evidence_id"), str) else ""

        if phase not in PHASES:
            probs.append(_err(f"{where}: unknown phase {phase!r}"))
        else:
            idx = PHASES.index(phase)
            if idx < last_phase_idx:
                probs.append(_err(f"{where}: phase {phase} after {PHASES[last_phase_idx]} "
                                  "(order must be FINDING, REMEDIATION, VERIFICATION)"))
            last_phase_idx = max(last_phase_idx, idx)
        if op not in OPERATIONS:
            probs.append(_err(f"{where}: unknown operation {op!r}"))
        else:
            want_phase, want_state = OPERATIONS[op]
            if state != want_state:
                probs.append(_err(f"{where}: {op} requires state {want_state}, got {state!r}"))
            if phase in PHASES and phase != want_phase:
                probs.append(_err(f"{where}: {op} belongs to phase {want_phase}, got {phase}"))
        if not s.get("system_id"):
            probs.append(_err(f"{where}: empty system_id"))
        if not s.get("resource_id"):
            probs.append(_err(f"{where}: empty resource_id"))

        # evidence rules by status
        if status == "COMPLETE":
            if phase != "VERIFICATION":
                probs.append(_err(f"{where}: COMPLETE may contain only VERIFICATION steps (got {phase})"))
            if not ev:
                probs.append(_err(f"{where}: COMPLETE verification step needs a non-empty evidence_id"))
        elif status == "INCOMPLETE":
            if phase == "FINDING" and not ev:
                probs.append(_err(f"{where}: INCOMPLETE finding needs a non-empty evidence_id"))
            if phase in ("REMEDIATION", "VERIFICATION") and ev:
                probs.append(_err(f"{where}: INCOMPLETE {phase} step must have evidence_id='' (got {ev})"))
        if ev:
            if not EVIDENCE_ID_RE.match(ev):
                probs.append(_err(f"{where}: evidence_id {ev!r} is not file_id evidence_NN / activity_NN / audit_NN"))
            elif evidence_index is not None and evidence_index:
                when = evidence_index.get(ev)
                if when is None:
                    probs.append(_warn(f"{where}: evidence_id {ev} not found in the dataset"))
                elif audit_at is not None and when > audit_at:
                    probs.append(_warn(f"{where}: evidence_id {ev} is dated after audit_at "
                                       f"({when:%Y-%m-%dT%H:%M:%SZ} > {audit_at:%Y-%m-%dT%H:%M:%SZ})"))
        seen[tuple(str(s.get(f, "")) for f in BODY_FIELDS)] += 1

    for body, n in sorted(seen.items()):
        if n > 1:
            probs.append(_warn(f"{cid}: duplicate step {body[1]}/{body[2]} {body[3]} x{n}"))

    # within-phase order: operation table order, then system_id, then resource_id (natural)
    for ph in PHASES:
        items = [s for s in raw_steps if isinstance(s, dict) and s.get("phase") == ph]
        keys = [_within_phase_key(s) for s in items]
        if keys != sorted(keys):
            probs.append(_warn(f"{cid}: {ph} steps not in operation/system_id/resource_id order"))
    return probs


def _raw_rows_from_path(path: Path) -> tuple[list[str], list[dict], list[str]]:
    if not path.is_file():
        return [], [], [_err(f"predictions file not found: {path}")]
    header, rows = _read_csv(path)
    probs = []
    if tuple(header) != OUTPUT_COLUMNS:
        probs.append(_err(f"header must be exactly {','.join(OUTPUT_COLUMNS)} (got {','.join(header)})"))
    return header, rows, probs


def _raw_rows_from_pred(pred: Pred) -> list[dict]:
    rows = []
    for cid, (status, steps) in pred.items():
        rows.append({
            "case_id": cid,
            "deletion_status": status,
            "steps_json": json.dumps([s.to_json_dict() for s in steps]),
        })
    return rows


def validate_contract(pred: Union[Pred, PathLike], requests_csv_path: PathLike,
                      evidence_index: Optional[dict[str, datetime]] = None) -> list[str]:
    """Return problems, each prefixed 'ERROR: ' or 'WARNING: '. Empty list = clean.

    pred may be a path to an output CSV (checks header, duplicates, raw JSON) or an already-loaded
    dict (row order = insertion order; duplicate rows can no longer be seen).
    evidence_index (see load_evidence_index) enables 'unknown id' and 'dated after audit_at' warnings.
    """
    probs: list[str] = []
    if isinstance(pred, (str, Path)):
        _, rows, head_probs = _raw_rows_from_path(Path(pred))
        probs.extend(head_probs)
        if any("not found" in p for p in head_probs):
            return probs
    else:
        rows = _raw_rows_from_pred(pred)

    req_ids = load_request_ids(requests_csv_path)
    audit_at = load_request_audit_at(requests_csv_path)
    req_set = set(req_ids)

    ids = [(r.get("case_id") or "").strip() for r in rows]
    counts = Counter(ids)
    for cid, n in sorted(counts.items(), key=lambda kv: case_sort_key(kv[0])):
        if n > 1:
            probs.append(_err(f"{cid}: {n} rows (exactly one required)"))
    missing = sorted(req_set - set(ids), key=case_sort_key)
    extra = sorted(set(ids) - req_set, key=case_sort_key)
    if missing:
        probs.append(_err(f"{len(missing)} case_ids from the requests file have no row: "
                          f"{', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}"))
    if extra:
        probs.append(_err(f"{len(extra)} rows have case_ids not in the requests file: "
                          f"{', '.join(extra[:10])}{' ...' if len(extra) > 10 else ''}"))
    if len(rows) != len(req_ids):
        probs.append(_err(f"{len(rows)} data rows, requests file has {len(req_ids)}"))

    natural = ids == sorted(ids, key=case_sort_key)
    lexical = ids == sorted(ids)
    if not natural and lexical:
        probs.append(_warn("rows are in plain lexical order (case_100 < case_33), not natural order; "
                           "the requests file itself is in natural order"))
    elif not natural and not lexical:
        probs.append(_err("rows are not sorted by case_id (neither natural nor lexical order)"))

    for r in rows:
        cid = (r.get("case_id") or "").strip()
        status = (r.get("deletion_status") or "").strip()
        if status not in STATUSES:
            probs.append(_err(f"{cid}: deletion_status {status!r} not in {STATUSES}"))
        try:
            raw = json.loads(r.get("steps_json") or "")
        except (TypeError, ValueError) as exc:
            probs.append(_err(f"{cid}: steps_json is not valid JSON ({exc})"))
            continue
        if not isinstance(raw, list):
            probs.append(_err(f"{cid}: steps_json must be a JSON array"))
            continue
        probs.extend(_check_steps(cid, status, raw, audit_at.get(cid), evidence_index))
    return probs


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def step_tuple(s: Step) -> tuple:
    return (s.step_no, s.phase, s.system_id, s.resource_id, s.operation, s.state, s.evidence_id)


def step_body(s: Step) -> tuple:
    return (s.phase, s.system_id, s.resource_id, s.operation, s.state, s.evidence_id)


def step_key(s: Step) -> tuple:
    return (s.system_id, s.resource_id, s.operation)


def _prf(tp: int, n_pred: int, n_truth: int) -> tuple[float, float, float]:
    """Precision, recall, F1. Both sides empty counts as a perfect 1.0."""
    if n_pred == 0 and n_truth == 0:
        return 1.0, 1.0, 1.0
    p = tp / n_pred if n_pred else 0.0
    r = tp / n_truth if n_truth else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def lcs_len(a: list, b: list) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def lcs_ratio(a: list, b: list) -> float:
    if not a and not b:
        return 1.0
    return lcs_len(a, b) / max(len(a), len(b))


def _align_by_key(pred_steps: list[Step], truth_steps: list[Step]) -> list[tuple[Step, Step]]:
    """Pair truth and predicted steps with the same (system_id, resource_id, operation).

    Repeated keys are paired in order of appearance.
    """
    pool: dict[tuple, list[Step]] = {}
    for s in pred_steps:
        pool.setdefault(step_key(s), []).append(s)
    pairs = []
    for t in truth_steps:
        bucket = pool.get(step_key(t))
        if bucket:
            pairs.append((bucket.pop(0), t))
    return pairs


def _r(x: float) -> float:
    return round(float(x), 6)


def score(pred: Pred, truth: Pred, case_ids: Optional[Iterable[str]] = None) -> dict:
    """Compare predictions to truth over case_ids (default: every truth case). See notes/ws_a_scorer.md."""
    ids = sorted(set(truth) if case_ids is None else set(case_ids), key=case_sort_key)
    ids = [c for c in ids if c in truth]

    n = len(ids)
    status_ok = exact = seq_ok = 0
    tp_all = np_all = nt_all = 0
    f1_sum = lcs_sum = 0.0
    aligned = state_ok = ev_ok = 0
    pos_ok = Counter()
    n_truth_steps = n_pred_steps = 0
    confusion: dict[str, dict[str, int]] = {}
    rows = []
    missing_preds = 0

    for cid in ids:
        t_status, t_steps = truth[cid]
        if cid in pred:
            p_status, p_steps = pred[cid]
        else:
            p_status, p_steps = MISSING, []
            missing_preds += 1

        confusion.setdefault(t_status, {}).setdefault(p_status, 0)
        confusion[t_status][p_status] += 1
        s_ok = p_status == t_status
        status_ok += s_ok

        t_tup = [step_tuple(s) for s in t_steps]
        p_tup = [step_tuple(s) for s in p_steps]
        same_seq = t_tup == p_tup
        seq_ok += same_seq
        is_exact = s_ok and same_seq
        exact += is_exact

        t_body = [step_body(s) for s in t_steps]
        p_body = [step_body(s) for s in p_steps]
        tp = sum((Counter(t_body) & Counter(p_body)).values())
        tp_all += tp
        np_all += len(p_body)
        nt_all += len(t_body)
        _, _, f1 = _prf(tp, len(p_body), len(t_body))
        f1_sum += f1
        lcs = lcs_ratio(p_body, t_body)
        lcs_sum += lcs

        for ps, ts in _align_by_key(p_steps, t_steps):
            aligned += 1
            state_ok += ps.state == ts.state
            ev_ok += ps.evidence_id == ts.evidence_id

        n_truth_steps += len(t_steps)
        n_pred_steps += len(p_steps)
        for i, tb in enumerate(t_body):
            if i < len(p_body):
                for f, tv, pv in zip(BODY_FIELDS, tb, p_body[i]):
                    pos_ok[f] += tv == pv

        rows.append({
            "case_id": cid,
            "truth_status": t_status,
            "pred_status": p_status,
            "n_truth": len(t_steps),
            "n_pred": len(p_steps),
            "f1": _r(f1),
            "exact": bool(is_exact),
            "lcs": _r(lcs),
        })

    def frac(a: int, b: int, empty: float = 1.0) -> float:
        return _r(a / b) if b else empty

    p_all, r_all, f_all = _prf(tp_all, np_all, nt_all)
    pos_total = sum(pos_ok.values())
    extra_preds = sorted((set(pred) - set(truth)), key=case_sort_key) if case_ids is None else []

    return {
        "n_cases": n,
        "missing_predictions": missing_preds,
        "extra_predictions": len(extra_preds),
        "status_accuracy": frac(status_ok, n, 0.0),
        "exact_case_match": frac(exact, n, 0.0),
        "step": {
            "precision": _r(p_all),
            "recall": _r(r_all),
            "f1": _r(f_all),
            "mean_case_f1": _r(f1_sum / n) if n else 0.0,
            "true_positives": tp_all,
            "n_pred_steps": np_all,
            "n_truth_steps": nt_all,
        },
        "field": {
            "aligned_steps": aligned,
            "key_recall": frac(aligned, n_truth_steps),
            "key_precision": frac(aligned, n_pred_steps),
            "state_accuracy_aligned": frac(state_ok, aligned),
            "evidence_accuracy_aligned": frac(ev_ok, aligned),
            "positional_accuracy": frac(pos_total, 6 * n_truth_steps),
            "positional_by_field": {f: frac(pos_ok[f], n_truth_steps) for f in BODY_FIELDS},
        },
        "sequence": {
            "exact_sequence_accuracy": frac(seq_ok, n, 0.0),
            "mean_lcs_ratio": _r(lcs_sum / n) if n else 0.0,
        },
        "confusion": {t: dict(sorted(v.items())) for t, v in sorted(confusion.items())},
        "per_case": rows,
    }


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def _fmt_step(s: Optional[Step]) -> str:
    if s is None:
        return ""
    ev = s.evidence_id or "-"
    return f"{s.step_no:>2} {s.phase[:5]} {s.system_id}/{s.resource_id} {s.operation} {s.state} [{ev}]"


def _lcs_pairs(a: list, b: list) -> list[tuple[int, int]]:
    """Index pairs (i, j) of one longest common subsequence, deterministic (prefers earliest)."""
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def diff_case(pred: Pred, truth: Pred, case_id: str) -> str:
    """Side-by-side view of truth vs predicted steps for one case.

    Markers: '=' same step at the aligned position, '~' step exists on both sides but out of order,
    '*' same system/resource/operation with a different state or evidence_id,
    '-' missing from the prediction, '+' extra in the prediction.
    """
    t_status, t_steps = truth.get(case_id, (MISSING, []))
    p_status, p_steps = pred.get(case_id, (MISSING, []))
    t_body = [step_body(s) for s in t_steps]
    p_body = [step_body(s) for s in p_steps]
    pairs = _lcs_pairs(t_body, p_body)
    matched_t = {i for i, _ in pairs}
    matched_p = {j for _, j in pairs}
    left_t = Counter(t_body[i] for i in range(len(t_body)) if i not in matched_t)
    left_p = Counter(p_body[j] for j in range(len(p_body)) if j not in matched_p)
    misordered = left_t & left_p

    rows: list[tuple[str, Optional[Step], Optional[Step]]] = []

    def flush_gap(ti: list[int], pj: list[int]) -> None:
        # pair leftovers with the same key first so state/evidence changes read on one line
        pj = list(pj)
        for i in ti:
            ts = t_steps[i]
            if t_body[i] in misordered:
                rows.append(("~", ts, None))
                continue
            k = next((j for j in pj if step_key(p_steps[j]) == step_key(ts) and p_body[j] not in misordered), None)
            if k is not None:
                pj.remove(k)
                rows.append(("*", ts, p_steps[k]))
            else:
                rows.append(("-", ts, None))
        for j in pj:
            rows.append(("~" if p_body[j] in misordered else "+", None, p_steps[j]))

    ti_prev, pj_prev = 0, 0
    for i, j in pairs + [(len(t_body), len(p_body))]:
        flush_gap(list(range(ti_prev, i)), list(range(pj_prev, j)))
        if i < len(t_body) and j < len(p_body):
            rows.append(("=", t_steps[i], p_steps[j]))
        ti_prev, pj_prev = i + 1, j + 1

    tp = sum((Counter(t_body) & Counter(p_body)).values())
    _, _, f1 = _prf(tp, len(p_body), len(t_body))
    width = max([len("truth")] + [len(_fmt_step(t)) for _, t, _ in rows])
    out = [
        f"{case_id}: truth={t_status} pred={p_status} "
        f"status {'OK' if t_status == p_status else 'MISMATCH'}",
        f"steps truth={len(t_steps)} pred={len(p_steps)} f1={f1:.3f} "
        f"lcs={lcs_ratio(p_body, t_body):.3f} exact={'yes' if t_status == p_status and [step_tuple(s) for s in t_steps] == [step_tuple(s) for s in p_steps] else 'no'}",
        "",
        f"  {'truth'.ljust(width)} | pred",
        f"  {'-' * width}-+-{'-' * max(4, width)}",
    ]
    for mark, ts, ps in rows:
        out.append(f"{mark} {_fmt_step(ts).ljust(width)} | {_fmt_step(ps)}")
    if not rows:
        out.append("  (no steps on either side)")
    out.append("")
    out.append("legend: = match, ~ misordered, * same target but state/evidence differs, - missing, + extra")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

HEADLINE_METRICS = (
    ("Cases", lambda m: str(m["n_cases"])),
    ("Status accuracy", lambda m: f"{m['status_accuracy']:.3f}"),
    ("Exact case match (status + all steps)", lambda m: f"{m['exact_case_match']:.3f}"),
    ("Step precision", lambda m: f"{m['step']['precision']:.3f}"),
    ("Step recall", lambda m: f"{m['step']['recall']:.3f}"),
    ("Step F1 (pooled)", lambda m: f"{m['step']['f1']:.3f}"),
    ("Step F1 (mean per case)", lambda m: f"{m['step']['mean_case_f1']:.3f}"),
    ("Target recall (system/resource/operation)", lambda m: f"{m['field']['key_recall']:.3f}"),
    ("State accuracy on matched targets", lambda m: f"{m['field']['state_accuracy_aligned']:.3f}"),
    ("Evidence-id accuracy on matched targets", lambda m: f"{m['field']['evidence_accuracy_aligned']:.3f}"),
    ("Positional field accuracy", lambda m: f"{m['field']['positional_accuracy']:.3f}"),
    ("Exact sequence accuracy", lambda m: f"{m['sequence']['exact_sequence_accuracy']:.3f}"),
    ("Mean LCS ratio", lambda m: f"{m['sequence']['mean_lcs_ratio']:.3f}"),
)


def format_text(name: str, m: dict, per_case: bool = False) -> str:
    lines = [f"== {name} =="]
    for label, fn in HEADLINE_METRICS:
        lines.append(f"  {label:<44} {fn(m)}")
    if m["missing_predictions"]:
        lines.append(f"  {'Cases with no prediction row':<44} {m['missing_predictions']}")
    pf = m["field"]["positional_by_field"]
    lines.append("  positional by field: " + ", ".join(f"{f}={pf[f]:.3f}" for f in BODY_FIELDS))
    lines.append("  status confusion (truth -> pred: count):")
    for t, row in m["confusion"].items():
        lines.append(f"    {t:<15} " + ", ".join(f"{p}={c}" for p, c in row.items()))
    misses = [r["case_id"] for r in m["per_case"] if not r["exact"]]
    lines.append(f"  not exact ({len(misses)}): {', '.join(misses) if misses else '-'}")
    if per_case:
        lines.append(f"  {'case':<10} {'truth':<15} {'pred':<15} {'n_t':>4} {'n_p':>4} {'f1':>6} {'lcs':>6} exact")
        for r in m["per_case"]:
            lines.append(f"  {r['case_id']:<10} {r['truth_status']:<15} {r['pred_status']:<15} "
                         f"{r['n_truth']:>4} {r['n_pred']:>4} {r['f1']:>6.3f} {r['lcs']:>6.3f} "
                         f"{'yes' if r['exact'] else 'no'}")
    return "\n".join(lines)


def format_markdown(blocks: dict[str, dict]) -> str:
    names = list(blocks)
    out = ["| Metric | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]
    for label, fn in HEADLINE_METRICS:
        out.append(f"| {label} | " + " | ".join(fn(blocks[n]) for n in names) + " |")
    for f in BODY_FIELDS:
        out.append(f"| Positional accuracy: {f} | "
                   + " | ".join(f"{blocks[n]['field']['positional_by_field'][f]:.3f}" for n in names) + " |")
    first = blocks[names[0]]
    statuses = sorted({p for row in first["confusion"].values() for p in row} | set(first["confusion"]))
    out.append("")
    out.append(f"Status confusion ({names[0]}; rows = truth, columns = prediction):")
    out.append("")
    out.append("| truth \\ pred | " + " | ".join(statuses) + " |")
    out.append("|---|" + "---:|" * len(statuses))
    for t in sorted(first["confusion"]):
        row = first["confusion"][t]
        out.append(f"| {t} | " + " | ".join(str(row.get(p, 0)) for p in statuses) + " |")
    return "\n".join(out)
