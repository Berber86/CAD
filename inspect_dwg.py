#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Полный дамп активного чертежа AutoCAD для разбора «в офлайне».

Скрипт ничего не меняет в чертеже. Он читает ModelSpace (или только выбранные
объекты) и пишет рядом с собой два файла:

  <имя чертежа>.dump.json — все объекты с координатами: линии и полилинии
      (вершины, дуги), тексты и MText (строка, точка вставки, высота, поворот,
      стиль, ширина рамки, привязка), выноски, размеры, блоки с атрибутами,
      штриховки, растры; плюс слои, текстовые стили, системные переменные,
      определения блоков и объекты листов;
  <имя чертежа>.dump.txt  — короткая сводка (она же печатается в консоль).

Запуск (AutoCAD открыт, нужный чертёж активен):

  python inspect_dwg.py                      весь ModelSpace
  python inspect_dwg.py --select             только выбранные объекты: выделите
                                             их в AutoCAD заранее либо укажите
                                             рамкой после запуска и нажмите Enter
  python inspect_dwg.py --select --tag final имя файла: <чертёж>.final.dump.json
  python inspect_dwg.py --out D:\\dumps       папка для файлов
  python inspect_dwg.py --no-blocks --no-layouts   без определений блоков и листов
  python inspect_dwg.py --lean --tag before       для сравнения «до/после» расстановки оффсетов:
  python inspect_dwg.py --lean --tag after        без листов и без слоя рамки BORDER; дамп «до»
                                                  и «после» одного чертежа — объекты сохраняют handle,
                                                  новые подписи = разница (tests/dump_diff.py)
  python inspect_dwg.py --skip-layer BORDER --skip-layer TITLE   пропустить слои по имени (повторяемый)

Пришлите оба файла (.json и .txt).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

FORMAT = "survey-dwg-dump/2"
#: слои, которые в режиме --lean не пишутся: рамка листа (чертёж заказчика, не объекты участка)
LEAN_SKIP_LAYERS = ("BORDER",)
ROUND = 4  # знаков после запятой в координатах
SELECTION_SET = "SURVEY_DUMP_SELECTION"
BLOCK_ENTITY_LIMIT = 3000
LAYOUT_ENTITY_LIMIT = 3000
MAX_BLOCK_DEFINITIONS = 300

# RPC_E_CALL_REJECTED / RPC_E_SERVERCALL_RETRYLATER: AutoCAD занят — повторить.
_BUSY_CODES = {-2147418111, -2147417846}
_RETRIES = 40
_RETRY_DELAY = 0.25

VARIABLES = (
    "ACADVER", "DWGNAME", "DWGPREFIX", "INSUNITS", "MEASUREMENT", "LUNITS", "LUPREC",
    "AUNITS", "AUPREC", "ANGBASE", "ANGDIR", "DIMSCALE", "DIMSTYLE", "LTSCALE",
    "TEXTSIZE", "TEXTSTYLE", "CANNOSCALE", "CANNOSCALEVALUE", "CLAYER", "INSBASE",
    "EXTMIN", "EXTMAX", "UCSNAME", "UCSORG", "UCSXDIR", "UCSYDIR", "TILEMODE",
)
INSUNITS_NAMES = {
    0: "не заданы", 1: "дюймы", 2: "футы", 3: "мили", 4: "миллиметры",
    5: "сантиметры", 6: "метры", 7: "километры", 21: "геодезические футы США",
}


# ----------------------------------------------------------------- COM-обёртки

def com_call(func: Callable[..., Any], *args: Any) -> Any:
    """Вызов COM с повторами, пока AutoCAD отвечает «занят»."""
    last: Exception | None = None
    for _attempt in range(_RETRIES):
        try:
            return func(*args)
        except Exception as exc:  # noqa: BLE001 - pywintypes.com_error и др.
            code = getattr(exc, "hresult", None)
            if code is None and exc.args and isinstance(exc.args[0], int):
                code = exc.args[0]
            if code not in _BUSY_CODES:
                raise
            last = exc
            time.sleep(_RETRY_DELAY)
    assert last is not None
    raise last


def prop(obj: Any, name: str, default: Any = None) -> Any:
    """Свойство COM-объекта либо ``default``, если его нет или оно не читается."""
    try:
        return com_call(getattr, obj, name)
    except Exception:  # noqa: BLE001
        return default


def short_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return (text[:197] + "...") if len(text) > 200 else text


# ------------------------------------------------------------ преобразователи

def num(value: Any, digits: int = ROUND) -> float | int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    rounded = round(number, digits)
    if rounded == int(rounded) and abs(rounded) < 1e15:
        return int(rounded)
    return rounded


def fine(value: Any) -> float | int | None:
    """Число без грубого округления — масштабы видовых экранов (1/192 и т. п.)."""
    return num(value, 8)


def deg(value: Any) -> float | int | None:
    try:
        return num(math.degrees(float(value)), 3)
    except (TypeError, ValueError):
        return None


def point(value: Any) -> list[Any] | None:
    """Точка [x, y] (или [x, y, z], если z не нулевая)."""
    try:
        values = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if len(values) < 2 or not all(math.isfinite(item) for item in values[:3]):
        return None
    result = [num(values[0]), num(values[1])]
    if len(values) > 2 and abs(values[2]) > 1e-9:
        result.append(num(values[2]))
    return result


