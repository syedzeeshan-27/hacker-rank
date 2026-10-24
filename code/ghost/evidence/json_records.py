"""JSON evidence -> Observations + Coverage.

A generic walker finds every dict that carries a resource_id and keeps the context above it
(captured_at / exported_at / data_store / collection / service key / list key / source). The shapes
in the dataset all reduce to this:
  * records_assurance bundles      snapshots[] {captured_at, data_store, collection, records[]}
  * service_resource_export        service_exports.<service>.<lookup list>[] (status_code, profile null,
                                   hits{total, documents[]})
  * lookup ledgers                 records[] {recorded_at, system, resource_id, record_type, result,
                                   subject_reference}  (connector_configurations, export manifest,
                                   security audit ledger)
  * single-collection reports      {captured_at, collection, records[]} (analytics report, export jobs)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import EvidenceFile, Observation
from .common import InjectionScanner, build_observation, effective_time, resolve_system

CONTEXT_TIME_KEYS = ("captured_at", "snapshot_at", "exported_at", "generated_at", "as_of")
CONTEXT_NAME_KEYS = ("data_store", "collection", "source", "origin", "system", "service", "table")
LOOKUP_MARKERS = ("result", "status_code", "hits", "query", "lookup_value", "rows")


@dataclass
class _Container:
    path: list[str]
    ctx: dict
    items: list[dict] = field(default_factory=list)
    resource_ids: list[str] = field(default_factory=list)


def _ctx_time(ctx: dict) -> Any:
    # prefer the innermost snapshot capture time over the outer export time
    for k in ("captured_at", "snapshot_at", "as_of", "generated_at", "exported_at"):
        if ctx.get(k):
            return ctx[k]
    return None


def extract(ef: EvidenceFile, path: Path, scanner: InjectionScanner):
    from .registry import Coverage

    data = json.loads(path.read_text(encoding="utf-8"))
    observations: list[Observation] = []
    containers: list[_Container] = []

    def emit(rec: dict, ctx: dict, names: list[str]) -> None:
        # nested dicts with a different resource_id are separate records
        own = {}
        for k, v in rec.items():
            if isinstance(v, dict) and v.get("resource_id") and v.get("resource_id") != rec.get("resource_id"):
                emit(v, ctx, names + [k])
                continue
            own[k] = v
        sys_names = [ctx.get("system", ""), ctx.get("service_key", ""), ctx.get("data_store", ""),
                     ctx.get("collection", ""), ctx.get("list_key", ""), ctx.get("source", ""),
                     ctx.get("origin", "")]
        cat_names = [ctx.get("collection", ""), ctx.get("list_key", ""), ctx.get("service_key", ""),
                     ctx.get("data_store", "")]
        extra = {"format": "json", "json_path": "/".join(names)}
        for k in ("data_store", "collection", "service_key", "list_key", "source", "origin"):
            if ctx.get(k):
                extra[k] = ctx[k]
        obs = build_observation(
            own,
            evidence_id=ef.file_id,
            manifest_at=ef.captured_at,
            source_path=ef.file_path,
            container_time=_ctx_time(ctx),
            system_names=sys_names,
            category_names=cat_names,
            extra=extra,
            scanner=scanner,
        )
        if obs is not None:
            observations.append(obs)

    def walk(obj: Any, ctx: dict, names: list[str]) -> None:
        if isinstance(obj, dict):
            if "resource_id" in obj:
                emit(obj, ctx, names)
                return
            new_ctx = dict(ctx)
            for k, v in obj.items():
                if isinstance(v, str):
                    scanner.check(ef.file_id, v)
                if k in CONTEXT_TIME_KEYS or k in CONTEXT_NAME_KEYS:
                    if isinstance(v, (str, int)):
                        new_ctx[k] = v
            for k, v in obj.items():
                if isinstance(v, list):
                    c_ctx = dict(new_ctx)
                    c_ctx["list_key"] = k
                    cont =_Container(path=names + [k], ctx=c_ctx)
                    containers.append(cont)
                    for item in v:
                        if isinstance(item, dict) and "resource_id" in item:
                            cont.items.append(item)
                            cont.resource_ids.append(str(item.get("resource_id")))
                        walk(item, c_ctx, names + [k])
                elif isinstance(v, dict):
                    c_ctx = dict(new_ctx)
                    if names and names[-1] == "service_exports":
                        c_ctx["service_key"] = k
                    walk(v, c_ctx, names + [k])
        elif isinstance(obj, list):
            for item in obj:
                walk(item, ctx, names)
        elif isinstance(obj, str):
            scanner.check(ef.file_id, obj)

    walk(data, {}, [])

    coverage = []
    for cont in containers:
        lookup_style = any(any(m in it for m in LOOKUP_MARKERS) for it in cont.items)
        ledger_style = any("result" in it and "record_type" in it for it in cont.items)
        if ledger_style:
            continue  # cross-system lookup ledger: lists checks, does not enumerate a store
        ctx = cont.ctx
        sys_names = [ctx.get("service_key", ""), ctx.get("data_store", ""), ctx.get("collection", ""),
                     ctx.get("system", ""), ctx.get("source", "")]
        if not cont.items and not (ctx.get("collection") or ctx.get("data_store") or ctx.get("service_key")):
            continue
        sys_id = ""
        if cont.resource_ids:
            systems = {resolve_system(r, names=sys_names) for r in cont.resource_ids}
            if len(systems) == 1:
                sys_id = systems.pop()
        if not sys_id:
            sys_id = resolve_system("", names=sys_names)
        if not sys_id:
            continue
        if not (ctx.get("collection") or ctx.get("data_store") or ctx.get("service_key")):
            continue
        kind = "lookup" if (lookup_style or ctx.get("service_key")) else "snapshot"
        cap, _ = effective_time(ef.captured_at, _ctx_time(ctx))
        scope = "/".join(x for x in (ctx.get("service_key") or ctx.get("data_store", ""),
                                     ctx.get("collection") or ctx.get("list_key", "")) if x)
        coverage.append(Coverage(evidence_id=ef.file_id, captured_at=cap, system_id=sys_id,
                                 scope=scope, resource_ids=set(cont.resource_ids), kind=kind))
    return observations, coverage
