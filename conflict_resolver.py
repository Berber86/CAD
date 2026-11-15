"""
Разбор конфликтов между участками набора: чего не хватает в одном описании
и что можно взять из описания соседа.

Работа идёт в два этапа.

1. **Правила на Python.** Программа сама находит пробелы и расхождения и
   пробует закрыть их данными соседних описаний:

   * курс вдоль улицы задан только словом («southerly along Alvine Avenue
     40 feet»), а в описании соседа линия той же улицы имеет румб;
   * румб такого курса вычисляется как замыкающий по остальным сторонам —
     это независимая проверка, а без соседа ещё и запасной источник;
   * контур не замыкается, а та же общая граница у соседа записана иначе, и
     со значением соседа контур замыкается.

2. **ИИ-советник.** Если после находок Python конфликт остался, нейросети
   отправляются данные участков, тексты или страницы документов и акт
   сверки. Она объясняет причину и говорит, что попробовать.

Каждая находка — и правила, и советника — проверяется геометрией: невязка
участка, наложение на соседей, стыковка общих границ. Что не улучшает
схему, отбрасывается. Данные участка меняются только после подтверждения
пользователем: здесь правки предлагаются, а не применяются.

Окна в модуле нет — им пользуется ``main.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from acad_builder import (
    AcadBuilder,
    DrawPlan,
    NeighborVerificationReport,
    _normalize_street_key,
    polygon_intersection_area,
    verify_with_neighbors,
)
from llm_parser import LegalDescription, Segment, bearing_label
from pdf_set import CLOSURE_LIMIT_FT

#: Слово «southerly» допускает такой отход настоящего румба от стороны света.
WORD_DIRECTION_TOLERANCE_DEG = 50.0
#: Замыкающий курс подтверждает сторону, если его длина совпала с документом.
CLOSING_LENGTH_TOLERANCE_FT = 0.10
#: Наложение, с которого соседние участки без нашего считаются конфликтующими.
HARD_OVERLAP_SQFT = 6.0

Components = tuple[str, int, int, float, str]


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """Одна правка данных участка: румб или длина стороны либо румб привязки к углу."""

    target: str  # "side" | "tie"
    index: int  # номер стороны с нуля; для привязки -1
    field: str  # "bearing" | "distance"
    ns: str = "N"
    degrees: int = 0
    minutes: int = 0
    seconds: float = 0.0
    ew: str = "E"
    distance_ft: float = 0.0
    old_label: str = ""
    new_label: str = ""

    @property
    def slot(self) -> tuple[str, int, str]:
        return (self.target, self.index, self.field)

    @property
    def where(self) -> str:
        return "привязка к углу" if self.target == "tie" else f"сторона #{self.index + 1}"

    def describe(self) -> str:
        what = "румб" if self.field == "bearing" else "длина"
        return f"{self.where}: {what} {self.old_label} → {self.new_label}"


@dataclass(frozen=True)
class Metrics:
    """Чем измеряется польза правки для одного участка."""

    closure_ft: float
    overlap_sqft: float
    edge_errors: int
    edge_warnings: int


@dataclass(frozen=True)
class Proposal:
    """Находка: что поменять в данных участка, откуда значение и чем это подтверждено."""

    id: str
    lot_key: str
    lot_name: str
    kind: str  # "street_bearing" | "closing_course" | "neighbor_value" | "advisor"
    origin: str  # "rule" — правило Python, "ai" — ИИ-советник
    title: str
    changes: tuple[Change, ...]
    source: str
    reasons: tuple[str, ...]
    before: Metrics
    after: Metrics
    verdict: str  # "verified" | "partial"
    #: Находки, вместе с которыми эта проверялась (применяются заодно).
    requires: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.verdict == "verified"

    def verdict_label(self) -> str:
        return "подтверждено геометрией" if self.verified else "подтверждено частично"

    def metrics_line(self) -> str:
        parts = [f"невязка {self.before.closure_ft:.3f} → {self.after.closure_ft:.3f} ft"]
        if max(self.before.overlap_sqft, self.after.overlap_sqft) > 0.5:
            parts.append(
                f"наложение на соседние участки {self.before.overlap_sqft:,.1f} → "
                f"{self.after.overlap_sqft:,.1f} sq ft"
            )
        return " · ".join(parts)


@dataclass
class LotInput:
    """Участок набора в том виде, в каком он пришёл из разбора."""

    key: str
    name: str
    is_main: bool
    description: LegalDescription
    #: Правки, уже принятые пользователем.
    fixes: tuple[Proposal, ...] = ()
    source_text: str = ""
    images: tuple[str, ...] = ()
    used_ai: bool = False

    @property
    def title(self) -> str:
        return f"{'наш участок' if self.is_main else 'сосед'} «{self.name}»"


@dataclass
class AdvisorReply:
    """Ответ ИИ-советника, как он получен (до проверки геометрией)."""

    analysis: str = ""
    explanations: list[tuple[str, str]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    usage: Any = None


@dataclass
class ConflictAnalysis:
    """Итог разбора конфликтов набора."""

    proposals: list[Proposal] = field(default_factory=list)
    applied: list[Proposal] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    remaining_hard: bool = False
    notes: list[str] = field(default_factory=list)
    #: "" — советник не понадобился; "ok" — ответил сейчас; "cached" — ответ уже был;
    #: "skipped" — не запрошен (причина в advisor_note); "error" — запрос не удался.
    advisor_status: str = ""
    advisor_note: str = ""
    advisor_reply: AdvisorReply | None = None
    advisor_rejected: list[str] = field(default_factory=list)
    advisor_key: str = ""
    error: str = ""

    def proposal(self, proposal_id: str) -> Proposal | None:
        return next((p for p in self.proposals if p.id == proposal_id), None)

    def with_requirements(self, proposal_id: str) -> list[Proposal]:
        """Находка вместе с теми, на которые она опирается, в порядке применения."""
        wanted = self.proposal(proposal_id)
        if wanted is None:
            return []
        chain = [p for pid in wanted.requires if (p := self.proposal(pid)) is not None]
        return chain + [wanted]

    def number(self, proposal: Proposal) -> int:
        return self.proposals.index(proposal) + 1

    @property
    def has_content(self) -> bool:
        return bool(
            self.proposals or self.applied or self.remaining or self.advisor_reply
            or self.advisor_rejected or self.error
            or (self.advisor_status in {"skipped", "error"} and self.advisor_note)
        )

    def headline(self) -> str:
        verified = sum(1 for p in self.proposals if p.verified)
        if self.proposals:
            text = f"Находок по данным соседей: {len(self.proposals)}"
            if verified != len(self.proposals):
                text += f" (подтверждено геометрией: {verified})"
            return text + " — ждут вашего решения"
        if self.applied:
            return f"Принятых правок по данным соседей: {len(self.applied)}"
        return ""

    def format_report(self) -> str:
        """Текст раздела для итога набора."""
        lines: list[str] = []
        if self.error:
            lines.append(f"Анализ не выполнен: {self.error}")
            return "\n".join(lines)
        if self.proposals:
            lines.append(
                "Программа сверила описания между собой и нашла, чем закрыть пробелы. "
                "Данные участков НЕ изменены: каждая находка применяется кнопкой «Применить»."
            )
            for proposal in self.proposals:
                mark = "✔" if proposal.verified else "◐"
                who = "ИИ-советник" if proposal.origin == "ai" else "правило Python"
                lines.append("")
                lines.append(f"[{self.number(proposal)}] {mark} {proposal.title}")
                lines.append(f"    Участок: «{proposal.lot_name}» · {who} · {proposal.verdict_label()}")
                lines.append(f"    Откуда значение: {proposal.source}")
                for change in proposal.changes:
                    lines.append(f"    Правка: {change.describe()}")
                lines.append(f"    Проверка: {proposal.metrics_line()}")
                for reason in proposal.reasons:
                    lines.append(f"    • {reason}")
                if proposal.requires:
                    numbers = ", ".join(
                        f"[{self.number(p)}]" for pid in proposal.requires
                        if (p := self.proposal(pid)) is not None
                    )
                    if numbers:
                        lines.append(f"    Применяется вместе с находкой {numbers}.")
        if self.applied:
            if lines:
                lines.append("")
            lines.append("Принятые правки (участки построены с ними; каждую можно вернуть):")
            for fix in self.applied:
                lines.append(f"  ✎ «{fix.lot_name}»: {fix.title}")
                lines.append(f"      откуда: {fix.source}")
        if self.remaining:
            if lines:
                lines.append("")
            lines.append(
                "После находок остаётся:" if self.proposals else "Расхождения, которые остаются:"
            )
            for text in self.remaining:
                lines.append(f"  • {text}")
        for note in self.notes:
            lines.append(f"  ℹ {note}")
        reply = self.advisor_reply
        if reply is not None:
            lines.append("")
            tag = f" ({reply.model})" if reply.model else ""
            when = "" if self.advisor_status == "ok" else " — получено ранее для этого же разбора"
            lines.append(f"ИИ-СОВЕТНИК{tag}{when}:")
            if reply.analysis:
                lines.append(f"  {reply.analysis}")
            for conflict, cause in reply.explanations:
                lines.append(f"  • {conflict}: {cause}" if conflict else f"  • {cause}")
            for text in self.advisor_rejected:
                lines.append(f"  ✗ {text}")
            if not (reply.analysis or reply.explanations or self.advisor_rejected):
                lines.append("  Советник не нашёл, что добавить.")
        elif self.advisor_status in {"skipped", "error"} and self.advisor_note:
            lines.append("")
            lines.append(f"ИИ-советник: {self.advisor_note}")
        if not lines:
            lines.append("Пробелов, которые можно закрыть данными соседей, не найдено.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Румбы
# ---------------------------------------------------------------------------


def _dir_diff(a: float, b: float) -> float:
    """Разница направлений, 0…180°."""
    diff = abs(a - b) % 360.0
    return min(diff, 360.0 - diff)


def _axis_diff(a: float, b: float) -> float:
    """Разница прямых без учёта направления, 0…90°."""
    diff = _dir_diff(a, b)
    return min(diff, 180.0 - diff)


def _azimuth(parts: Components) -> float:
    ns, degrees, minutes, seconds, ew = parts
    angle = degrees + minutes / 60.0 + seconds / 3600.0
    if ns == "N":
        return angle % 360.0 if ew == "E" else (360.0 - angle) % 360.0
    return 180.0 - angle if ew == "E" else 180.0 + angle


def _components(azimuth_deg: float, *, whole_seconds: bool = False) -> Components:
    """Азимут → буквы и градусы румба."""
    az = azimuth_deg % 360.0
    if az <= 90.0:
        ns, ew, angle = "N", "E", az
    elif az <= 180.0:
        ns, ew, angle = "S", "E", 180.0 - az
    elif az < 270.0:
        ns, ew, angle = "S", "W", az - 180.0
    else:
        ns, ew, angle = "N", "W", 360.0 - az
    total = round(angle * 3600.0, 0 if whole_seconds else 2)
    degrees = int(total // 3600)
    minutes = int((total - degrees * 3600) // 60)
    seconds = round(total - degrees * 3600 - minutes * 60, 2)
    return ns, degrees, minutes, seconds, ew


def _of(seg: Segment, *, flip: bool = False) -> Components:
    if not flip:
        return seg.ns, seg.degrees, seg.minutes, seg.seconds, seg.ew
    return ("S" if seg.ns == "N" else "N"), seg.degrees, seg.minutes, seg.seconds, ("W" if seg.ew == "E" else "E")


def _toward(parts: Components, azimuth_deg: float) -> Components:
    """Тот же румб, направленный в сторону ``azimuth_deg`` (сам или развёрнутый на 180°)."""
    ns, degrees, minutes, seconds, ew = parts
    flipped: Components = ("S" if ns == "N" else "N", degrees, minutes, seconds, "W" if ew == "E" else "E")
    return parts if _dir_diff(_azimuth(parts), azimuth_deg) <= _dir_diff(_azimuth(flipped), azimuth_deg) else flipped


def _label(parts: Components) -> str:
    return bearing_label(*parts)


def _arcsec(value_deg: float) -> str:
    seconds = abs(value_deg) * 3600.0
    if seconds < 60:
        return f"{seconds:.0f}″"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes:.0f}′{rest:02.0f}″"
    degrees, minutes = divmod(minutes, 60)
    return f"{degrees:.0f}°{minutes:02.0f}′"


def parse_bearing_text(value: Any) -> Components | None:
    """Румб из свободной записи: «S 22°26'12" E», «South 22 degrees 26 minutes 12 seconds East»."""
    if isinstance(value, dict):
        try:
            seg = Segment(
                ns=value.get("ns"), degrees=value.get("degrees") or 0, minutes=value.get("minutes") or 0,
                seconds=value.get("seconds") or 0, ew=value.get("ew"), distance_ft=1.0,
            )
        except Exception:  # noqa: BLE001
            return None
        return _of(seg)
    text = str(value or "").strip().upper()
    match = re.match(r"^(N|S)(?:ORTH|OUTH)?\.?\s*(.*?)\s*(E|W)(?:AST|EST)?\.?$", text, re.DOTALL)
    if not match:
        return None
    numbers = re.findall(r"\d+(?:\.\d+)?", match.group(2))
    if not 1 <= len(numbers) <= 3:
        return None
    numbers += ["0"] * (3 - len(numbers))
    try:
        seg = Segment(
            ns=match.group(1), degrees=numbers[0], minutes=numbers[1], seconds=numbers[2],
            ew=match.group(3), distance_ft=1.0,
        )
    except Exception:  # noqa: BLE001
        return None
    return _of(seg)


