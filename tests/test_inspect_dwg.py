"""Тесты inspect_dwg.py — дампа чертежа AutoCAD — на заглушках COM-объектов."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import inspect_dwg as dwg  # noqa: E402


class Ent:
    """COM-объект чертежа: свойства — атрибуты, отсутствующее свойство — AttributeError."""

    def __init__(self, object_name, layer, bbox=None, **attrs):
        self.ObjectName = object_name
        self.Layer = layer
        self.Handle = attrs.pop("Handle", "1A")
        self.color = attrs.pop("color", 256)
        self.Linetype = attrs.pop("Linetype", "ByLayer")
        for name, value in attrs.items():
            setattr(self, name, value)
        if bbox is not None:
            self.GetBoundingBox = lambda: (
                (bbox[0], bbox[1], 0.0), (bbox[2], bbox[3], 0.0)
            )


class Table:
    """Коллекция AutoCAD: Count + Item(индекс или имя)."""

    def __init__(self, items, names=None):
        self.items = list(items)
        self.names = dict(names or {})

    @property
    def Count(self):
        return len(self.items)

    def Item(self, key):
        if isinstance(key, str):
            return self.names[key]
        return self.items[key]


class Busy(Exception):
    hresult = -2147418111  # RPC_E_CALL_REJECTED


def make_doc():
    arrow = Table([Ent("AcDbLine", "0", StartPoint=(0, 0, 0), EndPoint=(0, 2, 0), bbox=(0, 0, 0, 2))])
    arrow.Origin = (0.0, 0.0, 0.0)
    arrow.IsXRef = False
    attribute = Ent("AcDbAttribute", "0", TagString="SCALE", TextString="1\"=16'",
                    InsertionPoint=(1.0, 2.0, 0.0), Height=0.9)
    entities = [
        Ent("AcDbLine", "FENCE_CHAIN_LEFT", StartPoint=(0.3, 54.0, 0.0), EndPoint=(0.3, 99.7, 0.0),
            bbox=(0.3, 54.0, 0.3, 99.7), Handle="2B"),
        Ent("AcDbPolyline", "BUILDING_OUTLINE", Coordinates=(5.0, 21.6, 32.0, 21.6, 32.0, 69.0, 5.0, 69.0),
            Closed=True, Elevation=0.0, ConstantWidth=0.0, GetBulge=lambda index: 0.5 if index == 1 else 0.0,
            bbox=(5.0, 21.6, 32.0, 69.0), color=3),
        Ent("AcDbText", "D-TOPO-TEXT", TextString="CHAIN LINK FENCE", InsertionPoint=(1.4, 62.123456, 0.0),
            Height=0.9, Rotation=-math.pi / 2.0, StyleName="AI_BOUNDARY", Alignment=10,
            TextAlignmentPoint=(1.4, 62.0, 0.0), ScaleFactor=1.0, ObliqueAngle=0.0,
            bbox=(0.95, 55.6, 1.85, 68.5)),
        Ent("AcDbMText", "D-TOPO-TEXT", TextString="{\\fArial|b0;FRAME\\PDECK}", InsertionPoint=(18.0, 76.0, 0.0),
            Height=0.9, Rotation=0.0, StyleName="AI_BOUNDARY", Width=4.5, AttachmentPoint=5,
            DrawingDirection=1, LineSpacingFactor=1.0, bbox=(16.0, 75.0, 20.0, 77.0)),
        Ent("AcDbLeader", "D-TOPO-TEXT", Coordinates=(44.0, 28.0, 0.0, 44.0, 20.0, 0.0), Type=1,
            StyleName="Standard", bbox=(44.0, 20.0, 44.0, 28.0)),
        Ent("AcDbAlignedDimension", "D-TOPO-TEXT", Measurement=40.0, TextOverride="40.00'",
            TextPosition=(20.0, -2.0, 0.0), TextRotation=0.0, ExtLine1Point=(0.0, 0.0, 0.0),
            ExtLine2Point=(40.0, 0.0, 0.0), bbox=(0.0, -3.0, 40.0, 0.0)),
        Ent("AcDbBlockReference", "0", Name="NORTH", EffectiveName="NORTH", InsertionPoint=(60.0, 90.0, 0.0),
            Rotation=math.radians(33.0), XScaleFactor=1.0, YScaleFactor=1.0, ZScaleFactor=1.0,
            HasAttributes=True, GetAttributes=lambda: (attribute,), bbox=(59.0, 89.0, 61.0, 92.0)),
        Ent("AcDbCircle", "DERIVED", Center=(10.0, 10.0, 0.0), Radius=0.25, bbox=(9.75, 9.75, 10.25, 10.25)),
        # Второй «чертёж» в стороне — копия в 1000 ft правее.
        Ent("AcDbLine", "COPY", StartPoint=(1000.0, 0.0, 0.0), EndPoint=(1040.0, 0.0, 0.0),
            bbox=(1000.0, 0.0, 1040.0, 0.0)),
        Ent("AcDbText", "COPY", TextString="SHED", InsertionPoint=(1010.0, 5.0, 0.0), Height=1.2,
            Rotation=0.0, StyleName="Standard", bbox=(1010.0, 5.0, 1014.0, 6.2)),
    ]
    paper = Table([
        Ent("AcDbViewport", "0", Center=(5.5, 4.25, 0.0), Width=10.0, Height=7.5,
            CustomScale=1.0 / 192.0, bbox=(0.5, 0.5, 10.5, 8.0)),
    ])
    layers = Table([
        Ent("AcDbLayerTableRecord", "", Name="D-TOPO-TEXT", LayerOn=True, Freeze=False, Lock=False),
        Ent("AcDbLayerTableRecord", "", Name="FENCE_NOTE", LayerOn=False, Freeze=False, Lock=False),
        Ent("AcDbLayerTableRecord", "", Name="COPY", LayerOn=True, Freeze=True, Lock=False),
    ])
    styles = Table([
        Ent("AcDbTextStyleTableRecord", "", Name="AI_BOUNDARY", fontFile="romans.shx", BigFontFile="",
            Height=0.0, Width=1.0, ObliqueAngle=0.0),
    ])

    class Doc:
        Name = "87 Stafford.dwg"
        FullName = "C:\\jobs\\87 Stafford.dwg"
        ModelSpace = Table(entities)
        Layers = layers
        TextStyles = styles
        DimStyles = Table([Ent("AcDbDimStyleTableRecord", "", Name="Standard")])
        Blocks = Table([arrow], names={"NORTH": arrow})
        Layouts = Table([
            Ent("AcDbLayout", "", Name="Model", ModelType=True, Block=Table([])),
            Ent("AcDbLayout", "", Name="Layout1", ModelType=False, Block=paper),
        ])

        def GetVariable(self, name):
            values = {"INSUNITS": 2, "DIMSCALE": 16.0, "LTSCALE": 1.0, "EXTMIN": (0.0, -3.0, 0.0)}
            if name not in values:
                raise KeyError(name)
            return values[name]

    return Doc()


def by_type(dump, kind):
    return [record for record in dump["entities"] if record["type"] == kind]


def test_dump_records_geometry_and_text_properties():
    dump = dwg.collect_dump(make_doc())
    assert dump["format"] == dwg.FORMAT
    assert dump["drawing"]["modelspace_count"] == 10 and dump["drawing"]["dumped_count"] == 10
    assert dump["errors"] == []

    line = by_type(dump, "Line")[0]
    assert line == {
        "i": 0, "type": "Line", "layer": "FENCE_CHAIN_LEFT", "handle": "2B",
        "pts": [[0.3, 54], [0.3, 99.7]], "bbox": [0.3, 54, 0.3, 99.7],
    }
    poly = by_type(dump, "Polyline")[0]
    assert poly["pts"] == [[5, 21.6], [32, 21.6], [32, 69], [5, 69]]
    assert poly["closed"] is True and poly["color"] == 3
    assert poly["bulges"] == [0, 0.5, 0, 0]

    text = by_type(dump, "Text")[0]
    assert text["text"] == "CHAIN LINK FENCE" and text["style"] == "AI_BOUNDARY"
    assert text["pt"] == [1.4, 62.1235] and text["h"] == 0.9 and text["rot_deg"] == -90
    assert text["align"] == 10 and text["align_pt"] == [1.4, 62] and text["width_factor"] == 1
    assert text["bbox"] == [0.95, 55.6, 1.85, 68.5]

    mtext = by_type(dump, "MText")[0]
    assert mtext["text"] == "{\\fArial|b0;FRAME\\PDECK}"  # строка как в чертеже, с кодами
    assert (mtext["width"], mtext["attach"], mtext["h"]) == (4.5, 5, 0.9)

    leader = by_type(dump, "Leader")[0]
    assert leader["pts"] == [[44, 28], [44, 20]] and leader["leader_type"] == 1
    dim = by_type(dump, "AlignedDimension")[0]
    assert dim["measurement"] == 40 and dim["text_override"] == "40.00'"
    assert dim["ext1"] == [0, 0] and dim["ext2"] == [40, 0] and dim["text_pt"] == [20, -2]
    block = by_type(dump, "BlockReference")[0]
    assert block["name"] == "NORTH" and block["rot_deg"] == 33 and block["pt"] == [60, 90]
    assert block["attributes"] == [{"tag": "SCALE", "text": "1\"=16'", "pt": [1, 2], "h": 0.9}]
    circle = by_type(dump, "Circle")[0]
    assert circle["center"] == [10, 10] and circle["radius"] == 0.25


def test_dump_includes_tables_blocks_layouts_and_variables():
    dump = dwg.collect_dump(make_doc())
    assert dump["variables"] == {"INSUNITS": 2, "DIMSCALE": 16, "LTSCALE": 1, "EXTMIN": [0, -3, 0]}
    assert [layer["name"] for layer in dump["layers"]] == ["D-TOPO-TEXT", "FENCE_NOTE", "COPY"]
    assert dump["layers"][1]["on"] is False and dump["layers"][2]["frozen"] is True
    assert dump["text_styles"][0]["font"] == "romans.shx"
    assert dump["dim_styles"] == ["Standard"]
    assert dump["blocks"]["NORTH"]["count"] == 1
    assert dump["blocks"]["NORTH"]["entities"][0]["pts"] == [[0, 0], [0, 2]]
    assert [layout["name"] for layout in dump["layouts"]] == ["Layout1"]
    viewport = dump["layouts"][0]["entities"][0]
    assert viewport["type"] == "Viewport" and viewport["custom_scale"] == pytest.approx(1 / 192, abs=1e-8)

    bare = dwg.collect_dump(make_doc(), with_blocks=False, with_layouts=False)
    assert bare["blocks"] == {} and bare["layouts"] == []


def test_broken_object_is_reported_and_does_not_stop_the_dump():
    doc = make_doc()

    class Broken:
        @property
        def ObjectName(self):
            raise RuntimeError("объект удалён")

    doc.ModelSpace.items.insert(1, Broken())
    dump = dwg.collect_dump(doc)
    assert dump["drawing"]["modelspace_count"] == 11 and dump["drawing"]["dumped_count"] == 10
    assert dump["errors"] == [{"where": "ModelSpace", "i": 1, "error": "объект удалён"}]
    assert "ошибок чтения: 1" in dwg.build_summary(dump)
    assert "ModelSpace #1: объект удалён" in dwg.build_summary(dump)


def test_busy_autocad_call_is_retried(monkeypatch):
    monkeypatch.setattr(dwg.time, "sleep", lambda _seconds: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise Busy()
        return "ok"

    assert dwg.com_call(flaky) == "ok" and len(calls) == 3
    with pytest.raises(ValueError):
        dwg.com_call(lambda: (_ for _ in ()).throw(ValueError("другая ошибка")))


def test_selection_dump_takes_only_selected_objects():
    doc = make_doc()
    selection = Table(doc.ModelSpace.items[2:4])
    dump = dwg.collect_dump(doc, container=selection, scope="selection")
    assert dump["drawing"]["scope"] == "selection"
    assert dump["drawing"]["modelspace_count"] == 10 and dump["drawing"]["dumped_count"] == 2
    assert [record["type"] for record in dump["entities"]] == ["Text", "MText"]
    assert "только выбранные объекты" in dwg.build_summary(dump)


def test_summary_lists_layers_styles_blocks_and_separate_groups():
    summary = dwg.build_summary(dwg.collect_dump(make_doc()))
    assert "ДАМП ЧЕРТЕЖА: 87 Stafford.dwg" in summary
    assert "INSUNITS = 2 (футы)" in summary and "DIMSCALE = 16" in summary
    assert (
        "Слой 'D-TOPO-TEXT': Text: 1, MText: 1, Leader: 1, AlignedDimension: 1 | высота текста: 0.9 ×2"
        " | Примеры текста: ['CHAIN LINK FENCE', 'FRAME DECK']"
    ) in summary
    assert "Слой 'COPY' [заморожен]: Line: 1, Text: 1" in summary
    assert "Слои без записанных объектов (1): FENCE_NOTE" in summary
    assert "'AI_BOUNDARY': шрифт romans.shx, высота 0, сжатие 1 · текстов: 2" in summary
    assert "Блок 'NORTH' ×1 | Слой: '0' | Поворот: 33°" in summary
    # Копия чертежа в стороне видна как отдельная группа.
    assert "Группа 1: объектов 8" in summary
    assert "Мелких групп (меньше 5 объектов): 1, объектов в них: 2" in summary
    assert "'Layout1': Viewport: 1 | масштабы видовых экранов: [0.00520833]" in summary


def grid_records(x0, y0, layer, count=8, step=6.0):
    """Плотный «чертёж»: сетка объектов 4 × 4 с зазором 2 между ними."""
    return [
        {"type": "Line", "layer": layer,
         "bbox": [x0 + col * step, y0 + row * step, x0 + col * step + 4.0, y0 + row * step + 4.0]}
        for row in range(count) for col in range(count)
    ]


def test_groups_separate_drawings_lying_side_by_side():
    single = grid_records(0.0, 0.0, "FIELD")
    assert [group["count"] for group in dwg.find_groups(single)] == [64]
    summary = dwg.build_summary({"drawing": {"scope": "all"}, "entities": single})
    assert "Все объекты лежат одной группой." in summary

    # Три чертежа в ряд + рамка вокруг всех: рамка группы не склеивает.
    frame = {"type": "Polyline", "layer": "FRAME", "bbox": [-10.0, -10.0, 460.0, 60.0]}
    three = single + grid_records(200.0, 0.0, "WORK") + grid_records(400.0, 0.0, "FINAL") + [frame]
    groups = dwg.find_groups(three)
    assert [group["count"] for group in groups] == [64, 64, 64]
    assert sorted(group["layers"][0][0] for group in groups) == ["FIELD", "FINAL", "WORK"]
    assert dwg.find_groups([]) == [] and dwg.find_groups([{"type": "Xline", "layer": "0"}]) == []


def test_plain_text_strips_mtext_codes():
    assert dwg.plain_text("{\\fArial|b0|i0;FRAME\\PDECK}") == "FRAME DECK"
    assert dwg.plain_text("\\A1;1 STY\\PFRAME\r\nNo. 87") == "1 STY FRAME No. 87"
    assert dwg.plain_text(None) == ""


def test_files_are_valid_json_with_one_object_per_line(tmp_path):
    dump = dwg.collect_dump(make_doc())
    json_path, text_path = dwg.write_dump(dump, tmp_path, tag="final")
    assert json_path.name == "87 Stafford.final.dump.json"
    assert text_path.name == "87 Stafford.final.dump.txt"
    raw = json_path.read_text(encoding="utf-8")
    assert json.loads(raw) == json.loads(json.dumps(dump))
    # Каждый объект — отдельной строкой: удобно искать и сравнивать.
    assert sum(1 for line in raw.splitlines() if line.startswith(' {"i":')) == 10
    assert "ДАМП ЧЕРТЕЖА" in text_path.read_text(encoding="utf-8")
    assert dwg.output_stem('a<b>:c?.dwg', "мой тег!") == "a_b__c_.мой_тег"
    assert dwg.output_stem("", "") == "drawing"


def test_main_writes_files_and_prints_summary(tmp_path, capsys):
    doc = make_doc()
    code = dwg.main(["--out", str(tmp_path), "--tag", "field"], connect=lambda: (object(), doc))
    assert code == 0
    assert (tmp_path / "87 Stafford.field.dump.json").exists()
    assert (tmp_path / "87 Stafford.field.dump.txt").exists()
    out = capsys.readouterr().out
    assert "Сканирую чертёж: 87 Stafford.dwg (объектов в ModelSpace: 10)" in out
    assert "=== 1. СЛОИ И ОБЪЕКТЫ НА НИХ ===" in out and "Пришлите оба файла." in out


def test_main_select_uses_objects_already_selected_in_autocad(tmp_path, capsys):
    doc = make_doc()
    doc.PickfirstSelectionSet = Table(doc.ModelSpace.items[:3])
    code = dwg.main(["--select", "--out", str(tmp_path)], connect=lambda: (object(), doc))
    assert code == 0
    data = json.loads((tmp_path / "87 Stafford.selection.dump.json").read_text(encoding="utf-8"))
    assert data["drawing"]["scope"] == "selection" and len(data["entities"]) == 3


def test_main_select_asks_on_screen_and_cleans_up(tmp_path, capsys):
    doc = make_doc()
    events = []

    class Selection(Table):
        def SelectOnScreen(self):
            events.append("select")
            self.items = doc.ModelSpace.items[4:6]

        def Delete(self):
            events.append("delete")

    class Sets:
        def Item(self, name):
            raise KeyError(name)

        def Add(self, name):
            events.append(f"add {name}")
            return Selection([])

    doc.PickfirstSelectionSet = Table([])
    doc.SelectionSets = Sets()
    code = dwg.main(["--select", "--out", str(tmp_path), "--tag", "x"], connect=lambda: (object(), doc))
    assert code == 0
    assert events == [f"add {dwg.SELECTION_SET}", "select", "delete"]
    data = json.loads((tmp_path / "87 Stafford.x.dump.json").read_text(encoding="utf-8"))
    assert [record["type"] for record in data["entities"]] == ["Leader", "AlignedDimension"]


def test_skip_layers_drops_frame_objects_and_counts_them():
    full = dwg.collect_dump(make_doc())
    lean = dwg.collect_dump(make_doc(), skip_layers=["fence_chain_left"], stage="before")
    left = [r for r in full["entities"] if r["layer"] == "FENCE_CHAIN_LEFT"]
    assert left, "в фикстуре должны быть объекты слоя FENCE_CHAIN_LEFT"
    assert lean["drawing"]["skipped_layers"] == {"FENCE_CHAIN_LEFT": len(left)}
    assert lean["drawing"]["dumped_count"] == full["drawing"]["dumped_count"] - len(left)
    assert lean["drawing"]["stage"] == "before"
    assert all(r["layer"] != "FENCE_CHAIN_LEFT" for r in lean["entities"])
