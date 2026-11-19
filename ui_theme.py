"""Сохранение выбранной цветовой темы интерфейса отдельно от CAD-геометрии."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

BLUE = "blue"
LAVENDER = "lavender"
GRAY = "gray"
DEFAULT = BLUE
LABELS = {BLUE: "Голубоватая", LAVENDER: "Лавандовая", GRAY: "Серая"}

# Серая тема 4.44; семантические цвета CAD одинаковы для всех тем.
GRAY_PALETTE: dict[str, str] = {'bg_root': '#0d0d0d',
 'bg_header': '#141414',
 'bg_card': '#1b1b1b',
 'bg_inset': '#181818',
 'bg_editor': '#0b0b0b',
 'bg_field': '#131313',
 'border_card': '#3d3d3d',
 'border_subtle': '#333333',
 'border_active': '#a5a5a5',
 'text_main': '#f2f2f2',
 'text_muted': '#bdbdbd',
 'text_dim': '#a6a6a6',
 'accent_soft_bg': '#303030',
 'accent_soft_fg': '#e2e2e2',
 'seg_sel': '#484848',
 'side_label': '#bfbfbf',
 'grid_axis': '#4d6381',
 'btn_primary': '#484848',
 'btn_primary_hover': '#555555',
 'control_off': '#303030',
 'control_on': '#777777',
 'btn_neighbor': '#0d766f',
 'btn_neighbor_hover': '#0f7a72',
 'btn_secondary': '#292929',
 'btn_secondary_hover': '#393939',
 'btn_amber': '#a54808',
 'btn_amber_hover': '#ab500d',
 'btn_emerald': '#08785c',
 'btn_emerald_hover': '#087c5d',
 'version_bg': '#303030',
 'version_fg': '#e2e2e2',
 'badge_v1_bg': '#102d29',
 'badge_v1_border': '#275e50',
 'badge_v1_fg': '#87e8c3',
 'ok': '#65deb1',
 'ok_bg': '#102d29',
 'warn': '#fbbf24',
 'warn_bg': '#302318',
 'warn_border': '#9d6323',
 'err': '#ff9097',
 'err_border': '#b94c5b',
 'sky': '#9adcf8',
 'cad_grid': '#132235',
 'cad_grid_minor': '#101c2c',
 'cad_prop': '#34d399',
 'cad_front': '#f87171',
 'cad_neighbor': '#22d3ee',
 'cad_street': '#f87171',
 'cad_street_text': '#7dd3fc',
 'cad_tie': '#fbbf24',
 'cad_hover': '#fde047',
 'cad_origin': '#94a3b8',
 'accent_icon': '#d2d2d2',
 'teal_soft_bg': '#123c39',
 'teal_soft_fg': '#7ce8db'}

# Голубоватая тема 4.42: сохранён характер оригинала, контраст текста улучшен.
BLUE_OVERRIDES: dict[str, str] = {'bg_root': '#0a111a',
 'bg_header': '#0f1926',
 'bg_card': '#0d1622',
 'bg_inset': '#0d1622',
 'bg_editor': '#080e16',
 'bg_field': '#020617',
 'border_card': '#23354d',
 'border_subtle': '#1e293b',
 'border_active': '#3b82f6',
 'text_main': '#eaf1fb',
 'text_muted': '#94a3b8',
 'seg_sel': '#334155',
 'side_label': '#a7b6c9',
 'grid_axis': '#4d6381',
 'btn_primary': '#2459c4',
 'btn_primary_hover': '#204faf',
 'btn_secondary': '#1c2c40',
 'btn_secondary_hover': '#283d58',
 'cad_grid': '#132235',
 'cad_grid_minor': '#101c2c',
 'teal_soft_bg': '#042f2e',
 'teal_soft_fg': '#5eead4',
 'accent_soft_bg': '#0c1c33',
 'accent_soft_fg': '#bfdbfe',
 'accent_icon': '#a78bfa',
 'version_bg': '#0c1c33',
 'version_fg': '#bfdbfe',
 'control_off': '#334155',
 'control_on': '#4b8bfa',
 'text_dim': '#a6b2c5'}

# Лавандовая тема 4.43.
LAVENDER_OVERRIDES: dict[str, str] = {'bg_root': '#0b0d16',
 'bg_header': '#111321',
 'bg_card': '#171a29',
 'bg_inset': '#141725',
 'bg_editor': '#0a0d17',
 'bg_field': '#101321',
 'border_card': '#34394e',
 'border_subtle': '#2b3042',
 'border_active': '#a69bff',
 'text_main': '#f2f3fa',
 'text_muted': '#b7bfd1',
 'text_dim': '#9ca5bb',
 'accent_soft_bg': '#292344',
 'accent_soft_fg': '#ddd7ff',
 'seg_sel': '#3b375b',
 'side_label': '#bac4d6',
 'btn_primary': '#594bc4',
 'btn_primary_hover': '#6656d6',
 'btn_neighbor': '#0d766f',
 'btn_neighbor_hover': '#0f7a72',
 'btn_secondary': '#252b3e',
 'btn_secondary_hover': '#343b52',
 'btn_amber': '#a54808',
 'btn_amber_hover': '#ab500d',
 'btn_emerald': '#08785c',
 'btn_emerald_hover': '#087c5d',
 'version_bg': '#27213f',
 'version_fg': '#d6caff',
 'badge_v1_bg': '#102d29',
 'badge_v1_border': '#275e50',
 'badge_v1_fg': '#87e8c3',
 'ok': '#65deb1',
 'ok_bg': '#102d29',
 'warn': '#fbbf24',
 'warn_bg': '#302318',
 'warn_border': '#9d6323',
 'err': '#ff9097',
 'err_border': '#b94c5b',
 'sky': '#9adcf8',
 'accent_icon': '#c2b5ff',
 'teal_soft_bg': '#123c39',
 'teal_soft_fg': '#7ce8db',
 'control_off': '#3b375b',
 'control_on': '#8778f1'}

PALETTES = {
    BLUE: {**GRAY_PALETTE, **BLUE_OVERRIDES},
    LAVENDER: {**GRAY_PALETTE, **LAVENDER_OVERRIDES},
    GRAY: GRAY_PALETTE,
}


def normalise(value: Any) -> str:
    return value if isinstance(value, str) and value in LABELS else DEFAULT


def from_config(config: Mapping[str, Any] | None) -> str:
    ui = config.get("ui") if isinstance(config, Mapping) else None
    return normalise(ui.get("theme")) if isinstance(ui, Mapping) else DEFAULT


def key_for_label(label: str) -> str:
    return next((key for key, text in LABELS.items() if text == label), DEFAULT)


def save_to_file(path: str | Path, key: str) -> None:
    """Атомарно меняет только ui.theme в локальном конфиге (без потери остальных данных)."""
    target = Path(path)
    data = json.loads(target.read_text(encoding="utf-8") or "{}") if target.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("корень конфигурации должен быть объектом")
    if "ui" not in data:
        data["ui"] = {}
    if not isinstance(data["ui"], dict):
        raise ValueError("раздел ui должен быть объектом")
    data["ui"]["theme"] = normalise(key)
    fd, name = tempfile.mkstemp(prefix=".theme-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
