# WS-C notes: identity resolution, request intent, activity stream

Files: `code/ghost/identity.py`, `code/ghost/intent.py`, `code/ghost/activity.py`,
`code/prompts/request_intent.md`, tests in `code/tests/test_identity.py`, `test_intent.py`, `test_activity.py`
(36 tests, all passing). Hand check of intent labels: `code/evaluation/notes/intent_review.md`.
No personal data is reproduced here.

## 1. Identity resolution

Data shape: 850 identity rows (unique user_id, unique current email), 306 audit rows
(107 email, 107 email_hash, 92 phone changes). No historical email belongs to two users, and no historical
email equals another user's current email, so the tie-break rule never fires on the shipped data.

Rule (`resolve_subject`):
1. Normalize the request email (`models.norm_email`).
2. If it equals a current snapshot email, that user wins.
3. Else, if it appears as `old_value` or `new_value` of an `email` audit event, the user of the most recent such
   event wins (ties: smallest user_id, natural sort).
4. Else `None`.

The Subject holds every email / phone / email_hash the user ever had (snapshot plus all audit old/new values,
regardless of `as_of`, because retired identifiers still sit in old evidence), the raw name and its
`norm_name` form, and `audit_refs` value -> audit_id of the earliest audit event mentioning that value.
The index is built once per (identities, audit_log) list pair and cached at module level.
`identifier_timeline(user_id, ...)` gives the ordered change history if a caller needs "current at time T".

Measured on `deletion_requests.csv` (518 cases, 517 distinct emails):
- resolved: 518 / 518
- matched through the current snapshot: 418
- matched only through audit history: 100
- unresolved: 0

`sample_deletion_requests.csv` (32): 29 current, 3 history-only, 0 unresolved. 28 sample subjects have one
email, 3 have two, 1 has three.

## 2. Request intent

The 375 distinct request texts in `deletion_requests.csv` are templated:
- 360 English deletion texts, each `<opener> <procedural sentence> <confirmation sentence>`, with 12 opener
  sentences, all explicit ("Please permanently delete the account...", "I am asking you to remove my account...").
- 5 non-English deletion texts (fr, pt, es, ja, ko), 62 cases in total. These are the only languages present.
- 10 non-deletion texts, one case each (case_533 to case_542, all `reported_status = NOT_STARTED`):
  marketing opt-out (2), export / copy (2), invoice copy, access request, correction (2), change email, login help.
  Each says to keep the account or not to delete.

Rule pass (per distinct text, NFKC + casefold, curly quotes straightened):
- keep signal (do not delete/close, not asking for deletion, not a request to erase, keep / leave the account
  active / as it is / in place, continue using, still use it, without removing, "marketing ... only", plus
  es/fr/de/pt/it/nl/ru/ja/zh/ko negations) -> NOT_DELETION. Always wins, also over an LLM vote.
- deletion vocabulary (delete / erase / purge / wipe, remove-or-close + account/profile/personal data/records,
  "account ... removed / is gone", remove + search result / copies / export file, and multilingual verbs:
  suppression/supprimer/effacer, eliminar/borrar, exclusão/excluir/apagar, löschen/entfernen,
  cancellare/eliminare, verwijderen, usunąć, удалить, 削除/消去/退会/删除/注销, 삭제/탈퇴) with no other-request
  signal -> DELETION.
- other-request signal (export, download, portable, "need/send a copy of", what information you hold,
  correction / misspelled / out of date, change email/phone/name, reset / password / login, unsubscribe /
  marketing / promotional / newsletter, invoices / receipts) with no deletion vocabulary -> NOT_DELETION.
- anything else (mixed deletion + other signal, or neither) -> uncertain -> LLM.

Bug avoided: the deletion filler "Please apply the correct treatment to records" first matched a bare
`correct` pattern and sent 28 texts to "uncertain". `correct` now needs a correction object
(correct it / my / the name ...).

Counts (offline, no API key):
- distinct texts: 365 DELETION, 10 NOT_DELETION, 0 uncertain, all `method="rule"`
- cases: 508 DELETION, 10 NOT_DELETION
- sample set: 31 rule DELETION, 1 uncertain (deletion verb + "download your data" / "export file"), offline
  fallback DELETION, which matches the sample answer
- injection_suspected: 0 texts in either file

LLM path: uncertain texts are batched (`CONFIG["intent_batch_size"]` = 25 distinct texts) as
`<id>: <JSON-encoded text>` lines, sent through `llm.complete_text(purpose="request_intent")` with
`prompts/request_intent.md`, parsed with `extract_json`, and mapped back to every case with that text.
Missing / malformed items, an unparseable reply, or `LLMUnavailable` fall back to the rule result with
`method="rule-fallback"` (DELETION only if deletion vocabulary is present). The JSON mapping is unit-tested
with hand-written replies. No API key in `.env` at the time of writing, so the LLM pass was not run and there is
no cost entry in `.cache/usage.jsonl` from WS-C. On this dataset the rules settle every text, so a run with a key
would still make 0 intent calls.

