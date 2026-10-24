# WS-B: evidence extraction notes

Code: `code/ghost/evidence/` (`registry.py`, `common.py`, `sql_snapshots.py`, `json_records.py`,
`logs.py`, `text_records.py`, `pdf_docs.py`, `images.py`). Tests: `code/tests/test_evidence.py`
(36 cases, all passing). Validation report: `python -m ghost.evidence.registry` from `code/`.

## Entry points

- `extract_all(manifest, llm) -> list[Observation]`
- `extract_all_detailed(manifest, llm, *, dataset_dir=None, write_flags=True) -> ExtractionResult`
  - `observations`: manifest order, then row / line order inside each file (deterministic)
  - `coverage: list[Coverage(evidence_id, captured_at, system_id, scope, resource_ids, kind)]`
  - `injection_flags: {file_id: snippet}` (also written to `.cache/injection_flags.json`)
  - `skipped: [file_id]` (LLM unavailable, or a file that failed to parse)
  - extra fields: `errors {file_id: message}`, `llm_files [file_id]` (facts that came from the model)
- The parser is picked by file extension only. No behaviour keys on file_id, file name or manifest origin
  (origin is only used as a fallback system-name hint for PDFs / screenshots).

## Offline run (no API key), full manifest

| | count |
|---|---|
| observations | 2903 (sql 891, json 890, log 1072, txt 41, pdf 9, png 0) |
| with at least one identifier | 2650 |
| without resource_id | 7 (6 legal-register rows, 1 customer statement; see "external refs") |
| coverage entries | 471 |
| states | PRESENT 2021, ABSENT 245, ACTIVE 226, ANONYMIZED 215, REDACTED 71, BLOCKED 62, DISABLED 62, UNKNOWN 1 |
| systems | account 664, security 663, search 486, analytics 337, support 315, integration 243, backup 107, export 52, identity 18, legal 18 |
| zero-output files | the 11 PNGs (skipped, no key) and `evidence_90` (inventory snapshot whose tables are all empty; it still yields an `account_service` coverage entry with no ids, which is the useful signal) |
| injection flags | none in the text formats (screenshots not yet read) |

Public-sample spot checks pass (content-based, nothing special-cased): the 2026-09-15 service export
gives account_5684 ABSENT (404 / profile null), analytics_4566 ANONYMIZED, support_4715 REDACTED,
data_flow_6244 DISABLED, backup_3120 BLOCKED, audit_record_5416 PRESENT (the last four carry user_26);
the handover txt links novak.tomasz@icloud.com to account_4248 ABSENT, search_052 / analytics_2034 /
support_864 PRESENT, data_flow_9522 ACTIVE.

## Per format

### SQL (24 files) - `sql_snapshots.py`
- `sqlite3` in memory, `executescript`, every user table, one Observation per row. `extra`: `table`,
  `copy_role` (primary / read_replica / reporting_replica / failover_replica / regional_replica:<region>),
  `topology`, `collection`, `updated_at`, `row`.
- Generic tables (`resource_id, origin_system, collection_name, record_json`): record_json is merged
  into the row; origin_system / collection_name are container names, not record fields.
- Typed tables (`customer_profiles`, `support_records`, `connector_subscriptions`,
  `backup_restore_entries`, `security_audit_records`, ...): the table name gives system / category.
- Capture time: `snapshot_scope.captured_at` for the row's system, else `-- Captured at:` header,
  else manifest. `updated_at` is a last-write time, kept in extra, never used as capture time.
- Coverage: typed table -> one `snapshot` entry (skipped when `snapshot_scope` exists and does not
  list that system). Generic tables -> one entry per (copy table x (system, collection) pair seen in
  any copy of the same file); a pair missing from one copy is recorded as covered with zero ids
  (e.g. read-replica files: `customer_data_primary/customer_profiles` = [], replica = [account_994]).

