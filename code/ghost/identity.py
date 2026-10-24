"""WS-C: request email -> Subject (every identifier the person has ever had).

Contract
--------
resolve_subject(email, identities, audit_log, as_of) -> Subject | None
    * Match the request email (case-insensitive) against current identities.email OR any
      audit-log old_value/new_value with field_name == "email".
    * Expand to every current + historical email, phone, email_hash (audit log, any time; historical
      identifiers can still appear in old evidence) and the current name.
    * Fill Subject.audit_refs with identifier value -> audit_id for historical values, for citations.
    * Return None when no user matches.
Deterministic. No LLM.

Matching rule when several users could match one email (never happens in the shipped dataset, but
the rule is fixed so the answer is deterministic):
    1. a user whose CURRENT snapshot email equals the request email wins;
    2. otherwise the user whose most recent audit event (event_time, then audit_id) mentions the email
       as old_value or new_value wins;
    3. ties fall back to the smallest user_id (natural sort).

`as_of` does not narrow the identifier set: an identifier retired after audit_at, or introduced before
it, can still sit in older evidence, so every value the person ever had is returned. Callers that need
"was this value current at time T" can use `identifier_timeline`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import (
    AuditEvent,
    Identity,
    Subject,
    norm_email,
    norm_hash,
    norm_name,
    norm_phone,
    resource_sort_key,
)

_NORMALIZERS = {"email": norm_email, "phone": norm_phone, "email_hash": norm_hash}


@dataclass
class IdentityIndex:
    """Lookup tables built once per (identities, audit_log) pair."""
    by_user: dict[str, Identity] = field(default_factory=dict)
    current_email: dict[str, str] = field(default_factory=dict)          # email -> user_id
    history_email: dict[str, list[AuditEvent]] = field(default_factory=dict)  # email -> audit events
    audit_by_user: dict[str, list[AuditEvent]] = field(default_factory=dict)

    @classmethod
    def build(cls, identities: list[Identity], audit_log: list[AuditEvent]) -> "IdentityIndex":
        idx = cls()
        for ident in identities:
            idx.by_user[ident.user_id] = ident
            e = norm_email(ident.email)
            if e:
                # duplicate current emails: keep the smallest user_id (deterministic)
                prev = idx.current_email.get(e)
                if prev is None or resource_sort_key(ident.user_id) < resource_sort_key(prev):
                    idx.current_email[e] = ident.user_id
        for ev in sorted(audit_log, key=lambda a: (a.event_time, resource_sort_key(a.audit_id))):
            idx.audit_by_user.setdefault(ev.user_id, []).append(ev)
            if ev.field_name == "email":
                for v in (ev.old_value, ev.new_value):
                    e = norm_email(v)
                    if e:
                        idx.history_email.setdefault(e, []).append(ev)
        return idx

    # ------------------------------------------------------------------------------------------
    def match_user(self, email: str) -> tuple[str, str] | None:
        """email -> (user_id, how) where how is "current" or "history"."""
        e = norm_email(email)
        if not e:
            return None
        if e in self.current_email:
            return self.current_email[e], "current"
        events = self.history_email.get(e)
        if not events:
            return None
        best = max(events, key=lambda a: (a.event_time, resource_sort_key(a.audit_id)))
        # tie on time across users -> smallest user_id
        same_time = [a for a in events if a.event_time == best.event_time]
        user = min((a.user_id for a in same_time), key=resource_sort_key)
        return user, "history"

    def subject_for_user(self, user_id: str) -> Subject:
        subj = Subject(user_id=user_id)
        ident = self.by_user.get(user_id)
        if ident is not None:
            _add(subj, "email", ident.email)
            _add(subj, "phone", ident.phone)
            _add(subj, "email_hash", ident.email_hash)
            if ident.name.strip():
                subj.names.add(ident.name.strip())
                subj.names.add(norm_name(ident.name))
        for ev in self.audit_by_user.get(user_id, []):
            if ev.field_name not in _NORMALIZERS:
                continue
            for v in (ev.old_value, ev.new_value):
                nv = _add(subj, ev.field_name, v)
                # first (earliest) audit event that mentions the value is the citation
                if nv and nv not in subj.audit_refs:
                    subj.audit_refs[nv] = ev.audit_id
        return subj


def _add(subj: Subject, field_name: str, raw: str) -> str:
    nv = _NORMALIZERS[field_name](raw)
    if not nv:
        return ""
    if field_name == "email":
        subj.emails.add(nv)
    elif field_name == "phone":
        subj.phones.add(nv)
    else:
        subj.email_hashes.add(nv)
    return nv


_CACHE: dict[tuple[int, int, int, int], IdentityIndex] = {}


def get_index(identities: list[Identity], audit_log: list[AuditEvent]) -> IdentityIndex:
    """Module-level cache keyed by list identity and length, so 518 lookups build the index once."""
    key = (id(identities), len(identities), id(audit_log), len(audit_log))
    idx = _CACHE.get(key)
    if idx is None:
        if len(_CACHE) > 8:
            _CACHE.clear()
        idx = IdentityIndex.build(identities, audit_log)
        _CACHE[key] = idx
    return idx


def resolve_subject(email: str, identities: list[Identity], audit_log: list[AuditEvent],
                    as_of: datetime) -> Subject | None:
    idx = get_index(identities, audit_log)
    hit = idx.match_user(email)
    if hit is None:
        return None
    return idx.subject_for_user(hit[0])


def match_kind(email: str, identities: list[Identity], audit_log: list[AuditEvent]) -> str:
    """"current", "history" or "none" - for reporting resolution statistics."""
    hit = get_index(identities, audit_log).match_user(email)
    return hit[1] if hit else "none"


def identifier_timeline(user_id: str, identities: list[Identity],
                        audit_log: list[AuditEvent]) -> list[tuple[datetime, str, str, str, str]]:
    """(event_time, field_name, old_value, new_value, audit_id) for one user, oldest first."""
    idx = get_index(identities, audit_log)
    return [(a.event_time, a.field_name, a.old_value, a.new_value, a.audit_id)
            for a in idx.audit_by_user.get(user_id, [])]
