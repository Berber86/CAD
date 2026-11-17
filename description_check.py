"""
Объективная проверка разобранного описания участка (до чертежа).

Статус результата — одно слово, которое видит пользователь:

* ``ok``       — описание замыкается, все стороны с румбами и длинами, числа сходятся с текстом;
* ``warning``  — есть сомнения, но контур можно строить (небольшая невязка, нет расстояния
                 до угла в тексте, которое можно найти вручную);
* ``failed``   — контур не замыкается или данные неполные; чертёж — лучшая оценка, и его
                 надо проверить.

Функция чистая: ничего не меняет в модели, только считает признаки. Решение о том, каким
результатом пользоваться (локальным разбором или ответом ИИ), принимает
``llm_parser.parse_legal_description_full`` по статусу из этой проверки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Невязка, при которой описание считается замкнутым (футы).
CLOSURE_OK_FT = 0.5
#: Невязка, выше которой описание считается неверно прочитанным (футы).
CLOSURE_FAILED_FT = 5.0

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_FAILED = "failed"

_RANK = {STATUS_FAILED: 0, STATUS_WARNING: 1, STATUS_OK: 2}

#: «25.41 feet U.S. Standard (25.00 feet Deed)» — измеренная длина и длина по документу.
_STANDARD_DEED_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:feet|ft|')?\s*U\.?\s*S\.?\s*Standard\s*\(\s*(\d+(?:\.\d+)?)\s*\)?\s*(?:feet|ft|')?\s*Deed",
    re.IGNORECASE,
)
_DISTANT_RE = re.compile(r"\bdistant\s+\d[\d,]*(?:\.\d+)?\s*(?:feet|ft|')", re.IGNORECASE)
_THENCE_RE = re.compile(r"\bTHENCE\b", re.IGNORECASE)


@dataclass(frozen=True)
class CheckIssue:
    status: str  # STATUS_WARNING или STATUS_FAILED
    text: str
    side: int | None = None  # номер стороны (1-based), если проблема относится к стороне


@dataclass
class DescriptionCheck:
    status: str = STATUS_OK
    closure_ft: float | None = None
    issues: list[CheckIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def add(self, status: str, text: str, side: int | None = None) -> None:
        self.issues.append(CheckIssue(status=status, text=text, side=side))
        if _RANK[status] < _RANK[self.status]:
            self.status = status

    def warning_lines(self) -> list[str]:
        """Строки для журнала и сводки: первая — итог, затем конкретные места."""
        if self.status == STATUS_OK:
            return []
        head = {
            STATUS_FAILED: "✗ проверка описания: НЕ СХОДИТСЯ — чертёж нужно проверить",
            STATUS_WARNING: "⚠ проверка описания: есть сомнения",
        }[self.status]
        return [head] + [f"  • {issue.text}" for issue in self.issues]


def standard_deed_pairs(text: str) -> list[tuple[float, float]]:
    """Пары (измеренная, по документу) в порядке текста: «25.41 U.S. Standard (25.00 Deed)»."""
    return [(float(m.group(1)), float(m.group(2))) for m in _STANDARD_DEED_RE.finditer(text or "")]


def closure_with_deed_values(model: Any, text: str) -> tuple[float, int, float] | None:
    """
    Арбитр замыкания: если измеренные длины не замыкаются, а вариант «по Deed» — да.

    Возвращает (невязка_измеренная, номер_стороны, длина_по_Deed) для первой пары, при замене
    которой контур замыкается ≤ CLOSURE_OK_FT. Геометрию не меняет: решение за чертёжником.
    None — арбитр не нужен или не помогает.
    """
    from acad_builder import build_polygon_points, closure_error  # noqa: PLC0415

    segments = list(getattr(model, "segments", None) or ())
    pairs = standard_deed_pairs(text)
    if len(segments) < 3 or not pairs:
        return None
    if any(not getattr(seg, "has_bearing", True) for seg in segments):
        return None
    base = float(closure_error(build_polygon_points(segments)).distance)
    if base <= CLOSURE_OK_FT:
        return None
    for measured, deed in pairs:
        if abs(deed - measured) < 1e-6:
            continue
        for index, seg in enumerate(segments):
            if abs(float(getattr(seg, "distance_ft", 0.0) or 0.0) - measured) > 0.006:
                continue
            trial = list(segments)
            trial[index] = seg.model_copy(update={"distance_ft": deed})
            if float(closure_error(build_polygon_points(trial)).distance) <= CLOSURE_OK_FT:
                return base, index + 1, deed
    return None


def ranks_higher(a: str, b: str) -> bool:
    """Статус ``a`` лучше статуса ``b``."""
    return _RANK[a] > _RANK[b]


def check_description(model: Any, source_text: str = "") -> DescriptionCheck:
    """Проверяет разобранную модель LegalDescription относительно исходного текста."""
    # Импорт здесь: acad_builder тянет llm_parser, а llm_parser импортирует эту проверку.
    from acad_builder import build_polygon_points, closure_error

    check = DescriptionCheck()
    segments = list(getattr(model, "segments", None) or ())
    text = str(source_text or "")

    if len(segments) < 3:
        check.add(STATUS_FAILED, f"найдено сторон: {len(segments)} — участку нужно не меньше трёх")
        return check

    # Сторона без румба: если и заметка о ней («along the easterly line …») не нашлась,
    # это провал. Если заметка есть — сторона описана словами, но контур без румба не
    # замкнуть: это сомнение (warning), а не «ok».
    unbearing: list[int] = []
    for index, seg in enumerate(segments, start=1):
        if not getattr(seg, "has_bearing", True):
            unbearing.append(index)
            if not getattr(seg, "course_note", None):
                check.add(STATUS_FAILED, f"сторона №{index}: румб не прочитан", side=index)
    if unbearing and check.status == STATUS_OK:
        check.add(
            STATUS_WARNING,
            "сторона(ы) без румба, описаны словами: контур не проверен — "
            + ", ".join(f"№{index}" for index in unbearing),
        )

    # Замыкание считается только если у всех сторон есть румб. Поэтому статус «ok»
    # возможен только с вычисленным замыканием: пропущенная или нечитаемая сторона
    # не может пройти как «ok».
    if not unbearing:
        closure = closure_error(build_polygon_points(segments))
        check.closure_ft = float(closure.distance)
        if closure.distance > CLOSURE_FAILED_FT:
            check.add(
                STATUS_FAILED,
                f"описание не замыкается: невязка {closure.distance:.2f} ft",
            )
        elif closure.distance > CLOSURE_OK_FT:
            check.add(
                STATUS_WARNING,
                f"невязка {closure.distance:.2f} ft — проверьте длины сторон",
            )

    if text:
        arbiter = closure_with_deed_values(model, text)
        if arbiter is not None:
            base, side_no, deed = arbiter
            check.add(
                STATUS_WARNING,
                f"сторона №{side_no}: по документу (Deed) {deed:.2f} ft, а измеренное значение "
                f"не даёт замыкания (невязка {base:.2f} ft); с Deed контур замыкается — проверьте",
                side=side_no,
            )
        thence = len(_THENCE_RE.findall(text))
        if thence and thence != len(segments) and thence != len(segments) + 1:
            check.add(
                STATUS_WARNING,
                f"в тексте {thence} «THENCE», разобрано сторон: {len(segments)}",
            )
        distance = float(getattr(model, "distance_to_corner_ft", None) or 0.0)
        if _DISTANT_RE.search(text) and distance <= 1e-6:
            check.add(STATUS_FAILED, "в тексте есть «distant … feet», но расстояние до угла не найдено")

    return check
