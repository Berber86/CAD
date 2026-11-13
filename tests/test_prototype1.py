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


class FakeEntity:
    def __init__(self, **kwargs):
        self.Layer = "0"
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeModelSpace:
    def __init__(self):
        self.polylines = []
        self.polyline_entities = []
        self.texts = []
        self.lines = []
        self.arcs = []

    def AddLightWeightPolyline(self, coordinates):
        self.polylines.append(list(coordinates))
        poly = FakePolyline()
        self.polyline_entities.append(poly)
        return poly

    def AddLine(self, start, end):
        item = FakeEntity(StartPoint=start, EndPoint=end)
        self.lines.append(item)
        return item

    def AddArc(self, center, radius, start_angle, end_angle):
        item = FakeEntity(
            Center=center,
            Radius=radius,
            StartAngle=start_angle,
            EndAngle=end_angle,
        )
        self.arcs.append(item)
        return item

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
        self.zoom_scaled = None

    @property
    def ActiveDocument(self):
        return self.doc

    def ZoomExtents(self):
        self.zoomed = True

    def ZoomScaled(self, scale, scale_type):
        self.zoom_scaled = (scale, scale_type)


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
    # PROPERTY_LINE теперь строится отдельными отрезками AddLine (без полилинии)
    assert len(space.polylines) == 0
    prop_lines = [ln for ln in space.lines if ln.Layer == "PROPERTY_LINE"]
    assert len(prop_lines) == 4
    assert result.boundary_line_count == 4
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
    # Фасад смотрит на север -> крупная подпись улицы над первой (северной) стороной.
    assert math.dist(street.point, side_one.point) < 6.0
    assert street.height is not None and street.height > plan.text_height * 1.5
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
    def __init__(self, message, prompt_tokens=612, completion_tokens=194):
        self.choices = [type("Choice", (), {"message": message})()]
        self.usage = type(
            "Usage",
            (),
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )()


class _FakeChat:
    """Имитация client.chat / client.beta.chat с настраиваемыми отказами."""

    def __init__(self, mode):
        self.mode = mode
        self.completions = self

    def parse(self, **_kwargs):
        if self.mode in {"no-parse", "no-json-schema", "no-response-format"}:
            raise RuntimeError("parse is not supported by this provider")
        from llm_parser import LegalDescription

        return _FakeCompletion(_FakeMessage(parsed=LegalDescription.model_validate(_LLM_PAYLOAD)))

    def create(self, **kwargs):
        resp_fmt = kwargs.get("response_format")
        fmt = (resp_fmt or {}).get("type")
        if self.mode in {"no-json-schema", "no-response-format"} and fmt == "json_schema":
            raise RuntimeError("json_schema is not supported")
        if self.mode == "no-response-format" and resp_fmt is not None:
            raise RuntimeError("response_format is not supported by free model")
        text = json.dumps(_LLM_PAYLOAD)
        if fmt == "json_object":
            text = "```json\n" + text + "\n```"  # провайдер добавил разметку
        elif resp_fmt is None:
            # reasoning-модель вернула блок <think> с фигурными скобками перед JSON
            text = "<think>Check {\"draft\": 1}</think>\n```json\n" + text + "\n```"
        return _FakeCompletion(_FakeMessage(content=text))


class _FakeClient:
    def __init__(self, mode, **init_kwargs):
        self.init_kwargs = init_kwargs
        self.chat = _FakeChat(mode)
        self.beta = type("Beta", (), {"chat": _FakeChat(mode)})()


@pytest.mark.parametrize(
    "mode,expected_mode",
    [
        ("structured", "structured-outputs"),
        ("no-parse", "json_schema"),
        ("no-json-schema", "json_object"),
        ("no-response-format", "prompt-json"),
    ],
)
def test_llm_extraction_paths(monkeypatch, mode, expected_mode):
    import openai

    from llm_parser import _extract_with_llm, load_config

    monkeypatch.setattr(openai, "OpenAI", lambda *_a, **kw: _FakeClient(mode, **kw))
    cfg = load_config()
    model, used_mode, usage = _extract_with_llm("x" * 50, cfg, api_key="sk-test")
    assert used_mode == expected_mode
    assert len(model.segments) == 4
    assert model.segments[0].bearing_label == 'N 75°20\'20" E'
    assert usage.available is True
    assert usage.prompt_tokens == 612
    assert usage.completion_tokens == 194
    assert usage.total_tokens == 806
    assert "612" in usage.short_report() and "194" in usage.short_report()


def test_providers_catalog_and_switching(monkeypatch):
    """Все три провайдера (Hydra AI, OpenRouter, RouterAI) маршрутизируются со своими URL и ключами."""
    import openai

    from llm_parser import (
        PROVIDER_ORDER,
        clean_provider_key,
        load_config,
        parse_legal_description_full,
        resolve_api_target,
    )

    assert PROVIDER_ORDER == ("hydra", "openrouter", "routerai")
    assert clean_provider_key("hydra", "  hk-secret$$  ") == "hk-secret"

    monkeypatch.setenv("HYDRA_API_KEY", "hk-env$$")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-env")
    monkeypatch.setenv("ROUTERAI_API_KEY", "rai-env")
    cfg = load_config()

    hydra_t = resolve_api_target(cfg, provider="hydra")
    assert hydra_t["base_url"] == "https://api.hydraai.ru/v1"
    assert hydra_t["api_key"] == "hk-env"
    assert hydra_t["model"] == "glm-5.2"

    or_t = resolve_api_target(cfg, provider="openrouter")
    assert or_t["base_url"] == "https://openrouter.ai/api/v1"
    assert or_t["api_key"] == "or-env"
    assert "nvidia/nemotron-3.5-super-vl:free" in cfg["api"]["providers"]["openrouter"]["models"]
    assert "stepfun/step-3.5-flash:free" in cfg["api"]["providers"]["openrouter"]["models"]

    rai_t = resolve_api_target(cfg, provider="routerai", model="google/gemini-2.5-pro")
    assert rai_t["base_url"] == "https://routerai.ru/api/v1"
    assert rai_t["api_key"] == "rai-env"
    assert rai_t["model"] == "google/gemini-2.5-pro"

    captured: list[dict] = []

    def fake_openai(*_a, **kw):
        captured.append(kw)
        return _FakeClient("no-parse", **kw)

    monkeypatch.setattr(openai, "OpenAI", fake_openai)
    outcome = parse_legal_description_full(
        text="x" * 60,
        cfg=cfg,
        use_llm=True,
        provider="openrouter",
        model="nvidia/nemotron-3-super-120b-a12b:free",
    )
    assert captured[-1]["base_url"] == "https://openrouter.ai/api/v1"
    assert captured[-1]["api_key"] == "or-env"
    assert outcome.provider == "openrouter"
    assert outcome.provider_label == "OpenRouter"
    assert outcome.model == "nvidia/nemotron-3-super-120b-a12b:free"
    assert outcome.usage is not None
    assert outcome.usage.prompt_tokens == 612
    assert outcome.usage.completion_tokens == 194


def test_key_protection_against_stolen_json_and_env(monkeypatch, tmp_path):
    """
    Даже если злоумышленник украл config.json и .env и знает исходный код,
    пересчитать реальные API-ключи на чужом ПК (или без PIN) невозможно.
    """
    import hashlib

    import llm_parser
    from llm_parser import (
        KeyProtectionError,
        is_protected_key,
        load_config,
        load_dotenv_dict,
        protect_api_key,
        resolve_api_target,
        save_protected_env,
        unprotect_api_key,
    )

    owner_hw = hashlib.sha256(b"owner-windows-pc-guid-001").digest()
    attacker_hw = hashlib.sha256(b"attacker-machine-guid-999").digest()
    monkeypatch.setattr(llm_parser, "_machine_fingerprint", lambda: owner_hw)

    real_or = "sk-or-v1-9876543210abcdefSECRET"
    token = protect_api_key("openrouter", real_or, secret="7391")
    assert is_protected_key(token)
    assert real_or not in token and "SECRET" not in token

    # Повторный вызов protect_api_key не создаёт двойного шифрования
    token_again = protect_api_key("openrouter", token, secret="7391")
    assert unprotect_api_key("openrouter", token_again, secret="7391") == real_or

    # Без PIN или с неверным PIN даже на том же ПК ключ не раскрывается
    with pytest.raises(KeyProtectionError):
        unprotect_api_key("openrouter", token, secret="")
    with pytest.raises(KeyProtectionError):
        unprotect_api_key("openrouter", token, secret="0000")

    # Записываем защищённый .env (с сохранением комментариев и сторонних переменных)
    env_file = tmp_path / ".env"
    env_file.write_text("# My env\nOTHER_VAR=keep_me\nOPENROUTER_API_KEY=plain_old\n", encoding="utf-8")
    monkeypatch.setattr(llm_parser, "ENV_PATH", env_file)

    save_protected_env(
        {
            "hydra": "hk-live-secret$$",
            "openrouter": real_or,
            "routerai": "rai-live-secret",
        },
        secret="7391",
        path=env_file,
    )
    raw_env_text = env_file.read_text(encoding="utf-8")
    assert "OTHER_VAR=keep_me" in raw_env_text
    assert "hk-live-secret" not in raw_env_text
    assert real_or not in raw_env_text
    assert "rai-live-secret" not in raw_env_text
    parsed_env = load_dotenv_dict(env_file)
    assert is_protected_key(parsed_env["HYDRA_API_KEY"])
    assert is_protected_key(parsed_env["OPENROUTER_API_KEY"])
    assert is_protected_key(parsed_env["ROUTERAI_API_KEY"])

    # На ПК владельца с верным PIN реальный ключ пересчитывается в RAM
    cfg = load_config(tmp_path / "nonexistent.json")
    resolved = resolve_api_target(cfg, provider="hydra", key_secret="7391")
    assert resolved["api_key"] == "hk-live-secret"

    # Симуляция кражи: злоумышленник запускает код со всеми украденными файлами на своём ПК
    monkeypatch.setattr(llm_parser, "_machine_fingerprint", lambda: attacker_hw)
    with pytest.raises(KeyProtectionError):
        unprotect_api_key("openrouter", token, secret="7391")
    with pytest.raises(KeyProtectionError):
        resolve_api_target(cfg, provider="hydra", key_secret="7391")


def test_llm_auth_error_fails_fast(monkeypatch):
    """При HTTP 401 код не делает 4 повторных вызова с заведомо неверным ключом."""
    import openai

    from llm_parser import LLMParseError, _extract_with_llm, load_config

    calls = {"n": 0}

    class AuthErr(Exception):
        status_code = 401

    class FailingChat:
        def __init__(self):
            self.completions = self

        def parse(self, **_kw):
            calls["n"] += 1
            raise AuthErr("401 Unauthorized: invalid api key")

        def create(self, **_kw):
            calls["n"] += 1
            raise AuthErr("401 Unauthorized")

    class FailingClient:
        def __init__(self):
            self.chat = FailingChat()
            self.beta = type("Beta", (), {"chat": FailingChat()})()

    monkeypatch.setattr(openai, "OpenAI", lambda *_a, **_kw: FailingClient())
    with pytest.raises(LLMParseError, match="авторизации"):
        _extract_with_llm("x" * 50, load_config(), provider="routerai", api_key="bad-key")
    assert calls["n"] == 1


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


# ------------------------------------ реальные кейсы NYC Title Survey / PDF --


def test_nyc_dartmouth_loop_commencing_tie_and_narrow_townhouse():
    """
    147 Dartmouth Loop, Staten Island (Block 3960, Lot 129):
    многозвенная привязка COMMENCING (510.52' -> 433.78' -> 208.02' DEED / 208.22' T.M.)
    не попадает в стороны таунхауса 14.00' × 125.00', узкий фасад 14.00' раскладывается
    в 2 строки, а DARTMOUTH LOOP подписывается вдоль 14', а не вдоль стены 125'.
    """
    from llm_parser import NYC_SAMPLES

    desc, warnings = parse_with_regex(NYC_SAMPLES["147 Dartmouth Loop (Staten Island)"])
    assert warnings == []
    assert len(desc.segments) == 4
    assert [round(s.distance_ft, 2) for s in desc.segments] == [14.0, 125.0, 14.0, 125.0]
    assert desc.main_street == "Dartmouth Loop"
    assert desc.main_street_note == "(38' WIDE ROADWAY EASEMENT)"
    assert desc.cross_street == "Mill Road"
    assert desc.distance_to_corner_ft == pytest.approx(208.02)
    assert desc.tie_note == "208.02' (DEED) / 208.22' (T.M.)"
    assert desc.block_number == "3960"
    assert desc.lot_number == "129"
    assert desc.borough_or_city == "Staten Island, NY"

    builder = AcadBuilder(load_config())
    plan = builder.plan(desc, scale=16.0)
    assert plan.closure.distance < 1e-5
    assert plan.area_sqft == pytest.approx(14.0 * 125.0)
    assert plan.warnings == []

    by_kind = {item.kind: item for item in plan.items}
    assert by_kind["side-1"].text == "14.00'"
    assert by_kind["side-1-note"].text == 'S 33%%d58\'44" W'
    assert "125.00'" in by_kind["side-2"].text and "side-2-note" not in by_kind
    assert by_kind["main-street"].text == "DARTMOUTH LOOP"
    assert by_kind["main-street-note"].text == "(38' WIDE ROADWAY EASEMENT)"
    # Cross street ориентирована перпендикулярно фасадной улице
    rot_diff = abs(by_kind["cross-street"].rotation - by_kind["main-street"].rotation)
    assert rot_diff == pytest.approx(math.pi / 2.0, abs=1e-6)

    # Даже без явного front_segment_index fallback по скалярному произведению
    # (front_side == 'SE' для northwesterly side of Dartmouth Loop) выбирает
    # узкий фасад 14', а не соседскую стену 125' при наклонной сетке!
    assert desc.front_side == "SE"
    desc_no_idx = desc.model_copy(update={"front_segment_index": None})
    plan_no_idx = builder.plan(desc_no_idx, scale=16.0)
    street_pt = next(i for i in plan_no_idx.items if i.kind == "main-street").point
    side1_pt = next(i for i in plan_no_idx.items if i.kind == "side-1").point
    side2_pt = next(i for i in plan_no_idx.items if i.kind == "side-2").point
    assert math.dist(street_pt, side1_pt) < math.dist(street_pt, side2_pt)


