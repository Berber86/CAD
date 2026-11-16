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

#: Классификация строки: что это за документ по существу.
KIND_DEED = "DEED"
KIND_MORTGAGE = "MORTGAGE"
KIND_SATISFACTION = "SATISFACTION"
KIND_OTHER = "OTHER"

#: Русские названия: страницу часто сохраняют с переводом браузера (см. §5 плана).
DEED_WORDS = ("ПОСТУПОК", "КУПЧ", "АКТ КУПЛИ", "ДАРСТВЕНН", "ЗАВЕЩАНИ", "DEED")
MORTGAGE_WORDS = ("ИПОТЕК", "ЗАКЛАДН", "MORTGAGE", "MTGE")
#: Погашение ипотеки и переуступка — **не** описание участка, хотя в них есть слово «ипотека».
SATISFACTION_WORDS = (
    "ПОГАШЕН", "ПРЕКРАЩЕН", "РАСПИСК", "SATISFACTION", "DISCHARGE", "RELEASE",
    "ASSIGNMENT", "ПЕРЕУСТУП", "A/MTG", "A/MTGE",
)


def _has_word(text: str, word: str) -> bool:
    """Короткие сокращения (``SAT``, ``САТ``) ищем целым словом, длинные — подстрокой."""
    if len(word) <= 4 and word.isalpha():
        return re.search(rf"(?<![A-ZА-Я]){re.escape(word)}(?![A-ZА-Я])", text) is not None
    return word in text


def classify_document(doc_type: str) -> str:
    """
    DEED / MORTGAGE / SATISFACTION / OTHER — по названию строки, на любом языке.

    Порядок проверок важен: «Погашение ипотеки» содержит «ипотек», но это не ипотека
    заёмщика, а запись о её погашении — брать такой документ за описание участка
    нельзя. Переуступка (``A/MTG``, ``ASSIGNMENT``) — тоже не ипотека.
    """
    upper = str(doc_type or "").upper().replace("Ё", "Е").strip()
    if not upper:
        return KIND_OTHER
    for word in SATISFACTION_WORDS:
        if _has_word(upper, word):
            return KIND_SATISFACTION
    if _has_word(upper, "САТ") or _has_word(upper, "SAT"):
        return KIND_SATISFACTION
    for word in DEED_WORDS:
        if _has_word(upper, word):
            return KIND_DEED
    for word in MORTGAGE_WORDS:
        if _has_word(upper, word):
            return KIND_MORTGAGE
    return KIND_OTHER

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
    #: Номер инструмента у клерка (колонка Number), книга и страница — для паспорта.
    instrument: str = ""
    book: str = ""
    page: str = ""
    #: Место строки в списке: сайт отдаёт документы сверху вниз, от свежих к старым.
    index: int = 0

    @property
    def kind(self) -> str:
        return classify_document(self.doc_type)

    @property
    def is_deed(self) -> bool:
        return self.kind == KIND_DEED

    @property
    def is_mortgage(self) -> bool:
        return self.kind == KIND_MORTGAGE

    @property
    def clerk_number(self) -> str:
        """«№ 876753, кн. 45, стр. 3683» — то, что человек увидит в паспорте."""
        parts = []
        if self.instrument:
            parts.append(f"№ {self.instrument}")
        if self.book:
            parts.append(f"кн. {self.book}")
        if self.page:
            parts.append(f"стр. {self.page}")
        return ", ".join(parts)


def results_url(block: int | str, lot: int | str, *, base: str = BASE_URL) -> str:
    """Страница списка документов участка (как её открывает Коля)."""
    return (
        f"{base}/Search/ShowResultsBlocks/0"
        f"?Block={int(block)}&Lot={int(lot)}&SelectedDocumentIdentifier=0"
    )


CLOUDFLARE_MARKS = ("just a moment", "cf-challenge", "checking your browser", "attention required")


