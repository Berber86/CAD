"""
Шаг 0: собрать описания участка — одна кнопка от block/lot до папки файлов.

Модуль связывает готовые куски в один проход. Источник — по району, и
правила одного сайта на другой не переносятся:

* Staten Island / Richmond County → Richmond County Clerk (``rcclerk.py``,
  первый настроенный вариант): ``rcclerk.results_url`` → страница списка;
* Manhattan, Bronx, Brooklyn, Queens → ACRIS (``acris.py`` + читалка
  ``acris_viewer.py``): прямой запрос списка сразу по 99 строк, документ —
  дискета Save → OK в настоящем браузере, PDF — из «Загрузок».

Дальше русло одно:

1. страница списка документов участка;
2. ``rcclerk.parse_document_rows`` + ``rcclerk.choose_documents`` → что качать
   (Deed новее 2015 года, иначе свежая Mortgage с 2015 года и древний Deed как старый;
   нашему лоту — свежий и старый, соседу — свежий);
3. ``rcclerk.download_with_fallback`` → лестница «прямо → браузер → человек»;
4. ``deed_pages`` + ``deed_store`` → страницы описания, обрезанная копия, имя,
   паспорт, ``_manifest.json``.

Сеть, OCR и браузер здесь не вызываются напрямую: их передают параметрами
(``fetch_list``, ``downloader``, ``page_finder``). Поэтому весь проход, включая
аварии, проверяется тестами без интернета и без GUI.

Соседи на этом шаге вводятся **руками** и помечаются ролью «сосед»: номер лота
в поле — и файл получит имя ``LOT 30 NEW.pdf``. Автоматическое определение
соседей по геометрии города — отдельная тема (решение заказчика 15.11.2026).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import acris
import acris_viewer
import deed_pages
import deed_store
import ocr_pages
import rcclerk

#: Папка описаний внутри папки проекта (как в схеме шага).
DESCR_DIR_NAME = "01_описания"

#: Сколько ждать появления PDF в «Загрузках» (Клерк и читалка ACRIS одинаково).
#: 45 с хватает на проверку Cloudflare и сохранение; «долгая капча» на странице
#: «Данные» возвращает прежние 300 с для обоих источников.
PDF_WAIT_SEC = 45.0
PDF_WAIT_LONG_SEC = 300.0

Progress = Callable[[str], None]
StopCheck = Callable[[], bool]

#: ``downloader(строка списка, куда скачать)`` → путь (или ``(путь, ступень)``).
Downloader = Callable[[rcclerk.DeedRow, Path], object]
#: ``fetch_list(block, lot)`` → HTML страницы списка.
ListFetcher = Callable[[int, int], str]
#: ``page_finder(pdf)`` → ``deed_pages.DescriptionPages``.
PageFinder = Callable[[Path], deed_pages.DescriptionPages]
#: ``acris_fetch_page(код района, block, lot, страница)`` → HTML результатов ACRIS.
AcrisPageFetcher = Callable[[int, int, int, int], str]
#: ``acris_id_fetcher(crfn)`` → document_id документа ACRIS.
AcrisIdFetcher = Callable[[str], str]


class IntakeCancelled(RuntimeError):
    """Пользователь нажал «Стоп» — собранное остаётся, проход прекращается."""


def parse_lot_list(text: str | Iterable[int]) -> list[int]:
    """
    Номера лотов из поля «соседи»: ``"30, 28 35"`` → ``[28, 30, 35]``.

    Мусор — ошибка, а не тихий пропуск: молча потерять соседа дороже, чем
    показать человеку, что он вписал.
    """
    if isinstance(text, str):
        tokens = [chunk for chunk in text.replace(";", ",").replace(" ", ",").split(",") if chunk.strip()]
    else:
        tokens = [str(item) for item in text]
    lots: set[int] = set()
    for token in tokens:
        try:
            value = int(str(token).strip())
        except (TypeError, ValueError):
            raise ValueError(f"«{token}» — не номер лота") from None
        if value <= 0:
            raise ValueError(f"номер лота должен быть положительным: {value}")
        lots.add(value)
    return sorted(lots)


@dataclass
class IntakeOutcome:
    """Что получилось из прохода Шага 0."""

    folder: Path
    records: list[deed_store.StoredDeed] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # чего не хватает и почему
    notes: list[str] = field(default_factory=list)     # что произошло по ходу
    page_search_skipped: bool = False

    @property
    def ok(self) -> bool:
        """Наш участок собран хотя бы одним документом."""
        return any(record.role == "our" for record in self.records)

    @property
    def our_records(self) -> list[deed_store.StoredDeed]:
        return [record for record in self.records if record.role == "our"]

    def report(self, *, include_notes: bool = True) -> str:
        """Короткий отчёт для экрана Шага 0 (и для журнала).

        ``include_notes=False`` удобно для GUI, где события уже выведены отдельными
        строками с точным временем и не должны дублироваться без временных меток.
        """
        lines: list[str] = [f"Папка описаний: {self.folder}"]
        if self.records:
            lines.append("")
            lines.append("Собрано:")
            lines.extend(f"  • {record.file}"
                         + (f" — {record.doc_type} от {record.doc_date}" if record.doc_type else "")
                         + (f", описание стр. {record.pages}" if record.pages else
                            (", поиск страниц пропущен" if self.page_search_skipped
                             else ", страницы описания не найдены"))
                         for record in self.records)
        else:
            lines.append("")
            lines.append("Ни одного документа собрать не удалось.")
        if self.missing:
            lines.append("")
            lines.append("Чего не хватает:")
            lines.extend(f"  • {item}" for item in self.missing)
        if include_notes and self.notes:
            lines.append("")
            lines.append("Как шло:")
            lines.extend(f"  • {item}" for item in self.notes)
        return "\n".join(lines)


def _default_fetch_list(block: int, lot: int) -> str:
    return rcclerk.fetch_list_html(block, lot)


def _default_downloader(row: rcclerk.DeedRow, destination: Path) -> tuple[Path, str]:
    return rcclerk.download_with_fallback(row.endorsement_id, destination)


def make_downloader(
    *,
    auto_save=None,
    downloads_dir: str | Path | None = None,
    base_url: str = rcclerk.BASE_URL,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
    watch_timeout: float | None = None,
    fail_dir: str | Path | None = None,
):
    """
    Загрузчик по лестнице; при поддержке Windows безопасно нажимает Ctrl+S в
    браузере только после того, как просмотрщик PDF уже загружен.

    ``fail_dir`` — папка задания: аварийные снимки CLERK_FAIL и снимки
    «доктора» браузера должны лежать рядом с заданием, а не во временных
    файлах, иначе по живому прогону нечего разбирать.
    """

    def download(row: rcclerk.DeedRow, destination: Path) -> tuple[Path, str]:
        options: dict = {"base": base_url}
        if downloads_dir is not None:
            options["downloads_dir"] = downloads_dir
        if progress is not None:
            options["on_step"] = progress  # шаги лестницы видны в журнале Шага 0
        if should_stop is not None:
            options["should_stop"] = should_stop  # «Стоп» прерывает ожидание сразу
        if auto_save is not None:
            options["auto_save"] = auto_save
        if watch_timeout is not None:
            options["watch_timeout"] = float(watch_timeout)  # «долгая капча»: 300 вместо 45
        shot_dir = Path(fail_dir) if fail_dir is not None else Path(destination).parent
        options["fail_dir"] = shot_dir

        def doctor(endorsement_id: int | str) -> None:
            rcclerk.browser_doctor(endorsement_id, folder=shot_dir, emit=progress)

        options["prompt_operator"] = doctor
        return rcclerk.download_with_fallback(row.endorsement_id, destination, **options)

    return download


def make_acris_downloader(
    *,
    downloads_dir: str | Path | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
    viewer_download=None,
    warmup_url: str | None = None,
    crfn_opener=None,
    download_wait_sec: float | None = None,
):
    """
    Загрузчик ACRIS: читалка в настоящем браузере (дискета Save → OK),
    PDF забирается из «Загрузок». Строка здесь — адаптированная
    ``rcclerk.DeedRow``, у которой ``endorsement_id`` — это ``document_id``.
    ``warmup_url`` — глубокая ссылка поиска по этому лоту: если читалка по
    прямой ссылке покажет HTTP 405 (нет серверной сессии поиска), загрузчик
    сначала откроет поиск в браузере и повторит.

    Если ``endorsement_id`` — не document_id, а CRFN (NYC Open Data свежего
    документа ещё не знает), вызывается ``crfn_opener(crfn, destination,
    options)`` — браузерный резерв (``acris_browser``): клик IMG в
    результатах поиска и та же читалка.
    """
    download_it = viewer_download or acris_viewer.download_pdf_via_viewer

    def download(row: rcclerk.DeedRow, destination: Path) -> tuple[Path, str]:
        options: dict = {
            "downloads_dir": downloads_dir,
            "progress": progress,
            "should_stop": should_stop,
        }
        if warmup_url:
            options["warmup_url"] = warmup_url
        if download_wait_sec is not None:
            options["download_wait_sec"] = float(download_wait_sec)  # «долгая капча»: 300 вместо 45
        endorsement = str(row.endorsement_id or "").strip()
        if endorsement.isdigit() and len(endorsement) == 16:
            return download_it(
                acris.document_image_url(endorsement), destination, **options,
            )
        if crfn_opener is None:
            raise rcclerk.ManualStepNeeded(
                f"NYC Open Data не знает CRFN {endorsement or '?'} — document_id не получить; "
                "откройте документ в ACRIS вручную (кнопка IMG) и скачайте PDF.",
            )
        return crfn_opener(endorsement, destination, options)

    return download


def _default_page_finder(
    pdf: Path, *, dpi: int = ocr_pages.DEFAULT_DPI, progress: Progress | None = None,
) -> deed_pages.DescriptionPages:
    return deed_pages.find_description_pages(pdf, dpi=dpi, progress=progress)


def _row_label(row: rcclerk.DeedRow) -> str:
    """«DEED (Поступок) №2640217 от 2022-09-03» — вид, слово сайта, номер и дата."""
    kind = row.kind if row.kind != rcclerk.KIND_OTHER else "документ"
    site_word = f" ({row.doc_type})" if row.doc_type and row.doc_type.upper() != kind else ""
    when = row.doc_date.isoformat() if row.doc_date else "без даты"
    return f"{kind}{site_word} №{row.endorsement_id} от {when}"


def is_richmond_borough(value: object) -> bool:
    """Only explicit Staten Island/Richmond County identifies this clerk site."""
    import re
    text = re.sub(r"\s+", " ", str(value or "").strip()).casefold()
    return text in {"staten island", "staten island, ny", "staten island, new york",
                    "richmond county", "richmond county, ny", "richmond county, new york"}


def acris_borough_code(value: object) -> int | None:
    """Код района ACRIS (1–4) или ``None``, если район не для ACRIS."""
    try:
        return acris.borough_code(value)
    except (TypeError, ValueError):
        return None


def step0_source_name(borough: object) -> str:
    """Какой сайт собирает этот район: ``"Richmond County Clerk"``, ``"ACRIS"`` или ``""``."""
    if is_richmond_borough(borough):
        return "Richmond County Clerk"
    if acris_borough_code(borough) is not None:
        return "ACRIS"
    return ""


def collect_deeds(
    *,
    block: int | str,
    lot: int | str,
    borough: str = "",
    identity_by_lot: dict[str, dict] | None = None,
    neighbours: Sequence[int] | str = (),
    folder: str | Path | None = None,
    base_url: str = rcclerk.BASE_URL,
    with_old_for_neighbour: bool = False,
    fetch_list: ListFetcher | None = None,
    downloader: Downloader | None = None,
    acris_fetch_page: AcrisPageFetcher | None = None,
    acris_downloader: Downloader | None = None,
    acris_id_fetcher: AcrisIdFetcher | None = None,
    auto_save=None,
    ocr_dpi: int = ocr_pages.DEFAULT_DPI,
    downloads_dir: str | Path | None = None,
    page_finder: PageFinder | None = None,
    search_pages: bool = True,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
    on_record: Callable[[deed_store.StoredDeed], None] | None = None,
    pdf_wait_sec: float | None = None,
    include_our: bool = True,
) -> IntakeOutcome:
    """
    Собирает описания нашего участка и соседей в ``folder`` (по умолчанию
    ``01_описания`` в текущей папке).

    ``include_our=False`` — собрать только соседей: так автопоток докачивает
    соседей отдельным проходом после того, как наш участок уже скачан ранним
    проходом (номера соседей нейронки назвали позже).

    ``on_record`` вызывается сразу после сохранения каждого документа:
    автопоток в этот момент запускает поиск страниц и разбор этого PDF, не
    дожидаясь скачивания остальных файлов.

    Авария одного документа не отменяет остальные: проход идёт дальше, а причина
    попадает в ``missing``/``notes`` — так человек видит и то, что собралось, и
    что именно надо доделать руками.

    ``auto_save`` — безопасный хук, который ждёт загруженный PDF в браузере и
    отправляет Ctrl+S однократно; ``downloads_dir`` — папка «Загрузки».

    Источник выбирается по району: Staten Island / Richmond County — Клерк
    округа Richmond (``rcclerk``), Manhattan/Bronx/Brooklyn/Queens — ACRIS
    (``acris`` + ``acris_viewer``). Другой район — ``ValueError``: данные не
    должны попадать на чужой сайт.
    """
    if not isinstance(search_pages, bool):
        raise ValueError("search_pages must be boolean")
    block_no = int(block)
    lot_no = int(lot)
    if block_no <= 0 or lot_no <= 0:
        raise ValueError("block and lot must be positive")
    if is_richmond_borough(borough):
        return _collect_deeds_richmond(
            block_no=block_no, lot_no=lot_no, borough=borough,
            identity_by_lot=identity_by_lot, neighbours=neighbours, folder=folder,
            base_url=base_url, with_old_for_neighbour=with_old_for_neighbour,
            fetch_list=fetch_list, downloader=downloader, auto_save=auto_save,
            ocr_dpi=ocr_dpi, downloads_dir=downloads_dir, page_finder=page_finder,
            search_pages=search_pages, progress=progress, should_stop=should_stop,
            on_record=on_record, pdf_wait_sec=pdf_wait_sec, include_our=include_our,
        )
    code = acris_borough_code(borough)
    if code is None:
        raise ValueError(
            "укажите район: Staten Island / Richmond County (Richmond County Clerk) "
            "или Manhattan, Bronx, Brooklyn, Queens (ACRIS)"
        )
    return _collect_deeds_acris(
        code=code, block_no=block_no, lot_no=lot_no, borough=borough,
        identity_by_lot=identity_by_lot, neighbours=neighbours, folder=folder,
        with_old_for_neighbour=with_old_for_neighbour,
        acris_fetch_page=acris_fetch_page, acris_downloader=acris_downloader,
        acris_id_fetcher=acris_id_fetcher, downloads_dir=downloads_dir,
        ocr_dpi=ocr_dpi, page_finder=page_finder,
        search_pages=search_pages, progress=progress, should_stop=should_stop,
        on_record=on_record, pdf_wait_sec=pdf_wait_sec, include_our=include_our,
    )


def _collect_deeds_richmond(
    *,
    block_no: int,
    lot_no: int,
    borough: str = "",
    identity_by_lot: dict[str, dict] | None = None,
    neighbours: Sequence[int] | str = (),
    folder: str | Path | None = None,
    base_url: str = rcclerk.BASE_URL,
    with_old_for_neighbour: bool = False,
    fetch_list: ListFetcher | None = None,
    downloader: Downloader | None = None,
    auto_save=None,
    ocr_dpi: int = ocr_pages.DEFAULT_DPI,
    downloads_dir: str | Path | None = None,
    page_finder: PageFinder | None = None,
    search_pages: bool = True,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
    on_record: Callable[[deed_store.StoredDeed], None] | None = None,
    pdf_wait_sec: float | None = None,
    include_our: bool = True,
) -> IntakeOutcome:
    """Проход по Richmond County Clerk (Staten Island) — первый настроенный вариант."""
    neighbour_lots = parse_lot_list(neighbours)
    target_dir = Path(folder) if folder is not None else Path.cwd() / DESCR_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    fetch = fetch_list or _default_fetch_list
    download = downloader or make_downloader(
        auto_save=auto_save, downloads_dir=downloads_dir, base_url=base_url,
        progress=progress, should_stop=should_stop, watch_timeout=pdf_wait_sec,
        fail_dir=target_dir,
    )
    find_pages = page_finder or (
        lambda pdf: _default_page_finder(pdf, dpi=ocr_dpi, progress=progress)
    )
    outcome = IntakeOutcome(folder=target_dir, page_search_skipped=not search_pages)

    def say(message: str) -> None:
        outcome.notes.append(message)
        if progress is not None:
            progress(message)

    def check_stop() -> None:
        if should_stop is not None and should_stop():
            raise IntakeCancelled("сбор описаний остановлен")

    plans: list[tuple[str, int]] = [("our", lot_no)] if include_our else []
    plans.extend(("neighbour", value) for value in neighbour_lots)
    if not plans:
        outcome.missing.append("не указан номер лота нашего участка")

    for role, lot_value in plans:
        check_stop()
        who = "наш участок" if role == "our" else "сосед"
        say(f"{who}: блок {block_no}, лот {lot_value} — читаю список документов…")
        try:
            html = fetch(block_no, lot_value)
        except rcclerk.CloudflareBlocked as exc:
            outcome.missing.append(f"{who} (лот {lot_value}): список закрыт проверкой Cloudflare — {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - сеть бывает разной
            outcome.missing.append(f"{who} (лот {lot_value}): список не открылся — {exc}")
            continue
        rows = rcclerk.parse_document_rows(html)
        if not rows:
            outcome.missing.append(
                f"{who} (лот {lot_value}): в списке нет строк документов — "
                f"страница пустая или устроена иначе: {rcclerk.results_url(block_no, lot_value, base=base_url)}"
            )
            continue
        listed = "; ".join(
            f"{row.doc_type or '?'} {row.doc_date.strftime('%d.%m.%Y') if row.doc_date else 'без даты'}"
            for row in rows
        )
        say(f"{who} (лот {lot_value}): строк в списке — {len(rows)}: {listed}")
        chosen = rcclerk.choose_documents(
            rows, role=role, with_old_for_neighbour=with_old_for_neighbour
        )
        if not chosen:
            outcome.missing.append(
                f"{who} (лот {lot_value}): ни Deed, ни Mortgage в списке нет "
                f"(строки: {', '.join(row.doc_type or '?' for row in rows[:6])})"
            )
            continue
        for row, tag in chosen:
            check_stop()
            outcome_item = _collect_one(
                block_no=block_no, lot_value=lot_value, role=role, row=row, tag=tag,
                borough=borough, identity=(identity_by_lot or {}).get(str(lot_value)),
                target_dir=target_dir, base_url=base_url,
                download=download, find_pages=find_pages if search_pages else None, ocr_dpi=ocr_dpi,
                say=say, missing=outcome.missing,
            )
            if outcome_item is not None:
                outcome.records.append(outcome_item)
                if on_record is not None:
                    try:
                        on_record(outcome_item)
                    except Exception:  # noqa: BLE001 - конвейер не должен ронять сбор
                        say(f"  {outcome_item.file}: сразу запустить обработку не удалось — продолжу сбор")

    if outcome.records:
        deed_store.write_manifest(
            target_dir,
            block=block_no, lot=lot_no,
            missing=[{"reason": item} for item in outcome.missing],
        )
        say(f"папка описаний собрана: {len(outcome.records)} документ(ов)")
    if include_our and not outcome.ok \
            and not any("наш участок" in item for item in outcome.missing):
        outcome.missing.append("наш участок не собран")
    return outcome


#: Сколько страниц результатов ACRIS подряд готовы листать (по 99 строк).
_MAX_ACRIS_PAGES = 25


def _acris_as_clerk_row(row: acris.AcrisRow, index: int) -> rcclerk.DeedRow:
    """
    Строка ACRIS в виде строки Клерка — чтобы выбор Deed/Mortgage шёл тем же
    правилом (``rcclerk.choose_documents``). ``endorsement_id`` — document_id
    (пока не получен — CRFN); дата свежести — Doc Date, иначе Recorded/Filed.
    """
    doc_date = row.doc_date or (row.recorded.date() if row.recorded else None)
    return rcclerk.DeedRow(
        endorsement_id=row.document_id or row.crfn,
        doc_type=row.doc_type,
        doc_date=doc_date,
        index=index,
    )


def _fetch_acris_rows(
    code: int, block_no: int, lot_no: int, fetch_page: AcrisPageFetcher,
    *, on_first_page=None,
) -> list[acris.AcrisRow]:
    """Все строки лота: страницы по 99, пока ACRIS говорит, что есть дальше."""
    rows: list[acris.AcrisRow] = []
    seen: set[str] = set()
    for page_no in range(1, _MAX_ACRIS_PAGES + 1):
        html = fetch_page(code, block_no, lot_no, page_no)
        if page_no == 1 and on_first_page is not None:
            on_first_page(html)
        page = acris.parse_result_page(html)
        for row in page.rows:
            if row.crfn in seen:
                continue
            seen.add(row.crfn)
            rows.append(row)
        if not page.has_next or not page.rows:
            break
    return rows


def _page_text_snippet(html: str, limit: int = 180) -> str:
    """Первые слова текста страницы без тегов — образец для журнала при пустом разборе."""
    text = acris._TAG_RE.sub(" ", str(html or ""))  # noqa: SLF001 - общий разбор проекта
    return " ".join(text.split())[:limit]


def _collect_deeds_acris(
    *,
    code: int,
    block_no: int,
    lot_no: int,
    borough: str = "",
    identity_by_lot: dict[str, dict] | None = None,
    neighbours: Sequence[int] | str = (),
    folder: str | Path | None = None,
    with_old_for_neighbour: bool = False,
    acris_fetch_page: AcrisPageFetcher | None = None,
    acris_downloader: Downloader | None = None,
    acris_id_fetcher: AcrisIdFetcher | None = None,
    downloads_dir: str | Path | None = None,
    ocr_dpi: int = ocr_pages.DEFAULT_DPI,
    page_finder: PageFinder | None = None,
    search_pages: bool = True,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
    on_record: Callable[[deed_store.StoredDeed], None] | None = None,
    pdf_wait_sec: float | None = None,
    include_our: bool = True,
) -> IntakeOutcome:
    """
    Проход по ACRIS (Manhattan, Bronx, Brooklyn, Queens).

    Список — прямым запросом сразу по 99 строк; выбор Deed/Mortgage — тем же
    правилом, что у Клерка; ``document_id`` — по CRFN через NYC Open Data;
    документ — читалкой в браузере (дискета Save → OK, PDF из «Загрузок»).
    Дальше русло общее с Ричмондом: страницы описания, имя, паспорт, manifest.
    """
    neighbour_lots = parse_lot_list(neighbours)
    target_dir = Path(folder) if folder is not None else Path.cwd() / DESCR_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    fetch_page = acris_fetch_page or (
        lambda c, b, l, pg: acris.fetch_results_html(c, b, l, page=pg)
    )
    resolve_id = acris_id_fetcher or acris.fetch_document_id
    find_pages = page_finder or (
        lambda pdf: _default_page_finder(pdf, dpi=ocr_dpi, progress=progress)
    )
    outcome = IntakeOutcome(folder=target_dir, page_search_skipped=not search_pages)

    def say(message: str) -> None:
        outcome.notes.append(message)
        if progress is not None:
            progress(message)

    def check_stop() -> None:
        if should_stop is not None and should_stop():
            raise IntakeCancelled("сбор описаний остановлен")

    plans: list[tuple[str, int]] = [("our", lot_no)] if include_our else []
    plans.extend(("neighbour", value) for value in neighbour_lots)

    for role, lot_value in plans:
        check_stop()
        who = "наш участок" if role == "our" else "сосед"
        say(f"{who}: блок {block_no}, лот {lot_value} — читаю список документов в ACRIS…")
        first_page_html: list[str] = []
        try:
            rows = _fetch_acris_rows(code, block_no, lot_value, fetch_page,
                                     on_first_page=first_page_html.append)
        except acris.AcrisBlocked as exc:
            outcome.missing.append(f"{who} (лот {lot_value}): список закрыт страницей отказа ACRIS — {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - сеть бывает разной
            outcome.missing.append(f"{who} (лот {lot_value}): список не открылся — {exc}")
            continue
        if not rows:
            snippet = _page_text_snippet(first_page_html[0]) if first_page_html else ""
            outcome.missing.append(
                f"{who} (лот {lot_value}): в ACRIS нет строк документов — "
                f"проверьте block/lot: {acris.search_page_url()}"
                + (f"; образец страницы: {snippet}" if snippet else "")
            )
            continue
        listed = "; ".join(
            f"{row.doc_type or '?'} {row.doc_date.strftime('%d.%m.%Y') if row.doc_date else 'без даты'}"
            for row in rows
        )
        say(f"{who} (лот {lot_value}): строк в списке — {len(rows)}: {listed}")
        if all(row.doc_date is None and row.recorded is None for row in rows):
            say(f"{who} (лот {lot_value}): ACRIS не отдал даты в привычном виде — "
                f"образец строки: {rows[0].raw[:180]}")
        adapted_rows = [_acris_as_clerk_row(row, idx) for idx, row in enumerate(rows)]
        by_adapted = {id(clerk_row): row for clerk_row, row in zip(adapted_rows, rows)}
        chosen = rcclerk.choose_documents(
            adapted_rows, role=role, with_old_for_neighbour=with_old_for_neighbour,
        )
        if not chosen:
            outcome.missing.append(
                f"{who} (лот {lot_value}): ни Deed, ни Mortgage в ACRIS нет "
                f"(строки: {', '.join(row.doc_type or '?' for row in rows[:6])})"
            )
            continue
        # Загрузчик — на каждый лот свой: ссылка прогрева сессии поиска
        # в браузере относится именно к этому лоту (см. acris_viewer).
        # crfn_opener — браузерный резерв для свежих документов: клик IMG
        # в результатах вместо document_id из отстающего NYC Open Data.
        def crfn_opener(crfn: str, destination: Path, options: dict):
            import acris_browser

            say(f"  CRFN {crfn}: открываю документ в браузере кликом IMG (резерв для свежих)…")
            acris_browser.open_document_image(
                crfn, code, block_no, lot_value,
                progress=progress, should_stop=should_stop, fail_dir=target_dir,
            )
            return acris_viewer.download_pdf_via_viewer(
                acris.search_page_url(), destination, already_open=True, **options,
            )

        lot_download = acris_downloader or make_acris_downloader(
            downloads_dir=downloads_dir, progress=progress, should_stop=should_stop,
            warmup_url=acris.search_form_url(code, block_no, lot_value),
            crfn_opener=crfn_opener, download_wait_sec=pdf_wait_sec,
        )
        for clerk_row, tag in chosen:
            check_stop()
            row = by_adapted[id(clerk_row)]
            if not row.document_id:
                try:
                    row.document_id = resolve_id(row.crfn)
                except Exception as exc:  # noqa: BLE001
                    # Не пропускаем документ: загрузчик пойдёт браузерным
                    # резервом по CRFN (endorsement_id адаптированной
                    # строки в этом случае — сам CRFN).
                    say(f"лот {lot_value}: document_id по CRFN {row.crfn} не получен ({exc}) — "
                        "попробую открыть документ в браузере кликом IMG")
            outcome_item = _collect_one(
                block_no=block_no, lot_value=lot_value, role=role,
                row=_acris_as_clerk_row(row, 0), tag=tag,
                borough=borough, identity=(identity_by_lot or {}).get(str(lot_value)),
                target_dir=target_dir, base_url=acris.BASE_URL,
                download=lot_download, find_pages=find_pages if search_pages else None,
                ocr_dpi=ocr_dpi, say=say, missing=outcome.missing,
                source_url=row.image_url,
                extra_notes=[f"ACRIS: CRFN {row.crfn}"],
            )
            if outcome_item is not None:
                outcome.records.append(outcome_item)
                if on_record is not None:
                    try:
                        on_record(outcome_item)
                    except Exception:  # noqa: BLE001 - конвейер не должен ронять сбор
                        say(f"  {outcome_item.file}: сразу запустить обработку не удалось — продолжу сбор")

    if outcome.records:
        deed_store.write_manifest(
            target_dir,
            block=block_no, lot=lot_no,
            missing=[{"reason": item} for item in outcome.missing],
        )
        say(f"папка описаний собрана: {len(outcome.records)} документ(ов)")
    if include_our and not outcome.ok \
            and not any("наш участок" in item for item in outcome.missing):
        outcome.missing.append("наш участок не собран")
    return outcome


def merge_outcomes(first: IntakeOutcome, second: IntakeOutcome) -> IntakeOutcome:
    """Слияние двух проходов сбора одной папки (наш участок + соседи)."""
    return IntakeOutcome(
        folder=first.folder,
        records=[*first.records, *second.records],
        missing=[*first.missing, *second.missing],
        notes=[*first.notes, *second.notes],
        page_search_skipped=bool(first.page_search_skipped and second.page_search_skipped),
    )


def _collect_one(
    *,
    block_no: int,
    lot_value: int,
    role: str,
    borough: str = "",
    identity: dict | None = None,
    row: rcclerk.DeedRow,
    tag: str,
    target_dir: Path,
    base_url: str,
    download: Downloader,
    find_pages: PageFinder | None,
    ocr_dpi: int,
    say: Progress,
    missing: list[str],
    source_url: str = "",
    extra_notes: Iterable[str] = (),
) -> deed_store.StoredDeed | None:
    """Один документ: скачать → найти описание → обрезать → положить с паспортом."""
    name = Path(rcclerk.document_file_name(role, lot_value, tag)).stem
    say(f"  {name}: качаю {_row_label(row)}")
    with tempfile.TemporaryDirectory(prefix="deed_intake_") as tmp:
        raw = Path(tmp) / f"{name}.pdf"
        try:
            result = download(row, raw)
        except rcclerk.DownloadStopped as exc:
            raise IntakeCancelled(str(exc) or "сбор описаний остановлен") from exc
        except (rcclerk.CloudflareBlocked, rcclerk.ManualStepNeeded) as exc:
            missing.append(f"{name}: нужен ручной шаг — {exc}")
            return None
        except rcclerk.RcclerkError as exc:
            missing.append(f"{name}: не скачался — {exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{name}: не скачался — {exc}")
            return None
        saved, rung = (result if isinstance(result, tuple) else (result, "direct"))
        saved = Path(saved)
        if not saved.is_file():
            missing.append(f"{name}: загрузчик не создал файл {saved}")
            return None
        if rung != "direct":
            say(f"  {name}: скачано ступенью «{rung}»")
        pages = ""
        trimmed_path: Path | None = None
        found = deed_pages.DescriptionPages()
        if find_pages is None:
            say(f"  {name}: полный PDF сохранён; страницы найдёт следующий этап")
        else:
            say(f"  {name}: ищу начало и конец описания (локальный OCR, {int(ocr_dpi)} dpi)…")
            try:
                found = find_pages(saved)
            except Exception as exc:  # noqa: BLE001
                say(f"  {name}: страницы описания не искались — {exc}")
            if found.pages:
                pages = found.pages_spec
                trimmed_path = Path(tmp) / f"{name}_p{pages}.pdf"
                try:
                    deed_pages.trim_pdf(saved, found.pages, trimmed_path)
                except Exception as exc:  # noqa: BLE001
                    say(f"  {name}: не удалось сделать обрезанную копию — {exc}")
                    trimmed_path = None
                else:
                    say(f"  {name}: описание на стр. {pages} (копия {trimmed_path.name})")
            else:
                say(f"  {name}: страницы описания не найдены — файл кладу целиком")
        notes = [f"ступень загрузки: {rung}"] if rung != "direct" else []
        notes.extend(extra_notes)
        if row.clerk_number:
            notes.append(f"клерк: {row.clerk_number}")
        if row.doc_type and row.doc_type.upper() != row.kind:
            notes.append(f"тип на сайте: {row.doc_type}")
        record = deed_store.store_pdf(
            target_dir, saved,
            role=role, lot=lot_value, tag=tag, block=block_no, borough=borough, identity=identity,
            doc_type=row.kind, doc_date=row.doc_date,
            endorsement_id=row.endorsement_id,
            source_url=source_url or rcclerk.document_pdf_url(row.endorsement_id, base=base_url),
            pages=pages, trimmed_source=trimmed_path, text=found.text, notes=notes,
        )
        return record


def build_folder_for(address: str, *, parent: str | Path | None = None) -> Path:
    """
    Папка под адрес: ``<parent>/01_описания`` — или ``<parent>/<адрес>/01_описания``,
    если родительская папка общая для нескольких объектов.
    """
    root = Path(parent) if parent is not None else Path.cwd()
    slug = " ".join(str(address or "").split())
    if not slug:
        return root / DESCR_DIR_NAME
    safe = "".join(ch for ch in slug if ch not in '<>:"/\\|?*').strip(" .")
    return root / (safe or DESCR_DIR_NAME) / DESCR_DIR_NAME


def copy_manual_pdf(
    folder: str | Path, source: str | Path, *, role: str, lot: int | str, tag: str = "NEW",
    block: int | str = "", borough: str = "", identity: dict | None = None, notes: Iterable[str] = (),
) -> deed_store.StoredDeed:
    """
    Файл, скачанный руками (ступень 3 лестницы): назвать и положить по схеме.

    Нужен, когда браузер не справился, а документ у человека уже есть — имя и
    паспорт делает программа, чтобы папка описаний оставалась одинаковой.
    """
    return deed_store.store_pdf(
        folder, source, role=role, lot=lot, tag=tag, block=block, borough=borough, identity=identity,
        notes=[*notes, "файл скачан вручную"],
    )


def adopt_downloads(
    folder: str | Path, *, role: str, lot: int | str, tag: str = "NEW",
    block: int | str = "", borough: str = "", identity: dict | None = None, downloads: str | Path | None = None,
) -> deed_store.StoredDeed | None:
    """
    Забрать из «Загрузок» самый свежий PDF и положить его по схеме.

    Ручной вход: браузер уже скачал файл, программа больше не ждёт, а забирает готовое.
    """
    target = Path(folder)
    target.mkdir(parents=True, exist_ok=True)
    source = rcclerk.find_new_pdf(downloads) if downloads is not None else rcclerk.find_new_pdf(
        rcclerk._default_downloads_dir()  # noqa: SLF001 - та же папка, что у загрузки документа
    )
    if source is None:
        return None
    return copy_manual_pdf(
        target, source, role=role, lot=lot, tag=tag, block=block, borough=borough, identity=identity,
        notes=[f"взято из загрузок: {source.name}"],
    )