def test_nyc_jamie_lane_feet_inches_and_stacked_labels():
    """
    283 Jamie Lane, Staten Island (Block 5744, Lot 112):
    перевод футов-дюймов (13'8" = 13.67', 25'10" = 25.83'), отсечение 3 звеньев
    привязки от Arthur Kill Rd и двустрочная раскладка всех 4 узких сторон.
    """
    from llm_parser import NYC_SAMPLES

    desc, warnings = parse_with_regex(NYC_SAMPLES["283 Jamie Lane (Staten Island)"])
    assert warnings == []
    assert len(desc.segments) == 4
    assert desc.segments[0].distance_ft == pytest.approx(13.0 + 8.0 / 12.0)
    assert desc.segments[1].distance_ft == pytest.approx(25.0 + 10.0 / 12.0)
    assert desc.main_street == "Jamie Lane"
    assert desc.main_street_note == "(NOT MAPPED ON FINAL CITY PLAN)"
    assert desc.block_number == "5744"
    assert desc.lot_number == "112"

    plan = AcadBuilder(load_config()).plan(desc, scale=16.0)
    assert plan.closure.distance < 1e-5
    assert plan.warnings == []
    for side_num in (1, 2, 3, 4):
        inner = next(i for i in plan.items if i.kind == f"side-{side_num}")
        outer = next(i for i in plan.items if i.kind == f"side-{side_num}-note")
        assert point_in_polygon(inner.point, plan.vertices) is False
        assert point_in_polygon(outer.point, plan.vertices) is False


def test_nyc_borden_ave_relative_angles_no_bearings_corner_lot():
    """
    63-01 Borden Avenue, Queens (Block 2366, Lot 59):
    угловой участок без компасных румбов (interior angle 93°33'29.5",
    PARALLEL WITH ..., AT RIGHT ANGLES TO ...), невязка < 0.01 ft,
    подпись 63RD STREET вдоль боковой стороны и угол внутри вершины.
    """
    from llm_parser import NYC_SAMPLES

    desc, warnings = parse_with_regex(
        NYC_SAMPLES["63-01 Borden Ave (Queens — без румбов)"]
    )
    assert warnings == []
    assert len(desc.segments) == 4
    assert all(seg.has_bearing is False for seg in desc.segments)
    assert desc.segments[0].interior_angle == '93°33\'29.5"'
    assert desc.segments[1].course_note == "PARALLEL WITH QUEENS MIDTOWN EXPRESSWAY"
    assert desc.segments[2].course_note == "AT RIGHT ANGLES TO QUEENS MIDTOWN EXPRESSWAY"
    assert desc.main_street == "Borden Avenue"
    assert desc.cross_street == "63rd Street"
    assert desc.distance_to_corner_ft == pytest.approx(0.0)
    assert desc.front_segment_index == 3
    assert desc.cross_street_side_index == 0

    plan = AcadBuilder(load_config()).plan(desc, scale=16.0)
    assert plan.closure.distance < 0.01
    assert plan.warnings == []

    by_kind = {item.kind: item for item in plan.items}
    # На сторонах без румбов нет искусственных DMS-румбов вроде «N 90%%d00' E»
    assert by_kind["side-1"].text == "90.17'"
    assert by_kind["side-2"].text == "26.32'"
    # Короткая верхняя ремарка перенесена в 2 строки под длиной 26.32' (как в эталоне PDF)
    assert by_kind["side-2-note-2"].text == "PARALLEL WITH"
    assert by_kind["side-2-note"].text == "QUEENS MIDTOWN EXPRESSWAY"
    assert (
        by_kind["side-2"].point[1]
        > by_kind["side-2-note-2"].point[1]
        > by_kind["side-2-note"].point[1]
    )
    # Справа ремарка ближе к границе, а размер 90.00' — правее ремарки
    assert by_kind["side-3"].text == "90.00'"
    assert by_kind["side-3-note"].text == "AT RIGHT ANGLES TO QUEENS MIDTOWN EXPRESSWAY"
    assert by_kind["side-3"].point[0] > by_kind["side-3-note"].point[0]
    assert by_kind["side-4"].text == "20.72'"
    # Внутренний угол лежит внутри полигона у ЮЗ вершины вместе с угловой дугой
    assert by_kind["angle-1"].text == '93%%d33\'29.5"'
    assert point_in_polygon(by_kind["angle-1"].point, plan.vertices) is True
    assert len(plan.arcs) == 1
    assert len(plan.lines) == 4  # продолжения красных линий обеих улиц
    # BORDEN AVENUE крупным шрифтом строго выше подзаголовка (QUEENS MIDTOWN EXPRESSWAY)
    assert by_kind["main-street"].point[1] > by_kind["main-street-note"].point[1]
    # 63RD STREET параллельна боковой стороне #1 (угловой участок, без «0.00' TO CORNER»)
    assert by_kind["cross-street"].text == "63RD STREET"
    assert by_kind["cross-street"].rotation == pytest.approx(by_kind["side-1"].rotation)
    assert "cross-street-distance" not in by_kind


def test_vector_pdf_vision_pipeline_renders_and_calls_multimodal_llm(tmp_path, monkeypatch):
    """
    Векторный CAD-PDF без текстового слоя (как реальные чертежи NYC Survey):
    inspect_pdf сообщает has_text=False, render_pdf_pages_base64 рендерит PNG,
    а parse_legal_description_full отправляет мультимодальный image_url-запрос в LLM.
    """
    pymupdf = pytest.importorskip("pymupdf")
    import openai

    from llm_parser import (
        PDFError,
        inspect_pdf,
        parse_legal_description_full,
        render_pdf_pages_base64,
    )

    vec_pdf = tmp_path / "vector_survey.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=1008)
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(120, 200, 320, 620))
    shape.finish(color=(0, 0, 0), width=1.5)
    shape.commit()
    doc.save(vec_pdf)
    doc.close()

    info = inspect_pdf(vec_pdf)
    assert info["pages"] == 1
    assert info["has_text"] is False

    urls = render_pdf_pages_base64(vec_pdf, dpi=144)
    assert len(urls) == 1
    assert urls[0].startswith("data:image/png;base64,iVBOR")

    # Без LLM векторный PDF без текста даёт понятную ошибку с подсказкой про Vision
    with pytest.raises(PDFError, match="Vision"):
        parse_legal_description_full(pdf_path=vec_pdf, use_llm=False, cfg=load_config())

    # С LLM формируется мультимодальный запрос с image_url
    captured_messages: list[dict] = []

    class VisionFakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

            class _Completions:
                def create(_self, **ckwargs):
                    captured_messages.extend(ckwargs["messages"])
                    return type(
                        "Resp",
                        (),
                        {
                            "choices": [
                                type(
                                    "Choice",
                                    (),
                                    {
                                        "message": type(
                                            "Msg",
                                            (),
                                            {"content": json.dumps(_LLM_PAYLOAD)},
                                        )()
                                    },
                                )()
                            ],
                            "usage": {
                                "prompt_tokens": 1420,
                                "completion_tokens": 210,
                                "total_tokens": 1630,
                            },
                        },
                    )()

            self.chat = type("Chat", (), {"completions": _Completions()})()
            self.beta = None

    monkeypatch.setattr(openai, "OpenAI", VisionFakeOpenAI)
    outcome = parse_legal_description_full(
        pdf_path=vec_pdf,
        use_llm=True,
        provider="hydra",
        model="hydra-gemini-pro",
        api_key="hk-vision-test",
        cfg=load_config(),
    )
    assert outcome.source == "llm-vision"
    assert outcome.usage is not None and outcome.usage.total_tokens == 1630
    user_msg = next(m for m in captured_messages if m["role"] == "user")
    assert isinstance(user_msg["content"], list)
    assert any(
        block.get("type") == "image_url"
        and block["image_url"]["url"].startswith("data:image/png;base64,")
        for block in user_msg["content"]
    )


def test_vision_borden_auto_infers_interior_angle_arc_and_zoom_margin(monkeypatch):
    """
    Живой кейс из AutoCAD на 63-01 Borden Avenue: Vision LLM вычислил скошенный
    курс N 3°33'29.5" W и has_bearing=False, но оставил interior_angle=None.
    AcadBuilder обязан автоматически вывести точный внутренний угол 93°33'29.5",
    отрисовать дугу AddArc, линии продолжения улиц AddLine и выполнить ZoomScaled(0.84, 1).
    """
    import acad_builder as module

    monkeypatch.setattr(module, "_co_initialize", lambda: None)
    monkeypatch.setattr(module, "_variant_points", lambda points: [c for p in points for c in p])
    monkeypatch.setattr(module, "_variant_point", lambda point, z=0.0: [point[0], point[1], z])

    vision_desc = LegalDescription(
        main_street="Borden Avenue",
        main_street_note="(QUEENS MIDTOWN EXPRESSWAY)",
        cross_street="63rd Street",
        distance_to_corner_ft=0.0,
        front_side="S",
        front_segment_index=3,
        cross_street_side_index=0,
        segments=[
            Segment(
                ns="N",
                degrees=3,
                minutes=33,
                seconds=29.5,
                ew="W",
                distance_ft=90.17,
                has_bearing=False,
                interior_angle=None,
            ),
            Segment(
                ns="N",
                degrees=90,
                minutes=0,
                seconds=0.0,
                ew="E",
                distance_ft=26.32,
                has_bearing=False,
                course_note="PARALLEL WITH QUEENS MIDTOWN EXPY",
            ),
            Segment(
                ns="S",
                degrees=0,
                minutes=0,
                seconds=0.0,
                ew="E",
                distance_ft=90.00,
                has_bearing=False,
                course_note="AT RIGHT ANGLES TO QUEENS MIDTOWN EXPRESSWAY",
            ),
            Segment(
                ns="N",
                degrees=90,
                minutes=0,
                seconds=0.0,
                ew="W",
                distance_ft=20.72,
                has_bearing=False,
            ),
        ],
    )

    builder = AcadBuilder(load_config())
    app = FakeApp()
    builder._app, builder._doc = app, app.doc
    result = builder.draw(vision_desc, scale=16.0)

    space = app.doc.ModelSpace
    assert len(space.polylines) == 0
    assert len(space.arcs) == 1
    prop_lines = [ln for ln in space.lines if ln.Layer == "PROPERTY_LINE"]
    street_lines = [ln for ln in space.lines if ln.Layer == "STREET_ROW_AI"]
    assert len(prop_lines) == 4
    assert len(street_lines) == 4
    assert space.arcs[0].Layer == "BOUNDARY_TEXT_AI"
    assert any(t.TextString == '93%%d33\'29.5"' for t in space.texts)
    assert app.zoomed is True
    assert app.zoom_scaled == (pytest.approx(0.84), 1)
    assert result.warnings == []


def test_proactive_llm_geometry_repairs_reversed_or_unordered_vision_segments():
    """
    Упреждающая защита от типичных ошибок Vision LLM при чтении чертежа:
      1) разворот 180° для параллельной стены с одинаковой подписью румба + порядок сторон;
      2) сброс ошибочного front_segment_index с тыльной границы PARALLEL WITH;
      3) отсутствие маскировки реальной ошибки длин в deed'е (130' vs 120').
    """
    from llm_parser import _repair_llm_geometry

    # На чертеже обе длинные стены подписаны N 56°01'16" W, а стороны перечислены вразнобой:
    # [Front 14', Rear 14', Side 125', Side 125' (без разворота)] -> сырая невязка ~250 ft!
    scrambled = LegalDescription(
        main_street="Dartmouth Loop",
        front_segment_index=0,
        segments=[
            Segment(ns="S", degrees=33, minutes=58, seconds=44.0, ew="W", distance_ft=14.0),
            Segment(ns="N", degrees=33, minutes=58, seconds=44.0, ew="E", distance_ft=14.0),
            Segment(ns="N", degrees=56, minutes=1, seconds=16.0, ew="W", distance_ft=125.0),
            Segment(ns="N", degrees=56, minutes=1, seconds=16.0, ew="W", distance_ft=125.0),
        ],
    )
    repaired, notes = _repair_llm_geometry(scrambled, from_vision=True)
    assert notes
    plan = AcadBuilder(load_config()).plan(repaired, scale=16.0)
    assert plan.closure.distance < 0.01
    assert plan.area_sqft == pytest.approx(14.0 * 125.0, rel=1e-3)

    # Ошибочный front_segment_index на тыльной стороне PARALLEL WITH сбрасывается
    bad_front = LegalDescription(
        main_street="Borden Avenue",
        front_side="S",
        front_segment_index=1,
        segments=[
            Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=90.0, has_bearing=False),
            Segment(
                ns="N",
                degrees=90,
                minutes=0,
                seconds=0,
                ew="E",
                distance_ft=20.0,
                has_bearing=False,
                course_note="PARALLEL WITH BORDEN AVENUE",
            ),
            Segment(ns="S", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=90.0, has_bearing=False),
            Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="W", distance_ft=20.0, has_bearing=False),
        ],
    )
    fixed_front, front_notes = _repair_llm_geometry(bad_front, from_vision=True)
    assert fixed_front.front_segment_index is None
    assert front_notes

    # Настоящая опечатка длины в deed'е (130' вместо 120') НЕ маскируется
    bad_deed = LegalDescription(
        main_street="Main St",
        segments=[
            Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="E", distance_ft=130.0),
            Segment(ns="S", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=80.0),
            Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="W", distance_ft=120.0),
            Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=80.0),
        ],
    )
    untouched, deed_notes = _repair_llm_geometry(bad_deed, from_vision=True)
    assert deed_notes == []
    assert untouched.segments[0].distance_ft == 130.0