### JSON (28 files) - `json_records.py`
- Generic walker: every dict with `resource_id` is a record; context above it (captured_at /
  exported_at, data_store, collection, service key under `service_exports`, list key, source / origin)
  feeds system, category and capture time. Nested dicts with the same resource_id (search `hits`)
  are flattened into the record; a different resource_id becomes its own record.
- Shapes handled: snapshot bundles (incl. `operational_record_history` lookup slices whose rows carry
  their own `system` / `record_type` / `result` / `recorded_at`), `service_resource_export`
  (status_code 200/404, `profile: null`, `hits.total`), lookup ledgers (connector_configurations,
  export manifest, security audit ledger), single-collection reports (analytics report, export jobs).
- Capture time: record `recorded_at` / `observed_at`, else the innermost snapshot `captured_at`,
  else `exported_at`; clamped to the manifest time.
- Coverage: snapshot collections -> `snapshot` (an empty `records: []` gives an empty set);
  `service_exports.<svc>.<list>` -> `lookup`; ledger lists (system+record_type+result rows spanning many
  systems) -> no coverage.

### Logs (26 files) - `logs.py`
- One line parser for every format seen: key=value, `{json}` after the timestamp, syslog
  (`<pri>1 host service trace - -`), bracket (`[LEVEL] [service] [trace] event:`), pipe
  (`LEVEL | service | trace | event |`), cloud (`requestId=... function=... message="k=\"v\""`).
  1336 of 1336 lines that mention a resource_id parse it correctly.
- Correlation by trace id: `*_started` lines give the lookup key (`lookup_key`, `match_key`,
  `subject_key`, `scan_key`, `owner_key`, `subject_ref`); the outcome line gives the resource and the
  result. Lookup keys that are themselves resource ids (`lookup_key=analytics_257`) are not identifiers.
- One Observation per outcome line naming a resource (started / connection / cache / dependency /
  export lines are skipped). Capture time = line timestamp (clamped to manifest). `extra`: `event`,
  `service`, `line`, `line_ts`, `trace_id`, `trace_<key>`.
- Outcomes: rows=0 / http 404 / result=miss / marker_found=true -> ABSENT; rows>=1 / 200 / result=hit
  -> PRESENT unless the line shows the record's identifier fields all empty (then REDACTED with a user
  ref, ANONYMIZED without); `*_pii_scan` with `matched_value` -> PRESENT; `direct_identifiers=0` with
  null fields -> ANONYMIZED; `message_read message_text=[REDACTED] subject_ref=user_N` -> REDACTED;
  connector `enabled` / `scheduler_registered=false` / heartbeat; backup `restore_allowed`,
  `decision=deny` -> BLOCKED, `eligible=true deletion_block=false` -> ACTIVE; `retained_order_loaded`
  -> PRESENT; error events (`thumbnail_failed`) -> UNKNOWN.

### TXT (1 file) - `text_records.py`
- Pipe lines `ts | system | resource | CATEGORY | STATUS | identifier`; columns recognised by content.
  `no direct identifier` is not an identifier. `Snapshot:` header is the document time.
  Free-form lines naming a resource id become UNKNOWN observations.

### PDF (4 files) - `pdf_docs.py`
- All 4 PDFs have a usable text layer; none needs the LLM. Text is repaired ("10\n24 UTC" -> "10:24 UTC"),
  split into `Record` blocks and pipe table rows.
- Table rows -> one observation each (legal register: `ORD-... | user_08 | ... | Retain until ...`,
  category LEGAL_HOLD_RECORD, `retention_until` in extra, resource_id "" + `external_refs`).
- Resource ids in the text -> one observation per id (support_001 in the delivery-correction
  attachment: SUPPORT_ATTACHMENT PRESENT with name / email / phone; audit_record_003 in the
  compliance extract: SECURITY_AUDIT_LOG PRESENT, user_03, retention date).
