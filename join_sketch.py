"""
Схема фигур для ИИ-советника («склейка по карте»).

Рисует участки набора так, как программа их сейчас расставляет: все фигуры в одной
системе координат (футы, (0, 0) — угол улицы, к которому привязан каждый участок).
На схеме отмечены номера углов (угол k — начало стороны k) и номера сторон.
Только Pillow, без GUI.
"""

from __future__ import annotations

import base64
import io
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

#: Цвета: наш участок — синий, соседи — по кругу.
_MAIN_COLOR = (29, 78, 216)
_NEIGHBOUR_COLORS = [(180, 83, 9), (4, 120, 87), (124, 58, 237), (190, 18, 60)]
_FONT_CANDIDATES = [
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # старый Pillow без размера у встроенного шрифта
        return ImageFont.load_default()


def render_sketch(
    figures: Sequence[tuple[str, str, bool, Sequence[tuple[float, float]]]],
    *,
    width: int = 1100,
) -> bytes:
    """
    PNG со схемой. ``figures`` — ``(обозначение, название, наш ли, вершины)``;
    вершина k (с нуля) — начало стороны k+1, сторона k+1 идёт к следующей вершине.
    """
    points = [pt for _, _, _, verts in figures for pt in verts]
    if not points:
        raise ValueError("нет фигур для схемы")
    min_x = min(x for x, _ in points)
    max_x = max(x for x, _ in points)
    min_y = min(y for _, y in points)
    max_y = max(y for _, y in points)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)

    margin = 60
    legend_h = 28 * (len(figures) + 2)
    scale = (width - 2 * margin) / span_x
    height = int(span_y * scale + 2 * margin + legend_h)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = _font(15)
    small = _font(13)

    def to_px(x: float, y: float) -> tuple[float, float]:
        px = margin + (x - min_x) * scale
        py = height - legend_h - margin - (y - min_y) * scale
        return px, py

    # Начало отсчёта (0, 0) — угол улицы; у каждого участка он свой, но в этой системе общий.
    ox, oy = to_px(0.0, 0.0)
    if margin <= ox <= width - margin and margin <= oy <= height - legend_h:
        draw.line([(ox - 10, oy), (ox + 10, oy)], fill=(120, 120, 120), width=1)
        draw.line([(ox, oy - 10), (ox, oy + 10)], fill=(120, 120, 120), width=1)
        draw.text((ox + 12, oy + 4), "0,0", fill=(120, 120, 120), font=small)

    neighbour_index = 0
    for label, name, is_main, verts in figures:
        if is_main:
            color = _MAIN_COLOR
        else:
            color = _NEIGHBOUR_COLORS[neighbour_index % len(_NEIGHBOUR_COLORS)]
            neighbour_index += 1
        pix = [to_px(x, y) for x, y in verts]
        n = len(pix)
        for i in range(n):
            draw.line([pix[i], pix[(i + 1) % n]], fill=color, width=3)
        for i in range(n):
            # Номер стороны — у середины, с белой подложкой.
            (x1, y1), (x2, y2) = pix[i], pix[(i + 1) % n]
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            text = str(i + 1)
            left, top, right, bottom = draw.textbbox((mx, my), text, font=small)
            draw.rectangle([left - 2, top - 1, right + 2, bottom + 1], fill="white")
            draw.text((mx, my), text, fill=color, font=small)
        for i, (px, py) in enumerate(pix):
            # Номер угла — у вершины, крупнее и с кружком.
            draw.ellipse([px - 4, py - 4, px + 4, py + 4], fill=color)
            draw.text((px + 7, py - 17), f"{i + 1}", fill=color, font=font)

    # Легенда внизу.
    y = height - legend_h + 8
    draw.text((margin, y), "Углы — крупные номера у вершин; стороны — номера у середины.", fill=(40, 40, 40), font=small)
    y += 28
    neighbour_index = 0
    for label, name, is_main, _ in figures:
        if is_main:
            color = _MAIN_COLOR
        else:
            color = _NEIGHBOUR_COLORS[neighbour_index % len(_NEIGHBOUR_COLORS)]
            neighbour_index += 1
        draw.rectangle([margin, y + 4, margin + 18, y + 18], fill=color)
        role = "наш участок" if is_main else "сосед"
        draw.text((margin + 28, y), f"{label} — {role} «{name}»", fill=color, font=font)
        y += 28

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def as_data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
