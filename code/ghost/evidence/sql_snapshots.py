"""SQL snapshot exports -> Observations + Coverage.

Two shapes exist and both are handled generically:
  * "generic" tables with (resource_id, origin_system, collection_name, record_json, updated_at):
    one table per copy (primary / read / regional / reporting / failover replica).
  * typed tables (customer_profiles, support_records, connector_subscriptions, ...), optionally with a
    snapshot_scope(origin_system, captured_at) table naming which systems were captured and when.
The capture time is the snapshot_scope time for that system, else the "-- Captured at:" header,
else the manifest time; never later than the manifest time.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from ..models import EvidenceFile, Observation
from .common import (
    InjectionScanner,
    build_observation,
    effective_time,
    resolve_system,
    try_parse_ts,
)

HEADER_TS_RE = re.compile(r"^--\s*Captured at:\s*(\S+)", re.I | re.M)
HEADER_TOPOLOGY_RE = re.compile(r"^--\s*Database topology:\s*(\S+)", re.I | re.M)


def _copy_role(table: str) -> str:
    t = table.lower()
    for role in ("failover_replica", "read_replica", "reporting_replica", "replica", "primary"):
        if role in t:
            if role == "replica":
                m = re.search(r"replica_(\w+)$", t)
                return f"regional_replica:{m.group(1)}" if m else "replica"
            return role
    return "primary" if not re.search(r"replica", t) else "replica"


def extract(ef: EvidenceFile, path: Path, scanner: InjectionScanner):
    from .registry import Coverage  # local import to avoid a cycle

    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("--"):
            scanner.check(ef.file_id, line)
    header_ts = None
    m = HEADER_TS_RE.search(text)
    if m:
        header_ts = try_parse_ts(m.group(1))
    topology = (HEADER_TOPOLOGY_RE.search(text).group(1) if HEADER_TOPOLOGY_RE.search(text) else "")

    con = sqlite3.connect(":memory:")
    try:
        con.executescript(text)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
        scope: dict[str, object] = {}
        if "snapshot_scope" in tables:
            cols = [r[1] for r in con.execute('PRAGMA table_info("snapshot_scope")')]
            for row in con.execute('SELECT * FROM "snapshot_scope"'):
                d = dict(zip(cols, row))
                sys_name = str(d.get("origin_system") or d.get("system") or "").strip()
                if sys_name:
                    scope[resolve_system("", names=[sys_name]) or sys_name] = d.get("captured_at")

        observations: list[Observation] = []
        coverage: list = []
        generic_pairs: list[tuple[str, str]] = []  # (system, collection) seen in any generic table
        per_table_generic: dict[str, dict[tuple[str, str], set[str]]] = {}

        for table in tables:
            if table == "snapshot_scope":
                continue
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            role = _copy_role(table)
            generic = "record_json" in cols
            rows = list(con.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
            if generic:
                per_table_generic.setdefault(table, {})
            table_system = "" if generic else resolve_system("", names=[table])
            table_ids: set[str] = set()
            for idx, row in enumerate(rows):
                d = dict(zip(cols, row))
                rec = dict(d)
                collection = ""
                origin = ""
                if generic:
                    try:
                        payload = json.loads(d.get("record_json") or "{}")
                    except json.JSONDecodeError:
                        payload = {}
                    rec = {k: v for k, v in d.items() if k != "record_json"}
                    rec.update(payload if isinstance(payload, dict) else {})
                    collection = str(d.get("collection_name") or "")
                    origin = str(d.get("origin_system") or "")
                rid = str(rec.get("resource_id") or "")
                system_hint = resolve_system(rid, names=[origin, collection, table])
                cap_src = scope.get(system_hint) if scope else None
                container_time = cap_src or header_ts
                extra = {"format": "sql", "table": table, "copy_role": role, "row": idx}
                if topology:
                    extra["topology"] = topology
                if collection:
                    extra["collection"] = collection
                if "updated_at" in d:
                    extra["updated_at"] = d["updated_at"]
                # origin_system / collection_name are container names, not record fields
                rec_fields = {k: v for k, v in rec.items()
                              if k not in ("origin_system", "collection_name", "updated_at")}
                obs = build_observation(
                    rec_fields,
                    evidence_id=ef.file_id,
                    manifest_at=ef.captured_at,
                    source_path=ef.file_path,
                    container_time=container_time,
                    system_names=[origin, collection, table],
                    category_names=[collection, table],
                    extra=extra,
                    scanner=scanner,
                )
                if obs is None:
                    continue
                observations.append(obs)
                if generic:
                    key = (obs.system_id, collection)
                    per_table_generic[table].setdefault(key, set()).add(rid)
                    if key not in generic_pairs:
                        generic_pairs.append(key)
                else:
                    table_ids.add(rid)

            if not generic:
                sys_id = table_system
                if not sys_id:
                    continue
                if scope and sys_id not in scope:
                    continue  # snapshot_scope says this system was not captured
                cap, _ = effective_time(ef.captured_at, scope.get(sys_id) if scope else None, header_ts)
                coverage.append(Coverage(evidence_id=ef.file_id, captured_at=cap, system_id=sys_id,
                                         scope=table, resource_ids=table_ids, kind="snapshot"))

        # generic tables: every copy is assumed to enumerate every (system, collection) pair that
        # appears in any copy of the same snapshot; a pair missing from one copy = zero rows there.
        for table, pairs in per_table_generic.items():
            for key in generic_pairs:
                sys_id, collection = key
                cap, _ = effective_time(ef.captured_at, header_ts)
                coverage.append(Coverage(evidence_id=ef.file_id, captured_at=cap, system_id=sys_id,
                                         scope=f"{table}/{collection}",
                                         resource_ids=set(pairs.get(key, set())), kind="snapshot"))
        return observations, coverage
    finally:
        con.close()
