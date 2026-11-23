"""
Шаг 0: документы участка в ACRIS (Manhattan, Bronx, Brooklyn, Queens).

Staten Island сюда НЕ ходит: там Richmond County Clerk (``rcclerk.py``) —
первый, уже настроенный вариант. ACRIS покрывает остальные четыре района.

Что подтверждено настоящими страницами, сохранёнными пользователем
2026-11-23 (``docs/acris/*.mhtml``, копии HTML — в ``tests/data/``):

1. Форма ``GET /DS/DocumentSearch/BBL``: список ``borough`` (1 = MANHATTAN /
   NEW YORK, 2 = BRONX, 3 = BROOKLYN / KINGS, 4 = QUEENS, 5 = STATEN ISLAND /
   RICHMOND), поля ``edt_block`` (до 5 цифр) и ``edt_lot`` (до 4 цифр),
   ``edt_unit``, ``cmb_date``, ``combox_doc_doctype``.
2. Отправка идёт скрытой формой ``global`` прямым POST на
   ``/DS/DocumentSearch/BBLResult`` с полями ``hid_*`` — район, блок и лот
   передаются кодом без браузера и без кликов по форме.
3. Страница результатов: таблица «CRFN | Lot | Partial | Doc Date |
   Recorded/Filed | Document Type | Pages | Party1 | Party2 | … | Doc
   Amount», сверху — «Records 1 – N» и список ``com_maxrows`` (10/25/50/99).
   Человеку приходится выбирать 99 руками; в прямом запросе это просто
   ``hid_max_rows=99``, а дальше — ``hid_page`` постранично.

4. Кнопка IMG открывает ``DocumentImageView?doc_id=<16 цифр>``; внутри —
   читалка Acordex VTU (страницы по одной, Print и дискета Save). Диалог
   Save открывается с уже выбранным All: дискета → OK качает весь документ
   PDF в «Загрузки» (скриншот пользователя — ``docs/acris/acris_save_dialog.png``;
   автоматика этих двух кликов — ``acris_viewer.py``). Сам ``document_id``
   по CRFN строки даёт NYC Open Data (ACRIS Master), без стены для роботов.

Из песочницы разработки ACRIS automated-доступ режет («Further access to
ACRIS is denied… automated scripts/robots»), поэтому живая проверка прямого
POST — только на машине пользователя. Если прямой запрос зарежут и там,
вторая ступень — настоящий браузер (как у Клерка и портала NYC).

Модуль сам по себе в сеть не ходит: ``fetch_results_html`` принимает
``opener``, а разбор работает по готовому HTML.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Optional

import rcclerk

#: Адрес ACRIS Document Search.
BASE_URL = "https://a836-acris.nyc.gov"
BBL_PAGE_PATH = "/DS/DocumentSearch/BBL"
RESULTS_PATH = "/DS/DocumentSearch/BBLResult"
IMAGE_VIEW_PATH = "/DS/DocumentSearch/DocumentImageView"

#: Официальный NYC Open Data: ACRIS Real Property Master. По CRFN из строки
#: результатов даёт ``document_id`` — тот самый, что сайт подставляет в
#: ``DocumentImageView?doc_id=...`` (проверено на образце пользователя:
#: CRFN 2024000265906 -> 2024101000085001). Доступен без стены для роботов.
OPEN_DATA_MASTER_URL = "https://data.cityofnewyork.us/resource/bnx9-e6tj.json"

#: Код района -> подпись в форме ACRIS (она же hid_borough_name).
BOROUGH_NAMES: dict[int, str] = {
    1: "MANHATTAN / NEW YORK",
    2: "BRONX",
    3: "BROOKLYN / KINGS",
    4: "QUEENS",
    5: "STATEN ISLAND / RICHMOND",
}

_BOROUGH_ALIASES: dict[str, int] = {
    "1": 1, "manhattan": 1, "new york": 1, "manhattan / new york": 1,
    "2": 2, "bronx": 2, "the bronx": 2,
    "3": 3, "brooklyn": 3, "kings": 3, "brooklyn / kings": 3, "kings county": 3,
    "4": 4, "queens": 4, "queens county": 4,
    "5": 5, "staten island": 5, "richmond": 5, "staten island / richmond": 5,
    "richmond county": 5,
}

#: Признаки страницы-отказа ACRIS для роботов (Bandwidth Policy).
BLOCKED_MARKS = (
    "further access to acris is denied",
    "bandwidthpolicy",
    "detection of automated scripts/robots",
    "exceeded the bandwidth limits",
)


class AcrisError(RuntimeError):
    """ACRIS не отдал то, что просили."""


class AcrisBlocked(AcrisError):
    """ACRIS показал страницу отказа для автоматических запросов."""


def borough_code(value: object) -> int:
    """Код района ACRIS по номеру или названию; Staten Island — отдельно.

    Для Staten Island (5) бросает ``ValueError``: его документы собирает
    Richmond County Clerk (``rcclerk.py``), а не ACRIS.
    """
    text = re.sub(r"\s+", " ", str(value or "").strip()).casefold()
    code = _BOROUGH_ALIASES.get(text)
    if code is None:
        raise ValueError(f"неизвестный район для ACRIS: {value!r}")
    if code == 5:
        raise ValueError(
            "Staten Island / Richmond County собирается через Richmond County "
            "Clerk (rcclerk), а не через ACRIS"
        )
    return code


def search_page_url(*, base: str = BASE_URL) -> str:
    """Страница формы поиска по BBL (для браузерной ступени)."""
    return f"{base}{BBL_PAGE_PATH}"


def search_form_url(borough: object, block: int | str, lot: int | str, *, base: str = BASE_URL) -> str:
    """
    Глубокая ссылка на поиск по BBL с заполненными районом/блоком/лотом.

    Нужна браузерной ступени: открытая в браузере, она выполняет поиск и
    создаёт серверную сессию с критериями. Без такой сессии читалка
    документа (DocumentImageVtu) открывается, но страницы не отдаёт —
    вместо них «HTTP 405 Method Not Allowed» (живой прогон 2026-11-23).
    """
    code = borough_code(borough)
    block_no = int(block)
    lot_no = int(lot)
    if block_no <= 0 or lot_no <= 0:
        raise ValueError("block and lot must be positive")
    return f"{base}{BBL_PAGE_PATH}?borough={code}&block={block_no}&lot={lot_no}"


def search_post_data(
    borough: object,
    block: int | str,
    lot: int | str,
    *,
    unit: str = "",
    max_rows: int = 99,
    page: int = 1,
    doc_class: str = "All Document Classes",
) -> dict[str, str]:
    """
    Поля прямого POST на ``BBLResult`` — то, что форма собирает в скрытой
    форме ``global``. ``max_rows=99`` — максимум выпадающего списка на
    странице результатов: все строки видны сразу, руками ничего нажимать
    не надо.
    """
    code = borough_code(borough)
    block_no = int(block)
    lot_no = int(lot)
    if block_no <= 0 or lot_no <= 0:
        raise ValueError("block and lot must be positive")
    rows = max(1, min(int(max_rows or 99), 99))
    return {
        "hid_borough": str(code),
        "hid_borough_name": BOROUGH_NAMES[code],
        "hid_block": str(block_no),
        "hid_block_value": str(block_no),
        "hid_lot": str(lot_no),
        "hid_lot_value": str(lot_no),
        "hid_unit": str(unit or ""),
        "hid_doctype_name": str(doc_class or "All Document Classes"),
        "hid_max_rows": str(rows),
        "hid_page": str(max(1, int(page or 1))),
        "hid_SearchType": "BBL",
        "hid_ISIntranet": "N",
    }


def document_image_url(doc_id: str | int, *, base: str = BASE_URL) -> str:
    """Адрес обёртки документа (кнопка IMG): PDF-читалка Acordex VTU."""
    return f"{base}{IMAGE_VIEW_PATH}?doc_id={int(doc_id)}"


def _certifi_context():
    """TLS-контекст на bundled certifi CA — запасной, как в ``app_updater``."""
    try:
        import certifi
        import ssl

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - certifi может не стоять: тогда только системный
        return None


def _is_cert_failure(exc: BaseException) -> bool:
    import ssl
    import urllib.error

    reason = getattr(exc, "reason", exc) if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return isinstance(reason, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(reason)


def _open(request, *, timeout: float, opener=None):
    """
    Открывает запрос; при отказе проверки сертификата повторяет один раз с
    certifi CA. На части Windows-машин системное хранилище не знает издателя
    сертификата data.cityofnewyork.us (живой случай 2026-11-23), хотя тот же
    запрос в браузере проходит.
    """
    import urllib.error
    import urllib.request

    if opener is not None:
        return opener(request, timeout=timeout)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError as exc:
        if not _is_cert_failure(exc):
            raise
        context = _certifi_context()
        if context is None:
            raise
        return urllib.request.urlopen(request, timeout=timeout, context=context)


def _fetch_master_json(params: dict[str, str], *, opener=None, timeout: float = 45.0) -> list:
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"{OPEN_DATA_MASTER_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json",
    })
    try:
        with _open(request, timeout=timeout, opener=opener) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise AcrisError(f"NYC Open Data ответил {exc.code} вместо данных ACRIS Master") from exc
    except Exception as exc:  # noqa: BLE001
        raise AcrisError(f"не удалось спросить NYC Open Data: {exc}") from exc
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise AcrisError("NYC Open Data вернул не JSON") from exc
    if not isinstance(data, list):
        raise AcrisError("NYC Open Data вернул неожиданный ответ")
    return data


def fetch_document_ids(crfns, *, opener=None, timeout: float = 45.0) -> dict[str, str]:
    """CRFN -> document_id по ACRIS Master (NYC Open Data), одним запросом."""
    wanted = [str(c).strip() for c in crfns if str(c).strip()]
    if not wanted:
        return {}
    where = "crfn in(" + ",".join(f"'{c}'" for c in wanted) + ")"
    rows = _fetch_master_json(
        {"$where": where, "$select": "crfn,document_id", "$limit": "5000"},
        opener=opener, timeout=timeout,
    )
    found: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        crfn = str(row.get("crfn") or "").strip()
        doc_id = str(row.get("document_id") or "").strip()
        if crfn and re.fullmatch(r"\d{16}", doc_id):
            found[crfn] = doc_id
    return found


def fetch_document_id(crfn: str, *, opener=None, timeout: float = 45.0) -> str:
    """document_id одного документа по его CRFN; нет такого — ``AcrisError``."""
    found = fetch_document_ids([crfn], opener=opener, timeout=timeout)
    doc_id = found.get(str(crfn).strip())
    if not doc_id:
        raise AcrisError(f"NYC Open Data не знает CRFN {crfn} — document_id не получить")
    return doc_id


def attach_document_ids(rows, *, opener=None, timeout: float = 45.0) -> list:
    """Проставляет ``document_id`` строкам по их CRFN (кого нет в Master — пустой)."""
    ids = fetch_document_ids([r.crfn for r in rows], opener=opener, timeout=timeout)
    for row in rows:
        row.document_id = ids.get(row.crfn, "")
    return rows


def fetch_results_html(
    borough: object,
    block: int | str,
    lot: int | str,
    *,
    base: str = BASE_URL,
    timeout: float = 45.0,
    opener=None,
    max_rows: int = 99,
    page: int = 1,
) -> str:
    """
    Страница результатов поиска по BBL (HTML) прямым POST.

    Если ACRIS показал страницу отказа для роботов — ``AcrisBlocked``:
    тогда список открывают в настоящем браузере, а разбор работает по
    сохранённой странице (ровно как присланные образцы).
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    data = search_post_data(borough, block, lot, max_rows=max_rows, page=page)
    url = f"{base}{RESULTS_PATH}"
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": search_page_url(base=base),
    })
    try:
        with _open(request, timeout=timeout, opener=opener) as response:
            charset = ""
            try:
                charset = str(response.headers.get_content_charset() or "")
            except Exception:  # noqa: BLE001
                charset = ""
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise AcrisBlocked(
            f"ACRIS ответил {exc.code} вместо результатов — откройте в браузере: "
            f"{search_page_url(base=base)}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise AcrisError(f"не удалось получить результаты ACRIS: {exc}") from exc
    html = rcclerk._decode_html(payload, charset)  # noqa: SLF001 - общий декодер проекта
    lowered = html[:6000].lower()
    if any(mark in lowered for mark in BLOCKED_MARKS):
        raise AcrisBlocked(
            "ACRIS показал страницу отказа для автоматических запросов — "
            f"откройте поиск в браузере: {search_page_url(base=base)}"
        )
    return html


# ---------------------------------------------------------------------------
# Разбор страницы результатов
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_RECORDS_RE = re.compile(r"Records\s+(\d+)\s*[-\u2013]\s*(\d+)", re.IGNORECASE)
_CRITERIA_RE = re.compile(
    r"Borough:(?:&nbsp;|\s)*</b>\s*([^<]+?)\s*<br>\s*<b>Block:</b>(?:&nbsp;|\s)*(\d+)"
    r".{0,200}?<b>Lot:(?:&nbsp;|\s)*</b>\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
)


#: Вложенная таблица-виджет сортировки в заголовке «Doc Date» (текст + картинка).
_SORT_TABLE_RE = re.compile(
    r"<table[^>]*>\s*(?:<tbody>\s*)?<tr>\s*<td[^>]*>((?:(?!</td>).)*)</td>\s*<td[^>]*>(?:(?!</td>).)*</td>\s*</tr>\s*(?:</tbody>\s*)?</table>",
    re.IGNORECASE | re.DOTALL,
)


class _TableRows(HTMLParser):
    """Строки всех таблиц страницы в порядке документа.

    Страница ACRIS собрана из вложенных таблиц-раскладок, поэтому у каждой
    таблицы свой контекст строки/ячейки (стек): строки содержательной
    таблицы не сливаются с раскладкой вокруг. Незакрытые ``</td>``/``</tr>``
    закрываем по следующему открывающему тегу, как браузер.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        # Кадр таблицы: [текущая строка, текущая открытая ячейка]
        self._frames: list[list] = [[None, None]]

    @property
    def _frame(self) -> list:
        return self._frames[-1]

    def _close_cell(self) -> None:
        row, cell = self._frame
        if cell is not None and row is not None:
            row.append(" ".join("".join(cell).split()))
        self._frame[1] = None

    def _close_row(self) -> None:
        self._close_cell()
        row = self._frame[0]
        if row:
            self.rows.append(row)
        self._frame[0] = None

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag == "table":
            self._frames.append([None, None])
        elif tag == "tr":
            self._close_row()
            self._frame[0] = []
        elif tag in ("td", "th"):
            self._close_cell()
            if self._frame[0] is not None:
                self._frame[1] = []
        elif tag == "br" and self._frame[1] is not None:
            self._frame[1].append(" ")

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag == "table":
            if len(self._frames) > 1:
                self._frames.pop()
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_row()

    def handle_data(self, data):  # noqa: ANN001
        if self._frame[1] is not None:
            self._frame[1].append(data)


def _extract_rows(html: str) -> list[list[str]]:
    parser = _TableRows()
    parser.feed(html)
    return parser.rows

def _header_field(key: str) -> str:
    """
    Заголовок колонки -> поле строки, терпимо к оформлению.

    Живая страница (прямой POST) не обязана повторять сохранённый образец
    буква в букву: «Recorded/Filed» без пробелов, «Doc Date» с виджетом
    сортировки и т.п. Дословное сравнение в 4.81.14 оставило все даты
    пустыми при живых строках. Сравниваем по сжатому виду и началу слова.
    """
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    if compact == "crfn":
        return "crfn"
    if compact.startswith("reel"):
        return "reel_pg_file"
    if compact == "lot":
        return "lot"
    if compact.startswith("partial"):
        return "partial"
    if compact.startswith("docdate"):
        return "doc_date"
    if compact.startswith("recorded"):
        return "recorded"
    if compact.startswith("documenttype"):
        return "doc_type"
    if compact == "pages":
        return "pages"
    if compact.startswith("party3"):
        return "party3"
    if compact.startswith("party1"):
        return "party1"
    if compact.startswith("party2"):
        return "party2"
    if compact.startswith("docamount") or compact == "amount":
        return "amount"
    return ""


def _cell_text(raw: str) -> str:
    return " ".join(unescape(_TAG_RE.sub(" ", raw)).split())


def _header_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date(text: str) -> Optional[date]:
    raw = " ".join(str(text or "").split())
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:  # месяц/день/год, как пишет сам ACRIS
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:  # ISO: 2026-11-05
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", raw)
    if m:  # точки — день.месяц (так страницу показывает переведённый браузер)
        for day, month in ((m.group(1), m.group(2)), (m.group(2), m.group(1))):
            try:
                return date(int(m.group(3)), int(month), int(day))
            except ValueError:
                continue
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", raw)
    if m and m.group(1).lower()[:4] in _MONTH_NAMES or (m and m.group(1).lower()[:3] in _MONTH_NAMES):
        name = m.group(1).lower()
        month = _MONTH_NAMES.get(name[:4]) or _MONTH_NAMES.get(name[:3])
        try:
            return date(int(m.group(3)), int(month), int(m.group(2)))
        except (TypeError, ValueError):
            pass
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})", raw)
    if m:
        name = m.group(2).lower()
        month = _MONTH_NAMES.get(name[:4]) or _MONTH_NAMES.get(name[:3])
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(1)))
            except ValueError:
                pass
    return None


def _parse_datetime(text: str) -> Optional[datetime]:
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]M)?",
                  text or "", re.IGNORECASE)
    if not m:
        return None
    hour = int(m.group(4))
    ampm = (m.group(7) or "").upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)),
                        hour, int(m.group(5)), int(m.group(6) or 0))
    except ValueError:
        pass
    # Дата другим форматом (ISO, точки, месяц словом) + время отдельно.
    day = _parse_date(text)
    if day is None:
        return None
    t = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]M)?", text or "", re.IGNORECASE)
    if not t:
        return datetime(day.year, day.month, day.day)
    hour = int(t.group(1))
    ampm = (t.group(4) or "").upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        return datetime(day.year, day.month, day.day, hour, int(t.group(2)), int(t.group(3) or 0))
    except ValueError:
        return None


@dataclass
class AcrisRow:
    """Одна строка результатов ACRIS."""

    crfn: str = ""
    reel_pg_file: str = ""
    lot: str = ""
    partial: str = ""
    doc_date: Optional[date] = None
    recorded: Optional[datetime] = None
    doc_type: str = ""
    pages: Optional[int] = None
    party1: str = ""
    party2: str = ""
    party3: str = ""
    amount: str = ""
    document_id: str = ""
    raw: str = ""

    @property
    def image_url(self) -> str:
        """Адрес обёртки документа (IMG); пусто, пока document_id не получен."""
        return document_image_url(self.document_id) if self.document_id else ""

    @property
    def kind(self) -> str:
        """DEED / MORTGAGE / SATISFACTION / OTHER — как у Клерка Ричмонда."""
        return rcclerk.classify_document(self.doc_type)


@dataclass
class AcrisPage:
    """Разобранная страница результатов: строки + где мы в выдаче."""

    rows: list[AcrisRow] = field(default_factory=list)
    records_first: Optional[int] = None
    records_last: Optional[int] = None
    has_next: bool = False
    borough_name: str = ""
    block: Optional[int] = None
    lot: Optional[int] = None


def parse_result_page(html: str) -> AcrisPage:
    """Разбирает страницу результатов BBL по настоящей разметке ACRIS."""
    page = AcrisPage()
    m = _RECORDS_RE.search(html)
    if m:
        page.records_first = int(m.group(1))
        page.records_last = int(m.group(2))
    crit = _CRITERIA_RE.search(html)
    if crit:
        page.borough_name = _cell_text(crit.group(1))
        page.block = int(crit.group(2))
        page.lot = int(crit.group(3))
    # «next» — ссылка, только когда есть следующая страница; в образце с
    # 6 записями это просто серый текст без ссылки.
    tail = html[m.end():m.end() + 1200] if m else ""
    page.has_next = bool(re.search(r"<a[^>]*>\s*next\s*</a>", tail, re.IGNORECASE))

    rows = _extract_rows(_SORT_TABLE_RE.sub(lambda m: m.group(1), html))
    header_at: Optional[int] = None
    columns: dict[int, str] = {}
    for idx, cells in enumerate(rows):
        keys = [_header_key(c) for c in cells]
        if "crfn" in keys and any(k.startswith("document") for k in keys):
            header_at = idx
            columns = {i: field for i, k in enumerate(keys) if (field := _header_field(k))}
            break
    if header_at is None:
        return page
    for cells in rows[header_at + 1:]:
        if len(cells) < 6:
            continue
        values: dict[str, str] = {}
        for i, name in columns.items():
            if i < len(cells):
                values[name] = cells[i]
        crfn = values.get("crfn", "")
        if not re.fullmatch(r"\d{6,}", crfn or ""):
            continue
        pages_raw = values.get("pages", "")
        page.rows.append(AcrisRow(
            crfn=crfn,
            reel_pg_file=values.get("reel_pg_file", ""),
            lot=values.get("lot", ""),
            partial=values.get("partial", ""),
            doc_date=_parse_date(values.get("doc_date", "")),
            recorded=_parse_datetime(values.get("recorded", "")),
            doc_type=values.get("doc_type", ""),
            pages=int(pages_raw) if pages_raw.isdigit() else None,
            party1=values.get("party1", ""),
            party2=values.get("party2", ""),
            party3=values.get("party3", ""),
            amount=values.get("amount", ""),
            raw=" | ".join(cells)[:300],
        ))
    return page


def parse_result_rows(html: str) -> list[AcrisRow]:
    """Только строки документов со страницы результатов."""
    return parse_result_page(html).rows
