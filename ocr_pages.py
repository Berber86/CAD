"""
Локальное распознавание сканов (бесплатный OCR) для Шага 1.

Зачем: Шаг 1 умеет разбирать только текст, а у скана (например, документа из
ACRIS) текстового слоя нет вовсе — ``PyMuPDF`` отдаёт по такой странице пустую
строку. Раньше выход был один: отправлять файл в ИИ. Здесь бесплатный путь:
страница рендерится в картинку и распознаётся локальным движком RapidOCR
(ONNX, процессор, без интернета и без внешних программ).

Модуль **необязательный**: если пакет не установлен, ``available()`` возвращает
``False``, и программа работает как раньше. Никаких сетевых запросов здесь нет.

Точность: для наших задач достаточно, чтобы распознались румбы и длины
(``N 22°26'12" E, 100.05 feet``) — фразы и регистр не важны, парсер их всё равно
нормализует. Ошибку в цифре ловит замыкание: невязка в футах вместо сотых —
повод переключить файл на ИИ (это делает окно набора, см. ``main.py``).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

#: Разрешение по умолчанию. На Шаге 0 пользователь может выбрать 50–1200 dpi;
#: OCR cache key включает DPI, поэтому экспериментальные запуски не перепутаются.
DEFAULT_DPI = 400

#: На Шаге 0 доступны низкие значения для проверки скорости/читаемости.
MIN_DPI = 50
MAX_DPI = 1200
#: Для обычных/высоких запросов сохраняем прежний OCR floor: очень низкое native DPI
#: скана можно слегка увеличить до 200 для лучшей детекции. Явный запрос <200 не повышаем.
UPSAMPLE_FLOOR_DPI = 200

#: Пояснение для окна и отчёта: чем распознаём.
ENGINE_LABEL = "RapidOCR (локально, бесплатно)"

Progress = Callable[[str], None]


class OcrError(RuntimeError):
    """Распознавание не удалось: страница не читается или движок упал."""


class OcrUnavailable(OcrError):
    """Локальный OCR не установлен — нужен ИИ или установка пакета."""


_engine: Any = None
_engine_error = ""
_available: bool | None = None


def available() -> bool:
    """Установлен ли локальный OCR (проверка один раз за запуск)."""
    global _available
    if _available is None:
        try:  # noqa: SIM105 - проверяем именно импортом, без загрузки модели
            import rapidocr_onnxruntime  # noqa: F401, PLC0415
        except Exception as exc:  # noqa: BLE001 - нет пакета или нет системных библиотек
            _available = False
            globals()["_engine_error"] = str(exc) or type(exc).__name__
        else:
            _available = True
    return bool(_available)


def unavailable_reason() -> str:
    """Почему OCR недоступен — для подсказки в окне."""
    if available():
        return ""
    return _engine_error or "пакет rapidocr-onnxruntime не установлен"


def engine_label() -> str:
    return ENGINE_LABEL if available() else ""


def cache_dir() -> Path:
    """Где лежат распознанные тексты (вне проекта, чтобы не мусорить в папке работ)."""
    override = os.environ.get("TITLESURVEY_OCR_CACHE", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:
        return Path(base) / "TitleSurveyAI" / "ocr_cache"
    return Path.home() / ".cache" / "titlesurveyai" / "ocr_cache"


def _engine_or_raise() -> Any:
    """Движок создаётся один раз: загрузка моделей занимает секунды."""
    global _engine
    if _engine is not None:
        return _engine
    if not available():
        raise OcrUnavailable(
            "локальный OCR не установлен: " + unavailable_reason()
        )
    try:  # noqa: PLC0415 - тяжёлый импорт только по требованию
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:  # noqa: BLE001
        raise OcrUnavailable(f"локальный OCR не запускается: {exc}") from exc
    try:
        _engine = RapidOCR()
    except Exception as exc:  # noqa: BLE001 - нет модели, не хватило памяти и т. п.
        raise OcrUnavailable(f"локальный OCR не запускается: {exc}") from exc
    return _engine


def _cache_key(path: Path, first: int, last: int, dpi: int, tile: int = 1) -> str:
    """Ключ кэша: файл, его время и размер, страницы, разрешение и нарезка."""
    try:
        stat = path.stat()
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        stamp = "?"
    raw = f"{path.resolve()}|{stamp}|{first}-{last}|{dpi}|{tile}|{ENGINE_LABEL}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _read_cache(key: str) -> str | None:
    file = cache_dir() / f"{key}.txt"
    try:
        return file.read_text(encoding="utf-8") if file.is_file() else None
    except OSError:
        return None


def is_cached(path: str | Path, first: int, last: int, *, dpi: int = DEFAULT_DPI, tile: int = 1) -> bool:
    """Есть ли уже распознанный текст для этих страниц и разрешения (для честного отчёта)."""
    pdf = Path(path)
    return _read_cache(_cache_key(pdf, int(first), int(last), int(dpi), int(tile))) is not None


def _write_cache(key: str, text: str) -> None:
    try:
        folder = cache_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{key}.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass  # кэш — удобство, а не условие работы


def clear_cache() -> int:
    """Удаляет распознанные тексты; возвращает число файлов."""
    folder = cache_dir()
    count = 0
    if folder.is_dir():
        for file in folder.glob("*.txt"):
            try:
                file.unlink()
                count += 1
            except OSError:
                pass
    return count


def native_dpi(page: Any) -> float:
    """
    Какое разрешение реально лежит в странице (dpi встроенной картинки).

    Скан ACRIS — это картинка внутри PDF: рендер выше её собственного разрешения
    ничего не добавляет, только растягивает пиксели. Для векторной страницы
    картинок нет — вернём 0 (рендерить можно с любым dpi).
    """
    try:
        images = page.get_images(full=True)
        width_in = float(page.rect.width) / 72.0
        height_in = float(page.rect.height) / 72.0
    except Exception:  # noqa: BLE001 - страница без картинок нам не мешает
        return 0.0
    best = 0.0
    for image in images or []:
        try:
            width_px, height_px = int(image[2]), int(image[3])
        except (IndexError, TypeError, ValueError):
            continue
        if width_in <= 0 or height_in <= 0:
            continue
        best = max(best, width_px / width_in, height_px / height_in)
    return best


def page_render_dpi(page: Any, requested: int = DEFAULT_DPI) -> int:
    """Разрешает малый DPI, но не уменьшает традиционный рендер по умолчанию ниже 200."""
    wanted = max(MIN_DPI, min(MAX_DPI, int(requested)))
    native = native_dpi(page)
    if native <= 0:
        return wanted
    native = max(MIN_DPI, round(native))
    if wanted < UPSAMPLE_FLOOR_DPI:
        # Пользовательский low-DPI эксперимент соблюдается; при этом не растягиваем
        # embedded scan выше его native resolution без причины.
        return min(wanted, native)
    # For normal/high settings, preserve the former 200-DPI OCR floor. Mild upsampling
    # from e.g. 150 to 200 improves small text detection; larger scans stay native-capped.
    return min(wanted, max(UPSAMPLE_FLOOR_DPI, native))


def _lines_to_text(result: Any) -> str:
    """
    Строки OCR в текст сверху вниз.

    RapidOCR возвращает список ``[рамка, текст, уверенность]``; рамка — четыре
    точки. Сортируем по верхней границе рамки, а внутри строки — по левой, с
    округлением по вертикали: иначе слова одной строки перемешиваются.
    """
    rows: list[tuple[float, float, str]] = []
    for item in result or []:
        try:
            box, text = item[0], str(item[1])
        except (TypeError, IndexError):
            continue
        if not text.strip():
            continue
        try:
            ys = [float(point[1]) for point in box]
            xs = [float(point[0]) for point in box]
        except (TypeError, IndexError, ValueError):
            ys, xs = [0.0], [0.0]
        rows.append((min(ys), min(xs), text.strip()))
    rows.sort(key=lambda row: (round(row[0] / 12.0), row[1]))
    return "\n".join(text for _y, _x, text in rows)


def _page_tiles(page: Any, dpi: int, tile: int, folder: Path) -> list[Path]:
    """Куски страницы в порядке чтения (слева направо, сверху вниз)."""
    import pymupdf  # noqa: PLC0415 - уже зависимость программы

    grid = max(1, int(tile))
    rect = page.rect
    paths: list[Path] = []
    for row in range(grid):
        for col in range(grid):
            clip = pymupdf.Rect(
                rect.x0 + rect.width * col / grid,
                rect.y0 + rect.height * row / grid,
                rect.x0 + rect.width * (col + 1) / grid,
                rect.y0 + rect.height * (row + 1) / grid,
            )
            pixmap = page.get_pixmap(dpi=int(dpi), clip=clip)
            out = folder / f"p{page.number + 1}_{row}{col}.png"
            pixmap.save(str(out))
            paths.append(out)
    return paths


def ocr_pdf_pages(
    path: str | Path,
    first: int,
    last: int,
    *,
    dpi: int = DEFAULT_DPI,
    progress: Progress | None = None,
    use_cache: bool = True,
    tile: int = 1,
) -> str:
    """
    Распознаёт страницы ``first``–``last`` PDF и возвращает их текст.

    Кэш по файлу и страницам: повторный запуск (правка подписей, второй проход)
    не тратит ни секунды. Ошибки движка и рендера — ``OcrError``/``OcrUnavailable``.

    ``tile`` — на сколько кусков резать страницу перед распознаванием. Аналог
    «600 dpi», но честный: мелкий шрифт становится крупнее для узнавателя, а
    время растёт пропорционально числу кусков. Обычно 1; второй проход с 2×2
    включается, только если страница не читается целиком.
    """
    pdf = Path(path)
    first, last = int(first), int(last)
    if first < 1 or last < first:
        raise OcrError(f"неверные страницы для распознавания: {first}–{last}")
    key = _cache_key(pdf, first, last, int(dpi), int(tile))
    if use_cache:
        cached = _read_cache(key)
        if cached is not None:
            if progress:
                progress("страницы уже распознаны раньше — беру из кэша")
            return cached
    engine = _engine_or_raise()
    try:  # noqa: PLC0415 - PyMuPDF уже зависимость программы
        import pymupdf
    except ImportError as exc:  # pragma: no cover - PyMuPDF обязателен
        raise OcrError(f"нет PyMuPDF для рендера страниц: {exc}") from exc
    try:
        doc = pymupdf.open(str(pdf))
    except Exception as exc:  # noqa: BLE001
        raise OcrError(f"не удалось открыть «{pdf.name}»: {exc}") from exc
    parts: list[str] = []
    try:
        total = len(doc)
        if last > total:
            last = total
        for page_no in range(first, last + 1):
            page = doc.load_page(page_no - 1)
            # Выше собственного разрешения картинки рендерить незачем: только пиксели растягиваем.
            page_dpi = page_render_dpi(page, dpi)
            if progress:
                where = "целиком" if int(tile) <= 1 else f"кусками {int(tile)}×{int(tile)}"
                progress(
                    f"распознаю страницу {page_no} из {last} (OCR, локально, {page_dpi} dpi, {where})…"
                )
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    images = _page_tiles(page, page_dpi, tile, Path(tmp))
                except Exception as exc:  # noqa: BLE001
                    raise OcrError(f"страница {page_no} не рендерится: {exc}") from exc
                page_parts: list[str] = []
                for image in images:
                    try:
                        result, _elapsed = engine(str(image))
                    except Exception as exc:  # noqa: BLE001
                        raise OcrError(f"страница {page_no} не распознана: {exc}") from exc
                    text = _lines_to_text(result)
                    if text.strip():
                        page_parts.append(text)
            if page_parts:
                parts.append("\n".join(page_parts))
    finally:
        doc.close()
    if not parts:
        raise OcrError("на этих страницах ничего не распознано")
    text = "\n".join(parts)
    if use_cache:
        _write_cache(key, text)
    return text