def test_segment_decimal_degrees_carry_cardinal_and_strict_90_bound():
    """
    Ревизия валидатора Segment:
      * дробные градусы (75.5 -> 75°30') и минуты (20.5 -> 20'30") не теряют точность;
      * перенос minutes=60 / 59.999" не падает на Field(le=59);
      * кардинальная запись LLM ns='E', degrees=0 приводится к N 90°00' E;
      * невозможный квадрантный румб > 90°00'00" (напр. N 90°30' E) отклоняется.
    """
    s_dec = Segment(ns="N", degrees=75.5, minutes=0, seconds=0, ew="E", distance_ft=100.0)
    assert (s_dec.degrees, s_dec.minutes, s_dec.seconds) == (75, 30, 0.0)

    s_min = Segment(ns="S", degrees=12, minutes=20.5, seconds=0, ew="W", distance_ft=50.0)
    assert (s_min.degrees, s_min.minutes, s_min.seconds) == (12, 20, 30.0)

    s_carry = Segment(ns="N", degrees=14, minutes=59, seconds=59.999, ew="E", distance_ft=40.0)
    assert (s_carry.degrees, s_carry.minutes, s_carry.seconds) == (15, 0, 0.0)

    s_card = Segment(ns="E", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=25.0)
    assert s_card.bearing_label == "N 90°00' E"

    with pytest.raises(Exception):
        Segment(ns="N", degrees=90, minutes=30, seconds=0, ew="E", distance_ft=100.0)


def test_json_from_text_handles_prose_braces_and_wrapper_objects():
    from llm_parser import _json_from_text

    raw = (
        "Step {1}: inspect boundary calls.\n"
        '{"result": {"main_street": "Main St", "segments": [{"ns": "N", "degrees": 0, '
        '"minutes": 0, "seconds": 0, "ew": "E", "distance_ft": 10}]}}'
    )
    parsed = _json_from_text(raw)
    assert parsed["main_street"] == "Main St"
    assert len(parsed["segments"]) == 1


def test_large_sheet_arch_d_pdf_clamps_vision_dpi_and_hybrid_titleblock_uses_vision(
    tmp_path, monkeypatch
):
    """
    1) Лист ARCH D (24"x36" = 1728x2592 pt) адаптивно ограничивает разрешение <= 3200 px;
    2) Гибридный CAD-PDF со штампом TrueType (has_text=True, has_legal_text=False)
       отправляется в Vision LLM вместе с отрендеренным чертежом, а не падает на штампе.
    """
    import base64
    pymupdf = pytest.importorskip("pymupdf")
    import openai
    from llm_parser import inspect_pdf, parse_legal_description_full, render_pdf_pages_base64

    arch_d = tmp_path / "arch_d_survey.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=1728, height=2592)  # 24" x 36"
    page.insert_text(
        (72, 72),
        "ARCHITECTURAL SURVEY CERTIFIED TO TITLE COMPANY SCALE 1 INCH = 16 FEET",
        fontsize=12,
    )
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(300, 400, 900, 1800))
    shape.finish(color=(0, 0, 0), width=2.0)
    shape.commit()
    doc.save(arch_d)
    doc.close()

    info = inspect_pdf(arch_d)
    assert info["has_text"] is True
    assert info["has_legal_text"] is False

    urls = render_pdf_pages_base64(arch_d, dpi=220)
    png_raw = base64.b64decode(urls[0].split(",", 1)[1])
    pix = pymupdf.Pixmap(png_raw)
    assert max(pix.width, pix.height) <= 3250

    class HybridVisionOpenAI:
        def __init__(self, **kwargs):
            class _Completions:
                def create(_self, **ckwargs):
                    return type(
                        "Resp",
                        (),
                        {
                            "choices": [
                                type(
                                    "Choice",
                                    (),
                                    {"message": type("Msg", (), {"content": json.dumps(_LLM_PAYLOAD)})()},
                                )()
                            ],
                            "usage": {"prompt_tokens": 900, "completion_tokens": 180, "total_tokens": 1080},
                        },
                    )()

            self.chat = type("Chat", (), {"completions": _Completions()})()
            self.beta = None

    monkeypatch.setattr(openai, "OpenAI", HybridVisionOpenAI)
    outcome = parse_legal_description_full(
        pdf_path=arch_d,
        use_llm=True,
        provider="hydra",
        model="hydra-gemini-pro",
        api_key="hk-hybrid",
        cfg=load_config(),
    )
    assert outcome.source == "llm-vision"


def test_skewed_parallelogram_nyc_relative_courses_and_acad_paperspace_shx(monkeypatch):
    """
    1) Косой параллелограмм в Queens с «parallel with 63rd Street» замыкается в < 0.01 ft;
    2) AcadBuilder.draw выставляет fontFile='romans.shx', Height=0.0 и переключает
       PaperSpace (ActiveSpace=0) в ModelSpace (ActiveSpace=1);
    3) Самопересекающийся контур («восьмёрка») выдаёт явное предупреждение в plan().
    """
    import acad_builder as module

    skewed_text = (
        "BEGINNING at the corner formed by the intersection of the northerly side of "
        "Borden Avenue with the easterly side of 63rd Street;\n"
        "RUNNING THENCE northerly along the easterly side of 63rd Street, 90.17 feet, "
        "forming an interior angle of 93°33'29.5\" with the northerly side of Borden Avenue;\n"
        "THENCE easterly parallel with Borden Avenue, 25.00 feet;\n"
        "THENCE southerly parallel with 63rd Street, 90.17 feet;\n"
        "THENCE westerly along the northerly side of Borden Avenue, 25.00 feet "
        "to the point or place of BEGINNING."
    )
    desc, _ = parse_with_regex(skewed_text)
    builder = AcadBuilder(load_config())
    plan = builder.plan(desc, scale=16.0)
    assert plan.closure.distance < 0.01

    monkeypatch.setattr(module, "_co_initialize", lambda: False)
    monkeypatch.setattr(module, "_variant_points", lambda points: [c for p in points for c in p])
    monkeypatch.setattr(module, "_variant_point", lambda point, z=0.0: [point[0], point[1], z])

    app = FakeApp()
    app.doc.ActiveSpace = 0  # имитация открытого листа Layout / PaperSpace
    builder._app, builder._doc = app, app.doc
    builder.draw(desc, scale=16.0)
    assert app.doc.ActiveSpace == 1
    style = app.doc.TextStyles.Item("AI_BOUNDARY")
    assert getattr(style, "fontFile", None) == "romans.shx"
    assert getattr(style, "Height", None) == 0.0

    bowtie = LegalDescription(
        main_street="Bowtie Rd",
        segments=[
            Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=50.0),
            Segment(ns="S", degrees=45, minutes=0, seconds=0, ew="E", distance_ft=70.71),
            Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=50.0),
            Segment(ns="S", degrees=45, minutes=0, seconds=0, ew="W", distance_ft=70.71),
        ],
    )
    bowtie_plan = builder.plan(bowtie, scale=16.0)
    assert any("самопересекается" in w or "площадь" in w for w in bowtie_plan.warnings)


# ------------------------- Привязка POB от (0, 0) и Neighbor Lots ----------


def test_aspenwall_main_and_neighbor_lots_fit_wall_to_wall_from_street_corner():
    """
    Три участка на Aspenwall Street (365', 420', 475' восточнее угла 149th Street):
      * замыкающий сегмент вдоль Aspenwall Street = S 75°20'20" W -> вектор от перекрёстка
        к POB автоматически разворачивается на 180° в N 75°20'20" E;
      * перекрёсток улиц лежит в (0.0, 0.0), линия привязки идёт от (0.0, 0.0) до POB;
      * общая стена левого соседа (365'+55'=420') и основного участка (420'),
        а также основного (420'+55'=475') и правого соседа (475') совпадают до 1e-9 ft.
    """
    from acad_builder import compute_pob_point
    from llm_parser import ASPENWALL_SAMPLES

    desc_left, w_left = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    desc_main, w_main = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    desc_right, w_right = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"])
    assert w_left == [] and w_main == [] and w_right == []

    for desc, expected_dist in ((desc_left, 365.0), (desc_main, 420.0), (desc_right, 475.0)):
        assert desc.distance_to_corner_ft == pytest.approx(expected_dist)
        assert desc.corner_to_pob_bearing is not None
        assert desc.corner_to_pob_bearing.bearing_label == "N 75°20'20\" E"
        assert desc.corner_to_pob_bearing.distance_ft == pytest.approx(expected_dist)
        pob = compute_pob_point(desc)
        assert math.hypot(*pob) == pytest.approx(expected_dist, abs=1e-9)

    builder = AcadBuilder(load_config())
    plan_left = builder.plan(desc_left, scale=16.0, is_neighbor=True)
    plan_main = builder.plan(desc_main, scale=16.0, is_neighbor=False)
    plan_right = builder.plan(desc_right, scale=16.0, is_neighbor=True)

    # Правая стена левого соседа (v3 на улице, v2 в глубине) == левая стена основного (v0, v1)
    assert math.dist(plan_left.vertices[3], plan_main.vertices[0]) == pytest.approx(0.0, abs=1e-9)
    assert math.dist(plan_left.vertices[2], plan_main.vertices[1]) == pytest.approx(0.0, abs=1e-9)

    # Правая стена основного (v3 на улице, v2 в глубине) == левая стена правого соседа (v0, v1)
    assert math.dist(plan_main.vertices[3], plan_right.vertices[0]) == pytest.approx(0.0, abs=1e-9)
    assert math.dist(plan_main.vertices[2], plan_right.vertices[1]) == pytest.approx(0.0, abs=1e-9)

    # Выносная линия привязки основного участка идёт от перекрёстка (0.0, 0.0) до POB (420')
    tie_line = next(ln for ln in plan_main.lines if ln.kind == "main-street-row-tie")
    assert math.dist(tie_line.start, (0.0, 0.0)) == pytest.approx(0.0, abs=1e-9)
    assert math.dist(tie_line.end, plan_main.pob) == pytest.approx(0.0, abs=1e-9)


def test_neighbor_mode_draws_only_neighbor_layer_and_centroid_label_in_autocad(monkeypatch):
    """
    Совместная отрисовка основного участка (420') и двух соседей (365' и 475')
    в один активный ModelSpace AutoCAD:
      * основной участок — слой PROPERTY_LINE + BOUNDARY_TEXT_AI + STREET_ROW_AI;
      * соседи — замкнутая полилиния в слое NEIGHBOR_LOT (acCyan = 4), БЕЗ подписей
        сторон/углов/улиц, только 1 компактная метка внутри центроида полигона.
    """
    import acad_builder as module
    from llm_parser import ASPENWALL_SAMPLES

    monkeypatch.setattr(module, "_co_initialize", lambda: False)
    monkeypatch.setattr(module, "_variant_points", lambda points: [c for p in points for c in p])
    monkeypatch.setattr(module, "_variant_point", lambda point, z=0.0: [point[0], point[1], z])

    builder = AcadBuilder(load_config())
    app = FakeApp()
    builder._app, builder._doc = app, app.doc

    desc_main, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    desc_left, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    desc_right, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"])

    res_main = builder.draw(desc_main, scale=16.0, is_neighbor=False)
    main_texts_before = len(app.doc.ModelSpace.texts)
    main_lines_before = len(app.doc.ModelSpace.lines)

    res_left = builder.draw(desc_left, scale=16.0, is_neighbor=True)
    res_right = builder.draw(desc_right, scale=16.0, is_neighbor=True)

    space = app.doc.ModelSpace
    assert len(space.polyline_entities) == 0
    prop_lines = [ln for ln in space.lines if ln.Layer == "PROPERTY_LINE"]
    neighbor_lines = [ln for ln in space.lines if ln.Layer == "NEIGHBOR_LOT"]
    street_lines = [ln for ln in space.lines if ln.Layer == "STREET_ROW_AI"]
    assert len(prop_lines) == 4
    assert len(neighbor_lines) == 8
    assert app.doc.Layers.Item("NEIGHBOR_LOT").color == 4
    assert res_main.is_neighbor is False
    assert res_left.is_neighbor is True and res_right.is_neighbor is True

    # Соседи не добавляют ни одной линии улицы и создают ровно по 1 тексту в центроиде
    assert len(street_lines) == main_lines_before - 4
    assert res_left.text_count == 1
    assert res_right.text_count == 1
    assert len(space.texts) == main_texts_before + 2

    neighbor_texts = space.texts[-2:]
    assert [t.TextString for t in neighbor_texts] == [
        "NEIGHBOR (365' from corner)",
        "NEIGHBOR (475' from corner)",
    ]
    assert {t.Layer for t in neighbor_texts} == {"NEIGHBOR_LOT"}
    assert point_in_polygon(res_left.plan.items[0].point, res_left.vertices) is True
    assert point_in_polygon(res_right.plan.items[0].point, res_right.vertices) is True


