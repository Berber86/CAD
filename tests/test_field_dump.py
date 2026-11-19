"""
Полевые чертежи «до выставления оффсетов» и эталон Николая из архивов ``docs/primer*.zip``.

Модуль собирает из дампа AutoCAD то, что видит программа (``DrawingSnapshot``), и то, что
добавил чертёжник: пары «до» / «после» лежат в архивах вместе с дампами. Здесь только загрузка
и подсчёт — сами правила оффсетов проверяются в ``test_offsets_field_rules.py``.

Почему не через ``DrawingSnapshot`` соседних тестов: дамп содержит блоки, полилинии с дугами и
атрибуты точек съёмки; сжатая запись ``tests/data/*_field.txt`` (см. ``dump_to_fixture.py``)
теряет разбиение и потому здесь не используется. Контур участка восстанавливается по линиям
так же, как программа видит участок на чертеже.

Внимание: 1373 East 55th Street снят в другой системе координат (итоговый лист повёрнут на
180.245° и сдвинут) — перед сравнением координаты эталона переносятся в систему «до» по
общим ручкам (``_frame_transform``). Север берётся из стрелки файла «до».
"""

from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path

import offsets as off

#: Слои границы участка по приоритету — те же три способа наименования, что в программе
#: (``config.json`` → ``feature_labeling.property_line_layers``): новый стиль заказчика и наш
#: Шаг 1 (``PL``, ``PL-PRINT``), затем старый стиль, где стороны участка начерчены линиями
#: зданий (``ST-BLDG-LINE``). Проверяются по очереди: первый, из которого складывается контур.
LOT_LAYER_GROUPS = (
    # 1. Отдельный слой property line: новый стиль заказчика (`PL`) и наш Шаг 1 (`PL`, `PL-PRINT`)
    ("PL", "PL-*", "PL_*", "PROPERTY_LINE", "PROPERTY_LINES_APPROX", "S-PROP-LINE", "PROP"),
    # 2. Старый стиль: стороны участка начерчены линиями зданий
    ("ST-BLDG-LINE",),
    # 3. Резерв: линии налоговой карты
    ("M-PROPERTY", "M-TAX-LINE"),
)
#: фасадная сторона участка — красная линия улицы
LOT_CLOSING = ("M-ROW-LINE",)
LOT_LAYERS = LOT_LAYER_GROUPS[0] + LOT_LAYER_GROUPS[1] + LOT_CLOSING
#: точки съёмки: отдельные точки слоя PNT-X и блоки srvpno3/srvpno1 с атрибутом DESC2
SURVEY_LAYERS = ("PNT-X", "PNTS", "PNT", "SRVPTS")
SURVEY_BLOCKS = ("SRVPNO", "SPT")
NORTH_LAYERS = ("M-NTH-ARROW",)
SNAP_FT = 0.05


# ------------------------------------------------------------------ геометрия


def _segments(dump: dict) -> list[tuple[str, tuple[float, float], tuple[float, float]]]:
    out = []
    for entity in dump.get("entities", []):
        kind = entity.get("type")
        if kind not in {"Line", "Polyline"}:
            continue
        pts = [(float(p[0]), float(p[1])) for p in entity.get("pts") or []]
        if kind == "Polyline" and entity.get("closed") and len(pts) >= 3:
            pts = pts + [pts[0]]
        for a, b in zip(pts, pts[1:]):
            if math.dist(a, b) > 1e-6:
                out.append((str(entity.get("layer")), a, b))
    return out


def _snap_map(points, tol=SNAP_FT) -> dict[tuple[float, float], tuple[float, float]]:
    """
    Слияние концов линий: каждая точка → самая «левая-нижняя» из точек в пределах допуска.

    Нужно именно слияние, а не «ближайший сосед»: до 4.27 стоял поиск ближайшей точки, и он
    либо находил саму точку (``d=0``), либо две точки ссылались друг на друга — вершины не
    сливались вовсе. На 18 Zoe Street концы линий ``PL`` расходятся на 0.003 ft, контур
    4470 sq ft не собирался, и вместо участка попадался контур дома 1120 sq ft.
    """
    uniq = sorted({(round(p[0], 6), round(p[1], 6)) for p in points})
    out = {}
    for point in uniq:
        near = [other for other in uniq if math.dist(point, other) <= tol]
        out[point] = min(near)
    return out


