"""
Картинка-сравнение docs/step2_aspinwall_preview.png: слева — подписи Шага 2 на
полевом чертеже «for MLP 225 Aspinwall Street, Staten Island»
(tests/data/aspinwall_field.txt), справа — итоговый чертёж заказчика,
приведённый к координатам полевого (итоговый повёрнут севером вверх).

Запуск из корня репозитория:  python docs/make_aspinwall_preview.py
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import feature_labeler as fl  # noqa: E402
import test_feature_labeling as t  # noqa: E402

#: Итоговый чертёж повёрнут относительно полевого на −75.394° вокруг угла участка.
FINAL_TURN = math.radians(-75.394)
FINAL_PIVOT, FIELD_PIVOT = (915462.68, 122338.50), (915530.19, 122465.58)
COLORS = {
    "S-FENCE": "#d08a00", "ST-WALL": "#888888", "ST-BLDG-LINE": "#111111", "ST-MISC": "#2f7d32",
    "ST-OHED": "#7b1fa2", "ST-STEP": "#00838f", "S-DWY": "#8d6e63", "S-CONC-MISC": "#b0a090",
    "M-PROPERTY": "#c62828", "M-ROW-LINE": "#c62828",
}
XLIM, YLIM = (915462, 915542), (122456, 122572)
#: Стрелка севера стоит в стороне от участка: на картинке она перенесена в угол кадра.
NORTH_AT = (915469.5, 122463.5)
HOUSE = "2 STY\nFRAME\nW/CELLAR\nNo. 225"


def to_field(point):
    dx, dy = point[0] - FINAL_PIVOT[0], point[1] - FINAL_PIVOT[1]
    cos, sin = math.cos(-FINAL_TURN), math.sin(-FINAL_TURN)
    return (FIELD_PIVOT[0] + dx * cos - dy * sin, FIELD_PIVOT[1] + dx * sin + dy * cos)


def setup(ax, title):
    ax.set_aspect("equal")
    ax.set_xlim(*XLIM)
    ax.set_ylim(*YLIM)
    ax.set_title(title, fontsize=12)
    ax.set_xticks([])
    ax.set_yticks([])


def put(ax, fig, x, y, text, height, rot, color="black"):
    """Текст с высотой прописной буквы ``height`` в единицах чертежа."""
    p0, p1 = ax.transData.transform((0, 0)), ax.transData.transform((0, height))
    points = (p1[1] - p0[1]) * 72.0 / fig.dpi / 0.72
    ax.text(x, y, text, fontsize=points, rotation=rot, rotation_mode="anchor", ha="center",
            va="center", color=color, family="DejaVu Sans Mono", linespacing=1.25)


def draw_north(ax, point, north_deg):
    length = 4.0
    dx, dy = math.cos(math.radians(north_deg)), math.sin(math.radians(north_deg))
    ax.annotate(
        "", xy=(point[0] + dx * length, point[1] + dy * length),
        xytext=(point[0] - dx * length, point[1] - dy * length),
        arrowprops={"arrowstyle": "-|>", "color": "#c62828", "lw": 1.6},
    )
    ax.text(point[0] + dx * (length + 1.3), point[1] + dy * (length + 1.3), "N", color="#c62828",
            fontsize=13, ha="center", va="center", weight="bold")


def clean(text: str) -> str:
    text = re.sub(r"\\[pP][^;]*;", "", text.replace("\\P", "\n"))
    text = re.sub(r"\\[HhAaFfCcWwQqTt][^;]*;", "", text)
    return text.replace("{", "").replace("}", "").strip()


def main() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(21, 15.6), dpi=110)
    snap = t.load_aspinwall()
    plan = t.make_plan(snap, house=HOUSE, height=0.9)
    arrow = fl.find_north_arrow(snap, {})

    ax = axes[0]
    setup(ax, f"Шаг 2 (версия {fl.STEP2_VERSION.split()[0]}) на полевом чертеже «for MLP 225 Aspinwall Street»")
    for line in snap.lines:
        xs, ys = zip(*line.points)
        dashed = line.layer in ("M-PROPERTY", "M-ROW-LINE")
        ax.plot(xs, ys, color=COLORS.get(line.layer, "#999999"), lw=1.1, ls="--" if dashed else "-")
    draw_north(ax, NORTH_AT, arrow.north_deg)
    fig.canvas.draw()
    for item in plan.items:
        center = item.box.centroid
        put(ax, fig, center.x, center.y, item.text, item.height, math.degrees(item.rotation), "#0d47a1")
        if item.leader_points:
            xs, ys = zip(*item.leader_points)
            ax.plot(xs, ys, color="#0d47a1", lw=0.8)

    ax = axes[1]
    setup(ax, "Итоговый чертёж заказчика (выбранные объекты, в координатах полевого)")
    dump = json.loads(
        (ROOT / "docs" / "225 Aspinwall Street, Staten Island.final.dump.json").read_text(encoding="utf-8")
    )
    base = math.degrees(FINAL_TURN)
    for entity in dump["entities"]:
        kind, layer = entity["type"], entity["layer"]
        if kind in ("Line", "Polyline") and entity.get("pts") and (layer in COLORS or layer == "D-TOPO-TEXT"):
            xs, ys = zip(*[to_field(p) for p in entity["pts"]])
            dashed = layer in ("M-PROPERTY", "M-ROW-LINE")
            ax.plot(xs, ys, color=COLORS.get(layer, "black"), lw=1.1 if layer in COLORS else 0.6,
                    ls="--" if dashed else "-")
        elif kind == "Leader" and layer == "D-TOPO-TEXT":
            xs, ys = zip(*[to_field(p) for p in entity["pts"]])
            ax.plot(xs, ys, color="black", lw=0.8)
        elif kind == "BlockReference" and entity["name"] == "NA":
            draw_north(ax, NORTH_AT, entity["rot_deg"] + 90.0 - base)
    fig.canvas.draw()
    for entity in dump["entities"]:
        if entity["type"] not in ("Text", "MText") or entity["layer"] not in ("D-TOPO-TEXT", "0"):
            continue
        if "SCALE" in entity["text"]:
            continue
        box = entity["bbox"]
        center = to_field(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
        put(ax, fig, center[0], center[1], clean(entity["text"]), entity["h"], (entity["rot_deg"] - base) % 360)
    fig.tight_layout()
    target = ROOT / "docs" / "step2_aspinwall_preview.png"
    fig.savefig(target)
    print("saved", target)


if __name__ == "__main__":
    main()
