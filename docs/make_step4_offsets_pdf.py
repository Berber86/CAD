#!/usr/bin/env python3
"""
Пример «как должен выглядеть чертёж с оффсетами» — PDF с английским текстом.

Лист собран из настоящих данных заказчика: пара ``docs/primer5i.zip`` (18 Zoe Street,
Staten Island; файл «3 … — копия» — это уже размеченный чертёж Николая). Геометрия участка
(``PL``), дома (``ST-BLDG-LINE``), заборов (``S-FENCE``), улицы (``M-ROW-LINE``) и его подписи
берутся из дампов, поэтому лист показывает именно тот вид, к которому мы приводим программу.

Страница 1 — чертёж с подписями, страница 2 — правила оформления подписей по-английски
(перевод §3–§8 ``docs/step4_offsets_rules.md``, без изменений в правилах).

Запуск: ``python docs/make_step4_offsets_pdf.py`` (нужен ``pymupdf``).
Результат: ``docs/step4_offsets_example.pdf`` и растры страниц в ``docs/img/``.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_field_dump as fd  # noqa: E402
from shapely.geometry import LineString, Polygon, box as shapely_box  # noqa: E402

PAIR = ROOT / "docs" / "primer5i.zip"
OUT_PDF = ROOT / "docs" / "step4_offsets_example.pdf"
OUT_IMG = ROOT / "docs" / "img"

INK = (0.0, 0.0, 0.0)
GREY = (0.45, 0.45, 0.45)
LIGHT = (0.75, 0.75, 0.75)
BLUE = (0.10, 0.25, 0.55)
RED = (0.65, 0.10, 0.10)

PAGE_W, PAGE_H = 595.0, 842.0  # A4 portrait, points

#: сдвиг подписи (dx, dy в футах, поворот в градусах или None) — чтобы текст не стоял на линиях.
NUDGE = {
    "FE.0.5'N": (-10.5, 7.4, 0.0),
    "FE.0.3'N": (-12.0, 0.4, 0.0),
    "FE.5.4'W": (2.6, -7.6, 0.0),
    "FE.5.3'W": (0.0, 3.0, 0.0),
    "FE.0.7'E|FE.0.3'N": (-10.0, -1.4, 0.0),
    "FE.0.4'E|FE.0.8'N": (-10.0, 3.6, 0.0),
    "BLDG|6.8'S": (-19.0, -2.0, 0.0),
    "FE.0.3'S|FE.0.1'W": (3.0, -2.0, 0.0),
    "2.0'N": (-2.2, 0.0, None),
}


# --------------------------------------------------------------------- геометрия


def load_sheet_data():
    """Что рисуем: участок, дом, заборы, улица, север и подписи Николая."""
    before, after = fd.load_pair(PAIR)
    geometry = fd.field_geometry(before, after)
    lot = fd._lot_polygon(geometry)
    assert lot is not None, "контур участка не восстановлен"
    ring = [(float(x), float(y)) for x, y in lot.exterior.coords]

    house, fences = [], []
    for feature in fd.features(geometry):
        if feature.kind == "house":
            house.append([(float(x), float(y)) for x, y in feature.points])
        elif feature.kind == "fence":
            fences.append([(float(x), float(y)) for x, y in feature.points])

    street = [
        [(float(p[0]), float(p[1])) for p in entity.get("pts") or []]
        for entity in geometry.get("entities", [])
        if str(entity.get("layer")).upper() in {"M-ROW-LINE", "S-CURB-LINE"}
        and entity.get("type") in {"Line", "Polyline"}
    ]
    labels = _nikolai_texts(before, after)
    return {
        "lot": ring,
        "house": house,
        "fences": fences,
        "street": street,
        "labels": labels,
        "north_deg": fd.north_deg(geometry),
        "area_sqft": lot.area,
    }


def _nikolai_texts(before: dict, after: dict) -> list[dict]:
    """Подписи, которые чертёжник добавил в файле «после»: текст, точка вставки, высота, поворот."""
    placed = {str(e.get("handle")): e for e in after.get("entities", []) if e.get("handle")}
    out = []
    for handle, _parts, point, _full in fd._added_texts(before, after):
        entity = placed[handle]
        text = str(entity.get("text") or "").replace("\\\\P", "\n").replace("\\P", "\n")
        out.append(
            {
                "text": text,
                "point": (float(point[0]), float(point[1])),
                "height_ft": float(entity.get("h") or 0.9),
                "rot_deg": float(entity.get("rot_deg") or 0.0),
            }
        )
    return out


# ------------------------------------------------------------------------ лист


class Sheet:
    """Страница с переводом координат чертежа (футы) в координаты листа (пункты)."""

    def __init__(self, page, box, scale, flip_x=False):
        self.page = page
        self.x0, self.y0, self.x1, self.y1 = box  # окно листа под чертёж
        self.scale = scale
        self.flip_x = flip_x

    def set_model(self, model_box):
        minx, miny, maxx, maxy = model_box
        self.model_box = model_box
        self.ox = self.x0 + (self.x1 - self.x0 - (maxx - minx) * self.scale) / 2.0 - minx * self.scale
        self.oy = self.y0 + (self.y1 - self.y0 - (maxy - miny) * self.scale) / 2.0 + maxy * self.scale
        return self

    def pt(self, point):
        x, y = point
        px = self.ox + x * self.scale
        if self.flip_x:
            px = self.x0 + self.x1 - (px - self.x0)
        return pymupdf.Point(px, self.oy - y * self.scale)

    def polyline(self, points, color=INK, width=0.8, close=False, dashes=None):
        pts = [self.pt(p) for p in points]
        if close and pts:
            pts = pts + [pts[0]]
        shape = self.page.new_shape()
        shape.draw_polyline(pts)
        shape.finish(color=color, width=width, dashes=dashes, closePath=False)
        shape.commit()

    def text(self, point, text, size, color=INK, rot=0.0, anchor_right=False):
        pt = self.pt(point) if isinstance(point, tuple) else point
        lines = text.split("\n")
        for index, line in enumerate(lines):
            width = pymupdf.get_text_length(line, "helv", size)
            origin = pymupdf.Point(pt.x - (width if anchor_right else 0.0), pt.y + index * size * 1.35)
            if rot:
                origin = origin - pymupdf.Point(size * 0.72, 0)
                morph = (origin, pymupdf.Matrix(rot))
                self.page.insert_text(origin, line, fontsize=size, fontname="helv",
                                      color=color, morph=morph)
            else:
                self.page.insert_text(origin, line, fontsize=size, fontname="helv", color=color)


def page_frame(page, title, subtitle, footer):
    page.draw_rect(pymupdf.Rect(24, 24, PAGE_W - 24, PAGE_H - 24), color=INK, width=1.2)
    page.insert_text((40, 48), title, fontsize=13, fontname="hebo", color=INK)
    page.insert_text((40, 62), subtitle, fontsize=8.5, fontname="helv", color=GREY)
    page.insert_text((40, PAGE_H - 34), footer, fontsize=7.5, fontname="helv", color=GREY)


# -------------------------------------------------------------------- страница 1


def draw_plan(doc, data):
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page_frame(
        page,
        "OFFSET LABELS — REFERENCE SHEET",
        "18 ZOE STREET, STATEN ISLAND, N.Y.  ·  BLOCK 3536, LOT 165  ·  SCALE 1\" = 16'",
        "Labels as the client places them (survey file “after”).  Text is set beside each feature; "
        "no leaders or arrows, per client requirement.",
    )

    lot = data["lot"]
    xs = [p[0] for p in lot]
    ys = [p[1] for p in lot]
    margin = 16.0
    model_box = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
    width_ft = model_box[2] - model_box[0]
    height_ft = model_box[3] - model_box[1]
    box = (52.0, 78.0, 380.0, 700.0)
    scale = min((box[2] - box[0]) / width_ft, (box[3] - box[1]) / height_ft)
    sheet = Sheet(page, box, scale).set_model(model_box)
    label_boost = 1.9  # на листе подписи крупнее чертёжных — иначе не читаются

    # улица: линии красной линии и бордюра, обрезанные по рамке чертежа
    clip = shapely_box(*model_box)
    for line in data["street"]:
        for part in _clip_line(line, clip):
            sheet.polyline(part, color=GREY, width=0.6)

    # дом: контур тонкой линией + штриховка
    for contour in data["house"]:
        _draw_house(page, sheet, contour)

    # заборы
    for fence in data["fences"]:
        sheet.polyline(fence, color=INK, width=0.7, dashes="[2 2] 0")

    # граница участка — самой толстой линией
    sheet.polyline(lot, color=BLUE, width=1.6, close=True)

    # подписи заказчика (без выносок). Точки вставки — его собственные; там, где они попадали
    # на линию участка или на соседнюю подпись (он ставил их под выноску, а выносок мы не пишем),
    # подпись сдвинута в свободное место рядом — сдвиги в футах, для читаемости листа.
    for label in data["labels"]:
        size = label["height_ft"] * scale * label_boost
        nudge = NUDGE.get(label["text"].replace("\n", "|"), (0.0, 0.0, None))
        point = (label["point"][0] + nudge[0], label["point"][1] + nudge[1])
        rot = nudge[2] if nudge[2] is not None else -label["rot_deg"]
        sheet.text(point, label["text"], size, color=INK, rot=rot)

    # север
    _north_arrow(page, sheet, data["north_deg"], model_box)

    # масштабная линейка 10 ft
    _scale_bar(page, sheet, model_box)

    # стрелка/указатель на улицу
    _street_name(page, sheet, data["street"], model_box)

    # штамп и пояснения справа
    _title_block(page, data)
    _layers_note(page)
    return page


def _draw_house(page, sheet, contour):
    """Дом: контур, светлая заливка и штриховка под 45° строго внутри контура."""
    if len(contour) < 4 or math.dist(contour[0], contour[-1]) > 1e-6:
        sheet.polyline(contour, color=INK, width=0.9)  # открытый контур (соседнее строение)
        return
    polygon = Polygon(contour)
    pts = [sheet.pt((x, y)) for x, y in contour]
    shape_fill = page.new_shape()
    shape_fill.draw_polyline(pts)
    shape_fill.finish(color=INK, fill=(0.93, 0.93, 0.93), width=0.9, closePath=True)
    shape_fill.commit()

    shape = page.new_shape()
    minx, miny, maxx, maxy = polygon.bounds
    value = minx - (maxy - miny)
    while value < maxx:
        hatch = LineString([(value, miny), (value + (maxy - miny), maxy)])
        clipped = hatch.intersection(polygon)
        for part in getattr(clipped, "geoms", [clipped]):
            if part.is_empty or part.geom_type != "LineString":
                continue
            coords = list(part.coords)
            shape.draw_line(sheet.pt(coords[0]), sheet.pt(coords[-1]))
        value += 2.5
    shape.finish(color=(0.72, 0.72, 0.72), width=0.35)
    shape.commit()


def _clip_line(points, clip):
    """Части линии, попавшие в рамку чертежа."""
    line = LineString(points).intersection(clip)
    out = []
    for part in getattr(line, "geoms", [line]):
        if part.is_empty or part.geom_type != "LineString":
            continue
        out.append([(float(x), float(y)) for x, y in part.coords])
    return out


def _north_arrow(page, sheet, north_deg, model_box):
    centre = (model_box[2] - 8.0, model_box[3] - 10.0)
    c = sheet.pt(centre)
    length = 26.0
    rad = math.radians(north_deg)
    tip = pymupdf.Point(c.x + math.cos(rad) * length, c.y - math.sin(rad) * length)
    tail = pymupdf.Point(c.x - math.cos(rad) * length * 0.55, c.y + math.sin(rad) * length * 0.55)
    page.draw_line(tail, tip, color=INK, width=1.0)
    for sign in (-1, 1):
        wing = pymupdf.Point(
            tip.x - math.cos(rad) * 7.0 + sign * math.sin(rad) * 3.6,
            tip.y + math.sin(rad) * 7.0 + sign * math.cos(rad) * 3.6,
        )
        page.draw_line(tip, wing, color=INK, width=1.0)
    page.insert_text((tip.x + 3, tip.y - 2), "N", fontsize=8, fontname="hebo", color=INK)
    page.insert_text((c.x - 12, c.y + 34), f"north {north_deg:.2f}°",
                     fontsize=6.5, fontname="helv", color=GREY)


def _scale_bar(page, sheet, model_box):
    y = model_box[1] + 4.0
    x0 = model_box[0] + 2.0
    for index in range(2):
        a = (x0 + index * 10.0, y)
        b = (x0 + (index + 1) * 10.0, y)
        page.draw_line(sheet.pt(a), sheet.pt(b), color=INK,
                       width=2.4 if index % 2 == 0 else 0.8)
    label_pt = sheet.pt((x0, y - 2.5))
    page.insert_text((label_pt.x, label_pt.y - 2), "0", fontsize=6, fontname="helv", color=INK)
    mid = sheet.pt((x0 + 10.0, y - 2.5))
    page.insert_text((mid.x - 5, mid.y - 2), "10'", fontsize=6, fontname="helv", color=INK)
    end = sheet.pt((x0 + 20.0, y - 2.5))
    page.insert_text((end.x - 5, end.y - 2), "20'", fontsize=6, fontname="helv", color=INK)


def _street_name(page, sheet, street, model_box):
    """Название улицы — вдоль самой верхней линии улицы, выше участка."""
    if not street:
        return
    best = None
    for points in street:
        for a, b in zip(points, points[1:]):
            if math.dist(a, b) < 20.0:
                continue
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            if best is None or mid[1] > best[0][1]:
                best = (mid, a, b)
    if best is None:
        return
    mid, a, b = best
    angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    if angle > 90:
        angle -= 180
    elif angle < -90:
        angle += 180
    spot = (mid[0] - 26.0, mid[1] + 7.0)
    pt = sheet.pt(spot)
    page.insert_text(pt, "ZOE  STREET", fontsize=8, fontname="hebo", color=GREY,
                     morph=(pt, pymupdf.Matrix(angle)))


def _layers_note(page):
    """Пояснение: тремя способами записанная линия участка на этом же листе."""
    x, y = 52.0, 712.0
    page.insert_text((x, y), "LOT LINE — THREE WAYS IT IS DRAWN", fontsize=8, fontname="hebo", color=BLUE)
    rows = [
        ("PL", "lot line as the surveyor draws it now (dash-and-dot on the print)"),
        ("PL-PRINT", "lot line printed by this program"),
        ("ST-BLDG-LINE", "old layer name — the house walls are on it as well; the lot is found"),
    ]
    for index, (layer, text) in enumerate(rows):
        row_y = y + 14 + index * 11
        page.insert_text((x, row_y), layer, fontsize=7, fontname="hebo", color=INK)
        page.insert_text((x + 74, row_y), text, fontsize=7, fontname="helv", color=GREY)
    page.insert_text((x + 74, y + 47 + 11), "by the side that runs along the street line",
                     fontsize=7, fontname="helv", color=GREY)


def _title_block(page, data):
    x = 400.0
    y = 96.0
    page.draw_rect(pymupdf.Rect(x, y, PAGE_W - 40, 356), color=INK, width=0.8)
    page.insert_text((x + 8, y + 16), "SURVEY DATA", fontsize=8, fontname="hebo", color=INK)
    rows = [
        ("Address", "18 Zoe Street"),
        ("Borough", "Staten Island, Richmond Co., NY"),
        ("Block / Lot", "3536 / 165"),
        ("Lot area", f"{data['area_sqft']:,.0f} sq ft"),
        ("Frontage", "44.00 ft on Zoe Street"),
        ("Surveyed", "October 2, 2026"),
    ]
    for index, (key, value) in enumerate(rows):
        line_y = y + 32 + index * 13
        page.insert_text((x + 8, line_y), key, fontsize=7, fontname="helv", color=GREY)
        page.insert_text((x + 90, line_y), value, fontsize=7, fontname="helv", color=INK)
    page.draw_line(pymupdf.Point(x, y + 116), pymupdf.Point(PAGE_W - 40, y + 116),
                   color=LIGHT, width=0.6)

    legend_y = y + 132
    page.insert_text((x + 8, legend_y), "LABEL LEGEND", fontsize=8, fontname="hebo", color=INK)
    legend = [
        ("FE.0.4'N", "fence offset: 0.4 ft on the north side of the fence"),
        ("3.2'S", "building wall offset, 3.2 ft to the south"),
        ("FE.O/L", "fence stands on the lot line (no value)"),
        ("BLDG O/L", "building wall on the street line"),
        ("2 lines", "two nearest sides in one MText block (\\P)"),
        ("no arrow", "text sits beside the feature, no leader"),
    ]
    for index, (code, meaning) in enumerate(legend):
        row_y = legend_y + 14 + index * 20
        if code in {"2 lines", "no arrow"}:
            page.insert_text((x + 8, row_y), code, fontsize=7, fontname="hebo", color=INK)
        else:
            page.insert_text((x + 8, row_y), code, fontsize=8, fontname="helv", color=RED)
        page.insert_text((x + 8, row_y + 8), meaning, fontsize=6.5, fontname="helv", color=GREY)


# -------------------------------------------------------------------- страница 2


RULES = [
    ("1. WHAT IS MEASURED", [
        "A label gives the clear distance from a feature (fence corner, building wall) to the boundary of the lot.",
        "The reference is the lot line (PL) and the street right-of-way line (M-ROW-LINE); the widening line",
        "(M-TAX-LINE) is not a lot boundary.",
        "The perpendicular may land on the extension of a side, up to 5 ft beyond its end.",
        "The value is rounded to 0.1 ft, halves up: 0.35 → 0.4, 5.17 → 5.2.",
        "Below 0.05 ft the feature stands ON the line: no value, the label reads O/L.",
    ]),
    ("2. TEXT OF A LABEL", [
        "Fence: FE. + value + side letter  →  FE.0.4'N, FE.5.2'S.   Fence on the line: FE.O/L.",
        "Building: value + side letter only  →  3.2'S, 0.4'W  (no FE., no prefix).  On the street line: BLDG O/L.",
        "Two near sides of one corner (second side closer than 2 ft) go into one block, second line under the first:",
        "FE.0.3'S / FE.0.1'W — the FE. prefix is repeated on every line; lines are stacked, not set in one row.",
    ]),
    ("3. SIDE LETTER", [
        "The vector runs from the boundary to the feature: the letter names the side where the feature stands.",
        "North comes from the surveyor's north arrow closest to the lot, not from the sheet orientation.",
        "A diagonal side takes the letter by the sheet axes (up = N, right = E) — as the client does.",
    ]),
    ("4. SIZE", [
        "At scale 1\" = 16': fence labels 0.9 ft high, building labels 1.1 ft high; heights scale with the drawing.",
        "All labels go to layer D-TOPO-TEXT, one undo group per operation.",
        "On this sheet the label heights are enlarged 1.9× so that the text is readable in a PDF viewer.",
    ]),
    ("5. PLACEMENT", [
        "Text is set beside the feature, on the far side from the lot line, with a gap of 0.3 text heights.",
        "No leaders, no arrows: this is the client's requirement for the finished sheet.",
        "Text never crosses lot, street or feature lines and never covers an existing note.",
        "The label may stand outside the lot: the client sets 4 of 8 labels outside the boundary.",
        "A label follows its line: 0° beside a horizontal line, 90° beside a vertical one.",
    ]),
    ("6. WHAT THE PROGRAM NEVER DRAWS", [
        "No dimension lines, no extension lines, no arrows: only the text block of each offset label.",
        "No edits to existing notes: if a label does not fit, it moves aside and the report warns instead.",
        "One undo group per run, one layer (D-TOPO-TEXT); the drawing is never changed anywhere else.",
    ]),
    ("7. CHECKED ON THE CLIENT'S FIELD PAIRS", [
        "The program is measured on five “before / after” drawings: 192 Geldner, 24 Alvine, 1373 East 55th,",
        "378 Mother Gaston (all under test) and 18 Zoe Street (new pair, work in progress).  On the four tested",
        "drawings: 36 of 51 client parts matched, 59 labels placed, none over an existing text.",
    ]),
    ("8. DRAWING WITHOUT STREET LINE", [
        "The lot may be closed by the street line when the surveyor left the front side open; the missing side is",
        "taken from M-ROW-LINE and the report says so.  If the surveyor drew the lot on the old layer name",
        "(ST-BLDG-LINE, the same layer as the house walls), the lot is recognised by the side that runs along the",
        "street line, and the house on that layer is not taken for the lot.",
    ]),
]


def draw_rules(doc):
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page_frame(
        page,
        "OFFSET LABELS — STYLE RULES",
        "What the program writes and how it must look on the sheet  ·  rules as implemented in version 4.37",
        "Full rule book (in Russian) with every flag: docs/step4_offsets_rules.md  ·  this sheet: English summary.",
    )

    y = 88.0
    for heading, lines in RULES:
        page.insert_text((40, y), heading, fontsize=9.5, fontname="hebo", color=BLUE)
        y += 13
        for line in lines:
            page.insert_text((48, y), line, fontsize=8, fontname="helv", color=INK)
            y += 11
        y += 9

    _rules_picture(page, 40.0, 612.0)
    _two_line_picture(page, 320.0, 612.0)
    return page


def _rules_picture(page, x, y):
    """Схема: линия участка, вершина забора, перпендикуляр и подпись."""
    page.insert_text((x, y), "HOW A VALUE IS TAKEN", fontsize=9.5, fontname="hebo", color=BLUE)

    line_y = y + 106.0
    page.draw_line(pymupdf.Point(x, line_y), pymupdf.Point(x + 240, line_y), color=BLUE, width=1.6)
    page.insert_text((x, line_y + 14), "lot line (PL)", fontsize=7, fontname="helv", color=GREY)

    box = pymupdf.Rect(x + 110, line_y - 42, x + 174, line_y - 14)
    page.draw_rect(box, color=INK, width=0.8, fill=(0.95, 0.95, 0.95))
    page.insert_text((x + 180, line_y - 26), "fence corner", fontsize=7, fontname="helv", color=GREY)

    page.draw_line(pymupdf.Point(x + 142, line_y), pymupdf.Point(x + 142, line_y - 14),
                   color=RED, width=0.7, dashes="[2 2] 0")
    page.insert_text((x + 147, line_y - 4), "0.4'", fontsize=8, fontname="hebo", color=RED)

    arrow_x = x + 70
    page.draw_line(pymupdf.Point(arrow_x, line_y - 16), pymupdf.Point(arrow_x, line_y - 44),
                   color=GREY, width=0.7)
    page.draw_line(pymupdf.Point(arrow_x, line_y - 44), pymupdf.Point(arrow_x - 3.5, line_y - 37),
                   color=GREY, width=0.7)
    page.draw_line(pymupdf.Point(arrow_x, line_y - 44), pymupdf.Point(arrow_x + 3.5, line_y - 37),
                   color=GREY, width=0.7)
    page.insert_text((arrow_x + 5, line_y - 38), "N", fontsize=7, fontname="helv", color=GREY)

    page.insert_text((x, y + 26), "FE.0.4'N", fontsize=12, fontname="helv", color=RED)
    page.insert_text((x, y + 42), "text beside the feature, no leader, 0.9 ft high",
                     fontsize=6.5, fontname="helv", color=GREY)


def _two_line_picture(page, x, y):
    """Схема: две стороны у одной вершины — столбиком в одном блоке MText."""
    page.insert_text((x, y), "TWO SIDES, ONE BLOCK", fontsize=9.5, fontname="hebo", color=BLUE)

    line_y = y + 106.0
    page.draw_line(pymupdf.Point(x, line_y), pymupdf.Point(x + 200, line_y), color=BLUE, width=1.6)
    page.draw_line(pymupdf.Point(x + 150, line_y), pymupdf.Point(x + 150, line_y - 60),
                   color=BLUE, width=1.6)
    page.insert_text((x, line_y + 14), "lot corner", fontsize=7, fontname="helv", color=GREY)

    page.draw_circle(pymupdf.Point(x + 132, line_y - 26), 1.6, color=GREY, fill=GREY)
    page.draw_line(pymupdf.Point(x + 150, line_y - 12), pymupdf.Point(x + 132, line_y - 26),
                   color=LIGHT, width=0.6, dashes="[2 2] 0")

    page.insert_text((x, y + 26), "FE.0.3'S", fontsize=12, fontname="helv", color=RED)
    page.insert_text((x, y + 42), "FE.0.1'W", fontsize=12, fontname="helv", color=RED)
    page.insert_text((x, y + 60), "two sides within 2 ft of one fence corner —",
                     fontsize=6.5, fontname="helv", color=GREY)
    page.insert_text((x, y + 70), "one MText block, one line per side",
                     fontsize=6.5, fontname="helv", color=GREY)


# --------------------------------------------------------------------------- main


def main() -> None:
    data = load_sheet_data()
    doc = pymupdf.open()
    draw_plan(doc, data)
    draw_rules(doc)
    doc.set_metadata(
        {
            "title": "Offset labels — reference sheet (18 Zoe Street)",
            "subject": "Step 4 offset labels: how the finished sheet must look",
            "creator": "CAD offset labels, version 4.34",
        }
    )
    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT_PDF, deflate=True, garbage=3)

    OUT_IMG.mkdir(parents=True, exist_ok=True)
    for index, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=170)
        pix.save(OUT_IMG / f"step4_offsets_example_page{index}.png")
    print(f"готово: {OUT_PDF.relative_to(ROOT)} ({OUT_PDF.stat().st_size} байт), "
          f"страниц {doc.page_count}")


if __name__ == "__main__":
    main()