def points(flat: Any, size: int) -> list[Any] | None:
    try:
        values = [float(item) for item in flat]
    except (TypeError, ValueError):
        return None
    result = [point(values[i:i + size]) for i in range(0, len(values) - size + 1, size)]
    return [item for item in result if item is not None]


def points2(flat: Any) -> list[Any] | None:
    return points(flat, 2)


def points3(flat: Any) -> list[Any] | None:
    return points(flat, 3)


def text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value)
    return result if result != "" else None


def flag(value: Any) -> bool | None:
    return None if value is None else bool(value)


def jsonable(value: Any) -> Any:
    """Значение системной переменной → то, что можно записать в JSON."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        return num(value, 8)
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    return str(value)


_MTEXT_CODE = re.compile(r"\\[A-Za-z][^;\\{}]*;")


def plain_text(raw: Any) -> str:
    """Текст без кодов форматирования MText — для сводки."""
    value = str(raw or "")
    value = value.replace("\r\n", " ").replace("\n", " ")
    value = re.sub(r"\\[Pp]", " ", value)
    value = _MTEXT_CODE.sub("", value)
    value = value.replace("{", "").replace("}", "")
    return " ".join(value.split())


# ------------------------------------------------------------- описание объекта

def bounding_box(obj: Any) -> list[Any] | None:
    """Габарит объекта [minx, miny, maxx, maxy] (``GetBoundingBox``)."""
    try:
        result = com_call(obj.GetBoundingBox)
        low, high = result[0], result[1]
        values = [float(low[0]), float(low[1]), float(high[0]), float(high[1])]
    except Exception:  # noqa: BLE001 - при позднем связывании нужны выходные VARIANT
        try:
            import pythoncom
            import win32com.client

            kind = pythoncom.VT_BYREF | pythoncom.VT_ARRAY | pythoncom.VT_R8
            low_ref = win32com.client.VARIANT(kind, (0.0, 0.0, 0.0))
            high_ref = win32com.client.VARIANT(kind, (0.0, 0.0, 0.0))
            com_call(obj.GetBoundingBox, low_ref, high_ref)
            low, high = low_ref.value, high_ref.value
            values = [float(low[0]), float(low[1]), float(high[0]), float(high[1])]
        except Exception:  # noqa: BLE001 - у объекта нет габарита (Xline, пустой текст)
            return None
    if not all(math.isfinite(item) for item in values):
        return None
    return [num(item) for item in values]


_TEXT_FIELDS = (
    ("text", "TextString", text),
    ("pt", "InsertionPoint", point),
    ("h", "Height", num),
    ("rot_deg", "Rotation", deg),
    ("style", "StyleName", text),
)
_DIMENSION_FIELDS = (
    ("measurement", "Measurement", num),
    ("text_override", "TextOverride", text),
    ("text_pt", "TextPosition", point),
    ("text_rot_deg", "TextRotation", deg),
    ("text_h", "TextHeight", num),
    ("style", "StyleName", text),
    ("dim_scale", "ScaleFactor", num),
    ("arrow_size", "ArrowheadSize", num),
    ("ext1", "ExtLine1Point", point),
    ("ext2", "ExtLine2Point", point),
    ("rot_deg", "Rotation", deg),
)

#: ObjectName → ((ключ в JSON, свойство ActiveX, преобразователь), ...)
FIELDS: dict[str, tuple[tuple[str, str, Callable[[Any], Any]], ...]] = {
    "AcDbLine": (("pts", "StartPoint", point), ("pts", "EndPoint", point)),
    "AcDbPolyline": (
        ("pts", "Coordinates", points2),
        ("closed", "Closed", flag),
        ("elevation", "Elevation", num),
        ("width", "ConstantWidth", num),
    ),
    "AcDb2dPolyline": (("pts", "Coordinates", points3), ("closed", "Closed", flag)),
    "AcDb3dPolyline": (("pts", "Coordinates", points3), ("closed", "Closed", flag)),
    "AcDbMline": (("pts", "Coordinates", points3),),
    "AcDbCircle": (("center", "Center", point), ("radius", "Radius", num)),
    "AcDbArc": (
        ("center", "Center", point),
        ("radius", "Radius", num),
        ("start_deg", "StartAngle", deg),
        ("end_deg", "EndAngle", deg),
        ("start_pt", "StartPoint", point),
        ("end_pt", "EndPoint", point),
    ),
    "AcDbEllipse": (
        ("center", "Center", point),
        ("major_axis", "MajorAxis", point),
        ("ratio", "RadiusRatio", num),
        ("start_deg", "StartAngle", deg),
        ("end_deg", "EndAngle", deg),
    ),
    "AcDbSpline": (
        ("fit_pts", "FitPoints", points3),
        ("control_pts", "ControlPoints", points3),
        ("closed", "Closed", flag),
    ),
    "AcDbText": _TEXT_FIELDS + (
        ("align", "Alignment", num),
        ("align_pt", "TextAlignmentPoint", point),
        ("width_factor", "ScaleFactor", num),
        ("oblique_deg", "ObliqueAngle", deg),
    ),
    "AcDbAttributeDefinition": _TEXT_FIELDS + (
        ("tag", "TagString", text),
        ("prompt", "PromptString", text),
    ),
    "AcDbMText": _TEXT_FIELDS + (
        ("width", "Width", num),
        ("attach", "AttachmentPoint", num),
        ("direction", "DrawingDirection", num),
        ("line_spacing", "LineSpacingFactor", num),
    ),
    "AcDbLeader": (
        ("pts", "Coordinates", points3),
        ("leader_type", "Type", num),
        ("style", "StyleName", text),
        ("arrow", "ArrowheadType", num),
        ("arrow_size", "ArrowheadSize", num),
        ("dim_scale", "ScaleFactor", num),
    ),
    "AcDbMLeader": (
        ("text", "TextString", text),
        ("text_h", "TextHeight", num),
        ("text_rot_deg", "TextRotation", deg),
        ("style", "StyleName", text),
        ("text_style", "TextStyleName", text),
        ("arrow_size", "ArrowheadSize", num),
        ("dogleg", "DoglegLength", num),
        ("landing_gap", "LandingGap", num),
        ("scale", "ScaleFactor", num),
    ),
    "AcDbBlockReference": (
        ("name", "Name", text),
        ("effective_name", "EffectiveName", text),
        ("pt", "InsertionPoint", point),
        ("rot_deg", "Rotation", deg),
        ("sx", "XScaleFactor", num),
        ("sy", "YScaleFactor", num),
        ("sz", "ZScaleFactor", num),
        ("dynamic", "IsDynamicBlock", flag),
    ),
    "AcDbHatch": (
        ("pattern", "PatternName", text),
        ("pattern_scale", "PatternScale", num),
        ("pattern_angle_deg", "PatternAngle", deg),
        ("area", "Area", num),
        ("associative", "AssociativeHatch", flag),
        ("loops", "NumberOfLoops", num),
    ),
    "AcDbSolid": (("pts", "Coordinates", points3),),
    "AcDbTrace": (("pts", "Coordinates", points3),),
    "AcDbFace": (("pts", "Coordinates", points3),),
    "AcDbPoint": (("pt", "Coordinates", point),),
    "AcDbRasterImage": (
        ("name", "Name", text),
        ("file", "ImageFile", text),
        ("pt", "Origin", point),
        ("width", "ImageWidth", num),
        ("height", "ImageHeight", num),
        ("rot_deg", "Rotation", deg),
        ("scale", "ScaleFactor", num),
        ("shown", "ImageVisibility", flag),
    ),
    "AcDbXline": (("pt", "BasePoint", point), ("direction", "DirectionVector", point)),
    "AcDbRay": (("pt", "BasePoint", point), ("direction", "DirectionVector", point)),
    "AcDbViewport": (
        ("center", "Center", point),
        ("width", "Width", num),
        ("height", "Height", num),
        ("custom_scale", "CustomScale", fine),
        ("standard_scale", "StandardScale", num),
        ("twist_deg", "TwistAngle", deg),
        ("target", "Target", point),
        ("view_on", "ViewportOn", flag),
        ("locked", "DisplayLocked", flag),
    ),
    "AcDbTable": (
        ("pt", "InsertionPoint", point),
        ("rows", "Rows", num),
        ("columns", "Columns", num),
        ("width", "Width", num),
        ("height", "Height", num),
    ),
}


def _polyline_bulges(obj: Any, record: dict[str, Any]) -> None:
    """Выпуклости сегментов (дуги) — только если они есть."""
    count = len(record.get("pts") or [])
    if not count:
        return
    bulges = []
    for index in range(count):
        try:
            bulges.append(num(com_call(obj.GetBulge, index), 6) or 0)
        except Exception:  # noqa: BLE001
            return
    if any(bulges):
        record["bulges"] = bulges


def _leader_annotation(obj: Any, record: dict[str, Any]) -> None:
    annotation = prop(obj, "Annotation")
    if annotation is None:
        return
    handle = prop(annotation, "Handle")
    if handle:
        record["annotation"] = str(handle)


def _mleader_lines(obj: Any, record: dict[str, Any]) -> None:
    lines = []
    for index in range(8):
        try:
            flat = com_call(obj.GetLeaderLineVertices, index)
        except Exception:  # noqa: BLE001 - линий выноски больше нет
            break
        path = points3(flat)
        if path:
            lines.append(path)
    if lines:
        record["leader_lines"] = lines


def _block_attributes(obj: Any, record: dict[str, Any]) -> None:
    if not prop(obj, "HasAttributes"):
        return
    attributes = []
    for attribute in com_call(obj.GetAttributes) or ():
        item = {"tag": text(prop(attribute, "TagString")), "text": text(prop(attribute, "TextString"))}
        where = point(prop(attribute, "InsertionPoint"))
        if where:
            item["pt"] = where
        height = num(prop(attribute, "Height"))
        if height:
            item["h"] = height
        attributes.append({key: value for key, value in item.items() if value is not None})
    if attributes:
        record["attributes"] = attributes


SPECIAL: dict[str, Callable[[Any, dict[str, Any]], None]] = {
    "AcDbPolyline": _polyline_bulges,
    "AcDb2dPolyline": _polyline_bulges,
    "AcDbLeader": _leader_annotation,
    "AcDbMLeader": _mleader_lines,
    "AcDbBlockReference": _block_attributes,
}


def describe_entity(obj: Any, index: int | None = None) -> dict[str, Any]:
    """Одна запись дампа: тип, слой, геометрия и свойства объекта."""
    kind = str(com_call(getattr, obj, "ObjectName"))
    record: dict[str, Any] = {}
    if index is not None:
        record["i"] = index
    record["type"] = kind[4:] if kind.startswith("AcDb") else kind
    record["layer"] = str(com_call(getattr, obj, "Layer"))
    handle = prop(obj, "Handle")
    if handle:
        record["handle"] = str(handle)
    color = num(prop(obj, "color"))
    if color is not None and color != 256:  # 256 = ByLayer
        record["color"] = color
    linetype = text(prop(obj, "Linetype"))
    if linetype and linetype.upper() != "BYLAYER":
        record["ltype"] = linetype
    if prop(obj, "Visible", True) is False:
        record["visible"] = False

    fields = FIELDS.get(kind)
    if fields is None and kind.endswith("Dimension"):
        fields = _DIMENSION_FIELDS
    for key, attribute, convert in fields or ():
        raw = prop(obj, attribute)
        if raw is None:
            continue
        try:
            value = convert(raw)
        except Exception:  # noqa: BLE001
            continue
        if value is None:
            continue
        if kind == "AcDbLine" and key == "pts":
            record.setdefault("pts", []).append(value)
        else:
            record[key] = value
    special = SPECIAL.get(kind)
    if special is not None:
        try:
            special(obj, record)
        except Exception as exc:  # noqa: BLE001
            record["warn"] = short_error(exc)
    bbox = bounding_box(obj)
    if bbox is not None:
        record["bbox"] = bbox
    return record


# ---------------------------------------------------------------- сбор данных

def collect_entities(
    container: Any,
    errors: list[dict[str, Any]],
    *,
    label: str,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Все объекты контейнера (ModelSpace, блок, набор выбора) → записи дампа."""
    count = int(com_call(getattr, container, "Count"))
    records: list[dict[str, Any]] = []
    for index in range(count):
        if limit is not None and len(records) >= limit:
            errors.append({"where": label, "error": f"прочитаны первые {limit} из {count} объектов"})
            break
        try:
            records.append(describe_entity(com_call(container.Item, index), index))
        except Exception as exc:  # noqa: BLE001 - объект пропущен, но учтён
            errors.append({"where": label, "i": index, "error": short_error(exc)})
        if progress is not None and (index + 1) % 250 == 0:
            progress(index + 1, count)
    return records, count


