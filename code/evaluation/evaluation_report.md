# Evaluation report

How we measured the deletion-audit agent, what it gets right, where it fails, and what we changed.

<!-- STATUS: numbers below are from the latest sample run; see "Run used" in each table. -->

## 1. Method

The only ground truth we have is `dataset/sample_output.csv` (32 public cases). Before writing any
decision logic we built a scorer (`code/evaluation/main.py`, library `code/evaluation/scoring.py`) and
split the samples into **24 development cases** and **8 held-out cases** (`case_04, 08, 12, 16, 20, 24,
28, 32`, every fourth case, fixed in `code/config.json`). All rules were developed and debugged on the
24; the 8 were scored once at the end to check for over-fitting. Both numbers are reported below.

Metric definitions (plain English):

| Metric | Meaning |
|---|---|
| Status accuracy | share of cases whose `deletion_status` matches |
| Exact case match | status and the whole ordered step list match |
| Step precision / recall / F1 | steps as (phase, system, resource, operation, state, evidence) tuples, pooled over cases |
| Target recall | share of truth (system, resource, operation) targets we produced |
| State / evidence accuracy on matched targets | on targets we produced, is the state / evidence id right |
| Positional field accuracy | compare the 6 fields position by position (denominator 6 x truth steps) |
| Exact sequence accuracy | share of cases whose ordered step list is identical |
| Mean LCS ratio | longest common subsequence of steps / longer list length, averaged |

Reproduce: `python code/main.py --cases sample` then
`python code/evaluation/main.py --predictions .cache/runs/sample_output.csv --truth dataset/sample_output.csv --subset all`.

## 2. Results on the public samples

Run used: the final cold run that produced `output.csv` (`python code/main.py --clear-cache`), which read all
94 evidence files including the 11 screenshots (11 vision calls, see `usage_report.md`), followed by a warm
run of the 32 samples for scoring (same code, same cache).

| Metric | All 32 | Development 24 | Held-out 8 |
|---|---|---|---|
| Status accuracy | 1.000 | 1.000 | 1.000 |
| Exact case match | 0.875 | 0.958 | 0.625 |
| Step precision | 0.984 | 1.000 | 0.927 |
| Step recall | 0.964 | 0.993 | 0.864 |
| Step F1 (pooled) | 0.974 | 0.997 | 0.894 |
| Target recall | 0.964 | 0.993 | 0.864 |
| State accuracy on matched targets | 1.000 | 1.000 | 1.000 |
| Evidence-id accuracy on matched targets | 1.000 | 1.000 | 1.000 |
| Positional field accuracy | 0.956 | 0.980 | 0.875 |
| Exact sequence accuracy | 0.875 | 0.958 | 0.625 |
| Mean LCS ratio | 0.944 | 0.997 | 0.784 |

Status confusion (all 32): COMPLETE 10/10, INCOMPLETE 22/22. Every step we produce has the right state
and cites the right evidence file; the misses are missing or mis-typed steps, never wrong citations.

### 2.1 The four cases that are not exact, and why

| Case | Set | What differs | Cause |
|---|---|---|---|
| case_18 | dev | one missing check: `VERIFY_RECORD_PRESENT security_service/audit_record_6645` | that audit record id appears nowhere in the inputs: not in any text evidence file, not in any of the 11 screenshots, not in the activity stream (the only "6645" anywhere is an unrelated account id) |
| case_20 | held-out | one missing check: `VERIFY_RECORD_PRESENT security_service/audit_record_5569` | same as case_18 |
| case_12 | held-out | the analytics record should be deleted (REPORT_REMAINING_RECORD / DELETE_RECORD / VERIFY_RECORD_ABSENT), we proposed anonymizing it | the record carries a marketing opt-in flag and the worker itself issued DELETE_RECORD on it; our policy mapped it by its analytics category (ANONYMIZE). This pattern occurs once in the whole dataset (that single activity row), so no evaluation case is affected |
| case_04 | held-out | we produced no steps for a COMPLETE case (expected: account absent + analytics anonymized) | `account_004` appears in no participant-facing input at all (no evidence file, no activity row, no catalog entry); the only tie is the sample numbering, which does not generalise. The anonymized analytics record is only linked by a worker row with no account row in its run; 539 account rows exist for 540 deletion requests and this is the one without |

Held-out verdict: status generalises perfectly (8/8); step lists are weaker (5/8 exact). One of the
three misses is unreachable from the data, one needs the screenshots, one is a one-off policy pattern.
No rule was changed after looking at the held-out cases, so the 24 / 8 numbers above are a clean
development / held-out comparison.

## 3. Custom evaluation cases

Nine extra cases built from small synthetic fixtures (`code/evaluation/custom_cases.py`, data under
`code/evaluation/custom_cases_data/`), run end-to-end through the same engine. Run with
`python code/evaluation/custom_cases.py`.

