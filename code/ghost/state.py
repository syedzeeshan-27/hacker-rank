"""WS-D: Observations + coverage + activity -> ResourceState per resource, as of case.audit_at.

Entry point
-----------
reconstruct(case, subject, extraction, activity_index, context=None)
    -> dict[(system_id, resource_id), ResourceState]

`context` (an AuditContext) holds the indexes built once per run (evidence index, deletion-run index,
run assignment across the request queue). planner.audit_case builds a default one from the dataset when
the caller passes none; custom evaluation cases pass their own.

Time rules (the core of "respect audit_at")
-------------------------------------------
* A file is *eligible* for a case when its manifest captured_at <= audit_at. Only eligible files give
  states and citations. The manifest time is the file's delivery time; a bundle delivered after audit_at
  is never cited, even if a snapshot inside it is older (Observation.extra["manifest_captured_at"]).
* Inside an eligible file the fact time is Observation.captured_at (the inner snapshot / line time).
* Identity linkage is separate from state. Which account / record belongs to which person does not
  change over time, so a tie ("resource X belongs to user U") may come from ANY file, including one
  captured after audit_at (LINKAGE_IGNORES_AUDIT_CUTOFF). Such a tie only says which resources to look
  at; the resource's state and citation always come from eligible files only (_facts_for).
* When nothing ties an account to the person, the account id implied by a zero-padded user id is used
  (identity.derived_account_id, the one place that pattern lives); its state still comes from
  eligible evidence.
* Activity rows count only when event_time <= audit_at.

How a resource is tied to the subject (Link.reasons)
----------------------------------------------------
"identifier"  an observation on the resource carries the subject's email / phone / email hash / user_id,
              or the subject's name with no conflicting identifier (names shared by two identities are
              not used). Lookup keys count too: a 404 for a lookup by the subject's email ties that id.
"catalog"     a LINK_RESOURCE_IDENTITY activity row names the subject (plus the one-hop data flow /
              backup expansion done by ActivityIndex).
"run"         deletion-run reconstruction (see RunIndex): the deletion worker's run for this request.
              Its account row is the subject's account record and its follow-up rows name the other
              resources it processed (analytics profiles and tombstones often carry no identifier).

How a state is chosen
---------------------
* Explicit observations (state_hint != UNKNOWN) from eligible files, grouped by copy (primary,
  read_replica, failover_replica, regional_replica:<r>, reporting_replica). Latest per copy by
  (fact time, manifest time, evidence_id natural order).
* A snapshot Coverage (kind "snapshot") that enumerates the resource's system in some copy without
  listing the resource is an ABSENT fact for that copy at the coverage time. It is used when the copy has
  no explicit observation, or when it is newer than an explicit non-ABSENT one. An explicit ABSENT (404,
  rows=0) is preferred over a coverage-derived ABSENT.
* A copy whose latest fact predates the deletion request is stale when another copy has newer facts
  (pre-request data says nothing about whether the deletion worked).
* Across the remaining copies the worst state wins (PRESENT > REDACTED > ANONYMIZED > ABSENT; ACTIVE >
  DISABLED / BLOCKED): one surviving replica copy keeps the record PRESENT.
* Activity rows supply a state only when no eligible file shows the resource (CHECK_DATA_FLOW
  "remains enabled", BLOCK_RESTORE / DISABLE_DATA_FLOW / RETAIN_RECORD claims); they are claims, so a
  file always wins.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from .activity import ActivityIndex, system_for
from .evidence import ExtractionResult
from .identity import derived_account_id
from .models import (
    ActivityEvent,
    Case,
    Observation,
    ResourceState,
    Subject,
    norm_name,
    parse_ts,
    resource_sort_key,
)

# ---------------------------------------------------------------------------------------------
# Tunables (documented in code/evaluation/notes/ws_d_engine.md)
# ---------------------------------------------------------------------------------------------

# Identity linkage (which resource belongs to whom) uses every file, whatever its capture time; only
# states and citations are subject to the audit_at cutoff. Set False to also restrict linkage.
LINKAGE_IGNORES_AUDIT_CUTOFF = True

# Deletion-run reconstruction. Derived from the shape of system_activity.csv (not from answers):
# the deletion worker dispatches a run's follow-up actions at a fixed cadence, so consecutive follow-ups
# of one run are separated by one of these gaps (seconds, +-1), and the first follow-up sits a whole number
# of minutes plus a fixed phase after the run's account deletion row.
RUN_CADENCE_GAPS = (390, 449, 1110, 1169)
RUN_PHASES = (41, 52)
RUN_TOLERANCE_S = 1
RUN_FIRST_FOLLOWUP_MIN_S = 20 * 60
RUN_FIRST_FOLLOWUP_MAX_S = 4 * 3600
# The run's account row is dispatched before the workflow reports its status.
RUN_HEAD_WINDOW = (timedelta(minutes=-80), timedelta(minutes=-40))   # relative to reported_at
RUN_HEAD_NEAR_WINDOW = (timedelta(minutes=-10), timedelta(minutes=10))  # fallback when the main window is empty
DELETION_WORKER = "deletion_worker"
# Snapshot slices are partial: "not listed" is only read as ABSENT for records the policy wants gone.
# Retained records, archives and connectors are never inferred absent / inactive from an omission.
NO_COVERAGE_ABSENCE = frozenset({"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "DATA_FLOW"})


def default_violation_states(category: str) -> frozenset:
    """States that would make a finding for a category (planner passes the exact, policy-driven set)."""
    if category in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
        return frozenset()
    if category in ("DATA_FLOW", "BACKUP_ARCHIVE"):
        return frozenset({"ACTIVE"})
    return frozenset({"PRESENT", "REDACTED", "ANONYMIZED"})

STATE_RANK = {"PRESENT": 4, "REDACTED": 3, "ANONYMIZED": 2, "ACTIVE": 4, "UNKNOWN": 1,
              "DISABLED": 0, "BLOCKED": 0, "ABSENT": 0}

# Status values that mark a record as queued for deletion rather than listed as part of the store.
PENDING_WORDS = frozenset({"deletion_pending", "pending_deletion", "pending", "queued",
                           "scheduled_for_deletion", "awaiting_deletion"})


def _status_word(v) -> str:
    return str(v).strip().lower().replace(" ", "_").replace("-", "_")

_REPLICA_SCOPE = [
    (re.compile(r"failover"), "failover_replica"),
    (re.compile(r"read_replica"), "read_replica"),
    (re.compile(r"reporting_replica"), "reporting_replica"),
    (re.compile(r"replica_([a-z]+_[a-z]+)"), "regional_replica:{0}"),
]


def file_time(o: Observation) -> datetime:
    """Delivery (manifest) time of the file an observation came from."""
    m = o.extra.get("manifest_captured_at") if isinstance(o.extra, dict) else None
    if m:
        try:
            return parse_ts(m) if isinstance(m, str) else m
        except ValueError:
            pass
    return o.captured_at


def copy_of(o: Observation) -> str:
    role = (o.extra or {}).get("copy_role") or "primary"
    return str(role)


def copy_of_scope(scope: str) -> str:
    s = scope.lower()
    for rx, label in _REPLICA_SCOPE:
        m = rx.search(s)
        if m:
            return label.format(*m.groups()) if m.groups() else label
    return "primary"


def _ev_key(evidence_id: str) -> tuple:
    return resource_sort_key(evidence_id)


# ---------------------------------------------------------------------------------------------
# Evidence index
# ---------------------------------------------------------------------------------------------

class EvidenceIndex:
    """Observations and coverage grouped for fast per-case lookups. Built once per extraction."""

    def __init__(self, extraction: ExtractionResult):
        self.extraction = extraction
        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        self.by_email: dict[str, list[Observation]] = defaultdict(list)
        self.by_phone: dict[str, list[Observation]] = defaultdict(list)
        self.by_hash: dict[str, list[Observation]] = defaultdict(list)
        self.by_user: dict[str, list[Observation]] = defaultdict(list)
        self.by_name: dict[str, list[Observation]] = defaultdict(list)
        self.by_ref: dict[str, list[Observation]] = defaultdict(list)   # resource-less, by external ref
        for o in extraction.observations:
            if o.resource_id:
                self.by_resource[o.resource_id].append(o)
            else:
                for ref in (o.extra or {}).get("external_refs", []) or []:
                    self.by_ref[str(ref)].append(o)
            for e in o.emails:
                self.by_email[e].append(o)
            for p in o.phones:
                self.by_phone[p].append(o)
            for h in o.email_hashes:
                self.by_hash[h].append(o)
            for u in o.user_ids:
                self.by_user[u].append(o)
            for n in o.names:
                self.by_name[norm_name(n)].append(o)
        # delivery (manifest) time per file; coverage entries carry only their inner capture time
        self.file_time: dict[str, datetime] = {}
        for o in extraction.observations:
            ft = file_time(o)
            if o.evidence_id not in self.file_time or ft > self.file_time[o.evidence_id]:
                self.file_time[o.evidence_id] = ft
        for c in extraction.coverage:
            self.file_time.setdefault(c.evidence_id, c.captured_at)
        self.coverage_by_system: dict[str, list] = defaultdict(list)
        for c in extraction.coverage:
            if c.kind == "snapshot":
                self.coverage_by_system[c.system_id].append(c)
        # Work queues: a source whose slice of a system only ever lists records that are pending
        # deletion is a deletion queue, not an inventory of that system. "Not listed" there says
        # nothing, so such slices never imply absence (see _facts_for).
        origin_of = {c.evidence_id: c.origin for c in extraction.coverage if c.origin}
        seen: dict[tuple[str, str], list[bool]] = defaultdict(list)
        for o in extraction.observations:
            org = origin_of.get(o.evidence_id)
            if not org or not o.resource_id or o.state_hint in ("", "UNKNOWN"):
                continue
            st = (o.extra or {}).get("status") or {}
            words = {_status_word(v) for v in st.values() if v is not None}
            seen[(org, o.system_id)].append(bool(words & PENDING_WORDS))
        self.queue_slices: frozenset = frozenset(k for k, flags in seen.items() if flags and all(flags))

    def candidates(self, subject: Subject, ambiguous_names: frozenset = frozenset()) -> list[tuple[Observation, str]]:
        """(observation, how) for every observation that names the subject. Deterministic order."""
        seen: dict[int, tuple[Observation, str]] = {}
        for table, values, how in (
            (self.by_email, subject.emails, "email"),
            (self.by_phone, subject.phones, "phone"),
            (self.by_hash, subject.email_hashes, "email_hash"),
            (self.by_user, {subject.user_id} if subject.user_id else set(), "user_id"),
        ):
            for v in sorted(values):
                for o in table.get(v, ()):
                    seen.setdefault(id(o), (o, how))
        names = {norm_name(n) for n in subject.names if n.strip()} - set(ambiguous_names)
        for n in sorted(names):
            for o in self.by_name.get(n, ()):
                if id(o) in seen:
                    continue
                if _conflicts(o, subject):
                    continue
                seen.setdefault(id(o), (o, "name"))
        return list(seen.values())


def _conflicts(o: Observation, subject: Subject) -> bool:
    """True when the observation carries identifiers that belong to somebody else."""
    if o.user_ids and subject.user_id not in o.user_ids:
        return True
    if o.emails and not (o.emails & subject.emails):
        return True
    if o.phones and not (o.phones & subject.phones):
        return True
    if o.email_hashes and not (o.email_hashes & subject.email_hashes):
        return True
    return False


# ---------------------------------------------------------------------------------------------
# Deletion-run reconstruction
# ---------------------------------------------------------------------------------------------

class RunIndex:
    """Groups the deletion worker's rows into runs.

    head      : a deletion_worker DELETE_RECORD row on an account_* resource (one per request).
    follow-up : every other deletion_worker row (DELETE / REDACT / ANONYMIZE / DISABLE_DATA_FLOW /
                BLOCK_RESTORE / CHECK_* / RETAIN_RECORD on non-account resources).
    chain     : follow-ups linked by the dispatch cadence (RUN_CADENCE_GAPS).
    A chain belongs to a head when the chain's first row is RUN_FIRST_FOLLOWUP_MIN_S..MAX_S after the
    head and the offset has one of the RUN_PHASES as its seconds part.
    """

    def __init__(self, events: list[ActivityEvent]):
        rows = sorted((e for e in events if e.source_system == DELETION_WORKER),
                      key=lambda e: (e.event_time, resource_sort_key(e.activity_id)))
        self.heads = [e for e in rows if e.event_type == "DELETE_RECORD"
                      and system_for(e.resource_id, e.target_system)[0] == "account_service"]
        head_ids = {e.activity_id for e in self.heads}
        self.followups = [e for e in rows if e.activity_id not in head_ids]
        self.by_id = {e.activity_id: e for e in rows}
        self.followups_by_resource: dict[str, list[ActivityEvent]] = defaultdict(list)
        for e in self.followups:
            self.followups_by_resource[e.resource_id].append(e)
        self.heads_by_resource: dict[str, list[ActivityEvent]] = defaultdict(list)
        for e in self.heads:
            self.heads_by_resource[e.resource_id].append(e)
        # chains
        self.chain_of: dict[str, str] = {}
        self.chain_members: dict[str, list[ActivityEvent]] = defaultdict(list)
        by_second: dict[int, list[ActivityEvent]] = defaultdict(list)
        for e in self.followups:
            by_second[int(e.event_time.timestamp())].append(e)
        for e in self.followups:   # time order, so a predecessor is always resolved first
            t = int(e.event_time.timestamp())
            pred = None
            for gap in RUN_CADENCE_GAPS:
                for d in range(-RUN_TOLERANCE_S, RUN_TOLERANCE_S + 1):
                    for p in by_second.get(t - gap + d, ()):
                        if p.activity_id in self.chain_of and (pred is None or _row_key(p) < _row_key(pred)):
                            pred = p
            start = self.chain_of[pred.activity_id] if pred is not None else e.activity_id
            self.chain_of[e.activity_id] = start
            self.chain_members[start].append(e)
        # chain -> fitting heads
        self.chain_heads: dict[str, list[str]] = {}
        self.head_chains: dict[str, list[str]] = defaultdict(list)
        for start in self.chain_members:
            first = self.by_id[start]
            fits = []
            for h in self.heads:
                d = (first.event_time - h.event_time).total_seconds()
                if RUN_FIRST_FOLLOWUP_MIN_S <= d <= RUN_FIRST_FOLLOWUP_MAX_S and _phase_ok(d):
                    fits.append(h.activity_id)
            self.chain_heads[start] = fits
            for h in fits:
                self.head_chains[h].append(start)

    def heads_in_window(self, case: Case, window: tuple[timedelta, timedelta]) -> list[ActivityEvent]:
        lo, hi = case.reported_at + window[0], case.reported_at + window[1]
        return [h for h in self.heads if lo <= h.event_time <= hi and h.event_time <= case.audit_at]

    def chains_for_resources(self, resources: Iterable[str], case: Case) -> list[str]:
        out = []
        for r in sorted(set(resources), key=resource_sort_key):
            for e in self.followups_by_resource.get(r, ()):
                if case.requested_at <= e.event_time <= case.audit_at:
                    s = self.chain_of[e.activity_id]
                    if s not in out:
                        out.append(s)
        return out

    def run_resources(self, head_id: str, case: Case, anchors: Iterable[str] = ()) -> list[ActivityEvent]:
        """Follow-up rows of the run started by head_id (event_time <= audit_at).

        A chain counts when it contains a resource already tied to the subject (anchor) and fits the
        head within the tolerance, or - with no subject resource on it - when its first row sits at an
        exact run phase from the head (a looser fit is too often a neighbouring run's row)."""
        anchors = set(anchors)
        head = self.by_id[head_id]
        anchored, exact = [], []
        for start in self.head_chains.get(head_id, ()):
            members = self.chain_members[start]
            if any(m.resource_id in anchors for m in members):
                anchored.append(start)
            else:
                d = int(round((self.by_id[start].event_time - head.event_time).total_seconds()))
                if d % 60 in RUN_PHASES:
                    exact.append(start)
        chosen = anchored if anchored else (exact if len(exact) == 1 else [])
        out = []
        for start in chosen:
            for e in self.chain_members[start]:
                if e.event_time <= case.audit_at:
                    out.append(e)
        return out


def _row_key(e: ActivityEvent) -> tuple:
    return (e.event_time, resource_sort_key(e.activity_id))


def _phase_ok(d: float) -> bool:
    sec = int(round(d)) % 60
    return any(min(abs(sec - p), 60 - abs(sec - p)) <= RUN_TOLERANCE_S for p in RUN_PHASES)


# ---------------------------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------------------------

@dataclass
class PoolEntry:
    case: Case
    subject: Subject


@dataclass
class AuditContext:
    """Per-run indexes. `pool` is the request queue used to tell runs apart (all requests that
    authorize deletion, each with its resolved subject); it may be empty."""
    evidence: EvidenceIndex
    runs: RunIndex
    pool: list[PoolEntry] = field(default_factory=list)
    ambiguous_names: frozenset = frozenset()
    _claims_cache: dict = field(default_factory=dict)

    # catalog aliases: resource_id -> [(alias, event_time)] from LINK_RESOURCE_IDENTITY rows
    aliases: dict[str, list] = field(default_factory=dict)

    @classmethod
    def build(cls, extraction: ExtractionResult, activity_index: ActivityIndex,
              pool: Optional[list[PoolEntry]] = None, ambiguous_names: Iterable[str] = ()) -> "AuditContext":
        aliases: dict[str, list] = defaultdict(list)
        for rid, alias, _emails, _users, ev in getattr(activity_index, "links", []):
            if alias and alias != rid:
                aliases[rid].append((alias, ev.event_time))
        return cls(evidence=EvidenceIndex(extraction), runs=RunIndex(activity_index.events),
                   pool=list(pool or []), ambiguous_names=frozenset(ambiguous_names), aliases=dict(aliases))

    def observations_on(self, resource_id: str, as_of: datetime) -> list[Observation]:
        """Observations on a resource, plus resource-less ones (statements, registers) whose external
        reference is a catalog alias of it (LINK_RESOURCE_IDENTITY row at or before as_of)."""
        out = list(self.evidence.by_resource.get(resource_id, ()))
        for alias, t in self.aliases.get(resource_id, ()):
            if t <= as_of:
                out.extend(self.evidence.by_ref.get(alias, ()))
        return out


# ---------------------------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------------------------

@dataclass
class Link:
    system_id: str
    resource_id: str
    reasons: set[str] = field(default_factory=set)
    category: str = ""


def _add_link(links: dict, system_id: str, resource_id: str, reason: str, category: str = "") -> None:
    if not resource_id or not system_id:
        return
    key = (system_id, resource_id)
    lk = links.get(key)
    if lk is None:
        lk = links[key] = Link(system_id, resource_id)
    lk.reasons.add(reason)
    if category and not lk.category:
        lk.category = category


def _linkable(o: Observation, case: Case) -> bool:
    """Identity linkage ignores the audit cutoff (ownership does not change); state never does."""
    if LINKAGE_IGNORES_AUDIT_CUTOFF:
        return True
    return o.captured_at <= case.audit_at and file_time(o) <= case.audit_at


def direct_links(case: Case, subject: Subject, ctx: AuditContext) -> dict[tuple[str, str], Link]:
    """Resources tied to the subject by identifiers in evidence (no run logic). Linkage only: a resource
    found here still gets its state and citation from files eligible for the case."""
    links: dict[tuple[str, str], Link] = {}
    for o, how in ctx.evidence.candidates(subject, ctx.ambiguous_names):
        if not o.resource_id or not _linkable(o, case):
            continue
        _add_link(links, o.system_id or system_for(o.resource_id)[0], o.resource_id, "identifier", o.category)
    return links


def catalog_links(case: Case, subject: Subject, activity_index: ActivityIndex) -> set[tuple[str, str]]:
    return set(activity_index.resources_linked_to(subject, as_of=case.audit_at))


@dataclass
class RunChoice:
    head_id: str = ""
    account_id: str = ""
    how: str = ""                       # "evidence" | "cadence" | "window" | "elimination" | ""
    candidates: list[str] = field(default_factory=list)


def _strong_heads(case: Case, linked: dict[tuple[str, str], Link], runs: RunIndex) -> tuple[list[str], list[str], str]:
    """(strong candidate head ids, window head ids, how) for one request.

    strong: heads on an account the evidence ties to the subject (inside the windows), or heads that the
    cadence ties to a chain containing a subject resource (inside the main window).
    """
    window = [h.activity_id for h in runs.heads_in_window(case, RUN_HEAD_WINDOW)]
    if not window:
        window = [h.activity_id for h in runs.heads_in_window(case, RUN_HEAD_NEAR_WINDOW)]
    near = set(window) | {h.activity_id for h in runs.heads_in_window(case, RUN_HEAD_NEAR_WINDOW)}
    accounts = sorted({r for (s, r) in linked if s == "account_service"}, key=resource_sort_key)
    acc_heads = [h.activity_id for a in accounts for h in runs.heads_by_resource.get(a, ())
                 if h.activity_id in near]
    if acc_heads:
        return acc_heads, window, "evidence"
    chains = runs.chains_for_resources([r for (_s, r) in linked], case)
    fit = []
    for st in chains:
        for h in runs.chain_heads.get(st, ()):
            if h in window and h not in fit:
                fit.append(h)
    if fit:
        return fit, window, "cadence"
    return [], window, ""


def _claims(ctx: AuditContext, activity_index: ActivityIndex, as_of: datetime) -> dict[str, str]:
    """head_id -> case_id for heads that other requests in the pool claim, as known at as_of.

    Every request submitted by as_of is linked with what was known at min(its audit_at, as_of). A
    request with exactly one strong candidate claims it; then, repeatedly, a request whose remaining
    candidates (strong if any, else its window) shrink to one claims that one.
    """
    key = as_of
    if key in ctx._claims_cache:
        return ctx._claims_cache[key]
    entries = []
    for pe in ctx.pool:
        c = pe.case
        if c.requested_at > as_of:
            continue
        view = Case(c.case_id, c.email, c.requested_at, c.reported_status, c.reported_at,
                    min(c.audit_at, as_of), c.request_text)
        linked = direct_links(view, pe.subject, ctx)
        for s, r in catalog_links(view, pe.subject, activity_index):
            _add_link(linked, s, r, "catalog")
        strong, window, _how = _strong_heads(view, linked, ctx.runs)
        entries.append((c.case_id, strong, window))
    entries.sort(key=lambda x: resource_sort_key(x[0]))
    claimed: dict[str, str] = {}
    done: set[str] = set()
    changed = True
    while changed:
        changed = False
        for cid, strong, window in entries:
            if cid in done:
                continue
            cand = strong or window
            cand = [h for h in cand if claimed.get(h, cid) == cid]
            if len(cand) == 1:
                claimed[cand[0]] = cid
                done.add(cid)
                changed = True
    ctx._claims_cache[key] = claimed
    return claimed


def _owned_by_other(resource_id: str, subject: Subject, case: Case, ctx: AuditContext) -> bool:
    """Ownership is identity linkage, so any file may show that a resource belongs to someone else."""
    for o in ctx.evidence.by_resource.get(resource_id, ()):
        if not _linkable(o, case):
            continue
        if (o.emails or o.user_ids or o.phones or o.email_hashes) and not o.mentions(subject) \
                and _conflicts(o, subject):
            return True
    return False


def _has_eligible_fact(resource_id: str, case: Case, ctx: AuditContext) -> bool:
    return any(file_time(o) <= case.audit_at and o.captured_at <= case.audit_at
               and o.state_hint not in ("", "UNKNOWN")
               for o in ctx.evidence.by_resource.get(resource_id, ()))


def choose_run(case: Case, linked: dict[tuple[str, str], Link], ctx: AuditContext,
               activity_index: ActivityIndex, subject: Optional[Subject] = None) -> RunChoice:
    subject = subject or Subject(user_id="")
    strong, window, how = _strong_heads(case, linked, ctx.runs)
    if len(strong) == 1:
        cand = strong
    else:
        cand = strong if strong else window
        steps = []
        if len(cand) > 1 and ctx.pool:
            claims = _claims(ctx, activity_index, case.audit_at)
            rest = [h for h in cand if claims.get(h, case.case_id) == case.case_id]
            if len(rest) < len(cand):
                steps.append("elimination")
            cand = rest
        if len(cand) > 1:
            # an account whose eligible evidence names somebody else is not this subject's
            rest = [h for h in cand if not _owned_by_other(ctx.runs.by_id[h].resource_id, subject, case, ctx)]
            if len(rest) < len(cand):
                steps.append("owner")
            cand = rest
        if len(cand) > 1:
            # among the remaining runs prefer the one whose account the evidence exports looked up
            rest = [h for h in cand if _has_eligible_fact(ctx.runs.by_id[h].resource_id, case, ctx)]
            if rest:
                if len(rest) < len(cand):
                    steps.append("evidenced")
                cand = rest
        if not how:
            how = "window"          # timing based (last resort): documented in ws_d_engine.md
        if steps:
            how = "+".join([how] + steps)
    if len(cand) != 1:
        return RunChoice(candidates=sorted(cand, key=resource_sort_key))
    h = ctx.runs.by_id[cand[0]]
    return RunChoice(head_id=h.activity_id, account_id=h.resource_id, how=how, candidates=list(cand))


def link_resources(case: Case, subject: Subject, ctx: AuditContext,
                   activity_index: ActivityIndex) -> tuple[dict[tuple[str, str], Link], RunChoice]:
    links = direct_links(case, subject, ctx)
    for s, r in catalog_links(case, subject, activity_index):
        _add_link(links, s, r, "catalog")
    run = choose_run(case, links, ctx, activity_index, subject)
    if run.head_id:
        _add_link(links, "account_service", run.account_id, "run:" + run.how, "USER_PROFILE")
        anchors = [r for (_s, r) in links]
        for e in ctx.runs.run_resources(run.head_id, case, anchors):
            if _owned_by_other(e.resource_id, subject, case, ctx):
                continue        # a neighbouring run's row that happens to fit the cadence
            s, cat = system_for(e.resource_id, e.target_system)
            _add_link(links, s, e.resource_id, "run", cat)
    if not any(s == "account_service" for (s, _r) in links):
        # last resort: the account id implied by the user id (identity.derived_account_id)
        derived = derived_account_id(subject.user_id)
        if derived and not _owned_by_other(derived, subject, case, ctx):
            _add_link(links, "account_service", derived, "derived", "USER_PROFILE")
    return links, run


# ---------------------------------------------------------------------------------------------
# State reconstruction
# ---------------------------------------------------------------------------------------------

@dataclass
class _Fact:
    state: str
    evidence_id: str
    captured_at: datetime
    filed_at: datetime
    copy: str
    explicit: bool
    obs: Optional[Observation] = None

    def key(self) -> tuple:
        return (self.captured_at, self.filed_at, _ev_key(self.evidence_id))


def _facts_for(system_id: str, resource_id: str, case: Case, ctx: AuditContext) -> list[_Fact]:
    facts: list[_Fact] = []
    for o in ctx.observations_on(resource_id, case.audit_at):
        if o.state_hint in ("", "UNKNOWN"):
            continue
        if o.resource_id and o.system_id and o.system_id != system_id:
            continue
        ft = file_time(o)
        if ft > case.audit_at or o.captured_at > case.audit_at:
            continue
        facts.append(_Fact(o.state_hint, o.evidence_id, o.captured_at, ft, copy_of(o), True, o))
    # coverage-derived absence (snapshot that enumerates the system without this resource)
    by_ev_copy: dict[tuple[str, str], list] = defaultdict(list)
    for c in ctx.evidence.coverage_by_system.get(system_id, ()):
        if c.captured_at > case.audit_at or ctx.evidence.file_time.get(c.evidence_id, c.captured_at) > case.audit_at:
            continue
        if (c.origin, c.system_id) in ctx.evidence.queue_slices:
            continue    # a work queue (every row it ever lists is pending deletion) enumerates nothing
        by_ev_copy[(c.evidence_id, copy_of_scope(c.scope))].append(c)
    for (ev, cp), covs in by_ev_copy.items():
        if any(resource_id in c.resource_ids for c in covs):
            continue
        t = max(c.captured_at for c in covs)
        facts.append(_Fact("ABSENT", ev, t, ctx.evidence.file_time.get(ev, t), cp, False))
    return facts


def _pick_copy_state(facts: list[_Fact], requested_at: datetime) -> Optional[_Fact]:
    explicit = [f for f in facts if f.explicit]
    implied = [f for f in facts if not f.explicit]
    e = max(explicit, key=_Fact.key) if explicit else None
    c = max(implied, key=_Fact.key) if implied else None
    if e is None:
        return c
    if e.state == "ABSENT" or c is None:
        return e
    # Snapshot slices are partial, so a later snapshot that does not list the record overrides an
    # explicit sighting only when that sighting predates the deletion request (pre-request presence is
    # expected; a post-request sighting stays authoritative).
    if e.state in ("PRESENT", "REDACTED", "ANONYMIZED") and e.captured_at < requested_at             and c.captured_at > e.captured_at and c.captured_at >= requested_at:
        return c
    return e


def resource_state(system_id: str, resource_id: str, category: str, case: Case,
                   ctx: AuditContext, activity_obs: list[Observation],
                   violation_states: Optional[frozenset] = None) -> Optional[ResourceState]:
    if violation_states is None:
        violation_states = default_violation_states(category)

    def pre_request_violation(f: "_Fact") -> bool:
        # A sighting from before the request says nothing about whether the deletion worked, so it
        # cannot establish a remaining record / exposed PII / active flow / restore risk. It may still
        # have linked the resource to the subject.
        return f.state in violation_states and f.captured_at < case.requested_at

    facts = _facts_for(system_id, resource_id, case, ctx)
    if category in NO_COVERAGE_ABSENCE:
        facts = [f for f in facts if f.explicit]
    explicit_any = any(f.explicit for f in facts)
    per_copy: dict[str, _Fact] = {}
    if explicit_any:
        groups: dict[str, list[_Fact]] = defaultdict(list)
        for f in facts:
            groups[f.copy].append(f)
        for cp, fs in groups.items():
            pick = _pick_copy_state(fs, case.requested_at)
            if pick is not None:
                per_copy[cp] = pick
        # copies that only ever had coverage (never held the record explicitly) say nothing new
        explicit_copies = {f.copy for f in facts if f.explicit}
        per_copy = {cp: f for cp, f in per_copy.items() if cp in explicit_copies or cp == "primary"}
        # stale copies: latest fact before the request while another copy is newer
        if len(per_copy) > 1:
            newest = max(f.captured_at for f in per_copy.values())
            per_copy = {cp: f for cp, f in per_copy.items()
                        if not (f.captured_at < case.requested_at and f.captured_at < newest)}
        # (stale-copy rule above drops pre-request copies that a newer copy supersedes; this one also
        # drops a lone pre-request copy when its state would be a violation)
        per_copy = {cp: f for cp, f in per_copy.items() if not pre_request_violation(f)}
        chosen = max(per_copy.values(), key=lambda f: (STATE_RANK.get(f.state, 1),) + f.key()) if per_copy else None
    else:
        implied = [f for f in facts if not f.explicit]
        chosen = None
        if implied:
            # no explicit fact: the newest snapshot that enumerates the system decides
            chosen = max(implied, key=_Fact.key)
        if chosen is None:
            act = [o for o in activity_obs if o.resource_id == resource_id and o.state_hint not in ("", "UNKNOWN")]
            if act:
                o = max(act, key=lambda o: (o.captured_at, _ev_key(o.evidence_id)))
                chosen = _Fact(o.state_hint, o.evidence_id, o.captured_at, o.captured_at, "activity", True, o)
                if pre_request_violation(chosen):
                    chosen = None
    if chosen is None:
        return None
    obs = chosen.obs
    exposes = bool(obs and (obs.emails or obs.phones or obs.email_hashes or obs.names)
                   and not (obs.extra or {}).get("lookup_identifiers") and chosen.state == "PRESENT")
    linked_users = set(obs.user_ids) if obs else set()
    extra = dict(obs.extra) if obs else {}
    extra["copy"] = chosen.copy
    extra["explicit"] = chosen.explicit
    return ResourceState(system_id=system_id, resource_id=resource_id, category=category,
                         state=chosen.state, evidence_id=chosen.evidence_id, captured_at=chosen.captured_at,
                         exposes_pii=exposes, linked_user_ids=linked_users, extra=extra)


def category_for(system_id: str, resource_id: str, link: Link, ctx: AuditContext, case: Case) -> str:
    """Most specific category seen on the resource's eligible observations, else the link / id prefix."""
    cats = []
    for o in ctx.evidence.by_resource.get(resource_id, ()):
        if o.category and o.captured_at <= case.audit_at:
            cats.append(o.category)
    if "SUPPORT_ATTACHMENT" in cats:
        return "SUPPORT_ATTACHMENT"
    if system_id == "support_platform" and set(cats) <= {"SUPPORT_MESSAGE"}:
        # The evidence never states whether a support_* record is a conversation or a stored copy
        # (attachment, legacy contact index); the platform's own deletion run classifies it: it
        # DELETEs stored copies and REDACTs conversations. Only the operation type is used, never
        # its reported result.
        ops = {e.event_type for e in ctx.runs.followups_by_resource.get(resource_id, ())
               if e.event_time <= case.audit_at}
        if "DELETE_RECORD" in ops and "REDACT_RECORD" not in ops:
            return "SUPPORT_ATTACHMENT"
    if cats:
        # most frequent, ties alphabetical (deterministic)
        best = sorted(set(cats), key=lambda c: (-cats.count(c), c))[0]
        return best
    return link.category or system_for(resource_id)[1]


def reconstruct(case: Case, subject: Subject, extraction: ExtractionResult,
                activity_index: ActivityIndex, context: Optional[AuditContext] = None
                ) -> dict[tuple[str, str], ResourceState]:
    states, _links, _run = reconstruct_detailed(case, subject, extraction, activity_index, context)
    return states


def reconstruct_detailed(case: Case, subject: Subject, extraction: ExtractionResult,
                         activity_index: ActivityIndex, context: Optional[AuditContext] = None,
                         violation_states_for=None):
    ctx = context or AuditContext.build(extraction, activity_index)
    links, run = link_resources(case, subject, ctx, activity_index)
    activity_obs = activity_index.observations_for(subject, case.audit_at,
                                                   resources=[(s, r) for (s, r) in links])
    for o in activity_obs:   # one-hop data flow / backup expansion done by the activity index
        if o.resource_id and o.system_id in ("integration_service", "backup_service") \
                and (o.system_id, o.resource_id) not in links \
                and o.extra.get("claim") in ("connector_repopulated", "restore_ran"):
            _add_link(links, o.system_id, o.resource_id, "repopulated", o.category)
    states: dict[tuple[str, str], ResourceState] = {}
    for key in sorted(links, key=lambda k: (k[0], resource_sort_key(k[1]))):
        lk = links[key]
        cat = category_for(lk.system_id, lk.resource_id, lk, ctx, case)
        vs = violation_states_for(cat) if violation_states_for else None
        st = resource_state(lk.system_id, lk.resource_id, cat, case, ctx, activity_obs, vs)
        if st is None:
            st = ResourceState(system_id=lk.system_id, resource_id=lk.resource_id, category=cat,
                               state="UNKNOWN", evidence_id="", captured_at=case.audit_at)
        st.extra["link_reasons"] = sorted(lk.reasons)
        states[key] = st
    return states, links, run
