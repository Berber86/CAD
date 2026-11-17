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
     со значением соседа контур замыкается;
   * у стороны напечатаны две длины («174.89 feet (deed) 176.92 feet
     (tax map)»), и второе значение совпадает с общей межой соседа: тогда
     предлагается перейти на ту систему, которая стыкуется с соседом, —
     вместе с остальными сторонами, напечатанными так же, иначе контур
     перестаёт замыкаться.

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

import difflib
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
    segment_vector,
    street_name_canon as acad_builder_street_canon,
    verify_with_neighbors,
)
from llm_parser import LegalDescription, Segment, bearing_label
from street_names import share_street_aliases, with_shared_aliases
from pdf_set import CLOSURE_LIMIT_FT

#: Слово «southerly» допускает такой отход настоящего румба от стороны света.
WORD_DIRECTION_TOLERANCE_DEG = 50.0
#: Замыкающий курс подтверждает сторону, если его длина совпала с документом.
CLOSING_LENGTH_TOLERANCE_FT = 0.10
#: Наложение, с которого соседние участки без нашего считаются конфликтующими.
HARD_OVERLAP_SQFT = 6.0
#: Насколько второе напечатанное значение должно совпасть с межой соседа.
ALTERNATE_MATCH_TOLERANCE_FT = 0.02
#: Расхождение румбов, при котором общая межа считается одной и той же линией.
ALTERNATE_AXIS_TOLERANCE_ARCSEC = 60.0
#: Остаток невязки (4″), который ещё объясняется округлением напечатанных длин.
ALTERNATE_RESIDUAL_LIMIT_FT = 0.35

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
    #: Для привязки к углу с новой длиной: задан ли и румб (иначе румб выводится сам).
    bearing_set: bool = False

    @property
    def slot(self) -> tuple[str, int, str]:
        return (self.target, self.index, self.field)

    @property
    def where(self) -> str:
        if self.target == "street":
            return "перекрёсток" if self.field == "cross" else "название улицы"
        return "привязка к углу" if self.target == "tie" else f"сторона #{self.index + 1}"

    @property
    def is_name(self) -> bool:
        """Правка названия (перекрёсток или переименование улицы): геометрию не меняет."""
        return self.target == "street"

    def describe(self) -> str:
        if self.is_name:
            if self.field == "cross":
                return f"перекрёсток: «{self.old_label}» → «{self.new_label}»"
            return f"название улицы: добавить «{self.new_label}»"
        what = "румб" if self.field == "bearing" else "длина"
        return f"{self.where}: {what} {self.old_label} → {self.new_label}"


@dataclass(frozen=True)
class Metrics:
    """Чем измеряется польза правки для одного участка."""

    closure_ft: float
    overlap_sqft: float
    edge_errors: int
    edge_warnings: int
    #: Расстояние до ближайшего соседнего контура (0 — общая граница есть).
    gap_ft: float = 0.0
    #: Из ошибок стыка — только уступы задней межи (короткая сторона внутри длинной, остаток — к третьему).
    stepped_edges: int = 0


@dataclass(frozen=True)
class Proposal:
    """Находка: что поменять в данных участка, откуда значение и чем это подтверждено."""

    id: str
    lot_key: str
    lot_name: str
    kind: str  # "street_bearing" | "closing_course" | "neighbor_value" | "advisor" | "street_name"
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
        if max(self.before.gap_ft, self.after.gap_ft) > 0.5:
            parts.append(f"разрыв до соседа {self.before.gap_ft:,.2f} → {self.after.gap_ft:,.2f} ft")
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
    #: "" — обычный разбор советника; иначе режим карты ("turn" или "shift").
    join_mode: str = ""
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
            mode = " по карте" if self.join_mode else ""
            lines.append(f"ИИ-СОВЕТНИК{mode}{tag}{when}:")
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
        if change.is_name:
            if change.field == "cross":
                data["cross_street"] = change.new_label
            else:
                aliases = list(data.get("street_aliases") or [])
                if change.new_label not in aliases:
                    aliases.append(change.new_label)
                data["street_aliases"] = aliases
            continue
        if change.target == "tie" and change.field == "distance":
            # Расстояние от угла улицы до POB. Румб: если советник дал — ставим его, иначе
            # валидатор выведет румб по улице (или оставит прежний, если привязка уже есть).
            data["distance_to_corner_ft"] = change.distance_ft
            if change.bearing_set:
                data["corner_to_pob_bearing"] = {
                    "ns": change.ns, "degrees": change.degrees, "minutes": change.minutes,
                    "seconds": change.seconds, "ew": change.ew,
                    "distance_ft": change.distance_ft, "has_bearing": True,
                }
            elif data.get("corner_to_pob_bearing"):
                data["corner_to_pob_bearing"]["distance_ft"] = change.distance_ft
            continue
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
        #: Переименования, напечатанные в любом описании набора: общие для всех участков набора.
        self.shared_aliases = share_street_aliases(lot.description for lot in self.lots)
        self._plans: dict[tuple[str, tuple[Change, ...]], DrawPlan | None] = {}
        #: Подтверждённые находки, принятые «в уме» (после них считается остаток).
        self.virtual: dict[str, tuple[Change, ...]] = {}

    def description(self, lot: LotInput, extra: Sequence[Change] = ()) -> LegalDescription | None:
        key = (lot.key, tuple(extra))
        if key not in self._descriptions:
            try:
                built = apply_changes(lot.description, fix_changes(lot.fixes) + tuple(extra))
                self._descriptions[key] = with_shared_aliases(built, self.shared_aliases)
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

    def shared_edges(
        self, lot: LotInput, extra: Sequence[Change] = (), *, state: dict[str, tuple[Change, ...]] | None = None,
    ) -> list[tuple[LotInput, Any]]:
        """Общие межи участка с соседями при пробной правке: ``(сосед, SharedEdgeCheck)``."""
        state = state or {}
        plan = self.plan(lot, state.get(lot.key, ()) + tuple(extra))
        if plan is None:
            return []
        found: list[tuple[LotInput, Any]] = []
        for other, other_plan in self._others(lot, state):
            pair = verify_with_neighbors(plan, [other_plan]).pair_reports[0]
            found.extend((other, edge) for edge in pair.shared_edges)
        return found

    def metrics(
        self, lot: LotInput, extra: Sequence[Change] = (), *, state: dict[str, tuple[Change, ...]] | None = None,
    ) -> Metrics | None:
        plan = self.plan(lot, extra)
        if plan is None:
            return None
        others = [p for _, p in self._others(lot, state or {})]
        overlap, errors, warnings = 0.0, 0, 0
        gap = 0.0
        stepped = 0
        if others:
            report = verify_with_neighbors(plan, others)
            overlap = sum(pair.overlap_area_sqft for pair in report.pair_reports)
            errors, warnings = report.error_count, report.warning_count
            gap = _nearest_gap_ft(plan, others)
            for pair in report.pair_reports:
                for edge in pair.shared_edges:
                    if edge.status == "error" and edge.issue_codes and set(edge.issue_codes) == {"STEPPED_BOUNDARY_DEPTH"}:
                        stepped += 1
        return Metrics(plan.closure.distance, overlap, errors, warnings, gap, stepped)


