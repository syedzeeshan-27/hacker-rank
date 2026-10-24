"""Tests for code/evaluation/scoring.py on small synthetic data (no dataset files needed)."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from evaluation import scoring  # noqa: E402
from ghost.models import Step  # noqa: E402


def st(no, phase, system, resource, op, state, ev=""):
    return Step(no, phase, system, resource, op, state, ev)


F1 = st(1, "FINDING", "account_service", "account_001", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_01")
F2 = st(2, "FINDING", "search_service", "search_002", "REPORT_REMAINING_RECORD", "PRESENT", "activity_10")
R1 = st(3, "REMEDIATION", "account_service", "account_001", "DELETE_RECORD", "ABSENT")
R2 = st(4, "REMEDIATION", "search_service", "search_002", "DELETE_RECORD", "ABSENT")
V1 = st(5, "VERIFICATION", "account_service", "account_001", "VERIFY_RECORD_ABSENT", "ABSENT")
V2 = st(6, "VERIFICATION", "search_service", "search_002", "VERIFY_RECORD_ABSENT", "ABSENT")
INC = [F1, F2, R1, R2, V1, V2]
COMP = [st(1, "VERIFICATION", "account_service", "account_009", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_05")]

TRUTH = {
    "case_01": ("INCOMPLETE", INC),
    "case_02": ("COMPLETE", COMP),
    "case_03": ("NOT_APPLICABLE", []),
}


def renumber(steps):
    return [Step(i + 1, *scoring.step_body(s)) for i, s in enumerate(steps)]


def test_perfect_match():
    m = scoring.score(TRUTH, TRUTH)
    assert m["n_cases"] == 3
    assert m["status_accuracy"] == 1.0
    assert m["exact_case_match"] == 1.0
    assert m["step"]["precision"] == m["step"]["recall"] == m["step"]["f1"] == 1.0
    assert m["step"]["mean_case_f1"] == 1.0
    assert m["field"]["state_accuracy_aligned"] == 1.0
    assert m["field"]["evidence_accuracy_aligned"] == 1.0
    assert m["field"]["positional_accuracy"] == 1.0
    assert m["sequence"]["exact_sequence_accuracy"] == 1.0
    assert m["sequence"]["mean_lcs_ratio"] == 1.0
    assert m["confusion"] == {"COMPLETE": {"COMPLETE": 1}, "INCOMPLETE": {"INCOMPLETE": 1},
                              "NOT_APPLICABLE": {"NOT_APPLICABLE": 1}}


def test_wrong_status_only():
    pred = dict(TRUTH, case_02=("INCOMPLETE", COMP))
    m = scoring.score(pred, TRUTH)
    assert m["status_accuracy"] == pytest.approx(2 / 3, abs=1e-6)
    assert m["exact_case_match"] == pytest.approx(2 / 3, abs=1e-6)
    assert m["step"]["f1"] == 1.0  # steps untouched
    assert m["sequence"]["exact_sequence_accuracy"] == 1.0
    assert m["confusion"]["COMPLETE"] == {"INCOMPLETE": 1}


def test_missing_step():
    pred = dict(TRUTH, case_01=("INCOMPLETE", renumber([F1, F2, R1, R2, V1])))  # V2 dropped
    m = scoring.score(pred, TRUTH)
    # pooled over all truth steps: 7 truth, 6 pred, 6 tp
    assert m["step"]["true_positives"] == 6
    assert m["step"]["precision"] == 1.0
    assert m["step"]["recall"] == pytest.approx(6 / 7, abs=1e-6)
    row = next(r for r in m["per_case"] if r["case_id"] == "case_01")
    assert row["f1"] == pytest.approx(2 * 1 * (5 / 6) / (1 + 5 / 6), abs=1e-6)
    assert row["lcs"] == pytest.approx(5 / 6, abs=1e-6)
    assert not row["exact"]
    assert m["field"]["key_recall"] == pytest.approx(6 / 7, abs=1e-6)
    lines = scoring.diff_case(pred, TRUTH, "case_01").splitlines()
    assert any(line.startswith("-") and "VERIFY_RECORD_ABSENT" in line and "search_002" in line for line in lines)


def test_extra_step():
    extra = st(0, "FINDING", "cache_service", "cache_7", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_09")
    pred = dict(TRUTH, case_01=("INCOMPLETE", renumber([F1, F2, extra, R1, R2, V1, V2])))
    m = scoring.score(pred, TRUTH)
    assert m["step"]["recall"] == 1.0
    assert m["step"]["precision"] == pytest.approx(7 / 8, abs=1e-6)
    assert m["field"]["key_precision"] == pytest.approx(7 / 8, abs=1e-6)
    row = next(r for r in m["per_case"] if r["case_id"] == "case_01")
    assert row["lcs"] == pytest.approx(6 / 7, abs=1e-6)
    lines = scoring.diff_case(pred, TRUTH, "case_01").splitlines()
    assert any(line.startswith("+") and "cache_7" in line for line in lines)


def test_swapped_order():
    pred = dict(TRUTH, case_01=("INCOMPLETE", renumber([F1, F2, R2, R1, V1, V2])))
    m = scoring.score(pred, TRUTH)
    assert m["step"]["f1"] == 1.0  # same multiset
    assert m["sequence"]["exact_sequence_accuracy"] == pytest.approx(2 / 3, abs=1e-6)
    row = next(r for r in m["per_case"] if r["case_id"] == "case_01")
    assert row["lcs"] == pytest.approx(5 / 6, abs=1e-6)
    assert m["field"]["state_accuracy_aligned"] == 1.0  # alignment is by key, not position
    # positional: steps 3 and 4 differ in system_id and resource_id -> 4 of 36 fields wrong (plus COMP 6/6)
    assert m["field"]["positional_accuracy"] == pytest.approx((42 - 4) / 42, abs=1e-6)
    lines = scoring.diff_case(pred, TRUTH, "case_01").splitlines()
    assert any(line.startswith("~") for line in lines)


def test_wrong_evidence_id():
    bad_f1 = Step(1, "FINDING", "account_service", "account_001", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_02")
    pred = dict(TRUTH, case_01=("INCOMPLETE", [bad_f1] + INC[1:]))
    m = scoring.score(pred, TRUTH)
    assert m["field"]["key_recall"] == 1.0
    assert m["field"]["state_accuracy_aligned"] == 1.0
    assert m["field"]["evidence_accuracy_aligned"] == pytest.approx(6 / 7, abs=1e-6)
    assert m["field"]["positional_by_field"]["evidence_id"] == pytest.approx(6 / 7, abs=1e-6)
    assert m["step"]["true_positives"] == 6
    lines = scoring.diff_case(pred, TRUTH, "case_01").splitlines()
    assert any(line.startswith("*") and "evidence_02" in line for line in lines)


def test_missing_prediction_row_and_case_subset():
    pred = {k: v for k, v in TRUTH.items() if k != "case_03"}
    m = scoring.score(pred, TRUTH)
    assert m["missing_predictions"] == 1
    assert m["confusion"]["NOT_APPLICABLE"] == {scoring.MISSING: 1}
    sub = scoring.score(pred, TRUTH, ["case_01", "case_02"])
    assert sub["n_cases"] == 2 and sub["exact_case_match"] == 1.0


def test_split_subsets_natural_order():
    s = scoring.split_subsets(["case_100", "case_04", "case_33", "case_08"], holdout_ids=["case_04", "case_08"])
    assert s["all"] == ["case_04", "case_08", "case_33", "case_100"]
    assert s["holdout"] == ["case_04", "case_08"]
    assert s["dev"] == ["case_33", "case_100"]


def test_lcs_ratio_edges():
    assert scoring.lcs_ratio([], []) == 1.0
    assert scoring.lcs_ratio([1], []) == 0.0
    assert scoring.lcs_ratio([1, 2, 3], [1, 3]) == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# contract validator
# ---------------------------------------------------------------------------

def write_requests(path: Path, ids):
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"])
        for cid in ids:
            w.writerow([cid, "a@example.com", "2026-08-01T00:00:00Z", "COMPLETE",
                        "2026-08-02T00:00:00Z", "2026-09-01T00:00:00Z", "please delete"])


def write_output(path: Path, rows):
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for cid, status, steps in rows:
            w.writerow([cid, status, steps if isinstance(steps, str)
                        else json.dumps([s.to_json_dict() if isinstance(s, Step) else s for s in steps])])


def errors(problems):
    return [p for p in problems if scoring.is_error(p)]


def warnings(problems):
    return [p for p in problems if not scoring.is_error(p)]


@pytest.fixture
def req(tmp_path):
    p = tmp_path / "requests.csv"
    write_requests(p, ["case_01", "case_02", "case_03"])
    return p


def test_validator_clean(tmp_path, req):
    out = tmp_path / "out.csv"
    write_output(out, [(c, s, steps) for c, (s, steps) in TRUTH.items()])
    assert scoring.validate_contract(out, req) == []
    assert scoring.validate_contract(TRUTH, req) == []  # dict form too


def test_validator_bad_phase_order(tmp_path, req):
    out = tmp_path / "out.csv"
    write_output(out, [("case_01", "INCOMPLETE", renumber([F1, F2, V1, R1, R2, V2])),
                       ("case_02", "COMPLETE", COMP), ("case_03", "NOT_APPLICABLE", [])])
    errs = errors(scoring.validate_contract(out, req))
    assert any("phase REMEDIATION after VERIFICATION" in e for e in errs)


def test_validator_within_phase_order_is_warning(tmp_path, req):
    out = tmp_path / "out.csv"
    write_output(out, [("case_01", "INCOMPLETE", renumber([F1, F2, R2, R1, V1, V2])),
                       ("case_02", "COMPLETE", COMP), ("case_03", "NOT_APPLICABLE", [])])
    probs = scoring.validate_contract(out, req)
    assert errors(probs) == []
    assert any("REMEDIATION steps not in operation/system_id/resource_id order" in w for w in warnings(probs))


def test_validator_non_consecutive_step_no(tmp_path, req):
    gap = [F1, F2, R1, R2, V1, Step(9, *scoring.step_body(V2))]
    out = tmp_path / "out.csv"
    write_output(out, [("case_01", "INCOMPLETE", gap), ("case_02", "COMPLETE", COMP),
                       ("case_03", "NOT_APPLICABLE", [])])
    assert any("step_no 9, expected 6" in e for e in errors(scoring.validate_contract(out, req)))


def test_validator_illegal_operation_and_state(tmp_path, req):
    bad_state = st(1, "VERIFICATION", "account_service", "account_009", "VERIFY_RECORD_ABSENT", "PRESENT", "evidence_05")
    bad_op = {"step_no": 1, "phase": "FINDING", "system_id": "x", "resource_id": "y",
              "operation": "PURGE_EVERYTHING", "state": "GONE", "evidence_id": "evidence_01"}
    wrong_phase = st(1, "FINDING", "account_service", "account_009", "DELETE_RECORD", "ABSENT", "evidence_05")
    out = tmp_path / "out.csv"
    write_output(out, [("case_01", "INCOMPLETE", [bad_op]), ("case_02", "COMPLETE", [bad_state]),
                       ("case_03", "INCOMPLETE", [wrong_phase])])
    errs = errors(scoring.validate_contract(out, req))
    assert any("unknown operation 'PURGE_EVERYTHING'" in e for e in errs)
    assert any("VERIFY_RECORD_ABSENT requires state ABSENT" in e for e in errs)
    assert any("DELETE_RECORD belongs to phase REMEDIATION" in e for e in errs)


def test_validator_status_rules(tmp_path, req):
    remed_with_ev = Step(3, "REMEDIATION", "account_service", "account_001", "DELETE_RECORD", "ABSENT", "evidence_01")
    out = tmp_path / "out.csv"
    write_output(out, [
        ("case_01", "INCOMPLETE", [F1, F2, remed_with_ev, R2, V1, V2]),
        ("case_02", "COMPLETE", [Step(1, *scoring.step_body(F1))]),  # finding in COMPLETE
        ("case_03", "NOT_APPLICABLE", COMP),
    ])
    errs = errors(scoring.validate_contract(out, req))
    assert any("INCOMPLETE REMEDIATION step must have evidence_id=''" in e for e in errs)
    assert any("COMPLETE may contain only VERIFICATION" in e for e in errs)
    assert any("NOT_APPLICABLE must have steps_json=[]" in e for e in errs)


def test_validator_rows_json_and_evidence_format(tmp_path, req):
    out = tmp_path / "out.csv"
    bad_ev = Step(1, "VERIFICATION", "account_service", "account_009", "VERIFY_RECORD_ABSENT", "ABSENT", "file_5")
    write_output(out, [("case_01", "BOGUS", "not json"), ("case_01", "COMPLETE", [bad_ev]),
                       ("case_04", "NOT_APPLICABLE", [])])
    errs = errors(scoring.validate_contract(out, req))
    assert any("case_01: 2 rows" in e for e in errs)
    assert any("no row" in e and "case_02" in e for e in errs)
    assert any("not in the requests file" in e and "case_04" in e for e in errs)
    assert any("deletion_status 'BOGUS'" in e for e in errs)
    assert any("not valid JSON" in e for e in errs)
    assert any("is not file_id evidence_NN" in e for e in errs)


def test_validator_sort_order(tmp_path):
    req = tmp_path / "requests.csv"
    write_requests(req, ["case_33", "case_100"])
    lexical = tmp_path / "lex.csv"
    write_output(lexical, [("case_100", "NOT_APPLICABLE", []), ("case_33", "NOT_APPLICABLE", [])])
    probs = scoring.validate_contract(lexical, req)
    assert errors(probs) == [] and any("lexical order" in w for w in warnings(probs))
    natural = tmp_path / "nat.csv"
    write_output(natural, [("case_33", "NOT_APPLICABLE", []), ("case_100", "NOT_APPLICABLE", [])])
    assert scoring.validate_contract(natural, req) == []


def test_validator_evidence_index_warnings(tmp_path, req):
    from ghost.models import parse_ts
    index = {"evidence_05": parse_ts("2026-12-01T00:00:00Z")}  # after audit_at 2026-09-01
    pred = {"case_01": ("NOT_APPLICABLE", []), "case_02": ("COMPLETE", COMP), "case_03": ("NOT_APPLICABLE", [])}
    probs = scoring.validate_contract(pred, req, evidence_index=index)
    assert errors(probs) == []
    assert any("dated after audit_at" in w for w in warnings(probs))


def test_load_output_csv_missing_and_lenient(tmp_path):
    assert scoring.load_output_csv(tmp_path / "nope.csv") == {}
    out = tmp_path / "out.csv"
    write_output(out, [("case_01", "INCOMPLETE", "{broken"), ("case_02", "COMPLETE", COMP)])
    loaded = scoring.load_output_csv(out)
    assert loaded["case_01"] == ("INCOMPLETE", [])
    assert loaded["case_02"] == ("COMPLETE", COMP)
