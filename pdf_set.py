"""
Набор PDF Шага 1: наш участок и соседи выбираются одним разом.

Здесь только данные и правила без окна:

* какие страницы файла идут в разбор (из имени ``…_p17`` / ``…_p2_5`` или
  из полей «с … по …», которые правит пользователь);
* чем разбирается файл — бесплатным локальным парсером или ИИ;
* что именно уходит в разбор: только выбранные страницы.

Окно со списком и запуск разбора находятся в ``main.py``.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import ocr_pages
from llm_parser import (
    PDFError,
    has_legal_courses,
    pages_from_pdf_name,
    read_pdf_page_texts,
)

#: Невязка, выше которой контур считается незамкнутым (как в проверке перед отрисовкой).
CLOSURE_LIMIT_FT = 0.08


def format_pages(first: int, last: int) -> str:
    """«стр. 3» или «стр. 2–5» — так диапазон показан пользователю."""
    return f"стр. {first}" if first == last else f"стр. {first}–{last}"


def pages_spec(first: int, last: int) -> str:
    """Тот же диапазон в виде, который понимает ``parse_page_range``."""
    return str(first) if first == last else f"{first}-{last}"


def read_page_boxes(first_raw: Any, last_raw: Any, total_pages: int) -> tuple[int, int]:
    """
    Проверяет поля «с … по …» одного файла.

    Пустое «по» означает одну страницу, пустое «с» — первую. Ошибки
    возвращаются понятным текстом в ``ValueError``.
    """
    first_text = str(first_raw if first_raw is not None else "").strip()
    last_text = str(last_raw if last_raw is not None else "").strip()
    if not first_text and not last_text:
        raise ValueError("не указаны страницы")
    if not first_text:
        first_text = "1"
    if not last_text:
        last_text = first_text
    if not first_text.isdigit() or not last_text.isdigit():
        raise ValueError("страницы задаются целыми числами")
    first, last = int(first_text), int(last_text)
    if first < 1 or last < 1:
        raise ValueError("нумерация страниц начинается с 1")
    if first > last:
        raise ValueError(f"первая страница ({first}) больше последней ({last})")
    if last > total_pages:
        raise ValueError(f"в файле всего {total_pages} стр., а указано до {last}")
    return first, last


@dataclass
class PdfSetItem:
    """Один PDF набора: роль, страницы, способ разбора и результат последнего разбора."""

    path: Path
    page_texts: list[str]
    first_page: int = 1
    last_page: int = 1
    is_main: bool = False
    use_llm: bool = False
    #: Локальный OCR доступен и разрешён: скан разбирается бесплатно, без ИИ.
    ocr_ok: bool = False
    #: Этот файл уже распознавался локальным OCR (для отчёта).
    ocr_used: bool = False
    #: Способ ещё не выбирали вручную — он следует за страницами.
    auto_mode: bool = True
    #: Страницы, прочитанные из имени файла (None — в имени их нет).
    name_pages: tuple[int, int] | None = None
    #: Пояснение, откуда взяты страницы по умолчанию.
    pages_note: str = ""
    # --- результат последнего разбора ---
    outcome: Any = None
    parsed_key: tuple[Any, ...] | None = None
    issues: list[str] = field(default_factory=list)
    error: str = ""
    on_scene: bool = False
    #: Правки по данным соседей, принятые пользователем (``conflict_resolver.Proposal``).
    #: Относятся к текущему разбору файла: новый разбор их сбрасывает.
    fixes: list[Any] = field(default_factory=list)

    @property
    def total_pages(self) -> int:
        return len(self.page_texts)

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1

    def selected_text(self, first: int | None = None, last: int | None = None) -> str:
        """Текстовый слой только выбранных страниц."""
        first = self.first_page if first is None else first
        last = self.last_page if last is None else last
        return "\n".join(self.page_texts[first - 1:last])

    def has_legal_text(self, first: int | None = None, last: int | None = None) -> bool:
        """Есть ли на выбранных страницах текст обхода для бесплатного разбора."""
        return has_legal_courses(self.selected_text(first, last))

    def pages_spec(self) -> str:
        return pages_spec(self.first_page, self.last_page)

    def pages_label(self) -> str:
        return f"{format_pages(self.first_page, self.last_page)} из {self.total_pages}"

    def mode_label(self) -> str:
        if not self.use_llm:
            if self.has_legal_text():
                return "локально, бесплатно"
            if self.ocr_used:
                return "локальный OCR, распознано (бесплатно)"
            if self.ocr_ok:
                return "локальный OCR, бесплатно"
            return "локально, бесплатно (текста нет)"
        return "ИИ по тексту страниц" if self.has_legal_text() else "ИИ / Vision по изображению страниц"

    def uses_ocr(self) -> bool:
        """Файл пойдёт в локальный OCR: локальный разбор, текста обхода нет, OCR есть."""
        return bool(self.ocr_ok and not self.use_llm and not self.has_legal_text())

    def parse_key(self) -> tuple[Any, ...]:
        """От чего зависит результат разбора; роль на него не влияет."""
        return (str(self.path), self.first_page, self.last_page, bool(self.use_llm))

    def signature(self) -> tuple[Any, ...]:
        return self.parse_key() + (bool(self.is_main),)

    def has_fresh_result(self) -> bool:
        return self.outcome is not None and self.parsed_key == self.parse_key()

    def set_pages(self, first: int, last: int) -> None:
        """Меняет страницы; пока способ не выбран вручную, он подстраивается."""
        self.first_page, self.last_page = first, last
        if self.auto_mode:
            # Скан при доступном локальном OCR остаётся бесплатным; иначе — как раньше, ИИ.
            self.use_llm = not (self.has_legal_text() or self.ocr_ok)

    def set_mode(self, use_llm: bool) -> None:
        """Способ выбран пользователем и больше не меняется сам."""
        self.use_llm = bool(use_llm)
        self.auto_mode = False

    def forget_result(self) -> None:
        self.outcome = None
        self.parsed_key = None
        self.issues = []
        self.error = ""
        self.on_scene = False
        self.fixes = []

    def parse_source(self) -> dict[str, Any]:
        """
        Аргументы источника для разбора: в парсер и в ИИ идут только выбранные страницы.

        * локально — текстовый слой этих страниц;
        * ИИ, на страницах есть текст обхода — тот же текст (дешевле изображений);
        * ИИ, текста обхода нет (скан, чертёж) — изображения этих страниц (Vision).
        """
        text = self.selected_text()
        if not self.use_llm:
            if len(text.strip()) < 20:
                if self.ocr_ok:
                    # Скан: текст страниц появится в фоне из локального OCR (бесплатно).
                    return {
                        "ocr_path": self.path,
                        "ocr_pages": self.pages_spec(),
                        "ocr_dpi": int(ocr_pages.DEFAULT_DPI),
                    }
                raise PDFError(
                    f"на {self.pages_label()} нет текстового слоя (скан или чертёж). "
                    "Переключите файл на ИИ или укажите другие страницы."
                )
            return {"text": text}
        if has_legal_courses(text):
            return {"text": text}
        return {"pdf_path": self.path, "pdf_pages": self.pages_spec()}


@dataclass(frozen=True)
class PdfSetJob:
    """Неизменяемый снимок файла набора для фонового разбора."""

    path: Path
    is_main: bool
    use_llm: bool
    pages_label: str
    mode_label: str
    parse_key: tuple[Any, ...]
    source: dict[str, Any] | None = None
    source_error: str = ""
    cached_outcome: Any = None
    cached_issues: tuple[str, ...] = ()
    #: Принятые правки; действуют только вместе с готовым результатом разбора.
    fixes: tuple[Any, ...] = ()
    #: Файл — скан: текст появится из локального OCR (бесплатно), уже в фоне.
    ocr: bool = False

    @property
    def role_label(self) -> str:
        return "наш участок" if self.is_main else "сосед"


@dataclass
class PdfSetResult:
    """Итог разбора одного файла набора."""

    path: Path
    is_main: bool
    parse_key: tuple[Any, ...]
    outcome: Any = None
    plan: Any = None
    pre_counted: bool = False
    cached: bool = False
    #: Текст страниц получен локальным OCR (бесплатно), ИИ не вызывался.
    ocr: bool = False
    issues: list[str] = field(default_factory=list)
    error: str = ""


def make_job(item: PdfSetItem, *, force: bool = False) -> PdfSetJob:
    """Готовит файл к разбору; готовый результат при тех же страницах не запрашивается заново."""
    reuse = item.has_fresh_result() and not force
    source: dict[str, Any] | None = None
    source_error = ""
    if not reuse:
        try:
            source = item.parse_source()
        except Exception as exc:  # noqa: BLE001 - ошибка относится только к этому файлу
            source_error = str(exc)
    return PdfSetJob(
        path=item.path,
        is_main=item.is_main,
        use_llm=item.use_llm,
        pages_label=item.pages_label(),
        mode_label=item.mode_label(),
        parse_key=item.parse_key(),
        source=source,
        source_error=source_error,
        cached_outcome=item.outcome if reuse else None,
        cached_issues=tuple(item.issues) if reuse else (),
        fixes=tuple(item.fixes) if reuse else (),
        ocr=bool(source is not None and "ocr_path" in source),
    )


def pages_from_spec(spec: Any) -> tuple[int, int]:
    """Страницы из спецификации ``"3"`` / ``"3-5"`` (как в ``_p3-5`` в имени файла)."""
    text = str(spec if spec is not None else "").strip()
    first_raw, _, last_raw = text.partition("-")
    try:
        first = int(first_raw)
        last = int(last_raw) if last_raw.strip() else first
    except ValueError as exc:
        raise PDFError(f"непонятные страницы: «{text}»") from exc
    if first < 1 or last < first:
        raise PDFError(f"непонятные страницы: «{text}»")
    return first, last


def resolve_ocr_source(
    source: dict[str, Any], *, progress: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """
    Достаёт текст скана локальным OCR — и только в фоне, не в окне.

    ``parse_source`` для скана возвращает не текст, а отметку «это надо
    распознать»: рендер страниц и OCR занимают секунды на страницу, и делать это
    в потоке окна нельзя. Фоновый разбор вызывает эту функцию, получает текст и
    дальше идёт обычным путём бесплатного разбора. Если распознать не удалось,
    ``PDFError`` говорит прямо, что делать: переключить файл на ИИ.
    """
    if "ocr_path" not in source:
        return source
    path = Path(str(source["ocr_path"]))
    first, last = pages_from_spec(source.get("ocr_pages"))
    dpi = int(source.get("ocr_dpi") or ocr_pages.DEFAULT_DPI)
    try:
        text = ocr_pages.ocr_pdf_pages(
            path, first, last, dpi=dpi, progress=progress
        )
        if not has_legal_courses(text):
            # Второй заход «мельче»: страница режется на 2×2 и распознаётся по кускам.
            # Это дешевле платного Vision и спасает плотный мелкий шрифт.
            if progress:
                progress("целиком не разобрать — пробую по кускам 2×2")
            tiled = ocr_pages.ocr_pdf_pages(
                path, first, last, dpi=dpi, progress=progress, tile=2
            )
            if has_legal_courses(tiled):
                text = tiled
    except ocr_pages.OcrUnavailable as exc:
        raise PDFError(
            f"локальный OCR недоступен ({exc}). "
            "Переключите файл на ИИ или установите rapidocr-onnxruntime."
        ) from exc
    except ocr_pages.OcrError as exc:
        raise PDFError(
            f"локальный OCR не прочитал «{path.name}»: {exc}. Переключите файл на ИИ."
        ) from exc
    if not has_legal_courses(text):
        raise PDFError(
            f"локальный OCR не нашёл обхода границ на {format_pages(first, last)} "
            f"файла «{path.name}» (распознано {len(text)} симв.). "
            "Переключите файл на ИИ или укажите другие страницы."
        )
    return {"text": text, "ocr": True}


#: Ступень 2 фонового разбора скана: локальный OCR на повышенном разрешении.
ESCALATION_DPI = int(ocr_pages.DEFAULT_DPI)


def describe_exc(exc: BaseException) -> str:
    """«IndexError: string index out of range (llm_parser.py:123)» — тип, текст и место."""
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ is not None else []
    if frames:
        last = frames[-1]
        text += f" ({Path(last.filename).name}:{last.lineno})"
    return text


@dataclass(frozen=True)
class OcrEscalation:
    """Итог ступени 2: текст (если получен), ошибка (если нет) и что реально выполнено."""

    text: str = ""
    error: str = ""
    seconds: float = 0.0
    cached: bool = False
    tiled: bool = False
    pages: tuple[int, int] = (1, 1)
    dpi: int = ESCALATION_DPI

    def summary(self) -> str:
        if self.error:
            return f"ошибка за {self.seconds:.1f} с: {self.error}"
        how = "из кеша OCR" if self.cached else "распознано заново"
        if self.tiled:
            how += ", кусками 2×2"
        return f"{self.seconds:.1f} с, {how}, {format_pages(*self.pages)}, {self.dpi} dpi"


def escalate_ocr(
    path: str | Path, source: dict[str, Any] | None, *,
    dpi: int = ESCALATION_DPI, progress: Callable[[str], None] | None = None,
) -> OcrEscalation:
    """
    Ступень 2 для скана в фоновом разборе: реальное локальное распознавание.

    * страницы — выбранные пользователем (``ocr_pages`` из источника, а не «1–2»);
    * сначала страница целиком на ``dpi``; если обхода нет — тот же диапазон кусками 2×2
      (это и есть «мельче» при плотном шрифте, см. ``resolve_ocr_source``);
    * ошибки не глотаются: они возвращаются в ``error`` и попадают в отчёт;
    * готовый текст из кеша OCR отдаётся сразу, но помечается как «из кеша».
    """
    src = dict(source or {})
    pdf = Path(path)
    started = time.monotonic()
    try:
        first, last = pages_from_spec(src.get("ocr_pages") or src.get("pdf_pages") or "1")
    except PDFError as exc:
        return OcrEscalation(error=str(exc), seconds=0.0, dpi=dpi)
    pages = (first, last)
    cached = ocr_pages.is_cached(pdf, first, last, dpi=dpi)
    try:
        text = ocr_pages.ocr_pdf_pages(pdf, first, last, dpi=dpi, progress=progress)
    except Exception as exc:  # noqa: BLE001 - ошибка уходит в отчёт, а не теряется
        return OcrEscalation(
            error=describe_exc(exc), seconds=time.monotonic() - started,
            cached=cached, pages=pages, dpi=dpi,
        )
    tiled = False
    if not has_legal_courses(text):
        if progress:
            progress("целиком обход не прочитан — пробую по кускам 2×2 (OCR, локально)…")
        tiled_cached = ocr_pages.is_cached(pdf, first, last, dpi=dpi, tile=2)
        try:
            tiled_text = ocr_pages.ocr_pdf_pages(
                pdf, first, last, dpi=dpi, progress=progress, tile=2,
            )
        except Exception:  # noqa: BLE001 - целиком текст уже есть, его и оставляем
            tiled_text = ""
        if has_legal_courses(tiled_text):
            text, tiled, cached = tiled_text, True, tiled_cached
    return OcrEscalation(
        text=text.strip(), seconds=time.monotonic() - started,
        cached=cached, tiled=tiled, pages=pages, dpi=dpi,
    )


def make_item(
    path: str | Path, *, vision_max_pages: int = 8, ocr_available: bool = False
) -> PdfSetItem:
    """
    Открывает PDF и подбирает страницы и способ разбора по умолчанию.

    Страницы берутся из имени файла (``…_p17``, ``…_p2_5``). Если в имени их
    нет — все страницы; для скана без текста обхода — не больше лимита
    Vision, чтобы случайно не отправить в ИИ весь многостраничный документ.
    """
    pdf = Path(path)
    if pdf.suffix.lower() != ".pdf":
        raise PDFError(f"ожидался PDF, получено: {pdf.name}")
    texts = read_pdf_page_texts(pdf)
    total = len(texts)
    item = PdfSetItem(
        path=pdf, page_texts=texts, first_page=1, last_page=total,
        ocr_ok=bool(ocr_available),
    )
    named = pages_from_pdf_name(pdf.name)
    item.name_pages = named
    limit = max(1, int(vision_max_pages or 8))

    if named is not None and named[0] <= total:
        first, last = named
        if last > total:
            item.pages_note = (
                f"в имени указано {format_pages(first, last)}, но в файле {total} стр. — взято до {total}-й"
            )
            last = total
        else:
            item.pages_note = "страницы взяты из имени файла"
        item.set_pages(first, last)
        return item

    if named is not None:
        item.pages_note = (
            f"в имени указано {format_pages(*named)}, но в файле всего {total} стр. — проверьте страницы"
        )
    item.set_pages(1, total)
    if item.use_llm and total > limit:
        # Скан без текста: в ИИ ушли бы изображения всех страниц.
        item.set_pages(1, limit)
        note = f"в имени нет страниц; взяты первые {limit} из {total} (лимит изображений для ИИ)"
        item.pages_note = f"{item.pages_note}; {note}" if item.pages_note else note
    return item


def set_signature(items: list[PdfSetItem]) -> tuple[Any, ...]:
    """Состав набора: файлы, роли, страницы и способ разбора."""
    return tuple(item.signature() for item in items)
