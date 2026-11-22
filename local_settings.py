"""Small atomic edits to local settings without rewriting unrelated credentials."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def save_value(path: str | Path, section: str, key: str, value: Any) -> None:
    target = Path(path)
    data = json.loads(target.read_text(encoding="utf-8") or "{}") if target.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("корень конфигурации должен быть объектом")
    if section not in data:
        data[section] = {}
    if not isinstance(data[section], dict):
        raise ValueError(f"раздел {section} должен быть объектом")
    data[section][key] = value
    fd, name = tempfile.mkstemp(prefix=".setting-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
