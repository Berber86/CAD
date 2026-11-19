"""
Шаг 4, версия 4.20: проверка трёх модернизированных правил построения оффсетов и сверка с эталонами
Николая на реальных дампах «до оффсетов» (192 Geldner, 24 Alvine, 1373 East 55th, 225 Aspinwall,
6910 15th Avenue + полный чертёж Alvine из primer11.zip).

Проверяемые правила:
1. Модернизированное правило узлов и пересечений заборов (H25 / модернизация H11, H14, H18):
   склейка коллинеарных вершин участка и прямых пролётов забора, приоритет параллельной стороны,
   точки пересечения с поперечной границей / её продолжением за углом, отсев внутренних примыканий
   к дому и перпендикулярных соседских отростков, несколько выносок на многосторонний/изогнутый забор.
2. Модернизированное правило двухстороннего угла, ортогональных осей компаса и выделения контура
   участка (H26 / модернизация R4, R2, H16): вторая сторона угла с коротким продолжением до 3.5 ft
   у общей вершины участка, пара ортогональных букв N/S + E/W, выбор ближайшей к участку стрелки NA
   и сохранение внешних заступов/выносок при выделении полилинии самого участка (``area == ring``).
3. Модернизированное правило оффсетов строений по сторонам участка и двум углам одной стены
   (H27 / модернизация R5, H18): по одному прямому оффсету на каждую сближенную сторону границы
   (до 6.2 ft), оба угла одной сближенной стены (>= 15 ft, <= 3.6 ft, разные значения), пропуск
   сквозных стен таунхаусов (party wall), префикс «BLDG» у выноски дома и учёт незамкнутых П-контуров.
"""

from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path

import pytest

import acad_builder as ab
import feature_labeler as fl
import offsets as off
from llm_parser import load_config

DOCS = Path(__file__).resolve().parents[1] / "docs"
CFG = load_config()
FCFG = fl.feature_settings(CFG)
CLASSIFIER = fl.LayerClassifier(FCFG)


def _zload(zname: str, substr: str) -> dict:
    zpath = DOCS / zname
    if not zpath.exists():
        pytest.skip(f"{zpath} не найден")
    with zipfile.ZipFile(zpath) as archive:
        for name in archive.namelist():
            if substr in name and name.endswith(".json"):
                return json.loads(archive.read(name).decode("utf-8"))
    raise AssertionError(f"В {zname} не найден дамп с подстрокой {substr!r}")


def _fload(fname: str) -> dict:
    fpath = DOCS / fname
    if not fpath.exists():
        pytest.skip(f"{fpath} не найден")
    return json.loads(fpath.read_text(encoding="utf-8"))


