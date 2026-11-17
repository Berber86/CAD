"""Режим «Эконом» в наборе PDF: флаг в задании и перепроверка локального результата."""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_set  # noqa: E402
from pdf_set import PdfSetItem, make_job  # noqa: E402

_src = (ROOT / "tests" / "test_description_check.py").read_text(encoding="utf-8")
CLEAN = re.search(r'CLEAN = """(.*?)"""', _src, re.S).group(1)


def _item(**kw) -> PdfSetItem:
    base = dict(path=Path("/tmp/OUR LOT 1.pdf"), page_texts=[CLEAN], first_page=1, last_page=1)
    base.update(kw)
    return PdfSetItem(**base)


def _finished(item: PdfSetItem, *, ok: bool, source: str = "regex") -> PdfSetItem:
    """Имитирует уже сохранённый результат разбора этого файла."""
    item.outcome = SimpleNamespace(check=SimpleNamespace(ok=ok), source=source)
    item.parsed_key = item.parse_key()
    return item


def test_economy_flag_reaches_text_job_without_llm():
    job = make_job(_item(), economy=True)
    assert job.economy is True
    assert job.use_llm is False


def test_economy_flag_is_off_for_llm_items():
    job = make_job(_item(use_llm=True), economy=True)
    assert job.economy is False


def test_economy_reparses_local_result_that_failed_the_check():
    item = _finished(_item(), ok=False)
    assert make_job(item, economy=False).cached_outcome is item.outcome
    assert make_job(item, economy=True).cached_outcome is None


def test_economy_keeps_local_result_that_passed_the_check():
    item = _finished(_item(), ok=True)
    assert make_job(item, economy=True).cached_outcome is item.outcome


def test_economy_does_not_recompute_a_saved_ai_answer():
    item = _finished(_item(), ok=False, source="llm")
    assert make_job(item, economy=True).cached_outcome is item.outcome


def test_pdf_set_module_exposes_economy_flag_default_off():
    assert pdf_set.PdfSetJob.__dataclass_fields__["economy"].default is False
