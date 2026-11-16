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
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Iterable, Sequence

from version import APP_DATE, APP_VERSION

try:
    from shapely.geometry import LineString, Point, Polygon, box as shapely_box
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
    "NorthArrow",
    "find_north_arrow",
    "is_north_arrow_block",
    "vertical_text_direction",
    "DrawingText",
    "Feature",
    "FieldNote",
    "FeatureLabelError",
    "LayerClassifier",
    "PointCode",
    "PropertyBoundary",
    "RecognitionResult",
    "FenceLinetypeItem",
    "FenceLinetypePlan",
    "bridge_gaps",
    "feature_settings",
    "fence_linetype_plan",
    "fence_linetype_scale",
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
    "property_line_layers": [
        "PROPERTY_LINE", "PL", "M-PROPERTY", "M-TAX-LINE", "PROPERTY_LINES_APPROX",
    ],
    #: линии, которыми замыкается граница, если в слое участка её не хватает:
    #: фасадная сторона участка обычно начерчена только как красная линия улицы
    "property_closing_layers": ["M-ROW-LINE"],
    "point_label_layers": ["POINT_LABELS", "PNT-X", "PNTS"],
    "fence_layers": ["S-FENCE", "FENCE", "CODE_CREW", "FENCE_*"],
    "fence_layer_exclude": ["*_NOTE"],
    #: линии, соединяющие снятые точки (CODE_CREW): забором они считаются
    #: только там, где рядом стоит код забора; без кода — молча пропускаются
    "code_linework_layers": ["CODE_CREW"],
    #: калитки и ворота: подпись «<сокращение материала> / GATE» поперёк линии
    "gate_layers": ["GATE_*", "*_GATE"],
    "structure_layers": {
        "house": ["BUILDING_OUTLINE", "ST-BLDG-LINE", "REF_VEC_BUILDING"],
        "shed": ["*SHED*"],
        "porch": ["*PORCH*", "*LANDING*", "*DECK*"],
        "steps": ["*_STEPS", "*STAIR*", "ST-STEP"],
        "planter": ["*PLANTER*"],
        "pool": ["POOL"],
        "wall": ["ST-WALL", "WALL"],
        "overhang": ["ST-OHED"],
        "surface": [
            "DRIVEWAY_EDGE",
            "SIDEWALK_EDGE",
            "WALK_EDGE",
            "WALK_*_SKETCH",
            "GRASS_STRIP_EDGE",
            "S-ROAD-DWY",
            "S-ROAD-SW-LINE",
            "S-DWY",
            "S-CONC-MISC",
        ],
        #: «разное» шаблона геодезиста: что это за объект, говорит описание точки
        "misc": ["ST-MISC"],
    },
    #: категории линейных объектов: подпись вдоль линии, площадь не строится
    "linear_structure_categories": ["overhang"],
    #: узкие длинные контуры этих категорий (стена — две линии в футе друг от
    #: друга) подписываются как линия: текст вдоль длинной стороны
    "ribbon_categories": ["wall"],
    #: слои этих категорий тоже могут содержать ленты из двух линий без торцов
    #: (стенка клумбы в ST-MISC): торцы достраиваются, лента становится контуром
    "ribbon_cap_categories": ["misc"],
    "ribbon_cap_min_length_ft": 6.0,
    "ribbon_max_width_ft": 1.6,
    "step_tread_layers": ["*TREADS*"],
    "label_output_layer": "D-TOPO-TEXT",
    "label_output_layer_color": 7,
    #: Описание основного дома в итоговых чертежах заказчика — MText в слое 0,
    #: стиль RDS, высота 1.5 ft при подписях 0.9 ft. Стиль берётся, если он есть
    #: в чертеже (иначе — общий стиль подписей). Пустой слой — как все подписи.
    "house_label_layer": "0",
    "house_label_text_style": "RDS",
    #: Стиль описания дома создаётся с этими параметрами, если его в чертеже нет
    #: (так он задан в шаблоне заказчика: ROMAND.shx, сжатие 0.8, наклон 20°).
    "house_label_style_definition": {"font": "romand.shx", "width_factor": 0.8, "oblique_deg": 20.0},
    #: Высоты текста заказчика при масштабе 1"=16' (``drawing.base_scale``), ft:
    #: 0.9 — подписи объектов, 1.1 — отступы (offsets) до дома, 1.5 — описание дома.
    #: Для другого масштаба все три умножаются на масштаб / 16
    #: (1"=10' ×0.625, 20 ×1.25, 30 ×1.875, 40 ×2.5, 50 ×3.125, 60 ×3.75).
    "standard_text_heights_ft": {"label": 0.9, "house_offset": 1.1, "house": 1.5},
    #: Описание дома, не помещающееся в контур, уменьшается шагами по
    #: ``house_label_height_step_ft`` (при 1"=16'), но не мельче обычной подписи.
    "house_label_shrink_to_fit": True,
    "house_label_height_step_ft": 0.1,
    #: слои готовых подписей (итоговый чертёж): по ним проверяются дубликаты
    "existing_label_layers": ["D-TOPO-TEXT"],
    # --- источник 1: заметки полевого чертёжника -----------------------------
    #: слои полевых заметок: текст заметки переводится в формулировку заказчика
    #: и ставится в D-TOPO-TEXT у объекта, на который указывает заметка
    "field_note_layers": [
        "FENCE_NOTE",
        "ENTRANCE_TEXT",
        "SURFACE_TEXT",
        "CONTEXT_TEXT",
        "CONTEXT_SURFACE_TEXT",
        "LOW_EDGE_NOTE",
        "*_NOTE",
        "*_TEXT",
    ],
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
        "BLC*", "DW*", "OP*", "PLAN*", "SHED*", "STEP*", "SW*", "LST*", "FD*", "XP*", "TTP*",
        "BL", "BL?",
    ],
    "point_to_fence_max_ft": 3.5,
    # --- источник 2б: описания точек съёмки (атрибуты блоков) ---------------
    #: Типовой чертёж геодезиста: подписей на экране нет, описание объекта
    #: записано в атрибуте блока точки (``DESC2`` блока ``srvpno3``), а сам блок
    #: лежит в слое объекта. Теги атрибутов блока точки (шаблоны * и ?):
    "point_block_attribute_tags": {
        "number": ["PT#", "PT*", "PNT*", "NUM*"],
        "description": ["DESC*"],
        "elevation": ["ELEV*"],
    },
    #: Перевод рабочих описаний геодезиста в подписи заказчика. Срабатывает
    #: ПЕРВОЕ подходящее правило; регистр, пробелы и знаки («CONC-SW» =
    #: «conc sw») не важны. ``category`` — тип объекта: fence, shed, porch,
    #: steps, planter, pool, wall, overhang, surface. Пустая подпись — точка
    #: известна, но объект не подписывается (углы домов, точки хода, отметки).
    #: Номер линии Carlson в конце кода (``BLC2``, ``SHED1``) отбрасывается.
    #: ``{area}`` в подписи — площадь контура в кв. футах; ``label_narrow`` —
    #: подпись для объекта уже ``narrow_object_max_width_ft`` (одна ступень).
    "point_descriptions": [
        {"match": ["VINYL FENCE", "V FENCE", "PVC FENCE"], "label": "VINYL FENCE", "category": "fence"},
        {"match": ["F E C", "CHAIN LINK FENCE", "C L F"], "label": "CHAIN LINK FENCE", "category": "fence"},
        {"match": ["WOOD FENCE", "W FENCE", "STOCKADE FENCE"], "label": "WOOD FENCE", "category": "fence"},
        {
            "match": ["IRON FENCE", "I FENCE", "I P F", "IRON PICKET FENCE", "F E I"],
            "label": "IRON FENCE", "category": "fence",
        },
        {"match": ["O PORCH", "OPEN PORCH", "PORCH"], "label": "O.P.", "category": "porch"},
        # Материал настила в коде «DECK» не записан (у заказчика бывает и FRAME
        # DECK, и METAL DECK) — без уточнения пишется просто DECK.
        {"match": ["WOOD DECK", "FRAME DECK"], "label": "FRAME\nDECK", "category": "porch"},
        {"match": ["METAL DECK", "IRON DECK"], "label": "METAL\nDECK", "category": "porch"},
        {"match": ["DECK"], "label": "DECK", "category": "porch"},
        {"match": ["VESTIBULE", "VEST"], "label": "VEST.", "category": "porch"},
        {"match": ["CELDOOR", "CEL DOOR", "CELLAR DOOR"], "label": "C.D.", "category": "cellar_door"},
        {"match": ["FIRESCAPE", "FIRE ESCAPE", "FIRE ESC"], "label": "FIRE ESC.", "category": "fire_escape"},
        {"match": ["AW OVERH", "AWNING", "AWN"], "label": "AWNING", "category": "awning"},
        {
            "match": ["STEPS", "STEP", "STAIRS"], "label": "STEPS", "label_narrow": "STEP",
            "category": "steps",
        },
        {"match": ["SHED"], "label": "SHED", "category": "shed"},
        {"match": ["POOL"], "label": "POOL\nAREA {area} Sq.Ft.", "category": "pool"},
        {
            "match": ["PLANTER"], "label": "PLANTER", "label_narrow": "PLANT.",
            "category": "planter",
        },
        {"match": ["BOT WALL", "TOP WALL", "WALL"], "label": "WALL", "category": "wall"},
        {"match": ["CANTILEVER", "OVERHANG"], "label": "CANTIL.", "category": "overhang"},
        {"match": ["EDG DWY", "EDGE DWY", "DWY", "DRIVEWAY"], "label": "DRIVEWAY", "category": "surface"},
        # COL — колонна: сама не подписывается, но стена с колоннами — «WALL W./COLUMNS».
        {"match": ["COLUMN", "COL"], "label": "", "category": "column"},
        # BLC — building corner, BL — building line; TTP — точка хода; CONC-SW —
        # кромка тротуара/дорожки: материал покрытия заказчик берёт с фотографий.
        # COLUMN — колонна у входа, BBC — точка бортового камня: в итоговых
        # чертежах заказчика не подписаны.
        # W U P — столб: подписывается его условный знак (symbol_block_labels).
        {
            "match": [
                "BLC*", "BL", "TTP*", "SPOT GRADE*", "CONC SW", "SW", "BBC*",
                "BC*", "TC*", "EDG CONC", "EDGE CONC", "DW", "W U P", "WUP", "U P",
            ],
            "label": "",
        },
    ],
    "narrow_object_max_width_ft": 1.5,
    #: стена, у которой стоят колонны (точки категории column не дальше
    #: ``wall_columns_reach_ft`` от её линии, не меньше ``wall_columns_min_count``)
    "wall_with_columns_format": "{wall} W./COLUMNS",
    "wall_columns_reach_ft": 1.5,
    "wall_columns_min_count": 2,
    #: козырёк (AW OVERH), под которым сняты точки крыльца: подпись крыльца -> подпись
    #: крытого крыльца. Пустой словарь — правило выключено (останется AWNING).
    "roofed_porch_labels": {"O.P.": "ROOFED\nO.P."},
    #: слова в конце описания точки, которые к названию объекта не относятся:
    #: NE — «no elevation», точка снята без отметки («VINYL FENCE NE», «WALL NE»)
    "point_description_ignored_suffixes": ["NE"],
    #: точки с отметкой лежат в слое «<слой объекта><суффикс>» (ST-STEP-ELEV,
    #: S-CONC-MISC-ELEV): описание относится к линиям слоя без суффикса
    "point_layer_suffixes": ["-ELEV"],
    # --- источник 2в: условные знаки (вставки блоков без атрибутов) ---------
    #: имя блока -> подпись. Знак подписывается выноской. ``symbol_label_scope``:
    #: "lot" — только знаки на самом участке (столб во дворе заказчик
    #: подписывает, столбы на улице — нет), "all" — все в зоне поиска,
    #: "none" — не подписывать.
    "symbol_block_labels": {"WUP": "UTILITY\nPOLE", "UP": "UTILITY\nPOLE"},
    "symbol_label_scope": "lot",
    "symbol_lot_tolerance_ft": 1.0,
    # --- общие стены (party wall) -------------------------------------------
    #: Две параллельные линии этих слоёв, идущие вдоль границы участка не
    #: дальше ``party_wall_max_width_ft`` друг от друга, — общая с соседом стена
    #: дома блокированной застройки: подпись вдоль неё со стороны соседа.
    "party_wall_layers": ["0"],
    "party_wall_label": "PARTY WALL",
    "party_wall_max_width_ft": 1.6,
    "party_wall_min_length_ft": 10.0,
    #: забор, идущий по стене (в пределах ``fence_atop_wall_reach_ft`` от её
    #: контура), подписывается вместе с ней; у стены своей подписи нет
    "fence_atop_wall_format": "{fence} ATOP {wall}",
    #: Стена под забором почти всегда бетонная: стена без материала в описании
    #: (``fence_atop_plain_wall_names``) пишется «CONC. WALL», а там, где подпись
    #: не помещается вдоль забора, — «C.WALL». Пустая строка — писать как снято.
    "fence_atop_plain_wall_names": ["WALL"],
    "fence_atop_wall_default": "CONC. WALL",
    "fence_atop_wall_compact": {"CONC. WALL": "C.WALL", "CONCRETE WALL": "C.WALL"},
    "fence_atop_wall_reach_ft": 0.5,
    "fence_atop_wall_min_ft": 4.0,
    #: общая подпись ставится, если забор идёт по стене не короче этой длины;
    #: на более коротком участке стена просто остаётся без подписи
    "fence_atop_wall_label_min_ft": 10.0,
    #: точка описания относится к ближайшей линии своего слоя не дальше этого
    "point_to_feature_max_ft": 2.0,
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
    #: два контура участка в разных слоях (граница Шага 1 и участок геодезиста)
    #: считаются расходящимися, если их общая площадь меньше этой доли объединённой
    "property_mismatch_overlap": 0.85,
    "boundary_snap_tolerance_ft": 0.05,
    "boundary_gap_tolerance_ft": 2.0,
    "merge_tolerance_ft": 0.05,
    # Стыки линий: вершины ближе этого допуска друг к другу считаются одной точкой, а
    # вершина ближе допуска к чужому отрезку — лежащей на нём. В AutoCAD концы соседних
    # отрезков расходятся в 5–8-м знаке после запятой — без этого объект рвётся на куски.
    "vertex_snap_tolerance_ft": 0.005,
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
        "VINYL FENCE": ["V.FE.", "VIN.FE.", "VINYL FE.", "PVC FENCE"],
        # сокращение заказчик пишет двумя способами (в итоговых чертежах поровну)
        "V.FE.": ["VIN.FE.", "VINYL FE."],
        "WOOD FENCE": ["W.FE.", "WD. FENCE", "STOCKADE FENCE"],
        "IRON FENCE": ["I.FE.", "W.I. FENCE"],
        "UTILITY POLE": ["U.P.", "UTIL. POLE", "U. POLE"],
        "CATCH BASIN": ["C.B."],
        "MANHOLE": ["M.H."],
        "HYDRANT": ["HYD."],
        "CONC. WALK": ["CONCRETE WALK", "CONCRETE SIDEWALK", "CONC. SIDEWALK"],
        "DRIVEWAY": ["DRWY.", "D/W", "DRIVE"],
        "DRIVE": ["DRIVEWAY"],
        "FRAME DECK": ["WOOD DECK", "WD. DECK"],
        "O.P.": ["OPEN PORCH", "PORCH"],
        "PLANTER": ["PLANT."],
        "STEPS": ["STEP", "STAIRS"],
        "STEP": ["STEPS"],
    },
    # --- размещение подписей (acad_builder.py) -------------------------------
    "text_height_ft": None,
    "text_height_factor": 1.0,
    #: вертикальный текст: "auto" — по стрелке севера (лист держат севером
    #: вверх: стрелка смотрит вверх или влево — текст читается снизу вверх,
    #: поворот +90°; стрелка смотрит вправо — сверху вниз, −90°); "up" / "down" —
    #: всегда снизу вверх / всегда сверху вниз
    "vertical_text_reads": "auto",
    #: стрелка севера: вставка блока с таким именем или в таком слое
    "north_arrow_block_names": ["NA", "NORTH", "NORTH?ARROW*", "NORTHARROW*", "N?ARROW*", "NARROW*"],
    "north_arrow_layers": ["M-NTH-ARROW", "*NTH?ARROW*", "*NORTH?ARROW*", "*NORTHARROW*"],
    #: куда смотрит север в собственных координатах блока (90° = вдоль +Y блока)
    "north_arrow_block_north_deg": 90.0,
    #: стрелка «смотрит вправо», если отклонена от оси +X меньше чем на столько градусов
    "north_arrow_side_sector_deg": 45.0,
    #: после подписания перерисовать чертёж (как команда _REGENALL): типы линий
    #: заборов и новые подписи показываются сразу
    "regen_after_labeling": True,
    #: после подписания выключить слои полевых заметок (их заменили подписи)
    "hide_field_notes_after_labeling": True,
    "fence_min_extra_length_ft": 4.0,
    #: Открытое крыльцо, обведённое вместе со ступенями (ступени отделены линиями
    #: того же слоя): подпись ставится по центру самой площадки — наибольшей
    #: части контура, если она занимает не меньше этой доли его площади.
    "main_cell_categories": ["porch"],
    "main_cell_min_share": 0.5,
    #: Длинная ступень с изломом (два длинных участка, соединённых коротким) —
    #: правило заказчика: подпись у середины каждого длинного участка.
    #: Категории объектов, наименьшая длина участка и наибольшая ширина ленты, ft.
    "arm_label_categories": ["steps"],
    "arm_label_min_length_ft": 8.0,
    "arm_label_max_width_ft": 3.0,
    #: Подписи, которые пишутся вдоль направления движения (правило заказчика):
    #: ``DRIVEWAY`` — вдоль заезда, а не поперёк. Значение — короткая форма на
    #: случай, когда полное слово вдоль заезда не помещается (``DRIVE``).
    #: Направление движения — длинная сторона контура заезда; у почти квадратного
    #: контура (стороны отличаются меньше чем в ``travel_axis_min_ratio`` раз)
    #: направление не определяется и текст ставится как обычно.
    "travel_direction_labels": {"DRIVEWAY": "DRIVE"},
    "travel_axis_min_ratio": 1.25,
    #: Просвет между линией и текстом, написанным вдоль неё (правило заказчика), ft
    #: при масштабе 1"=16' — для других масштабов пропорционально высоте текста:
    #: забор 0.7, линия дома 0.7, граница участка 0.8 (чтобы подпись не сливалась с
    #: толстой линией при печати), остальные линии 0.55. Те же просветы подпись
    #: держит до соседних линий сбоку: между забором и границей в 1.8 ft она не
    #: встаёт, а переносится за забор. Пустой словарь — прежнее правило
    #: (``fence_text_offset_factor`` и общий ``line_clearance_factor``).
    "along_line_gaps_ft": {"fence": 0.7, "house": 0.7, "property": 0.8, "other": 0.55},
    #: расстояние от линии забора до середины текста, в высотах текста
    #: (действует, когда ``along_line_gaps_ft`` пуст)
    "fence_text_offset_factor": 1.1,
    "fence_end_margin_ft": 2.0,
    #: зазор между концом текста вдоль линии и поперечной линией, в высотах текста
    "fence_text_end_gap_factor": 0.15,
    #: подпись ставится на каждом прямом участке забора не короче этой длины
    "fence_label_every_run_min_ft": 25.0,
    #: одинаковые подписи заборов не ставятся ближе этого расстояния
    "fence_label_spacing_ft": 15.0,
    #: участки забора короче этой длины не подписываются
    "fence_min_label_length_ft": 3.0,
    "straight_angle_tolerance_deg": 2.0,
    "text_clearance_factor": 0.3,
    "line_clearance_factor": 0.3,
    "inside_clearance_factor": 0.3,
    "mtext_line_spacing_factor": 1.67,
    #: Ширина символов шрифта подписей в высотах текста (чертёжный SHX-шрифт
    #: заказчика): буква/цифра, пробел, узкий знак. В AutoCAD эти значения
    #: уточняются измерением пробного текста (measure_text_in_autocad).
    "char_width_factor": 0.92,
    "space_width_factor": 1.0,
    "narrow_char_width_factor": 0.48,
    #: поля первого и последнего символа, не входящие в габарит текста
    "text_bearing_factor": 0.26,
    "narrow_chars": ".,'`:;!|",
    "measure_text_in_autocad": True,
    #: короткие заборы целиком за пределами участка (возвраты и продолжения
    #: соседских заборов) подписываются сокращением «C.L.F.», как в итоговом
    #: чертеже заказчика (false — не подписывать)
    "label_short_fences_outside_lot": True,
    #: полное название («CHAIN LINK FENCE») пишется на прямом участке, который
    #: длиннее подписи не меньше чем в столько раз; на более коротком —
    #: сокращение («C.L.F.»)
    "fence_full_label_ratio": 2.0,
    #: Соседский забор — тот, что больше чем наполовину идёт за линией участка
    #: (дальше fence_on_line_tolerance_ft). Сокращение ставится на каждом его
    #: прямом участке, включая отводы (у своего забора — только на самом
    #: длинном). Одинаковые сокращения — не ближе fence_abbreviation_spacing_ft.
    "fence_on_line_tolerance_ft": 0.15,
    "fence_abbreviation_spacing_ft": 4.0,
    #: прямой участок своего забора, уходящий за границу участка (вдоль улицы
    #: или к соседу), подписывается сокращением отдельно
    "fence_label_runs_beyond_lot": True,
    #: категории, у которых подпись вдоль кромки ставится с внешней стороны
    #: (клумба — полоса между своей кромкой и забором у границы участка)
    "outward_label_categories": ["planter", "party_wall"],
    "fence_near_lot_ft": 2.0,
    #: заметка о покрытии снимает подпись по слою у такого же покрытия в этом радиусе
    "field_note_surface_radius_ft": 15.0,
    #: "spline" — объект Leader, как у заказчика (QLEADER: сплайн со стрелкой,
    #: две точки, MText привязан к выноске; размерный стиль — текущий);
    #: "leader" — объект Leader из прямых отрезков с полкой;
    #: "arc" — дуга полилинией с клиновидной стрелкой (не объект Leader).
    #: Если AutoCAD не создаёт Leader, чертится полилиния.
    "leader_style": "spline",
    "leader_arc_bulge": 0.25,
    #: Наименьшая длина выноски, в высотах текста. AutoCAD рисует стрелку объекта
    #: Leader, только если выноска длиннее двух стрелок (2 x 1.2 = 2.4 ft при
    #: 1"=16'); у заказчика самая короткая из 42 выносок — 2.50 ft. 2.8 x 0.9 = 2.52 ft.
    "leader_min_length_factor": 2.8,
    "leader_max_length_factor": 12.0,
    "leader_landing_factor": 1.0,
    #: зазор между концом выноски и текстом (у заказчика 0.30 ft при тексте 0.9 ft)
    "leader_text_gap_factor": 0.333,
    #: стрелка-клин полилинии, в высотах текста
    "leader_arrow_size_factor": 0.8,
    #: стрелка объекта Leader берётся из размерного стиля чертежа (у заказчика
    #: CURB-DIM 1-16: 1.2 ft при тексте 0.9 ft); если она меньше 0.6 или больше
    #: 2.2 высоты текста — ставится такая, в высотах текста
    "leader_object_arrow_factor": 1.333,
    "leader_attach_annotation": True,
    #: Текст над (под) концом выноски вместо текста сбоку — запасной вариант:
    #: у заказчика текст всегда сбоку. Штраф в «высотах текста длины выноски».
    "leader_stacked_penalty": 2.0,
    #: выноска короче этого числа стрелок осталась бы без стрелки — стрелка такой
    #: выноски уменьшается (страховка для чужого размерного стиля)
    "leader_min_length_arrows": 2.1,
    #: текстовый стиль подписей: стиль чертежа заказчика, если он есть; если
    #: нет — стиль программы (drawing.text_style, шрифт romans.shx)
    "label_text_style": "ROMANS",
    #: Чем создавать подписи объектов: ``"text"`` — однострочным TEXT по центру
    #: (стабильно в чужих CAD: в Carlson Survey MText с привязкой middle-center
    #: встаёт по левой линии и «разъезжается»), ``"mtext"`` — всё MText (до 3.7),
    #: ``"auto"`` — вдоль линии TEXT, остальное MText. Описание дома (свой слой и
    #: стиль RDS) и текст выносок остаются MText в любом случае: аннотацией
    #: объекта Leader может быть только MText.
    "label_text_kind": "text",
    #: забор перед красной линией в створе участка (палисадник, приямок) — свой:
    #: одна подпись на длинной стороне, со стороны улицы; глубина полосы, ft
    "front_yard_depth_ft": 15.0,
    # --- масштаб типа линий (LinetypeScale) для линий заборов -----------------
    #: Правила заказчика по слоям. ``fixed`` — масштаб типа линий, с которым
    #: забор чертится всегда (он же — для отрезков, не попавших под особые
    #: условия); ``divisor`` — во сколько раз узор этого слоя мельче забора
    #: (у S-FENCE-RAIL крестики нарисованы в 8.06 раза чаще, поэтому его
    #: значения — это значения S-FENCE, делённые на 8.06).
    "linetype_scale_rules": {
        "S-FENCE": {"fixed": 5.0, "divisor": 1.0},
        "S-FENCE-RAIL": {"fixed": 0.62, "divisor": 8.06},
    },
    #: отрезок короче этого — масштаб равен его длине: иначе на нём не видно ни
    #: одного крестика. Округление вниз до одной сотой.
    "linetype_scale_short_below_ft": 5.0,
    #: отрезок в этом промежутке (включительно) — половина его длины: при
    #: масштабе 5 на нём видно одно перекрестие, а просятся два.
    "linetype_scale_halve_from_ft": 8.0,
    "linetype_scale_halve_to_ft": 10.0,
    #: Особые условия даны для масштаба чертежа 1"=16' (``drawing.base_scale``);
    #: для другого масштаба масштаб типа линий умножается на scale / 16 —
    #: так же, как высоты текста.
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