- No resource id and no rows -> one document observation with resource_id "" (customer statement:
  user_18, email, name, `external_refs` [NST-2026-0718-1842, ORD-20261260]).
- Only if the text layer is empty (< 40 non-space chars) is `llm.complete_with_pdf` called.

### PNG (11 files) - `images.py`
- Always `llm.complete_with_image` with `prompts/image_extraction.md` (refined: the page's main subject
  and open detail panel are records too; order / ticket numbers and file names go into `fields`;
  masked values are copied as shown; `suspicious_text` for instructions). Reply parsed with
  `extract_json`, mapped by `map_llm_payload` (unit-tested with a hand-written reply) through the same
  `build_observation` rules. Image `status` words: `Restore allowed` -> ACTIVE, `Restore blocked`
  -> BLOCKED, etc.
- API key: **not available** (checked at start and end: `.env` has no ANTHROPIC_API_KEY;
  `.cache/usage.jsonl` does not exist). All 11 PNGs are in `skipped`; no cost incurred. When a key
  is added, the first run makes 11 vision calls (cached afterwards).

## State inference (`common.derive_state`)

Order of precedence; the extractor records what the file shows, never what should have happened.
1. Absence: status / result / status_code / http_status in {NOT_FOUND, 404, ABSENT, MISSING, miss, ...},
   rows=0, hits.total=0, `profile: null`, marker_found=true -> ABSENT.
2. DATA_FLOW: `enabled` -> ACTIVE / DISABLED; else `scheduler_registered=false`, status / delivery words
   (stopped, paused -> DISABLED). BACKUP_ARCHIVE: `restore_allowed` -> ACTIVE / BLOCKED; else
   deletion_block, decision deny/allow, eligible. Otherwise UNKNOWN.
3. Explicit REDACTED / ANONYMIZED words; `direct_identifiers>0` -> PRESENT; a lookup that FOUND /
   IDENTIFIABLE / hit / rows>=1 the record and shows no identifier fields of its own -> PRESENT.
4. Identifier analysis on the record's own fields: any email / phone / hash / name -> PRESENT;
   record kind without identifier fields (audit events, legal holds) -> PRESENT; identifier fields
   present but empty / null / masked with a user reference left -> REDACTED; no user reference -> ANONYMIZED.
- Tombstones: `account_status: DELETED` is not an absence marker (the tombstone exists); state comes
  from step 4 (all-null + subject_reference -> REDACTED; identifiers present -> PRESENT).
- USER_PROFILE `account_status` (ACTIVE, DELETION_PENDING) is kept in `extra.status` and does not change
  the state; a DELETION_PENDING profile that still has an email is PRESENT.
- Raw status words are always in `extra["status"]`.

## Identifiers

- Classified by value shape, any key: email (lower-cased; uppercase emails are common), `user_N`,
  64-hex hash (not integrity seals / checksums), phone (`models.norm_phone`), names from name-like keys
  only. Free-text fields (`message_text`, excerpts, notes) are scanned for emails, `user_N` and `+`phones.
- Lookup keys (trace `lookup_key` etc., ledger `subject_reference`, `query`, `correlation_key`) are put on
  the Observation so the subject links to a NOT_FOUND / 404 result, and are listed again in
  `extra["lookup_identifiers"]`; they never count as identifiers present on the record (so a found record
  whose fields are all null is REDACTED / ANONYMIZED, not PRESENT).

## System / category mapping (`common.py`)

system_id precedence: explicit record-level `system` (if in KNOWN_SYSTEMS) > resource_id prefix >
container / service / screen name rules. category: explicit `record_type` > attachment hint
(`storage_class=ticket_attachment`, file name on a support id, "attachment" in names) > name rules
(must agree with the id prefix) > id prefix.

