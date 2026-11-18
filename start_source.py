"""
Стартовая вкладка источника на Шаге 1: текст или PDF.

Значение хранится в ``config.json`` под ключом ``ui.start_source``:

- ``text`` — «📝 Текст» (по умолчанию, как было раньше);
- ``pdf`` — «📄 PDF».

Остальные ключи файла при сохранении не меняются. Настройка действует при следующем
запуске программы: выбранная во время работы вкладка не сбрасывается.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

TEXT = "text"
PDF = "pdf"
DEFAULT_START_SOURCE = TEXT

#: Подписи переключателя источника (совпадают с подписями на Шаге 1).
LABELS: dict[str, str] = {TEXT: "📝 Текст", PDF: "📄 PDF"}
_BY_LABEL: dict[str, str] = {label: key for key, label in LABELS.items()}


def normalise_start_source(value: Any) -> str:
    """Приводит значение к ``text`` или ``pdf``; всё неизвестное — ``text``."""
    text = str(value or "").strip().lower()
    return text if text in LABELS else DEFAULT_START_SOURCE


def start_source_from_config(config: Mapping[str, Any] | None) -> str:
    """Читает ``ui.start_source`` из уже загруженного конфига."""
    if not isinstance(config, Mapping):
        return DEFAULT_START_SOURCE
    ui = config.get("ui")
    if not isinstance(ui, Mapping):
        return DEFAULT_START_SOURCE
    return normalise_start_source(ui.get("start_source"))


def label_for(key: str) -> str:
    """Подпись вкладки для ``text`` / ``pdf``."""
    return LABELS[normalise_start_source(key)]


def key_for_label(label: str) -> str:
    """Ключ по подписи переключателя; неизвестная подпись даёт ``text``."""
    return _BY_LABEL.get(str(label or ""), DEFAULT_START_SOURCE)


def save_start_source_to_file(path: str | Path, key: str) -> Path:
    """Записывает ``ui.start_source`` в config.json, не трогая остальные ключи."""
    target = Path(path)
    data: dict[str, Any] = {}
    if target.exists():
        raw = json.loads(target.read_text(encoding="utf-8") or "{}")
        if isinstance(raw, dict):
            data = raw
    ui = data.get("ui")
    if not isinstance(ui, dict):
        ui = {}
        data["ui"] = ui
    ui["start_source"] = normalise_start_source(key)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
