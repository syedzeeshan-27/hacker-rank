"""The custom evaluation cases (code/evaluation/custom_cases.py) as pytest cases."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from evaluation import custom_cases as cc  # noqa: E402


@pytest.fixture(scope="module")
def world():
    return cc.build_world()


@pytest.mark.parametrize("case", cc.CASES, ids=[c.case.case_id for c in cc.CASES])
def test_custom_case(case, world):
    status, got, is_del = cc.run_case(case, world)
    assert is_del == case.expected_intent
    assert status == case.expected_status
    assert got == case.expected_steps


def test_cli_exit_code_zero():
    assert cc.main() == 0
