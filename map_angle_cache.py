"""
Шаг 0: JSON-кэш углов фронтальной улицы для одного снимка tax map.

Рядом со снимком карты (``TAX_MAP_*.jpg``) лежит ``<имя>_angles.json``:
угол фронтальной улицы нашего лота к северу (север — строго вверх по
снимку, градусы по часовой, ``[0, 180)``) из трёх независимых источников:

* ``llm`` — назвала Vision-модель в том же запросе, где соседей;
* ``python_hough_lines`` / ``python_structure_tensor`` — посчитал код
  (``front_street.py``) двумя разными способами.

Программа не выбирает «правильный»: клиент сравнивает значения сам и
применяет самое точное для своих целей. Здесь только запись, чтение и
попарные расхождения для удобного сравнения.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import file_metadata
import front_street

CACHE_VERSION = 1
ANGLE_KEYS = ("llm", "python_hough_lines", "python_structure_tensor")
CONVENTION = (
    "градусы по часовой стрелке от севера; север — строго вверх по снимку; "
    "ориентация линии улицы, 0..180"
)


def angles_path(map_image: str | Path) -> Path:
    """Путь JSON-кэша для снимка карты: рядом, с суффиксом ``_angles``."""
    source = Path(map_image)
    return source.with_name(f"{source.stem}_angles.json")


def fmt_angle(value: Optional[float]) -> str:
    """«35°» или «—», если источник угол не дал."""
    return "—" if value is None else f"{float(value):.0f}°"


def _pairwise(angles: dict[str, Optional[float]]) -> dict[str, Optional[float]]:
    pairs: dict[str, Optional[float]] = {}
    keys = [key for key in ANGLE_KEYS if angles.get(key) is not None]
    for i, first in enumerate(keys):
        for second in keys[i + 1:]:
            pairs[f"{first} vs {second}"] = round(
                front_street.angle_difference(angles[first], angles[second]), 1
            )
    return pairs


def write_angles(
    map_image: str | Path,
    *,
    llm_angle: Optional[float] = None,
    python_angles: Optional[dict[str, Optional[float]]] = None,
    address: str = "",
    block: str = "",
    lot: str = "",
) -> Path:
    """
    Пишет (или обновляет) JSON-кэш углов для снимка карты.

    ``python_angles`` — словарь ``front_street.estimate_all``. Возвращает
    путь файла кэша; sha256 снимка внутри — чтобы кэш нельзя было молча
    отнести к другой картинке.
    """
    source = Path(map_image)
    estimates = dict(python_angles or {})
    angles: dict[str, Optional[float]] = {
        "llm": llm_angle,
        "python_hough_lines": estimates.get("python_hough_lines"),
        "python_structure_tensor": estimates.get("python_structure_tensor"),
    }
    payload = {
        "version": CACHE_VERSION,
        "image": source.name,
        "sha256": file_metadata._digest(source) if source.is_file() else "",
        "created": datetime.now().isoformat(timespec="seconds"),
        "address": str(address or ""),
        "block": str(block or ""),
        "lot": str(lot or ""),
        "convention": CONVENTION,
        "front_street_angle_deg": angles,
        "pairwise_difference_deg": _pairwise(angles),
    }
    target = angles_path(source)
    file_metadata._write_json(target, payload)
    return target


def read_angles(map_image: str | Path) -> dict:
    """Содержимое JSON-кэша углов снимка (пустой словарь, если кэша нет)."""
    path = angles_path(map_image)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}
