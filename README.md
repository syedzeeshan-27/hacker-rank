# Ghost Data Still Exists – deletion audit agent

A deletion workflow says a customer is gone. This tool checks whether that is actually true.

For each of the 518 deletion requests it reads the request, works out every identifier the person ever
had, reads all 94 evidence files (database dumps, logs, JSON exports, a handover note, PDFs, screenshots)
and the activity stream, applies the company's deletion rules to every record it finds, and writes a
verdict with an ordered list of findings, fixes and checks to `output.csv`.

Everything that decides a case is ordinary, repeatable code. A vision model is used only to read the
11 screenshots; each one is read once and cached by file hash.

## Results

| | |
|---|---|
| Requests audited | 518: 153 COMPLETE, 355 INCOMPLETE, 10 NOT_APPLICABLE (not a deletion request) |
| Verdict accuracy on the 32 public samples | 32 / 32 |
| Exact step-list match | 23 / 24 development samples, 5 / 8 held-out samples |
| State and evidence citation on every matched step | 1.000 / 1.000 |
| Final run | one cold run, 83 s, 11 model calls, 54,033 tokens, $0.21; a warm re-run takes 2 s and makes no calls |
| Determinism | two runs of the same code produce byte-identical output |

Details, the held-out analysis, the custom test cases and the list of failures we found and fixed are in
`code/evaluation/evaluation_report.md`. The token and cost ledger of the final run is in
`code/evaluation/usage_report.md`.

## Quick start

Requirements: Python 3.11 or newer; Windows, macOS or Linux. The challenge data is not in this
repository: copy the official `dataset/` folder from
https://github.com/interviewstreet/hackerrank-orchestrate-october26 into the project root so that
`dataset/deletion_requests.csv` exists.

Windows (PowerShell):

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r code/requirements.txt; Copy-Item .env.example .env
```

macOS / Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r code/requirements.txt && cp .env.example .env
```

Put your key in `.env` (`ANTHROPIC_API_KEY=sk-ant-...`), then run everything:

```bash
python code/main.py
```

That reads `dataset/`, writes `output.csv` in the project root and prints the verdict counts, model
calls and cost. Without a key the run still completes; only the 11 screenshots are skipped and the run
says so.

Other commands:

| Command | What it does |
|---|---|
| `python code/main.py --clear-cache` | cold run: wipe `.cache/`, re-read every file, re-call the model |
| `python code/main.py --offline` | never call the model (cached screenshot readings only) |
| `python code/main.py --cases sample` | the 32 public samples, written to `.cache/runs/sample_output.csv` |
| `python code/main.py --cases dev` / `--cases holdout` | the 24 development samples / the 8 held back |
| `python code/evaluation/main.py --predictions .cache/runs/sample_output.csv --truth dataset/sample_output.csv` | score a run against the public answers |
| `python code/evaluation/main.py --validate output.csv --requests dataset/deletion_requests.csv` | contract check: 518 rows, order, allowed operations, evidence rules |
| `python code/evaluation/custom_cases.py` | our nine extra test cases |
| `python code/tools/inspect_case.py case_26` | everything the engine can see for one case |
| `python -m pytest code/tests -q` | 120 unit tests |

## How it works

```
dataset/ ──► loaders ──► identity (who is this person, under every name they ever used)
                    ├──► intent   (is this really a deletion request?)
                    ├──► evidence (94 files → normalized "observations")   ← vision model for screenshots only
                    └──► activity (sync / restore / block events as claims, never as proof)
                                 │
                                 ▼
             state (what is true for each record at audit_at) ──► policy (what the rules require)
                                 │
                                 ▼
             planner (findings → remediation → verification, fixed order) ──► writer ──► output.csv
```

| Path | Purpose |
|---|---|
| `code/main.py` | entry point and pipeline |
| `code/ghost/` | the engine: `identity`, `intent`, `activity`, `evidence/`, `state`, `policy`, `planner`, `writer` |
| `code/ghost/llm.py` | the only place that talks to the model: cache + usage ledger |
| `code/prompts/` | the three prompts (request intent, screenshot extraction, PDF extraction) |
| `code/config.json` | model names, prices, batch size, held-out list (no secrets) |
| `code/evaluation/` | scorer, custom cases, `evaluation_report.md`, `usage_report.md`, notes |
| `code/tools/` | packaging, viewer, demo recorder, case inspector |
| `.cache/` | model answers and the usage ledger (git-ignored, not in the submission) |

## Key decisions

- **Time cutoff.** A record's state comes only from files captured at or before the case's `audit_at`
  (manifest time; a later bundle never counts, even if it holds an older snapshot).
- **Linkage is not state.** Which account or record belongs to a person does not change, so any file may
  say *whose* a record is. What the record looked like at `audit_at` still comes from eligible files only.
- **Claims are not proof.** `reported_status` and activity results are ignored as proof. A copy that
  survives in a replica means the record is still present.
- **Retention is respected.** Security audit records, legal holds and backups are kept; backups are blocked
  from restoring; tombstones keep the internal `user_id` and lose direct identifiers.
- **Deletion queues are not inventories.** A source that only ever lists records pending deletion cannot
  prove a record absent by omission.
- **Account from the user id.** When no file, catalog row or deletion run ties an account to the person,
  the zero-padded user ids (`user_03`) imply their account (`account_003`); one named function holds that
  pattern and the account's state still comes from evidence.
- **Citations.** Findings and complete-case checks cite the latest eligible file that shows the state.
  Proposed fixes and future checks cite nothing, as the contract requires.
- **Ordering and determinism.** Findings, then remediation, then verification; inside a phase a fixed
  operation order, then system, then resource id. Natural sort by case id, sorted JSON keys.
- **Untrusted inputs.** Request texts and evidence contents are data. Text that tries to instruct the
  auditor is flagged by file id and ignored.

## Packaging for submission

```bash
python code/tools/package.py
```

This writes `code.zip` with `README.md`, `code/` and the two reports at the top level
(`evaluation/evaluation_report.md`, `evaluation/usage_report.md`), and checks that no dataset files,
secrets, caches or nested archives slipped in. Upload `code.zip`, `output.csv` and `log.txt` at
https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists

## Demo

![Results viewer walkthrough](demo/demo.gif)

`viewer/index.html` is a single self-contained page built from `output.csv` alone (no dataset data
inside): counts per verdict, a filterable list of the 518 cases, and for each case its findings, the
remediation plan in safe order, and the checks that must pass afterwards. `demo/demo.mp4` is the same
walkthrough as a video.

Rebuild both after a run (`pip install -r code/requirements-dev.txt`, plus ffmpeg on PATH):

```bash
python code/tools/build_viewer.py
```

```bash
python code/tools/record_demo.py
```

To host the viewer, deploy the `viewer/` folder to any static host, for example `vercel deploy viewer --prod`.