def test_corner_to_pob_bearing_inference_and_llm_repair_edge_cases():
    """
    Проверяет граничные случаи вычисления POB и центроида:
      * угловой участок (distance_to_corner_ft=0.0) -> POB=(0,0), corner_to_pob_bearing=None;
      * чистая модель LegalDescription без текста разворачивает замыкающий румб на 180°;
      * _repair_llm_geometry исправляет неразвёрнутый LLM-румб при 'easterly from the corner';
      * узкий таунхаус (14'×125') и Г-образный участок держат метку строго внутри полигона.
    """
    from acad_builder import compute_pob_point, polygon_centroid
    from llm_parser import (
        ASPENWALL_SAMPLES,
        BoundarySegment,
        LegalDescriptionData,
        NYC_SAMPLES,
        _repair_llm_geometry,
    )

    assert BoundarySegment is Segment
    assert LegalDescriptionData is LegalDescription

    # 1) Чистая модель без текста: замыкающая сторона S 75°20'20" W -> N 75°20'20" E
    pure_nyc = LegalDescription(
        main_street="Aspenwall Street",
        cross_street="149th Street",
        distance_to_corner_ft=420.0,
        front_segment_index=3,
        segments=[
            Segment(ns="S", degrees=14, minutes=39, seconds=40, ew="E", distance_ft=100.0),
            Segment(ns="N", degrees=75, minutes=20, seconds=20, ew="E", distance_ft=55.0),
            Segment(ns="N", degrees=14, minutes=39, seconds=40, ew="W", distance_ft=100.0),
            Segment(ns="S", degrees=75, minutes=20, seconds=20, ew="W", distance_ft=55.0),
        ],
    )
    assert pure_nyc.corner_to_pob_bearing is not None
    assert pure_nyc.corner_to_pob_bearing.bearing_label == "N 75°20'20\" E"

    # 2) Если LLM ошибочно вернула S 75°20'20" W при 'easterly from the corner', repair разворачивает
    wrong_tie = Segment(ns="S", degrees=75, minutes=20, seconds=20, ew="W", distance_ft=420.0)
    bad_llm = pure_nyc.model_copy(update={"corner_to_pob_bearing": wrong_tie})
    repaired, notes = _repair_llm_geometry(
        bad_llm, source_text=ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    )
    assert repaired.corner_to_pob_bearing is not None
    assert repaired.corner_to_pob_bearing.bearing_label == "N 75°20'20\" E"
    assert any("corner_to_pob_bearing" in n for n in notes)

    # 3) Угловой участок (63-01 Borden Ave): POB строго (0.0, 0.0)
    corner_desc, _ = parse_with_regex(NYC_SAMPLES["63-01 Borden Ave (Queens — без румбов)"])
    assert corner_desc.distance_to_corner_ft == pytest.approx(0.0)
    assert corner_desc.corner_to_pob_bearing is None
    assert compute_pob_point(corner_desc) == (0.0, 0.0)

    # 4) Узкий таунхаус 14'×125' как сосед: метка поворачивается вдоль длинной стены 125'
    th_desc, _ = parse_with_regex(NYC_SAMPLES["147 Dartmouth Loop (Staten Island)"])
    th_plan = AcadBuilder(load_config()).plan(th_desc, scale=16.0, is_neighbor=True)
    assert len(th_plan.items) == 1
    assert point_in_polygon(th_plan.items[0].point, th_plan.vertices) is True

    # 5) Г-образный сильно вогнутый участок: центроид гарантированно внутри контура
    concave_l = [(0.0, 0.0), (100.0, 0.0), (100.0, 12.0), (12.0, 12.0), (12.0, 100.0), (0.0, 100.0)]
    c_pt = polygon_centroid(concave_l)
    assert point_in_polygon(c_pt, concave_l) is True


# ---------------------------------------------------------------------------
# Финальные геодезические правила Николая: отрезки LINE, аналитическое
# закрытие прямоугольной трапеции, автопоиск опечаток и размещение гэпа
# ---------------------------------------------------------------------------


def test_borden_ave_analytical_right_trapezoid_closes_to_zero_with_explanation():
    """
    Исключение №1 (63-01 Borden Ave):
    * Фронт по Borden Ave (20.72') и перпендикулярная глубина под 90° (90.00') неприкосновенны;
    * Косая сторона по 63rd St (90.17') уточняется до 90 / cos(3°33'29.5") = 90.174010';
    * Задняя параллельная сторона (26.32') уточняется до 20.72 + 90 * tan(3°33'29.5") = 26.316405';
    * Контур замыкается строго в 0.000000 ft из 4 отдельных отрезков LINE без разрывов,
      подписи на чертеже остаются 90.17' и 26.32', а в plan.notes пишется явное пояснение.
    """
    from llm_parser import NYC_SAMPLES

    desc, warnings = parse_with_regex(NYC_SAMPLES["63-01 Borden Ave (Queens — без румбов)"])
    assert warnings == []

    plan = AcadBuilder(load_config()).plan(desc, scale=16.0)
    assert plan.closure.distance == pytest.approx(0.0, abs=1e-9)
    assert plan.gap_points is None
    assert plan.warnings == []
    assert len(plan.boundary_lines) == 4
    assert any("Аналитическое замыкание прямоугольной трапеции" in n for n in plan.notes)
    assert any("90.1738'" in n and "26.3164'" in n for n in plan.notes)

    # Проверяем точные длины 4 отрезков LINE: [90.173824, 26.316405, 90.000000, 20.720000]
    line_lengths = [
        math.hypot(ln.end[0] - ln.start[0], ln.end[1] - ln.start[1])
        for ln in plan.boundary_lines
    ]
    alpha = math.radians(3.0 + 33.0 / 60.0 + 29.5 / 3600.0)
    assert line_lengths[0] == pytest.approx(90.0 / math.cos(alpha), abs=1e-6)
    assert line_lengths[1] == pytest.approx(20.72 + 90.0 * math.tan(alpha), abs=1e-6)
    assert line_lengths[2] == pytest.approx(90.00, abs=1e-9)
    assert line_lengths[3] == pytest.approx(20.72, abs=1e-9)

    # Все 4 угла состыкованы в ноль, а подписи на чертеже сохраняют паспортные 90.17' и 26.32'
    for idx in range(4):
        assert plan.boundary_lines[idx].end == pytest.approx(
            plan.boundary_lines[(idx + 1) % 4].start, abs=1e-9
        )
    texts = {it.text for it in plan.items}
    assert "90.17'" in texts
    assert "26.32'" in texts


def test_obvious_typos_repaired_with_mandatory_russian_explanations():
    """
    Исключения №2 и №3:
      1) Опечатка направления N↔S (например N 75°20'20" W вместо S 75°20'20" W);
      2) Опечатка направления E↔W (например S 14°39'40" W вместо S 14°39'40" E);
      3) Опечатка цифры в угле (3↔8 / 8↔0 / 5↔8: N 78°20'20" E вместо N 75°20'20" E);
      4) Опечатка похожей цифры в длине (5↔6: задняя сторона 56.00' вместо 55.00',
         при этом фасадная сторона по улице 55.00' защищена и не трогается!).
    Во всех 4 случаях контур исправляется и обязательно возвращается явное пояснение.
    """
    base_deed = (
        "ALL that certain plot, piece or parcel of land, situate, lying and being in the "
        "Borough of Queens, BEGINNING at a point on the southerly side of Aspenwall Street, "
        "distant 420.00 feet easterly from the corner formed by the intersection of the "
        "southerly side of Aspenwall Street with the easterly side of 149th Street; "
        "RUNNING THENCE South 14 degrees 39 minutes 40 seconds East, 100.00 feet; "
        "THENCE North 75 degrees 20 minutes 20 seconds East, 55.00 feet; "
        "THENCE North 14 degrees 39 minutes 40 seconds West, 100.00 feet to the southerly side "
        "of Aspenwall Street; "
        "THENCE South 75 degrees 20 minutes 20 seconds West along the southerly side of "
        "Aspenwall Street, 55.00 feet to the point or place of BEGINNING."
    )

    # 1) Опечатка N↔S в 4-й стороне
    deed_ns = base_deed.replace(
        "THENCE South 75 degrees 20 minutes 20 seconds West",
        "THENCE North 75 degrees 20 minutes 20 seconds West",
    )
    d1, w1 = parse_with_regex(deed_ns)
    assert d1.segments[3].bearing_label == 'S 75°20\'20" W'
    assert any("опечатка направления" in w and "N→S" in w for w in w1)
    assert AcadBuilder(load_config()).plan(d1).closure.distance < 1e-6

    # 2) Опечатка E↔W в 1-й стороне
    deed_ew = base_deed.replace(
        "South 14 degrees 39 minutes 40 seconds East",
        "South 14 degrees 39 minutes 40 seconds West",
    )
    d2, w2 = parse_with_regex(deed_ew)
    assert d2.segments[0].bearing_label == 'S 14°39\'40" E'
    assert any("опечатка направления" in w and "W→E" in w for w in w2)
    assert AcadBuilder(load_config()).plan(d2).closure.distance < 1e-6

    # 3) Опечатка цифры в градусах угла (78° вместо 75° на задней параллельной стороне)
    deed_ang = base_deed.replace(
        "THENCE North 75 degrees 20 minutes 20 seconds East, 55.00 feet",
        "THENCE North 78 degrees 20 minutes 20 seconds East, 55.00 feet",
    )
    d3, w3 = parse_with_regex(deed_ang)
    assert d3.segments[1].bearing_label == 'N 75°20\'20" E'
    assert any("опечатка цифр в угле" in w and "8→5" in w for w in w3)
    assert AcadBuilder(load_config()).plan(d3).closure.distance < 1e-6

    # 4) Опечатка похожей цифры 6↔5 в длине задней стороны (56.00' вместо 55.00'):
    # фасад по Aspenwall Street (индекс 3, 55.00') защищён, исправляется именно задняя сторона №2!
    deed_len = base_deed.replace(
        "THENCE North 75 degrees 20 minutes 20 seconds East, 55.00 feet",
        "THENCE North 75 degrees 20 minutes 20 seconds East, 56.00 feet",
    )
    d4, w4 = parse_with_regex(deed_len)
    assert d4.segments[3].distance_ft == pytest.approx(55.0)
    assert d4.segments[1].distance_ft == pytest.approx(55.0)
    assert any("стороны №2" in w and "6→5" in w for w in w4)
    assert AcadBuilder(load_config()).plan(d4).closure.distance < 1e-6