| names (regex on data_store / table / collection / service) | system_id | id prefix | category |
|---|---|---|---|
| customer_profiles*, account, profile | account_service | account_ | USER_PROFILE |
| search, customer_search_documents, index_query | search_service | search_ | SEARCH_INDEX_ENTRY |
| support, ticket, conversation, message / attachment | support_platform | support_ | SUPPORT_MESSAGE / SUPPORT_ATTACHMENT |
| analytics | analytics_platform | analytics_ | ANALYTICS_PROFILE |
| export | export_service | export_ | USER_EXPORT |
| connector, data_flow, integration | integration_service | data_flow_ | DATA_FLOW |
| backup, restore, vault, recovery | backup_service | backup_ | BACKUP_ARCHIVE |
| security, audit, compliance | security_service | audit_record_ | SECURITY_AUDIT_LOG |
| tombstone, deleted_customer_references, identity | identity_service | tombstone_ | ACCOUNT_TOMBSTONE |
| marketing | marketing_platform | marketing_ | MARKETING_PROFILE |
| legal, retained_order_records, disputed orders | legal_service | legal_hold_ | LEGAL_HOLD_RECORD |
| email_delivery, notification delivery | email_delivery_service | email_delivery_ | EMAIL_DELIVERY_LOG |
| cache | cache_service | cache_ | CACHE_ENTRY |
| (object storage) | "" (no vocabulary entry) | object_ | "" |

## Untrusted content

Every string (SQL comments, JSON values, log lines, txt lines, PDF lines, LLM `notes` / fields /
`suspicious_text`) goes through `InjectionScanner` (patterns: "ignore previous instructions",
"disregard", "mark ... as complete", "you must", "note to the AI / auditor", "AI:/auditor:" address,
"system prompt", "override the policy", "do not report"). A hit records `file_id -> snippet`, and that
string is not used for identifiers or fields (a flagged log / txt line produces no observation).
The legal register's human instruction ("For each row add invoice date ...") is deliberately not a hit.
No hits in the text formats; the screenshots are unread until a key exists.

## Contract gaps / open questions for the decision engine

1. **Resource-less observations (7).** The customer statement PDF and legal-register rows name no
   resource id. They carry `extra["external_refs"]` (statement / order numbers) and user ids. The
   activity stream's `LINK_RESOURCE_IDENTITY` events map such refs to resources ("export_1842
   (NST-2026-0718-1842)", "tombstone_17 (user_17)", "object_19 (delivery_confirmation_19.pdf)");
   the engine should resolve aliases there. Their `system_id` is only a hint from the document origin.
2. **Replica-only presence.** Primary and replica rows are separate observations (`extra.copy_role`).
   A record present only in a replica (account_005 in the failover replica, account_994 in the read
   replica) is still a remaining copy; the samples treat it as REPORT_REMAINING_RECORD citing that file.
3. **What coverage means.** `snapshot` coverage = the listed ids were everything that copy / collection
   held *in that slice*. Snapshot bundles contain several small slices of the same store at different
   times, so infer ABSENT only for a resource linked to the subject, from a covering snapshot of the same
   system captured after the last presence and at or before audit_at. `lookup` coverage (service
   exports) only covers what was asked; prefer the explicit 404 / null observation there. Logs, txt and
   ledgers give explicit per-resource results and no coverage.
4. **Lookup-only links.** A NOT_FOUND / 404 observation carries the looked-up email as an identifier
   (needed to link the subject); `extra.lookup_identifiers` tells the engine it was a search key, not a
   field on a record.
5. **Analytics "REDACTED".** An analytics row with user_id but no direct identifiers is REDACTED here
   (still attributable); policy wants ANONYMIZED, so the engine should treat it as exposed / not yet
   anonymized.
6. **object_storage / support attachments.** `object_N` resources (attachment objects) have no system in
   KNOWN_SYSTEMS; the engine decides whether they map to support_platform.
7. **Screenshots.** Their facts are missing until a key is configured; a backup console row may show an
   archive and an account but no restore permission (state UNKNOWN), so it links identities rather than
   proving a state.
