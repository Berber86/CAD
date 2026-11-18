"""Итог чтения легального описания для журнала: уровень «ключ» виден в упрощённом режиме."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_parser  # noqa: E402
import log_mode  # noqa: E402
from description_check import reading_summary  # noqa: E402

_src = (ROOT / "tests" / "test_description_check.py").read_text(encoding="utf-8")
CLEAN = re.search(r'CLEAN = """(.*?)"""', _src, re.S).group(1)


def test_clean_description_reports_all_values_read():
    out = llm_parser.parse_legal_description_full(text=CLEAN, use_llm=False, ask_on_doubt=True)
    line = reading_summary(out.check)
    assert line.startswith("Легал дескрипшен: прочитано 14 из 14")
    assert "сторон 4 из 4" in line and "румбов 4 из 4" in line and "длин 4 из 4" in line
    assert "расстояние до угла 1 из 1" in line and "замыкание 1 из 1" in line
    assert "сходится" in line


def test_missing_bearing_is_counted_and_named():
    text = (
        "THENCE North 90 degrees 00 minutes 00 seconds East 100.00 feet; running\n"
        "THENCE South 00 degrees 00 minutes 00 seconds East 25.00 feet; running\n"
        "THENCE South 90 degrees 00 minutes 00 seconds West 100.00 feet; running\n"
        "east 30.00 feet to land now or formerly of Smith; running\n"
        "THENCE North 00 degrees 00 minutes 00 seconds West 25.00 feet to the point or place of BEGINNING."
    )
    out = llm_parser.parse_legal_description_full(text=text, use_llm=False, ask_on_doubt=True)
    line = reading_summary(out.check)
    assert "румбов" in line
    assert "румб стороны №4" in line
    assert "НЕ СХОДИТСЯ" in line


def test_summary_is_hidden_in_simple_journal_and_shown_in_full():
    """По решению пользователя строка «прочитано N из M» — только в подробном журнале."""
    out = llm_parser.parse_legal_description_full(text=CLEAN, use_llm=False, ask_on_doubt=True)
    line = "12:00:00  " + reading_summary(out.check)
    entries = [(log_mode.LEVEL_INFO, line)]
    assert log_mode.filter_entries(entries, log_mode.SIMPLE) == []
    assert log_mode.filter_entries(entries, log_mode.FULL) == [line]


def test_summary_without_score_says_not_read():
    assert reading_summary(None) == "Легал дескрипшен: не разобран"
