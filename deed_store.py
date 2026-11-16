"""
Шаг 0: папка описаний участка — имена, паспорта документов и общий список.

Задача простая и скучная, но именно она превращает «накачал PDF» в рабочий
набор: файл лежит под понятным именем, рядом лежит его паспорт (откуда взят,
что за документ, какие страницы содержат описание), а ``_manifest.json``
позволяет Шагу 1 поднять весь набор одной кнопкой — с ролями и страницами.

Имена по схеме Коли: ``OUR 32 NEW.pdf``, ``OUR 32 OLD.pdf`` (наш участок —
свежий и старый документ), ``LOT 30 NEW.pdf`` (сосед — только свежий).
Суффикс ``_p4-6`` добавляется, когда известны страницы с описанием: его
понимает Шаг 1 без всяких паспортов.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

MANIFEST_NAME = "_manifest.json"
MANIFEST_VERSION = 1


class DeedStoreError(RuntimeError):
    """Папку описаний не удалось собрать."""


@dataclass
class StoredDeed:
    """Один документ в папке описаний."""

    file: str
    role: str = "our"              # our | neighbour
    lot: str = ""
    block: str = ""
    tag: str = "NEW"               # NEW | OLD
    doc_type: str = ""             # DEED / MORTGAGE
    doc_date: str = ""             # ISO, как пришло
    endorsement_id: str = ""       # номер документа на сайте клерка
    source_url: str = ""
    pages: str = ""                # "4-6" — страницы с описанием
    trimmed: str = ""              # обрезанная копия только с описанием
    downloaded: str = ""
    text_file: str = ""            # распознанный текст описания
    checks: dict[str, Any] = field(default_factory=dict)
    accepted: bool = False
    notes: list[str] = field(default_factory=list)

    def sidecar_name(self) -> str:
        return f"{Path(self.file).stem}.json"


def _iso(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value or "")


def store_pdf(
    folder: str | Path,
    source_pdf: str | Path,
    *,
    role: str,
    lot: int | str,
    tag: str,
    block: int | str = "",
    doc_type: str = "",
    doc_date: Any = "",
    endorsement_id: int | str = "",
    source_url: str = "",
    pages: str = "",
    trimmed_source: str | Path | None = None,
    text: str = "",
    notes: Iterable[str] = (),
) -> StoredDeed:
    """
    Кладёт скачанный PDF в папку под именем по схеме и пишет паспорт.

    Документ хранится **целиком** (``OUR 32 NEW.pdf``): обложка и RP-5217 иногда
    нужны, а места они почти не занимают. Если известны страницы описания и дана
    обрезанная копия, рядом появляется ``OUR 32 NEW_p4-6.pdf`` — её и открывать,
    её и читать Шагу 1.

    Если такой документ уже лежит (то же имя), файл не дублируется: обновляется
    только паспорт. Возвращает запись для ``_manifest.json``.
    """
    target_dir = Path(folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    from rcclerk import document_file_name  # локальный импорт: модуль без сети

    destination = target_dir / document_file_name(role, lot, tag)
    final_name = destination.name
    source = Path(source_pdf)
    if source.suffix.lower() != ".pdf":
        raise DeedStoreError(f"ожидался PDF, получено: {source.name}")
    if source.resolve() != destination.resolve():
        shutil.copyfile(source, destination)
    trimmed_name = ""
    if pages and trimmed_source is not None:
        trimmed_name = f"{destination.stem}_p{pages}.pdf"
        trimmed_source_path = Path(trimmed_source)
        if trimmed_source_path.resolve() != (target_dir / trimmed_name).resolve():
            shutil.copyfile(trimmed_source_path, target_dir / trimmed_name)
    record = StoredDeed(
        file=final_name,
        role=role,
        lot=str(lot),
        block=str(block),
        tag=tag,
        doc_type=doc_type,
        doc_date=_iso(doc_date),
        endorsement_id=str(endorsement_id),
        source_url=source_url,
        pages=pages,
        trimmed=trimmed_name,
        text_file=f"{Path(final_name).stem}_text.txt" if text.strip() else "",
        downloaded=datetime.now().isoformat(timespec="seconds"),
        notes=list(notes),
    )
    if record.text_file:
        (target_dir / record.text_file).write_text(text, encoding="utf-8")
    (target_dir / record.sidecar_name()).write_text(
        json.dumps(asdict(record), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_manifest(target_dir)
    return record


def read_records(folder: str | Path) -> list[StoredDeed]:
    """Все паспорта из папки (файлы ``*.json``, кроме ``_manifest.json``)."""
    records: list[StoredDeed] = []
    for path in sorted(Path(folder).glob("*.json")):
        if path.name == MANIFEST_NAME:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or "file" not in raw:
            continue
        known = {field_name for field_name in StoredDeed.__dataclass_fields__}
        records.append(StoredDeed(**{key: value for key, value in raw.items() if key in known}))
    return records


def duplicate_of(records: Iterable[StoredDeed], endorsement_id: int | str) -> StoredDeed | None:
    """Тот же документ уже в папке? Один номер клерка = один файл."""
    key = str(endorsement_id or "").strip()
    if not key:
        return None
    for record in records:
        if str(record.endorsement_id).strip() == key:
            return record
    return None


def write_manifest(
    folder: str | Path,
    *,
    address: str = "",
    bbl: str = "",
    block: int | str = "",
    lot: int | str = "",
    missing: Iterable[dict[str, Any]] = (),
) -> Path:
    """
    Общий список набора. Шаг 1 читает ``items``: первая запись с ``role="our"``
    становится «нашим участком», остальные — соседями; ``pages`` задаёт страницы.
    """
    target_dir = Path(folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    records = read_records(target_dir)
    ordered = sorted(records, key=lambda record: (record.role != "our", record.lot, record.tag != "NEW"))
    manifest = {
        "version": MANIFEST_VERSION,
        "address": address,
        "bbl": bbl,
        "block": str(block or ""),
        "lot": str(lot or ""),
        "created": datetime.now().isoformat(timespec="seconds"),
        "items": [asdict(record) for record in ordered],
        "missing": list(missing),
    }
    path = target_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_manifest(folder: str | Path) -> dict[str, Any]:
    """Содержимое ``_manifest.json`` (пустой словарь, если его нет)."""
    path = Path(folder) / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeedStoreError(f"{MANIFEST_NAME} повреждён: {exc}") from exc
    return raw if isinstance(raw, dict) else {}


def summary(folder: str | Path) -> str:
    """Короткий отчёт по папке: что нашли и чего не хватает (для Шага 0)."""
    records = read_records(folder)
    if not records:
        return "Папка описаний пуста: документов пока нет."
    lines = [f"Документов в папке описаний: {len(records)}"]
    for record in sorted(records, key=lambda item: (item.role != "our", item.lot, item.tag != "NEW")):
        who = "наш участок" if record.role == "our" else "сосед"
        kind = record.doc_type or "документ"
        year = record.doc_date[:4] if record.doc_date else "год?"
        line = f"  • {record.file} — {who}, {kind} {year}, лот {record.lot}"
        if record.pages:
            line += f", описание на стр. {record.pages}"
            if record.trimmed:
                line += f" (обрезанная копия: {record.trimmed})"
        else:
            line += ", страницы описания не найдены"
        if not record.accepted:
            line += " ⚠ не проверен"
        lines.append(line)
    ours = [record for record in records if record.role == "our"]
    if not ours:
        lines.append("  ⚠ нашего участка в папке нет — Шаг 1 не сможет начать с него")
    elif not any(record.tag == "OLD" for record in ours):
        lines.append("  • старого документа нашего участка нет (качается, если он есть в списке)")
    return "\n".join(lines)
