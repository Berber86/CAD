"""Conservative lot identification from filenames, separate from the deed's metadata."""
from __future__ import annotations

import copy
import re
from pathlib import PurePath
from typing import Any

# Step 0 creates OUR LOT 32 NEW_p4-6.pdf and LOT 30 NEW.pdf. Avoid interpreting
# an arbitrary address, page, block, year, or bare number as a lot.
_MARKED = re.compile(r"(?<![A-Za-z0-9А-Яа-я])(?:LOT|OUR|ЛОТ)[\s_#№-]*(\d+[A-Za-z]?)(?![A-Za-z0-9]|[-–]\d)", re.IGNORECASE)
_PAGES = re.compile(r"_p\d+(?:[_,-]\d+)*$", re.IGNORECASE)
_OLD_TOKEN = re.compile(r"(?<![A-Za-z0-9А-Яа-я])OLD(?![A-Za-z0-9А-Яа-я])", re.IGNORECASE)


def lot_from_filename(path: Any) -> str | None:
    if not path:
        return None
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    stem = _PAGES.sub("", PurePath(name).stem.strip())
    matches = [m.group(1).upper() for m in _MARKED.finditer(stem)]
    if not matches or len({canonical(x) for x in matches}) != 1:
        return None
    return matches[0]


def has_our_prefix(path: Any) -> bool:
    """Role hint from the filename, not from a directory or an embedded word."""
    if not path:
        return False
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem = PurePath(name).stem
    return stem[:3].casefold() == "our" and (len(stem) == 3 or not stem[3].isalpha())


def is_old_tag(path: Any) -> bool:
    """Return True when filename designates OUR OLD deed fallback (e.g. OUR LOT 76 OLD.pdf)."""
    if not path or not has_our_prefix(path):
        return False
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    stem = _PAGES.sub("", PurePath(name).stem.strip())
    return bool(_OLD_TOKEN.search(stem))


def canonical(value: Any) -> str:
    value = str(value or "").strip().upper()
    match = re.fullmatch(r"0*(\d+)([A-Z]?)", value)
    return (str(int(match.group(1))) + match.group(2)) if match else value


def bind_filename(description: Any, path: Any) -> Any:
    """Return a copy with file identity, never overwriting the lot printed in the deed."""
    if not path:
        return description
    result = description.model_copy()
    result._source_lot_number = lot_from_filename(path)
    result._source_lot_filename = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    result._source_is_old = is_old_tag(path)
    return result


def bind_outcome(outcome: Any, path: Any) -> Any:
    if not path:
        return outcome
    result = copy.copy(outcome)
    result.data = bind_filename(outcome.data, path)
    try:
        import file_metadata
        result.data._source_identity = file_metadata.record_outcome(path, result)
    except (OSError, ValueError) as exc:
        result.warnings = list(getattr(outcome, "warnings", []) or [])
        result.warnings.append(f"не удалось сохранить паспорт {path}: {exc}")
    return result


def source_number(description: Any, fallback_filename: Any = None) -> str | None:
    return getattr(description, "_source_lot_number", None) or lot_from_filename(fallback_filename)


def effective_number(description: Any, fallback_filename: Any = None) -> str | None:
    return source_number(description, fallback_filename) or (str(getattr(description, "lot_number", "") or "").strip() or None)


def lot_label(description: Any, fallback_filename: Any = None) -> str:
    number = effective_number(description, fallback_filename) or "?"
    is_old = bool(getattr(description, "_source_is_old", False)) or (
        fallback_filename is not None and is_old_tag(fallback_filename)
    )
    return f"OLD {number}" if is_old else f"LOT {number}"


def conflict(description: Any, fallback_filename: Any = None) -> bool:
    filename_lot = source_number(description, fallback_filename)
    deed_lot = str(getattr(description, "lot_number", "") or "").strip()
    return bool(filename_lot and deed_lot and canonical(filename_lot) != canonical(deed_lot))


def context(description: Any, filename: Any = None) -> str:
    file_lot = source_number(description, filename)
    deed_lot = str(getattr(description, "lot_number", "") or "").strip() or None
    line = f"lot identifier from filename: {file_lot or 'unknown'}; lot from parser result: {deed_lot or 'unknown'}"
    if conflict(description, filename):
        line += "; DISAGREEMENT: verify both sources; do not treat filename as proof"
    return line
