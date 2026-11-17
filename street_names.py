"""
Названия улиц в legal description и подписи под ними на чертеже (требования заказчика).

* Старое название улицы («formerly», «previously known as», «formerly known as», «also known as»,
  «a/k/a», «f/k/a») — в скобках или без них — отделяется от текущего названия. На чертеже оно
  выводится отдельной строкой под шириной: «(FORMERLY CARLTON STREET)».
* Под названием фасадной улицы в скобках пишется ширина: «(38' WIDE)». Ширина читается из
  описания в нескольких формах («60 feet wide», «60-foot wide», «width of 60 feet»,
  «60 feet in width», «sixty (60) feet wide»). Если ширины в описании нет, пишется
  «(XX' WIDE)» — место для ручного заполнения.
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

# Ширина улицы в описании. Числа прописью перед «feet» переводятся в цифры в llm_parser
# до разбора; «sixty (60) feet» здесь сводится к «60 feet» функцией _flat.
_WIDTH_PATTERNS = (
    # «60 feet wide», «38' WIDE», «60-foot wide», «40 FT WIDE», «60 feet-wide», «60 WIDE»
    re.compile(
        r"(?<![\d.])(?P<width>\d+(?:\.\d+)?)\s*(?:'|’|′|-)?\s*(?:feet|foot|ft\.?)?\s*-?\s*wide\b",
        re.IGNORECASE,
    ),
    # «width of 60 feet», «width is 60'», «width: 60 feet», «width 60 feet»
    re.compile(
        r"\bwidth\s*(?::|of|is|being)?\s*(?P<width>\d+(?:\.\d+)?)\s*(?:feet|foot|ft\.?|'|’|′)",
        re.IGNORECASE,
    ),
    # «60 feet in width», «60 feet of width»
    re.compile(
        r"(?<![\d.])(?P<width>\d+(?:\.\d+)?)\s*(?:feet|foot|ft\.?|'|’|′)\s+(?:in|of)\s+width\b",
        re.IGNORECASE,
    ),
)

# «SIXTY (60)» → «60»: число прописью с цифрами в скобках.
_WORD_PAREN_NUMBER_RE = re.compile(
    r"(?:[A-Z]+[\s-]+)*[A-Z]+\s*\(\s*(\d+(?:\.\d+)?)\s*\)"
)

_CLAUSE_END_RE = re.compile(r"\bTHENCE\b|;", re.IGNORECASE)


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
    """
    Старое название рядом с упоминанием улицы в полном тексте описания.

    Перебираются все упоминания: первое часто стоит в адресе владельца («52 Doty Avenue»),
    где старого названия нет.
    """
    if not street or not full_text:
        return None
    for pos in re.finditer(re.escape(street), full_text, re.IGNORECASE):
        chunk = full_text[pos.end(): pos.end() + window]
        match = FORMER_NAME_RE.search(chunk)
        if match:
            former = " ".join(match.group("name").split()).strip(" ,;")
            if former:
                return former
    return None


def _flat(text: str | None) -> str:
    """Примечание или фрагмент описания: верхний регистр, без скобок, «SIXTY (60)» → «60»."""
    upper = str(text or "").upper()
    upper = _WORD_PAREN_NUMBER_RE.sub(lambda m: f" {m.group(1)} ", upper)
    return " ".join(upper.replace("(", " ").replace(")", " ").split())


def _format_width(value: str) -> str:
    return f"{float(value):g}"


def _find_width(text: str) -> tuple[str, int, int] | None:
    """Первая по тексту ширина: (значение, начало, конец) в уже приведённом тексте."""
    best: tuple[str, int, int] | None = None
    for pattern in _WIDTH_PATTERNS:
        match = pattern.search(text)
        if match and (best is None or match.start() < best[1]):
            best = (_format_width(match.group("width")), match.start(), match.end())
    return best


def has_width(note: str | None) -> bool:
    """В примечании под улицей уже есть ширина."""
    return _find_width(_flat(note)) is not None


def find_width_text(
    full_text: str,
    street: str | None,
    window: int = 160,
    stop: str | None = None,
) -> str | None:
    """
    Ширина улицы, найденная рядом с любым упоминанием улицы, в виде «(60' WIDE)».

    Фрагмент после названия обрывается на «THENCE», «;» или на упоминании поперечной
    улицы (stop), чтобы не взять ширину соседней улицы.
    """
    if not street or not full_text:
        return None
    for pos in re.finditer(re.escape(street), full_text, re.IGNORECASE):
        chunk = full_text[pos.end(): pos.end() + window]
        ends = [len(chunk)]
        cut = _CLAUSE_END_RE.search(chunk)
        if cut:
            ends.append(cut.start())
        if stop:
            cross = re.search(re.escape(stop), chunk, re.IGNORECASE)
            if cross:
                ends.append(cross.start())
        found = _find_width(_flat(chunk[: min(ends)]))
        if found:
            return f"({found[0]}' WIDE)"
    return None


def parse_street_note(note: str | None) -> tuple[str | None, list[str]]:
    """
    Примечание под улицей → (ширина или None, доп. строки в скобках).

    «(38' WIDE ROADWAY EASEMENT)» → («38», [«(ROADWAY EASEMENT)»]);
    «(NOT MAPPED ON FINAL CITY PLAN)» → (None, [«(NOT MAPPED ON FINAL CITY PLAN)»]);
    «(SIXTY (60) FEET WIDE)» → («60», []).
    Старое название из примечания здесь не выводится: оно хранится отдельно.
    """
    text = _flat(note)
    if not text:
        return None, []
    width = None
    found = _find_width(text)
    if found:
        width, start, end = found
        text = (text[:start] + " " + text[end:]).strip()
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
