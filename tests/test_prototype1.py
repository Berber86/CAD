"""
Тесты Прототипа 1 (без AutoCAD и без сети).

Запуск:  python -m pytest -q      (или python tests/test_prototype1.py)
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from acad_builder import (  # noqa: E402
    AcadBuilder,
    bearing_azimuth_deg,
    bearing_to_xy,
    build_polygon_points,
    build_side_label,
    closure_error,
    format_distance,
    point_in_polygon,
    polygon_signed_area,
    text_rotation,
)
from llm_parser import (  # noqa: E402
    LegalDescription,
    Segment,
    bearing_label,
    load_config,
    parse_with_regex,
)

# Прямоугольник 120 x 85.5, замыкается точно:
# N 75°20'20" E 120, S 14°39'40" E 85.5, S 75°20'20" W 120, N 14°39'40" W 85.5
RECT_ANGLES = [
    ("N", 75, 20, 20.0, "E", 120.0),
    ("S", 14, 39, 40.0, "E", 85.5),
    ("S", 75, 20, 20.0, "W", 120.0),
    ("N", 14, 39, 40.0, "W", 85.5),
]

EXAMPLE = """BEING Lot 12, Block A, Smith Addition, according to the plat recorded in
Volume 5, Page 120, Plat Records of Travis County, Texas, fronting on the South side
of Main Street, being 150.00 feet East of the East line of Oak Avenue;
THENCE N 75°20'20" E, a distance of 120.00 feet to a point for corner;
THENCE S 14°39'40" E, a distance of 85.50 feet to a point for corner;
THENCE S 75°20'20" W, a distance of 120.00 feet to a point for corner;
THENCE N 14°39'40" W, a distance of 85.50 feet to the POINT OF BEGINNING."""


def make_description() -> LegalDescription:
    return LegalDescription(
        main_street="Main Street",
        cross_street="Oak Avenue",
        distance_to_corner_ft=150.0,
        segments=[
            dict(zip(("ns", "degrees", "minutes", "seconds", "ew", "distance_ft"), seg))
            for seg in RECT_ANGLES
        ],
    )


# --------------------------------------------------------------- геометрия --

@pytest.mark.parametrize(
    "ns,ew,expected",
    [("N", "E", 45.0), ("N", "W", 315.0), ("S", "E", 135.0), ("S", "W", 225.0)],
)
def test_azimuth_quadrants(ns, ew, expected):
    assert bearing_azimuth_deg(ns, 45, 0, 0, ew) == pytest.approx(expected)


def test_azimuth_dms_and_axes():
    assert bearing_azimuth_deg("N", 75, 20, 20, "E") == pytest.approx(75.338888, abs=1e-6)
    # 0° = север = +Y, 90° = восток = +X
    x, y = bearing_to_xy("N", 0, 0, 0, "E", 10.0)
    assert (x, y) == pytest.approx((0.0, 10.0), abs=1e-9)
    x, y = bearing_to_xy("N", 90, 0, 0, "E", 10.0)
    assert (x, y) == pytest.approx((10.0, 0.0), abs=1e-9)


def test_polygon_closes_and_area():
    desc = make_description()
    traverse = build_polygon_points(desc.segments)
    assert len(traverse) == 5
    closure = closure_error(traverse)
    assert closure.distance < 1e-6
    vertices = traverse[:-1]
    assert abs(polygon_signed_area(vertices)) == pytest.approx(120 * 85.5, rel=1e-9)
    center = (
        sum(x for x, _ in vertices) / len(vertices),
        sum(y for _, y in vertices) / len(vertices),
    )
    assert point_in_polygon(center, vertices) is True
    assert point_in_polygon((500.0, 500.0), vertices) is False


def test_closure_error_is_not_zero_for_bad_deed():
    desc = make_description()
    desc.segments[0].distance_ft = 130.0  # испорченная сторона -> невязка 10 ft
    closure = closure_error(build_polygon_points(desc.segments))
    assert closure.distance == pytest.approx(10.0, abs=1e-6)
    assert closure.precision and closure.precision > 30


# ------------------------------------------------------------------ подписи --

def test_bearing_label_canonical_and_carry():
    assert bearing_label("N", 75, 20, 20.0, "E") == 'N 75°20\'20" E'
    seg = Segment(ns="n", degrees=75, minutes=59, seconds=59.999, ew="e", distance_ft=1)
    # перенос 75°59'59.999" -> 76°00' (ровные минуты печатаются без секунд)
    assert seg.bearing_label == "N 76°00' E"


