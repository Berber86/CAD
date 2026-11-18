"""
Режим «Эконом»: локальный разбор; ИИ вызывается только если локальный результат
не прошёл проверку или локально текст не прочитан. Без ключа и при чистом тексте ИИ не трогается.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_parser  # noqa: E402
from description_check import STATUS_OK  # noqa: E402
from llm_parser import parse_legal_description_full  # noqa: E402

_src = (ROOT / "tests" / "test_description_check.py").read_text(encoding="utf-8")
CLEAN = re.search(r'CLEAN = """(.*?)"""', _src, re.S).group(1)
# Непохожие цифры 3 -> 6: локальная правка не делается, контур не замыкается.
BROKEN = CLEAN.replace(
    "North 31 degrees 46 minutes 56 seconds west", "North 61 degrees 46 minutes 56 seconds west"
)


@pytest.fixture
def ai_calls(monkeypatch):
    """Подменяет вызов ИИ: возвращает правильный контур и считает вызовы."""
    calls: list[str] = []
    good_model, _ = llm_parser.parse_with_regex(CLEAN, auto_repair_typos=False)

    def fake_extract(combined, cfg, **kwargs):
        calls.append(combined)
        usage = llm_parser.TokenUsage(
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
            provider="test", provider_label="test", model="test", available=False,
        )
        return good_model, "structured-outputs", usage

    monkeypatch.setattr(llm_parser, "_extract_with_llm", fake_extract)
    return calls


def test_clean_local_text_never_calls_ai(ai_calls):
    out = parse_legal_description_full(text=CLEAN, use_llm=False, escalate_to_llm=True, ask_on_doubt=True)
    assert out.check.status == STATUS_OK
    assert out.source == "regex"
    assert ai_calls == []
    assert not any(w.startswith("Эконом") for w in out.warnings)


def test_failed_local_check_escalates_to_ai(ai_calls):
    out = parse_legal_description_full(text=BROKEN, use_llm=False, escalate_to_llm=True, ask_on_doubt=True)
    assert len(ai_calls) == 1
    assert out.source == "llm"
    assert out.warnings[0].startswith("Эконом: локальный разбор не прошёл проверку")
    assert out.check.status == STATUS_OK


def test_without_economy_local_result_stays_local(ai_calls):
    """Обычный «Локально» не меняется: ИИ не вызывается даже при ошибке."""
    out = parse_legal_description_full(text=BROKEN, use_llm=False, escalate_to_llm=False, ask_on_doubt=True)
    assert ai_calls == []
    assert out.source == "regex"
    assert out.check.status != STATUS_OK


def test_ai_failure_keeps_local_result_with_warning(monkeypatch):
    def failing(*args, **kwargs):
        raise llm_parser.LegalDescriptionError("сервер ИИ недоступен")

    monkeypatch.setattr(llm_parser, "_extract_with_llm", failing)
    out = parse_legal_description_full(text=BROKEN, use_llm=False, escalate_to_llm=True, ask_on_doubt=True)
    assert out.source == "regex"
    assert out.warnings[0].startswith("Эконом: ИИ не отработал")
    assert out.data.segments[0].degrees == 61


def test_unreadable_local_input_goes_straight_to_ai(ai_calls):
    """PDF без текстового слоя локально не читается — сразу ИИ, а не ошибка."""
    scan = ROOT / "tests" / "data" / "lot114_p1.pdf"
    out = parse_legal_description_full(
        pdf_path=scan, use_llm=False, escalate_to_llm=True, ask_on_doubt=True,
    )
    assert len(ai_calls) == 1
    assert out.warnings[0].startswith("Эконом: локально не прочитано")
