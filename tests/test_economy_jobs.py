"""Режим «Эконом» у файла набора PDF: флаг у файла, ключ разбора и выбор в окне набора."""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_set import PdfSetItem, make_job  # noqa: E402

_src = (ROOT / "tests" / "test_description_check.py").read_text(encoding="utf-8")
CLEAN = re.search(r'CLEAN = """(.*?)"""', _src, re.S).group(1)


def _item(**kw) -> PdfSetItem:
    base = dict(path=Path("/tmp/OUR LOT 1.pdf"), page_texts=[CLEAN], first_page=1, last_page=1)
    base.update(kw)
    return PdfSetItem(**base)


def _finished(item: PdfSetItem, *, ok: bool) -> PdfSetItem:
    """Имитирует уже сохранённый результат разбора этого файла."""
    item.outcome = SimpleNamespace(check=SimpleNamespace(ok=ok), source="regex")
    item.parsed_key = item.parse_key()
    return item


def test_economy_is_a_per_file_mode_for_local_items():
    item = _item()
    item.set_mode(False, economy=True)
    job = make_job(item)
    assert job.economy is True
    assert job.use_llm is False
    assert item.mode_label() == "эконом: локально, ИИ при проблемах"


def test_economy_is_ignored_for_llm_items():
    item = _item()
    item.set_mode(True, economy=True)
    assert item.economy is False
    assert make_job(item).economy is False


def test_switching_to_economy_reparses_a_finished_local_result():
    item = _finished(_item(), ok=False)
    assert make_job(item).cached_outcome is item.outcome
    item.set_mode(False, economy=True)
    assert make_job(item).cached_outcome is None


def test_plain_local_result_is_reused_when_mode_unchanged():
    item = _finished(_item(), ok=True)
    assert make_job(item).cached_outcome is item.outcome


def test_per_file_modes_are_independent():
    a, b = _item(path=Path("/tmp/A.pdf")), _item(path=Path("/tmp/B.pdf"))
    a.set_mode(False, economy=True)
    b.set_mode(False, economy=False)
    assert make_job(a).economy is True
    assert make_job(b).economy is False
