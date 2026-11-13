"""
make_preview.py
===============
Рендер превью раскладки участка в PNG (без AutoCAD) — для документации и
быстрой визуальной проверки геометрии/подписей.

Использует ту же математику, что и отрисовка: acad_builder.AcadBuilder.plan().
Текст рисуется в стиле топосъёмки: румб — у начала стороны, длина — у конца,
обе подписи снаружи контура и повёрнуты вдоль линии (растяжка пробелами,
которая используется в DBTEXT, здесь заменена разнесением двух частей).

Запуск:
    python docs/make_preview.py exemplo.txt docs/preview.png
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Polygon as MplPolygon  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acad_builder import AcadBuilder, text_rotation  # noqa: E402
from llm_parser import load_config, parse_with_regex  # noqa: E402


def render(text: str, out_path: Path, scale: float = 16.0) -> None:
    description, _ = parse_with_regex(text)
    builder = AcadBuilder(load_config())
    plan = builder.plan(description, scale=scale)

    vertices = plan.vertices
    fig, ax = plt.subplots(figsize=(11, 8.5), dpi=140)
    ax.add_patch(
        MplPolygon(vertices, closed=True, facecolor="#d62828", alpha=0.06,
                   edgecolor="#d62828", linewidth=1.8, joinstyle="miter")
    )

    # --- подписи сторон: румб у начала, длина у конца --------------------
    count = len(vertices)
    for index in range(count):
        start = vertices[index]
        end = vertices[(index + 1) % count]
        nx, ny, length = builder._outward_normal(start, end, plan.ccw)
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        text_angle = math.degrees(text_rotation(angle))
        offset = 1.5 * plan.text_height
        segment = description.segments[index]

        bearing = segment.bearing_label
        distance = f"{segment.distance_ft:.2f}'"
        # румб — в первой трети стороны, длина — в последней
        b_point = (start[0] * 0.62 + end[0] * 0.38, start[1] * 0.62 + end[1] * 0.38)
        d_point = (start[0] * 0.22 + end[0] * 0.78, start[1] * 0.22 + end[1] * 0.78)
        for point, label, ha in ((b_point, bearing, "center"), (d_point, distance, "center")):
            ax.text(
                point[0] + nx * offset,
                point[1] + ny * offset,
                label,
                rotation=text_angle,
                rotation_mode="anchor",
                ha=ha,
                va="center",
                fontsize=7.5,
                color="#0b6e4f",
                family="DejaVu Sans Mono",
            )

    # --- улицы -----------------------------------------------------------
    street_color = "#1d4ed8"
    cx = sum(v[0] for v in vertices) / len(vertices)
    cy = sum(v[1] for v in vertices) / len(vertices)
    for item in plan.items:
        if item.kind not in {"main-street", "cross-street", "cross-street-distance"}:
            continue
        px, py = item.point
        if item.kind == "cross-street-distance":
            # в превью шрифт крупнее, чем DBTEXT 0.9 в AutoCAD: раздвигаем строки,
            # иначе «150.00' TO CORNER» наезжает на название улицы
            dx, dy = px - cx, py - cy
            norm = math.hypot(dx, dy) or 1.0
            px += dx / norm * 3.5 * plan.text_height
            py += dy / norm * 3.5 * plan.text_height
        ax.text(
            px,
            py,
            item.text.replace("%%d", "°"),
            rotation=math.degrees(item.rotation),
            rotation_mode="anchor",
            ha="center",
            va="center",
            fontsize=9,
            color=street_color,
            fontweight="bold" if item.kind != "cross-street-distance" else "normal",
        )

    # --- оформление --------------------------------------------------------
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    pad = 0.22 * max(max(xs) - min(xs), max(ys) - min(ys))
    ax.set_xlim(min(xs) - pad, max(xs) + pad)
    ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.set_aspect("equal")
    ax.grid(alpha=0.25, linestyle=":")
    ax.set_xlabel("X, ft (East)", fontsize=8)
    ax.set_ylabel("Y, ft (North)", fontsize=8)
    ax.tick_params(labelsize=7)

    info = (
        f"scale 1\"={scale:g}'   text height {plan.text_height:.2f}\n"
        f"Closure Error {plan.closure.distance:.3f} ft\n"
        f"perimeter {plan.closure.perimeter:.2f} ft   area {plan.area_sqft:,.0f} sq ft"
    )
    ax.text(
        0.02, 0.98, info, transform=ax.transAxes, va="top", ha="left", fontsize=7.5,
        bbox=dict(boxstyle="round,pad=0.4", facecolor="#fffbe6", edgecolor="#c9b458"),
    )

    # северная стрелка
    ax.annotate(
        "N", xy=(0.93, 0.93), xytext=(0.93, 0.80), xycoords="axes fraction",
        ha="center", fontsize=9, fontweight="bold",
        arrowprops=dict(arrowstyle="-|>", color="black", lw=1.2),
    )
    sides_word = "стороны" if len(vertices) % 10 in (2, 3, 4) and len(vertices) % 100 not in (12, 13, 14) else "сторон"
    title = f"Прототип 1 — раскладка подписей  •  {len(vertices)} {sides_word}"
    ax.set_title(title, fontsize=10, color="#334155")

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    print(f"saved: {out_path}")
    print(plan.closure.report())
    for warning in plan.warnings:
        print("! ", warning)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    source = Path(sys.argv[1])
    render(source.read_text(encoding="utf-8"), Path(sys.argv[2]))