def _bearing_change(target: str, index: int, old: Segment, parts: Components) -> Change:
    old_label = old.bearing_label or _label(_of(old))
    if not old.has_bearing:
        old_label += " (в документе румба нет)"
    return Change(
        target=target, index=index, field="bearing",
        ns=parts[0], degrees=parts[1], minutes=parts[2], seconds=parts[3], ew=parts[4],
        old_label=old_label, new_label=_label(parts),
    )


def apply_changes(description: LegalDescription, changes: Sequence[Change]) -> LegalDescription:
    """Данные участка с правками. Исходное описание не меняется."""
    if not changes:
        return description
    data = description.model_dump()
    for change in changes:
        if change.target == "tie":
            node = data.get("corner_to_pob_bearing")
            if not node:
                continue
        elif 0 <= change.index < len(data["segments"]):
            node = data["segments"][change.index]
        else:
            continue
        if change.field == "bearing":
            node.update(
                ns=change.ns, degrees=change.degrees, minutes=change.minutes,
                seconds=change.seconds, ew=change.ew, has_bearing=True,
            )
            node.pop("bearing_label", None)
        elif change.target == "side":
            node["distance_ft"] = change.distance_ft
    return LegalDescription.model_validate(data)


def fix_changes(fixes: Sequence[Proposal]) -> tuple[Change, ...]:
    return tuple(change for fix in fixes for change in fix.changes)