def _dump_to_snapshot(dump: dict) -> fl.DrawingSnapshot:
    snap = fl.DrawingSnapshot()
    layers_raw = dump.get("layers") or []
    hidden = {
        str(info.get("name", "")).upper()
        for info in layers_raw
        if info.get("off") or info.get("frozen")
    }
    for e in dump.get("entities", []):
        layer, kind = str(e.get("layer") or "0"), str(e.get("type") or "")
        is_hidden = layer.upper() in hidden
        if kind in {"Text", "MText", "MLeader"}:
            is_point = CLASSIFIER.is_point_label(layer)
            is_label = CLASSIFIER.is_existing_label(layer) or CLASSIFIER.is_field_note(layer)
            if is_hidden and not (is_point or is_label):
                continue
            text = fl.plain_text(e.get("text") or "")
            if not text:
                continue
            bbox = tuple(e["bbox"]) if e.get("bbox") and not (is_point and not is_label) else None
            if kind == "MLeader":
                if bbox is None:
                    continue
                pt, h, rot = (float(bbox[0]), float(bbox[3])), 0.0, 0.0
            else:
                raw_pt = e.get("pt") or [0.0, 0.0]
                pt = (float(raw_pt[0]), float(raw_pt[1]))
                h = float(e.get("h") or 0.0)
                rot = math.radians(float(e.get("rot_deg") or 0.0))
            snap.texts.append(
                fl.DrawingText(layer=layer, text=text, point=pt, height=h, rotation=rot, bbox=bbox, hidden=is_hidden)
            )
        elif kind == "BlockReference":
            name = str(e.get("effective_name") or e.get("name") or "")
            attrs = e.get("attributes") or []
            is_north = fl.is_north_arrow_block(name, layer, FCFG)
            if is_north or not attrs:
                if is_north:
                    raw_pt = e.get("pt") or [0.0, 0.0]
                    snap.symbols.append(
                        fl.DrawingSymbol(
                            layer=layer,
                            name=name,
                            point=(float(raw_pt[0]), float(raw_pt[1])),
                            rotation=math.radians(float(e.get("rot_deg") or 0.0)),
                        )
                    )
                continue
            desc = next(
                (fl.plain_text(a.get("text") or "") for a in attrs if a.get("tag") == "DESC2" and a.get("text")),
                "",
            )
            if desc:
                raw_pt = e.get("pt") or [0.0, 0.0]
                snap.survey_points.append(
                    fl.SurveyPoint(
                        layer=layer, number="", description=desc, point=(float(raw_pt[0]), float(raw_pt[1]))
                    )
                )
        elif kind == "Leader":
            path = [(float(p[0]), float(p[1])) for p in (e.get("pts") or [])]
            if len(path) >= 2:
                snap.leaders.append(fl.DrawingLine(layer=layer, points=tuple(path), kind="AcDbLeader"))
        elif kind in {"Line", "Polyline", "2dPolyline", "3dPolyline", "Arc"}:
            is_note = CLASSIFIER.is_field_note(layer)
            wanted = (
                CLASSIFIER.is_feature_linework(layer)
                or CLASSIFIER.is_obstacle(layer)
                or CLASSIFIER.is_existing_label(layer)
                or is_note
            )
            if not wanted or (is_hidden and not is_note):
                continue
            pts, closed = [], bool(e.get("closed"))
            if kind == "Arc" and e.get("center") and e.get("radius"):
                pts = ab._arc_points(
                    (float(e["center"][0]), float(e["center"][1])),
                    float(e["radius"]),
                    math.radians(float(e.get("start_deg") or 0.0)),
                    math.radians(float(e.get("end_deg") or 0.0)),
                )
            elif e.get("pts"):
                raw_pts = [(float(p[0]), float(p[1])) for p in e["pts"]]
                bulges = [float(b) for b in (e.get("bulges") or [])]
                pts = ab._expand_polyline_bulges(raw_pts, bulges, closed) if any(bulges) else raw_pts
            if len(pts) >= 2:
                snap.lines.append(
                    fl.DrawingLine(
                        layer=layer,
                        points=tuple(pts),
                        closed=closed,
                        kind=f"AcDb{kind}",
                        color=e.get("color") if isinstance(e.get("color"), int) else None,
                    )
                )
    return snap


