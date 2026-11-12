"""
llm_parser.py
=============
Разбор текста Legal Description (Title Survey, США) в строгую Pydantic-модель.

Источники текста:
  * обычная строка (вставка из буфера / OCR);
  * PDF-файл (PyMuPDF, только текстовый слой — без OCR).

Основной путь — LLM через OpenAI-совместимый API и Structured Outputs
(`client.beta.chat.completions.parse`). Если провайдер/модель не поддерживает
`parse`, автоматически пробуются:
  1) `response_format={"type": "json_schema", ...}`;
  2) `response_format={"type": "json_object"}` + валидация Pydantic.

Дополнительно есть полностью локальный детерминированный regex-парсер
(`parse_with_regex`) для работы без сети: он разбирает типовые формулировки
deed'ов (румбы DMS/десятичные, длины в футах, фасадная и пересекаемая улицы,
расстояние до угла). GUI включает его осознанно (выключатель «Использовать LLM»),
а не подставляет молча вместо ответа модели: ошибка LLM должна быть видна.

Автор: Senior Python / AutoCAD Automation
Python: 3.11+
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ConfigError",
    "LegalDescriptionError",
    "LLMParseError",
    "PDFError",
    "CONFIG_PATH",
    "DEFAULT_CONFIG",
    "LegalDescription",
    "ParseOutcome",
    "Segment",
    "bearing_label",
    "dms_to_degrees",
    "load_config",
    "make_strict_schema",
    "parse_legal_description",
    "parse_legal_description_full",
    "parse_with_regex",
    "read_pdf_text",
    "save_config",
]


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------


class LegalDescriptionError(RuntimeError):
    """Базовая ошибка разбора Legal Description."""


class LLMParseError(LegalDescriptionError):
    """LLM недоступна или вернула некорректный ответ."""


class PDFError(LegalDescriptionError):
    """PDF не читается / в нём нет текстового слоя."""


class ConfigError(LegalDescriptionError):
    """config.json повреждён."""


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "api": {
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1",
        "temperature": 0.0,
        "timeout_sec": 180,
        "max_retries": 2,
        "max_input_chars": 60000,
    },
    "drawing": {
        "base_scale": 16.0,
        "base_text_height": 0.9,
        "available_scales": [10.0, 16.0, 20.0, 30.0],
        "text_offset_factor": 1.5,
        "street_offset_factor": 4.0,
        "corner_label_gap_factor": 2.0,
        "corner_distance_offset_factor": 1.7,
        "long_side_threshold_ft": 40.0,
        "char_width_factor": 0.62,
        "space_width_factor": 0.45,
        "max_label_fill_ratio": 0.9,
        "max_stretch_spaces": 240,
        "front_side_index": 0,
        "degrees_as_control_code": True,
        "text_style": "AI_BOUNDARY",
        "zoom_extents": True,
    },
    "layers": {
        "property_line": "PROPERTY_LINE",
        "boundary_text": "BOUNDARY_TEXT_AI",
    },
    "layer_colors": {
        "property_line": 1,
        "boundary_text": 3,
    },
}


def _deep_update(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивно накладывает extra на base (base меняется на месте)."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """
    Читает config.json и дополняет его значениями по умолчанию.

    Пустой `api.api_key` подхватывается из окружения:
    OPENAI_API_KEY или CAD_LLM_API_KEY (удобно не хранить ключ в файле).
    """
    cfg_path = Path(path) if path is not None else CONFIG_PATH
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config.json повреждён ({exc}); путь: {cfg_path}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"config.json должен содержать JSON-объект; путь: {cfg_path}")
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    _deep_update(cfg, raw)

    api = cfg["api"]
    if not str(api.get("api_key") or "").strip():
        api["api_key"] = (
            os.environ.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("CAD_LLM_API_KEY", "").strip()
        )
    return cfg


def save_config(cfg: dict[str, Any], path: str | Path | None = None) -> Path:
    """Сохраняет конфигурацию (используется кнопкой «Сохранить» в GUI)."""
    cfg_path = Path(path) if path is not None else CONFIG_PATH
    cfg_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return cfg_path


# ---------------------------------------------------------------------------
# Геодезические румбы: DMS <-> десятичные градусы, каноничная подпись
# ---------------------------------------------------------------------------


def dms_to_degrees(degrees: float, minutes: float, seconds: float) -> float:
    """75°20'20" -> 75.338888..."""
    return float(degrees) + float(minutes) / 60.0 + float(seconds) / 3600.0


def _carry_dms(degrees: int, minutes: int, seconds: float) -> tuple[int, int, float]:
    """Нормализует DMS: 75°59'59.999" -> 75°59'60" -> 76°00'00"."""
    d, m, s = int(degrees), int(minutes), round(float(seconds), 2)
    if s >= 60.0:
        s -= 60.0
        m += 1
    if m >= 60:
        m -= 60
        d += 1
    if s < 0:  # защита от мусорного ввода
        s = 0.0
    return d, m, s


