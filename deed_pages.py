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


def find_description_pages(
    pdf: str | Path,
    *,
    progress: Progress | None = None,
    dpi: int = ocr_pages.DEFAULT_DPI,
    max_pages: int | None = None,
) -> DescriptionPages:
    """
    Ищет страницы с обходом границ: сначала текстовый слой, потом локальный OCR.

    Дешёвый порядок важен: у документа с текстовым слоем OCR не запускается вовсе.
    У скана распознаётся каждая страница по очереди, поэтому вызов стоит делать
    в фоне (5 с на страницу) и показывать прогресс.
    """
    path = Path(pdf)
    try:
        texts = read_pdf_page_texts(path)
    except PDFError:
        texts = []
    total = len(texts)
    limit = total if max_pages is None else min(total, max(1, int(max_pages)))
    found: list[int] = []
    pieces: list[str] = []
    scanned = 0
    used_layer = False
    for index in range(limit):
        page_no = index + 1
        page_text = texts[index] if index < len(texts) else ""
        if has_legal_courses(page_text):
            found.append(page_no)
            pieces.append(page_text)
            used_layer = True
            continue
        if not ocr_pages.available():
            continue
        if progress:
            progress(f"ищу описание: страница {page_no} из {limit} (локальный OCR)…")
        try:
            recognized = ocr_pages.ocr_pdf_pages(path, page_no, page_no, dpi=dpi)
        except ocr_pages.OcrError:
            continue
        scanned += 1
        if has_legal_courses(recognized):
            found.append(page_no)
            pieces.append(recognized)
    return DescriptionPages(
        pages=found,
        text="\n".join(pieces),
        scanned=scanned,
        from_text_layer=used_layer and not scanned,
    )


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