def _align_b_to_a(snap_a: fl.DrawingSnapshot, snap_b: fl.DrawingSnapshot):
    def _segs(snap):
        out = []
        for ln in snap.lines:
            if ln.layer in ("ST-BLDG-LINE", "ST-STEP", "ST-WALL", "S-FENCE"):
                pts = list(ln.points) + ([ln.points[0]] if ln.closed else [])
                for a, b in zip(pts, pts[1:]):
                    length = math.dist(a, b)
                    if length >= 4.0:
                        out.append((a, b, length))
        return out

    sa, sb = _segs(snap_a), _segs(snap_b)
    pts_a = list({p for a, b, _ in sa for p in (a, b)})
    pts_b = list({p for a, b, _ in sb for p in (a, b)})
    best_inliers, best_tf = -1, (1.0, 0.0, 0.0, 0.0)
    for b1, b2, lb in sb:
        for a1, a2, la in sa:
            if abs(la - lb) > 0.15:
                continue
            for p1, p2 in ((a1, a2), (a2, a1)):
                dtheta = math.atan2(p2[1] - p1[1], p2[0] - p1[0]) - math.atan2(b2[1] - b1[1], b2[0] - b1[0])
                c, s = math.cos(dtheta), math.sin(dtheta)
                tx, ty = p1[0] - (c * b1[0] - s * b1[1]), p1[1] - (s * b1[0] + c * b1[1])
                inliers = sum(
                    1
                    for pb in pts_b
                    if any(
                        math.hypot(c * pb[0] - s * pb[1] + tx - pa[0], s * pb[0] + c * pb[1] + ty - pa[1]) < 0.15
                        for pa in pts_a
                    )
                )
                if inliers > best_inliers:
                    best_inliers, best_tf = inliers, (c, s, tx, ty)
    c, s, tx, ty = best_tf
    return lambda p: (c * p[0] - s * p[1] + tx, s * p[0] + c * p[1] + ty)


def _run_on_before(dump: dict):
    snap = _dump_to_snapshot(dump)
    xs = [p[0] for ln in snap.lines for p in ln.points]
    ys = [p[1] for ln in snap.lines for p in ln.points]
    wide_area = [
        (min(xs) - 15.0, min(ys) - 15.0),
        (max(xs) + 15.0, min(ys) - 15.0),
        (max(xs) + 15.0, max(ys) + 15.0),
        (min(xs) - 15.0, max(ys) + 15.0),
    ]
    boundary = fl.find_property_polygon(snap, CFG, near=ab._area_polygon(wide_area))
    rec = fl.recognize_features(snap, boundary, CFG, "")
    north = fl.find_north_arrow(snap, CFG, near=boundary.polygon)
    north_deg = float(north.north_deg) if north else 90.0
    feats = [ab.AcadBuilder._offset_feature(f) for f in rec.features if ab.AcadBuilder._offset_feature(f)]
    front = [list(ln.points) for ln in snap.lines if ln.layer.upper() == off.FRONT_BOUNDARY_LAYER]
    texts = [(str(t.text), tuple(t.point), float(t.height or 0.0)) for t in snap.texts if not t.hidden]
    ring = list(boundary.polygon.exterior.coords)
    wfn = lambda t, h: ab.feature_text_width(t, h, FCFG)
    plan_wide = off.plan_offsets(
        ring, feats, front=front, area=wide_area, north_deg=north_deg, texts=texts, text_width=wfn
    )
    plan_lot = off.plan_offsets(
        ring, feats, front=front, area=ring, north_deg=north_deg, texts=texts, text_width=wfn
    )
    return snap, plan_wide, plan_lot


def _item_parts(item: off.OffsetItem) -> tuple[tuple[float, str], ...]:
    cleaned = off.clean_text(item.text.replace("\n", "\\P"))
    return tuple((float(m["value"]), m["dir"]) for m in off._PART_RE.finditer(cleaned))


# ---------------------------------------------------------------- модульные проверки трёх правил


def test_rule1_fence_crossing_and_house_gate_filtering():
    """Правило 1: пролёт забора вдоль границы даёт пересечение у угла, а хвост к дому внутри отсекается."""
    lot = [(0.0, 0.0), (0.0, 50.0), (0.0, 100.0), (40.0, 100.0), (40.0, 0.0)]  # коллинеарная вершина на западе
    house = off.Feature("house", "H1", ((10.0, 20.0), (30.0, 20.0), (30.0, 60.0), (10.0, 60.0)))
    # Забор идёт от стены дома (10, 22) на запад к (0.2, 22), затем вдоль западной границы до (0.4, 70):
    # хвост у дома (10.0'E) отсекается, а вдоль западной границы остаются два разных значения 0.2'E и 0.4'E.
    gate_and_run = off.Feature("fence", "F1", ((10.0, 22.0), (0.2, 22.0), (0.4, 70.0)))
    # Внешний забор вдоль восточной границы x=41.4, уходящий за южный угол y=0 до y=-5: пересечение даёт 1.4'E.
    outside_east = off.Feature("fence", "F2", ((41.1, 80.0), (41.4, -5.0)))
    plan = off.plan_offsets(lot, [house, gate_and_run, outside_east])
    fence_texts = [it.text for it in plan.items if it.kind == "fence"]
    assert "FE.10.0'E" not in fence_texts
    assert "FE.0.2'E" in fence_texts
    assert "FE.0.4'E" in fence_texts
    assert "FE.1.4'E" in fence_texts


