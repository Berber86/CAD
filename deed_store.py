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

import file_metadata

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
    borough: str = ""
    identity: dict[str, list[dict[str, str]]] = field(default_factory=dict)
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


@file_metadata.with_lock
def store_pdf(
    folder: str | Path,
    source_pdf: str | Path,
    *,
    role: str,
    lot: int | str,
    tag: str,
    block: int | str = "",
    borough: str = "",
    identity: dict[str, list[dict[str, str]]] | None = None,
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
    passport = target_dir / f"{destination.stem}.json"
    previous: dict[str, Any] = {}
    if passport.exists():
        previous = file_metadata._read_json(passport)
        if previous.get("file") != final_name:
            raise DeedStoreError(f"паспорт {passport.name} принадлежит другому файлу")
        if "file_digests" in previous and not isinstance(previous["file_digests"], dict):
            raise DeedStoreError(f"формат file_digests в {passport.name} не поддерживается; паспорт не изменён")
        if "document_history" in previous and not isinstance(previous["document_history"], list):
            raise DeedStoreError(f"формат document_history в {passport.name} не поддерживается; паспорт не изменён")
        if "identity" in previous and not isinstance(previous["identity"], dict):
            raise DeedStoreError(f"формат identity в {passport.name} не поддерживается; паспорт не изменён")
        try:
            file_metadata.combine(previous.get("identity"))
        except ValueError as exc:
            raise DeedStoreError(f"формат identity в {passport.name} не поддерживается; паспорт не изменён") from exc
    supplied = file_metadata.combine(identity)
    source = Path(source_pdf)
    if source.suffix.lower() != ".pdf":
        raise DeedStoreError(f"ожидался PDF, получено: {source.name}")
    previous_digest = file_metadata._digest(destination) if previous and destination.is_file() else ""
    if source.resolve() != destination.resolve():
        shutil.copyfile(source, destination)
    same_content = bool(previous_digest and previous_digest == file_metadata._digest(destination))
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
        borough=str(borough or ""),
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
    if previous and same_content:
        if not record.borough:
            record.borough = str(previous.get("borough") or "")
        if not record.block:
            record.block = str(previous.get("block") or "")
        if not pages and previous.get("pages"):
            record.pages = str(previous["pages"])
        if not record.trimmed and record.pages == str(previous.get("pages") or ""):
            old_trimmed = str(previous.get("trimmed") or "")
            if old_trimmed and (target_dir / old_trimmed).is_file():
                record.trimmed = old_trimmed
        if not record.text_file:
            old_text = str(previous.get("text_file") or "")
            if old_text and (target_dir / old_text).is_file():
                record.text_file = old_text
        old_notes = previous.get("notes")
        if isinstance(old_notes, list):
            record.notes = list(dict.fromkeys([*old_notes, *record.notes]))
    if text.strip() and record.text_file:
        (target_dir / record.text_file).write_text(text, encoding="utf-8")
    observations = file_metadata.filename_observations(destination)
    # Без известной истории ввода это ручные значения, а не данные портала.
    for field_name, value in (("block", block), ("lot", lot), ("borough", borough)):
        if value and not any(str(entry.get("value", "")).strip() == str(value).strip()
                             for entry in supplied.get(field_name, [])):
            file_metadata.add(supplied, field_name, file_metadata.observation(value, "manual"))
    digest = file_metadata._digest(destination)
    for field_name in file_metadata.FIELDS:
        for entry in supplied.get(field_name, []):
            entry.setdefault("file", final_name)
            entry.setdefault("sha256", digest)
    legacy: dict[str, list[dict[str, str]]] = {}
    if previous and "identity" not in previous:
        for field_name in file_metadata.FIELDS:
            file_metadata.add(legacy, field_name, file_metadata.observation(previous.get(field_name), "unknown", method="legacy passport"))
    record.identity = file_metadata.combine(previous.get("identity"), legacy, observations, supplied)
    if previous and same_content:
        record.accepted = bool(previous.get("accepted", False))
        record.checks = previous.get("checks") if isinstance(previous.get("checks"), dict) else {}
    raw = dict(previous)
    if previous_digest and not same_content:
        history = raw.get("document_history", [])
        if not isinstance(history, list):
            raise DeedStoreError(f"формат document_history в {passport.name} не поддерживается")
        old_snapshot = {key: value for key, value in previous.items() if key != "document_history"}
        old_snapshot["content_sha256"] = previous_digest
        history = [*history, old_snapshot]
        raw["document_history"] = history
    hashes = dict(raw.get("file_digests", {}))
    hashes[final_name] = digest
    if record.trimmed and (target_dir / record.trimmed).is_file():
        hashes[record.trimmed] = file_metadata._digest(target_dir / record.trimmed)
    raw["file_digests"] = hashes
    raw.pop("standalone", None)
    raw.update(asdict(record))
    file_metadata._write_json(passport, raw)
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
        if not isinstance(raw, dict) or "file" not in raw or raw.get("standalone"):
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


@file_metadata.with_lock
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
    existing = read_manifest(target_dir)
    items = []
    for record in ordered:
        # Include forward-compatible passport fields, rather than dropping them.
        passport_path = target_dir / record.sidecar_name()
        passport = file_metadata._read_json(passport_path) if passport_path.is_file() else asdict(record)
        items.append(passport)
    manifest = {
        **existing,
        "version": MANIFEST_VERSION,
        "address": address or existing.get("address", ""),
        "bbl": bbl or existing.get("bbl", ""),
        "block": str(block or existing.get("block", "")),
        "lot": str(lot or existing.get("lot", "")),
        "created": datetime.now().isoformat(timespec="seconds"),
        "items": items,
        "missing": list(missing),
    }
    path = target_dir / MANIFEST_NAME
    file_metadata._write_json(path, manifest)
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
