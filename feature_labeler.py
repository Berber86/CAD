"""
feature_labeler.py
==================
Шаг 2 сценария геодезиста: автоматическое распознавание и подписание объектов
на участке (Feature Labeling) — ПОСЛЕ усадки границы участка и ПЕРЕД
расстановкой офсетов.

Модуль работает с уже открытым в AutoCAD чертежом полевого съёмщика (DWG):

  * читает все объекты ModelSpace активного документа через ActiveX/COM
    (`ModelSpace`, `LWPOLYLINE`, `POLYLINE`, `LINE`, `TEXT`, `MTEXT`,
    `INSERT` → `Explode` и т.п.);
  * определяет целевой полигон участка: либо из `DrawPlan` (шаг 1 —
    вкладка «Границы участка»), либо по слоям `property_line_layers`,
    либо по указанной пользователем замкнутой полилинии;
  * ищет неподписанные объекты в буфере `search_buffer_ft` (по умолчанию
    50 футов) вокруг участка и определяет для них текст подписи по двум
    источникам:
      1. коды геодезических точек из слоёв `POINT_LABELS` / `PNT-X` / `PNTS`
         (`'2 FEC'`, `'3 FEV64'` → «CHAIN LINK FENCE», «VINYL FENCE»),
         код привязывается к ближайшей линии забора в радиусе 3.5 футов;
      2. имена слоёв (`layer_label_mapping`: `FENCE_VINYL*`, `SUBJECT_SHED_OUTLINE`,
         `SIDE_PORCH`, `FRONT_STEPS`, `DRIVEWAY_EDGE`, `PLANTER_EDGE`, …), причём
         для ступенек считается количество линий `*_STEP_TREADS` внутри контура
         и подпись становится `"3 STEPS"`;
  * раскладывает подписи чистой математикой (без COM): заборы — параллельно
    линии посередине (`linemerge` смежных сегментов), строения/крыльцо/терраса —
    `MText` внутри контура по центру, узкие ступеньки/клумбы — выноска со
    стрелкой (`Leader` + `MText`) на свободное место; всё проверяется
    `shapely.geometry.box`, чтобы текстовые блоки не перекрывали друг друга,
    линии заборов, контуры строений и уже существующие надписи;
  * дедупликация: перед подписанием читаются все тексты из слоёв
    `D-TOPO-TEXT`, `FENCE_NOTE`, `ENTRANCE_TEXT`, `SURFACE_TEXT`,
    `CONTEXT_TEXT` и объект не подписывается повторно, если в радиусе
    6.0 футов уже стоит аналогичная надпись.

Модуль полностью тестируется без AutoCAD: чтение/запись выполняются через
тонкий слой `AcadDrawing`, у которого есть in-memory замена `FakeCadDrawing`.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# ---------------------------------------------------------------------------
# shapely — геометрическое ядро раскладки подписей
# ---------------------------------------------------------------------------
try:  # pragma: no cover - ветка исполняется только без установленного shapely
    from shapely import STRtree
    from shapely.geometry import (
        LineString,
        MultiLineString,
        MultiPoint,
        MultiPolygon,
        Point,
        Polygon,
        box as shapely_box,
    )
    from shapely.geometry.base import BaseGeometry
    from shapely.ops import linemerge, nearest_points, polygonize, unary_union

    SHAPELY_AVAILABLE = True
    SHAPELY_IMPORT_ERROR = ""
except Exception as _shapely_exc:  # noqa: BLE001 - нужен любой сбой импорта
    STRtree = None  # type: ignore[assignment]
    LineString = MultiLineString = MultiPoint = MultiPolygon = Point = Polygon = None  # type: ignore[assignment,misc]
    BaseGeometry = object  # type: ignore[assignment,misc]
    linemerge = nearest_points = polygonize = unary_union = None  # type: ignore[assignment]
    shapely_box = None  # type: ignore[assignment]

    SHAPELY_AVAILABLE = False
    SHAPELY_IMPORT_ERROR = str(_shapely_exc)


ACAD_PROGID = "AutoCAD.Application"


class FeatureLabelerError(RuntimeError):
    """Ошибка распознавания/подписания объектов (шов для GUI)."""


class ShapelyMissingError(FeatureLabelerError):
    """Не установлен shapely — раскладка подписей без него не работает."""


def ensure_shapely() -> None:
    """Явная проверка зависимости с понятным сообщением для геодезиста."""
    if not SHAPELY_AVAILABLE:
        raise ShapelyMissingError(
            "Не установлена библиотека shapely (нужна для шага 2 — автоподписание "
            "объектов).\nВыполните:  python -m pip install -r requirements.txt\n"
            f"Детали импорта: {SHAPELY_IMPORT_ERROR}"
        )


# ---------------------------------------------------------------------------
# Нормализация имён слоёв и кодов точек
# ---------------------------------------------------------------------------

_NON_LAYER_CHARS = re.compile(r"[^A-Z0-9*?]+")


def normalize_layer_name(name: str | None) -> str:
    """
    Приводит имя слоя к верхнему регистру без пробелов/дефисов.

    `"S-Fence"` → ``"S_FENCE"``, ``"M-Tax-Line"`` → ``"M_TAX_LINE"`` —
    сравнение в config.json не зависит от регистра и разделителей.
    """
    raw = str(name or "").strip().upper()
    if not raw:
        return ""
    raw = raw.replace("$", "S")
    return _NON_LAYER_CHARS.sub("_", raw).strip("_")


def layer_matches_pattern(layer: str, pattern: str) -> bool:
    """
    Совпадение имени слоя с шаблоном из config.json.

    Поддерживаются `*` и `?` (как в AutoCAD wildcard), а также «голый» префикс:
    шаблон ``LOW_EDGE`` совпадёт и с ``LOW_EDGE_3``, и с ``LOW_EDGE``.
    """
    lay = normalize_layer_name(layer)
    pat = normalize_layer_name(pattern)
    if not lay or not pat:
        return False
    if pat.endswith("*") or pat.endswith("?"):
        return _wildcard_match(lay, pat)
    return lay == pat or lay.startswith(pat + "_") or _wildcard_match(lay, pat + "*")


def _wildcard_match(value: str, pattern: str) -> bool:
    regex = "^" + "".join(
        ".*" if ch == "*" else ("." if ch == "?" else re.escape(ch)) for ch in pattern
    ) + "$"
    return re.fullmatch(regex, value) is not None


_TEXT_LAYER_NOTE = re.compile(r"(^|_)(NOTE|NOTES|TEXT|TXT|TAGS?|ANNO)(_|$)")


def matches_tread_layer(layer: str, patterns: Sequence[str]) -> bool:
    """
    Слой проточки ступеньки.

    Шаблон ``*_STEP_TREADS`` из config.json совпадает и с ``FRONT_STEP_TREADS``,
    и с ``FRONT_STEPS_TREADS``: полевые чертёжники пишут и «STEP», и «STEPS».
    """
    lay = normalize_layer_name(layer)
    for raw in patterns:
        pat = normalize_layer_name(raw)
        if not pat:
            continue
        if layer_matches_pattern(lay, pat):
            return True
        if "STEP_" in pat and layer_matches_pattern(lay, pat.replace("STEP_", "STEPS_")):
            return True
        if "STEPS_" in pat and layer_matches_pattern(lay, pat.replace("STEPS_", "STEP_")):
            return True
    return False


def is_note_text_layer(layer: str) -> bool:
    """`*_NOTE` / `*_TEXT` — слои с пояснительными надписями, а не геометрией."""
    return _TEXT_LAYER_NOTE.search(normalize_layer_name(layer)) is not None


# ---------------------------------------------------------------------------
# Справочники шага 2 — ровно та структура входящего DWG, что указана в config.json
# ---------------------------------------------------------------------------

DEFAULT_FEATURE_CFG: dict[str, Any] = {
    "property_line_layers": ["PROPERTY_LINE", "PL", "M-TAX-LINE", "PROPERTY_LINES_APPROX"],
    "point_label_layers": ["POINT_LABELS", "PNT-X", "PNTS"],
    "fence_layers": ["S-FENCE", "ST-WALL", "CODE_CREW"],
    "fence_prefixes": ["FENCE_", "GATE_", "LOW_EDGE_"],
    "fence_exclude_patterns": ["*_NOTE", "*_TEXT"],
    "structure_layers": {
        "house": ["BUILDING_OUTLINE", "ST-BLDG-LINE", "REF_VEC_BUILDING"],
        "shed": ["SUBJECT_SHED_OUTLINE", "NEIGHBOUR_SHED_OUTLINE"],
        "porch": [
            "SIDE_PORCH",
            "NEIGHBOUR_SIDE_PORCH",
            "FRONT_LANDING",
            "REAR_DECK_EDGE",
        ],
        "steps": ["FRONT_STEPS", "REAR_STEPS", "ST-STEP", "NEIGHBOUR_SIDE_STAIR"],
        "walk": [
            "DRIVEWAY_EDGE",
            "SIDEWALK_EDGE",
            "WALK_EDGE",
            "WALK_43_SKETCH",
            "PLANTER_EDGE",
            "SIDE_PLANTER_5",
            "GRASS_STRIP_EDGE",
        ],
    },
    "step_tread_pattern": "*_STEP_TREADS",
    "label_output_layer": "D-TOPO-TEXT",
    "dedupe_text_layers": [
        "D-TOPO-TEXT",
        "FENCE_NOTE",
        "ENTRANCE_TEXT",
        "SURFACE_TEXT",
        "CONTEXT_TEXT",
    ],
    "search_buffer_ft": 50.0,
    "point_code_radius_ft": 3.5,
    "dedupe_radius_ft": 6.0,
    "text_height": 0.16,
    "text_height_by_scale": {"factor": 0.18},
    "mtext_width_factor": 6.5,
    "text_clearance_ft": 0.35,
    "parallel_text_offset_factor": 0.8,
    "min_parallel_extra_ft": 4.0,
    "leader_gap_factor": 2.2,
    "leader_min_length_ft": 2.5,
    "leader_max_length_ft": 60.0,
    "max_leader_attempts": 16,
    "leader_arc_factor": 0.35,
    "fence_area_tolerance_sqft": 1.0,
    "filled_fence_dedupe_ft": 1.5,
    "curved_segment_min_ft": 6.0,
    "curved_segment_step_deg": 26.0,
    "text_angle_tolerance_deg": 10.0,
    "label_house_inside": True,
    "house_label": "",
    "skip_existing_labels": True,
    "annotated_radius_ft": 3.0,
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
    "layer_label_mapping": {
        "FENCE_CHAIN*": "CHAIN LINK FENCE",
        "FENCE_VINYL*": "VINYL FENCE",
        "FENCE_WOOD*": "WOOD FENCE",
        "LOW_EDGE*": "LOW TIMBER EDGE",
        "SUBJECT_SHED_OUTLINE": "SHED",
        "SIDE_PORCH": "PORCH",
        "FRONT_LANDING": "PORCH",
        "REAR_DECK_EDGE": "WOOD DECK",
        "FRONT_STEPS": "STEPS",
        "REAR_STEPS": "STEPS",
        "ST-STEP": "STEPS",
        "DRIVEWAY_EDGE": "DRIVEWAY",
        "SIDEWALK_EDGE": "CONCRETE WALK",
        "WALK_EDGE": "CONCRETE WALK",
        "PLANTER_EDGE": "PLANTER",
        "SIDE_PLANTER*": "PLANTER",
    },
    "curved_fence_prefixes": ["FENCE_ARC_", "FENCE_CURVED_"],
    "show_debug_markers": True,
    "zoom_to_labels": True,
}

# Категории объектов: «штриховая» геометрия (заборы, бордюры) и «контурная».
LINE_CATEGORIES = {
    "fence": "fence",
    "low_edge": "edge",
    "shed": "outline",
    "porch": "outline",
    "deck": "outline",
    "steps": "outline",
    "driveway": "outline",
    "walk": "outline",
    "planter": "outline",
    "building": "outline",
    "unknown": "outline",
}

# Категории, которые подписываются ПОСЛЕ остальных (чтобы главный дом
# получал свободное место в центре пятна застройки).
_LABEL_PRIORITY = {
    "building": 0,   # последним: центр пятна застройки
    "low_edge": 1,   # бордюры — плотные короткие линии, им нужен воздух
    "planter": 2,
    "steps": 3,
    "porch": 4,
    "deck": 4,
    "shed": 5,
    "driveway": 6,
    "walk": 7,
    "fence": 8,
    "unknown": 9,
}

_FENCE_CODE_FAMILY = {"FEC": "fence", "FEV": "fence", "FEW": "fence", "FEI": "fence"}
_POINT_CODE_RE = re.compile(r"^\s*(?P<num>[0-9]{1,4})?\s*(?P<code>[A-Za-z]{2,4})(?P<digits>[0-9]{0,3})\s*$")

_SKIP_CODE_WORDS = {
    "NO", "NOS", "AT", "TO", "OF", "ST", "EL", "ELEV", "FROM", "AND", "SEE",
    "NOTE", "LINE", "SET", "FND", "FOUND", "CALC", "CALCULATED", "EXIST",
    "EXISTING", "PROP", "PROPOSED", "DWG", "DRAWING", "PLAT", "DEED",
}


def configure_feature_cfg(raw: dict[str, Any] | None) -> dict[str, Any]:
    """
    Дополняет секцию конфига значениями по умолчанию (без потери пользовательских).

    cfg — либо полный config.json (тогда берётся секция ``features``/``labeling``),
    либо уже сама секция. Возвращает независимую копию.
    """
    source = raw or {}
    if "features" in source and isinstance(source.get("features"), dict):
        source = source["features"]
    elif "labeling" in source and isinstance(source.get("labeling"), dict):
        source = source["labeling"]
    cfg: dict[str, Any] = {}
    for key, value in DEFAULT_FEATURE_CFG.items():
        if isinstance(value, dict):
            cfg[key] = dict(value)
        elif isinstance(value, list):
            cfg[key] = list(value)
        else:
            cfg[key] = value
    for key, value in source.items():
        if key not in cfg:
            cfg[key] = value
        elif isinstance(cfg[key], dict) and isinstance(value, dict):
            merged = dict(cfg[key])
            merged.update(value)
            cfg[key] = merged
        elif isinstance(cfg[key], list) and isinstance(value, (list, tuple)):
            cfg[key] = [str(item) for item in value]
        else:
            cfg[key] = value
    return cfg


def feature_text_height(feature_cfg: dict[str, Any], scale: float | None = None) -> float:
    """Высота подписи плана: из config.json либо по масштабу чертежа."""
    explicit = feature_cfg.get("text_height")
    by_scale = feature_cfg.get("text_height_by_scale") or {}
    factor = float((by_scale or {}).get("factor", 0.02) or 0.02)
    if explicit not in (None, "", 0):
        # Явная высота из config.json/поля GUI всегда главнее пересчёта по масштабу.
        try:
            return max(0.02, float(explicit))
        except (TypeError, ValueError):
            pass
    if scale and float(scale) > 0:
        # Масштаб из шапки (1"=16') — это футы на дюйм, поэтому коэффициент малый.
        return max(0.02, factor * float(scale))
    return 0.16


# ---------------------------------------------------------------------------
# Коды и подписи
# ---------------------------------------------------------------------------


def parse_point_code(text: str) -> tuple[str | None, str, str] | None:
    """
    Разбирает текст геодезической точки ``'<Номер> <Код>'``.

    ``'2 FEC'`` → ``("2", "FEC", "")``; ``'3 FEV64'`` → ``("3", "FEV", "64")``.
    Возвращает None, если это не код точки (например, «PROPERTY LINE», «1/4" IRON PIPE»).
    """
    cleaned = re.sub(r"[^0-9A-Za-z ]+", " ", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or len(cleaned) > 24:
        return None
    match = _POINT_CODE_RE.match(cleaned)
    if match is None:
        return None
    code = (match.group("code") or "").upper()
    if code in _SKIP_CODE_WORDS or len(code) < 2:
        return None
    return (match.group("num") or None, code, (match.group("digits") or ""))


def label_for_point_code(text: str, dictionary: dict[str, Any] | None = None) -> str | None:
    """
    Подпись для кода точки по ``point_code_dictionary`` из config.json.

    ``FEC*`` → «CHAIN LINK FENCE», ``FEV64`` → «VINYL FENCE» (цифры высоты
    намеренно не уменьшают ценность подписи — базово всегда «VINYL FENCE»).
    """
    parsed = parse_point_code(text)
    if parsed is None:
        return None
    _num, code, _digits = parsed
    dictionary = dictionary or DEFAULT_FEATURE_CFG["point_code_dictionary"]
    for pattern, label in dictionary.items():
        pat = normalize_layer_name(pattern)
        if not pat:
            continue
        if pat.endswith("*"):
            if code.startswith(pat[:-1]):
                return str(label)
        elif code == pat:
            return str(label)
    return None


def point_code_family(text: str) -> str | None:
    """``'FEC'``/``'FEV64'`` → ``'fence'`` (семейство объекта), иначе None."""
    parsed = parse_point_code(text)
    if parsed is None:
        return None
    return _FENCE_CODE_FAMILY.get(parsed[1])


def classify_layer(layer: str, feature_cfg: dict[str, Any] | None = None) -> tuple[str, str | None]:
    """
    Определяет роль слоя: ``(категория, подпись_по_слою)``.

    Категория нужна для порядка подписания и для эвристик «не перекрывать»;
    подпись берётся из ``layer_label_mapping``.
    """
    cfg = feature_cfg or DEFAULT_FEATURE_CFG
    lay = normalize_layer_name(layer)
    mapping = cfg.get("layer_label_mapping") or {}
    label: str | None = None
    for pattern, text in mapping.items():
        if layer_matches_pattern(lay, str(pattern)):
            label = str(text)
            break
    if is_note_text_layer(lay):
        return "unknown", None
    if _matches_any(lay, cfg.get("fence_layers") or []) or _matches_prefix(
        lay, cfg.get("fence_prefixes") or [], cfg
    ):
        if layer_matches_pattern(lay, "LOW_EDGE*"):
            return "low_edge", label or "LOW TIMBER EDGE"
        if label or layer_matches_pattern(lay, "GATE_*"):
            return "fence", label
        return "fence", None
    structures = cfg.get("structure_layers") or {}
    if _matches_any(lay, structures.get("house") or []):
        return "building", label
    if _matches_any(lay, structures.get("shed") or []):
        return "shed", label or "SHED"
    if _matches_any(lay, structures.get("porch") or []):
        if layer_matches_pattern(lay, "REAR_DECK_EDGE"):
            return "deck", label or "WOOD DECK"
        return "porch", label or "PORCH"
    if _matches_any(lay, structures.get("steps") or []):
        return "steps", label or "STEPS"
    if _matches_any(lay, structures.get("walk") or []):
        if layer_matches_pattern(lay, "DRIVEWAY_EDGE"):
            return "driveway", label or "DRIVEWAY"
        if layer_matches_pattern(lay, "PLANTER*") or layer_matches_pattern(lay, "SIDE_PLANTER*"):
            return "planter", label or "PLANTER"
        if layer_matches_pattern(lay, "GRASS_STRIP_EDGE"):
            return "walk", label
        return "walk", label or "CONCRETE WALK"
    return "unknown", label


def fence_prefix_label(layer: str, feature_cfg: dict[str, Any] | None = None) -> str | None:
    """
    Подпись забора по префиксу слоя, когда словарь не содержит точного имени.

    ``FENCE_VINYL_CURVED`` / ``FENCE_ARC_VINYL`` / ``FENCE_CHAIN_6FT`` →
    «VINYL FENCE» / «CHAIN LINK FENCE» — полевые слои часто имеют хвосты.
    """
    cfg = feature_cfg or DEFAULT_FEATURE_CFG
    lay = normalize_layer_name(layer)
    if layer_matches_pattern(lay, "LOW_EDGE*"):
        return "LOW TIMBER EDGE"
    for token, label in (
        ("CHAIN", "CHAIN LINK FENCE"),
        ("VINYL", "VINYL FENCE"),
        ("WOOD", "WOOD FENCE"),
        ("IRON", "IRON FENCE"),
        ("FEC", "CHAIN LINK FENCE"),
        ("FEV", "VINYL FENCE"),
        ("FEW", "WOOD FENCE"),
        ("FEI", "IRON FENCE"),
    ):
        if token in lay.split("_"):
            return label
    mapping = cfg.get("layer_label_mapping") or {}
    for pattern, text in mapping.items():
        if layer_matches_pattern(lay, str(pattern)):
            return str(text)
    return None


def _matches_any(layer: str, patterns: Iterable[str]) -> bool:
    return any(layer_matches_pattern(layer, str(pat)) for pat in patterns)


def _matches_prefix(layer: str, prefixes: Iterable[str], cfg: dict[str, Any]) -> bool:
    for raw_prefix in prefixes:
        prefix = normalize_layer_name(raw_prefix)
        if not prefix:
            continue
        if not layer.startswith(prefix):
            continue
        if any(layer_matches_pattern(layer, str(pat)) for pat in (cfg.get("fence_exclude_patterns") or [])):
            continue
        return True
    return False


def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]+", " ", str(text or "").upper())).strip()


def dedupe_key(label: str) -> str:
    """
    Ключ «аналогичности» подписи для дедупликации.

    «VINYL FENCE», «Vinyl Fence (6' HIGH)», «VINYL FENCE» → ``"VINYL FENCE"``;
    «3 STEPS» и «4 STEPS» — РАЗНЫЕ ключи (число ступенек важно для геодезиста).
    """
    text = str(label or "")
    # Пояснения в скобках и хвосты про высоту («(6' HIGH)», «6 FT») не меняют тип
    # объекта: «VINYL FENCE (6' HIGH)» и «VINYL FENCE» — одна и та же надпись.
    text = re.sub(r"\([^)]*\)", " ", text)
    norm = _norm_label(text)
    norm = re.sub(r"\s+\d+\s*(FT|FOOT|FEET)\b", "", norm)
    norm = re.sub(r"^(\d+)\s+(STEP|STORY|STY)\b", r"\1 \2", norm)
    return re.sub(r"\s+", " ", norm).strip()


def labels_equivalent(a: str, b: str) -> bool:
    """Одна и та же надпись (с учётом родительного падежа «STEPS»/«STEPS»)."""
    ka, kb = dedupe_key(a), dedupe_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    # «3 STEPS» ↔ «STEPS» считаем разными; «WOOD FENCE» ⊂ «WOOD FENCE 6FT» — тоже
    # разными. Совпадают только точные нормализованные формы и ед./мн. число.
    singles = {
        ("STEP", "STEPS"),
        ("PLANTER", "PLANTERS"),
        ("STOREY", "STORIES"),
        ("STORY", "STORIES"),
        ("STY", "STORY"),
        ("STY", "STORIES"),
        ("FRAME", "FRK"),
        ("BRICK", "BRK"),
        ("CHAIN", "CHAINLINK"),
    }
    ta, tb = ka.split(), kb.split()
    if len(ta) == len(tb):
        for idx, (wa, wb) in enumerate(zip(ta, tb)):
            if wa == wb:
                continue
            if (wa, wb) in singles or (wb, wa) in singles:
                continue
            return False
        return True
    return False


# ---------------------------------------------------------------------------
# Структуры данных
# ---------------------------------------------------------------------------


@dataclass
class CadText:
    """Текст/MTEXT, уже стоящий в чертеже (для дедупликации)."""

    text: str
    point: tuple[float, float]
    layer: str
    height: float = 0.0
    rotation: float = 0.0


@dataclass
class CadFeature:
    """Один распознанный объект чертежа (изолированный контур или линия)."""

    category: str
    layer: str
    entity_type: str
    geom: Any                      # shapely Polygon / MultiPolygon / LineString / MultiLineString
    handle: str = ""
    label: str | None = None
    source: str = ""               # "point_code" | "layer" | "user"
    code_text: str = ""            # исходный код точки, например '3 FEV64'
    treads: int = 0                # посчитанные линии *_STEP_TREADS
    is_house: bool = False
    closed: bool = False
    curved: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def area(self) -> float:
        try:
            return float(self.geom.area)
        except Exception:  # noqa: BLE001
            return 0.0

    @property
    def length(self) -> float:
        try:
            return float(self.geom.length)
        except Exception:  # noqa: BLE001
            return 0.0

    @property
    def is_linear(self) -> bool:
        return LINE_CATEGORIES.get(self.category, "outline") in {"fence", "edge"}

    @property
    def is_empty_dimension(self) -> bool:
        return self.is_linear and self.length < 0.3

    def centroid(self) -> tuple[float, float]:
        try:
            c = self.geom.centroid
            return (float(c.x), float(c.y))
        except Exception:  # noqa: BLE001
            return (0.0, 0.0)

    def bounds(self) -> tuple[float, float, float, float]:
        try:
            minx, miny, maxx, maxy = self.geom.bounds
            return (float(minx), float(miny), float(maxx), float(maxy))
        except Exception:  # noqa: BLE001
            return (0.0, 0.0, 0.0, 0.0)

    @property
    def shortest_side(self) -> float:
        """Толщина контура: минимальная сторона bbox (для проверки «текст влезает»)."""
        minx, miny, maxx, maxy = self.bounds()
        return min(maxx - minx, maxy - miny)


@dataclass
class LabelPlan:
    """Готовая подпись: либо MText внутри контура, либо выноска с включённым текстом."""

    text: str
    point: tuple[float, float]
    layer: str
    height: float
    rotation: float = 0.0
    mode: str = "inside"           # inside | parallel | leader
    box: Any = None                # shapely Polygon — занятая подписью область
    leader_points: list[tuple[float, float]] = field(default_factory=list)
    leader_text_point: tuple[float, float] | None = None
    source: str = ""
    category: str = ""
    feature_handle: str = ""
    feature_layer: str = ""
    anchor_points: list[tuple[float, float]] = field(default_factory=list)
    is_house: bool = False

    def leader_summary(self) -> str:
        if not self.leader_points or self.leader_text_point is None:
            return ""
        x0, y0 = self.leader_points[0]
        x1, y1 = self.leader_text_point
        return (
            f"выноска ({x0:+.2f}, {y0:+.2f}) → ({x1:+.2f}, {y1:+.2f})"
        )


@dataclass
class LabelingStats:
    """Подробный отчёт для лог-панели GUI (сколько найдено и как подписано)."""

    points_total: int = 0
    point_codes_total: int = 0
    point_codes_used: int = 0
    point_codes_unmapped: int = 0
    point_codes_out_of_radius: int = 0
    point_codes_on_filled_object: int = 0
    point_codes_non_fence: int = 0
    features_layer_total: int = 0
    features_geometry_total: int = 0
    features_in_buffer: int = 0
    inside_lot: int = 0
    created: int = 0
    parallel: int = 0
    leaders: int = 0
    skipped_existing: int = 0
    skipped_no_label: int = 0
    skipped_no_room: int = 0
    skipped_no_label_layer: int = 0
    treads_labels: int = 0
    house_labeled: int = 0
    existing_texts: int = 0
    layers_written: list[str] = field(default_factory=list)
    per_category: dict[str, int] = field(default_factory=dict)
    per_label: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False
    point_code_radius_ft: float = 3.5
    elapsed_sec: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        payload = {k: v for k, v in self.__dict__.items()}
        return payload

    def format_report(self, *, compact: bool = False) -> str:
        """Русскоязычный отчёт для панели «Журнал» и буфера обмена."""
        lines: list[str] = []
        head = "АВТОПОДПИСАНИЕ ОБЪЕКТОВ (D-TOPO-TEXT)"
        if self.dry_run:
            head += " · ПРОБНЫЙ ПРОГОН (без записи)"
        lines.append(head)
        lines.append("─" * 62)
        lines.append(
            f"Текстов точек в слоях POINT_LABELS: {self.points_total} "
            f"(кодов распознано: {self.point_codes_total})"
        )
        lines.append(f"Кодов привязано к заборам: {self.point_codes_used}")
        if self.point_codes_unmapped:
            lines.append(f"  · код без словаря: {self.point_codes_unmapped}")
        if self.point_codes_out_of_radius:
            lines.append(
                "  · код дальше "
                f"{self._radius_hint()}: {self.point_codes_out_of_radius}"
                " (подпись пойдёт по имени слоя)"
            )
        if self.point_codes_on_filled_object:
            lines.append(
                f"  · код на объекте с приоритетной подписью: "
                f"{self.point_codes_on_filled_object}"
            )
        if self.point_codes_non_fence:
            lines.append(
                "  · кодов точечных объектов (столбы/гидранты/кессоны): "
                f"{self.point_codes_non_fence} — относятся к точкам, а не к заборам"
            )
        lines.append(
            f"Геометрии на слоях объектов: {self.features_layer_total}; "
            f"изолированных контуров/линий: {self.features_geometry_total}; "
            f"в буфере участка: {self.features_in_buffer}"
        )
        lines.append(
            f"Найдено объектов: {self.features_in_buffer} "
            f"(по кодам POINT_LABELS — {self.point_codes_used}, "
            f"по слоям — {max(0, self.features_in_buffer - self.point_codes_used)})"
        )
        lines.append(
            f"Подписано объектов: {self.created} "
            f"(параллельно линии забора — {self.parallel}, выноской — {self.leaders}, "
            f"внутри контура — {max(0, self.created - self.parallel - self.leaders)})"
        )
        if self.treads_labels:
            lines.append(f"  · подписей ступенек с подсчётом *_STEP_TREADS: {self.treads_labels}")
        if self.is_site_label_present:
            lines.append("  · главный дом подписан описанием из GUI")
        lines.append(f"Существующих текстов прочитано: {self.existing_texts}")
        lines.append(f"Пропущено как уже подписанные: {self.skipped_existing}")
        if self.skipped_no_label:
            lines.append(f"Пропущено без подписи (нет кода/слоя в словаре): {self.skipped_no_label}")
        if self.skipped_no_room:
            lines.append(f"Пропущено из-за нехватки места под текст: {self.skipped_no_room}")
        if self.skipped_no_label_layer:
            lines.append(
                "Предупреждение: целевой слой не создан, подписи ушли в исходные слои: "
                f"{self.skipped_no_label_layer}"
            )
        if self.per_label:
            top = sorted(self.per_label.items(), key=lambda kv: (-kv[1], kv[0]))
            lines.append(
                "Подписи: "
                + ", ".join(f"{text} × {count}" for text, count in top[:12])
            )
        if self.per_category:
            cats = sorted(self.per_category.items(), key=lambda kv: (-kv[1], kv[0]))
            lines.append(
                "Категории: " + ", ".join(f"{cat} × {count}" for cat, count in cats)
            )
        if self.layers_written:
            lines.append("Слои подписей: " + ", ".join(self.layers_written))
        for note in self.notes:
            lines.append(f"• {note}")
        for warning in self.warnings:
            lines.append(f"! {warning}")
        if not compact:
            lines.append(f"Время раскладки: {self.elapsed_sec:.2f} с")
        return "\n".join(lines)

    @property
    def is_site_label_present(self) -> bool:
        return self.house_labeled > 0

    def _radius_hint(self) -> str:
        return f"{float(self.point_code_radius_ft):g} ft"


@dataclass
class LabelingResult:
    """Результат шага 2: план подписей, отчёт и предупреждения записи."""

    plans: list[LabelPlan]
    stats: LabelingStats
    features: list[CadFeature]
    site_polygon_pts: list[tuple[float, float]]
    warnings: list[str] = field(default_factory=list)
    written: int = 0
    source_of_site: str = ""

    def summary(self) -> str:
        return self.stats.format_report()

    def label_texts(self) -> list[str]:
        return [plan.text for plan in self.plans]


# ---------------------------------------------------------------------------
# Чтение пространства модели: снимок геометрии и текстов
# ---------------------------------------------------------------------------


@dataclass
class CadEntitySnapshot:
    """Сырой объект ModelSpace, уже превращённый в точки/текст."""

    kind: str                                  # line | polyline | point | text | solid
    layer: str
    handle: str
    vertices: list[tuple[float, float]] = field(default_factory=list)
    closed: bool = False
    curves: list[tuple[float | None, float | None]] = field(default_factory=list)
    bulges: list[float] = field(default_factory=list)
    text: str = ""
    point: tuple[float, float] = (0.0, 0.0)
    height: float = 0.0
    rotation: float = 0.0
    entity_type: str = ""


class AcadDrawing:
    """
    Тонкая обёртка над ActiveX-объектами AutoCAD (ModelSpace + Layers).

    Вся COM-логика собрана здесь: чтение примитивов, снимок текстов, запись
    `MText`/`Leader` в итоговый слой. Раскладка подписей выполняется в
    `FeatureLabeler` на чистой геометрии, поэтому её можно тестировать
    без установленного AutoCAD (см. `FakeCadDrawing`).
    """

    def __init__(self, builder: Any | None = None) -> None:
        self.builder = builder
        self._app: Any | None = None
        self._doc: Any | None = None
        self._ms: Any | None = None
        self._owned: bool = False

    # -- подключение -------------------------------------------------------
    def bind(self, doc: Any, app: Any | None = None) -> "AcadDrawing":
        """Использовать уже внедрённый документ (тесты, повторная запись)."""
        self._doc = doc
        self._app = app
        self._ms = None
        self._owned = False
        return self

    def connect(self) -> tuple[Any, Any, Any]:
        """Подключается к запущенному AutoCAD; возвращает (app, doc, modelspace)."""
        cur_tid = None
        try:
            import threading

            cur_tid = threading.get_ident()
        except Exception:  # noqa: BLE001
            cur_tid = None
        if self._doc is not None and self._ms is not None:
            return self._app, self._doc, self._ms

        if self.builder is not None:
            builder = self.builder
            if getattr(builder, "_doc", None) is not None and getattr(builder, "_app", None) is not None:
                app, doc = builder._app, builder._doc
            else:
                app, doc = builder.connect()
            self._app, self._doc = app, doc
            try:
                self._ms = doc.ModelSpace
            except Exception as exc:  # noqa: BLE001
                raise FeatureLabelerError(f"не удалось получить ModelSpace: {exc}") from exc
            return self._app, self._doc, self._ms

        try:
            import win32com.client  # noqa: F401
        except ImportError as exc:  # pragma: no cover - Windows only
            raise FeatureLabelerError(
                "pywin32 не установлен. В Windows выполните: pip install pywin32"
            ) from exc
        try:
            from acad_builder import _co_initialize

            self._owned = bool(_co_initialize())
        except Exception:  # noqa: BLE001
            self._owned = False
        try:
            self._app = win32com.client.GetActiveObject(ACAD_PROGID)
        except Exception as exc:  # noqa: BLE001
            raise FeatureLabelerError(
                "Не удалось подключиться к запущенному AutoCAD.\n"
                "1) Откройте AutoCAD и нужный DWG (не стартовый экран);\n"
                "2) Дождитесь полной загрузки чертежа;\n"
                f"3) Повторите попытку. Детали: {exc}"
            ) from exc
        try:
            self._doc = self._app.ActiveDocument
            self._ms = self._doc.ModelSpace
        except Exception as exc:  # noqa: BLE001
            raise FeatureLabelerError(
                f"AutoCAD запущен, но активный документ/ModelSpace недоступен: {exc}"
            ) from exc
        return self._app, self._doc, self._ms

    def release(self) -> None:
        if self._owned:
            try:
                from acad_builder import _co_uninitialize

                _co_uninitialize()
            except Exception:  # noqa: BLE001
                pass
        self._owned = False
        self._app = None
        self._doc = None
        self._ms = None

    # -- вспомогательные ---------------------------------------------------
    @property
    def model_space(self) -> Any:
        if self._ms is None:
            _, _, self._ms = self.connect()
        return self._ms

    @property
    def document(self) -> Any:
        if self._doc is None:
            _, self._doc, _ = self.connect()
        return self._doc

    @staticmethod
    def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
        try:
            return getattr(obj, name)
        except Exception:  # noqa: BLE001
            return default

    @classmethod
    def _coords_to_pairs(cls, coords: Any) -> list[tuple[float, float]]:
        """VARIANT-массив `(x, y, …)` → список точек; поддержка 2D/3D и вложенных."""
        try:
            values = list(coords)
        except TypeError:
            return []
        pairs: list[tuple[float, float]] = []
        try:
            if values and isinstance(values[0], (list, tuple)):
                for item in values:
                    if len(item) >= 2:
                        pairs.append((float(item[0]), float(item[1])))
                return pairs
            for idx in range(0, len(values) - 1, 2):
                pairs.append((float(values[idx]), float(values[idx + 1])))
        except (TypeError, ValueError, IndexError):
            return []
        return pairs

    @staticmethod
    def _point_xy(point: Any) -> tuple[float, float] | None:
        if point is None:
            return None
        for attrs in (("x", "y"), ("X", "Y")):
            try:
                return (float(getattr(point, attrs[0])), float(getattr(point, attrs[1])))
            except Exception:  # noqa: BLE001
                continue
        try:
            values = list(point)
            if len(values) >= 2:
                return (float(values[0]), float(values[1]))
        except Exception:  # noqa: BLE001
            return None
        return None

    def iterate_model_space(self) -> list[Any]:
        """Все объекты ModelSpace (список, чтобы переживать блокировки COM)."""
        ms = self.model_space
        entities: list[Any] = []
        try:
            count = int(self._safe_attr(ms, "Count", 0) or 0)
        except Exception:  # noqa: BLE001
            count = 0
        if count:
            for idx in range(count):
                try:
                    entities.append(ms.Item(idx))
                except Exception:  # noqa: BLE001
                    continue
            return entities
        try:  # pragma: no cover - итератор ActiveX редко доступен
            return [entity for entity in ms]
        except Exception:  # noqa: BLE001
            return []

    # -- снимок геометрии --------------------------------------------------
    def snapshot_entities(
        self,
        *,
        max_depth: int = 2,
        explode_blocks: bool = True,
    ) -> list[CadEntitySnapshot]:
        """
        Читает ModelSpace в плоский список простых объектов.

        Вложенные `INSERT` разворачиваются через `Explode` (результат считается
        одноразовым и удаляется), поэтому чертежи с блоками полевого съёмщика
        читаются так же, как «разобранные» объекты.
        """
        snapshots: list[CadEntitySnapshot] = []
        for entity in self.iterate_model_space():
            snapshots.extend(
                self._snapshot_entity(entity, depth=0, max_depth=max_depth, explode=explode_blocks)
            )
        return snapshots

    def _snapshot_entity(
        self,
        entity: Any,
        *,
        depth: int,
        max_depth: int,
        explode: bool,
    ) -> list[CadEntitySnapshot]:
        try:
            entity_type = str(self._safe_attr(entity, "EntityName", "") or "")
        except Exception:  # noqa: BLE001
            entity_type = ""
        layer = str(self._safe_attr(entity, "Layer", "") or "")
        handle = str(self._safe_attr(entity, "Handle", "") or "")
        name = entity_type.upper()

        if name in {"ACDBLINE", "LINE"}:
            start = self._point_xy(self._safe_attr(entity, "StartPoint"))
            end = self._point_xy(self._safe_attr(entity, "EndPoint"))
            if start and end:
                return [
                    CadEntitySnapshot(
                        kind="line",
                        layer=layer,
                        handle=handle,
                        vertices=[start, end],
                        entity_type=name,
                    )
                ]
            return []

        if "POLYLINE" in name:
            closed = bool(self._safe_attr(entity, "Closed", False))
            vertices: list[tuple[float, float]] = []
            bulges: list[float] = []
            if name == "LWPOLYLINE":
                vertices = self._coords_to_pairs(self._safe_attr(entity, "Coordinates"))
                bulges = self._read_bulges(entity, len(vertices))
            else:
                # ActiveX у POLYLINE/3D-POLYLINE отдаёт 2D-«Coordinates» либо
                # плоский массив (x, y, z) через `Coordinates` старой версии.
                vertices = self._coords_to_pairs(self._safe_attr(entity, "Coordinates"))
                if not vertices:
                    vertices = self._coords_to_pairs(self._safe_attr(entity, "Coordinates2D"))
                if not vertices:
                    vertices = self._vertices_from_objects(entity)
                bulges = self._read_bulges(entity, len(vertices))
                if not bulges:
                    bulges = self._bulges_from_vertices(entity, len(vertices))
            if len(vertices) < 2:
                return []
            return [
                CadEntitySnapshot(
                    kind="polyline",
                    layer=layer,
                    handle=handle,
                    vertices=vertices,
                    closed=closed,
                    bulges=bulges,
                    entity_type=name,
                )
            ]

        if name in {"ACDBTEXT", "TEXT", "ACDBMTEXT", "MTEXT"}:
            text = str(self._safe_attr(entity, "TextString", "") or "")
            if name in {"ACDBMTEXT", "MTEXT"}:
                text = strip_mtext_formatting(text)
            point = (
                self._point_xy(self._safe_attr(entity, "InsertionPoint"))
                or self._point_xy(self._safe_attr(entity, "TextAlignmentPoint"))
                or (0.0, 0.0)
            )
            return [
                CadEntitySnapshot(
                    kind="text",
                    layer=layer,
                    handle=handle,
                    text=text,
                    point=point,
                    height=float(self._safe_attr(entity, "Height", 0.0) or 0.0),
                    rotation=float(self._safe_attr(entity, "Rotation", 0.0) or 0.0),
                    entity_type=name,
                )
            ]

        if name in {"ACDBPOINT", "POINT"}:
            point = self._point_xy(self._safe_attr(entity, "Coordinates"))
            if point is None:
                return []
            return [
                CadEntitySnapshot(
                    kind="point",
                    layer=layer,
                    handle=handle,
                    vertices=[point],
                    entity_type=name,
                )
            ]

        if name in {"ACDBBLOCKREFERENCE", "INSERT", "ACDBBLOCKREF"} and explode and depth < max_depth:
            return self._explode_block(entity, depth=depth, max_depth=max_depth)

        # Остальные объекты (SOLID, HATCH, …) для подписей не используются.
        return []

    def _read_bulges(self, entity: Any, count: int) -> list[float]:
        bulges: list[float] = []
        getter = self._safe_attr(entity, "GetBulge")
        if not callable(getter):
            return bulges
        for idx in range(count):
            try:
                bulges.append(float(getter(idx)))
            except Exception:  # noqa: BLE001
                bulges.append(0.0)
        return bulges

    def _read_bulges_from_polyline(self, entity: Any, count: int) -> list[float]:
        """У тяжёлой POLYLINE бугели доступны через `GetBulge` (если есть)."""
        return self._read_bulges(entity, count)

    def _vertices_from_objects(self, entity: Any) -> list[tuple[float, float]]:
        """POLYLINE → точки из вложенной коллекции `Vertex` (`Coordinates`)."""
        points: list[tuple[float, float]] = []
        try:
            vertices = self._safe_attr(entity, "Coordinates")
            if vertices is not None:
                return self._coords_to_pairs(vertices)
        except Exception:  # noqa: BLE001
            pass
        for getter in ("GetVertex", "Item"):
            method = self._safe_attr(entity, getter)
            if not callable(method):
                continue
            try:
                count = int(self._safe_attr(entity, "NumberOfVertices", 0) or 0)
            except Exception:  # noqa: BLE001
                count = 0
            for idx in range(count):
                try:
                    vertex = method(idx)
                    point = self._point_xy(self._safe_attr(vertex, "Coordinates"))
                    if point is not None:
                        points.append(point)
                except Exception:  # noqa: BLE001
                    continue
            if points:
                return points
        return points

    def _bulges_from_vertices(self, entity: Any, count: int) -> list[float]:
        """Бугели тяжёлой POLYLINE: `Bulge` у каждого вложенного `Vertex`."""
        bulges: list[float] = []
        for getter in ("GetVertex", "Item"):
            method = self._safe_attr(entity, getter)
            if not callable(method):
                continue
            collected: list[float] = []
            for idx in range(count):
                try:
                    vertex = method(idx)
                    collected.append(float(self._safe_attr(vertex, "Bulge", 0.0) or 0.0))
                except Exception:  # noqa: BLE001
                    collected.append(0.0)
            if any(abs(value) > 1e-9 for value in collected):
                return collected
            bulges = collected
        return bulges


    def _explode_block(
        self, entity: Any, *, depth: int, max_depth: int
    ) -> list[CadEntitySnapshot]:
        exploded: Any = None
        try:
            exploded = entity.Explode()
        except Exception:  # noqa: BLE001
            return []
        result: list[CadEntitySnapshot] = []
        try:
            for child in exploded:
                result.extend(
                    self._snapshot_entity(child, depth=depth + 1, max_depth=max_depth, explode=True)
                )
        finally:
            try:
                for child in reversed(list(exploded)):
                    try:
                        child.Delete()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
        return result

    # -- тексты чертежа ----------------------------------------------------
    def read_existing_texts(self, layers: Sequence[str]) -> list[CadText]:
        """Все тексты из перечисленных слоёв (для дедупликации подписей)."""
        wanted = [normalize_layer_name(name) for name in layers if str(name).strip()]
        found: list[CadText] = []
        if not wanted:
            return found
        if self._ms is not None or self._doc is not None or self.builder is not None:
            for snap in self.snapshot_entities(explode_blocks=False):
                if snap.kind != "text":
                    continue
                if normalize_layer_name(snap.layer) in wanted:
                    found.append(
                        CadText(
                            text=snap.text,
                            point=snap.point,
                            layer=snap.layer,
                            height=snap.height,
                            rotation=snap.rotation,
                        )
                    )
            return found
        return found

    # -- запись ------------------------------------------------------------
    def ensure_layer(self, name: str, color: int = 7) -> bool:
        """Создаёт слой, если его нет; True — слой доступен."""
        doc = self.document
        try:
            doc.Layers.Item(name)
            return True
        except Exception:  # noqa: BLE001
            pass
        try:
            layer = doc.Layers.Add(name)
            try:
                layer.color = int(color)
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

    def add_mtext(
        self,
        text: str,
        point: tuple[float, float],
        height: float,
        width: float,
        layer: str,
        rotation: float = 0.0,
        attachment: int = 5,
    ) -> Any:
        """`AddMText` с привязкой MiddleCenter (attachmentPoint = 5)."""
        ms = self.model_space
        try:
            from acad_builder import _variant_point

            pt = _variant_point(point)
        except Exception:  # noqa: BLE001
            pt = (float(point[0]), float(point[1]), 0.0)
        mtext = ms.AddMText(pt, float(max(width, height)), str(text))
        try:
            mtext.Height = float(height)
        except Exception:  # noqa: BLE001
            pass
        try:
            mtext.Layer = layer
        except Exception:  # noqa: BLE001
            pass
        try:
            mtext.Rotation = float(rotation)
        except Exception:  # noqa: BLE001
            pass
        try:
            mtext.AttachmentPoint = int(attachment)
        except Exception:  # noqa: BLE001
            pass
        try:  # фиксируем точку вставки после смены привязки
            mtext.InsertionPoint = pt
        except Exception:  # noqa: BLE001
            pass
        return mtext

    def add_dtext(
        self,
        text: str,
        point: tuple[float, float],
        height: float,
        layer: str,
        rotation: float = 0.0,
        style: str | None = None,
    ) -> Any:
        """
        `AddText` (DBTEXT) с выравниванием MiddleCenter и поворотом вдоль линии.

        Параллельная подпись забора делается именно DBTEXT: так её проще
        растянуть/подвинуть вручную в AutoCAD, не заходя в редактор MTEXT.
        """
        ms = self.model_space
        try:
            from acad_builder import _variant_point

            pt = _variant_point(point)
        except Exception:  # noqa: BLE001
            pt = (float(point[0]), float(point[1]), 0.0)
        dtext = ms.AddText(str(text), pt, float(height))
        try:
            if style:
                dtext.StyleName = style
        except Exception:  # noqa: BLE001
            pass
        try:
            dtext.Layer = layer
        except Exception:  # noqa: BLE001
            pass
        try:
            dtext.Alignment = 10  # acAlignmentMiddleCenter
            dtext.TextAlignmentPoint = pt
        except Exception:  # noqa: BLE001
            pass
        try:
            dtext.Rotation = float(rotation)
        except Exception:  # noqa: BLE001
            pass
        return dtext

    def add_leader(
        self,
        points: Sequence[tuple[float, float]],
        layer: str,
        *,
        arrow_head_size: float = 0.0,
        mtext: Any | None = None,
        text_width: float = 0.0,
        rotation: float = 0.0,
        attachment: int = 8,
    ) -> tuple[Any | None, Any | None]:
        """
        `AddLeader` (стрелка у объекта) + `AddMText` на свободном месте.

        Если ActiveX не дал `AddLeader`, возвращает (None, mtext) — текст всё
        равно ставится, а вызывающая сторона добавляет предупреждение.
        """
        leader = None
        try:
            from acad_builder import _variant_points

            pts = _variant_points(points)
        except Exception:  # noqa: BLE001
            pts = [coord for pt in points for coord in (float(pt[0]), float(pt[1]), 0.0)]
        ms = self.model_space
        try:
            leader = ms.AddLeader(pts)
            try:
                leader.Layer = layer
            except Exception:  # noqa: BLE001
                pass
            if arrow_head_size > 0:
                try:
                    leader.ArrowHeadSize = float(arrow_head_size)
                except Exception:  # noqa: BLE001
                    pass
            if mtext is not None:
                try:
                    leader.Annotation = mtext
                    try:
                        leader.AnnotationType = 1
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            leader = None
        return leader, mtext

    def zoom_window(self, corner1: tuple[float, float], corner2: tuple[float, float]) -> bool:
        """`ZoomWindow` на область подписей (не мешает работе пользователя)."""
        app = self._app
        if app is None:
            return False
        try:
            from acad_builder import _variant_point

            app.ZoomWindow(_variant_point(corner1), _variant_point(corner2))
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- полилиния пользователя -------------------------------------------
    def prompt_polyline(
        self,
        prompt: str = "Укажите замкнутую полилинию границы участка: ",
    ) -> Any | None:
        """
        Просит пользователя выбрать `LWPOLYLINE`/`POLYLINE` (или отрезки контура).

        Возвращает ActiveX-объект или None, если выбор отменён. Вызывается ТОЛЬКО
        из главного потока Tkinter (AutoCAD уводит фокус в своё окно).
        """
        doc = self.document
        try:
            utility = doc.Utility
        except Exception as exc:  # noqa: BLE001
            raise FeatureLabelerError(f"недоступен объект Utility AutoCAD: {exc}") from exc
        try:
            picked = utility.GetEntity(f"\n{prompt}", "\n")
        except Exception:  # noqa: BLE001
            return None
        if isinstance(picked, (tuple, list)):
            if not picked:
                return None
            candidate = picked[0]
        else:
            candidate = picked
        if candidate is None:
            return None
        return candidate


def strip_mtext_formatting(raw: str) -> str:
    """
    Убирает управляющие коды MTEXT/DBTEXT: `{\\fArial|b0;Текст}` → `Текст`.

    Нужно, чтобы сравнение существующих надписей с новыми не зависело от
    форматирования и кодировки градусов (`%%d`, `\\U+00B0`).
    """
    text = str(raw or "")
    if not text:
        return ""
    text = re.sub(r"\\[A-Za-z][^;{}]*;", "", text)          # \fArial|b0;  \H2.5x;
    text = re.sub(r"\\P", " ", text)
    text = re.sub(r"\\~", " ", text)
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), text)
    text = text.replace("%%d", "°").replace("%%D", "°").replace("%%p", "±")
    text = text.replace("%%c", "Ø").replace("%%P", "±")
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Геометрические хелперы
# ---------------------------------------------------------------------------


def _clean_points(
    points: Iterable[tuple[float, float]], *, close_tol: float = 0.05
) -> list[tuple[float, float]]:
    """Убирает подряд идущие дубли и замыкающую точку (полилиния «почти замкнута»)."""
    result: list[tuple[float, float]] = []
    for pt in points:
        try:
            x, y = float(pt[0]), float(pt[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        if result and abs(result[-1][0] - x) < 1e-9 and abs(result[-1][1] - y) < 1e-9:
            continue
        result.append((x, y))
    if len(result) >= 3:
        first, last = result[0], result[-1]
        if math.hypot(first[0] - last[0], first[1] - last[1]) <= max(close_tol, 1e-9):
            result.pop()
    return result


def points_to_polygon(
    points: Sequence[tuple[float, float]],
) -> Any | None:
    """Контур из точек → Polygon / MultiPolygon (None, если площадь нулевая)."""
    ensure_shapely()
    pts = _clean_points(points)
    if len(pts) < 3:
        return None
    try:
        poly = Polygon(pts)
    except Exception:  # noqa: BLE001
        return None
    if not poly.is_valid:
        poly = poly.buffer(0.0)
    if poly.is_empty or float(poly.area) < 0.01:
        return None
    return poly


def densify_bulge_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    bulge: float,
    *,
    min_arc_ft: float = 6.0,
    step_deg: float = 26.0,
) -> list[tuple[float, float]]:
    """
    Заменяет дуговой сегмент полилинии (bulge ≠ 0) ломаной.

    Длинные дуги (≥ ``min_arc_ft``) остаются отдельными «дуговыми» линиями с
    ``curved=True``: они подписываются выноской, а не вдоль хорды — уличная
    линия забора изогнута, и текст вдоль хорды выглядел бы неправильно.
    """
    if abs(bulge) < 1e-9:
        return [start, end]
    sx, sy = start
    ex, ey = end
    chord = math.hypot(ex - sx, ey - sy)
    if chord < 1e-9:
        return [start, end]
    theta = 4.0 * math.atan(abs(bulge))
    radius = chord / (2.0 * math.sin(theta / 2.0))
    arc_len = abs(radius * theta)
    if arc_len < min_arc_ft:
        return [start, end]
    # центр дуги
    mx, my = 0.5 * (sx + ex), 0.5 * (sy + ey)
    dx, dy = ex - sx, ey - sy
    h = math.sqrt(max(radius * radius - (chord / 2.0) ** 2, 0.0))
    sign = 1.0 if bulge > 0 else -1.0
    nx, ny = -dy / chord, dx / chord
    cx, cy = mx + nx * h * sign, my + ny * h * sign
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    if bulge > 0:
        if a1 <= a0:
            a1 += 2.0 * math.pi
    else:
        if a1 >= a0:
            a1 -= 2.0 * math.pi
    steps = int(math.ceil(abs(a1 - a0) / math.radians(max(1.0, step_deg))))
    pts = [start]
    for idx in range(1, steps):
        angle = a0 + (a1 - a0) * idx / steps
        pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    pts.append(end)
    return pts


def flatten_polyline(
    vertices: Sequence[tuple[float, float]],
    bulges: Sequence[float] | None,
    *,
    closed: bool,
    min_arc_ft: float = 6.0,
    step_deg: float = 26.0,
) -> tuple[list[tuple[float, float]], bool]:
    """
    Строит «артефактную» геометрию полилинии.

    Возвращает (точки, есть_дуги). Линии забора из дуг нарезаются на отдельные
    хорды ≥ ``min_arc_ft`` в точке минимума деформации.
    """
    pts = list(vertices)
    has_curve = False
    if not pts:
        return ([], False)
    count = len(pts)
    result: list[tuple[float, float]] = [pts[0]]
    for idx in range(count if closed else count - 1):
        start = pts[idx]
        end = pts[(idx + 1) % count]
        bulge = 0.0
        if bulges and idx < len(bulges):
            bulge = float(bulges[idx] or 0.0)
        if abs(bulge) < 1e-9:
            result.append(end)
            continue
        arc_pts = densify_bulge_segment(
            start, end, bulge, min_arc_ft=min_arc_ft, step_deg=step_deg
        )
        if len(arc_pts) > 2:
            has_curve = True
        result.extend(arc_pts[1:])
    return (result, has_curve)


def _split_geometry_into_features(
    geom: Any,
) -> list[tuple[Any, bool]]:
    """
    geom → список (изолированный кусок, замкнут_ли).

    Lines / polygons разъединяются через `unary_union`; контуры, которые
    `polygonize` не собрал, остаются линиями (тогда подпись ставится выноской).
    """
    ensure_shapely()
    if geom is None or getattr(geom, "is_empty", True):
        return []
    merged = geom
    try:
        if not getattr(geom, "is_valid", True):
            merged = unary_union(geom)
        elif isinstance(geom, (MultiLineString, LineString)):
            # Смежные отрезки одного забора склеиваются в непрерывную линию
            # (LINEMERGE): 12 сегментов по 10 ft → один объект длиной 120 ft.
            merged = linemerge(geom)
        elif isinstance(geom, MultiPolygon):
            merged = unary_union(geom)
    except Exception:  # noqa: BLE001
        merged = geom
    parts: list[Any] = []
    if isinstance(merged, (Polygon,)):
        parts = [merged]
    elif isinstance(merged, (MultiPolygon,)):
        parts = list(merged.geoms)
    elif isinstance(merged, LineString):
        parts = [merged]
    elif isinstance(merged, MultiLineString):
        parts = list(merged.geoms)
    else:
        try:
            parts = [part for part in merged.geoms]
        except Exception:  # noqa: BLE001
            parts = [merged]

    result: list[tuple[Any, bool]] = []
    for part in parts:
        if part is None or getattr(part, "is_empty", True):
            continue
        if isinstance(part, Polygon):
            if float(part.area) >= 0.05:
                result.append((part, True))
            elif float(part.length) >= 0.3:
                result.append((part.exterior, False))
            continue
        if isinstance(part, (LineString,)):
            if float(part.length) < 0.3:
                continue
            if part.is_closed and float(part.length) > 0.0:
                poly = points_to_polygon(list(part.coords))
                if poly is not None:
                    result.append((poly, True))
                    continue
            result.append((part, False))
            continue
        if hasattr(part, "geoms"):
            result.extend(_split_geometry_into_features(part))
    return result


def text_box(
    text: str,
    center: tuple[float, float],
    height: float,
    *,
    rotation: float = 0.0,
    width_factor: float = 0.62,
    space_factor: float = 0.62,
    padding: float = 0.0,
) -> Any:
    """
    Ориентированный прямоугольник подписи (`shapely.geometry.box` + поворот).

    Длина считается по символам, как в `acad_builder.estimate_text_width`:
    строчные буквы узкие, пробел — ``space_factor``. Именно эти боксы
    проверяются на пересечение с линиями заборов, контурами и другими подписями.
    """
    ensure_shapely()
    txt = strip_mtext_formatting(text)
    lines = txt.split("\n") or [""]
    widest = 0.0
    for line in lines:
        width = 0.0
        for char in line:
            if char == " ":
                width += height * space_factor
            elif char in "il.,'`:;|!1":
                width += height * width_factor * 0.55
            elif char.isupper():
                width += height * width_factor * 1.12
            else:
                width += height * width_factor
        widest = max(widest, width)
    widest = max(widest, height * 0.6) + 2.0 * padding
    total_h = height * (len(lines) * 1.32) + 2.0 * padding
    cx, cy = center
    local = shapely_box(
        cx - widest / 2.0, cy - total_h / 2.0, cx + widest / 2.0, cy + total_h / 2.0
    )
    if abs(rotation) > 1e-6:
        from shapely import affinity

        local = affinity.rotate(local, math.degrees(rotation), origin=(cx, cy))
    return local


def read_text_rotation(rotation: float) -> float:
    """Защита от перевёрнутого текста: 90°..270° → +180° (mod 180°)."""
    angle = float(rotation) % math.pi
    if angle > math.pi / 2.0:
        angle -= math.pi
    return angle


def rotation_flexibility(a: float, b: float) -> float:
    """
    Насколько две надписи «одной ориентации»: 0° — идеально, 90° — хуже всего.

    Тексты вдоль перпендикулярных заборов не считаются аналогичными даже в
    радиусе дедупликации: это подписи разных стен.
    """
    diff = abs((float(a) - float(b)) % math.pi)
    if diff > math.pi / 2.0:
        diff = math.pi - diff
    return diff


def orientation_compatible(a: float, b: float, tolerance_deg: float = 10.0) -> bool:
    return math.degrees(rotation_flexibility(a, b)) <= float(tolerance_deg)


# ---------------------------------------------------------------------------
# Чтение объектов чертежа
# ---------------------------------------------------------------------------


def features_from_snapshots(
    snapshots: Sequence[CadEntitySnapshot],
    feature_cfg: dict[str, Any],
    *,
    tread_layers: Sequence[str] | None = None,
) -> tuple[list[CadFeature], list[CadText], LabelingStats]:
    """
    Снимок ModelSpace → распознанные объекты (`CadFeature`) и тексты.

    Геометрия одного слоя сначала объединяется (`unary_union`), затем
    изолируется в отдельные куски: забор из 12 смежных сегментов остаётся
    ОДНИМ объектом, два отдельных забора — двумя. Замкнутые контуры
    восстанавливаются через `polygonize`.
    """
    ensure_shapely()
    cfg = feature_cfg
    stats = LabelingStats()
    tread_patterns = list(tread_layers or [cfg.get("step_tread_pattern") or "*_STEP_TREADS"])

    layer_lines: dict[str, list[Any]] = {}
    layer_curved: dict[str, list[Any]] = {}
    tread_lines: list[Any] = []
    tread_polys: list[Any] = []
    texts: list[CadText] = []
    for snap in snapshots:
        if snap.kind in {"text", "point"}:
            if snap.kind == "text":
                texts.append(
                    CadText(
                        text=snap.text,
                        point=snap.point,
                        layer=snap.layer,
                        height=snap.height,
                        rotation=snap.rotation,
                    )
                )
            continue
        if snap.kind not in {"line", "polyline"} or len(snap.vertices) < 2:
            continue
        is_tread = matches_tread_layer(snap.layer, tread_patterns)
        pts, has_curve = flatten_polyline(
            snap.vertices,
            snap.bulges,
            closed=bool(snap.closed),
            min_arc_ft=float(cfg.get("curved_segment_min_ft", 6.0)),
            step_deg=float(cfg.get("curved_segment_step_deg", 26.0)),
        )
        if snap.kind == "polyline" and snap.closed and not has_curve:
            pts = _clean_points(pts)
        if len(pts) < 2:
            continue
        try:
            coords = {"kind": "poly", "closed": bool(snap.closed) and not has_curve}
            geom = LineString(pts + ([pts[0]] if coords["closed"] else []))
        except Exception:  # noqa: BLE001
            continue
        if is_tread:
            tread_lines.append(geom)
            if len(pts) >= 3:
                tread_poly = points_to_polygon(pts)
                if tread_poly is not None:
                    tread_polys.append(tread_poly)
            continue
        stats.features_layer_total += 1
        layer_lines.setdefault(snap.layer, []).append(geom)
        if has_curve:
            layer_curved.setdefault(snap.layer, []).append(geom)

    features: list[CadFeature] = []
    for layer, geoms in layer_lines.items():
        if is_note_text_layer(layer):
            continue
        category, layer_label = classify_layer(layer, cfg)
        try:
            union = unary_union(geoms)
        except Exception:  # noqa: BLE001
            union = geoms[0]
        curved_geoms = layer_curved.get(layer) or []
        for geom, closed in _split_geometry_into_features(union):
            wrapped_curved = any(
                _geometry_touches(geom, curved) for curved in curved_geoms
            )
            if wrapped_curved and not closed:
                # Дуговой участок забора/бордюра — подписывается выноской.
                pass
            feature = CadFeature(
                category=category,
                layer=layer,
                entity_type="POLYLINE" if closed else "LINE",
                geom=geom,
                label=layer_label,
                source="layer" if layer_label else "",
                closed=bool(closed),
                is_house=(category == "building"),
                curved=bool(wrapped_curved and not closed),
            )
            feature.label = feature.label or layer_label
            if category == "steps" or layer_matches_pattern(layer, "ST-STEP") \
                    or matches_tread_layer(layer, [str(p) for p in tread_patterns]):
                count = count_treads(geom, tread_lines, tread_polys)
                feature.treads = count
                if count > 0:
                    feature.label = f"{count} STEPS"
                    feature.notes.append(f"подсчитано линий *_STEP_TREADS: {count}")
            if not feature.label and not is_note_text_layer(layer):
                # Словарь layer_label_mapping мог не знать точное имя слоя
                # (FENCE_ARC_*), но префикс даёт категорию — подпись по префиксу.
                feature.label = fence_prefix_label(layer, cfg)
            if category == "building":
                # Описание дома задаётся пользователем в GUI (не словарём слоёв).
                feature.label = None
            features.append(feature)
    stats.features_geometry_total = len(features)
    return features, texts, stats


def _geometry_touches(geom: Any, other: Any, tol: float = 0.35) -> bool:
    try:
        return bool(geom.intersects(other.buffer(tol)) or geom.distance(other) <= tol)
    except Exception:  # noqa: BLE001
        return False


def count_treads(
    geom: Any,
    tread_lines: Sequence[Any],
    tread_polys: Sequence[Any] = (),
) -> int:
    """
    Считает линии `*_STEP_TREADS` внутри/на границе контура ступенек.

    Работает и для замкнутого контура ступенек (проточки — короткие отрезки
    внутри контура), и для незамкнутой цепочки: тогда линия ступеньки ищется
    в пределах 0.5 ft от проточки.
    """
    if not tread_lines:
        return 0
    ensure_shapely()
    count = 0
    try:
        probe = geom if isinstance(geom, Polygon) else geom.buffer(0.5)
        for line in tread_lines:
            mid = line.interpolate(0.5, normalized=True)
            if probe.contains(mid) or probe.distance(mid) <= 0.12:
                count += 1
                continue
            if geom.distance(line) <= 0.5:
                count += 1
    except Exception:  # noqa: BLE001
        return count
    if count == 0 and tread_polys:
        # Проточки — миниатюрные прямоугольники внутри общего контура ступенек.
        for tread_poly in tread_polys:
            try:
                if geom.distance(tread_poly) <= 0.75:
                    count += 1
            except Exception:  # noqa: BLE001
                continue
    return count


# ---------------------------------------------------------------------------
# Слой надписей и защита от дублей
# ---------------------------------------------------------------------------


class TextRegistry:
    """Существующие надписи + уже поставленные подписи (дедупликация и анти-наложение)."""

    def __init__(
        self,
        existing: Sequence[CadText],
        *,
        dedupe_radius_ft: float = 6.0,
        angle_tolerance_deg: float = 10.0,
        filled_fence_dedupe_ft: float = 1.5,
    ) -> None:
        self.dedupe_radius_ft = float(dedupe_radius_ft)
        self.angle_tolerance_deg = float(angle_tolerance_deg)
        self.filled_fence_dedupe_ft = float(filled_fence_dedupe_ft)
        self.existing: list[CadText] = list(existing)
        self.created: list[LabelPlan] = []

    # -- проверка дублей ---------------------------------------------------
    def has_existing(
        self,
        label: str,
        anchor_points: Sequence[tuple[float, float]],
        rotation: float = 0.0,
    ) -> CadText | None:
        """
        Ищет аналогичную надпись в радиусе дедупликации от объекта.

        Для линий забора anchor_points — множество точек вдоль линии: надпись
        «CHAIN LINK FENCE» над забором в 5.9 ft не должна ставиться повторно.
        """
        if not label:
            return None
        for item in self.existing:
            if not labels_equivalent(item.text, label):
                continue
            if not orientation_compatible(item.rotation, rotation, self.angle_tolerance_deg):
                continue
            for point in anchor_points:
                if math.hypot(item.point[0] - point[0], item.point[1] - point[1]) <= self.dedupe_radius_ft:
                    return item
        return None

    def has_created(
        self,
        label: str,
        anchor_points: Sequence[tuple[float, float]],
        rotation: float = 0.0,
        *,
        ignore_rotation: bool = False,
        own_anchors: Sequence[tuple[float, float]] | None = None,
    ) -> LabelPlan | None:
        """
        Уже поставлена ли такая надпись рядом?

        Два сарая в 10 ft друг от друга — два разных объекта: их подписи
        «SHED» легальны. Повтор не создаётся только когда подпись стоит
        в радиусе дедупликации от ТОГО ЖЕ объекта (``own_anchors``).
        """
        own = list(own_anchors or [])
        for plan in self.created:
            if not labels_equivalent(plan.text, label):
                continue
            if not ignore_rotation and not orientation_compatible(
                plan.rotation, rotation, self.angle_tolerance_deg
            ):
                continue
            if own and plan.anchor_points:
                for anchor in own:
                    for saved in plan.anchor_points:
                        if math.hypot(anchor[0] - saved[0], anchor[1] - saved[1]) <= self.dedupe_radius_ft:
                            return plan
                continue
            candidates = [plan.point]
            if plan.leader_text_point is not None:
                candidates.append(plan.leader_text_point)
            for point in candidates:
                for anchor in anchor_points:
                    if math.hypot(point[0] - anchor[0], point[1] - anchor[1]) <= self.dedupe_radius_ft:
                        return plan
        return None

    def register(self, plan: LabelPlan) -> None:
        self.created.append(plan)

    # -- анти-наложение ----------------------------------------------------
    def overlaps_created(self, candidate: Any, clearance: float = 0.0) -> bool:
        probe = candidate.buffer(float(clearance)) if clearance > 0 else candidate
        for plan in self.created:
            if plan.box is None:
                continue
            try:
                if probe.intersects(plan.box):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def overlaps_existing(self, candidate: Any, *, clearance: float = 0.0) -> bool:
        probe = candidate.buffer(float(clearance)) if clearance > 0 else candidate
        for item in self.existing:
            h = item.height or 0.14
            text = strip_mtext_formatting(item.text)
            if not text:
                continue
            box = text_box(
                text, item.point, max(h, 0.05), rotation=read_text_rotation(item.rotation)
            )
            try:
                if probe.intersects(box):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def distance_to_existing(self, point: tuple[float, float]) -> float:
        best = float("inf")
        for item in self.existing:
            best = min(best, math.hypot(item.point[0] - point[0], item.point[1] - point[1]))
        return best


# ---------------------------------------------------------------------------
# Раскладка подписей (чистая математика, без COM)
# ---------------------------------------------------------------------------


@dataclass
class _Obstacles:
    """Геометрия, которую новые подписи не должны перекрывать."""

    lines: list[Any] = field(default_factory=list)
    outlines: list[Any] = field(default_factory=list)
    tree: Any | None = None

    @classmethod
    def build(
        cls,
        features: Sequence[CadFeature],
        extra: Sequence[Any] = (),
        *,
        site_polygon: Any | None = None,
    ) -> "_Obstacles":
        lines: list[Any] = []
        outlines: list[Any] = []
        for feature in features:
            geom = feature.geom
            if geom is None:
                continue
            if feature.is_linear or feature.category == "unknown":
                # Межа участка и служебные линии — линии, а не контуры строений.
                lines.append(geom)
            else:
                outlines.append(geom)
        lines.extend(extra)
        if site_polygon is not None and isinstance(site_polygon, Polygon):
            # Межа участка тоже линия: подпись не должна её перечёркивать.
            lines.append(site_polygon.exterior)
        obstacles = cls(lines=lines, outlines=outlines)
        obstacles.rebuild()
        return obstacles

    def rebuild(self) -> None:
        all_geoms = list(self.lines) + list(self.outlines)
        if all_geoms and STRtree is not None:
            try:
                self.tree = STRtree(all_geoms)
                self._tree_geoms = all_geoms
                return
            except Exception:  # noqa: BLE001
                self.tree = None
        self._tree_geoms = all_geoms

    def conflicts(self, candidate: Any, *, allow_own: Any | None = None) -> list[Any]:
        """Пересечения с линиями/контурами (свой контур `allow_own` игнорируется)."""
        found: list[Any] = []
        allowed: list[Any] = []
        if allow_own is None:
            pass
        elif isinstance(allow_own, (list, tuple, set)):
            allowed = [item for item in allow_own if item is not None]
        else:
            allowed = [allow_own]
        def is_allowed(geom: Any) -> bool:
            for own in allowed:
                if geom is own:
                    return True
                try:
                    if bool(geom.equals(own)):
                        return True
                except Exception:  # noqa: BLE001
                    continue
            return False

        tree = getattr(self, "tree", None)
        geoms = getattr(self, "_tree_geoms", [])
        if tree is not None:
            try:
                for hit in tree.query(candidate, predicate="intersects"):
                    geom = geoms[int(hit)]
                    if is_allowed(geom):
                        continue
                    found.append(geom)
            except Exception:  # noqa: BLE001
                pass
            return found
        for geom in geoms:
            if is_allowed(geom):
                continue
            try:
                if candidate.intersects(geom):
                    found.append(geom)
            except Exception:  # noqa: BLE001
                continue
        return found


def _line_pieces(geom: Any) -> list[Any]:
    """Геометрия → список простых LineString (для параллельных подписей)."""
    pieces: list[Any] = []
    if isinstance(geom, LineString):
        pieces.append(geom)
    elif isinstance(geom, MultiLineString):
        pieces.extend(list(geom.geoms))
    elif isinstance(geom, Polygon):
        pieces.append(geom.exterior)
    elif isinstance(geom, MultiPolygon):
        for part in geom.geoms:
            pieces.append(part.exterior)
    else:
        try:
            for part in geom.geoms:
                pieces.extend(_line_pieces(part))
        except Exception:  # noqa: BLE001
            pass
    return [piece for piece in pieces if float(piece.length) >= 0.3]


def _segment_angles(geom: Any) -> list[float]:
    angles: list[float] = []
    for piece in _line_pieces(geom):
        coords = list(piece.coords)
        for idx in range(len(coords) - 1):
            x1, y1 = coords[idx]
            x2, y2 = coords[idx + 1]
            if math.hypot(x2 - x1, y2 - y1) < 1e-6:
                continue
            angles.append(math.atan2(y2 - y1, x2 - x1))
    return angles


def dominant_orientation(geom: Any) -> float:
    """
    Доминирующее направление набора линий (взвешенно по длине, mod 180°).

    Забор из 12 сегментов под углом 12° получает угол 12°, а не 90°: подпись
    ставится параллельно фактической линии, а не по одной случайной хорде.
    """
    buckets: dict[int, float] = {}
    for piece in _line_pieces(geom):
        coords = list(piece.coords)
        for idx in range(len(coords) - 1):
            x1, y1 = coords[idx]
            x2, y2 = coords[idx + 1]
            length = math.hypot(x2 - x1, y2 - y1)
            if length < 1e-6:
                continue
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
            bucket = int(round(angle)) % 180
            buckets[bucket] = buckets.get(bucket, 0.0) + length
    if not buckets:
        return 0.0
    best = max(buckets.items(), key=lambda kv: kv[1])[0]
    return math.radians(float(best))


def text_fits_along(
    piece_length: float, text_width: float, extra_ft: float = 4.0
) -> bool:
    """Прямой участок забора длиннее `ширины_текста + 4 ft` — текст встаёт вдоль."""
    return float(piece_length) >= float(text_width) + float(extra_ft)


def _best_straight_piece(geom: Any, min_length: float) -> Any | None:
    """Самая длинная прямая (без заметных изломов) часть линии."""
    best: Any | None = None
    best_len = 0.0
    for piece in _line_pieces(geom):
        coords = list(piece.coords)
        if len(coords) < 2:
            continue
        run = [coords[0]]
        prev_angle: float | None = None
        runs: list[list[tuple[float, float]]] = []
        for idx in range(len(coords) - 1):
            x1, y1 = coords[idx]
            x2, y2 = coords[idx + 1]
            angle = math.atan2(y2 - y1, x2 - x1)
            if prev_angle is not None:
                delta = abs((angle - prev_angle + math.pi / 2.0) % math.pi - math.pi / 2.0)
                if math.degrees(delta) > 4.0:
                    runs.append(run)
                    run = [coords[idx]]
            run.append(coords[idx + 1])
            prev_angle = angle
        runs.append(run)
        for item in runs:
            if len(item) < 2:
                continue
            length = LineString(item).length
            if length > best_len:
                best_len = length
                best = LineString(item)
    if best is None or best_len < min_length:
        return None
    return best


def _safe_interior_point(geom: Any) -> tuple[float, float]:
    """Точка внутри полигона с запасом от границ (representative_point при узости)."""
    try:
        point = geom.representative_point()
        return (float(point.x), float(point.y))
    except Exception:  # noqa: BLE001
        c = geom.centroid
        return (float(c.x), float(c.y))


def _centroid(geom: Any) -> tuple[float, float]:
    try:
        c = geom.centroid
        return (float(c.x), float(c.y))
    except Exception:  # noqa: BLE001
        return (0.0, 0.0)


class FeatureLabeler:
    """
    Шаг 2: распознать объекты вокруг участка и разложить подписи.

    Чистая часть (`collect_features` + `build_plans`) не трогает AutoCAD,
    запись выполняет `apply` через `AcadDrawing`. Такой разрез позволяет
    прогнать весь сценарий в тестах на `FakeCadDrawing`.
    """

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = configure_feature_cfg(cfg)
        self.warnings: list[str] = []

    # -- свойства конфига --------------------------------------------------
    @property
    def output_layer(self) -> str:
        return str(self.cfg.get("label_output_layer") or "D-TOPO-TEXT")

    @property
    def text_height(self) -> float:
        try:
            return float(self.cfg.get("text_height") or 0.16)
        except (TypeError, ValueError):
            return 0.16

    # -- полигон участка ---------------------------------------------------
    def site_polygon(
        self,
        snapshots: Sequence[CadEntitySnapshot] | None = None,
        *,
        plan_vertices: Sequence[tuple[float, float]] | None = None,
        picked_vertices: Sequence[tuple[float, float]] | None = None,
    ) -> tuple[Any | None, str]:
        """
        Контур участка: указанная полилиния → план шага 1 → слои границы участка.

        Возвращает (полигон shapely, источник) — источник показывается в GUI:
        «указанная полилиния», «шаг 1», «слой PROPERTY_LINE».
        """
        ensure_shapely()
        candidates: list[tuple[list[tuple[float, float]], str]] = []
        if picked_vertices:
            candidates.append((list(picked_vertices), "указанная полилиния"))
        if plan_vertices:
            candidates.append((list(plan_vertices), "шаг 1 (граница участка)"))
        if snapshots:
            for layer in self.cfg.get("property_line_layers") or []:
                collected: list[tuple[float, float]] = []
                for snap in snapshots:
                    if snap.kind not in {"line", "polyline"}:
                        continue
                    if not layer_matches_pattern(snap.layer, str(layer)):
                        continue
                    collected.extend(snap.vertices)
                if not collected:
                    continue
                poly = self._polygon_from_candidate(collected)
                if poly is not None:
                    candidates.append((list(collected), f"слой {layer}"))
                    break
        for points, source in candidates:
            poly = self._polygon_from_candidate(points)
            if poly is not None:
                return poly, source
        return None, ""

    def _polygon_from_candidate(
        self, points: Sequence[tuple[float, float]]
    ) -> Any | None:
        """Замкнутый контур из набора вершин (порядок восстанавливается по углу)."""
        ensure_shapely()
        pts = _clean_points(points)
        if len(pts) < 3:
            return None
        poly = points_to_polygon(pts)
        if poly is not None and float(poly.area) >= 1.0:
            return poly
        # Вершины могли быть собраны из отдельных отрезков без порядка:
        # пробуем замыкание по полярному углу вокруг центра и по объединению линий.
        try:
            lines = [LineString([pts[idx], pts[(idx + 1) % len(pts)]]) for idx in range(len(pts))]
            merged = unary_union(lines)
            polys = list(polygonize(merged))
            if polys:
                biggest = max(polys, key=lambda p: float(p.area))
                if float(biggest.area) >= 1.0:
                    return biggest
        except Exception:  # noqa: BLE001
            pass
        convex_hull = Polygon(pts).convex_hull
        if float(convex_hull.area) >= 1.0:
            return convex_hull
        return None

    # -- сбор объектов -----------------------------------------------------
    def collect_features(
        self,
        snapshots: Sequence[CadEntitySnapshot],
        site_polygon: Any,
        *,
        stats: LabelingStats | None = None,
    ) -> tuple[list[CadFeature], list[CadText], LabelingStats]:
        """
        Все объекты в буфере вокруг участка + привязка кодов точек к заборам.

        Код точки (`'2 FEC'`) может стоять на отдельном слое, а не на линии
        забора: код привязывается к ближайшей линии забора в радиусе
        `point_code_radius_ft` и её подпись получает приоритет над именем слоя.
        """
        stats = stats or LabelingStats()
        features, texts, snap_stats = features_from_snapshots(
            snapshots, self.cfg, tread_layers=[self.cfg.get("step_tread_pattern") or "*_STEP_TREADS"]
        )
        stats.features_layer_total = snap_stats.features_layer_total
        stats.features_geometry_total = snap_stats.features_geometry_total
        stats.points_total = sum(
            1
            for snap in snapshots
            if snap.kind == "text" and self._is_point_label_layer(snap.layer)
        )
        buffer_ft = float(self.cfg.get("search_buffer_ft", 50.0))
        zone = site_polygon.buffer(buffer_ft)
        inside_features: list[CadFeature] = []
        for feature in features:
            if feature.is_empty_dimension:
                continue
            if feature.category == "unknown" and not feature.label:
                # Служебные/контекстные слои: не подписываются и не попадают в отчёт
                # (межа участка добавляется в препятствия отдельно).
                continue
            try:
                if not feature.geom.intersects(zone):
                    continue
            except Exception:  # noqa: BLE001
                continue
            inside_features.append(feature)
            if feature.geom.within(site_polygon) or site_polygon.contains(feature.geom.centroid):
                stats.inside_lot += 1
        stats.features_in_buffer = len(inside_features)

        # --- Источник 1: коды точек из POINT_LABELS / PNT-X / PNTS --------
        code_targets: dict[int, str] = {}
        code_text_map: dict[int, str] = {}
        for snap in snapshots:
            if snap.kind != "text":
                continue
            if not self._is_point_label_layer(snap.layer):
                continue
            label = label_for_point_code(snap.text, self.cfg.get("point_code_dictionary"))
            if label is None:
                continue
            stats.point_codes_total += 1
            target = self._nearest_fence(inside_features, snap.point, stats)
            if target is None:
                continue
            idx = id(target)
            if target.label and target.source == "point_code":
                continue
            if point_code_family(str(snap.text).strip()) != "fence":
                # Коды столбов/гидрантов/кессонов относятся к точечным объектам:
                # забор с «UTILITY POLE» был бы ошибкой подписи.
                stats.point_codes_non_fence += 1
                continue
            if target.category not in {"fence", "unknown"}:
                # Бордюр/клумба имеют собственную подпись — код забора её не затирает.
                stats.point_codes_on_filled_object += 1
                continue
            code_targets[idx] = label
            code_text_map[idx] = str(snap.text).strip()

        # Подпись из словаря слоёв возвращается тем объектам, у которых её
        # снял код точки (иначе бордюр/клумба остались бы без подписи вовсе).
        restore: dict[int, str] = {}
        for feature in inside_features:
            if not feature.label:
                _category, layer_label = classify_layer(feature.layer, self.cfg)
                if layer_label:
                    restore[id(feature)] = layer_label
        for feature in inside_features:
            idx = id(feature)
            if idx in code_targets:
                feature.label = code_targets[idx]
                feature.source = "point_code"
                feature.code_text = code_text_map.get(idx, "")
                stats.point_codes_used += 1
            elif feature.label:
                feature.source = feature.source or "layer"
            elif idx in restore:
                feature.label = restore[idx]
                feature.source = "layer"

        # Дом получает описание из GUI, а не из словаря слоёв.
        for feature in inside_features:
            if feature.category == "building":
                feature.source = "user"
        return inside_features, texts, stats

    def _warn_orphan_point_codes(
        self,
        snapshots: Sequence[CadEntitySnapshot],
        features: Sequence[CadFeature],
        site_polygon: Any,
        stats: LabelingStats,
    ) -> None:
        """
        Точечные объекты (столбы, гидранты, кессоны) не подписываются геометрией,
        но геодезист должен видеть их в отчёте: коды найдены, цели-линии нет.
        """
        buffer_ft = float(self.cfg.get("search_buffer_ft", 50.0)) * 1.2
        counted = 0
        for snap in snapshots:
            if snap.kind != "text" or not self._is_point_label_layer(snap.layer):
                continue
            label = label_for_point_code(snap.text, self.cfg.get("point_code_dictionary"))
            if label is None:
                continue
            if self._nearest_fence(features, snap.point, LabelingStats()) is not None:
                continue
            try:
                if float(site_polygon.distance(Point(snap.point))) > buffer_ft:
                    continue
            except Exception:  # noqa: BLE001
                pass
            counted += 1
            stats.notes.append(
                f"код '{str(snap.text).strip()}' → «{label}»: точечный объект, "
                "линии забора/стены в радиусе 3.5 ft нет — подпись оставлена на усмотрение человека"
            )
        if counted:
            stats.notes.append(
                f"точечных объектов без подписи-линии: {counted} (столбы, гидранты, кессоны)"
            )

    def _is_point_label_layer(self, layer: str) -> bool:
        return any(
            layer_matches_pattern(layer, str(pat))
            for pat in (self.cfg.get("point_label_layers") or [])
        )

    def _nearest_fence(
        self,
        features: Sequence[CadFeature],
        point: tuple[float, float],
        stats: LabelingStats,
    ) -> CadFeature | None:
        radius = float(self.cfg.get("point_code_radius_ft", 3.5))
        probe = Point(point)
        best: CadFeature | None = None
        best_dist = float("inf")
        beyond_dist = float("inf")
        for feature in features:
            if not feature.is_linear:
                continue
            if feature.category not in {"fence", "low_edge", "unknown"}:
                continue
            try:
                distance = float(feature.geom.distance(probe))
            except Exception:  # noqa: BLE001
                continue
            if distance <= radius and distance < best_dist:
                best, best_dist = feature, distance
            elif distance < beyond_dist:
                beyond_dist = distance
        if best is None:
            if beyond_dist < float("inf"):
                stats.point_codes_out_of_radius += 1
            return None
        return best

    # -- раскладка ---------------------------------------------------------
    def build_plans(
        self,
        features: Sequence[CadFeature],
        existing: Sequence[CadText],
        site_polygon: Any,
        *,
        house_label: str = "",
        stats: LabelingStats | None = None,
        dry_run: bool = False,
    ) -> LabelingResult:
        """
        Раскладка подписей: сначала контуры (дом, сарай, крыльцо, ступеньки,
        дорожки), затем заборы; каждый текст проверяется на пересечения.
        """
        ensure_shapely()
        started = time.perf_counter()
        stats = stats or LabelingStats()
        stats.dry_run = dry_run
        stats.point_code_radius_ft = float(self.cfg.get("point_code_radius_ft", 3.5))
        stats.existing_texts = len(existing)
        height = self.text_height
        clearance = float(self.cfg.get("text_clearance_ft", 0.35))
        obstacles = _Obstacles.build(features, site_polygon=site_polygon)
        registry = TextRegistry(
            existing,
            dedupe_radius_ft=float(self.cfg.get("dedupe_radius_ft", 6.0)),
            angle_tolerance_deg=float(self.cfg.get("text_angle_tolerance_deg", 10.0)),
            filled_fence_dedupe_ft=float(self.cfg.get("filled_fence_dedupe_ft", 1.5)),
        )
        site_label = str(house_label or self.cfg.get("house_label") or "").strip()
        plans: list[LabelPlan] = []

        ordered = sorted(
            enumerate(features),
            key=lambda pair: (
                _LABEL_PRIORITY.get(pair[1].category, 9),
                -float(pair[1].area),
                pair[0],
            ),
        )
        for _order, feature in ordered:
            label = self._label_for_feature(feature, site_label, stats)
            if not label:
                if feature.category != "building":
                    stats.skipped_no_label += 1
                continue
            anchors = self._anchor_points(feature)
            rotation_hint = (
                dominant_orientation(feature.geom) if feature.is_linear else 0.0
            )
            if (
                bool(self.cfg.get("skip_existing_labels", True))
                and registry.has_existing(label, anchors, rotation_hint) is not None
            ):
                stats.skipped_existing += 1
                continue
            plan = self._plan_feature_label(
                feature,
                label,
                height,
                site_polygon,
                obstacles,
                registry,
                clearance,
                stats,
            )
            if plan is None:
                stats.skipped_no_room += 1
                continue
            if registry.has_created(
                label,
                list(anchors) + [plan.point],
                rotation_hint,
                ignore_rotation=True,
                own_anchors=list(anchors),
            ) is not None:
                # Рядом уже стоит такая же подпись другого объекта: не дублируем.
                stats.skipped_existing += 1
                continue
            if plan.mode == "parallel":
                stats.parallel += 1
            elif plan.mode == "leader":
                stats.leaders += 1
            registry.register(plan)
            plans.append(plan)
            stats.created += 1
            stats.per_category[feature.category] = stats.per_category.get(feature.category, 0) + 1
            stats.per_label[plan.text] = stats.per_label.get(plan.text, 0) + 1
            if feature.category == "steps" and feature.treads > 0:
                stats.treads_labels += 1
            if feature.is_house:
                stats.house_labeled += 1

        stats.elapsed_sec = time.perf_counter() - started
        stats.layers_written = [self.output_layer] if plans else []
        site_pts = self._polygon_points(site_polygon)
        return LabelingResult(
            plans=plans,
            stats=stats,
            features=list(features),
            site_polygon_pts=site_pts,
            warnings=list(self.warnings),
            written=0,
        )

    def _label_for_feature(
        self, feature: CadFeature, site_label: str, stats: LabelingStats
    ) -> str | None:
        if feature.is_house:
            return site_label or None
        if feature.label:
            return feature.label
        mapping = self.cfg.get("layer_label_mapping") or {}
        for pattern, text in mapping.items():
            if layer_matches_pattern(feature.layer, str(pattern)):
                return str(text)
        return None

    def _anchor_points(self, feature: CadFeature) -> list[tuple[float, float]]:
        """Точки вдоль объекта для проверки радиуса дедупликации."""
        geom = feature.geom
        points: list[tuple[float, float]] = [feature.centroid()]
        try:
            for piece in _line_pieces(geom):
                length = float(piece.length)
                steps = max(1, min(24, int(length // 3.0) + 1))
                for idx in range(steps + 1):
                    dist = length * idx / steps
                    pt = piece.interpolate(dist)
                    points.append((float(pt.x), float(pt.y)))
        except Exception:  # noqa: BLE001
            pass
        return points

    def _polygon_points(self, poly: Any) -> list[tuple[float, float]]:
        if poly is None:
            return []
        try:
            if isinstance(poly, (Polygon,)):
                return [(float(x), float(y)) for x, y in list(poly.exterior.coords)[:-1]]
            if isinstance(poly, MultiPolygon):
                biggest = max(poly.geoms, key=lambda p: float(p.area))
                return [(float(x), float(y)) for x, y in list(biggest.exterior.coords)[:-1]]
        except Exception:  # noqa: BLE001
            return []
        return []

    def _plan_feature_label(
        self,
        feature: CadFeature,
        label: str,
        height: float,
        site_polygon: Any,
        obstacles: "_Obstacles",
        registry: TextRegistry,
        clearance: float,
        stats: LabelingStats,
    ) -> LabelPlan | None:
        if feature.is_linear:
            # Бордюр/клумба — это тоже «линия», но текст ставится выноской.
            if feature.category in {"low_edge"} or (
                feature.category == "planter" and feature.length < 40.0
            ):
                return self._plan_leader_label(
                    feature, label, height, site_polygon, obstacles, registry, clearance
                )
            if feature.curved:
                # Дуговой участок забора/бордюра: текст вдоль хорды выглядел бы
                # неправильно — ставим выноску на свободное место.
                return self._plan_leader_label(
                    feature, label, height, site_polygon, obstacles, registry, clearance
                )
            return self._plan_fence_label(
                feature, label, height, site_polygon, obstacles, registry, clearance
            )
        inside = self._plan_inside_label(
            feature, label, height, obstacles, registry, clearance, site_polygon
        )
        if inside is not None:
            return inside
        return self._plan_leader_label(
            feature, label, height, site_polygon, obstacles, registry, clearance
        )

    # -- подписи внутри контура -------------------------------------------
    def _plan_inside_label(
        self,
        feature: CadFeature,
        label: str,
        height: float,
        obstacles: "_Obstacles",
        registry: TextRegistry,
        clearance: float,
        site_polygon: Any | None = None,
    ) -> LabelPlan | None:
        """`MText` по центру внутри контура (если текст помещается и никого не задевает)."""
        poly = feature.geom
        if not isinstance(poly, (Polygon, MultiPolygon)):
            return None
        box_probe = text_box(label, (0.0, 0.0), height)
        text_w = float(box_probe.bounds[2] - box_probe.bounds[0])
        text_h = float(box_probe.bounds[3] - box_probe.bounds[1])
        if feature.shortest_side < text_h * 1.45:
            return None
        candidates: list[tuple[float, float]] = []
        point = _centroid(poly) if self._centroid_inside(poly) else _safe_interior_point(poly)
        candidates.append(point)
        for frac in (0.0, 0.22, -0.22, 0.4, -0.4):
            if frac == 0.0:
                continue
            shifted = self._shift_along_long_axis(poly, frac * feature.shortest_side)
            candidates.append(shifted)
        candidates.append(_safe_interior_point(poly))
        inner = poly.buffer(-clearance)
        for cand in candidates:
            if not self._fits_inside(poly, cand, text_w, text_h):
                continue
            candidate_box = text_box(label, cand, height)
            if not inner.is_empty and not inner.contains(candidate_box):
                # Текст «вылезает» за контур — попробуем следующую позицию.
                if not poly.contains(candidate_box):
                    continue
            if registry.overlaps_created(candidate_box, clearance * 0.5):
                continue
            if registry.overlaps_existing(candidate_box, clearance=0.0):
                continue
            own = feature.geom
            blocked = [
                hit
                for hit in obstacles.conflicts(candidate_box.buffer(clearance * 0.5), allow_own=own)
                if not self._is_site_exterior(hit, site_polygon)
            ]
            if blocked:
                continue
            return LabelPlan(
                text=label,
                point=cand,
                layer=self.output_layer,
                height=height,
                rotation=0.0,
                mode="inside",
                box=candidate_box,
                source=feature.source or "layer",
                category=feature.category,
                feature_handle=feature.handle,
                feature_layer=feature.layer,
                anchor_points=self._anchor_points(feature),
                is_house=feature.is_house,
            )
        return None

    def _centroid_inside(self, poly: Any) -> bool:
        try:
            return bool(poly.contains(poly.centroid) or poly.centroid.within(poly))
        except Exception:  # noqa: BLE001
            return True

    def _fits_inside(
        self, poly: Any, center: tuple[float, float], width: float, height: float
    ) -> bool:
        """
        Помещается ли текстовый блок `width × height` внутри контура.

        Проверяет и весь чертёж (box целиком), и «внутренность» с небольшим
        допуском: текст у самой стены дома не должен вылезать за контур.
        """
        probe = shapely_box(
            center[0] - width / 2.0,
            center[1] - height / 2.0,
            center[0] + width / 2.0,
            center[1] + height / 2.0,
        )
        try:
            if poly.contains(probe):
                return True
            return bool(poly.buffer(0.02).contains(probe))
        except Exception:  # noqa: BLE001
            return False

    def _shift_along_long_axis(
        self, poly: Any, delta: float
    ) -> tuple[float, float]:
        minx, miny, maxx, maxy = poly.bounds
        base = _centroid(poly) if self._centroid_inside(poly) else _safe_interior_point(poly)
        if (maxx - minx) >= (maxy - miny):
            return (base[0] + delta, base[1])
        return (base[0], base[1] + delta)

    # -- подписи заборов ---------------------------------------------------
    def _plan_fence_label(
        self,
        feature: CadFeature,
        label: str,
        height: float,
        site_polygon: Any,
        obstacles: "_Obstacles",
        registry: TextRegistry,
        clearance: float,
    ) -> LabelPlan | None:
        """
        Длинный прямой забор — текст параллельно линии посередине
        (отступ ``0.8 * высоты``); короткий/занятый — выноска со стрелкой.
        """
        geom = feature.geom
        probe = text_box(label, (0.0, 0.0), height)
        text_w = float(probe.bounds[2] - probe.bounds[0])
        text_h = float(probe.bounds[3] - probe.bounds[1])
        # Отступ считается от высоты СИМВОЛА (0.8·h), а половина высоты блока
        # компенсируется, чтобы КРОМКА текста стояла ровно в 0.8·h от линии.
        offset = float(self.cfg.get("parallel_text_offset_factor", 0.8)) * height + text_h / 2.0
        extra = float(self.cfg.get("min_parallel_extra_ft", 4.0))

        min_piece = float(text_w) + extra
        for piece in self._candidate_pieces(geom, min_piece):
            if not text_fits_along(float(piece.length), text_w, extra):
                continue
            mid = piece.interpolate(0.5, normalized=True)
            coords = list(piece.coords)
            angle = self._piece_angle(coords)
            rotation = read_text_rotation(angle)
            normal = (-math.sin(angle), math.cos(angle))
            for direction in (1.0, -1.0):
                center = (
                    float(mid.x) + normal[0] * offset * direction,
                    float(mid.y) + normal[1] * offset * direction,
                )
                candidate = text_box(label, center, height, rotation=rotation)
                if registry.overlaps_created(candidate, clearance * 0.5):
                    continue
                if registry.overlaps_existing(candidate):
                    continue
                if obstacles.conflicts(
                    candidate.buffer(clearance * 0.5),
                    allow_own=self._own_geometries(geom, site_polygon),
                ):
                    continue
                if not self._near_site(center, site_polygon, margin=12.0):
                    continue
                return LabelPlan(
                    text=label,
                    point=center,
                    layer=self.output_layer,
                    height=height,
                    rotation=rotation,
                    mode="parallel",
                    box=candidate,
                    source=feature.source or "layer",
                    category=feature.category,
                    feature_handle=feature.handle,
                    feature_layer=feature.layer,
                    anchor_points=self._anchor_points(feature),
                )
        return self._plan_leader_label(
            feature, label, height, site_polygon, obstacles, registry, clearance
        )

    def _candidate_pieces(self, geom: Any, min_length: float) -> list[Any]:
        """Смежные сегменты одного забора объединяются `linemerge` (`_line_pieces`)."""
        pieces = _line_pieces(geom)
        straight: list[Any] = []
        for piece in pieces:
            best = _best_straight_piece(piece, min_length)
            straight.append(best if best is not None else piece)
        try:
            merged = linemerge(MultiLineString(pieces))
            for part in _line_pieces(merged):
                if float(part.length) >= min_length:
                    straight.append(part)
        except Exception:  # noqa: BLE001
            pass
        # Один и тот же сегмент приходит и «прямым», и из linemerge — оставляем один.
        unique: list[Any] = []
        for piece in straight:
            duplicate = False
            for kept in unique:
                try:
                    if piece.equals(kept):
                        duplicate = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not duplicate:
                unique.append(piece)
        unique.sort(key=lambda p: float(p.length), reverse=True)
        return unique

    @staticmethod
    def _piece_angle(coords: Sequence[tuple[float, float]]) -> float:
        x1, y1 = coords[0]
        x2, y2 = coords[-1]
        return math.atan2(y2 - y1, x2 - x1)

    def _near_site(self, point: tuple[float, float], site: Any, margin: float) -> bool:
        """Подпись не улетает на соседний квартал: расстояние до участка ≤ margin."""
        if site is None:
            return True
        try:
            return float(site.distance(Point(point))) <= float(margin)
        except Exception:  # noqa: BLE001
            return True

    # -- подписи выноской --------------------------------------------------
    def _plan_leader_label(
        self,
        feature: CadFeature,
        label: str,
        height: float,
        site_polygon: Any,
        obstacles: "_Obstacles",
        registry: TextRegistry,
        clearance: float,
    ) -> LabelPlan | None:
        """
        Выноска: `Leader` со стрелкой на объект + `MText` на свободном месте.

        Свободное место ищется по 16 направлениям от стрелки и нескольким
        радиусам (от «рядом с объектом» до дальнего): выбранная позиция не
        пересекает линии заборов, контуры строений, межу участка и уже
        поставленные тексты. Стрелка обязана стоять на самом объекте.
        """
        probe = text_box(label, (0.0, 0.0), height)
        text_w = float(probe.bounds[2] - probe.bounds[0])
        text_h = float(probe.bounds[3] - probe.bounds[1])
        min_len = float(self.cfg.get("leader_min_length_ft", 2.5))
        max_len = float(self.cfg.get("leader_max_length_ft", 60.0))
        gap = float(self.cfg.get("leader_gap_factor", 2.2)) * text_h
        attempts = int(self.cfg.get("max_leader_attempts", 16))
        near_margin = float(self.cfg.get("search_buffer_ft", 50.0)) * 0.4
        text_offset = 0.75 * text_h + gap

        distances: list[float] = []
        for raw in (
            text_w * 0.55 + gap,
            text_w * 0.55 + gap * 2.0,
            2.5 + text_w * 0.55,
            8.0,
            14.0,
            22.0,
            max_len * 0.5,
        ):
            value = max(min_len, min(max_len, float(raw)))
            if all(abs(value - existing) > 0.25 for existing in distances):
                distances.append(value)

        best: tuple[float, LabelPlan] | None = None
        for anchor in self._leader_anchor_points(feature):
            if not self._leader_in_lot(anchor, site_polygon):
                continue
            for angle_idx in range(attempts):
                angle = 2.0 * math.pi * angle_idx / attempts + math.pi / 8.0
                ux, uy = math.cos(angle), math.sin(angle)
                for distance in distances:
                    # Стрелка на объекте, затем «плечо» и свободный конец выноски.
                    bend = (anchor[0] + ux * distance, anchor[1] + uy * distance)
                    text_center = (
                        anchor[0] + ux * (distance + text_offset),
                        anchor[1] + uy * (distance + text_offset),
                    )
                    if not self._near_site(text_center, site_polygon, margin=near_margin):
                        continue
                    # Внутри участка текст читается лучше, чем за межой: такие
                    # варианты получают заметную фору в оценке, но не запрещаются
                    # (у границы участка снаружи иногда только и есть место).
                    inside_lot = self._point_inside_site(text_center, site_polygon)
                    candidate = text_box(label, text_center, height)
                    if registry.overlaps_created(candidate, clearance * 0.6):
                        continue
                    if registry.overlaps_existing(candidate):
                        continue
                    leader_geom = LineString([anchor, bend, text_center])
                    blocked = False
                    for hit in obstacles.conflicts(leader_geom.buffer(clearance * 0.5)):
                        if self._is_site_exterior(hit, site_polygon):
                            continue
                        if self._leader_touches_own(hit, feature, anchor, text_h):
                            continue
                        blocked = True
                        break
                    if blocked:
                        continue
                    if obstacles.conflicts(candidate.buffer(clearance * 0.5)):
                        continue
                    score = (
                        distance
                        + text_offset
                        + float(feature.geom.distance(Point(text_center)))
                        + (0.0 if inside_lot else 12.0)
                    )
                    if best is None or score < best[0]:
                        best = (
                            score,
                            LabelPlan(
                                text=label,
                                point=text_center,
                                layer=self.output_layer,
                                height=height,
                                rotation=0.0,
                                mode="leader",
                                box=candidate,
                                leader_points=[anchor, bend, text_center],
                                leader_text_point=text_center,
                                source=feature.source or "layer",
                                category=feature.category,
                                feature_handle=feature.handle,
                                feature_layer=feature.layer,
                                anchor_points=self._anchor_points(feature),
                                is_house=feature.is_house,
                            ),
                        )
        return best[1] if best is not None else None

    def _leader_touches_own(
        self,
        geom: Any,
        feature: CadFeature,
        anchor: tuple[float, float],
        text_half_height: float,
        tol: float = 1.2,
    ) -> bool:
        """
        Препятствие — это сам подписываемый объект (или межа, на которой он стоит)?

        Выноска по определению начинается на объекте, поэтому её собственные
        линии (забор, бордюр, контур ступенек) не считаются конфликтом.
        """
        try:
            if geom is feature.geom:
                return True
            if float(geom.distance(feature.geom)) <= max(0.02, tol * 0.05):
                return True
            return float(geom.distance(Point(anchor))) <= max(tol, text_half_height * 4.0)
        except Exception:  # noqa: BLE001
            return False

    def _own_geometries(self, geom: Any, site: Any) -> list[Any]:
        """
        Своя геометрия объекта + межа участка, если забор стоит прямо на ней.

        Полевой забор часто совпадает с границей участка: подпись вдоль такого
        забора не должна «конфликтовать» с его же линией межи.
        """
        own: list[Any] = [geom]
        if site is None or not isinstance(site, Polygon):
            return own
        try:
            if float(geom.distance(site.exterior)) <= 0.75:
                own.append(site.exterior)
        except Exception:  # noqa: BLE001
            pass
        return own

    def _is_site_exterior(self, geom: Any, site: Any) -> bool:
        """Это линия межи участка (её пересекать выноской можно)?"""
        if site is None or geom is None:
            return False
        try:
            if not isinstance(site, Polygon):
                return False
            return bool(float(geom.distance(site.exterior)) <= 1e-6 and geom.length > 0)
        except Exception:  # noqa: BLE001
            return False

    def _leader_anchor_points(self, feature: CadFeature) -> list[tuple[float, float]]:
        """Стрелка ставится на саму линию/контур, ближе к центру объекта."""
        anchors: list[tuple[float, float]] = []
        center = feature.centroid()
        try:
            nearest = nearest_points(feature.geom, Point(center))
            anchors.append((float(nearest[0].x), float(nearest[0].y)))
        except Exception:  # noqa: BLE001
            pass
        for piece in _line_pieces(feature.geom)[:4]:
            for frac in (0.5, 0.25, 0.75):
                try:
                    pt = piece.interpolate(frac, normalized=True)
                    anchors.append((float(pt.x), float(pt.y)))
                except Exception:  # noqa: BLE001
                    continue
        # Убираем почти совпадающие точки, чтобы не дублировать попытки.
        uniq: list[tuple[float, float]] = []
        for pt in anchors:
            if all(math.hypot(pt[0] - u[0], pt[1] - u[1]) > 0.4 for u in uniq):
                uniq.append(pt)
        return uniq or [center]

    @staticmethod
    def _point_inside_site(point: tuple[float, float], site: Any) -> bool:
        """Точка внутри участка (не за межой) — для приоритета при выборе места."""
        if site is None:
            return True
        try:
            probe = Point(point)
            if bool(site.contains(probe)):
                return True
            # допуск на «на межe»: центр текста у самой линии забора
            return float(site.exterior.distance(probe)) <= 0.05 if isinstance(site, Polygon) else False
        except Exception:  # noqa: BLE001
            return True

    def _leader_in_lot(
        self, anchor: tuple[float, float], site: Any, tol: float = 12.0
    ) -> bool:
        """Стрелка — на объекте участка либо в буфере вокруг него (не на соседнем квартале)."""
        if site is None:
            return True
        try:
            return float(site.distance(Point(anchor))) <= float(
                self.cfg.get("search_buffer_ft", 50.0)
            ) + tol
        except Exception:  # noqa: BLE001
            return True

    def _leader_touches_own(
        self,
        geom: Any,
        feature: CadFeature,
        anchor: tuple[float, float],
        text_half_height: float,
        tol: float = 1.2,
    ) -> bool:
        """Линия выноски касается СВОЕГО подписываемого объекта (учёт ширины полки)."""
        try:
            return float(geom.distance(feature.geom)) <= max(tol, text_half_height * 4.0)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _bend_point(
        anchor: tuple[float, float], text_point: tuple[float, float], min_len: float
    ) -> tuple[float, float]:
        """Точка перелома выноски: 1/3 пути, слегка сдвинутая (плечо стрелки)."""
        dx = text_point[0] - anchor[0]
        dy = text_point[1] - anchor[1]
        length = math.hypot(dx, dy)
        if length < max(min_len, 1e-6):
            return (anchor[0] + dx * 0.5, anchor[1] + dy * 0.5)
        ux, uy = dx / length, dy / length
        px, py = -uy, ux
        return (
            anchor[0] + ux * length * 0.42 + px * min_len * 0.5,
            anchor[1] + uy * length * 0.42 + py * min_len * 0.5,
        )

    # -- запись в AutoCAD --------------------------------------------------
    def apply(
        self,
        drawing: "AcadDrawing",
        result: LabelingResult,
        *,
        dry_run: bool = False,
    ) -> int:
        """
        Пишет готовые подписи в слой `D-TOPO-TEXT` (MText + при необходимости Leader).

        Возвращает количество созданных текстов; предупреждения COM попадают
        в ``result.warnings`` и ``result.stats.warnings``.
        """
        written = 0
        if dry_run:
            result.written = 0
            return 0
        layer_ok = drawing.ensure_layer(self.output_layer, 3)
        if not layer_ok:
            message = (
                f"Не удалось создать/открыть слой {self.output_layer}: подписи "
                "уйдут в текущий слой объектов"
            )
            result.warnings.append(message)
            result.stats.warnings.append(message)
            result.stats.skipped_no_label_layer = len(result.plans)
        arrow = max(0.06, float(result.stats and self.text_height) * 0.9)
        for plan in result.plans:
            layer = self.output_layer if layer_ok else plan.layer
            width = self._mtext_width(plan.text, plan.height)
            mtext = None
            try:
                if plan.mode == "parallel" and hasattr(drawing, "add_dtext"):
                    # Подпись забора вдоль линии: обычный DBTEXT (AddText).
                    mtext = drawing.add_dtext(
                        plan.text,
                        plan.point,
                        plan.height,
                        layer,
                        rotation=plan.rotation,
                    )
                else:
                    mtext = drawing.add_mtext(
                        plan.text,
                        plan.point,
                        plan.height,
                        width,
                        layer,
                        rotation=plan.rotation,
                        attachment=5,
                    )
            except Exception as exc:  # noqa: BLE001
                message = f"не удалось создать подпись «{plan.text}»: {exc}"
                result.warnings.append(message)
                result.stats.warnings.append(message)
                continue
            written += 1
            if plan.mode == "leader" and plan.leader_points and plan.leader_text_point:
                try:
                    leader, _ = drawing.add_leader(
                        plan.leader_points,
                        layer,
                        arrow_head_size=arrow,
                        mtext=mtext,
                        text_width=width,
                        rotation=plan.rotation,
                        attachment=8,
                    )
                    if leader is None:
                        message = (
                            "AutoCAD не создал Leader (AddLeader недоступен) — "
                            f"выноска «{plan.text}» поставлена как MText"
                        )
                        if message not in result.warnings:
                            result.warnings.append(message)
                            result.stats.warnings.append(
                                "часть выносок нарисована без линии-стрелки"
                            )
                except Exception as exc:  # noqa: BLE001
                    message = f"не удалось создать выноску «{plan.text}»: {exc}"
                    result.warnings.append(message)
                    result.stats.warnings.append(message)
        result.written = written
        if written and bool(self.cfg.get("zoom_to_labels", True)):
            self._zoom_to_result(drawing, result)
        return written

    def _mtext_width(self, text: str, height: float) -> float:
        box_probe = text_box(text, (0.0, 0.0), height)
        width = float(box_probe.bounds[2] - box_probe.bounds[0])
        return max(width * 1.15, height * 2.0)

    def _zoom_to_result(self, drawing: "AcadDrawing", result: LabelingResult) -> None:
        points: list[tuple[float, float]] = list(result.site_polygon_pts)
        for plan in result.plans:
            points.append(plan.point)
            points.extend(plan.leader_points)
        if not points:
            return
        xs = [pt[0] for pt in points]
        ys = [pt[1] for pt in points]
        margin = max(float(self.cfg.get("search_buffer_ft", 50.0)) * 0.25, 5.0)
        drawing.zoom_window(
            (min(xs) - margin, min(ys) - margin),
            (max(xs) + margin, max(ys) + margin),
        )

    # -- полный сценарий ---------------------------------------------------
    def run(
        self,
        drawing: "AcadDrawing",
        *,
        plan_vertices: Sequence[tuple[float, float]] | None = None,
        picked_vertices: Sequence[tuple[float, float]] | None = None,
        house_label: str = "",
        skip_existing: bool = True,
        dry_run: bool = False,
        read_only: bool = False,
    ) -> LabelingResult:
        """
        Полный шаг 2 для активного чертежа: чтение → распознавание → запись.

        `read_only=True` — только анализ (полезно для «Показать, что будет
        подписано» без единого нового объекта в DWG).
        """
        ensure_shapely()
        self.cfg["skip_existing_labels"] = bool(skip_existing)
        snapshots = drawing.snapshot_entities()
        site_polygon, source = self.site_polygon(
            snapshots, plan_vertices=plan_vertices, picked_vertices=picked_vertices
        )
        if site_polygon is None:
            raise FeatureLabelerError(
                "Не найден контур участка. Нарисуйте/укажите замкнутую полилинию "
                "границы (слои PROPERTY_LINE / PL / M-TAX-LINE / PROPERTY_LINES_APPROX) "
                "или сначала выполните шаг 1."
            )
        stats = LabelingStats()
        features, _texts, stats = self.collect_features(snapshots, site_polygon, stats=stats)
        existing: list[CadText] = []
        if skip_existing:
            existing = [
                item
                for item in (drawing.read_existing_texts(self.cfg.get("dedupe_text_layers") or []))
                if str(item.text or "").strip()
            ]
        result = self.build_plans(
            features,
            existing,
            site_polygon,
            house_label=house_label,
            stats=stats,
            dry_run=dry_run or read_only,
        )
        result.source_of_site = source
        self._warn_orphan_point_codes(snapshots, features, site_polygon, stats)
        stats.notes.append(f"Контур участка: {source}")
        if not read_only:
            self.apply(drawing, result, dry_run=dry_run)
        return result


# ---------------------------------------------------------------------------
# In-memory замена AutoCAD для тестов и «пробного прогона»
# ---------------------------------------------------------------------------


def _pairs_to_variant(pairs: Sequence[tuple[float, float]]) -> list[float]:
    flat: list[float] = []
    for x, y in pairs:
        flat.extend([float(x), float(y)])
    return flat


class FakeCadDrawing:
    """
    Полностью функциональная замена `AcadDrawing` без COM.

    Хранит геометрию и тексты в памяти, поддерживает `AddMText`,
    `AddLeader`, `read_existing_texts` и `ensure_layer`. Используется в тестах
    и в режиме «только анализ», чтобы геодезист увидел отчёт до записи.
    """

    def __init__(self) -> None:
        self.entities: list[CadEntitySnapshot] = []
        self.texts: list[dict[str, Any]] = []
        self.layers: set[str] = {"0"}
        self.leaders: list[dict[str, Any]] = []
        self.dtexts: list[dict[str, Any]] = []
        self.zoomed: tuple[tuple[float, float], tuple[float, float]] | None = None
        self.mtexts: list[dict[str, Any]] = []

    # -- наполнение --------------------------------------------------------
    def add_line(
        self, start: tuple[float, float], end: tuple[float, float], layer: str, handle: str = ""
    ) -> "FakeCadDrawing":
        self.entities.append(
            CadEntitySnapshot(
                kind="line",
                layer=layer,
                handle=handle or f"L{len(self.entities)}",
                vertices=[(float(start[0]), float(start[1])), (float(end[0]), float(end[1]))],
                entity_type="AcDbLine",
            )
        )
        self.layers.add(layer)
        return self

    def add_polyline(
        self,
        points: Sequence[tuple[float, float]],
        layer: str,
        *,
        closed: bool = True,
        bulges: Sequence[float] | None = None,
        handle: str = "",
    ) -> "FakeCadDrawing":
        self.entities.append(
            CadEntitySnapshot(
                kind="polyline",
                layer=layer,
                handle=handle or f"P{len(self.entities)}",
                vertices=[(float(x), float(y)) for x, y in points],
                closed=bool(closed),
                bulges=list(bulges or []),
                entity_type="AcDbPolyline",
            )
        )
        self.layers.add(layer)
        return self

    def add_rect(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        layer: str,
        *,
        handle: str = "",
    ) -> "FakeCadDrawing":
        return self.add_polyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], layer, closed=True, handle=handle
        )

    def add_text(
        self,
        text: str,
        point: tuple[float, float],
        layer: str,
        height: float = 0.16,
        rotation: float = 0.0,
    ) -> "FakeCadDrawing":
        """Добавить СУЩЕСТВУЮЩИЙ текст чертежа (не наша подпись)."""
        self.entities.append(
            CadEntitySnapshot(
                kind="text",
                layer=layer,
                handle=f"T{len(self.entities)}",
                text=str(text),
                point=(float(point[0]), float(point[1])),
                height=float(height),
                rotation=float(rotation),
                entity_type="AcDbText",
            )
        )
        self.layers.add(layer)
        return self

    # -- интерфейс AcadDrawing --------------------------------------------
    def snapshot_entities(self, **_kwargs: Any) -> list[CadEntitySnapshot]:
        return list(self.entities)

    def read_existing_texts(self, layers: Sequence[str]) -> list[CadText]:
        wanted = {normalize_layer_name(name) for name in layers if str(name).strip()}
        found: list[CadText] = []
        for snap in self.entities:
            if snap.kind != "text":
                continue
            if normalize_layer_name(snap.layer) in wanted:
                found.append(
                    CadText(
                        text=snap.text,
                        point=snap.point,
                        layer=snap.layer,
                        height=snap.height,
                        rotation=snap.rotation,
                    )
                )
        for item in self.mtexts:
            if normalize_layer_name(str(item.get("layer") or "")) in wanted:
                found.append(
                    CadText(
                        text=str(item.get("text") or ""),
                        point=tuple(item.get("point") or (0.0, 0.0)),  # type: ignore[arg-type]
                        layer=str(item.get("layer") or ""),
                        height=float(item.get("height") or 0.0),
                        rotation=float(item.get("rotation") or 0.0),
                    )
                )
        return found

    def ensure_layer(self, name: str, color: int = 7) -> bool:
        self.layers.add(name)
        return True

    def add_mtext(
        self,
        text: str,
        point: tuple[float, float],
        height: float,
        width: float,
        layer: str,
        rotation: float = 0.0,
        attachment: int = 5,
    ) -> dict[str, Any]:
        record = {
            "text": str(text),
            "point": (float(point[0]), float(point[1])),
            "height": float(height),
            "width": float(width),
            "layer": layer,
            "rotation": float(rotation),
            "attachment": int(attachment),
        }
        self.mtexts.append(record)
        self.layers.add(layer)
        return record

    def add_dtext(
        self,
        text: str,
        point: tuple[float, float],
        height: float,
        layer: str,
        rotation: float = 0.0,
        style: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "text": str(text),
            "point": (float(point[0]), float(point[1])),
            "height": float(height),
            "width": 0.0,
            "layer": layer,
            "rotation": float(rotation),
            "attachment": 10,
            "kind": "dtext",
            "style": style,
        }
        self.mtexts.append(record)
        self.dtexts.append(record)
        self.layers.add(layer)
        return record

    def add_leader(
        self,
        points: Sequence[tuple[float, float]],
        layer: str,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], None]:
        record = {
            "points": [(float(x), float(y)) for x, y in points],
            "layer": layer,
        }
        self.leaders.append(record)
        self.layers.add(layer)
        return record, None

    def zoom_window(
        self, corner1: tuple[float, float], corner2: tuple[float, float]
    ) -> bool:
        self.zoomed = (corner1, corner2)
        return True

    def release(self) -> None:
        return None

    # -- удобства для проверок --------------------------------------------
    def labels(self) -> list[str]:
        return [str(item["text"]) for item in self.mtexts]

    def labels_on(self, layer: str) -> list[str]:
        return [str(item["text"]) for item in self.mtexts if item["layer"] == layer]


# ---------------------------------------------------------------------------
# Опции шага 2 и удобные обёртки
# ---------------------------------------------------------------------------


@dataclass
class LabelingOptions:
    """Настройки шага 2, которые выставляет GUI."""

    house_label: str = ""
    skip_existing: bool = True
    dry_run: bool = False
    text_height: float | None = None
    scale: float | None = None
    search_buffer_ft: float | None = None


def label_features(
    drawing: Any,
    cfg: dict[str, Any] | None = None,
    options: LabelingOptions | None = None,
    *,
    plan_vertices: Sequence[tuple[float, float]] | None = None,
    picked_vertices: Sequence[tuple[float, float]] | None = None,
) -> LabelingResult:
    """
    Удобная обёртка шага 2 для GUI.

    cfg — полный config.json; секция `features` читается внутри. Масштаб из GUI
    (`scale`) задаёт высоту подписи, если она не задана явно.
    """
    opts = options or LabelingOptions()
    labeler = FeatureLabeler(cfg)
    if opts.text_height is not None:
        labeler.cfg["text_height"] = float(opts.text_height)
    elif opts.scale:
        labeler.cfg["text_height"] = feature_text_height(labeler.cfg, opts.scale)
    if opts.search_buffer_ft is not None:
        labeler.cfg["search_buffer_ft"] = float(opts.search_buffer_ft)
    return labeler.run(
        drawing,
        plan_vertices=plan_vertices,
        picked_vertices=picked_vertices,
        house_label=opts.house_label,
        skip_existing=opts.skip_existing,
        dry_run=opts.dry_run,
    )


def analyze_drawing(
    snapshots: Sequence[CadEntitySnapshot],
    cfg: dict[str, Any] | None = None,
    *,
    plan_vertices: Sequence[tuple[float, float]] | None = None,
    house_label: str = "",
    skip_existing: bool = True,
) -> LabelingResult:
    """
    Анализ уже прочитанного чертежа (без AutoCAD) — для предпросмотра в GUI.

    Используется, чтобы показать геодезисту план подписей до записи в DWG.
    """
    labeler = FeatureLabeler(cfg)
    fake = FakeCadDrawing()
    fake.entities = list(snapshots)
    return labeler.run(
        fake,
        plan_vertices=plan_vertices,
        house_label=house_label,
        skip_existing=skip_existing,
        read_only=True,
    )


def load_feature_cfg(config_path: str | Path | None = None) -> dict[str, Any]:
    """Читает секцию `features` из config.json (или отдаёт значения по умолчанию)."""
    path = Path(config_path) if config_path is not None else Path(__file__).resolve().with_name("config.json")
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return configure_feature_cfg(None)
    return configure_feature_cfg(raw)


__all__ = [
    "ACAD_PROGID",
    "AcadDrawing",
    "CadEntitySnapshot",
    "CadFeature",
    "CadText",
    "DEFAULT_FEATURE_CFG",
    "FakeCadDrawing",
    "FeatureLabeler",
    "FeatureLabelerError",
    "LabelPlan",
    "LabelingOptions",
    "LabelingResult",
    "LabelingStats",
    "SHAPELY_AVAILABLE",
    "ShapelyMissingError",
    "TextRegistry",
    "analyze_drawing",
    "classify_layer",
    "configure_feature_cfg",
    "count_treads",
    "dedupe_key",
    "dominant_orientation",
    "ensure_shapely",
    "feature_text_height",
    "fence_prefix_label",
    "features_from_snapshots",
    "flatten_polyline",
    "is_note_text_layer",
    "label_features",
    "label_for_point_code",
    "labels_equivalent",
    "layer_matches_pattern",
    "load_feature_cfg",
    "matches_tread_layer",
    "normalize_layer_name",
    "orientation_compatible",
    "parse_point_code",
    "point_code_family",
    "read_text_rotation",
    "strip_mtext_formatting",
    "text_box",
    "text_fits_along",
]
