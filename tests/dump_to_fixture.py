"""
Сжимает дамп inspect_dwg.py (``*.dump.json``) в текстовую запись полевого чертежа
для тестов Шага 2 (``tests/data/*_field.txt``): отрезки (дуги и дуговые звенья
полилиний — хордами по 10°, как их читает программа), блоки точек съёмки с
описаниями, тексты. Формат читает ``load_field_fixture`` в test_feature_labeling.py.

    python tests/dump_to_fixture.py "docs/for MLP 6910 15th Avenue, Brooklyn.dump.json" \
        tests/data/brooklyn_field.txt "6910 15th Avenue, Brooklyn"
    python tests/dump_to_fixture.py "docs/for MLP 225 Aspinwall Street, Staten Island.dump.json" \
        tests/data/aspinwall_field.txt "225 Aspinwall Street, Staten Island" 4
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

HEADER = """\
# Полевой чертёж геодезиста «{title}» (дамп inspect_dwg.py, {count} объектов), сжатая запись.
# Координаты — футы, 2 знака.
#   L слой x1 y1 x2 y2            — отрезок (AcDbLine / звено полилинии)
#   P слой номер x y | DESC2      — блок точки съёмки с атрибутами PT#, ELEV2, DESC2
#   Q номер x y                   — блок точки с пустым DESC2
#   H слой minx miny maxx maxy    — штриховка (габарит)
#   T слой x y высота поворот | текст — Text / MText
#   B слой имя x y поворот        — вставка блока без атрибутов
"""


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Дуги и дуговые звенья полилиний раскладываются теми же функциями, которыми
# программа читает чертёж из AutoCAD: запись совпадает с тем, что увидит Шаг 2.
from acad_builder import _arc_points, _expand_polyline_bulges  # noqa: E402


def arc_points(center, radius, start_deg, end_deg):
    """Дуга AcDbArc (против часовой стрелки, углы в градусах) — точками через 10°."""
    return _arc_points(
        (float(center[0]), float(center[1])), float(radius),
        math.radians(float(start_deg)), math.radians(float(end_deg)),
    )


def polyline_points(points, bulges, closed):
    """Вершины LWPOLYLINE с дуговыми звеньями (bulge) — ломаной; замкнутая — с повтором начала."""
    path = _expand_polyline_bulges(points, bulges, closed) if any(bulges) else list(points)
    if closed:
        path = path + [path[0]]
    return path


def convert(source: Path, target: Path, title: str, digits: int = 2) -> Counter:
    """
    ``digits`` — знаков после запятой у координат линий и точек. Двух знаков
    хватает, чтобы узнать объекты; четыре (точность дампа) сохраняют стыки линий
    такими, какими их видит программа в AutoCAD: конец отрезка, не дошедший до
    соседней линии на 0.00005 ft, после округления до сотых «уезжает» на 0.005 ft.
    """
    dump = json.loads(source.read_text(encoding="utf-8"))
    rows: list[str] = []
    stats: Counter = Counter()
    skipped: Counter = Counter()
    for entity in dump["entities"]:
        kind, layer = entity["type"], entity["layer"].replace(" ", "_")
        if kind in {"Line", "Polyline"} and isinstance(entity.get("pts"), list):
            points = [(p[0], p[1]) for p in entity["pts"]]
            bulges = [float(value) for value in (entity.get("bulges") or [])]
            path = polyline_points(points, bulges, bool(entity.get("closed")))
            for (x0, y0), (x1, y1) in zip(path, path[1:]):
                rows.append(f"L {layer} {x0:.{digits}f} {y0:.{digits}f} {x1:.{digits}f} {y1:.{digits}f}")
                stats["L"] += 1
        elif kind == "Arc" and entity.get("center"):
            path = arc_points(entity["center"], entity["radius"], entity["start_deg"], entity["end_deg"])
            for (x0, y0), (x1, y1) in zip(path, path[1:]):
                rows.append(f"L {layer} {x0:.{digits}f} {y0:.{digits}f} {x1:.{digits}f} {y1:.{digits}f}")
                stats["L"] += 1
        elif kind == "BlockReference":
            x, y = entity["pt"][0], entity["pt"][1]
            attributes = {a["tag"]: str(a.get("text", "")).strip() for a in entity.get("attributes", [])}
            if attributes.get("DESC2"):
                rows.append(f"P {layer} {attributes.get('PT#', '?')} {x:.{digits}f} {y:.{digits}f} | {attributes['DESC2']}")
                stats["P"] += 1
            elif attributes:
                rows.append(f"Q {attributes.get('PT#', '?')} {x:.2f} {y:.2f}")
                stats["Q"] += 1
            elif entity.get("bbox"):
                rows.append(f"B {layer} {entity['name']} {x:.2f} {y:.2f} {entity.get('rot_deg', 0):g}")
                stats["B"] += 1
            else:
                skipped[f"{entity['name']} (блок без атрибутов и геометрии)"] += 1
        elif kind == "Hatch" and entity.get("bbox"):
            box = entity["bbox"]
            rows.append(f"H {layer} {box[0]:.2f} {box[1]:.2f} {box[2]:.2f} {box[3]:.2f}")
            stats["H"] += 1
        elif kind in {"Text", "MText"} and entity.get("text"):
            x, y = entity["pt"][0], entity["pt"][1]
            text = " ".join(str(entity["text"]).split())
            rows.append(f"T {layer} {x:.2f} {y:.2f} {entity.get('h', 0):g} {entity.get('rot_deg', 0):g} | {text}")
            stats["T"] += 1
        else:
            skipped[f"{kind} в слое {layer}"] += 1
    header = HEADER.format(title=title, count=len(dump["entities"]))
    if digits != 2:
        header = header.replace("футы, 2 знака", f"футы, {digits} знака")
    for name, count in sorted(skipped.items()):
        header += f"# В запись не вошли: {name} — {count}\n"
    target.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
    return stats


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    print(dict(convert(
        Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3],
        digits=int(sys.argv[4]) if len(sys.argv) == 5 else 2,
    )))
