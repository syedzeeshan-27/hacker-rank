"""Shared helpers for the evidence extractors (WS-B).

Everything here is content-driven: system / category / state are derived from table, collection,
service and field names, status words and identifier patterns found inside the files. Nothing keys
on file_id values or file names.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..models import (
    CATEGORIES,
    KNOWN_SYSTEMS,
    Observation,
    fmt_ts,
    norm_email,
    norm_hash,
    norm_phone,
    parse_ts,
)

# ---------------------------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b", re.I)
HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
PHONE_VALUE_RE = re.compile(r"^\+?[\d][\d \-().]{6,}\d$")
PHONE_TEXT_RE = re.compile(r"(?<![\w+])\+\d{1,3}[\d \-().]{6,}\d")
RESOURCE_PREFIXES = (
    "audit_record", "data_flow", "legal_hold", "email_delivery", "account", "search", "support",
    "analytics", "export", "backup", "tombstone", "marketing", "cache", "object",
)
RESOURCE_RE = re.compile(r"\b(?:" + "|".join(RESOURCE_PREFIXES) + r")_\d+\b")
EXTERNAL_REF_RE = re.compile(r"\b[A-Z]{2,5}-\d[\d-]{2,}\b|\b[\w\-]+\.(?:pdf|zip|csv|png|jpg|tar\.zst|tar\.gz)\b")
MASK_RE = re.compile(r"\*|redact|\[removed\]|masked|•|x{3,}", re.I)
NULLISH = {"", "null", "none", "nil", "n/a", "-", "--"}

# ---------------------------------------------------------------------------------------------
# System and category mapping
# ---------------------------------------------------------------------------------------------

PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "support": "support_platform",
    "analytics": "analytics_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "marketing": "marketing_platform",
    "legal_hold": "legal_service",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
}

PREFIX_CATEGORY = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "support": "SUPPORT_MESSAGE",
    "analytics": "ANALYTICS_PROFILE",
    "export": "USER_EXPORT",
    "data_flow": "DATA_FLOW",
    "backup": "BACKUP_ARCHIVE",
    "audit_record": "SECURITY_AUDIT_LOG",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "marketing": "MARKETING_PROFILE",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "cache": "CACHE_ENTRY",
    "email_delivery": "EMAIL_DELIVERY_LOG",
}

# Ordered: the first matching rule wins. Applied to lower-cased data_store / table / collection /
# service / screen names. Specific names come before generic ones (customer_profiles last).
SYSTEM_NAME_RULES: tuple[tuple[re.Pattern, str], ...] = tuple((re.compile(p), s) for p, s in (
    (r"tombstone|deleted_customer_ref|customer_reference_registry|identity", "identity_service"),
    (r"analytics", "analytics_platform"),
    (r"search", "search_service"),
    (r"support|ticket", "support_platform"),
    (r"export", "export_service"),
    (r"connector|data_flow|integration", "integration_service"),
    (r"backup|restore|vault|recovery", "backup_service"),
    (r"security|audit|compliance", "security_service"),
    (r"marketing", "marketing_platform"),
    (r"legal|retained_order|retention_register|disputed_order", "legal_service"),
    (r"email_delivery|notification_deliver|delivery_log", "email_delivery_service"),
    (r"cache", "cache_service"),
    (r"customer_profiles?|account|profile", "account_service"),
))

CATEGORY_NAME_RULES: tuple[tuple[re.Pattern, str], ...] = tuple((re.compile(p), c) for p, c in (
    (r"tombstone|deleted_customer_ref|retained_reference", "ACCOUNT_TOMBSTONE"),
    (r"attachment", "SUPPORT_ATTACHMENT"),
    (r"analytics", "ANALYTICS_PROFILE"),
    (r"search|index_query|index_entr", "SEARCH_INDEX_ENTRY"),
    (r"support|ticket|conversation|message", "SUPPORT_MESSAGE"),
    (r"export", "USER_EXPORT"),
    (r"connector|data_flow", "DATA_FLOW"),
    (r"backup|restore|archive", "BACKUP_ARCHIVE"),
    (r"audit", "SECURITY_AUDIT_LOG"),
    (r"legal|retained_order|retention_register|disputed_order", "LEGAL_HOLD_RECORD"),
    (r"marketing", "MARKETING_PROFILE"),
    (r"email_delivery|notification_deliver", "EMAIL_DELIVERY_LOG"),
    (r"cache", "CACHE_ENTRY"),
    (r"customer_profiles?|profile_lookup|account", "USER_PROFILE"),
))


def resource_prefix(resource_id: str) -> str:
    rid = (resource_id or "").strip().lower()
    for p in RESOURCE_PREFIXES:  # longest / compound prefixes are listed first
        if rid.startswith(p + "_"):
            return p
    return ""


def _name_system(name: str) -> str:
    n = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not n:
        return ""
    if n in KNOWN_SYSTEMS:
        return n
    for pat, sys_id in SYSTEM_NAME_RULES:
        if pat.search(n):
            return sys_id
    return ""


def resolve_system(resource_id: str, *, explicit: str = "", names: Iterable[str] = ()) -> str:
    """explicit record-level system (if in vocabulary) > resource_id prefix > container names."""
    e = (explicit or "").strip().lower()
    if e in KNOWN_SYSTEMS:
        return e
    by_prefix = PREFIX_SYSTEM.get(resource_prefix(resource_id), "")
    if by_prefix:
        return by_prefix
    for candidate in [explicit, *names]:
        s = _name_system(candidate or "")
        if s:
            return s
    return ""


def resolve_category(resource_id: str, *, explicit: str = "", names: Iterable[str] = (),
                     attachment_hint: bool = False) -> str:
    """explicit record_type > attachment hint > container / event names > resource_id prefix."""
    e = (explicit or "").strip().upper()
    if e in CATEGORIES:
        return e
    prefix = resource_prefix(resource_id)
    if attachment_hint and prefix in ("support", ""):
        return "SUPPORT_ATTACHMENT"
    for name in names:
        n = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not n:
            continue
        if n.upper() in CATEGORIES:
            return n.upper()
        for pat, cat in CATEGORY_NAME_RULES:
            if pat.search(n):
                # a name rule must not contradict a known prefix (e.g. a support_ id under an
                # "account" service name); the prefix is the stronger signal then.
                if prefix and PREFIX_CATEGORY.get(prefix) and cat not in _COMPATIBLE.get(prefix, ()):
                    break
                return cat
    return PREFIX_CATEGORY.get(prefix, "")


_COMPATIBLE = {
    "support": ("SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT"),
    "account": ("USER_PROFILE",),
    "search": ("SEARCH_INDEX_ENTRY",),
    "analytics": ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"),
    "export": ("USER_EXPORT",),
    "data_flow": ("DATA_FLOW",),
    "backup": ("BACKUP_ARCHIVE",),
    "audit_record": ("SECURITY_AUDIT_LOG",),
    "tombstone": ("ACCOUNT_TOMBSTONE",),
    "marketing": ("MARKETING_PROFILE",),
    "legal_hold": ("LEGAL_HOLD_RECORD",),
    "cache": ("CACHE_ENTRY",),
    "email_delivery": ("EMAIL_DELIVERY_LOG",),
}

# ---------------------------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_LOOSE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})(?:[ ,T]+(\d{1,2})[:.](\d{2})(?::(\d{2}))?)?")


def try_parse_ts(value: Any) -> Optional[datetime]:
    """ISO-8601 or '26 Apr 2026 02:00 UTC'. Returns None when it cannot tell (never guesses a year)."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}", v):
            return parse_ts(v.replace(" ", "T", 1) if "T" not in v and " " in v else v)
    except ValueError:
        pass
    m = _LOOSE_RE.search(v)
    if m and m.group(2).lower()[:3] in _MONTHS:
        day, mon, year = int(m.group(1)), _MONTHS[m.group(2).lower()[:3]], int(m.group(3))
        hh = int(m.group(4) or 0)
        mm = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        try:
            return datetime(year, mon, day, hh, mm, ss, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def effective_time(manifest_at: datetime, *candidates: Any) -> tuple[datetime, Optional[datetime]]:
    """Earliest stated time among candidates, never later than the manifest time.

    Returns (captured_at, stated) where stated is the in-file time that was used (or None).
    """
    stated: Optional[datetime] = None
    for c in candidates:
        dt = c if isinstance(c, datetime) else try_parse_ts(c)
        if dt is None:
            continue
        stated = dt if stated is None else min(stated, dt)
    if stated is None:
        return manifest_at, None
    return min(stated, manifest_at), stated


# ---------------------------------------------------------------------------------------------
# Identifier extraction
# ---------------------------------------------------------------------------------------------

# Keys whose value is the *lookup key* used to find a record, not a field on the record.
LOOKUP_KEYS = {
    "lookup_key", "lookup_value", "query", "match_key", "subject_key", "scan_key", "owner_key",
    "correlation_key", "subject_ref", "lookup", "search_key",
}
FREE_TEXT_KEYS = re.compile(r"message|excerpt|text|body|note|comment|details|description", re.I)
NAME_KEYS = re.compile(r"(^|_)(full_name|name|display_name|requester_name|customer_name|holder|requester|"
                       r"customer|owner_name|contact_name)$", re.I)
NON_NAME_KEYS = re.compile(r"file|connector|index|event|actor|channel|cohort|destination|region|"
                           r"pool|instance|service|system|table|collection|archive|object|path|"
                           r"storage|product|screen|title|logger|function|dependency", re.I)
PII_FIELD_KEYS = re.compile(r"email|phone|(^|_)hash$|^(full_|display_|requester_|customer_)?name$|"
                            r"requester|uploaded_by|message|excerpt|^text$|body|selector", re.I)


def is_nullish(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() in NULLISH
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def is_masked(v: Any) -> bool:
    return isinstance(v, str) and bool(MASK_RE.search(v)) and not EMAIL_RE.search(v.replace("*", ""))


@dataclass
class Idents:
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    email_hashes: set[str] = field(default_factory=set)
    user_ids: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)

    def direct(self) -> bool:
        return bool(self.emails or self.phones or self.email_hashes or self.names)

    def merge(self, other: "Idents") -> None:
        self.emails |= other.emails
        self.phones |= other.phones
        self.email_hashes |= other.email_hashes
        self.user_ids |= other.user_ids
        self.names |= other.names

    def as_sorted_dict(self) -> dict:
        return {k: sorted(getattr(self, k)) for k in ("emails", "phones", "email_hashes", "user_ids", "names")
                if getattr(self, k)}


def classify_value(value: str, into: Idents, *, key: str = "") -> None:
    """Put one scalar value into the right identifier bucket by its shape (and key, for names)."""
    v = value.strip()
    if not v or v.lower() in NULLISH or is_masked(v):
        return
    k = key.lower()
    if EMAIL_RE.fullmatch(v):
        into.emails.add(norm_email(v))
        return
    if USER_RE.fullmatch(v):
        into.user_ids.add(v.lower())
        return
    if HASH_RE.match(v):
        if "seal" in k or "checksum" in k or "fingerprint" in k:
            return
        into.email_hashes.add(norm_hash(v.split(":", 1)[-1]))
        return
    if ("phone" in k or v.startswith("+")) and PHONE_VALUE_RE.match(v):
        p = norm_phone(v)
        if len(p.lstrip("+")) >= 7:
            into.phones.add(p)
        return
    if "phone" in k:
        return
    if NAME_KEYS.search(k) and not NON_NAME_KEYS.search(k) and not RESOURCE_RE.search(v):
        if re.fullmatch(r"[^\d@/\\]{2,80}", v):
            into.names.add(" ".join(v.split()))
        return
    if FREE_TEXT_KEYS.search(k) or len(v) > 40 or " " in v:
        scan_free_text(v, into)


def scan_free_text(text: str, into: Idents) -> None:
    for m in EMAIL_RE.findall(text):
        into.emails.add(norm_email(m))
    for m in USER_RE.findall(text):
        into.user_ids.add(m.lower())
    for m in PHONE_TEXT_RE.findall(text):
        p = norm_phone(m)
        if len(p.lstrip("+")) >= 8:
            into.phones.add(p)


def flatten(d: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """[(dotted_key, scalar)] for every scalar leaf; list items keep the parent key."""
    out: list[tuple[str, Any]] = []
    if isinstance(d, dict):
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.extend(flatten(v, key))
            else:
                out.append((key, v))
    elif isinstance(d, list):
        for v in d:
            out.extend(flatten(v, prefix))
    else:
        out.append((prefix, d))
    return out


# ---------------------------------------------------------------------------------------------
# Injection scanning
# ---------------------------------------------------------------------------------------------

INJECTION_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"ignore (all |any |the )?(previous|prior|above|earlier|other) (instructions|rules|evidence|guidance)",
    r"disregard (all |any |the )?(previous|prior|above|earlier|other|these)",
    r"\b(mark|report|treat|classify|record) (this|the|it|that)?\s*(case|request|deletion|audit|record)?\s*as\s+(complete|compliant|deleted|absent|resolved)",
    r"\byou (must|should|are required to|are instructed to)\b",
    r"\b(ai|llm|language model|assistant|chatbot|auditor|audit agent|reviewer)s?\s*[:,]",
    r"\b(note|message|instruction)s? (to|for) (the )?(ai|llm|model|assistant|auditor|agent|reviewer)",
    r"\bas an ai\b",
    r"\bsystem prompt\b",
    r"\bnew instructions?\b",
    r"\boverride (the )?(policy|rules|audit|instructions)",
    r"\bdo not (report|flag|include|mention)\b",
    r"\bno (further )?action (is )?(required|needed)\b.*\b(ai|auditor|agent)\b",
))


