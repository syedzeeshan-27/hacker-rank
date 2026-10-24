"""Plain-text records -> Observations.

The handover format is a list of pipe-delimited check lines:
    - <ts> | <system> | <resource_id> | <CATEGORY> | <STATUS> | <identifier or 'no direct identifier'>
Columns are recognised by content (timestamp shape, resource-id shape, CATEGORY vocabulary, system
names), not by position alone, so reordered columns still parse. A "Snapshot:" / "Captured at:" line
gives the document time. Free-form lines that name a resource id become UNKNOWN-state observations
carrying whatever identifiers the line shows.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..models import CATEGORIES, EvidenceFile, Observation
from .common import (
    RESOURCE_RE,
    InjectionScanner,
    _name_system,
    build_observation,
    try_parse_ts,
)

DOC_TIME_RE = re.compile(r"^\s*(snapshot|captured at|as of|generated)\s*:\s*(.+)$", re.I)


def _parse_pipe_line(line: str) -> dict | None:
    parts = [p.strip() for p in line.strip().lstrip("-*• ").split("|")]
    if len(parts) < 3:
        return None
    rec: dict = {}
    rest: list[str] = []
    for p in parts:
        if not p:
            continue
        if "recorded_at" not in rec and try_parse_ts(p) and re.match(r"^\d{4}-\d{2}-\d{2}", p):
            rec["recorded_at"] = p
        elif "resource_id" not in rec and RESOURCE_RE.fullmatch(p):
            rec["resource_id"] = p
        elif "record_type" not in rec and p.upper() in CATEGORIES:
            rec["record_type"] = p.upper()
        elif "system" not in rec and _name_system(p) and re.fullmatch(r"[a-z_]+", p):
            rec["system"] = p
        else:
            rest.append(p)
    if "resource_id" not in rec:
        return None
    # status = first remaining all-caps token; identifier = the remaining value
    for p in list(rest):
        if "result" not in rec and re.fullmatch(r"[A-Z][A-Z_ ]{1,30}", p):
            rec["result"] = p.replace(" ", "_")
            rest.remove(p)
    if rest:
        ident = rest[0]
        if re.search(r"\bno (direct )?identifier", ident, re.I):
            rec["identifier_note"] = ident
        else:
            rec["subject_reference"] = ident
        if len(rest) > 1:
            rec["notes"] = " | ".join(rest[1:])
    return rec


def extract(ef: EvidenceFile, path: Path, scanner: InjectionScanner):
    observations: list[Observation] = []
    doc_time = None
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        m = DOC_TIME_RE.match(line)
        if m and try_parse_ts(m.group(2)):
            doc_time = m.group(2).strip()
            break
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if scanner.check(ef.file_id, line):
            continue
        rec = _parse_pipe_line(line) if "|" in line else None
        if rec is None:
            ids = RESOURCE_RE.findall(line)
            if not ids:
                continue
            for rid in dict.fromkeys(ids):
                rec2 = {"resource_id": rid, "notes": line.strip()}
                obs = build_observation(rec2, evidence_id=ef.file_id, manifest_at=ef.captured_at,
                                        source_path=ef.file_path, container_time=doc_time,
                                        extra={"format": "txt", "line": line_no, "free_form": True},
                                        scanner=scanner)
                if obs is not None:
                    obs.state_hint = "UNKNOWN"
                    observations.append(obs)
            continue
        obs = build_observation(rec, evidence_id=ef.file_id, manifest_at=ef.captured_at,
                                source_path=ef.file_path, container_time=doc_time,
                                system_names=[rec.get("system", "")],
                                category_names=[rec.get("record_type", "")],
                                extra={"format": "txt", "line": line_no}, scanner=scanner)
        if obs is not None:
            observations.append(obs)
    return observations, []
