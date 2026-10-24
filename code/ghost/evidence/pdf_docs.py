"""PDF documents -> Observations.

Text layer first (pymupdf). The documents are rendered as "Record / Detail / Item" label-value blocks
and pipe-delimited table rows. Parsing:
  * table rows (>= 3 " | " cells)  -> one observation each (resource id in the row, else external refs)
  * resource ids in the text      -> one observation per id, with identifiers from the blocks that
                                     mention it (all document identifiers when the document names
                                     exactly one resource)
  * no resource id and no rows    -> one document-level observation with resource_id "" and the
                                     document's external references (statement / order / ticket numbers)
                                     so the decision engine can resolve it through catalog links
Only when there is no usable text layer is the PDF sent to the model (prompts/pdf_extraction.md).
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import prompt_text
from ..llm import LLMClient, extract_json
from ..models import EvidenceFile, Observation
from .common import (
    EXTERNAL_REF_RE,
    RESOURCE_RE,
    Idents,
    InjectionScanner,
    build_observation,
    resolve_category,
    scan_free_text,
    try_parse_ts,
)
from .images import map_llm_payload

MIN_TEXT_CHARS = 40
BLOCK_MARKERS = {"record", "detail", "item"}
PDF_USER_TEXT = ("Transcribe every record in this document into the JSON format described in the "
                 "system prompt. Return only JSON.")
LABELLED_TIME_RE = re.compile(
    r"\b(issued|generated|captured|recorded|received|submitted|retain until|retained until|order date)\s+"
    r"(\d{1,2}\s+[A-Za-z]{3}[a-z]*\s+\d{4}(?:\s+\d{1,2}:\d{2})?(?:\s*UTC)?)", re.I)
NAME_LINE_RE = re.compile(r"^(?:Customer|Requester|Account holder|Name)\s+([A-Z][^\d@|]{2,60})$")
BARE_NAME_RE = re.compile(r"^[A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+){1,3}$")


def read_text(path: Path) -> str:
    import pymupdf  # local import: only needed for PDFs

    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


def repair_text(text: str) -> str:
    # times are split across lines when the renderer drops the colon: "10\n24 UTC" -> "10:24 UTC"
    text = re.sub(r"(\d{1,2})\n(\d{2})\s*UTC", r"\1:\2 UTC", text)
    return text.replace("\r", "")


def _blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = [[]]
    for ln in lines:
        if ln.strip().lower() == "record":
            blocks.append([])
            continue
        if ln.strip().lower() in BLOCK_MARKERS:
            continue
        blocks[-1].append(ln.strip())
    return [b for b in blocks if b]


def _idents_from_lines(lines: list[str]) -> Idents:
    ids = Idents()
    for ln in lines:
        scan_free_text(ln, ids)
        m = NAME_LINE_RE.match(ln)
        if m:
            ids.names.add(" ".join(m.group(1).split()))
        elif BARE_NAME_RE.match(ln) and len(ln.split()) <= 4 and not re.search(
                r"(Request|Register|Statement|Shop|Journal|Record|Entry|Details|Notes|Order|Delivery|"
                r"Customer|Product|Charges|Timeline|Payment|Continued)", ln):
            ids.names.add(ln)
    return ids


def _times(text: str) -> dict:
    out: dict = {}
    for label, value in LABELLED_TIME_RE.findall(text):
        key = label.lower().replace(" ", "_")
        if key not in out and try_parse_ts(value):
            out[key] = value.strip()
    return out


def _obs_from(ef, rid, ids: Idents, lines: list[str], doc_title: str, times: dict, scanner, kind: str):
    rec: dict = {"resource_id": rid}
    if ids.emails:
        rec["email"] = ", ".join(sorted(ids.emails))
    if ids.phones:
        rec["phone"] = ", ".join(sorted(ids.phones))
    if ids.email_hashes:
        rec["email_hash"] = ", ".join(sorted(ids.email_hashes))
    if ids.user_ids:
        rec["subject_reference"] = ", ".join(sorted(ids.user_ids))
    if ids.names:
        rec["full_name"] = "; ".join(sorted(ids.names))
    for k in ("retain_until", "retained_until"):
        if k in times:
            rec["retention_until"] = times[k]
    joined = " ".join(lines)
    refs = sorted(set(EXTERNAL_REF_RE.findall(joined)))
    extra = {"format": "pdf", "document_title": doc_title[:120], "pdf_block": kind}
    if times:
        extra["document_times"] = times
    if refs:
        extra["external_refs"] = refs
    category_hint = resolve_category(rid, names=[doc_title, joined])
    obs = build_observation(rec, evidence_id=ef.file_id, manifest_at=ef.captured_at,
                            source_path=ef.file_path, container_time=None,
                            system_names=[ef.origin, doc_title], category_names=[category_hint, doc_title],
                            extra=extra, scanner=scanner)
    # build_observation splits identifiers into fields; names/phones joined above are re-split here
    if obs is not None:
        obs.emails |= ids.emails
        obs.phones |= ids.phones
        obs.email_hashes |= ids.email_hashes
        obs.user_ids |= ids.user_ids
        obs.names = set(ids.names)
        if ids.emails or ids.phones or ids.email_hashes or ids.names:
            if obs.state_hint in ("REDACTED", "ANONYMIZED", "UNKNOWN"):
                obs.state_hint = "PRESENT"
    return obs


def parse_pdf_text(ef: EvidenceFile, text: str, scanner: InjectionScanner) -> list[Observation]:
    text = repair_text(text)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    clean_lines = [ln for ln in lines if not scanner.check(ef.file_id, ln)]
    doc_title = clean_lines[0].strip() if clean_lines else ""
    times = _times("\n".join(clean_lines))
    out: list[Observation] = []

    table_rows = [ln for ln in clean_lines if ln.count("|") >= 3]
    other = [ln for ln in clean_lines if ln.count("|") < 3]
    blocks = _blocks(other)

    for row in table_rows:
        ids = _idents_from_lines([c.strip() for c in row.split("|")])
        rids = RESOURCE_RE.findall(row)
        row_times = _times(row)
        for rid in (rids or [""]):
            obs = _obs_from(ef, rid, ids, [row], doc_title, {**times, **row_times}, scanner, "table_row")
            if obs is not None:
                out.append(obs)

    doc_rids = list(dict.fromkeys(RESOURCE_RE.findall("\n".join(other))))
    if doc_rids:
        for rid in doc_rids:
            if len(doc_rids) == 1:
                rel = other
            else:
                rel = [ln for b in blocks if any(rid in x for x in b) for ln in b]
            ids = _idents_from_lines(rel)
            obs = _obs_from(ef, rid, ids, rel, doc_title, times, scanner, "document")
            if obs is not None:
                out.append(obs)
    elif not table_rows:
        ids = _idents_from_lines(other)
        obs = _obs_from(ef, "", ids, other, doc_title, times, scanner, "document")
        if obs is not None:
            out.append(obs)
    return out


def extract(ef: EvidenceFile, path: Path, scanner: InjectionScanner, llm: LLMClient) -> tuple[list[Observation], bool]:
    """Returns (observations, used_llm)."""
    try:
        text = read_text(path)
    except Exception:  # broken / encrypted PDF: fall through to the model
        text = ""
    if len(re.sub(r"\s+", "", text)) >= MIN_TEXT_CHARS:
        return parse_pdf_text(ef, text, scanner), False
    reply = llm.complete_with_pdf(
        purpose="evidence_pdf_extraction",
        system=prompt_text("pdf_extraction.md"),
        user_text=PDF_USER_TEXT,
        pdf_path=path,
    )
    return map_llm_payload(extract_json(reply), ef, scanner, fmt="pdf"), True