`injection_suspected` is set when the text addresses the auditor / AI / system, asks to ignore policy or
instructions, asks to mark / report the case as complete or not applicable, names output fields, or asks to
skip verification (rule patterns, OR-ed with the LLM's `injection` flag). It never changes the label by itself.

Cross-check: the activity stream has exactly 10 `CLASSIFY_REQUEST` rows ("classified request for account_N as
non-deletion"), matching the 10 non-deletion texts. They are tied to account ids, not emails, so they cannot be
joined to cases from the stream alone.

## 3. Activity stream (`system_activity.csv`, 4011 rows)

What `details` contains, per event type:
- Only the 12 `LINK_RESOURCE_IDENTITY` rows (activity_4000 to activity_4011) name an email and a user_id.
  Each email resolves to the same user_id the row names. They link account / search / data_flow / analytics /
  export (2) / support / backup / object_19 (object_storage, "delivery_confirmation_19.pdf") / marketing_001 /
  audit_record_003 / tombstone_17 to users 03..22. Some carry an alias in parentheses (export_1842 ->
  `NST-2026-0718-1842`, tombstone_17 -> user_17, object_19 -> the PDF name), kept in `extra["alias"]` for
  matching evidence files.
- `SYNC_RECORD` on `data_flow_*` (31, "Active connector repopulated downstream resource search_N") and
  `RESTORE_BACKUP` (1, backup_9287 -> search_4882) name a second resource. These give data_flow / backup ->
  search links.
- Every other row names only its own resource id plus a fixed sentence: DELETE / REDACT / ANONYMIZE ("submitted",
  "received the final result", "Policy action completed"), CHECK_DATA_FLOW ("remains enabled", 89 rows),
  CHECK_RESTORE_ACCESS ("can still be restored", 28), DISABLE_DATA_FLOW (36) and BLOCK_RESTORE (36, all
  "Policy action completed", all SUCCESS), RETAIN_RECORD on audit_record_* (196, "Security-event archival
  processed", no retention dates) and legal_hold_* (10), and routine UPDATE / INDEX / REFRESH_CACHE / SEND_EMAIL /
  CREATE_EXPORT rows. No counts, no retention_until values.

So the stream ties to a subject only through resource ids. Deletion runs of different cases interleave in time,
so rows cannot be grouped into a case by time or id order. `observations_for(subject, as_of, resources=...)`
therefore takes resource ids that evidence files already tie to the subject; it adds LINK rows for the subject
and one hop of data_flow / backup <-> downstream links.

Sample subjects (32 sample cases, using the sample answers only to check what the stream means):
- No sample answer cites an activity_id; every cited evidence_id is a file_id. Activity rows are supporting hints.
- 31 of the 32 sample account resources, and most search / analytics / support / data_flow / backup resources,
  have activity rows, all at or before audit_at. None of the sample `audit_record_*` ids appear in the stream.
- Data flows and backups agree with the sample answers every time: CHECK_DATA_FLOW "remains enabled" (6) and
  CHECK_RESTORE_ACCESS "can still be restored" (3) sit on resources with a REPORT_ACTIVE_DATA_FLOW /
  REPORT_RESTORE_RISK finding. DISABLE_DATA_FLOW (3) and BLOCK_RESTORE (3) sit on COMPLETE cases with
  VERIFY_*_DISABLED / BLOCKED.
- `reported_result` on DELETE_RECORD is unreliable: 9 FAILED deletes were in fact fine, and 5 SUCCESS deletes
  left the record present. ANONYMIZE / REDACT results matched the sample answer in these 22 rows, but they are
  still claims.
- An account `SYNC_RECORD` to `account_replication_cluster` / `account_failover_replica` after the delete
  shows up exactly on the 3 sample accounts reported as remaining (case_05, case_16, case_24), and on none of
  the other 29. It is emitted as an UNKNOWN hint with `extra["replica"]`.
- LINK rows touch two sample subjects (object_19 for case_07, tombstone_17 for case_17), and neither resource
  is in that case's sample answer, so a LINK row on its own does not make a resource relevant.

Observation mapping (`evidence_id = activity_id`, only `event_time <= as_of`, sorted by
(event_time, activity_id, resource_id)):
- DISABLE_DATA_FLOW SUCCESS -> integration_service DISABLED; BLOCK_RESTORE SUCCESS -> backup_service BLOCKED
- CHECK_DATA_FLOW -> ACTIVE ("enabled") / DISABLED ("disabled", "paused"); CHECK_RESTORE_ACCESS -> ACTIVE
  ("can still be restored", "allowed") / BLOCKED ("blocked", "denied")
- RESTORE_BACKUP -> backup ACTIVE with `extra["downstream"]`
- RETAIN_RECORD SUCCESS -> PRESENT (security_service for audit_record_*, legal_service for legal_hold_*);
  other results -> UNKNOWN
- LINK_RESOURCE_IDENTITY -> UNKNOWN with identifiers, `claim="linked_to_subject"` and alias
- SYNC_RECORD, INDEX_RECORD, DELETE / REDACT / ANONYMIZE, CLASSIFY_REQUEST and routine rows -> UNKNOWN with
  `extra["claim"]`, because results are claims, not proof
- Systems come from the resource-id prefix (account_ -> account_service, search_ -> search_service,
  support_ -> support_platform, analytics_ -> analytics_platform, export_ -> export_service,
  backup_ -> backup_service, data_flow_ -> integration_service, audit_record_ -> security_service,
  tombstone_ -> identity_service, cache_ -> cache_service, email_delivery_ -> email_delivery_service,
  marketing_ -> marketing_platform, legal_hold_ -> legal_service). Assumption: `object_*` (object_storage
  attachment) -> support_platform / SUPPORT_ATTACHMENT. The raw systems are kept in `extra`.

`resources_linked_to(subject, as_of=None)` returns LINK-row resources for the subject's emails / user_id
plus one hop (data flows / backups that repopulated them, and resources a linked flow / backup repopulated).
On the real data only 14 of the 550 request + sample subjects have any LINK row.
