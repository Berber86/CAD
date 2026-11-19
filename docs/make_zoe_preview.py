"""
Наглядная картинка по пятой полевой паре — «18 Zoe Street, Staten Island».

Слева-вверху — условные обозначения, дальше поверх **того же PDF заказчика** (растр, 1"=16')
нарисовано то, что программа выдаёт сейчас (4.27):

* зелёным — граница участка, найденная в новом слое ``PL`` (4469.9 sq ft, на PDF 4,470.0);
* синим — наши подписи оффсетов (высота 0.9 ft для забора, 1.1 ft для дома);
* красным — подписи Николая, которым у нас пары нет (обведено место и написано его значение).

Красные подписи — это вопросы к следующей версии, а не «как надо»; картинка показывает разрыв между
нашим результатом и его чертежом на одном и том же листе.

Запуск из корня репозитория::

    python docs/make_zoe_preview.py

Нужны ``pymupdf`` и ``Pillow`` (см. ``requirements.txt``); matplotlib не требуется.
"""

from __future__ import annotations

import io
import math
import sys
import zipfile
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from version import APP_VERSION  # noqa: E402
from test_field_dump import (  # noqa: E402
    _kind_of, _lot_polygon, _parts, _zip_name, field_plan, load_pair, nikolai_labels, plan_labels,
    score_labels,
)

ZIP = ROOT / "docs" / "primer5i.zip"
OUT = ROOT / "docs" / "step4_zoe_preview.png"

#: Масштаб листа заказчика: 1"=16' (подписано на самом листе «SCALE: 1"=16'»).
PT_PER_FT = 4.5
#: Точность рендера листа: 200 dpi — подписи 0.9 ft получаются ~25 px и читаются.
DPI = 300
#: Окно листа, которое попадает на картинку: участок целиком + подписи вокруг.
VIEW = (4948.0, 4954.0, 5019.0, 5100.0)  # x0, y0, x1, y1 в координатах дампа

BLUE = (0, 52, 160)
RED = (198, 18, 28)
GREEN = (0, 122, 51)
GREY = (110, 110, 110)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def load_pdf_bytes() -> bytes:
    """PDF лежит внутри ``docs/primer5i.zip`` рядом с двумя дампами."""
    with zipfile.ZipFile(ZIP) as archive:
        for info in archive.infolist():
            if _zip_name(info).lower().endswith(".pdf"):
                return archive.read(info)
    raise SystemExit("в docs/primer5i.zip нет PDF")


def register(page, entities, *, dpi: int = 60) -> tuple[float, float, float]:
    """
    Куда на растре листа попадает чертёж.

    Масштаб известен из самого листа (1"=16'), поэтому ищем только сдвиг: рисуем растром контуры
    объектов дампа (окно ``VIEW``) и ищем максимум корреляции с растром страницы.
    """
    scale = PT_PER_FT * dpi / 72.0  # пикселей на фут
    x0, y0, x1, y1 = VIEW
    width = int((x1 - x0) * scale) + 2
    height = int((y1 - y0) * scale) + 2
    template = Image.new("L", (width, height), 0)
    pen = ImageDraw.Draw(template)

    def to_px(point):
        return ((point[0] - x0) * scale, (y1 - point[1]) * scale)

    for entity in entities:
        kind = entity.get("type")
        if kind in ("Line", "Polyline"):
            points = entity.get("pts") or []
            if len(points) > 1:
                pen.line([to_px(p) for p in points], fill=255, width=1)
                if entity.get("closed"):
                    pen.line([to_px(points[-1]), to_px(points[0])], fill=255, width=1)
        elif kind in ("Text", "MText", "Arc"):
            box = entity.get("bbox")
            if box:
                pen.rectangle([to_px((box[0], box[3])), to_px((box[2], box[1]))], outline=255)
        elif kind in ("Point", "BlockReference"):
            point = entity.get("pt") or entity.get("insert")
            if point:
                px, py = to_px(point)
                pen.ellipse([px - 1, py - 1, px + 1, py + 1], outline=255)

    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    page_grey = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    ink = (255.0 - page_grey) / 255.0
    mask = np.asarray(template, dtype=np.float64) / 255.0
    mask -= mask.mean()
    corr = np.fft.irfft2(
        np.fft.rfft2(ink) * np.conj(np.fft.rfft2(mask, s=ink.shape)), s=ink.shape
    )
    index = int(np.argmax(corr))
    dy, dx = divmod(index, ink.shape[1])
    return scale, float(dx), float(dy)


