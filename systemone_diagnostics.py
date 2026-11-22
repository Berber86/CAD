"""Privacy-preserving summaries of Hydra System One validation errors.

Server responses may echo the full request (including credentials, document text
or image data). This module's summary emits only a fixed vocabulary of field
names and categories; the UI separately shows the complete response by explicit
user request, without writing it to disk.
"""
from __future__ import annotations

import io
import re
from typing import Any

import httpx

# Hydra may echo an entire base64 image in validation errors. Our outgoing
# JSON is capped at 40 MiB, so allow a bounded 48 MiB response for diagnosis.
MAX_ERROR_BODY = 48 * 1024 * 1024
MAX_DETAILS = 4

# These are structural names used by our own request, not arbitrary keys from
# documents, filenames, question text, provider responses or user settings.
_FIELD_NAMES = frozenset({
    "body", "model", "state", "questions", "assets", "type", "format",
    "data_base64", "instructions", "criteria", "image", "image_url", "url",
    "headers", "header", "request", "file", "files", "content", "input",
})
_ERROR_TYPES = {
    "missing": "обязательное поле отсутствует",
    "value_error.missing": "обязательное поле отсутствует",
    "extra_forbidden": "лишнее или неподдерживаемое поле",
    "value_error.extra": "лишнее или неподдерживаемое поле",
    "string_type": "ожидалась строка",
    "type_error.str": "ожидалась строка",
    "dict_type": "ожидался объект",
    "type_error.dict": "ожидался объект",
    "list_type": "ожидался список",
    "type_error.list": "ожидался список",
    "int_type": "ожидалось целое число",
    "int_parsing": "ожидалось целое число",
    "literal_error": "значение не входит в допустимый набор",
    "enum": "значение не входит в допустимый набор",
    "string_too_long": "строка превышает допустимую длину",
    "too_long": "превышено допустимое количество элементов",
    "json_invalid": "некорректный JSON",
    "string_pattern_mismatch": "строка не соответствует формату",
    "url_parsing": "некорректный формат URL",
    "greater_than": "значение меньше допустимого",
    "greater_than_equal": "значение меньше допустимого",
    "less_than": "значение превышает допустимое",
    "less_than_equal": "значение превышает допустимое",
    "invalid_image": "сервер указал на недопустимое изображение",
    "unsupported_image": "сервер указал, что изображение не поддерживается",
    "image_not_supported": "сервер указал, что изображение не поддерживается",
    "invalid_image_format": "сервер указал на недопустимый формат изображения",
}


# Common provider codes are displayed only if they match a constant exactly.
_PUBLIC_CODES = frozenset(_ERROR_TYPES) | frozenset({
    "invalid_request_error", "bad_request", "invalid_parameter", "invalid_params",
    "validation_error", "unsupported_model",
})


def _safe_code(item: dict[str, Any]) -> str:
    for key in ("code", "type"):
        value = item.get(key)
        if isinstance(value, str) and value in _PUBLIC_CODES:
            return value
    return ""


def _mentioned_field(item: dict[str, Any]) -> str:
    # Fixed vocabulary only. It is just a mention, not a proven field location.
    for key in ("msg", "message"):
        value = item.get(key)
        if not isinstance(value, str):
            continue
        text = value[:4096].lower()
        for name in sorted(_FIELD_NAMES):
            if re.search(r"(?:field|parameter|argument|param|поле|параметр)\s*[:=]?\s*['\"`]?" + re.escape(name) + r"\b", text):
                return name
    return ""


def _safe_location(value: Any) -> str:
    if isinstance(value, str):
        # Provider "param" paths can be strings. Do not echo unknown segments.
        if len(value) > 1024:
            return ""
        parts: list[Any] = re.split(r"[.\[\]/]+", value)
    elif isinstance(value, (list, tuple)):
        parts = list(value[:12])
    else:
        return ""
    safe = []
    for part in parts[:12]:
        if part == "":
            continue
        if isinstance(part, str) and part in _FIELD_NAMES:
            safe.append(part)
        elif isinstance(part, str) and re.fullmatch(r"(?:q|page_)\d{1,4}", part):
            safe.append(part)
        else:
            # Never show asset IDs, arbitrary field names or array values.
            safe.append("<элемент>")
    # A provider-controlled string made exclusively of unknown items is not
    # useful as a location, and must not masquerade as a valid field name.
    if not safe or not any(item != "<элемент>" for item in safe):
        return ""
    return ".".join(safe)