def collect_table(table: Any, describe: Callable[[Any], dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    try:
        count = int(com_call(getattr, table, "Count"))
    except Exception:  # noqa: BLE001
        return result
    for index in range(count):
        try:
            item = describe(com_call(table.Item, index))
        except Exception:  # noqa: BLE001
            continue
        result.append({key: value for key, value in item.items() if value is not None})
    return result


def describe_layer(layer: Any) -> dict[str, Any]:
    return {
        "name": text(prop(layer, "Name")),
        "on": flag(prop(layer, "LayerOn")),
        "frozen": flag(prop(layer, "Freeze")),
        "locked": flag(prop(layer, "Lock")),
        "color": num(prop(layer, "color")),
        "ltype": text(prop(layer, "Linetype")),
        "plot": flag(prop(layer, "Plottable")),
    }


def describe_text_style(style: Any) -> dict[str, Any]:
    info = {
        "name": text(prop(style, "Name")),
        "font": text(prop(style, "fontFile")),
        "bigfont": text(prop(style, "BigFontFile")),
        "h": num(prop(style, "Height")),
        "width_factor": num(prop(style, "Width")),
        "oblique_deg": deg(prop(style, "ObliqueAngle") or 0.0),
    }
    try:
        typeface = com_call(style.GetFont)
        if typeface and typeface[0]:
            info["typeface"] = str(typeface[0])
    except Exception:  # noqa: BLE001 - шрифт SHX: гарнитуры нет
        pass
    return info


def collect_variables(doc: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in VARIABLES:
        try:
            result[name] = jsonable(com_call(doc.GetVariable, name))
        except Exception:  # noqa: BLE001 - переменной нет в этой версии AutoCAD
            continue
    return result


def _referenced_blocks(records: Iterable[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for record in records:
        if record.get("type") == "BlockReference":
            for key in ("name", "effective_name"):
                if record.get(key):
                    names.add(str(record[key]))
    return names


def collect_blocks(
    doc: Any, records: list[dict[str, Any]], errors: list[dict[str, Any]]
) -> dict[str, Any]:
    """Определения блоков, на которые ссылаются записанные объекты (с вложенными)."""
    result: dict[str, Any] = {}
    try:
        blocks = com_call(getattr, doc, "Blocks")
    except Exception as exc:  # noqa: BLE001
        errors.append({"where": "Blocks", "error": short_error(exc)})
        return result
    pending = sorted(_referenced_blocks(records))
    while pending and len(result) < MAX_BLOCK_DEFINITIONS:
        name = pending.pop(0)
        if name in result:
            continue
        try:
            block = com_call(blocks.Item, name)
        except Exception as exc:  # noqa: BLE001
            errors.append({"where": f"блок {name}", "error": short_error(exc)})
            result[name] = {"error": short_error(exc)}
            continue
        info: dict[str, Any] = {}
        origin = point(prop(block, "Origin"))
        if origin:
            info["origin"] = origin
        if prop(block, "IsXRef"):
            info["xref"] = True
            path = text(prop(block, "Path"))
            if path:
                info["path"] = path
        entities, count = collect_entities(
            block, errors, label=f"блок {name}", limit=BLOCK_ENTITY_LIMIT
        )
        info["count"] = count
        info["entities"] = entities
        result[name] = info
        for nested in sorted(_referenced_blocks(entities)):
            if nested not in result and nested not in pending:
                pending.append(nested)
    return result


def collect_layouts(doc: Any, errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Листы (пространство листа): рамка, штамп, видовые экраны с масштабом."""
    result: list[dict[str, Any]] = []
    try:
        layouts = com_call(getattr, doc, "Layouts")
        count = int(com_call(getattr, layouts, "Count"))
    except Exception as exc:  # noqa: BLE001
        errors.append({"where": "Layouts", "error": short_error(exc)})
        return result
    for index in range(count):
        try:
            layout = com_call(layouts.Item, index)
            name = str(com_call(getattr, layout, "Name"))
            if prop(layout, "ModelType") or name.strip().upper() == "MODEL":
                continue
            block = com_call(getattr, layout, "Block")
            entities, total = collect_entities(
                block, errors, label=f"лист {name}", limit=LAYOUT_ENTITY_LIMIT
            )
            info: dict[str, Any] = {"name": name, "count": total, "entities": entities}
            media = text(prop(layout, "CanonicalMediaName"))
            if media:
                info["paper"] = media
            result.append(info)
        except Exception as exc:  # noqa: BLE001
            errors.append({"where": f"лист #{index}", "error": short_error(exc)})
    return result


def collect_dump(
    doc: Any,
    *,
    container: Any | None = None,
    scope: str = "all",
    with_blocks: bool = True,
    with_layouts: bool = True,
    skip_layers: Iterable[str] = (),
    stage: str = "",
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Собирает весь дамп в словарь (COM читается только здесь).

    ``skip_layers`` — имена слоёв, объекты которых не пишутся (без учёта регистра); в дампе
    остаётся число пропущенных по каждому слою. ``stage`` — «before»/«after» и т.п., для сравнения.
    """
    errors: list[dict[str, Any]] = []
    model_space = com_call(getattr, doc, "ModelSpace")
    model_count = int(com_call(getattr, model_space, "Count"))
    source = model_space if container is None else container
    entities, _total = collect_entities(
        source, errors, label="ModelSpace" if container is None else "выбор", progress=progress
    )
    skip = {str(name).upper() for name in skip_layers if str(name).strip()}
    skipped_layers: Counter = Counter()
    if skip:
        kept = []
        for record in entities:
            if str(record.get("layer", "")).upper() in skip:
                skipped_layers[record.get("layer", "")] += 1
            else:
                kept.append(record)
        entities = kept
    dump: dict[str, Any] = {
        "format": FORMAT,
        "drawing": {
            "name": text(prop(doc, "Name")) or "",
            "path": text(prop(doc, "FullName")) or "",
            "dumped_at": datetime.now().isoformat(timespec="seconds"),
            "scope": scope,
            "stage": stage,
            "modelspace_count": model_count,
            "dumped_count": len(entities),
            "skipped_layers": dict(sorted(skipped_layers.items())),
        },
        "variables": collect_variables(doc),
        "layers": collect_table(prop(doc, "Layers"), describe_layer),
        "text_styles": collect_table(prop(doc, "TextStyles"), describe_text_style),
        "dim_styles": [
            item["name"]
            for item in collect_table(
                prop(doc, "DimStyles"), lambda style: {"name": text(prop(style, "Name"))}
            )
            if item.get("name")
        ],
        "entities": entities,
        "blocks": collect_blocks(doc, entities, errors) if with_blocks else {},
        "layouts": collect_layouts(doc, errors) if with_layouts else [],
        "errors": errors,
    }
    return dump


# ----------------------------------------------------------------------- сводка

def overall_extent(entities: Iterable[dict[str, Any]]) -> list[float] | None:
    boxes = [record["bbox"] for record in entities if record.get("bbox")]
    if not boxes:
        return None
    return [
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    ]


def find_groups(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Пространственно отдельные группы объектов — «несколько чертежей рядом в
    одном файле». Оценка приблизительная: объекты объединяются, если между их
    габаритами меньше 5 % размера всего чертежа.
    """
    extent = overall_extent(entities)
    if extent is None:
        return []
    width, height = extent[2] - extent[0], extent[3] - extent[1]
    size = max(width, height)
    if size <= 0:
        return []
    gap = 0.05 * size
    total_area = max(width * height, 1e-12)
    items = []
    for record in entities:
        box = record.get("bbox")
        if not box:
            continue
        # Рамки, подложки и бесконечные линии связали бы все группы в одну.
        area = (box[2] - box[0]) * (box[3] - box[1])
        if len(entities) > 50 and area > 0.5 * total_area:
            continue
        items.append((box, record))
    items.sort(key=lambda item: item[0][0])
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for first in range(len(items)):
        box = items[first][0]
        for second in range(first + 1, len(items)):
            other = items[second][0]
            if other[0] > box[2] + gap:
                break
            if other[1] <= box[3] + gap and other[3] >= box[1] - gap:
                root_a, root_b = find(first), find(second)
                if root_a != root_b:
                    parent[root_b] = root_a
    grouped: dict[int, list[tuple[list[float], dict[str, Any]]]] = defaultdict(list)
    for index, item in enumerate(items):
        grouped[find(index)].append(item)
    result = []
    for members in grouped.values():
        layers = Counter(record["layer"] for _box, record in members)
        types = Counter(record["type"] for _box, record in members)
        result.append({
            "count": len(members),
            "bbox": [
                min(box[0] for box, _r in members), min(box[1] for box, _r in members),
                max(box[2] for box, _r in members), max(box[3] for box, _r in members),
            ],
            "layers": layers.most_common(4),
            "types": types.most_common(4),
        })
    result.sort(key=lambda group: -group["count"])
    return result


def _fmt(value: Any) -> str:
    return f"{float(value):,.2f}".replace(",", " ")


def _layer_state(layer: dict[str, Any]) -> str:
    states = []
    if layer.get("on") is False:
        states.append("выключен")
    if layer.get("frozen"):
        states.append("заморожен")
    if layer.get("locked"):
        states.append("заблокирован")
    return f" [{', '.join(states)}]" if states else ""


def build_summary(dump: dict[str, Any]) -> str:
    """Короткая текстовая сводка дампа (печатается в консоль и пишется в .txt)."""
    drawing = dump.get("drawing", {})
    entities = dump.get("entities", [])
    variables = dump.get("variables", {})
    errors = dump.get("errors", [])
    scope = "весь ModelSpace" if drawing.get("scope") == "all" else "только выбранные объекты"
    lines = [
        f"ДАМП ЧЕРТЕЖА: {drawing.get('name', '')}",
        f"Файл: {drawing.get('path', '')}",
        f"Область: {scope} · объектов в ModelSpace: {drawing.get('modelspace_count', 0)} · "
        f"записано: {drawing.get('dumped_count', 0)} · ошибок чтения: {len(errors)}",
    ]
    if drawing.get("stage"):
        lines.append(f"Этап: {drawing['stage']}")
    if drawing.get("skipped_layers"):
        skipped = ", ".join(f"{name} ({count})" for name, count in drawing["skipped_layers"].items())
        lines.append(f"Не записаны слои (по настройке): {skipped}")
    units = variables.get("INSUNITS")
    if units is not None:
        lines.append(
            f"Единицы чертежа: INSUNITS = {units} ({INSUNITS_NAMES.get(units, 'другие')}); "
            f"DIMSCALE = {variables.get('DIMSCALE', '?')}; LTSCALE = {variables.get('LTSCALE', '?')}; "
            f"масштаб аннотаций: {variables.get('CANNOSCALE', '?')}"
        )
    extent = overall_extent(entities)
    if extent is not None:
        lines.append(
            f"Габариты: X {_fmt(extent[0])} … {_fmt(extent[2])}, Y {_fmt(extent[1])} … {_fmt(extent[3])} "
            f"({_fmt(extent[2] - extent[0])} × {_fmt(extent[3] - extent[1])})"
        )

    by_layer: dict[str, Counter] = defaultdict(Counter)
    samples: dict[str, list[str]] = defaultdict(list)
    heights: dict[str, Counter] = defaultdict(Counter)
    style_use: Counter = Counter()
    for record in entities:
        layer = record.get("layer", "")
        by_layer[layer][record.get("type", "?")] += 1
        if record.get("type") in ("Text", "MText", "MLeader"):
            if record.get("style"):
                style_use[str(record["style"]).upper()] += 1
            value = record.get("h") or record.get("text_h")
            if value:
                heights[layer][value] += 1
            if len(samples[layer]) < 3 and record.get("text"):
                samples[layer].append(plain_text(record["text"])[:40])
    layer_info = {str(layer.get("name", "")).upper(): layer for layer in dump.get("layers", [])}

    lines += ["", "=== 1. СЛОИ И ОБЪЕКТЫ НА НИХ ==="]
    for layer in sorted(by_layer):
        types = ", ".join(f"{kind}: {count}" for kind, count in by_layer[layer].most_common())
        line = f"Слой '{layer}'{_layer_state(layer_info.get(layer.upper(), {}))}: {types}"
        if heights[layer]:
            top = ", ".join(f"{value} ×{count}" for value, count in heights[layer].most_common(3))
            line += f" | высота текста: {top}"
        if samples[layer]:
            line += f" | Примеры текста: {samples[layer]}"
        lines.append(line)
    empty = sorted(
        str(layer.get("name", "")) for layer in dump.get("layers", [])
        if str(layer.get("name", "")) not in by_layer
    )
    if empty:
        lines.append(f"Слои без записанных объектов ({len(empty)}): {', '.join(empty)}")

    lines += ["", "=== 2. ТЕКСТОВЫЕ СТИЛИ ==="]
    for style in dump.get("text_styles", []):
        name = str(style.get("name", ""))
        font = style.get("typeface") or style.get("font") or "?"
        lines.append(
            f"  - '{name}': шрифт {font}, высота {style.get('h', 0)}, "
            f"сжатие {style.get('width_factor', 1)} · текстов: {style_use.get(name.upper(), 0)}"
        )

    lines += ["", "=== 3. БЛОКИ (СТРЕЛКА СЕВЕРА, РАМКА) ==="]
    blocks: Counter = Counter()
    for record in entities:
        if record.get("type") == "BlockReference":
            name = record.get("effective_name") or record.get("name") or "?"
            blocks[(name, record.get("layer", ""), record.get("rot_deg", 0))] += 1
    if not blocks:
        lines.append("  (вставок блоков нет)")
    for (name, layer, rotation), count in sorted(blocks.items(), key=lambda item: str(item[0])):
        lines.append(f"  - Блок '{name}' ×{count} | Слой: '{layer}' | Поворот: {rotation}°")

    groups = find_groups(entities)
    lines += ["", "=== 4. ОТДЕЛЬНЫЕ ГРУППЫ ОБЪЕКТОВ (несколько чертежей в одном файле; оценка) ==="]
    if len(groups) <= 1:
        lines.append("  Все объекты лежат одной группой.")
    else:
        small = [group for group in groups if group["count"] < 5]
        for number, group in enumerate([g for g in groups if g["count"] >= 5], start=1):
            box = group["bbox"]
            top_layers = ", ".join(f"{name} ({count})" for name, count in group["layers"])
            lines.append(
                f"  Группа {number}: объектов {group['count']} · "
                f"X {_fmt(box[0])} … {_fmt(box[2])}, Y {_fmt(box[1])} … {_fmt(box[3])} · слои: {top_layers}"
            )
        if small:
            lines.append(
                f"  Мелких групп (меньше 5 объектов): {len(small)}, "
                f"объектов в них: {sum(group['count'] for group in small)}"
            )

    layouts = dump.get("layouts", [])
    if layouts:
        lines += ["", "=== 5. ЛИСТЫ ==="]
        for layout in layouts:
            types = Counter(record.get("type", "?") for record in layout.get("entities", []))
            listing = ", ".join(f"{kind}: {count}" for kind, count in types.most_common())
            scales = sorted({
                record["custom_scale"] for record in layout.get("entities", [])
                if record.get("type") == "Viewport" and record.get("custom_scale")
            })
            scale_note = f" | масштабы видовых экранов: {scales}" if scales else ""
            lines.append(f"  - '{layout.get('name', '')}': {listing or 'пусто'}{scale_note}")

    if errors:
        lines += ["", f"=== ОШИБКИ ЧТЕНИЯ ({len(errors)}) ==="]
        for error in errors[:15]:
            where = error.get("where", "")
            index = f" #{error['i']}" if "i" in error else ""
            lines.append(f"  - {where}{index}: {error.get('error', '')}")
        if len(errors) > 15:
            lines.append(f"  … и ещё {len(errors) - 15} (полный список — в JSON, раздел errors)")
    return "\n".join(lines)


# ------------------------------------------------------------------------ запись

def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _entity_lines(entities: list[dict[str, Any]], indent: str) -> str:
    """Список объектов: по одному объекту в строке — файл удобно читать и сравнивать."""
    if not entities:
        return "[]"
    body = ",\n".join(indent + _dumps(record) for record in entities)
    return "[\n" + body + "\n" + indent[:-1] + "]"


def dump_to_json(dump: dict[str, Any]) -> str:
    parts = []
    for key, value in dump.items():
        if key == "entities":
            rendered = _entity_lines(value, " ")
        elif key == "blocks":
            inner = []
            for name, info in value.items():
                head = {k: v for k, v in info.items() if k != "entities"}
                text_head = _dumps(head)[:-1]
                separator = "," if head else ""
                inner.append(
                    f" {_dumps(name)}:{text_head}{separator}\"entities\":"
                    f"{_entity_lines(info.get('entities', []), '  ')}}}"
                )
            rendered = "{\n" + ",\n".join(inner) + "\n}" if inner else "{}"
        elif key == "layouts":
            inner = []
            for layout in value:
                head = {k: v for k, v in layout.items() if k != "entities"}
                text_head = _dumps(head)[:-1]
                separator = "," if head else ""
                inner.append(
                    f" {text_head}{separator}\"entities\":"
                    f"{_entity_lines(layout.get('entities', []), '  ')}}}"
                )
            rendered = "[\n" + ",\n".join(inner) + "\n]" if inner else "[]"
        elif key in ("layers", "text_styles", "errors"):
            rendered = _entity_lines(value, " ")
        else:
            rendered = _dumps(value)
        parts.append(f"{_dumps(key)}:{rendered}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def output_stem(drawing_name: str, tag: str = "") -> str:
    stem = Path(str(drawing_name or "drawing")).stem or "drawing"
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "drawing"
    tag = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", str(tag or "")).strip("_")
    return f"{stem}.{tag}" if tag else stem


def write_dump(dump: dict[str, Any], folder: Path, tag: str = "") -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    stem = output_stem(dump.get("drawing", {}).get("name", ""), tag)
    json_path = folder / f"{stem}.dump.json"
    text_path = folder / f"{stem}.dump.txt"
    json_path.write_text(dump_to_json(dump), encoding="utf-8")
    text_path.write_text(build_summary(dump) + "\n", encoding="utf-8")
    return json_path, text_path


# ---------------------------------------------------------------------- AutoCAD

def connect_autocad() -> tuple[Any, Any]:
    try:
        import win32com.client
    except ImportError as exc:
        raise SystemExit(
            "Не найден модуль win32com. Установите его: pip install pywin32"
        ) from exc
    try:
        app = win32com.client.GetActiveObject("AutoCAD.Application")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"AutoCAD не запущен или недоступен ({short_error(exc)}). "
            "Откройте чертёж в AutoCAD и запустите скрипт снова."
        ) from exc
    try:
        doc = com_call(getattr, app, "ActiveDocument")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"В AutoCAD нет открытого чертежа или идёт команда ({short_error(exc)}). "
            "Завершите команду (Esc) и запустите скрипт снова."
        ) from exc
    return app, doc


def _bring_to_front(app: Any) -> None:
    try:
        app.Visible = True
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetForegroundWindow(int(app.HWND))
    except Exception:  # noqa: BLE001 - не Windows или окно недоступно
        pass


def pick_selection(app: Any, doc: Any) -> tuple[Any, Callable[[], None]]:
    """
    Набор объектов для дампа: уже выделенные в AutoCAD (ручки на объектах) либо
    выбор на экране. Возвращает набор и функцию, которая убирает его за собой.
    """
    try:
        preselected = com_call(getattr, doc, "PickfirstSelectionSet")
        if int(com_call(getattr, preselected, "Count")) > 0:
            return preselected, lambda: None
    except Exception:  # noqa: BLE001 - предварительного выбора нет
        pass
    sets = com_call(getattr, doc, "SelectionSets")
    try:
        com_call(sets.Item, SELECTION_SET).Delete()
    except Exception:  # noqa: BLE001 - набора с таким именем ещё нет
        pass
    selection = com_call(sets.Add, SELECTION_SET)

    def cleanup() -> None:
        try:
            selection.Delete()
        except Exception:  # noqa: BLE001
            pass

    _bring_to_front(app)
    print("Перейдите в AutoCAD: выберите объекты (рамкой) и нажмите Enter…")
    try:
        selection.SelectOnScreen()  # без повторов: Esc не должен запускать выбор заново
    except Exception as exc:  # noqa: BLE001
        cleanup()
        raise SystemExit(
            f"Выбор отменён или AutoCAD занят другой командой ({short_error(exc)})."
        ) from exc
    return selection, cleanup


def main(argv: list[str] | None = None, connect: Callable[[], tuple[Any, Any]] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Полный дамп активного чертежа AutoCAD (JSON + текстовая сводка)."
    )
    parser.add_argument("--select", action="store_true",
                        help="только выбранные объекты (выделите заранее или укажите после запуска)")
    parser.add_argument("--tag", default="",
                        help="метка в имени файла, например final или field")
    parser.add_argument("--out", default=".", help="папка для файлов (по умолчанию текущая)")
    parser.add_argument("--no-blocks", action="store_true", help="не писать определения блоков")
    parser.add_argument("--no-layouts", action="store_true", help="не писать объекты листов")
    parser.add_argument("--lean", action="store_true",
                        help="для сравнения до/после: без листов и без слоя рамки BORDER")
    parser.add_argument("--skip-layer", action="append", default=[], metavar="СЛОЙ",
                        help="не писать объекты слоя (можно повторять)")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:  # консоль Windows не должна падать на символах вне своей кодировки
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    app, doc = (connect or connect_autocad)()
    name = prop(doc, "Name") or "?"
    container, cleanup, scope = None, (lambda: None), "all"
    if args.select:
        container, cleanup = pick_selection(app, doc)
        scope = "selection"
        chosen = int(com_call(getattr, container, "Count"))
        if chosen == 0:
            cleanup()
            print("Ничего не выбрано — файлы не созданы.")
            return 1
        print(f"Сканирую выбранные объекты чертежа {name}: {chosen}…")
    else:
        total = int(com_call(getattr, com_call(getattr, doc, "ModelSpace"), "Count"))
        print(f"Сканирую чертёж: {name} (объектов в ModelSpace: {total})…")

    def progress(done: int, total: int) -> None:
        print(f"  прочитано {done} из {total}…")

    started = time.time()
    try:
        skip_layers = list(args.skip_layer) + (list(LEAN_SKIP_LAYERS) if args.lean else [])
        dump = collect_dump(
            doc, container=container, scope=scope,
            with_blocks=not args.no_blocks,
            with_layouts=not (args.no_layouts or args.lean),
            skip_layers=skip_layers,
            stage=args.tag,
            progress=progress,
        )
    finally:
        cleanup()
    tag = args.tag or ("selection" if args.select else "")
    json_path, text_path = write_dump(dump, Path(args.out), tag)

    print()
    print(build_summary(dump))
    print()
    print(f"Готово за {time.time() - started:.1f} с.")
    size_kb = json_path.stat().st_size / 1024.0
    print(f"Полный дамп: {json_path.resolve()} ({size_kb:.0f} КБ)")
    print(f"Сводка:      {text_path.resolve()}")
    print("Пришлите оба файла.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
