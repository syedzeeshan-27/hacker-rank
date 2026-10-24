# Token usage and cost report

This report covers the single cold run that produced `output.csv` at the repository root. The numbers are recorded by the code itself (`code/ghost/llm.py` appends one line per model call to `.cache/usage.jsonl`); nothing here is estimated by hand. Prices are Anthropic list prices per million tokens from `code/config.json`.

## Run

- Run id: `20261024T235156Z`
- Generated: 2026-10-24 23:53 UTC
- Requests processed: 518
- Wall-clock time: 81.8 s
- Statuses: COMPLETE 154, INCOMPLETE 354, NOT_APPLICABLE 10
- Provider: Anthropic (Claude API)
- Where the model is used: reading screenshots (PNG) and PDFs that have no text layer, and classifying whether each distinct request text asks for deletion. Every other step is plain code.

## Overall totals

| Metric | Value |
|---|---|
| Model calls | 11 |
| Cache hits (no API call) | 0 |
| Input tokens | 40,781 |
| Output tokens | 13,289 |
| Total tokens | 54,070 |
| Average tokens per request | 104.4 |
| Average model calls per request | 0.021 |
| Estimated total cost | $0.2145 |
| Estimated cost per request | $0.000414 |

## Per model

| Provider | Model | Calls | Cache hits | Input tokens | Output tokens | Cost (USD) | Price in/out per MTok |
|---|---|---|---|---|---|---|---|
| anthropic | `claude-sonnet-5-5` | 11 | 0 | 40,781 | 13,289 | $0.2145 | $2.0 / $10.0 |

## Per purpose

| Model | Purpose | Calls | Input tokens | Output tokens | Cost (USD) |
|---|---|---|---|---|---|
| `claude-sonnet-5-5` | evidence_image_extraction | 11 | 40,781 | 13,289 | $0.2145 |

## How to reproduce

```
python code/main.py --clear-cache          # cold run, re-reads every file, re-calls the model
python code/evaluation/usage_report.py     # rebuilds this file from that run's ledger
```

A warm re-run (without `--clear-cache`) makes zero model calls because every extraction is cached by file hash and prompt hash.
## Notes

- The 11 calls are one vision call per PNG screenshot in `dataset/evidence/images/`. The four PDFs have a
  text layer and were parsed without a model. All 375 distinct request texts were settled by the
  deterministic rules in `code/ghost/intent.py`, so the text-classification fallback made no call in this run.
- Every call's answer is cached under `.cache/llm/` keyed by model, prompt and file hash; a warm re-run
  makes zero calls and gives byte-identical output.
- A second, identical cold run earlier in the same session cost $0.2101 with the same 11 calls; vision
  output length varies slightly between cold runs, hence the small difference.