def _nearest_gap_ft(plan: DrawPlan, others: Sequence[DrawPlan]) -> float:
    """Расстояние от контура участка до ближайшего соседнего контура (0 — касаются)."""
    from shapely.geometry import Polygon

    try:
        mine = Polygon(plan.vertices)
        if not mine.is_valid:
            mine = mine.buffer(0)
        gaps = []
        for other in others:
            poly = Polygon(other.vertices)
            if not poly.is_valid:
                poly = poly.buffer(0)
            gaps.append(mine.distance(poly))
        return float(min(gaps)) if gaps else 0.0
    except Exception:  # noqa: BLE001 - метрика не должна ломать разбор
        return 0.0


def judge(before: Metrics | None, after: Metrics | None) -> str:
    """
    Оценка пробной правки: ``verified`` — контур замыкается и хуже не стало,
    ``partial`` — стало заметно лучше, но не всё сошлось, ``rejected`` — пользы нет.

    Правка, которая только ставит участок к соседу (уменьшает разрыв до него), —
    ``partial``: её подтверждает человек, она не применяется «все подтверждённые».
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
    gap_better = (before.gap_ft - after.gap_ft) > 1.0 and not overlap_worse
    if closure_worse or not (closure_better or overlap_better or edges_better or gap_better):
        return "rejected"
    if overlap_worse and not closure_better:
        return "rejected"
    if gap_better and not (closure_better or overlap_better or edges_better):
        return "partial"
    return "verified" if closure_ok and not overlap_worse else "partial"


def advisor_verdict(before: Metrics | None, after: Metrics | None, *, allow_stepped: bool = False) -> str:
    """
    Вердикт совета ИИ. Советник получает ``verified`` (его можно применить одним действием
    «все подтверждённые») только если контур замыкается, наложения нет и общие межи совпадают
    с соседями (ни одной ошибки стыка). Иначе вердикт остаётся ``partial`` — решает геодезист.

    ``allow_stepped`` — для склейки по карте: уступ задней межи (короткая сторона внутри длинной,
    остаток граничит с третьим участком) не считается ошибкой стыка.
    """
    verdict = judge(before, after)
    if verdict != "verified" or after is None:
        return verdict
    stepped = after.stepped_edges if allow_stepped else 0
    if after.closure_ft > CLOSURE_LIMIT_FT or after.edge_errors - stepped > 0:
        return "partial"
    return verdict


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
                # Почему румба нет — три разные ситуации, и путать их нельзя:
                # соседей вообще не загружали; соседи есть, но их румб не сходится;
                # соседи есть, а такой линии у них нет.
                others = [donor for donor in scene.lots if donor is not lot]
                if not others:
                    why = (
                        "соседей в наборе нет (их описания не загружены), поэтому румб вычислен "
                        "по замыканию контура этого участка"
                    )
                elif groups:
                    why = (
                        "румб этой линии из описаний соседей не подтверждается замыканием "
                        "контура, поэтому он вычислен"
                    )
                else:
                    why = (
                        "в описаниях соседей такой линии нет, поэтому румб вычислен по "
                        "замыканию контура"
                    )
                reasons = [
                    no_bearing,
                    f"{why}: это единственный румб, при котором контур "
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
# Правило 3: у стороны напечатаны две длины (deed / tax map) — берём ту,
#            что совпадает с общей межой соседа, и держим контур замкнутым
# ---------------------------------------------------------------------------

#: Слова, которыми в описаниях помечают второе (альтернативное) значение длины.
_ALTERNATE_LABEL_RE = re.compile(
    r"deed|tax\s*map|u\.?\s*s\.?\s*standard|us\s*standard|actual|record|measured|"
    r"survey|title|assessor|map",
    re.IGNORECASE,
)

#: «174.89 feet (deed) 176.92 feet (tax map)» — два значения с пометками.
_TWO_LABELLED_LENGTHS_RE = re.compile(
    r"(?P<first>\d+(?:\.\d+)?)\s*(?:feet|foot|ft)\s*\(\s*(?P<label1>[^()]{1,40}?)\s*\)"
    r"\s*[,;]?\s*(?P<second>\d+(?:\.\d+)?)\s*(?:feet|foot|ft)\s*\(\s*(?P<label2>[^()]{1,40}?)\s*\)",
    re.IGNORECASE,
)

#: «198.83 feet (196.60 feet U.S. Standard)» — второе значение в скобках.
_BRACKETED_LENGTH_RE = re.compile(
    r"(?P<first>\d+(?:\.\d+)?)\s*(?:feet|foot|ft)\s*\(\s*(?P<second>\d+(?:\.\d+)?)\s*"
    r"(?:feet|foot|ft)?\s*[,;]?\s*(?P<label2>[^()]{0,40})\)",
    re.IGNORECASE,
)


def printed_alternate_lengths(
    description: LegalDescription, source_text: str
) -> dict[int, tuple[float, str, str]]:
    """
    Вторые (альтернативные) длины сторон, напечатанные в самом описании:
    ``{индекс стороны: (длина, пометка, как напечатано)}``.

    В нью-йоркских deeds длины нередко даны двумя системами рядом:
    ``174.89 feet (deed) 176.92 feet (tax map)`` или
    ``198.83 feet (196.60 feet U.S. Standard)``. Напечатаны обе, чертится одна.
    """
    text = " ".join((source_text or "").split())
    if not text or not description.segments:
        return {}
    found: dict[int, tuple[float, str, str]] = {}
    for pattern in (_TWO_LABELLED_LENGTHS_RE, _BRACKETED_LENGTH_RE):
        for match in pattern.finditer(text):
            first = float(match.group("first"))
            second = float(match.group("second"))
            label = " ".join((match.group("label2") or "").split())
            if abs(second - first) < 0.005:
                continue
            if pattern is _BRACKETED_LENGTH_RE and not _ALTERNATE_LABEL_RE.search(label):
                continue
            index = next(
                (
                    i for i, seg in enumerate(description.segments)
                    if abs(seg.distance_ft - first) <= 0.005
                ),
                None,
            )
            if index is None or index in found:
                continue
            found[index] = (second, label, " ".join(match.group(0).split()))
    return found


def _distance_change(index: int, seg: Segment, value: float) -> Change:
    return Change(
        target="side", index=index, field="distance", distance_ft=value,
        old_label=f"{seg.distance_ft:.2f}'", new_label=f"{value:.2f}'",
    )


def _alternate_verdict(
    before: Metrics | None,
    after: Metrics | None,
    *,
    span_ft: float = 40.0,
    neighbour_closure_ft: float = 0.0,
) -> str:
    """
    Оценка переключения системы длин.

    ``verified`` — контур замыкается в допуске; ``partial`` — общая межа с соседом
    сходится точно, а остаток невязки не больше, чем у самого описания соседа
    (доли фута от округления tax map, а не от правки); ``rejected`` — стало хуже.
    """
    if before is None or after is None:
        return "rejected"
    # Тонкий «волосяной» клин вдоль общей межи — это отвод гэпа в угол, а не наложение.
    if after.overlap_sqft > 1.0 and after.overlap_sqft / max(span_ft, 10.0) > 0.12:
        return "rejected"
    if after.closure_ft <= CLOSURE_LIMIT_FT:
        return "verified"
    if (
        after.closure_ft <= ALTERNATE_RESIDUAL_LIMIT_FT
        and after.closure_ft <= neighbour_closure_ft + 0.05
    ):
        return "partial"
    return "rejected"


def _printed_alternate_proposals(
    scene: _Scene,
    lot: LotInput,
    *,
    state: dict[str, tuple[Change, ...]] | None = None,
    pending: Sequence[Proposal] = (),
) -> list[Proposal]:
    """
    Правило 3: сторона напечатана дважды (deed / tax map), и второе значение
    совпадает с общей межой соседа — предлагается перейти на ту систему,
    которая стыкуется с соседом.

    Так участок и сосед встают стенка к стенке без щели: у обоих одна и та же
    длина общей межи. Остальные стороны, напечатанные в том же виде, переходят
    вместе с ней — иначе контур перестаёт замыкаться. Решение подтверждается
    геометрией (невязка в допуске, наложения нет) и остаётся за геодезистом:
    данные меняются только кнопкой «Применить».
    """
    state = state or {}
    base = tuple(state.get(lot.key, ()))
    description = scene.description(lot, base)
    plan = scene.plan(lot, base)
    if description is None or plan is None:
        return []
    alternates = printed_alternate_lengths(description, lot.source_text)
    if not alternates:
        return []
    before = scene.metrics(lot, base, state=state)
    if before is None:
        return []
    plan_segments = plan.description.segments

    # Общие межи, у которых второе напечатанное значение совпадает с межой соседа.
    confirmed: dict[int, tuple[LotInput, Any]] = {}
    checked: set[int] = set()
    for donor, edge in scene.shared_edges(lot, base, state=state):
        index = edge.main_seg_index - 1
        if index not in alternates or index in checked or not 0 <= index < len(plan_segments):
            continue
        checked.add(index)
        if edge.abs_length_diff_ft <= 0.015:
            continue  # межа уже сходится — менять нечего
        if abs(alternates[index][0] - edge.neighbor_length_ft) > ALTERNATE_MATCH_TOLERANCE_FT:
            continue
        if edge.angle_diff_arcsec > ALTERNATE_AXIS_TOLERANCE_ARCSEC:
            continue  # расходится не только длина: это не переключение систем
        confirmed[index] = (donor, edge)
    if not confirmed:
        return []

    candidates: list[frozenset[int]] = [frozenset({index}) for index in sorted(confirmed)]
    full = frozenset(alternates)
    if len(full) > 1 and full not in candidates:
        candidates.append(full)

    span_ft = max((edge.overlap_span_ft for _, edge in confirmed.values()), default=40.0)
    donors = {donor.key: donor for donor, _ in confirmed.values()}
    neighbour_closure = min(
        (
            donor_plan.closure.distance
            for donor in donors.values()
            if (donor_plan := scene.plan(donor, state.get(donor.key, ()))) is not None
        ),
        default=0.0,
    )

    found: list[Proposal] = []
    rejected: dict[frozenset[int], float] = {}
    for ids in candidates:
        changes = tuple(
            _distance_change(index, plan_segments[index], alternates[index][0])
            for index in sorted(ids)
        )
        after = scene.metrics(lot, base + changes, state=state)
        verdict = _alternate_verdict(
            before, after, span_ft=span_ft, neighbour_closure_ft=neighbour_closure
        )
        if verdict == "rejected" or after is None:
            if after is not None:
                rejected[ids] = after.closure_ft
            continue

        switched = ", ".join(
            f"#{index + 1} {plan_segments[index].distance_ft:.2f}' → {alternates[index][0]:.2f}'"
            for index in sorted(ids)
        )
        labels: list[str] = []
        for index in sorted(ids):
            label = alternates[index][1]
            if label and label not in labels:
                labels.append(label)
        system = " / ".join(labels) if labels else "второе напечатанное значение"
        donor = next((confirmed[i][0] for i in sorted(ids) if i in confirmed), None)
        edge = next((confirmed[i][1] for i in sorted(ids) if i in confirmed), None)

        title = f"Длины по {system}: {switched}"
        if donor is not None:
            title += f" — общая межа сходится с «{donor.name}»"

        reasons: list[str] = []
        if donor is not None and edge is not None:
            index = edge.main_seg_index - 1
            reasons.append(
                f"общая граница с «{donor.name}» не сходится: у нас {edge.main_length_ft:.2f}', "
                f"у соседа {edge.neighbor_length_ft:.2f}' — расхождение "
                f"{edge.abs_length_diff_ft:.2f}' ({edge.abs_length_diff_ft * 12.0:.1f}″)"
            )
            reasons.append(
                f"в нашем же описании у этой стороны напечатано второе значение — "
                f"«{alternates[index][2]}»: {alternates[index][0]:.2f}' "
                f"({alternates[index][1] or 'второе значение'}) совпадает с общей межой соседа"
            )
        if len(ids) > 1:
            extra = ", ".join(
                f"#{index + 1} {plan_segments[index].distance_ft:.2f}' → {alternates[index][0]:.2f}'"
                for index in sorted(ids) if index not in confirmed
            )
            text = (
                "вместе с ней берутся вторые значения остальных сторон, напечатанных так же"
                + (f" ({extra})" if extra else "")
                + f": невязка {before.closure_ft:.3f} → {after.closure_ft:.3f} ft"
            )
            single = next((value for key, value in rejected.items() if len(key) == 1), None)
            if single is not None and math.isfinite(single):
                text += f" (одна только общая межа даёт невязку {single:.3f} ft)"
            reasons.append(text)
        if verdict == "partial":
            reasons.append(
                f"контур после переключения замыкается с невязкой {after.closure_ft:.3f} ft — это "
                f"округление напечатанных значений (у соседа в его описании невязка "
                f"{neighbour_closure:.3f} ft), а не следствие правки: общая межа сходится точно, "
                "остаток отводится в угол — сверьте с оригиналом чертежа"
            )
        reasons.append(
            "deed и tax map — две системы одного описания, а не опечатка: чертится одна. "
            "Здесь выбрана та, что сходится с описанием соседа; вторая остаётся в документе"
        )

        quotes = "; ".join(
            f"#{index + 1}: «{alternates[index][2]}»" for index in sorted(ids)
        )
        source = f"напечатано в нашем описании — {quotes}"
        if edge is not None and donor is not None:
            source += (
                f"; у соседа «{donor.name}», сторона #{edge.neighbor_seg_index} — "
                f"{edge.neighbor_length_ft:.2f}'"
            )

        requires: tuple[str, ...] = ()
        alone = scene.metrics(lot, changes)
        if alone is None or alone.closure_ft > CLOSURE_LIMIT_FT:
            requires = tuple(
                proposal.id for proposal in pending
                if proposal.verified and proposal.lot_key == lot.key
            )

        found.append(Proposal(
            id=_proposal_id(lot, changes), lot_key=lot.key, lot_name=lot.name,
            kind="printed_alternate", origin="rule", title=title,
            changes=changes, source=source, reasons=tuple(reasons),
            before=before, after=after, verdict=verdict, requires=requires,
        ))
    return found


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
5. Alternate lengths printed side by side: "(deed)" and "(tax map)", "(U.S. Standard)", "(actual)", "(record)" and "(measured)". These are NOT errors. Do NOT invent a system that is not printed. Python itself offers the switch when the neighbour's side of a shared boundary is printed as the alternate value in our document and the contour stays closed (it is listed among Python's findings, applied by the surveyor with one button); say which system each lot was built with, and which printed alternate would match the neighbour when Python did not already offer it. Never switch a lot to an alternate value that Python's geometry check rejected.
6. The lots are not actually adjacent, or the wrong page or the wrong parcel was parsed.
7. A genuine conflict between the recorded documents (the overlap or gap exists on paper). Say so plainly.

GLUING SIDES AND ANCHORING A NEIGHBOUR TO A CORNER (read before you propose any tie change):
- A shared boundary is a side of one lot that lies on the same line as a side of another lot: the two bearings are reversed (for example S 56 degrees 36 minutes 12 seconds E against N 56 degrees 36 minutes 12 seconds W) and the lengths are equal, or one length is a part of the other. A deeper lot's side may run past a shorter neighbour's side; the rest of it then borders a third lot. This is not an error. Check this on the printed values before you blame a tie or a length.
- A lot may consist of several parcels printed in ONE document ("PARCEL I", "PARCEL II", "Parcel II being the same premises ..."). They form one lot. If a neighbour's point of beginning or side lies on the outer boundary of such a parcel and the figure does not contain that parcel, the figure is incomplete. Say so in the explanation. The operations you have cannot add a parcel: do not invent sides for it.
- Anchoring: a neighbour's figure is attached to our figure through the street corner its own text names. Re-tying it to a different corner (cross_street, street_alias, or a tie distance) is allowed only when the names and numbers are printed in that lot's own text. If the neighbour's tie is measured from a corner whose position relative to our corner is not printed in any document (for example "distant 965.57 feet southeasterly from the corner formed by ... Boulevard and ... Road" while no document gives the distance from that corner to our corner), you cannot compute the offset. Do not invent a distance. Say that the neighbour must be placed by the shared boundary, name the two sides that coincide, and state which printed distance would settle it.
- When our side and the neighbour's side coincide in direction and length but the neighbour's polygon sits off by a distance, the cause is the anchor (the tie), not the side lengths. Never change side lengths to remove that offset.
- Python applies your proposal on its own only when the lot closes, nothing overlaps and every shared boundary fits its neighbour; otherwise the surveyor decides. Propose the change anyway when the printed values support it.

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
"target" is "side" (then "side" is the 1-based number of the side of that lot) or "tie" (the point of beginning relative to the street corner). For a side: "bearing" in the traverse direction of that side, or null to keep it; "distance_ft" in decimal feet, or null to keep it.
For "tie" there are two operations:
  - the distance from the street corner to the point of beginning, printed as "distant N feet <direction> from the corner": give "distance_ft": N (a number, decimal feet). Use this when the lot has no tie or the tie distance is wrong. Give "bearing" as the quadrant bearing of the course from the corner to the point of beginning if the document or a neighbour gives it, otherwise null (it is then derived from the street).
  - only the bearing of an existing tie: "bearing" with "distance_ft": null.
Two more operations change NAMES, not geometry. Python accepts them only when the name is printed in that lot's own text:
  - {"target": "cross_street", "name": "Seaside Boulevard"}: the street that this lot's corner is tied to (the cross street), exactly as printed in this lot's document. Use it when the lot is attached to a different intersection only because its cross street is named differently or wrongly.
  - {"target": "street_alias", "current": "Father Capodanno Blvd", "old": "Seaside Boulevard"}: a printed renaming ("X (now Y)", "Y (formerly X)", "f/k/a", "a/k/a", "also known as"). Both names must be printed in this lot's text, together with that marker.
If a street name in a lot is misread (for example a neighbouring corner street spelled differently from the other documents), the fix MUST be a proposal with a cross_street or street_alias change, not only an explanation. Put a naming fix in its own proposal, with "changes" holding only these items, and quote the printed words in "source_quote".
"source_lot" and "source_quote" may be null when the value is computed."""


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
    if desc.street_aliases:
        lines.append("  street names printed as renamed: " + "; ".join(desc.street_aliases))
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


