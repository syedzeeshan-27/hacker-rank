# WS-A: scorer and contract checker

Code: `code/evaluation/scoring.py` (library), `code/evaluation/main.py` (CLI),
tests in `code/tests/test_scoring.py` (`python -m pytest code/tests -q`).

The scorer reads a predictions CSV and a truth CSV at runtime and compares them. It holds no
per-case knowledge. The official grader is hidden, so these numbers are our own proxies for the
things the problem statement says it looks at (status, discovered records and risks, cited
evidence, policy decisions, completeness and order of `steps_json`).

## What a "step" means in the metrics

Every step has seven fields: `step_no, phase, system_id, resource_id, operation, state, evidence_id`.

- **Step tuple**: all seven fields.
- **Step body**: the same without `step_no`. Used wherever we compare steps as a set, so one
  inserted step does not make every later step "wrong" just because its number shifted.
- **Step target**: `(system_id, resource_id, operation)`, meaning what the step is about and what is
  being done to it, ignoring the claimed state and the evidence cited.

## Metric definitions

**Status accuracy.** Share of cases where `deletion_status` matches the truth.

**Exact case match.** Share of cases where the status matches and the ordered list of step
tuples is identical, step numbers included. This is the strictest number: one wrong evidence id
fails the case.

**Step precision / recall / F1 (pooled).** We put every predicted step body from every case
into one bag and every truth step body into another, and count overlaps (a step counts once per
copy). Precision = how many of our steps are correct; recall = how many of the true steps we
produced; F1 is their harmonic mean. Order is ignored here. A step only counts if all six
body fields match, evidence id included. Long cases weigh more than short ones in the pooled
number.

**Mean per-case F1.** The same F1 computed inside each case, then averaged across cases, so every
case weighs the same. A case where both sides have no steps (for example a correct
`NOT_APPLICABLE`) scores 1.0. A case where only one side has steps scores 0.0.

**Field-level accuracy, aligned.** We pair predicted and truth steps that share a step target
(repeats are paired in order). Then:
- *target recall* = paired steps / truth steps (did we find the right records and risks?);
- *target precision* = paired steps / predicted steps (`key_precision` in JSON);
- *state accuracy* = share of paired steps whose `state` matches;
- *evidence-id accuracy* = share of paired steps whose `evidence_id` matches. For remediation and
  future verification steps both sides should be empty, so this mostly measures citations on
  findings and on COMPLETE verifications.

Because state is fixed by the operation in the allowed-operations table, state accuracy on
aligned steps is almost always 1.0 for contract-valid output; evidence-id accuracy is the
informative one.

**Positional field accuracy.** Compare the predicted and truth step lists slot by slot (first
with first, second with second, ...) on the six body fields. Score = matching fields /
(6 x number of truth steps). Missing slots count as wrong, extra predicted slots are ignored.
Also reported per field. This punishes order mistakes and insertions harshly, which is why we
also report LCS.

**Exact sequence accuracy.** Share of cases whose ordered list of step tuples equals the truth
exactly (status not considered). The gap between this and exact case match is the number of
cases with right steps but wrong status.

**Mean LCS ratio.** For each case, the length of the longest common subsequence of predicted and
truth step bodies, divided by the longer of the two lists (1.0 when both are empty), averaged
over cases. It rewards getting most steps in the right relative order, so a single insertion or
swap costs only a little, unlike positional accuracy.

**Status confusion.** Counts of truth status (rows) against predicted status (columns). A case
with no prediction row shows up under the pseudo-status `MISSING`.

**Per-case table** (`--per-case`, or `per_case` in JSON): case_id, truth status, predicted status,
number of truth and predicted steps, per-case F1, exact (yes/no), LCS ratio.

## Dev / holdout split

`code/config.json["sample_holdout_cases"]` lists the holdout cases (currently every 4th sample
case: case_04, 08, ..., 32). Dev is the other 24. When the truth file contains holdout cases and
`--subset all` (the default) is used, the CLI prints three blocks: all, dev, holdout. Report both
dev and holdout numbers; a large gap between them points at overfitting to the dev cases.

## Contract check (`--validate`)

Errors (exit code 1): wrong header; not exactly one row per `case_id` in the requests file;
rows not sorted at all; status not in `COMPLETE/INCOMPLETE/NOT_APPLICABLE`; `steps_json` not a
JSON array; missing or extra step fields; `step_no` not an integer counting 1, 2, 3, ...; phases
out of FINDING, REMEDIATION, VERIFICATION order; operation not in the allowed table, or with the
wrong state or phase; empty system or resource id; `NOT_APPLICABLE` with steps; `COMPLETE` with a
non-verification step or an empty evidence id; `INCOMPLETE` finding with no evidence id, or
remediation/verification with one; evidence id not shaped like `evidence_NN`, `activity_NN` or
`audit_NN`.