def test_rule2_obtuse_corner_orthogonal_pair_and_lot_polyline_area():
    """Правило 2: на тупом/косом углу вторая сторона даёт пару N/S + E/W, а area==ring не режет заступы."""
    # Участок с косой восточной стороной (как 24 Alvine) при повороте севера 67.56°.
    lot = [(0.0, 40.0), (153.64, 40.0), (174.90, 0.0), (0.0, 0.0)]
    # Вершина у северо-восточного тупого угла: 1.44 ft до косой стороны и 1.84 ft до продолжения северной.
    corner_fence = off.Feature("fence", "FC", ((173.21, 0.41), (156.25, 38.16), (60.0, 38.5)))
    outside_fence = off.Feature("fence", "FO", ((45.0, 43.2), (60.0, 43.2)))
    plan_lot = off.plan_offsets(lot, [corner_fence, outside_fence], area=lot, north_deg=67.563)
    texts = [it.text for it in plan_lot.items]
    assert "FE.1.4'E\nFE.1.8'S" in texts
    assert "FE.3.2'N" in texts


def test_rule2_nearest_north_arrow_selected_on_multi_sheet_alvine_dump():
    """Правило 2: в полном чертеже Alvine (primer11.zip, 12 блоков NA) выбирается ближайший к участку NA."""
    dump = _zload("primer11.zip", "")
    snap = _dump_to_snapshot(dump)
    assert len(snap.symbols) >= 10
    area = ab._area_polygon([(3360.0, 4995.0), (3550.0, 4995.0), (3550.0, 5050.0), (3360.0, 5050.0)])
    boundary = fl.find_property_polygon(snap, CFG, near=area)
    north = fl.find_north_arrow(snap, CFG, near=boundary.polygon)
    assert north is not None
    assert north.north_deg == pytest.approx(67.563, abs=0.05)


def test_rule3_per_side_garage_and_two_corner_single_wall_and_bldg_leader():
    """Правило 3: гараж у 3 границ даёт 3 оффсета по сторонам; наклонная стена у 1 границы — оба угла."""
    lot = [(0.0, 0.0), (18.0, 0.0), (18.0, 100.0), (0.0, 100.0)]
    garage = off.Feature(
        "house", "G1", ((2.1, 78.0), (16.1, 78.0), (16.1, 97.0), (2.1, 97.0)), closed=True
    )
    plan = off.plan_offsets(lot, [garage])
    vals = sorted((it.value_ft, it.direction) for it in plan.items)
    assert vals == [(1.9, "W"), (2.1, "E"), (3.0, "S")]
    assert any(it.text.startswith("BLDG ") for it in plan.items if it.style == "leader")

    wide_lot = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    tilted_house = off.Feature(
        "house", "H1", ((0.3, 60.0), (15.0, 60.0), (15.0, 82.0), (0.4, 82.0)), closed=False
    )
    plan2 = off.plan_offsets(wide_lot, [tilted_house])
    vals2 = sorted((it.value_ft, it.direction) for it in plan2.items)
    assert vals2 == [(0.3, "E"), (0.4, "E")]


# ---------------------------------------------------------------- сверка на 5 реальных дампах «до»


