# Token usage and cost report

This report covers the single cold run that produced `output.csv` at the repository root. The numbers are recorded by the code itself (`code/ghost/llm.py` appends one line per model call to `.cache/usage.jsonl`); nothing here is estimated by hand. Prices are Anthropic list prices per million tokens from `code/config.json`.

## Run

- Run id: `20261025T004034Z`
- Generated: 2026-10-25 00:41 UTC
- Requests processed: 518
- Wall-clock time: 83.4 s
- Statuses: COMPLETE 153, INCOMPLETE 355, NOT_APPLICABLE 10
- Provider: Anthropic (Claude API)
- Where the model is used: reading screenshots (PNG) and PDFs that have no text layer, and classifying whether each distinct request text asks for deletion. Every other step is plain code.

## Overall totals

| Metric | Value |
|---|---|
| Model calls | 11 |
| Cache hits (no API call) | 0 |
| Input tokens | 40,781 |
| Output tokens | 13,252 |
| Total tokens | 54,033 |
| Average tokens per request | 104.3 |
| Average model calls per request | 0.021 |
| Estimated total cost | $0.2141 |
| Estimated cost per request | $0.000413 |

## Per model

| Provider | Model | Calls | Cache hits | Input tokens | Output tokens | Cost (USD) | Price in/out per MTok |
|---|---|---|---|---|---|---|---|
| anthropic | `claude-sonnet-5-5` | 11 | 0 | 40,781 | 13,252 | $0.2141 | $2.0 / $10.0 |

## Per purpose

| Model | Purpose | Calls | Input tokens | Output tokens | Cost (USD) |
|---|---|---|---|---|---|
| `claude-sonnet-5-5` | evidence_image_extraction | 11 | 40,781 | 13,252 | $0.2141 |

## How to reproduce

```
python code/main.py --clear-cache          # cold run, re-reads every file, re-calls the model
python code/evaluation/usage_report.py     # rebuilds this file from that run's ledger
```

A warm re-run (without `--clear-cache`) makes zero model calls because every extraction is cached by file hash and prompt hash.