def match_pairs(produced, expected, radius: float = 6.0, tol: float = 0.11):
    """Те же пары «наша подпись — подпись Николая», что считает ``score_labels``."""
    candidates = []
    for i, label in enumerate(produced):
        for j, target in enumerate(expected):
            if target["kind"] != label["kind"]:
                continue
            distance = math.dist(target["anchor"], label["anchor"])
            if distance > radius:
                continue
            shared = sum(
                1
                for value, direction in label["parts"]
                for other_value, other_direction in target["parts"]
                if direction == other_direction and abs(value - other_value) <= tol
            )
            if shared:
                candidates.append((-shared, distance, i, j))
    candidates.sort()
    pairs, used_produced, used_expected = [], set(), set()
    for _shared, _distance, i, j in candidates:
        if i in used_produced or j in used_expected:
            continue
        used_produced.add(i)
        used_expected.add(j)
        pairs.append((i, j))
    return pairs


def label_text(label) -> str:
    """`BLDG` / `FE.` + значения, как в дампе заказчика (две части — через `|`)."""
    prefix = "BLDG" if label["kind"] == "house" else "FE."
    return "|".join(f"{prefix}{value:.1f}'{direction}" for value, direction in label["parts"])


def main() -> None:
    before, after = load_pair(str(ZIP))
    data, _ = field_plan(before, after)
    # Наши подписи вместе с готовым местом вставки: на картинке видно не только «что подписано»,
    # но и где именно встанет текст и какой длины будет выноска.
    ours = []
    for item in data["plan"].items:
        parts = _parts(item.text.replace("\n", " "))
        if not parts:
            continue
        kinds = {_kind_of(label) for label, _v, _d in parts}
        ours.append({
            "kind": "fence" if "fence" in kinds else "wall" if "wall" in kinds else "house",
            "parts": [(value, direction) for _l, value, direction in parts],
            "anchor": (float(item.anchor[0]), float(item.anchor[1])),
            "text": item.text,
            "item": item,
        })
    theirs = nikolai_labels(before, after)
    pairs = match_pairs(ours, theirs)
    matched_ours = {i for i, _ in pairs}
    matched_theirs = {j for _, j in pairs}

    doc = pymupdf.open(stream=load_pdf_bytes(), filetype="pdf")
    page = doc[0]
    scale, dx, dy = register(page, after["entities"])
    # Пересчёт найденного сдвига на рабочий dpi.
    ratio = DPI / 60.0
    s = scale * ratio
    ox, oy = dx * ratio, dy * ratio

    pix = page.get_pixmap(dpi=DPI)
    sheet = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    # Светлеет лист, чтобы цветные подписи читались поверх тонких линий.
    sheet = Image.blend(sheet, Image.new("RGB", sheet.size, "white"), 0.35)
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(FONT_PATH, max(11, int(0.9 * s * 1.05)))
    font_bold = ImageFont.truetype(FONT_BOLD, max(11, int(0.9 * s * 1.05)))
    font_small = ImageFont.truetype(FONT_PATH, max(10, int(0.75 * s)))

    def px(point):
        return ((point[0] - VIEW[0]) * s + ox, (VIEW[3] - point[1]) * s + oy)

    def halo_text(where, text, colour, font_, anchor="lm"):
        draw.text(where, text, font=font_, fill=colour, anchor=anchor,
                  stroke_width=3, stroke_fill="white")

    # Граница участка, найденная программой (слой PL).
    ring = list(data["ring"])
    draw.line([px(p) for p in ring] + [px(ring[0])], fill=GREEN, width=4, joint="curve")
    area = _lot_polygon(before).area

    placed: list[tuple[float, float, float, float]] = []

    def overlaps(box) -> bool:
        return any(
            not (box[2] < other[0] - 4 or box[0] > other[2] + 4
                 or box[3] < other[1] - 4 or box[1] > other[3] + 4)
            for other in placed
        )

    def place_label(anchor_px, text, colour, font_, *, ring: bool = False, lead: bool = True):
        """Плашка с подписью и поводок к точке: перебираем места, пока не встанет свободно."""
        x, y = anchor_px
        tw = draw.textlength(text, font=font_)
        th = font_.size * 1.25
        pad = 5
        radius = 1.1 * s if ring else 0.0
        if ring:
            draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=colour, width=3)
        candidates = []
        for dx_ft, dy_ft in ((1.4, 0.55), (1.4, -1.2), (-1.4, 0.55), (-1.4, -1.2),
                             (2.6, 1.5), (2.6, -2.2), (-2.6, 1.5), (-2.6, -2.2),
                             (0.4, 1.6), (0.4, -2.3), (0.0, 2.6), (0.0, -3.3)):
            side = 1.0 if dx_ft > 0 else -1.0
            bx = x + dx_ft * s
            box = (bx if side > 0 else bx - tw - 2 * pad, y - dy_ft * s - th - pad,
                   (bx + tw + 2 * pad) if side > 0 else bx, y - dy_ft * s + pad)
            candidates.append(box)
        best = None
        for box in candidates:
            if not overlaps(box):
                best = box
                break
        if best is None:
            best = candidates[0]
        placed.append(best)
        # поводок от точки до ближайшего края плашки
        if lead:
            ex = min(max(x, best[0]), best[2])
            ey = min(max(y, best[1]), best[3])
            draw.line([(x, y), (ex, ey)], fill=colour, width=2)
        fill = (220, 240, 226) if colour == GREEN else "white"
        draw.rounded_rectangle(best, radius=4, fill=fill, outline=colour, width=3)
        draw.text((best[0] + pad, (best[1] + best[3]) / 2), text, font=font_, fill=colour, anchor="lm")

    for index, label in enumerate(ours):
        # Совпавшая с Николаем подпись — зелёная рамка, без пары — синяя.
        colour = GREEN if index in matched_ours else BLUE
        item = label["item"]
        if item.leader:
            start, end = px(item.leader[0]), px(item.leader[1])
            draw.line([start, end], fill=colour, width=2)
            draw.ellipse([start[0] - 3, start[1] - 3, start[0] + 3, start[1] + 3], fill=colour)
        place_label(px(item.point), item.text.replace("\n", " | "), colour, font_bold, lead=False)

    for index, label in enumerate(theirs):
        if index in matched_theirs:
            continue
        place_label(px(label["anchor"]), label_text(label), RED, font, ring=True)

    # Окно картинки — участок и подписи вокруг.
    left, top = px((VIEW[0], VIEW[3]))
    right, bottom = px((VIEW[2], VIEW[1]))
    box = (max(0, int(left)), max(0, int(top)),
           min(sheet.width, int(right)), min(sheet.height, int(bottom)))
    sheet = sheet.crop(box)

    # ---------------------------------------------------------------- шапка и подвал
    title = ImageFont.truetype(FONT_BOLD, 34)
    body = ImageFont.truetype(FONT_PATH, 25)
    small = ImageFont.truetype(FONT_PATH, 23)
    hits = score_labels(ours, theirs)
    title_text = f"18 Zoe Street, Staten Island — 5-я полевая пара, версия {APP_VERSION}"
    summary_text = (
        f"граница участка — слой PL: {area:.1f} sq ft (на листе 4,470.0)   ·   "
        f"наших подписей {len(ours)}, совпало с Николаем {hits}, у него всего {len(theirs)}"
    )
    legend = [
        (GREEN, "граница участка (PL); наши подписи в зелёной рамке совпали с подписями Николая"),
        (BLUE, "наши подписи оффсетов без пары у Николая (забор 0.9 ft, дом 1.1 ft)"),
        (RED, "есть у Николая, у нас нет — вопрос следующей версии"),
    ]
    missing = [label for index, label in enumerate(theirs) if index not in matched_theirs]
    footer_lines = []
    if missing:
        footer_lines.append("Расхождения (подпись Николая → наша у той же вершины):")
        for label in missing:
            near = min(
                (item for item in ours if item["kind"] == label["kind"]),
                key=lambda item: math.dist(item["anchor"], label["anchor"]),
                default=None,
            )
            distance = math.dist(near["anchor"], label["anchor"]) if near else None
            where = f"у нас {label_text(near)} в {distance:.0f} ft" if near else "нашей подписи нет"
            footer_lines.append(f"   • {label['kind']:5s} {label_text(label):20s} — {where}")
    footer_h = 20 + (24 + 30 * len(footer_lines) if footer_lines else 0)
    header_h = 30 + 42 + 30 + 12 + len(legend) * 38 + 10

    margin = 24
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    rows = [(title_text, title), (summary_text, body)]
    rows += [(text, body) for _colour, text in legend]
    rows += [(line, small) for line in footer_lines]
    needed = max(margin + 64 + int(probe.textlength(text, font=font_)) + margin for text, font_ in rows)
    width = max(needed, sheet.width + 2 * margin)
    canvas = Image.new("RGB", (width, header_h + sheet.height + footer_h), "white")
    draw2 = ImageDraw.Draw(canvas)
    draw2.text((margin, 26), title_text, font=title, fill=(20, 20, 20))
    draw2.text((margin, 26 + 42), summary_text, font=body, fill=(60, 60, 60))
    y = 26 + 42 + 30 + 12
    for colour, text in legend:
        draw2.rectangle([margin + 4, y + 4, margin + 26, y + 20], outline=colour, width=4)
        draw2.text((margin + 64, y), text, font=body, fill=colour)
        y += 38
    canvas.paste(sheet, ((width - sheet.width) // 2, header_h))
    y = header_h + sheet.height + 12
    for index, line in enumerate(footer_lines):
        draw2.text((margin, y), line, font=small, fill=(20, 20, 20) if index == 0 else (70, 70, 70))
        y += 30

    canvas.save(OUT)
    print(f"{OUT.relative_to(ROOT)}: {canvas.width}x{canvas.height}, сдвиг листа {ox:.1f},{oy:.1f} px, "
          f"{s:.2f} px/ft, совпало {hits} из {len(theirs)}")


if __name__ == "__main__":
    main()
