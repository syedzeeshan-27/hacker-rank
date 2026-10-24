# WS-D: decision engine (state, policy, planner)

Code: `code/ghost/state.py` (what is true at audit_at), `code/ghost/policy.py` (deletion_rules.csv ->
treatment), `code/ghost/planner.py` (`audit_case`, `decide_unknown_subject`, step building and
ordering). Tests: `code/tests/test_engine.py` (13), `code/tests/test_custom_cases.py` (10). Custom
evaluation cases: `code/evaluation/custom_cases.py` with fixtures in `code/evaluation/custom_cases_data/`.
Full suite: `python -m pytest code/tests -q` -> 114 passed. Offline run of all 518 cases: 1.7 s, no LLM
calls, byte-identical output on two runs.

## Decision rules in plain English

**1. What counts as "known at audit_at".** A file can be used for a state or a citation only when its
manifest time (delivery time) is at or before audit_at. A bundle delivered two days after the audit is
never cited, even if a snapshot inside it is older (sample case_26 cites the 11:44 export, not the
12:00 snapshot inside the 09-17 bundle). Inside an eligible file the fact time is the record's own
time. Activity rows count only when they happened at or before audit_at.

One exception, for linking only: if a later-delivered file holds a record snapshot taken at or before
audit_at that says "resource X belongs to user U", we use that tie to know *which* records to check
(typically the security audit record). It never supplies a state or a citation, and it only ever adds
future verification steps in INCOMPLETE cases (on the eval set no COMPLETE answer depends on it).
Switch: `state.LINK_FROM_LATER_FILES`.

**2. Which records belong to the person.** A resource is tied to the subject when:
- an observation on it carries one of the subject's emails, phones, email hashes or user_id, current
  or historical (lookup keys count: a 404 for a lookup by the subject's email ties that id);
- or it carries the subject's name and no identifier of somebody else (names shared by two people in
  user_identities.csv are never used);
- or a LINK_RESOURCE_IDENTITY catalog row names the subject; a resource-less document (customer
  statement) whose external reference is a catalog alias (NST-2026-0718-1842 -> export_1842) is read as
  an observation of that resource;
- or the deletion run for this request processed it (rule 3);
- or a data flow / backup row says it repopulated one of the subject's resources.
A run row is dropped when eligible evidence shows the resource belongs to someone else.