_STREET_SUFFIX_FORMS: dict[str, tuple[str, ...]] = {
    "avenue": ("avenue", "ave"), "ave": ("avenue", "ave"),
    "boulevard": ("boulevard", "blvd"), "blvd": ("boulevard", "blvd"),
    "street": ("street", "st"), "st": ("street", "st"),
    "road": ("road", "rd"), "rd": ("road", "rd"),
    "drive": ("drive", "dr"), "dr": ("drive", "dr"),
    "place": ("place", "pl"), "pl": ("place", "pl"),
    "lane": ("lane", "ln"), "ln": ("lane", "ln"),
    "parkway": ("parkway", "pkwy"), "pkwy": ("parkway", "pkwy"),
    "court": ("court", "ct"), "ct": ("court", "ct"),
    "terrace": ("terrace", "ter"), "ter": ("terrace", "ter"),
}
# Слова о переименовании. «commonly known as» (адрес) и «now or formerly» (владелец) — не они.
_RENAME_MARK_RE = re.compile(
    r"\(now\b|now\s+known\s+as|(?<!or )formerly|previously|also\s+known\s+as|f/k/a|a/k/a",
    re.IGNORECASE,
)


def _street_in_text(name: str, text: str) -> bool:
    """
    Напечатано ли название улицы в тексте описания. Пробелы, регистр и формы «Blvd» /
    «Boulevard» не важны; остальные буквы — важны, поэтому чужая улица не совпадёт.
    """
    tokens = re.findall(r"[a-z0-9]+", str(name or "").lower())
    forms: tuple[str, ...] = ()
    if len(tokens) > 1 and tokens[-1] in _STREET_SUFFIX_FORMS:
        forms = _STREET_SUFFIX_FORMS[tokens[-1]]
        tokens = tokens[:-1]
    core = "".join(tokens)
    if not core:
        return False
    compact = re.sub(r"[^a-z0-9]", "", str(text or "").lower())
    tail = "(?:" + "|".join(forms) + ")" if forms else ""
    return re.search(re.escape(core) + tail, compact) is not None