def _fmt_seconds(seconds: float) -> str:
    text = f"{seconds:.2f}".rstrip("0").rstrip(".")
    return text if text else "0"


def bearing_label(ns: str, degrees: int, minutes: int, seconds: float, ew: str) -> str:
    """
    Каноничная подпись румба: ``N 75°20'20" E``.

    Для ровных минут секунды не печатаются: ``N 45°00' E``.
    """
    degrees = int(degrees)
    minutes = int(minutes)
    if round(float(seconds), 2) == 0.0:
        return f"{ns} {degrees}°{minutes:02d}' {ew}"
    return f"{ns} {degrees}°{minutes:02d}'{_fmt_seconds(seconds)}\" {ew}"


# ---------------------------------------------------------------------------
# Pydantic-модели (Structured Outputs)
# ---------------------------------------------------------------------------


def _coerce_float(value: Any) -> float | None:
    """'1,250.50 ft' -> 1250.5; '' / None -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("ожидалось число, получено bool")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("\u00a0", " ")
    if not text or text in {"-", "—", "n/a", "N/A", "none", "null"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError(f"не удалось прочитать число из {value!r}")
    return float(match.group(0))


def _clean_name(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = text.strip(" ,;:")
    return text or None


class Segment(BaseModel):
    """
    Одна сторона участка («call» в legal description), по порядку обхода.

    Румб задаётся компонентами (ns/degrees/minutes/seconds/ew) и подписью
    `bearing_label`, которая всегда пересчитывается в каноничный вид —
    так подпись гарантированно совпадает с геометрией.
    """

    model_config = ConfigDict(extra="forbid")

    ns: Literal["N", "S"] = Field(description="Первая буква румба: N или S")
    degrees: int = Field(ge=0, le=90, description="Градусы румба, 0..90")
    minutes: int = Field(ge=0, le=59, description="Минуты, 0..59")
    seconds: float = Field(ge=0, le=60, description="Секунды, 0..60")
    ew: Literal["E", "W"] = Field(description="Вторая буква румба: E или W")
    distance_ft: float = Field(gt=0, description="Длина стороны в футах")
    bearing_label: str | None = Field(
        default=None,
        description='Готовая подпись румба, например N 75°20\'20" E',
    )

    @field_validator("ns", "ew", mode="before")
    @classmethod
    def _normalize_quadrant(cls, value: Any) -> str:
        if isinstance(value, str):
            letter = value.strip().upper()[:1]
            if letter in {"N", "S", "E", "W"}:
                return letter
        raise ValueError(f"некорректная буква румба: {value!r}")

    @field_validator("degrees", "minutes", mode="before")
    @classmethod
    def _normalize_int(cls, value: Any) -> int:
        number = _coerce_float(value)
        if number is None:
            raise ValueError("пустое значение в румбе")  # pragma: no cover
        return int(round(number))

    @field_validator("seconds", mode="before")
    @classmethod
    def _normalize_seconds(cls, value: Any) -> float:
        number = _coerce_float(value)
        if number is None:
            return 0.0
        return round(number, 2)

    @field_validator("distance_ft", mode="before")
    @classmethod
    def _normalize_distance(cls, value: Any) -> float:
        number = _coerce_float(value)
        if number is None:
            raise ValueError("не указана длина стороны")
        if number <= 0:
            raise ValueError(f"длина стороны должна быть > 0, получено {number}")
        return number

    @field_validator("bearing_label", mode="before")
    @classmethod
    def _normalize_label(cls, value: Any) -> str | None:
        return _clean_name(value)

    @model_validator(mode="after")
    def _finalize(self) -> "Segment":
        d, m, s = _carry_dms(self.degrees, self.minutes, self.seconds)
        if d > 90:
            raise ValueError("градусы румба не могут превышать 90")
        self.degrees, self.minutes, self.seconds = d, m, s
        self.bearing_label = bearing_label(self.ns, d, m, s, self.ew)
        return self

    # Удобные производные величины -----------------------------------------
    @property
    def azimuth_deg(self) -> float:
        """Азимут от севера по часовой стрелке, градусы."""
        angle = dms_to_degrees(self.degrees, self.minutes, self.seconds)
        if self.ns == "N":
            az = angle if self.ew == "E" else 360.0 - angle
        else:
            az = 180.0 - angle if self.ew == "E" else 180.0 + angle
        return az % 360.0

    @property
    def vector(self) -> tuple[float, float]:
        """(dX, dY) в футах: 0° = +Y (север), 90° = +X (восток)."""
        rad = math.radians(self.azimuth_deg)
        return self.distance_ft * math.sin(rad), self.distance_ft * math.cos(rad)


class LegalDescription(BaseModel):
    """Результат разбора документа целиком."""

    model_config = ConfigDict(extra="forbid")

    main_street: str = Field(
        description="Улица, на которую выходит фасад участка (fronting ...)"
    )
    cross_street: str | None = Field(
        default=None,
        description="Пересекаемая улица, от которой отмеряется расстояние до угла",
    )
    distance_to_corner_ft: float | None = Field(
        default=None,
        description="Расстояние вдоль фасадной улицы до угла/пересечения, футы",
    )
    front_side: Literal["N", "S", "E", "W"] | None = Field(
        default=None,
        description=(
            "Сторона света, в которую смотрит фасадная граница (наружу участка). "
            "Если участок «fronting the South side of Main Street», он лежит южнее "
            "улицы, значит фасад смотрит на север -> N"
        ),
    )
    segments: list[Segment] = Field(
        min_length=1,
        description="Стороны участка по порядку обхода (каждый call = один элемент)",
    )

    @field_validator("main_street", mode="before")
    @classmethod
    def _normalize_main_street(cls, value: Any) -> str:
        return _clean_name(value) or ""

    @field_validator("cross_street", mode="before")
    @classmethod
    def _normalize_cross_street(cls, value: Any) -> str | None:
        return _clean_name(value)

    @field_validator("distance_to_corner_ft", mode="before")
    @classmethod
    def _normalize_corner_distance(cls, value: Any) -> float | None:
        return _coerce_float(value)

    @field_validator("front_side", mode="before")
    @classmethod
    def _normalize_front_side(cls, value: Any) -> str | None:
        if value is None:
            return None
        letter = str(value).strip().upper()[:1]
        return letter if letter in {"N", "S", "E", "W"} else None

    @field_validator("segments", mode="before")
    @classmethod
    def _drop_empty_segments(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
            return value
        cleaned = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, dict) and not any(
                v not in (None, "", 0, 0.0) for v in item.values()
            ):
                continue
            cleaned.append(item)
        return cleaned

    @model_validator(mode="after")
    def _check_geometry(self) -> "LegalDescription":
        if not self.segments:
            raise ValueError("в описании не найдено ни одной стороны участка")
        if len(self.segments) < 3:
            raise ValueError(
                f"для замкнутого участка нужно минимум 3 стороны, найдено {len(self.segments)}"
            )
        return self

    @property
    def perimeter_ft(self) -> float:
        return sum(seg.distance_ft for seg in self.segments)

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)


# ---------------------------------------------------------------------------
# Чтение PDF
# ---------------------------------------------------------------------------


def read_pdf_text(pdf_path: str | Path, max_pages: int = 0) -> str:
    """
    Извлекает текстовый слой PDF через PyMuPDF (fitz).

    max_pages = 0 — читать все страницы. Сканы без OCR дадут пустой текст,
    в этом случае поднимается PDFError с понятным сообщением.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise PDFError(f"файл не найден: {path}")
    if path.suffix.lower() != ".pdf":
        raise PDFError(f"ожидался PDF, получено: {path.name}")

    try:  # PyMuPDF >= 1.24 : современное имя модуля
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - старые версии PyMuPDF
        try:
            import fitz  # type: ignore[no-redef]
        except ImportError as exc:
            raise PDFError(
                "PyMuPDF не установлен. В Windows выполните: pip install pymupdf"
            ) from exc

    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001 - библиотека кидает разные типы
        raise PDFError(f"не удалось открыть PDF: {exc}") from exc

    try:
        pages = range(len(doc)) if not max_pages else range(min(max_pages, len(doc)))
        chunks: list[str] = []
        for index in pages:
            page = doc.load_page(index)
            text = page.get_text("text") or ""
            if text.strip():
                chunks.append(text)
    finally:
        doc.close()

    result = "\n".join(chunks)
    if len(result.strip()) < 20:
        raise PDFError(
            "в PDF нет текстового слоя (вероятно, скан). Нужен OCR "
            "(например, Tesseract) или вставка текста вручную."
        )
    return result


