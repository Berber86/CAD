"""
Замер пространственных правил оффсетов по пяти полевым парам заказчика.

Печатает то, на чём построены предложения версии 4.28:

* длины выносок и просветы «текст ↔ линия» у Николая и у нас;
* поворот подписи относительно линии замера;
* согласие буквы (`N`/`S`/`E`/`W`) с тремя возможными конвенциями;
* минимальное/максимальное значение и кратность 0.1;
* прогон кандидатов `LEADER_DISTANCES_FT` и `LINE_CLEARANCE_FT` (сколько подписей теряется).

Запуск из корня репозитория::

    /tmp/venv/bin/python docs/step4_spatial_measure.py

``LINE_CLEARANCE_FT`` в поставке равен 0.0 (правило выключено) — прогон показывает, что оно
делает при 0.4…0.7: просвет растёт, но две подписи перестают влезать.
"""

from __future__ import annotations

import math
import statistics as st
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import offsets as off  # noqa: E402
from test_field_dump import (  # noqa: E402
    _kind_of, _leader_tips, _parts, field_plan, load_pair, nikolai_labels, plan_labels, score_labels,
)

PAIRS = {
    "Geldner": "docs/primer3i.zip",
    "Brooklyn": "docs/primer2i.zip",
    "Alvine": "docs/primer1i.zip",
    "Gaston": "docs/primer4i.zip",
    "Zoe": "docs/primer5i.zip",
    "East 64th": "docs/primer7i.zip",
}

PROPERTY = {"PL", "PROP", "M-PROPERTY", "M-TAX-LINE", "S-PROP-LINE", "M-ROW-LINE"}
KIND_LAYER = {
    "property": PROPERTY,
    "row": {"M-ROW-LINE"},
    "fence": {"S-FENCE"},
}


def layer_kind(layer: str) -> str:
    name = layer.upper()
    if name == "M-ROW-LINE":
        return "row"
    if name in PROPERTY or name.startswith(("PL-", "PL_", "PROPERTY")):
        return "property"
    if "FENCE" in name:
        return "fence"
    if "WALL" in name:
        return "wall"
    if "BLDG" in name:
        return "house"
    return "other"


