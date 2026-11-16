"""
Шаг 0: документы участка в Richmond County Clerk (Staten Island).

Схема Коли (15.11.2026, по его видео):

1. по block/lot открывается список документов участка:
   ``/Search/ShowResultsBlocks/0?Block=6323&Lot=32&SelectedDocumentIdentifier=0``;
2. в списке берутся строки **Deed**, а если их нет — **Mortgage**;
3. по строке открывается карточка ``/Search/ViewDocumentInfo/<id>``, а из неё —
   сам PDF ``/ViewVscmsDocument/ViewContent?p_endorsementId=<id>``;
4. для **нашего** лота качаются самый свежий и самый старый документ, для
   **соседей** — только свежий;
5. имя файла: ``OUR 32 NEW.pdf`` / ``OUR 32 OLD.pdf`` (сосед — ``LOT 30 NEW.pdf``).

Важно: Staten Island — это Richmond County, и его документы лежат **не в ACRIS**
(ACRIS покрывает остальные четыре района). Поэтому Шаг 0 по районам разный:
здесь — County Clerk, остальные — ACRIS.

Модуль не ходит в сеть сам по себе: ``parse_document_rows`` работает с HTML,
который ему дали (страница из браузера или ответ загрузчика), а ``download_pdf``
умеет скачать файл, но честно падает ``CloudflareBlocked``, если сайт показал
проверку на бота. Это позволяет тестировать всю логику без интернета.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlencode

#: Адрес сайта (можно переопределить в тестах и настройках).
BASE_URL = "https://www.richmondcountyclerk.com"

#: Типы документов, которые нам нужны, в порядке предпочтения.
DEED_KINDS = ("DEED", "WAR DEED", "BARGAIN AND SALE DEED", "ADMINISTRATRIX DEED",
              "EXECUTOR DEED", "REFEREE DEED", "QUITCLAIM DEED", "CORRECTIVE DEED")
MORTGAGE_KINDS = ("MORTGAGE", "MTGE", "CONSOLIDATION", "AGREEMENT")

#: Как называются файлы (§5 схемы Коли).
OUR_PREFIX = "OUR"
NEIGHBOUR_PREFIX = "LOT"
NEW_TAG = "NEW"
OLD_TAG = "OLD"


class RcclerkError(RuntimeError):
    """Что-то не так со страницей или документом."""


class CloudflareBlocked(RcclerkError):
    """Сайт показал проверку на бота вместо PDF."""


class ManualStepNeeded(RcclerkError):
    """Автоматические ступени не сработали: нужен ручной шаг человека."""

    def __init__(self, message: str, url: str = "", *, folder: str | Path | None = None):
        super().__init__(message)
        self.url = url
        self.folder = Path(folder) if folder is not None else None


@dataclass(frozen=True)
class DeedRow:
    """Строка списка документов: чем открывается и что это за документ."""

    endorsement_id: str
    doc_type: str = ""
    doc_date: Optional[date] = None
    party: str = ""
    raw: str = ""

    @property
    def is_deed(self) -> bool:
        return self.doc_type.upper() in DEED_KINDS or "DEED" in self.doc_type.upper()

    @property
    def is_mortgage(self) -> bool:
        text = self.doc_type.upper()
        return text in MORTGAGE_KINDS or "MORTGAGE" in text or "MTGE" in text


def results_url(block: int | str, lot: int | str, *, base: str = BASE_URL) -> str:
    """Страница списка документов участка (как её открывает Коля)."""
    return (
        f"{base}/Search/ShowResultsBlocks/0"
        f"?Block={int(block)}&Lot={int(lot)}&SelectedDocumentIdentifier=0"
    )


def document_info_url(endorsement_id: int | str, *, base: str = BASE_URL) -> str:
    """Карточка документа."""
    return f"{base}/Search/ViewDocumentInfo/{int(endorsement_id)}"


def document_pdf_url(endorsement_id: int | str, *, base: str = BASE_URL) -> str:
    """Сам PDF документа (та самая ссылка, за которой стоит Cloudflare)."""
    return f"{base}/ViewVscmsDocument/ViewContent?p_endorsementId={int(endorsement_id)}"


# ---------------------------------------------------------------------------
# Разбор списка документов
# ---------------------------------------------------------------------------

_ROW_LINK_RE = re.compile(r"ViewDocumentInfo/(\d+)", re.IGNORECASE)
_ROW_SPLIT_RE = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")


def _text_of(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    return " ".join(text.replace("&nbsp;", " ").replace("&amp;", "&").split())


def _row_date(text: str) -> Optional[date]:
    for month, day, year in _DATE_RE.findall(text):
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            continue
    return None


def _row_type(text: str) -> str:
    """Тип документа из текста строки: «DEED», «MORTGAGE», …"""
    upper = text.upper()
    # Составные названия важнее: «WAR DEED» не должно стать просто «DEED».
    for kind in sorted(set(DEED_KINDS) | set(MORTGAGE_KINDS), key=len, reverse=True):
        if kind in upper:
            return kind
    if "DEED" in upper:
        return "DEED"
    if "MORTGAGE" in upper or "MTGE" in upper:
        return "MORTGAGE"
    return ""


def parse_document_rows(html: str) -> list[DeedRow]:
    """
    Строки списка документов: номер, тип, дата.

    Разбор нарочно терпимый: страницу сайта мы пока видели только глазами, поэтому
    ищем ссылки ``ViewDocumentInfo/<id>`` и вокруг них — тип документа и дату.
    Если сайт отдаёт строки не в ``<tr>``, разбор всё равно найдёт номера и типы
    (тогда дата может не определиться). Настоящий HTML надо один раз прогнать
    через ``docs/step0_deed_intake_plan.md`` §15 и, если нужно, уточнить правила.
    """
    rows: list[DeedRow] = []
    blocks = _ROW_SPLIT_RE.findall(html or "")
    if not blocks:
        blocks = [html or ""]
    seen: set[str] = set()
    for block in blocks:
        match = _ROW_LINK_RE.search(block)
        if match is None:
            continue
        endorsement_id = match.group(1)
        if endorsement_id in seen:
            continue
        seen.add(endorsement_id)
        text = _text_of(block)
        rows.append(DeedRow(
            endorsement_id=endorsement_id,
            doc_type=_row_type(text),
            doc_date=_row_date(text),
            party="",
            raw=text[:400],
        ))
    return rows


# ---------------------------------------------------------------------------
# Что именно качать
# ---------------------------------------------------------------------------


def _sorted_by_date(rows: Iterable[DeedRow]) -> list[DeedRow]:
    """Старые сверху; строки без даты — в конец (не выбрасываем: могут быть единственными)."""
    dated = [row for row in rows if row.doc_date is not None]
    undated = [row for row in rows if row.doc_date is None]
    return sorted(dated, key=lambda row: row.doc_date) + undated


def choose_documents(
    rows: Iterable[DeedRow], *, role: str = "our", with_old_for_neighbour: bool = False
) -> list[tuple[DeedRow, str]]:
    """
    Какие документы качать: ``[(строка, "NEW" | "OLD"), …]``.

    Правило Коли: ищем Deed; если Deed нет — Mortgage. Для нашего участка берём
    самый свежий **и самый старый** документ, для соседей — только свежий
    (``with_old_for_neighbour`` добавляет старый, когда с межой соседа проблемы).
    """
    rows = list(rows)
    deeds = [row for row in rows if row.is_deed]
    pool = deeds or [row for row in rows if row.is_mortgage]
    if not pool:
        return []
    ordered = _sorted_by_date(pool)
    dated = [row for row in ordered if row.doc_date is not None]
    # «Свежий» — самый поздний из датированных: строка без даты не может быть
    # новее документа 2019 года, её честное место — среди старых.
    newest = dated[-1] if dated else ordered[-1]
    oldest = next((row for row in ordered if row.endorsement_id != newest.endorsement_id), None)
    take_old = role == "our" or with_old_for_neighbour
    chosen: list[tuple[DeedRow, str]] = [(newest, NEW_TAG)]
    if take_old and oldest is not None:
        chosen.append((oldest, OLD_TAG))
    return chosen


def document_file_name(role: str, lot: int | str, tag: str) -> str:
    """
    Имя файла по схеме Коли: ``OUR 32 NEW.pdf``, ``OUR 32 OLD.pdf``,
    ``LOT 30 NEW.pdf``. Шаг 1 узнаёт страницы по ``_p4-6`` — этот суффикс
    добавляется отдельно, когда найдены страницы с описанием.
    """
    prefix = OUR_PREFIX if role == "our" else NEIGHBOUR_PREFIX
    return f"{prefix} {int(lot)} {tag}.pdf"


# ---------------------------------------------------------------------------
# Скачивание
# ---------------------------------------------------------------------------


def download_pdf(
    endorsement_id: int | str,
    destination: str | Path,
    *,
    base: str = BASE_URL,
    timeout: float = 60.0,
    opener=None,
) -> Path:
    """
    Скачивает PDF документа в ``destination``.

    Если вместо файла пришла HTML-страница (проверка Cloudflare) — ``CloudflareBlocked``
    с понятным текстом: такую ссылку надо открыть в браузере (там проверка проходит
    сама), а скачанный файл положить в папку описаний — остальное сделает программа.
    """
    import urllib.error
    import urllib.request

    url = document_pdf_url(endorsement_id, base=base)
    request = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": document_info_url(endorsement_id, base=base),
    })
    open_it = opener or urllib.request.urlopen
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open_it(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type", "")).lower()
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise CloudflareBlocked(
            f"сайт ответил {exc.code} вместо PDF (похоже на проверку Cloudflare). "
            f"Откройте в браузере: {url} — файл скачается, положите его в папку описаний."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - сеть бывает разной
        raise RcclerkError(f"не удалось скачать документ {endorsement_id}: {exc}") from exc
    if data[:5] == b"%PDF-" or "application/pdf" in content_type:
        target.write_bytes(data)
        return target
    if b"<html" in data[:2048].lower() or "text/html" in content_type:
        raise CloudflareBlocked(
            "вместо PDF пришла HTML-страница — это проверка Cloudflare. "
            f"Откройте в браузере: {url} — файл скачается, положите его в папку описаний."
        )
    raise RcclerkError(
        f"документ {endorsement_id} пришёл неизвестного вида ({content_type or 'без типа'}, "
        f"{len(data)} байт)"
    )


# ---------------------------------------------------------------------------
# Лестница скачивания: от самого автоматического к ручному
# ---------------------------------------------------------------------------

#: Недокачанные файлы браузера — их нельзя принимать за документ.
PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp", ".download")


def _default_downloads_dir() -> Path:
    return Path.home() / "Downloads"


def _open_in_browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


def _is_pdf_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def find_new_pdf(folder: str | Path, *, since: float | None = None) -> Optional[Path]:
    """
    Самый свежий готовый PDF в папке, появившийся после ``since`` (время в секундах).

    Недокачанные файлы (``.crdownload`` и подобные) и файлы без сигнатуры ``%PDF-``
    пропускаются: принять недокачанный документ за документ хуже, чем подождать.
    """
    best: Optional[Path] = None
    best_time = -1.0
    for path in sorted(Path(folder).glob("*.pdf")):
        if path.suffix.lower() in PARTIAL_SUFFIXES:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if since is not None and mtime < since:
            continue
        if not _is_pdf_file(path):
            continue
        if mtime > best_time:
            best, best_time = path, mtime
    return best


def download_with_fallback(
    endorsement_id: int | str,
    destination: str | Path,
    *,
    base: str = BASE_URL,
    opener=None,
    open_browser=None,
    downloads_dir: str | Path | None = None,
    watch_timeout: float = 300.0,
    poll: float = 1.0,
    on_step=None,
) -> tuple[Path, str]:
    """
    Скачивает документ, спускаясь по лестнице: прямо → браузер → человек.

    1. **Прямое скачивание** (`download_pdf`). Работает, если Cloudflare не мешает.
    2. **Браузер**: открываем ссылку в браузере по умолчанию и ждём, пока в «Загрузках»
       появится новый PDF (проверка на бота проходит сама, минуты за три). Файл
       подхватывается и копируется в нужное место — человеку ничего делать не надо.
    3. **Ручной шаг**: если браузер не открылся или файл не появился, ``ManualStepNeeded``
       с готовой ссылкой: открыть, дождаться и положить файл в папку описаний.

    Возвращает ``(путь, ступень)``, где ступень — ``"direct"`` или ``"browser"``.
    """
    import shutil
    import time

    url = document_pdf_url(endorsement_id, base=base)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)

    def step(message: str) -> None:
        if on_step is not None:
            on_step(message)

    step("1/3 пробую скачать напрямую…")
    try:
        return download_pdf(endorsement_id, target, base=base, opener=opener), "direct"
    except CloudflareBlocked:
        step("прямое скачивание закрыто проверкой Cloudflare — открываю браузер")

    folder = Path(downloads_dir) if downloads_dir is not None else _default_downloads_dir()
    open_it = open_browser or _open_in_browser
    # Небольшой запас: время файла может оказаться чуть раньше времени открытия.
    since = time.time() - 1.0
    try:
        open_it(url)
    except Exception as exc:  # noqa: BLE001 - браузера может не быть вовсе
        raise ManualStepNeeded(
            f"не удалось открыть браузер ({exc}). Откройте ссылку вручную: {url} — "
            "файл скачается, положите его в папку описаний",
            url, folder=folder,
        ) from exc
    step(f"2/3 ждём файл в «{folder}» — проверка на сайте идёт сама, до {int(watch_timeout)} с")

    deadline = time.monotonic() + max(0.0, float(watch_timeout))
    while True:
        found = find_new_pdf(folder, since=since)
        if found is not None:
            shutil.copyfile(found, target)
            step(f"файл взят из загрузок: {found.name}")
            return target, "browser"
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.05, float(poll)))
    raise ManualStepNeeded(
        f"файл не появился в «{folder}» за {int(watch_timeout)} с. Откройте ссылку вручную: "
        f"{url} — дождитесь скачивания и положите PDF в папку описаний",
        url, folder=folder,
    )