def _lot_polygon(dump: dict):
    """
    Участок — замкнутый контур по слоям границы с максимумом точек съёмки внутри.

    Группы слоёв проверяются по приоритету (``LOT_LAYER_GROUPS``), в каждой к линиям
    добавляется фасадная линия улицы (``LOT_CLOSING``): у заказчика фасад всегда начерчен
    красной линией, а стороны — либо на новом слое ``PL``, либо (старый стиль) линиями
    зданий ``ST-BLDG-LINE``.
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import polygonize, unary_union

    from feature_labeler import layer_matches

    survey = [Point(p) for p in survey_points(dump)]
    closers = [s for s in _segments(dump) if layer_matches(s[0], LOT_CLOSING)]
    for group in LOT_LAYER_GROUPS:
        segs = [s for s in _segments(dump) if layer_matches(s[0], group)] + closers
        if len(segs) < 3:
            continue
        ends = [p for _layer, a, b in segs for p in (a, b)]
        snap = _snap_map(ends)
        lines = [
            LineString([
                snap[(round(a[0], 6), round(a[1], 6))], snap[(round(b[0], 6), round(b[1], 6))],
            ])
            for _layer, a, b in segs
        ]
        best = None
        for polygon in polygonize(unary_union(lines)):
            if polygon.area < 400:
                continue
            inside = sum(1 for p in survey if polygon.contains(p))
            key = (inside, polygon.area)
            if best is None or key > best[0]:
                best = (key, polygon)
        if best is not None:
            return best[1]
    return None


def survey_points(dump: dict) -> list[tuple[float, float]]:
    """
    Точки съёмки чертежа: точки слоя ``PNT-X`` и вставки блоков точек (``srvpno3`` и др.).

    Блоки точек стоят на самых разных слоях (``ST-BLDG-LINE``, ``S-FENCE``), поэтому слой для них
    не признак — признак имя блока с атрибутами ``PT#``/``DESC2``.
    """
    out = []
    for entity in dump.get("entities", []):
        kind = entity.get("type")
        pt = entity.get("pt")
        if not pt:
            continue
        layer = str(entity.get("layer")).upper()
        name = str(entity.get("name") or entity.get("effective_name") or "").upper()
        if kind == "Point" and layer in SURVEY_LAYERS:
            out.append((float(pt[0]), float(pt[1])))
        elif kind == "BlockReference" and any(block in name for block in SURVEY_BLOCKS):
            out.append((float(pt[0]), float(pt[1])))
    return out


def survey_notes(dump: dict) -> list[tuple[tuple[float, float], str]]:
    """
    Пары «точка съёмки — её описание» (R13b).

    Описание лежит в атрибуте ``DESC2`` блока точки; блоки точек в этих чертежах стоят на самых
    разных слоях (``ST-BLDG-LINE``, ``S-FENCE``), поэтому признак — имя блока, а не слой.
    Точки слоя ``PNT-X`` описания не несут.
    """
    out = []
    for entity in dump.get("entities", []):
        if entity.get("type") != "BlockReference":
            continue
        name = str(entity.get("name") or entity.get("effective_name") or "").upper()
        if not name.startswith("SRVPNO"):
            continue
        pt = entity.get("pt")
        if not pt:
            continue
        description = ""
        for attribute in entity.get("attributes") or []:
            if str(attribute.get("tag")) == "DESC2":
                description = str(attribute.get("text") or "")
        out.append(((float(pt[0]), float(pt[1])), description))
    return out


def north_deg(dump: dict) -> float:
    """Север из стрелки чертежа: блок M-NTH-ARROW, текст N; без стрелки +Y."""
    for entity in dump.get("entities", []):
        if str(entity.get("layer")).upper() not in NORTH_LAYERS:
            continue
        if entity.get("type") != "BlockReference":
            continue
        rot = float(entity.get("rot_deg") or 0.0)
        return (90.0 + rot) % 360.0
    return 90.0


def front_lines(dump: dict) -> list[list[tuple[float, float]]]:
    return [
        [a, b]
        for layer, a, b in _segments(dump)
        if layer.upper() == off.FRONT_BOUNDARY_LAYER
    ]


# ------------------------------------------------------------------ признаки


def _closed(points: list[tuple[float, float]]) -> bool:
    return len(points) >= 4 and math.dist(points[0], points[-1]) <= SNAP_FT


_KIND_BY_LAYER = {
    "S-FENCE": "fence",
    "S-WALL": "wall",
    "ST-WALL": "wall",
    "ST-BLDG-LINE": "house",
    "ST-BLDG": "house",
    "ST-OHED": "house",
}


def features(dump: dict) -> list[off.Feature]:
    """
    Объекты для оффсетов: заборы, стены и контуры строений.

    Объект — не отдельный отрезок, а цепочка соприкасающихся отрезков одного слоя: контур дома
    нарисован четырьмя линиями ``ST-BLDG-LINE``, забор — ломаной из нескольких отрезков.
    Склейка идёт по общим концам (``linemerge``), вставки блоков (в том числе блоков точек
    съёмки, которые тоже лежат на ``ST-BLDG-LINE``) объектами не считаются.
    """
    from shapely.geometry import LineString
    from shapely.ops import linemerge, unary_union

    groups: dict[str, list[LineString]] = {}
    for entity in dump.get("entities", []):
        if entity.get("type") not in {"Line", "Polyline"}:
            continue
        kind = _KIND_BY_LAYER.get(str(entity.get("layer")).upper())
        if kind is None:
            continue
        pts = [(float(p[0]), float(p[1])) for p in entity.get("pts") or []]
        if len(pts) >= 2 and math.dist(pts[0], pts[-1]) > 1e-6:
            groups.setdefault(kind, []).append(LineString(pts))

    out: list[off.Feature] = []
    for kind, lines in groups.items():
        merged = linemerge(unary_union(lines)) if len(lines) > 1 else lines[0]
        for index, part in enumerate(getattr(merged, "geoms", [merged])):
            coords = [(float(x), float(y)) for x, y in part.coords]
            if len(coords) < 2 or part.length < 1.0:
                continue
            out.append(
                off.Feature(
                    kind=kind, handle=f"{kind}/{index}", points=tuple(coords),
                    closed=_closed(coords),
                )
            )
    return out


# ------------------------------------------------------------------ эталон Николая


def _zip_name(info: zipfile.ZipInfo) -> str:
    """
    Имя файла в архиве заказчика.

    Архивы приходят из Windows, и кириллица в них записана в cp866 без флага UTF-8:
    ``zipfile`` отдаёт такие имена как cp437-кракозябры, и метки «итог»/«перед» в имени
    не находятся. Раскодируем обратно.
    """
    if info.flag_bits & 0x800:
        return info.filename
    for encoding in ("cp866", "cp1251"):
        try:
            return info.filename.encode("cp437").decode(encoding)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return info.filename


def load_pair(zip_path: str | Path) -> tuple[dict, dict]:
    """
    Пара «до» / «после» из архива заказчика.

    Имена файлов в архивах разные: ``...before.dump.json`` / ``...after.dump.json``,
    ``...перед оффсетами...final.dump.json`` / ``...ЗАВЕРШЁННЫЙ...final.dump.json``,
    ``2) ... — копия.dump.json`` / ``3_ИТОГОВЫЙ_ФАЙЛ....final.dump.json`` /
    ``3_ИТОГ_378_Mother_Gaston...dump.json``. Различаем по меткам
    в имени (``before``/``final``/``перед``), а не по порядку файлов в архиве.
    """
    with zipfile.ZipFile(zip_path) as archive:
        dumps = {
            _zip_name(info): archive.read(info).decode("utf-8")
            for info in archive.infolist()
            if info.filename.endswith("dump.json")
        }
    assert len(dumps) == 2, sorted(dumps)
    before = after = None
    for name, text in dumps.items():
        low = name.lower()
        if "before" in low or "перед_оффсетами" in low or "ручные_подписи" in low:
            before = text
        elif any(
            mark in low
            for mark in ("after", "final", "итог", "завершённый", "оффсеты")
        ):
            after = text
    if before is None:
        # Brooklyn: файл «до» назван «2) … — копия», «после» — «3_ИТОГОВЫЙ…».
        before = next(text for name, text in dumps.items() if text is not after)
    if after is None:
        # 18 Zoe Street: «2 … — копия» (до оффсетов) и «3 … — копия» (итог); метки в имени
        # нет, различаем по номеру этапа, как у Gaston («2_ПОДПИСАНО» / «3_ИТОГ»).
        numbered = {name.strip()[:1]: text for name, text in dumps.items()}
        before, after = numbered.get("2", before), numbered.get("3")
    assert before is not None and after is not None, sorted(dumps)
    return json.loads(before), json.loads(after)


def _parts(text: str) -> list[tuple[str, float, str]]:
    clean = off.clean_text(text)
    return [
        (str(m["label"]), float(m["value"]), str(m["dir"]))
        for m in off._PART_RE.finditer(clean)
    ]


def _leader_tips(dump: dict) -> dict[str, tuple[float, float]]:
    """Куда указывает выноска: конец ``pts[0]`` у Leader, привязанного к тексту по ручке."""
    out: dict[str, tuple[float, float]] = {}
    for entity in dump.get("entities", []):
        if entity.get("type") != "Leader":
            continue
        target = entity.get("annotation")
        pts = entity.get("pts") or []
        if target and pts:
            out[str(target)] = (float(pts[0][0]), float(pts[0][1]))
    return out


def _added_texts(before: dict, after: dict):
    """
    Что чертёжник дописал: новые тексты слоя оффсетов и новые части в уже стоявших текстах
    (Николай часто дописывает замер в существующую подпись, а не создаёт новую).
    Возвращает (handle, добавленные части, точка вставки, все части подписи).
    """
    old = {str(e.get("handle")): e for e in before.get("entities", []) if e.get("handle")}
    out = []
    for entity in after.get("entities", []):
        if str(entity.get("layer")).upper() != off.OFFSET_LAYER:
            continue
        if entity.get("type") not in {"Text", "MText"}:
            continue
        handle = str(entity.get("handle"))
        text = str(entity.get("text") or "")
        new_parts = _parts(text)
        previous = old.get(handle)
        if previous is not None:
            before_parts = _parts(str(previous.get("text") or ""))
            new_parts = [p for p in new_parts if p not in before_parts]
        if not new_parts:
            continue
        pt = entity.get("pt") or entity.get("ins") or (0.0, 0.0)
        out.append((handle, new_parts, (float(pt[0]), float(pt[1])), new_parts and _parts(text)))
    return out


def _frame_transform(before: dict, after: dict, tol: float = 0.15):
    """Перенос координат «после» → «до» по общим ручкам (RANSAC, подобие)."""
    import numpy as np

    old = {str(e.get("handle")): e for e in before.get("entities", []) if e.get("handle")}
    src, dst = [], []
    for entity in after.get("entities", []):
        twin = old.get(str(entity.get("handle")))
        if twin is None or entity.get("type") != twin.get("type"):
            continue
        if entity.get("pts") and twin.get("pts") and len(entity["pts"]) == len(twin["pts"]):
            for a, b in zip(twin["pts"], entity["pts"]):
                dst.append(a[:2])
                src.append(b[:2])
        elif entity.get("pt") and twin.get("pt"):
            dst.append(twin["pt"][:2])
            src.append(entity["pt"][:2])
    if len(src) < 5:
        return None
    src_a, dst_a = np.array(src, float), np.array(dst, float)
    rng = np.random.default_rng(0)
    best = None
    for _ in range(1500):
        i = rng.choice(len(src_a), 2, replace=False)
        d1, d2 = dst_a[i[1]] - dst_a[i[0]], src_a[i[1]] - src_a[i[0]]
        if np.linalg.norm(d1) < 5 or np.linalg.norm(d2) < 5:
            continue
        scale = np.linalg.norm(d2) / np.linalg.norm(d1)
        angle = math.atan2(d2[1], d2[0]) - math.atan2(d1[1], d1[0])
        rot = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
        shift = src_a[i[0]] - scale * rot @ dst_a[i[0]]
        residual = np.linalg.norm(src_a - (scale * dst_a @ rot.T + shift), axis=1)
        inliers = int((residual < tol).sum())
        if best is None or inliers > best[0]:
            best = (inliers, scale, rot, shift)
    inliers, scale, rot, shift = best
    # Уточняем по инлайерам: грубая пара даёт сдвиг в десятые доли фута, а подписи сравниваются
    # по вершине с радиусом 6 ft — этой точности хватает, но лишний шаг почти бесплатен.
    residual = np.linalg.norm(src_a - (scale * dst_a @ rot.T + shift), axis=1)
    mask = residual < tol
    if mask.sum() >= 5:
        src_in, dst_in = src_a[mask], dst_a[mask]
        src_mean = dst_in.mean(axis=0)      # средняя в системе «до»
        dst_mean = src_in.mean(axis=0)      # средняя в системе «после»
        src_c, dst_c = dst_in - src_mean, src_in - dst_mean
        h = src_c.T @ dst_c
        u, _s, vt = np.linalg.svd(h)
        rot_refined = vt.T @ u.T
        if np.linalg.det(rot_refined) < 0:
            vt[-1] *= -1
            rot_refined = vt.T @ u.T
        scale_refined = float(_s.sum() / (src_c ** 2).sum()) ** 0.5
        shift_refined = dst_mean - scale_refined * rot_refined @ src_mean
        refined = np.linalg.norm(src_a - (scale_refined * rot_refined @ dst_a.T).T - shift_refined, axis=1)
        if (refined < tol).sum() > inliers:
            inliers, scale, rot, shift = int((refined < tol).sum()), scale_refined, rot_refined, shift_refined
    return {"scale": scale, "rot": rot, "shift": shift, "inliers": inliers, "total": len(src)}


def _to_before(transform, point) -> tuple[float, float]:
    """
    Точка листа «после» → система «до».

    ``_frame_transform`` подбирает подобие «до → после» (``после = scale·rot·до + shift``),
    поэтому перевод обратный: ``до = (после − shift)·rot / scale``. Пока направление было
    прямым, на Brooklyn точка уезжала на 4.1 ft (внутри радиуса 6 ft — ошибка не видна),
    а на 378 Mother Gaston со сдвигом 1440 ft сравнивать было нечем вовсе.
    """
    import numpy as np

    mapped = (np.array(point[:2], float) - transform["shift"]) @ transform["rot"] / transform["scale"]
    return (float(mapped[0]), float(mapped[1]))


def nikolai_labels(before: dict, after: dict) -> list[dict]:
    """
    Подписи Николая целиком (для H17): одна подпись — один текст, в котором есть новые части;
    вершина — конец выноски; вид — по метке в подписи.
    """
    transform = None
    if not off.same_frame(before, after).same_frame:
        transform = _frame_transform(before, after)
    tips = _leader_tips(after)
    out = []
    for handle, parts, pt, full in _added_texts(before, after):
        base = None
        for label, _value, _direction in full:
            if label:
                base = label
        kinds = {_kind_of(label or base) for label, _v, _d in parts}
        kind = "fence" if "fence" in kinds else "wall" if "wall" in kinds else "house"
        if not kinds - {None}:
            continue
        tip = tips.get(handle)
        anchor = transform and tip and _to_before(transform, tip) or tip or (
            transform and _to_before(transform, pt) or pt
        )
        out.append(
            {
                "kind": kind,
                "parts": [
                    (value, direction)
                    for label, value, direction in parts
                    if _kind_of(label or base) is not None
                ],
                "anchor": anchor,
            }
        )
    return out


def nikolai_parts(before: dict, after: dict) -> list[dict]:
    """
    Оффсеты Николая: вид объекта, значение, буква и вершина, к которой ведёт выноска.

    Вид определяется по метке в той же подписи (``W.FE.``, ``C.L.F.``, ``WALL`` — забор/стена,
    без метки — дом), буква — по самой части. Координаты переносятся в систему «до», если
    чертежи сняты в разных системах (Brooklyn).
    """
    transform = None
    if not off.same_frame(before, after).same_frame:
        transform = _frame_transform(before, after)
    tips = _leader_tips(after)
    out = []
    for handle, parts, pt, full in _added_texts(before, after):
        # Метка вида объекта берётся из всей подписи: Николай дописывает замер в готовый
        # «W.FE.» и «C.L.F.», и добавленная часть приходит без метки.
        base = None
        for label, _value, _direction in full:
            if label:
                base = label
        tip = tips.get(handle)
        anchor = transform and tip and _to_before(transform, tip) or tip or (
            transform and _to_before(transform, pt) or pt
        )
        for label, value, direction in parts:
            kind = _kind_of(label or base)
            if kind is None:
                continue
            out.append({"kind": kind, "value": value, "dir": direction, "anchor": anchor})
    return out


_FENCE_MARKS = ("FE.", "FE", "F.", "WOOD", "CHAIN", "VINYL", "C.L.F.", "W.FE.", "V.FE.")


def _kind_of(label: str | None) -> str | None:
    text = (label or "").strip().upper()
    if text.startswith("WALL"):
        return "wall"
    if text.startswith(("COL", "C.STEP", "CONC", "STEP")):
        return None  # колонки и ступени в оффсетах не считаем: их подписывают размером
    if any(text.startswith(mark) for mark in _FENCE_MARKS):
        return "fence"
    return "house"


# ------------------------------------------------------------------ оценка


def plan_labels(plan) -> list[dict]:
    """Подписи плана целиком: для H17 считаются подписи, а не строки внутри них."""
    out = []
    for item in plan.items:
        parts = _parts(item.text.replace("\n", " "))
        if not parts:
            # Подпись без числа («FE.O/L», «BLDG» / «O/L») — не оффсет: у Николая такие
            # в подсчёт не входят, и программа их в счёт не берёт.
            continue
        kinds = {_kind_of(label) for label, _v, _d in parts}
        kind = "fence" if "fence" in kinds else "wall" if "wall" in kinds else "house"
        out.append(
            {
                "kind": kind,
                "parts": [(value, direction) for _l, value, direction in parts],
                "anchor": (float(item.anchor[0]), float(item.anchor[1])),
                "text": item.text,
            }
        )
    return out


def score_labels(produced: list[dict], expected: list[dict], radius: float = 6.0,
                 tol: float = 0.11) -> int:
    """
    Сколько подписей программы совпало с подписями Николая.

    Подпись считается совпавшей, если рядом с той же вершиной стоит подпись того же вида
    и в ней есть хотя бы одна общая часть (значение в пределах 0.1 ft и та же буква).
    """
    pairs = []
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
                pairs.append((-shared, distance, i, j))
    # Жадно, но от лучших пар к худшим: иначе дальняя подпись с одной общей частью
    # забирает совпадение у подписи, стоящей у самой вершины (Brooklyn: «0.1'N» за 37 ft
    # перехватывала «0.2'N», которая на самом деле стоит в 4 ft).
    pairs.sort()
    used_produced: set[int] = set()
    used_expected: set[int] = set()
    hits = 0
    for _shared, _distance, i, j in pairs:
        if i in used_produced or j in used_expected:
            continue
        used_produced.add(i)
        used_expected.add(j)
        hits += 1
    return hits


def plan_parts(plan) -> list[dict]:
    """Части плана программы в том же виде, что и ``nikolai_parts``."""
    out = []
    for item in plan.items:
        parts = _parts(item.text.replace("\n", " "))
        # Значения столбиком идут без префикса («FE.0.2'S» / «0.3'W») — вид объекта
        # берётся из первой строки, иначе вторая часть выпадала бы из счёта.
        kinds = [_kind_of(label) for label, _value, _direction in parts]
        item_kind = next((kind for kind in kinds if kind is not None), None)
        for label, value, direction in parts:
            kind = _kind_of(label) or item_kind
            if kind is None:
                continue
            out.append(
                {
                    "kind": kind,
                    "value": value,
                    "dir": direction,
                    "anchor": (float(item.anchor[0]), float(item.anchor[1])),
                    "text": item.text,
                }
            )
    return out


def score(produced: list[dict], expected: list[dict], radius: float = 6.0, tol: float = 0.11) -> int:
    """Сколько подписей программы совпало с эталоном (вид, значение, буква, вершина)."""
    used: set[int] = set()
    hits = 0
    for part in produced:
        best = None
        for index, target in enumerate(expected):
            if index in used or target["kind"] != part["kind"]:
                continue
            if abs(target["value"] - part["value"]) > tol:
                continue
            if target["dir"] != part["dir"]:
                continue
            distance = math.dist(target["anchor"], part["anchor"])
            if distance <= radius and (best is None or distance < best[0]):
                best = (distance, index)
        if best is not None:
            used.add(best[1])
            hits += 1
    return hits


def field_plan(before: dict, after: dict):
    """План программы по файлу «до» и эталон Николая из файла «после»."""
    polygon = _lot_polygon(before)
    assert polygon is not None, "контур участка не восстановлен"
    ring = [(float(x), float(y)) for x, y in polygon.exterior.coords]
    plan = off.plan_offsets(
        ring,
        features(before),
        front=front_lines(before),
        scale=16.0,
        north_deg=north_deg(before),
        texts=[
            (str(e.get("text")), tuple(e["pt"][:2]), float(e.get("h") or 0.0))
            for e in before.get("entities", [])
            if e.get("type") in {"Text", "MText"} and e.get("pt")
        ],
        survey_pts=survey_points(before),
        survey_notes=survey_notes(before),
    )
    return {"plan": plan, "parts": plan_parts(plan), "ring": ring}, nikolai_parts(before, after)


def added_labels(before: dict, after: dict) -> list[dict]:
    """Только эталон Николая (для отчётов)."""
    return nikolai_parts(before, after)


__all__ = [
    "added_labels",
    "load_pair",
    "features",
    "field_plan",
    "front_lines",
    "nikolai_labels",
    "nikolai_parts",
    "plan_labels",
    "score_labels",
    "north_deg",
    "plan_parts",
    "score",
    "survey_notes",
    "survey_points",
]