# ---------------------------------------------------------------------------
# Схема набора: участки с правками и их показатели
# ---------------------------------------------------------------------------


class _Scene:
    """Строит участки набора с пробными правками и меряет результат."""

    def __init__(self, builder: AcadBuilder, lots: Sequence[LotInput], scale: float) -> None:
        self.builder = builder
        self.lots = list(lots)
        self.scale = scale
        self._descriptions: dict[tuple[str, tuple[Change, ...]], LegalDescription | None] = {}
        self._plans: dict[tuple[str, tuple[Change, ...]], DrawPlan | None] = {}
        #: Подтверждённые находки, принятые «в уме» (после них считается остаток).
        self.virtual: dict[str, tuple[Change, ...]] = {}

    def description(self, lot: LotInput, extra: Sequence[Change] = ()) -> LegalDescription | None:
        key = (lot.key, tuple(extra))
        if key not in self._descriptions:
            try:
                self._descriptions[key] = apply_changes(lot.description, fix_changes(lot.fixes) + tuple(extra))
            except Exception:  # noqa: BLE001 - пробная правка дала недопустимые данные
                self._descriptions[key] = None
        return self._descriptions[key]

    def plan(self, lot: LotInput, extra: Sequence[Change] = ()) -> DrawPlan | None:
        key = (lot.key, tuple(extra))
        if key not in self._plans:
            desc = self.description(lot, extra)
            try:
                self._plans[key] = (
                    None if desc is None
                    else self.builder.plan(desc, scale=self.scale, is_neighbor=not lot.is_main)
                )
            except Exception:  # noqa: BLE001 - участок с такой правкой не строится
                self._plans[key] = None
        return self._plans[key]

    def _others(self, lot: LotInput, state: dict[str, tuple[Change, ...]]) -> list[tuple[LotInput, DrawPlan]]:
        found = []
        for other in self.lots:
            if other is lot:
                continue
            plan = self.plan(other, state.get(other.key, ()))
            if plan is not None:
                found.append((other, plan))
        return found

    def metrics(
        self, lot: LotInput, extra: Sequence[Change] = (), *, state: dict[str, tuple[Change, ...]] | None = None,
    ) -> Metrics | None:
        plan = self.plan(lot, extra)
        if plan is None:
            return None
        others = [p for _, p in self._others(lot, state or {})]
        overlap, errors, warnings = 0.0, 0, 0
        if others:
            report = verify_with_neighbors(plan, others)
            overlap = sum(pair.overlap_area_sqft for pair in report.pair_reports)
            errors, warnings = report.error_count, report.warning_count
        return Metrics(plan.closure.distance, overlap, errors, warnings)


def judge(before: Metrics | None, after: Metrics | None) -> str:
    """
    Оценка пробной правки: ``verified`` — контур замыкается и хуже не стало,
    ``partial`` — стало заметно лучше, но не всё сошлось, ``rejected`` — пользы нет.
    """
    if before is None or after is None:
        return "rejected"
    closure_ok = after.closure_ft <= CLOSURE_LIMIT_FT
    closure_gain = before.closure_ft - after.closure_ft
    closure_better = closure_gain > max(0.02, 0.5 * before.closure_ft)
    closure_worse = closure_gain < -0.02 and not closure_ok
    overlap_gain = before.overlap_sqft - after.overlap_sqft
    overlap_better = overlap_gain > max(1.0, 0.5 * before.overlap_sqft)
    overlap_worse = overlap_gain < -max(1.0, 0.05 * before.overlap_sqft)
    edges_better = after.edge_errors < before.edge_errors and not overlap_worse and closure_gain > -0.02
    if closure_worse or not (closure_better or overlap_better or edges_better):
        return "rejected"
    if overlap_worse and not closure_better:
        return "rejected"
    return "verified" if closure_ok and not overlap_worse else "partial"


