"""Не хватает только расстояния от угла до точки начала: без ИИ, с пометкой «не найдено».

Решение пользователя: ИИ не может найти значение, которого нет в тексте документа, поэтому
при сходящемся контуре и полных сторонах второй рендер и платный запрос не выполняются.
"""

import re
from pathlib import Path

import pytest

import llm_parser
from description_check import (
    CORNER_GAP_ISSUE_PREFIX,
    CORNER_GAP_MISSING,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_WARNING,
    CheckIssue,
    DescriptionCheck,
    FigureScore,
    accepted_without_ai,
)
from llm_parser import parse_legal_description_full

_src = (Path(__file__).resolve().parent / "test_description_check.py").read_text(encoding="utf-8")
CLEAN = re.search(r'CLEAN = """(.*?)"""', _src, re.S).group(1)
DISTANT = (
    "distant 74.96 feet U.S. Standard (75.00 feet Deed) northwesterly from the corner formed by the "
    "intersection of the southwesterly side of Doty Avenue with the northwesterly side of Father "
    "Capodanno Boulevard, running"
)
NO_CORNER = CLEAN.replace(DISTANT, "running")


def _corner_issue() -> CheckIssue:
    return CheckIssue(status=STATUS_WARNING, text=CORNER_GAP_ISSUE_PREFIX + " — участок построен от угла")


def _check(**kw) -> DescriptionCheck:
    base = dict(
        status=STATUS_WARNING,
        issues=[_corner_issue()],
        score=FigureScore(missing=[CORNER_GAP_MISSING]),
        closure_ft=0.2,
    )
    base.update(kw)
    return DescriptionCheck(**base)


def test_only_corner_gap_is_accepted_without_ai():
    assert accepted_without_ai(_check()) is True
    assert accepted_without_ai(DescriptionCheck(status=STATUS_OK, score=FigureScore(), closure_ft=0.0)) is True


def test_any_other_gap_still_needs_ai():
    # Не сошёлся контур.
    assert accepted_without_ai(_check(closure_ft=3.0)) is False
    # Не хватает ещё и стороны.
    assert accepted_without_ai(_check(score=FigureScore(missing=[CORNER_GAP_MISSING, "румб стороны №2"]))) is False
    # Есть другое предупреждение или ошибка.
    other = CheckIssue(status=STATUS_WARNING, text="возможная опечатка в румбе стороны №3")
    assert accepted_without_ai(_check(issues=[_corner_issue(), other])) is False
    assert accepted_without_ai(_check(issues=[CheckIssue(status=STATUS_FAILED, text="сбой")])) is False
    assert accepted_without_ai(None) is False


def test_economy_takes_local_result_without_corner_distance(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_parser, "_extract_with_llm", lambda *a, **k: calls.append(1) or pytest.fail("ИИ"))
    out = parse_legal_description_full(text=NO_CORNER, use_llm=False, escalate_to_llm=True, ask_on_doubt=True)
    assert calls == []
    assert out.source == "regex"
    assert out.check.score.missing == [CORNER_GAP_MISSING]
    assert out.check.closure_ft is not None and out.check.closure_ft <= 0.5
