"""Посимвольно-осмысленное слияние двух рендеров скана и сужение кеша до строк описания.

Данные: tests/data/lot114_render_e600.txt и lot114_render_native.txt — два распознавания одной
страницы (OUR LOT 114). Ожидаемые значения сверены с эталоном (сторона 110.68, угол 31°46'56").
"""

from pathlib import Path

import llm_parser
import ocr_pages
from description_check import STATUS_OK, check_description

DATA = Path(__file__).resolve().parent / "data"


def _render(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


def _check(text: str):
    model, _ = llm_parser.parse_with_regex(text, auto_repair_typos=False)
    return check_description(model, text)


def test_numeric_lookalikes_resolve_to_digits():
    assert ocr_pages.vote_line("East llo.68 feet", "East lio.68 feet") == "East 110.68 feet"
    assert ocr_pages.vote_line("North 3l degreca", "North 31 degreca") == "North 31 degreca"


def test_word_choice_prefers_description_word():
    assert ocr_pages.vote_tokens("thc", "the") == "the"
    assert ocr_pages.vote_tokens("Avcnue", "Avenue") == "Avenue"


def test_direction_words_are_never_guessed():
    """Догадок по сходству нет: если ни один вариант не словарный, слово не меняется.

    Иначе «northweaterly» могло бы стать northeasterly (0.92 по сходству) — направление неверное.
    """
    assert ocr_pages.vote_tokens("northweaterly", "northweaterlx") == "northweaterly"
    # Словарное слово побеждает испорченное — это выбор между двумя чтениями, не догадка.
    assert ocr_pages.vote_tokens("northweaterly", "northwesterly") == "northwesterly"


def test_merge_with_vote_fixes_numbers_and_keeps_the_check_ok():
    e600, native = _render("lot114_render_e600.txt"), _render("lot114_render_native.txt")
    for first, second in ((e600, native), (native, e600)):
        merged = ocr_pages.merge_line_texts(first, second)
        assert "East 110.68 feet" in merged
        assert "North 31 degree" in merged or "North 31 degreca" in merged
        check = _check(merged)
        assert check.status == STATUS_OK
        assert check.closure_ft is not None and check.closure_ft <= 0.5


def test_key_lines_keep_the_same_check_and_drop_the_header():
    merged = ocr_pages.merge_line_texts(_render("lot114_render_e600.txt"), _render("lot114_render_native.txt"))
    key = ocr_pages.description_key_lines(merged)
    assert key
    assert len(key) < len(merged) * 0.5
    assert "THISINDENTURE" not in key and "Standard" not in key.split("\n")[0]
    full, short = _check(merged), _check(key)
    assert short.status == full.status == STATUS_OK
    assert short.closure_ft == full.closure_ft


def test_key_lines_empty_when_no_description_block():
    """Нет блока описания — пустая строка: вызывающий сохранит полный текст, а не пустой кеш."""
    assert ocr_pages.description_key_lines("PAGE 2\nREEL 3\n28942") == ""
