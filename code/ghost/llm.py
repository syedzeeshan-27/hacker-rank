"""Single door to the LLM.

Rules enforced here:
  * API key only from the environment (see config.py). No key -> LLMUnavailable, callers decide a fallback.
  * Every call is cached under .cache/llm/<sha256>.json keyed by (model, system prompt, user content).
    A file is therefore extracted once; re-runs make zero API calls.
  * Every real call is appended to .cache/usage.jsonl with tokens and list-price cost, so the
    usage report is produced by the code itself, from the run that produced output.csv.
  * Decisions are never made here. This returns text; deterministic code interprets it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import CACHE_DIR, CONFIG, anthropic_api_key, anthropic_base_url


class LLMUnavailable(RuntimeError):
    """Raised when a call is needed but no API key / client is available."""


class LLMClient:
    def __init__(self, cache_dir: Path | None = None, run_id: str | None = None, offline: bool = False):
        self.cache_dir = (cache_dir or CACHE_DIR) / "llm"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.usage_path = (cache_dir or CACHE_DIR) / "usage.jsonl"
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.offline = offline
        self._client = None
        self.calls = 0
        self.cache_hits = 0

    # -- client ---------------------------------------------------------------------------------
    def _get_client(self):
        if self._client is not None:
            return self._client
        if self.offline:
            raise LLMUnavailable("LLM disabled (--offline)")
        key = anthropic_api_key()
        if not key:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set (put it in .env at the repo root)")
        import anthropic  # imported lazily so offline runs do not need the SDK

        self._client = anthropic.Anthropic(api_key=key, base_url=anthropic_base_url(), max_retries=3)
        return self._client

    # -- public API -----------------------------------------------------------------------------
    def complete_text(self, *, purpose: str, system: str, user_text: str, kind: str = "text") -> str:
        """Plain text prompt. Returns the model's text."""
        content = [{"type": "text", "text": user_text}]
        return self._complete(purpose=purpose, system=system, content=content, kind=kind)

    def complete_with_image(self, *, purpose: str, system: str, user_text: str, image_path: Path) -> str:
        data = image_path.read_bytes()
        media = _media_type(image_path)
        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media, "data": base64.b64encode(data).decode("ascii")},
            },
            {"type": "text", "text": user_text},
        ]
        return self._complete(purpose=purpose, system=system, content=content, kind="vision",
                              cache_material=hashlib.sha256(data).hexdigest())

    def complete_with_pdf(self, *, purpose: str, system: str, user_text: str, pdf_path: Path) -> str:
        data = pdf_path.read_bytes()
        content = [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode("ascii")},
            },
            {"type": "text", "text": user_text},
        ]
        return self._complete(purpose=purpose, system=system, content=content, kind="vision",
                              cache_material=hashlib.sha256(data).hexdigest())

    # -- internals ------------------------------------------------------------------------------
    def _complete(self, *, purpose: str, system: str, content: list[dict], kind: str,
                  cache_material: Optional[str] = None) -> str:
        model = CONFIG["models"][kind]
        max_tokens = CONFIG["max_tokens"][kind]
        material = cache_material or json.dumps(content, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(f"{model}\n{system}\n{material}".encode("utf-8")).hexdigest()
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            self.cache_hits += 1
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            self._ledger(purpose, model, 0, 0, cache_hit=True)
            return cached["text"]

        client = self._get_client()
        started = time.time()
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        usage = resp.usage
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        self.calls += 1
        self._ledger(purpose, model, in_tok, out_tok, cache_hit=False, seconds=time.time() - started,
                     stop_reason=getattr(resp, "stop_reason", None))
        cache_file.write_text(json.dumps({
            "model": model,
            "purpose": purpose,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "text": text,
            "created_at": _now(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        return text

    def _ledger(self, purpose: str, model: str, in_tok: int, out_tok: int, *, cache_hit: bool,
                seconds: float = 0.0, stop_reason: Any = None) -> None:
        price = CONFIG["pricing_usd_per_mtok"].get(model, {"input": 0.0, "output": 0.0})
        cost = in_tok / 1e6 * price["input"] + out_tok / 1e6 * price["output"]
        row = {
            "ts": _now(),
            "run_id": self.run_id,
            "purpose": purpose,
            "provider": "anthropic",
            "model": model,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_usd": round(cost, 6),
            "cache_hit": cache_hit,
            "seconds": round(seconds, 2),
            "stop_reason": stop_reason,
        }
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------------------------
# Helpers for callers
# ---------------------------------------------------------------------------------------------

def extract_json(text: str) -> Any:
    """Pull the first JSON value out of a model reply (tolerates ``` fences and prose)."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # fall back: first {...} or [...] block
    for opener, closer in (("[", "]"), ("{", "}")):
        start = t.find(opener)
        end = t.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in model reply")


def usage_summary(usage_path: Path | None = None, run_id: str | None = None) -> dict:
    """Aggregate the ledger (optionally one run_id) per model and overall."""
    path = usage_path or (CACHE_DIR / "usage.jsonl")
    per_model: dict[str, dict] = {}
    total = {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    if not path.exists():
        return {"per_model": per_model, "total": total}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if run_id and row.get("run_id") != run_id:
            continue
        m = per_model.setdefault(row["model"], {"provider": row.get("provider", "anthropic"), "calls": 0,
                                                 "cache_hits": 0, "input_tokens": 0, "output_tokens": 0,
                                                 "cost_usd": 0.0, "purposes": {}})
        bucket = "cache_hits" if row.get("cache_hit") else "calls"
        m[bucket] += 1
        total[bucket] += 1
        if not row.get("cache_hit"):
            m["input_tokens"] += row["input_tokens"]
            m["output_tokens"] += row["output_tokens"]
            m["cost_usd"] += row["cost_usd"]
            total["input_tokens"] += row["input_tokens"]
            total["output_tokens"] += row["output_tokens"]
            total["cost_usd"] += row["cost_usd"]
            p = m["purposes"].setdefault(row["purpose"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
            p["calls"] += 1
            p["input_tokens"] += row["input_tokens"]
            p["output_tokens"] += row["output_tokens"]
            p["cost_usd"] += row["cost_usd"]
    return {"per_model": per_model, "total": total}


def _media_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".webp": "image/webp"}.get(ext, "application/octet-stream")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
