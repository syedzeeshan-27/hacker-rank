"""Screenshots (PNG/JPG) -> Observations via the vision model.

The model only transcribes (prompts/image_extraction.md). Deterministic code below maps its JSON to
Observations with the same rules as every other format. The reply is cached by LLMClient, so a file
is sent once. Without an API key LLMClient raises LLMUnavailable and the registry records the file as
skipped.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import prompt_text
from ..llm import LLMClient, extract_json
from ..models import EvidenceFile, Observation
from .common import (
    EXTERNAL_REF_RE,
    RESOURCE_RE,
    InjectionScanner,
    build_observation,
    try_parse_ts,
)

IMAGE_USER_TEXT = ("Transcribe every record visible in this screenshot into the JSON format described "
                   "in the system prompt. Return only JSON.")


def _norm_key(k: str) -> str:
    k = re.sub(r"[^0-9a-zA-Z]+", "_", str(k).strip().lower()).strip("_")
    return k or "field"


def _scalarize(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, list):
        return ", ".join(str(_scalarize(x)) for x in v)
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_scalarize(x)}" for k, x in v.items())
    return str(v)


def map_llm_payload(payload: Any, ef: EvidenceFile, scanner: InjectionScanner, *, fmt: str) -> list[Observation]:
    """JSON reply (image or pdf prompt shape) -> Observations. Pure function, unit-tested offline."""
    if isinstance(payload, list):
        payload = {"records": payload}
    if not isinstance(payload, dict):
        return []
    suspicious = payload.get("suspicious_text")
    if isinstance(suspicious, str) and suspicious.strip():
        scanner.flags.setdefault(ef.file_id, suspicious.strip()[:200])
    title = str(payload.get("screen_title") or payload.get("document_title") or "")
    page_time = payload.get("captured_at") or payload.get("document_date") or ""
    notes = payload.get("notes")
    if isinstance(notes, str):
        scanner.check(ef.file_id, notes)
    out: list[Observation] = []
    records = payload.get("records") or []
    if not isinstance(records, list):
        return []
    for idx, r in enumerate(records):
        if not isinstance(r, dict):
            continue
        fields = r.get("fields") if isinstance(r.get("fields"), dict) else {}
        rec: dict = {}
        for k, v in fields.items():
            nk = _norm_key(k)
            sv = _scalarize(v)
            if isinstance(sv, str) and scanner.check(ef.file_id, sv):
                continue
            rec[nk if nk not in rec else f"{nk}_{idx}"] = sv
        rid = str(r.get("resource_id") or "").strip()
        if not RESOURCE_RE.fullmatch(rid):
            # look for a resource id inside the transcribed values
            found = [m for v in [rid, *[str(x) for x in rec.values()]] for m in RESOURCE_RE.findall(str(v))]
            rid = found[0] if found else ""
        rec["resource_id"] = rid
        status = str(r.get("status") or "").strip()
        if status:
            rec["status"] = status
        system = str(r.get("system") or "").strip()
        rec_time = str(r.get("captured_at") or "").strip()
        # A record's creation / retention date is not the time the screen was captured: when the model
        # copied such a field into captured_at, fall back to the screen time (then the manifest time).
        dated_fields = {str(v).strip() for k, v in fields.items()
                        if re.search(r"creat|opened|since|retention|drill|expir|start", str(k), re.I)}
        if rec_time and rec_time not in dated_fields and try_parse_ts(rec_time):
            rec["captured_at"] = rec_time
        extra = {"format": fmt, "llm_record": idx}
        if title:
            extra["screen_title"] = title[:120]
        if status:
            extra["raw_status"] = status
        obs = build_observation(
            rec,
            evidence_id=ef.file_id,
            manifest_at=ef.captured_at,
            source_path=ef.file_path,
            container_time=page_time if try_parse_ts(str(page_time)) else None,
            system_names=[system, title, ef.origin],
            category_names=[system, title, status],
            extra=extra,
            scanner=scanner,
        )
        if obs is None:
            continue
        if not obs.resource_id:
            refs = sorted(set(obs.extra.get("external_refs", [])) |
                          {m for v in rec.values() if isinstance(v, str) for m in EXTERNAL_REF_RE.findall(v)})
            if refs:
                obs.extra["external_refs"] = refs
        out.append(obs)
    return out


def extract(ef: EvidenceFile, path: Path, scanner: InjectionScanner, llm: LLMClient) -> list[Observation]:
    reply = llm.complete_with_image(
        purpose="evidence_image_extraction",
        system=prompt_text("image_extraction.md"),
        user_text=IMAGE_USER_TEXT,
        image_path=path,
    )
    payload = extract_json(reply)
    return map_llm_payload(payload, ef, scanner, fmt="png")
