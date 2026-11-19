"""Документ правил оффсетов не должен расходиться с кодом.

``docs/step4_offsets_rules.md`` — единственное место, где собраны принятые правила и стили. В §11 таблица
«Флаг | Значение | Что означает»; здесь значения из таблицы сверяются с ``offsets.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import offsets as off

DOC = Path(__file__).resolve().parent.parent / "docs" / "step4_offsets_rules.md"
ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*`(.+?)`\s*\|", re.MULTILINE)


def _documented() -> dict[str, object]:
    rows = ROW.findall(DOC.read_text(encoding="utf-8"))
    assert rows, "в документе нет таблицы флагов"
    return {name: ast.literal_eval(value) for name, value in rows}


def test_documented_values_match_the_code():
    documented = _documented()
    assert len(documented) >= 60
    wrong = {
        name: (value, getattr(off, name))
        for name, value in documented.items()
        if name in dir(off) and getattr(off, name) != value
    }
    assert not wrong, wrong


def test_every_flag_in_the_code_is_documented():
    """Флаг, который влияет на правила, должен быть описан в документе."""
    # версия каркаса и подписи этапов в окне — не правила
    skip = {"STEP4_VERSION", "STEP4_STAGES"}
    flags = {
        name
        for name in dir(off)
        if name.isupper() and not name.startswith("_") and isinstance(getattr(off, name), (int, float, str, bool, tuple))
    }
    missing = flags - set(_documented()) - skip
    assert not missing, sorted(missing)
