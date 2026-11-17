"""
Проверка разобранного описания и выбор между локальным разбором и ответом ИИ.

Правило: локальный разбор, который сошёлся сам, ИИ не вызывает. Иначе ИИ вызывается,
и при равных статусах его ответ авторитетен; сошедшийся локальный текст побеждает ответ,
который не сходится.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_parser  # noqa: E402
from description_check import (  # noqa: E402
    STATUS_FAILED,
    STATUS_OK,
    STATUS_WARNING,
    check_description,
    ranks_higher,
)
from llm_parser import (  # noqa: E402
    LegalDescription,
    TokenUsage,
    load_config,
    parse_legal_description_full,
    parse_with_regex,
)

CLEAN = """ALL that certain plot of land, situate in the Borough of Richmond, bounded and described as follows:
BEGINNING at a point on the southwesterly side of Doty Avenue (formerly known as Surf Avenue), distant 74.96 feet U.S. Standard (75.00 feet Deed) northwesterly from the corner formed by the intersection of the southwesterly side of Doty Avenue with the northwesterly side of Father Capodanno Boulevard, running
THENCE along the southwesterly side of Doty Avenue, North 31 degrees 46 minutes 56 seconds west 25.00 feet; running
THENCE South 56 degrees 48 minutes 40 seconds west 115.15 feet to land now or formerly of Emily Rodgers; running
THENCE along said last mentioned land south 42 degrees 03 minutes 54 seconds east 25.41 feet U.S. Standard (25.00 feet Deed) to land now or formerly of the City of New York; running
THENCE along the northwesterly line of said last mentioned land, North 56 degrees 57 minutes 43 seconds East 110.68 feet to the point or place of BEGINNING."""


def _clean_model() -> LegalDescription:
    model, _ = parse_with_regex(CLEAN, auto_repair_typos=True)
    return model


# ------------------------------------------------------------------ сама проверка


def test_clean_description_is_ok():
    check = check_description(_clean_model(), CLEAN)
    assert check.status == STATUS_OK
    assert check.closure_ft is not None and check.closure_ft < 0.5
    assert check.warning_lines() == []


def test_small_closure_is_a_warning_not_a_failure():
    text = CLEAN.replace("110.68 feet", "112.68 feet")  # невязка ~2 ft
    check = check_description(parse_with_regex(text, auto_repair_typos=True)[0], text)
    assert check.status == STATUS_WARNING
    assert any("невязка" in issue.text for issue in check.issues)


def test_large_closure_is_a_failure():
    text = CLEAN.replace("110.68 feet", "68.00 feet")  # невязка десятки футов
    check = check_description(parse_with_regex(text, auto_repair_typos=True)[0], text)
    assert check.status == STATUS_FAILED
    assert check.warning_lines()[0].startswith("✗")


def test_missing_distance_to_corner_is_a_failure():
    # В тексте есть «distant 74.96 feet», а модель его не получила
    model = _clean_model().model_copy(update={"distance_to_corner_ft": 0.0})
    check = check_description(model, CLEAN)
    assert check.status == STATUS_FAILED
    assert any("расстояние до угла" in issue.text for issue in check.issues)


def test_side_without_bearing_is_a_failure_and_is_named():
    model = _clean_model()
    segs = list(model.segments)
    segs[1] = segs[1].model_copy(update={"has_bearing": False, "course_note": None})
    check = check_description(model.model_copy(update={"segments": segs}), CLEAN)
    assert check.status == STATUS_FAILED
    assert any(issue.side == 2 for issue in check.issues)


def test_fewer_than_three_sides_is_a_failure():
    model = _clean_model()
    check = check_description(model.model_copy(update={"segments": list(model.segments)[:2]}), "")
    assert check.status == STATUS_FAILED


def test_status_ranking():
    assert ranks_higher(STATUS_OK, STATUS_WARNING)
    assert ranks_higher(STATUS_WARNING, STATUS_FAILED)
    assert not ranks_higher(STATUS_FAILED, STATUS_FAILED)


# ------------------------------------------------------------------ выбор результата


def _usage() -> TokenUsage:
    return TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                      provider="hydra", provider_label="Hydra AI", model="test", available=True)


def _fake_llm(monkeypatch, model: LegalDescription, calls: list) -> None:
    def fake(*_args, **_kwargs):
        calls.append(1)
        return model, "structured-outputs", _usage()

    monkeypatch.setattr(llm_parser, "_extract_with_llm", fake)


def _cfg():
    cfg = load_config()
    cfg["api"]["api_key"] = "sk-test"
    return cfg


def test_checked_local_text_never_calls_llm(monkeypatch):
    calls: list[int] = []
    _fake_llm(monkeypatch, _clean_model(), calls)
    outcome = parse_legal_description_full(text=CLEAN, cfg=_cfg(), use_llm=True)
    assert calls == []
    assert outcome.source == "regex"
    assert outcome.check.ok


def test_llm_answer_that_closes_is_used_when_local_text_fails(monkeypatch):
    bad_local = CLEAN.replace("110.68 feet", "68.00 feet")  # локально не сходится
    calls: list[int] = []
    _fake_llm(monkeypatch, _clean_model(), calls)  # ИИ выдал сходящийся участок
    outcome = parse_legal_description_full(text=bad_local, cfg=_cfg(), use_llm=True)
    assert calls == [1]
    assert outcome.source == "llm"
    assert outcome.check.ok


def test_equal_status_gives_llm_the_last_word(monkeypatch):
    """Оба результата не сходятся: ответ ИИ авторитетен при равных статусах."""
    bad_local = CLEAN.replace("110.68 feet", "68.00 feet")
    model = _clean_model()
    segs = list(model.segments)[:3]  # ИИ потерял сторону: тоже не сходится
    bad_llm = model.model_copy(update={"segments": segs})
    calls: list[int] = []
    _fake_llm(monkeypatch, bad_llm, calls)
    outcome = parse_legal_description_full(text=bad_local, cfg=_cfg(), use_llm=True)
    assert outcome.source == "llm"
    assert outcome.check.status == STATUS_FAILED


def test_checked_local_text_beats_llm_answer_that_does_not_close(monkeypatch):
    local_warning = CLEAN.replace("110.68 feet", "112.68 feet")  # локально «предупреждение»
    model = _clean_model()
    bad_llm = model.model_copy(update={"segments": list(model.segments)[:3]})  # ИИ «не сходится»
    calls: list[int] = []
    _fake_llm(monkeypatch, bad_llm, calls)
    outcome = parse_legal_description_full(text=local_warning, cfg=_cfg(), use_llm=True)
    assert calls == [1]
    assert outcome.source == "regex"
    assert any("взят локальный разбор" in line for line in outcome.warnings)


@pytest.mark.parametrize("status", [STATUS_OK, STATUS_WARNING, STATUS_FAILED])
def test_outcome_always_carries_its_check(monkeypatch, status):
    calls: list[int] = []
    _fake_llm(monkeypatch, _clean_model(), calls)
    text = CLEAN if status == STATUS_OK else CLEAN.replace("110.68 feet", "68.00 feet")
    outcome = parse_legal_description_full(text=text, cfg=_cfg(), use_llm=True)
    assert outcome.check is not None
    assert outcome.check.status in {STATUS_OK, STATUS_WARNING, STATUS_FAILED}