@pytest.fixture(scope="module")
def five_surveys():
    cases = [
        ("192 Geldner", _zload("primer3i.zip", "before"), _zload("primer3i.zip", "after")),
        ("24 Alvine", _zload("primer1i.zip", "перед_оффсетами"), _zload("primer1i.zip", "ЗАВЕРШЕННЫЙ")),
        ("1373 East 55th", _zload("primer2i.zip", "2)"), _zload("primer2i.zip", "3_")),
        (
            "225 Aspinwall",
            _fload("edited for MLP 225 Aspinwall Street, Staten Island.dump.json"),
            _fload("225 Aspinwall Street, Staten Island.final.dump.json"),
        ),
        (
            "6910 15th Ave",
            _fload("edited for MLP 6910 15th Avenue, Brooklyn.dump.json"),
            _fload("6910 15th Avenue, Brooklyn.final.dump.json"),
        ),
    ]
    results = {}
    for name, d_bef, d_aft in cases:
        s_bef, plan_wide, plan_lot = _run_on_before(d_bef)
        s_aft = _dump_to_snapshot(d_aft)
        same = off.same_frame(d_bef, d_aft).same_frame
        b2a = (lambda p: p) if same else _align_b_to_a(s_bef, s_aft)
        leaders_aft = {str(e.get("handle")): e for e in d_aft["entities"] if e.get("type") == "Leader"}
        obs_aft = off.parse_observed_offsets(d_aft)
        if same:
            bef_texts = {str(e.get("handle")): str(e.get("text") or "") for e in d_bef["entities"] if e.get("handle")}
            nik = [o for o in obs_aft if o.handle not in bef_texts or bef_texts.get(o.handle) != o.raw]
        else:
            nik = obs_aft
        nik_by_kind = {"fence": [], "house": []}
        for o in nik:
            if o.style == "dimension":
                continue
            up = o.text.upper()
            if any(k in up for k in ("FE.", "FENCE", "C.L.F.")):
                kind = "fence"
            elif any(k in up for k in ("WALL", "GEN.", "COL.", "STEP")) or o.handle == "26276":
                continue
            else:
                kind = "house"
            tip = None
            if o.leader_handle and o.leader_handle in leaders_aft:
                lpts = leaders_aft[o.leader_handle].get("pts") or []
                if lpts:
                    tip = b2a((lpts[0][0], lpts[0][1]))
            nik_by_kind[kind].append(
                (o.text, tuple((p.value_ft, p.direction) for p in o.parts), b2a(o.pt), tip)
            )
        results[name] = (plan_wide, plan_lot, nik_by_kind)
    return results


def test_real_before_dumps_increase_placed_offsets_and_keep_lot_selection_parity(five_surveys):
    """На всех 5 файлах «до» число построенных оффсетов выросло (39 -> 55, при выборе лота 18 -> 55)."""
    baseline_v419_wide = {
        "192 Geldner": 14,
        "24 Alvine": 7,
        "1373 East 55th": 8,
        "225 Aspinwall": 7,
        "6910 15th Ave": 3,
    }
    baseline_v419_lot = {
        "192 Geldner": 4,
        "24 Alvine": 4,
        "1373 East 55th": 3,
        "225 Aspinwall": 4,
        "6910 15th Ave": 3,
    }
    for name, (plan_wide, plan_lot, _nik) in five_surveys.items():
        assert plan_wide.warnings == []
        assert plan_lot.warnings == []
        assert len(plan_wide.items) > baseline_v419_wide[name], name
        assert len(plan_lot.items) > baseline_v419_lot[name], name
        assert len(plan_lot.items) == len(plan_wide.items), name

    total_placed = sum(len(pw.items) for pw, _pl, _n in five_surveys.values())
    assert total_placed == 55