Warnings (do not change the exit code): rows in plain lexical order instead of natural order;
steps inside a phase not in operation-table order, then `system_id`, then `resource_id`
(natural); a `COMPLETE` or `INCOMPLETE` row with no steps; duplicate steps; a cited id not found
in `dataset/` (evidence_files, system_activity, user_audit_log); a cited id dated after the
case's `audit_at` (manifest `captured_at`, or the activity/audit `event_time`).

`dataset/sample_output.csv` passes with 0 errors and 0 warnings against
`dataset/sample_deletion_requests.csv`.

## CLI examples (run from the repo root)

```text
python code/evaluation/main.py                                   # .cache/runs/sample_output.csv vs dataset/sample_output.csv
python code/evaluation/main.py --predictions some_run.csv --subset holdout
python code/evaluation/main.py --per-case                       # add the per-case table
python code/evaluation/main.py --case case_09                   # side-by-side diff for one case
python code/evaluation/main.py --markdown                       # table for evaluation_report.md
python code/evaluation/main.py --json .cache/runs/metrics.json  # machine-readable metrics
python code/evaluation/main.py --validate output.csv --requests dataset/deletion_requests.csv
python code/evaluation/main.py --validate .cache/runs/sample_output.csv --requests dataset/sample_deletion_requests.csv
```

Exit codes: 0 normally, 1 when `--validate` finds errors, 2 when an input file is missing.

Diff markers: `=` same step in the aligned position, `~` step present on both sides but out of
order, `*` same target with a different state or evidence id, `-` missing from the prediction,
`+` extra in the prediction.

## Sanity results

- `dataset/sample_output.csv` scored against itself: every metric 1.000 on all, dev and holdout.
- Same file with every status forced to INCOMPLETE: status accuracy 0.688 (22/32), exact case
  match 0.688, all step, field and sequence metrics 1.000 (steps untouched). The 10 COMPLETE
  cases appear as COMPLETE -> INCOMPLETE in the confusion table.
- Perturbing two cases (one step dropped, one fake step inserted, two steps swapped in case_09;
  one wrong evidence id in case_11): exact 0.938, pooled step F1 0.990, mean LCS 0.985, and the
  validator flags the swap that put a remediation step after a verification step.

## Contract ambiguities found

1. **Row order.** "Sort rows by case_id" does not say natural or lexical. The requests file
   itself is in natural order (case_33 ... case_99, case_100 ... case_550). We treat natural as
   correct and only warn on lexical. The writer should use natural order (`case_sort_key`).
2. **Within-phase order vs dependency order.** The problem statement orders steps by the
   operation table, then `system_id`, then `resource_id`. AGENTS.md 6.5 also says "process
   downstream copies before their source records when the dependency requires it", which could
   clash with alphabetical `system_id`. All 32 sample answers follow the table/system/resource
   order exactly (0 violations), so we treat that as the rule and report deviations as warnings.
3. **Evidence types.** Every cited id in the sample truth is a `file_id` (`evidence_NN`, 75 cited
   steps); none cite `activity_NN` or `audit_NN`, although the rules allow them. Activity ids go
   up to 4 digits and audit ids up to 3, so the format check accepts 2 or more digits.
4. **No NOT_APPLICABLE in the sample.** The sample truth has 22 INCOMPLETE and 10 COMPLETE, so
   the scorer cannot measure NOT_APPLICABLE detection on sample data. Custom cases are needed.
5. **Hidden grader weighting.** We do not know how the official grader weights status, steps and
   evidence, or whether it gives partial credit. Our headline numbers are proxies.

## Known limitations

- Pooled step metrics are dominated by cases with many steps; read mean per-case F1 alongside.
- Positional accuracy collapses after an early insertion; LCS ratio is the fairer order measure.
- Target alignment pairs repeated targets in order of appearance. The sample truth never repeats
  a target inside a case, so this rarely matters.
- The "dated after audit_at" warning uses manifest `captured_at`. A file whose internal snapshot
  time is earlier than its manifest time is not inspected.
- `load_output_csv` is lenient: unparseable `steps_json` scores as an empty step list and the
  first row wins on duplicate case ids. Run `--validate` to see those problems.
- A predictions file with case ids that are not in the truth is ignored when scoring (counted as
  `extra_predictions` only when no case subset is passed).
