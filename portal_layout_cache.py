"""Локальные координаты метки формы NYC Portal, полученные надёжным поиском.

Файл не содержит адресов, данных участка или снимков. Координаты используются
только для ускорения OCR, а не как готовые координаты для слепых кликов.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", "").strip()
    return (Path(base) / "TitleSurveyAI" if base else Path.home() / ".cache" / "titlesurveyai") / "portal_layout.json"


def _validated(data: object, browser: str, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    if not browser or data.get("browser") != browser.lower() or data.get("size") != list(size):
        return None
    box = data.get("address_label")
    if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
        return None
    x0, y0, x1, y1 = box
    w, h = size
    if w < 400 or h < 300 or not (0 <= x0 < x1 <= w * .65 and 0 <= y0 < y1 <= h * .8):
        return None
    return x0, y0, x1, y1


def load(browser: str, size: tuple[int, int], path: Path | None = None) -> tuple[int, int, int, int] | None:
    try:
        file = path or cache_path()
        if file.stat().st_size > 16384:
            return None
        return _validated(json.loads(file.read_text(encoding="utf-8")), browser, size)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def save(browser: str, size: tuple[int, int], box: tuple[int, int, int, int], path: Path | None = None) -> bool:
    data = {"version": 1, "browser": browser.lower(), "size": list(size), "address_label": list(box)}
    if _validated(data, browser, size) is None:
        return False
    target = path or cache_path()
    name = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".portal-layout-", suffix=".tmp", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, target)
        return True
    except OSError:
        return False
    finally:
        if name:
            try:
                os.unlink(name)
            except OSError:
                pass


def label_region(box: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[float, float, float, float]:
    """Область вокруг метки, с запасом на мелкие изменения сайта."""
    x0, y0, x1, y1 = box
    w, h = size
    return (max(0, x0 - 60) / w, max(0, y0 - 80) / h,
            min(w, x1 + 100) / w, min(h, y1 + 100) / h)