def test_open_gap_placement_regular_and_two_street_corner_lots():
    """
    Когда опечатки нет (косоугольная фигура с округлением или неопределяемое расхождение),
    все N отрезков LINE строятся со СТРОГО паспортными длинами и углами, без принудительного
    замыкания, а открытый гэп выводится:
      * Рядовой участок, дорога сверху -> правый нижний угол (задняя сторона);
      * Рядовой участок, дорога снизу -> правый верхний угол (задняя сторона);
      * Угловой участок с фронтом на 2 улицы (боковая улица слева ИЛИ справа) -> строго во
        внутренний задний угол, НЕ соприкасающийся ни с одной из двух улиц!
    """
    builder = AcadBuilder(load_config())

    # --- А. Рядовой участок, дорога сверху (Main St на севере от лота, front_segment_index=3) ---
    # Сторона 2 (правая стена) короче на 0.40' (99.60' вместо 100.00' — не одноцифровая опечатка)
    reg_top = LegalDescription(
        main_street="Aspenwall Street",
        cross_street="149th Street",
        distance_to_corner_ft=100.0,
        front_side="south",
        front_segment_index=3,
        segments=[
            Segment(ns="S", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=100.0),   # v0->v1 левая (вниз)
            Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="E", distance_ft=50.0),   # v1->v2 задняя (вправо)
            Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="W", distance_ft=99.60),   # v2->v3 правая (вверх)
            Segment(ns="S", degrees=90, minutes=0, seconds=0, ew="W", distance_ft=50.0),   # v3->v0 фасад сверху (влево)
        ],
    )
    plan_top = builder.plan(reg_top)
    assert plan_top.gap_points is not None
    assert plan_top.gap_vertex_index == 2  # правый нижний угол!
    assert "правый нижний угол (дорога сверху)" in plan_top.gap_corner_label
    # Все 4 отрезка LINE имеют 100% паспортные длины [100.0, 50.0, 99.60, 50.0]
    lens_top = [math.hypot(l.end[0] - l.start[0], l.end[1] - l.start[1]) for l in plan_top.boundary_lines]
    assert lens_top == pytest.approx([100.0, 50.0, 99.60, 50.0], abs=1e-9)
    # Уличные вершины v3 и v0 состыкованы идеально, разрыв локализован ровно между line[1].end и line[2].start!
    assert plan_top.boundary_lines[3].end == pytest.approx(plan_top.boundary_lines[0].start, abs=1e-9)
    assert plan_top.boundary_lines[2].end == pytest.approx(plan_top.boundary_lines[3].start, abs=1e-9)
    assert plan_top.gap_points == (plan_top.boundary_lines[2].start, plan_top.boundary_lines[1].end)

    # --- Б. Рядовой участок, дорога снизу (Main St на юге от лота, front_segment_index=0) ---
    reg_bot = LegalDescription(
        main_street="Broadway",
        cross_street="1st Ave",
        distance_to_corner_ft=80.0,
        front_side="north",
        front_segment_index=0,
        segments=[
            Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="E", distance_ft=50.0),   # v0->v1 фасад снизу (вправо)
            Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=100.0),   # v1->v2 правая (вверх)
            Segment(ns="S", degrees=90, minutes=0, seconds=0, ew="W", distance_ft=50.0),   # v2->v3 задняя сверху (влево)
            Segment(ns="S", degrees=0, minutes=0, seconds=0, ew="W", distance_ft=99.60),   # v3->v0 левая (вниз)
        ],
    )
    plan_bot = builder.plan(reg_bot)
    assert plan_bot.gap_points is not None
    assert plan_bot.gap_vertex_index == 2  # правый верхний угол (x=+130, y=+100)!
    assert "правый верхний угол (дорога снизу)" in plan_bot.gap_corner_label

    # --- В. Угловой участок с 2 улицами: улица №1 сверху (edge 3), улица №2 СПРАВА (edge 2) ---
    # Здесь правый нижний угол (v2) касается боковой улицы Right Ave! Поэтому гэп ОБЯЗАН уйти
    # в ЛЕВЫЙ нижний внутренний угол v1 (единственную вершину вне обеих улиц)!
    corner_right_street = LegalDescription(
        main_street="Top Avenue",
        cross_street="Right Street",
        distance_to_corner_ft=0.0,
        front_segment_index=3,
        cross_street_side_index=2,
        segments=[
            Segment(ns="S", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=100.0),   # edge 0: v0->v1 внутренняя левая
            Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="E", distance_ft=50.0),   # edge 1: v1->v2 внутренняя задняя
            Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="W", distance_ft=99.60),   # edge 2: v2->v3 по Right Street!
            Segment(ns="S", degrees=90, minutes=0, seconds=0, ew="W", distance_ft=50.0),   # edge 3: v3->v0 по Top Avenue!
        ],
    )
    plan_corner = builder.plan(corner_right_street)
    assert plan_corner.gap_points is not None
    assert plan_corner.gap_vertex_index == 1  # левый внутренний угол вне обеих улиц!
    assert "вне обеих улиц" in plan_corner.gap_corner_label
    # Оба уличных фронта (edge 2 по Right St и edge 3 по Top Ave) состыкованы без малейшего сдвига!
    assert plan_corner.boundary_lines[1].end == pytest.approx(plan_corner.boundary_lines[2].start, abs=1e-9)
    assert plan_corner.boundary_lines[2].end == pytest.approx(plan_corner.boundary_lines[3].start, abs=1e-9)
    assert plan_corner.boundary_lines[3].end == pytest.approx(plan_corner.boundary_lines[0].start, abs=1e-9)


# ---------------------------------------------------------------------------
# 2–3 независимых прохода LLM, сравнение ответов и повторный арбитраж
# ---------------------------------------------------------------------------


def test_compare_legal_descriptions_ignores_cosmetics_and_catches_real_diffs():
    """
    Сравнение 2–3 ответов LLM:
      * НЕ даёт ложных конфликтов на сокращениях улиц ('Aspenwall St.' vs 'ASPENWALL STREET')
        и на эквивалентных осевых румбах ('N 0°00' E' vs 'N 0°00' W');
      * Ловит расхождения в длине стороны, угле/румбе, числе сторон, привязке POB и смене улицы.
    """
    from llm_parser import ParseOutcome, compare_legal_descriptions

    d_a = LegalDescription(
        main_street="Aspenwall St.",
        cross_street="63rd Ave",
        distance_to_corner_ft=100.0,
        segments=[
            Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=90.0),
            Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="E", distance_ft=25.0),
            Segment(ns="S", degrees=0, minutes=0, seconds=0, ew="W", distance_ft=90.0),
            Segment(ns="S", degrees=90, minutes=0, seconds=0, ew="W", distance_ft=25.0),
        ],
    )
    d_b_cosmetic = LegalDescription(
        main_street="ASPENWALL STREET (60' WIDE)",
        cross_street="63rd Avenue",
        distance_to_corner_ft=100.0,
        segments=[
            Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="W", distance_ft=90.0),
            Segment(ns="S", degrees=90, minutes=0, seconds=0, ew="E", distance_ft=25.0),
            Segment(ns="S", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=90.0),
            Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="W", distance_ft=25.0),
        ],
    )
    agreed, diffs = compare_legal_descriptions(
        [ParseOutcome(data=d_a, source="llm"), ParseOutcome(data=d_b_cosmetic, source="llm")]
    )
    assert agreed is True
    assert diffs == []

    # Реальное отличие в длине стороны №2 (25.00' vs 28.00') и румбе стороны №1 (0° vs 1°)
    d_c_diff = d_a.model_copy(
        update={
            "segments": [
                Segment(ns="N", degrees=1, minutes=0, seconds=0, ew="E", distance_ft=90.0),
                Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="E", distance_ft=28.0),
                Segment(ns="S", degrees=0, minutes=0, seconds=0, ew="W", distance_ft=90.0),
                Segment(ns="S", degrees=90, minutes=0, seconds=0, ew="W", distance_ft=25.0),
            ]
        }
    )
    agreed2, diffs2 = compare_legal_descriptions(
        [ParseOutcome(data=d_a, source="llm"), ParseOutcome(data=d_c_diff, source="llm")]
    )
    assert agreed2 is False
    field_ids = {item.field_id for item in diffs2}
    assert "segments[0].bearing" in field_ids
    assert "segments[1].distance_ft" in field_ids


def test_multipass_3_models_parallel_and_prompt_injected_arbitration(monkeypatch):
    """
    Проверяет 3 независимых параллельных вызова (как с разными провайдерами, так и с
    одинаковой моделью), суммирование токенов, отчёт о конфликте и повторный вызов
    «Попробуй разобраться ещё раз» с инъекцией фокуса на спорный момент.
    """
    import copy
    import llm_parser
    from llm_parser import (
        ASPENWALL_SAMPLES,
        PassTarget,
        TokenUsage,
        build_discrepancy_focus_prompt,
        load_config,
        parse_with_regex,
        recheck_consensus_discrepancies,
        run_multipass_consensus,
    )

    sample = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
    clean_desc, _ = parse_with_regex(sample)
    # Проход #3 в 1-м раунде расходится по ширине участка (60' вместо 55' на обеих сторонах,
    # чтобы это не было одиночной опечаткой, а стало настоящим спором между моделями!)
    wide_segs = list(clean_desc.segments)
    wide_segs[1] = wide_segs[1].model_copy(update={"distance_ft": 60.0})
    wide_segs[3] = wide_segs[3].model_copy(update={"distance_ft": 60.0})
    disputed_desc = clean_desc.model_copy(update={"segments": wide_segs})

    cfg = copy.deepcopy(load_config())
    cfg["api"]["providers"]["hydra"]["api_key"] = "sk-h"
    cfg["api"]["providers"]["openrouter"]["api_key"] = "sk-o"
    cfg["api"]["providers"]["routerai"]["api_key"] = "sk-r"

    targets = [
        PassTarget(pass_index=1, provider="hydra", model="glm-5.2"),
        PassTarget(pass_index=2, provider="hydra", model="glm-5.2"),  # та же самая модель!
        PassTarget(pass_index=3, provider="routerai", model="openai/gpt-4.1"),
    ]

    seen_prompts: list[tuple[str, str, str | None]] = []

    def fake_extract(text, cfg_arg, api_key=None, progress=None, *, provider=None, model=None, key_secret="", image_data_urls=None, focus_instruction=None):
        seen_prompts.append((str(provider), str(model), focus_instruction))
        usage = TokenUsage(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            provider=str(provider),
            provider_label=str(provider),
            model=str(model),
        )
        if focus_instruction is None and provider == "routerai":
            return disputed_desc, "structured-outputs", usage
        return clean_desc, "structured-outputs", usage

    monkeypatch.setattr(llm_parser, "_extract_with_llm", fake_extract)

    # Раунд #1: 3 параллельных вызова -> конфликт в сторонах №2 и №4 (55.00' vs 60.00')
    res_r1 = run_multipass_consensus(text=sample, cfg=cfg, targets=targets)
    assert res_r1.agreed is False
    assert res_r1.round_number == 1
    assert len(res_r1.pass_outcomes) == 3
    assert res_r1.total_usage.total_tokens == 450  # 3 * 150
    report = res_r1.discrepancy_report()
    assert "Сторона №2 — длина" in report
    assert "55.00'" in report and "60.00'" in report

    # Проверяем промпт-инъекцию для арбитража
    focus = build_discrepancy_focus_prompt(res_r1)
    assert "ARBITRATION ROUND #2" in focus
    assert "Сторона №2 — длина" in focus
    assert "Сторона №4 — длина" in focus

    # Раунд #2 («Попробуй разобраться ещё раз»): все 3 модели получают фокусный промпт и сходятся!
    res_r2 = recheck_consensus_discrepancies(res_r1, cfg=cfg)
    assert res_r2.agreed is True
    assert res_r2.round_number == 2
    assert res_r2.total_usage.total_tokens == 900  # 450 (раунд #1) + 450 (раунд #2)
    assert any("Конфликт разрешён на повторной проверке" in w for w in res_r2.selected_outcome.warnings)
    assert len(seen_prompts) == 6
    assert all(p[2] is not None and "Сторона №2 — длина" in p[2] for p in seen_prompts[3:])


def test_1980s_jpg_exif_rotation_scan_enhancer_and_fractional_inches(tmp_path):
    """
    Стресс-тест конвейера под архивные чертежи 1980-х годов:
      1) нормализация смартфонного EXIF Orientation=6 (кадр лёжа на боку) + ручной поворот 90°/180°;
      2) пакет из 4 крупных .jpg — адаптивный ресайз ≤2200 px, улучшение контраста синьки и JPEG 4:4:4;
      3) дробные дюймы 80-х ('25\\'-6 1/2\"', '100\\' 3-1/4\"', '50\\'-1/2\"', '25\\'-6½\"');
      4) автоисправление перевёрнутой опечатки цифр 6↔9 (60.00' vs 90.00') с русским пояснением.
    """
    import base64
    import io

    PIL_Image = pytest.importorskip("PIL.Image")
    from llm_parser import (
        Segment,
        parse_with_regex,
        prepare_survey_image_bytes,
        render_image_files_base64,
    )

    # 1. Имитируем смартфонное фото портретного листа 400×600, сохранённое боком (600×400) с EXIF Orientation=6
    # В «сыром» буфере при Orientation=6 (90° CW при показе) будущий левый верхний угол находится в левом нижнем углу (x=0..30, y=370..400).
    raw_cam = PIL_Image.new("RGB", (600, 400), (210, 205, 185))
    for x in range(30):
        for y in range(370, 400):
            raw_cam.putpixel((x, y), (220, 20, 20))
    exif = raw_cam.getexif()
    exif[0x0112] = 6  # Rotate 90 CW
    exif_jpg = tmp_path / "survey_1984_phone.jpg"
    raw_cam.save(exif_jpg, format="JPEG", quality=95, exif=exif.tobytes())

    out_bytes, mime, meta = prepare_survey_image_bytes(
        exif_jpg, rotation_deg=0, enhance_scan=True, output_format="jpeg"
    )
    assert mime == "image/jpeg"
    assert meta["exif_transposed"] is True
    assert (meta["width"], meta["height"]) == (400, 600)
    upright = PIL_Image.open(io.BytesIO(out_bytes))
    r_tl, g_tl, b_tl = upright.getpixel((10, 10))
    assert r_tl > 180 and g_tl < 70 and b_tl < 70  # красная метка встала точно в левый верхний угол!

    # Поворот +90° по часовой стрелке переводит метку из левого верхнего в правый верхний угол (600×400)
    rot90_bytes, _, meta90 = prepare_survey_image_bytes(
        exif_jpg, rotation_deg=90, enhance_scan=False, output_format="png"
    )
    assert (meta90["width"], meta90["height"]) == (600, 400)
    im90 = PIL_Image.open(io.BytesIO(rot90_bytes))
    r_tr, g_tr, _ = im90.getpixel((590, 10))
    assert r_tr > 180 and g_tr < 70

    # 2. Пакет из 4 крупных (3200×2400) фотографий старой синьки — адаптивное сжатие ≤2200 px
    bundle: list = []
    for i in range(4):
        p = tmp_path / f"diazo_1982_part_{i + 1}.jpg"
        big = PIL_Image.new("RGB", (3200, 2400), (175, 185, 205))
        big.save(p, format="JPEG", quality=90)
        bundle.append(p)

    urls, notes = render_image_files_base64(bundle, rotation_deg=[0, 90, 180, 270], enhance_scan=True)
    assert len(urls) == 4
    assert all(u.startswith("data:image/jpeg;base64,") for u in urls)
    assert any("поворот +180°" in n for n in notes)
    b0 = base64.b64decode(urls[0].split(",", 1)[1])
    im_b0 = PIL_Image.open(io.BytesIO(b0))
    assert max(im_b0.size) <= 2200

    # 3. Дробные дюймы в стиле 1980-х ('25'-6 1/2"', '100' 3-1/4"', '25'-6½"')
    seg_frac = Segment(ns="N", degrees=90, ew="E", distance_ft="25'-6 1/2\"")
    assert seg_frac.distance_ft == pytest.approx(25.0 + 6.5 / 12.0, abs=1e-4)
    seg_vulgar = Segment(ns="N", degrees=0, ew="E", distance_ft="100' 3-1/4\"")
    assert seg_vulgar.distance_ft == pytest.approx(100.0 + 3.25 / 12.0, abs=1e-4)

    deed_80s = (
        "BEGINNING at the corner formed by the intersection of the northerly side of "
        "84th Avenue with the easterly side of 164th Street;\n"
        "RUNNING THENCE N 00°00'00\" E along 164th Street, 100' 3-1/4\";\n"
        "THENCE N 90°00'00\" E, 25'-6½\";\n"
        "THENCE S 00°00'00\" W, 100' 3-1/4\";\n"
        "THENCE S 90°00'00\" W along 84th Avenue, 25'-6 1/2\" to the point of BEGINNING."
    )
    parsed_80s, _ = parse_with_regex(deed_80s)
    assert parsed_80s.segments[0].distance_ft == pytest.approx(100.270833, abs=1e-4)
    assert parsed_80s.segments[1].distance_ft == pytest.approx(25.541667, abs=1e-4)

    # 4. Перевёрнутая опечатка цифр 6↔9 в длине тыльной стороны (90.00' вместо 60.00')
    deed_inverted_6_9 = (
        "BEGINNING at a point on the southerly side of Maple Road, distant 120.00 feet "
        "easterly from the corner of Maple Road and 1st Street;\n"
        "THENCE S 00°00'00\" E, 100.00 feet;\n"
        "THENCE N 90°00'00\" E, 90.00 feet;\n"
        "THENCE N 00°00'00\" W, 100.00 feet to the southerly side of Maple Road;\n"
        "THENCE S 90°00'00\" W along Maple Road, 60.00 feet to the point of BEGINNING."
    )
    fixed_6_9, warns_6_9 = parse_with_regex(deed_inverted_6_9)
    assert fixed_6_9.segments[1].distance_ft == pytest.approx(60.0)
    assert any("9→6" in w and "90.00' → 60.00'" in w for w in warns_6_9)


