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
| Step precision | 0.984 | 1.000 | 0.929 |
| Step recall | 0.969 | 0.993 | 0.886 |
| Step F1 (pooled) | 0.977 | 0.997 | 0.907 |
| Target recall | 0.969 | 0.993 | 0.886 |
| State accuracy on matched targets | 1.000 | 1.000 | 1.000 |
| Evidence-id accuracy on matched targets | 1.000 | 1.000 | 1.000 |
| Positional field accuracy | 0.961 | 0.980 | 0.898 |
| Exact sequence accuracy | 0.875 | 0.958 | 0.625 |
| Mean LCS ratio | 0.959 | 0.997 | 0.847 |

Status confusion (all 32): COMPLETE 10/10, INCOMPLETE 22/22. Every step we produce has the right state
and cites the right evidence file; the misses are missing or mis-typed steps, never wrong citations.

### 2.1 The four cases that are not exact, and why

| Case | Set | What differs | Cause |
|---|---|---|---|
| case_18 | dev | one missing check: `VERIFY_RECORD_PRESENT security_service/audit_record_6645` | that audit record id appears nowhere in the inputs: not in any text evidence file, not in any of the 11 screenshots, not in the activity stream (the only "6645" anywhere is an unrelated account id) |
| case_20 | held-out | one missing check: `VERIFY_RECORD_PRESENT security_service/audit_record_5569` | same as case_18 |
| case_12 | held-out | the analytics record should be deleted (REPORT_REMAINING_RECORD / DELETE_RECORD / VERIFY_RECORD_ABSENT), we proposed anonymizing it | the record carries a marketing opt-in flag and the worker itself issued DELETE_RECORD on it; our policy mapped it by its analytics category (ANONYMIZE). This pattern occurs once in the whole dataset (that single activity row), so no evaluation case is affected |
| case_04 | held-out | one missing check: `VERIFY_RECORD_ANONYMIZED analytics_platform/analytics_901` (the account step `VERIFY_RECORD_ABSENT account_004 [evidence_54]` now matches, see section 7) | `account_004` appears in no input; the account is now derived from the user id. The anonymized analytics record is only linked by a worker row that belongs to no account row (539 account rows exist for 540 deletion requests and this is the one without), so nothing ties it to the person |

Held-out verdict: status generalises perfectly (8/8); step lists are weaker (5/8 exact). One of the
three misses is now a single missing step on an untraceable record, one needs an audit record that no
input names, one is a one-off policy pattern. The first version of the engine was built without
looking at the held-out cases; the revision in section 7 was triggered by an external review that
pointed at case_04, so the held-out column is no longer a blind test for that one case (the other
seven held-out answers did not change).

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

| 9 | (review) three evaluation requests from early-numbered users had no account step at all (case_45, 61, 63) | their accounts appear in no evidence file and no deletion-worker row | the account id implied by a zero-padded user id (`user_03` -> `account_003`) is used when nothing else supplies an account; one named function, `identity.derived_account_id`, holds the pattern, and the account's state still comes from eligible evidence (case_45 now cites the 09-02 failover capture, exactly as the case_04 answer does) |
| 10 | (review) identity linkage was cut off at audit_at like a state | the same time filter served two different questions | linkage ("whose record is this") may use any file; state and citation use eligible files only (`LINKAGE_IGNORES_AUDIT_CUTOFF`). On the evaluation set this added the account and / or the retained audit record to 7 INCOMPLETE plans, added evidence-backed checks to 3 COMPLETE plans, and removed run-linked records that a later file shows belong to someone else from 4 plans |
| 11 | (review) two COMPLETE answers had an empty step list (case_39, case_55) | the connector console screenshot showed the flow as "Healthy / Continuous sync" and the backup console showed an intact archive with a retention end date, which the state mapper did not read; the backup record also took the archive's creation date as its capture time, which put it before the request | console vocabulary mapped to states (healthy / continuous = running flow; checksum / drill / retention fields = restorable archive unless blocked); a record's creation date is never its capture time. Both cases are now INCOMPLETE with the screenshot cited |
| 12 | (review follow-up) while fixing 9, the derived account was proven absent by the empty 09-05 inventory, but the case_04 answer cites the 09-02 failover capture | the `customer_data_inventory_capture` files only ever list accounts that are DELETION_PENDING (8 rows over 11 files): a deletion queue, not an inventory, so "not listed" there proves nothing | a source whose slice of a system only ever lists pending-deletion rows is detected from the statuses themselves and never implies absence (`EvidenceIndex.queue_slices`); 23 evaluation COMPLETE answers moved their account citation from an inventory file to the latest real enumeration |

