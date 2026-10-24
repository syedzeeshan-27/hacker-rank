"""WS-D: deletion_rules.csv -> required treatment for a resource.

required_treatment(category, resource_state, rules, as_of=None)
    -> "DELETE" | "REDACT" | "ANONYMIZE" | "RETAIN" | "BLOCK_RESTORE" | "DISABLE" | ""

Driven by the rule table (data_category -> required_action, with the condition text checked where the
record lets us check it), never by resource ids:

* the table is indexed by data_category exactly as loaded; the first rule for a category wins (the table
  has one rule per category).
* RETAIN whose condition is about restore access (BACKUP_ARCHIVE "Restore access can be blocked") means
  "keep the archive, block the restore path" -> "BLOCK_RESTORE".
* RETAIN whose condition is a retention period / legal hold is honoured while the record's
  retention_until (when it carries one) is not before as_of; an expired retention means the rule's
  condition fails and no rule applies -> "" (the engine then neither verifies nor deletes it).
* DATA_FLOW is not a rule category: an active sync / import can recreate prohibited data, so the
  required treatment is "DISABLE" (problem statement: no active sync, import or restore path may remain).
* SUPPORT_ATTACHMENT's DELETE rule applies when the attachment "belongs only to the deletion subject":
  an attachment that also carries another user's reference fails the condition and falls back to the
  SUPPORT_MESSAGE rule (redact the subject's identifiers, keep the shared record).
* A category with no rule returns "".
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from .models import ResourceState, Rule, parse_ts

_RETENTION_FMT = ("%d %B %Y", "%d %b %Y", "%Y-%m-%d")

_TABLE_CACHE: dict[int, dict[str, Rule]] = {}


def rule_table(rules: list[Rule]) -> dict[str, Rule]:
    key = id(rules)
    tbl = _TABLE_CACHE.get(key)
    if tbl is None:
        tbl = {}
        for r in rules:
            tbl.setdefault(r.data_category.strip().upper(), r)
        _TABLE_CACHE[key] = tbl
    return tbl


def _retention_until(extra: dict) -> Optional[datetime]:
    raw = (extra or {}).get("retention_until")
    if not raw:
        row = (extra or {}).get("row")
        if isinstance(row, dict):
            raw = row.get("retention_until")
    if not raw or not isinstance(raw, str):
        return None
    try:
        return parse_ts(raw)
    except ValueError:
        pass
    for fmt in _RETENTION_FMT:
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def required_treatment(category: str, resource_state: Optional[ResourceState], rules: list[Rule],
                       as_of: Optional[datetime] = None) -> str:
    cat = (category or "").strip().upper()
    if cat == "DATA_FLOW":
        return "DISABLE"
    rule = rule_table(rules).get(cat)
    if rule is None:
        return ""
    action = rule.required_action.strip().upper()
    cond = rule.condition.lower()
    extra = resource_state.extra if resource_state is not None else {}
    if action == "RETAIN":
        if re.search(r"restore", cond):
            return "BLOCK_RESTORE"
        until = _retention_until(extra)
        if until is not None and as_of is not None and until < as_of:
            return ""
        return "RETAIN"
    if cat == "SUPPORT_ATTACHMENT" and action == "DELETE" and "only" in cond and resource_state is not None:
        others = {u for u in resource_state.linked_user_ids if u}
        subject_refs = set(extra.get("subject_user_ids", []))
        if others and subject_refs and not others <= subject_refs:
            fallback = rule_table(rules).get("SUPPORT_MESSAGE")
            return fallback.required_action.strip().upper() if fallback else ""
    return action
