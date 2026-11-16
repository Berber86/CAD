"""
Автоматическое Ctrl+S для PDF, открытого в браузере (`browser_save.py`).

Windows-API здесь подменено: проверяется логика — какое окно считать просмотрщиком,
что делать с диалогом «Сохранить как», куда просить файл и как всё это тихо
отключается, если система не Windows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import browser_save
from browser_save import BrowserWindow


def _windows(*titles: str):
    return [BrowserWindow(handle=index + 1, title=title) for index, title in enumerate(titles)]


# ------------------------------------------------------------------ заголовки


@pytest.mark.parametrize(
    "title, expected",
    [
        ("876753.pdf — Google Chrome", True),
        ("ViewContent", True),                       # имя из Content-Disposition
        ("richmondcountyclerk.com — Google Chrome", True),
        ("Клерк округа Ричмонд, штат Нью-Йорк", False),
        ("Таблица Excel", False),
        ("", False),
    ],
)
def test_only_a_pdf_like_window_is_chosen(title, expected):
    assert browser_save.looks_like_pdf_window(title) is expected


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Save As", True),
        ("Сохранить как", True),
        ("Saving 876753.pdf", True),
        ("876753.pdf — Google Chrome", False),
        ("", False),
    ],
)
def test_save_dialog_titles_are_recognised(title, expected):
    assert browser_save.looks_like_save_dialog(title) is expected


def test_pending_file_has_an_understandable_name(tmp_path):
    path = browser_save.pending_path(tmp_path, 2640217)
    assert path == tmp_path / "tsai_step0_2640217.pdf"
    assert browser_save.PENDING_PREFIX in path.name


# ------------------------------------------------------- «нажали Ctrl+S сами»


def test_no_browser_window_means_no_attempt():
    pressed: list[str] = []
    result = browser_save.save_pdf_if_possible(
        Path("nowhere.pdf"),
        windows=lambda: _windows("Таблица Excel", "Проводник"),
        press_ctrl_s_fn=lambda: pressed.append("ctrl+s") or True,
        sleep=lambda seconds: None,
    )
    assert result is None and pressed == []


def test_silent_save_into_downloads_is_found(tmp_path):
    """Браузер сохраняет молча (без диалога) — файл просто появляется на диске."""
    target = tmp_path / "tsai_step0_1.pdf"

    def press() -> bool:
        target.write_bytes(b"%PDF-1.7 deed")
        return True

    saved = browser_save.save_pdf_if_possible(
        target,
        windows=lambda: _windows("Таблица Excel", "876753.pdf — Google Chrome"),
        foreground_fn=lambda handle: True,
        press_ctrl_s_fn=press,
        dialog_open_fn=lambda: False,
        sleep=lambda seconds: None,
    )
    assert saved == target


def test_save_dialog_gets_the_full_path_and_enter(tmp_path):
    target = tmp_path / "tsai_step0_2640217.pdf"
    typed: list[str] = []
    entered: list[int] = []

    def fake_type(text: str) -> bool:
        typed.append(text)
        Path(text).write_bytes(b"%PDF-1.7 deed")
        return True

    saved = browser_save.save_pdf_if_possible(
        target,
        windows=lambda: _windows("ViewContent — Google Chrome"),
        foreground_fn=lambda handle: True,
        press_ctrl_s_fn=lambda: True,
        dialog_open_fn=lambda: True,          # редактор не тот, а диалог есть
        type_text_fn=fake_type,
        press_enter_fn=lambda: entered.append(1) or True,
        sleep=lambda seconds: None,
    )
    assert saved == target
    assert typed == [str(target)]            # вписан полный путь, а не имя
    assert entered


def test_wrong_window_is_brought_to_front_before_the_keys(tmp_path):
    order: list[str] = []
    browser_save.save_pdf_if_possible(
        tmp_path / "x.pdf",
        attempts=1,
        windows=lambda: _windows("Таблица Excel", "876753.pdf — Google Chrome"),
        foreground_fn=lambda handle: order.append(f"focus {handle}") or True,
        press_ctrl_s_fn=lambda: order.append("ctrl+s") or True,
        dialog_open_fn=lambda: False,
        sleep=lambda seconds: None,
    )
    assert order == ["focus 2", "ctrl+s"]    # выбран именно PDF-браузер


def test_three_attempts_then_it_gives_up_quietly(tmp_path):
    presses: list[int] = []
    result = browser_save.save_pdf_if_possible(
        tmp_path / "never.pdf",
        attempts=3,
        windows=lambda: _windows("876753.pdf — Google Chrome"),
        foreground_fn=lambda handle: True,
        press_ctrl_s_fn=lambda: presses.append(1) or True,
        dialog_open_fn=lambda: False,
        sleep=lambda seconds: None,
    )
    assert result is None and len(presses) == 3


def test_a_broken_windows_layer_cannot_break_the_download(tmp_path):
    def boom() -> list[BrowserWindow]:
        raise OSError("нет доступа к окнам")

    assert browser_save.save_pdf_if_possible(
        tmp_path / "x.pdf", windows=boom, sleep=lambda seconds: None,
    ) is None


# ------------------------------------------------------------------ хук лестницы


def test_hook_is_silent_where_windows_api_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_save, "available", lambda: False)
    hook = browser_save.auto_save_hook(tmp_path)
    assert hook(2640217) is None


def test_hook_asks_for_the_pending_name(monkeypatch, tmp_path):
    seen: list[Path] = []

    def fake_save(target, **_kwargs):
        seen.append(Path(target))
        return Path(target)

    monkeypatch.setattr(browser_save, "available", lambda: True)
    monkeypatch.setattr(browser_save, "save_pdf_if_possible", fake_save)
    hook = browser_save.auto_save_hook(tmp_path)
    assert hook(2640217) == tmp_path / "tsai_step0_2640217.pdf"
    assert seen == [tmp_path / "tsai_step0_2640217.pdf"]