def test_real_before_dumps_pass_customer_50_percent_extra_limit_and_match_nikolai(five_surveys):
    """По критерию заказчика оффсетов программы не больше 1.5x от подписей Николая (заборы 1.21x, дома 1.40x)."""
    tot_n_fence = tot_p_fence = tot_exact_fence = 0
    tot_n_house = tot_p_house = tot_exact_house = 0

    for name, (plan_wide, _plan_lot, nik_by_kind) in five_surveys.items():
        p_fence = [it for it in plan_wide.items if it.kind == "fence"]
        p_house = [it for it in plan_wide.items if it.kind == "house"]
        n_fence = nik_by_kind["fence"]
        n_house = nik_by_kind["house"]

        # На каждом отдельном чертеже заборы укладываются в <= +50% к Николаю.
        assert len(p_fence) <= 1.5 * len(n_fence), f"{name}: fences {len(p_fence)} vs {len(n_fence)}"
        if n_house:
            assert len(p_house) <= 1.5 * len(n_house), f"{name}: houses {len(p_house)} vs {len(n_house)}"

        for kind, p_list, n_list, tol in (
            ("fence", p_fence, n_fence, 6.0),
            ("house", p_house, n_house, 10.0),
        ):
            used = set()
            for it in p_list:
                pp = _item_parts(it)
                for idx, (_ntxt, np, npt, ntip) in enumerate(n_list):
                    if idx in used:
                        continue
                    refs = [npt] + ([ntip] if ntip else [])
                    if set(pp) == set(np) and min(math.dist(it.anchor, r) for r in refs) <= tol:
                        used.add(idx)
                        break
            if kind == "fence":
                tot_n_fence += len(n_list)
                tot_p_fence += len(p_list)
                tot_exact_fence += len(used)
            else:
                tot_n_house += len(n_list)
                tot_p_house += len(p_list)
                tot_exact_house += len(used)

    assert tot_p_fence <= 1.5 * tot_n_fence  # 34 <= 42.0 (1.21x)
    assert tot_p_house <= 1.5 * tot_n_house  # 21 <= 22.5 (1.40x)
    assert tot_exact_fence >= 16  # в v4.19 было 6
    assert tot_exact_house >= 10  # в v4.19 было 7


def test_specific_nikolai_offsets_recovered_on_geldner_alvine_and_brooklyn(five_surveys):
    """Конкретные оффсеты Николая, ранее терявшиеся в v4.19, теперь строятся на файлах «до»."""
    geldner_parts = {frozenset(_item_parts(it)) for it in five_surveys["192 Geldner"][0].items}
    assert frozenset({(0.1, "E"), (1.2, "S")}) in geldner_parts
    assert frozenset({(1.4, "E")}) in geldner_parts
    assert frozenset({(0.7, "E")}) in geldner_parts
    assert frozenset({(0.3, "W")}) in geldner_parts
    assert frozenset({(0.4, "W")}) in geldner_parts
    assert frozenset({(0.8, "N")}) in geldner_parts

    alvine_parts = {frozenset(_item_parts(it)) for it in five_surveys["24 Alvine"][0].items}
    assert frozenset({(0.4, "N"), (1.3, "W")}) in alvine_parts
    assert frozenset({(1.4, "E"), (1.8, "S")}) in alvine_parts
    assert frozenset({(3.2, "N")}) in alvine_parts
    assert frozenset({(0.2, "S")}) in alvine_parts
    assert frozenset({(2.5, "S")}) in alvine_parts
    assert frozenset({(2.6, "S")}) in alvine_parts

    east55_parts = {frozenset(_item_parts(it)) for it in five_surveys["1373 East 55th"][0].items}
    assert frozenset({(0.1, "S"), (0.5, "W")}) in east55_parts
    assert frozenset({(0.1, "S")}) in east55_parts
    assert frozenset({(0.2, "N")}) in east55_parts
    assert frozenset({(1.9, "N")}) in east55_parts
    assert frozenset({(3.0, "W")}) in east55_parts
    assert frozenset({(4.0, "N")}) in east55_parts

    b6910_parts = {frozenset(_item_parts(it)) for it in five_surveys["6910 15th Ave"][0].items}
    assert frozenset({(0.4, "E")}) in b6910_parts
    assert frozenset({(5.4, "E")}) in b6910_parts
