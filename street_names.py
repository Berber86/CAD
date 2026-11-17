"""
Названия улиц в legal description и подписи под ними на чертеже (требования заказчика).

* Старое название улицы («formerly», «previously known as», «formerly known as», «also known as»,
  «a/k/a», «f/k/a») — в скобках или без них — отделяется от текущего названия. На чертеже оно
  выводится отдельной строкой под шириной: «(FORMERLY CARLTON STREET)».
* Под названием фасадной улицы в скобках пишется ширина: «(38' WIDE)». Если ширины в описании
  нет, пишется «(XX' WIDE)» — место для ручного заполнения.
* Между словами названия улицы на чертеже — два пробела.
"""

from __future__ import annotations

import re

PLACEHOLDER_WIDTH = "XX"

_FORMER_PHRASE = (
    r"(?:formerly\s+known\s+as|previously\s+known\s+as|previous(?:ly)?\s+known\s+as"
    r"|also\s+known\s+as|formerly|a/k/a|f/k/a)"
)

# «Stafford Avenue (formerly Carlton Street)», «Stafford Avenue, formerly Carlton Street,»,
# «Stafford Avenue (also known as Carlton Street)».
FORMER_NAME_RE = re.compile(
    r"(?:\s*\(\s*|\s*,\s*|\s+)"
    + _FORMER_PHRASE
    + r"\s+(?P<name>[^,;()]{2,60}?)\s*\)?(?=\s*[,;)]|\s*$)",
    re.IGNORECASE,
)

# «38' WIDE», «60 FEET WIDE», «40 FT WIDE».
WIDTH_RE = re.compile(
    r"(?P<width>\d+(?:\.\d+)?)\s*(?:'|’|′|FEET|FOOT|FT\.?)?\s*WIDE\b",
    re.IGNORECASE,
)


def split_former_name(text: str | None) -> tuple[str, str | None]:
    """«Stafford Avenue (formerly Carlton Street)» → («Stafford Avenue», «Carlton Street»)."""
    raw = str(text or "")
    match = FORMER_NAME_RE.search(raw)
    if not match:
        return raw.strip(), None
    former = " ".join(match.group("name").split()).strip(" ,;")
    clean = (raw[: match.start()] + raw[match.end():]).strip(" ,;()")
    clean = " ".join(clean.split())
    return clean, (former or None)


def find_former_name(full_text: str, street: str | None, window: int = 160) -> str | None:
    """Старое название, которое стоит рядом с упоминанием улицы в полном тексте описания."""
    if not street or not full_text:
        return None
    pos = re.search(re.escape(street), full_text, re.IGNORECASE)
    if not pos:
        return None
    chunk = full_text[pos.end(): pos.end() + window]
    match = FORMER_NAME_RE.search(chunk)
    if not match:
        return None
    former = " ".join(match.group("name").split()).strip(" ,;")
    return former or None


def find_width_text(full_text: str, street: str | None, window: int = 160) -> str | None:
    """Ширина улицы рядом с её упоминанием, в виде примечания «(60' WIDE)»."""
    if not street or not full_text:
        return None
    pos = re.search(re.escape(street), full_text, re.IGNORECASE)
    if not pos:
        return None
    match = WIDTH_RE.search(full_text[pos.end(): pos.end() + window])
    if not match:
        return None
    return f"({_format_width(match.group('width'))}' WIDE)"


def _format_width(value: str) -> str:
    number = float(value)
    return f"{number:g}"


def parse_street_note(note: str | None) -> tuple[str | None, list[str]]:
    """
    Примечание под улицей → (ширина или None, доп. строки в скобках).

    «(38' WIDE ROADWAY EASEMENT)» → («38», [«(ROADWAY EASEMENT)»]);
    «(NOT MAPPED ON FINAL CITY PLAN)» → (None, [«(NOT MAPPED ON FINAL CITY PLAN)»]).
    Старое название из примечания здесь не выводится: оно хранится отдельно.
    """
    text = " ".join(str(note or "").replace("(", " ").replace(")", " ").split()).upper()
    if not text:
        return None, []
    width = None
    match = WIDTH_RE.search(text)
    if match:
        width = _format_width(match.group("width"))
        text = (text[: match.start()] + " " + text[match.end():]).strip()
    padded = " " + text + " "
    if FORMER_NAME_RE.search(padded):
        text = FORMER_NAME_RE.sub(" ", padded).strip()
    remainder = " ".join(text.split()).strip(" ,;-/")
    extras = [f"({remainder})"] if remainder else []
    return width, extras


def width_line(width: str | None) -> str:
    """Строка ширины под названием улицы: «(38' WIDE)» или «(XX' WIDE)», если ширины нет."""
    return f"({width or PLACEHOLDER_WIDTH}' WIDE)"


def former_line(former: str | None) -> str | None:
    """Строка старого названия: «(FORMERLY CARLTON STREET)»."""
    if not former:
        return None
    return f"(FORMERLY {street_text(former)})"


def street_text(name: str | None) -> str:
    """Название улицы для чертежа: верхний регистр, между словами два пробела."""
    return "  ".join(str(name or "").upper().split())
