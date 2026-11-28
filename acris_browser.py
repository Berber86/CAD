"""
Шаг 0: ACRIS в настоящем браузере — резерв для свежих документов.

Зачем: ``document_id`` программа получает по CRFN из NYC Open Data, а тот
отстаёт — живой случай 24.11.2026: MORTGAGE от 20.10.2026 (CRFN
2026000329272) через месяц после записи Open Data не знал, и документ не
собирался. Человек в такой ситуации просто кликает IMG на странице
результатов, и читалка открывается сразу (проверено пользователем вручную:
клик IMG → ``DocumentImageView?doc_id=2026111700598001``, без промежуточных
страниц). Этот модуль делает то же самое руками программы: форма поиска по
BBL → список → клик IMG нужной строки → читалка открыта. Дальше PDF качает
существующая автоматика читалки (``acris_viewer`` с ``already_open=True``).

Как и портал NYC: только экран, OCR и самопроверки; каждый сбой — снимок
«чёрного ящика» ``PORTAL_FAIL_*.png`` в папку задания (если её передали),
а не молчаливый провал. Вычислять ``document_id`` из CRFN нельзя: префикс
doc_id — дата подготовки/записи, хвост — порядковый номер дня; на двух
живых парах (2024000265906 → 2024101000085001, 2026000329272 →
2026111700598001) арифметики нет.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

import acris
import acris_viewer
import browser_save

#: Форма поиска по BBL — та же, что открывает человек.
FORM_URL = f"{acris.BASE_URL}/DS/DocumentSearch/BBL"

VK_HOME = 0x24
VK_MENU = 0x12  # Alt
VK_LEFT = 0x25

#: Заголовки пустой новой вкладки (как у портала: печатаем только в пустую).
NEW_TAB_TITLE_MARKS = browser_save.NEW_TAB_TITLE_MARKS

Progress = Callable[[str], None]
StopCheck = Callable[[], bool]
Rows = Sequence[acris_viewer.ScreenText]


class AcrisBrowserError(RuntimeError):
    """Браузерный резерв ACRIS не смог открыть документ."""


def _norm(text: object) -> str:
    return "".join(char for char in str(text or "").lower() if char.isalnum())


def _digits(text: object) -> str:
    return "".join(char for char in str(text or "") if char.isdigit())


def _find(rows: Rows, needle: str) -> Optional[acris_viewer.ScreenText]:
    target = _norm(needle)
    return next((row for row in rows if target in _norm(row.text)), None)


def _find_exact(rows: Rows, text: str) -> Optional[acris_viewer.ScreenText]:
    target = _norm(text)
    return next((row for row in rows if _norm(row.text) == target), None)


def _row_with_crfn(rows: Rows, crfn: str) -> Optional[acris_viewer.ScreenText]:
    target = _digits(crfn)
    if not target:
        return None
    return next((row for row in rows if _digits(row.text) == target), None)


def open_document_image(
    crfn: str,
    borough_code: int,
    block: int,
    lot: int,
    *,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
    fail_dir: str | Path | None = None,
    capture_rows_fn: Optional[Callable[[int], list]] = None,
    click_fn: Optional[Callable[[int, int, int], bool]] = None,
    focus_fn: Optional[Callable[[int], bool]] = None,
    key_fn: Optional[Callable[[int], bool]] = None,
    ctrl_a_fn: Optional[Callable[[], bool]] = None,
    type_fn: Optional[Callable[[str], bool]] = None,
    open_form_fn: Optional[Callable[[int], None]] = None,
    back_fn: Optional[Callable[[], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    form_wait_sec: float = 40.0,
    results_wait_sec: float = 45.0,
    viewer_wait_sec: float = 45.0,
    poll_sec: float = 1.0,
) -> None:
    """
    Открывает читалку документа кликом IMG в настоящем браузере.

    Документ находится по ``crfn`` в результатах поиска по BBL. Успех —
    открытая читалка (``acris_viewer.looks_like_viewer``); PDF дальше
    скачивает ``acris_viewer.download_pdf_via_viewer(already_open=True)``.
    Любой сбой — ``AcrisBrowserError`` (со снимком экрана, если есть папка).
    """
    capture_rows = capture_rows_fn or acris_viewer._capture_rows  # noqa: SLF001
    click = click_fn or acris_viewer._click  # noqa: SLF001
    focus = focus_fn or acris_viewer._focus  # noqa: SLF001
    press_key = key_fn or browser_save.press_key
    press_ctrl_a = ctrl_a_fn or browser_save.press_ctrl_a
    type_text = type_fn or browser_save.type_text

    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    def check_stop() -> None:
        if should_stop is not None and should_stop():
            raise AcrisBrowserError("остановлено пользователем")

    window = acris_viewer._pick_window(  # noqa: SLF001
        browser_save.list_windows, browser_save.active_window_handle)
    handle = window.handle if window is not None else 0

    def fail(message: str) -> AcrisBrowserError:
        shot = ""
        if fail_dir is not None and handle:
            try:
                from nyc_portal import _save_failure_shot

                shot = _save_failure_shot(handle, fail_dir)
            except Exception:  # noqa: BLE001 - диагностика не ломает остановку
                shot = ""
        return AcrisBrowserError(f"{message}{shot}")

    if window is None:
        raise fail("Не найдено видимое окно поддерживаемого браузера для ACRIS.")
    if not browser_save.available():
        raise fail("Автоматизация окна доступна только в Windows.")

    def rows_now() -> list:
        check_stop()
        if not focus(handle):
            return []
        try:
            return capture_rows(handle)
        except Exception:  # noqa: BLE001 - снимок мог не удаться на полпути
            return []

    def wait_rows(predicate, timeout: float):
        deadline = monotonic() + max(0.0, float(timeout))
        while True:
            rows = rows_now()
            if predicate(rows):
                return rows
            if monotonic() >= deadline:
                return None
            sleep(max(0.05, float(poll_sec)))

    def click_row(row, dx: int = 0, dy: int = 0) -> None:
        bounds = browser_save.window_bounds(handle)
        if bounds is None:
            raise fail("Окно браузера изменилось, координаты клика недоступны.")
        cx, cy = row.center
        if not click(handle, bounds[0] + cx + dx, bounds[1] + cy + dy):
            raise fail("Не удалось нажать элемент в окне ACRIS.")

    say("открываю форму поиска ACRIS в новой вкладке…")
    if open_form_fn is not None:
        open_form_fn(handle)
    else:
        # Сначала фокус: Ctrl+T уходит в переднее окно ОС. Портал окно сначала
        # активирует, а здесь фокуса не было — в ночном прогоне 24–25.11.2026
        # (3876, LOT 62) сочетание ушло в окно программы, вкладка не открылась,
        # а проверка четыре секунды читала заголовок чужого окна и честно
        # остановилась. Без подтверждённого фокуса клавиши никуда не шлём.
        if not focus(handle):
            raise fail("Не удалось активировать окно браузера; форму ACRIS не открываю.")
        browser_id = browser_save.browser_key(window.executable, window.image_path)
        before_tab_image = browser_save.grab_window_image(handle) if browser_id == "yandex" else None
        if not browser_save.press_ctrl_t():
            raise fail("Не удалось открыть новую вкладку в браузере.")
        # Живой сбой 24.11.2026 (3876): одна проверка через 0.6 с дала «не новая
        # вкладка», хотя портал этим же способом прошёл в том же прогоне.
        # Несколько спокойных проверок дешевле, чем отказ от всего резерва.
        # Яндекс Браузер открывает «Табло» поверх текущей вкладки, оставляя старый
        # Win32-заголовок, поэтому для него перепроверяем экран/OCR Табло.
        deadline = monotonic() + 4.0
        while True:
            active_title = browser_save.active_window_title() or ""
            if browser_save.title_looks_like_new_tab(
                active_title, browser=browser_id, extra_marks=NEW_TAB_TITLE_MARKS,
            ):
                break
            if browser_id == "yandex":
                active_handle = browser_save.active_window_handle()
                same_window = (
                    (not active_handle or int(active_handle) == int(handle))
                    and (os.name != "nt" or int(active_handle or 0) == int(handle))
                )
                if same_window:
                    after_tab_image = browser_save.grab_window_image(handle)
                    tab_rows = (
                        rows_now()
                        if (capture_rows_fn is not None or after_tab_image is not None)
                        else []
                    )
                    if browser_save.looks_like_yandex_new_tab_screen(
                        after_tab_image,
                        before_image=before_tab_image,
                        ocr_rows=tab_rows,
                    ):
                        break
            if monotonic() >= deadline:
                raise fail("После Ctrl+T активна не новая вкладка; в чужую страницу ничего не печатаю.")
            focus(handle)  # фокус могли увести — возвращаем окно браузера
            sleep(0.4)
        if not browser_save.press_ctrl_l() or not press_ctrl_a() or not type_text(FORM_URL):
            raise fail("Не удалось ввести адрес формы ACRIS в новой вкладке.")
        # Delete — против автодополнения из истории (как в портале): Enter
        # обязан открывать напечатанную форму, а не последнюю страницу ACRIS.
        press_key(browser_save.VK_DELETE)
        if not press_key(browser_save.VK_RETURN):
            raise fail("Не удалось открыть форму ACRIS.")
    rows = wait_rows(
        lambda r: _find(r, "Enter Block") is not None or _find(r, "Select Borough") is not None,
        form_wait_sec)
    if rows is None:
        raise fail("Форма поиска ACRIS не загрузилась в браузере.")

    # Район: клик по выпадающему списку, HOME — в начало, дальше Down по коду.
    label = _find(rows, "Select Borough")
    if label is not None:
        click_row(label, dx=60, dy=max(16, label.box[3] - label.box[1]))
        sleep(0.4)
        press_key(VK_HOME)
        for _ in range(max(0, int(borough_code) - 1)):
            press_key(browser_save.VK_DOWN)
            sleep(0.1)
        press_key(browser_save.VK_RETURN)
        sleep(0.4)
        rows = rows_now()

    def fill(label_text: str, value: int, description: str, current) -> None:
        anchor = _find(current, label_text)
        if anchor is None:
            raise fail(f"На форме ACRIS не нашёл подпись «{label_text}» ({description}).")
        # Поле ввода — справа от подписи на той же строке.
        click_row(anchor, dx=(anchor.box[2] - anchor.box[0]) // 2 + 70, dy=0)
        if not press_ctrl_a() or not type_text(str(value)):
            raise fail(f"Не удалось ввести {description} в форму ACRIS.")

    # Подписи и кнопка не переезжают от ввода цифр: один снимок формы
    # обслуживает Block, Lot и Search. Раньше каждое поле и кнопка требовали
    # отдельный снимок+OCR, хотя координаты уже были известны.
    fill("Enter Block", int(block), "Block", rows)
    fill("Enter Lot", int(lot), "Lot", rows)
    search = _find_exact(rows, "Search")
    if search is None:
        raise fail("На форме ACRIS не нашёл кнопку Search.")
    click_row(search)
    say(f"ищу в ACRIS блок {block}, лот {lot} и документ CRFN {crfn}…")
    rows = wait_rows(lambda r: _row_with_crfn(r, crfn) is not None, results_wait_sec)
    if rows is None:
        if _find(rows_now(), "No Records") is not None:
            raise fail(f"ACRIS в браузере не нашёл документов по блоку {block}, лоту {lot}.")
        raise fail(f"Строка документа CRFN {crfn} не появилась в результатах ACRIS.")
    target_row = _row_with_crfn(rows, crfn)
    assert target_row is not None  # для проверяльщика типов; wait_rows это гарантировал

    def go_back() -> bool:
        """Шаг назад в браузере (после промаха в DET вместо IMG)."""
        if back_fn is not None:
            return bool(back_fn())
        try:
            browser_save._key(VK_MENU)  # noqa: SLF001 - Alt вниз
            pressed = browser_save.press_key(VK_LEFT)
            browser_save._key(VK_MENU, up=True)  # noqa: SLF001
            return bool(pressed)
        except Exception:  # noqa: BLE001
            return False

    # IMG — в первой колонке строки, левее CRFN; DET ещё левее. Проверяем
    # результатом (открылась читалка); при промахе (открылись детали DET) —
    # назад к результатам и клик правее.
    for attempt, dx in enumerate((-30, -8)):
        bounds = browser_save.window_bounds(handle)
        if bounds is None:
            raise fail("Окно браузера изменилось перед кликом IMG.")
        _cx, cy = target_row.center
        if not click(handle, bounds[0] + target_row.box[0] + dx, bounds[1] + cy):
            raise fail("Не удалось нажать IMG в результатах ACRIS.")
        opened = wait_rows(acris_viewer.looks_like_viewer, viewer_wait_sec if attempt == 0 else 20.0)
        if opened is not None:
            say("читалка документа открыта кликом IMG из результатов ACRIS")
            return
        if attempt == 0:
            say("клик не открыл читалку (возможно, попал в DET) — возвращаюсь к результатам и нажимаю правее…")
            if not go_back():
                break
            rows = wait_rows(lambda r: _row_with_crfn(r, crfn) is not None, results_wait_sec)
            if rows is None:
                break
            target_row = _row_with_crfn(rows, crfn) or target_row
    raise fail(
        f"Клик IMG по строке CRFN {crfn} не открыл читалку документа.")
