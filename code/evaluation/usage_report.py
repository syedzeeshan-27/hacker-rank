"""Generate code/evaluation/usage_report.md from the LLM usage ledger of one run.

The ledger (.cache/usage.jsonl) is written by ghost/llm.py for every model call. This script only
aggregates it; nothing here estimates tokens by hand.

    python code/evaluation/usage_report.py                 # uses .cache/last_run.json
    python code/evaluation/usage_report.py --run-id 20261025T120000Z --cases 518
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost.config import CACHE_DIR, CONFIG  # noqa: E402
from ghost.llm import usage_summary  # noqa: E402

REPORT_PATH = Path(__file__).resolve().parent / "usage_report.md"


def build_markdown(run_id: str, n_cases: int, meta: dict | None) -> str:
    s = usage_summary(run_id=run_id)
    total = s["total"]
    per_model = s["per_model"]
    n = max(n_cases, 1)
    lines: list[str] = []
    lines.append("# Token usage and cost report")
    lines.append("")
    lines.append("This report covers the single cold run that produced `output.csv` at the repository root. "
                 "The numbers are recorded by the code itself (`code/ghost/llm.py` appends one line per model "
                 "call to `.cache/usage.jsonl`); nothing here is estimated by hand. Prices are Anthropic list "
                 "prices per million tokens from `code/config.json`.")
    lines.append("")
    lines.append("## Run")
    lines.append("")
    lines.append(f"- Run id: `{run_id}`")
    lines.append(f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"- Requests processed: {n_cases}")
    if meta:
        lines.append(f"- Wall-clock time: {meta.get('seconds', '?')} s")
        st = meta.get("statuses", {})
        if st:
            lines.append(f"- Statuses: " + ", ".join(f"{k} {v}" for k, v in st.items()))
    lines.append("- Provider: Anthropic (Claude API)")
    lines.append("- Where the model is used: reading screenshots (PNG) and PDFs that have no text layer, and "
                 "classifying whether each distinct request text asks for deletion. Every other step is plain code.")
    lines.append("")
    lines.append("## Overall totals")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Model calls | {total['calls']} |")
    lines.append(f"| Cache hits (no API call) | {total['cache_hits']} |")
    lines.append(f"| Input tokens | {total['input_tokens']:,} |")
    lines.append(f"| Output tokens | {total['output_tokens']:,} |")
    tt = total["input_tokens"] + total["output_tokens"]
    lines.append(f"| Total tokens | {tt:,} |")
    lines.append(f"| Average tokens per request | {tt / n:,.1f} |")
    lines.append(f"| Average model calls per request | {total['calls'] / n:.3f} |")
    lines.append(f"| Estimated total cost | ${total['cost_usd']:.4f} |")
    lines.append(f"| Estimated cost per request | ${total['cost_usd'] / n:.6f} |")
    lines.append("")
    lines.append("## Per model")
    lines.append("")
    lines.append("| Provider | Model | Calls | Cache hits | Input tokens | Output tokens | Cost (USD) | Price in/out per MTok |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for model, m in sorted(per_model.items()):
        price = CONFIG["pricing_usd_per_mtok"].get(model, {"input": 0, "output": 0})
        lines.append(f"| {m['provider']} | `{model}` | {m['calls']} | {m['cache_hits']} | {m['input_tokens']:,} | "
                     f"{m['output_tokens']:,} | ${m['cost_usd']:.4f} | ${price['input']} / ${price['output']} |")
    if not per_model:
        lines.append("| - | - | 0 | 0 | 0 | 0 | $0.0000 | - |")
    lines.append("")
    lines.append("## Per purpose")
    lines.append("")
    lines.append("| Model | Purpose | Calls | Input tokens | Output tokens | Cost (USD) |")
    lines.append("|---|---|---|---|---|---|")
    any_purpose = False
    for model, m in sorted(per_model.items()):
        for purpose, p in sorted(m["purposes"].items()):
            any_purpose = True
            lines.append(f"| `{model}` | {purpose} | {p['calls']} | {p['input_tokens']:,} | {p['output_tokens']:,} | ${p['cost_usd']:.4f} |")
    if not any_purpose:
        lines.append("| - | - | 0 | 0 | 0 | $0.0000 |")
    lines.append("")
    lines.append("## How to reproduce")
    lines.append("")
    lines.append("```")
    lines.append("python code/main.py --clear-cache          # cold run, re-reads every file, re-calls the model")
    lines.append("python code/evaluation/usage_report.py     # rebuilds this file from that run's ledger")
    lines.append("```")
    lines.append("")
    lines.append("A warm re-run (without `--clear-cache`) makes zero model calls because every extraction is cached "
                 "by file hash and prompt hash.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", default=None)
    p.add_argument("--cases", type=int, default=None)
    p.add_argument("--out", default=str(REPORT_PATH))
    args = p.parse_args(argv)
    meta = None
    last = CACHE_DIR / "last_run.json"
    if last.exists():
        meta = json.loads(last.read_text(encoding="utf-8"))
    run_id = args.run_id or (meta or {}).get("run_id")
    if not run_id:
        raise SystemExit("no run id: pass --run-id or run code/main.py first")
    if not meta or meta.get("run_id") != run_id:
        per_run = CACHE_DIR / "runs" / f"run_{run_id}.json"
        meta = json.loads(per_run.read_text(encoding="utf-8")) if per_run.exists() else None
    n_cases = args.cases or (meta or {}).get("n_cases") or 0
    if not n_cases:
        raise SystemExit("unknown case count for this run: pass --cases N")
    md = build_markdown(run_id, n_cases, meta)
    Path(args.out).write_text(md, encoding="utf-8")
    print(f"wrote {args.out} for run {run_id} ({n_cases} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