def _category(item: dict[str, Any]) -> str:
    for key in ("type", "code"):
        value = item.get(key)
        if isinstance(value, str) and value in _ERROR_TYPES:
            return _ERROR_TYPES[value]
    # Some providers return only a free-form message. Classify a small set of
    # common signals, but never copy any part of the provider's message.
    for key in ("msg", "message"):
        text = item.get(key)
        if not isinstance(text, str):
            continue
        text = text[:4096].lower()
        if "field required" in text or "field is required" in text:
            return "обязательное поле отсутствует"
        if any(word in text for word in ("extra inputs are not permitted", "extra fields not permitted", "unknown field", "unexpected field", "unknown parameter", "unsupported parameter", "unexpected parameter")):
            return "лишнее или неподдерживаемое поле"
        if ("danger-zone-images" in text and "header" in text and any(word in text for word in ("required", "missing", "must", "need"))):
            return "в ответе указано требование заголовка для экспериментальных изображений"
        if ("image" in text or "изображен" in text) and any(word in text for word in ("unsupported", "not supported", "does not support", "not accept", "not allowed", "не поддерж")):
            return "в ответе указано, что изображение не поддерживается или не разрешено"
        if ("image" in text or "изображен" in text) and any(word in text for word in ("dimension", "resolution", "размер", "pixel", "width", "height")):
            return "в ответе указана проблема с размером изображения"
        if ("image" in text or "изображен" in text) and any(word in text for word in ("invalid", "format", "decode", "base64", "неверн")):
            return "в ответе указана проблема с форматом или содержимым изображения"
        if "context" in text and any(word in text for word in ("too long", "limit", "exceed", "maximum")):
            return "превышен лимит контекста"
        if "invalid json" in text or "json decode" in text:
            return "некорректный JSON"
    return ""


def summarize_validation_error(response: httpx.Response) -> str:
    """Extract bounded, non-sensitive diagnostics, or explain why none is shown.

    The output never contains raw provider messages or request values. Do not
    infer model support from a validation failure, even if its message refers to
    an image. This function deliberately does not save or print the response.
    """
    try:
        if len(response.content) > MAX_ERROR_BODY:
            return "Подробности ответа слишком велики и скрыты для защиты данных."
        body = response.json()
    except (ValueError, RuntimeError, TypeError):
        return "Безопасные подробности ошибки отсутствуют в ответе сервера."

    found: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 4 or len(found) >= MAX_DETAILS:
            return
        if isinstance(value, list):
            for element in value[:MAX_DETAILS]:
                visit(element, depth + 1)
            return
        if isinstance(value, str):
            visit({"message": value}, depth + 1)
            return
        if not isinstance(value, dict):
            return
        location = ""
        for key in ("loc", "param", "field"):
            location = _safe_location(value.get(key))
            if location:
                break
        category = _category(value)
        code = _safe_code(value)
        mentioned = _mentioned_field(value) if not location else ""
        if location or mentioned or category or code:
            parts = []
            if location:
                parts.append(f"Поле: {location}")
            elif mentioned:
                parts.append(f"В сообщении упомянуто поле: {mentioned}")
            if code:
                parts.append(f"код: {code}")
            if category:
                parts.append(f"категория: {category}")
            detail = "; ".join(parts)
            if detail not in found:
                found.append(detail)
        # Only traverse known error-envelope keys; never inspect input, body,
        # context, documents, request headers, or arbitrary nested fields.
        for key in ("error", "detail", "errors"):
            if key in value:
                visit(value[key], depth + 1)

    visit(body)
    if not found:
        return "Сервер не сообщил распознанных безопасных подробностей; исходный ответ скрыт, так как может содержать данные запроса."
    return "Безопасная диагностика: " + " | ".join(found[:MAX_DETAILS]) + "."


def describe_visual_inputs(images: list[bytes], question_count: int) -> str:
    """Dimensions and byte counts only; no filenames, image content or paths."""
    from PIL import Image
    descriptions = []
    for data in images[:4]:
        try:
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
            descriptions.append(f"{width}×{height} пикс., {len(data)} байт")
        except Exception:  # malformed image: report no raw data or error string
            descriptions.append("размер не определён")
    if len(images) > 4:
        descriptions.append(f"ещё изображений: {len(images) - 4}")
    return (f"Параметры запроса: PNG-изображений {len(images)}, вопросов {question_count}; "
            + "; ".join(descriptions) + ".")
