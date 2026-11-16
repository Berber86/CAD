"""
Шаг 0: страницы описания в скачанном документе и его обрезанная копия.

Схема Коли: PDF сохраняется **целиком** (там обложка, RP-5217, Exhibit A), а
рядом кладётся обрезанная копия только со страницами описания — её и удобно
открывать, и Шаг 1 читает её мгновенно. Имена: ``OUR 32 NEW.pdf`` (целиком) и
``OUR 32 NEW_p4-6.pdf`` (описание); суффикс ``_p4-6`` Шаг 1 понимает сам.

Страницы ищутся бесплатно: сначала текстовый слой (если он есть), затем
локальный OCR (`ocr_pages`) — тот же, что распознаёт сканы в Шаге 1. Платный ИИ
здесь не нужен ни на каком шаге.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Callable, Iterable

import ocr_pages
from llm_parser import PDFError, has_legal_courses, read_pdf_page_texts

Progress = Callable[[str], None]


@dataclass
class DescriptionPages:
    """Что нашлось: страницы с описанием и их текст."""

    pages: list[int] = field(default_factory=list)
    text: str = ""
    scanned: int = 0          # сколько страниц пришлось распознавать OCR
    from_text_layer: bool = False

    @property
    def pages_spec(self) -> str:
        """``"4-6"`` для имени файла и паспорта (пустая строка, если страниц нет)."""
        return pages_spec(self.pages)


def pages_spec(pages: Iterable[int]) -> str:
    """Номера страниц в вид ``"4"`` / ``"4-6"`` / ``"4,7-8"`` (как в имени файла)."""
    numbers = sorted({int(page) for page in pages if int(page) > 0})
    if not numbers:
        return ""
    chunks: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        chunks.append(f"{start}" if start == previous else f"{start}-{previous}")
        start = previous = number
    chunks.append(f"{start}" if start == previous else f"{start}-{previous}")
    return ",".join(chunks)


_DESCRIPTION_START_RE = re.compile(r"\bBEGINNING\s+(?:AT|ON)\b", re.IGNORECASE)
_OCR_SEP = r"[\s.,:;\-]+"
_DESCRIPTION_END_RE = re.compile(
    rf"\bT[O0]{_OCR_SEP}(?:TH[EA]{_OCR_SEP})?(?:TRUE{_OCR_SEP})?"
    rf"(?:P[O0][I1L]NT(?:{_OCR_SEP}(?:[O0]R|AND){_OCR_SEP}P[L1I]ACE)?|P[L1I]ACE)"
    rf"{_OCR_SEP}[O0]F{_OCR_SEP}BEG[I1L]NN[I1L]NG\b",
    re.IGNORECASE,
)


def has_description_start(text: str | None) -> bool:
    """Страница содержит характерное начало юридического описания участка."""
    return _DESCRIPTION_START_RE.search(str(text or "")) is not None


def has_description_end(text: str | None, *, after: int = 0) -> bool:
    """Стандартная заключительная фраза: «to the point [or place] of beginning»."""
    return _DESCRIPTION_END_RE.search(str(text or ""), max(0, int(after))) is not None


def _description_start_end(text: str) -> int:
    match = _DESCRIPTION_START_RE.search(text)
    return match.end() if match else 0


def find_description_pages(
    pdf: str | Path,
    *,
    progress: Progress | None = None,
    dpi: int = ocr_pages.DEFAULT_DPI,
    max_pages: int | None = None,
) -> DescriptionPages:
    """
    Ищет описание с приоритетом на фразы его начала и завершения.

    Для документов от 4 страниц сначала идёт по первой половине (``floor(n/2)``),
    затем — только если там нет ни начала, ни достаточных курсов — по второй.
    Как только найдено начало или устойчивая последовательность курсов, поиск
    прекращается; если фраза конца ещё не встретилась, добавляются следующие
    страницы до неё. Текстовый слой проверяется мгновенно, OCR вызывается только
    для страниц без текста и только по мере продвижения к найденному описанию.
    """
    path = Path(pdf)
    try:
        texts = read_pdf_page_texts(path)
    except PDFError:
        texts = []
    total = len(texts)
    limit = total if max_pages is None else min(total, max(1, int(max_pages)))
    if limit <= 0:
        return DescriptionPages()

    mid = max(1, limit // 2) if limit >= 4 else limit
    ranges = [(0, mid, 1)]
    if mid < limit:
        ranges.append((mid, limit, 2))

    cache: dict[int, str] = {}
    scanned_pages: set[int] = set()
    used_text_layer = False
    ocr_available = ocr_pages.available()

    def page_text(index: int, half_no: int) -> str:
        nonlocal used_text_layer
        if index in cache:
            return cache[index]
        text = texts[index] if index < len(texts) else ""
        if text.strip():
            used_text_layer = True
            cache[index] = text
            return text
        if not ocr_available:
            cache[index] = ""
            return ""
        page_no = index + 1
        if progress:
            progress(
                f"ищу ключевые фразы: часть {half_no}/2, страница {page_no}/{limit}, "
                f"локальный OCR {int(dpi)} dpi…"
            )
        scanned_pages.add(page_no)
        try:
            text = ocr_pages.ocr_pdf_pages(path, page_no, page_no, dpi=int(dpi))
        except ocr_pages.OcrError:
            text = ""
        cache[index] = text
        return text

    def collect_from(start_index: int, start_text: str) -> DescriptionPages:
        pages = [start_index + 1]
        pieces = [start_text]
        marker_end = _description_start_end(start_text)
        if has_description_end(start_text, after=marker_end):
            return DescriptionPages(
                pages=pages, text="\n".join(pieces), scanned=len(scanned_pages),
                from_text_layer=used_text_layer and not scanned_pages,
            )
        # Если фраза завершения не найдена на той же странице — берём с запасом
        # только следующую страницу и дальше не ищем.
        next_index = start_index + 1
        if next_index < limit:
            text = page_text(next_index, 2 if next_index >= mid and limit >= 4 else 1)
            pages.append(next_index + 1)
            pieces.append(text)
            if progress and not has_description_end("\n".join(pieces), after=marker_end):
                progress(
                    f"фраза завершения на стр. {start_index + 1} не найдена — "
                    f"беру с запасом стр. {start_index + 1}–{pages[-1]}"
                )
        return DescriptionPages(
            pages=pages, text="\n".join(pieces), scanned=len(scanned_pages),
            from_text_layer=used_text_layer and not scanned_pages,
        )

    for first, stop, half_no in ranges:
        if progress and len(ranges) > 1:
            progress(f"ищу начало описания в части {half_no}/2 (стр. {first + 1}–{stop})…")
        for index in range(first, stop):
            text = page_text(index, half_no)
            if not text.strip():
                continue
            if has_description_start(text):
                if progress:
                    progress(f"начало описания найдено на стр. {index + 1}; проверяю фразу завершения…")
                return collect_from(index, text)
            if has_legal_courses(text) and not re.search(r"\bCOMMENCING\b", text, re.IGNORECASE):
                if progress:
                    progress(f"курсы обхода найдены на стр. {index + 1}; беру как начало описания")
                return collect_from(index, text)

    return DescriptionPages(scanned=len(scanned_pages))

def trim_pdf(source: str | Path, pages: Iterable[int], destination: str | Path) -> Path:
    """Копия документа только с указанными страницами (порядок сохраняется)."""
    numbers = [int(page) for page in pages]
    if not numbers:
        raise PDFError("не указаны страницы для обрезки")
    import pymupdf  # noqa: PLC0415 - уже зависимость программы

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(str(source))
    trimmed = pymupdf.open()
    try:
        for number in numbers:
            if not 1 <= number <= len(doc):
                raise PDFError(f"в «{Path(source).name}» нет страницы {number}")
            trimmed.insert_pdf(doc, from_page=number - 1, to_page=number - 1)
        trimmed.save(str(target))
    finally:
        trimmed.close()
        doc.close()
    return target
