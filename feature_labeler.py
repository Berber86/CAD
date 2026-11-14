"""
feature_labeler.py
==================
Шаг 2 рабочего сценария геодезиста: автоматическое распознавание объектов на
участке (Feature Labeling) — после усадки границы участка и ПЕРЕД расстановкой
офсетов.

Модуль не обращается к AutoCAD: он получает «снимок» чертежа
(:class:`DrawingSnapshot` — линии и тексты, прочитанные из DWG) и средствами
``shapely`` выполняет чистую геометрию:

  * классифицирует слои входящего чертежа по ``config.json``
    (раздел ``feature_labeling``);
  * находит полигон участка (автопоиск по слоям границы или по объектам,
    указанным геодезистом в AutoCAD);
  * вокруг участка (``Property Polygon`` + буфер 50 ft) собирает заборы,
    строения, крыльца, ступени, дорожки и точечные объекты;
  * определяет текст подписи по двум источникам:
      1. коды геодезических точек (``'2 FEC'``, ``'3 FEV64'``) — привязка
         к ближайшей линии забора в радиусе 3.5 ft;
      2. имя слоя (``FENCE_CHAIN*`` -> ``CHAIN LINK FENCE`` и т. д.),
         включая подсчёт проступей ``*_STEP_TREADS`` -> ``"3 STEPS"``.

Размещение подписей в слое ``D-TOPO-TEXT`` (дедупликация, текст вдоль забора,
выноски, проверка пересечений) выполняет ``acad_builder.py``.

Система координат: WCS чертежа, единицы — футы.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Sequence

try:
    from shapely.geometry import LineString, Point, Polygon
    from shapely.geometry.base import BaseGeometry
    from shapely.ops import linemerge, polygonize, polygonize_full, substring, unary_union
    from shapely.strtree import STRtree
except ImportError as exc:  # pragma: no cover - зависит от установки
    raise ImportError(
        "Не установлена библиотека shapely (нужна для Шага 2 — автоподписания объектов). "
        "Выполните: pip install -r requirements.txt"
    ) from exc

__all__ = [
    "DEFAULT_FEATURE_CONFIG",
    "DrawingLine",
    "DrawingSnapshot",
    "DrawingText",
    "Feature",
    "FieldNote",
    "FeatureLabelError",
    "LayerClassifier",
    "PointCode",
    "PropertyBoundary",
    "RecognitionResult",
    "bridge_gaps",
    "feature_settings",
    "find_property_polygon",
    "label_tokens",
    "labels_similar",
    "layer_matches",
    "line_to_shapely",
    "lookup_pattern_label",
    "merge_lines",
    "parse_point_code",
    "plain_text",
    "polygons_from_lines",
    "property_polygon_from_selection",
    "recognize_features",
    "snap_endpoints",
]


# ---------------------------------------------------------------------------
# Конфигурация по умолчанию (зеркало раздела "feature_labeling" в config.json)
# ---------------------------------------------------------------------------

#: Значения по умолчанию. Раздел ``feature_labeling`` из config.json замещает
#: ключи целиком (словари не сливаются: то, что записано в config.json, —
#: полный и окончательный список правил). ``null`` в config.json = «по умолчанию».
DEFAULT_FEATURE_CONFIG: dict[str, Any] = {
    # --- слои входящего чертежа (шаблоны * и ?, регистр не важен) ----------
    "property_line_layers": ["PROPERTY_LINE", "PL", "M-TAX-LINE", "PROPERTY_LINES_APPROX"],
    "point_label_layers": ["POINT_LABELS", "PNT-X", "PNTS"],
    "fence_layers": ["S-FENCE", "ST-WALL", "CODE_CREW", "FENCE_*"],
    "fence_layer_exclude": ["*_NOTE"],
    #: калитки и ворота: подпись «<сокращение материала> / GATE» поперёк линии
    "gate_layers": ["GATE_*", "*_GATE"],
    "structure_layers": {
        "house": ["BUILDING_OUTLINE", "ST-BLDG-LINE", "REF_VEC_BUILDING"],
        "shed": ["*SHED*"],
        "porch": ["*PORCH*", "*LANDING*", "*DECK*"],
        "steps": ["*_STEPS", "*STAIR*", "ST-STEP"],
        "planter": ["*PLANTER*"],
        "surface": [
            "DRIVEWAY_EDGE",
            "SIDEWALK_EDGE",
            "WALK_EDGE",
            "WALK_*_SKETCH",
            "GRASS_STRIP_EDGE",
        ],
    },
    "step_tread_layers": ["*_STEP_TREADS"],
    "label_output_layer": "D-TOPO-TEXT",
    "label_output_layer_color": 7,
    #: слои готовых подписей (итоговый чертёж): по ним проверяются дубликаты
    "existing_label_layers": ["D-TOPO-TEXT"],
    # --- источник 1: заметки полевого чертёжника -----------------------------
    #: слои полевых заметок: текст заметки переводится в формулировку заказчика
    #: и ставится в D-TOPO-TEXT у объекта, на который указывает заметка
    "field_note_layers": ["FENCE_NOTE", "ENTRANCE_TEXT", "SURFACE_TEXT", "CONTEXT_TEXT"],
    #: Правила перевода. Срабатывает ПЕРВОЕ подходящее правило. ``target`` —
    #: категория объекта, к которому привязывается подпись: fence, gate, shed,
    #: porch, steps, planter, surface либо ground (покрытие/грунт: подпись
    #: ставится в точке заметки). ``labels`` — сразу несколько подписей
    #: {категория: подпись}. Пустая подпись — заметка не переносится.
    #: Перенос строки в подписи — ``\n``.
    "field_note_rules": [
        {"match": "*HOUSE*", "label": ""},
        {"match": "*DWELLING*", "label": ""},
        {"match": "*TIMBER*EDGE*", "label": ""},
        {"match": "*POLE*", "label": ""},
        {"match": "*CHAIN*LINK*GATE*", "label": "C.L.F.\nGATE", "target": "gate"},
        {"match": "*VINYL*GATE*", "label": "V.FE.\nGATE", "target": "gate"},
        {"match": "*WOOD*GATE*", "label": "W.FE.\nGATE", "target": "gate"},
        {"match": "*IRON*GATE*", "label": "I.FE.\nGATE", "target": "gate"},
        {"match": "*GATE*", "label": "GATE", "target": "gate"},
        {"match": "*CHAIN*LINK*", "label": "CHAIN LINK FENCE", "target": "fence"},
        {"match": "*VINYL*FENCE*", "label": "VINYL FENCE", "target": "fence"},
        {"match": "*STOCKADE*", "label": "WOOD FENCE", "target": "fence"},
        {"match": "*WOOD*FENCE*", "label": "WOOD FENCE", "target": "fence"},
        {"match": "*IRON*FENCE*", "label": "IRON FENCE", "target": "fence"},
        {
            "match": "*WOOD*PORCH*STEP*",
            "labels": {"porch": "FRAME\nDECK", "steps": "FRAME\nSTEPS"},
        },
        {"match": "*PORCH*TREAD*", "labels": {"porch": "O.P.", "steps": "CONC.\nSTEPS"}},
        {"match": "*PORCH*STEP*", "labels": {"porch": "O.P.", "steps": "CONC.\nSTEPS"}},
        {"match": "*WOOD*STEP*", "label": "FRAME\nSTEPS", "target": "steps"},
        {"match": "*WOOD*STAIR*", "label": "FRAME\nSTEPS", "target": "steps"},
        {"match": "*WOOD*TREAD*", "label": "FRAME\nSTEPS", "target": "steps"},
        {"match": "*BRICK*STEP*", "label": "BRICK\nSTEPS", "target": "steps"},
        {"match": "*CONC*STEP*", "label": "CONC.\nSTEPS", "target": "steps"},
        {"match": "*STEP*", "label": "STEPS", "target": "steps"},
        {"match": "*STAIR*", "label": "STEPS", "target": "steps"},
        {"match": "*TREAD*", "label": "STEPS", "target": "steps"},
        {"match": "*WOOD*PORCH*", "label": "FRAME\nDECK", "target": "porch"},
        {"match": "*DECK*", "label": "FRAME\nDECK", "target": "porch"},
        {"match": "*PORCH*", "label": "O.P.", "target": "porch"},
        {"match": "*SHED*", "label": "SHED", "target": "shed"},
        {"match": "*PLANTER*", "label": "PLANTER", "target": "planter"},
        {"match": "*PAVER*", "label": "DECO. PAVERS", "target": "ground"},
        {"match": "*ASPHALT*", "label": "ASPHALT", "target": "ground"},
        {"match": "*BLACKTOP*", "label": "ASPHALT", "target": "ground"},
        {"match": "*GRAVEL*", "label": "GRAVEL", "target": "ground"},
        {"match": "*CONC*WALK*", "label": "CONC. WALK", "target": "ground"},
        {"match": "*CONC*", "label": "CONC.", "target": "ground"},
        {"match": "*GRASS*", "label": "GRASS", "target": "ground"},
        {"match": "*LAWN*", "label": "GRASS", "target": "ground"},
        {"match": "*GROUND*", "label": "EARTH", "target": "ground"},
        {"match": "*EARTH*", "label": "EARTH", "target": "ground"},
        {"match": "*DIRT*", "label": "EARTH", "target": "ground"},
        {"match": "*SOIL*", "label": "EARTH", "target": "ground"},
        {"match": "*YARD*", "label": "YARD", "target": "ground"},
        {"match": "*DRIVEWAY*", "label": "DRIVEWAY", "target": "ground"},
    ],
    #: заметка привязывается к объекту, если конец её выноски (или сам текст)
    #: не дальше этого расстояния от объекта
    "field_note_bind_radius_ft": 5.0,
    # --- источник 2: коды геодезических точек ------------------------------
    "point_code_dictionary": {
        "FEC*": "CHAIN LINK FENCE",
        "FEV*": "VINYL FENCE",
        "FEW*": "WOOD FENCE",
        "FEI*": "IRON FENCE",
        "TTP*": "UTILITY POLE",
        "LP*": "UTILITY POLE",
        "UP*": "UTILITY POLE",
        "CB*": "CATCH BASIN",
        "MH*": "MANHOLE",
        "HYD*": "HYDRANT",
    },
    #: коды, которые привязываются к линиям заборов (остальные — точечные объекты)
    "fence_point_codes": ["FE*"],
    #: коды углов объектов (дом, крыльцо, ступени…): подпись не нужна, в отчёте
    #: «коды не из словаря» они не показываются
    "point_codes_without_label": [
        "BLC*", "DW*", "OP*", "PLAN*", "SHED*", "STEP*", "SW*", "LST*", "FD*", "XP*",
    ],
    "point_to_fence_max_ft": 3.5,
    #: столбы, колодцы, гидранты: заказчик их не подписывает (true — подписывать)
    "label_point_features": False,
    # --- источник 3: имена слоёв --------------------------------------------
    "layer_label_mapping": {
        "FENCE_CHAIN*": "CHAIN LINK FENCE",
        "FENCE_VINYL*": "VINYL FENCE",
        "FENCE_WOOD*": "WOOD FENCE",
        "FENCE_IRON*": "IRON FENCE",
        "*SHED*": "SHED",
        "*PORCH*": "O.P.",
        "*LANDING*": "O.P.",
        "*DECK*": "FRAME\nDECK",
        "*_STEPS": "STEPS",
        "*STAIR*": "STEPS",
        "ST-STEP": "STEPS",
        "*PLANTER*": "PLANTER",
        "DRIVEWAY_EDGE": "DRIVEWAY",
        "SIDEWALK_EDGE": "CONC. WALK",
        "WALK_EDGE": "CONC. WALK",
        "WALK_*_SKETCH": "CONC. WALK",
        "GRASS_STRIP_EDGE": "GRASS",
    },
    #: сокращение материала забора для калиток и коротких участков
    "fence_abbreviations": {
        "CHAIN LINK FENCE": "C.L.F.",
        "VINYL FENCE": "V.FE.",
        "WOOD FENCE": "W.FE.",
        "IRON FENCE": "I.FE.",
    },
    "gate_label_format": "{abbr}\nGATE",
    "gate_label_plain": "GATE",
    "gate_adjacent_tolerance_ft": 1.0,
    #: true — считать проступи и писать «3 STEPS» (заказчик количество не пишет)
    "step_count_labels": False,
    "step_count_label_format": "{n} STEPS",
    "step_count_label_format_single": "{n} STEP",
    # --- зона поиска и допуски геометрии ------------------------------------
    "search_buffer_ft": 50.0,
    "min_property_area_sqft": 200.0,
    "boundary_snap_tolerance_ft": 0.05,
    "boundary_gap_tolerance_ft": 2.0,
    "merge_tolerance_ft": 0.05,
    "min_feature_length_ft": 0.5,
    "min_area_width_ft": 0.75,
    "edge_pair_max_gap_ft": 12.0,
    "ignore_hidden_layers": True,
    "obstacle_layers": ["*"],
    "obstacle_layer_exclude": [],
    # Тексты-препятствия — только те, что остаются в итоговом чертеже: готовые
    # подписи (existing_label_layers) и подписи границы Шага 1. Tax-тексты
    # полевого чертежа («#87 LOT 70», румбы красных линий) подписям не мешают.
    "text_obstacle_layers": ["BOUNDARY_TEXT*"],
    "point_labels_as_obstacles": False,
    # --- дедупликация --------------------------------------------------------
    "skip_existing_labels": True,
    "duplicate_radius_ft": 6.0,
    "label_synonyms": {
        "CHAIN LINK FENCE": ["C.L.F.", "C.L. FENCE", "CHAIN LINK FNC."],
        "VINYL FENCE": ["V.FE.", "VIN.FE.", "PVC FENCE"],
        "WOOD FENCE": ["W.FE.", "WD. FENCE", "STOCKADE FENCE"],
        "IRON FENCE": ["I.FE.", "W.I. FENCE"],
        "UTILITY POLE": ["U.P.", "UTIL. POLE", "U. POLE"],
        "CATCH BASIN": ["C.B."],
        "MANHOLE": ["M.H."],
        "HYDRANT": ["HYD."],
        "CONC. WALK": ["CONCRETE WALK", "CONCRETE SIDEWALK", "CONC. SIDEWALK"],
        "DRIVEWAY": ["DRWY.", "D/W"],
        "FRAME DECK": ["WOOD DECK", "WD. DECK"],
        "O.P.": ["OPEN PORCH", "PORCH"],
        "STEPS": ["STEP", "STAIRS"],
    },
    # --- размещение подписей (acad_builder.py) -------------------------------
    "text_height_ft": None,
    "text_height_factor": 1.0,
    #: вертикальный текст читается сверху вниз ("down", как у заказчика) или
    #: снизу вверх ("up")
    "vertical_text_reads": "down",
    #: после подписания выключить слои полевых заметок (их заменили подписи)
    "hide_field_notes_after_labeling": True,
    "fence_min_extra_length_ft": 4.0,
    "fence_text_offset_factor": 0.8,
    "fence_end_margin_ft": 2.0,
    #: подпись ставится на каждом прямом участке забора не короче этой длины
    "fence_label_every_run_min_ft": 25.0,
    #: одинаковые подписи заборов не ставятся ближе этого расстояния
    "fence_label_spacing_ft": 15.0,
    #: участки забора короче этой длины не подписываются
    "fence_min_label_length_ft": 3.0,
    "straight_angle_tolerance_deg": 2.0,
    "text_clearance_factor": 0.3,
    "line_clearance_factor": 0.15,
    "inside_clearance_factor": 0.3,
    "mtext_line_spacing_factor": 1.67,
    #: "arc" — короткая дуговая выноска полилинией со стрелкой (как у заказчика);
    #: "leader" — объект AddLeader с полкой
    "leader_style": "arc",
    "leader_arc_bulge": 0.25,
    "leader_min_length_factor": 2.5,
    "leader_max_length_factor": 12.0,
    "leader_landing_factor": 1.0,
    "leader_text_gap_factor": 0.5,
    "leader_arrow_size_factor": 0.8,
    "leader_attach_annotation": True,
}


def feature_settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """
    Итоговые настройки Шага 2: значения по умолчанию + раздел
    ``feature_labeling`` конфигурации (либо сам этот раздел).

    Каждый ключ раздела замещает значение по умолчанию целиком; ``null``
    означает «использовать значение по умолчанию».
    """
    result = copy.deepcopy(DEFAULT_FEATURE_CONFIG)
    section: Any = None
    if isinstance(cfg, dict):
        section = cfg.get("feature_labeling")
        if section is None and "property_line_layers" in cfg:
            section = cfg
    if isinstance(section, dict):
        for key, value in section.items():
            if value is None:
                continue
            result[key] = copy.deepcopy(value)
    return result


class FeatureLabelError(RuntimeError):
    """Понятная пользователю ошибка распознавания (например, не найден участок)."""


# ---------------------------------------------------------------------------
# Сопоставление имён слоёв и кодов с шаблонами
# ---------------------------------------------------------------------------


@lru_cache(maxsize=2048)
def _wild_regex(pattern: str) -> re.Pattern[str]:
    """Шаблон с ``*`` и ``?`` -> регулярное выражение (остальные символы — буквально)."""
    parts: list[str] = []
    for char in str(pattern).strip().upper():
        if char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("^" + "".join(parts) + "$")


def _as_patterns(patterns: Any) -> list[str]:
    if patterns is None:
        return []
    if isinstance(patterns, str):
        return [patterns]
    return [str(item) for item in patterns if str(item).strip()]


def layer_matches(name: str, patterns: Any) -> bool:
    """``True``, если имя слоя/кода подходит хотя бы под один шаблон (без учёта регистра)."""
    return _first_match_index(name, patterns) is not None


def _first_match_index(name: str, patterns: Any) -> int | None:
    key = str(name or "").strip().upper()
    for index, pattern in enumerate(_as_patterns(patterns)):
        if _wild_regex(pattern).match(key):
            return index
    return None


def _pattern_specificity(pattern: str) -> tuple[int, int]:
    """Чем больше буквальных символов и меньше масок, тем точнее правило."""
    text = str(pattern).strip()
    literal = len(text.replace("*", "").replace("?", ""))
    wildcards = text.count("*") + text.count("?")
    return (0 if wildcards else 1, literal)


def lookup_pattern_label(name: str, mapping: Any) -> tuple[str, str] | None:
    """
    Подбирает подпись по словарю ``{шаблон: подпись}``.

    Возвращает ``(подпись, сработавший шаблон)`` самого точного правила: точное
    имя важнее маски, длинная маска важнее короткой (``FENCE_CHAIN*`` > ``FENCE_*``).
    Пустая подпись (``""`` / ``null``) отключает правило.
    """
    if not isinstance(mapping, dict):
        return None
    key = str(name or "").strip().upper()
    best: tuple[tuple[int, int], str, str] | None = None
    for pattern, label in mapping.items():
        text = str(label or "").strip()
        if not text or not _wild_regex(str(pattern)).match(key):
            continue
        rank = _pattern_specificity(str(pattern))
        if best is None or rank > best[0]:
            best = (rank, text, str(pattern))
    if best is None:
        return None
    return best[1], best[2]


class LayerClassifier:
    """Роль каждого слоя входящего чертежа по настройкам ``feature_labeling``."""

    def __init__(self, fcfg: dict[str, Any]) -> None:
        self.fcfg = fcfg
        self._property = _as_patterns(fcfg.get("property_line_layers"))
        self._points = _as_patterns(fcfg.get("point_label_layers"))
        self._fence = _as_patterns(fcfg.get("fence_layers"))
        self._fence_exclude = _as_patterns(fcfg.get("fence_layer_exclude"))
        self._gate = _as_patterns(fcfg.get("gate_layers"))
        self._notes = _as_patterns(fcfg.get("field_note_layers"))
        self._treads = _as_patterns(fcfg.get("step_tread_layers"))
        raw_structures = fcfg.get("structure_layers") or {}
        self._structures: list[tuple[str, list[str]]] = [
            (str(category), _as_patterns(patterns))
            for category, patterns in raw_structures.items()
        ] if isinstance(raw_structures, dict) else []
        output = str(fcfg.get("label_output_layer") or "").strip()
        self._existing = _as_patterns(fcfg.get("existing_label_layers"))
        if output and not layer_matches(output, self._existing):
            self._existing.append(output)
        self._obstacle = _as_patterns(fcfg.get("obstacle_layers"))
        self._obstacle_exclude = _as_patterns(fcfg.get("obstacle_layer_exclude"))
        self._text_obstacle = _as_patterns(fcfg.get("text_obstacle_layers"))
        self._cache: dict[tuple[str, str], Any] = {}

    def _cached(self, kind: str, layer: str, compute: Any) -> Any:
        key = (kind, str(layer or "").strip().upper())
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    def property_rank(self, layer: str) -> int | None:
        """Порядковый номер слоя границы (0 — самый приоритетный) или ``None``."""
        return self._cached("prop", layer, lambda: _first_match_index(layer, self._property))

    def is_property(self, layer: str) -> bool:
        return self.property_rank(layer) is not None

    def is_point_label(self, layer: str) -> bool:
        return self._cached("pnt", layer, lambda: layer_matches(layer, self._points))

    def is_tread(self, layer: str) -> bool:
        return self._cached("tread", layer, lambda: layer_matches(layer, self._treads))

    def structure_category(self, layer: str) -> str | None:
        def compute() -> str | None:
            for category, patterns in self._structures:
                if layer_matches(layer, patterns):
                    return category
            return None

        return self._cached("struct", layer, compute)

    def structure_rank(self, layer: str) -> int:
        """Позиция слоя внутри своей категории (для выбора основного дома)."""
        for _category, patterns in self._structures:
            index = _first_match_index(layer, patterns)
            if index is not None:
                return index
        return 10_000

    def is_fence(self, layer: str) -> bool:
        def compute() -> bool:
            if layer_matches(layer, self._fence_exclude):
                return False
            if self.is_property(layer) or self.is_point_label(layer) or self.is_tread(layer):
                return False
            if self.structure_category(layer) is not None:
                return False
            return layer_matches(layer, self._fence) or layer_matches(layer, self._gate)

        return self._cached("fence", layer, compute)

    def is_gate(self, layer: str) -> bool:
        """Калитка/ворота: линия слоя ``gate_layers`` (частный случай забора)."""
        return self._cached(
            "gate", layer,
            lambda: self.is_fence(layer) and layer_matches(layer, self._gate),
        )

    def is_field_note(self, layer: str) -> bool:
        """Слой заметок полевого чертёжника (источник подписей, не препятствие)."""
        def compute() -> bool:
            if self.is_existing_label(layer) or self.is_point_label(layer):
                return False
            return layer_matches(layer, self._notes)

        return self._cached("note", layer, compute)

    def is_existing_label(self, layer: str) -> bool:
        return self._cached("label", layer, lambda: layer_matches(layer, self._existing))

    def is_obstacle(self, layer: str) -> bool:
        def compute() -> bool:
            if layer_matches(layer, self._obstacle_exclude):
                return False
            return layer_matches(layer, self._obstacle)

        return self._cached("obst", layer, compute)

    def is_text_obstacle(self, layer: str) -> bool:
        """Текст слоя остаётся в итоговом чертеже — новые подписи его обходят."""
        return self.is_existing_label(layer) or self._cached(
            "tobst", layer, lambda: layer_matches(layer, self._text_obstacle)
        )

    def is_feature_linework(self, layer: str) -> bool:
        """Слой с линиями, из которых строятся участок или объекты."""
        return (
            self.is_property(layer)
            or self.is_fence(layer)
            or self.is_tread(layer)
            or self.structure_category(layer) is not None
        )

    def layer_label(self, layer: str) -> tuple[str, str] | None:
        return self._cached(
            "map", layer,
            lambda: lookup_pattern_label(layer, self.fcfg.get("layer_label_mapping")),
        )


# ---------------------------------------------------------------------------
# Снимок чертежа
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrawingLine:
    """Линейный объект DWG (LINE / POLYLINE / ARC / CIRCLE), приведённый к ломаной."""

    layer: str
    points: tuple[tuple[float, float], ...]
    closed: bool = False
    kind: str = "AcDbLine"
    handle: str = ""


@dataclass(frozen=True)
class DrawingText:
    """Текстовый объект DWG (TEXT / MTEXT) с очищенным от форматирования текстом."""

    layer: str
    text: str
    point: tuple[float, float]
    height: float = 0.0
    rotation: float = 0.0
    bbox: tuple[float, float, float, float] | None = None
    kind: str = "AcDbText"
    handle: str = ""
    hidden: bool = False  # слой заморожен/выключен: текст не считается препятствием


@dataclass
class DrawingSnapshot:
    """Всё, что прочитано из ModelSpace за один проход."""

    lines: list[DrawingLine] = field(default_factory=list)
    texts: list[DrawingText] = field(default_factory=list)
    leaders: list[DrawingLine] = field(default_factory=list)
    entity_count: int = 0
    skipped_hidden: int = 0
    read_errors: int = 0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Тексты: очистка MTEXT, коды точек, сравнение подписей
# ---------------------------------------------------------------------------

_MTEXT_ARG_CODE_RE = re.compile(r"\\[ACFHQTWacfhpqtw][^;\\{}]*;")
_MTEXT_STACK_RE = re.compile(r"\\S([^;]*);")
_MTEXT_TOGGLE_RE = re.compile(r"\\[LlOoKk]")
_CONTROL_CODE_RE = re.compile(r"%%([a-zA-Z])")
_CONTROL_CODES = {"d": "°", "p": "±", "c": "Ø"}


def plain_text(raw: Any) -> str:
    """Убирает форматирование MTEXT (``\\P``, ``{\\fArial;…}``) и коды ``%%u``/``%%d``."""
    text = str(raw or "")
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    # Экранированные \\ { } сначала прячем, чтобы не принять их за коды формата.
    text = text.replace("\\\\", "\x00").replace("\\{", "\x01").replace("\\}", "\x02")
    text = text.replace("\\P", " ").replace("\\X", " ").replace("\\~", " ")
    text = _MTEXT_STACK_RE.sub(lambda m: m.group(1).replace("^", " ").replace("#", "/"), text)
    text = _MTEXT_ARG_CODE_RE.sub("", text)
    text = _MTEXT_TOGGLE_RE.sub("", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("\x00", "\\").replace("\x01", "{").replace("\x02", "}")
    text = _CONTROL_CODE_RE.sub(lambda m: _CONTROL_CODES.get(m.group(1).lower(), ""), text)
    return re.sub(r"\s+", " ", text).strip()


_POINT_CODE_RE = re.compile(r"^\s*(?:(\d+)[\s,;:]+)?([A-Za-z][A-Za-z0-9]*)")


def parse_point_code(text: str) -> tuple[str, str] | None:
    """
    Разбирает подпись геодезической точки ``'<Номер> <Код>'``.

    ``'2 FEC'`` -> ``('2', 'FEC')``, ``'3 FEV64'`` -> ``('3', 'FEV64')``,
    ``'TTP'`` -> ``('', 'TTP')``. Текст без буквенного кода -> ``None``.
    """
    match = _POINT_CODE_RE.match(plain_text(text))
    if match is None:
        return None
    return (match.group(1) or ""), match.group(2).upper()


def label_tokens(text: str) -> list[str]:
    """
    Смысловые слова подписи для сравнения: верхний регистр, без пунктуации и чисел.

    Точки сокращений склеиваются (``C.L.F.`` -> ``CLF``), числа и размеры
    (``6'``, ``0.3``) отбрасываются: ``"6' VINYL FENCE"`` -> ``["VINYL", "FENCE"]``.
    """
    cleaned = plain_text(text).upper().replace(".", "")
    words = re.split(r"[^A-Z0-9]+", cleaned)
    return [word for word in words if word and any(char.isalpha() for char in word)]


def _token_sequence_similar(existing: list[str], wanted: list[str]) -> bool:
    if not existing or not wanted:
        return False
    if existing == wanted:
        return True
    existing_set, wanted_set = set(existing), set(wanted)
    # «6' VINYL FENCE ON LINE» уже содержит нашу подпись «VINYL FENCE».
    if wanted_set <= existing_set:
        return max(len(word) for word in wanted_set) >= 2
    # Короткая существующая подпись («DECK», «FENCE») считается аналогичной,
    # только если совпадает главное (последнее) слово нашей подписи.
    if existing_set <= wanted_set and wanted[-1] in existing_set:
        return len(wanted[-1]) >= 2
    return False


def labels_similar(existing_text: str, label: str, synonyms: Any = None) -> bool:
    """
    ``True``, если существующая на чертеже надпись аналогична новой подписи.

    Сравниваются слова без чисел и пунктуации; учитываются синонимы и
    сокращения из ``label_synonyms`` (``C.L.F.`` = ``CHAIN LINK FENCE``).
    """
    existing = label_tokens(existing_text)
    if not existing:
        return False
    # «C.L.F. GATE» — подпись калитки, а не забора из той же сетки.
    if ("GATE" in existing) != ("GATE" in label_tokens(label)):
        return False
    variants = [label]
    if isinstance(synonyms, dict):
        wanted_key = " ".join(label_tokens(label))
        for base, aliases in synonyms.items():
            if " ".join(label_tokens(str(base))) == wanted_key:
                variants.extend(str(alias) for alias in _as_patterns(aliases))
    for variant in variants:
        if _token_sequence_similar(existing, label_tokens(variant)):
            return True
    return False


@dataclass(frozen=True)
class PointCode:
    """Геодезическая точка с кодом из слоёв ``POINT_LABELS`` / ``PNT-X`` / ``PNTS``."""

    number: str
    code: str
    label: str | None
    pattern: str | None
    point: tuple[float, float]
    layer: str
    raw: str
    is_fence: bool = False

    @property
    def title(self) -> str:
        return f"{self.number} {self.code}".strip()


# ---------------------------------------------------------------------------
# Геометрические помощники (shapely)
# ---------------------------------------------------------------------------


def _clean_coords(points: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
    """Убирает нечисловые и подряд идущие совпадающие вершины."""
    result: list[tuple[float, float]] = []
    for point in points:
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        if result and abs(result[-1][0] - x) < 1e-9 and abs(result[-1][1] - y) < 1e-9:
            continue
        result.append((x, y))
    return result


def line_to_shapely(line: DrawingLine) -> LineString | None:
    """``DrawingLine`` -> ``LineString`` (замкнутые полилинии замыкаются явно)."""
    coords = _clean_coords(line.points)
    if line.closed and len(coords) >= 3 and coords[0] != coords[-1]:
        coords.append(coords[0])
    if len(coords) < 2:
        return None
    geom = LineString(coords)
    return geom if geom.length > 0.0 else None


def _line_parts(geom: BaseGeometry | None) -> list[LineString]:
    """Все ``LineString`` внутри произвольной геометрии."""
    if geom is None or geom.is_empty:
        return []
    kind = geom.geom_type
    if kind == "LineString":
        return [geom]  # type: ignore[list-item]
    if kind == "LinearRing":
        return [LineString(geom.coords)]
    if kind in {"MultiLineString", "GeometryCollection"}:
        parts: list[LineString] = []
        for item in geom.geoms:  # type: ignore[attr-defined]
            parts.extend(_line_parts(item))
        return parts
    return []


def _polygon_parts(geom: BaseGeometry | None) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    kind = geom.geom_type
    if kind == "Polygon":
        return [geom]  # type: ignore[list-item]
    if kind in {"MultiPolygon", "GeometryCollection"}:
        parts: list[Polygon] = []
        for item in geom.geoms:  # type: ignore[attr-defined]
            parts.extend(_polygon_parts(item))
        return parts
    return []


def _largest_polygon(geom: BaseGeometry | None) -> Polygon | None:
    parts = _polygon_parts(geom)
    return max(parts, key=lambda part: part.area) if parts else None


def snap_endpoints(lines: Sequence[LineString], tolerance: float) -> list[LineString]:
    """
    Стягивает концы линий, расходящиеся меньше чем на ``tolerance``.

    Полевой чертёж редко замкнут «точка в точку»: без этого шага
    ``linemerge`` / ``polygonize`` не соединят соседние сегменты.
    """
    lines = [line for line in lines if line is not None and not line.is_empty]
    if tolerance <= 0.0 or not lines:
        return list(lines)
    cell = float(tolerance)
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}

    def snap(point: tuple[float, float]) -> tuple[float, float]:
        gx, gy = math.floor(point[0] / cell), math.floor(point[1] / cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for anchor in grid.get((gx + dx, gy + dy), ()):
                    if math.hypot(anchor[0] - point[0], anchor[1] - point[1]) <= tolerance:
                        return anchor
        grid.setdefault((gx, gy), []).append(point)
        return point

    result: list[LineString] = []
    for line in lines:
        coords = [(float(x), float(y)) for x, y, *_rest in line.coords]
        coords[0] = snap(coords[0])
        coords[-1] = snap(coords[-1])
        coords = _clean_coords(coords)
        if len(coords) >= 2:
            snapped = LineString(coords)
            if snapped.length > 0.0:
                result.append(snapped)
    return result


def _noded(lines: Sequence[LineString]) -> list[LineString]:
    """Разрезает линии во всех взаимных пересечениях и убирает дубликаты."""
    if not lines:
        return []
    return _line_parts(unary_union(list(lines)))


def merge_lines(lines: Sequence[LineString], tolerance: float) -> list[LineString]:
    """
    Объединяет смежные сегменты в цельные ломаные (``shapely.ops.linemerge``).

    Сегменты одного забора, начерченные отдельными отрезками, становятся одной
    линией; в тройных стыках линии остаются раздельными.
    """
    noded = _noded(snap_endpoints(lines, tolerance))
    if not noded:
        return []
    if len(noded) == 1:
        return list(noded)
    return _line_parts(linemerge(noded))


def _dangling_nodes(noded: Sequence[LineString]) -> list[tuple[float, float]]:
    """Свободные концы: узлы, в которых заканчивается ровно одна линия."""
    degree: dict[tuple[float, float], int] = {}
    for line in noded:
        coords = list(line.coords)
        start = (float(coords[0][0]), float(coords[0][1]))
        end = (float(coords[-1][0]), float(coords[-1][1]))
        if start == end:
            continue
        degree[start] = degree.get(start, 0) + 1
        degree[end] = degree.get(end, 0) + 1
    return [node for node, count in degree.items() if count == 1]


def bridge_gaps(
    lines: Sequence[LineString], gap_tolerance: float
) -> tuple[list[LineString], list[LineString]]:
    """
    Соединяет ближайшие свободные концы короткими перемычками (разрыв <= допуска).

    Возвращает ``(линии с перемычками, сами перемычки)``. Нужен для границы,
    которую Шаг 1 по правилам оставляет с открытым разрывом невязки.
    """
    noded = _noded(lines)
    if gap_tolerance <= 0.0 or not noded:
        return list(noded), []
    nodes = _dangling_nodes(noded)
    pairs: list[tuple[float, int, int]] = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            dist = math.hypot(nodes[i][0] - nodes[j][0], nodes[i][1] - nodes[j][1])
            if 0.0 < dist <= gap_tolerance:
                pairs.append((dist, i, j))
    pairs.sort()
    used: set[int] = set()
    bridges: list[LineString] = []
    for _dist, i, j in pairs:
        if i in used or j in used:
            continue
        used.update((i, j))
        bridges.append(LineString([nodes[i], nodes[j]]))
    return list(noded) + bridges, bridges


def _polygonize(lines: Sequence[LineString]) -> list[Polygon]:
    noded = _noded(lines)
    if not noded:
        return []
    return [poly for poly in polygonize(noded) if poly.is_valid and poly.area > 0.0]


def _close_single_chain(lines: Sequence[LineString], max_gap_ratio: float) -> tuple[Polygon | None, float]:
    """
    Крайний случай: все линии образуют одну незамкнутую цепочку — замыкаем её,
    если разрыв между концами невелик относительно длины цепочки.
    """
    merged = merge_lines(lines, 0.0)
    if len(merged) != 1:
        return None, 0.0
    chain = merged[0]
    coords = _clean_coords(chain.coords)
    if len(coords) < 3:
        return None, 0.0
    gap = math.hypot(coords[0][0] - coords[-1][0], coords[0][1] - coords[-1][1])
    if gap <= 1e-9:
        polygon = Polygon(coords)
        return (polygon if polygon.is_valid and polygon.area > 0 else None), 0.0
    if gap > max_gap_ratio * chain.length:
        return None, gap
    polygon = Polygon(coords)
    if not polygon.is_valid or polygon.area <= 0.0:
        return None, gap
    return polygon, gap


def polygons_from_lines(
    lines: Sequence[LineString],
    *,
    snap_tolerance: float,
    gap_tolerance: float = 0.0,
    min_area: float = 0.0,
    allow_chain_closure: bool = False,
) -> tuple[list[Polygon], dict[int, float]]:
    """
    Собирает замкнутые контуры из отдельных линий.

    Возвращает ``(полигоны, {индекс полигона: величина закрытого разрыва, ft})``.
    Сначала линии используются как есть; если это не дало контура, свободные
    концы соединяются перемычками (``gap_tolerance``), а единственная
    незамкнутая цепочка при ``allow_chain_closure`` замыкается принудительно.
    """
    snapped = snap_endpoints(lines, snap_tolerance)
    polygons = [poly for poly in _polygonize(snapped) if poly.area >= min_area]
    gaps: dict[int, float] = {}
    if polygons:
        return polygons, gaps
    if gap_tolerance > 0.0:
        bridged, bridges = bridge_gaps(snapped, gap_tolerance)
        if bridges:
            polygons = [poly for poly in _polygonize(bridged) if poly.area >= min_area]
            for index, poly in enumerate(polygons):
                boundary = poly.exterior
                on_boundary = [
                    bridge.length for bridge in bridges
                    if boundary.distance(bridge.interpolate(0.5, normalized=True)) < 1e-6
                ]
                if on_boundary:
                    gaps[index] = max(on_boundary)
            if polygons:
                return polygons, gaps
    if allow_chain_closure:
        polygon, gap = _close_single_chain(snapped, 0.15)
        if polygon is not None and polygon.area >= min_area:
            return [polygon], ({0: gap} if gap > 0 else {})
    return [], {}


# ---------------------------------------------------------------------------
# Полигон участка
# ---------------------------------------------------------------------------


@dataclass
class PropertyBoundary:
    """Найденный полигон участка и сведения о том, как он получен."""

    polygon: Polygon
    layer: str
    method: str = "auto"  # "auto" | "pick"
    candidates: int = 1
    closed_gap_ft: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def area_sqft(self) -> float:
        return float(self.polygon.area)

    @property
    def vertex_count(self) -> int:
        return max(0, len(self.polygon.exterior.coords) - 1)


def _normalize_polygon(polygon: Polygon | None) -> Polygon | None:
    if polygon is None or polygon.is_empty:
        return None
    if not polygon.is_valid:
        polygon = _largest_polygon(polygon.buffer(0))
        if polygon is None:
            return None
    # Участок — сплошной контур: внутренние «дырки» от вложенных линий не нужны.
    return Polygon(polygon.exterior.coords)


def _survey_lines(snapshot: DrawingSnapshot, classifier: LayerClassifier) -> list[LineString]:
    """Линии заборов и строений — признак того, какой участок снимала бригада."""
    result: list[LineString] = []
    for line in snapshot.lines:
        if classifier.is_fence(line.layer) or classifier.structure_category(line.layer):
            geom = line_to_shapely(line)
            if geom is not None:
                result.append(geom)
    return result


def _activity_score(polygon: Polygon, survey_lines: Sequence[LineString]) -> float:
    """Суммарная длина снятых линий (заборы, строения), середина которых внутри контура."""
    total = 0.0
    minx, miny, maxx, maxy = polygon.bounds
    for line in survey_lines:
        mid = line.interpolate(0.5, normalized=True)
        if not (minx <= mid.x <= maxx and miny <= mid.y <= maxy):
            continue
        if polygon.contains(mid):
            total += line.length
    return total


def _choose_property_candidate(
    polygons: Sequence[Polygon], survey_lines: Sequence[LineString]
) -> tuple[int, bool]:
    """Индекс лучшего кандидата и признак неоднозначности выбора."""
    if len(polygons) == 1:
        return 0, False
    scored = [
        (_activity_score(poly, survey_lines), poly.area, index)
        for index, poly in enumerate(polygons)
    ]
    scored.sort(reverse=True)
    ambiguous = scored[0][0] <= 0.0 or (
        len(scored) > 1 and scored[1][0] > 0.0 and scored[1][0] >= 0.8 * scored[0][0]
    )
    return scored[0][2], ambiguous


def find_property_polygon(
    snapshot: DrawingSnapshot, cfg: dict[str, Any] | None = None
) -> PropertyBoundary:
    """
    Автопоиск полигона участка по слоям ``property_line_layers``.

    Слои проверяются в порядке приоритета из конфигурации; используется первый
    слой, линии которого образуют замкнутый контур. Если контуров несколько
    (например, сетка ``M-TAX-LINE``), выбирается тот, внутри которого больше
    всего снятых бригадой заборов и строений.
    """
    fcfg = feature_settings(cfg)
    classifier = LayerClassifier(fcfg)
    by_rank: dict[int, dict[str, list[LineString]]] = {}
    for line in snapshot.lines:
        rank = classifier.property_rank(line.layer)
        if rank is None:
            continue
        geom = line_to_shapely(line)
        if geom is not None:
            by_rank.setdefault(rank, {}).setdefault(line.layer, []).append(geom)

    layer_list = ", ".join(_as_patterns(fcfg.get("property_line_layers")))
    if not by_rank:
        raise FeatureLabelError(
            f"На чертеже нет линий в слоях границы участка ({layer_list}). "
            "Начертите границу (Шаг 1) или укажите её в AutoCAD вручную."
        )

    snap_tol = float(fcfg.get("boundary_snap_tolerance_ft") or 0.0)
    gap_tol = float(fcfg.get("boundary_gap_tolerance_ft") or 0.0)
    min_area = float(fcfg.get("min_property_area_sqft") or 0.0)
    survey_lines: list[LineString] | None = None
    seen: list[str] = []
    for rank in sorted(by_rank):
        layers = by_rank[rank]
        layer_name = ", ".join(sorted(layers))
        lines = [geom for group in layers.values() for geom in group]
        seen.append(f"{layer_name} ({len(lines)} лин.)")
        polygons, gaps = polygons_from_lines(
            lines, snap_tolerance=snap_tol, gap_tolerance=gap_tol,
            min_area=min_area, allow_chain_closure=True,
        )
        if not polygons:
            continue
        if survey_lines is None:
            survey_lines = _survey_lines(snapshot, classifier)
        index, ambiguous = _choose_property_candidate(polygons, survey_lines)
        polygon = _normalize_polygon(polygons[index])
        if polygon is None:
            continue
        notes: list[str] = []
        gap = float(gaps.get(index, 0.0))
        if gap > 0.0:
            notes.append(
                f"контур границы не был замкнут: разрыв {gap:.2f} ft закрыт автоматически"
            )
        if len(polygons) > 1:
            notes.append(
                f"в слое {layer_name} найдено замкнутых контуров: {len(polygons)}; выбран "
                + ("самый крупный — проверьте результат или укажите границу в AutoCAD"
                   if ambiguous else "контур с наибольшим числом снятых объектов")
            )
        return PropertyBoundary(
            polygon=polygon, layer=layer_name, method="auto",
            candidates=len(polygons), closed_gap_ft=gap, notes=notes,
        )
    raise FeatureLabelError(
        "Линии границы найдены, но не образуют замкнутый контур: "
        + "; ".join(seen)
        + f". Допуск разрыва — {gap_tol:g} ft. Замкните границу или укажите её в AutoCAD вручную."
    )


def property_polygon_from_selection(
    selected: Sequence[DrawingLine],
    snapshot: DrawingSnapshot,
    cfg: dict[str, Any] | None = None,
) -> PropertyBoundary:
    """
    Полигон участка по объектам, которые геодезист указал в AutoCAD.

    Достаточно выбрать замкнутую полилинию либо все стороны границы. Если
    указана только часть сторон, контур достраивается по остальным линиям тех
    же слоёв и слоёв границы участка.
    """
    fcfg = feature_settings(cfg)
    classifier = LayerClassifier(fcfg)
    picked = [geom for geom in (line_to_shapely(line) for line in selected) if geom is not None]
    if not picked:
        raise FeatureLabelError(
            "Среди выбранных объектов нет линий или полилиний. "
            "Укажите стороны границы участка (LINE / POLYLINE)."
        )
    snap_tol = float(fcfg.get("boundary_snap_tolerance_ft") or 0.0)
    gap_tol = float(fcfg.get("boundary_gap_tolerance_ft") or 0.0)
    min_area = float(fcfg.get("min_property_area_sqft") or 0.0)
    layer_name = ", ".join(sorted({line.layer for line in selected if line.layer})) or "?"
    survey_lines = _survey_lines(snapshot, classifier)

    polygons, gaps = polygons_from_lines(
        picked, snap_tolerance=snap_tol, gap_tolerance=gap_tol,
        min_area=min_area, allow_chain_closure=True,
    )
    if polygons:
        index, _ambiguous = _choose_property_candidate(polygons, survey_lines)
        if len(polygons) > 1 and _activity_score(polygons[index], survey_lines) <= 0.0:
            index = max(range(len(polygons)), key=lambda i: polygons[i].area)
        polygon = _normalize_polygon(polygons[index])
        if polygon is not None:
            notes: list[str] = []
            gap = float(gaps.get(index, 0.0))
            if gap > 0.0:
                notes.append(f"выбранный контур замкнут автоматически (разрыв {gap:.2f} ft)")
            if len(polygons) > 1:
                notes.append(f"выбранные линии образуют контуров: {len(polygons)}; взят основной")
            return PropertyBoundary(
                polygon=polygon, layer=layer_name, method="pick",
                candidates=len(polygons), closed_gap_ft=gap, notes=notes,
            )

    # Указана только часть сторон: ищем контур, на границе которого лежат выбранные линии.
    picked_layers = {str(line.layer).upper() for line in selected}
    pool = list(picked)
    for line in snapshot.lines:
        if str(line.layer).upper() in picked_layers or classifier.is_property(line.layer):
            geom = line_to_shapely(line)
            if geom is not None:
                pool.append(geom)
    pool_polygons, pool_gaps = polygons_from_lines(
        pool, snap_tolerance=snap_tol, gap_tolerance=gap_tol, min_area=min_area,
    )
    total = sum(geom.length for geom in picked)
    best: tuple[float, float, float, int] | None = None
    for index, poly in enumerate(pool_polygons):
        near = poly.exterior.buffer(max(snap_tol, 0.1))
        coverage = sum(geom.intersection(near).length for geom in picked)
        if total <= 0.0 or coverage < 0.5 * total:
            continue
        key = (round(coverage, 3), _activity_score(poly, survey_lines), poly.area, index)
        if best is None or key > best:
            best = key
    if best is not None:
        index = best[3]
        polygon = _normalize_polygon(pool_polygons[index])
        if polygon is not None:
            notes = ["контур достроен по соседним линиям: были указаны не все стороны границы"]
            gap = float(pool_gaps.get(index, 0.0))
            if gap > 0.0:
                notes.append(f"разрыв {gap:.2f} ft закрыт автоматически")
            return PropertyBoundary(
                polygon=polygon, layer=layer_name, method="pick",
                candidates=len(pool_polygons), closed_gap_ft=gap, notes=notes,
            )
    raise FeatureLabelError(
        "Выбранные объекты не образуют замкнутый контур участка. Укажите замкнутую "
        "полилинию или все стороны границы и повторите."
    )


# ---------------------------------------------------------------------------
# Объекты на участке
# ---------------------------------------------------------------------------

_CATEGORY_TITLES = {
    "fence": "забор",
    "gate": "калитка",
    "planter": "клумба",
    "ground": "покрытие/грунт",
    "house": "строение",
    "shed": "сарай",
    "porch": "крыльцо/терраса",
    "steps": "ступени",
    "surface": "покрытие",
    "point": "точка",
}


@dataclass
class Feature:
    """Распознанный объект: линия забора, контур строения или точечный объект."""

    feature_id: int
    kind: str  # "fence" | "area" | "edge" | "point" | "spot" (точка полевой заметки)
    category: str
    layer: str
    geometry: BaseGeometry
    label: str | None = None
    source: str | None = None  # "field_note" | "point_code" | "layer" | "user"
    rule: str = ""
    codes: tuple[PointCode, ...] = ()
    closed: bool = True
    is_main_house: bool = False
    tread_count: int = 0
    note_text: str = ""  # исходный текст полевой заметки (source == "field_note")
    skip_reason: str = ""  # почему объект намеренно оставлен без подписи

    @property
    def flat_label(self) -> str:
        """Подпись одной строкой (переносы заменены пробелами)."""
        return " ".join(part for part in str(self.label or "").split("\n") if part)

    @property
    def size_text(self) -> str:
        if self.kind == "area":
            return f"{self.geometry.area:.0f} sq ft"
        if self.kind in {"fence", "edge"}:
            return f"{self.geometry.length:.1f} ft"
        return ""

    @property
    def title(self) -> str:
        """Короткое описание объекта для отчёта."""
        name = _CATEGORY_TITLES.get(self.category, self.category)
        if self.kind == "point" and self.codes:
            return f"{name} {self.codes[0].title} ({self.layer})"
        if self.kind == "spot":
            return f"{name} (заметка слоя {self.layer})"
        size = self.size_text
        return f"{name} {self.layer}" + (f", {size}" if size else "")

    @property
    def source_text(self) -> str:
        if self.source == "field_note":
            return f"полевая заметка «{self.note_text}»"
        if self.source == "point_code":
            titles = ", ".join(code.title for code in self.codes[:4])
            more = f" и ещё {len(self.codes) - 4}" if len(self.codes) > 4 else ""
            return f"код точки {titles}{more}"
        if self.source == "layer":
            extra = f", проступей: {self.tread_count}" if self.tread_count else ""
            return f"слой {self.layer} ({self.rule}){extra}"
        if self.source == "user":
            return "текст из окна программы"
        if self.source == "adjacent":
            return f"материал соседнего забора ({self.rule})"
        return self.skip_reason or "нет правила"


@dataclass
class RecognitionResult:
    """Итог распознавания: участок, зона поиска и объекты с подписями."""

    boundary: PropertyBoundary
    zone: Polygon
    features: list[Feature] = field(default_factory=list)
    point_codes: list[PointCode] = field(default_factory=list)
    unknown_codes: dict[str, int] = field(default_factory=dict)
    unbound_fence_codes: list[PointCode] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    field_notes: list["FieldNote"] = field(default_factory=list)

    @property
    def labeled(self) -> list[Feature]:
        return [feature for feature in self.features if feature.label]

    @property
    def unlabeled(self) -> list[Feature]:
        """Объекты без подписи, для которых не нашлось правила."""
        return [
            feature for feature in self.features
            if not feature.label and not feature.skip_reason
        ]

    @property
    def by_notes(self) -> int:
        return self.count_by_source("field_note")

    @property
    def unknown_notes(self) -> list["FieldNote"]:
        return [note for note in self.field_notes if note.status == "unknown"]

    @property
    def unbound_notes(self) -> list["FieldNote"]:
        return [note for note in self.field_notes if note.status in {"unbound", "unused"}]

    @property
    def ignored_notes(self) -> list["FieldNote"]:
        return [note for note in self.field_notes if note.status == "ignored"]

    def count_by_source(self, source: str) -> int:
        return sum(1 for feature in self.features if feature.label and feature.source == source)

    @property
    def by_point_codes(self) -> int:
        return self.count_by_source("point_code")

    @property
    def by_layers(self) -> int:
        return self.count_by_source("layer") + self.count_by_source("adjacent")

    @property
    def by_user(self) -> int:
        return self.count_by_source("user")

    @property
    def known_point_codes(self) -> int:
        return sum(1 for code in self.point_codes if code.label)


def _within_bounds(geom: BaseGeometry, bounds: tuple[float, float, float, float]) -> bool:
    minx, miny, maxx, maxy = geom.bounds
    return not (maxx < bounds[0] or maxy < bounds[1] or minx > bounds[2] or miny > bounds[3])


def _collect_point_codes(
    snapshot: DrawingSnapshot,
    classifier: LayerClassifier,
    fcfg: dict[str, Any],
    zone: Polygon,
    reach: float,
) -> tuple[list[PointCode], dict[str, int]]:
    dictionary = fcfg.get("point_code_dictionary")
    fence_codes = fcfg.get("fence_point_codes")
    silent = fcfg.get("point_codes_without_label")
    search = zone.buffer(max(0.0, reach))
    codes: list[PointCode] = []
    unknown: dict[str, int] = {}
    for text in snapshot.texts:
        if not classifier.is_point_label(text.layer):
            continue
        parsed = parse_point_code(text.text)
        if parsed is None:
            continue
        if not search.contains(Point(text.point)):
            continue
        number, code = parsed
        found = lookup_pattern_label(code, dictionary)
        if found is None and not layer_matches(code, silent):
            # FEV64 / SW3: числовой хвост — номер линии, в отчёте коды группируются.
            base = re.sub(r"\d+$", "", code) or code
            unknown[base] = unknown.get(base, 0) + 1
        codes.append(
            PointCode(
                number=number,
                code=code,
                label=found[0] if found else None,
                pattern=found[1] if found else None,
                point=(float(text.point[0]), float(text.point[1])),
                layer=text.layer,
                raw=text.text,
                is_fence=layer_matches(code, fence_codes),
            )
        )
    return codes, unknown


# ---------------------------------------------------------------------------
# Заметки полевого чертёжника -> формулировки заказчика
# ---------------------------------------------------------------------------

#: категория «покрытие/грунт»: подпись ставится в точке заметки, без объекта
GROUND_TARGET = "ground"


@dataclass
class FieldNote:
    """Заметка полевого чертёжника (текст + конец выноски) и её перевод."""

    text: str
    layer: str
    box: Any  # shapely Polygon: габарит текста заметки
    height: float = 0.0
    rotation: float = 0.0
    tips: list[tuple[float, float]] = field(default_factory=list)  # концы выносок
    pattern: str = ""  # сработавшее правило
    labels: dict[str, str] = field(default_factory=dict)  # {категория: подпись}
    #: "unknown" — нет правила; "ignored" — правило «не переносить»; "bound" —
    #: подпись отдана объекту; "spot" — подпись в точке заметки; "unbound" —
    #: объект заметки не найден
    status: str = "unknown"

    @property
    def anchor(self) -> tuple[float, float]:
        """Точка, на которую указывает заметка: конец выноски либо центр текста."""
        if self.tips:
            return self.tips[0]
        center = self.box.centroid
        return (float(center.x), float(center.y))


def approx_text_box(text: DrawingText, fallback_height: float = 1.0) -> Polygon:
    """Габарит текста чертежа: из AutoCAD (``bbox``) либо оценка по числу символов."""
    bbox = text.bbox
    if bbox is not None:
        minx, miny, maxx, maxy = bbox
        if all(math.isfinite(value) for value in bbox) and maxx > minx and maxy > miny:
            return Polygon([(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)])
    height = float(text.height) if text.height and text.height > 0 else float(fallback_height)
    lines = str(text.text or "").split("\n") or [""]
    width = max(0.5 * height, 0.75 * height * max(len(line) for line in lines))
    total = height * (1.0 + 1.67 * (len(lines) - 1))
    x, y = float(text.point[0]), float(text.point[1])
    if text.kind == "AcDbText":
        corners = [(0.0, 0.0), (width, 0.0), (width, total), (0.0, total)]
    else:  # MText: вставка в левом верхнем углу
        corners = [(0.0, -total), (width, -total), (width, 0.0), (0.0, 0.0)]
    cos_a, sin_a = math.cos(text.rotation), math.sin(text.rotation)
    return Polygon(
        [(x + dx * cos_a - dy * sin_a, y + dx * sin_a + dy * cos_a) for dx, dy in corners]
    )


def translate_field_note(text: str, rules: Any) -> tuple[str, dict[str, str]] | None:
    """
    Переводит заметку полевика по ``field_note_rules``.

    Возвращает ``(шаблон, {категория: подпись})`` первого подходящего правила
    (пустой словарь — заметка намеренно не переносится) либо ``None``.
    """
    key = re.sub(r"\s+", " ", plain_text(text)).strip().upper()
    if not key or not isinstance(rules, (list, tuple)):
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        pattern = str(rule.get("match") or "").strip()
        if not pattern or not _wild_regex(pattern).match(key):
            continue
        labels: dict[str, str] = {}
        many = rule.get("labels")
        if isinstance(many, dict):
            for target, label in many.items():
                value = _normalize_house_text(str(label or ""))
                if value:
                    labels[str(target).strip().lower()] = value
        value = _normalize_house_text(str(rule.get("label") or ""))
        if value:
            target = str(rule.get("target") or GROUND_TARGET).strip().lower()
            labels.setdefault(target, value)
        return pattern, labels
    return None


_FILE_REFERENCE_RE = re.compile(
    r"\.(png|jpe?g|tiff?|bmp|gif|pdf|dwg|dxf|ecw|sid|jp2|csv|txt)\b", re.IGNORECASE
)


def is_file_reference(text: str) -> bool:
    """Текст-путь к файлу (подпись подложки ``…\\ortho2024.png``): не заметка и не препятствие."""
    return bool(_FILE_REFERENCE_RE.search(str(text or "")))


def _collect_field_notes(
    snapshot: DrawingSnapshot,
    classifier: LayerClassifier,
    fcfg: dict[str, Any],
    zone: Polygon,
) -> list[FieldNote]:
    """Заметки полевика в зоне поиска с переводом и концами выносок."""
    rules = fcfg.get("field_note_rules")
    notes: list[FieldNote] = []
    for text in snapshot.texts:
        if not classifier.is_field_note(text.layer):
            continue
        value = re.sub(r"\s+", " ", plain_text(text.text)).strip()
        if not value or is_file_reference(text.text):
            continue
        rect = approx_text_box(text)
        if not rect.intersects(zone):
            continue
        note = FieldNote(
            text=value, layer=text.layer, box=rect,
            height=float(text.height or 0.0), rotation=float(text.rotation or 0.0),
        )
        translated = translate_field_note(value, rules)
        if translated is not None:
            note.pattern, note.labels = translated
            note.status = "unused" if note.labels else "ignored"
        notes.append(note)
    if not notes:
        return notes

    # --- выноски заметок: ближний к тексту конец — «полка», дальний — стрелка
    paths: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for leader in snapshot.leaders:
        if classifier.is_field_note(leader.layer) and len(leader.points) >= 2:
            paths.append((leader.points[0], leader.points[-1]))
    loose: list[LineString] = []
    for line in snapshot.lines:
        if line.closed or len(line.points) < 2 or not classifier.is_field_note(line.layer):
            continue
        geom = line_to_shapely(line)
        if geom is not None:
            loose.append(geom)
    tolerance = float(fcfg.get("merge_tolerance_ft") or 0.0)
    for chain in merge_lines(loose, tolerance) if loose else []:
        coords = _clean_coords(chain.coords)
        if len(coords) >= 2 and chain.length > 0.5:
            paths.append((coords[0], coords[-1]))
    for first, last in paths:
        p_first, p_last = Point(first), Point(last)
        best: tuple[float, FieldNote, tuple[float, float]] | None = None
        for note in notes:
            link = max(2.0, 2.5 * (note.height or 1.0))
            d_first, d_last = note.box.distance(p_first), note.box.distance(p_last)
            near, far, tip = (d_first, d_last, last) if d_first <= d_last else (d_last, d_first, first)
            if near > link or far < 0.5:
                continue
            if best is None or near < best[0]:
                best = (near, note, (float(tip[0]), float(tip[1])))
        if best is not None and best[2] not in best[1].tips:
            best[1].tips.append(best[2])
    return notes


def _apply_field_notes(
    result: "RecognitionResult",
    notes: Sequence[FieldNote],
    fcfg: dict[str, Any],
    new_id: Any,
) -> None:
    """
    Привязывает переведённые заметки к объектам: подпись заметки заменяет
    подпись по коду точки и по слою. Заметка о покрытии/грунте (и заметка,
    объект которой не найден) ставится отдельной подписью в своей точке.
    """
    radius = max(0.0, float(fcfg.get("field_note_bind_radius_ft") or 0.0))
    claims: dict[int, tuple[float, FieldNote, str]] = {}
    spots: list[tuple[FieldNote, str, str, str]] = []  # заметка, подпись, категория, пояснение
    for note in notes:
        for target, label in note.labels.items():
            anchors: list[BaseGeometry] = [Point(tip) for tip in note.tips] or [note.box]
            best: tuple[tuple[float, float, int], int] | None = None
            for index, feature in enumerate(result.features):
                if feature.is_main_house or feature.kind in {"point", "spot"}:
                    continue
                if target == GROUND_TARGET:
                    if feature.category != "surface":
                        continue
                elif feature.category != target:
                    continue
                distance = min(feature.geometry.distance(anchor) for anchor in anchors)
                if distance > radius:
                    continue
                size = feature.geometry.area if feature.kind == "area" else feature.geometry.length
                key = (round(distance, 2), size, index)
                if best is None or key < best[0]:
                    best = (key, index)
            if target == GROUND_TARGET:
                feature = result.features[best[1]] if best is not None else None
                if feature is not None and feature.kind == "area" and best[0][0] <= 0.5:
                    pass  # заметка стоит внутри контура покрытия: подписывается сам контур
                else:
                    if feature is not None:
                        # Кромка того же покрытия: её подпись по слою заменяет заметка.
                        feature.label = None
                        feature.source = None
                        feature.skip_reason = f"покрытие подписано по заметке «{note.text}»"
                    spots.append((note, label, GROUND_TARGET, ""))
                    continue
            if best is None:
                # Объект заметки не найден по слоям. Если у заметки есть выноска,
                # подпись ставится у её стрелки; заборы и калитки подписываются
                # только по своим линиям, а заметка без выноски стоит в стороне
                # от объекта — такие попадают в отчёт.
                if note.tips and target not in {"fence", "gate"}:
                    spots.append((note, label, target, "объект не найден по слоям"))
                elif note.status == "unused":
                    note.status = "unbound"
                continue
            current = claims.get(best[1])
            if current is None or best[0][0] < current[0]:
                claims[best[1]] = (best[0][0], note, label)
            note.status = "bound"
    for index, (_distance, note, label) in claims.items():
        feature = result.features[index]
        feature.label = label
        feature.source = "field_note"
        feature.rule = note.pattern
        feature.note_text = note.text
        feature.skip_reason = ""
    for note, label, target, comment in spots:
        note.status = "spot" if note.status != "bound" else note.status
        result.features.append(
            Feature(
                feature_id=new_id(), kind="spot", category=target, layer=note.layer,
                geometry=Point(note.anchor), label=label, source="field_note",
                rule=note.pattern, note_text=note.text, closed=False,
                skip_reason=comment,
            )
        )


def _clip_lines(line: LineString, zone: Polygon, min_length: float) -> list[LineString]:
    if zone.contains(line):
        return [line] if line.length >= min_length else []
    parts = _line_parts(line.intersection(zone))
    if len(parts) > 1:
        parts = _line_parts(linemerge(parts)) or parts
    return [part for part in parts if part.length >= min_length]


def _corner_positions(line: LineString, min_turn_deg: float = 20.0) -> list[float]:
    """Расстояния от начала линии до её вершин с заметным поворотом (углы забора)."""
    coords = _clean_coords(line.coords)
    positions: list[float] = []
    travelled = 0.0
    for index in range(1, len(coords) - 1):
        prev_pt, here, next_pt = coords[index - 1], coords[index], coords[index + 1]
        travelled += math.hypot(here[0] - prev_pt[0], here[1] - prev_pt[1])
        heading_in = math.atan2(here[1] - prev_pt[1], here[0] - prev_pt[0])
        heading_out = math.atan2(next_pt[1] - here[1], next_pt[0] - here[0])
        turn = abs((heading_out - heading_in + math.pi) % (2.0 * math.pi) - math.pi)
        if math.degrees(turn) >= min_turn_deg:
            positions.append(travelled)
    return positions


def _split_fence_by_labels(
    line: LineString, codes: Sequence[PointCode], corner_reach: float = 1.0
) -> list[tuple[LineString, list[PointCode]]]:
    """
    Делит линию забора там, где вдоль неё меняется материал (``FEC`` -> ``FEV``).

    Граница участков ставится в углу забора между соседними точками с разными
    кодами, а если угла между ними нет — посередине между этими точками.
    """
    ordered = sorted(codes, key=lambda code: line.project(Point(code.point)))
    groups: list[list[PointCode]] = []
    for code in ordered:
        if groups and groups[-1][-1].label == code.label:
            groups[-1].append(code)
        else:
            groups.append([code])
    if len(groups) <= 1:
        return [(line, list(ordered))]
    corners = _corner_positions(line)
    cuts = [0.0]
    for left, right in zip(groups, groups[1:]):
        a = line.project(Point(left[-1].point))
        b = line.project(Point(right[0].point))
        middle = (a + b) / 2.0
        # Материал обычно меняется в углу забора: если между точками есть поворот
        # линии, граница ставится в нём, иначе — посередине между точками.
        between = [pos for pos in corners if a - corner_reach <= pos <= b + corner_reach]
        cut = min(between, key=lambda pos: abs(pos - middle)) if between else middle
        cuts.append(min(max(cut, cuts[-1]), line.length))
    cuts.append(line.length)
    pieces: list[tuple[LineString, list[PointCode]]] = []
    for index, group in enumerate(groups):
        start, end = cuts[index], cuts[index + 1]
        if end - start <= 1e-6:
            continue
        piece = substring(line, start, end)
        if piece.geom_type == "LineString" and piece.length > 0.0:
            pieces.append((piece, group))
    return pieces or [(line, list(ordered))]


def _connected_groups(lines: Sequence[LineString], tolerance: float) -> list[list[LineString]]:
    """Группы линий, соприкасающихся друг с другом (с допуском)."""
    count = len(lines)
    if count <= 1:
        return [list(lines)] if lines else []
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    tree = STRtree(list(lines))
    reach = max(tolerance, 1e-6)
    for index, line in enumerate(lines):
        for other in tree.query(line.buffer(reach), predicate="intersects"):
            other_index = int(other)
            if other_index <= index:
                continue
            root_a, root_b = find(index), find(other_index)
            if root_a != root_b:
                parent[root_b] = root_a
    groups: dict[int, list[LineString]] = {}
    for index, line in enumerate(lines):
        groups.setdefault(find(index), []).append(line)
    return list(groups.values())


def _min_width(polygon: Polygon) -> float:
    """Короткая сторона минимального описанного прямоугольника."""
    rect = polygon.minimum_rotated_rectangle
    if rect.geom_type != "Polygon":
        return 0.0
    coords = list(rect.exterior.coords)
    side_a = math.hypot(coords[1][0] - coords[0][0], coords[1][1] - coords[0][1])
    side_b = math.hypot(coords[2][0] - coords[1][0], coords[2][1] - coords[1][1])
    return min(side_a, side_b)


def _ribbon_polygon(first: LineString, second: LineString) -> Polygon | None:
    """Полоса между двумя кромками (дорожка, заезд): концы кромок соединяются."""
    a = _clean_coords(first.coords)
    b = _clean_coords(second.coords)
    best: Polygon | None = None
    for other in (list(reversed(b)), b):
        candidate = Polygon(a + other)
        if candidate.is_valid and candidate.area > 0.0:
            if best is None or candidate.length < best.length:
                best = candidate
    return best


def _shape_from_open_lines(lines: Sequence[LineString], min_width: float) -> Polygon | None:
    """
    Приближённая площадь объекта с незамкнутым контуром.

    * одна ломаная (терраса из трёх сторон у стены дома) — замыкается по концам;
    * две кромки (дорожка, заезд) — полоса между ними;
    * иначе — выпуклая оболочка.
    Слишком узкий результат (прямая линия) отбрасывается.
    """
    merged = merge_lines(lines, 0.0) or list(lines)
    candidate: Polygon | None = None
    if len(merged) == 1:
        coords = _clean_coords(merged[0].coords)
        if len(coords) >= 3:
            closed = Polygon(coords)
            if closed.is_valid and closed.area > 0.0:
                candidate = closed
    elif len(merged) == 2:
        candidate = _ribbon_polygon(merged[0], merged[1])
    if candidate is None:
        hull = unary_union(list(merged)).convex_hull
        if hull.geom_type == "Polygon" and hull.area > 0.0:
            candidate = hull
    if candidate is None or _min_width(candidate) < min_width:
        return None
    return candidate


@dataclass
class _LayerShape:
    geometry: BaseGeometry
    closed: bool
    is_area: bool


def _extract_layer_shapes(
    lines: Sequence[LineString],
    *,
    tolerance: float,
    min_width: float,
    pair_gap: float,
) -> list[_LayerShape]:
    """
    Делит линии одного слоя на отдельные объекты: замкнутые контуры, открытые
    контуры (приближённая площадь) и одиночные кромки.
    """
    noded = _noded(snap_endpoints(lines, tolerance))
    if not noded:
        return []
    polys, dangles, cuts, invalid = polygonize_full(noded)
    faces = [poly for poly in _polygon_parts(polys) if poly.is_valid and poly.area > 0.0]
    solids: list[Polygon] = []
    if faces:
        for solid in _polygon_parts(unary_union(faces)):
            solids.append(Polygon(solid.exterior.coords))
    shapes = [_LayerShape(geometry=solid, closed=True, is_area=True) for solid in solids]

    leftover = _line_parts(dangles) + _line_parts(cuts) + _line_parts(invalid)
    if not leftover:
        return shapes
    reach = max(tolerance, 1e-6)
    groups = _connected_groups(leftover, reach)
    # Хвосты, примыкающие к замкнутому контуру, — детали того же объекта.
    free_groups: list[list[LineString]] = []
    for group in groups:
        geom = unary_union(group)
        if any(geom.distance(solid) <= reach for solid in solids):
            continue
        free_groups.append(group)
    if not free_groups:
        return shapes

    # Кромки одного покрытия (две стороны дорожки) объединяются в один объект.
    clusters: list[list[LineString]] = []
    if pair_gap > 0.0 and len(free_groups) > 1:
        group_geoms = [unary_union(group) for group in free_groups]
        parent = list(range(len(free_groups)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        for i in range(len(free_groups)):
            for j in range(i + 1, len(free_groups)):
                if group_geoms[i].distance(group_geoms[j]) <= pair_gap:
                    root_a, root_b = find(i), find(j)
                    if root_a != root_b:
                        parent[root_b] = root_a
        merged_clusters: dict[int, list[LineString]] = {}
        for index, group in enumerate(free_groups):
            merged_clusters.setdefault(find(index), []).extend(group)
        clusters = list(merged_clusters.values())
    else:
        clusters = free_groups

    for cluster in clusters:
        polygon = _shape_from_open_lines(cluster, min_width)
        if polygon is not None:
            shapes.append(_LayerShape(geometry=polygon, closed=False, is_area=True))
            continue
        for chain in merge_lines(cluster, 0.0) or cluster:
            shapes.append(_LayerShape(geometry=chain, closed=False, is_area=False))
    return shapes


def _count_treads(polygon: Polygon, treads: Sequence[LineString], tolerance: float) -> int:
    """Число линий проступей (``*_STEP_TREADS``), лежащих внутри контура ступеней."""
    if not treads:
        return 0
    region = polygon.buffer(max(tolerance, 0.1))
    count = 0
    for tread in treads:
        if not _within_bounds(tread, region.bounds):
            continue
        if tread.intersection(region).length >= 0.5 * tread.length:
            count += 1
    return count


def recognize_features(
    snapshot: DrawingSnapshot,
    boundary: PropertyBoundary,
    cfg: dict[str, Any] | None = None,
    house_text: str = "",
) -> RecognitionResult:
    """
    Находит объекты вокруг участка (полигон + буфер) и определяет их подписи.

    Источник 1 — заметки полевого чертёжника, переведённые в формулировки
    заказчика (``field_note_rules``); источник 2 — коды геодезических точек,
    привязанные к ближайшей линии забора (не дальше ``point_to_fence_max_ft``);
    источник 3 — имя слоя. Описание основного дома берётся из ``house_text``.
    """
    fcfg = feature_settings(cfg)
    classifier = LayerClassifier(fcfg)
    buffer_ft = max(0.0, float(fcfg.get("search_buffer_ft") or 0.0))
    zone = boundary.polygon.buffer(buffer_ft) if buffer_ft > 0 else boundary.polygon
    zone_bounds = zone.bounds
    tolerance = float(fcfg.get("merge_tolerance_ft") or 0.0)
    min_length = float(fcfg.get("min_feature_length_ft") or 0.0)
    min_width = float(fcfg.get("min_area_width_ft") or 0.0)
    pair_gap = float(fcfg.get("edge_pair_max_gap_ft") or 0.0)
    max_code_dist = float(fcfg.get("point_to_fence_max_ft") or 0.0)

    result = RecognitionResult(boundary=boundary, zone=zone)
    next_id = 0

    def new_id() -> int:
        nonlocal next_id
        next_id += 1
        return next_id

    # --- линии по слоям -----------------------------------------------------
    fence_lines: dict[str, list[LineString]] = {}
    structure_lines: dict[str, list[LineString]] = {}
    tread_lines: list[LineString] = []
    for line in snapshot.lines:
        is_fence = classifier.is_fence(line.layer)
        category = classifier.structure_category(line.layer)
        is_tread = classifier.is_tread(line.layer)
        if not (is_fence or category or is_tread):
            continue
        geom = line_to_shapely(line)
        if geom is None or not _within_bounds(geom, zone_bounds):
            continue
        if is_tread:
            tread_lines.append(geom)
        elif category:
            structure_lines.setdefault(line.layer, []).append(geom)
        else:
            fence_lines.setdefault(line.layer, []).append(geom)

    # --- коды точек ---------------------------------------------------------
    codes, unknown = _collect_point_codes(snapshot, classifier, fcfg, zone, max_code_dist)
    result.point_codes = codes
    result.unknown_codes = unknown

    # --- заборы: объединение сегментов и привязка кодов ---------------------
    fence_parts: list[tuple[str, LineString]] = []
    for layer in sorted(fence_lines):
        for merged in merge_lines(fence_lines[layer], tolerance):
            for part in _clip_lines(merged, zone, min_length):
                fence_parts.append((layer, part))

    bound: dict[int, list[PointCode]] = {}
    if fence_parts and max_code_dist > 0.0:
        geoms = [part for _layer, part in fence_parts]
        tree = STRtree(geoms)
        for code in codes:
            if not (code.is_fence and code.label):
                continue
            point = Point(code.point)
            best: tuple[float, float, int] | None = None
            for raw_index in tree.query(point.buffer(max_code_dist), predicate="intersects"):
                index = int(raw_index)
                dist = geoms[index].distance(point)
                if dist > max_code_dist:
                    continue
                # При равном расстоянии (стык забора и калитки) выбирается более длинная линия.
                key = (round(dist, 2), -geoms[index].length, index)
                if best is None or key < best:
                    best = key
            if best is None:
                result.unbound_fence_codes.append(code)
            else:
                bound.setdefault(best[2], []).append(code)
    else:
        result.unbound_fence_codes.extend(
            code for code in codes if code.is_fence and code.label
        )

    abbreviations = fcfg.get("fence_abbreviations")
    gate_plain = _normalize_house_text(str(fcfg.get("gate_label_plain") or "GATE"))

    def gate_label(material: str | None) -> str:
        abbr = lookup_pattern_label(str(material or "").replace("\n", " "), abbreviations)
        if abbr is None:
            return gate_plain
        template = str(fcfg.get("gate_label_format") or "{abbr}\nGATE")
        try:
            return _normalize_house_text(template.format(abbr=abbr[0], material=material))
        except (KeyError, IndexError, ValueError):
            return f"{abbr[0]}\n{gate_plain}"

    # Код на стыке забора с калиткой или соседним забором (точка у самого конца
    # линии, материал другой, чем у остальных точек линии) относится к соседу:
    # забор из-за него не делится, а калитка без своего кода получает этот код.
    end_reach = max(1.0, max_code_dist)
    for index, (layer, part) in enumerate(fence_parts):
        part_codes = bound.get(index) or []
        if classifier.is_gate(layer) or len(part_codes) < 2:
            continue
        positions = {id(code): part.project(Point(code.point)) for code in part_codes}
        inner = [
            code for code in part_codes
            if end_reach < positions[id(code)] < part.length - end_reach
        ]
        majority = {code.label for code in inner} or {
            max(
                {code.label for code in part_codes},
                key=lambda label: sum(1 for code in part_codes if code.label == label),
            )
        }
        for code in list(part_codes):
            if code in inner or code.label in majority:
                continue
            part_codes.remove(code)
            point = Point(code.point)
            for other_index, (other_layer, other_part) in enumerate(fence_parts):
                if other_index == index or not classifier.is_gate(other_layer):
                    continue
                if other_part.distance(point) <= max_code_dist and not bound.get(other_index):
                    bound.setdefault(other_index, []).append(code)
                    break

    for index, (layer, part) in enumerate(fence_parts):
        part_codes = bound.get(index) or []
        if classifier.is_gate(layer):
            # Калитка: материал — по коду точки, иначе по соседнему забору (ниже).
            first = part_codes[0] if part_codes else None
            result.features.append(
                Feature(
                    feature_id=new_id(), kind="fence", category="gate", layer=layer,
                    geometry=part,
                    label=gate_label(first.label) if first else None,
                    source="point_code" if first else None,
                    rule=f"{first.code} → {first.pattern}" if first else "",
                    codes=tuple(part_codes),
                )
            )
            continue
        if part_codes:
            for piece, piece_codes in _split_fence_by_labels(
                part, part_codes, corner_reach=max(1.0, max_code_dist)
            ):
                if piece.length < min_length:
                    continue
                first = piece_codes[0]
                result.features.append(
                    Feature(
                        feature_id=new_id(), kind="fence", category="fence", layer=layer,
                        geometry=piece, label=first.label, source="point_code",
                        rule=f"{first.code} → {first.pattern}", codes=tuple(piece_codes),
                    )
                )
            continue
        mapped = classifier.layer_label(layer)
        result.features.append(
            Feature(
                feature_id=new_id(), kind="fence", category="fence", layer=layer,
                geometry=part,
                label=mapped[0] if mapped else None,
                source="layer" if mapped else None,
                rule=mapped[1] if mapped else "",
            )
        )

    # --- калитки без кода точки: материал соседнего забора ------------------
    reach = max(0.0, float(fcfg.get("gate_adjacent_tolerance_ft") or 0.0))
    fences = [f for f in result.features if f.category == "fence" and f.label]
    for gate in result.features:
        if gate.category != "gate" or gate.label:
            continue
        near = [f for f in fences if f.geometry.distance(gate.geometry) <= reach]
        if near:
            # Материал, определённый по коду точки, надёжнее имени слоя.
            donor = min(
                near,
                key=lambda f: (0 if f.source == "point_code" else 1, -f.geometry.length),
            )
            gate.label = gate_label(donor.label)
            gate.source = "adjacent"
            gate.rule = f"{donor.layer}: {donor.flat_label}"
        else:
            gate.label = gate_plain
            gate.source = "layer"
            gate.rule = "gate_layers"

    # --- строения, крыльца, ступени, покрытия -------------------------------
    count_treads = bool(fcfg.get("step_count_labels", False))
    house_candidates: list[Feature] = []
    for layer in sorted(structure_lines):
        category = classifier.structure_category(layer) or "structure"
        shapes = _extract_layer_shapes(
            structure_lines[layer],
            tolerance=tolerance,
            min_width=min_width,
            pair_gap=pair_gap if category == "surface" else 0.0,
        )
        mapped = classifier.layer_label(layer)
        for shape in shapes:
            geometry = shape.geometry
            if not geometry.intersects(zone):
                continue
            if not shape.is_area:
                clipped = _clip_lines(geometry, zone, min_length)  # type: ignore[arg-type]
                if not clipped:
                    continue
                geometry = max(clipped, key=lambda part: part.length)
            feature = Feature(
                feature_id=new_id(),
                kind="area" if shape.is_area else "edge",
                category=category,
                layer=layer,
                geometry=geometry,
                label=mapped[0] if mapped else None,
                source="layer" if mapped else None,
                rule=mapped[1] if mapped else "",
                closed=shape.closed,
            )
            if count_treads and category == "steps" and feature.label and feature.kind == "area":
                treads = _count_treads(geometry, tread_lines, tolerance)  # type: ignore[arg-type]
                if treads > 0:
                    key = "step_count_label_format_single" if treads == 1 else "step_count_label_format"
                    template = str(fcfg.get(key) or fcfg.get("step_count_label_format") or "{n} STEPS")
                    feature.tread_count = treads
                    try:
                        feature.label = template.format(n=treads, N=treads)
                    except (KeyError, IndexError, ValueError):
                        feature.label = f"{treads} STEPS"
            if category == "house" and feature.kind == "area":
                house_candidates.append(feature)
            result.features.append(feature)

    # --- основной дом: текст из окна программы ------------------------------
    house_label = _normalize_house_text(house_text)
    if house_label:
        main_house = _select_main_house(house_candidates, boundary.polygon, classifier)
        if main_house is None:
            house_layers = ", ".join(
                _as_patterns((fcfg.get("structure_layers") or {}).get("house"))
            )
            result.notes.append(
                f"описание дома «{house_label}» не размещено: на участке нет контура дома "
                f"в слоях {house_layers}"
            )
        else:
            main_house.is_main_house = True
            main_house.label = house_label
            main_house.source = "user"
            main_house.rule = "поле «Описание основного дома»"

    # --- заметки полевого чертёжника: формулировки заказчика ----------------
    result.field_notes = _collect_field_notes(snapshot, classifier, fcfg, zone)
    _apply_field_notes(result, result.field_notes, fcfg, new_id)

    # --- точечные объекты (столбы, колодцы, гидранты) -----------------------
    if bool(fcfg.get("label_point_features", False)):
        for code in codes:
            if code.is_fence or not code.label:
                continue
            point = Point(code.point)
            if not zone.contains(point):
                continue
            result.features.append(
                Feature(
                    feature_id=new_id(), kind="point", category="point", layer=code.layer,
                    geometry=point, label=code.label, source="point_code",
                    rule=f"{code.code} → {code.pattern}", codes=(code,),
                )
            )

    if result.unbound_fence_codes:
        titles = ", ".join(code.title for code in result.unbound_fence_codes[:6])
        more = "…" if len(result.unbound_fence_codes) > 6 else ""
        result.notes.append(
            f"коды заборов без линии в радиусе {max_code_dist:g} ft: {titles}{more}"
        )
    return result


def _normalize_house_text(text: str) -> str:
    """Текст дома: строки через ``\\P`` или перевод строки -> внутренний ``\\n``."""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\\P", "\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.split("\n")]
    return "\n".join(line for line in lines if line)


def _select_main_house(
    candidates: Sequence[Feature], lot: Polygon, classifier: LayerClassifier
) -> Feature | None:
    """Основной дом — самый крупный контур дома, лежащий на участке."""
    scored: list[tuple[int, float, Feature]] = []
    for feature in candidates:
        polygon = feature.geometry
        if polygon.geom_type != "Polygon" or polygon.area <= 0.0:
            continue
        try:
            inside = polygon.intersection(lot).area
        except Exception:  # noqa: BLE001 - некорректная геометрия исходного чертежа
            continue
        if inside <= 0.0 or inside < 0.5 * polygon.area:
            continue
        scored.append((classifier.structure_rank(feature.layer), -inside, feature))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]))
    return scored[0][2]