class InjectionScanner:
    def __init__(self) -> None:
        self.flags: dict[str, str] = {}

    def check(self, file_id: str, text: Any) -> bool:
        if not isinstance(text, str) or len(text) < 8:
            return False
        for pat in INJECTION_PATTERNS:
            m = pat.search(text)
            if m:
                if file_id not in self.flags:
                    start = max(0, m.start() - 40)
                    self.flags[file_id] = text[start:m.end() + 60].replace("\n", " ").strip()[:200]
                return True
        return False


# ---------------------------------------------------------------------------------------------
# Record -> Observation (shared by every structured format)
# ---------------------------------------------------------------------------------------------

ABSENT_WORDS = {"not_found", "notfound", "404", "absent", "missing", "purged", "miss", "no_match",
                "gone", "deleted_confirmed", "410"}
PRESENT_WORDS = {"found", "identifiable", "present", "hit", "exists", "200", "visible", "available"}
REDACTED_WORDS = {"redacted", "masked"}
ANON_WORDS = {"anonymized", "anonymised", "pseudonymized", "unlinked"}
ACTIVE_WORDS = {"active", "enabled", "running", "restore_allowed", "allowed", "allow", "eligible",
                "continuous_sync", "syncing", "on"}
DISABLED_WORDS = {"disabled", "stopped", "paused", "inactive", "off", "suspended"}
BLOCKED_WORDS = {"blocked", "deny", "denied", "restore_blocked", "locked"}