**3. Reconstructing the deletion run.** The account record and the anonymized analytics profiles
usually carry no identifier at all, so the only tie is the deletion worker's run in
system_activity.csv. Each request has one run: an account DELETE_RECORD row (the head) and follow-up
rows on the other records. The stream itself shows a fixed dispatch cadence: consecutive follow-ups of
a run are 390 / 449 / 1110 / 1169 s apart, and the first follow-up sits a whole number of minutes plus
41 or 52 s after the head. With those constants (derived from the stream's timing histogram, +-1 s):
- candidate heads are the account deletions 40-80 minutes before reported_at (10 minutes either side
  when that window is empty);
- a candidate is chosen, in this order, because the evidence ties its account to the subject
  ("evidence"), because the cadence ties it to a chain holding a subject resource ("cadence"), or
  because it is the only candidate left after removing runs that other requests in the queue already
  claim with what was known at this audit_at, runs whose account evidence names somebody else, and
  runs whose account no eligible file ever looked up ("window", the documented last resort);
- if more than one candidate survives, no account is claimed. A wrong account would cite wrong
  evidence, so "no claim" is preferred.
The run's follow-up resources join the subject's set: chains holding a subject resource, or (when no
chain does) the single chain whose first row sits at an exact run phase.

**4. The state of each record at audit_at.**
- Explicit observations are grouped per copy (primary, read / failover / reporting / regional
  replica); per copy the latest fact wins (fact time, then delivery time, then evidence id).
- A snapshot that enumerates the system without listing the record is an ABSENT fact, used for
  records the policy wants deleted / redacted / anonymized when nothing explicit exists (case_01,
  case_31), or when the only explicit sighting predates the request. It never overrides a
  post-request sighting, and it is never applied to audit logs, legal holds, backups or connectors
  (snapshot slices are partial: a retained record missing from one slice is not "gone").
- A copy whose latest fact predates the request is ignored when another copy has newer facts.
- Across copies the worst state wins: one surviving replica row keeps the record PRESENT (case_05).
- Activity rows give a state only when no eligible file shows the resource (one eval case:
  CHECK_RESTORE_ACCESS "can still be restored").
- One account per request: the run's account when evidence or cadence chose it, else the identifier-
  linked account with the newest fact (an old lookup of another account id is ignored, case_17).

**5. Policy (deletion_rules.csv, by data category).** DELETE for profiles, secondary identifiers,
search entries, caches, exports, marketing profiles, support attachments; REDACT for support
conversations, email delivery logs and account tombstones (keep the user_id, strip name / email /
phone / hash); ANONYMIZE for analytics and telemetry (a row with only a user_id left is still
attributable); RETAIN for security audit logs and legal holds while the retention date has not
passed; BLOCK_RESTORE for backup archives (the rule keeps the archive "if restore access can be
blocked"); DISABLE for active data flows. The table is read at runtime; nothing is keyed by resource id.
Support records: the evidence never says whether a support_* row is a conversation or a stored copy
(attachment, legacy contact index). When the evidence only gives the default, the platform's own run
classifies it: rows the worker DELETEs are treated as attachments, rows it REDACTs as conversations.
Only the operation type is used, never its reported result.

**6. Steps.**
| treatment | state | finding | remediation | verification |
|---|---|---|---|---|
| DELETE | PRESENT / REDACTED / ANONYMIZED | REPORT_REMAINING_RECORD | DELETE_RECORD | VERIFY_RECORD_ABSENT |
| DELETE | ABSENT | - | - | VERIFY_RECORD_ABSENT (account, or a record this run processed) |
| REDACT | PRESENT | REPORT_EXPOSED_PII | REDACT_RECORD | VERIFY_RECORD_REDACTED |
| REDACT | REDACTED | - | - | VERIFY_RECORD_REDACTED |
| ANONYMIZE | PRESENT / REDACTED | REPORT_EXPOSED_PII | ANONYMIZE_RECORD | VERIFY_RECORD_ANONYMIZED |
| ANONYMIZE | ANONYMIZED | - | - | VERIFY_RECORD_ANONYMIZED |
| RETAIN | any | - | - | VERIFY_RECORD_PRESENT |
| BLOCK_RESTORE | ACTIVE / BLOCKED | REPORT_RESTORE_RISK / - | BLOCK_RESTORE / - | VERIFY_RESTORE_BLOCKED |
| DISABLE | ACTIVE / DISABLED | REPORT_ACTIVE_DATA_FLOW / - | DISABLE_DATA_FLOW / - | VERIFY_DATA_FLOW_DISABLED |

Any finding makes the case INCOMPLETE: findings cite the evidence that shows the state, remediation
and verification use "". Otherwise COMPLETE: only verifications whose state an eligible file proves,
each citing the latest such file. Order: FINDING, REMEDIATION, VERIFICATION; within a phase the
operation order of the allowed-operations table (so connectors and restores are closed before records
are changed), then system_id, then resource_id in natural order; steps numbered from 1.

**Unknown subject** (deletion authorized, email matches nobody): there is no record we can tie to the
person, so nothing to remediate and nothing evidence-backed to verify -> `COMPLETE, []` with a note.
Never triggers (all 518 + 32 emails resolve).

**17 eval cases with no subject-mentioning evidence before audit_at.** They are handled by the same
rules: only run-linked resources (and catalog links) can appear; if the run is ambiguous the answer
is COMPLETE with whatever eligible evidence proves (possibly no steps). No guessing.

## Failures found on dev and the rule fixed (in order)

Starting point after the first implementation: dev exact 0.375, status 0.958.

1. Security audit records missing from verification (11 cases). A coverage snapshot slice that did
   not list the record turned it ABSENT, and RETAIN + ABSENT emitted nothing. Fix: a retained record
   always gets VERIFY_RECORD_PRESENT in INCOMPLETE plans; coverage never makes retained / backup /
   connector records absent.
2. case_19 predicted COMPLETE. Coverage entries carry only the inner snapshot time, so the 09-17
   bundle's 09-15 slice counted as eligible. Fix: coverage uses its file's manifest time.
3. case_13 / case_18 / case_22 extra records from neighbouring runs (a row whose offset from the head
   matched the cadence within 1 s by coincidence). Fix: anchored chains first; a chain without a
   subject resource only on an exact phase and only if it is the only one; drop run rows whose
   evidence names somebody else.
4. case_26 / case_27 audit record dropped in COMPLETE plans: a later snapshot without the record
   overrode the explicit post-request sighting. Fix: coverage overrides an explicit sighting only when
   the sighting predates the request.
5. case_10 support_002 treated as a conversation (REDACT) while it is a legacy contact-index copy
   (truth: DELETE). Fix: worker operation type as the support category hint (rule 5).
6. case_21 / case_25 no account (several runs in the window). Fix: exclude runs claimed by other
   requests (as known at audit_at), runs whose account belongs to someone else, and prefer runs whose
   account an eligible export looked up. case_21 / case_25 now exact.
7. case_33 (eval, found by inspection, no answer available): a customer statement PDF with no resource
   id named export_1842 only through its statement number. Fix: catalog aliases resolve resource-less
   documents.

8. case_07 (after the 11 screenshots were extracted): object_19 (catalog-linked attachment) became a
   REPORT_REMAINING_RECORD citing the object-storage screenshot captured months before the request.
   Checked first: all 58 cited steps in the dev answers cite a file delivered after the request, and
   only 2 of 723 eval findings cited pre-request evidence. Fix: a fact captured before requested_at
   cannot establish a finding (remaining record, exposed PII, active flow, restore risk); it may still
   link the resource. The violation states come from the policy table via the planner. Interaction
   with the stale-copy rule: that rule drops a pre-request copy only when a newer copy exists; this
   one also drops a lone pre-request copy, but only when its state would be a violation, so
   pre-request verifications are untouched. Dev back to 23 / 24; eval: 2 answers lose a finding
   (case_33, case_34), no status changes.

Dev after the fixes: 23 / 24 exact.

## Remaining dev miss (needs information not in the text evidence)

- case_18: `VERIFY_RECORD_PRESENT security_service/audit_record_6645` is in the answer, but
  audit_record_6645 appears in no text evidence file and no activity row. The only unread sources at
  or before its audit_at are the 11 PNG screenshots (evidence_04 account admin console, captured at
  exactly case_18's audit_at, and evidence_37 security audit explorer). Their observations go through
  the same Observation contract, so they plug in with no engine change.

## Open issues / risks (for the orchestrator)

- After the screenshots: audit_record_6645 (case_18) is still in no evidence file, screenshot
  extraction or activity row (the only "6645" in the evidence is an unrelated account_6645), so it is
  unreachable from the inputs. The orchestrator reports the same for a holdout case (not inspected here).

- **Run cadence constants** (390 / 449 / 1110 / 1169 s gaps, 41 / 52 s phase) describe how this
  dataset's deletion worker dispatches; they are the weakest, most dataset-specific rule. Eval run
  resolution: evidence 234, cadence 178, window / elimination 39, ambiguous 45 (no account claimed),
  none 12 (the catalog / screenshot cases).
- 82 INCOMPLETE eval answers have no security audit record because no text evidence names one; if
  the hidden answers always include one (case_18 suggests they might), those cases lose one step.
- **Screenshots**: 11 eval cases are built around a PNG and a catalog row and currently come out
  COMPLETE with few or no steps: case_34 (evidence_04), case_35 (09), case_39 (14), case_45 (37),
  case_46 (42), case_50 (19), case_52 (23), case_53 (28), case_55 (32), case_61 (46), case_63 (60).
  Dev case_18 may also need evidence_04 / evidence_37.
- Using later-delivered files for identity ties is a judgement call (rule 1). It recovers
  the audit record ids the dev answers expect (case_09, 23, 29, 30). Turn it off with
  `LINK_FROM_LATER_FILES = False` if the orchestrator reads "respect audit_at" more strictly.
- Retention / legal-hold expiry: an expired retention makes the rule's condition fail -> no step. No
  record on the shipped data has an expired date at its case's audit_at.

## Custom evaluation cases (`python code/evaluation/custom_cases.py`)

```
PASS  a_history_email        COMPLETE        steps=2   identity resolved only through the audit log; evidence looked the account up by the old email
PASS  b_tombstone_pii        INCOMPLETE      steps=4   ACCOUNT_TOMBSTONE rule is REDACT: keep the user_id reference, strip name/email/phone
PASS  c_order_flow_restore   INCOMPLETE      steps=10  recreation paths are closed before the dependent record is deleted
PASS  d_replica_only         INCOMPLETE      steps=3   a copy surviving only in the failover replica is still a remaining record
PASS  e1_audit_before        COMPLETE        steps=1   the 2026-09-20 re-index file is after audit_at and must not change the answer
PASS  e2_audit_after         INCOMPLETE      steps=4   same evidence, later audit_at: the re-indexed entry is now in scope
PASS  f_marketing_only       NOT_APPLICABLE  steps=0   marketing opt-out is not a deletion request
PASS  g_negated_deletion     NOT_APPLICABLE  steps=0   negated deletion vocabulary
PASS  h_export_link          INCOMPLETE      steps=4   mentions export / download but asks for deletion of the export file
9/9 custom cases pass
```
(h) is an "uncertain" text for the intent rules; offline it falls back to deletion (deletion
vocabulary, no keep signal); with an API key the LLM pass decides it.

## Final metrics (one run after the last code change)

`python code/main.py --cases sample --offline` then
`python code/evaluation/main.py --predictions .cache/runs/sample_output.csv --truth dataset/sample_output.csv --subset all`

```
== dev (24 cases) ==
  Status accuracy                              1.000
  Exact case match (status + all steps)        0.958
  Step precision                               1.000
  Step recall                                  0.993
  Step F1 (pooled)                             0.997
  Step F1 (mean per case)                      0.998
  Target recall (system/resource/operation)    0.993
  State accuracy on matched targets            1.000
  Evidence-id accuracy on matched targets      1.000
  Positional field accuracy                    0.980
  Exact sequence accuracy                      0.958
  Mean LCS ratio                               0.997
  not exact (1): case_18

== holdout (8 cases) ==
  Status accuracy                              1.000
  Exact case match (status + all steps)        0.625
  Step precision                               0.927
  Step recall                                  0.864
  Step F1 (pooled)                             0.894
  Step F1 (mean per case)                      0.792
  Target recall (system/resource/operation)    0.864
  State accuracy on matched targets            1.000
  Evidence-id accuracy on matched targets      1.000
  Positional field accuracy                    0.875
  Exact sequence accuracy                      0.625
  Mean LCS ratio                               0.784
  not exact (3): case_04, case_12, case_20

== all (32 cases) ==
  Status accuracy 1.000, exact 0.875, step F1 (pooled) 0.974
```

Holdout is clearly worse than dev on exact match (5 / 8 against 23 / 24): status is right on all 8 but
three answers have missing or extra steps. The holdout answers were not inspected and nothing was tuned
on them, so this gap is the honest estimate of how much of the dev score is fitted to the dev cases.
