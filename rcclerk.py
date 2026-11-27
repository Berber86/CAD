"""
Шаг 0: документы участка в Richmond County Clerk (Staten Island).

Схема Коли (15.11.2026, по его видео):

1. по block/lot открывается список документов участка:
   ``/Search/ShowResultsBlocks/0?Block=6323&Lot=32&SelectedDocumentIdentifier=0``;
2. в списке берутся строки **Deed** новее 2015 года; если такого нет, а есть **Mortgage**
   с 2015 года — берётся она (NEW), а самый древний **Deed** становится OLD;
3. по строке открывается карточка ``/Search/ViewDocumentInfo/<id>``, а из неё —
   сам PDF ``/ViewVscmsDocument/ViewContent?p_endorsementId=<id>``;
4. для **нашего** лота качаются самый свежий и самый старый документ, для
   **соседей** — только свежий;
5. имя файла: ``OUR LOT 32 NEW.pdf`` / ``OUR LOT 32 OLD.pdf`` (сосед — ``LOT 30 NEW.pdf``).

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


class DocumentServerError(RcclerkError):
    """Сайт округа сам не смог отдать документ (его страница ошибки). Это не Cloudflare."""


#: Фразы страницы ошибки сайта округа (HTML/JSON), которую нельзя принимать за проверку бота.
_SERVER_ERROR_MARKERS = (
    "could not load the requested document",
    "unexpected error",
    "error reading jobject",
)


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


#: Deed имеет приоритет над Mortgage, только если он новее этого года (правило Коли, 17.11.2026).
#: Mortgage, начиная с этого года, при отсутствии свежего Deed становится NEW, а древний Deed — OLD.
DEED_PRIORITY_FROM_YEAR = 2015


def _is_fresh(row: DeedRow) -> bool:
    return row.doc_date is not None and row.doc_date.year >= DEED_PRIORITY_FROM_YEAR


def choose_documents(
    rows: Iterable[DeedRow], *, role: str = "our", with_old_for_neighbour: bool = False
) -> list[tuple[DeedRow, str]]:
    """
    Какие документы качать: ``[(строка, "NEW" | "OLD"), …]``.

    Правило Коли:

    * есть Deed новее ``DEED_PRIORITY_FROM_YEAR`` → NEW = самый свежий Deed;
    * иначе, если есть Mortgage с ``DEED_PRIORITY_FROM_YEAR`` года и позже → NEW = самая
      свежая такая ипотека, а OLD (только для нашего участка) = самый древний Deed;
    * иначе, если Deed есть → как раньше: NEW = самый свежий Deed, OLD = самый древний;
    * иначе Mortgage: NEW = самая свежая, OLD = самая древняя.

    Для нашего участка берём свежий **и** старый, для соседей — только свежий
    (``with_old_for_neighbour`` добавляет старый, когда с межой соседа проблемы).
    Строка без даты не считается «новее 2015 года».
    """
    rows = list(rows)
    deeds = [row for row in rows if row.is_deed]
    mortgages = [row for row in rows if row.is_mortgage]
    fresh_deeds = [row for row in deeds if _is_fresh(row)]
    fresh_mortgages = [row for row in mortgages if _is_fresh(row)]
    if not deeds and not mortgages:
        return []
    if deeds and not fresh_deeds and fresh_mortgages:
        # Древний Deed не побеждает свежую ипотеку: ипотека — NEW, Deed — OLD.
        newest = _sorted_by_date(fresh_mortgages)[-1]
        oldest_pool = deeds
    else:
        pool = deeds or mortgages
        ordered = _sorted_by_date(pool)
        dated = [row for row in ordered if row.doc_date is not None]
        # «Свежий» — самый поздний из датированных: строка без даты не может быть
        # новее документа 2019 года, её честное место — среди старых.
        newest = dated[-1] if dated else ordered[-1]
        oldest_pool = pool
    oldest = next(
        (row for row in _sorted_by_date(oldest_pool) if row.endorsement_id != newest.endorsement_id),
        None,
    )
    take_old = role == "our" or with_old_for_neighbour
    chosen: list[tuple[DeedRow, str]] = [(newest, NEW_TAG)]
    if take_old and oldest is not None:
        chosen.append((oldest, OLD_TAG))
    return chosen


def document_file_name(role: str, lot: int | str, tag: str) -> str:
    """
    Имя файла по схеме Коли: ``OUR LOT 32 NEW.pdf``, ``OUR LOT 32 OLD.pdf``
    (наш лот — слово OUR в начале), ``LOT 30 NEW.pdf`` (сосед — без OUR).
    Шаг 1 узнаёт страницы по ``_p4-6`` — этот суффикс добавляется отдельно,
    когда найдены страницы с описанием.
    """
    prefix = "OUR LOT" if role == "our" else NEIGHBOUR_PREFIX
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
    head = data[:4096].decode("utf-8", errors="replace").lower()
    if any(marker in head for marker in _SERVER_ERROR_MARKERS):
        raise DocumentServerError(
            f"сайт округа не отдал документ {endorsement_id}: ошибка на стороне сайта. "
            "Попробуйте позже; если повторяется — клерк округа Richmond, 718-675-7700."
        )
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


def default_downloads_dir() -> Path:
    """Папка «Загрузки» текущего пользователя (куда браузер сохраняет файлы)."""
    return _default_downloads_dir()


def _open_in_browser(url: str) -> None:
    import browser_save

    browser_save.open_url(url)


def _is_pdf_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def _pdf_snapshot(folder: str | Path) -> dict[Path, tuple[int, int]]:
    """Снимок уже лежащих в папке PDF: ``resolved_path -> (mtime_ns, size)``."""
    snapshot: dict[Path, tuple[int, int]] = {}
    try:
        paths = list(Path(folder).glob("*.pdf"))
    except OSError:
        return snapshot
    for path in paths:
        if path.suffix.lower() in PARTIAL_SUFFIXES:
            continue
        try:
            st = path.stat()
            snapshot[path.resolve()] = (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            continue
    return snapshot


def find_new_pdf(
    folder: str | Path,
    *,
    since: float | None = None,
    ignore_snapshot: dict[Path, tuple[int, int]] | None = None,
) -> Optional[Path]:
    """
    Самый свежий готовый PDF в папке, появившийся после ``since`` (время в секундах).

    Недокачанные файлы (``.crdownload`` и подобные) и файлы без сигнатуры ``%PDF-``
    пропускаются: принять недокачанный документ за документ хуже, чем подождать.
    """
    candidates: list[tuple[float, Path]] = []
    try:
        paths = list(Path(folder).glob("*.pdf"))
    except OSError:
        return None
    for path in paths:
        if path.suffix.lower() in PARTIAL_SUFFIXES:
            continue
        try:
            st = path.stat()
            mtime = st.st_mtime
        except OSError:
            continue
        if ignore_snapshot is not None:
            try:
                prev = ignore_snapshot.get(path.resolve())
            except OSError:
                prev = None
            if prev == (int(st.st_mtime_ns), int(st.st_size)):
                continue
        if since is not None and mtime < since:
            continue
        candidates.append((mtime, path))
    # Не открываем каждый PDF в папке на каждом опросе: сначала самые свежие,
    # и первая же настоящая сигнатура PDF — ответ. Недействительный свежий
    # файл не мешает проверить следующий по времени.
    for _mtime, path in sorted(candidates, key=lambda item: (-item[0], str(item[1]))):
        if _is_pdf_file(path):
            return path
    return None


def _drop_our_pending(path: Path, downloads_dir: Path) -> None:
    """Удаляет наш собственный временный файл из «Загрузок» (чужие не трогаем)."""
    try:
        if path.parent.resolve() != Path(downloads_dir).resolve():
            return
        if not path.name.startswith("tsai_step0_"):
            return
        path.unlink()
    except OSError:
        return


#: Сколько раз повторить прямое скачивание, если сайт округа ответил ошибкой, и пауза между попытками.
SERVER_ERROR_RETRIES = 2
SERVER_ERROR_RETRY_PAUSE_SEC = 5.0


class DownloadStopped(Exception):
    """Пользователь нажал «Стоп» во время ожидания загрузки — это не ошибка документа."""


def _default_failure_shot(folder: Path, endorsement_id: int | str) -> str:
    """«Чёрный ящик»: снимок окна браузера в папку при тайм-ауте загрузки.

    Без снимка не понять, почему PDF не скачался: проверка Cloudflare, не та
    страница, зависший просмотрщик, чужой диалог. Любой сбой снимка — пустой
    хвост: диагностика не должна ломать и без того ручной шаг.
    """
    try:
        import acris_viewer
        import browser_save
        from nyc_portal import _save_failure_shot

        window = acris_viewer._pick_window(  # noqa: SLF001 - общий выбор окна браузера
            browser_save.list_windows, browser_save.active_window_handle)
        if window is None:
            return ""
        return _save_failure_shot(
            window.handle, folder, prefix=f"CLERK_FAIL_{endorsement_id}")
    except Exception:  # noqa: BLE001 - диагностика не ломает остановку
        return ""


def browser_doctor(endorsement_id: int | str, *, folder: str | Path | None = None,
                   emit=None, windows_fn=None) -> list[str]:
    """«Доктор» браузера: вслух фиксирует, что реально видит автоматика.

    Живой прогон 25.11.2026: четыре документа подряд не скачались, а журнал
    молчал, какое окно и заголовок видела автоматика, — чинить было нечего.
    Доктор перечисляет окна поддерживаемых браузеров с вердиктом по каждому
    (PDF опознан / проверка Cloudflare / не PDF), снимает окно в папку
    задания ``BROWSER_DOCTOR_<id>_<время>.png`` и читает экран OCR-ом.
    Ничего не кликает и не печатает: капчу проходит человек.
    """
    say = emit if emit is not None else (lambda _message: None)
    lines: list[str] = []

    def note(message: str) -> None:
        lines.append(message)
        try:
            say(message)
        except Exception:  # noqa: BLE001 - диагностика не ломает ожидание
            pass

    try:
        import browser_save

        windows = list((windows_fn or browser_save.list_windows)())
    except Exception:  # noqa: BLE001
        note("доктор: не удалось перечислить окна браузера")
        return lines
    preferred = browser_save.get_preferred_browser()
    if preferred != "auto":
        note(f"доктор: приоритетный браузер — {browser_save.preferred_browser_label(preferred)}")
    browsers = [
        window for window in windows
        if browser_save.is_supported_browser(window.executable, window.image_path)
    ]
    if not browsers:
        note("доктор: окон поддерживаемых браузеров (Chrome, Edge, Firefox, Yandex и т.п.) "
             "не видно — браузер закрыт или не поддерживается автоматикой")
        return lines
    for window in browsers:
        if browser_save.looks_like_loaded_pdf(window.title):
            verdict = "заголовок опознан как загруженный PDF"
        elif browser_save.looks_like_cloudflare_page(window.title):
            verdict = "идёт проверка Cloudflare — отметьте «я не робот» прямо в этой вкладке"
        else:
            verdict = "заголовок не похож на PDF"
        if preferred != "auto" and not browser_save.matches_browser(window, preferred):
            verdict += " (пропущено: выбран другой приоритетный браузер)"
        note(f"доктор: окно [{window.executable}] «{window.title}» — {verdict}")
    if folder is not None:
        try:
            import acris_viewer
            from nyc_portal import _save_window_capture, _screen_ocr_region

            window = acris_viewer._pick_window(  # noqa: SLF001 - общий выбор окна браузера
                browser_save.list_windows, browser_save.active_window_handle)
            if window is not None:
                from datetime import datetime

                shot = Path(folder) / (
                    f"BROWSER_DOCTOR_{endorsement_id}_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
                _save_window_capture(window.handle, shot)
                note(f"доктор: снимок окна браузера: {shot}")
                try:
                    from PIL import Image

                    with Image.open(shot) as shot_img:
                        adobe_pt = browser_save.find_adobe_download_point(shot_img)
                    if adobe_pt is not None:
                        note(f"доктор: активен просмотрщик Adobe Acrobat (кнопка скачивания ↓ на {adobe_pt})")
                except Exception:  # noqa: BLE001 - диагностика необязательна
                    pass
                try:
                    rows = _screen_ocr_region(shot, (0.0, 0.0, 1.0, 1.0))
                    texts = [row.text.strip() for row in rows if row.text.strip()]
                    if texts:
                        note("доктор: на экране читается: " + " | ".join(texts[:12]))
                except Exception:  # noqa: BLE001 - OCR необязателен
                    pass
        except Exception:  # noqa: BLE001 - диагностика не ломает ожидание
            pass
    return lines


def download_with_fallback(
    endorsement_id: int | str,
    destination: str | Path,
    *,
    base: str = BASE_URL,
    opener=None,
    open_browser=None,
    downloads_dir: str | Path | None = None,
    watch_timeout: float = 45.0,
    poll: float = 1.0,
    auto_save=None,
    on_step=None,
    should_stop=None,
    fail_dir: str | Path | None = None,
    shot_fn=None,
    prompt_operator=None,
) -> tuple[Path, str]:
    """
    Скачивает документ, спускаясь по лестнице: прямо → браузер → человек.

    1. **Прямое скачивание** (`download_pdf`). Работает, если Cloudflare не мешает.
    2. **Браузер**: открываем ссылку и ждём завершения проверки Cloudflare. Когда
       заголовок браузерного окна показывает уже загруженный PDF, безопасный хук
       один раз отправляет Ctrl+S именно этому браузеру. Затем файл подхватывается
       из «Загрузок».
    3. **Ручной шаг**: если браузер/просмотрщик не распознан или файл не появился
       за ``watch_timeout`` (по умолчанию 45 с — дольше ждать бессмысленно,
       исход ясен раньше), ``ManualStepNeeded`` подсказывает ссылку и папку для
       ручного завершения, а в папку назначения падает аварийный снимок окна
       браузера ``CLERK_FAIL_<endorsement>_<время>.png` для диагностики.

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

    def check_stop() -> None:
        if should_stop is not None and should_stop():
            raise DownloadStopped("остановлено пользователем")

    check_stop()
    step("1/3 пробую скачать напрямую…")
    server_attempts = 0
    while True:
        try:
            return download_pdf(endorsement_id, target, base=base, opener=opener), "direct"
        except DocumentServerError:
            # Ошибка сайта округа: браузер тут не поможет. Повторяем пару раз и сдаёмся.
            server_attempts += 1
            if server_attempts > SERVER_ERROR_RETRIES:
                raise
            step(f"сайт округа вернул ошибку — повтор {server_attempts} из {SERVER_ERROR_RETRIES} через "
                 f"{int(SERVER_ERROR_RETRY_PAUSE_SEC)} с")
            deadline = time.monotonic() + SERVER_ERROR_RETRY_PAUSE_SEC
            while time.monotonic() < deadline:
                check_stop()
                time.sleep(0.2)
        except CloudflareBlocked:
            step("прямое скачивание закрыто проверкой Cloudflare — открываю браузер")
            break

    folder = Path(downloads_dir) if downloads_dir is not None else _default_downloads_dir()
    open_it = open_browser or _open_in_browser
    # Запоминаем существующие окна до открытия URL: даже если в другом браузерном
    # окне уже открыт PDF, не будем посылать ему Ctrl+S вместо новой вкладки.
    prepare_save = getattr(auto_save, "before_open", None)
    if callable(prepare_save):
        try:
            prepare_save(endorsement_id)
        except Exception:  # noqa: BLE001 - безопасность важнее необязательной автоматики
            pass
    # Небольшой запас: время файла может оказаться чуть раньше времени открытия,
    # но файлы, уже лежавшие в «Загрузках» до открытия текущей ссылки, не берём
    # повторно (например RCC-*.pdf от предыдущего документа в той же серии).
    seen_before = _pdf_snapshot(folder)
    since = time.time() - 1.0
    try:
        open_it(url)
    except Exception as exc:  # noqa: BLE001 - браузера может не быть вовсе
        raise ManualStepNeeded(
            f"не удалось открыть браузер ({exc}). Откройте ссылку вручную: {url} — "
            "файл скачается, положите его в папку описаний",
            url, folder=folder,
        ) from exc
    step(
        f"2/3 жду, пока в браузере закончится Cloudflare и откроется PDF "
        f"(до {int(watch_timeout)} с). Если в открывшейся вкладке появится проверка "
        f"«вы не робот» — отметьте галочку прямо в окне браузера и не закрывайте вкладку: "
        f"пока проверка идёт, автоматика вкладку не трогает"
    )

    def _cleanup_save() -> None:
        cleanup_fn = getattr(auto_save, "cleanup", None)
        if callable(cleanup_fn):
            try:
                cleanup_fn(endorsement_id)
            except Exception:  # noqa: BLE001 - очистка не должна ломать ручной шаг/остановку
                pass

    def wait_wave(seconds: float) -> Optional[tuple[Path, str]]:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            check_stop()
            found = find_new_pdf(folder, since=since, ignore_snapshot=seen_before)
            if found is not None:
                shutil.copyfile(found, target)
                _drop_our_pending(found, folder)
                step(f"файл взят из загрузок: {found.name}")
                return target, "browser"
            if auto_save is not None:
                saved = None
                try:
                    saved = auto_save(endorsement_id)
                except Exception:  # noqa: BLE001 - автоматизация необязательна
                    saved = None
                if saved is not None and Path(saved).is_file():
                    shutil.copyfile(saved, target)
                    _drop_our_pending(Path(saved), folder)
                    step("PDF сохранён через Ctrl+S в окне браузера")
                    return target, "browser"
            if time.monotonic() >= deadline:
                return None
            time.sleep(max(0.05, float(poll)))

    try:
        result = wait_wave(watch_timeout)
        if result is not None:
            return result
        doctor_note = ""
        if prompt_operator is not None:
            # Вторая волна: сначала «доктор» вслух говорит, что видит автоматика
            # (окна, заголовки, снимок, OCR экрана), затем ждём ещё — человек
            # успевает пройти капчу, а просмотрщик с незнакомым заголовком
            # получает слепую попытку Ctrl+S от хука.
            second = min(float(watch_timeout), 60.0)
            step(f"первая волна ({int(watch_timeout)} с) пуста — зову «доктора» браузера "
                 f"и жду ещё {int(second)} с")
            try:
                prompt_operator(endorsement_id)
            except Exception:  # noqa: BLE001 - диагностика не ломает ожидание
                pass
            if auto_save is not None:
                state_fn = getattr(auto_save, "state", None)
                if callable(state_fn):
                    try:
                        doctor_note = f" Автоматика браузера докладывает: {state_fn(endorsement_id)}."
                        step(doctor_note.strip())
                    except Exception:  # noqa: BLE001
                        doctor_note = ""
            result = wait_wave(second)
            if result is not None:
                return result
    except DownloadStopped:
        _cleanup_save()
        raise
    # Аварийный снимок окна браузера в папку задания: без него не видно, что
    # было на экране вместо скачивания (капча, не та страница, зависший
    # просмотрщик). Сбой снимка ручной шаг не отменяет.
    shot_dir = Path(fail_dir) if fail_dir is not None else target.parent
    try:
        shot_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        shot = (shot_fn or _default_failure_shot)(shot_dir, endorsement_id)
    except Exception:  # noqa: BLE001 - диагностика не ломает остановку
        shot = ""
    if not doctor_note and auto_save is not None:
        state_fn = getattr(auto_save, "state", None)
        if callable(state_fn):
            try:
                doctor_note = f" Автоматика браузера докладывает: {state_fn(endorsement_id)}."
            except Exception:  # noqa: BLE001
                doctor_note = ""
    _cleanup_save()
    raise ManualStepNeeded(
        f"PDF не появился в «{folder}» за {int(watch_timeout)} с ожидания"
        f"{' плюс вторая волна после «доктора»' if prompt_operator is not None else ''}. "
        f"Ссылка: {url}. Если во вкладке висит проверка Cloudflare — отметьте "
        "«я не робот» и дайте PDF догрузиться, затем нажмите Ctrl+S в его окне; "
        "либо сохраните файл в папку описаний вручную."
        f"{doctor_note}{shot}",
        url, folder=folder,
    )
