"""
Разница двух дампов одного чертежа «до» и «после» расстановки оффсетов.

Объекты, которые были до расстановки, сохраняют handle — поэтому новые объекты находятся без совмещения
координат. Показывает новые текстовые подписи (и выноски), изменённые и удалённые объекты.

    python tests/dump_diff.py "Чертёж.before.dump.json" "Чертёж.after.dump.json"
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

TEXT_TYPES = {"Text", "MText", "MLeader", "Leader"}


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _key(record: dict) -> str | None:
    return record.get("handle") or None


def diff(before: dict, after: dict) -> dict:
    """Новые, удалённые и изменённые объекты по handle; объекты без handle считаются отдельно."""
    old = {}
    old_nohandle = 0
    for record in before.get("entities", []):
        key = _key(record)
        if key is None:
            old_nohandle += 1
        else:
            old[key] = record
    new, changed, same = [], [], set()
    new_nohandle = 0
    for record in after.get("entities", []):
        key = _key(record)
        if key is None:
            new_nohandle += 1
            continue
        if key not in old:
            new.append(record)
            continue
        same.add(key)
        if _signature(old[key]) != _signature(record):
            changed.append((old[key], record))
    removed = [record for key, record in old.items() if key not in same and key not in {_key(r) for r in new}]
    return {
        "new": new,
        "changed": changed,
        "removed": removed,
        "nohandle": {"before": old_nohandle, "after": new_nohandle},
    }


def _signature(record: dict) -> str:
    keys = ("type", "layer", "pts", "pt", "text", "h", "rot_deg", "center", "radius", "bbox", "leader_lines")
    return json.dumps({k: record.get(k) for k in keys}, sort_keys=True, ensure_ascii=False, default=str)


def label_text(record: dict) -> str:
    """Текст подписи: для MText/Text — строка; для выноски — текст MLeader."""
    raw = record.get("text") or record.get("text_override") or ""
    return " ".join(str(raw).split())


def label_point(record: dict) -> tuple[float, float] | None:
    pt = record.get("pt") or record.get("text_pt") or record.get("align_pt")
    if pt:
        return float(pt[0]), float(pt[1])
    box = record.get("bbox")
    if box:
        return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    return None


def report(before: dict, after: dict) -> str:
    result = diff(before, after)
    lines = [
        f"Было: {len(before.get('entities', []))} объектов; стало: {len(after.get('entities', []))}",
        f"Новых: {len(result['new'])}; изменённых: {len(result['changed'])}; удалённых: {len(result['removed'])}",
        f"Без handle (не сравниваются): до {result['nohandle']['before']}, после {result['nohandle']['after']}",
    ]
    counts = Counter(record.get("type", "?") for record in result["new"])
    if counts:
        lines.append("Новые по типам: " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
    lines.append("")
    lines.append("Новые подписи (текст | слой | точка):")
    for record in result["new"]:
        if record.get("type") in TEXT_TYPES or label_text(record):
            pt = label_point(record)
            where = f"({pt[0]:.2f}, {pt[1]:.2f})" if pt else "-"
            lines.append(f"  {label_text(record)!r:<28} | {record.get('layer', '')} | {where} | {record.get('type', '')}")
    if result["changed"]:
        lines.append("")
        lines.append("Изменённые (было → стало):")
        for old, new in result["changed"]:
            lines.append(f"  {old.get('type')} {old.get('handle')} {old.get('layer')}: {label_text(old)!r} → {label_text(new)!r}")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(report(load(sys.argv[1]), load(sys.argv[2])))