def segment(point, a, b):
    """Расстояние от точки до отрезка и угол отрезка."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    share = 0.0 if length == 0 else max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / length))
    foot = (ax + share * dx, ay + share * dy)
    return math.hypot(point[0] - foot[0], point[1] - foot[1]), math.degrees(math.atan2(dy, dx)), foot


def sides_of(dump: dict) -> list[dict]:
    out = []
    for entity in dump.get("entities", []):
        if entity.get("type") not in {"Line", "Polyline"}:
            continue
        points = entity.get("pts") or []
        if len(points) < 2:
            continue
        count = len(points) if entity.get("closed") else len(points) - 1
        for index in range(max(0, count)):
            a, b = points[index][:2], points[(index + 1) % len(points)][:2]
            if math.dist(a, b) < 0.05:
                continue
            out.append({"layer": str(entity.get("layer")), "kind": layer_kind(str(entity.get("layer"))),
                        "a": tuple(a), "b": tuple(b)})
    return out


def compass(vector) -> str:
    if abs(vector[0]) > abs(vector[1]):
        return "E" if vector[0] > 0 else "W"
    return "N" if vector[1] > 0 else "S"


def his_labels(path: str) -> list[dict]:
    """Подписи Николая вместе с рамкой текста, выноской и найденной линией замера."""
    before, after = load_pair(path)
    tips = _leader_tips(after)
    sides = sides_of(after)
    old = {str(e.get("handle")): e for e in before.get("entities", []) if e.get("handle")}
    out = []
    for entity in after.get("entities", []):
        if str(entity.get("layer")).upper() != off.OFFSET_LAYER:
            continue
        if entity.get("type") not in {"Text", "MText"}:
            continue
        handle = str(entity.get("handle"))
        text = str(entity.get("text") or "")
        parts = _parts(text)
        previous = old.get(handle)
        before_parts = _parts(str(previous.get("text"))) if previous else []
        added = [part for part in parts if part not in before_parts]
        box = entity.get("bbox")
        if not added or not box:
            continue
        base = None
        for label, _value, _direction in parts:
            if label:
                base = label
        kinds = {_kind_of(label or base) for label, _v, _d in parts}
        kind = "fence" if "fence" in kinds else "wall" if "wall" in kinds else (
            "house" if "house" in kinds else None)
        if kind is None:
            continue
        anchor = tips.get(handle) or entity.get("pt") or ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        corners = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        nearest = min(sides, key=lambda side: segment(centre, side["a"], side["b"])[0])
        out.append({
            "kind": kind, "text": text.replace("\\P", "|"), "box": box,
            "anchor": (float(anchor[0]), float(anchor[1])),
            "leader": handle in tips,
            "leader_len": min(math.dist((float(anchor[0]), float(anchor[1])), c) for c in corners)
                          if handle in tips else None,
            "gap": min(segment(c, nearest["a"], nearest["b"])[0] for c in corners),
            "near_angle": segment(centre, nearest["a"], nearest["b"])[1],
            "rot": float(entity.get("rot_deg") or 0.0),
            "parts": [(value, direction) for _l, value, direction in added],
            "sides": sides,
        })
    return out


def our_labels(path: str):
    before, after = load_pair(path)
    data, _ = field_plan(before, after)
    plan = data["plan"]
    sides = sides_of(before)
    out = []
    for item in plan.items:
        width, height = off._text_block(item.text, item.height_ft, off.default_text_width)
        x, y = item.point
        corners = [(x - width / 2, y - height / 2), (x + width / 2, y - height / 2),
                   (x + width / 2, y + height / 2), (x - width / 2, y + height / 2)]
        out.append({
            "kind": item.kind, "text": item.text.replace("\n", "|"), "style": item.style,
            "leader_len": math.dist(item.leader[0], item.leader[1]) if item.leader else None,
            "gap": min(segment(c, s["a"], s["b"])[0] for c in corners for s in sides),
            "rot": item.rotation_deg, "anchor": item.anchor, "point": item.point,
            "sides": sides, "value": item.value_ft,
        })
    return out, plan, before, after


def stat_line(title: str, values, reference: str = "") -> None:
    if not values:
        print(f"  {title:26s} —")
        return
    values = sorted(values)
    p10 = values[len(values) // 10]
    p90 = values[min(len(values) - 1, 9 * len(values) // 10)]
    print(f"  {title:26s} n={len(values):3d}  медиана {st.median(values):.2f}  "
          f"p10 {p10:.2f}  p90 {p90:.2f}  max {values[-1]:.2f}   {reference}")


def main() -> None:
    his, ours = {}, {}
    for name, path in PAIRS.items():
        his[name] = his_labels(path)
        ours[name] = our_labels(path)[0]

    print("=" * 100)
    print("ПОДПИСИ НИКОЛАЯ")
    print("=" * 100)
    print("  по видам:", dict(Counter(label["kind"] for rows in his.values() for label in rows)))
    for kind in ("fence", "house", "wall"):
        rows = [label for data in his.values() for label in data if label["kind"] == kind]
        if not rows:
            continue
        with_leader = [r for r in rows if r["leader"]]
        print(f"  {kind}: с выноской {len(with_leader)} из {len(rows)}")
        stat_line(f"  {kind} длина выноски", [r["leader_len"] for r in with_leader], "у нас ниже")
        stat_line(f"  {kind} просвет до линии", [r["gap"] for r in rows])
        diffs = []
        for r in rows:
            diff = abs((r["rot"] - r["near_angle"]) % 180.0)
            diffs.append(min(diff, 180.0 - diff))
        if diffs:
            share = sum(1 for d in diffs if d <= 3.0) / len(diffs)
            print(f"  {kind} поворот вдоль ближайшей линии: {share:.0%} в пределах 3°")

    for kind in ("fence", "house"):
        rows = [label for data in ours.values() for label in data if label["kind"] == kind]
        leaders = [r["leader_len"] for r in rows if r["leader_len"]]
        print(f"\n  НАШИ {kind}: подписей {len(rows)}, с выноской {len(leaders)}")
        stat_line("  длина выноски", leaders)
        stat_line("  просвет до линии", [r["gap"] for r in rows])

    print("\n" + "=" * 100)
    print("БУКВА НАПРАВЛЕНИЯ: какая конвенция согласуется с подписями Николая")
    print("=" * 100)
    score = Counter()
    examples = []
    for name, rows in his.items():
        for label in rows:
            if not label["leader"]:
                continue
            for value, direction in label["parts"]:
                if value is None or not direction:
                    continue
                candidates = []
                for side in label["sides"]:
                    distance, angle, foot = segment(label["anchor"], side["a"], side["b"])
                    if abs(distance - value) <= 0.16:
                        candidates.append((abs(distance - value), side, foot))
                if not candidates:
                    continue
                _distance, side, foot = min(candidates, key=lambda item: item[0])
                to_object = (label["anchor"][0] - foot[0], label["anchor"][1] - foot[1])
                middle = ((side["a"][0] + side["b"][0]) / 2, (side["a"][1] + side["b"][1]) / 2)
                along = (foot[0] - middle[0], foot[1] - middle[1])
                for tag, vector in (("от линии к объекту", to_object),
                                    ("от объекта к линии", (-to_object[0], -to_object[1])),
                                    ("вдоль стороны от середины", along)):
                    if math.hypot(*vector) < 1e-6:
                        continue
                    score[tag, compass(vector) == direction] += 1
                if compass(to_object) != direction and len(examples) < 10:
                    examples.append((name, label["text"], direction, compass(to_object), side["layer"]))
    for tag in ("от линии к объекту", "от объекта к линии", "вдоль стороны от середины"):
        ok, bad = score[tag, True], score[tag, False]
        if ok + bad:
            print(f"  {tag:28s} {ok}/{ok + bad} = {ok / (ok + bad):.0%}")
    print("  где наша конвенция не работает:")
    for item in examples:
        print("   ", item)

    print("\n" + "=" * 100)
    print("ЗНАЧЕНИЯ")
    print("=" * 100)
    values = [v for rows in his.values() for label in rows for v, _d in label["parts"] if v is not None]
    if values:
        print(f"  у Николая: {len(values)} значений, min {min(values)} max {max(values)}, "
              f"кратных 0.1 — {sum(1 for v in values if abs(v * 10 - round(v * 10)) < 1e-6) / len(values):.0%}")
    ours_values = [r["value"] for rows in ours.values() for r in rows if r["value"] > 0.0]
    print(f"  у нас: {len(ours_values)} значений, min {min(ours_values):.1f} max {max(ours_values):.1f}")

    print("\n" + "=" * 100)
    print("ПРОГОН КАНДИДАТОВ")
    print("=" * 100)

    def run(tag: str) -> None:
        total = warned = hits = 0
        leaders = []
        for name, path in PAIRS.items():
            before, after = load_pair(path)
            data, _ = field_plan(before, after)
            plan = data["plan"]
            total += len(plan.items)
            warned += len(plan.warnings)
            leaders += [math.dist(item.leader[0], item.leader[1]) for item in plan.items if item.leader]
            hits += score_labels(plan_labels(plan), nikolai_labels(before, after))
        print(f"  {tag:44s} подписей {total:3d} совпало {hits:2d} предупр. {warned} "
              f"выноска медиана {st.median(leaders):.2f}")

    base_leader, base_gap = off.LEADER_DISTANCES_FT, off.LINE_CLEARANCE_FT
    run(f"как есть: LEADER_DISTANCES_FT={base_leader}, LINE_CLEARANCE_FT={base_gap}")
    off.LEADER_DISTANCES_FT = (2.0, 2.6, 1.5, 1.0, 0.5, 0.0, 3.2)
    run("выноска от 2 ft: " + str(off.LEADER_DISTANCES_FT))
    off.LEADER_DISTANCES_FT = base_leader
    for clearance in (0.4, 0.6):
        off.LINE_CLEARANCE_FT = clearance
        run(f"просвет до линий {clearance} высоты")
    off.LINE_CLEARANCE_FT = base_gap


if __name__ == "__main__":
    main()