STATUS_KEYS = ("result", "status", "state", "decision", "outcome", "status_code", "http_status")


def _word(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).strip().lower().replace(" ", "_").replace("-", "_") if v is not None else ""


def _truthy(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    w = _word(v)
    if w in ("true", "1", "yes", "y"):
        return True
    if w in ("false", "0", "no", "n"):
        return False
    return None


def derive_state(rec: dict, category: str, idents: Idents, *, lookup_style: bool,
                 pii_schema: bool) -> str:
    """Map what the record shows to a STATE_HINT. Never decides what SHOULD have happened."""
    status_words = [_word(rec.get(k)) for k in STATUS_KEYS if not is_nullish(rec.get(k))]
    # 1. explicit absence
    if any(w in ABSENT_WORDS for w in status_words):
        return "ABSENT"
    rows = rec.get("rows")
    if rows is not None and str(rows).strip().isdigit() and int(str(rows).strip()) == 0:
        return "ABSENT"
    total = rec.get("hits.total", rec.get("total_hits"))
    if total is not None and str(total).strip().isdigit() and int(str(total).strip()) == 0:
        return "ABSENT"
    if "profile" in rec and rec.get("profile") is None:
        return "ABSENT"
    if _truthy(rec.get("marker_found")) is True:
        return "ABSENT"

    # 2. processes: data flows and backups
    if category == "DATA_FLOW":
        en = _truthy(rec.get("enabled"))
        if en is not None:
            return "ACTIVE" if en else "DISABLED"
        if _truthy(rec.get("scheduler_registered")) is False:
            return "DISABLED"
        for w in status_words + [_word(rec.get("last_delivery_status"))]:
            if w in DISABLED_WORDS:
                return "DISABLED"
            if w in ACTIVE_WORDS:
                return "ACTIVE"
        return "UNKNOWN"
    if category == "BACKUP_ARCHIVE":
        ra = _truthy(rec.get("restore_allowed"))
        if ra is not None:
            return "ACTIVE" if ra else "BLOCKED"
        if _truthy(rec.get("deletion_block")) is True:
            return "BLOCKED"
        for w in status_words:
            if w in BLOCKED_WORDS:
                return "BLOCKED"
            if w in ACTIVE_WORDS:
                return "ACTIVE"
        if _truthy(rec.get("eligible")) is True and _truthy(rec.get("deletion_block")) is False:
            return "ACTIVE"
        return "UNKNOWN"

    # 3. explicit treatment words
    if any(w in REDACTED_WORDS for w in status_words):
        return "REDACTED"
    if any(w in ANON_WORDS for w in status_words):
        return "ANONYMIZED"
    di = rec.get("direct_identifiers")
    if di is not None and str(di).strip().isdigit():
        if int(str(di).strip()) > 0:
            return "PRESENT"
    elif lookup_style and any(w in PRESENT_WORDS for w in status_words):
        # a lookup that found the record and shows no record fields of its own
        if not pii_schema:
            return "PRESENT"

    # 4. identifier analysis on the record's own fields
    if idents.direct():
        return "PRESENT"
    if not pii_schema:
        # record kinds that never carry direct identifiers (audit events, legal holds, ...)
        return "PRESENT" if (idents.user_ids or rec.get("resource_id")) else "UNKNOWN"
    if idents.user_ids:
        return "REDACTED"
    return "ANONYMIZED"


def build_observation(
    rec: dict,
    *,
    evidence_id: str,
    manifest_at: datetime,
    source_path: str,
    container_time: Any = None,
    system_names: Iterable[str] = (),
    category_names: Iterable[str] = (),
    extra: Optional[dict] = None,
    lookup_idents: Optional[Idents] = None,
    scanner: Optional[InjectionScanner] = None,
) -> Optional[Observation]:
    """Turn one flat-ish record dict into an Observation.

    rec          : the record's own fields (nested dicts allowed; flattened here).
    lookup_idents: identifiers that were used to look the record up (trace lookup keys); they
                   link the subject to the resource but do not count as fields on the record.
    """
    flat = dict(flatten(rec))
    resource_id = str(rec.get("resource_id") or flat.get("resource_id") or "").strip()
    lookup_style = "result" in rec or "record_type" in rec or "status_code" in rec or "rows" in rec \
        or "http_status" in rec

    on_record = Idents()
    lookups = Idents()
    if lookup_idents:
        lookups.merge(lookup_idents)
    pii_schema = False
    for key, val in flat.items():
        last = key.rsplit(".", 1)[-1].lower()
        if last in ("resource_id", "source_resource_id"):
            continue
        if PII_FIELD_KEYS.search(last) and last not in LOOKUP_KEYS and not isinstance(val, (bool, int, float)) \
                and _word(val) not in ("true", "false"):
            pii_schema = True  # the record has a field that can hold a direct identifier
        if not isinstance(val, str):
            continue
        if scanner is not None and scanner.check(evidence_id, val):
            continue  # untrusted instruction text: never used as data
        # in a lookup ledger row (system/record_type/result), subject_reference is the searched key
        is_lookup = last in LOOKUP_KEYS or (last == "subject_reference" and "result" in rec)
        classify_value(val, lookups if is_lookup else on_record, key=last)

    # a subject_reference / customer_reference holding an email on a non-lookup record is a direct
    # identifier on that record (backup catalog rows, connector selectors) - already classified.
    explicit_system = str(rec.get("system") or "")
    explicit_type = str(rec.get("record_type") or "")
    attach = bool(re.search(r"attachment", " ".join(str(x) for x in category_names), re.I)) or \
        _word(rec.get("storage_class")) == "ticket_attachment" or \
        (resource_prefix(resource_id) == "support" and ("file_name" in rec or "file" in rec))
    system_id = resolve_system(resource_id, explicit=explicit_system, names=system_names)
    category = resolve_category(resource_id, explicit=explicit_type, names=category_names,
                                attachment_hint=attach)
    state = derive_state(flat, category, on_record, lookup_style=lookup_style, pii_schema=pii_schema)

    rec_time = None
    for k in ("recorded_at", "observed_at", "captured_at", "snapshot_at", "checked_at"):
        if rec.get(k):
            rec_time = rec.get(k)
            break
    captured_at, stated = effective_time(manifest_at, rec_time, container_time)

    ex: dict = dict(extra or {})
    status = {k: rec[k] for k in (*STATUS_KEYS, "account_status", "enabled", "restore_allowed",
                                  "last_delivery_status", "rows", "marker_found", "available",
                                  "download_available", "eligible", "deletion_block",
                                  "direct_identifiers", "scheduler_registered", "integrity_check",
                                  "detector", "last_run", "archive_present")
              if k in rec and rec[k] is not None}
    if "hits" in rec and isinstance(rec["hits"], dict):
        status["hits_total"] = rec["hits"].get("total")
    if status:
        ex["status"] = status
    for k in ("retention_until", "retained_until", "expires_at", "created_at", "updated_at",
              "connector_name", "destination", "mode", "archive_key", "archive_path", "object_key",
              "file_name", "file", "index_name", "index", "event_code", "event_name", "ticket_id",
              "order_reference", "retention_reason", "audience_cohort", "channel", "storage_class",
              "included_sections", "last_write", "source", "policy", "integrity_seal"):
        if k in rec and not is_nullish(rec[k]):
            ex.setdefault(k, rec[k])
    if lookups.as_sorted_dict():
        ex["lookup_identifiers"] = lookups.as_sorted_dict()
    if stated is not None and stated != manifest_at:
        ex["manifest_captured_at"] = fmt_ts(manifest_at)
        ex["stated_captured_at"] = fmt_ts(stated)
    refs = sorted({m for _, v in flat.items() if isinstance(v, str) for m in EXTERNAL_REF_RE.findall(v)})
    if refs:
        ex.setdefault("external_refs", refs)

    all_ids = Idents()
    all_ids.merge(on_record)
    all_ids.merge(lookups)
    if not resource_id and not all_ids.as_sorted_dict() and not refs:
        return None
    return Observation(
        evidence_id=evidence_id,
        captured_at=captured_at,
        system_id=system_id,
        resource_id=resource_id,
        category=category,
        state_hint=state,
        emails=all_ids.emails,
        phones=all_ids.phones,
        email_hashes=all_ids.email_hashes,
        user_ids=all_ids.user_ids,
        names=all_ids.names,
        extra=ex,
        source_path=source_path,
    )


def json_default(o: Any) -> Any:
    if isinstance(o, set):
        return sorted(o)
    if isinstance(o, datetime):
        return fmt_ts(o)
    return str(o)


def dumps(o: Any) -> str:
    return json.dumps(o, default=json_default, ensure_ascii=False, sort_keys=True)