def test_text_rotation_never_upside_down():
    # линия на восток (0) — текст горизонтален
    assert text_rotation(0.0) == pytest.approx(0.0)
    # линия на север (pi/2) — текст вертикален, читается снизу вверх
    assert text_rotation(math.pi / 2) == pytest.approx(math.pi / 2)
    # линия на запад (pi) — разворот на 180°, текст снова горизонтален
    assert text_rotation(math.pi) == pytest.approx(0.0)
    for degrees in range(0, 360, 5):
        angle = text_rotation(math.radians(degrees))
        assert -math.pi / 2 - 1e-9 <= angle <= math.pi / 2 + 1e-9


def test_long_side_stretches_label():
    cfg = load_config()["drawing"]
    seg = Segment(ns="N", degrees=75, minutes=20, seconds=20, ew="E", distance_ft=200.0)
    short, stretched_short = build_side_label(seg, 0.9, 20.0, cfg)
    long, stretched_long = build_side_label(seg, 0.9, 200.0, cfg)
    assert not stretched_short
    assert stretched_long
    assert long.count(" ") > short.count(" ")
    assert long.startswith("N 75%%d20'20\" E")
    assert long.strip().endswith("200.00'")


def test_text_height_scales():
    cfg = load_config()
    assert AcadBuilder.text_height_for_scale(16.0, cfg) == pytest.approx(0.9)
    assert AcadBuilder.text_height_for_scale(10.0, cfg) == pytest.approx(0.5625)
    assert AcadBuilder.text_height_for_scale(30.0, cfg) == pytest.approx(1.6875)


def test_format_distance():
    assert format_distance(55) == "55.00'"
    assert format_distance(85.505) == "85.51'"


# -------------------------------------------------------------- раскладка --

def test_plan_places_side_labels_outside_polygon():
    desc = make_description()
    builder = AcadBuilder(load_config())
    plan = builder.plan(desc, scale=16.0)
    assert abs(plan.area_sqft) == pytest.approx(120 * 85.5, rel=1e-9)
    side_items = [item for item in plan.items if item.kind.startswith("side-")]
    assert len(side_items) == 4
    offset = 1.5 * plan.text_height
    for item in side_items:
        # Все подписи сторон лежат снаружи контура.
        assert point_in_polygon(item.point, plan.vertices) is False
        # И не дальше, чем на offset + половина диагонали от линии.
        assert min(
            math.dist(item.point, plan.vertices[i % 4])
            for i in range(4)
        ) >= 0.0
    assert offset == pytest.approx(1.35)
    kinds = {item.kind for item in plan.items}
    assert {"main-street", "cross-street"} <= kinds


def _square(directions):
    """10x10 квадрат по заданной последовательности сторон."""
    letters = {
        "N": ("N", 0, "E"),
        "E": ("N", 90, "E"),
        "S": ("S", 0, "E"),
        "W": ("N", 90, "W"),
    }
    segments = []
    for letter in directions:
        ns, degrees, ew = letters[letter]
        segments.append(
            Segment(ns=ns, degrees=degrees, minutes=0, seconds=0, ew=ew, distance_ft=10.0)
        )
    return segments


def test_plan_handles_both_traverse_directions():
    builder = AcadBuilder(load_config())
    # N, W, S, E — обход против часовой стрелки
    ccw_plan = builder.plan(
        LegalDescription(main_street="A St", segments=_square("NWSE")), scale=16.0
    )
    assert ccw_plan.ccw is True
    assert ccw_plan.area_sqft == pytest.approx(100.0)

    # N, E, S, W — обход по часовой стрелке
    cw_plan = builder.plan(
        LegalDescription(main_street="A St", segments=_square("NESW")), scale=16.0
    )
    assert cw_plan.ccw is False
    assert cw_plan.area_sqft == pytest.approx(100.0)

    # В обоих случаях подписи сторон лежат снаружи контура.
    for plan in (ccw_plan, cw_plan):
        for item in plan.items:
            if item.kind.startswith("side-"):
                assert point_in_polygon(item.point, plan.vertices) is False


# ---------------------------------------------------------------- парсинг --

