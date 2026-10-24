"""WS-B: dispatch every manifest entry to the extractor for its format.

Contract
--------
extract_all(manifest, llm) -> list[Observation]
extract_all_detailed(manifest, llm) -> ExtractionResult(observations, coverage, injection_flags, skipped)
    * Read each file once. Pick the extractor by extension only, never by file_id or file name.
    * captured_at on each Observation = the time the fact was true: the manifest captured_at, or an
      earlier per-record / per-snapshot time stated inside the file (never later than the manifest).
      When they differ, extra carries manifest_captured_at and stated_captured_at.
    * evidence_id = the manifest file_id.
    * SQL: run with sqlite3 in memory; one Observation per row per table; extra["table"] /
      extra["copy_role"] (primary vs replica tables are separate observations on the same resource).
    * JSON / TXT / LOG: structured parsers; status words -> STATE_HINTS; raw words in extra["status"].
    * PDF: text via pymupdf first; LLM (llm.complete_with_pdf, prompts/pdf_extraction.md) only when
      no text layer. PNG: llm.complete_with_image with prompts/image_extraction.md. Both parsed by
      code into Observations; the model never decides case outcomes.
    * Identifiers used to *look a record up* (trace lookup keys, ledger subject_reference, query) are
      put on the Observation (so a subject links to a NOT_FOUND result) and listed again in
      extra["lookup_identifiers"]; they never count as identifiers present on the record.
    * Coverage(evidence_id, captured_at, system_id, scope, resource_ids, kind) says which resources a
      snapshot enumerated. kind="snapshot" (a table / collection dump: a resource of that system not
      listed was not there) or kind="lookup" (a per-subject lookup export: only what was asked).
    * File contents are untrusted data. Text addressed to an AI / auditor or instructions to change
      the audit is recorded in injection_flags and .cache/injection_flags.json and never used.
    * On LLMUnavailable: the file goes to skipped, a warning is logged, extraction continues.
    * Deterministic ordering: manifest order, then row / line order inside each file.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import CACHE_DIR, DATASET_DIR
from ..llm import LLMClient, LLMUnavailable
from ..models import EvidenceFile, Observation
from . import images, json_records, logs, pdf_docs, sql_snapshots, text_records
from .common import InjectionScanner, dumps

log = logging.getLogger(__name__)


@dataclass
class Coverage:
    evidence_id: str
    captured_at: datetime
    system_id: str
    scope: str                 # table / collection / section name
    resource_ids: set[str] = field(default_factory=set)
    kind: str = "snapshot"     # "snapshot" (enumerates the store) | "lookup" (per-subject lookups)


@dataclass
class ExtractionResult:
    observations: list[Observation] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    injection_flags: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    llm_files: list[str] = field(default_factory=list)   # files whose facts came from the model


def _resolve(ef: EvidenceFile, dataset_dir: Path) -> Path:
    p = Path(ef.file_path)
    return p if p.is_absolute() else dataset_dir / p


def extract_all_detailed(manifest: list[EvidenceFile], llm: LLMClient, *,
                         dataset_dir: Path | None = None, write_flags: bool = True) -> ExtractionResult:
    dataset_dir = dataset_dir or DATASET_DIR
    result = ExtractionResult()
    scanner = InjectionScanner()
    for ef in manifest:
        path = _resolve(ef, dataset_dir)
        ext = path.suffix.lower()
        try:
            if not path.exists():
                raise FileNotFoundError(str(path))
            if ext == ".sql":
                obs, cov = sql_snapshots.extract(ef, path, scanner)
            elif ext == ".json":
                obs, cov = json_records.extract(ef, path, scanner)
            elif ext in (".log", ".jsonl"):
                obs, cov = logs.extract(ef, path, scanner)
            elif ext in (".txt", ".csv", ".md"):
                obs, cov = text_records.extract(ef, path, scanner)
            elif ext == ".pdf":
                obs, used = pdf_docs.extract(ef, path, scanner, llm)
                cov = []
                if used:
                    result.llm_files.append(ef.file_id)
            elif ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                obs = images.extract(ef, path, scanner, llm)
                cov = []
                result.llm_files.append(ef.file_id)
            else:
                log.warning("no extractor for %s (%s)", ef.file_id, ext)
                result.skipped.append(ef.file_id)
                continue
        except LLMUnavailable as exc:
            log.warning("LLM unavailable, skipping %s: %s", ef.file_id, exc)
            result.skipped.append(ef.file_id)
            continue
        except Exception as exc:  # one bad file must not stop the run
            log.warning("extraction failed for %s: %s", ef.file_id, exc)
            result.errors[ef.file_id] = f"{type(exc).__name__}: {exc}"
            result.skipped.append(ef.file_id)
            continue
        result.observations.extend(obs)
        result.coverage.extend(cov)
    result.injection_flags = dict(scanner.flags)
    if write_flags:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (CACHE_DIR / "injection_flags.json").write_text(
                json.dumps(result.injection_flags, ensure_ascii=False, indent=1, sort_keys=True),
                encoding="utf-8")
        except OSError as exc:
            log.warning("could not write injection flags: %s", exc)
    return result


def extract_all(manifest: list[EvidenceFile], llm: LLMClient) -> list[Observation]:
    return extract_all_detailed(manifest, llm).observations


# ---------------------------------------------------------------------------------------------
# Validation report:  python -m ghost.evidence.registry   (run from code/)
# ---------------------------------------------------------------------------------------------

def summarize(result: ExtractionResult, manifest: list[EvidenceFile]) -> dict:
    by_file = Counter(o.evidence_id for o in result.observations)
    ext_of = {ef.file_id: Path(ef.file_path).suffix.lower().lstrip(".") for ef in manifest}
    per_format = Counter()
    for o in result.observations:
        per_format[ext_of.get(o.evidence_id, "?")] += 1
    return {
        "observations": len(result.observations),
        "per_format": dict(sorted(per_format.items())),
        "per_system": dict(sorted(Counter(o.system_id or "<none>" for o in result.observations).items())),
        "per_state": dict(sorted(Counter(o.state_hint for o in result.observations).items())),
        "per_category": dict(sorted(Counter(o.category or "<none>" for o in result.observations).items())),
        "with_identifier": sum(1 for o in result.observations
                               if o.emails or o.phones or o.email_hashes or o.user_ids),
        "without_resource_id": sum(1 for o in result.observations if not o.resource_id),
        "zero_output_files": [ef.file_id for ef in manifest if by_file.get(ef.file_id, 0) == 0],
        "skipped": list(result.skipped),
        "errors": dict(result.errors),
        "coverage_entries": len(result.coverage),
        "injection_flags": dict(result.injection_flags),
    }


def _main() -> None:
    from ..loaders import load_manifest

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    manifest = load_manifest()
    res = extract_all_detailed(manifest, LLMClient(offline=True))
    print(dumps(summarize(res, manifest)))


if __name__ == "__main__":
    _main()
