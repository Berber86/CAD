"""
Шаг 0: собрать описания участка — одна кнопка от block/lot до папки файлов.

Модуль связывает четыре уже готовых куска в один проход:

1. ``rcclerk.results_url`` → страница списка документов участка;
2. ``rcclerk.parse_document_rows`` + ``rcclerk.choose_documents`` → что качать
   (Deed, иначе Mortgage; нашему лоту — свежий и старый, соседу — свежий);
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

import deed_pages
import deed_store
import rcclerk

#: Папка описаний внутри папки проекта (как в схеме шага).
DESCR_DIR_NAME = "01_описания"

Progress = Callable[[str], None]
StopCheck = Callable[[], bool]

#: ``downloader(строка списка, куда скачать)`` → путь (или ``(путь, ступень)``).
Downloader = Callable[[rcclerk.DeedRow, Path], object]
#: ``fetch_list(block, lot)`` → HTML страницы списка.
ListFetcher = Callable[[int, int], str]
#: ``page_finder(pdf)`` → ``deed_pages.DescriptionPages``.
PageFinder = Callable[[Path], deed_pages.DescriptionPages]


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

    @property
    def ok(self) -> bool:
        """Наш участок собран хотя бы одним документом."""
        return any(record.role == "our" for record in self.records)

    @property
    def our_records(self) -> list[deed_store.StoredDeed]:
        return [record for record in self.records if record.role == "our"]

    def report(self) -> str:
        """Короткий отчёт для экрана Шага 0 (и для журнала)."""
        lines: list[str] = [f"Папка описаний: {self.folder}"]
        if self.records:
            lines.append("")
            lines.append("Собрано:")
            lines.extend(f"  • {record.file}"
                         + (f" — {record.doc_type} от {record.doc_date}" if record.doc_type else "")
                         + (f", описание стр. {record.pages}" if record.pages else ", страницы описания не найдены")
                         for record in self.records)
        else:
            lines.append("")
            lines.append("Ни одного документа собрать не удалось.")
        if self.missing:
            lines.append("")
            lines.append("Чего не хватает:")
            lines.extend(f"  • {item}" for item in self.missing)
        if self.notes:
            lines.append("")
            lines.append("Как шло:")
            lines.extend(f"  • {item}" for item in self.notes)
        return "\n".join(lines)


def _default_fetch_list(block: int, lot: int) -> str:
    return rcclerk.fetch_list_html(block, lot)


def _default_downloader(row: rcclerk.DeedRow, destination: Path) -> tuple[Path, str]:
    return rcclerk.download_with_fallback(row.endorsement_id, destination)


def _default_page_finder(pdf: Path) -> deed_pages.DescriptionPages:
    return deed_pages.find_description_pages(pdf)


def _row_label(row: rcclerk.DeedRow) -> str:
    kind = row.doc_type or "документ"
    when = row.doc_date.isoformat() if row.doc_date else "без даты"
    return f"{kind} №{row.endorsement_id} от {when}"


def collect_deeds(
    *,
    block: int | str,
    lot: int | str,
    neighbours: Sequence[int] | str = (),
    folder: str | Path | None = None,
    base_url: str = rcclerk.BASE_URL,
    with_old_for_neighbour: bool = False,
    fetch_list: ListFetcher | None = None,
    downloader: Downloader | None = None,
    page_finder: PageFinder | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
) -> IntakeOutcome:
    """
    Собирает описания нашего участка и соседей в ``folder`` (по умолчанию
    ``01_описания`` в текущей папке).

    Авария одного документа не отменяет остальные: проход идёт дальше, а причина
    попадает в ``missing``/``notes`` — так человек видит и то, что собралось, и
    что именно надо доделать руками.
    """
    block_no = int(block)
    lot_no = int(lot)
    neighbour_lots = parse_lot_list(neighbours)
    target_dir = Path(folder) if folder is not None else Path.cwd() / DESCR_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    fetch = fetch_list or _default_fetch_list
    download = downloader or _default_downloader
    find_pages = page_finder or _default_page_finder
    outcome = IntakeOutcome(folder=target_dir)

    def say(message: str) -> None:
        outcome.notes.append(message)
        if progress is not None:
            progress(message)

    def check_stop() -> None:
        if should_stop is not None and should_stop():
            raise IntakeCancelled("сбор описаний остановлен")

    plans: list[tuple[str, int]] = [("our", lot_no)]
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
        say(f"{who} (лот {lot_value}): строк в списке — {len(rows)}")
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
                target_dir=target_dir, base_url=base_url,
                download=download, find_pages=find_pages, say=say, missing=outcome.missing,
            )
            if outcome_item is not None:
                outcome.records.append(outcome_item)

    if outcome.records:
        deed_store.write_manifest(
            target_dir,
            block=block_no, lot=lot_no,
            missing=[{"reason": item} for item in outcome.missing],
        )
        say(f"папка описаний собрана: {len(outcome.records)} документ(ов)")
    if not outcome.ok and not any("наш участок" in item for item in outcome.missing):
        outcome.missing.append("наш участок не собран")
    return outcome


def _collect_one(
    *,
    block_no: int,
    lot_value: int,
    role: str,
    row: rcclerk.DeedRow,
    tag: str,
    target_dir: Path,
    base_url: str,
    download: Downloader,
    find_pages: PageFinder,
    say: Progress,
    missing: list[str],
) -> deed_store.StoredDeed | None:
    """Один документ: скачать → найти описание → обрезать → положить с паспортом."""
    name = Path(rcclerk.document_file_name(role, lot_value, tag)).stem
    say(f"  {name}: качаю {_row_label(row)}")
    with tempfile.TemporaryDirectory(prefix="deed_intake_") as tmp:
        raw = Path(tmp) / f"{name}.pdf"
        try:
            result = download(row, raw)
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
        try:
            found = find_pages(saved)
        except Exception as exc:  # noqa: BLE001 - страницы не обязательны для файла
            say(f"  {name}: страницы описания не искались — {exc}")
            found = deed_pages.DescriptionPages()
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
        if row.clerk_number:
            notes.append(f"клерк: {row.clerk_number}")
        if row.doc_type and row.doc_type != row.kind:
            notes.append(f"тип на сайте: {row.doc_type} ({row.kind})")
        record = deed_store.store_pdf(
            target_dir, saved,
            role=role, lot=lot_value, tag=tag, block=block_no,
            doc_type=row.doc_type, doc_date=row.doc_date,
            endorsement_id=row.endorsement_id,
            source_url=rcclerk.document_pdf_url(row.endorsement_id, base=base_url),
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
    block: int | str = "", notes: Iterable[str] = (),
) -> deed_store.StoredDeed:
    """
    Файл, скачанный руками (ступень 3 лестницы): назвать и положить по схеме.

    Нужен, когда браузер не справился, а документ у человека уже есть — имя и
    паспорт делает программа, чтобы папка описаний оставалась одинаковой.
    """
    return deed_store.store_pdf(
        folder, source, role=role, lot=lot, tag=tag, block=block,
        notes=[*notes, "файл скачан вручную"],
    )


def adopt_downloads(
    folder: str | Path, *, role: str, lot: int | str, tag: str = "NEW",
    block: int | str = "", downloads: str | Path | None = None,
) -> deed_store.StoredDeed | None:
    """
    Забрать из «Загрузок» самый свежий PDF и положить его по схеме.

    Это ступень 2 лестницы, вызванная вручную: браузер уже скачал файл, программа
    больше не ждёт, а забирает готовое.
    """
    target = Path(folder)
    target.mkdir(parents=True, exist_ok=True)
    source = rcclerk.find_new_pdf(downloads) if downloads is not None else rcclerk.find_new_pdf(
        rcclerk._default_downloads_dir()  # noqa: SLF001 - та же папка, что у лестницы
    )
    if source is None:
        return None
    return copy_manual_pdf(
        target, source, role=role, lot=lot, tag=tag, block=block,
        notes=[f"взято из загрузок: {source.name}"],
    )