def test_regex_parser_reads_metes_and_bounds():
    model, warnings = parse_with_regex(EXAMPLE)
    assert len(model.segments) == 4
    first = model.segments[0]
    assert (first.ns, first.degrees, first.minutes, first.ew) == ("N", 75, 20, "E")
    assert first.seconds == pytest.approx(20.0)
    assert first.distance_ft == pytest.approx(120.0)
    assert first.bearing_label == 'N 75°20\'20" E'
    assert model.main_street == "Main Street"
    assert model.cross_street == "Oak Avenue"
    assert model.distance_to_corner_ft == pytest.approx(150.0)
    traverse = build_polygon_points(model.segments)
    assert closure_error(traverse).distance < 0.01
    assert isinstance(warnings, list)


def test_segment_validation_errors():
    with pytest.raises(Exception):
        Segment(ns="N", degrees=95, minutes=0, seconds=0, ew="E", distance_ft=10)
    with pytest.raises(Exception):
        Segment(ns="N", degrees=10, minutes=0, seconds=0, ew="E", distance_ft=0)
    with pytest.raises(Exception):
        LegalDescription(main_street="X", segments=[])


def test_parse_full_offline_uses_regex():
    from llm_parser import parse_legal_description_full

    outcome = parse_legal_description_full(
        text=EXAMPLE, use_llm=False, cfg=load_config()
    )
    assert outcome.source == "regex"
    assert len(outcome.data.segments) == 4
    json.loads(outcome.data.to_json())  # модель сериализуется


def test_config_loads_and_env_key(monkeypatch):
    cfg = load_config()
    assert cfg["drawing"]["base_scale"] == 16.0
    assert cfg["layers"]["property_line"] == "PROPERTY_LINE"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert load_config()["api"]["api_key"] == "sk-test"


# ------------------------------------------------------------------- PDF --