def test_photo_pdf_page_range_and_ai_auto_orient_prepass(tmp_path, monkeypatch):
    """
    Проверяет многостраничный PDF-альбом из фотографий старых чертежей:
      * выбор конкретных страниц (pdf_pages='2, 4' из 5);
      * ИИ-автоповорот (auto_orient=True): предпроход по превью 768 px определяет,
        что стр. #2 перевёрнута вверх ногами (+180°), а стр. #4 лежит на боку (+90°),
        и физически переворачивает full-res кадры ДО основного геометрического чтения!
    """
    import base64
    import io
    import openai

    pymupdf = pytest.importorskip("pymupdf")
    PIL_Image = pytest.importorskip("PIL.Image")
    from llm_parser import (
        PDFError,
        inspect_pdf,
        load_config,
        parse_legal_description_full,
        parse_page_range,
    )

    assert parse_page_range("2, 4-5", 6) == [1, 3, 4]
    assert parse_page_range("все", 5, max_pages=2) == [0, 1, 2, 3, 4]
    with pytest.raises(PDFError):
        parse_page_range("1-9", 5)

    # Собираем 5-страничный фото-PDF, где стр. 2 перевёрнута на 180° (красная шапка снизу!),
    # а стр. 4 имеет альбомный формат 800×500 (нужен поворот +90° CW -> станет 500×800).
    album_pdf = tmp_path / "archive_1981_album.pdf"
    doc = pymupdf.open()
    for p_i in range(5):
        if p_i == 1:  # Страница №2 (0-based 1): вверх ногами — шапка снизу!
            im = PIL_Image.new("RGB", (500, 800), (235, 232, 220))
            for x in range(500):
                for y in range(760, 800):
                    im.putpixel((x, y), (225, 25, 25))
            pw, ph = 500, 800
        elif p_i == 3:  # Страница №4 (0-based 3): на боку (800×500)
            im = PIL_Image.new("RGB", (800, 500), (230, 235, 225))
            pw, ph = 800, 500
        else:
            im = PIL_Image.new("RGB", (500, 700), (240, 240, 240))
            pw, ph = 500, 700
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=92)
        page = doc.new_page(width=pw, height=ph)
        page.insert_image(pymupdf.Rect(0, 0, pw, ph), stream=buf.getvalue())
    doc.save(album_pdf)
    doc.close()

    info = inspect_pdf(album_pdf)
    assert info["pages"] == 5
    assert info["has_raster_images"] is True

    calls: list[list[dict]] = []

    class AutoOrientFakeOpenAI:
        def __init__(self, **kwargs):
            class _Completions:
                def create(_self, **ckwargs):
                    msgs = ckwargs["messages"]
                    calls.append(msgs)
                    user_blocks = msgs[-1]["content"]
                    prompt_txt = user_blocks[0]["text"]
                    if "CLOCKWISE rotation in degrees" in prompt_txt:
                        # Предпроход автоповорота: проверяем, что присланы именно 2 компактных превью ≤768 px
                        img_blocks = [b for b in user_blocks if b.get("type") == "image_url"]
                        assert len(img_blocks) == 2
                        for ib in img_blocks:
                            raw_t = base64.b64decode(ib["image_url"]["url"].split(",", 1)[1])
                            assert max(PIL_Image.open(io.BytesIO(raw_t)).size) <= 768
                        payload = {
                            "rotations": [
                                {"index": 1, "clockwise_deg": 180, "reason": "upside-down title block"},
                                {"index": 2, "clockwise_deg": 90, "reason": "sideways landscape scan"},
                            ]
                        }
                        tok = {"prompt_tokens": 240, "completion_tokens": 30, "total_tokens": 270}
                    else:
                        payload = _LLM_PAYLOAD
                        tok = {"prompt_tokens": 1300, "completion_tokens": 200, "total_tokens": 1500}
                    return type(
                        "Resp",
                        (),
                        {
                            "choices": [
                                type(
                                    "Choice",
                                    (),
                                    {"message": type("Msg", (), {"content": json.dumps(payload)})()},
                                )()
                            ],
                            "usage": tok,
                        },
                    )()

            self.chat = type("Chat", (), {"completions": _Completions()})()
            self.beta = None

    monkeypatch.setattr(openai, "OpenAI", AutoOrientFakeOpenAI)
    outcome = parse_legal_description_full(
        pdf_path=album_pdf,
        pdf_pages="2, 4",
        auto_orient=True,
        use_llm=True,
        provider="routerai",
        model="google/gemini-2.5-pro",
        api_key="rk-test-orient",
        cfg=load_config(),
    )

    assert len(calls) == 2
    assert outcome.usage is not None and outcome.usage.total_tokens == 1770
    assert any("Автоповорот ИИ" in w and "#1 → +180°" in w and "#2 → +90°" in w for w in outcome.warnings)

    # Убеждаемся, что во второй (основной) вызов модель получила УЖЕ ПЕРЕВЁРНУТЫЕ ПРЯМО изображения!
    main_user_blocks = calls[1][-1]["content"]
    main_imgs = [b["image_url"]["url"] for b in main_user_blocks if b.get("type") == "image_url"]
    assert len(main_imgs) == 2

    im1_after = PIL_Image.open(io.BytesIO(base64.b64decode(main_imgs[0].split(",", 1)[1])))
    r_top, g_top, b_top = im1_after.getpixel((im1_after.width // 2, 10))
    assert r_top > 180 and g_top < 80 and b_top < 80  # красная шапка теперь СВЕРХУ!

    im2_after = PIL_Image.open(io.BytesIO(base64.b64decode(main_imgs[1].split(",", 1)[1])))
    assert im2_after.height > im2_after.width  # было 800×500 (на боку), стало портретным 500×800!

    # По умолчанию (без явного auto_orient=True) платный предпроход автоповорота НЕ выполняется!
    assert load_config()["api"]["vision_auto_orient"] is False
    calls.clear()
    outcome_default = parse_legal_description_full(
        pdf_path=album_pdf,
        pdf_pages="2, 4",
        use_llm=True,
        provider="routerai",
        model="google/gemini-2.5-pro",
        api_key="rk-test-orient",
        cfg=load_config(),
    )
    assert len(calls) == 1  # ровно 1 основной запрос к LLM без лишнего вызова автоповорота
    assert outcome_default.usage is not None and outcome_default.usage.total_tokens == 1500
    assert not any("Автоповорот ИИ" in w for w in outcome_default.warnings)


# ------------------------- Интеллектуальная сверка с соседями (v0.9.0) ------


def test_neighbor_verification_clean_wall_to_wall_and_polygon_intersection():
    """
    Проверяет:
      * на идеальной тройке участков Aspenwall St (365' + 420' + 475') сверка с обоими
        соседями выдаёт overall_status == 'ok', 0 ошибок, 0 предупреждений и 2 общие межи;
      * точное вычисление площади пересечения 2D-полигонов (в т.ч. невыпуклого L-участка).
    """
    from acad_builder import polygon_intersection_area, verify_with_neighbors
    from llm_parser import ASPENWALL_SAMPLES

    builder = AcadBuilder(load_config())
    dm, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    dl, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    dr, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"])

    pm = builder.plan(dm, scale=16.0, is_neighbor=False)
    pl = builder.plan(dl, scale=16.0, is_neighbor=True)
    pr = builder.plan(dr, scale=16.0, is_neighbor=True)

    rep = verify_with_neighbors(pm, [pl, pr])
    assert rep.overall_status == "ok"
    assert rep.has_issues is False
    assert rep.error_count == 0 and rep.warning_count == 0
    assert rep.total_shared_edges == 2
    assert "✓ норма" in rep.short_badge()
    for pair in rep.pair_reports:
        assert pair.status == "ok"
        assert pair.overlap_area_sqft == 0.0
        assert len(pair.shared_edges) == 1
        ec = pair.shared_edges[0]
        assert ec.angle_diff_arcsec == pytest.approx(0.0, abs=1e-4)
        assert ec.abs_length_diff_ft == pytest.approx(0.0, abs=1e-6)

    # Проверка площади 2D-пересечения невыпуклого Г-образного полигона и прямоугольника
    l_shape = [(0, 0), (60, 0), (60, 20), (20, 20), (20, 60), (0, 60)]
    rect = [(10, 10), (40, 10), (40, 50), (10, 50)]
    # Внутри L-фигуры лежит полоса [10..20]×[10..50] (400) + [20..40]×[10..20] (200) = 600 sq ft
    assert polygon_intersection_area(l_shape, rect) == pytest.approx(600.0, abs=1e-4)


def test_neighbor_verification_intelligent_diagnostics_all_heuristics():
    """
    Комплексный тест интеллектуальных эвристик «Сверки с соседями»:
      1) ошибка OCR в секундах румба (40" -> 48": SECONDS_ONLY_DIFF + «0 ↔ 8»);
      2) опечатка перевёрнутой цифры в минутах (39' -> 36': MINUTES_DIFF + «9 ↔ 6»)
         и в градусах (14° -> 19°: DEGREES_TYPO + «4 ↔ 9»);
      3) перепутана буква квадранта (E -> W: QUADRANT_LETTER_TYPO + виновник 'neighbor');
      4) путаница «дюймы ↔ десятые фута» (100.50' = 100'-6" vs 100.60': FEET_INCHES_CONFUSION)
         и перестановка соседних цифр в длине (105.00' vs 150.00': «05 ↔ 50»);
      5) систематический поворот базиса румбов (+0°18'00" по всем сторонам: DATUM_ROTATION);
      6) параллельная щель Gore (1.50') и параллельный заступ Overlap (150.00 sq ft);
      7) кросс-проверка замкнутости при ошибке в НАШЕМ участке (likely_culprit == 'main').
    """
    from acad_builder import verify_with_neighbors
    from llm_parser import ASPENWALL_SAMPLES

    builder = AcadBuilder(load_config())
    dm, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    dl, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 365' (Сосед слева)"])
    dr, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 475' (Сосед справа)"])
    pm = builder.plan(dm, scale=16.0, is_neighbor=False)

    # 1) Разница только в секундах (40" vs 48" -> OCR 0↔8, без ложной тревоги Overlap)
    dl_sec = dl.model_copy(deep=True)
    s3 = dl_sec.segments[2]
    dl_sec.segments[2] = Segment(
        ns=s3.ns, degrees=s3.degrees, minutes=s3.minutes, seconds=48.0, ew=s3.ew, distance_ft=s3.distance_ft
    )
    rep1 = verify_with_neighbors(pm, [builder.plan(dl_sec, scale=16.0, is_neighbor=True)])
    ec1 = rep1.pair_reports[0].shared_edges[0]
    assert "SECONDS_ONLY_DIFF" in ec1.issue_codes
    assert any("0 ↔ 8" in d for d in ec1.diagnoses)
    assert rep1.pair_reports[0].overlap_area_sqft == 0.0

    # 2) Опечатка в минутах (39' -> 36' = 9↔6) и в градусах (14° -> 19° = 4↔9)
    dl_min = dl.model_copy(deep=True)
    dl_min.segments[2] = Segment(
        ns=s3.ns, degrees=s3.degrees, minutes=36, seconds=s3.seconds, ew=s3.ew, distance_ft=s3.distance_ft
    )
    ec2_m = verify_with_neighbors(pm, [builder.plan(dl_min, scale=16.0, is_neighbor=True)]).pair_reports[0].shared_edges[0]
    assert "MINUTES_DIFF" in ec2_m.issue_codes
    assert any("9 ↔ 6" in d for d in ec2_m.diagnoses)

    dl_deg = dl.model_copy(deep=True)
    dl_deg.segments[2] = Segment(
        ns=s3.ns, degrees=19, minutes=s3.minutes, seconds=s3.seconds, ew=s3.ew, distance_ft=s3.distance_ft
    )
    ec2_d = verify_with_neighbors(pm, [builder.plan(dl_deg, scale=16.0, is_neighbor=True)]).pair_reports[0].shared_edges[0]
    assert "DEGREES_TYPO" in ec2_d.issue_codes
    assert any("4 ↔ 9" in d for d in ec2_d.diagnoses)

    # 3) Зеркальная опечатка буквы квадранта (W вместо E у правого соседа)
    dr_flip = dr.model_copy(deep=True)
    r1 = dr_flip.segments[0]
    dr_flip.segments[0] = Segment(
        ns=r1.ns, degrees=r1.degrees, minutes=r1.minutes, seconds=r1.seconds, ew="W", distance_ft=r1.distance_ft
    )
    ec3 = verify_with_neighbors(pm, [builder.plan(dr_flip, scale=16.0, is_neighbor=True)]).pair_reports[0].shared_edges[0]
    assert "QUADRANT_LETTER_TYPO" in ec3.issue_codes
    assert ec3.likely_culprit == "neighbor"
    assert any("«W» вместо ожидаемого «E»" in d for d in ec3.diagnoses)

    # 4) Путаница «дюймы ↔ десятые фута» (100.50' vs 100.60') и перестановка цифр длины (105 vs 150)
    dm_1005 = dm.model_copy(deep=True)
    dm_1005.segments[0] = Segment(ns="S", degrees=14, minutes=39, seconds=40, ew="E", distance_ft=100.50)
    dm_1005.segments[2] = Segment(ns="N", degrees=14, minutes=39, seconds=40, ew="W", distance_ft=100.50)
    dl_1006 = dl.model_copy(deep=True)
    dl_1006.segments[2] = Segment(ns="N", degrees=14, minutes=39, seconds=40, ew="W", distance_ft=100.60)
    ec4_fi = verify_with_neighbors(
        builder.plan(dm_1005, scale=16.0), [builder.plan(dl_1006, scale=16.0, is_neighbor=True)]
    ).pair_reports[0].shared_edges[0]
    assert "FEET_INCHES_CONFUSION" in ec4_fi.issue_codes
    assert any("100'-6\"" in d for d in ec4_fi.diagnoses)

    dm_105 = dm.model_copy(deep=True)
    dm_105.segments[0] = Segment(ns="S", degrees=14, minutes=39, seconds=40, ew="E", distance_ft=105.0)
    dm_105.segments[2] = Segment(ns="N", degrees=14, minutes=39, seconds=40, ew="W", distance_ft=105.0)
    dl_150 = dl.model_copy(deep=True)
    dl_150.segments[2] = Segment(ns="N", degrees=14, minutes=39, seconds=40, ew="W", distance_ft=150.0)
    ec4_tr = verify_with_neighbors(
        builder.plan(dm_105, scale=16.0), [builder.plan(dl_150, scale=16.0, is_neighbor=True)]
    ).pair_reports[0].shared_edges[0]
    assert "LENGTH_DIGIT_TYPO" in ec4_tr.issue_codes
    assert any("05 ↔ 50" in d for d in ec4_tr.diagnoses)

    # 5) Систематический поворот базиса румбов (+0°18'00" по всем 4 сторонам соседа)
    dr_rot = dr.model_copy(deep=True)
    dr_rot.segments = [
        Segment(ns="S", degrees=14, minutes=21, seconds=40, ew="E", distance_ft=100.0),
        Segment(ns="N", degrees=75, minutes=38, seconds=20, ew="E", distance_ft=55.0),
        Segment(ns="N", degrees=14, minutes=21, seconds=40, ew="W", distance_ft=100.0),
        Segment(ns="S", degrees=75, minutes=38, seconds=20, ew="W", distance_ft=55.0),
    ]
    rep5 = verify_with_neighbors(pm, [builder.plan(dr_rot, scale=16.0, is_neighbor=True)])
    pr5 = rep5.pair_reports[0]
    assert pr5.datum_rotation_arcsec == pytest.approx(1080.0, abs=1.0)
    assert "DATUM_ROTATION" in pr5.shared_edges[0].issue_codes

    # 6) Параллельная щель Gore (363.5' -> 1.50') и параллельный Overlap (366.5' -> 150 sq ft)
    dl_gore = dl.model_copy(deep=True)
    dl_gore.distance_to_corner_ft = 363.5
    dl_gore.corner_to_pob_bearing = Segment(ns="N", degrees=75, minutes=20, seconds=20, ew="E", distance_ft=363.5)
    dl_over = dl.model_copy(deep=True)
    dl_over.distance_to_corner_ft = 366.5
    dl_over.corner_to_pob_bearing = Segment(ns="N", degrees=75, minutes=20, seconds=20, ew="E", distance_ft=366.5)
    rep6 = verify_with_neighbors(
        pm,
        [
            builder.plan(dl_gore, scale=16.0, is_neighbor=True),
            builder.plan(dl_over, scale=16.0, is_neighbor=True),
        ],
    )
    assert rep6.pair_reports[0].overlap_area_sqft == 0.0
    assert "PARALLEL_SHIFT" in rep6.pair_reports[0].shared_edges[0].issue_codes
    assert any("Gore" in d for d in rep6.pair_reports[0].shared_edges[0].diagnoses)
    assert rep6.pair_reports[1].overlap_area_sqft == pytest.approx(150.0, abs=0.1)
    assert any("Overlap" in d for d in rep6.pair_reports[1].shared_edges[0].diagnoses)

    # 7) Кросс-проверка замкнутости при опечатке В НАШЕМ основном участке (103' вместо 100')
    dm_bad = dm.model_copy(deep=True)
    dm_bad.segments[0] = Segment(ns="S", degrees=14, minutes=39, seconds=40, ew="E", distance_ft=103.0)
    rep7 = verify_with_neighbors(
        builder.plan(dm_bad, scale=16.0, is_neighbor=False),
        [builder.plan(dl, scale=16.0, is_neighbor=True)],
    )
    ec7 = rep7.pair_reports[0].shared_edges[0]
    assert ec7.likely_culprit == "main"
    assert any("НАШЕМ описании" in d for d in ec7.diagnoses)


def test_lot_suspicion_clean_presets_and_oblique_parallelogram() -> None:
    """Все штатные пресеты (прямоугольники, прямоугольная трапеция Borden Ave) и честный параллелограмм — без ложных тревог."""
    from llm_parser import ASPENWALL_SAMPLES, NYC_SAMPLES, Segment, load_config, parse_with_regex
    from acad_builder import AcadBuilder

    builder = AcadBuilder(load_config())
    for name, txt in {**NYC_SAMPLES, **ASPENWALL_SAMPLES}.items():
        desc, _ = parse_with_regex(txt)
        plan = builder.plan(desc, scale=16.0)
        assert plan.suspicion is not None, name
        assert plan.suspicion.is_suspicious is False, f"{name}: {plan.suspicion.format_report()}"
        assert plan.suspicion.status == "ok"

    # Честный косой параллелограмм (углы 70° и 110°, далеко от 90° — не должен считаться «почти прямоугольником»)
    dm, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    d_para = dm.model_copy(deep=True)
    d_para.distance_to_corner_ft = 0.0
    d_para.corner_to_pob_bearing = None
    d_para.segments = [
        Segment(ns="N", degrees=20, minutes=0, seconds=0, ew="E", distance_ft=120.0),
        Segment(ns="S", degrees=90, minutes=0, seconds=0, ew="E", distance_ft=60.0),
        Segment(ns="S", degrees=20, minutes=0, seconds=0, ew="W", distance_ft=120.0),
        Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="W", distance_ft=60.0),
    ]
    p_para = builder.plan(d_para, scale=16.0)
    assert p_para.suspicion is not None
    assert p_para.suspicion.shape_kind == "параллелограмм"
    assert p_para.suspicion.is_suspicious is False


def test_lot_suspicion_almost_rectangle_and_euclid_laws() -> None:
    """Ловит скошенный в 0.000' «почти прямоугольник», нарушение теорем Евклида и Пифагора у трапеции."""
    from llm_parser import ASPENWALL_SAMPLES, Segment, load_config, parse_with_regex
    from acad_builder import AcadBuilder

    builder = AcadBuilder(load_config())
    dm, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])

    # 1) Скошенный «почти прямоугольник», замкнутый в 0.000' (ошибка заёма 1' при вычитании из 90°)
    d_borrow = dm.model_copy(deep=True)
    d_borrow.segments = [
        Segment(ns="S", degrees=14, minutes=40, seconds=40, ew="E", distance_ft=100.0),
        Segment(ns="N", degrees=75, minutes=20, seconds=20, ew="E", distance_ft=55.0),
        Segment(ns="N", degrees=14, minutes=40, seconds=40, ew="W", distance_ft=100.0),
        Segment(ns="S", degrees=75, minutes=20, seconds=20, ew="W", distance_ft=55.0),
    ]
    p_borrow = builder.plan(d_borrow, scale=16.0)
    assert p_borrow.closure.distance < 1e-6  # невязка нулевая, но участок кривой!
    assert p_borrow.suspicion is not None and p_borrow.suspicion.is_suspicious is True
    codes_b = {it.code for it in p_borrow.suspicion.items}
    assert "ALMOST_RECTANGLE_SKEW" in codes_b
    assert "заёма 1 минуты" in p_borrow.suspicion.format_report()

    # 2) Прямая теорема Евклида: стороны попарно равны (100/100 и 55/55), но #1 и #3 не параллельны на 25"
    d_euc1 = dm.model_copy(deep=True)
    d_euc1.segments[2] = Segment(ns="N", degrees=14, minutes=39, seconds=15, ew="W", distance_ft=100.0)
    p_euc1 = builder.plan(d_euc1, scale=16.0)
    assert p_euc1.suspicion is not None and p_euc1.suspicion.is_suspicious is True
    codes_e1 = {it.code for it in p_euc1.suspicion.items}
    assert "EUCLID_EQUAL_SIDES_NOT_PARALLEL" in codes_e1
    assert "секундах" in p_euc1.suspicion.format_report()

    # 3) Обратная теорема Евклида: углы все ровно 90°, но парные стороны 100.00' против 100.05' (100' 6")
    d_euc2 = dm.model_copy(deep=True)
    d_euc2.segments[0] = Segment(ns="S", degrees=14, minutes=39, seconds=40, ew="E", distance_ft=100.5)
    d_euc2.segments[2] = Segment(ns="N", degrees=14, minutes=39, seconds=40, ew="W", distance_ft=100.05)
    p_euc2 = builder.plan(d_euc2, scale=16.0)
    assert p_euc2.suspicion is not None and p_euc2.suspicion.is_suspicious is True
    codes_e2 = {it.code for it in p_euc2.suspicion.items}
    assert "EUCLID_PARALLEL_SIDES_UNEQUAL_LENGTH" in codes_e2
    assert "дюймы" in p_euc2.suspicion.format_report()

    # 4) Прямоугольная трапеция с ошибкой в длине косой стороны (нарушение Пифагора):
    # основания 100' и 110' по N/S, перпендикуляр 80' по E, косая 88' вместо √(80²+10²)=80.62'
    d_trap = dm.model_copy(deep=True)
    d_trap.distance_to_corner_ft = 0.0
    d_trap.corner_to_pob_bearing = None
    d_trap.segments = [
        Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=100.0),
        Segment(ns="N", degrees=90, minutes=0, seconds=0, ew="E", distance_ft=80.0),
        Segment(ns="S", degrees=0, minutes=0, seconds=0, ew="W", distance_ft=110.0),
        Segment(ns="N", degrees=82, minutes=52, seconds=30, ew="W", distance_ft=88.0),
    ]
    p_trap = builder.plan(d_trap, scale=16.0)
    assert p_trap.suspicion is not None
    assert "TRAPEZOID_PYTHAGORAS_MISMATCH" in {it.code for it in p_trap.suspicion.items}

    # 5) Непарный 4-угольник с подозрительно «почти параллельными» противоположными сторонами (расхождение 0°12'00")
    d_alm = dm.model_copy(deep=True)
    d_alm.distance_to_corner_ft = 0.0
    d_alm.corner_to_pob_bearing = None
    d_alm.segments = [
        Segment(ns="N", degrees=10, minutes=0, seconds=0, ew="E", distance_ft=100.0),
        Segment(ns="S", degrees=80, minutes=0, seconds=0, ew="E", distance_ft=60.0),
        Segment(ns="S", degrees=10, minutes=12, seconds=0, ew="W", distance_ft=105.0),
        Segment(ns="N", degrees=75, minutes=0, seconds=0, ew="W", distance_ft=61.0),
    ]
    p_alm = builder.plan(d_alm, scale=16.0)
    assert p_alm.suspicion is not None
    assert "ALMOST_PARALLEL_OPPOSITE_SIDES" in {it.code for it in p_alm.suspicion.items}


