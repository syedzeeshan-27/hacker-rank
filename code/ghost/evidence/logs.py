"""Log files -> Observations.

Every line format in the dataset is reduced to (timestamp, key/value dict):
  * plain key=value / key="value"         (service events, readback traces, record_lookup lines)
  * JSON object after the timestamp        (structured service events)
  * syslog  "<pri>1 host service trace - - k=v ..."
  * bracket "[LEVEL] [service] [trace] event: k=v ..."
  * pipe    "LEVEL | service | trace | event | k=v ..."
  * cloud   "requestId=... function=... message=\"k=\\\"v\\\" ...\""
Lookups are correlated by trace id: the *_started line carries the lookup key (lookup_key, match_key,
subject_key, scan_key, owner_key, subject_ref); the completion line carries resource_id and the
outcome (rows, http_status, result, enabled, restore_allowed, marker_found, ...). One Observation per
outcome line that names a resource; capture time = the line timestamp (never later than manifest).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import EvidenceFile, Observation
from .common import RESOURCE_RE, Idents, InjectionScanner, build_observation, classify_value

TS_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s*(.*)$")
KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
BRACKET_RE = re.compile(r"^\[(\w+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+([\w.]+):?")
SYSLOG_RE = re.compile(r"^<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s+")

NOISE_KEYS = {
    "instance", "pod", "region", "level", "severity", "logger", "attempt", "duration_ms", "wait_ms",
    "took_ms", "latency_ms", "requestId", "function", "shard", "seq", "pool", "cache", "message",
    "retry_in_ms", "dependency", "route", "node",
}
# lookup keys carried on *_started lines (and correlation keys on completion lines)
TRACE_KEY_FIELDS = ("lookup_key", "match_key", "subject_key", "scan_key", "owner_key", "subject_ref",
                    "correlation_key", "query")
NON_OUTCOME_EVENTS = re.compile(r"_started$|^connection_acquired$|^cache_lookup$|^dependency_|"
                                r"^export_(started|completed)$|^batch_started$|^checkpoint")


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return v.replace('\\"', '"')


def parse_line(line: str) -> tuple[str, dict] | None:
    m = TS_RE.match(line)
    if not m:
        return None
    ts, rest = m.group(1), m.group(2)
    kv: dict[str, str] = {}
    if rest.startswith("{"):
        try:
            obj = json.loads(rest)
            if isinstance(obj, dict):
                kv = {str(k): ("" if v is None else str(v)) for k, v in obj.items()}
                return ts, kv
        except json.JSONDecodeError:
            pass
    positional: dict[str, str] = {}
    if " | " in rest:
        parts = [p.strip() for p in rest.split(" | ")]
        if len(parts) >= 4:
            positional.update(level=parts[0], service=parts[1], trace_id=parts[2], event=parts[3])
    mb = BRACKET_RE.match(rest)
    if mb:
        positional.update(level=mb.group(1), service=mb.group(2), trace_id=mb.group(3), event=mb.group(4))
    ms = SYSLOG_RE.match(rest)
    if ms:
        positional.update(service=ms.group(2), trace_id=ms.group(3))
    body = rest.replace('\\"', '"')
    for k, v in KV_RE.findall(body):
        kv[k] = _unquote(v)
    if "requestId" in kv and "trace_id" not in kv:
        kv["trace_id"] = kv["requestId"]
    if "function" in kv and "service" not in kv:
        kv["service"] = kv["function"]
    for k, v in positional.items():
        kv.setdefault(k, v)
    return ts, kv


def extract(ef: EvidenceFile, path: Path, scanner: InjectionScanner):
    observations: list[Observation] = []
    trace_keys: dict[str, Idents] = {}
    trace_raw: dict[str, dict] = {}
    lines = path.read_text(encoding="utf-8").splitlines()

    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        flagged = scanner.check(ef.file_id, line)
        parsed = parse_line(line)
        if parsed is None:
            continue
        ts, kv = parsed
        event = kv.get("event", "")
        trace = kv.get("trace_id", "")
        # collect lookup keys for this trace
        if trace:
            ids = trace_keys.setdefault(trace, Idents())
            raw = trace_raw.setdefault(trace, {})
            for f in TRACE_KEY_FIELDS:
                val = kv.get(f)
                if val and not flagged:
                    if RESOURCE_RE.fullmatch(val.strip()):
                        raw.setdefault("looked_up_resource", val.strip())
                        continue
                    classify_value(val, ids, key=f)
                    raw.setdefault(f, val)
            if kv.get("source"):
                raw.setdefault("source", kv["source"])
        rid = kv.get("resource_id", "").strip()
        if not rid or not RESOURCE_RE.fullmatch(rid):
            continue
        if NON_OUTCOME_EVENTS.search(event):
            continue
        if flagged:
            continue
        rec = {k: v for k, v in kv.items() if k not in NOISE_KEYS and k not in ("trace_id", "service", "event")}
        # normalise "null" strings to None so empty identifier fields read as empty
        rec = {k: (None if isinstance(v, str) and v.strip().lower() in ("null", "none", "") else v)
               for k, v in rec.items()}
        rec["resource_id"] = rid
        # on an outcome line, subject_ref is a field of the returned record (e.g. a message's
        # subject), not the key that was searched for
        if "subject_ref" in rec and "subject_reference" not in rec:
            rec["subject_reference"] = rec.pop("subject_ref")
        # trace-level lookup keys link the subject to the resource; line-level correlation_key /
        # lookup_key stay lookups (handled in build_observation); matched_value is a detector hit
        lookup = Idents()
        if trace and trace in trace_keys:
            lookup.merge(trace_keys[trace])
        service = kv.get("service", "")
        extra = {"format": "log", "line": line_no, "event": event, "service": service,
                 "line_ts": ts}
        if trace:
            extra["trace_id"] = trace
            for k, v in trace_raw.get(trace, {}).items():
                extra.setdefault(f"trace_{k}", v)
        rec.setdefault("recorded_at", ts)
        obs = build_observation(
            rec,
            evidence_id=ef.file_id,
            manifest_at=ef.captured_at,
            source_path=ef.file_path,
            container_time=None,
            system_names=[str(rec.get("system") or ""), service, event],
            category_names=[event, service],
            extra=extra,
            lookup_idents=lookup,
            scanner=scanner,
        )
        if obs is None:
            continue
        if FAILURE_EVENTS.search(event) and not obs.extra.get("status"):
            obs.state_hint = "UNKNOWN"  # an error about a resource says nothing about its contents
        observations.append(obs)
    return observations, []


FAILURE_EVENTS = re.compile(r"fail|error|abandon|timeout", re.I)