# ---------------------------------------------------------------------------
# Масштаб типа линий забора (LinetypeScale по каждому отрезку)
# ---------------------------------------------------------------------------

def _segments_word(count: int) -> str:
    """«у 1 отрезка / у 2 отрезков / у 21 отрезка» — родительный падеж для отчёта."""
    return "отрезка" if count % 10 == 1 and count % 100 != 11 else "отрезков"


def _floor_hundredths(value: float) -> float:
    """Округление ВНИЗ до одной сотой (как просит заказчик: 4.999 → 4.99)."""
    return math.floor(float(value) * 100.0 + 1e-9) / 100.0


def fence_linetype_rule(layer: str, fcfg: dict[str, Any]) -> dict[str, Any] | None:
    """Правило слоя из ``linetype_scale_rules`` (точное имя или шаблон ``*``), иначе ``None``."""
    rules = fcfg.get("linetype_scale_rules") or {}
    if not isinstance(rules, dict) or not layer:
        return None
    if layer in rules and isinstance(rules[layer], dict):
        return rules[layer]
    upper = layer.upper()
    for name, rule in rules.items():
        if isinstance(rule, dict) and str(name).upper() == upper:
            return rule
    for name, rule in rules.items():
        if isinstance(rule, dict) and layer_matches(layer, [name]):
            return rule
    return None


def fence_linetype_scale(
    length_ft: float,
    rule: dict[str, Any],
    fcfg: dict[str, Any],
    *,
    scale: float | None = None,
    base_scale: float | None = None,
) -> tuple[float, str]:
    """
    Масштаб типа линий для одного отрезка забора: ``(значение, почему)``.

    Правило заказчика (масштаб 1"=16'):

    * отрезок короче 5 ft — масштаб равен длине (вниз до 0.01): при масштабе 5
      крестиков на нём не видно вообще;
    * отрезок от 8 до 10 ft — половина длины: при масштабе 5 видно одно
      перекрестие, а для забора просятся два;
    * остальные — масштаб слоя по умолчанию (``fixed``: 5 у S-FENCE, 0.62 у
      S-FENCE-RAIL, где узор мельче в 8.06 раза).

    Для слоя с ``divisor`` > 1 (S-FENCE-RAIL) посчитанное значение делится на
    него и снова округляется вниз до сотой: «те же условия, но результат
    разделён на 8.06» (4.5 ft → 4.5 / 8.06 = 0.5583 → 0.55). Для другого
    масштаба чертежа всё умножается на ``scale / base_scale``.
    """
    fixed = float(rule.get("fixed", 0.0) or 0.0)
    divisor = float(rule.get("divisor", 1.0) or 1.0)
    if divisor <= 0:
        divisor = 1.0
    short = float(fcfg.get("linetype_scale_short_below_ft", 5.0) or 0.0)
    halve_from = float(fcfg.get("linetype_scale_halve_from_ft", 8.0) or 0.0)
    halve_to = float(fcfg.get("linetype_scale_halve_to_ft", 10.0) or 0.0)
    length = max(0.0, float(length_ft))
    if short > 0 and length < short:
        value, why = _floor_hundredths(length) / divisor, "short"
    elif halve_from > 0 and halve_from <= length <= halve_to:
        value, why = _floor_hundredths(length / 2.0) / divisor, "halved"
    else:
        value, why = fixed, "fixed"
    if why != "fixed" and divisor > 1:
        # «результат разделён на 8.06» — и тоже вниз до сотой (ответ заказчика):
        # 4.5 ft на S-FENCE-RAIL → 4.5 / 8.06 = 0.5583 → 0.55.
        value = _floor_hundredths(value)
    factor = 1.0
    if scale and base_scale and float(base_scale) > 0:
        factor = float(scale) / float(base_scale)
    return round(value * factor, 4), why


