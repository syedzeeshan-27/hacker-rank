"""WS-C: system_activity.csv -> Observations citing activity_id.

Contract
--------
class ActivityIndex(events)
    observations_for(subject, as_of) -> list[Observation]
        Activity rows are CLAIMS, not proof of record state. Emit observations only for facts the
        stream itself establishes: e.g. a DISABLE_DATA_FLOW / BLOCK_RESTORE / CHECK_DATA_FLOW /
        CHECK_RESTORE_ACCESS / RESTORE_BACKUP / LINK_RESOURCE_IDENTITY / RETAIN_RECORD row tied to the
        subject (via details text, resource_id linked to the subject elsewhere, or
        LINK_RESOURCE_IDENTITY rows). event_time must be <= as_of.
        Map source/target systems to the output vocabulary (integration_service for data flows,
        backup_service for backups, security_service for audit_record_*).
    resources_linked_to(subject) -> set[(system_id, resource_id)]   # from LINK_RESOURCE_IDENTITY etc.
Deterministic. No LLM.

What the stream actually contains (see code/evaluation/notes/ws_c_identity_intent_activity.md):
    * Only the 12 LINK_RESOURCE_IDENTITY rows name an email / user_id. Every other row names just a
      resource id (and, for SYNC_RECORD on data_flow_* and RESTORE_BACKUP, the downstream resource it
      repopulated). So the stream can only be tied to a subject through resource ids, which mostly come
      from evidence files: pass them as `resources=` to observations_for.
    * reported_result is not reliable (sample answers show FAILED deletes that are ABSENT and SUCCESS
      deletes that are PRESENT), so DELETE / REDACT / ANONYMIZE / SYNC / INDEX rows only become
      UNKNOWN-state hints carrying extra["claim"].
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional

from .models import (
    ActivityEvent,
    Observation,
    Subject,
    norm_email,
    resource_sort_key,
)

# resource id prefix -> (system_id, category)
_PREFIX_SYSTEM: list[tuple[str, str, str]] = [
    ("audit_record_", "security_service", "SECURITY_AUDIT_LOG"),
    ("data_flow_", "integration_service", "DATA_FLOW"),
    ("email_delivery_", "email_delivery_service", "EMAIL_DELIVERY_LOG"),
    ("legal_hold_", "legal_service", "LEGAL_HOLD_RECORD"),
    ("account_", "account_service", "USER_PROFILE"),
    ("search_", "search_service", "SEARCH_INDEX_ENTRY"),
    ("support_", "support_platform", "SUPPORT_MESSAGE"),
    ("analytics_", "analytics_platform", "ANALYTICS_PROFILE"),
    ("export_", "export_service", "USER_EXPORT"),
    ("backup_", "backup_service", "BACKUP_ARCHIVE"),
    ("marketing_", "marketing_platform", "MARKETING_PROFILE"),
    ("cache_", "cache_service", "CACHE_ENTRY"),
    ("tombstone_", "identity_service", "ACCOUNT_TOMBSTONE"),
    # object_19 is described as an attachment ("delivery_confirmation_19.pdf") in object_storage.
    # Assumption: attachments belong to the support platform's SUPPORT_ATTACHMENT category.
    ("object_", "support_platform", "SUPPORT_ATTACHMENT"),
]

# raw system names in the stream that are not output vocabulary
_SYSTEM_ALIASES = {
    "object_storage": "support_platform",
    "object_store": "export_service",
    "archive_service": "security_service",
    "account_replication_cluster": "account_service",
    "account_failover_replica": "account_service",
    "privacy_case_management": "account_service",
}

_RES_RE = re.compile(r"\b(?:[a-z]+_)+\d+\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_USER_RE = re.compile(r"\buser_\d+\b")
_LINK_RE = re.compile(r"linked\s+((?:[a-z]+_)+\d+)\s*\(([^)]*)\)|confirmed\s+((?:[a-z]+_)+\d+)\s*\(([^)]*)\)")


def system_for(resource_id: str, raw_system: str = "") -> tuple[str, str]:
    """(system_id, category) for a resource id; falls back to the raw system name."""
    for prefix, system, category in _PREFIX_SYSTEM:
        if resource_id.startswith(prefix):
            return system, category
    return _SYSTEM_ALIASES.get(raw_system, raw_system), ""


def _event_key(ev: ActivityEvent) -> tuple:
    return (ev.event_time, resource_sort_key(ev.activity_id))


class ActivityIndex:
    def __init__(self, events: list[ActivityEvent]):
        self.events = sorted(events, key=_event_key)
        self.by_resource: dict[str, list[ActivityEvent]] = {}
        # resource -> rows whose details name it as the downstream target (SYNC data_flow, RESTORE)
        self.repopulating: dict[str, list[ActivityEvent]] = {}
        # data flow / backup -> rows that say it repopulated a downstream resource
        self.downstream_rows: dict[str, list[ActivityEvent]] = {}
        # LINK_RESOURCE_IDENTITY: (resource_id, alias, emails, user_ids, event)
        self.links: list[tuple[str, str, set[str], set[str], ActivityEvent]] = []
        for ev in self.events:
            self.by_resource.setdefault(ev.resource_id, []).append(ev)
            for rid in sorted(set(_RES_RE.findall(ev.details))):
                if rid != ev.resource_id and not rid.startswith("user_"):
                    if ev.event_type in ("SYNC_RECORD", "RESTORE_BACKUP"):
                        self.repopulating.setdefault(rid, []).append(ev)
                        self.downstream_rows.setdefault(ev.resource_id, []).append(ev)
            if ev.event_type == "LINK_RESOURCE_IDENTITY":
                m = _LINK_RE.search(ev.details)
                alias = ""
                if m:
                    alias = (m.group(2) if m.group(1) else m.group(4)) or ""
                emails = {norm_email(e) for e in _EMAIL_RE.findall(ev.details)}
                users = set(_USER_RE.findall(ev.details))
                self.links.append((ev.resource_id, alias.strip(), emails, users, ev))

    # ------------------------------------------------------------------------------------------
    def _subject_links(self, subject: Subject, as_of: Optional[datetime] = None):
        for rid, alias, emails, users, ev in self.links:
            if as_of is not None and ev.event_time > as_of:
                continue
            if (emails & subject.emails) or (subject.user_id and subject.user_id in users):
                yield rid, alias, emails, users, ev

    def resources_linked_to(self, subject: Subject, as_of: Optional[datetime] = None) -> set[tuple[str, str]]:
        """(system_id, resource_id) pairs a row ties to the subject's identifiers / user_id.

        Direct ties come from LINK_RESOURCE_IDENTITY. A data flow / backup whose row says it
        repopulated one of those resources is tied transitively (one hop), and so is the downstream
        resource that a linked data flow / backup repopulated.
        """
        out: set[tuple[str, str]] = set()
        for rid, _alias, _e, _u, ev in self._subject_links(subject, as_of):
            out.add((system_for(rid, ev.target_system)[0], rid))
        return self._expand(out, as_of)

    def _expand(self, pairs: set[tuple[str, str]], as_of: Optional[datetime]) -> set[tuple[str, str]]:
        out = set(pairs)
        for _sys, rid in list(pairs):
            for ev in self.repopulating.get(rid, []):
                if as_of is None or ev.event_time <= as_of:
                    out.add((system_for(ev.resource_id, ev.source_system)[0], ev.resource_id))
            for ev in self.downstream_rows.get(rid, []):
                if as_of is None or ev.event_time <= as_of:
                    d = _downstream(ev)
                    if d:
                        out.add((system_for(d, ev.target_system)[0], d))
        return out

    def observations_for(self, subject: Subject, as_of: datetime,
                         resources: Optional[Iterable[str | tuple[str, str]]] = None) -> list[Observation]:
        """Observations for resources tied to the subject, from rows with event_time <= as_of.

        resources: extra resource ids (or (system_id, resource_id) pairs) that other evidence already
        ties to the subject. Data flows / backups that repopulated any of them are added (one hop).
        Sorted by (event_time, activity_id, resource_id).
        """
        pairs = self.resources_linked_to(subject, as_of)
        extra_pairs = {(r if isinstance(r, tuple) else (system_for(r)[0], r)) for r in (resources or ())}
        wanted: set[str] = {rid for _s, rid in pairs | self._expand(extra_pairs, as_of)}

        obs: list[Observation] = []
        seen: set[tuple[str, str]] = set()
        # link rows first (they carry identifiers)
        for rid, alias, emails, users, ev in self._subject_links(subject, as_of):
            system, category = system_for(rid, ev.target_system)
            obs.append(Observation(
                evidence_id=ev.activity_id, captured_at=ev.event_time, system_id=system,
                resource_id=rid, category=category, state_hint="UNKNOWN",
                emails=emails & subject.emails, user_ids=users,
                extra={"event_type": ev.event_type, "claim": "linked_to_subject", "alias": alias,
                       "raw_system": ev.target_system, "reported_result": ev.reported_result},
            ))
            seen.add((ev.activity_id, rid))
        for rid in sorted(wanted, key=resource_sort_key):
            for ev in self.by_resource.get(rid, []):
                if ev.event_time > as_of or (ev.activity_id, rid) in seen:
                    continue
                ob = self._observation(ev)
                if ob is not None:
                    obs.append(ob)
                    seen.add((ev.activity_id, rid))
        obs.sort(key=lambda o: (o.captured_at, resource_sort_key(o.evidence_id), resource_sort_key(o.resource_id)))
        return obs

    def observations_for_resources(self, resources: Iterable[str | tuple[str, str]],
                                   as_of: datetime) -> list[Observation]:
        """Same as observations_for without a subject (resource ids from evidence files only)."""
        return self.observations_for(Subject(user_id=""), as_of, resources=resources)

    # ------------------------------------------------------------------------------------------
    @staticmethod
    def _observation(ev: ActivityEvent) -> Optional[Observation]:
        system, category = system_for(ev.resource_id, ev.target_system)
        details = ev.details.lower()
        ok = ev.reported_result == "SUCCESS"
        extra: dict = {"event_type": ev.event_type, "reported_result": ev.reported_result,
                       "raw_source": ev.source_system, "raw_target": ev.target_system}
        state = "UNKNOWN"
        t = ev.event_type

        if t == "DISABLE_DATA_FLOW":
            state = "DISABLED" if ok else "UNKNOWN"
            extra["claim"] = "data_flow_disabled"
        elif t == "BLOCK_RESTORE":
            state = "BLOCKED" if ok else "UNKNOWN"
            extra["claim"] = "restore_blocked"
        elif t == "CHECK_DATA_FLOW":
            if "disabled" in details or "paused" in details or "no longer" in details:
                state = "DISABLED"
            elif "enabled" in details or "active" in details:
                state = "ACTIVE"
            extra["claim"] = "data_flow_check"
        elif t == "CHECK_RESTORE_ACCESS":
            if "blocked" in details or "cannot" in details or "denied" in details:
                state = "BLOCKED"
            elif "can still be restored" in details or "allowed" in details or "restorable" in details:
                state = "ACTIVE"
            extra["claim"] = "restore_access_check"
        elif t == "RESTORE_BACKUP":
            state = "ACTIVE"
            extra["claim"] = "restore_ran"
            extra["downstream"] = _downstream(ev)
        elif t == "RETAIN_RECORD":
            state = "PRESENT" if ok else "UNKNOWN"
            extra["claim"] = "retained"
        elif t == "LINK_RESOURCE_IDENTITY":
            extra["claim"] = "linked_identity"
        elif t == "SYNC_RECORD":
            down = _downstream(ev)
            if ev.resource_id.startswith("data_flow_"):
                # "Active connector repopulated downstream resource search_N": a claim that the flow ran
                extra["claim"] = "connector_repopulated"
                extra["downstream"] = down
            else:
                extra["claim"] = "synced_to:" + ev.target_system
                extra["replica"] = ev.target_system
        elif t == "INDEX_RECORD":
            extra["claim"] = "reindexed"
        elif t in ("DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"):
            extra["claim"] = t.lower() + "_reported_" + ev.reported_result.lower()
        elif t == "CLASSIFY_REQUEST":
            extra["claim"] = "classified_non_deletion" if "non-deletion" in details else "classified"
        else:
            # UPDATE_RECORD, REFRESH_CACHE, SEND_EMAIL, CREATE_EXPORT: routine processing claims
            extra["claim"] = t.lower()
        return Observation(
            evidence_id=ev.activity_id, captured_at=ev.event_time, system_id=system,
            resource_id=ev.resource_id, category=category, state_hint=state, extra=extra,
        )


def _downstream(ev: ActivityEvent) -> str:
    ids = [r for r in _RES_RE.findall(ev.details) if r != ev.resource_id]
    return ids[0] if ids else ""