def test_lot_suspicion_angle_sum_and_primitive_bugs() -> None:
    """Ловит невязку суммы углов -> 360°, потерю десятичной точки, бантик, иглу 180°, дубль стороны и излом POB."""
    from llm_parser import ASPENWALL_SAMPLES, Segment, load_config, parse_with_regex
    from acad_builder import AcadBuilder

    builder = AcadBuilder(load_config())
    dm, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])

    # 1) Явные внутренние углы суммируются в 359°58'30" вместо 360°00'00"
    d_ang = dm.model_copy(deep=True)
    d_ang.segments[0] = Segment(
        ns="S", degrees=14, minutes=39, seconds=40, ew="E", distance_ft=100.0, interior_angle="90°00'00\""
    )
    d_ang.segments[1] = Segment(
        ns="N", degrees=75, minutes=20, seconds=20, ew="E", distance_ft=55.0, interior_angle="89°58'30\""
    )
    d_ang.segments[2] = Segment(
        ns="N", degrees=14, minutes=39, seconds=40, ew="W", distance_ft=100.0, interior_angle="90°00'00\""
    )
    d_ang.segments[3] = Segment(
        ns="S", degrees=75, minutes=20, seconds=20, ew="W", distance_ft=55.0, interior_angle="90°00'00\""
    )
    p_ang = builder.plan(d_ang, scale=16.0)
    assert p_ang.suspicion is not None and p_ang.suspicion.is_suspicious is True
    assert "EXPLICIT_ANGLE_SUM_MISMATCH" in {it.code for it in p_ang.suspicion.items}
    assert "359°58'30\"" in p_ang.suspicion.format_report()

    # 2) Потеря десятичной точки: 10000' вместо 100.00' (нарушение неравенства многоугольника)
    d_dec = dm.model_copy(deep=True)
    d_dec.segments[0] = Segment(ns="S", degrees=14, minutes=39, seconds=40, ew="E", distance_ft=10000.0)
    p_dec = builder.plan(d_dec, scale=16.0)
    assert p_dec.suspicion is not None
    assert "POLYGON_INEQUALITY_VIOLATION" in {it.code for it in p_dec.suspicion.items}
    assert "100.00'" in p_dec.suspicion.format_report()

    # 3) Самопересечение «песочные часы» (диагональный перехлёст несмежных сторон)
    d_cross = dm.model_copy(deep=True)
    d_cross.distance_to_corner_ft = 0.0
    d_cross.corner_to_pob_bearing = None
    d_cross.segments = [
        Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=100.0),
        Segment(ns="S", degrees=45, minutes=0, seconds=0, ew="E", distance_ft=141.42),
        Segment(ns="N", degrees=0, minutes=0, seconds=0, ew="E", distance_ft=100.0),
        Segment(ns="S", degrees=45, minutes=0, seconds=0, ew="W", distance_ft=141.42),
    ]
    p_cross = builder.plan(d_cross, scale=16.0)
    assert p_cross.suspicion is not None
    assert "SELF_INTERSECTING_CONTOUR" in {it.code for it in p_cross.suspicion.items}

    # 4) Разворот на 180° назад («игла») + дубликат подряд идущей стороны + микроизлом ~180° + микросегмент
    d_bugs = dm.model_copy(deep=True)
    d_bugs.segments = [
        Segment(ns="S", degrees=14, minutes=39, seconds=40, ew="E", distance_ft=50.0),
        Segment(ns="S", degrees=14, minutes=39, seconds=40, ew="E", distance_ft=50.0),  # дубликат!
        Segment(ns="N", degrees=75, minutes=20, seconds=20, ew="E", distance_ft=54.8),
        Segment(ns="N", degrees=75, minutes=10, seconds=20, ew="E", distance_ft=0.20),  # микроизлом ~180° + микросегмент
        Segment(ns="N", degrees=14, minutes=39, seconds=40, ew="W", distance_ft=100.0),
        Segment(ns="S", degrees=14, minutes=39, seconds=40, ew="E", distance_ft=10.0),  # разворот 180° назад!
        Segment(ns="S", degrees=75, minutes=20, seconds=20, ew="W", distance_ft=55.0),
    ]
    p_bugs = builder.plan(d_bugs, scale=16.0)
    bug_codes = {it.code for it in p_bugs.suspicion.items}
    assert "DUPLICATE_CONSECUTIVE_SEGMENT" in bug_codes
    assert "ALMOST_STRAIGHT_180_BEND" in bug_codes
    assert "TINY_ARTIFACT_SEGMENT" in bug_codes
    assert "SPIKE_BACKTRACK_180" in bug_codes

    # 5) Излом линии фасадной улицы в точке POB (привязка от угла улицы и возврат по фасаду расходятся на 0°15'00")
    d_kink = dm.model_copy(deep=True)
    d_kink.corner_to_pob_bearing = Segment(
        ns="N", degrees=75, minutes=5, seconds=20, ew="E", distance_ft=420.0
    )
    p_kink = builder.plan(d_kink, scale=16.0)
    assert p_kink.suspicion is not None
    assert "POB_STREET_LINE_KINK" in {it.code for it in p_kink.suspicion.items}


