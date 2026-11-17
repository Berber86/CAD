"""Числа с ошибками OCR в тексте описания участка: буквы вместо цифр.

Примеры из реальных сканов: «1l5.15» (115.15), «lio.68» (110.68), «3l degrees» (31 degrees),
«2l4.0O feet» (214.00 feet), «B5.00» (85.00), «5l"» (51").

Правила (строгие, чтобы не испортить слова и направления):

* Меняется только токен, в котором есть буква-двойник цифры (см. ``_ALWAYS``) и который после
  замены становится числом вида ``1–5 цифр`` или ``1–5 цифр . 1–4 цифры``.
* Признак числа обязателен: десятичная точка в токене ИЛИ рядом единица измерения —
  знак ° ' " или слово degrees / minutes / seconds / feet (с ошибками OCR: «degreca», «ainutes»).
* Единица проверяет правдоподобие: градусы ≤ 360, минуты и секунды < 60.
* ``S`` меняется только между цифрами: «S56°» — это направление South, а не число.
* Нечёткого подбора слов здесь нет: слова исправляет ``llm_parser._fix_ocr_words``.

Модуль не зависит от других модулей проекта и используется и парсером, и слиянием рендеров.
"""

from __future__ import annotations

import difflib
import re

# Буквы, которые OCR принимает за цифры внутри числа (в любом месте токена).
_ALWAYS = {
    "l": "1", "I": "1", "i": "1", "|": "1", "!": "1",
    "o": "0", "O": "0",
    "B": "8", "Z": "2", "z": "2", "G": "6", "g": "9", "q": "9",
}
# «S» — только между цифрами: иначе «S56°» (South 56°) превратилось бы в «556°».
_INNER_ONLY = {"S": "5", "s": "5"}

_NUMBER_SHAPE = re.compile(r"\d{1,5}(?:\.\d{1,4})?")
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9.|!])([0-9A-Za-z|!]+(?:\.[0-9A-Za-z|!]+)?)(?![A-Za-z0-9|!]|\.\d)"
)
_AFTER_RE = re.compile(r"\s*([°'\"]?)\s*([A-Za-z]*)")

_UNIT_WORDS = {
    "degree": "deg", "degrees": "deg",
    "minute": "min", "minutes": "min",
    "second": "sec", "seconds": "sec",
    "feet": "ft", "foot": "ft", "ft": "ft",
}
# Порог сходства слова с единицей и признаки единиц по началу/концу слова: OCR портит середину
# слова («degneca», «degrcca», «aiuutes», «scconds»), но начало и конец чаще целы.
_UNIT_SIMILARITY = 0.7
_UNIT_PREFIXES = {
    "deg": ("deg", "dcg", "deq", "dgr"),
    "min": ("min", "ain", "mln"),
    "sec": ("sec", "scc", "sco", "scn"),
    "ft": ("fee", "fet", "fec", "fcet", "feo", "feot"),
}
_UNIT_SUFFIXES = {"deg": ("rees", "rces", "recs"), "min": ("utes", "uuts"), "sec": ("onds",), "ft": ("feet",)}


def _map_chars(token: str) -> str | None:
    """Переводит токен в цифры. None — если в нём есть буква, которая не цифра."""
    out: list[str] = []
    for index, char in enumerate(token):
        if char.isdigit() or char == ".":
            out.append(char)
        elif char in _ALWAYS:
            out.append(_ALWAYS[char])
        elif char in _INNER_ONLY:
            inner = 0 < index < len(token) - 1 and token[index - 1].isdigit() and token[index + 1].isdigit()
            if not inner:
                return None
            out.append(_INNER_ONLY[char])
        else:
            return None
    return "".join(out)


def number_key(token: str) -> str | None:
    """Цифровой вид токена без учёта контекста — для сравнения двух чтений одного слова.

    «llo.68» и «110.68» дают один ключ «110.68»; «thc» — None (это слово, не число).
    Токен без цифр и без точки (например, «lo») числом не считается.
    """
    mapped = _map_chars(token)
    if mapped is None or not (any(c.isdigit() for c in token) or "." in token):
        return None
    return mapped if _NUMBER_SHAPE.fullmatch(mapped) else None


def _unit_after(after: str) -> str | None:
    """Единица сразу после токена: знак (° ' ") или слово (с допуском на ошибки OCR)."""
    match = _AFTER_RE.match(after)
    if match is None:
        return None
    mark, word = match.group(1), match.group(2).lower()
    if mark == "°":
        return "deg"
    if mark == "\"":
        return "sec"
    if mark == "'":
        return "ft"  # ' — футы или минуты; проверка правдоподобия ниже учтёт оба случая
    if not word:
        return None
    if word in _UNIT_WORDS:
        return _UNIT_WORDS[word]
    if len(word) < 3:
        return None
    for unit, prefixes in _UNIT_PREFIXES.items():
        if word.startswith(prefixes) or (len(word) >= 4 and word.endswith(_UNIT_SUFFIXES[unit])):
            return unit
    best = max(_UNIT_WORDS, key=lambda known: difflib.SequenceMatcher(None, word, known).ratio())
    if difflib.SequenceMatcher(None, word, best).ratio() >= _UNIT_SIMILARITY:
        return _UNIT_WORDS[best]
    return None


def _plausible(value: str, unit: str | None) -> bool:
    number = float(value)
    if unit == "deg":
        return number <= 360
    if unit in ("min", "sec"):
        return number < 60
    return True


def _repair_token(token: str, after: str) -> str | None:
    """Исправленный токен или None, если менять нельзя."""
    if not any(char in _ALWAYS or char in _INNER_ONLY for char in token):
        return None  # цифры без букв-двойников — менять нечего
    mapped = number_key(token)
    if mapped is None:
        return None
    unit = _unit_after(after)
    has_decimal = "." in token
    leading = len(token) - len(token.lstrip("".join(_ALWAYS)))
    if not has_decimal and leading >= 2:
        # «llo68» без точки — это либо «110.68» с потерянной точкой, либо не число: не угадываем.
        # Одна ведущая буква («l2 feet» → «12 feet») — обычная путаница, её исправляем.
        return None
    if unit is None and not has_decimal:
        return None  # «1l dayof» (дата), «36o Burgher» (адрес) — не трогаем
    if not _plausible(mapped, unit):
        return None
    return mapped


def fix_text(text: str) -> str:
    """Исправляет числа с буквами-двойниками в тексте описания."""

    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        fixed = _repair_token(token, text[match.end(): match.end() + 40])
        return token if fixed is None else fixed

    return _TOKEN_RE.sub(repl, text)
