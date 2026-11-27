"""
Шаг 0 (ACRIS): скачивание документа через читалку Acordex VTU — как человек.

По словам и скриншоту пользователя (2026-11-23): «буквально тыкаем на дискету
и сразу на OK». В читалке (``DocumentImageView?doc_id=...``, внутри —
DocumentImageVtu) диалог Save открывается с уже выбранным **All**, поэтому
весь ручной сценарий — два клика:

1. дискета (Save) в тулбаре читалки;
2. OK в диалоге Save — браузер качает весь документ PDF в «Загрузки».

Отсюда сценарии сходятся с Ричмондом в одно русло: свежий PDF забирается из
«Загрузок» и дальше обрабатывается общим проходом Шага 0
(``deed_intake.adopt_downloads`` / ``_collect_one``).

Как находятся клики (снимок окна + локальный OCR, как у NYC Portal):

* готовность читалки — по тексту «DOCUMENT ID» и «of N» на снимке;
* дискета — картинка 15×15, OCR её не видит. Тулбар VTU имеет фиксированную
  вёрстку, поэтому клик считается от текстового якоря той же строки:
  центр дискеты = правый край текста «73%» + 134 px (замерено шаблонным
  поиском по настоящему скриншоту пользователя: «73%» кончается на x=291,
  центр дискеты — x=425, y совпадает со строкой тулбара);
* OK — обычный текст диалога. На первом документе кликается по центру его
  OCR-рамки, а смещение от дискеты запоминается. На следующих документах
  после дискеты выдерживается секунда и OK нажимается по запомненной точке
  без промежуточного снимка; если PDF не пошёл, диалог один раз проверяется
  снимком и точка перекалибровывается.

Каждая ступень, кроме откалиброванного быстрого OK, проверяется снимком
после действия; если что-то не нашлось — ``rcclerk.ManualStepNeeded`` со
ссылкой и подсказкой, а не слепой второй клик.
Сеть и Windows здесь подменяются параметрами, поэтому сценарий проверяется
тестами без браузера и без интернета.

Живой прогон 2026-11-23: читалка по прямой ссылке открылась, но вместо
страницы документа показала «HTTP 405 Method Not Allowed» — у браузера не
было серверной сессии поиска (список мы забираем прямым запросом из кода).
Поэтому: страница проверяется на такую ошибку до клика по дискете; при ней
сессия прогревается поиском по участку в браузере (``warmup_url``) и
документ открывается ещё раз, а во время ожидания PDF кадр поглядывается —
та же ошибка при сборке PDF прекращает ожидание досрочно.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

import acris
import browser_save
import ocr_pages
import rcclerk

#: Центр дискеты правее правого края текста «73%» на столько пикселей
#: (замер по скриншоту пользователя 2026-11-23, см. docstring модуля).
SAVE_DX_FROM_ZOOM_RIGHT = 134
#: Запасной якорь: центр дискеты правее левого края «of» на столько пикселей.
SAVE_DX_FROM_OF_LEFT = 303
#: После первого успешно найденного диалога Save его OK нажимается без
#: промежуточного снимка: дискета → пауза → клик по запомненному смещению.
FAST_OK_DELAY_SEC = 1.0
#: Если после быстрого OK PDF так и не пошёл, один снимок спасает ситуацию:
#: диалог ещё открыт — OK нажимается уже по свежему OCR.
FAST_OK_GRACE_SEC = 8.0
#: Смещение центра OK относительно точки дискеты по размеру окна браузера.
_OK_POINT_CACHE: dict[tuple[str, int, int], tuple[int, int]] = {}

Progress = Callable[[str], None]
StopCheck = Callable[[], bool]


class _ViewerUnavailable(RuntimeError):
    """Снимок/OCR окна недоступны — автоматика дальше небезопасна."""


@dataclass(frozen=True)
class ScreenText:
    """Строка OCR со снимка окна: текст и рамка в координатах снимка."""

    text: str
    box: tuple[int, int, int, int]
    confidence: float = 0.0

    @property
    def center(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self.box
        return (x0 + x1) // 2, (y0 + y1) // 2


def _normalise(text: str) -> str:
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def looks_like_viewer(rows: Sequence[ScreenText]) -> bool:
    """Открылась именно читалка документа: «DOCUMENT ID» или «of N» + масштаб."""
    joined = " ".join(row.text for row in rows)
    if "documentid" in _normalise(joined):
        return True
    return bool(re.search(r"\bof\s+\d+", joined, re.IGNORECASE)) and "%" in joined


def _zoom_anchor(rows: Sequence[ScreenText]) -> Optional[ScreenText]:
    """Текст масштаба тулбара («73%») — ближайший текстовый сосед дискеты."""
    candidates = [row for row in rows if re.fullmatch(r"\d{1,3}\s*%", row.text.strip())]
    return min(candidates, key=lambda row: row.box[1]) if candidates else None


def _of_anchor(rows: Sequence[ScreenText]) -> Optional[ScreenText]:
    """Текст «of 13» тулбара — запасной якорь, если масштаб не прочитался."""
    for row in rows:
        if re.search(r"\bof\s+\d+", row.text, re.IGNORECASE):
            return row
    return None


def save_button_point(rows: Sequence[ScreenText]) -> Optional[tuple[int, int]]:
    """Точка клика по дискете (Save) в координатах снимка окна."""
    zoom = _zoom_anchor(rows)
    if zoom is not None:
        return zoom.box[2] + SAVE_DX_FROM_ZOOM_RIGHT, zoom.center[1]
    anchor = _of_anchor(rows)
    if anchor is not None:
        return anchor.box[0] + SAVE_DX_FROM_OF_LEFT, anchor.center[1]
    return None


def looks_like_save_dialog(rows: Sequence[ScreenText]) -> bool:
    """Диалог Save на экране: есть OK и хотя бы одно из Save/Cancel/All."""
    texts = {_normalise(row.text) for row in rows}
    joined = " ".join(row.text for row in rows)
    has_ok = "ok" in texts
    has_marks = (
        "save" in texts or "cancel" in texts
        or bool(re.search(r"\ball\b", joined, re.IGNORECASE))
    )
    return has_ok and has_marks


def ok_button(rows: Sequence[ScreenText]) -> Optional[ScreenText]:
    """Рамка кнопки OK диалога Save (точный текст, не подстрока)."""
    if not looks_like_save_dialog(rows):
        return None
    return next((row for row in rows if _normalise(row.text) == "ok"), None)


def _ok_cache_key(window: Optional[browser_save.BrowserWindow],
                  bounds: Optional[tuple[int, int, int, int]]) -> Optional[tuple[str, int, int]]:
    if window is None or bounds is None:
        return None
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    if width <= 0 or height <= 0:
        return None
    return (str(getattr(window, "executable", "") or "").lower(), width, height)


def _cached_ok_point(window: Optional[browser_save.BrowserWindow],
                     bounds: Optional[tuple[int, int, int, int]],
                     save_point: tuple[int, int]) -> Optional[tuple[int, int]]:
    """Точка OK, вычисленная из смещения, запомненного на первом диалоге."""
    key = _ok_cache_key(window, bounds)
    if key is None:
        return None
    delta = _OK_POINT_CACHE.get(key)
    if delta is None:
        return None
    point = (save_point[0] + delta[0], save_point[1] + delta[1])
    width, height = key[1], key[2]
    return point if 0 <= point[0] < width and 0 <= point[1] < height else None


def _remember_ok_point(window: Optional[browser_save.BrowserWindow],
                       bounds: Optional[tuple[int, int, int, int]],
                       save_point: tuple[int, int], ok_point: tuple[int, int]) -> None:
    key = _ok_cache_key(window, bounds)
    if key is not None:
        _OK_POINT_CACHE[key] = (ok_point[0] - save_point[0], ok_point[1] - save_point[1])


_HTTP_ERROR_RE = re.compile(
    r"http\s*(?:error\s*)?\d{3}\b|\b\d{3}\s+method\s+not\s+allowed|method\s+not\s+allowed",
    re.IGNORECASE,
)


def http_error_text(rows: Sequence[ScreenText]) -> Optional[str]:
    """
    Текст серверной ошибки вместо страницы документа («HTTP 405 Method Not
    Allowed»): тулбар читалки есть, а VTU страницу не отдал. Ищем и по
    строкам, и по склейке — OCR может разрезать фразу. Голая цифра вроде
    «405» в тексте самого документа ошибкой не считается.
    """
    for row in rows:
        if _HTTP_ERROR_RE.search(row.text or ""):
            return str(row.text).strip()[:80]
    match = _HTTP_ERROR_RE.search(" ".join(row.text for row in rows))
    return match.group(0)[:80] if match else None


# ---------------------------------------------------------------------------
# Настоящий экран (только Windows): снимок окна + локальный OCR
# ---------------------------------------------------------------------------


def _screen_ocr(image_path: str | Path) -> list[ScreenText]:
    """OCR снимка тем же локальным движком, что и NYC Portal."""
    engine = ocr_pages._engine_or_raise()  # noqa: SLF001 - общий движок проекта
    result = ocr_pages.run_engine(engine, str(image_path))
    if isinstance(result, tuple):
        result = result[0]
    rows: list[ScreenText] = []
    for item in result or []:
        try:
            points, text = item[0], str(item[1]).strip()
            confidence = float(item[2]) if len(item) > 2 else 0.0
            xs = [int(float(point[0])) for point in points]
            ys = [int(float(point[1])) for point in points]
            if text and xs and ys:
                rows.append(ScreenText(text, (min(xs), min(ys), max(xs), max(ys)), confidence))
        except (TypeError, ValueError, IndexError):
            continue
    return rows


def _capture_rows(handle: int) -> list[ScreenText]:
    """Снимок только окна браузера + OCR; окно должно быть передним планом."""
    import tempfile

    from PIL import ImageGrab

    bounds = browser_save.window_bounds(handle)
    if bounds is None:
        raise _ViewerUnavailable("не удалось получить размер окна браузера")
    try:
        with tempfile.TemporaryDirectory(prefix="tsai_acris_view_") as directory:
            path = Path(directory) / "screen.png"
            image = ImageGrab.grab(bbox=bounds, all_screens=True)
            if image.width < 400 or image.height < 300:
                raise _ViewerUnavailable("окно браузера свёрнуто или слишком мало")
            image.save(path, format="PNG")
            return _screen_ocr(path)
    except _ViewerUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - OCR/снимок недоступны: дальше человек
        raise _ViewerUnavailable(f"не удалось прочитать экран браузера: {exc}") from exc


def _focus(handle: int) -> bool:
    return bool(browser_save.foreground(handle))


def _click(handle: int, x: int, y: int) -> bool:
    """Клик внутри переднего окна браузера (browser_save страхует фокус)."""
    if not browser_save.foreground_is_acceptable(handle):
        if not browser_save.foreground(handle):
            return False
    return bool(browser_save.click_browser_window(handle, x, y))


def _pick_window(windows_fn, active_fn) -> Optional[browser_save.BrowserWindow]:
    """Окно поддерживаемого браузера с учётом выбранного приоритетного браузера."""
    try:
        windows = list(windows_fn())
    except Exception:  # noqa: BLE001
        return None
    if not windows:
        return None
    try:
        active = int(active_fn())
    except Exception:  # noqa: BLE001
        active = 0
    return browser_save.pick_browser_window(
        windows, active, remember=(windows_fn is browser_save.list_windows),
    )


_MANUAL_HINT = (
    "Откройте ссылку, дождитесь документа, нажмите дискету (Save) вверху "
    "читалки и сразу OK — «All» уже выбрано, PDF упадёт в «Загрузки»: {url}"
)


def _default_timeout_shot(handle: int, folder: Path) -> str:
    """Аварийный снимок окна читалки при тайм-ауте PDF — «чёрный ящик».

    Как ``CLERK_FAIL_*`` у Клерка: без снимка не видно, что было на экране
    вместо скачивания. Любой сбой снимка — пустой хвост.
    """
    try:
        from nyc_portal import _save_failure_shot

        return _save_failure_shot(handle, folder, prefix="ACRIS_FAIL")
    except Exception:  # noqa: BLE001 - диагностика не ломает остановку
        return ""


def download_pdf_via_viewer(
    url: str,
    destination: str | Path,
    *,
    already_open: bool = False,
    downloads_dir: str | Path | None = None,
    warmup_url: str | None = None,
    open_browser: Optional[Callable[[str], object]] = None,
    windows_fn: Callable[[], list] = browser_save.list_windows,
    active_fn: Callable[[], int] = browser_save.active_window_handle,
    bounds_fn: Callable[[int], Optional[tuple[int, int, int, int]]] = browser_save.window_bounds,
    capture_rows_fn: Optional[Callable[[int], list[ScreenText]]] = None,
    click_fn: Optional[Callable[[int, int, int], bool]] = None,
    focus_fn: Optional[Callable[[int], bool]] = None,
    find_pdf_fn: Callable[..., Optional[Path]] = rcclerk.find_new_pdf,
    viewer_wait_sec: float = 90.0,
    dialog_wait_sec: float = 20.0,
    download_wait_sec: float = 45.0,
    fast_ok_delay_sec: float = FAST_OK_DELAY_SEC,
    fast_ok_grace_sec: float = FAST_OK_GRACE_SEC,
    poll_sec: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
    shot_fn: Optional[Callable[[int, Path], str]] = None,
) -> tuple[Path, str]:
    """
    Скачивает PDF одного документа читалкой: дискета → OK → «Загрузки».

    Появление PDF ждём ``download_wait_sec`` (по умолчанию 45 с; «долгая
    капча» на странице «Данные» ставит 300). При тайм-ауте в папку назначения
    падает аварийный снимок окна читалки ``ACRIS_FAIL_*.png`` для диагностики.

    ``already_open=True``: читалка уже открыта в браузере (например, кликом
    IMG из результатов поиска — резерв для свежих документов, которых ещё
    нет в NYC Open Data); URL тогда не открываем, а дожидаемся живую
    читалку и дальше работаем как обычно.

    Возвращает ``(путь, "browser")`` — как ступень лестницы Клерка.
    Не вышло автоматически — ``rcclerk.ManualStepNeeded`` (ссылка + папка
    загрузок внутри): человек дожимает два клика руками, а свежий PDF потом
    забирает ``deed_intake.adopt_downloads``. Остановка — ``DownloadStopped``.
    """
    capture_rows = capture_rows_fn or _capture_rows
    click = click_fn or _click
    focus = focus_fn or _focus
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    folder = Path(downloads_dir) if downloads_dir is not None else rcclerk.default_downloads_dir()

    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    def check_stop() -> None:
        if should_stop is not None and should_stop():
            raise rcclerk.DownloadStopped("остановлено пользователем")

    def manual(message: str) -> rcclerk.ManualStepNeeded:
        return rcclerk.ManualStepNeeded(f"{message} " + _MANUAL_HINT.format(url=url), url, folder=folder)

    def manual_http_error(error: str) -> rcclerk.ManualStepNeeded:
        # Прямая ссылка при такой ошибке не поможет и человеку: документ надо
        # открывать из результатов поиска, где у сайта есть сессия.
        return rcclerk.ManualStepNeeded(
            f"сервер читалки не отдал страницу документа ({error}). "
            "Откройте ACRIS, выполните поиск по Block/Lot и откройте документ "
            "значком просмотра из результатов; дождитесь страницы документа, "
            "нажмите дискету (Save) и сразу OK — PDF упадёт в «Загрузки», "
            "его подберёт кнопка «📂 Загрузки» в Шаге 0.",
            url, folder=folder,
        )

    check_stop()
    since = time.time() - 1.0

    def wait_for(predicate, timeout: float):
        deadline = monotonic() + max(0.0, float(timeout))
        while True:
            check_stop()
            result = predicate()
            if result:
                return result
            if monotonic() >= deadline:
                return None
            sleep(max(0.05, float(poll_sec)))

    window: Optional[browser_save.BrowserWindow] = None
    probe_state: dict[str, object] = {"error_seq": 0, "last_error": ""}

    def probe_viewer() -> Optional[list[ScreenText]]:
        """Один снимок окна: строки здоровой читалки; None — рано или битая."""
        nonlocal window
        picked = _pick_window(windows_fn, active_fn)
        if picked is None:
            return None
        window = picked
        if not focus(window.handle):
            return None
        try:
            rows = capture_rows(window.handle)
        except _ViewerUnavailable as exc:
            raise manual(f"автоматика экрана недоступна ({exc}).") from exc
        if not looks_like_viewer(rows):
            return None
        error = http_error_text(rows)
        if error:
            probe_state["error_seq"] = int(probe_state["error_seq"]) + 1
            probe_state["last_error"] = error
            return None
        probe_state["error_seq"] = 0
        return rows

    def wait_healthy() -> Optional[list[ScreenText]]:
        """
        Дождаться живой читалки: два здоровых снимка подряд — открылась;
        два снимка с HTTP-ошибкой подряд — битая.
        """
        probe_state["error_seq"] = 0
        probe_state["last_error"] = ""
        healthy = 0
        announced = False
        deadline = monotonic() + max(0.0, float(viewer_wait_sec))
        while True:
            check_stop()
            rows = probe_viewer()
            if rows is not None:
                healthy += 1
                if healthy >= 2:
                    return rows
            else:
                healthy = 0
                if int(probe_state["error_seq"]) == 1 and not announced:
                    announced = True
                    say("читалка открылась, но страница документа не загрузилась: "
                        f"«{probe_state['last_error']}»")
                if int(probe_state["error_seq"]) >= 2:
                    return None
            if monotonic() >= deadline:
                return None
            sleep(max(0.05, float(poll_sec)))

    def open_and_wait(target_url: str) -> Optional[list[ScreenText]]:
        """Открыть читалку по ссылке и дождаться живой страницы."""
        try:
            (open_browser or browser_save.open_url)(target_url)
        except Exception as exc:  # noqa: BLE001 - браузера может не быть вовсе
            raise manual(f"не удалось открыть браузер ({exc}).") from exc
        return wait_healthy()

    def screen_has_rows() -> bool:
        """На экране браузера хоть что-то есть — поиск в браузере выполнился."""
        nonlocal window
        picked = _pick_window(windows_fn, active_fn)
        if picked is None:
            return False
        window = picked
        if not focus(window.handle):
            return False
        try:
            return len(capture_rows(window.handle)) >= 3
        except _ViewerUnavailable:
            return False

    if already_open:
        say("читалка уже открыта кликом из результатов — жду живую страницу документа…")
        rows = wait_healthy()
    else:
        say("открываю документ в браузере…")
        rows = open_and_wait(url)
    if rows is None and warmup_url and not already_open:
        # У браузера не было серверной сессии поиска — VTU показывает
        # HTTP 405 вместо страницы. Прогреваем сессию поиском по этому же
        # участку в браузере и открываем документ ещё раз.
        say("прогреваю сессию ACRIS поиском по участку в браузере и открываю документ ещё раз…")
        try:
            (open_browser or browser_save.open_url)(warmup_url)
        except Exception:  # noqa: BLE001 - не вышло: ниже честный ручной шаг
            pass
        wait_for(screen_has_rows, 25.0)
        rows = open_and_wait(url)
    if not rows or window is None:
        if probe_state["last_error"]:
            raise manual_http_error(str(probe_state["last_error"]))
        raise manual("читалка ACRIS не открылась за отведённое время.")
    say("читалка открылась — нажимаю дискету (Save)")

    bounds = bounds_fn(window.handle)
    point = save_button_point(rows)
    if bounds is None or point is None:
        raise manual("не нашёл на экране тулбар читалки с дискетой.")
    if not click(window.handle, bounds[0] + point[0], bounds[1] + point[1]):
        raise manual("не удалось нажать дискету в окне браузера.")

    def dialog_ok() -> Optional[ScreenText]:
        try:
            current = capture_rows(window.handle)  # type: ignore[union-attr]
        except _ViewerUnavailable as exc:
            raise manual(f"автоматика экрана недоступна ({exc}).") from exc
        return ok_button(current)

    # Быстрый путь после первого документа: место OK уже вычислено по первому
    # проходу. Не делаем снимок и OCR между дискетой и OK — просто выдерживаем
    # секунду, как предложил пользователь, и нажимаем запомненную точку.
    fast_ok_deadline: Optional[float] = None
    if _cached_ok_point(window, bounds, point) is not None:
        say("диалог Save уже калиброван — через секунду нажму OK без промежуточного снимка")
        check_stop()
        sleep(max(0.0, float(fast_ok_delay_sec)))
        check_stop()
        ok_bounds = bounds_fn(window.handle)
        fast_point = _cached_ok_point(window, ok_bounds, point)
        if ok_bounds is not None and fast_point is not None and click(
                window.handle, ok_bounds[0] + fast_point[0], ok_bounds[1] + fast_point[1]):
            fast_ok_deadline = monotonic() + max(0.0, float(fast_ok_grace_sec))
        else:
            say("быстрое нажатие OK не подтвердилось — проверяю диалог Save по снимку")

    if fast_ok_deadline is None:
        ok_row = wait_for(dialog_ok, dialog_wait_sec)
        if ok_row is None:
            raise manual("диалог Save после дискеты не появился.")
        say("диалог Save на месте — нажимаю OK (All уже выбрано)")
        ok_bounds = bounds_fn(window.handle)
        if ok_bounds is None:
            raise manual("окно браузера изменилось перед нажатием OK.")
        _remember_ok_point(window, ok_bounds, point, ok_row.center)
        ocx, ocy = ok_row.center
        if not click(window.handle, ok_bounds[0] + ocx, ok_bounds[1] + ocy):
            raise manual("не удалось нажать OK в диалоге Save.")
    # Отсечка для «Загрузок» ставится после OK, а не до открытия читалки:
    # PDF предыдущего документа, оставшийся в папке, не должен приниматься
    # за результат этого клика. Секундный запас — на расхождение часов ФС.
    since = time.time() - 1.0
    say(f"жду, пока PDF упадёт в «{folder}»…")

    found: Optional[Path] = None
    download_deadline = monotonic() + max(0.0, float(download_wait_sec))
    next_probe = monotonic() + 15.0
    while found is None:
        check_stop()
        found = find_pdf_fn(folder, since=since)
        if found is not None:
            break
        if monotonic() >= download_deadline:
            break
        now = monotonic()
        if fast_ok_deadline is not None and now >= fast_ok_deadline:
            # Быстрый клик не дал PDF за короткий срок. Это не промежуточная
            # проверка на успешном пути, а спасение: если диалог всё ещё открыт,
            # значит запомненная точка устарела — нажимаем OK по свежему снимку
            # и заодно перекалибровываем её для следующих документов.
            fast_ok_deadline = None
            try:
                current = capture_rows(window.handle)  # type: ignore[union-attr]
            except _ViewerUnavailable:
                current = []
            error = http_error_text(current)
            if error:
                raise manual_http_error(error)
            rescue = ok_button(current)
            if rescue is not None:
                say("PDF после быстрого OK не пошёл — диалог Save ещё открыт, нажимаю OK по снимку")
                rescue_bounds = bounds_fn(window.handle)  # type: ignore[union-attr]
                if rescue_bounds is None:
                    raise manual("окно браузера изменилось перед повторным нажатием OK.")
                _remember_ok_point(window, rescue_bounds, point, rescue.center)
                rcx, rcy = rescue.center
                if not click(window.handle, rescue_bounds[0] + rcx, rescue_bounds[1] + rcy):  # type: ignore[union-attr]
                    raise manual("не удалось нажать OK в диалоге Save.")
            next_probe = monotonic() + 15.0
        elif window is not None and now >= next_probe:
            next_probe = monotonic() + 15.0
            try:
                current = capture_rows(window.handle)
            except _ViewerUnavailable:
                current = []
            error = http_error_text(current)
            if error:
                raise manual_http_error(error)
        # Папка загрузок проверяется чаще, чем экран: это дешёвая файловая
        # проверка, а снимок+OCR здесь не нужны.
        sleep(max(0.05, min(float(poll_sec), 0.25)))
    if found is None:
        # Аварийный снимок окна читалки в папку задания — почему PDF нет,
        # из одного текста не понять (см. CLERK_FAIL у Клерка).
        shot = ""
        if window is not None:
            try:
                shot = (shot_fn or _default_timeout_shot)(window.handle, target.parent)
            except Exception:  # noqa: BLE001 - диагностика не ломает остановку
                shot = ""
        raise manual(f"PDF не появился в «{folder}» за {int(download_wait_sec)} с.{shot}")
    shutil.copyfile(found, target)
    say(f"файл взят из загрузок: {Path(found).name}")
    return target, "browser"


def download_row_pdf(
    row: acris.AcrisRow,
    destination: str | Path,
    **kwargs,
) -> tuple[Path, str]:
    """
    PDF документа по строке результатов: при необходимости сначала получает
    ``document_id`` по CRFN через NYC Open Data, затем — читалка в браузере.
    """
    if not row.document_id:
        row.document_id = acris.fetch_document_id(row.crfn)
    return download_pdf_via_viewer(row.image_url, destination, **kwargs)