def test_pdf_text_extraction(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from llm_parser import PDFError, read_pdf_text

    pdf_path = tmp_path / "deed.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 72), EXAMPLE, fontsize=8)
    doc.save(pdf_path)
    doc.close()

    text = read_pdf_text(pdf_path)
    assert "N 75" in text
    model, _ = parse_with_regex(text)
    assert len(model.segments) == 4

    empty_pdf = tmp_path / "scan.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(empty_pdf)
    doc.close()
    with pytest.raises(PDFError):
        read_pdf_text(empty_pdf)


# -------------------------------------------------- интеграция с COM (fake) --

class FakePoint:
    def __init__(self, values):
        self.values = list(values)

    @property
    def x(self):
        return self.values[0]


class FakeText:
    def __init__(self, text, point, height):
        self.TextString = text
        self.InsertionPoint = point
        self.Height = height
        self.Alignment = None
        self.TextAlignmentPoint = None
        self.Rotation = 0.0
        self.Layer = "0"


class FakePolyline:
    Handle = "1A2B"


class FakeModelSpace:
    def __init__(self):
        self.polylines = []
        self.texts = []

    def AddLightWeightPolyline(self, coordinates):
        self.polylines.append(list(coordinates))
        return FakePolyline()

    def AddText(self, text, point, height):
        item = FakeText(text, FakePoint(point), height)
        self.texts.append(item)
        return item


class FakeLayer:
    def __init__(self, name):
        self.Name = name
        self.LayerOn = True
        self.Freeze = False
        self.Lock = False
        self.color = 7


class FakeLayers:
    def __init__(self):
        self._items = {name: FakeLayer(name) for name in ("0", "PROPERTY_LINE", "BOUNDARY_TEXT_AI")}

    def Add(self, name):
        self._items[name] = FakeLayer(name)
        return self._items[name]

    def Item(self, name):
        return self._items[name]

    def __iter__(self):
        return iter(self._items.values())


class FakeTextStyle:
    Name = "AI_BOUNDARY"

    def SetFont(self, *args):
        self.font = args[0]


class FakeTextStyles:
    def __init__(self):
        self._items = {"Standard": FakeTextStyle()}

    def Add(self, name):
        style = FakeTextStyle()
        style.Name = name
        self._items[name] = style
        return style

    def Item(self, name):
        return self._items[name]

    def __iter__(self):
        return iter(self._items.values())


class FakeDoc:
    def __init__(self):
        self.ModelSpace = FakeModelSpace()
        self.Layers = FakeLayers()
        self.TextStyles = FakeTextStyles()

    def GetVariable(self, name):
        return 2  # feet

    def SendCommand(self, command):
        pass


class FakeApp:
    def __init__(self):
        self.doc = FakeDoc()
        self.zoomed = False

    @property
    def ActiveDocument(self):
        return self.doc

    def ZoomExtents(self):
        self.zoomed = True


def test_draw_writes_into_fake_autocad(monkeypatch):
    import acad_builder as module

    monkeypatch.setattr(module, "_co_initialize", lambda: None)
    monkeypatch.setattr(module, "_variant_points", lambda points: [c for p in points for c in p])
    monkeypatch.setattr(module, "_variant_point", lambda point, z=0.0: [point[0], point[1], z])

    builder = AcadBuilder(load_config())
    app = FakeApp()
    builder._app, builder._doc = app, app.doc

    result = builder.draw(make_description(), scale=10.0)

    space = app.doc.ModelSpace
    assert len(space.polylines) == 1
    coords = space.polylines[0]
    assert len(coords) == 8  # 4 вершины * (x, y)
    # 4 стороны + фасадная улица + пересекаемая улица + расстояние до угла
    assert result.text_count == 7
    texts = {item.TextString for item in space.texts}
    assert "MAIN STREET" in texts
    assert "OAK AVENUE" in texts
    assert any(item.startswith("N 75%%d20'20\" E") for item in texts)
    assert result.text_height == pytest.approx(0.9 * 10.0 / 16.0)
    assert result.closure.distance < 1e-6
    assert app.zoomed is True
    # подписи создаются в слое текста и с выравниванием MiddleCenter
    assert {item.Layer for item in space.texts} == {"BOUNDARY_TEXT_AI"}
    assert {item.Alignment for item in space.texts} == {10}


# ---------------------------------------------- фасадная сторона и улицы --

def test_regex_parser_street_names_with_route_numbers():
    text = """BEING Lot 7, Block 2, Hillside Addition, fronting the North side of
    Ranch Road 620, being 42.50 feet West of the West line of Sunrise Boulevard;
    THENCE N 0°12'30" E, 210.45 feet to a point;
    THENCE N 89°47'30" W, 42.50 feet to a point;
    THENCE S 0°12'30" W, 210.45 feet to a point;
    THENCE S 89°47'30" E, 42.50 feet to the POINT OF BEGINNING."""
    model, _ = parse_with_regex(text)
    assert model.main_street == "Ranch Road 620"
    assert model.cross_street == "Sunrise Boulevard"
    assert model.distance_to_corner_ft == pytest.approx(42.5)
    # участок «fronting the North side of …» лежит СЕВЕРНЕЕ улицы -> фасад на юг
    assert model.front_side == "S"


def test_front_side_drives_street_label_placement():
    model, _ = parse_with_regex(EXAMPLE)
    assert model.front_side == "N"
    plan = AcadBuilder(load_config()).plan(model, scale=16.0)
    street = next(item for item in plan.items if item.kind == "main-street")
    side_one = next(item for item in plan.items if item.kind == "side-1")
    # Фасад смотрит на север -> подпись улицы над первой (северной) стороной.
    assert math.dist(street.point, side_one.point) < 3.0
    assert point_in_polygon(street.point, plan.vertices) is False


def test_front_side_selection_uses_geometry():
    # Квадрат 10x10, фасад смотрит на восток -> подпись MAIN ST у правой стороны.
    builder = AcadBuilder(load_config())
    desc = LegalDescription(main_street="Main St", front_side="E", segments=_square("NWSE"))
    plan = builder.plan(desc, scale=16.0)
    street = next(item for item in plan.items if item.kind == "main-street")
    assert street.point[0] > max(v[0] for v in plan.vertices) - 1e-6
    assert abs(street.point[1] - 5.0) < 1e-6


def test_llm_not_configured_raises_clear_error():
    from llm_parser import LLMParseError, parse_legal_description_full

    cfg = load_config()
    cfg["api"]["api_key"] = ""
    with pytest.raises(LLMParseError):
        parse_legal_description_full(text=EXAMPLE, cfg=cfg, use_llm=True)


# ------------------------------------------ LLM-путь на подменённом клиенте --

_LLM_PAYLOAD = {
    "main_street": "Main Street",
    "cross_street": "Oak Avenue",
    "distance_to_corner_ft": 150.0,
    "front_side": "N",
    "segments": [
        {"ns": "N", "degrees": 75, "minutes": 20, "seconds": 20, "ew": "E",
         "distance_ft": 120.0, "bearing_label": 'N 75°20\'20" E'},
        {"ns": "S", "degrees": 14, "minutes": 39, "seconds": 40, "ew": "E",
         "distance_ft": 85.5, "bearing_label": None},
        {"ns": "S", "degrees": 75, "minutes": 20, "seconds": 20, "ew": "W",
         "distance_ft": 120.0, "bearing_label": None},
        {"ns": "N", "degrees": 14, "minutes": 39, "seconds": 40, "ew": "W",
         "distance_ft": 85.5, "bearing_label": None},
    ],
}


class _FakeMessage:
    def __init__(self, content=None, parsed=None, refusal=None):
        self.content = content
        self.parsed = parsed
        self.refusal = refusal


class _FakeCompletion:
    def __init__(self, message):
        self.choices = [type("Choice", (), {"message": message})()]


class _FakeChat:
    """Имитация client.chat / client.beta.chat с настраиваемыми отказами."""

    def __init__(self, mode):
        self.mode = mode
        self.completions = self

    def parse(self, **_kwargs):
        if self.mode in {"no-parse", "no-json-schema"}:
            raise RuntimeError("parse is not supported by this provider")
        from llm_parser import LegalDescription

        return _FakeCompletion(_FakeMessage(parsed=LegalDescription.model_validate(_LLM_PAYLOAD)))

    def create(self, **kwargs):
        fmt = (kwargs.get("response_format") or {}).get("type")
        if self.mode == "no-json-schema" and fmt == "json_schema":
            raise RuntimeError("json_schema is not supported")
        text = json.dumps(_LLM_PAYLOAD)
        if fmt == "json_object":
            text = "```json\n" + text + "\n```"  # провайдер добавил разметку
        return _FakeCompletion(_FakeMessage(content=text))


class _FakeClient:
    def __init__(self, mode):
        self.chat = _FakeChat(mode)
        self.beta = type("Beta", (), {"chat": _FakeChat(mode)})()


@pytest.mark.parametrize(
    "mode,expected_mode",
    [
        ("structured", "structured-outputs"),
        ("no-parse", "json_schema"),
        ("no-json-schema", "json_object"),
    ],
)
def test_llm_extraction_paths(monkeypatch, mode, expected_mode):
    import openai

    from llm_parser import _extract_with_llm, load_config

    monkeypatch.setattr(openai, "OpenAI", lambda *_a, **_kw: _FakeClient(mode))
    cfg = load_config()
    model, used_mode = _extract_with_llm("x" * 50, cfg, api_key="sk-test")
    assert used_mode == expected_mode
    assert len(model.segments) == 4
    assert model.segments[0].bearing_label == 'N 75°20\'20" E'


def test_llm_refusal_is_reported(monkeypatch):
    import openai

    from llm_parser import LLMParseError, _extract_with_llm, load_config

    class RefusingChat(_FakeChat):
        def parse(self, **_kwargs):
            return _FakeCompletion(_FakeMessage(refusal="cannot parse"))

    class RefusingClient(_FakeClient):
        def __init__(self):
            self.chat = RefusingChat("structured")
            self.beta = type("Beta", (), {"chat": RefusingChat("structured")})()

    monkeypatch.setattr(openai, "OpenAI", lambda *_a, **_kw: RefusingClient())
    with pytest.raises(LLMParseError, match="отказалась"):
        _extract_with_llm("x" * 50, load_config(), api_key="sk-test")


# ---------------------------------------- устойчивость к форматам записи --

@pytest.mark.parametrize(
    "text,expected_first",
    [
        # OCR без символов: N 45 03 12 E
        ("THENCE N 45 03 12 E 100.00 ft; THENCE S 44 56 48 E 50.0';"
         " THENCE S 45 03 12 W 100.00 feet; THENCE N 44 56 48 W 50.00 feet.",
         'N 45°03\'12" E'),
        # десятичные градусы: N 30.5 E
        ("THENCE N 30.5 E, 100.0 feet; THENCE S 59.5 E, 100.0 feet;"
         " THENCE S 30.5 W, 100.0 feet; THENCE N 59.5 W, 100.0 feet.",
         "N 30°30' E"),
        # словами: North 12 degrees 15 minutes 30 seconds East
        ("THENCE North 12 degrees 15 minutes 30 seconds East, 200.00 feet;"
         " THENCE South 77 degrees 44 minutes 30 seconds East, 100.00 feet;"
         " THENCE South 12 degrees 15 minutes 30 seconds West, 200.00 feet;"
         " THENCE North 77 degrees 44 minutes 30 seconds West, 100.00 feet.",
         'N 12°15\'30" E'),
        # типографика Word/PDF: °, ′, ″, фигурные кавычки
        ("THENCE N 12° 15' 30\u201d E, 200.00 feet; THENCE S 77°44\u2032 30\u2033 E, 100.00 feet;"
         " THENCE S 12°15'30\" W, 200.00 feet; THENCE N 77°44'30\" W, 100.00 feet.",
         'N 12°15\'30" E'),
    ],
)
def test_regex_parses_common_bearing_formats(text, expected_first):
    model, _ = parse_with_regex(text)
    assert len(model.segments) == 4
    assert model.segments[0].bearing_label == expected_first
    assert all(segment.distance_ft > 0 for segment in model.segments)


# ------------------------------------------------------------ конфигурация --

def test_config_matches_shipped_file():
    import json

    shipped = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    from llm_parser import DEFAULT_CONFIG

    # ключи, которые читает код, должны присутствовать в поставляемом config.json
    for section in ("api", "drawing", "layers", "layer_colors"):
        for key in DEFAULT_CONFIG[section]:
            assert key in shipped[section], f"{section}.{key} отсутствует в config.json"
    assert shipped["drawing"]["base_scale"] == 16.0
    assert shipped["drawing"]["base_text_height"] == 0.9
    assert shipped["layers"]["property_line"] == "PROPERTY_LINE"
    assert shipped["layers"]["boundary_text"] == "BOUNDARY_TEXT_AI"


def test_scale_menu_helpers():
    """Масштабы берутся из конфига, высота текста — по формуле 0.9*scale/16."""
    pytest.importorskip("tkinter", reason="GUI-модуль требует Tk (есть в Windows-сборке Python)")
    gui = pytest.importorskip("main")

    cfg = load_config()
    values = gui.scale_options(cfg)
    assert values == ['1"=10\'', '1"=16\'', '1"=20\'', '1"=30\'']
    assert gui.default_scale(values, cfg) == '1"=16\''
    for label in values:
        scale = gui.scale_value(label)
        assert AcadBuilder.text_height_for_scale(scale, cfg) == pytest.approx(
            0.9 * scale / 16.0
        )


def test_segment_helpers_match_builder_math():
    """Дублирующая математика в модели и в builder должна совпадать."""
    for ns, degrees, minutes, seconds, ew, distance in RECT_ANGLES:
        segment = Segment(
            ns=ns, degrees=degrees, minutes=minutes, seconds=seconds, ew=ew,
            distance_ft=distance,
        )
        assert segment.azimuth_deg == pytest.approx(
            bearing_azimuth_deg(ns, degrees, minutes, seconds, ew)
        )
        assert segment.vector == pytest.approx(
            bearing_to_xy(ns, degrees, minutes, seconds, ew, distance)
        )


def test_insunits_warning_on_draw(monkeypatch):
    """Если чертёж не в футах — предупреждение попадает в результат отрисовки."""
    import acad_builder as module

    monkeypatch.setattr(module, "_co_initialize", lambda: None)
    monkeypatch.setattr(
        module, "_variant_points", lambda points: [c for p in points for c in p]
    )
    monkeypatch.setattr(
        module, "_variant_point", lambda point, z=0.0: [point[0], point[1], z]
    )

    app = FakeApp()
    app.doc.GetVariable = lambda name: 4  # millimeters
    builder = AcadBuilder(load_config())
    builder._app, builder._doc = app, app.doc
    result = builder.draw(make_description(), scale=16.0)
    assert any("INSUNITS=4" in warning for warning in result.warnings)

    # Футы (INSUNITS=2) — предупреждения нет
    app2 = FakeApp()
    builder2 = AcadBuilder(load_config())
    builder2._app, builder2._doc = app2, app2.doc
    result2 = builder2.draw(make_description(), scale=16.0)
    assert not any("INSUNITS" in warning for warning in result2.warnings)