# ---------------------------------------------------------------------------
# Промпт для LLM
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior land-survey technician in the USA.
You convert a metes-and-bounds legal description (title survey) into strict JSON.

RULES
1. Return every boundary call ("THENCE ...") as one element of `segments`,
   in the exact order of traverse from Point of Beginning.
   Do NOT include the closing call "to the Point of Beginning" twice: if the
   last call returns to the POB, keep it (it closes the polygon) but do not repeat it.
2. Bearings are quadrant bearings. Split them into:
   ns ("N" or "S"), degrees (0..90), minutes (0..59), seconds (decimal),
   ew ("E" or "W"), and also provide bearing_label in the canonical form
   N 75°20'20" E (degree sign, two-digit minutes, double-quote seconds).
3. `distance_ft` is the length of that side in decimal feet. Convert
   feet-and-inches ("55' 6\"") to decimal feet (55.5). Never convert feet to metres.
4. `main_street` — the street the lot fronts on ("fronting the North side of
   Main Street", "lying on the South line of ..."). Use the street name only,
   without "the", "North side of", etc.
5. `cross_street` — the intersecting street from which the distance is measured
   ("being 150.00 feet East of the East line of Oak Avenue"). None if absent.
6. `distance_to_corner_ft` — that same distance to the corner/intersection, in feet.
   None if absent.
7. `front_side` — the compass direction the FRONT boundary faces, pointing
   away from the lot. If the lot "fronts on the South side of Main Street" the lot
   lies south of the street, so its front boundary faces NORTH -> "N".
   If no side of the street is mentioned, use null.
8. Curves: if the description contains a curve and gives chord bearing + chord
   distance, use the CHORD as one straight segment. If only an arc is given with
   a delta and radius, compute chord bearing and chord distance and use them;
   note nothing else.
9. If a call is "to a point", "to a stake", "to the POB" etc., those words are
   not part of the distance and must be ignored.
10. Never invent calls, streets or numbers. If something is truly absent, use null
   (or "" for main_street). Do not return prose, markdown or comments.
11. Distances given in varas or chains: convert to feet (1 vara = 2.77778 ft,
    1 chain = 66 ft)."""

USER_PROMPT_TEMPLATE = """LEGAL DESCRIPTION:
\"\"\"
{text}
\"\"\"

Extract the lot geometry exactly as described."""


def _messages(text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT_TEMPLATE.format(text=text)},
    ]


# ---------------------------------------------------------------------------
# Работа с LLM
# ---------------------------------------------------------------------------


def _make_client(api_cfg: dict[str, Any], api_key: str | None = None) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise LLMParseError(
            "пакет openai не установлен. Выполните: pip install openai"
        ) from exc

    key = (api_key or str(api_cfg.get("api_key") or "")).strip()
    if not key:
        raise LLMParseError(
            "не задан API-ключ: впишите его в config.json (api.api_key) "
            "или в поле «API key» в окне программы"
        )
    base_url = str(api_cfg.get("base_url") or "").strip() or None
    return OpenAI(
        api_key=key,
        base_url=base_url,
        timeout=float(api_cfg.get("timeout_sec") or 180),
        max_retries=int(api_cfg.get("max_retries") or 0),
    )


def _call_with_temperature(func: Callable[..., Any], **kwargs: Any) -> Any:
    """Некоторые модели (o-series, gpt-5, reasoning) не принимают temperature."""
    try:
        return func(**kwargs)
    except Exception as exc:  # noqa: BLE001
        message = str(exc).lower()
        if "temperature" in message and "temperature" in kwargs:
            kwargs.pop("temperature")
            return func(**kwargs)
        raise


def _json_from_text(raw: str) -> Any:
    """Достаёт JSON-объект из ответа модели (снимает ```json-обёртки)."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def make_strict_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """
    JSON-схема в формате strict Structured Outputs: additionalProperties=false
    и все свойства в required. Использует нормализатор из openai SDK, если он
    доступен, иначе применяет эквивалентное преобразование вручную.
    """
    try:
        from openai.lib._pydantic import to_strict_json_schema

        return to_strict_json_schema(model_cls)
    except Exception:  # noqa: BLE001 - старые версии SDK
        pass

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            for key in ("$defs", "definitions"):
                if isinstance(node.get(key), dict):
                    node[key] = {name: walk(value) for name, value in node[key].items()}
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
                properties = node.get("properties")
                if isinstance(properties, dict):
                    node["required"] = list(properties)
                    node["properties"] = {
                        name: walk(value) for name, value in properties.items()
                    }
            if isinstance(node.get("items"), dict):
                node["items"] = walk(node["items"])
            if isinstance(node.get("anyOf"), list):
                node["anyOf"] = [walk(value) for value in node["anyOf"]]
            if isinstance(node.get("allOf"), list):
                node["allOf"] = [walk(value) for value in node["allOf"]]
            if node.get("default", "keep") is None:
                node.pop("default", None)
        return node

    return walk(copy.deepcopy(model_cls.model_json_schema()))


def _extract_with_llm(
    text: str,
    cfg: dict[str, Any],
    api_key: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[LegalDescription, str]:
    """Возвращает (модель, название использованного режима Structured Outputs)."""
    api_cfg = cfg["api"]
    model_name = str(api_cfg.get("model") or "gpt-4.1")
    temperature = api_cfg.get("temperature", 0.0)
    client = _make_client(api_cfg, api_key=api_key)
    messages = _messages(text)

    if progress:
        progress(f"Запрос к LLM: {model_name} …")

    errors: list[str] = []

    # 1) Самый надёжный путь: SDK сам строит strict-схему и валидирует ответ.
    try:
        beta_parse = getattr(getattr(client, "beta", None), "chat", None)
        if beta_parse is not None:
            completion = _call_with_temperature(
                client.beta.chat.completions.parse,
                model=model_name,
                messages=messages,
                response_format=LegalDescription,
                temperature=temperature,
            )
            message = completion.choices[0].message
            refusal = getattr(message, "refusal", None)
            if refusal:
                raise LLMParseError(f"модель отказалась отвечать: {refusal}")
            parsed = getattr(message, "parsed", None)
            if parsed is not None:
                return parsed, "structured-outputs"
            raise LLMParseError("модель вернула пустой structured-ответ")
    except LLMParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        errors.append(f"parse(): {exc}")

    # 2) Ручная JSON-схема (совместимые провайдеры: vLLM, OpenRouter, LM Studio…)
    try:
        schema = make_strict_schema(LegalDescription)
        completion = _call_with_temperature(
            client.chat.completions.create,
            model=model_name,
            messages=messages,
            temperature=temperature,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "legal_description",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        content = completion.choices[0].message.content or ""
        return LegalDescription.model_validate(_json_from_text(content)), "json_schema"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"json_schema: {exc}")

    # 3) Последняя попытка: обычный JSON-mode + инструкция со схемой.
    try:
        schema_hint = json.dumps(make_strict_schema(LegalDescription), ensure_ascii=False)
        guide = (
            "Return ONLY a JSON object that validates against this JSON Schema:\n"
            f"{schema_hint}"
        )
        completion = _call_with_temperature(
            client.chat.completions.create,
            model=model_name,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + guide},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(text=text)},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        content = completion.choices[0].message.content or ""
        return LegalDescription.model_validate(_json_from_text(content)), "json_object"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"json_object: {exc}")

    detail = " | ".join(errors[-3:])
    raise LLMParseError(f"не удалось получить корректный JSON от LLM: {detail}")


# ---------------------------------------------------------------------------
# Локальный regex-парсер (аварийный путь без LLM)
# ---------------------------------------------------------------------------

_NS = r"N(?:orth)?\.?"
_EW = r"E(?:ast)?\.?"

_DMS_BEARING_RE = re.compile(
    rf"(?P<ns>(?:{_NS})|(?:S(?:outh)?\.?))\s*,?\s*"
    r"(?P<deg>\d{1,3})\s*(?:°|º|deg(?:rees?)?\.?)\s*"
    r"(?P<min>\d{1,2})\s*(?:'|′|’|min(?:utes?)?\.?)\s*"
    r"(?P<sec>\d{1,2}(?:\.\d+)?)\s*(?:\"|″|”|''|sec(?:onds?)?\.?)\s*"
    rf"(?P<ew>(?:{_EW})|(?:W(?:est)?\.?))(?![A-Za-z])",
    re.IGNORECASE,
)

_DECIMAL_BEARING_RE = re.compile(
    rf"(?P<ns>{_NS}|S(?:outh)?\.?)\s*,?\s*(?P<ddeg>\d{{1,3}}\.\d+)\s*°?\s*"
    rf"(?P<ew>{_EW}|W(?:est)?\.?)(?![A-Za-z])",
    re.IGNORECASE,
)

# Румб без символов: «N 45 03 12 E» (частая ошибка OCR) — только как последняя
# попытка, иначе «N 45 E …» в посторонних фразах даёт ложные срабатывания.
_SPACED_BEARING_RE = re.compile(
    rf"(?P<ns>{_NS}|S(?:outh)?\.?)\s*,?\s*(?P<deg>\d{{1,3}})\s+"
    rf"(?P<min>\d{{1,2}})\s+(?P<sec>\d{{1,2}}(?:\.\d+)?)\s*"
    rf"(?P<ew>{_EW}|W(?:est)?\.?)(?![A-Za-z])",
    re.IGNORECASE,
)

_DISTANCE_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?:'|′|ft\b\.?|feet\b|foot\b|foot|f\.)",
    re.IGNORECASE,
)

_STREET_SUFFIX = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|Way|Court|Ct|"
    r"Place|Pl|Parkway|Pkwy|Highway|Hwy|Circle|Cir|Trail|Trl|Terrace|Ter|Loop|Pike|"
    r"Expressway|Freeway|Alley|Row|Square|Sq|Crossing|Xing|Belt|Turnpike|Tpke|Bend|"
    r"Pass|Path|Run|Cove|Point|Ridge|Route|Rte|Farm[- ]to[- ]Market|Fm|County Road|"
    r"Cr|Street)\.?"
)

_STREET_RE = re.compile(
    rf"(?P<name>(?:[A-Za-z0-9][\w.'’-]*\s+){{0,3}}{_STREET_SUFFIX})(?![A-Za-z])",
    re.IGNORECASE,
)

_STOP_WORDS = {
    "the", "of", "and", "to", "from", "line", "lines", "side", "sides", "being",
    "feet", "foot", "ft", "a", "an", "at", "on", "in", "with", "along", "same",
    "right", "way", "thereof", "said", "east", "west", "north", "south",
    "easterly", "westerly", "northerly", "southerly", "beginning", "point",
    "corner", "intersection", "centerline", "center", "property", "lot", "block",
    "tract", "addition", "survey", "abstract", "county", "state", "city",
}


def _normalize_legal_text(text: str) -> str:
    """Приводит типографику к единому виду (градусы, минуты, секунды)."""
    replacements = {
        "\u00ba": "°", "\u02da": "°", "\u00b0": "°",
        "\u2032": "'", "\u2019": "'", "\u2018": "'", "\u02b9": "'",
        "\u2033": '"', "\u201d": '"', "\u201c": '"', "\u02ba": '"',
        "\u2013": "-", "\u2014": "-", "\u00a0": " ",
        "\r\n": "\n", "\r": "\n",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _parse_quadrant(ns: str, ew: str) -> tuple[str, str]:
    return ns.strip().upper()[:1], ew.strip().upper()[:1]


def _distance_after(chunk: str) -> float | None:
    match = _DISTANCE_RE.search(chunk)
    if not match:
        return None
    return float(match.group("num").replace(",", ""))


def _bearing_matches(text: str) -> list[re.Match[str]]:
    """Сначала румбы с символами °/', затем десятичные, затем «N 45 03 12 E»."""
    for pattern in (_DMS_BEARING_RE, _DECIMAL_BEARING_RE, _SPACED_BEARING_RE):
        matches = list(pattern.finditer(text))
        if matches:
            return matches
    return []


def _segments_from_regex(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    matches = _bearing_matches(text)
    if not matches:
        return [], ["в тексте не найдено ни одного румба (N 45°00'00\" E) — формат не распознан"]

    raw_segments: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunk = text[match.end() : end]
        distance = _distance_after(chunk)
        if distance is None:
            warnings.append(
                f"сторона #{index + 1} ({match.group(0).strip()}): не найдена длина — пропущена"
            )
            continue
        groups = match.groupdict()
        if groups.get("ddeg") is not None:
            deg_float = float(groups["ddeg"])
            degrees = int(deg_float)
            minutes_f = (deg_float - degrees) * 60
            minutes = int(minutes_f)
            seconds = round((minutes_f - minutes) * 60, 2)
        else:
            degrees = int(groups["deg"])
            minutes = int(groups["min"])
            seconds = float(groups["sec"])

        ns, ew = _parse_quadrant(groups["ns"], groups["ew"])
        if ns not in {"N", "S"} or ew not in {"E", "W"}:
            warnings.append(f"сторона #{index + 1}: некорректный румб — пропущена")
            continue
        raw_segments.append(
            {
                "ns": ns,
                "degrees": degrees,
                "minutes": minutes,
                "seconds": seconds,
                "ew": ew,
                "distance_ft": distance,
            }
        )

    return raw_segments, warnings


def _street_mentions(text: str) -> list[tuple[int, str]]:
    """Все упоминания улиц в тексте: [(позиция, «Main Street»), …]."""
    mentions: list[tuple[int, str]] = []
    for match in _STREET_RE.finditer(text):
        name = _clean_street_candidate(match.group("name"))
        if name:
            mentions.append((match.start(), name))
    return mentions


def _title_case_if_lower(text: str) -> str:
    return text.title() if text.islower() else text


_SIZE_WORDS = {
    "north", "south", "east", "west", "old", "new", "big", "little", "upper",
    "lower", "great", "grand",
}
_NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety", "hundred", "first", "second", "third", "fourth", "fifth",
    "sixth", "seventh", "eighth", "ninth", "tenth",
}


def _clean_street_candidate(candidate: str) -> str | None:
    """
    Приводит фразу вроде «the South side of Ranch Road 620» к «Ranch Road 620»:
    срезает служебные слова/направление, сохраняет номер в конце.
    """
    text = re.sub(r"\s+", " ", candidate).strip(" ,;:-.")
    if not text:
        return None
    words = text.split()
    # Срезаем «the North side of», «East line of», «Southerly line of» с начала.
    while words:
        head = words[0].lower()
        if head in {"the", "of", "a", "an"} or head in _DIRECTION_WORDS or head in {
            "side", "line", "lines", "property", "said", "fronting",
        }:
            words.pop(0)
        else:
            break
    # Срезаем хвост из служебных слов («... Street, and being»).
    while words and words[-1].lower().strip(".,") in {
        "and", "being", "thereof", "of", "the", "same", "more", "less",
    }:
        words.pop()

    keep: list[str] = []
    for index, word in enumerate(words):
        cleaned = word.strip(".,;:")
        if not cleaned:
            continue
        keep.append(cleaned)
        if index == len(words) - 1:
            break
        # После названия улицы идёт номер («Ranch Road 620») — забираем его.
        if re.fullmatch(_STREET_SUFFIX, cleaned, re.IGNORECASE):
            rest = [w.strip(".,;:") for w in words[index + 1 :]]
            if rest and re.fullmatch(r"\d{1,4}[A-Za-z]?", rest[0]):
                keep.append(rest[0])
            elif rest and rest[0].lower() in _NUMBER_WORDS:
                keep.append(rest[0])
            break
    if not keep:
        return None
    return _title_case_if_lower(" ".join(keep))


_DIRECTION_WORDS = {
    "north", "south", "east", "west", "northeast", "northwest", "southeast",
    "southwest", "n", "s", "e", "w", "ne", "nw", "se", "sw",
    "northerly", "southerly", "easterly", "westerly",
}

#: «fronting the South side of …», «along the North line of …», «fronts on …»
_FRONT_RE = re.compile(
    r"(?:fronting|fronts?|facing|faces|abuts?|abutting|along)\s+"
    r"(?:(?:the|a|an)\s+)?(?:(?:[A-Za-z]+ly|[A-Za-z]+)\s+)?"
    r"(?:side|line|lines|right[- ]of[- ]way)?\s*"
    r"(?:of|on|to)?\s*"
    r"(?P<block>[^.;:0-9]{0,80}?(?:%s|%s)[^.;:]{0,20})"
    % (_STREET_SUFFIX, r"\b\d{1,4}\b"),
    re.IGNORECASE,
)


def _street_from_block(block: str) -> str | None:
    """
    Достаёт название улицы из фразы. Номер маршрута, стоящий сразу после
    типа улицы («Ranch Road 620», «Highway 71»), приклеивается к названию.
    """
    mention = _STREET_RE.search(block)
    if not mention:
        return _clean_street_candidate(block)
    name = _clean_street_candidate(mention.group("name"))
    if not name:
        return None
    tail = re.match(r"\s*#?\s*(\d{1,4}[A-Za-z]?)\b", block[mention.end() :])
    if tail:
        return f"{name} {tail.group(1)}"
    return name


def _front_direction(prefix: str) -> str | None:
    """«fronts on the South side of» -> 'S' (направление, куда смотрит фасад)."""
    match = re.search(
        r"\b(?:the\s+)?([A-Za-z]+)\s+(?:side|line|lines|right[- ]of[- ]way)\b",
        prefix,
        re.IGNORECASE,
    )
    if not match:
        return None
    word = match.group(1).lower()
    mapping = {
        "north": "S", "south": "N", "east": "W", "west": "E",
        "northerly": "N", "southerly": "S", "easterly": "E", "westerly": "W",
    }
    return mapping.get(word)


def _extract_streets(text: str) -> tuple[str, str | None, float | None, "Literal['N','S','E','W'] | None", list[str]]:
    """Возвращает (main_street, cross_street, distance_to_corner, front_side, warnings)."""
    warnings: list[str] = []
    main_street = ""
    cross_street: str | None = None
    distance_to_corner: float | None = None
    front_side: str | None = None

    # 1. Фасадная улица и сторона света, куда смотрит фасад.
    fronting = _FRONT_RE.search(text)
    if fronting:
        block = fronting.group("block")
        # Вся фраза до названия улицы: «fronts on the South side of …»
        prefix = text[fronting.start() : fronting.start("block")] + block
        front_side = _front_direction(prefix)
        main_street = _street_from_block(block) or ""

    # 2. Общий случай: «... North line of Main Street» / «... side of Main Street».
    if not main_street:
        side_of = re.search(
            r"(?:side|line|lines|right[- ]of[- ]way)\s+(?:of|at)\s+(?:the\s+)?(?P<block>[^.;:]{0,80})",
            text,
            re.IGNORECASE,
        )
        if side_of:
            main_street = _street_from_block(side_of.group("block")) or ""
    if not main_street:
        mentions = _street_mentions(text)
        if mentions:
            main_street = mentions[0][1]

    # 3. Пересекаемая улица: «... feet East of the East line of Oak Avenue».
    cross_patterns = [
        r"(?:feet|ft|')\s+(?:East|West|North|South|Easterly|Westerly|Northerly|Southerly)?\s*"
        r"(?:of|from)\s+the\s+(?:[A-Za-z]+\s+){0,2}?"
        r"(?:line|right[- ]of[- ]way|centerline)\s+of\s+(?P<block>[^.;:]{0,80})",
        r"(?:line|right[- ]of[- ]way)\s+of\s+(?P<block>[^.;:]{0,80})",
    ]
    for pattern in cross_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            candidate = _street_from_block(match.group("block"))
            if candidate and candidate.lower() != main_street.lower():
                cross_street = candidate
                break
        if cross_street:
            break

    # 4. Расстояние до угла: «being 150.00 feet East of the East line of ...».
    distance_match = re.search(
        r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?:feet|ft|')\s+"
        r"(?:East|West|North|South|Easterly|Westerly|Northerly|Southerly)?\s*"
        r"(?:of|from)\s+the\s+",
        text,
        re.IGNORECASE,
    )
    if distance_match:
        distance_to_corner = float(distance_match.group("num").replace(",", ""))

    if not main_street:
        warnings.append("фасадная улица не распознана — подпись улицы не будет нанесена")
    if cross_street and distance_to_corner is None:
        warnings.append("пересекаемая улица найдена, но расстояние до угла не распознано")
    return main_street, cross_street, distance_to_corner, front_side, warnings


def parse_with_regex(text: str) -> tuple[LegalDescription, list[str]]:
    """
    Локальный разбор без LLM. Возвращает (модель, список предупреждений).
    Бросает LegalDescriptionError, если геометрия не распознана.
    """
    normalized = _normalize_legal_text(text)
    raw_segments, warnings = _segments_from_regex(normalized)
    if not raw_segments:
        raise LegalDescriptionError(
            "локальный парсер не нашёл сторон участка: " + ("; ".join(warnings) or "текст не распознан")
        )
    (
        main_street,
        cross_street,
        distance_to_corner,
        front_side,
        street_warnings,
    ) = _extract_streets(normalized)
    warnings.extend(street_warnings)

    try:
        model = LegalDescription.model_validate(
            {
                "main_street": main_street,
                "cross_street": cross_street,
                "distance_to_corner_ft": distance_to_corner,
                "front_side": front_side,
                "segments": raw_segments,
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise LegalDescriptionError(f"локальный парсер вернул некорректные данные: {exc}") from exc
    return model, warnings


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


@dataclass
class ParseOutcome:
    """Результат разбора + метаданные для GUI/логов."""

    data: LegalDescription
    source: str
    warnings: list[str] = field(default_factory=list)


def parse_legal_description_full(
    text: str | None = None,
    pdf_path: str | Path | None = None,
    cfg: dict[str, Any] | None = None,
    *,
    use_llm: bool = True,
    api_key: str | None = None,
    allow_regex_fallback: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ParseOutcome:
    """
    Полный разбор: PDF/текст -> LLM (или regex) -> LegalDescription.

    :param text: текст Legal Description; можно передать вместе с pdf_path —
                 тогда PDF-текст добавляется к переданному.
    :param pdf_path: путь к PDF.
    :param cfg: конфигурация (по умолчанию load_config()).
    :param use_llm: False — сразу локальный regex-парсер.
    :param allow_regex_fallback: при ошибке LLM попробовать локальный regex-парсер
                 (по умолчанию выключено: для юридических описаний лучше явная
                 ошибка, чем геометрия с неверно прочитанными румбами).
    :param progress: callback для сообщений о ходе работы (вызывается из
                     рабочего потока — не трогайте в нём Tkinter).
    """
    from pathlib import Path as _Path

    cfg = copy.deepcopy(cfg) if cfg is not None else load_config()
    warnings: list[str] = []

    pdf_text = ""
    if pdf_path:
        if progress:
            progress(f"Читаю PDF: {_Path(pdf_path).name}")
        pdf_text = read_pdf_text(pdf_path)

    combined = "\n\n".join(part for part in (pdf_text, text or "") if part and part.strip())
    combined = _normalize_legal_text(combined).strip()
    if len(combined) < 20:
        raise LegalDescriptionError("текст Legal Description пуст или слишком короткий")
    max_chars = int(cfg["api"].get("max_input_chars") or 60000)
    if len(combined) > max_chars:
        warnings.append(
            f"текст длиннее {max_chars} символов — обрезан до {max_chars} для запроса"
        )
        combined = combined[:max_chars]

    llm_error: Exception | None = None
    if use_llm:
        try:
            model, mode = _extract_with_llm(
                combined, cfg, api_key=api_key, progress=progress
            )
            if mode != "structured-outputs":
                warnings.append(f"провайдер не поддержал Structured Outputs, режим: {mode}")
            return ParseOutcome(data=model, source="llm", warnings=warnings)
        except Exception as exc:  # noqa: BLE001
            llm_error = exc
            if not allow_regex_fallback:
                raise
            warnings.append(f"LLM недоступна ({exc}); использую локальный парсер")

    if progress:
        progress("Локальный разбор текста (regex)…")
    model, regex_warnings = parse_with_regex(combined)
    warnings.extend(regex_warnings)

    if llm_error is not None:
        warnings.insert(0, "геометрия получена локальным парсером — проверьте размеры вручную")
    return ParseOutcome(
        data=model,
        source="llm" if not llm_error and use_llm else "regex",
        warnings=warnings,
    )


def parse_legal_description(
    text: str | None = None,
    pdf_path: str | Path | None = None,
    cfg: dict[str, Any] | None = None,
    **kwargs: Any,
) -> LegalDescription:
    """Упрощённый вход: возвращает только Pydantic-модель."""
    return parse_legal_description_full(
        text=text, pdf_path=pdf_path, cfg=cfg, **kwargs
    ).data