@dataclass
class FenceLinetypeItem:
    """Один отрезок забора и масштаб типа линий, который ему положен."""

    layer: str
    length_ft: float
    value: float
    why: str  # "short" | "halved" | "fixed"


@dataclass
class FenceLinetypePlan:
    """Что программа сделает с масштабом типа линий заборов (или уже сделала)."""

    items: list[FenceLinetypeItem] = field(default_factory=list)
    #: отрезки, которым масштаб не понадобился: уже стоит нужный
    unchanged: int = 0
    #: объекты слоя, длину которых прочитать не удалось
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    scale: float = 16.0
    base_scale: float = 16.0

    @property
    def changed(self) -> int:
        """Сколько отрезков получат новый масштаб (в пробном расчёте — получат)."""
        return len(self.items)

    @property
    def factor(self) -> float:
        """Коэффициент масштаба чертежа против 1"=16' (для других масштабов)."""
        return self.scale / self.base_scale if self.base_scale > 0 else 1.0

    def by_layer(self) -> dict[str, list[FenceLinetypeItem]]:
        found: dict[str, list[FenceLinetypeItem]] = {}
        for item in self.items:
            found.setdefault(item.layer, []).append(item)
        return found

    def count(self, layer: str, why: str) -> int:
        return sum(1 for item in self.items if item.layer == layer and item.why == why)

    def has_work(self) -> bool:
        return bool(self.items or self.failed)

    def summary(self) -> str:
        """Строка для статуса и журнала: «S-FENCE: 6 из 24 отрезков»."""
        if not self.items:
            return "масштаб типа линий заборов менять не нужно"
        parts = [
            f"{layer}: {len(items)} из {len(items) + self.unchanged}"
            for layer, items in sorted(self.by_layer().items())
        ]
        return "Масштаб типа линий заборов — " + ", ".join(parts) + " отрезков"

    def _examples(self, items: Sequence[FenceLinetypeItem], limit: int = 3) -> str:
        shown = [
            f"{round(item.length_ft, 3):g}' → {item.value:g}" for item in items[:limit]
        ]
        rest = len(items) - len(shown)
        return ", ".join(shown) + (f" и ещё {rest}" if rest > 0 else "")

    def lines(self, *, dry_run: bool = False) -> list[str]:
        """Подробные строки отчёта Шага 2 (по слоям и условиям правила)."""
        if not self.items and not self.failed and not self.skipped:
            if self.unchanged:
                return [
                    "МАСШТАБ ТИПА ЛИНИЙ ЗАБОРОВ: уже стоит как нужно у "
                    f"{self.unchanged} {_segments_word(self.unchanged)} "
                    "(правила заказчика по длине отрезка)"
                ]
            return []
        verb = "будет изменён" if dry_run else "изменён"
        out = [
            "МАСШТАБ ТИПА ЛИНИЙ ЗАБОРОВ (LinetypeScale у каждого отрезка): короче "
            "5 ft — равен длине отрезка, 8–10 ft — половина длины, остальные — "
            "масштаб слоя по умолчанию; всё вниз до 0.01"
            + (
                f"; правила даны для 1\"=16', рисунок ×{self.factor:g} под этот масштаб"
                if abs(self.factor - 1.0) > 1e-9 else ""
            )
        ]
        for layer, items in sorted(self.by_layer().items()):
            counts: list[str] = []
            for why, title in (("short", "короче 5 ft"), ("halved", "8–10 ft")):
                group = [item for item in items if item.why == why]
                if group:
                    counts.append(f"{title}: {len(group)} ({self._examples(group)})")
            fixed = [item for item in items if item.why == "fixed"]
            if fixed:
                counts.append(f"по умолчанию: {len(fixed)} ({self._examples(fixed)})")
            text = f"  • слой {layer}: {verb} у {len(items)} {_segments_word(len(items))}"
            if counts:
                text += " — " + "; ".join(counts)
            if self.unchanged:
                text += f"; уже стоял нужный у {self.unchanged}"
            out.append(text)
        if self.skipped:
            out.append(
                f"  • не удалось прочитать длину объектов слоя забора: {self.skipped} "
                "(масштаб типа линий у них не тронут)"
            )
        for message in self.failed:
            out.append(f"  • ОШИБКА: {message}")
        return out


def fence_linetype_plan(
    segments: Iterable[tuple[str, float]],
    cfg: dict[str, Any] | None,
    *,
    scale: float | None = None,
) -> FenceLinetypePlan:
    """
    Пробный расчёт масштаба типа линий заборов по снимку чертежа.

    ``segments`` — пары ``(слой, длина отрезка в футах)``; для полилинии
    передаётся длина её самого короткого сегмента: у полилинии масштаб типа
    линий один на весь объект, и по короткому сегменту крестик будет виден на
    всех.
    """
    fcfg = feature_settings(cfg)
    drawing = (cfg or {}).get("drawing", {}) if isinstance(cfg, dict) else {}
    base_scale = float(drawing.get("base_scale", 16.0) or 16.0) if isinstance(drawing, dict) else 16.0
    plan = FenceLinetypePlan(scale=float(scale or base_scale), base_scale=base_scale)
    for layer, length in segments:
        rule = fence_linetype_rule(layer, fcfg)
        if rule is None:
            continue
        value, why = fence_linetype_scale(
            length, rule, fcfg, scale=scale, base_scale=base_scale
        )
        plan.items.append(FenceLinetypeItem(layer=layer, length_ft=float(length), value=value, why=why))
    return plan


