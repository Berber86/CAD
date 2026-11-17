"""Итог ИИ (в том числе после консенсуса нескольких проходов) несёт проверку описания.

Без проверки ранг итога равен 0, и верный ответ ИИ отбрасывался бы при сравнении с кешем.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_parser  # noqa: E402
from description_check import STATUS_OK  # noqa: E402

_src = (ROOT / "tests" / "test_description_check.py").read_text(encoding="utf-8")
CLEAN = re.search(r'CLEAN = """(.*?)"""', _src, re.S).group(1)


def _outcome_without_check():
    model, _ = llm_parser.parse_with_regex(CLEAN, auto_repair_typos=False)
    return llm_parser.ParseOutcome(
        data=model,
        source="llm",
        warnings=[],
        usage=None,
        provider="test",
        provider_label="test",
        model="test",
        llm_mode="structured-outputs",
        prepared_text=CLEAN,
        prepared_images=[],
        base_warnings=[],
    )


def test_llm_outcome_gets_check_from_its_own_data() -> None:
    raw = _outcome_without_check()
    assert raw.check is None
    checked = llm_parser._with_check(raw, CLEAN)
    assert checked.check is not None
    assert checked.check.status == STATUS_OK
    # Уже проверенный итог не пересчитывается.
    assert llm_parser._with_check(checked, CLEAN) is checked