Development exact match after the fixes: 0.958 (23/24), before and after the screenshots were added,
and unchanged after the review revision.

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
  account rather than cite the wrong evidence (43 evaluation answers have no account step).
- Incomplete evaluation answers contain a security audit record only when some evidence names one for
  that person; if the hidden answers always include one, the other cases lose one verification step.
- 11 evaluation cases are built around a screenshot (`case_34, 35, 39, 45, 46, 50, 52, 53, 55, 61, 63`).
  All 11 now produce findings or evidence-backed checks. Screenshot facts come from a vision model
  (`code/prompts/image_extraction.md`) and are the one non-deterministic input; they are cached by file hash
  so a re-run is stable, and a cold run can read a screen slightly differently (the final cold run produced
  the same 518 rows as the cached run before it).
- Identity linkage may use any file, including one captured after audit_at; states and citations never
  do. Switch `state.LINKAGE_IGNORES_AUDIT_CUTOFF` to restrict linkage as well.
- No COMPLETE answer has an empty step list any more; every COMPLETE row carries at least one
  evidence-backed verification step.

## 7. Revision after review

An external review of the first submission raised five points. What changed, and what it did to the
answers (all numbers from the final cold run, `output.csv` re-generated and re-scored):

| Review point | Change | Effect |
|---|---|---|
| Users `user_01..user_09` map to `account_001..account_009`; three requests had no account step | `identity.derived_account_id` (fix 9) | case_45: `VERIFY_RECORD_ABSENT account_003 [evidence_54]`; case_61 and case_63: future `VERIFY_RECORD_ABSENT` on `account_007` / `account_009` in their INCOMPLETE plans; case_04's account step now matches the truth |
| COMPLETE with an empty step list (case_39, case_55) | fix 11 | both are INCOMPLETE: an active connector (`REPORT_ACTIVE_DATA_FLOW data_flow_1121 [evidence_14]`) and a restorable archive (`REPORT_RESTORE_RISK backup_9287 [evidence_32]`), each with its remediation and checks |
| Linkage and state used the same time cutoff | fix 10 | 12 evaluation answers changed through linkage alone (plus the account / audit-record checks of case_39 and case_55); one status flipped (case_162 INCOMPLETE -> COMPLETE: its restore-risk row came from a run row on an archive that a later export shows is another customer's) |
| (found while fixing the first point) the empty 09-05 inventory was cited for an absent account | fix 12 | 23 COMPLETE answers now cite the latest real enumeration instead of a deletion-queue file |
| Packaging: `evaluation/` must sit at the top of `code.zip` | `code/tools/package.py` builds the zip with `README.md`, `code/` and `evaluation/*.md` at the root | the zip is checked for required entries and for anything forbidden (`dataset/`, `.env`, caches, nested archives) |
| README | rewritten: what it does, results table, copy-pasteable quick start, packaging command | numbers match this run |

Totals: 39 of 518 rows changed against the first submission. Statuses moved from 154 / 354 / 10 to
153 / 355 / 10 (COMPLETE / INCOMPLETE / NOT_APPLICABLE). Three statuses flipped (case_39 and case_55 to
INCOMPLETE, case_162 to COMPLETE); the other 36 changed rows kept their status and only gained, lost or
re-cited steps. Public-sample scores: status 32/32 unchanged, development 23/24 unchanged, held-out 5/8
unchanged with step recall up from 0.864 to 0.886. The first README quoted 160 / 348 for the previous
run; the file itself held 154 / 354, so that figure was wrong and is corrected here.

Per-workstream notes with more detail: `code/evaluation/notes/`.