def fetch_list_html(
    block: int | str,
    lot: int | str,
    *,
    base: str = BASE_URL,
    timeout: float = 45.0,
    opener=None,
) -> str:
    """
    Страница списка документов участка (HTML).

    Если сайт показал проверку на бота — ``CloudflareBlocked``: список тогда
    открывают в браузере, а разбор работает по сохранённой странице.
    """
    import urllib.error
    import urllib.request

    url = results_url(block, lot, base=base)
    request = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{base}/Search",
    })
    open_it = opener or urllib.request.urlopen
    try:
        with open_it(request, timeout=timeout) as response:
            charset = ""
            try:
                charset = str(response.headers.get_content_charset() or "")
            except Exception:  # noqa: BLE001 - заголовков может не быть
                charset = ""
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise CloudflareBlocked(
            f"сайт ответил {exc.code} вместо списка документов — откройте в браузере: {url}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RcclerkError(f"не удалось открыть список документов {block}/{lot}: {exc}") from exc
    html = _decode_html(payload, charset)
    lowered = html[:4000].lower()
    if any(mark in lowered for mark in CLOUDFLARE_MARKS) and "ViewDocumentInfo" not in html:
        raise CloudflareBlocked(
            f"вместо списка пришла страница проверки Cloudflare — откройте в браузере: {url}"
        )
    return html


def _decode_html(payload: bytes, charset: str = "") -> str:
    """Текст страницы: заявленная кодировка, потом utf-8, потом cp1252."""
    for encoding in (charset, "utf-8", "cp1252"):
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


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
_DATE_RE = re.compile(r"\b(\d{1,2})\s*([./-])\s*(\d{1,2})\s*[./-]\s*(\d{4})\b", re.UNICODE)
#: Ячейки с устойчивыми английскими именами колонок: их не трогает даже перевод страницы.
_CELL_RE = re.compile(r'<td\b[^>]*\bdata-label\s*=\s*"([^"]*)"[^>]*>(.*?)</td>',
                      re.IGNORECASE | re.DOTALL)


def _text_of(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    return " ".join(text.replace("&nbsp;", " ").replace("&amp;", "&").split())


def _row_date(text: str) -> Optional[date]:
    """
    Дата регистрации из текста строки.

    Формат разный, и это не прихоть: сайт пишет по-американски (``9/3/2022`` —
    месяц/день), а страница с переводом браузера приходит с точками и европейским
    порядком (``03.09.2022`` — день.месяц). Поэтому точки читаем как день.месяц,
    слэш и дефис — как месяц/день; если так не получается, пробуем обратный порядок.
    """
    for first, sep, second, year in _DATE_RE.findall(text):
        # Точки — день.месяц (переведённая страница), слэш и дефис — месяц/день (сайт).
        pairs = ((first, second), (second, first)) if sep == "." else (
            (second, first), (first, second)
        )
        for day, month in pairs:
            try:
                return date(int(year), int(month), int(day))
            except ValueError:
                continue
    return None


def _row_type(text: str) -> str:
    """Тип документа из текста строки: «DEED», «MORTGAGE», «Поступок», …"""
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


def _row_cells(block: str) -> dict[str, str]:
    """Ячейки строки по ``data-label``: ``{"type": "ПОСТУПОК", "recorded": "03.09.2022", …}."""
    cells: dict[str, str] = {}
    for label, inner in _CELL_RE.findall(block):
        key = " ".join(str(label).split()).lower()
        if not key or key in cells:
            continue
        cells[key] = _text_of(inner)
    return cells


def _cell(cells: dict[str, str], *names: str) -> str:
    for name in names:
        value = cells.get(name, "")
        if value:
            return value
    return ""


def parse_document_rows(html: str) -> list[DeedRow]:
    """
    Строки списка документов: номер, тип, дата, книга и страница.

    Разбор опирается на ``data-label`` у ячеек (``Block``, ``Lot``, ``Book``, ``Page``,
    ``Recorded``, ``Type``, ``Number``): эти имена не меняются, даже когда браузер
    переводит страницу на русский — а настоящую страницу заказчик присылает именно
    переведённой (``Поступок`` = Deed, ``Ипотека`` = Mortgage, ``Погашение ипотеки`` =
    Satisfaction). Если ``data-label`` нет (страница другой вёрстки), работает старый
    терпимый разбор по тексту строки: номер из ссылки ``ViewDocumentInfo/<id>``, тип и
    дата — из текста.
    """
    rows: list[DeedRow] = []
    blocks = _ROW_SPLIT_RE.findall(html or "")
    if not blocks:
        blocks = [html or ""]
    seen: set[str] = set()
    for position, block in enumerate(blocks):
        match = _ROW_LINK_RE.search(block)
        if match is None:
            continue
        endorsement_id = match.group(1)
        if endorsement_id in seen:
            continue
        seen.add(endorsement_id)
        text = _text_of(block)
        cells = _row_cells(block)
        doc_type = _cell(cells, "type", "document type", "тип")
        date_text = _cell(cells, "recorded", "recorded date", "дата регистрации")
        instrument = _cell(cells, "number", "instrument", "номер")
        book = _cell(cells, "book", "книга")
        page = _cell(cells, "page", "страница")
        if not doc_type:
            doc_type = _row_type(text)
        number = re.search(r"\d+", instrument)
        rows.append(DeedRow(
            endorsement_id=endorsement_id,
            doc_type=doc_type,
            doc_date=_row_date(date_text or text),
            party="",
            raw=text[:400],
            instrument=number.group(0) if number else "",
            book=book,
            page=page,
            index=position,
        ))
    return rows


# ---------------------------------------------------------------------------
# Что именно качать
# ---------------------------------------------------------------------------


def _sorted_by_date(rows: Iterable[DeedRow]) -> list[DeedRow]:
    """
    Старые сверху; строки без даты — в конец (не выбрасываем: могут быть единственными).

    При одинаковой дате порядок берём со страницы: сайт показывает документы сверху
    вниз, от свежих к старым, поэтому верхняя строка считается более новой.
    """
    dated = [row for row in rows if row.doc_date is not None]
    undated = [row for row in rows if row.doc_date is None]
    return sorted(dated, key=lambda row: (row.doc_date, -row.index)) + undated


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