def _proposal_id(lot: LotInput, changes: Sequence[Change]) -> str:
    raw = lot.key + "|" + "|".join(
        f"{c.target}:{c.index}:{c.field}:{c.new_label}:{c.distance_ft:.4f}" for c in changes
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _pob_reason(scene: _Scene, lot: LotInput, changes: Sequence[Change]) -> str | None:
    old, new = scene.plan(lot), scene.plan(lot, changes)
    if old is None or new is None or math.hypot(old.pob[0] - new.pob[0], old.pob[1] - new.pob[1]) < 0.01:
        return None
    return (
        "привязка к углу идёт по той же линии улицы, поэтому точка начала (POB) переносится: "
        f"({old.pob[0]:.2f}, {old.pob[1]:.2f}) → ({new.pob[0]:.2f}, {new.pob[1]:.2f})"
    )


# ---------------------------------------------------------------------------
# Правило 1: румб курса вдоль улицы — из описания соседа или из замыкания
# ---------------------------------------------------------------------------


def _mentions(note_key: str, street_key: str) -> bool:
    return bool(note_key and street_key) and f" {street_key} " in f" {note_key} "


def _street_lines(desc: LegalDescription) -> list[tuple[str, str, Segment, str]]:
    """Где в описании напечатан румб линии улицы: (ключ улицы, улица, румб, где записан)."""
    lines: list[tuple[str, str, Segment, str]] = []
    seen: set[tuple[str, int]] = set()
    main_key = _normalize_street_key(desc.main_street)
    cross_key = _normalize_street_key(desc.cross_street)

    def add(key: str, name: str | None, index: int) -> None:
        if not key or (key, index) in seen or not 0 <= index < len(desc.segments):
            return
        seg = desc.segments[index]
        if seg.has_bearing:
            seen.add((key, index))
            lines.append((key, name or "", seg, f"сторона #{index + 1}"))

    if desc.front_segment_index is not None:
        add(main_key, desc.main_street, desc.front_segment_index)
    if desc.cross_street_side_index is not None:
        add(cross_key, desc.cross_street, desc.cross_street_side_index)
    for index, seg in enumerate(desc.segments):
        note_key = _normalize_street_key(seg.course_note)
        if _mentions(note_key, main_key):
            add(main_key, desc.main_street, index)
        if _mentions(note_key, cross_key):
            add(cross_key, desc.cross_street, index)
    tie = desc.corner_to_pob_bearing
    if main_key and tie is not None and tie.has_bearing and desc.distance_to_corner_ft > 1e-6:
        lines.append((main_key, desc.main_street or "", tie, "привязка к углу"))
    return lines


def _gap_street_keys(desc: LegalDescription, index: int) -> set[str]:
    """Вдоль каких улиц идёт курс без румба (по его ремарке или по роли стороны)."""
    seg = desc.segments[index]
    note_key = _normalize_street_key(seg.course_note)
    main_key = _normalize_street_key(desc.main_street)
    cross_key = _normalize_street_key(desc.cross_street)
    keys = {key for key in (main_key, cross_key) if _mentions(note_key, key)}
    if not keys:
        if desc.front_segment_index == index and main_key:
            keys.add(main_key)
        elif desc.cross_street_side_index == index and cross_key:
            keys.add(cross_key)
    return keys


def _linked_tie_change(desc: LegalDescription, index: int, parts: Components) -> Change | None:
    """
    Привязка к углу лежит на той же линии улицы, что и курс без румба:
    новый румб линии получает и она.
    """
    tie = desc.corner_to_pob_bearing
    seg = desc.segments[index]
    if tie is None or desc.distance_to_corner_ft <= 1e-6:
        return None
    if _axis_diff(tie.azimuth_deg, seg.azimuth_deg) > 0.5:
        return None
    main_key = _normalize_street_key(desc.main_street)
    if tie.has_bearing and main_key not in _gap_street_keys(desc, index):
        return None
    turned = _toward(parts, tie.azimuth_deg)
    if tie.has_bearing and _label(turned) == _label(_of(tie)):
        return None
    return _bearing_change("tie", -1, tie, turned)


def closing_course(desc: LegalDescription, index: int) -> tuple[float, float]:
    """Азимут и длина стороны ``index``, при которых контур замыкается по остальным сторонам."""
    sum_x = sum(seg.vector[0] for i, seg in enumerate(desc.segments) if i != index)
    sum_y = sum(seg.vector[1] for i, seg in enumerate(desc.segments) if i != index)
    return math.degrees(math.atan2(-sum_x, -sum_y)) % 360.0, math.hypot(sum_x, sum_y)


def _street_bearing_proposals(scene: _Scene, lot: LotInput) -> list[Proposal]:
    desc = scene.description(lot)
    if desc is None:
        return []
    gaps = [i for i, seg in enumerate(desc.segments) if not seg.has_bearing]
    if not gaps or len(gaps) == len(desc.segments):
        # Пробелов нет либо румбов нет вовсе (описание целиком «от улиц») — это другая задача.
        return []
    before = scene.metrics(lot)
    if before is None:
        return []
    found: list[Proposal] = []
    for index in gaps:
        seg = desc.segments[index]
        note = " ".join((seg.course_note or "").split())
        side = f"Сторона #{index + 1}" + (f" («{note}»)" if note else "")
        relative = bool(re.search(r"right\s+angles?|perpendicular", note, re.IGNORECASE))
        closing = closing_course(desc, index) if len(gaps) == 1 else None
        closing_ok = (
            closing is not None
            and abs(closing[1] - seg.distance_ft) <= CLOSING_LENGTH_TOLERANCE_FT
            and _dir_diff(closing[0], seg.azimuth_deg) <= WORD_DIRECTION_TOLERANCE_DEG
        )
        no_bearing = (
            f"в описании «{lot.name}» у этой стороны нет румба — только направление словом, "
            f"поэтому она стояла по {seg.bearing_label}"
        )

        # Румб той же линии улицы в описаниях соседей.
        groups: dict[int, dict[str, Any]] = {}
        own_keys = _gap_street_keys(desc, index)
        note_key = _normalize_street_key(seg.course_note)
        if not relative:
            for donor in scene.lots:
                if donor is lot:
                    continue
                donor_desc = scene.description(donor)
                if donor_desc is None:
                    continue
                for key, street, donor_seg, ref in _street_lines(donor_desc):
                    if key not in own_keys and not _mentions(note_key, key):
                        continue
                    parts = _toward(_of(donor_seg), seg.azimuth_deg)
                    if _dir_diff(_azimuth(parts), seg.azimuth_deg) > WORD_DIRECTION_TOLERANCE_DEG:
                        continue
                    group = groups.setdefault(
                        round(_azimuth(parts) * 3600.0), {"parts": parts, "street": street, "refs": []},
                    )
                    group["refs"].append((donor, f"{ref} — {donor_seg.bearing_label}"))
        verified_from_neighbor = False
        for group in groups.values():
            parts: Components = group["parts"]
            changes = [_bearing_change("side", index, seg, parts)]
            tie_change = _linked_tie_change(desc, index, parts)
            if tie_change is not None:
                changes.append(tie_change)
            after = scene.metrics(lot, changes)
            verdict = judge(before, after)
            if verdict == "rejected" or after is None:
                continue
            by_donor: dict[str, list[str]] = {}
            for donor, ref in group["refs"]:
                by_donor.setdefault(donor.name, []).append(ref)
            source = "; ".join(f"«{name}»: {', '.join(refs)}" for name, refs in by_donor.items())
            street = group["street"] or "улицы"
            reasons = [no_bearing, f"в описании соседа линия «{street}» имеет румб {_label(parts)}"]
            if closing is not None:
                delta = _dir_diff(closing[0], _azimuth(parts))
                reasons.append(
                    "независимая проверка без соседа: замыкающий курс по остальным сторонам "
                    f"этого же описания — {_label(_components(closing[0], whole_seconds=True))}, "
                    f"{closing[1]:.3f} ft (в документе {seg.distance_ft:.2f} ft); "
                    f"с румбом соседа расходится на {_arcsec(delta)}"
                )
            pob = _pob_reason(scene, lot, changes) if tie_change is not None else None
            if pob:
                reasons.append(pob)
            names = ", ".join(f"«{name}»" for name in by_donor)
            found.append(Proposal(
                id=_proposal_id(lot, changes), lot_key=lot.key, lot_name=lot.name,
                kind="street_bearing", origin="rule",
                title=f"{side}: румб {_label(parts)} — из описания {names}",
                changes=tuple(changes), source=source, reasons=tuple(reasons),
                before=before, after=after, verdict=verdict,
            ))
            verified_from_neighbor = verified_from_neighbor or verdict == "verified"

        # Запасной источник: румб из замыкания самого контура.
        if closing_ok and not verified_from_neighbor and closing is not None:
            parts = _components(closing[0], whole_seconds=True)
            changes = [_bearing_change("side", index, seg, parts)]
            tie_change = _linked_tie_change(desc, index, parts)
            if tie_change is not None:
                changes.append(tie_change)
            after = scene.metrics(lot, changes)
            verdict = judge(before, after)
            if verdict != "rejected" and after is not None:
                why = (
                    "румб этой линии из описаний соседей не подтверждается замыканием контура"
                    if groups else "в описаниях соседей румб этой линии не найден"
                )
                reasons = [
                    no_bearing,
                    f"{why}, поэтому он вычислен: это единственный румб, при котором контур "
                    "замыкается по остальным сторонам",
                    f"длина замыкающей стороны получается {closing[1]:.3f} ft при {seg.distance_ft:.2f} ft "
                    "в документе — совпадение подтверждает остальные румбы и длины",
                    "румба нет ни в одном документе: значение расчётное, сверьте его с планом или картой",
                ]
                pob = _pob_reason(scene, lot, changes) if tie_change is not None else None
                if pob:
                    reasons.append(pob)
                found.append(Proposal(
                    id=_proposal_id(lot, changes), lot_key=lot.key, lot_name=lot.name,
                    kind="closing_course", origin="rule",
                    title=f"{side}: румб {_label(parts)} — вычислен из замыкания контура",
                    changes=tuple(changes), source="расчёт по остальным сторонам этого же описания",
                    reasons=tuple(reasons), before=before, after=after, verdict=verdict,
                ))
    return found


# ---------------------------------------------------------------------------
# Правило 2: та же граница у соседа записана иначе, и с его значением контур замыкается
# ---------------------------------------------------------------------------


def _neighbor_value_proposals(scene: _Scene, lot: LotInput) -> list[Proposal]:
    plan = scene.plan(lot)
    desc = scene.description(lot)
    if plan is None or desc is None or plan.closure.distance <= CLOSURE_LIMIT_FT:
        return []
    before = scene.metrics(lot)
    if before is None:
        return []
    found: dict[str, Proposal] = {}
    for donor, donor_plan in scene._others(lot, {}):
        donor_desc = scene.description(donor)
        if donor_desc is None:
            continue
        pair = verify_with_neighbors(plan, [donor_plan]).pair_reports[0]
        for edge in pair.shared_edges:
            own_i, donor_i = edge.main_seg_index - 1, edge.neighbor_seg_index - 1
            if edge.status == "ok" or not (
                0 <= own_i < len(desc.segments) and 0 <= donor_i < len(donor_desc.segments)
            ):
                continue
            own, theirs = desc.segments[own_i], donor_desc.segments[donor_i]
            options: list[tuple[Change, str, str]] = []
            if abs(own.distance_ft - theirs.distance_ft) > 0.015:
                options.append((
                    Change(
                        target="side", index=own_i, field="distance", distance_ft=theirs.distance_ft,
                        old_label=f"{own.distance_ft:.2f}'", new_label=f"{theirs.distance_ft:.2f}'",
                    ),
                    f"длина {theirs.distance_ft:.2f}'", f"{theirs.distance_ft:.2f}'",
                ))
            if own.has_bearing and theirs.has_bearing and edge.angle_diff_arcsec > 1.0:
                parts = _toward(_of(theirs), own.azimuth_deg)
                options.append((
                    _bearing_change("side", own_i, own, parts), f"румб {_label(parts)}", theirs.bearing_label or "",
                ))
            for change, what, donor_value in options:
                after = scene.metrics(lot, [change])
                if judge(before, after) != "verified" or after is None:
                    continue
                proposal = Proposal(
                    id=_proposal_id(lot, [change]), lot_key=lot.key, lot_name=lot.name,
                    kind="neighbor_value", origin="rule",
                    title=f"Сторона #{own_i + 1}: {what} — как у той же границы в описании «{donor.name}»",
                    changes=(change,),
                    source=f"«{donor.name}»: сторона #{donor_i + 1} — {donor_value}",
                    reasons=(
                        f"это общая граница: в описании «{lot.name}» записано {change.old_label}, "
                        f"у соседа — {donor_value}",
                        "со значением соседа контур замыкается, с напечатанным — нет: похоже на "
                        "опечатку или неверно прочитанный знак, сверьте с документом",
                    ),
                    before=before, after=after, verdict="verified",
                )
                found.setdefault(proposal.id, proposal)
    return list(found.values())


# ---------------------------------------------------------------------------
# Что сейчас конфликтует
# ---------------------------------------------------------------------------


def _conflicts(
    scene: _Scene, state: dict[str, tuple[Change, ...]],
) -> tuple[list[str], bool, NeighborVerificationReport | None]:
    """Список расхождений набора: (строки, есть ли серьёзные, акт сверки нашего участка)."""
    lines: list[str] = []
    hard = False
    plans: dict[str, DrawPlan] = {}
    for lot in scene.lots:
        plan = scene.plan(lot, state.get(lot.key, ()))
        if plan is None:
            continue
        plans[lot.key] = plan
        if plan.closure.distance > CLOSURE_LIMIT_FT:
            lines.append(f"{lot.title}: контур не замыкается, невязка {plan.closure.distance:.3f} ft")
            hard = True
    main = next((lot for lot in scene.lots if lot.is_main and lot.key in plans), None)
    report: NeighborVerificationReport | None = None
    if main is not None:
        others = [lot for lot in scene.lots if lot is not main and lot.key in plans]
        if others:
            report = verify_with_neighbors(plans[main.key], [plans[lot.key] for lot in others])
            hard = hard or report.overall_status == "error"
            for pair, other in zip(report.pair_reports, others):
                if pair.status == "ok":
                    continue
                if pair.overlap_area_sqft > 0.5:
                    lines.append(
                        f"наш участок и «{other.name}» накладываются: {pair.overlap_area_sqft:,.1f} sq ft"
                    )
                for edge in pair.shared_edges:
                    if edge.status == "ok":
                        continue
                    lines.append(
                        f"общая граница с «{other.name}»: наша сторона #{edge.main_seg_index} "
                        f"({edge.main_bearing}, {edge.main_length_ft:.2f}') ↔ сторона соседа "
                        f"#{edge.neighbor_seg_index} ({edge.neighbor_bearing}, {edge.neighbor_length_ft:.2f}') — "
                        f"угол расходится на {edge.angle_diff_dms}, длина на {edge.abs_length_diff_ft:.2f}'"
                    )
                for finding in pair.findings:
                    lines.append(f"«{other.name}»: {' '.join(finding.split())}")
                tie = pair.street_tie_check or ""
                if any(word in tie for word in ("отличается", "РАЗНЫХ", "Внимание")):
                    lines.append(f"«{other.name}»: {tie}")
    else:
        keyed = [lot for lot in scene.lots if lot.key in plans]
        for i, first in enumerate(keyed):
            for second in keyed[i + 1:]:
                area = polygon_intersection_area(plans[first.key].vertices, plans[second.key].vertices)
                if area > HARD_OVERLAP_SQFT:
                    lines.append(f"«{first.name}» и «{second.name}» накладываются: {area:,.1f} sq ft")
                    hard = True
    return lines, hard, report


# ---------------------------------------------------------------------------
# ИИ-советник
# ---------------------------------------------------------------------------

ADVISOR_SYSTEM_PROMPT = """You are a senior land surveyor's assistant. You review metes-and-bounds legal descriptions of ADJACENT lots (normally the same tax block, often New York City deeds and title reports).

A Python geometry engine has already parsed every description, built each lot polygon and cross-checked the lots against each other. It also applied its own verified findings (listed in the input). Your job is to REASON about the conflicts Python could not explain and to tell Python what to try next. Python will test every suggestion numerically (closure of the lot, overlap with neighbours, fit of shared boundaries) and discards whatever does not improve the geometry. A human surveyor confirms every change, so be precise and honest about uncertainty.

COORDINATES: feet; origin (0, 0) is the street corner the lots are tied to; +Y is north, +X is east. Bearings are quadrant bearings in the traverse direction of the side. A side marked NO BEARING PRINTED has only a word direction in the document ("southerly along ..."), so its bearing in the data is a nominal compass direction, not a real value.

INPUT: for every lot (L1, L2, ...) its role, streets, tie to the corner, sides in traverse order, closure error, polygon vertices and the document text and/or page images. Then Python's findings, and the conflicts that REMAIN after them.

WORK THROUGH THIS CHECKLIST FOR EVERY REMAINING CONFLICT:
1. Data missing in one document but printed in another: a course along a street given only by a word, a tie without a bearing, a street width, a skipped course.
2. Misread characters (the sources are often scans): N/S or E/W letter, 6-9, 3-8, 5-6, 1-7, 0-8, a dropped digit, feet-and-inches read as decimal feet. A boundary shared by two lots must have the same bearing (reversed) in both documents, and the same length unless one lot is deeper than the other.
3. Tie problems: a different reference corner, a distance measured along the other side of the street, lots on opposite sides of a street, a distance to the corner that contradicts the neighbours' frontages (for example 160 + 40 = 200).
4. A different bearing basis: all bearings of one document rotated by the same angle against the other.
5. Alternate lengths printed side by side: "(deed)" and "(tax map)", "(U.S. Standard)", "(actual)", "(record)" and "(measured)". These are NOT errors. Do NOT propose switching a lot from one system to the other. Say which system each lot was built with and which printed alternate value would match the neighbour. This is an explanation only: the customer decides which system to draw.
6. The lots are not actually adjacent, or the wrong page or the wrong parcel was parsed.
7. A genuine conflict between the recorded documents (the overlap or gap exists on paper). Say so plainly.

HARD RULES:
- Never invent numbers. A proposed value must be printed in one of the documents (quote the words) or follow from printed values by arithmetic that you state.
- Never change a value only to make a polygon close.
- Do not repeat Python's findings.
- When page images are attached, read the relevant course yourself and say so if the parsed value differs from what is printed.
- If nothing should be changed, return an empty "proposals" list. An explanation alone is a valid and useful answer.
- Write "analysis", "conflict", "cause", "title" and "reason" in RUSSIAN, in plain words for a surveyor. Be brief: "analysis" is 2 to 6 sentences.

OUTPUT: exactly one JSON object and nothing else:
{
  "analysis": "what is going on between these lots, in Russian",
  "explanations": [
    {"conflict": "which remaining conflict", "cause": "why it happens and whether a document is wrong"}
  ],
  "proposals": [
    {
      "lot": "L1",
      "title": "short name of the fix",
      "reason": "why this value is right",
      "source_lot": "L2",
      "source_quote": "the exact words of the document that give the value",
      "changes": [
        {"target": "side", "side": 4, "bearing": "S 22°26'12\\" E", "distance_ft": null}
      ]
    }
  ]
}
"target" is "side" (then "side" is the 1-based number of the side of that lot) or "tie" (the bearing from the street corner to the point of beginning). Give "bearing" in the traverse direction of that side, or null to keep it. Give "distance_ft" in decimal feet, or null to keep it. "source_lot" and "source_quote" may be null when the value is computed."""


def _lot_block(
    scene: _Scene, lot: LotInput, label: str, state: dict[str, tuple[Change, ...]],
    image_range: tuple[int, int] | None, text_limit: int,
) -> str:
    extra = state.get(lot.key, ())
    desc = scene.description(lot, extra)
    plan = scene.plan(lot, extra)
    raw = lot.description
    lines = [f"[{label}] {'OUR LOT' if lot.is_main else 'NEIGHBOUR LOT'} — file \"{lot.name}\""]
    if desc is None:
        lines.append("  (the lot could not be built)")
        return "\n".join(lines)
    lines.append(f"  parsed by: {'AI model' if lot.used_ai else 'local text parser'}")
    street = desc.main_street or "?"
    if desc.main_street_note:
        street += f" {desc.main_street_note}"
    lines.append(
        f"  fronting street: {street}; cross street: {desc.cross_street or 'none'}; "
        f"lot faces: {desc.front_side or '?'}; block {desc.block_number or '?'}, lot {desc.lot_number or '?'}"
    )
    tie = desc.corner_to_pob_bearing
    if tie is not None and desc.distance_to_corner_ft > 1e-6:
        raw_tie = raw.corner_to_pob_bearing
        mark = "" if raw_tie is None or raw_tie.has_bearing else " [NO BEARING PRINTED in this document]"
        if raw_tie is not None and _label(_of(raw_tie)) != _label(_of(tie)):
            mark = f" [was {raw_tie.bearing_label}; changed by a verified finding]"
        lines.append(
            f"  tie: point of beginning is {desc.distance_to_corner_ft:.2f} ft from the corner, "
            f"bearing {tie.bearing_label}{mark}"
        )
    else:
        lines.append("  tie: the point of beginning is the street corner itself")
    if plan is not None:
        lines.append(
            f"  point of beginning (x, y) = ({plan.pob[0]:.2f}, {plan.pob[1]:.2f}); "
            f"closure error {plan.closure.distance:.3f} ft; area {plan.area_sqft:,.0f} sq ft"
        )
        lines.append("  vertices: " + " ".join(f"({x:.2f}, {y:.2f})" for x, y in plan.vertices))
    lines.append("  sides in traverse order:")
    for index, seg in enumerate(desc.segments):
        old = raw.segments[index] if index < len(raw.segments) else seg
        marks = []
        if not seg.has_bearing:
            marks.append("NO BEARING PRINTED — nominal direction only")
        elif not old.has_bearing:
            marks.append(f"no bearing printed in this document; was {old.bearing_label}; taken by a verified finding")
        elif _label(_of(old)) != _label(_of(seg)):
            marks.append(f"was {old.bearing_label}; changed by a verified finding")
        if abs(old.distance_ft - seg.distance_ft) > 1e-6:
            marks.append(f"was {old.distance_ft:.2f} ft; changed by a verified finding")
        if seg.course_note:
            marks.append(f'note: "{seg.course_note}"')
        if seg.interior_angle:
            marks.append(f"interior angle {seg.interior_angle}")
        lines.append(
            f"    #{index + 1}  {seg.bearing_label}  {seg.distance_ft:.2f} ft"
            + (f"  [{'; '.join(marks)}]" if marks else "")
        )
    if desc.anomaly_note:
        lines.append(f"  parser remark: {desc.anomaly_note}")
    text = (lot.source_text or "").strip()
    if image_range is not None:
        first, last = image_range
        lines.append(
            f"  document pages: attached images {first}" + (f"–{last}" if last != first else "")
        )
    elif text:
        if len(text) > text_limit:
            text = text[:text_limit].rstrip() + " …"
        lines.append('  document text:\n  """\n' + text + '\n  """')
    else:
        lines.append("  document pages: not attached")
    return "\n".join(lines)


def build_advisor_request(
    scene: _Scene, chosen: Sequence[Proposal], remaining: Sequence[str],
    report: NeighborVerificationReport | None, *, max_images: int = 16, text_limit: int = 6000,
) -> tuple[str, list[str], dict[str, LotInput]]:
    """Собирает запрос советнику: (текст, изображения страниц, обозначения участков)."""
    ordered = sorted(scene.lots, key=lambda lot: not lot.is_main)
    labels = {f"L{i}": lot for i, lot in enumerate(ordered, 1)}
    by_key = {lot.key: label for label, lot in labels.items()}
    images: list[str] = []
    blocks: list[str] = []
    for label, lot in labels.items():
        image_range = None
        if lot.images and len(images) + len(lot.images) <= max(0, int(max_images)):
            image_range = (len(images) + 1, len(images) + len(lot.images))
            images.extend(lot.images)
        blocks.append(_lot_block(scene, lot, label, scene.virtual, image_range, text_limit))
    parts = ["LOTS (the data already includes Python's verified findings):", "\n\n".join(blocks)]
    if chosen:
        parts.append("PYTHON'S VERIFIED FINDINGS (already included above, do not repeat them):")
        parts.append("\n".join(
            f"- {by_key.get(p.lot_key, '?')}: {'; '.join(c.describe() for c in p.changes)} "
            f"(source: {p.source}; closure {p.before.closure_ft:.3f} -> {p.after.closure_ft:.3f} ft, "
            f"overlap {p.before.overlap_sqft:.1f} -> {p.after.overlap_sqft:.1f} sq ft)"
            for p in chosen
        ))
    else:
        parts.append("PYTHON'S VERIFIED FINDINGS: none.")
    parts.append("CONFLICTS THAT REMAIN (Python's wording, in Russian):")
    parts.append("\n".join(f"- {text}" for text in remaining) or "- none")
    if report is not None and report.pair_reports:
        parts.append("PYTHON'S CHECK OF OUR LOT AGAINST THE NEIGHBOURS (full report, in Russian):")
        parts.append(report.format_report(compact=True))
    parts.append("Explain every remaining conflict and propose only fixes you can justify from the documents.")
    return "\n\n".join(parts), images, labels


def read_advisor_reply(data: Any) -> AdvisorReply:
    """Приводит ответ модели к ожидаемому виду; лишнее и непонятное отбрасывается."""
    reply = AdvisorReply()
    if not isinstance(data, dict):
        return reply
    reply.analysis = " ".join(str(data.get("analysis") or "").split())
    for item in data.get("explanations") or []:
        if isinstance(item, dict):
            conflict = " ".join(str(item.get("conflict") or "").split())
            cause = " ".join(str(item.get("cause") or "").split())
            if conflict or cause:
                reply.explanations.append((conflict, cause))
        elif isinstance(item, str) and item.strip():
            reply.explanations.append(("", " ".join(item.split())))
    for item in data.get("proposals") or []:
        if isinstance(item, dict) and isinstance(item.get("changes"), list):
            reply.proposals.append(item)
    return reply


def _value_origin(scene: _Scene, lot: LotInput, change: Change) -> str | None:
    """Есть ли предложенное значение в разобранных данных какого-либо участка набора."""
    for other in scene.lots:
        desc = other.description
        if change.field == "bearing":
            wanted = _azimuth((change.ns, change.degrees, change.minutes, change.seconds, change.ew))
            places = [(f"сторона #{i + 1}", seg) for i, seg in enumerate(desc.segments)]
            if desc.corner_to_pob_bearing is not None:
                places.append(("привязка к углу", desc.corner_to_pob_bearing))
            for place, seg in places:
                if other is lot and place == change.where:
                    continue
                if seg.has_bearing and _axis_diff(seg.azimuth_deg, wanted) * 3600.0 <= 0.5:
                    return f"«{other.name}», {place} ({seg.bearing_label})"
        else:
            for i, seg in enumerate(desc.segments):
                if other is lot and i == change.index:
                    continue
                if abs(seg.distance_ft - change.distance_ft) <= 0.006:
                    return f"«{other.name}», сторона #{i + 1} ({seg.distance_ft:.2f}')"
    return None


def _advisor_proposals(
    scene: _Scene, reply: AdvisorReply, labels: dict[str, LotInput], chosen: Sequence[Proposal],
) -> tuple[list[Proposal], list[str]]:
    """Проверяет советы геометрией: возвращает принятые находки и строки об отклонённых."""
    accepted: list[Proposal] = []
    rejected: list[str] = []
    requires = tuple(p.id for p in chosen)
    for item in reply.proposals:
        lot = labels.get(str(item.get("lot") or "").strip().upper())
        title = " ".join(str(item.get("title") or "совет без названия").split())
        if lot is None:
            rejected.append(f"{title} — не указан участок")
            continue
        base = scene.virtual.get(lot.key, ())
        desc = scene.description(lot, base)
        if desc is None:
            continue
        changes: list[Change] = []
        problem = ""
        for raw in item.get("changes") or []:
            if not isinstance(raw, dict):
                continue
            target = "tie" if str(raw.get("target") or "side").strip().lower() == "tie" else "side"
            if target == "tie":
                index, old = -1, desc.corner_to_pob_bearing
                if old is None:
                    problem = "у участка нет привязки к углу"
                    break
            else:
                try:
                    index = int(raw.get("side")) - 1
                except (TypeError, ValueError):
                    problem = "не указан номер стороны"
                    break
                if not 0 <= index < len(desc.segments):
                    problem = f"стороны #{index + 1} у участка нет"
                    break
                old = desc.segments[index]
            if raw.get("bearing") not in (None, "", "null"):
                parts = parse_bearing_text(raw.get("bearing"))
                if parts is None:
                    problem = f"не удалось прочитать румб «{raw.get('bearing')}»"
                    break
                if not old.has_bearing or _label(parts) != _label(_of(old)):
                    changes.append(_bearing_change(target, index, old, parts))
            if target == "side" and raw.get("distance_ft") not in (None, "", "null"):
                try:
                    distance = float(raw.get("distance_ft"))
                except (TypeError, ValueError):
                    problem = f"не удалось прочитать длину «{raw.get('distance_ft')}»"
                    break
                if distance > 0 and abs(distance - old.distance_ft) > 1e-6:
                    changes.append(Change(
                        target="side", index=index, field="distance", distance_ft=distance,
                        old_label=f"{old.distance_ft:.2f}'", new_label=f"{distance:.2f}'",
                    ))
        if problem:
            rejected.append(f"{title} — {problem}")
            continue
        if not changes:
            # Совет повторяет то, что уже есть в данных.
            continue
        slots = {change.slot for change in changes}
        for change in list(changes):
            if change.target == "side" and change.field == "bearing" and ("tie", -1, "bearing") not in slots:
                tie_change = _linked_tie_change(
                    desc, change.index, (change.ns, change.degrees, change.minutes, change.seconds, change.ew),
                )
                if tie_change is not None and not desc.segments[change.index].has_bearing:
                    changes.append(tie_change)
                    slots.add(tie_change.slot)
        before = scene.metrics(lot, base, state=scene.virtual)
        after = scene.metrics(lot, base + tuple(changes), state=scene.virtual)
        verdict = judge(before, after)
        if verdict == "rejected" or before is None or after is None:
            detail = (
                "участок с такой правкой не строится" if after is None or before is None
                else f"невязка {before.closure_ft:.3f} → {after.closure_ft:.3f} ft, "
                     f"наложение {before.overlap_sqft:,.1f} → {after.overlap_sqft:,.1f} sq ft"
            )
            rejected.append(
                f"«{lot.name}»: {title} ({'; '.join(c.describe() for c in changes)}) — "
                f"совет не подтверждён геометрией: {detail}"
            )
            continue
        reasons: list[str] = []
        reason = " ".join(str(item.get("reason") or "").split())
        if reason:
            reasons.append(f"довод советника: {reason}")
        for change in changes:
            origin = _value_origin(scene, lot, change)
            reasons.append(
                f"{change.where}: значение {change.new_label} есть в разобранных данных — {origin}"
                if origin else
                f"{change.where}: значения {change.new_label} нет в разобранных данных участков — "
                "сверьте с документом"
            )
        donor = labels.get(str(item.get("source_lot") or "").strip().upper())
        quote = " ".join(str(item.get("source_quote") or "").split())
        source = "ИИ-советник"
        if donor is not None:
            source += f"; источник — «{donor.name}»"
        if quote:
            source += f"; цитата: «{quote[:220]}»"
        accepted.append(Proposal(
            id=_proposal_id(lot, changes), lot_key=lot.key, lot_name=lot.name,
            kind="advisor", origin="ai", title=title, changes=tuple(changes), source=source,
            reasons=tuple(reasons), before=before, after=after, verdict=verdict,
            requires=tuple(pid for pid in requires),
        ))
    return accepted, rejected


def advisor_cache_key(lots: Sequence[LotInput]) -> str:
    """Ключ разбора набора: пока файлы разобраны так же, советник заново не запрашивается."""
    payload = [
        [lot.key, lot.is_main, lot.description.model_dump(mode="json")]
        for lot in sorted(lots, key=lambda lot: lot.key)
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

AdvisorCall = Callable[[str, str, list[str]], tuple[Any, Any]]


def analyse_conflicts(
    builder: AcadBuilder,
    lots: Sequence[LotInput],
    *,
    scale: float,
    advisor: AdvisorCall | None = None,
    advisor_note: str = "",
    advisor_cache: dict[str, AdvisorReply] | None = None,
    force_advisor: bool = False,
    max_images: int = 16,
    progress: Callable[[str], None] | None = None,
) -> ConflictAnalysis:
    """
    Сверяет данные участков набора между собой и предлагает правки.

    ``advisor(system_prompt, user_text, images)`` → ``(JSON-объект, usage)`` —
    платный вопрос к нейросети; вызывается, только если после находок Python
    остался серьёзный конфликт. ``advisor=None`` — советник недоступен, причина
    в ``advisor_note``. Ответ хранится в ``advisor_cache`` по ключу разбора:
    принятие или возврат правки не вызывает новый запрос.
    """
    analysis = ConflictAnalysis()
    lots = list(lots)
    analysis.applied = [fix for lot in lots for fix in lot.fixes]
    if not lots:
        return analysis
    scene = _Scene(builder, lots, scale)

    proposals: list[Proposal] = []
    for lot in sorted(lots, key=lambda lot: not lot.is_main):
        proposals.extend(_street_bearing_proposals(scene, lot))
        proposals.extend(_neighbor_value_proposals(scene, lot))
    proposals.sort(key=lambda p: (not p.verified, p.after.closure_ft))

    # Подтверждённые находки принимаются «в уме»: остаток считается после них.
    chosen: list[Proposal] = []
    taken: set[tuple[str, tuple[str, int, str]]] = set()
    for proposal in proposals:
        slots = {(proposal.lot_key, change.slot) for change in proposal.changes}
        if proposal.verified and not (slots & taken):
            chosen.append(proposal)
            taken |= slots
    virtual: dict[str, list[Change]] = {}
    for proposal in chosen:
        virtual.setdefault(proposal.lot_key, []).extend(proposal.changes)
    scene.virtual = {key: tuple(changes) for key, changes in virtual.items()}

    analysis.conflicts, _, _ = _conflicts(scene, {})
    analysis.remaining, analysis.remaining_hard, report = _conflicts(scene, scene.virtual)

    # ИИ-советник: только если Python не закрыл всё сам.
    analysis.advisor_key = advisor_cache_key(lots)
    cached = (advisor_cache or {}).get(analysis.advisor_key)
    reply: AdvisorReply | None = None
    labels: dict[str, LotInput] = {
        f"L{i}": lot for i, lot in enumerate(sorted(lots, key=lambda lot: not lot.is_main), 1)
    }
    if cached is not None and not force_advisor:
        reply = cached
        analysis.advisor_status = "cached"
    elif analysis.remaining_hard or force_advisor:
        if advisor is None:
            analysis.advisor_status = "skipped"
            analysis.advisor_note = advisor_note or "не запрошен"
        else:
            try:
                user_text, images, labels = build_advisor_request(
                    scene, chosen, analysis.remaining, report, max_images=max_images,
                )
                if progress:
                    progress("ИИ-советник разбирает оставшиеся расхождения между участками…")
                data, usage = advisor(ADVISOR_SYSTEM_PROMPT, user_text, images)
                reply = read_advisor_reply(data)
                reply.usage = usage
                reply.model = str(getattr(usage, "model", "") or "")
                analysis.advisor_status = "ok"
            except Exception as exc:  # noqa: BLE001 - сбой советника не срывает разбор набора
                analysis.advisor_status = "error"
                analysis.advisor_note = f"запрос не удался — {' '.join(str(exc).split()) or type(exc).__name__}"
    if reply is not None:
        analysis.advisor_reply = reply
        accepted, analysis.advisor_rejected = _advisor_proposals(scene, reply, labels, chosen)
        known = {p.id for p in proposals}
        proposals.extend(p for p in accepted if p.id not in known)
    analysis.proposals = proposals
    return analysis