| Case | Checks | Expected | Result |
|---|---|---|---|
| a_history_email | request email only exists in the audit log (old address) | COMPLETE, 2 checks | pass |
| b_tombstone_pii | account tombstone still carries email / phone | INCOMPLETE: REPORT_EXPOSED_PII, REDACT_RECORD, VERIFY_RECORD_REDACTED | pass |
| c_order_flow_restore | active data flow + restorable backup + leftover search entry | INCOMPLETE; flow disabled and restore blocked before the delete | pass |
| d_replica_only | record survives only in the failover replica | INCOMPLETE: REPORT_REMAINING_RECORD on the account | pass |
| e1 / e2 audit cutoff | identical evidence, audit_at before / after a re-index | COMPLETE then INCOMPLETE | pass |
| f_marketing_only | "remove me from marketing lists only, keep the account" | NOT_APPLICABLE | pass |
| g_negated_deletion | "I am not asking for deletion" | NOT_APPLICABLE | pass |
| h_export_link | "delete that export file as well" | deletion request, INCOMPLETE | pass |

Hand check of request texts: 30 distinct texts covering every language present (English, French,
Portuguese, Spanish, Japanese, Korean) were read by a person and compared with the rule-based label; all
30 agreed (`code/evaluation/notes/intent_review.md`). The one text a reader could argue about (marketing
list removal only) is case f above.

## 4. Failures found and improvements made

Starting point after the first end-to-end run: development exact match 0.375 (status 0.958).

| # | Failure seen on development cases | Root cause | Fix (general rule) |
|---|---|---|---|
| 1 | security audit records missing from verification (11 cases) | a partial snapshot that did not list the record made it look absent; RETAIN + ABSENT emitted nothing | retained records always get `VERIFY_RECORD_PRESENT` in incomplete plans; "not listed in a snapshot" never makes retained / backup / connector records absent |
| 2 | case_19 predicted COMPLETE | a bundle delivered after audit_at was treated as eligible because its inner snapshot was older | eligibility uses the file's manifest capture time, never the inner snapshot time |
| 3 | extra records from a neighbouring deletion run (case_13, 18, 22) | a row whose timing matched by coincidence was attached to the wrong run | chains must be anchored by a subject resource; rows whose evidence names someone else are dropped |
| 4 | audit record dropped from COMPLETE plans (case_26, 27) | a later partial snapshot overrode an explicit sighting | a snapshot omission overrides a sighting only when the sighting predates the request |
| 5 | support record redacted instead of deleted (case_10) | all `support_*` rows were treated as conversations | the worker's own operation type (DELETE vs REDACT) tells copies from conversations; its result is never trusted |
| 6 | no account found when several deletion runs sat in the time window (case_21, 25) | ambiguity | exclude runs already claimed by other requests, runs whose account belongs to someone else, prefer accounts that an eligible export looked up; if still ambiguous, claim nothing |
| 7 | (evaluation set, found by inspection) a customer statement PDF named an export only by statement number | resource-less documents were dropped | catalog aliases resolve statement numbers to resource ids |
| 8 | after the screenshots were read, case_07 gained a false finding: a stored attachment seen in an object-storage screenshot taken months before the request | a sighting that predates the request was allowed to prove that a record "remains" | a fact captured before `requested_at` may link a resource to the person but can never establish a finding (remaining record, exposed PII, active flow, restore risk). Checked first: all 58 cited steps in the development answers cite files delivered after the request; on the evaluation set the rule changed 2 findings and no status |

Development exact match after the fixes: 0.958 (23/24), before and after the screenshots were added.

Earlier, in the extraction layer: lookup keys in trace logs (a 404 for a lookup by the person's email)
are attached to the observation so that "not found" results can be tied to the person; replica tables
are kept as separate copies of the same record so replication drift is visible.

## 5. Untrusted content

Evidence files and request texts are treated as data. Every extracted text field is scanned for
instructions aimed at an AI, an auditor or the audit process; hits are recorded by file id in
`.cache/injection_flags.json` and ignored. On this dataset: no injection attempt was found in the text
formats or the request texts. The 11 screenshots were read through the vision model with a prompt that asks
it to copy any text addressed to an AI or auditor into a separate field; the parsed output was scanned the
same way and nothing was flagged.

## 6. Known limitations

- The account record and anonymized analytics profiles often carry no identifier; they are tied to the
  request by reconstructing the deletion worker's run from the activity stream (the dispatch cadence is
  visible in the stream itself). This is the most dataset-specific rule; when it is ambiguous we claim no
  account rather than cite the wrong evidence (45 evaluation cases).
- 82 incomplete evaluation answers contain no security audit record because no text evidence names one
  for that person; if the hidden answers always include one, those cases lose one verification step.
- 11 evaluation cases are built around a screenshot (`case_34, 35, 39, 45, 46, 50, 52, 53, 55, 61, 63`).
  With the screenshots read, 9 of them produce findings or evidence-backed checks from the screenshot; two
  (`case_39`, `case_55`) still come out COMPLETE with an empty step list because nothing in their screenshot
  or elsewhere could be tied to the person with a usable state. Screenshot facts come from a vision model
  (`code/prompts/image_extraction.md`) and are the one non-deterministic input; they are cached by file hash
  so a re-run is stable, and a cold run can read a screen slightly differently.
- Identity ties may come from a later-delivered file whose record time is at or before audit_at (used
  only to know which records to check, never for a state or a citation); switch `state.LINK_FROM_LATER_FILES`.

Per-workstream notes with more detail: `code/evaluation/notes/`.
