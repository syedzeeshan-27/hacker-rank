"""Tests for ghost/identity.py: synthetic fixtures plus a check against the real CSVs."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from ghost.identity import derived_account_id, match_kind, resolve_subject  # noqa: E402
from ghost.models import AuditEvent, Identity, parse_ts  # noqa: E402

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_derived_account_id_only_for_zero_padded_user_ids():
    assert derived_account_id("user_03") == "account_003"
    assert derived_account_id("user_09") == "account_009"
    assert derived_account_id(" user_04 ") == "account_004"
    # no leading zero -> no pattern (these users' accounts carry unrelated numbers)
    assert derived_account_id("user_10") == ""
    assert derived_account_id("user_14") == ""
    assert derived_account_id("user_541") == ""
    assert derived_account_id("user_9") == ""
    assert derived_account_id("account_03") == ""
    assert derived_account_id("") == ""


def ident(uid, email, phone="+1 555 0100", h="AA11", name="Ada  Lovelace"):
    return Identity(uid, name, email, phone, h, parse_ts("2026-01-01T00:00:00Z"))


def ev(aid, uid, t, fld, old, new):
    return AuditEvent(aid, uid, parse_ts(t), fld, old, new)


IDS = [ident("user_1", "ada.new@example.com"), ident("user_2", "bob@example.com", "+44 20 7946 0000", "BB22", "Bob")]
AUD = [
    ev("audit_1", "user_1", "2025-03-01T00:00:00Z", "email", "Ada.Old@Example.com", "ada.mid@example.com"),
    ev("audit_2", "user_1", "2025-06-01T00:00:00Z", "email", "ada.mid@example.com", "ada.new@example.com"),
    ev("audit_3", "user_1", "2025-06-01T00:00:00Z", "phone", "+1 (555) 0199", "+1 555 0100"),
    ev("audit_4", "user_1", "2025-06-01T00:00:00Z", "email_hash", "OLDHASH", "AA11"),
]


def test_current_email_case_insensitive():
    s = resolve_subject("ADA.NEW@example.com ", IDS, AUD, NOW)
    assert s is not None and s.user_id == "user_1"
    assert match_kind("ada.new@EXAMPLE.com", IDS, AUD) == "current"


def test_historical_email_expands_everything():
    s = resolve_subject("ada.old@example.com", IDS, AUD, NOW)
    assert s.user_id == "user_1"
    assert s.emails == {"ada.old@example.com", "ada.mid@example.com", "ada.new@example.com"}
    assert s.phones == {"+15550199", "+15550100"}
    assert s.email_hashes == {"oldhash", "aa11"}
    assert "Ada  Lovelace" in s.names and "ada lovelace" in s.names
    assert s.audit_refs["ada.old@example.com"] == "audit_1"
    assert s.audit_refs["ada.mid@example.com"] == "audit_1"  # earliest event mentioning it
    assert s.audit_refs["oldhash"] == "audit_4"
    assert match_kind("ada.old@example.com", IDS, AUD) == "history"


def test_as_of_does_not_drop_later_identifiers():
    early = parse_ts("2025-01-01T00:00:00Z")
    s = resolve_subject("ada.old@example.com", IDS, AUD, early)
    assert "ada.new@example.com" in s.emails


def test_unknown_email_returns_none():
    assert resolve_subject("nobody@example.com", IDS, AUD, NOW) is None
    assert resolve_subject("", IDS, AUD, NOW) is None


def test_current_match_beats_history_of_other_user():
    ids = IDS + [ident("user_3", "shared@example.com", h="CC")]
    aud = AUD + [ev("audit_9", "user_2", "2026-01-01T00:00:00Z", "email", "shared@example.com", "bob@example.com")]
    assert resolve_subject("shared@example.com", ids, aud, NOW).user_id == "user_3"


def test_most_recent_audit_event_wins_between_histories():
    aud = AUD + [
        ev("audit_10", "user_2", "2024-01-01T00:00:00Z", "email", "recycled@example.com", "x@example.com"),
        ev("audit_11", "user_1", "2024-05-01T00:00:00Z", "email", "y@example.com", "recycled@example.com"),
    ]
    assert resolve_subject("recycled@example.com", IDS, aud, NOW).user_id == "user_1"


def test_real_dataset_every_request_and_sample_email_resolves():
    from ghost.loaders import load_audit_log, load_cases, load_identities, load_sample_cases

    ids, aud = load_identities(), load_audit_log()
    for c in load_sample_cases() + load_cases():
        s = resolve_subject(c.email, ids, aud, c.audit_at)
        assert s is not None, c.case_id
        assert c.email.strip().lower() in s.emails