def _north_settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Готовые настройки Шага 2 не копируются заново (вызов на каждую вставку блока)."""
    if isinstance(cfg, dict) and "north_arrow_block_names" in cfg and "vertical_text_reads" in cfg:
        return cfg
    return feature_settings(cfg)


def is_north_arrow_block(name: str, layer: str, cfg: dict[str, Any] | None) -> bool:
    """Вставка блока — стрелка севера (по имени блока или по слою)."""
    fcfg = _north_settings(cfg)
    return layer_matches(name, fcfg.get("north_arrow_block_names")) or layer_matches(
        layer, fcfg.get("north_arrow_layers")
    )


def find_north_arrow(snapshot: "DrawingSnapshot", cfg: dict[str, Any] | None = None) -> "NorthArrow | None":
    """
    Стрелка севера среди вставок блоков чертежа (``snapshot.symbols``).

    Блок, найденный по имени, важнее найденного только по слою. Направление —
    поворот вставки плюс направление севера в самом блоке
    (``north_arrow_block_north_deg``), с учётом зеркальной вставки.
    """
    fcfg = _north_settings(cfg)
    names = fcfg.get("north_arrow_block_names")
    layers = fcfg.get("north_arrow_layers")
    by_name = [s for s in snapshot.symbols if layer_matches(s.name, names)]
    by_layer = [s for s in snapshot.symbols if layer_matches(s.layer, layers)]
    both = [s for s in by_name if s in by_layer]
    found = both or by_name or by_layer
    if not found:
        return None
    symbol = found[0]
    try:
        own = math.radians(float(fcfg.get("north_arrow_block_north_deg")))
    except (TypeError, ValueError):
        own = math.pi / 2.0
    x_sign = -1.0 if symbol.x_scale < 0 else 1.0
    y_sign = -1.0 if symbol.y_scale < 0 else 1.0
    local = math.atan2(y_sign * math.sin(own), x_sign * math.cos(own))
    north = math.degrees(float(symbol.rotation) + local) % 360.0
    try:
        sector = float(fcfg.get("north_arrow_side_sector_deg"))
    except (TypeError, ValueError):
        sector = 45.0
    sector = min(max(sector, 0.0), 90.0)

    def off(axis_deg: float) -> float:
        return abs((north - axis_deg + 180.0) % 360.0 - 180.0)

    if off(0.0) < sector:
        side = "right"
    elif off(180.0) < sector:
        side = "left"
    elif off(270.0) <= 90.0 - sector and off(270.0) < off(90.0):
        side = "down"
    else:
        side = "up"
    return NorthArrow(layer=symbol.layer, name=symbol.name, point=symbol.point, north_deg=north, side=side)


def vertical_text_direction(
    snapshot: "DrawingSnapshot", cfg: dict[str, Any] | None = None
) -> tuple[str, str]:
    """
    Как читается вертикальный текст: ``("up" | "down", пояснение для отчёта)``.

    Готовый лист держат стрелкой севера вверх. Если на экране стрелка смотрит
    вправо, лист поворачивают против часовой стрелки — и вертикальная подпись
    читается слева направо, только когда на экране она написана сверху вниз.
    В остальных случаях (стрелка вверх или влево, стрелки нет) — снизу вверх.
    """
    fcfg = _north_settings(cfg)
    setting = str(fcfg.get("vertical_text_reads") or "auto").strip().lower()
    if setting in {"up", "down"}:
        how = "снизу вверх" if setting == "up" else "сверху вниз"
        return setting, f"вертикальные подписи читаются {how} (настройка vertical_text_reads: \"{setting}\")"
    arrow = find_north_arrow(snapshot, fcfg)
    if arrow is None:
        return "up", (
            "стрелка севера не найдена (блок NA, слой M-NTH-ARROW) — "
            "вертикальные подписи читаются снизу вверх"
        )
    words = {"up": "вверх", "right": "вправо", "left": "влево", "down": "вниз"}
    turn = arrow.clockwise_from_up_deg
    if abs(turn) < 0.5:
        tilt = "ровно вверх"
    else:
        tilt = f"{abs(turn):.0f}° {'по часовой' if turn > 0 else 'против часовой'} от верха листа"
    reads = "down" if arrow.side == "right" else "up"
    how = "снизу вверх" if reads == "up" else "сверху вниз"
    tail = ""
    if arrow.side == "right":
        tail = ": лист читают, повернув севером вверх"
    elif arrow.side == "down":
        tail = " (стрелка вниз — проверьте поворот чертежа)"
    return reads, (
        f"стрелка севера (блок {arrow.name}, слой {arrow.layer}) смотрит {words[arrow.side]}, {tilt} — "
        f"вертикальные подписи читаются {how}{tail}"
    )


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
        self._code_linework = _as_patterns(fcfg.get("code_linework_layers"))
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
            if self.is_tread(layer):
                return None  # линии проступей — не контур ступеней
            for category, patterns in self._structures:
                if layer_matches(layer, patterns):
                    return category
            return None

        return self._cached("struct", layer, compute)

    def is_code_linework(self, layer: str) -> bool:
        """Линии между снятыми точками: без кода забора подписи не требуют."""
        return self._cached("crew", layer, lambda: layer_matches(layer, self._code_linework))

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
            if self.is_property(layer) or layer_matches(layer, self._text_obstacle):
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
class SurveyPoint:
    """
    Точка съёмки: вставка блока с атрибутами «номер / отметка / описание».

    ``layer`` — слой вставки блока: геодезист кладёт точку в слой своего объекта
    (``S-FENCE``, ``ST-MISC``…), сами атрибуты обычно лежат в замороженных слоях.
    """

    layer: str
    number: str
    description: str
    point: tuple[float, float]
    elevation: str = ""
    block: str = ""


@dataclass
class DrawingSymbol:
    """Условный знак: вставка блока без атрибутов (столб ``WUP``)."""

    layer: str
    name: str
    point: tuple[float, float]
    #: поворот вставки, радианы, и знаки масштабов (зеркальная вставка)
    rotation: float = 0.0
    x_scale: float = 1.0
    y_scale: float = 1.0


@dataclass
class NorthArrow:
    """Стрелка севера чертежа и вывод из неё для вертикального текста."""

    layer: str
    name: str
    point: tuple[float, float]
    #: направление на север: градусы от оси +X против часовой стрелки, [0, 360)
    north_deg: float
    #: куда смотрит стрелка на экране: "up" / "right" / "down" / "left"
    side: str

    @property
    def clockwise_from_up_deg(self) -> float:
        """Отклонение стрелки от верха листа по часовой стрелке, (−180, 180]."""
        value = (90.0 - self.north_deg) % 360.0
        return value - 360.0 if value > 180.0 else value


#: Версия программы (``version.py``) с датой: печатается в отчёте Шага 2, чтобы
#: было видно, какая сборка работала. Тот же номер стоит в углу окна.
STEP2_VERSION = f"{APP_VERSION} ({APP_DATE})"


@dataclass
class DrawingSnapshot:
    """Всё, что прочитано из ModelSpace за один проход."""

    lines: list[DrawingLine] = field(default_factory=list)
    texts: list[DrawingText] = field(default_factory=list)
    survey_points: list[SurveyPoint] = field(default_factory=list)
    symbols: list["DrawingSymbol"] = field(default_factory=list)
    leaders: list[DrawingLine] = field(default_factory=list)
    entity_count: int = 0
    skipped_hidden: int = 0
    read_errors: int = 0
    notes: list[str] = field(default_factory=list)
    #: блоки с атрибутами, в которых не нашлось описания точки, и их теги
    blocks_without_description: int = 0
    block_tags: dict[str, int] = field(default_factory=dict)
    #: допуск, с которым вершины линий уже стянуты (``snap_snapshot_vertices``), и их число
    vertex_snap_ft: float = 0.0
    vertices_snapped: int = 0


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


_DESCRIPTION_SPLIT_RE = re.compile(r"[^A-Z0-9*?]+")


def normalize_description(text: str) -> str:
    """
    Описание точки съёмки в виде для сравнения: верхний регистр, знаки и
    лишние пробелы убраны (``'conc-sw'`` -> ``'CONC SW'``,
    ``'SPOT GRADE PAV.'`` -> ``'SPOT GRADE PAV'``).
    """
    upper = plain_text(text).upper()
    return " ".join(part for part in _DESCRIPTION_SPLIT_RE.split(upper) if part)


def translate_description(text: str, rules: Any) -> tuple[str, str, str] | None:
    """
    Перевод описания точки по ``point_descriptions``.

    Возвращает ``(подпись, категория, сработавший шаблон)`` первого подходящего
    правила; пустая подпись — «точка известна, подпись не нужна». ``None`` —
    правила для описания нет.
    """
    found = find_description_rule(text, rules)
    if found is None:
        return None
    rule, pattern = found
    label = str(rule.get("label") or "").strip()
    return label, str(rule.get("category") or "").strip().lower(), pattern


_LINE_NUMBER_RE = re.compile(r"\s*\d+$")


def find_description_rule(text: str, rules: Any) -> tuple[dict[str, Any], str] | None:
    """
    Правило ``point_descriptions`` для описания точки: ``(правило, шаблон)``.

    Если целиком описание не подошло, отбрасывается номер линии Carlson в
    конце (``SHED1``, ``F E C 2``): он говорит, какие точки соединять, а не
    что это за объект.
    """
    key = normalize_description(text)
    if not key or not isinstance(rules, (list, tuple)):
        return None
    keys = [key]
    stripped = _LINE_NUMBER_RE.sub("", key)
    if stripped and stripped != key:
        keys.append(stripped)
    for candidate in keys:
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            for pattern in _as_patterns(rule.get("match")):
                if _wild_regex(normalize_description(pattern)).match(candidate):
                    return rule, pattern
    return None


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
    wanted_tokens = label_tokens(label)
    if ("GATE" in existing) != ("GATE" in wanted_tokens):
        return False
    # «C.L.F. ATOP WALL» — подпись забора на стене, а не соседней стены «WALL».
    if ("ATOP" in existing) != ("ATOP" in wanted_tokens):
        return False
    if "ATOP" in existing:
        # Забор на стене: сравнивается забор; материал стены заказчик пишет по
        # фотографии («MASONRY WALL», «CONC. WALL», «C.WALL») — он может не совпасть.
        cut_existing, cut_wanted = existing.index("ATOP"), wanted_tokens.index("ATOP")
        walls = [
            any(token.endswith("WALL") for token in tokens[cut + 1:])
            for tokens, cut in ((existing, cut_existing), (wanted_tokens, cut_wanted))
        ]
        if all(walls) and cut_existing > 0 and cut_wanted > 0:
            fences = (" ".join(existing[:cut_existing]), " ".join(wanted_tokens[:cut_wanted]))
            # В обе стороны: на чертеже полное название, а новая подпись — сокращение
            # («CHAIN LINK FENCE ATOP MASONRY WALL» и «C.L.F. ATOP CONC. WALL»), и наоборот.
            if labels_similar(fences[0], fences[1], synonyms) or labels_similar(
                fences[1], fences[0], synonyms
            ):
                return True
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
    #: тип объекта из правила ``point_descriptions`` (shed, steps, pool…)
    category: str = ""
    #: точка — блок с атрибутами (а не текст «64 FEV» слоя POINT_LABELS)
    from_block: bool = False
    #: подпись для узкого объекта (``label_narrow`` правила): STEPS -> STEP
    narrow_label: str = ""

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


def snap_snapshot_vertices(snapshot: "DrawingSnapshot", cfg: dict[str, Any] | None = None) -> int:
    """
    Сводит стыки линий чертежа в общие точки (допуск ``vertex_snap_tolerance_ft``)
    и возвращает число исправленных стыков.

    В настоящем чертеже конец отрезка совпадает с концом соседнего или лежит на
    соседней линии не «точка в точку», а с точностью до 5–8-го знака. Для
    ``linemerge`` / ``polygonize`` это не стык: забор или стенка клумбы
    распадаются на куски, а результат зависит от последних знаков координат.

    1. Вершины ближе допуска друг к другу становятся одной точкой.
    2. Вершина, лежащая на чужом отрезке (ближе допуска, но не у его концов),
       переносится на этот отрезок и добавляется в него: Т-образный стык
       получает общую точку, отрезок остаётся прямым.

    Вершины границы участка (``property_line_layers``, ``property_closing_layers``)
    не сдвигаются: к ним подтягиваются остальные линии. Линии снимка заменяются
    на месте; повторный вызов ничего не меняет.
    """
    fcfg = feature_settings(cfg)
    tolerance = float(fcfg.get("vertex_snap_tolerance_ft") or 0.0)
    if tolerance <= 0.0 or snapshot.vertex_snap_ft >= tolerance:
        return 0
    classifier = LayerClassifier(fcfg)
    closing_layers = _as_patterns(fcfg.get("property_closing_layers"))

    def is_fixed(layer: str) -> bool:
        return classifier.is_property(layer) or layer_matches(layer, closing_layers)

    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    moved = 0

    def snap(point: tuple[float, float]) -> tuple[float, float]:
        nonlocal moved
        gx, gy = math.floor(point[0] / tolerance), math.floor(point[1] / tolerance)
        best: tuple[float, float] | None = None
        best_distance = tolerance
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for anchor in grid.get((gx + dx, gy + dy), ()):
                    distance = math.hypot(anchor[0] - point[0], anchor[1] - point[1])
                    if distance <= best_distance:
                        best, best_distance = anchor, distance
        if best is None:
            grid.setdefault((gx, gy), []).append(point)
            return point
        if best != point:
            moved += 1
        return best

    # --- 1. общие вершины (сначала граница участка: её точки — опорные) --------------
    fixed_flags = [is_fixed(line.layer) for line in snapshot.lines]
    order = sorted(range(len(snapshot.lines)), key=lambda index: (not fixed_flags[index], index))
    snapped: dict[int, list[tuple[float, float]]] = {}
    fixed_points: set[tuple[float, float]] = set()
    for index in order:
        line = snapshot.lines[index]
        points: list[tuple[float, float]] = []
        for x, y, *_rest in line.points:
            point = snap((float(x), float(y)))
            if not points or point != points[-1]:
                points.append(point)
        if line.closed and len(points) > 2 and points[0] == points[-1]:
            points.pop()
        if len(points) < 2:
            continue  # отрезок короче допуска: линии здесь нет
        snapped[index] = points
        if fixed_flags[index]:
            fixed_points.update(points)

    # --- 2. Т-образные стыки ----------------------------------------------------
    segments: list[tuple[int, int, tuple[float, float], tuple[float, float]]] = []
    for index, points in snapped.items():
        closed = snapshot.lines[index].closed and len(points) > 2
        for position in range(len(points) if closed else len(points) - 1):
            segments.append((index, position, points[position], points[(position + 1) % len(points)]))
    inserts: dict[tuple[int, int], list[tuple[float, tuple[float, float]]]] = {}
    relocated: dict[tuple[float, float], tuple[float, float]] = {}
    if segments:
        tree = STRtree([LineString([start, end]) for _line, _position, start, end in segments])
        for vertex in sorted(point for anchors in grid.values() for point in anchors):
            area = shapely_box(
                vertex[0] - tolerance, vertex[1] - tolerance,
                vertex[0] + tolerance, vertex[1] + tolerance,
            )
            best_hit: tuple[float, int, float, tuple[float, float]] | None = None
            for found in sorted(int(value) for value in tree.query(area)):
                _line, _position, start, end = segments[found]
                if vertex == start or vertex == end:
                    continue
                dx, dy = end[0] - start[0], end[1] - start[1]
                length_sq = dx * dx + dy * dy
                if length_sq <= 0.0:
                    continue
                along = ((vertex[0] - start[0]) * dx + (vertex[1] - start[1]) * dy) / length_sq
                if not 0.0 < along < 1.0:
                    continue
                foot = (start[0] + along * dx, start[1] + along * dy)
                offset = math.hypot(foot[0] - vertex[0], foot[1] - vertex[1])
                if offset > tolerance:
                    continue
                # Вершина ложится на все отрезки, рядом с которыми она стоит (например,
                # на две линии, начерченные одна поверх другой); переносится — на ближайший.
                inserts.setdefault((segments[found][0], segments[found][1]), []).append((along, vertex))
                if best_hit is None or offset < best_hit[0]:
                    best_hit = (offset, found, along, foot)
            if best_hit is None:
                continue
            target_fixed = fixed_flags[segments[best_hit[1]][0]]
            if vertex in fixed_points and not target_fixed:
                continue  # точка границы участка остаётся на месте: изгибается чужая линия
            relocated[vertex] = best_hit[3]

    lines: list[DrawingLine] = []
    for index, line in enumerate(snapshot.lines):
        points = snapped.get(index)
        if points is None:
            continue
        joined: list[tuple[float, float]] = []
        for position, point in enumerate(points):
            for candidate in [point] + [
                vertex for _along, vertex in sorted(set(inserts.get((index, position), ())))
            ]:
                candidate = relocated.get(candidate, candidate)
                if not joined or candidate != joined[-1]:
                    joined.append(candidate)
        if line.closed and len(joined) > 2 and joined[0] == joined[-1]:
            joined.pop()
        if len(joined) < 2:
            continue
        lines.append(line if tuple(joined) == tuple(line.points) else replace(line, points=tuple(joined)))
    moved += sum(len(found) for found in inserts.values())
    snapshot.lines[:] = lines
    snapshot.vertex_snap_ft = tolerance
    snapshot.vertices_snapped += moved
    return moved


def _noded(lines: Sequence[LineString]) -> list[LineString]:
    """Разрезает линии во всех взаимных пересечениях и убирает дубликаты."""
    if not lines:
        return []
    return _line_parts(unary_union(list(lines)))


def merge_lines(
    lines: Sequence[LineString], tolerance: float, through_turn_deg: float = 0.0
) -> list[LineString]:
    """
    Объединяет смежные сегменты в цельные ломаные (``shapely.ops.linemerge``).

    Сегменты одного забора, начерченные отдельными отрезками, становятся одной
    линией; в тройных стыках линии остаются раздельными. Если задан
    ``through_turn_deg``, в стыке трёх и более линий две, идущие «напрямую»
    (поворот не больше этого угла), остаются одной линией: забор, к которому
    сбоку примыкает другой, не рвётся в месте примыкания.
    """
    noded = _noded(snap_endpoints(lines, tolerance))
    if not noded:
        return []
    if len(noded) == 1:
        return list(noded)
    if through_turn_deg > 0.0:
        noded = _join_through(noded, through_turn_deg)
        if len(noded) == 1:
            return list(noded)
    return _line_parts(linemerge(noded))


def _join_through(noded: Sequence[LineString], max_turn_deg: float) -> list[LineString]:
    """
    В узлах, где сходятся три и более линий, соединяет пары, продолжающие друг
    друга (поворот не больше ``max_turn_deg``; сначала самые прямые пары).
    """
    pieces: list[list[tuple[float, float]] | None] = []
    for line in noded:
        coords = [(float(x), float(y)) for x, y, *_rest in line.coords]
        pieces.append(coords if len(coords) >= 2 else None)
    limit = math.cos(math.radians(max_turn_deg))
    while True:
        nodes: dict[tuple[float, float], list[tuple[int, bool]]] = {}
        for index, coords in enumerate(pieces):
            if coords is None or coords[0] == coords[-1]:
                continue  # замкнутое кольцо в стыках не участвует
            nodes.setdefault(coords[0], []).append((index, True))
            nodes.setdefault(coords[-1], []).append((index, False))
        best: tuple[float, int, bool, int, bool] | None = None
        for node in sorted(nodes):
            ends = nodes[node]
            if len(ends) < 3:
                continue
            directions = []
            for index, at_start in ends:
                coords = pieces[index]
                inner = coords[1] if at_start else coords[-2]
                length = math.hypot(inner[0] - node[0], inner[1] - node[1])
                directions.append(
                    ((inner[0] - node[0]) / length, (inner[1] - node[1]) / length) if length > 0.0 else None
                )
            for first in range(len(ends)):
                for second in range(first + 1, len(ends)):
                    if ends[first][0] == ends[second][0]:
                        continue
                    a, b = directions[first], directions[second]
                    if a is None or b is None:
                        continue
                    straight = -(a[0] * b[0] + a[1] * b[1])  # 1.0 — точно напрямую
                    if straight >= limit and (best is None or straight > best[0] + 1e-12):
                        best = (straight, ends[first][0], ends[first][1], ends[second][0], ends[second][1])
        if best is None:
            break
        _straight, first, first_at_start, second, second_at_start = best
        head = pieces[first] if not first_at_start else list(reversed(pieces[first]))
        tail = pieces[second] if second_at_start else list(reversed(pieces[second]))
        pieces[first] = head + tail[1:]
        pieces[second] = None
    return [LineString(coords) for coords in pieces if coords is not None]


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
        """Число углов участка: точки примыкания чужих линий на прямой стороне не считаются."""
        corners = self.polygon.simplify(0.001)
        if corners.geom_type != "Polygon" or corners.is_empty:
            corners = self.polygon
        return max(0, len(corners.exterior.coords) - 1)


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


def _boundary_mismatch_note(
    chosen: PropertyBoundary, other: PropertyBoundary, limit: float
) -> str:
    """
    Предупреждение, если два контура участка из разных слоёв расходятся:
    общая площадь меньше ``limit`` от объединённой (IoU). Пусто — совпадают.
    """
    if limit <= 0.0:
        return ""
    try:
        union = chosen.polygon.union(other.polygon).area
        common = chosen.polygon.intersection(other.polygon).area
    except Exception:  # noqa: BLE001 - некорректная геометрия исходного чертежа
        return ""
    if union <= 0.0 or common / union >= limit:
        return ""
    chosen_area = f"{chosen.area_sqft:,.0f}".replace(",", " ")
    other_area = f"{other.area_sqft:,.0f}".replace(",", " ")
    return (
        f"ВНИМАНИЕ: граница слоя {chosen.layer} ({chosen_area} sq ft) расходится с "
        f"участком слоя {other.layer} ({other_area} sq ft): совпадают на "
        f"{100.0 * common / union:.0f} %. Подписи рассчитаны по {chosen.layer}; проверьте "
        f"посадку этой границы. Если верен контур {other.layer}, запустите Шаг 2 в режиме "
        "«указать границу в AutoCAD» и выберите его линии"
    )


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
    snap_snapshot_vertices(snapshot, cfg)
    classifier = LayerClassifier(fcfg)
    by_rank: dict[int, dict[str, list[LineString]]] = {}
    closing_layers = _as_patterns(fcfg.get("property_closing_layers"))
    closers: dict[str, list[LineString]] = {}
    for line in snapshot.lines:
        rank = classifier.property_rank(line.layer)
        if rank is None:
            if closing_layers and layer_matches(line.layer, closing_layers):
                geom = line_to_shapely(line)
                if geom is not None:
                    closers.setdefault(line.layer, []).append(geom)
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
    empty: PropertyBoundary | None = None  # контур, внутри которого ничего не снято
    primary: PropertyBoundary | None = None  # выбранный контур
    mismatch_limit = float(fcfg.get("property_mismatch_overlap") or 0.0)
    for rank in sorted(by_rank):
        layers = by_rank[rank]
        layer_name = ", ".join(sorted(layers))
        lines = [geom for group in layers.values() for geom in group]
        seen.append(f"{layer_name} ({len(lines)} лин.)")
        polygons, gaps = polygons_from_lines(
            lines, snap_tolerance=snap_tol, gap_tolerance=gap_tol,
            min_area=min_area, allow_chain_closure=True,
        )
        closed_by = ""
        if not polygons and closers:
            # Фасадной стороны в слое участка нет — она начерчена красной линией улицы.
            extra = [geom for group in closers.values() for geom in group]
            candidates, gaps = polygons_from_lines(
                lines + extra, snap_tolerance=snap_tol, gap_tolerance=gap_tol,
                min_area=min_area, allow_chain_closure=False,
            )
            own = unary_union(lines).buffer(max(snap_tol, 0.01))
            kept = [
                (index, poly) for index, poly in enumerate(candidates)
                if poly.exterior.intersection(own).length >= 0.5 * poly.exterior.length
            ]
            polygons = [poly for _index, poly in kept]
            gaps = {new: gaps[old] for new, (old, _poly) in enumerate(kept) if old in gaps}
            if polygons:
                closed_by = ", ".join(sorted(closers))
        if not polygons:
            continue
        if survey_lines is None:
            survey_lines = _survey_lines(snapshot, classifier)
        index, ambiguous = _choose_property_candidate(polygons, survey_lines)
        polygon = _normalize_polygon(polygons[index])
        if polygon is None:
            continue
        notes: list[str] = []
        if closed_by:
            notes.append(
                f"в слое {layer_name} граница не замкнута: недостающая сторона взята "
                f"из слоя {closed_by} (красная линия улицы)"
            )
        gap = float(gaps.get(index, 0.0))
        if gap > 0.0:
            note = f"контур границы не был замкнут: разрыв {gap:.2f} ft закрыт автоматически"
            if closers and not closed_by:
                # Недостающая сторона — фасадная: она начерчена красной линией улицы.
                own = unary_union(lines).buffer(max(snap_tol, 0.05))
                rest = polygon.exterior.difference(own)
                street = unary_union(
                    [geom for group in closers.values() for geom in group]
                ).buffer(0.1)
                if rest.length > 0.0 and rest.difference(street).length <= 0.05 * rest.length:
                    note = (
                        f"в слое {layer_name} граница не замкнута: недостающая сторона "
                        f"({gap:.2f} ft) идёт по линии слоя {', '.join(sorted(closers))} "
                        "(красная линия улицы)"
                    )
            notes.append(note)
        if len(polygons) > 1:
            notes.append(
                f"в слое {layer_name} найдено замкнутых контуров: {len(polygons)}; выбран "
                + ("самый крупный — проверьте результат или укажите границу в AutoCAD"
                   if ambiguous else "контур с наибольшим числом снятых объектов")
            )
        found = PropertyBoundary(
            polygon=polygon, layer=layer_name, method="auto",
            candidates=len(polygons), closed_gap_ft=gap, notes=notes,
        )
        if not survey_lines or _activity_score(polygon, survey_lines) > 0.0:
            if primary is not None:
                # Второй контур участка в другом слое (граница Шага 1 и участок
                # геодезиста): если они заметно расходятся — предупреждение.
                mismatch = _boundary_mismatch_note(primary, found, mismatch_limit)
                if mismatch:
                    primary.notes.append(mismatch)
                break
            if empty is not None:
                found.notes.append(
                    f"контур слоя {empty.layer} пропущен: внутри него нет снятых заборов и "
                    f"строений (он начерчен в стороне от съёмки); взят слой {layer_name}"
                )
            primary = found
            continue
        # Контур есть, но съёмки внутри нет (например, граница Шага 1 начерчена
        # в стороне): смотрим следующие слои, этот оставляем запасным.
        if empty is None and primary is None:
            empty = found
    if primary is not None:
        return primary
    if empty is not None:
        return empty
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
    snap_snapshot_vertices(snapshot, cfg)
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

    # Выбрано всё подряд (рамкой): вместе с границей попали заборы и строения.
    # Контур из такой смеси — случайный, поэтому граница строится только по
    # выбранным линиям слоёв участка, как при автопоиске.
    closing_layers = _as_patterns(fcfg.get("property_closing_layers"))

    def is_boundary_layer(layer: str) -> bool:
        return classifier.is_property(layer) or bool(
            closing_layers and layer_matches(layer, closing_layers)
        )

    own = [line for line in selected if classifier.is_property(line.layer)]
    foreign = [line for line in selected if not is_boundary_layer(line.layer)]
    if own and foreign:
        narrowed = replace(
            snapshot,
            lines=[line for line in selected if is_boundary_layer(line.layer)]
            + [line for line in snapshot.lines if not is_boundary_layer(line.layer)],
        )
        try:
            boundary = find_property_polygon(narrowed, cfg)
        except FeatureLabelError:
            boundary = None
        if boundary is not None:
            boundary.method = "pick"
            boundary.notes.insert(
                0,
                f"выбраны объекты {len({line.layer for line in selected})} слоёв: граница "
                f"построена только по линиям слоя участка ({boundary.layer}), остальные "
                "выбранные линии — заборы и строения — в контур не вошли",
            )
            return boundary

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
    "pool": "бассейн",
    "wall": "стена",
    "overhang": "навес/консоль",
    "cellar_door": "люк подвала",
    "fire_escape": "пожарная лестница",
    "awning": "козырёк",
    "party_wall": "общая стена",
    "symbol": "условный знак",
    "misc": "объект",
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
    #: контур, внутри которого лежит объект (крыльцо для ступеней без своего
    #: контура): подпись ставится за пределами этого контура
    host: BaseGeometry | None = None

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


#: Причины, по которым дом остаётся без подписи.
HOUSE_TEXT_MISSING_REASON = "поле «Текст для основного дома» пустое"
OTHER_HOUSE_REASON = "строение не на участке (соседний дом) — описание вписывается вручную"


@dataclass
class RecognitionResult:
    """Итог распознавания: участок, зона поиска и объекты с подписями."""

    boundary: PropertyBoundary
    zone: Polygon
    #: основной дом найден, но текст для него не введён
    house_without_text: bool = False
    features: list[Feature] = field(default_factory=list)
    point_codes: list[PointCode] = field(default_factory=list)
    unknown_codes: dict[str, int] = field(default_factory=dict)
    unbound_fence_codes: list[PointCode] = field(default_factory=list)
    #: точки с описанием объекта (не забора), рядом с которыми нет линий слоя
    unbound_descriptions: list[PointCode] = field(default_factory=list)
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

    # Точки-блоки: описание в атрибуте, слой блока — слой объекта.
    rules = fcfg.get("point_descriptions")
    ignored_suffixes = {
        normalize_description(word) for word in _as_patterns(fcfg.get("point_description_ignored_suffixes"))
    }
    layer_suffixes = [
        str(suffix).strip().upper() for suffix in _as_patterns(fcfg.get("point_layer_suffixes"))
    ]
    for survey in snapshot.survey_points:
        description = normalize_description(survey.description)
        # «VINYL FENCE NE»: NE — точка снята без отметки (no elevation), не часть названия.
        words = description.split(" ")
        while len(words) > 1 and words[-1] in ignored_suffixes:
            words.pop()
        description = " ".join(words)
        if not description or not search.contains(Point(survey.point)):
            continue
        # Точки с отметкой лежат в слое «<слой объекта>-ELEV» (ST-STEP-ELEV -> ST-STEP).
        point_layer = str(survey.layer or "")
        for suffix in layer_suffixes:
            if suffix and point_layer.upper().endswith(suffix) and len(point_layer) > len(suffix):
                point_layer = point_layer[: -len(suffix)]
                break
        label: str | None = None
        pattern: str | None = None
        category = ""
        is_fence = False
        narrow_label = ""
        translated = find_description_rule(description, rules)
        if translated is not None:
            rule, pattern = translated
            label = str(rule.get("label") or "").strip() or None
            category = str(rule.get("category") or "").strip().lower()
            narrow_label = str(rule.get("label_narrow") or "").strip()
            is_fence = category == "fence"
        else:
            # Короткий код в атрибуте («FEV64») — как текст слоя POINT_LABELS.
            single = description if " " not in description else ""
            found = lookup_pattern_label(single, dictionary) if single else None
            if found is not None:
                label, pattern = found
                is_fence = layer_matches(single, fence_codes)
                category = "fence" if is_fence else "point"
            elif not (single and layer_matches(single, silent)):
                unknown[description] = unknown.get(description, 0) + 1
        codes.append(
            PointCode(
                number=str(survey.number or "").strip(),
                code=description,
                label=label,
                pattern=pattern,
                point=(float(survey.point[0]), float(survey.point[1])),
                layer=point_layer,
                raw=plain_text(survey.description),
                is_fence=is_fence,
                category=category,
                from_block=True,
                narrow_label=narrow_label,
            )
        )
    return codes, unknown


# ---------------------------------------------------------------------------
# Заметки полевого чертёжника -> формулировки заказчика
# ---------------------------------------------------------------------------

#: почему объект намеренно оставлен без подписи
SURFACE_EDGE_REASON = "кромка покрытия: материал по линиям не определить"
WALL_UNDER_FENCE_REASON = "стена подписана вместе с забором на ней"
WALL_SIDE_REASON = "вторая линия подписанной стены"
NO_LABEL_CODE_REASON = "описание точки подписи не требует"
ROOFED_PORCH_REASON = "площадка подписана вместе с козырьком над ней"

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


#: причина пропуска линии слоя ``code_linework_layers`` без кода забора
CODE_LINEWORK_REASON = "линия между точками съёмки без кода забора"

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
        # Выноска принадлежит переведённой заметке; посторонний текст на слое
        # заметок (tax-подпись у конца стрелки) её не перехватывает.
        best: tuple[tuple[int, float], FieldNote, tuple[float, float]] | None = None
        for note in notes:
            link = max(2.0, 2.5 * (note.height or 1.0))
            d_first, d_last = note.box.distance(p_first), note.box.distance(p_last)
            near, far, tip = (d_first, d_last, last) if d_first <= d_last else (d_last, d_first, first)
            if near > link or far < 0.5:
                continue
            key = (0 if note.labels else 1, near)
            if best is None or key < best[0]:
                best = (key, note, (float(tip[0]), float(tip[1])))
        if best is not None and best[2] not in best[1].tips:
            best[1].tips.append(best[2])
    return notes


def _tread_clusters(
    treads: Sequence[tuple[str, LineString]], gap: float = 1.6
) -> list[tuple[str, BaseGeometry]]:
    """Группы линий проступей (одна лестница): ``(слой, выпуклая оболочка)``."""
    if not treads:
        return []
    groups = _connected_groups([line for _layer, line in treads], gap)
    by_id = {id(line): layer for layer, line in treads}
    clusters: list[tuple[str, BaseGeometry]] = []
    for group in groups:
        hull = unary_union(list(group)).convex_hull
        if hull.geom_type != "Polygon" or hull.area < 0.05:
            hull = hull.buffer(0.3, cap_style=2)
        clusters.append((by_id.get(id(group[0]), ""), hull))
    return clusters


def _apply_field_notes(
    result: "RecognitionResult",
    notes: Sequence[FieldNote],
    fcfg: dict[str, Any],
    new_id: Any,
    treads: Sequence[tuple[str, LineString]] = (),
) -> None:
    """
    Привязывает переведённые заметки к объектам: подпись заметки заменяет
    подпись по коду точки и по слою. Заметка о покрытии/грунте (и заметка,
    объект которой не найден) ставится отдельной подписью в своей точке.

    Ступени без собственного контура (в чертеже есть только линии проступей
    внутри контура крыльца) получают объект по этим линиям: подпись
    ``CONC. STEPS`` ставится рядом с лестницей, за контуром крыльца.
    """
    radius = max(0.0, float(fcfg.get("field_note_bind_radius_ft") or 0.0))
    wide = max(radius, float(fcfg.get("field_note_surface_radius_ft") or 0.0))
    synonyms = fcfg.get("label_synonyms")
    claims: dict[int, tuple[float, FieldNote, str]] = {}
    spots: list[tuple[FieldNote, str, str, str]] = []  # заметка, подпись, категория, пояснение
    # Лестницы без контура: группы проступей, не лежащие в контуре ступеней.
    step_areas = [
        f.geometry for f in result.features if f.category == "steps" and f.kind == "area"
    ]
    clusters = [
        (layer, hull) for layer, hull in _tread_clusters(treads)
        if not any(area.buffer(0.3).intersects(hull.centroid) for area in step_areas)
    ]
    used_clusters: set[int] = set()
    for note in notes:
        for target, label in note.labels.items():
            anchors: list[BaseGeometry] = [Point(tip) for tip in note.tips] or [note.box]
            if target == "steps" and clusters:
                # Ближайшая лестница без контура — раньше, чем чужой контур ступеней.
                near = [
                    (min(hull.distance(anchor) for anchor in anchors), index)
                    for index, (_layer, hull) in enumerate(clusters)
                    if index not in used_clusters
                ]
                near = [pair for pair in near if pair[0] <= 2.0 * radius]
                outlined = [
                    min(f.geometry.distance(anchor) for anchor in anchors)
                    for f in result.features
                    if f.category == "steps" and f.kind in {"area", "edge"}
                ]
                if near and (not outlined or min(near)[0] < min(outlined)):
                    distance, index = min(near)
                    layer, hull = clusters[index]
                    used_clusters.add(index)
                    hosts = [
                        f.geometry for f in result.features
                        if f.kind == "area" and f.category in {"porch", "steps"}
                        and f.geometry.buffer(0.3).intersects(hull.centroid)
                    ]
                    result.features.append(
                        Feature(
                            feature_id=new_id(), kind="area", category="steps", layer=layer,
                            geometry=hull, label=label, source="field_note",
                            rule=note.pattern, note_text=note.text, closed=False,
                            host=min(hosts, key=lambda g: g.area) if hosts else None,
                        )
                    )
                    note.status = "bound"
                    continue
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
                # Размер — с точностью до 0.1: выбор между двумя одинаковыми заборами не
                # должен зависеть от тысячных долей фута.
                key = (round(distance, 2), round(size, 1), index)
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
                    # То же покрытие чуть дальше (тротуар в 8 ft от заметки
                    # «CONCRETE SIDEWALK»): двух одинаковых подписей не нужно.
                    for other in result.features:
                        if other.category != "surface" or other.source != "layer":
                            continue
                        if not other.label or not labels_similar(other.label, label, synonyms):
                            continue
                        if min(other.geometry.distance(anchor) for anchor in anchors) <= wide:
                            other.label = None
                            other.source = None
                            other.skip_reason = f"покрытие подписано по заметке «{note.text}»"
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


def _corner_angle_deg(
    first: Sequence[float], corner: Sequence[float], last: Sequence[float]
) -> float:
    """Угол между сторонами ``corner-first`` и ``corner-last`` (0…180°); 90° — прямой угол."""
    ux, uy = first[0] - corner[0], first[1] - corner[1]
    vx, vy = last[0] - corner[0], last[1] - corner[1]
    norm = math.hypot(ux, uy) * math.hypot(vx, vy)
    if norm <= 0.0:
        return 0.0
    angle = math.degrees(math.acos(max(-1.0, min(1.0, (ux * vx + uy * vy) / norm))))
    return min(angle, 180.0 - angle)


def _shape_from_open_lines(lines: Sequence[LineString], min_width: float) -> Polygon | None:
    """
    Приближённая площадь объекта с незамкнутым контуром.

    * одна ломаная (терраса из трёх сторон у стены дома) — замыкается по концам;
    * угол из двух сторон (навес или тамбур в углу между домом и соседом: сняты
      только две свободные стороны) — достраивается до параллелограмма;
    * две кромки (дорожка, заезд) — полоса между ними;
    * иначе — выпуклая оболочка.
    Слишком узкий результат (прямая линия) отбрасывается.
    """
    merged = merge_lines(lines, 0.0) or list(lines)
    candidate: Polygon | None = None
    if len(merged) == 1:
        coords = _clean_coords(merged[0].coords)
        corner = _clean_coords(merged[0].simplify(0.05).coords)
        if len(corner) == 3:
            coords = corner
        if len(coords) == 3 and _corner_angle_deg(*coords) >= 45.0:
            (ax, ay), (bx, by), (cx, cy) = coords
            coords = [coords[0], coords[1], coords[2], (ax + cx - bx, ay + cy - by)]
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
    #: точки съёмки с описанием, лежащие на контуре (самое частое описание — первым)
    codes: tuple[PointCode, ...] = ()


def _majority_first(codes: Sequence[PointCode]) -> tuple[PointCode, ...]:
    """Точки объекта: сначала точки самого частого описания."""
    counts: dict[str, int] = {}
    for code in codes:
        counts[str(code.label)] = counts.get(str(code.label), 0) + 1
    order = {label: index for index, label in enumerate(counts)}
    return tuple(
        sorted(codes, key=lambda code: (-counts[str(code.label)], order[str(code.label)]))
    )


def _assign_points_to_shapes(
    faces: Sequence[Polygon],
    groups: Sequence[BaseGeometry],
    points: Sequence[PointCode],
    reach: float,
) -> tuple[dict[int, list[PointCode]], dict[int, list[PointCode]], list[PointCode]]:
    """
    Относит точки с описанием к контурам (``faces``) и незамкнутым линиям
    (``groups``) одного слоя: к ближайшему объекту не дальше ``reach``.

    Точка в общем углу двух объектов (крыльцо и первая проступь) достаётся:
    замкнутому контуру, а не примыкающему хвосту; контуру, у которого есть
    «своя» точка с тем же описанием; при прочих равных — меньшему контуру
    (бассейн, а не двор вокруг него).
    """
    targets: list[tuple[int, int, BaseGeometry, float]] = []  # (тип, индекс, геометрия, размер)
    for index, face in enumerate(faces):
        targets.append((0, index, face.exterior, face.area))
    for index, group in enumerate(groups):
        targets.append((1, index, group, group.length))
    options: list[list[int]] = []
    for code in points:
        point = Point(code.point)
        distances = [target[2].distance(point) for target in targets]
        nearest = min(distances, default=math.inf)
        if nearest > reach:
            options.append([])
            continue
        options.append([i for i, dist in enumerate(distances) if dist <= nearest + 0.1])
    own: dict[tuple[int, str], int] = {}
    for code, found in zip(points, options):
        if len(found) == 1:
            key = (found[0], str(code.label))
            own[key] = own.get(key, 0) + 1
    by_face: dict[int, list[PointCode]] = {}
    by_group: dict[int, list[PointCode]] = {}
    loose: list[PointCode] = []
    for code, found in zip(points, options):
        if not found:
            loose.append(code)
            continue
        best = min(
            found,
            key=lambda i: (targets[i][0], -own.get((i, str(code.label)), 0), targets[i][3]),
        )
        kind, index = targets[best][0], targets[best][1]
        (by_face if kind == 0 else by_group).setdefault(index, []).append(code)
    return by_face, by_group, loose


def _extract_layer_shapes(
    lines: Sequence[LineString],
    *,
    tolerance: float,
    min_width: float,
    pair_gap: float,
    points: Sequence[PointCode] = (),
    point_reach: float = 0.0,
    loose: list[PointCode] | None = None,
    linear: bool = False,
    linear_categories: Iterable[str] = (),
    described_gap: float = 0.0,
) -> list[_LayerShape]:
    """
    Делит линии одного слоя на отдельные объекты: замкнутые контуры, открытые
    контуры (приближённая площадь) и одиночные кромки.

    ``points`` — точки съёмки этого слоя с описанием. Контур, на котором лежат
    такие точки, становится отдельным объектом с этим описанием (бассейн и
    ступени к нему — два объекта, хотя линии общие); примыкающие к нему
    контуры без точек (проступи у крыльца) — детали, отдельно не выдаются.
    Точки, рядом с которыми нет линий слоя, складываются в ``loose``.

    ``linear`` — слой линейных объектов (пунктир консоли вокруг дома): площадь
    по незамкнутым линиям не строится, объект — каждая ломаная длиннее 5 ft.
    Если описание точки относит объект к категории не из ``linear_categories``
    (пожарная лестница или козырёк в слое навесов ST-OHED), это площадка:
    контур строится как обычно, подпись ставится внутри.
    """
    linear_set = {str(name).strip().lower() for name in linear_categories}
    noded = _noded(snap_endpoints(lines, tolerance))
    if not noded:
        if loose is not None:
            loose.extend(points)
        return []
    polys, dangles, cuts, invalid = polygonize_full(noded)
    faces = [
        Polygon(poly.exterior.coords)
        for poly in _polygon_parts(polys) if poly.is_valid and poly.area > 0.0
    ]
    reach = max(tolerance, 1e-6)
    leftover = _line_parts(dangles) + _line_parts(cuts) + _line_parts(invalid)
    groups = _connected_groups(leftover, reach) if leftover else []
    group_geoms = [unary_union(group) for group in groups]

    face_codes: dict[int, list[PointCode]] = {}
    group_codes: dict[int, list[PointCode]] = {}
    if points:
        face_codes, group_codes, missed = _assign_points_to_shapes(
            faces, group_geoms, points, max(point_reach, reach)
        )
        if loose is not None:
            loose.extend(missed)

    # --- контур — часть объекта, начерченного незамкнутыми линиями -----------------
    # Лестница снята «гребёнкой»: проступи примыкают к боковой линии, а замкнута
    # только случайная ячейка между двумя проступями. Ячейка без своего описания
    # (или с тем же описанием), к которой примыкают незамкнутые линии с описанием, —
    # не отдельный объект, а часть той же лестницы.
    owner: dict[int, int] = {}
    links: list[tuple[int, int]] = []
    if group_codes and faces and not linear:
        group_label = {
            index: str(_majority_first(found)[0].label or "") for index, found in group_codes.items()
        }
        face_label = {
            index: str(_majority_first(found)[0].label or "") for index, found in face_codes.items()
        }
        for group_index in sorted(group_codes):
            label = group_label[group_index]
            if not label:
                continue
            for index, face in enumerate(faces):
                if group_geoms[group_index].distance(face) > reach:
                    continue
                if index in face_label and face_label[index] != label:
                    continue
                if index not in owner:
                    owner[index] = group_index
                elif group_label[owner[index]] == label:
                    links.append((owner[index], group_index))
        # Соседние ячейки без описания (следующие проступи) — тоже её части, если
        # они не примыкают к контуру другого описанного объекта.
        others = [faces[index] for index in face_codes if index not in owner]
        frontier = list(owner)
        while frontier:
            current = frontier.pop()
            for index, face in enumerate(faces):
                if index in owner or index in face_codes:
                    continue
                if face.distance(faces[current]) > reach:
                    continue
                if any(face.distance(other) <= reach for other in others):
                    continue
                owner[index] = owner[current]
                frontier.append(index)

    shapes: list[_LayerShape] = []
    # --- замкнутые контуры --------------------------------------------------
    plain_faces = [index for index in range(len(faces)) if index not in owner]
    described_faces = [index for index in sorted(face_codes) if index not in owner]
    if described_faces:
        for index in described_faces:
            shapes.append(
                _LayerShape(
                    geometry=faces[index], closed=True, is_area=True,
                    codes=_majority_first(face_codes[index]),
                )
            )
        # Контуры без точек, примыкающие (и по цепочке) к описанному, — его детали.
        detail = set(described_faces) | set(owner)
        frontier = list(described_faces)
        while frontier:
            current = faces[frontier.pop()]
            for index, face in enumerate(faces):
                if index not in detail and face.distance(current) <= reach:
                    detail.add(index)
                    frontier.append(index)
        plain_faces = [index for index in plain_faces if index not in detail]
    solids: list[Polygon] = []
    if plain_faces:
        for solid in _polygon_parts(unary_union([faces[index] for index in plain_faces])):
            solids.append(Polygon(solid.exterior.coords))
    shapes.extend(_LayerShape(geometry=solid, closed=True, is_area=True) for solid in solids)

    # --- незамкнутые линии --------------------------------------------------
    # Хвосты без описания, примыкающие к замкнутому контуру, — детали того же объекта.
    free: list[int] = []
    for index, geom in enumerate(group_geoms):
        if index in group_codes:
            free.append(index)
            continue
        touched = [number for number, face in enumerate(faces) if geom.distance(face) <= reach]
        absorbed = [number for number in touched if number in owner]
        if not touched:
            free.append(index)
        elif absorbed:
            # хвост у ячейки лестницы — часть той же лестницы
            free.append(index)
            links.append((owner[absorbed[0]], index))
    if not free:
        return shapes

    # Кромки одного покрытия (две стороны дорожки) объединяются в один объект.
    parent = {index: index for index in free}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    if pair_gap > 0.0 and len(free) > 1:
        for position, i in enumerate(free):
            for j in free[position + 1:]:
                if group_geoms[i].distance(group_geoms[j]) <= pair_gap:
                    root_a, root_b = find(i), find(j)
                    if root_a != root_b:
                        parent[root_b] = root_a
    if described_gap > 0.0 and not linear and len(free) > 1:
        # Две параллельные стороны одной площадки с одинаковым описанием (крыльцо
        # между стеной дома и ступенями: сняты только две боковые стороны).
        for position, i in enumerate(free):
            for j in free[position + 1:]:
                if find(i) == find(j) or i not in group_codes or j not in group_codes:
                    continue
                first, second = _majority_first(group_codes[i]), _majority_first(group_codes[j])
                if not first[0].label or first[0].label != second[0].label:
                    continue
                if _opposite_sides(groups[i], groups[j], described_gap):
                    parent[find(j)] = find(i)
    for first, second in links:
        root_a, root_b = find(first), find(second)
        if root_a != root_b:
            parent[root_b] = root_a
    clusters: dict[int, list[int]] = {}
    for index in free:
        clusters.setdefault(find(index), []).append(index)

    for members in clusters.values():
        cluster = [line for index in members for line in groups[index]]
        cells = sorted(index for index, group_index in owner.items() if group_index in members)
        cluster += [LineString(faces[index].exterior.coords) for index in cells]
        codes = _majority_first(
            [code for index in members for code in group_codes.get(index, [])]
            + [code for index in cells for code in face_codes.get(index, [])]
        )
        chains = merge_lines(cluster, 0.0) or cluster
        # Кромки покрытия с описанием («CONC-SW» по трём линиям тротуара): выпуклая
        # оболочка накрыла бы чужие объекты, подпись ставится вдоль длинной кромки.
        is_linear = linear and not (
            bool(codes) and bool(codes[0].category) and codes[0].category not in linear_set
        )
        edges_only = is_linear or (
            bool(codes) and codes[0].category == "surface" and len(chains) > 2
        )
        if edges_only:
            longest = max(chains, key=lambda chain: chain.length)
            # Пунктир консоли идёт вдоль нескольких стен дома — подпись у каждой;
            # у кромок покрытия подписывается одна, самая длинная.
            kept = [chain for chain in chains if chain.length >= 5.0] if is_linear else []
            for chain in kept or [longest]:
                shapes.append(
                    _LayerShape(geometry=chain, closed=False, is_area=False, codes=codes)
                )
            continue
        polygon = _shape_from_open_lines(cluster, min_width)
        if polygon is not None:
            shapes.append(_LayerShape(geometry=polygon, closed=False, is_area=True, codes=codes))
            continue
        for chain in chains:
            near = tuple(
                code for code in codes
                if chain.distance(Point(code.point)) <= max(point_reach, reach)
            )
            shapes.append(
                _LayerShape(geometry=chain, closed=False, is_area=False, codes=near or codes)
            )
    return shapes


def _long_ribbon_caps(
    lines: Sequence[LineString], max_width: float, tolerance: float, min_length: float
) -> list[LineString]:
    """
    Торцы только тех лент, которые после замыкания действительно узкие и
    длинные: средняя ширина контура не больше ``max_width``, длина не меньше
    ``min_length`` и четырёх ширин.
    """
    caps = _ribbon_caps(lines, max_width, tolerance)
    if not caps:
        return []
    noded = _noded(snap_endpoints(list(lines) + caps, tolerance))
    polys, _dangles, _cuts, _invalid = polygonize_full(noded)
    ribbons = []
    for poly in _polygon_parts(polys):
        perimeter = poly.exterior.length
        if perimeter <= 0.0 or poly.area <= 0.0:
            continue
        width = 2.0 * poly.area / perimeter
        if width <= max_width and perimeter / 2.0 >= max(min_length, 4.0 * width):
            ribbons.append(poly.exterior)
    kept = []
    for cap in caps:
        middle = cap.interpolate(0.5, normalized=True)
        if any(ring.distance(middle) <= max(tolerance, 1e-6) for ring in ribbons):
            kept.append(cap)
    return kept


def _opposite_sides(
    first: Sequence[LineString], second: Sequence[LineString], max_gap: float
) -> bool:
    """
    Две прямые линии — противоположные стороны одной площадки: параллельны
    (в пределах 15°), лежат напротив друг друга (перекрытие проекций не меньше
    половины короткой) и не дальше ``max_gap``, но и не на одной прямой.
    """
    chains = []
    for group in (first, second):
        merged = merge_lines(list(group), 0.0) or list(group)
        if len(merged) != 1:
            return False
        coords = _clean_coords(merged[0].simplify(0.05).coords)
        if len(coords) != 2:
            return False
        chains.append(coords)
    (a0, a1), (b0, b1) = chains
    length_a = math.hypot(a1[0] - a0[0], a1[1] - a0[1])
    length_b = math.hypot(b1[0] - b0[0], b1[1] - b0[1])
    if length_a <= 0.0 or length_b <= 0.0:
        return False
    ux, uy = (a1[0] - a0[0]) / length_a, (a1[1] - a0[1]) / length_a
    cross = abs(ux * (b1[1] - b0[1]) - uy * (b1[0] - b0[0])) / length_b
    if cross > math.sin(math.radians(15.0)):
        return False
    gap = abs(-uy * (b0[0] - a0[0]) + ux * (b0[1] - a0[1]))
    if gap < 1.0 or gap > max_gap:
        return False
    start, end = sorted(
        (ux * (point[0] - a0[0]) + uy * (point[1] - a0[1])) for point in (b0, b1)
    )
    overlap = min(length_a, end) - max(0.0, start)
    return overlap >= 0.5 * min(length_a, length_b)


def _walls_with_columns(
    result: "RecognitionResult", codes: Sequence[PointCode], fcfg: dict[str, Any], ribbon_width: float
) -> None:
    """
    Стена с колоннами: у линии стены сняты точки колонн (``COL``) — подпись
    «WALL W./COLUMNS». Вторая линия той же стены (между колоннами она идёт
    отрезками) и квадраты самих колонн своей подписи не требуют.
    """
    walls = [
        feature for feature in result.features
        if feature.category == "wall" and feature.kind in {"edge", "area"}
    ]
    labeled = [wall for wall in walls if wall.label and " ATOP " not in wall.label]
    template = str(fcfg.get("wall_with_columns_format") or "").strip()
    reach = max(0.0, float(fcfg.get("wall_columns_reach_ft") or 0.0))
    minimum = max(1, int(fcfg.get("wall_columns_min_count") or 1))
    columns = [Point(code.point) for code in codes if code.category == "column"]
    if template and columns:
        for wall in labeled:
            near = [point for point in columns if wall.geometry.distance(point) <= reach]
            # Колонна снята двумя-четырьмя точками: считаются места, а не точки.
            places: list[Point] = []
            for point in near:
                if all(point.distance(other) > 3.0 for other in places):
                    places.append(point)
            if len(places) < minimum:
                continue
            try:
                wall.label = template.format(wall=wall.label, WALL=wall.label)
            except (KeyError, IndexError, ValueError):
                continue
            wall.rule = f"{wall.rule} + колонны ({len(places)})"
    if ribbon_width <= 0.0:
        return
    for wall in walls:
        if wall.label or wall.skip_reason or wall.kind != "edge":
            continue
        for other in labeled:
            if other is wall:
                continue
            band = other.geometry.buffer(ribbon_width, cap_style=2)
            if wall.geometry.intersection(band).length >= 0.8 * wall.geometry.length:
                wall.skip_reason = WALL_SIDE_REASON
                break


def _roofed_porches(
    result: "RecognitionResult", codes: Sequence[PointCode], fcfg: dict[str, Any]
) -> None:
    """
    Крытое крыльцо: под контуром козырька (``AW OVERH``) сняты точки крыльца
    (``O PORCH``) — геодезист показал и площадку, и крышу над ней. Заказчик
    пишет ``ROOFED O.P.`` вместо ``AWNING``.
    """
    mapping = fcfg.get("roofed_porch_labels")
    if not isinstance(mapping, dict) or not mapping:
        return
    roofed = {" ".join(str(key).split()): str(value) for key, value in mapping.items() if value}
    for awning in result.features:
        if awning.category != "awning" or awning.kind != "area" or not awning.label:
            continue
        cover = awning.geometry.buffer(0.3)
        under = [
            code for code in codes
            if code.category == "porch" and code.label
            and " ".join(str(code.label).split()) in roofed
            and cover.contains(Point(code.point))
        ]
        if not under:
            continue
        porch_label = " ".join(str(under[0].label).split())
        awning.rule = f"{awning.rule} + {under[0].raw} под козырьком ({len(under)} т.)"
        awning.label = roofed[porch_label]
        awning.category = "porch"
        # Площадка того же крыльца, если у неё есть свой контур под козырьком.
        for porch in result.features:
            if porch is awning or porch.category != "porch" or porch.kind != "area" or not porch.label:
                continue
            try:
                inside = porch.geometry.intersection(cover).area
            except Exception:  # noqa: BLE001 - некорректная геометрия исходного чертежа
                continue
            if inside >= 0.6 * porch.geometry.area:
                porch.label = None
                porch.skip_reason = ROOFED_PORCH_REASON


def _ribbon_caps(
    lines: Sequence[LineString], max_width: float, tolerance: float
) -> list[LineString]:
    """
    Торцы стен, начерченных двумя параллельными линиями без замыкания.

    Стена у дома или у границы участка часто снята только двумя длинными
    сторонами: концы линий «висят». Два висящих конца разных линий, лежащие
    напротив друг друга не дальше ``max_width``, соединяются отрезком — стена
    становится замкнутой лентой, как и начерченная с торцами.
    """
    if max_width <= 0.0:
        return []
    reach = max(tolerance, 1e-6)
    ends: list[tuple[tuple[float, float], tuple[float, float], int]] = []
    for index, line in enumerate(lines):
        coords = _clean_coords(line.coords)
        if len(coords) < 2:
            continue
        ends.append((coords[0], coords[1], index))
        ends.append((coords[-1], coords[-2], index))
    everything = unary_union(list(lines)) if lines else None
    dangling: list[tuple[tuple[float, float], tuple[float, float], int]] = []
    for point, inner, index in ends:
        # Конец «висит», если к нему не подходит никакая другая линия слоя.
        touching = sum(
            1 for other_index, other in enumerate(lines)
            if other_index != index and other.distance(Point(point)) <= reach
        )
        if touching == 0:
            dangling.append((point, inner, index))
    caps: list[LineString] = []
    used: set[int] = set()
    for first in range(len(dangling)):
        if first in used:
            continue
        (ax, ay), (aix, aiy), a_index = dangling[first]
        a_dir = (aix - ax, aiy - ay)
        a_len = math.hypot(*a_dir)
        best: tuple[float, int] | None = None
        for second in range(first + 1, len(dangling)):
            if second in used:
                continue
            (bx, by), (bix, biy), b_index = dangling[second]
            if b_index == a_index:
                continue
            gap = math.hypot(bx - ax, by - ay)
            if gap > max_width or gap <= reach:
                continue
            b_dir = (bix - bx, biy - by)
            b_len = math.hypot(*b_dir)
            if a_len <= 0.0 or b_len <= 0.0:
                continue
            # Линии идут рядом в одну сторону, торец — поперёк них.
            along = (a_dir[0] * b_dir[0] + a_dir[1] * b_dir[1]) / (a_len * b_len)
            across = abs((bx - ax) * a_dir[0] + (by - ay) * a_dir[1]) / (gap * a_len)
            if along < math.cos(math.radians(10.0)) or across > math.cos(math.radians(60.0)):
                continue
            if best is None or gap < best[0]:
                best = (gap, second)
        if best is None:
            continue
        (bx, by), _inner, _index = dangling[best[1]]
        cap = LineString([(ax, ay), (bx, by)])
        if everything is not None and cap.crosses(everything):
            continue
        used.update({first, best[1]})
        caps.append(cap)
    return caps


def _ribbon_side(polygon: Polygon, max_width: float) -> LineString | None:
    """
    Длинная сторона узкого контура-ленты (стена из двух параллельных линий).

    ``None`` — контур не лента: средняя ширина (2·площадь / периметр) больше
    ``max_width`` либо он короче четырёх своих ширин.
    """
    perimeter = polygon.exterior.length
    if perimeter <= 0.0 or max_width <= 0.0:
        return None
    width = 2.0 * polygon.area / perimeter
    if width > max_width or perimeter / 2.0 < 4.0 * width:
        return None
    coords = _clean_coords(polygon.exterior.coords)
    best: LineString | None = None
    for start, end in zip(coords, coords[1:]):
        side = LineString([start, end])
        if best is None or side.length > best.length:
            best = side
    return best


def _mean_width(geometry: BaseGeometry) -> float:
    """Средняя ширина контура: 2·площадь / периметр (у ленты 1 × 45 ft — 1 ft)."""
    perimeter = geometry.length
    return 2.0 * geometry.area / perimeter if perimeter > 0.0 else 0.0


def _fill_area(label: str, polygon: BaseGeometry | None) -> str:
    """``{area}`` -> площадь контура; у линии строка с ``{area}`` убирается."""
    lines = []
    for line in label.split("\n"):
        if "{area}" in line:
            if polygon is None:
                continue
            line = line.replace("{area}", f"{polygon.area:.0f}")
        lines.append(line)
    return "\n".join(lines)


def _fences_atop_walls(
    result: "RecognitionResult",
    wall_shapes: dict[int, Polygon],
    fcfg: dict[str, Any],
    new_id: Any,
) -> None:
    """
    Забор на стене: часть линии забора, идущая по контуру стены, становится
    отдельным объектом с подписью «VINYL FENCE ATOP WALL»; стена своей подписи
    не получает (заказчик подписывает их вместе).
    """
    reach = max(0.0, float(fcfg.get("fence_atop_wall_reach_ft") or 0.0))
    min_atop = max(0.0, float(fcfg.get("fence_atop_wall_min_ft") or 0.0))
    min_label = max(min_atop, float(fcfg.get("fence_atop_wall_label_min_ft") or 0.0))
    template = str(fcfg.get("fence_atop_wall_format") or "").strip()
    min_length = float(fcfg.get("min_feature_length_ft") or 0.0)
    if not template or not wall_shapes:
        return
    default_wall = " ".join(str(fcfg.get("fence_atop_wall_default") or "").split())
    plain_walls = {
        " ".join(str(name).upper().split()) for name in _as_patterns(fcfg.get("fence_atop_plain_wall_names"))
    }
    walls = {f.feature_id: f for f in result.features if f.feature_id in wall_shapes}
    wall_names = {wall_id: wall.label for wall_id, wall in walls.items()}
    for wall_id, polygon in wall_shapes.items():
        wall = walls.get(wall_id)
        if wall is None:
            continue
        zone = polygon.buffer(reach)
        for fence in list(result.features):
            if fence.kind != "fence" or fence.category != "fence" or not fence.label:
                continue
            if " ATOP " in fence.label:
                continue
            atop = merge_lines(_line_parts(fence.geometry.intersection(zone)), 1e-6)
            atop = [part for part in atop if part.length >= min_atop]
            if not atop:
                continue
            wall.label = None
            wall.skip_reason = WALL_UNDER_FENCE_REASON
            atop = [part for part in atop if part.length >= min_label]
            if not atop:
                continue  # короткий участок стены под забором: только забор
            wall_name = " ".join(str(wall_names.get(wall_id) or "WALL").split())
            assumed = ""
            if default_wall and wall_name.upper() in plain_walls:
                wall_name = default_wall
                assumed = f"; материал стены не снят — принят {default_wall} (проверьте по фото)"
            try:
                label = template.format(fence=fence.flat_label, wall=wall_name)
            except (KeyError, IndexError, ValueError):
                label = f"{fence.flat_label} ATOP {wall_name}"
            rest = merge_lines(_line_parts(fence.geometry.difference(zone)), 1e-6)
            rest = [part for part in rest if part.length >= max(min_length, 1e-6)]
            result.features.remove(fence)
            for part in atop:
                result.features.append(
                    Feature(
                        feature_id=new_id(), kind="fence", category="fence", layer=fence.layer,
                        geometry=part, label=label, source=fence.source,
                        rule=f"{fence.rule}; на стене {wall.layer}{assumed}", codes=fence.codes,
                    )
                )
            for part in rest:
                result.features.append(
                    Feature(
                        feature_id=new_id(), kind="fence", category="fence", layer=fence.layer,
                        geometry=part, label=fence.label, source=fence.source,
                        rule=fence.rule, codes=fence.codes,
                    )
                )


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
    snap_snapshot_vertices(snapshot, cfg)
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
    tread_layers: list[tuple[str, LineString]] = []
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
            if geom.intersects(zone):
                tread_layers.append((line.layer, geom))
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
        # Забор, к которому сбоку примыкает другой, остаётся одной линией.
        for merged in merge_lines(fence_lines[layer], tolerance, through_turn_deg=20.0):
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

    # Два забора рядом (свой виниловый и соседская сетка в полуфуте): точка,
    # попавшая на линию с другим материалом, переносится на соседнюю линию,
    # у которой все точки — того же материала.
    if len(bound) > 1:
        moves: list[tuple[PointCode, int, int]] = []
        for index, part_codes in bound.items():
            counts: dict[str, int] = {}
            for code in part_codes:
                counts[str(code.label)] = counts.get(str(code.label), 0) + 1
            if len(counts) < 2:
                continue
            major = max(counts, key=lambda label: counts[label])
            for code in part_codes:
                if code.label == major:
                    continue
                point = Point(code.point)
                options = [
                    (fence_parts[other][1].distance(point), other)
                    for other, other_codes in bound.items()
                    if other != index
                    and all(item.label == code.label for item in other_codes)
                    and fence_parts[other][1].distance(point) <= max_code_dist
                ]
                if options:
                    moves.append((code, index, min(options)[1]))
        for code, source, target in moves:
            bound[source].remove(code)
            bound[target].append(code)

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
                # Линия между снятыми точками без кода забора — не забор.
                skip_reason=(
                    CODE_LINEWORK_REASON
                    if not mapped and classifier.is_code_linework(layer) else ""
                ),
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
    # Описания точек съёмки (блоки с атрибутами): точка лежит в слое объекта.
    described: dict[str, list[PointCode]] = {}
    for code in codes:
        if code.from_block and (code.label or code.category) and not code.is_fence:
            described.setdefault(code.layer.strip().upper(), []).append(code)
    feature_reach = max(0.0, float(fcfg.get("point_to_feature_max_ft") or 0.0))
    linear_categories = {
        str(name).strip().lower()
        for name in _as_patterns(fcfg.get("linear_structure_categories"))
    }
    ribbon_categories = {
        str(name).strip().lower() for name in _as_patterns(fcfg.get("ribbon_categories"))
    }
    ribbon_cap_categories = {
        str(name).strip().lower() for name in _as_patterns(fcfg.get("ribbon_cap_categories"))
    }
    ribbon_width = max(0.0, float(fcfg.get("ribbon_max_width_ft") or 0.0))
    narrow_width = max(0.0, float(fcfg.get("narrow_object_max_width_ft") or 0.0))
    wall_shapes: dict[int, Polygon] = {}
    loose_points: list[PointCode] = []
    for layer in sorted(structure_lines):
        category = classifier.structure_category(layer) or "structure"
        layer_lines = structure_lines[layer]
        if category in ribbon_categories:
            layer_lines = layer_lines + _ribbon_caps(layer_lines, ribbon_width, tolerance)
        elif category in ribbon_cap_categories:
            # В слое «разного» торец достраивается только у настоящей ленты
            # (стенка клумбы), а не у любых двух близких концов линий.
            layer_lines = layer_lines + _long_ribbon_caps(
                layer_lines, ribbon_width, tolerance,
                max(0.0, float(fcfg.get("ribbon_cap_min_length_ft") or 0.0)),
            )
        shapes = _extract_layer_shapes(
            layer_lines,
            tolerance=tolerance,
            min_width=min_width,
            pair_gap=(
                pair_gap if category == "surface"
                else max(1.0, feature_reach) if category in linear_categories
                else 0.0
            ),
            points=described.pop(layer.strip().upper(), []),
            point_reach=feature_reach,
            described_gap=pair_gap,
            loose=loose_points,
            linear=category in linear_categories,
            linear_categories=linear_categories,
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
            if shape.codes:
                # Описание точки важнее имени слоя: слой — класс, описание — объект.
                first = next((code for code in shape.codes if code.label), shape.codes[0])
                if not first.label:
                    feature.skip_reason = NO_LABEL_CODE_REASON  # колонна: известна, не подписывается
                feature.label = first.label
                feature.source = "point_code"
                feature.rule = f"{first.raw} → {first.pattern}"
                feature.codes = shape.codes
                feature.category = first.category or category
                if first.narrow_label and shape.is_area and _mean_width(geometry) <= narrow_width:
                    feature.label = first.narrow_label  # одна ступень: STEP, а не STEPS
            if feature.label and "{area}" in feature.label:
                feature.label = _fill_area(feature.label, geometry if shape.is_area else None)
            if feature.category == "wall" and feature.kind == "area":
                wall_shapes[feature.feature_id] = geometry  # type: ignore[assignment]
            if feature.kind == "area" and feature.category in ribbon_categories:
                side = _ribbon_side(geometry, ribbon_width)  # type: ignore[arg-type]
                if side is not None:
                    feature.kind = "edge"
                    feature.geometry = side
            if not feature.label and category == "surface":
                # Кромка тротуара или заезда без описания материала: что это за
                # покрытие, по линиям не понять — в «без подписи» не выносится.
                feature.skip_reason = SURFACE_EDGE_REASON
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

    _fences_atop_walls(result, wall_shapes, fcfg, new_id)
    _walls_with_columns(result, codes, fcfg, ribbon_width)
    _roofed_porches(result, codes, fcfg)

    # --- основной дом: текст из окна программы ------------------------------
    # Дом блокированной застройки: замкнутого контура нет, дом — часть участка
    # между линиями фасадов (боковые стены общие с соседями, идут по границам).
    main_house = _select_main_house(house_candidates, boundary.polygon, classifier)
    if main_house is None:
        house_layers = sorted(
            layer for layer in structure_lines
            if classifier.structure_category(layer) == "house"
        )
        row_house = _row_house_polygon(
            boundary.polygon,
            [line for layer in house_layers for line in structure_lines[layer]],
            [line for layer, lines in structure_lines.items() if layer not in house_layers
             for line in lines]
            + [line for lines in fence_lines.values() for line in lines]
            + tread_lines,
            min_area=max(50.0, 0.05 * boundary.polygon.area),
        )
        if row_house is not None:
            border = row_house.exterior.buffer(0.3)
            for wall_line in result.features:
                if (
                    wall_line.category == "house" and wall_line.kind == "edge"
                    and not wall_line.label
                    and wall_line.geometry.intersection(border).length >= 2.0
                ):
                    wall_line.skip_reason = ROW_HOUSE_WALL_REASON
            main_house = Feature(
                feature_id=new_id(), kind="area", category="house",
                layer=min(house_layers, key=classifier.structure_rank),
                geometry=row_house, label=None, source=None, rule=ROW_HOUSE_RULE,
                closed=False,
            )
            house_candidates.append(main_house)
            result.features.append(main_house)
            result.notes.append(
                "замкнутого контура дома на участке нет — дом взят как часть участка "
                f"между линиями стен ({row_house.area:.0f} sq ft, блокированная застройка)"
            )

    house_label = _normalize_house_text(house_text)
    if not house_label and main_house is not None:
        # Дом найден, но описывать его нечем: это не «слой без правила».
        main_house.skip_reason = HOUSE_TEXT_MISSING_REASON
        result.house_without_text = True
    for other in house_candidates:
        if other is main_house or other.label or other.skip_reason:
            continue
        try:
            on_lot = other.geometry.intersection(boundary.polygon).area
        except Exception:  # noqa: BLE001 - некорректная геометрия исходного чертежа
            continue
        if on_lot < 0.5 * other.geometry.area:
            other.skip_reason = OTHER_HOUSE_REASON
    # Обрывок контура соседнего дома (линия уходит за край съёмки) — тоже не «слой без правила».
    for piece in result.features:
        if piece.category != "house" or piece.kind != "edge" or piece.label or piece.skip_reason:
            continue
        try:
            on_lot = piece.geometry.intersection(boundary.polygon).length
        except Exception:  # noqa: BLE001 - некорректная геометрия исходного чертежа
            continue
        if on_lot < 0.5 * piece.geometry.length:
            piece.skip_reason = OTHER_HOUSE_REASON
    if house_label:
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

    # --- общие стены с соседями ---------------------------------------------
    party_label = str(fcfg.get("party_wall_label") or "").strip()
    party_layers = fcfg.get("party_wall_layers")
    if party_label and party_layers:
        party_lines: dict[str, list[LineString]] = {}
        for line in snapshot.lines:
            if not layer_matches(line.layer, party_layers):
                continue
            geom = line_to_shapely(line)
            if geom is not None and _within_bounds(geom, zone_bounds):
                party_lines.setdefault(line.layer, []).append(geom)
        for layer in sorted(party_lines):
            for axis in _party_wall_axes(
                party_lines[layer], boundary.polygon,
                float(fcfg.get("party_wall_max_width_ft") or 0.0),
                float(fcfg.get("party_wall_min_length_ft") or 0.0),
            ):
                result.features.append(
                    Feature(
                        feature_id=new_id(), kind="edge", category="party_wall", layer=layer,
                        geometry=axis, label=party_label, source="layer", rule=PARTY_WALL_RULE,
                        closed=False,
                    )
                )

    # --- условные знаки: вставки блоков (столб во дворе) --------------------
    scope = str(fcfg.get("symbol_label_scope") or "none").strip().lower()
    if scope in {"lot", "all"} and snapshot.symbols:
        lot_reach = boundary.polygon.buffer(
            max(0.0, float(fcfg.get("symbol_lot_tolerance_ft") or 0.0))
        )
        for symbol in snapshot.symbols:
            mapped_symbol = lookup_pattern_label(symbol.name, fcfg.get("symbol_block_labels"))
            if mapped_symbol is None or not mapped_symbol[0]:
                continue
            spot = Point(symbol.point)
            if not (lot_reach if scope == "lot" else zone).contains(spot):
                continue
            result.features.append(
                Feature(
                    feature_id=new_id(), kind="point", category="symbol", layer=symbol.layer,
                    geometry=spot, label=mapped_symbol[0], source="layer",
                    rule=f"условный знак, блок {symbol.name}",
                )
            )

    # --- заметки полевого чертёжника: формулировки заказчика ----------------
    result.field_notes = _collect_field_notes(snapshot, classifier, fcfg, zone)
    _apply_field_notes(result, result.field_notes, fcfg, new_id, tread_layers)

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

    loose_points.extend(code for layer_codes in described.values() for code in layer_codes)
    result.unbound_descriptions = loose_points
    if loose_points:
        titles = ", ".join(f"{code.title} ({code.layer})" for code in loose_points[:6])
        more = "…" if len(loose_points) > 6 else ""
        result.notes.append(
            f"описания точек без линии объекта в своём слое ближе {feature_reach:g} ft: "
            f"{titles}{more}"
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


PARTY_WALL_RULE = "две параллельные линии вдоль границы участка"


def _party_wall_axes(
    lines: Sequence[LineString], lot: Polygon, max_width: float, min_length: float
) -> list[LineString]:
    """
    Оси общих стен: пары параллельных отрезков не дальше ``max_width`` друг от
    друга, перекрывающиеся не меньше чем на ``min_length`` и идущие вдоль
    границы участка (ось пары лежит на линии границы).
    """
    segments: list[tuple[tuple[float, float], tuple[float, float], float]] = []
    for line in lines:
        coords = _clean_coords(line.coords)
        for start, end in zip(coords, coords[1:]):
            length = math.hypot(end[0] - start[0], end[1] - start[1])
            if length >= min_length:
                segments.append((start, end, length))
    if len(segments) < 2 or max_width <= 0.0:
        return []
    border = lot.exterior.buffer(max_width / 2.0)
    axes: list[LineString] = []
    used: set[int] = set()
    for first in range(len(segments)):
        if first in used:
            continue
        (ax, ay), (bx, by), a_len = segments[first]
        ux, uy = (bx - ax) / a_len, (by - ay) / a_len
        for second in range(first + 1, len(segments)):
            if second in used:
                continue
            (cx, cy), (dx, dy), c_len = segments[second]
            vx, vy = (dx - cx) / c_len, (dy - cy) / c_len
            if abs(ux * vy - uy * vx) > math.sin(math.radians(3.0)):
                continue
            t_start = (cx - ax) * ux + (cy - ay) * uy
            t_end = (dx - ax) * ux + (dy - ay) * uy
            low, high = max(0.0, min(t_start, t_end)), min(a_len, max(t_start, t_end))
            if high - low < min_length:
                continue
            # Смещение второй линии поперёк первой (со знаком).
            offset = -(cx - ax) * uy + (cy - ay) * ux
            if not (0.2 <= abs(offset) <= max_width):
                continue
            nx, ny = -uy * offset / 2.0, ux * offset / 2.0
            axis = LineString([
                (ax + ux * low + nx, ay + uy * low + ny),
                (ax + ux * high + nx, ay + uy * high + ny),
            ])
            if axis.intersection(border).length < 0.8 * axis.length:
                continue
            axes.append(axis)
            used.update({first, second})
            break
    return axes


ROW_HOUSE_RULE = "дом во всю ширину участка: между линиями стен и границами участка"
ROW_HOUSE_WALL_REASON = "линия стены дома (контур дома замкнут границами участка)"


def _extend_line(line: LineString, distance: float) -> LineString:
    """Ломаная, продлённая с обоих концов на ``distance`` по направлению крайних звеньев."""
    coords = _clean_coords(line.coords)
    if len(coords) < 2 or distance <= 0.0:
        return line

    def beyond(end: tuple[float, float], inner: tuple[float, float]) -> tuple[float, float]:
        length = math.hypot(end[0] - inner[0], end[1] - inner[1])
        if length <= 0.0:
            return end
        return (
            end[0] + (end[0] - inner[0]) / length * distance,
            end[1] + (end[1] - inner[1]) / length * distance,
        )

    return LineString([beyond(coords[0], coords[1])] + coords + [beyond(coords[-1], coords[-2])])


def _row_house_polygon(
    lot: Polygon,
    house_lines: Sequence[LineString],
    other_lines: Sequence[LineString],
    *,
    min_area: float,
    reach: float = 1.0,
    quiet_margin: float = 1.5,
) -> Polygon | None:
    """
    Контур дома блокированной застройки (row house).

    Дом занимает участок во всю ширину: боковые стены — общие с соседями и идут
    по боковым границам, отдельно не сняты; в слое строений есть только линии
    переднего и заднего фасадов, пересекающие участок (и продолженные на
    соседние дома). Такие линии делят участок на части; дом — часть, которая

    * ограничена линиями фасадов не меньше чем с двух сторон и
    * внутри пуста: ни заборов, ни стен, ни других снятых объектов (двор за
      домом, наоборот, заполнен ими).

    ``None`` — участок линиями строений не делится либо подходящей части нет.
    """
    if lot.is_empty or not house_lines:
        return None
    near = lot.buffer(reach)
    cutters = [
        _extend_line(chain, reach)
        for chain in merge_lines([line for line in house_lines if line.intersects(near)], 0.0)
        if chain.length >= 2.0
    ]
    if not cutters:
        return None
    try:
        noded = unary_union([LineString(lot.exterior.coords)] + cutters)
        faces = [face for face in polygonize(noded) if face.area >= min_area]
    except Exception:  # noqa: BLE001 - некорректная геометрия исходного чертежа
        return None
    inside = lot.buffer(0.05)
    faces = [face for face in faces if inside.contains(face.representative_point())]
    if not faces or (len(faces) == 1 and faces[0].area >= 0.98 * lot.area):
        return None  # линии строений не делят участок
    candidates: list[Polygon] = []
    for face in faces:
        border = face.exterior.buffer(0.3)
        sides = sum(1 for cutter in cutters if cutter.intersection(border).length >= 2.0)
        if sides < 2:
            continue
        core = face.buffer(-quiet_margin)
        if core.is_empty:
            continue
        busy = sum(
            line.intersection(core).length for line in other_lines if line.intersects(core)
        )
        if busy > 0.5:
            continue
        candidates.append(face)
    if not candidates:
        return None
    return _normalize_polygon(max(candidates, key=lambda face: face.area))


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

