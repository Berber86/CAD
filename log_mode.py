"""
Глобальный режим журналов программы.

- ``full`` («подробный») — показываются все записи как раньше.
- ``simple`` («упрощённый») — по умолчанию. Видны ошибки, итоги этапов и вопросы;
  подробные шаги («качаю…», «OCR страница N», таймеры, dpi, пути) и предупреждения скрыты.

Режим хранится в ``config.json`` под ключом ``ui.log_mode``. Остальные ключи файла
при сохранении не меняются.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

FULL = "full"
SIMPLE = "simple"
DEFAULT_MODE = SIMPLE

# Уровни записей журнала. ``key`` — итог этапа или вопрос к пользователю.
LEVEL_INFO = "info"
LEVEL_KEY = "key"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

_SIMPLE_LEVELS = frozenset({LEVEL_KEY, LEVEL_ERROR})

# В упрощённом режиме обычные (информационные) записи видны, если они короткие и одной строкой.
# Длинные описания, таблицы и дампы — только в подробном режиме. Порог подобран так, чтобы
# упрощённый журнал был примерно втрое короче полного по объёму текста.
SIMPLE_INFO_MAX_CHARS = 70

_STAMP_RE = re.compile(r"^\[?\d\d:\d\d:\d\d\]?\s{1,2}")

# Служебные информационные строки, которые в упрощённом журнале только отвлекают: пустые строки,
# шаги «[3/3] …», страницы «стр. 1 из 1 · …», пояснения «→ …», разделители «--- … ---»
# и дубль строки «Разбор набора PDF: …» (итог показан строкой «Файлов: …»).
_SIMPLE_NOISE = (
    re.compile(r"^\s*$"),
    re.compile(r"^\s*→"),
    re.compile(r"^\s*(\[\d+/\d+\]\s*)+"),
    re.compile(r"стр\. \d+[–-]?\d* из \d+"),
    re.compile(r"^\s*---"),
    re.compile(r"^\s*Разбор набора PDF:"),
)


def is_simple_noise(body: str) -> bool:
    """Служебная информационная строка, которую упрощённый журнал не показывает."""
    return any(pattern.search(body) for pattern in _SIMPLE_NOISE)

# Отчёт Шага 1 в упрощённом режиме: остаются заголовки, итоги, пункты с правками, ошибки
# (⛔ ✗ 🚨) и строки о расхождении соотношения участков. Предупреждения (⚠), «ок»-строки (✓),
# пояснения и детали (•, «Откуда значение», «Проверка») скрываются.
_STEP1_KEEP_MARKERS = ("⛔", "✗", "🚨", "✎")
_STEP1_KEEP_WORDS = ("критическ", "расхожден", "НАЛОЖЕНИ", "не замыкается", "не бьётся")
_STEP1_KEEP_PREFIXES = ("▸", "ИТОГ", "[", "Правка:", "Файлов")


def normalise_mode(value: Any) -> str:
    """Любое значение → ``full`` или ``simple``. Неизвестное — упрощённый (по умолчанию)."""
    return FULL if str(value or "").strip().lower() == FULL else SIMPLE


def mode_from_config(cfg: Any) -> str:
    """Режим из загруженного конфига; если ключа нет — упрощённый."""
    ui = cfg.get("ui") if isinstance(cfg, dict) else None
    value = ui.get("log_mode") if isinstance(ui, dict) else None
    return normalise_mode(value if value is not None else DEFAULT_MODE)


def level_visible(level: str, mode: str, text: str = "") -> bool:
    """Видна ли запись уровня ``level`` в режиме ``mode``.

    ``text`` нужен только для информационных записей упрощённого режима: их длина считается
    без метки времени, многострочные записи скрываются.
    """
    if normalise_mode(mode) == FULL:
        return True
    if level in _SIMPLE_LEVELS:
        return True
    if level != LEVEL_INFO or not text:
        return False
    body = _STAMP_RE.sub("", text, count=1)
    if is_simple_noise(body):
        return False
    return "\n" not in body and len(body) <= SIMPLE_INFO_MAX_CHARS


def filter_entries(entries: Iterable[tuple[str, str]], mode: str) -> list[str]:
    """Тексты записей ``(уровень, текст)``, которые видны в данном режиме."""
    return [text for level, text in entries if level_visible(level, mode, text)]


def report_line_level(line: str) -> str:
    """Уровень строки отчёта Шага 1 по её значкам (⛔/✗ — ошибка, ⚠ — предупреждение)."""
    if "⛔" in line or "✗" in line:
        return LEVEL_ERROR
    if "⚠" in line:
        return LEVEL_WARN
    stripped = line.strip()
    if stripped.startswith(("▸", "ИТОГ", "НАШ УЧАСТОК")):
        return LEVEL_KEY
    return LEVEL_INFO


def _step1_line_kept(line: str) -> bool:
    stripped = line.strip()
    if not stripped or "✓" in stripped:
        return False
    if any(marker in stripped for marker in _STEP1_KEEP_MARKERS):
        return True
    if any(word in stripped for word in _STEP1_KEEP_WORDS):
        return True
    if stripped.startswith(_STEP1_KEEP_PREFIXES):
        return True
    # Заголовки разделов — строки без отступа, написанные заглавными буквами.
    return line == line.lstrip() and stripped.upper() == stripped and any(
        ch.isalpha() for ch in stripped
    ) and len([w for w in stripped.split() if w.isalpha()]) >= 2


def filter_report_lines(lines: Sequence[str], mode: str) -> list[str]:
    """Строки отчёта Шага 1 для показа. В упрощённом режиме — см. _STEP1_KEEP_* выше."""
    if normalise_mode(mode) == FULL:
        return list(lines)
    kept: list[str] = []
    for line in lines:
        if not line.strip():
            if kept and kept[-1].strip():
                kept.append("")
            continue
        if _step1_line_kept(line):
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


# Маркеры строк отчёта Шага 2 (подписи). Ошибки подписей и сбоев AutoCAD в упрощённом режиме
# не скрываются.
_STEP2_ERROR_MARKERS = (
    "ОШИБК", "НЕ ВЫПОЛНЕН", "НЕ ПОДПИСАН", "не удалось", "⛔", "✗",
)
_STEP2_WARN_MARKERS = ("⚠", "подписей больше, чем объектов")
_STEP2_KEY_PREFIXES = (
    "ШАГ 2", "НАЙДЕНО", "ПОДПИСАНО", "К ПОДПИСАНИЮ", "МАСШТАБ ТИПА", "ИТОГ", "▸",
)


def step2_line_level(line: str) -> str:
    """Уровень строки отчёта Шага 2: ошибка подписи, предупреждение, итог или обычная строка."""
    if any(marker in line for marker in _STEP2_ERROR_MARKERS):
        return LEVEL_ERROR
    if any(marker in line for marker in _STEP2_WARN_MARKERS):
        return LEVEL_WARN
    stripped = line.strip()
    if stripped.startswith(_STEP2_KEY_PREFIXES):
        return LEVEL_KEY
    return LEVEL_INFO


def filter_step2_lines(lines: Sequence[str], mode: str) -> list[str]:
    """Строки отчёта Шага 2 для показа: в упрощённом режиме остаются итоги и ошибки.

    Отчёт о сбое («ШАГ 2 НЕ ВЫПОЛНЕН») показывается целиком: причина написана в его строках.
    """
    if normalise_mode(mode) == FULL or any("НЕ ВЫПОЛНЕН" in line for line in lines):
        return list(lines)
    kept: list[str] = []
    for line in lines:
        if not line.strip():
            if kept and kept[-1].strip():
                kept.append("")
            continue
        if step2_line_level(line) in _SIMPLE_LEVELS:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def save_mode_to_file(path: str | Path, mode: str) -> Path:
    """Записывает ``ui.log_mode`` в config.json, не трогая остальные ключи."""
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
    ui["log_mode"] = normalise_mode(mode)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
