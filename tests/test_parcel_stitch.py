"""Шаг 1: условное размещение соседа, не подмена документальной привязки.

Тексты Stafford — выдержки из трёх сканов (без сведений о владельцах). В логе
на сцене стояли только PARCEL I и неверная привязка 88' вместо 965.57'.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from acad_builder import AcadBuilder, verify_with_neighbors
from conflict_resolver import LotInput, _Scene, advisor_cache_key, analyse_conflicts, fix_changes
from llm_parser import load_config, parse_with_regex
from parcel_stitch import Joint, try_stitch
import test_gui_smoke as smoke

DATA = Path(__file__).parent / "data"
gui = smoke.gui


def lots(*, old_reading=False):
    result = []
    for number in (40, 38, 47):
        text = (DATA / f"stafford_lot{number}_legal.txt").read_text(encoding="utf-8")
        parsed = text.split("PARCEL II")[0]
        if old_reading and number == 47:
            parsed = parsed.replace("distant 965.57 feet", "distant 88 feet")
        name = f"LOT {number}.pdf"
        result.append(LotInput(name, name, number == 40, parse_with_regex(parsed)[0], source_text=text))
    return result


EDGE = {"target": "snap", "side": 1, "vertex": 1, "to_lot": "L1", "to_side": 2, "to_vertex": 2}


def proposal(change=None, **replace_fields):
    return {
        "lot": "L3", "reason": "Начала обеих боковых сторон выходят на Carlton Boulevard.",
        "source_lot": "L3", "source_quote": "BEGINNING at a point on the southwesterly side of Carlton Boulevard",
        "changes": [EDGE if change is None else change], **replace_fields,
    }


def analyse(items, *proposals, **kwargs):
    return analyse_conflicts(AcadBuilder(load_config()), items, scale=16, force_advisor=True,
                             advisor=lambda *args: ({"proposals": list(proposals)}, None), **kwargs)


def test_stafford_carlton_partial_edge_is_a_translation_not_a_deed_edit():
    items = lots(old_reading=True)
    before = [lot.description.model_dump() for lot in items]
    res = analyse(items, proposal(), proposal())  # повторы одного совета дают одну кнопку
    fix, = res.proposals
    assert fix.stitch and fix.verdict == "partial" and fix_changes([fix]) == ()
    assert fix.before.overlap_sqft == pytest.approx(1056) and fix.after.overlap_sqft == 0
    assert (fix.stitch.dx, fix.stitch.dy) == pytest.approx((10.019, -6.605), abs=0.001)
    report = res.format_report()
    assert "V1 «LOT 47.pdf» → V2 «LOT 40.pdf» (стороны #1 → #2)" in report
    assert "общий отрезок 88.00 ft" in report and "остаётся 12.00 ft" in report
    assert [lot.description.model_dump() for lot in items] == before

    builder = AcadBuilder(load_config())
    original = _Scene(builder, items, 16).plan(items[2])
    items[2].fixes = (fix,)
    placed = _Scene(builder, items, 16)
    main, n38, n47 = [placed.plan(lot) for lot in items]
    assert n47.pob == pytest.approx(main.vertices[1])
    assert n47.area_sqft == pytest.approx(original.area_sqft)
    assert n47.closure == original.closure and n47.description == original.description
    assert n47.description.distance_to_corner_ft == 88.0  # даже ошибочная запись не переписывается склейкой
    assert n47.items[0].point == pytest.approx((original.items[0].point[0] + fix.stitch.dx,
                                                original.items[0].point[1] + fix.stitch.dy))
    pair38, pair47 = verify_with_neighbors(main, [n38, n47]).pair_reports
    assert pair38.min_poly_distance_ft == pytest.approx(36)  # место PARCEL II не отдали lot 38
    assert pair47.overlap_area_sqft == 0 and pair47.shared_edges[0].overlap_span_ft == pytest.approx(88)
    assert any("Условное размещение" in msg for msg in pair47.findings)
    assert pair47.status != "ok"  # разные опорные перекрёстки остаются в акте


def test_correct_tie_and_missing_parcel_are_not_replaced_to_get_a_fit():
    ours, n38, n47 = lots()
    assert n47.description.distance_to_corner_ft == 965.57  # не «easterly 88 feet of ... Acre Plot»
    assert n47.description.segments[0].distance_ft == 88
    lost_tie = n47.source_text.replace("distant 965.57 feet southeasterly", "")
    assert parse_with_regex(lost_tie)[0].distance_to_corner_ft == 0
    p38 = proposal({**EDGE, "to_side": 3, "to_vertex": 4}, lot="L2")
    res = analyse([ours, n38, n47], p38, proposal())
    fix, = res.proposals
    assert fix.lot_key == n47.key and fix.after.gap_ft == 0
    assert res.advisor_rejected and "от того же перекрёстка" in res.advisor_rejected[0]
    assert n38.description.distance_to_corner_ft == 136
    assert ours.description.segments[1].distance_ft == 100


@pytest.mark.parametrize(("item", "message"), [
    (proposal(lot="L1"), "опоры"), (proposal({**EDGE, "to_lot": "L2"}), "опоры"),
    (proposal({**EDGE, "to_lot": "L9"}), "опоры"),
    (proposal({**EDGE, "vertex": 5}), "вершины"), (proposal({**EDGE, "to_vertex": 0}), "вершины"),
    (proposal({**EDGE, "vertex": True}), "вершины"), (proposal({**EDGE, "side": 1.9}), "стороны"),
    (proposal({**EDGE, "to_vertex": 1}), "не является концом"),
    (proposal({**EDGE, "to_vertex": 3}), "без поворота"),
    (proposal({**EDGE, "to_side": 1}), "на улицу"),
    (proposal({**EDGE, "vertex": 2, "to_vertex": 3}), "фасадов «Carlton Boulevard» разнесены на 12.00"),
    (proposal({**EDGE, "to_side": None}), "обе стороны"),
    (proposal({**EDGE, "dx": 4}), "только номера"),
    (proposal(changes=[EDGE, {"target": "tie", "distance_ft": 100}]), "отдельным советом"),
    (proposal(source_lot="MAP"), "карта не была приложена"),
    (proposal(source_quote="Not printed"), "цитата не найдена"),
    (proposal(reason=""), "нет обоснования"),
])
def test_invalid_advice_never_becomes_a_button(item, message):
    res = analyse(lots(), item)
    assert res.proposals == [] and message in res.advisor_rejected[0]


def test_reversed_last_side_wrap_and_a_pure_corner():
    ours = lots()[0]
    builder = AcadBuilder(load_config())
    main = builder.plan(ours.description, scale=16)
    nb = builder.plan(ours.description.model_copy(update={"cross_street": None}), scale=16, is_neighbor=True)
    args = {"anchor_key": ours.key, "others": []}
    snap = try_stitch(nb, main, Joint(1, 4), Joint(2, 2), **args)
    assert snap.place(nb).vertices[3] == pytest.approx(main.vertices[2])
    assert "V4 соседа → V3" in snap.contact
    # Для контакта в одной точке у соседа другая фасадная улица.
    nb = replace(nb, description=nb.description.model_copy(update={"main_street": "Other Street"}))
    corner = try_stitch(nb, main, Joint(3), Joint(1), **args)
    assert corner.place(nb).vertices[2] == pytest.approx(main.pob) and "только пара углов" in corner.contact
    with pytest.raises(ValueError, match="уже совпадают"):
        try_stitch(nb, main, Joint(1), Joint(1), **args)


def test_snap_cannot_hit_another_lot_and_cannot_survive_a_changed_anchor():
    ours, n38, n47 = lots(old_reading=True)
    fix, = analyse([ours, n38, n47], proposal()).proposals
    # Ещё один участок уже занимает именно то место, куда советник хотел сдвинуть L3.
    text = n47.source_text.replace("distant 965.57", "distant 100.00")
    fourth = LotInput("fourth", "fourth.pdf", False, parse_with_regex(text)[0])
    bad = analyse([ours, n38, n47, fourth], proposal())
    assert not bad.proposals and "наложение на «fourth.pdf»" in bad.advisor_rejected[0]
    n47.fixes = (fix,)
    moved_main = replace(ours, description=ours.description.model_copy(update={"block_number": "123"}))
    after = analyse([moved_main, n38, n47])
    assert after.applied == [] and "Склейка «LOT 47.pdf» снята" in after.format_report()
    assert not analyse([n38, n47]).applied  # старого опорного файла больше нет
    assert advisor_cache_key([ours, n38, n47]) != advisor_cache_key([ours, n47, n38])


def test_stitch_does_not_break_another_fitting_wall():
    ours, n38, n47 = lots()
    # Тот же перекрёсток, следующая граница по Carlton в 965.57 + 130 ft.
    text = n47.source_text.replace("distant 965.57", "distant 1095.57")
    fourth = LotInput("fourth", "fourth.pdf", False, parse_with_regex(text)[0])
    res = analyse([ours, n38, n47, fourth], proposal())
    assert not res.proposals and "разрывает уже совпадающую межу" in res.advisor_rejected[0]


def test_restore_respects_two_independently_approved_stitches_not_file_order():
    main, _, lot_a = lots(old_reading=True)
    desc = main.description.model_copy(update={
        "main_street": "Other Street", "cross_street": None, "front_segment_index": None,
        "distance_to_corner_ft": 110,
        "corner_to_pob_bearing": main.description.segments[0].model_copy(update={"distance_ft": 110}),
    })
    lot_b = LotInput("B", "B.pdf", False, desc, source_text="line adjoining the rear of A")
    items = [main, lot_a, lot_b]  # B стоит там, куда хотим сдвинуть A
    fix_b, = analyse(items, proposal({**EDGE, "to_side": 3, "to_vertex": 4},
                                     source_quote=lot_b.source_text)).proposals
    lot_b.fixes = (fix_b,)
    fix_a, = analyse(items, proposal(lot="L2", source_lot="L2")).proposals
    lot_a.fixes = (fix_a,)
    assert analyse(items).applied == [fix_a, fix_b]
    # Без переноса B его исходный контур снова на пути A.
    lot_b.fixes = ()
    assert not analyse(items).applied


def test_advisor_gets_numbered_calls_original_scan_and_separate_map_within_budget(tmp_path, monkeypatch):
    from conflict_resolver import build_advisor_request

    items = lots()
    items[0].images = ("deed1", "deed2")
    items[2].scan_pages = (Path(items[2].key), 4, 4)
    loads = []
    monkeypatch.setattr("conflict_resolver.render_pdf_pages_base64",
                        lambda path, **kw: loads.append((path, kw["pages"])) or ["original47"])
    monkeypatch.setattr("conflict_resolver.render_image_files_base64", lambda *a, **k: (["map"], []))
    scene = _Scene(AcadBuilder(load_config()), items, 16)
    path = tmp_path / "TAX_MAP.jpg"
    pictured = []
    text, images, labels = build_advisor_request(scene, [], [], None, reference_map=path, max_images=4, pictured_lots=pictured)
    assert images == ["deed1", "deed2", "original47", "map"] and pictured == ["L1", "L3"]
    assert labels["L3"] is items[2] and loads == [(Path(items[2].key), "4-4")]
    assert 'Image 4: TAX MAP "TAX_MAP.jpg"' in text and "document pages: attached images 3" in text
    assert "V1=(" in text and "#4 V4->V1" in text and "frontage:" in text
    loads.clear()
    pictured.clear()
    text, images, _ = build_advisor_request(scene, [], [], None, reference_map=path, max_images=1, pictured_lots=pictured)
    assert images == ["map"] and not pictured and not loads
    assert "PARCEL II" in text and "distant 965.57" in text  # OCR/текст как запасной источник


def test_map_button_consent_apply_and_undo_do_not_reparse_or_reask(gui, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox
    from PIL import Image
    from llm_parser import TokenUsage
    from test_conflict_resolver import _open_set

    items = lots()
    paths = [tmp_path / lot.name for lot in items]
    for lot, path in zip(items, paths):
        smoke._write_test_pdf(path, [lot.source_text.split("PARCEL II")[0]])
    map_path = tmp_path / "TAX_MAP.jpg"
    Image.new("RGB", (100, 100)).save(map_path)
    app = gui.SurveyApp()
    calls, parses = [], []
    parse = gui.parse_legal_description_full
    monkeypatch.setattr(gui, "parse_legal_description_full", lambda **kw: parses.append(kw) or parse(**kw))
    monkeypatch.setattr(gui, "resolve_api_target", lambda *a, **k: {"api_key": "test"})

    def advisor(_system, text, cfg, **kwargs):
        calls.append((text, kwargs["image_data_urls"]))
        return {"proposals": [proposal(source_lot="MAP", source_quote="У лотов 40 и 47 общий угол у Carlton.")]}, TokenUsage()

    monkeypatch.setattr(gui, "ask_llm_json", advisor)
    dialog = _open_set(app, monkeypatch, *paths)
    dialog.rows[0]["radio"].select()
    dialog.submit()
    smoke._wait_for_gui_worker(app)
    old_pob = app._neighbor_plan_sources[paths[2]].pob
    ask = app._pdf_set_report_window._advisor_map_button.cget("command")
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **_: str(map_path))
    confirmations = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, msg: confirmations.append(msg) or False)
    ask()
    assert map_path.name in confirmations[0] and not calls
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    ask()
    smoke._wait_for_gui_worker(app)
    assert len(calls) == 1 and 'Image 1: TAX MAP' in calls[0][0] and len(calls[0][1]) == 1
    fix, = app._pdf_set_analysis.proposals
    assert app._neighbor_plan_sources[paths[2]].pob == old_pob and app.pdf_set[2].fixes == []
    assert app._pdf_set_report_window._apply_all_button is None
    labels = [kw.get("text") for kind, _, kw in app.preview_canvas.items if kind == "text"]
    assert {"V1", "V2"}.issubset(labels)
    app._pdf_set_report_window._fix_buttons[fix.id].cget("command")()
    smoke._wait_for_gui_worker(app)
    assert app._neighbor_plan_sources[paths[2]].pob == pytest.approx(app._preview_main_plan.vertices[1])
    assert app.pdf_set[2].outcome.data.distance_to_corner_ft == 965.57
    assert app._neighbor_report.pair_reports[1].overlap_area_sqft == 0
    assert app._pdf_set_analysis.advisor_status == "cached" and len(parses) == 3 and len(calls) == 1
    from acad_builder import DrawResult
    drawn = []
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui.AcadBuilder, "draw_plan", lambda _, p: drawn.append(p) or DrawResult(p, 0, {}))
    app._start(parse_only=False)
    smoke._wait_for_gui_worker(app)
    assert any(p is app._neighbor_plan_sources[paths[2]] for p in drawn)
    app._pdf_set_report_window._revert_buttons[fix.id].cget("command")()
    smoke._wait_for_gui_worker(app)
    assert app._neighbor_plan_sources[paths[2]].pob == old_pob
    assert len(parses) == 3 and len(calls) == 1 and not app.pdf_set[2].fixes
    app._on_close()