_STREET_NAME_IN_TEXT_RE = re.compile(
    r"((?:[A-Za-z][A-Za-z'.-]*\s+){0,3}[A-Za-z][A-Za-z'-]*)\s+("
    + "|".join(sorted(_STREET_SUFFIX_FORMS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)
#: Насколько похожи два написания одной улицы (0..1), чтобы считать опиской, а не другой улицей.
STREET_TYPO_SIMILARITY = 0.5


def _street_name_proposals(scene: _Scene, lot: LotInput) -> list[Proposal]:
    """
    Перекрёсток, которого нет в тексте участка, но есть похожая улица, напечатанная в нём же
    и подтверждённая соседом набора. Пример: «Boby Avenue» вместо напечатанного «Doty Avenue»,
    а у соседа перекрёсток «Doty Avenue». Одна подходящая улица — находка; несколько — молчим.
    Правка только названия: геометрию не меняет, поэтому вердикт «подтверждено частично».
    """
    text = lot.source_text or ""
    base = scene.virtual.get(lot.key, ())
    desc = scene.description(lot, base)
    if desc is None or not desc.cross_street or not text.strip():
        return []
    if _street_in_text(desc.cross_street, text):
        return []
    cross_tokens = re.findall(r"[a-z0-9]+", desc.cross_street.lower())
    if len(cross_tokens) < 2 or cross_tokens[-1] not in _STREET_SUFFIX_FORMS:
        return []
    cross_core = " ".join(cross_tokens[:-1])
    cross_suffix = _STREET_SUFFIX_FORMS[cross_tokens[-1]][0]
    canon = acad_builder_street_canon(*[scene.description(other, scene.virtual.get(other.key, ()))
                                        or other.description for other in scene.lots])
    own_main = canon(desc.main_street)
    agreed = {
        canon(street)
        for other in scene.lots if other is not lot
        for street in (other.description.cross_street, other.description.main_street)
        if street
    }
    found: dict[str, str] = {}
    width = len(cross_tokens) - 1
    for match in _STREET_NAME_IN_TEXT_RE.finditer(text):
        words = re.findall(r"[A-Za-z0-9]+", match.group(1))
        suffix = match.group(2).lower()
        if len(words) < width or width < 1:
            continue
        core_words = words[-width:]
        candidate_core = " ".join(core_words).lower()
        candidate_suffix = _STREET_SUFFIX_FORMS[suffix][0]
        if candidate_suffix != cross_suffix:
            continue
        ratio = difflib.SequenceMatcher(None, cross_core, candidate_core).ratio()
        if ratio < STREET_TYPO_SIMILARITY:
            continue
        full = " ".join(core_words) + " " + match.group(2)
        key = canon(full)
        if key == canon(desc.cross_street) or key == own_main or key not in agreed:
            continue
        found[key] = full
    if len(found) != 1:
        return []
    new_name = next(iter(found.values()))
    change = Change(
        target="street", index=-1, field="cross", new_label=new_name, old_label=desc.cross_street,
    )
    before = scene.metrics(lot, base, state=scene.virtual)
    after = scene.metrics(lot, base + (change,), state=scene.virtual)
    if before is None or after is None:
        return []
    new_key = canon(new_name)
    donor = next(
        other for other in scene.lots if other is not lot and new_key in {
            canon(other.description.cross_street), canon(other.description.main_street),
        }
    )
    return [Proposal(
        id=_proposal_id(lot, [change]), lot_key=lot.key, lot_name=lot.name, kind="street_name", origin="rule",
        title=f"Перекрёсток: «{desc.cross_street}» → «{new_name}» (так напечатано в этом тексте)",
        changes=(change,),
        source=f"текст участка; та же улица у соседа «{donor.name}»",
        reasons=(
            f"«{new_name}» напечатано в тексте участка «{lot.name}»",
            f"у соседа «{donor.name}» та же улица",
            "геометрия не меняется: название подтверждено текстом и соседом",
        ),
        before=before, after=after, verdict="partial",
    )]


def _value_origin(scene: _Scene, lot: LotInput, change: Change) -> str | None:
    """Есть ли предложенное значение в разобранных данных какого-либо участка набора."""
    if change.target == "tie" and change.field == "distance":
        for other in scene.lots:
            if other is lot:
                continue
            if abs(other.description.distance_to_corner_ft - change.distance_ft) <= 0.006:
                return f"«{other.name}», привязка к углу ({other.description.distance_to_corner_ft:.2f}')"
        return None
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
        text = lot.source_text or ""
        for raw in item.get("changes") or []:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("target") or "").strip().lower()
            if kind == "cross_street":
                # Перекрёсток, к которому привязан участок: только название, напечатанное в его тексте.
                name = " ".join(str(raw.get("name") or "").split())
                if not name:
                    problem = "не указано название пересекающей улицы"
                    break
                if not text.strip():
                    problem = "у участка нет текста описания для проверки названия"
                    break
                if not _street_in_text(name, text):
                    problem = f"«{name}» не напечатано в тексте описания этого участка"
                    break
                if desc.cross_street and _normalize_street_key(desc.cross_street) == _normalize_street_key(name):
                    continue
                changes.append(Change(
                    target="street", index=-1, field="cross", new_label=name,
                    old_label=desc.cross_street or "нет",
                ))
                continue
            if kind == "street_alias":
                current = " ".join(str(raw.get("current") or "").split())
                old = " ".join(str(raw.get("old") or "").split())
                if not current or not old:
                    problem = "переименование должно содержать оба названия"
                    break
                if not text.strip():
                    problem = "у участка нет текста описания для проверки переименования"
                    break
                if not (_street_in_text(current, text) and _street_in_text(old, text)):
                    problem = f"«{current}» или «{old}» не напечатаны в тексте описания этого участка"
                    break
                if not _RENAME_MARK_RE.search(text):
                    problem = "в тексте описания нет слова о переименовании (now / formerly / f/k/a)"
                    break
                label = f"{current} = {old}"
                known = {" ".join(alias.split()).lower() for alias in (desc.street_aliases or [])}
                if label.lower() in known:
                    continue
                changes.append(Change(target="street", index=-1, field="alias", new_label=label))
                continue
            target = "tie" if kind == "tie" else "side"
            if target == "tie" and raw.get("distance_ft") not in (None, "", "null"):
                # Советник задаёт расстояние от угла улицы до POB (и, если знает, румб привязки).
                try:
                    tie_distance = float(str(raw.get("distance_ft")).replace(",", ""))
                except ValueError:
                    problem = f"не удалось прочитать расстояние до угла «{raw.get('distance_ft')}»"
                    break
                if tie_distance <= 0:
                    problem = "расстояние до угла должно быть больше нуля"
                    break
                if abs(tie_distance - desc.distance_to_corner_ft) <= 1e-6:
                    continue
                bearing_parts = None
                if raw.get("bearing") not in (None, "", "null"):
                    bearing_parts = parse_bearing_text(raw.get("bearing"))
                    if bearing_parts is None:
                        problem = f"не удалось прочитать румб привязки «{raw.get('bearing')}»"
                        break
                old_tie = desc.corner_to_pob_bearing
                changes.append(Change(
                    target="tie", index=-1, field="distance", distance_ft=tie_distance,
                    ns=bearing_parts[0] if bearing_parts else (old_tie.ns if old_tie else "N"),
                    degrees=bearing_parts[1] if bearing_parts else (old_tie.degrees if old_tie else 0),
                    minutes=bearing_parts[2] if bearing_parts else (old_tie.minutes if old_tie else 0),
                    seconds=bearing_parts[3] if bearing_parts else (old_tie.seconds if old_tie else 0.0),
                    ew=bearing_parts[4] if bearing_parts else (old_tie.ew if old_tie else "E"),
                    bearing_set=bearing_parts is not None,
                    old_label=f"{desc.distance_to_corner_ft:.2f}'",
                    new_label=f"{tie_distance:.2f}'" + (f" {_label(bearing_parts)}" if bearing_parts else ""),
                ))
                continue
            if target == "tie":
                index, old = -1, desc.corner_to_pob_bearing
                if old is None:
                    problem = "у участка нет привязки к углу — дайте расстояние от угла (distance_ft)"
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
        name_only = all(change.is_name for change in changes)
        before = scene.metrics(lot, base, state=scene.virtual)
        after = scene.metrics(lot, base + tuple(changes), state=scene.virtual)
        # Правка названий геометрию не меняет: её подтверждает напечатанный текст, а не замер.
        verdict = "partial" if name_only and before is not None and after is not None else advisor_verdict(before, after)
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
        if name_only:
            reasons.append("название подтверждено текстом описания этого участка; геометрия не меняется")
        for change in changes:
            if change.is_name:
                reasons.append(f"{change.where}: «{change.new_label}» напечатано в тексте участка «{lot.name}»")
                continue
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
# Склейка по карте: советник только соединяет фигуры (угол к углу, общая межа)
# ---------------------------------------------------------------------------

JOIN_SYSTEM_PROMPT = """You are a land surveyor's assistant. You join the figures of ADJACENT lots (New York City tax lots, metes-and-bounds descriptions).

Python has parsed every lot and draws each figure from its own tie corner, which is the origin (0, 0) of the drawing. Image 1 is a sketch of the figures as the program places them now, with numbered corners and numbered sides. Coordinates are in feet. Other attached images are the document pages and, when present, the tax map.

YOUR ONLY TASK: say which corner of our lot (L1) coincides with which corner of a neighbour, and which side of each is the shared boundary. Do nothing else. You do not change lengths, bearings, distances or names. Python moves the neighbour as a rigid body (no change of its shape or size) and checks the result numerically. A surveyor confirms it.

NUMBERING: corner k is the start of side k; side k runs from corner k to corner k+1, and the last side ends at corner 1.

A JOIN has: corner_a (a corner of L1), corner_b (a corner of the neighbour), side_a (a side of L1) and side_b (a side of the neighbour). The shared boundary is side_a of L1 and side_b of the neighbour. Python places the neighbour so that corner_b lands on corner_a and side_b lies along side_a on one line, running in the opposite direction, because the two lots are on opposite sides of their common boundary.

RULES:
- The shorter of the two shared sides must lie inside the longer one. A longer side may continue past the shorter one; the rest of it then borders a third lot. This is normal.
- Both sides must have a printed bearing (not "NO BEARING PRINTED").
- Use only the printed values and the printed words. Never invent a corner, a side or a number.
- Choose the join from the documents: the printed streets, the printed tie of the neighbour and the printed courses along the boundary.
- Join each neighbour at most once. An empty "joins" list is a valid answer when no join is clearly supported.

OUTPUT: exactly one JSON object and nothing else:
{
  "analysis": "2 to 4 sentences in Russian: which boundary is shared and why",
  "joins": [
    {"lot_a": "L1", "corner_a": 3, "lot_b": "L2", "corner_b": 2, "side_a": 3, "side_b": 3, "reason": "why these corners and sides, in Russian", "source_quote": "the printed words that support the choice"}
  ]
}
All corner and side numbers are integers."""

#: Допуск совпадения направлений общих сторон при переносе без поворота (как у сверки румбов).
JOIN_SHIFT_TOLERANCE_DEG = ALTERNATE_AXIS_TOLERANCE_ARCSEC / 3600.0
#: Допуск, на котором общая межа считается лежащей на одной прямой.
JOIN_LINE_TOLERANCE_FT = 0.05
#: Общая межа должна совпасть не меньше чем на эту долю длины большей из двух сторон (исключает дикие совпадения).
JOIN_MIN_SHARED_FRACTION = 0.40


def _wrap_rad(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _rotate_vec(vec: tuple[float, float], theta: float) -> tuple[float, float]:
    c, s = math.cos(theta), math.sin(theta)
    return (c * vec[0] - s * vec[1], s * vec[0] + c * vec[1])


def _vec_azimuth(vec: tuple[float, float]) -> float:
    """Азимут вектора (x — восток, y — север), градусы, по часовой от севера."""
    return math.degrees(math.atan2(vec[0], vec[1])) % 360.0


def _join_candidates(
    scene: _Scene, lot_a: LotInput, lot_b: LotInput, corner_a: int, side_a: int,
    corner_b: int, side_b: int, mode: str,
) -> tuple[list[tuple[list[Change], str]], str | None]:
    """
    Жёсткие соединения соседа с нашим участком: ``([(правки, описание), ...], причина отказа)``.

    Угол ``corner_b`` соседа совмещается с углом ``corner_a`` нашего участка; сторона ``side_b``
    ложится на сторону ``side_a`` на одну прямую. Направление зависит от обхода участков:
    у участков одного обхода (оба по часовой) общая межа проходит в противоположных
    направлениях, у разных — в одном. Поэтому пробуются оба варианта, и геометрия выбирает.
    ``mode`` = ``turn`` — перенос и поворот; ``shift`` — только перенос (направление уже совпадает).
    Длины и форма соседа не меняются.
    """
    state = scene.virtual
    base_a = state.get(lot_a.key, ())
    base_b = state.get(lot_b.key, ())
    desc_a = scene.description(lot_a, base_a)
    desc_b = scene.description(lot_b, base_b)
    plan_a = scene.plan(lot_a, base_a)
    plan_b = scene.plan(lot_b, base_b)
    if desc_a is None or desc_b is None or plan_a is None or plan_b is None:
        return [], "участок с такой склейкой не строится"
    va, vb = plan_a.vertices, plan_b.vertices
    if not (1 <= corner_a <= len(va) and 1 <= side_a <= len(desc_a.segments)):
        return [], f"у нашего участка нет угла {corner_a} или стороны {side_a}"
    if not (1 <= corner_b <= len(vb) and 1 <= side_b <= len(desc_b.segments)):
        return [], f"у соседа нет угла {corner_b} или стороны {side_b}"
    seg_a = desc_a.segments[side_a - 1]
    seg_b = desc_b.segments[side_b - 1]
    if not seg_a.has_bearing:
        return [], f"у стороны {side_a} нашего участка нет напечатанного румба"
    if not seg_b.has_bearing:
        return [], f"у стороны {side_b} соседа нет напечатанного румба"

    da = segment_vector(seg_a)
    db = segment_vector(seg_b)
    len_a, len_b = math.hypot(*da), math.hypot(*db)
    if len_a < 1e-6 or len_b < 1e-6:
        return [], "нулевая сторона не может быть общей межой"
    pa = va[corner_a - 1]
    pb = vb[corner_b - 1]
    angle_a = math.atan2(da[1], da[0])
    angle_b = math.atan2(db[1], db[0])
    # Поворот, при котором стороны идут противоположно (одинаковый обход) или в одну сторону (разный).
    turns = [_wrap_rad(angle_a + math.pi - angle_b), _wrap_rad(angle_a - angle_b)]
    if mode == "shift":
        turns = [t for t in turns if abs(t) <= math.radians(JOIN_SHIFT_TOLERANCE_DEG)]
        if not turns:
            return [], "при переносе без поворота стороны не совпадают по направлению"

    ux, uy = da[0] / len_a, da[1] / len_a
    candidates: list[tuple[list[Change], str]] = []
    problems: list[str] = []
    for theta in turns:
        changes, problem, summary = _join_with_turn(
            desc_b, lot_b, pa, pb, va, vb, da, db, ux, uy, len_a, len_b,
            corner_a, side_a, corner_b, side_b, theta,
        )
        if problem:
            problems.append(problem)
        else:
            candidates.append((changes, summary))
    if candidates:
        return candidates, None
    return [], (problems[0] if problems else "соединение не получилось")


def _join_with_turn(
    desc_b: LegalDescription, lot_b: LotInput, pa, pb, va, vb, da, db, ux, uy, len_a, len_b,
    corner_a: int, side_a: int, corner_b: int, side_b: int, theta: float,
) -> tuple[list[Change], str | None, str]:
    """Одно соединение при заданном повороте: проверка общей межи и правки данных соседа."""

    def place(point: tuple[float, float]) -> tuple[float, float]:
        rx, ry = _rotate_vec((point[0] - pb[0], point[1] - pb[1]), theta)
        return rx + pa[0], ry + pa[1]

    a0 = va[side_a - 1]
    b0 = place(vb[side_b - 1])
    rotated_db = _rotate_vec(db, theta)
    b1 = (b0[0] + rotated_db[0], b0[1] + rotated_db[1])
    for qx, qy in (b0, b1):
        offset = ux * (qy - a0[1]) - uy * (qx - a0[0])
        if abs(offset) > JOIN_LINE_TOLERANCE_FT:
            return [], f"общая межа не лежит на одной прямой (отклонение {abs(offset):.2f} ft)", ""
    s0 = ux * (b0[0] - a0[0]) + uy * (b0[1] - a0[1])
    s1 = ux * (b1[0] - a0[0]) + uy * (b1[1] - a0[1])
    overlap = min(len_a, max(s0, s1)) - max(0.0, min(s0, s1))
    longer = max(len_a, len_b)
    required = JOIN_MIN_SHARED_FRACTION * longer
    if overlap < required - JOIN_LINE_TOLERANCE_FT:
        return [], (
            f"общая межа совпадает лишь на {max(overlap, 0.0):.2f} ft; нужно не меньше "
            f"{required:.2f} ft ({JOIN_MIN_SHARED_FRACTION:.0%} большей стороны {longer:.2f} ft)"
        ), ""

    pob = place(vb[0])
    distance = math.hypot(*pob)
    if distance < 1e-6:
        return [], "после соединения сосед совпал бы с углом улицы", ""
    parts = _components(_vec_azimuth(pob))
    changes: list[Change] = [Change(
        target="tie", index=-1, field="distance", distance_ft=distance,
        ns=parts[0], degrees=parts[1], minutes=parts[2], seconds=parts[3], ew=parts[4],
        bearing_set=True,
        old_label=f"{desc_b.distance_to_corner_ft:.2f}'",
        new_label=f"{distance:.2f}' {_label(parts)}",
    )]
    if abs(theta) > 1e-12:
        for index, seg in enumerate(desc_b.segments):
            if not seg.has_bearing:
                return [], (
                    f"у стороны {index + 1} соседа нет румба в документе — поворот невозможен; "
                    "выберите режим «только перенос»"
                ), ""
            turned = _rotate_vec(segment_vector(seg), theta)
            changes.append(_bearing_change("side", index, seg, _components(_vec_azimuth(turned))))
    summary = (
        f"угол {corner_a} нашего участка = угол {corner_b} «{lot_b.name}»; общая межа — сторона {side_a} "
        f"и сторона {side_b}; {'стороны в одном направлении' if _is_same_direction(theta, da, db) else 'стороны навстречу'}; "
        f"поворот {math.degrees(theta):+.4f}°; общая часть {min(len_a, len_b):.2f} ft"
    )
    return changes, None, summary


def _is_same_direction(theta: float, da, db) -> bool:
    """Идут ли общие стороны в одну сторону после поворота (иначе — навстречу)."""
    rotated = _rotate_vec(db, theta)
    return rotated[0] * da[0] + rotated[1] * da[1] > 0


def build_join_request(
    scene: _Scene, chosen: Sequence[Proposal], remaining: Sequence[str], labels: dict[str, LotInput],
    *, text_limit: int = 3000,
) -> str:
    """Текст запроса для режима карты: координаты углов и сторон каждого участка."""
    parts = [
        "Lots: " + ", ".join(f"{label} = {'our lot' if lot.is_main else 'neighbour'} «{lot.name}»"
                             for label, lot in labels.items()) + ".",
        "Only L1 (our lot) may be joined to a neighbour; joins between two neighbours are not asked for.",
        "Image 1 is the sketch of the figures as the program places them now.",
    ]
    for label, lot in labels.items():
        base = scene.virtual.get(lot.key, ())
        plan = scene.plan(lot, base)
        desc = scene.description(lot, base)
        if plan is None or desc is None:
            parts.append(f"{label}: the figure does not build.")
            continue
        n = len(plan.vertices)
        head = [f"{label} — {'our lot' if lot.is_main else 'neighbour'} «{lot.name}»"]
        if desc.cross_street:
            head.append(f"tie: {desc.distance_to_corner_ft:.2f} ft from the corner of {desc.cross_street}")
        head.append(f"closure error {plan.closure.distance:.3f} ft")
        parts.append("; ".join(head))
        for k, (x, y) in enumerate(plan.vertices, 1):
            parts.append(f"  corner {k}: ({x:.2f}, {y:.2f}) ft — start of side {k}")
        for i, seg in enumerate(desc.segments):
            bearing = (seg.bearing_label or bearing_label(seg.ns, seg.degrees, seg.minutes, seg.seconds, seg.ew)
                       if seg.has_bearing else "NO BEARING PRINTED")
            parts.append(
                f"  side {i + 1}: from corner {i + 1} to corner {(i + 1) % n + 1}, {bearing}, {seg.distance_ft:.2f} ft"
            )
        text = " ".join((lot.source_text or "").split())
        if text:
            parts.append("  document text: " + text[:text_limit])
    if chosen:
        parts.append("PYTHON'S VERIFIED FINDINGS (already applied): " + "; ".join(p.title for p in chosen))
    parts.append("CONFLICTS THAT REMAIN (Python's wording, in Russian):")
    parts.append("\n".join(f"- {text}" for text in remaining) or "- none")
    parts.append("Propose joins only. Answer with the JSON object only.")
    return "\n\n".join(parts)


def read_join_reply(data: Any) -> AdvisorReply:
    """Ответ режима карты: только соединения, остальное отбрасывается."""
    reply = AdvisorReply()
    if not isinstance(data, dict):
        return reply
    reply.analysis = " ".join(str(data.get("analysis") or "").split())
    for item in data.get("joins") or []:
        if isinstance(item, dict):
            reply.proposals.append(item)
    return reply


def _join_proposals(
    scene: _Scene, reply: AdvisorReply, labels: dict[str, LotInput], mode: str, chosen: Sequence[Proposal],
) -> tuple[list[Proposal], list[str]]:
    """Проверяет соединения геометрией: принятые находки и строки об отклонённых."""
    accepted: list[Proposal] = []
    rejected: list[str] = []
    requires = tuple(p.id for p in chosen)
    joined: set[str] = set()
    main_labels = {label for label, lot in labels.items() if lot.is_main}
    for item in reply.proposals:
        lot_a_label = str(item.get("lot_a") or "").strip().upper()
        lot_b_label = str(item.get("lot_b") or "").strip().upper()
        lot_a = labels.get(lot_a_label)
        lot_b = labels.get(lot_b_label)
        if lot_a is None or lot_b is None:
            rejected.append(f"соединение {lot_a_label or '?'}–{lot_b_label or '?'} — не указан участок")
            continue
        if lot_a_label not in main_labels or lot_a is lot_b:
            rejected.append(f"«{lot_b.name}»: соединяем только с нашим участком")
            continue
        if lot_b_label in joined:
            rejected.append(f"«{lot_b.name}»: соседу уже назначено соединение")
            continue
        try:
            corner_a = int(item.get("corner_a"))
            side_a = int(item.get("side_a"))
            corner_b = int(item.get("corner_b"))
            side_b = int(item.get("side_b"))
        except (TypeError, ValueError):
            rejected.append(f"«{lot_b.name}»: не указаны номера углов или сторон")
            continue
        title = (
            f"Склейка: угол {corner_a} нашего участка = угол {corner_b} «{lot_b.name}»; "
            f"общая межа — сторона {side_a} и сторона {side_b}"
        )
        candidates, problem = _join_candidates(
            scene, lot_a, lot_b, corner_a, side_a, corner_b, side_b, mode,
        )
        if problem:
            rejected.append(f"{title} — {problem}")
            continue
        base = scene.virtual.get(lot_b.key, ())
        before = scene.metrics(lot_b, base, state=scene.virtual)
        chosen_join = None
        last_detail = ""
        for changes, summary in candidates:
            after = scene.metrics(lot_b, base + tuple(changes), state=scene.virtual)
            verdict = advisor_verdict(before, after, allow_stepped=True)
            if verdict != "rejected" and before is not None and after is not None:
                if chosen_join is None or verdict == "verified":
                    chosen_join = (changes, summary, after, verdict)
                if verdict == "verified":
                    break
            elif before is None or after is None:
                last_detail = "участок с такой склейкой не строится"
            else:
                last_detail = (
                    f"невязка {before.closure_ft:.3f} → {after.closure_ft:.3f} ft, "
                    f"наложение {before.overlap_sqft:,.1f} → {after.overlap_sqft:,.1f} sq ft"
                )
        if chosen_join is None:
            rejected.append(f"{title} — совет не подтверждён геометрией: {last_detail}")
            continue
        changes, summary, after, verdict = chosen_join
        if before is None:
            continue
        stepped_note = (
            f"уступ задней межи: {after.stepped_edges} сторона(ы) короче соседней — остаток граничит "
            "с третьим участком (проверьте по карте)"
            if after.stepped_edges else ""
        )
        joined.add(lot_b_label)
        reasons: list[str] = []
        reason = " ".join(str(item.get("reason") or "").split())
        if reason:
            reasons.append(f"довод советника: {reason}")
        reasons.append(f"соединение: {summary}")
        if stepped_note:
            reasons.append(stepped_note)
        reasons.append(
            "жёсткое перемещение: длины и форма соседа не меняются; расстояние и румб привязки "
            f"соседа вычислены от нашего угла ({changes[0].new_label}), в документе они не напечатаны"
        )
        quote = " ".join(str(item.get("source_quote") or "").split())
        source = "ИИ-советник по карте"
        if quote:
            source += f"; цитата: «{quote[:220]}»"
        accepted.append(Proposal(
            id=_proposal_id(lot_b, changes), lot_key=lot_b.key, lot_name=lot_b.name,
            kind="advisor", origin="ai", title=title, changes=tuple(changes), source=source,
            reasons=tuple(reasons), before=before, after=after, verdict=verdict, requires=requires,
        ))
    return accepted, rejected


def _join_sketch_url(scene: _Scene, lots: Sequence[LotInput]) -> str | None:
    """Схема фигур как программа их расставляет сейчас; без неё советник работает по тексту."""
    from join_sketch import as_data_url, render_sketch

    figures = []
    for label, lot in ((f"L{i}", lot) for i, lot in enumerate(sorted(lots, key=lambda lot: not lot.is_main), 1)):
        plan = scene.plan(lot, scene.virtual.get(lot.key, ()))
        if plan is not None:
            figures.append((label, lot.name, lot.is_main, list(plan.vertices)))
    if not figures:
        return None
    try:
        return as_data_url(render_sketch(figures))
    except Exception:  # noqa: BLE001 - схема не должна срывать запрос
        return None


def _join_round(
    scene: _Scene, lots: Sequence[LotInput], chosen: Sequence[Proposal], analysis: ConflictAnalysis,
    proposals: list[Proposal], advisor: AdvisorCall | None, *, mode: str,
    extra_images: Sequence[str], max_images: int, progress: Callable[[str], None] | None,
) -> None:
    """Один запрос режима карты: соединения советника, проверенные геометрией."""
    analysis.join_mode = mode
    labels: dict[str, LotInput] = {
        f"L{i}": lot for i, lot in enumerate(sorted(lots, key=lambda lot: not lot.is_main), 1)
    }
    if advisor is None:
        analysis.advisor_status = "skipped"
        analysis.advisor_note = analysis.advisor_note or "не запрошен"
        return
    try:
        text = build_join_request(scene, chosen, analysis.remaining, labels)
        sketch = _join_sketch_url(scene, lots)
        if not sketch:
            # Без схемы промпт (картинка 1 — схема) неверен: запрос не отправляем.
            raise ValueError("схема фигур не построена")
        images: list[str] = [sketch]
        for lot in sorted(lots, key=lambda lot: not lot.is_main):
            if lot.images and len(images) + len(lot.images) <= max(0, int(max_images)):
                images.extend(lot.images)
        images.extend(extra_images)
        if progress:
            progress("ИИ-советник соединяет фигуры по карте…")
        data, usage = advisor(JOIN_SYSTEM_PROMPT, text, images)
        reply = read_join_reply(data)
        reply.usage = usage
        reply.model = str(getattr(usage, "model", "") or "")
        analysis.advisor_status = "ok"
    except Exception as exc:  # noqa: BLE001 - сбой советника не срывает разбор набора
        analysis.advisor_status = "error"
        reason = " ".join(str(exc).split()) or type(exc).__name__
        analysis.advisor_note = f"запрос не удался — {reason}"
        return
    analysis.advisor_reply = reply
    accepted, rejected = _join_proposals(scene, reply, labels, mode, chosen)
    known = {p.id for p in proposals}
    proposals.extend(p for p in accepted if p.id not in known)
    analysis.advisor_rejected = rejected


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

AdvisorCall = Callable[[str, str, list[str]], tuple[Any, Any]]


def _accept_in_mind(scene: _Scene, proposals: Sequence[Proposal]) -> list[Proposal]:
    """
    Подтверждённые находки принимаются «в уме»: остаток расхождений считается
    так, как будто геодезист нажал «Применить». Данные участков не меняются.
    """
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
    return chosen


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
    join_mode: str | None = None,
    join_images: Sequence[str] = (),
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
        proposals.extend(_street_name_proposals(scene, lot))
    proposals.sort(key=lambda p: (not p.verified, p.after.closure_ft))
    chosen = _accept_in_mind(scene, proposals)

    # Второй проход: длины, напечатанные дважды (deed / tax map), сверяются уже
    # с учётом подтверждённых находок первого прохода — румб улицы ставит POB
    # на линию улицы, и общая межа соседа встаёт на своё место.
    for lot in sorted(lots, key=lambda lot: not lot.is_main):
        proposals.extend(_printed_alternate_proposals(scene, lot, state=scene.virtual, pending=proposals))
    proposals.sort(key=lambda p: (not p.verified, p.after.closure_ft))
    chosen = _accept_in_mind(scene, proposals)

    analysis.conflicts, _, _ = _conflicts(scene, {})
    analysis.remaining, analysis.remaining_hard, report = _conflicts(scene, scene.virtual)

    if join_mode in ("turn", "shift"):
        # Режим карты: советник только соединяет фигуры; кэш обычного ответа не трогаем.
        _join_round(
            scene, lots, chosen, analysis, proposals, advisor, mode=join_mode,
            extra_images=tuple(join_images), max_images=max_images, progress=progress,
        )
        analysis.proposals = proposals
        return analysis

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
                reason = " ".join(str(exc).split()) or type(exc).__name__
                # «ИИ отвечает криво: …» говорит само за себя — не приписываем
                # к нему ещё и «запрос не удался».
                if reason.lower().startswith("ии отвечает криво"):
                    # «ИИ-советник: отвечает криво: …» — без повторного «ИИ».
                    reason = re.sub(r"^ИИ\s+", "", reason, count=1)
                    analysis.advisor_note = f"{reason}. Находки Python от советника не зависят и остаются на месте"
                else:
                    analysis.advisor_note = f"запрос не удался — {reason}"
    if reply is not None:
        analysis.advisor_reply = reply
        accepted, analysis.advisor_rejected = _advisor_proposals(scene, reply, labels, chosen)
        known = {p.id for p in proposals}
        proposals.extend(p for p in accepted if p.id not in known)
    analysis.proposals = proposals
    return analysis