def test_unanimous_multipass_consensus_never_mutates_agreed_54_to_56(monkeypatch) -> None:
    """
    Защита от подмены согласованного мнения моделей:
    если ОБЕ независимые LLM прочитали в документе «54.00'» (при противоположной 56.00' или 55.00'),
    система НИКОГДА не подменяет 54 -> 56/55 ни при ask_on_doubt=True, ни при ask_on_doubt=False,
    а формирует TypoSuspicion и позволяет запустить раунд с инъекцией промпта на зону внимания.
    """
    import llm_parser
    from llm_parser import (
        ASPENWALL_SAMPLES,
        PassTarget,
        TokenUsage,
        load_config,
        parse_with_regex,
        recheck_typo_suspicion,
        run_multipass_consensus,
    )

    cfg = load_config()
    cfg["api"]["providers"]["hydra"]["api_key"] = "sk-hydra-test"
    cfg["api"]["providers"]["openrouter"]["api_key"] = "sk-or-test"

    dm, _ = parse_with_regex(ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"])
    # В документе реально напечатано 54.00' на задней меже против 56.00' по фасаду (невязка 2.00')
    d_54 = dm.model_copy(deep=True)
    d_54.segments[1] = d_54.segments[1].model_copy(update={"distance_ft": 54.0})
    d_54.segments[3] = d_54.segments[3].model_copy(update={"distance_ft": 56.0})

    captured_focus: list[str | None] = []

    def _fake_extract(
        text,
        cfg_arg,
        *,
        api_key=None,
        progress=None,
        provider=None,
        model=None,
        key_secret="",
        image_data_urls=None,
        focus_instruction=None,
    ):
        captured_focus.append(focus_instruction)
        out_model = d_54.model_copy(deep=True)
        if focus_instruction:
            out_model.anomaly_note = "Проверено под увеличением: на чертеже в стороне #2 действительно напечатано 54.00'."
        return (
            out_model,
            "structured-outputs",
            TokenUsage(prompt_tokens=200, completion_tokens=80, total_tokens=280, provider=str(provider), model=str(model), available=True),
        )

    monkeypatch.setattr(llm_parser, "_extract_with_llm", _fake_extract)

    targets = [
        PassTarget(pass_index=1, provider="hydra", model="glm-5.2", api_key="sk-hydra-test"),
        PassTarget(pass_index=2, provider="openrouter", model="nvidia/nemotron-3.5-super-vl:free", api_key="sk-or-test"),
    ]

    # 1) Даже при ask_on_doubt=False согласованное мнение 2/2 LLM (54.00') неприкосновенно!
    cons_free = run_multipass_consensus(
        text=ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"],
        cfg=cfg,
        targets=targets,
        ask_on_doubt=False,
    )
    assert cons_free.agreed is True
    assert cons_free.selected_outcome.data.segments[1].distance_ft == pytest.approx(54.0)
    assert len(cons_free.selected_outcome.typo_suspicions) == 1
    susp = cons_free.selected_outcome.typo_suspicions[0]
    assert susp.unanimous_llm_passes == 2
    assert susp.original_value == "54.00'"
    assert susp.suggested_value == "56.00'"
    assert susp.can_auto_apply(ask_on_doubt=False) is False

    # 2) Даже для визуально похожих цифр (56.00' vs 55.00', пара 6->5) единогласие 2/2 LLM НЕ перезаписывается!
    d_56 = dm.model_copy(deep=True)
    d_56.segments[1] = d_56.segments[1].model_copy(update={"distance_ft": 56.0})
    monkeypatch.setattr(
        llm_parser,
        "_extract_with_llm",
        lambda *a, **kw: (
            d_56.model_copy(deep=True),
            "structured-outputs",
            TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150, provider="hydra", model="glm-5.2", available=True),
        ),
    )
    cons_lookalike = run_multipass_consensus(
        text=ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"],
        cfg=cfg,
        targets=targets,
        ask_on_doubt=False,
    )
    assert cons_lookalike.selected_outcome.data.segments[1].distance_ft == pytest.approx(56.0)
    assert len(cons_lookalike.selected_outcome.typo_suspicions) == 1
    assert cons_lookalike.selected_outcome.typo_suspicions[0].can_auto_apply(ask_on_doubt=False) is False

    # 3) Повторный раунд с инъекцией промпта на зону внимания (recheck_typo_suspicion)
    monkeypatch.setattr(llm_parser, "_extract_with_llm", _fake_extract)
    captured_focus.clear()
    recheck_res = recheck_typo_suspicion(
        cons_free.selected_outcome,
        cfg,
        targets=targets,
        round_number=2,
    )
    assert len(captured_focus) == 2
    assert all(
        f is not None
        and "TARGETED ATTENTION-ZONE VERIFICATION (ROUND #2)" in f
        and "SIDE #2" in f
        and "54.00'" in f
        and "DO NOT GUESS OR FORCE CLOSURE" in f
        for f in captured_focus
    )
    assert recheck_res.selected_outcome.data.segments[1].distance_ft == pytest.approx(54.0)
    assert "действительно напечатано 54.00'" in (recheck_res.selected_outcome.data.anomaly_note or "")


def test_ask_on_doubt_policy_sane_autonomy_vs_strict_human_confirmation() -> None:
    """
    Проверяет матрицу «Спрашивать при сомнении»:
      * по умолчанию в config.json ask_on_doubt == True;
      * непохожие цифры (54 -> 56, т.е. 4->6 в длине И в DMS-угле) НЕ меняются даже при ask_on_doubt=False;
      * похожие цифры (56 -> 55, т.е. 6->5) в 1-проходном/regex режиме:
        - при ask_on_doubt=True остаются как в документе (56.00') и выносятся в typo_suspicions;
        - при ask_on_doubt=False аккуратно исправляются в 55.00' с русским пояснением.
    """
    from llm_parser import ASPENWALL_SAMPLES, DEFAULT_CONFIG, load_config, parse_legal_description_full

    assert DEFAULT_CONFIG["api"]["ask_on_doubt"] is True
    assert load_config()["api"]["ask_on_doubt"] is True

    base_56 = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"].replace(
        "55.00 feet", "56.00 feet"
    )
    # 54.00' против 56.00' (4->6 — НЕ похожие цифры!)
    deed_len_54 = base_56.replace('N 75°20\'20" E, 56.00 feet', 'N 75°20\'20" E, 54.00 feet')
    out_54_free = parse_legal_description_full(text=deed_len_54, use_llm=False, ask_on_doubt=False)
    assert out_54_free.data.segments[1].distance_ft == pytest.approx(54.0)
    assert len(out_54_free.typo_suspicions) == 1
    assert out_54_free.typo_suspicions[0].is_lookalike is False

    # 54" против 56" в секундах угла (4->6 — НЕ похожие цифры!)
    base_sec56 = (
        ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"]
        .replace("14°39'40\"", "14°39'56\"")
        .replace("75°20'20\"", "75°20'04\"")
    )
    deed_ang_54 = base_sec56.replace('N 14°39\'56" W', 'N 14°39\'54" W')
    out_ang54_free = parse_legal_description_full(text=deed_ang_54, use_llm=False, ask_on_doubt=False)
    assert out_ang54_free.data.segments[2].seconds == pytest.approx(54.0)

    # Похожая цифра 6->5 (56.00' вместо 55.00'):
    deed_len_56 = ASPENWALL_SAMPLES["Aspenwall St — 420' (Основной участок)"].replace(
        'N 75°20\'20" E, 55.00 feet', 'N 75°20\'20" E, 56.00 feet'
    )
    out_ask = parse_legal_description_full(text=deed_len_56, use_llm=False, ask_on_doubt=True)
    assert out_ask.data.segments[1].distance_ft == pytest.approx(56.0)
    assert len(out_ask.typo_suspicions) == 1
    assert out_ask.typo_suspicions[0].repaired_data is not None
    assert out_ask.typo_suspicions[0].repaired_data.segments[1].distance_ft == pytest.approx(55.0)

    out_auto = parse_legal_description_full(text=deed_len_56, use_llm=False, ask_on_doubt=False)
    assert out_auto.data.segments[1].distance_ft == pytest.approx(55.0)
    assert out_auto.typo_suspicions == []










