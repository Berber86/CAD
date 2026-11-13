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

import base64
import copy
import getpass
import hashlib
import hmac
import io
import itertools
import json
import math
import os
import platform
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Sequence

import concurrent.futures
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ASPENWALL_SAMPLES",
    "BoundarySegment",
    "ConfigError",
    "ConsensusResult",
    "DiscrepancyItem",
    "IMAGE_EXTENSIONS",
    "KeyProtectionError",
    "LegalDescription",
    "LegalDescriptionData",
    "LegalDescriptionError",
    "LLMParseError",
    "PDFError",
    "PassTarget",
    "CONFIG_PATH",
    "DEFAULT_CONFIG",
    "ENV_PATH",
    "LOCAL_CONFIG_PATH",
    "NYC_SAMPLES",
    "PROTECTED_KEY_PREFIX",
    "PROVIDER_ORDER",
    "PROVIDERS_CATALOG",
    "ParseOutcome",
    "Segment",
    "TokenUsage",
    "bearing_label",
    "build_discrepancy_focus_prompt",
    "build_pass_targets",
    "clean_provider_key",
    "compare_legal_descriptions",
    "detect_visual_orientations_llm",
    "dms_to_degrees",
    "enhance_archival_scan",
    "infer_corner_to_pob_bearing",
    "inspect_pdf",
    "is_image_file",
    "is_protected_key",
    "load_config",
    "load_dotenv_dict",
    "make_strict_schema",
    "parse_legal_description",
    "parse_legal_description_full",
    "parse_page_range",
    "parse_with_regex",
    "prepare_survey_image_bytes",
    "protect_api_key",
    "read_pdf_text",
    "recheck_consensus_discrepancies",
    "render_image_files_base64",
    "render_pdf_pages_base64",
    "resolve_api_target",
    "run_multipass_consensus",
    "save_config",
    "save_protected_env",
    "unprotect_api_key",
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


class KeyProtectionError(LegalDescriptionError):
    """Защищённый API-ключ не удалось пересчитать (чужой ПК или неверный PIN)."""


# ---------------------------------------------------------------------------
# Конфигурация и каталог провайдеров (Hydra AI / OpenRouter / RouterAI)
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
LOCAL_CONFIG_PATH = Path(__file__).resolve().with_name("config.local.json")
ENV_PATH = Path(__file__).resolve().with_name(".env")
PROTECTED_KEY_PREFIX = "tsai1:"

PROVIDER_ORDER: tuple[str, ...] = ("hydra", "openrouter", "routerai")

PROVIDERS_CATALOG: dict[str, dict[str, Any]] = {
    "hydra": {
        "label": "Hydra AI",
        "base_url": "https://api.hydraai.ru/v1",
        "api_key": "",
        "env_key": "HYDRA_API_KEY",
        "default_model": "glm-5.2",
        "selected_model": "glm-5.2",
        "models": [
            "glm-5.2",
            "hydra-gemini-pro",
            "hydra-gemini-3-pro",
            "hydra-gemini",
        ],
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "nvidia/nemotron-3.5-super-vl:free",
        "selected_model": "nvidia/nemotron-3.5-super-vl:free",
        "models": [
            "nvidia/nemotron-3.5-super-vl:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "nvidia/nemotron-3-nano-30b-a3b:free",
            "stepfun/step-3.5-flash:free",
            "z-ai/glm-4.5-air:free",
            "arcee-ai/trinity-large-preview:free",
            "mistralai/devstral-2512:free",
        ],
    },
    "routerai": {
        "label": "RouterAI",
        "base_url": "https://routerai.ru/api/v1",
        "api_key": "",
        "env_key": "ROUTERAI_API_KEY",
        "default_model": "openai/gpt-4.1",
        "selected_model": "openai/gpt-4.1",
        "models": [
            "openai/gpt-4.1",
            "openai/gpt-4.1-mini",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "google/gemini-2.5-pro",
            "google/gemini-2.5-flash",
            "anthropic/claude-sonnet-4.5",
            "deepseek/deepseek-chat",
        ],
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "api": {
        "provider": "hydra",
        "api_key": "",
        "base_url": "https://api.hydraai.ru/v1",
        "model": "glm-5.2",
        "temperature": 0.0,
        "timeout_sec": 180,
        "max_retries": 2,
        "max_input_chars": 60000,
        "vision_dpi": 220,
        "vision_max_pages": 8,
        "vision_enhance_scans": True,
        "vision_auto_orient": False,
        "providers": copy.deepcopy(PROVIDERS_CATALOG),
        "consensus": {
            "passes": 1,
            "pass2_provider": "openrouter",
            "pass2_model": "nvidia/nemotron-3.5-super-vl:free",
            "pass3_provider": "routerai",
            "pass3_model": "openai/gpt-4.1",
        },
    },
    "drawing": {
        "base_scale": 16.0,
        "base_text_height": 0.9,
        "available_scales": [10.0, 16.0, 20.0, 30.0],
        "text_offset_factor": 1.8,
        "street_offset_factor": 6.5,
        "street_height_factor": 1.9,
        "corner_label_gap_factor": 2.4,
        "corner_distance_offset_factor": 1.9,
        "long_side_threshold_ft": 40.0,
        "stack_short_side_ft": 30.0,
        "stacked_line_gap_factor": 1.9,
        "char_width_factor": 0.70,
        "space_width_factor": 0.75,
        "max_label_fill_ratio": 0.72,
        "max_stretch_spaces": 65,
        "front_side_index": 0,
        "degrees_as_control_code": True,
        "text_style": "AI_BOUNDARY",
        "draw_street_extensions": True,
        "property_as_lines": True,
        "leave_gap_open": True,
        "rounding_gap_tolerance_ft": 0.015,
        "zoom_extents": True,
        "zoom_margin_scale": 0.84,
    },
    "layers": {
        "property_line": "PROPERTY_LINE",
        "boundary_text": "BOUNDARY_TEXT_AI",
        "street_row": "STREET_ROW_AI",
        "neighbor_lot": "NEIGHBOR_LOT",
    },
    "layer_colors": {
        "property_line": 1,
        "boundary_text": 3,
        "street_row": 8,
        "neighbor_lot": 4,
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


def is_protected_key(value: str | None) -> bool:
    """Проверяет, хранится ли ключ в защищённом (пересчитываемом) виде tsai1:…."""
    return str(value or "").strip().startswith(PROTECTED_KEY_PREFIX)


def clean_provider_key(provider: str, raw_key: str | None) -> str:
    """
    Очищает API-ключ провайдера.

    В contextual-chatbot ключ Hydra AI может храниться с суффиксом ``$$``
    (маркер Full Hydra Mode) — срезаем его, чтобы такой ключ работал без правок.
    Защищённые токены ``tsai1:…`` возвращаются без модификации до момента пересчёта.
    """
    key = str(raw_key or "").strip()
    if is_protected_key(key):
        return key
    if provider.strip().lower() == "hydra" and key.endswith("$$"):
        key = key[:-2].strip()
    return key


def _machine_fingerprint() -> bytes:
    """
    Аппаратно-системный отпечаток текущего ПК и учётной записи ОС (32 байта SHA-256).

    Никогда не сохраняется в .json/.env и не попадает в git: украденные файлы
    на компьютере злоумышленника имеют другой MachineGuid/machine-id и не
    проходят проверку целостности HMAC / Windows DPAPI.
    """
    parts: list[str] = []
    if sys.platform == "win32":  # pragma: no cover - Windows registry
        try:
            import winreg

            access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                access,
            ) as reg_key:
                guid, _ = winreg.QueryValueEx(reg_key, "MachineGuid")
                if guid:
                    parts.append(str(guid).strip())
        except Exception:  # noqa: BLE001
            parts.append(os.environ.get("COMPUTERNAME", ""))
        parts.append(os.environ.get("USERNAME", ""))
    else:
        for mid_path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            if mid_path.exists():
                try:
                    mid = mid_path.read_text(encoding="utf-8").strip()
                except OSError:
                    mid = ""
                if mid:
                    parts.append(mid)
                    break
        if not parts:
            parts.append(platform.node())
        try:
            parts.append(getpass.getuser())
        except Exception:  # noqa: BLE001
            parts.append(str(os.getuid()) if hasattr(os, "getuid") else "user")

    parts.append(str(uuid.getnode()))
    raw = "tsai-hw-v1|" + "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _dpapi_protect(data: bytes, entropy: bytes) -> bytes | None:
    """Дополнительная обёртка Windows DPAPI (привязка к учётной записи Windows)."""
    if sys.platform != "win32":
        return None
    try:  # pragma: no cover - Windows Crypt32
        import ctypes
        import ctypes.wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        in_buf = ctypes.create_string_buffer(data, len(data))
        ent_buf = ctypes.create_string_buffer(entropy, len(entropy))
        in_blob = _DataBlob(len(data), in_buf)
        ent_blob = _DataBlob(len(entropy), ent_buf)
        out_blob = _DataBlob()

        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "TitleSurveyAI",
            ctypes.byref(ent_blob),
            None,
            None,
            0x01,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:  # noqa: BLE001
        return None


def _dpapi_unprotect(data: bytes, entropy: bytes) -> bytes:
    """Распаковка Windows DPAPI."""
    if sys.platform != "win32":
        raise KeyProtectionError(
            "ключ защищён через Windows DPAPI и может быть пересчитан только на том же ПК Windows"
        )
    try:  # pragma: no cover - Windows Crypt32
        import ctypes
        import ctypes.wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        in_buf = ctypes.create_string_buffer(data, len(data))
        ent_buf = ctypes.create_string_buffer(entropy, len(entropy))
        in_blob = _DataBlob(len(data), in_buf)
        ent_blob = _DataBlob(len(entropy), ent_buf)
        out_blob = _DataBlob()

        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(ent_blob),
            None,
            None,
            0x01,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(out_blob),
        )
        if not ok:
            raise KeyProtectionError("Windows DPAPI отклонил расшифровку (чужой ПК/профиль или неверный PIN)")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except KeyProtectionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise KeyProtectionError(f"ошибка Windows DPAPI при пересчёте ключа: {exc}") from exc


def _derive_protection_keys(provider: str, salt: bytes, secret: str) -> tuple[bytes, bytes]:
    master = (
        _machine_fingerprint()
        + b"|"
        + provider.strip().lower().encode("utf-8")
        + b"|"
        + str(secret or "").strip().encode("utf-8")
    )
    derived = hashlib.pbkdf2_hmac("sha256", master, salt, 200_000, dklen=64)
    return derived[:32], derived[32:]


def _xor_keystream(data: bytes, enc_key: bytes, salt: bytes) -> bytes:
    out = bytearray(len(data))
    offset = 0
    counter = 0
    while offset < len(data):
        block = hmac.new(
            enc_key, salt + counter.to_bytes(4, "big"), hashlib.sha256
        ).digest()
        chunk_len = min(len(block), len(data) - offset)
        for idx in range(chunk_len):
            out[offset + idx] = data[offset + idx] ^ block[idx]
        offset += chunk_len
        counter += 1
    return bytes(out)


def protect_api_key(
    provider: str,
    raw_key: str | None,
    *,
    secret: str = "",
    unlock_secret: str | None = None,
) -> str:
    """
    Преобразует реальный API-ключ в защищённый хранимый токен ``tsai1:…``.

    В файлах (.json / .env) сохраняется только этот токен: он привязан к
    аппаратному отпечатку ПК, Windows DPAPI (в Windows) и опциональному PIN-коду
    в памяти, поэтому украденные конфиги бесполезны для злоумышленника.
    """
    cleaned = clean_provider_key(provider, raw_key)
    if not cleaned:
        return ""
    if is_protected_key(cleaned):
        # Уже защищённый токен: сначала раскрываем реальный ключ, чтобы не шифровать дважды.
        prev_secret = secret if unlock_secret is None else unlock_secret
        try:
            cleaned = unprotect_api_key(provider, cleaned, secret=secret)
        except KeyProtectionError:
            if prev_secret != secret:
                cleaned = unprotect_api_key(provider, cleaned, secret=prev_secret)
            else:
                raise

    salt = os.urandom(16)
    enc_key, mac_key = _derive_protection_keys(provider, salt, secret)
    ciphertext = _xor_keystream(cleaned.encode("utf-8"), enc_key, salt)
    tag = hmac.new(mac_key, b"tsai1:v1|" + salt + ciphertext, hashlib.sha256).digest()[:16]
    payload = salt + tag + ciphertext

    entropy = hashlib.sha256(
        f"dpapi|{provider.strip().lower()}|{str(secret or '').strip()}".encode("utf-8")
    ).digest()
    dpapi_blob = _dpapi_protect(payload, entropy)
    if dpapi_blob is not None:
        b64 = base64.urlsafe_b64encode(dpapi_blob).decode("ascii").rstrip("=")
        return f"{PROTECTED_KEY_PREFIX}dpapi:{b64}"

    b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{PROTECTED_KEY_PREFIX}v1:{b64}"


def unprotect_api_key(provider: str, stored_key: str | None, *, secret: str = "") -> str:
    """
    Пересчитывает реальный API-ключ в оперативной памяти из хранимого ``tsai1:…``.
    Обычные (незащищённые) строки возвращает после стандартной очистки.
    """
    token = str(stored_key or "").strip()
    if not token:
        return ""
    if not is_protected_key(token):
        return clean_provider_key(provider, token)

    parts = token.split(":", 2)
    if len(parts) != 3 or parts[1] not in {"v1", "dpapi"} or not parts[2]:
        raise KeyProtectionError(f"повреждён формат защищённого ключа ({provider})")

    scheme, b64_data = parts[1], parts[2]
    padded = b64_data + "=" * (-len(b64_data) % 4)
    try:
        raw_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise KeyProtectionError(f"повреждён защищённый ключ ({provider}): {exc}") from exc

    if scheme == "dpapi":
        entropy = hashlib.sha256(
            f"dpapi|{provider.strip().lower()}|{str(secret or '').strip()}".encode("utf-8")
        ).digest()
        payload = _dpapi_unprotect(raw_bytes, entropy)
    else:
        payload = raw_bytes

    if len(payload) <= 32:
        raise KeyProtectionError(f"повреждён защищённый ключ ({provider}): слишком короткий блок")

    salt, tag, ciphertext = payload[:16], payload[16:32], payload[32:]
    enc_key, mac_key = _derive_protection_keys(provider, salt, secret)
    expected_tag = hmac.new(
        mac_key, b"tsai1:v1|" + salt + ciphertext, hashlib.sha256
    ).digest()[:16]
    if not hmac.compare_digest(tag, expected_tag):
        raise KeyProtectionError(
            f"не удалось пересчитать защищённый API-ключ ({provider}): "
            "введите верный «Код пересчёта (PIN)» или вставьте ключ заново "
            "(ключ привязан к конкретному ПК и PIN-коду)"
        )
    try:
        real_key = _xor_keystream(ciphertext, enc_key, salt).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KeyProtectionError(f"ошибка декодирования ключа ({provider})") from exc
    return clean_provider_key(provider, real_key)


def load_dotenv_dict(path: str | Path | None = None) -> dict[str, str]:
    """Читает пары KEY=VALUE из .env (без записи открытых ключей в os.environ)."""
    env_path = Path(path) if path is not None else ENV_PATH
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def save_protected_env(
    keys_by_provider: dict[str, str],
    *,
    secret: str = "",
    unlock_secret: str | None = None,
    path: str | Path | None = None,
) -> Path:
    """
    Сохраняет API-ключи провайдеров в .env ТОЛЬКО в защищённом виде ``tsai1:…``,
    сохраняя прочие строки и комментарии существующего .env.
    """
    env_path = Path(path) if path is not None else ENV_PATH
    env_updates: dict[str, str] = {}
    for pid, raw_val in keys_by_provider.items():
        p_cfg = PROVIDERS_CATALOG.get(pid, {})
        env_name = str(p_cfg.get("env_key") or f"{pid.upper()}_API_KEY").strip()
        val = str(raw_val or "").strip()
        if val:
            env_updates[env_name] = protect_api_key(
                pid, val, secret=secret, unlock_secret=unlock_secret
            )

    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    written: set[str] = set()
    output_lines: list[str] = []
    if not existing_lines:
        output_lines.append(
            "# Защищённые API-ключи Title Survey AI (реальный ключ пересчитывается в RAM)"
        )
    for raw_line in existing_lines:
        stripped = raw_line.strip()
        check = stripped[7:].strip() if stripped.lower().startswith("export ") else stripped
        if check and not check.startswith("#") and "=" in check:
            k_name = check.split("=", 1)[0].strip()
            if k_name in env_updates:
                output_lines.append(f"{k_name}={env_updates[k_name]}")
                written.add(k_name)
                continue
        output_lines.append(raw_line)

    for k_name, protected_val in env_updates.items():
        if k_name not in written:
            output_lines.append(f"{k_name}={protected_val}")

    env_path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")
    return env_path


def _read_json_dict(cfg_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{cfg_path.name} повреждён ({exc}); путь: {cfg_path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path.name} должен содержать JSON-объект; путь: {cfg_path}")
    return raw


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """
    Читает config.json (а при вызове по умолчанию также config.local.json и .env)
    и дополняет значениями по умолчанию.

    Защищённые токены ``tsai1:…`` остаются в зашифрованном виде до вызова
    ``resolve_api_target()``, где пересчитываются в реальный ключ в памяти.
    """
    cfg_path = Path(path) if path is not None else CONFIG_PATH
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if cfg_path.exists():
        _deep_update(cfg, _read_json_dict(cfg_path))
    if path is None and LOCAL_CONFIG_PATH.exists():
        _deep_update(cfg, _read_json_dict(LOCAL_CONFIG_PATH))

    dotenv_vars = load_dotenv_dict() if path is None else {}
    api = cfg["api"]
    providers = api.setdefault("providers", copy.deepcopy(PROVIDERS_CATALOG))
    for pid, p_cfg in providers.items():
        if not isinstance(p_cfg, dict):
            continue
        raw_key = clean_provider_key(pid, p_cfg.get("api_key"))
        env_name = str(p_cfg.get("env_key") or "").strip()
        if not raw_key and env_name:
            raw_key = clean_provider_key(
                pid, os.environ.get(env_name, "") or dotenv_vars.get(env_name, "")
            )
        p_cfg["api_key"] = raw_key

    active_pid = str(api.get("provider") or "hydra").strip().lower()
    active_key = ""
    if isinstance(providers.get(active_pid), dict):
        active_key = str(providers[active_pid].get("api_key") or "").strip()

    if not str(api.get("api_key") or "").strip():
        api["api_key"] = (
            active_key
            or os.environ.get("OPENAI_API_KEY", "").strip()
            or dotenv_vars.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("CAD_LLM_API_KEY", "").strip()
            or dotenv_vars.get("CAD_LLM_API_KEY", "").strip()
        )
    return cfg


def resolve_api_target(
    cfg: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    key_secret: str = "",
) -> dict[str, str]:
    """
    Определяет итоговые параметры вызова LLM и пересчитывает защищённый ключ в RAM.
    """
    api_cfg = cfg.get("api", {})
    providers = api_cfg.get("providers") or {}
    cfg_provider = str(api_cfg.get("provider") or "hydra").strip().lower()
    provider_id = str(provider or cfg_provider or "hydra").strip().lower()
    p_cfg = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(p_cfg, dict):
        p_cfg = {}

    provider_label = str(p_cfg.get("label") or provider_id or "LLM")
    env_key = str(p_cfg.get("env_key") or "").strip()
    dotenv_vars = load_dotenv_dict()

    if provider is not None and provider_id != cfg_provider:
        base_url = str(p_cfg.get("base_url") or api_cfg.get("base_url") or "").strip()
        chosen_model = str(
            model
            or p_cfg.get("selected_model")
            or p_cfg.get("default_model")
            or api_cfg.get("model")
            or "glm-5.2"
        ).strip()
    else:
        base_url = str(p_cfg.get("base_url") or api_cfg.get("base_url") or "").strip()
        chosen_model = str(
            model
            or api_cfg.get("model")
            or p_cfg.get("selected_model")
            or p_cfg.get("default_model")
            or "glm-5.2"
        ).strip()

    raw_key = (
        api_key
        or p_cfg.get("api_key")
        or (os.environ.get(env_key, "") if env_key else "")
        or (dotenv_vars.get(env_key, "") if env_key else "")
        or api_cfg.get("api_key")
        or os.environ.get("OPENAI_API_KEY", "")
        or dotenv_vars.get("OPENAI_API_KEY", "")
        or os.environ.get("CAD_LLM_API_KEY", "")
        or dotenv_vars.get("CAD_LLM_API_KEY", "")
    )
    real_key = unprotect_api_key(provider_id, raw_key, secret=key_secret)
    return {
        "provider": provider_id,
        "provider_label": provider_label,
        "base_url": base_url,
        "model": chosen_model,
        "api_key": real_key,
        "env_key": env_key,
    }


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


_VULGAR_FRACTIONS: dict[str, str] = {
    "½": " 1/2",
    "¼": " 1/4",
    "¾": " 3/4",
    "⅛": " 1/8",
    "⅜": " 3/8",
    "⅝": " 5/8",
    "⅞": " 7/8",
}

_FEET_INCHES_STR_RE = re.compile(
    r"(?P<ft>\d+(?:\.\d+)?)\s*(?:feet|foot|ft\.?|')\s*(?:-\s*|and\s+)?"
    r"(?:"
    r"(?P<in>\d+(?:\.\d+)?)(?:[\s-]+(?P<fnum>\d+)\s*/\s*(?P<fden>\d+))?"
    r"|(?P<pnum>\d+)\s*/\s*(?P<pden>\d+)"
    r")\s*(?:inches|inch|in\.?|\")",
    re.IGNORECASE,
)


def _replace_vulgar_fractions(text: str) -> str:
    for glyph, ascii_frac in _VULGAR_FRACTIONS.items():
        if glyph in text:
            text = text.replace(glyph, ascii_frac)
    return text


def _feet_inches_match_to_ft(fi: re.Match[str]) -> float:
    feet = float(fi.group("ft"))
    inches = float(fi.group("in")) if fi.group("in") is not None else 0.0
    if fi.group("fnum") and fi.group("fden") and float(fi.group("fden")) > 0:
        inches += float(fi.group("fnum")) / float(fi.group("fden"))
    elif fi.group("pnum") and fi.group("pden") and float(fi.group("pden")) > 0:
        inches += float(fi.group("pnum")) / float(fi.group("pden"))
    return feet + inches / 12.0


def _coerce_float(value: Any) -> float | None:
    """'1,250.50 ft' -> 1250.5; '25\\'-6 1/2\"' -> 25.5417; '' / None -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("ожидалось число, получено bool")
    if isinstance(value, (int, float)):
        return float(value)
    text = _replace_vulgar_fractions(
        str(value).strip().replace(",", "").replace("\u00a0", " ")
    )
    if not text or text in {"-", "—", "n/a", "N/A", "none", "null"}:
        return None
    fi = _FEET_INCHES_STR_RE.search(text)
    if fi:
        return _feet_inches_match_to_ft(fi)
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

    Для участков Нью-Йорка (Queens/Brooklyn/Bronx/Manhattan), где стороны
    описаны через «PARALLEL WITH …» / «AT RIGHT ANGLES TO …» / «INTERIOR ANGLE»
    без компасного румба, геометрия всё равно хранится через вычисленный румб,
    а `has_bearing=False` убирает искусственный DMS-румб с чертежа, оставляя
    длину и `course_note` / `interior_angle`.
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
    has_bearing: bool = Field(
        default=True,
        description=(
            "True, если в документе/на чертеже явно указан компасный румб (N/S deg E/W); "
            "False, если направление задано относительно улиц (PARALLEL WITH / AT RIGHT ANGLES / вдоль улицы)"
        ),
    )
    course_note: str | None = Field(
        default=None,
        description=(
            "Текстовая пометка вдоль стороны без румба, например "
            "'PARALLEL WITH QUEENS MIDTOWN EXPY' или 'AT RIGHT ANGLES TO QUEENS MIDTOWN EXPRESSWAY'"
        ),
    )
    interior_angle: str | None = Field(
        default=None,
        description='Внутренний угол в начальной вершине этой стороны, например 93°33\'29.5"',
    )

    @model_validator(mode="before")
    @classmethod
    def _preprocess_segment_raw(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        item = dict(data)

        # Нормализация кардинальных сокращений от LLM (напр. ns="E", degrees=0 -> N 90° E)
        raw_ns = str(item.get("ns") or "").strip().upper()[:1]
        raw_ew = str(item.get("ew") or "").strip().upper()[:1]
        deg_f = _coerce_float(item.get("degrees"))
        min_f = _coerce_float(item.get("minutes"))
        sec_f = _coerce_float(item.get("seconds"))
        if raw_ns in {"E", "W"} and (deg_f is None or abs(deg_f) < 1e-9):
            item["ns"] = "N"
            item["ew"] = raw_ns
            deg_f = 90.0
            item["degrees"] = 90
        elif raw_ns in {"N", "S"} and raw_ew in {"N", "S"} and (deg_f is None or abs(deg_f) < 1e-9):
            item["ew"] = "E"

        # Раскладываем дробные градусы/минуты (напр. degrees=75.5 -> 75°30'00")
        if deg_f is not None:
            m_val = min_f if min_f is not None else 0.0
            s_val = sec_f if sec_f is not None else 0.0
            deg_frac = abs(deg_f) - int(abs(deg_f))
            if deg_frac > 1e-7 and abs(m_val) < 1e-9 and abs(s_val) < 1e-9:
                sign_d = -1 if deg_f < 0 else 1
                whole_d = int(abs(deg_f))
                rem_m = (abs(deg_f) - whole_d) * 60.0
                whole_m = int(rem_m + 1e-9)
                rem_s = max(0.0, (rem_m - whole_m) * 60.0)
                deg_f = float(sign_d * whole_d)
                m_val = float(whole_m)
                s_val = round(rem_s, 2)
            else:
                min_frac = abs(m_val) - int(abs(m_val))
                if min_frac > 1e-7 and abs(s_val) < 1e-9:
                    whole_m = int(abs(m_val))
                    rem_s = (abs(m_val) - whole_m) * 60.0
                    m_val = float(-whole_m if m_val < 0 else whole_m)
                    s_val = round(rem_s, 2)

            if deg_f >= 0 and m_val >= 0 and s_val >= 0:
                c_deg, c_min, c_sec = _carry_dms(int(round(deg_f)), int(round(m_val)), s_val)
                item["degrees"] = c_deg
                item["minutes"] = c_min
                item["seconds"] = c_sec
        return item

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

    @field_validator("bearing_label", "course_note", "interior_angle", mode="before")
    @classmethod
    def _normalize_label(cls, value: Any) -> str | None:
        return _clean_name(value)

    @field_validator("has_bearing", mode="before")
    @classmethod
    def _normalize_has_bearing(cls, value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "none"}
        return bool(value)

    @model_validator(mode="after")
    def _finalize(self) -> "Segment":
        d, m, s = _carry_dms(self.degrees, self.minutes, self.seconds)
        if d > 90 or (d == 90 and (m > 0 or s > 0)):
            raise ValueError("угол румба не может превышать 90°00'00\"")
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


BoundarySegment = Segment


_CARDINAL_TO_AZIMUTH_DEG: dict[str, float] = {
    "N": 0.0,
    "NORTH": 0.0,
    "NORTHERLY": 0.0,
    "NORTHWARD": 0.0,
    "NE": 45.0,
    "NORTHEAST": 45.0,
    "NORTHEASTERLY": 45.0,
    "E": 90.0,
    "EAST": 90.0,
    "EASTERLY": 90.0,
    "EASTWARD": 90.0,
    "SE": 135.0,
    "SOUTHEAST": 135.0,
    "SOUTHEASTERLY": 135.0,
    "S": 180.0,
    "SOUTH": 180.0,
    "SOUTHERLY": 180.0,
    "SOUTHWARD": 180.0,
    "SW": 225.0,
    "SOUTHWEST": 225.0,
    "SOUTHWESTERLY": 225.0,
    "W": 270.0,
    "WEST": 270.0,
    "WESTERLY": 270.0,
    "WESTWARD": 270.0,
    "NW": 315.0,
    "NORTHWEST": 315.0,
    "NORTHWESTERLY": 315.0,
}


def _angle_diff_deg(a: float, b: float) -> float:
    """Кратчайшая угловая разница в градусах [0, 180]."""
    diff = (a - b) % 360.0
    return 360.0 - diff if diff > 180.0 else diff


def _make_tie_segment_like(
    seg: Segment,
    *,
    distance_ft: float,
    flip_180: bool = False,
) -> Segment:
    ns_val: Literal["N", "S"] = (
        ("S" if seg.ns == "N" else "N") if flip_180 else seg.ns
    )
    ew_val: Literal["E", "W"] = (
        ("W" if seg.ew == "E" else "E") if flip_180 else seg.ew
    )
    return Segment(
        ns=ns_val,
        degrees=seg.degrees,
        minutes=seg.minutes,
        seconds=seg.seconds,
        ew=ew_val,
        distance_ft=max(float(distance_ft), 1e-6),
        has_bearing=seg.has_bearing,
        course_note=None,
        interior_angle=None,
    )


def _select_front_segment_index_for_tie(
    segments: Sequence[Segment],
    *,
    front_segment_index: int | None = None,
    front_side: str | None = None,
    pob_direction_az: float | None = None,
) -> int:
    """Определяет индекс фасадного сегмента вдоль main_street для привязки POB."""
    n = len(segments)
    if front_segment_index is not None and 0 <= front_segment_index < n:
        return front_segment_index

    if front_side and front_side.upper() in _CARDINAL_TO_AZIMUTH_DEG and n >= 3:
        target_az = _CARDINAL_TO_AZIMUTH_DEG[front_side.upper()]
        tx = math.sin(math.radians(target_az))
        ty = math.cos(math.radians(target_az))
        pts: list[tuple[float, float]] = [(0.0, 0.0)]
        x, y = 0.0, 0.0
        for s in segments:
            dx, dy = s.vector
            x += dx
            y += dy
            pts.append((x, y))
        verts = pts[:-1]
        area2 = sum(
            verts[i][0] * verts[(i + 1) % n][1] - verts[(i + 1) % n][0] * verts[i][1]
            for i in range(n)
        )
        ccw = area2 >= 0.0
        best_idx = 0
        best_dot = -2.0
        for idx, s in enumerate(segments):
            c_note = (s.course_note or "").upper()
            if "PARALLEL WITH" in c_note or "AT RIGHT ANGLES" in c_note:
                continue
            dx, dy = s.vector
            length = math.hypot(dx, dy)
            if length < 1e-9:
                continue
            nx, ny = (dy / length, -dx / length) if ccw else (-dy / length, dx / length)
            dot = nx * tx + ny * ty
            if dot > best_dot:
                best_dot = dot
                best_idx = idx
        if best_dot > -1.5:
            return best_idx

    if pob_direction_az is not None:
        best_idx = 0
        best_collinear = 181.0
        for idx, s in enumerate(segments):
            c_note = (s.course_note or "").upper()
            if "PARALLEL WITH" in c_note or "AT RIGHT ANGLES" in c_note:
                continue
            d_same = _angle_diff_deg(s.azimuth_deg, pob_direction_az)
            d_opp = _angle_diff_deg((s.azimuth_deg + 180.0) % 360.0, pob_direction_az)
            collinear_err = min(d_same, d_opp)
            if collinear_err < best_collinear - 1e-6 or (
                abs(collinear_err - best_collinear) <= 1e-6 and idx == n - 1
            ):
                best_collinear = collinear_err
                best_idx = idx
        return best_idx

    return 0


def infer_corner_to_pob_bearing(
    segments: Sequence[Segment],
    *,
    distance_to_corner_ft: float | None,
    front_segment_index: int | None = None,
    front_side: str | None = None,
    pob_direction: str | float | None = None,
    explicit_tie_segment: Segment | None = None,
) -> Segment | None:
    """
    Вычисляет вектор привязки от перекрёстка улиц (0.0, 0.0) к точке начала участка (POB):
      * Для углового участка (`distance_to_corner_ft <= 0`) возвращает `None`.
      * Если в первом абзаце (COMMENCING AT) явно указан румб подхода к POB — берёт его.
      * Если указано кардинальное направление («distant 420.00 feet easterly from the corner…») —
        выбирает из `(front_seg, flip_180(front_seg))` румб вдоль `main_street`, сонаправленный ему.
      * Иначе определяет знак геометрически по положению POB относительно середины фасада:
        для замыкающего сегмента вдоль `main_street` (NYC: `S 75°20'20" W ... to POB`) разворачивает
        румб строго на 180° (`N 75°20'20" E`), а для стартового сегмента от POB дальше от угла —
        сохраняет направление фасадного сегмента.
    """
    if distance_to_corner_ft is None or distance_to_corner_ft <= 1e-6 or not segments:
        return None
    dist = float(distance_to_corner_ft)

    if explicit_tie_segment is not None:
        return _make_tie_segment_like(
            explicit_tie_segment, distance_ft=dist, flip_180=False
        )

    pob_az: float | None = None
    if isinstance(pob_direction, (int, float)):
        pob_az = float(pob_direction) % 360.0
    elif isinstance(pob_direction, str) and pob_direction.strip():
        token = pob_direction.strip().upper()
        pob_az = _CARDINAL_TO_AZIMUTH_DEG.get(token)

    front_idx = _select_front_segment_index_for_tie(
        segments,
        front_segment_index=front_segment_index,
        front_side=front_side,
        pob_direction_az=pob_az,
    )
    front_seg = segments[front_idx]

    if pob_az is not None:
        diff_same = _angle_diff_deg(front_seg.azimuth_deg, pob_az)
        diff_flip = _angle_diff_deg((front_seg.azimuth_deg + 180.0) % 360.0, pob_az)
        return _make_tie_segment_like(
            front_seg,
            distance_ft=dist,
            flip_180=(diff_flip + 1e-6 < diff_same),
        )

    x, y = 0.0, 0.0
    start_x, start_y = 0.0, 0.0
    for idx, seg in enumerate(segments):
        if idx == front_idx:
            start_x, start_y = x, y
            break
        dx, dy = seg.vector
        x += dx
        y += dy
    fdx, fdy = front_seg.vector
    mid_x = start_x + 0.5 * fdx
    mid_y = start_y + 0.5 * fdy
    dot_along_front = mid_x * fdx + mid_y * fdy
    return _make_tie_segment_like(
        front_seg,
        distance_ft=dist,
        flip_180=(dot_along_front < -1e-9),
    )


class LegalDescription(BaseModel):
    """Результат разбора документа целиком (Schedule A / Deed / Survey PDF)."""

    model_config = ConfigDict(extra="forbid")

    main_street: str = Field(
        description="Улица, на которую выходит фасад участка (fronting ...)"
    )
    main_street_note: str | None = Field(
        default=None,
        description=(
            "Пояснение под названием фасадной улицы, например "
            "'(38\\' WIDE ROADWAY EASEMENT)' или '(NOT MAPPED ON FINAL CITY PLAN)'"
        ),
    )
    cross_street: str | None = Field(
        default=None,
        description="Пересекаемая улица, от которой отмеряется расстояние до угла",
    )
    distance_to_corner_ft: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Расстояние вдоль фасадной улицы до угла/пересечения (0.0, 0.0), футы "
            "(0.0 для углового участка или если привязка к углу отсутствует)"
        ),
    )
    corner_to_pob_bearing: Segment | None = Field(
        default=None,
        description=(
            "Румб и длина шага ОТ перекрёстка улиц (0.0, 0.0) К точке начала (POB) "
            "вдоль main_street (null для углового участка distance_to_corner_ft == 0). "
            "Если в преамбуле указано только направление ('distant 420 feet easterly from the corner'), "
            "вычисляется по румбу main_street как противоположный на 180° замыкающему сегменту вдоль улицы."
        ),
    )
    tie_note: str | None = Field(
        default=None,
        description=(
            "Дополнительная подпись привязки к углу квартала (например, "
            "'208.02\\' (DEED)' при многозвенной привязке COMMENCING AT)"
        ),
    )
    front_side: Literal["N", "S", "E", "W", "NE", "NW", "SE", "SW"] | None = Field(
        default=None,
        description=(
            "Направление света (N/S/E/W/NE/NW/SE/SW), куда смотрит фасадная граница "
            "(наружу от участка к улице). Если участок на South side улицы -> N; "
            "если на Northwesterly side улицы -> SE"
        ),
    )
    front_segment_index: int | None = Field(
        default=None,
        description=(
            "0-based индекс элемента в segments, являющегося фасадной границей "
            "вдоль main_street (null если неизвестно)"
        ),
    )
    cross_street_side_index: int | None = Field(
        default=None,
        description=(
            "Для углового участка: 0-based индекс элемента в segments, "
            "выходящего на cross_street (null для рядового участка)"
        ),
    )
    block_number: str | None = Field(
        default=None,
        description="Номер квартала (Block), например '3960' или '2366'",
    )
    lot_number: str | None = Field(
        default=None,
        description="Номер лота (Lot), например '129' или '59'",
    )
    borough_or_city: str | None = Field(
        default=None,
        description="Район/город/округ, например 'Staten Island, NY' или 'Queens, NY'",
    )
    anomaly_note: str | None = Field(
        default=None,
        description=(
            "Если в тексте Legal Description или на скане обнаружена очевидная опечатка "
            "(перепутаны N/S или E/W, либо цифра 3↔8, 8↔0, 1↔7, 5↔6 в угле или длине) "
            "и она исправлена — обязательное пояснение на русском языке, что было указано "
            "и на что исправлено; иначе null"
        ),
    )
    segments: list[Segment] = Field(
        min_length=1,
        description="Стороны участка по порядку обхода (каждый call = один элемент)",
    )

    @model_validator(mode="before")
    @classmethod
    def _preprocess_corner_tie(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_dist = _coerce_float(data.get("distance_to_corner_ft"))
        dist = raw_dist if raw_dist is not None and raw_dist > 0.0 else 0.0
        tie = data.get("corner_to_pob_bearing")
        if dist <= 1e-6:
            if tie is not None:
                return {**data, "corner_to_pob_bearing": None}
            return data
        if isinstance(tie, dict):
            if not any(v not in (None, "", 0, 0.0, False) for v in tie.values()):
                return {**data, "corner_to_pob_bearing": None}
            tie_dist = _coerce_float(tie.get("distance_ft"))
            if tie_dist is None or tie_dist <= 0.0:
                return {
                    **data,
                    "corner_to_pob_bearing": {**tie, "distance_ft": dist},
                }
        return data

    @field_validator("main_street", mode="before")
    @classmethod
    def _normalize_main_street(cls, value: Any) -> str:
        return _clean_name(value) or ""

    @field_validator(
        "main_street_note",
        "cross_street",
        "tie_note",
        "block_number",
        "lot_number",
        "borough_or_city",
        "anomaly_note",
        mode="before",
    )
    @classmethod
    def _normalize_optional_text(cls, value: Any) -> str | None:
        return _clean_name(value)

    @field_validator("distance_to_corner_ft", mode="before")
    @classmethod
    def _normalize_corner_distance(cls, value: Any) -> float:
        num = _coerce_float(value)
        if num is None or num < 0.0:
            return 0.0
        return float(num)

    @field_validator("front_segment_index", "cross_street_side_index", mode="before")
    @classmethod
    def _normalize_optional_index(cls, value: Any) -> int | None:
        num = _coerce_float(value)
        if num is None:
            return None
        idx = int(round(num))
        return idx if idx >= 0 else None

    @field_validator("front_side", mode="before")
    @classmethod
    def _normalize_front_side(cls, value: Any) -> str | None:
        if value is None:
            return None
        token = str(value).strip().upper()
        if token in {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}:
            return token
        mapping = {
            "NORTH": "N",
            "SOUTH": "S",
            "EAST": "E",
            "WEST": "W",
            "NORTHEAST": "NE",
            "NORTHWEST": "NW",
            "SOUTHEAST": "SE",
            "SOUTHWEST": "SW",
        }
        if token in mapping:
            return mapping[token]
        letter = token[:1]
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
                v not in (None, "", 0, 0.0, False) for v in item.values()
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
        if self.front_segment_index is not None and self.front_segment_index >= len(self.segments):
            self.front_segment_index = None
        if (
            self.cross_street_side_index is not None
            and self.cross_street_side_index >= len(self.segments)
        ):
            self.cross_street_side_index = None

        if self.distance_to_corner_ft <= 1e-6:
            self.corner_to_pob_bearing = None
        elif self.corner_to_pob_bearing is None:
            self.corner_to_pob_bearing = infer_corner_to_pob_bearing(
                self.segments,
                distance_to_corner_ft=self.distance_to_corner_ft,
                front_segment_index=self.front_segment_index,
                front_side=self.front_side,
            )
        elif abs(self.corner_to_pob_bearing.distance_ft - self.distance_to_corner_ft) > 1e-6:
            self.corner_to_pob_bearing = _make_tie_segment_like(
                self.corner_to_pob_bearing,
                distance_ft=self.distance_to_corner_ft,
            )
        return self

    @property
    def perimeter_ft(self) -> float:
        return sum(seg.distance_ft for seg in self.segments)

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)


LegalDescriptionData = LegalDescription


# ---------------------------------------------------------------------------
# Чтение и рендер PDF и фотоархивов чертежей (JPG/PNG/TIFF/WEBP + 1980s scans)
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
)


def is_image_file(path: str | Path) -> bool:
    """True, если путь указывает на растровый файл чертежа/фотографии."""
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def parse_page_range(
    spec: str | Sequence[int] | None,
    total_pages: int,
    *,
    max_pages: int = 8,
) -> list[int]:
    """
    Разбирает выбор страниц PDF (1-based строка вроде ``'1'``, ``'2-5'``,
    ``'1, 3, 6-7'``, ``'все'`` / ``'all'``) в список 0-based индексов страниц.
    """
    if total_pages <= 0:
        raise PDFError("PDF не содержит страниц")
    if spec is None:
        cap = total_pages if max_pages <= 0 else min(total_pages, int(max_pages))
        return list(range(cap))

    if isinstance(spec, str):
        cleaned = spec.strip().lower()
        if cleaned in ("", "auto", "авто"):
            cap = total_pages if max_pages <= 0 else min(total_pages, int(max_pages))
            return list(range(cap))
        if cleaned in ("all", "все", "всё", "*"):
            return list(range(total_pages))

        indices: list[int] = []
        seen: set[int] = set()
        for raw_part in re.split(r"[,;]+", cleaned):
            part = raw_part.strip()
            if not part:
                continue
            range_m = re.fullmatch(r"(\d+)\s*(?:-|\.\.|–|—)\s*(\d+)", part)
            if range_m:
                start_p = int(range_m.group(1))
                end_p = int(range_m.group(2))
                if start_p > end_p:
                    start_p, end_p = end_p, start_p
                if start_p < 1 or end_p > total_pages:
                    raise PDFError(
                        f"диапазон страниц «{part}» выходит за пределы PDF (всего страниц: {total_pages})"
                    )
                for p1 in range(start_p, end_p + 1):
                    idx0 = p1 - 1
                    if idx0 not in seen:
                        seen.add(idx0)
                        indices.append(idx0)
                continue
            if part.isdigit():
                p1 = int(part)
                if not (1 <= p1 <= total_pages):
                    raise PDFError(
                        f"страница {p1} вне диапазона документа (всего страниц: {total_pages})"
                    )
                idx0 = p1 - 1
                if idx0 not in seen:
                    seen.add(idx0)
                    indices.append(idx0)
                continue
            raise PDFError(
                f"некорректный выбор страниц PDF: «{spec}» (пример: «1», «2-4» или «все»)"
            )
        if not indices:
            raise PDFError("не выбрана ни одна страница PDF")
        return indices

    indices_seq: list[int] = []
    seen_seq: set[int] = set()
    for p_raw in spec:
        p1 = int(p_raw)
        if not (1 <= p1 <= total_pages):
            raise PDFError(
                f"страница {p1} вне диапазона документа (всего страниц: {total_pages})"
            )
        idx0 = p1 - 1
        if idx0 not in seen_seq:
            seen_seq.add(idx0)
            indices_seq.append(idx0)
    if not indices_seq:
        raise PDFError("не выбрана ни одна страница PDF")
    return indices_seq


def _adaptive_max_side_px(item_count: int, requested_max_px: int | None = None) -> int:
    """Адаптивно выбирает макс. сторону кадра, чтобы пачка фото не вызвала HTTP 413."""
    if requested_max_px is not None and requested_max_px > 0:
        return max(768, int(requested_max_px))
    if item_count <= 2:
        return 2800
    if item_count <= 4:
        return 2200
    return 1800


def _normalize_rotation_list(
    rotation_deg: int | Sequence[int], count: int
) -> list[int]:
    if isinstance(rotation_deg, (int, float)):
        val = int(round(float(rotation_deg))) % 360
        return [val] * count
    rots = [int(round(float(r))) % 360 for r in rotation_deg]
    if len(rots) < count:
        fill = rots[-1] if rots else 0
        rots.extend([fill] * (count - len(rots)))
    return rots[:count]


def enhance_archival_scan(pil_img: Any) -> Any:
    """
    Мягкое геодезическое улучшение фотографий старых чертежей (синьки 1980-х,
    выцветшая калька, карандаш/трафарет Leroy, цветные штампы):
      * переводит в чистый RGB на белой подложке;
      * растягивает уровни по общей яркости (единая LUT для R/G/B без искажения цветов!);
      * умеренно повышает контраст и микрорезкость штрихов/цифр (UnsharpMask),
        не съедая десятичные точки и штрихи минут/секунд.
    """
    from PIL import Image, ImageEnhance, ImageFilter

    if pil_img.mode in ("RGBA", "LA") or (
        pil_img.mode == "P" and "transparency" in pil_img.info
    ):
        rgba = pil_img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        rgb = bg
    elif pil_img.mode != "RGB":
        rgb = pil_img.convert("RGB")
    else:
        rgb = pil_img

    gray_hist = rgb.convert("L").histogram()
    total_px = sum(gray_hist) or 1
    cut = int(total_px * 0.004)
    lo = 0
    acc = 0
    for val, cnt in enumerate(gray_hist):
        acc += cnt
        if acc > cut:
            lo = val
            break
    hi = 255
    acc = 0
    for val in range(255, -1, -1):
        acc += gray_hist[val]
        if acc > cut:
            hi = val
            break

    if hi - lo >= 25 and (lo > 12 or hi < 245):
        span = float(hi - lo)
        lut = [
            max(0, min(255, int(round(6.0 + (i - lo) * 246.0 / span))))
            for i in range(256)
        ]
        leveled = rgb.point(lut * 3)
    else:
        leveled = rgb

    contrasted = ImageEnhance.Contrast(leveled).enhance(1.15)
    sharpened = contrasted.filter(
        ImageFilter.UnsharpMask(radius=1.2, percent=135, threshold=2)
    )
    return sharpened


def _rotate_pil_clockwise(pil_img: Any, clockwise_deg: int) -> Any:
    from PIL import Image

    deg = int(clockwise_deg) % 360
    if deg == 0:
        return pil_img
    if deg == 90:
        return pil_img.transpose(Image.Transpose.ROTATE_270)
    if deg == 180:
        return pil_img.transpose(Image.Transpose.ROTATE_180)
    if deg == 270:
        return pil_img.transpose(Image.Transpose.ROTATE_90)
    return pil_img.rotate(
        -deg,
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=(255, 255, 255),
    )


def prepare_survey_image_bytes(
    source: str | Path | bytes,
    *,
    rotation_deg: int = 0,
    max_side_px: int = 2800,
    enhance_scan: bool = True,
    output_format: Literal["jpeg", "png"] = "jpeg",
    jpeg_quality: int = 88,
) -> tuple[bytes, str, dict[str, Any]]:
    """
    Открывает фото/скан чертежа (JPG/PNG/TIFF/WEBP/BMP или сырые байты),
    исправляет EXIF-ориентацию со смартфона, поворачивает по часовой стрелке на
    ``rotation_deg``, масштабирует до ``max_side_px``, опционально повышает
    читаемость старых чертежей 80-х и кодирует в JPEG 4:4:4 (или PNG).

    Возвращает ``(image_bytes, mime_type, metadata_dict)``.
    """
    if isinstance(source, (str, Path)):
        src_path = Path(source)
        if not src_path.exists():
            raise PDFError(f"файл изображения не найден: {src_path}")
        raw_bytes = src_path.read_bytes()
        src_name = src_path.name
    else:
        raw_bytes = bytes(source)
        src_name = "memory"

    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - резервный путь через PyMuPDF без Pillow
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz  # type: ignore[no-redef]
        pix = fitz.Pixmap(raw_bytes)
        if pix.alpha:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        png_b = pix.tobytes("png")
        return png_b, "image/png", {
            "name": src_name,
            "width": pix.width,
            "height": pix.height,
            "exif_transposed": False,
            "exif_orientation": 1,
            "rotation_deg": int(rotation_deg) % 360,
        }

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise PDFError(f"не удалось прочитать изображение «{src_name}»: {exc}") from exc

    exif_orient = 1
    try:
        exif_data = img.getexif()
        if exif_data:
            exif_orient = int(exif_data.get(0x0112, 1) or 1)
    except Exception:  # noqa: BLE001
        exif_orient = 1

    exif_transposed = False
    if exif_orient not in (0, 1):
        try:
            img = ImageOps.exif_transpose(img)
            exif_transposed = True
        except Exception:  # noqa: BLE001
            exif_transposed = False

    rot_cw = int(rotation_deg) % 360
    if rot_cw != 0:
        img = _rotate_pil_clockwise(img, rot_cw)

    safe_max = max(512, int(max_side_px or 2800))
    w0, h0 = img.size
    if max(w0, h0) > safe_max:
        ratio = safe_max / float(max(w0, h0))
        new_size = (max(1, int(round(w0 * ratio))), max(1, int(round(h0 * ratio))))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    if enhance_scan:
        img = enhance_archival_scan(img)
    elif img.mode != "RGB":
        if img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        else:
            img = img.convert("RGB")

    out_buf = io.BytesIO()
    if output_format == "png":
        img.save(out_buf, format="PNG", optimize=True)
        mime = "image/png"
    else:
        img.save(
            out_buf,
            format="JPEG",
            quality=max(60, min(96, int(jpeg_quality))),
            subsampling=0,
            optimize=True,
        )
        mime = "image/jpeg"

    w_fin, h_fin = img.size
    return out_buf.getvalue(), mime, {
        "name": src_name,
        "width": w_fin,
        "height": h_fin,
        "exif_transposed": exif_transposed,
        "exif_orientation": exif_orient,
        "rotation_deg": rot_cw,
    }


def render_image_files_base64(
    image_paths: Sequence[str | Path],
    *,
    rotation_deg: int | Sequence[int] = 0,
    max_side_px: int | None = None,
    enhance_scan: bool = True,
    output_format: Literal["jpeg", "png"] = "jpeg",
) -> tuple[list[str], list[str]]:
    """
    Готовит список фотографий/сканов чертежей (``.jpg/.png/.tif/.webp/.bmp``)
    к отправке в Vision LLM: EXIF-автоповорот, пользовательский поворот,
    улучшение контраста старых чертежей 80-х и компактное кодирование base64.

    Возвращает ``(data_urls, info_notes)``.
    """
    paths = [Path(p) for p in image_paths]
    if not paths:
        raise PDFError("не передано ни одного файла изображения")

    target_max_px = _adaptive_max_side_px(len(paths), max_side_px)
    rots = _normalize_rotation_list(rotation_deg, len(paths))
    urls: list[str] = []
    notes: list[str] = []

    for idx, img_path in enumerate(paths):
        rot_i = rots[idx]
        img_bytes, mime, meta = prepare_survey_image_bytes(
            img_path,
            rotation_deg=rot_i,
            max_side_px=target_max_px,
            enhance_scan=enhance_scan,
            output_format=output_format,
        )
        encoded = base64.b64encode(img_bytes).decode("ascii")
        urls.append(f"data:{mime};base64,{encoded}")
        applied_tags: list[str] = []
        if meta.get("exif_transposed"):
            applied_tags.append(f"EXIF-ориентация #{meta['exif_orientation']} → прямо")
        if rot_i:
            applied_tags.append(f"поворот +{rot_i}°")
        if applied_tags:
            notes.append(f"Фото «{img_path.name}»: {', '.join(applied_tags)}")
    return urls, notes


def _open_fitz_doc(pdf_path: str | Path) -> tuple[Any, Path]:
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
    return doc, path


def inspect_pdf(pdf_path: str | Path, max_pages: int = 0) -> dict[str, Any]:
    """
    Проверяет PDF без падения на векторных CAD-чертежах и сканах без текста.

    Возвращает ``{"pages": int, "has_text": bool, "has_legal_text": bool,
    "has_raster_images": bool, "text": str}``.
    """
    doc, _ = _open_fitz_doc(pdf_path)
    has_raster_images = False
    try:
        total_pages = len(doc)
        pages = range(total_pages) if not max_pages else range(min(max_pages, total_pages))
        chunks: list[str] = []
        for index in pages:
            page = doc.load_page(index)
            text = page.get_text("text") or ""
            if text.strip():
                chunks.append(text)
            try:
                if page.get_images(full=False):
                    has_raster_images = True
            except Exception:  # noqa: BLE001
                pass
    finally:
        doc.close()

    joined = "\n".join(chunks)
    has_text = len(joined.strip()) >= 20
    has_legal_text = bool(has_text and _has_min_courses(_normalize_legal_text(joined)))
    return {
        "pages": total_pages,
        "has_text": has_text,
        "has_legal_text": has_legal_text,
        "has_raster_images": has_raster_images,
        "text": joined,
    }


def read_pdf_text(pdf_path: str | Path, max_pages: int = 0) -> str:
    """
    Извлекает текстовый слой PDF через PyMuPDF (fitz).

    max_pages = 0 — читать все страницы. Векторные CAD-чертежи (SHX-шрифты в
    кривых) и сканы без OCR не имеют текстового слоя — в этом случае
    поднимается PDFError; для их разбора используйте ``render_pdf_pages_base64``
    или ``parse_legal_description_full(..., use_llm=True)`` (режим Vision).
    """
    info = inspect_pdf(pdf_path, max_pages=max_pages)
    if not info["has_text"]:
        raise PDFError(
            "в PDF нет текстового слоя (векторный CAD-чертёж или скан). "
            "Включите режим LLM с Vision-моделью или вставьте текст вручную."
        )
    return str(info["text"])


def render_pdf_pages_base64(
    pdf_path: str | Path,
    *,
    dpi: int = 220,
    max_pages: int = 8,
    max_side_px: int = 3200,
    pages: str | Sequence[int] | None = None,
    rotation_deg: int | Sequence[int] = 0,
    enhance_scan: bool = False,
    output_format: Literal["png", "jpeg", "auto"] = "png",
) -> list[str]:
    """
    Рендерит выбранные страницы PDF в base64 data-URL для Vision LLM.

    * Для чисто векторных CAD-PDF в режиме ``output_format='png'`` или ``'auto'``
      отдаёт чёткий lossless PNG (``data:image/png;base64,...``).
    * Для PDF-альбомов с вшитыми растровыми фото старых чертежей (``page.get_images()``)
      в режиме ``output_format='auto'`` включает фильтр контраста/резкости 80-х
      и компактное сжатие ``JPEG 4:4:4``, исключая ошибку HTTP 413 на пачке фото.
    """
    doc, _ = _open_fitz_doc(pdf_path)
    safe_dpi = max(96, min(int(dpi or 220), 350))
    urls: list[str] = []
    try:
        total_pages = len(doc)
        page_indices = parse_page_range(pages, total_pages, max_pages=max_pages)
        rots = _normalize_rotation_list(rotation_deg, len(page_indices))
        eff_max_px = (
            max(1200, int(max_side_px))
            if len(page_indices) <= 2
            else min(max(1200, int(max_side_px)), _adaptive_max_side_px(len(page_indices)))
        )

        for pos, index in enumerate(page_indices):
            page = doc.load_page(index)
            rect = page.rect
            max_inches = max(float(rect.width), float(rect.height)) / 72.0
            effective_dpi = safe_dpi
            if max_inches > 0:
                effective_dpi = min(safe_dpi, max(72, int(eff_max_px / max_inches)))
            pix = page.get_pixmap(dpi=effective_dpi, alpha=False)
            raw_png = pix.tobytes("png")

            rot_i = rots[pos]
            has_raster = False
            if output_format == "auto":
                try:
                    has_raster = bool(page.get_images(full=False))
                except Exception:  # noqa: BLE001
                    has_raster = False

            use_photo_pipeline = (
                output_format == "jpeg"
                or (output_format == "auto" and has_raster)
                or rot_i != 0
                or (enhance_scan and has_raster)
            )
            if not use_photo_pipeline:
                encoded = base64.b64encode(raw_png).decode("ascii")
                urls.append(f"data:image/png;base64,{encoded}")
            else:
                target_fmt: Literal["jpeg", "png"] = (
                    "jpeg"
                    if (output_format == "jpeg" or (output_format == "auto" and has_raster))
                    else "png"
                )
                processed_bytes, mime, _ = prepare_survey_image_bytes(
                    raw_png,
                    rotation_deg=rot_i,
                    max_side_px=eff_max_px,
                    enhance_scan=bool(enhance_scan and (has_raster or output_format == "jpeg")),
                    output_format=target_fmt,
                )
                encoded = base64.b64encode(processed_bytes).decode("ascii")
                urls.append(f"data:{mime};base64,{encoded}")
    finally:
        doc.close()
    if not urls:
        raise PDFError("PDF не содержит страниц для рендера")
    return urls


# ---------------------------------------------------------------------------
# Промпт для LLM (текст Schedule A / Deed + Vision-чертежи + архивные фото 80-х)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior licensed New York / USA land-survey technician.
You extract closed lot boundary geometry from a metes-and-bounds legal description (Schedule A / Deed) OR from one or more images/photos of an existing Title / Architectural Survey plat (including faded 1970s–1980s diazo blueprints, sepia/linen copies, Leroy-stenciled or hand-drafted plats, and randomly rotated camera photos) into strict JSON.

CRITICAL SURVEY RULES
0. ROTATED / UPSIDE-DOWN PHOTOS & NORTH ARROW ORIENTATION (MANDATORY FOR IMAGES):
   - Attached photos of old surveys may be tilted, sideways (90° / 270°), or completely upside-down (180°)!
   - STEP A: First determine which way the drawing is oriented by reading the Title Block, surveyor seal, and street names right-side up.
   - STEP B: Locate the drawn NORTH ARROW ("N", star, or half-barb arrow) on the plat! NEVER assume the top edge of the camera photo is North. Determine `front_side` and any relative NYC street directions strictly with respect to the drawn North Arrow (and printed quadrant bearings if present).
   - STEP C: When multiple photos of the SAME survey are attached (e.g., an overview photo + close-ups of boundary calls or North arrow), synthesize them: use the overview for global topology/streets/North arrow and the sharpest close-up for exact digits!
   - STEP D: On rotated or upside-down 1980s lettering, double-check inverted digits (`6` <-> `9`, e.g., `60.00'` vs `90.00'`, `16'` vs `19'`, `68°` vs `89°`) and faded blueprint/handwritten glyphs (`3` <-> `8`, `5` <-> `6`, `1` <-> `7`, `0` <-> `8`). Verify against opposite lot sides and polygon closure!
1. LOT BOUNDARY vs. TIE LINES ("COMMENCING AT") & CORNER-TO-POB VECTOR:
   - NEVER include tie/commencing calls from a distant street corner to the Point of Beginning (POB) in `segments`!
   - Example: if a deed or survey shows 1–3 tie calls from an intersection (e.g. 510.52', 433.78', 208.02' from Mill Rd & New Dorp Ln, or 101.42', 17.49', 1542.23' from Arthur Kill Rd) leading TO the lot corner (POB), EXCLUDE those tie calls from `segments`.
   - Put the immediate distance along `main_street` from the street intersection `(0.0, 0.0)` to POB into `distance_to_corner_ft` (use `0.0` for a corner lot), and put any compound tie or dual Deed/Tax-Map note (e.g. "208.02' (DEED) / 208.22' (T.M.)") into `tie_note`.
   - `corner_to_pob_bearing`: when `distance_to_corner_ft > 0`, provide the exact quadrant bearing (`ns`, `degrees`, `minutes`, `seconds`, `ew`) and `distance_ft = distance_to_corner_ft` pointing FROM the street corner `(0.0, 0.0)` TO `POB` along `main_street` (use `null` when `distance_to_corner_ft == 0`). If the opening paragraph only gives a word direction (e.g. "distant 420.00 feet easterly from the corner..."), take the exact DMS bearing of `main_street` and orient it toward POB — i.e., if the closing segment along `main_street` returning to POB is `S 75°20'20" W`, flip it by 180° to `N 75°20'20" E`!
2. IGNORE BUILDING & FENCE OFFSETS:
   - Never confuse building wall dimensions (e.g. 13.8', 24.3', 50.7' inside 2-sty frame/masonry house) or fence/setback offsets ("FE. 0.1'W", "1.5'E", "25.4'", "GARAGE", "SHED") with lot boundary lines.
   - When both "(DEED)" / "(REC.)" and "(T.M.)" / "(TAX MAP)" / "(MEAS.)" values appear, prefer the measured survey boundary or "(DEED)" record dimension consistently around the closed loop.
3. QUADRANT BEARINGS, RELATIVE NYC COURSES & TYPO EVALUATION:
   - Every element of `segments` represents one lot boundary side in continuous loop order around the parcel. On survey plats, orient each bearing's N/S and E/W letters along your chosen CW/CCW loop direction.
   - DO NOT alter small 0.01-ft rounding gaps (e.g. 0.003–0.015 ft on oblique trapezoids) — preserve exact record numbers; our CAD geometry engine handles analytical right-trapezoid closure and open rounding gaps automatically.
   - HOWEVER, act as a thinking surveyor when a LARGE closure gap (> 0.1 ft) appears: check whether it is caused by an OBVIOUS TYPO or visual OCR digit confusion:
     * Quadrant letter typo (`N` <-> `S` or `E` <-> `W`), OR
     * Digit confusion (`6` <-> `9`, `3` <-> `8`, `8` <-> `0`, `1` <-> `7`, `5` <-> `6`, `0` <-> `6`, `0` <-> `9`) in `degrees`, `minutes`, `seconds`, or `distance_ft` (compare against opposite parallel/perpendicular sides, building dimensions, and lot frontage).
     If an obvious typo is identified and fixing it cleanly closes the parcel, output the corrected value AND explain clearly in Russian in `anomaly_note` what was written in the source and what typo was corrected. If NO obvious typo can be determined with confidence, keep all printed lengths and angles strictly verbatim and set `anomaly_note` to `null` (or describe the unresolved discrepancy in `anomaly_note` without altering `segments`).
   - When explicit quadrant bearings are given, split into `ns` ("N"/"S"), `degrees` (0..90), `minutes` (0..59), `seconds` (decimal), `ew` ("E"/"W"), `bearing_label` ("N 75°20'20\" E"), and set `has_bearing = true`.
   - When NO compass bearings are printed (common in NYC & 1980s surveys using "PARALLEL WITH ...", "AT RIGHT ANGLES TO ...", cardinal directions along streets, and/or an `interior_angle` like `93°33'29.5"`):
     * Align the primary East-West street to N 90°00' E / N 90°00' W (or North-South street to N 0°00' E / S 0°00' E) according to the North Arrow, and apply exact printed interior/right angles without inventing compass labels;
     * Set `has_bearing = false` on every course without an explicit compass bearing;
     * Put exact directional notes printed on that side verbatim (e.g. "PARALLEL WITH QUEENS MIDTOWN EXPY", "AT RIGHT ANGLES TO QUEENS MIDTOWN EXPRESSWAY") into `course_note`;
     * ALWAYS look inside the parcel corners for any printed vertex angle (e.g. `93°33'29.5"`) and record it in `interior_angle` of the segment starting at that corner!
4. DISTANCES (INCLUDING 1980s FRACTIONAL INCHES):
   - `distance_ft` is in decimal feet. Convert all feet-and-inches and fractional inches accurately:
     * "13 feet 8 inches" -> 13.6667
     * "25' 10\"" -> 25.8333, "55'-6\"" -> 55.5
     * "25'-6 1/2\"" -> 25 + 6.5/12 = 25.5417
     * "100' 3-1/4\"" -> 100 + 3.25/12 = 100.2708
     * "50'-0 3/4\"" -> 50 + 0.75/12 = 50.0625
     * varas (×2.77778) or chains (×66) -> decimal feet.
5. STREETS, FRONTAGE & NYC METADATA:
   - `main_street`: primary street the lot fronts on (name only, e.g. "Dartmouth Loop", "Jamie Lane", "Borden Avenue").
   - `main_street_note`: parenthetical subtitle if shown, e.g. "(38' WIDE ROADWAY EASEMENT)", "(NOT MAPPED ON FINAL CITY PLAN)", "(QUEENS MIDTOWN EXPRESSWAY)", else null.
   - `front_segment_index`: 0-based index (0 = first segment) in `segments` of the lot boundary that fronts `main_street` (never pick a rear/side wall labeled PARALLEL WITH or AT RIGHT ANGLES!).
   - `front_side`: compass direction ("N", "S", "E", "W", "NE", "NW", "SE", "SW") pointing OUTWARD from the lot toward `main_street` relative to the North Arrow. If the lot is on the North/Northerly side of Main Street -> "S"; if on the Northwesterly side -> "SE".
   - `cross_street`: intersecting street (e.g. "63rd Street", "Mill Road", "Kenilworth Avenue"), else null.
   - `cross_street_side_index`: 0-based index in `segments` of the side abutting `cross_street` ONLY if this is a corner lot (`distance_to_corner_ft == 0`), else null.
   - `block_number`, `lot_number`, `borough_or_city`: extract NYC/county Block, Lot, and Borough/City if present, else null.
6. Curves: use chord bearing + chord distance as one straight segment. Never invent calls; return strict JSON only."""

USER_PROMPT_TEMPLATE = """LEGAL DESCRIPTION:
\"\"\"
{text}
\"\"\"

Extract the lot geometry and survey metadata into JSON."""

VISION_USER_PROMPT = """Examine the attached survey / title document image(s) carefully (note: photos may be 1980s blueprints/hand-drafted plats at an arbitrary rotation or tilt, or multiple close-up shots of the same survey).
1. ORIENTATION & NORTH ARROW FIRST: Check if any image is rotated (90°/180°/270° or tilted), locate the Title Block and the drawn NORTH ARROW ("N"), and establish true North before reading directions. Never assume the top of the photo is North!
2. EXACT BOUNDARY CALLS & 1980s GLYPHS: Read lot boundary dimensions (including fractional inches like `25'-6 1/2"` -> `25.5417`) and bearings (or NYC relative notes like `PARALLEL WITH ...` / `AT RIGHT ANGLES TO ...`). Watch carefully for upside-down `6` vs `9` and faded/handwritten `3` vs `8`, `5` vs `6`, `1` vs `7`, `0` vs `8`.
3. INTERIOR ANGLES & STREETS: Check inside every parcel corner for printed interior angles (such as `93°33'29.5"`) -> `interior_angle`. Read street names, subtitles in parentheses, corner tie distances (`distance_to_corner_ft`), and Block/Lot/Borough.
4. CLOSED LOOP & MULTI-PHOTO CHECK: If multiple photos of the same lot are attached, cross-reference overview and close-ups. Walk the parcel boundary in continuous loop order, excluding COMMENCING / corner tie lines and building/fence offsets from `segments`. If you spot and fix an obvious typo or OCR digit confusion, explain it in Russian in `anomaly_note`."""


def _messages(
    text: str,
    image_data_urls: Sequence[str] | None = None,
    *,
    system_override: str | None = None,
    focus_instruction: str | None = None,
) -> list[dict[str, Any]]:
    sys_content = system_override if system_override is not None else SYSTEM_PROMPT
    if not image_data_urls:
        user_text = USER_PROMPT_TEMPLATE.format(text=text)
        if focus_instruction and focus_instruction.strip():
            user_text += f"\n\n{focus_instruction.strip()}"
        return [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user_text},
        ]

    prompt_parts = [VISION_USER_PROMPT]
    if text and text.strip():
        prompt_parts.append(f"ADDITIONAL NOTES / TEXT:\n\"\"\"\n{text.strip()}\n\"\"\"")
    if focus_instruction and focus_instruction.strip():
        prompt_parts.append(focus_instruction.strip())
    user_blocks: list[dict[str, Any]] = [
        {"type": "text", "text": "\n\n".join(prompt_parts)}
    ]
    for url in image_data_urls:
        user_blocks.append({"type": "image_url", "image_url": {"url": url}})
    return [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": user_blocks},
    ]


# ---------------------------------------------------------------------------
# Работа с LLM и учёт токенов
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenUsage:
    """Статистика отправленных и полученных токенов за вызов LLM."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    provider: str = ""
    provider_label: str = ""
    model: str = ""
    available: bool = True

    def short_report(self) -> str:
        target = " / ".join(
            part for part in (self.provider_label or self.provider, self.model) if part
        )
        badge = f"  [{target}]" if target else ""
        if not self.available:
            return f"Токены за вызов: провайдер не вернул usage{badge}"
        return (
            f"Токены за вызов: ↑ ушло {self.prompt_tokens:,}  |  "
            f"↓ пришло {self.completion_tokens:,}  |  "
            f"всего {self.total_tokens:,}{badge}"
        )


def _extract_usage_counts(completion: Any) -> tuple[int, int, int] | None:
    """Достаёт (prompt_tokens, completion_tokens, total_tokens) из ответа SDK."""
    raw = (
        completion.get("usage")
        if isinstance(completion, dict)
        else getattr(completion, "usage", None)
    )
    if raw is None:
        return None

    def _get(obj: Any, *names: str) -> int | None:
        for name in names:
            val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return int(val)
        return None

    prompt = _get(raw, "prompt_tokens", "input_tokens")
    completion_tok = _get(raw, "completion_tokens", "output_tokens")
    total = _get(raw, "total_tokens")
    if prompt is None and completion_tok is None and total is None:
        return None
    p_val = int(prompt or 0)
    c_val = int(completion_tok or 0)
    t_val = int(total) if total is not None and total > 0 else (p_val + c_val)
    return p_val, c_val, t_val


def _make_client(
    api_cfg: dict[str, Any],
    *,
    target: dict[str, str],
) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise LLMParseError(
            "пакет openai не установлен. Выполните: pip install openai"
        ) from exc

    key = target["api_key"]
    provider_label = target["provider_label"]
    env_hint = f" (или переменную {target['env_key']})" if target.get("env_key") else ""
    if not key:
        raise LLMParseError(
            f"не задан API-ключ для провайдера {provider_label}: "
            f"впишите его в поле «{provider_label}» в окне программы, "
            f"в config.json{env_hint}"
        )
    base_url = target["base_url"] or None
    return OpenAI(
        api_key=key,
        base_url=base_url,
        timeout=float(api_cfg.get("timeout_sec") or 180),
        max_retries=int(api_cfg.get("max_retries") or 0),
        default_headers={
            "HTTP-Referer": "https://github.com/Berber86/CAD",
            "X-Title": "Title Survey AI",
        },
    )


def _is_auth_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return True
    msg = str(exc).lower()
    return "401" in msg and ("unauthorized" in msg or "invalid api key" in msg or "auth" in msg)


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


def _unwrap_llm_payload(obj: Any) -> Any:
    """Снимает лишнюю обёртку вроде ``[{"main_street": ...}]`` или ``{"result": {...}}``."""
    if isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], dict):
        obj = obj[0]
    if isinstance(obj, dict) and "segments" not in obj:
        for val in obj.values():
            if isinstance(val, dict) and "segments" in val:
                return val
    return obj


def _json_from_text(raw: str) -> Any:
    """Достаёт JSON-объект из ответа модели (снимает <think>, ```json и фигурные скобки в прозе)."""
    text = raw.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    if "</think>" in text.lower():
        idx = text.lower().rfind("</think>")
        text = text[idx + len("</think>") :].strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        return _unwrap_llm_payload(json.loads(text))
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch not in "{[":
                continue
            try:
                parsed, _ = decoder.raw_decode(text, idx)
                unwrapped = _unwrap_llm_payload(parsed)
                if isinstance(unwrapped, dict):
                    return unwrapped
            except json.JSONDecodeError:
                continue
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return _unwrap_llm_payload(json.loads(text[start : end + 1]))
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
        raw_schema = copy.deepcopy(model_cls.model_json_schema())

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

    return walk(raw_schema)


def _extract_with_llm(
    text: str,
    cfg: dict[str, Any],
    api_key: str | None = None,
    progress: Callable[[str], None] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    key_secret: str = "",
    image_data_urls: Sequence[str] | None = None,
    focus_instruction: str | None = None,
) -> tuple[LegalDescription, str, TokenUsage]:
    """
    Возвращает (Pydantic-модель, режим Structured Outputs, TokenUsage).
    Поддерживает как текстовый разбор Schedule A/Deed, так и мультимодальный
    Vision-разбор отрендеренных страниц PDF (image_data_urls), а также инъекцию
    фокусного задания (focus_instruction) для повторной проверки спорных мест.
    """
    api_cfg = cfg["api"]
    target = resolve_api_target(
        cfg, provider=provider, model=model, api_key=api_key, key_secret=key_secret
    )
    model_name = target["model"]
    provider_id = target["provider"]
    provider_label = target["provider_label"]
    temperature = api_cfg.get("temperature", 0.0)
    client = _make_client(api_cfg, target=target)
    messages = _messages(
        text,
        image_data_urls=image_data_urls,
        focus_instruction=focus_instruction,
    )

    if progress:
        mode_tag = f" (Vision, {len(image_data_urls)} стр.)" if image_data_urls else ""
        progress(f"Запрос к {provider_label}: {model_name}{mode_tag} …")

    errors: list[str] = []
    prompt_acc = 0
    completion_acc = 0
    total_acc = 0
    saw_usage = False

    def record_usage(completion_obj: Any) -> None:
        nonlocal prompt_acc, completion_acc, total_acc, saw_usage
        counts = _extract_usage_counts(completion_obj)
        if counts is not None:
            saw_usage = True
            prompt_acc += counts[0]
            completion_acc += counts[1]
            total_acc += counts[2]

    def build_usage() -> TokenUsage:
        return TokenUsage(
            prompt_tokens=prompt_acc,
            completion_tokens=completion_acc,
            total_tokens=total_acc,
            provider=provider_id,
            provider_label=provider_label,
            model=model_name,
            available=saw_usage,
        )

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
            record_usage(completion)
            message = completion.choices[0].message
            refusal = getattr(message, "refusal", None)
            if refusal:
                raise LLMParseError(f"модель отказалась отвечать: {refusal}")
            parsed = getattr(message, "parsed", None)
            if parsed is not None:
                return parsed, "structured-outputs", build_usage()
            raise LLMParseError("модель вернула пустой structured-ответ")
    except LLMParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise LLMParseError(
                f"{provider_label}: ошибка авторизации (проверьте API-ключ): {exc}"
            ) from exc
        errors.append(f"parse(): {exc}")

    # 2) Ручная JSON-схема (совместимые провайдеры: Hydra, OpenRouter, RouterAI…)
    schema = make_strict_schema(LegalDescription)
    try:
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
        record_usage(completion)
        content = completion.choices[0].message.content or ""
        return (
            LegalDescription.model_validate(_json_from_text(content)),
            "json_schema",
            build_usage(),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise LLMParseError(
                f"{provider_label}: ошибка авторизации (проверьте API-ключ): {exc}"
            ) from exc
        errors.append(f"json_schema: {exc}")

    schema_hint = json.dumps(schema, ensure_ascii=False)
    guide = (
        "Return ONLY a JSON object that validates against this JSON Schema:\n"
        f"{schema_hint}"
    )
    guided_messages = _messages(
        text,
        image_data_urls=image_data_urls,
        system_override=SYSTEM_PROMPT + "\n\n" + guide,
    )

    # 3) Обычный JSON-mode + инструкция со схемой.
    try:
        completion = _call_with_temperature(
            client.chat.completions.create,
            model=model_name,
            messages=guided_messages,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        record_usage(completion)
        content = completion.choices[0].message.content or ""
        return (
            LegalDescription.model_validate(_json_from_text(content)),
            "json_object",
            build_usage(),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise LLMParseError(
                f"{provider_label}: ошибка авторизации (проверьте API-ключ): {exc}"
            ) from exc
        errors.append(f"json_object: {exc}")

    # 4) Запрос без response_format — для бесплатных/reasoning-моделей OpenRouter/Hydra,
    #    которые отклоняют параметр response_format целиком.
    try:
        completion = _call_with_temperature(
            client.chat.completions.create,
            model=model_name,
            messages=guided_messages,
            temperature=temperature,
        )
        record_usage(completion)
        content = completion.choices[0].message.content or ""
        return (
            LegalDescription.model_validate(_json_from_text(content)),
            "prompt-json",
            build_usage(),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise LLMParseError(
                f"{provider_label}: ошибка авторизации (проверьте API-ключ): {exc}"
            ) from exc
        errors.append(f"prompt-json: {exc}")

    detail = " | ".join(errors[-3:])
    vision_hint = (
        " (для PDF без текстового слоя убедитесь, что выбрана Vision-модель: "
        "например hydra-gemini-pro, nvidia/nemotron-3.5-super-vl:free или openai/gpt-4.1)"
        if image_data_urls
        else ""
    )
    raise LLMParseError(
        f"не удалось получить корректный JSON от {provider_label} ({model_name}){vision_hint}: {detail}"
    )


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
    """Приводит типографику к единому виду (градусы, минуты, секунды, дроби дюймов)."""
    text = _replace_vulgar_fractions(text)
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
    """
    Читает длину стороны: сначала проверяет связку «13 feet 8 inches» / «25'-6 1/2"»,
    затем явную пометку ``(DEED)``, затем обычное число в футах.
    """
    fi = _FEET_INCHES_STR_RE.search(chunk)
    deed_match = re.search(
        r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?:'|′|ft\b\.?|feet\b|foot\b)\s*\(\s*DEED\s*\)",
        chunk,
        re.IGNORECASE,
    )
    plain = _DISTANCE_RE.search(chunk)
    if fi and (not plain or fi.start() <= plain.start()):
        return _feet_inches_match_to_ft(fi)
    if deed_match and plain and deed_match.start() <= plain.start() + 30:
        return float(deed_match.group("num").replace(",", ""))
    if plain:
        return float(plain.group("num").replace(",", ""))
    return None


def _has_min_courses(body: str, minimum: int = 3) -> bool:
    if len(_bearing_matches(body)) >= minimum:
        return True
    return len(re.findall(r"\bTHENCE\b", body, flags=re.IGNORECASE)) >= minimum


def _split_preamble_and_body(text: str) -> tuple[str, str]:
    """
    Отделяет многозвенную привязку ``COMMENCING AT … TO THE POINT OR PLACE OF BEGINNING``
    (или ``distant the following … courses and distances … RUNNING THENCE``) от
    собственно обхода границ участка, чтобы курсы привязки к углу квартала не
    попадали в ``segments``.

    Важно: в некоторых техасских deed'ах сам обход участка начинается со слова
    ``COMMENCING at a point …`` и заканчивается единственным ``to the POINT OF BEGINNING``
    в самом конце документа — поэтому отрезаем преамбулу ТОЛЬКО если после первого
    ``BEGINNING`` действительно остаётся минимум 3 курса обхода участка.
    """
    commencing = re.search(
        r"\bCOMMENCING\b.*?\bto\s+(?:the\s+)?(?:true\s+)?"
        r"(?:point(?:\s+or\s+place)?\s+of\s+|place\s+of\s+)?BEGINNING\b[;,.:\s]*",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if commencing and _has_min_courses(text[commencing.end() :]):
        return text[: commencing.end()], text[commencing.end() :]

    multi_tie = re.search(
        r"\b(?:distant|measured)\s+(?:the\s+)?following\s+(?:\w+\s+)?courses?\s+"
        r"and\s+distances?\b.*?(?=\bRUNNING\s+THENCE\b)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if multi_tie and _has_min_courses(text[multi_tie.end() :]):
        return text[: multi_tie.end()], text[multi_tie.end() :]

    pob = re.search(r"\bBEGINNING\b", text, re.IGNORECASE)
    if pob:
        head = text[: pob.start()]
        tail = text[pob.start() :]
        if _bearing_matches(head) and _has_min_courses(tail):
            return head, tail
    return "", text


def _bearing_matches(text: str) -> list[re.Match[str]]:
    """Сначала румбы с символами °/', затем десятичные, затем «N 45 03 12 E»."""
    for pattern in (_DMS_BEARING_RE, _DECIMAL_BEARING_RE, _SPACED_BEARING_RE):
        matches = list(pattern.finditer(text))
        if matches:
            return matches
    return []


def _azimuth_to_quadrant_dict(azimuth_deg: float) -> dict[str, Any]:
    """Переводит азимут 0..360° от севера по часовой стрелке в поля Segment."""
    az = azimuth_deg % 360.0
    if az <= 90.0:
        ns, ew, angle = "N", "E", az
    elif az <= 180.0:
        ns, ew, angle = "S", "E", 180.0 - az
    elif az < 270.0:
        ns, ew, angle = "S", "W", az - 180.0
    else:
        ns, ew, angle = "N", "W", 360.0 - az
    deg = int(angle + 1e-9)
    rem_min = max(0.0, (angle - deg) * 60.0)
    minutes = int(rem_min + 1e-9)
    seconds = round(max(0.0, (rem_min - minutes) * 60.0), 2)
    deg, minutes, seconds = _carry_dms(deg, minutes, seconds)
    return {
        "ns": ns,
        "degrees": deg,
        "minutes": minutes,
        "seconds": seconds,
        "ew": ew,
    }


_CARDINAL_AZIMUTH: dict[str, float] = {
    "north": 0.0,
    "northerly": 0.0,
    "northeast": 45.0,
    "northeasterly": 45.0,
    "east": 90.0,
    "easterly": 90.0,
    "southeast": 135.0,
    "southeasterly": 135.0,
    "south": 180.0,
    "southerly": 180.0,
    "southwest": 225.0,
    "southwesterly": 225.0,
    "west": 270.0,
    "westerly": 270.0,
    "northwest": 315.0,
    "northwesterly": 315.0,
}

_INTERIOR_ANGLE_RE = re.compile(
    r"interior\s+angle\s+of\s+"
    r"(?P<deg>\d{1,3})\s*(?:°|deg(?:rees?)?\.?)\s*"
    r"(?:(?P<min>\d{1,2})\s*(?:'|min(?:utes?)?\.?)\s*)?"
    r"(?:(?P<sec>\d{1,2}(?:\.\d+)?)\s*(?:\"|''|sec(?:onds?)?\.?))?",
    re.IGNORECASE,
)


def _extract_course_note(clause: str) -> str | None:
    parallel = re.search(
        r"\bparallel\s+with\s+(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?"
        r"(?P<target>[A-Za-z0-9 .'-]+?)(?:\s*\((?P<paren>[^)]+)\))?(?=\s*(?:,|;|\b\d|\bfeet\b|\bft\b))",
        clause,
        re.IGNORECASE,
    )
    if parallel:
        label = (parallel.group("paren") or parallel.group("target") or "").strip(" ,;.")
        if label:
            return f"PARALLEL WITH {label.upper()}"
    right_ang = re.search(
        r"\b(?:at\s+right\s+angles\s+to|perpendicular\s+to)\s+"
        r"(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?"
        r"(?P<target>[A-Za-z0-9 .'-]+?)(?:\s*\((?P<paren>[^)]+)\))?(?=\s*(?:,|;|\b\d|\bfeet\b|\bft\b))",
        clause,
        re.IGNORECASE,
    )
    if right_ang:
        label = (right_ang.group("paren") or right_ang.group("target") or "").strip(" ,;.")
        if label:
            return f"AT RIGHT ANGLES TO {label.upper()}"
    return None


def _segments_from_relative_nyc(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Разбирает описания участков Нью-Йорка (Queens / Brooklyn / Bronx / Manhattan)
    без компасных румбов — на кардинальных направлениях вдоль улиц, ``PARALLEL WITH``,
    ``AT RIGHT ANGLES TO`` и внутренних углах (``interior angle of 93°33'29.5"``).
    """
    parts = re.split(r"\b(?:RUNNING\s+)?THENCE\b", text, flags=re.IGNORECASE)
    if len(parts) < 4:
        return [], []
    clauses = parts[1:]

    parsed_clauses: list[dict[str, Any]] = []
    for clause in clauses:
        dist = _distance_after(clause)
        if dist is None:
            continue
        dir_match = re.search(
            r"\b(north(?:east|west)?(?:erly)?|south(?:east|west)?(?:erly)?|"
            r"east(?:erly)?|west(?:erly)?)\b",
            clause,
            re.IGNORECASE,
        )
        if not dir_match:
            continue
        nominal_az = _CARDINAL_AZIMUTH[dir_match.group(1).lower()]
        ia_match = _INTERIOR_ANGLE_RE.search(clause)
        ia_deg: float | None = None
        ia_label: str | None = None
        if ia_match:
            d = int(ia_match.group("deg"))
            m = int(ia_match.group("min") or 0)
            s = float(ia_match.group("sec") or 0.0)
            ia_deg = dms_to_degrees(d, m, s)
            sec_str = _fmt_seconds(s)
            ia_label = f'{d}°{m:02d}\'{sec_str}"' if s > 0 else f"{d}°{m:02d}'"
        parsed_clauses.append(
            {
                "clause": clause,
                "nominal_az": nominal_az,
                "distance_ft": dist,
                "ia_deg": ia_deg,
                "ia_label": ia_label,
                "right_angle": bool(
                    re.search(r"\b(?:at\s+right\s+angles|perpendicular)\b", clause, re.IGNORECASE)
                ),
                "course_note": _extract_course_note(clause),
            }
        )

    if len(parsed_clauses) < 3:
        return [], []

    # Замыкающий курс возвращается в POB вдоль базовой улицы; луч из POB внутрь
    # участка направлен противоположно замыкающему курсу (+180°).
    closing_az = parsed_clauses[-1]["nominal_az"]
    baseline_ray = (closing_az + 180.0) % 360.0
    street_azimuths: dict[str, float] = {}
    closing_along = re.search(
        r"\balong\s+(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?(?P<st>[^,;:(]+)",
        parsed_clauses[-1]["clause"],
        re.IGNORECASE,
    )
    if closing_along:
        st_name = (_street_from_block(closing_along.group("st")) or "").lower()
        if st_name:
            street_azimuths[st_name] = baseline_ray

    segments: list[dict[str, Any]] = []
    prev_az: float | None = None
    for idx, item in enumerate(parsed_clauses):
        nom = item["nominal_az"]
        clause_str = item["clause"]
        ref_street_az: float | None = None
        rel_target = re.search(
            r"\b(?:parallel\s+with|at\s+right\s+angles\s+to|perpendicular\s+to)\s+"
            r"(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?(?P<st>[^,;:(]+)",
            clause_str,
            re.IGNORECASE,
        )
        if rel_target:
            target_name = (_street_from_block(rel_target.group("st")) or "").lower()
            if target_name and target_name in street_azimuths:
                ref_street_az = street_azimuths[target_name]

        if item["ia_deg"] is not None:
            ref_ray = baseline_ray if idx == 0 or prev_az is None else (prev_az + 180.0) % 360.0
            cand_a = (ref_ray + item["ia_deg"]) % 360.0
            cand_b = (ref_ray - item["ia_deg"]) % 360.0
            az = cand_a if _angle_diff_deg(cand_a, nom) <= _angle_diff_deg(cand_b, nom) else cand_b
        elif item["right_angle"]:
            base_ref = ref_street_az if ref_street_az is not None else baseline_ray
            candidates_ra = [(base_ref + 90.0) % 360.0, (base_ref - 90.0) % 360.0]
            if ref_street_az is None and prev_az is not None:
                candidates_ra.extend([(prev_az + 90.0) % 360.0, (prev_az - 90.0) % 360.0])
            az = min(candidates_ra, key=lambda c: _angle_diff_deg(c, nom))
        elif ref_street_az is not None:
            cand_a = ref_street_az % 360.0
            cand_b = (ref_street_az + 180.0) % 360.0
            az = cand_a if _angle_diff_deg(cand_a, nom) <= _angle_diff_deg(cand_b, nom) else cand_b
        else:
            az = nom

        along_match = re.search(
            r"\balong\s+(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?(?P<st>[^,;:(]+)",
            clause_str,
            re.IGNORECASE,
        )
        if along_match:
            along_name = (_street_from_block(along_match.group("st")) or "").lower()
            if along_name:
                street_azimuths[along_name] = az

        prev_az = az
        seg = _azimuth_to_quadrant_dict(az)
        seg["distance_ft"] = item["distance_ft"]
        seg["has_bearing"] = False
        seg["course_note"] = item["course_note"]
        seg["interior_angle"] = item["ia_label"]
        segments.append(seg)

    return segments, []


def _segments_from_regex(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    matches = _bearing_matches(text)
    if not matches:
        rel_segments, rel_warnings = _segments_from_relative_nyc(text)
        if rel_segments:
            return rel_segments, rel_warnings
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
                "has_bearing": True,
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
        if head in {"the", "of", "a", "an", "with", "by", "intersection", "corner", "formed"} or head in _DIRECTION_WORDS or head in {
            "side", "line", "lines", "property", "said", "fronting", "along",
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
    "northeasterly", "northwesterly", "southeasterly", "southwesterly",
}

#: «fronting the South side of …», «along the North line of …», «fronts on …»
_FRONT_RE = re.compile(
    r"(?:fronting|fronts?|facing|faces|abuts?|abutting)\s+"
    r"(?:(?:the|a|an)\s+)?(?:(?:[A-Za-z]+ly|[A-Za-z]+)\s+)?"
    r"(?:side|line|lines|right[- ]of[- ]way)?\s*"
    r"(?:of|on|to)?\s*"
    r"(?P<block>[^.;:]{0,80}?(?:%s|%s)[^.;:]{0,20})"
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
    """«fronts on the South side of» -> 'N' (направление наружу к улице)."""
    match = re.search(
        r"\b(?:the\s+)?([A-Za-z]+)\s+(?:side|line|lines|right[- ]of[- ]way)\b",
        prefix,
        re.IGNORECASE,
    )
    if not match:
        return None
    word = match.group(1).lower()
    mapping = {
        "north": "S",
        "northerly": "S",
        "south": "N",
        "southerly": "N",
        "east": "W",
        "easterly": "W",
        "west": "E",
        "westerly": "E",
        "northwest": "SE",
        "northwesterly": "SE",
        "northeast": "SW",
        "northeasterly": "SW",
        "southwest": "NE",
        "southwesterly": "NE",
        "southeast": "NW",
        "southeasterly": "NW",
    }
    return mapping.get(word)


def _extract_nyc_metadata(
    full_text: str,
    lot_body: str,
    main_street: str,
    cross_street: str | None,
    distance_to_corner: float | None,
    raw_segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Извлекает Block/Lot/Borough, примечания к улице и индексы примыкающих сторон."""
    meta: dict[str, Any] = {
        "main_street_note": None,
        "tie_note": None,
        "front_segment_index": None,
        "cross_street_side_index": None,
        "block_number": None,
        "lot_number": None,
        "borough_or_city": None,
    }
    blk = re.search(r"\bBlock\s*#?\s*(\d+[A-Za-z-]*)\b", full_text, re.IGNORECASE)
    if blk:
        meta["block_number"] = blk.group(1)
    lot = re.search(r"\bLot\s*#?\s*(\d+[A-Za-z-]*)\b", full_text, re.IGNORECASE)
    if lot:
        meta["lot_number"] = lot.group(1)
    boro = re.search(
        r"\bBorough\s+(?:and\s+County\s+)?of\s+"
        r"(Staten\s+Island|Queens|Brooklyn|Manhattan|Bronx|The\s+Bronx)\b",
        full_text,
        re.IGNORECASE,
    )
    if boro:
        meta["borough_or_city"] = f"{boro.group(1).strip()}, NY"

    if main_street:
        note_re = re.compile(
            re.escape(main_street) + r"\s*(\([^)]{3,80}\))",
            re.IGNORECASE,
        )
        nm = note_re.search(full_text)
        if nm:
            meta["main_street_note"] = nm.group(1).strip().upper()

    is_corner = distance_to_corner is not None and distance_to_corner <= 1e-6
    clauses = re.split(r"\b(?:RUNNING\s+)?THENCE\b", lot_body, flags=re.IGNORECASE)[1:]
    if len(clauses) == len(raw_segments):
        for idx, clause in enumerate(clauses):
            if (
                main_street
                and re.search(
                    r"\balong\b[^.;]{0,50}?" + re.escape(main_street),
                    clause,
                    re.IGNORECASE,
                )
                and meta["front_segment_index"] is None
            ):
                meta["front_segment_index"] = idx
            if (
                is_corner
                and cross_street
                and re.search(
                    r"\balong\b[^.;]{0,50}?" + re.escape(cross_street),
                    clause,
                    re.IGNORECASE,
                )
                and meta["cross_street_side_index"] is None
            ):
                meta["cross_street_side_index"] = idx

    return meta


def _extract_streets(
    text: str,
    *,
    preamble: str = "",
) -> tuple[
    str,
    str | None,
    float | None,
    "Literal['N','S','E','W'] | None",
    str | None,
    list[str],
]:
    """Возвращает (main_street, cross_street, distance_to_corner, front_side, tie_note, warnings)."""
    warnings: list[str] = []
    main_street = ""
    cross_street: str | None = None
    distance_to_corner: float | None = None
    front_side: str | None = None
    tie_note: str | None = None
    body = text[len(preamble) :] if preamble and text.startswith(preamble) else text

    # 0. Угловой участок NYC: «BEGINNING at the corner formed by the intersection of
    #    the northerly side of Borden Avenue ... with the easterly side of 63rd Street».
    corner_pob = re.search(
        r"\bBEGINNING\s+at\s+(?:a\s+point\s+at\s+|the\s+)?corner\s+formed\s+by\s+"
        r"the\s+intersection\s+of\s+(?P<first>[^;]+?)\s+(?:with|and)\s+(?P<second>[^;:]+)",
        body,
        re.IGNORECASE,
    )
    if corner_pob:
        s1 = _street_from_block(corner_pob.group("first"))
        s2 = _street_from_block(corner_pob.group("second"))
        if s1:
            main_street = s1
            front_side = _front_direction(corner_pob.group("first"))
        if s2 and s2.lower() != main_street.lower():
            cross_street = s2
        distance_to_corner = 0.0

    # 1. Явная фраза «fronting / fronts on …» (сначала в теле участка, затем во всём тексте).
    if not main_street:
        fronting = _FRONT_RE.search(body) or _FRONT_RE.search(text)
        if fronting:
            src = body if fronting.string is body else text
            block = fronting.group("block")
            prefix = src[fronting.start() : fronting.start("block")] + block
            front_side = _front_direction(prefix)
            main_street = _street_from_block(block) or ""

    # 2. Конец привязки COMMENCING: «... to (a point on) the northwesterly side of Dartmouth Loop ...,
    #    the POINT OR PLACE OF BEGINNING».
    if not main_street and preamble:
        to_pob_street = re.search(
            r"\bto\s+(?:a\s+point\s+on\s+)?(?P<prefix>(?:the\s+)?[A-Za-z]+\s+"
            r"(?:side|line|right[- ]of[- ]way)\s+of\s+)(?P<block>[^,;:]{0,80})",
            preamble,
            re.IGNORECASE,
        )
        if to_pob_street:
            front_side = _front_direction(to_pob_street.group("prefix"))
            main_street = _street_from_block(to_pob_street.group("block")) or ""

    # 3. «BEGINNING at a point on the northwesterly side of Dartmouth Loop …»
    if not main_street:
        pob_on = re.search(
            r"\bBEGINNING\s+at\s+a\s+point\s+on\s+(?P<prefix>(?:the\s+)?[A-Za-z]+\s+"
            r"(?:side|line|right[- ]of[- ]way)\s+of\s+)(?P<block>[^,;:]{0,80})",
            text,
            re.IGNORECASE,
        )
        if pob_on:
            front_side = _front_direction(pob_on.group("prefix"))
            main_street = _street_from_block(pob_on.group("block")) or ""

    # 4. В теле участка: «along (the ... side of) <Street>».
    if not main_street:
        along_body = re.search(
            r"\balong\s+(?P<prefix>(?:(?:the\s+)?(?:[A-Za-z]+\s+)?(?:side|line|right[- ]of[- ]way)\s+of\s+)?)"
            r"(?P<block>[^,;:]{0,80})",
            body,
            re.IGNORECASE,
        )
        if along_body:
            cand = _street_from_block(along_body.group("block"))
            if cand:
                front_side = front_side or _front_direction(along_body.group("prefix"))
                main_street = cand

    # 5. Общий случай: «... North line of Main Street» / первое упоминание улицы (сначала в body).
    if not main_street:
        side_of = re.search(
            r"(?:side|line|lines|right[- ]of[- ]way)\s+(?:of|at)\s+(?:the\s+)?(?P<block>[^.;:]{0,80})",
            body,
            re.IGNORECASE,
        ) or re.search(
            r"(?:side|line|lines|right[- ]of[- ]way)\s+(?:of|at)\s+(?:the\s+)?(?P<block>[^.;:]{0,80})",
            text,
            re.IGNORECASE,
        )
        if side_of:
            main_street = _street_from_block(side_of.group("block")) or ""
    if not main_street:
        mentions = _street_mentions(body) or _street_mentions(text)
        if mentions:
            main_street = mentions[0][1]

    # 6. Пересекаемая улица: «... feet East of the East line of Oak Avenue» или угол из COMMENCING.
    _DIR_WORDS = (
        r"(?:Northeasterly|Northwesterly|Southeasterly|Southwesterly|"
        r"Northeast|Northwest|Southeast|Southwest|"
        r"Northerly|Southerly|Easterly|Westerly|"
        r"Northward|Southward|Eastward|Westward|"
        r"East|West|North|South)"
    )
    if not cross_street:
        cross_patterns = [
            r"(?:feet|ft|')\s*" + _DIR_WORDS + r"?\s*"
            r"(?:of|from)\s+the\s+(?:[A-Za-z]+\s+){0,2}?"
            r"(?:line|side|right[- ]of[- ]way|centerline|corner)\s+of\s+(?P<block>[^.;:]{0,80})",
            r"\bintersection\s+of\s+(?:the\s+(?:[A-Za-z]+\s+){0,2}?side\s+of\s+)?(?P<block>[^.;:]{0,80})",
            r"\bintersection\s+of\s+[^.;:]{1,95}?\b(?:with|and)\s+"
            r"(?:the\s+(?:[A-Za-z]+\s+){0,2}?(?:side|line|right[- ]of[- ]way)\s+of\s+)?(?P<block>[^.;:,]{1,80})",
            r"(?:line|side|right[- ]of[- ]way)\s+of\s+(?P<block>[^.;:]{0,80})",
        ]
        for pattern in cross_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                candidate = _street_from_block(match.group("block"))
                if candidate and candidate.lower() != main_street.lower():
                    cross_street = candidate
                    break
            if cross_street:
                break

    # 7. Расстояние до угла: обычное («150.00 feet East of ...») или последнее звено привязки COMMENCING.
    if distance_to_corner is None:
        distance_match = re.search(
            r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?:feet|ft|')"
            r"(?:\s*(?P<inch>\d+(?:\.\d+)?)\s*(?:inches|inch|in|\"))?\s*"
            r"(?:\([^)]*\)\s*)?"
            + _DIR_WORDS
            + r"?\s*(?:of|from)\s+the\s+",
            text,
            re.IGNORECASE,
        )
        if distance_match:
            feet_val = float(distance_match.group("num").replace(",", ""))
            inch_raw = distance_match.group("inch")
            inch_val = float(inch_raw) / 12.0 if inch_raw else 0.0
            distance_to_corner = feet_val + inch_val
        elif preamble:
            tie_dists = list(_DISTANCE_RE.finditer(preamble))
            if tie_dists:
                last_tie = float(tie_dists[-1].group("num").replace(",", ""))
                distance_to_corner = last_tie
                deed_vs_tm = re.search(
                    r"(\d[\d,]*(?:\.\d+)?)\s*(?:feet|ft|')\s*\(\s*DEED\s*\)"
                    r"[^.;]{0,30}?(\d[\d,]*(?:\.\d+)?)\s*(?:feet|ft|')?\s*\(\s*T\.?M\.?\s*\)",
                    preamble,
                    re.IGNORECASE,
                )
                if deed_vs_tm:
                    distance_to_corner = float(deed_vs_tm.group(1).replace(",", ""))
                    tie_note = f"{distance_to_corner:.2f}' (DEED) / {float(deed_vs_tm.group(2).replace(',', '')):.2f}' (T.M.)"

    if not main_street:
        warnings.append("фасадная улица не распознана — подпись улицы не будет нанесена")
    if cross_street and distance_to_corner is None:
        warnings.append("пересекаемая улица найдена, но расстояние до угла не распознано")
    return main_street, cross_street, distance_to_corner, front_side, tie_note, warnings


def _extract_pob_tie_hint(
    *,
    preamble: str,
    lot_body: str,
    distance_to_corner: float | None,
) -> tuple[str | None, Segment | None]:
    """
    Извлекает из преамбулы / первого абзаца до первого THENCE:
      * словесное направление от перекрёстка к POB ('easterly', 'West', 'northeasterly', ...);
      * явный румб последнего звена COMMENCING к POB (если указан в градусах).
    """
    if distance_to_corner is None or distance_to_corner <= 1e-6:
        return None, None

    explicit_seg: Segment | None = None
    if preamble:
        prem_segs, _ = _segments_from_regex(preamble)
        if prem_segs:
            last_raw = prem_segs[-1]
            if last_raw.get("has_bearing", True) and abs(
                float(last_raw.get("distance_ft", 0.0)) - float(distance_to_corner)
            ) <= max(1.0, 0.02 * float(distance_to_corner)):
                try:
                    explicit_seg = Segment.model_validate(last_raw)
                except Exception:  # noqa: BLE001
                    explicit_seg = None

    lot_intro = re.split(
        r"\b(?:RUNNING\s+)?THENCE\b", lot_body, maxsplit=1, flags=re.IGNORECASE
    )[0]
    header_text = f"{preamble} {lot_intro}".strip()

    dir_re_after_dist = re.compile(
        r"\d[\d,]*(?:\.\d+)?\s*(?:feet|ft|')"
        r"(?:\s*\d+(?:\.\d+)?\s*(?:inches|inch|in|\"))?\s*"
        r"(?:\([^)]*\)\s*)?"
        r"(?P<dir>northeasterly|northwesterly|southeasterly|southwesterly|"
        r"northeast|northwest|southeast|southwest|"
        r"northerly|southerly|easterly|westerly|"
        r"northward|southward|eastward|westward|"
        r"north|south|east|west)\s+"
        r"(?:along\s+[^,;]{1,60}?\s+)?(?:of|from)\b",
        re.IGNORECASE,
    )
    dir_re_before_dist = re.compile(
        r"\b(?P<dir>northeasterly|northwesterly|southeasterly|southwesterly|"
        r"northerly|southerly|easterly|westerly)\s+"
        r"(?:along\s+[^,;]{1,60}?\s+)?"
        r"(?:a\s+distance\s+of\s+|distant\s+)?"
        r"\d[\d,]*(?:\.\d+)?\s*(?:feet|ft|')\s+(?:of|from)\b",
        re.IGNORECASE,
    )
    m = dir_re_after_dist.search(header_text) or dir_re_before_dist.search(header_text)
    pob_dir = m.group("dir").upper() if m else None
    return pob_dir, explicit_seg


def parse_with_regex(text: str) -> tuple[LegalDescription, list[str]]:
    """
    Локальный разбор без LLM. Возвращает (модель, список предупреждений).
    Поддерживает:
      * классические румбы DMS / десятичные градусы;
      * футы с дюймами («13 feet 8 inches»);
      * отсечение многозвенной привязки «COMMENCING AT … TO POB»;
      * относительные курсы Нью-Йорка («PARALLEL WITH …», «AT RIGHT ANGLES TO …»,
        «interior angle of 93°33'29.5"») без компасных румбов.
    """
    normalized = _normalize_legal_text(text)
    preamble, lot_body = _split_preamble_and_body(normalized)
    raw_segments, warnings = _segments_from_regex(lot_body)
    if not raw_segments:
        raise LegalDescriptionError(
            "локальный парсер не нашёл сторон участка: "
            + ("; ".join(warnings) or "текст не распознан")
        )
    (
        main_street,
        cross_street,
        distance_to_corner,
        front_side,
        tie_note,
        street_warnings,
    ) = _extract_streets(normalized, preamble=preamble)
    warnings.extend(street_warnings)

    meta = _extract_nyc_metadata(
        normalized,
        lot_body,
        main_street,
        cross_street,
        distance_to_corner,
        raw_segments,
    )
    if tie_note and not meta.get("tie_note"):
        meta["tie_note"] = tie_note

    pob_dir, explicit_tie = _extract_pob_tie_hint(
        preamble=preamble,
        lot_body=lot_body,
        distance_to_corner=distance_to_corner,
    )
    validated_segs = [Segment.model_validate(s) for s in raw_segments]
    validated_segs, _, typo_notes = _detect_and_repair_obvious_typos(
        validated_segs,
        front_segment_index=meta["front_segment_index"],
        cross_street_side_index=meta["cross_street_side_index"],
        from_vision=False,
    )
    warnings.extend(typo_notes)
    corner_tie = infer_corner_to_pob_bearing(
        validated_segs,
        distance_to_corner_ft=distance_to_corner,
        front_segment_index=meta["front_segment_index"],
        front_side=front_side,
        pob_direction=pob_dir,
        explicit_tie_segment=explicit_tie,
    )

    try:
        model = LegalDescription.model_validate(
            {
                "main_street": main_street,
                "main_street_note": meta["main_street_note"],
                "cross_street": cross_street,
                "distance_to_corner_ft": distance_to_corner,
                "corner_to_pob_bearing": corner_tie,
                "tie_note": meta["tie_note"],
                "front_side": front_side,
                "front_segment_index": meta["front_segment_index"],
                "cross_street_side_index": meta["cross_street_side_index"],
                "block_number": meta["block_number"],
                "lot_number": meta["lot_number"],
                "borough_or_city": meta["borough_or_city"],
                "segments": validated_segs,
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise LegalDescriptionError(
            f"локальный парсер вернул некорректные данные: {exc}"
        ) from exc
    return model, warnings


# ---------------------------------------------------------------------------
# Эталонные реальные примеры NYC Title Survey (для кнопки «Пример» и тестов)
# ---------------------------------------------------------------------------

ASPENWALL_SAMPLES: dict[str, str] = {
    "Aspenwall St — 420' (Основной участок)": (
        "ALL that certain plot, piece or parcel of land, situate, lying and being "
        "in the Borough and County of Queens, City and State of New York, "
        "known as Block 5012 Lot 28:\n"
        "BEGINNING at a point on the southerly side of Aspenwall Street, distant 420.00 feet "
        "easterly from the corner formed by the intersection of the southerly side of "
        "Aspenwall Street with the easterly side of 149th Street;\n"
        "RUNNING THENCE S 14°39'40\" E, 100.00 feet;\n"
        "THENCE N 75°20'20\" E, 55.00 feet;\n"
        "THENCE N 14°39'40\" W, 100.00 feet to the southerly side of Aspenwall Street;\n"
        "THENCE S 75°20'20\" W along the southerly side of Aspenwall Street, 55.00 feet "
        "to the point or place of BEGINNING."
    ),
    "Aspenwall St — 365' (Сосед слева)": (
        "ALL that certain plot, piece or parcel of land, situate, lying and being "
        "in the Borough and County of Queens, City and State of New York, "
        "known as Block 5012 Lot 26:\n"
        "BEGINNING at a point on the southerly side of Aspenwall Street, distant 365.00 feet "
        "easterly from the corner formed by the intersection of the southerly side of "
        "Aspenwall Street with the easterly side of 149th Street;\n"
        "RUNNING THENCE S 14°39'40\" E, 100.00 feet;\n"
        "THENCE N 75°20'20\" E, 55.00 feet;\n"
        "THENCE N 14°39'40\" W, 100.00 feet to the southerly side of Aspenwall Street;\n"
        "THENCE S 75°20'20\" W along the southerly side of Aspenwall Street, 55.00 feet "
        "to the point or place of BEGINNING."
    ),
    "Aspenwall St — 475' (Сосед справа)": (
        "ALL that certain plot, piece or parcel of land, situate, lying and being "
        "in the Borough and County of Queens, City and State of New York, "
        "known as Block 5012 Lot 30:\n"
        "BEGINNING at a point on the southerly side of Aspenwall Street, distant 475.00 feet "
        "easterly from the corner formed by the intersection of the southerly side of "
        "Aspenwall Street with the easterly side of 149th Street;\n"
        "RUNNING THENCE S 14°39'40\" E, 100.00 feet;\n"
        "THENCE N 75°20'20\" E, 55.00 feet;\n"
        "THENCE N 14°39'40\" W, 100.00 feet to the southerly side of Aspenwall Street;\n"
        "THENCE S 75°20'20\" W along the southerly side of Aspenwall Street, 55.00 feet "
        "to the point or place of BEGINNING."
    ),
}

NYC_SAMPLES: dict[str, str] = {
    "147 Dartmouth Loop (Staten Island)": (
        "ALL that certain plot, piece or parcel of land, situate, lying and being "
        "in the Borough of Staten Island, County of Richmond, City and State of New York, "
        "known as Block 3960 Lot 129:\n"
        "COMMENCING at the corner formed by the intersection of the northeasterly side of "
        "Mill Road (80' adopted) with the northwesterly side of New Dorp Lane; "
        "THENCE N 34°55'50\" E along Mill Road 510.52 feet; "
        "THENCE N 55°04'10\" W 433.78 feet; "
        "THENCE S 33°58'44\" W 208.02 feet (DEED) / 208.22 feet (T.M.) to a point on the "
        "northwesterly side of Dartmouth Loop (38' wide roadway easement), the POINT OR PLACE OF BEGINNING;\n"
        "RUNNING THENCE S 33°58'44\" W along Dartmouth Loop, 14.00 feet;\n"
        "THENCE N 56°01'16\" W part of the distance through a party wall, 125.00 feet;\n"
        "THENCE N 33°58'44\" E, 14.00 feet;\n"
        "THENCE S 56°01'16\" E part of the distance through a party wall, 125.00 feet "
        "to the northwesterly side of Dartmouth Loop, the POINT OR PLACE OF BEGINNING."
    ),
    "283 Jamie Lane (Staten Island)": (
        "ALL that certain plot, piece or parcel of land, situate, lying and being "
        "in the Borough of Staten Island, County of Richmond, City and State of New York, "
        "known as Block 5744 Lot 112:\n"
        "COMMENCING at the monumented intersection of Kenilworth Avenue and Arthur Kill Road (80' adopted); "
        "THENCE S 73°57'40\" W 101.42 feet; "
        "THENCE S 09°22'42\" E 17.49 feet; "
        "THENCE S 04°43'12\" W 1,542.23 feet to the westerly side of Jamie Lane "
        "(Not Mapped on Final City Plan), the POINT OR PLACE OF BEGINNING;\n"
        "RUNNING THENCE S 07°13'39\" E along Jamie Lane, 13 feet 8 inches;\n"
        "THENCE S 82°46'21\" W, 25 feet 10 inches;\n"
        "THENCE N 07°13'39\" W, 13 feet 8 inches;\n"
        "THENCE N 82°46'21\" E, 25 feet 10 inches to the POINT OR PLACE OF BEGINNING."
    ),
    "63-01 Borden Ave (Queens — без румбов)": (
        "ALL that certain plot, piece or parcel of land, situate, lying and being "
        "in the Borough and County of Queens, City and State of New York, "
        "known as Block 2366 Lot 59:\n"
        "BEGINNING at the corner formed by the intersection of the northerly side of "
        "Borden Avenue (Queens Midtown Expressway) with the easterly side of 63rd Street (60' wide);\n"
        "RUNNING THENCE northerly along the easterly side of 63rd Street, 90.17 feet, "
        "forming an interior angle of 93°33'29.5\" with the northerly side of Borden Avenue;\n"
        "THENCE easterly parallel with Borden Avenue (Queens Midtown Expressway), 26.32 feet;\n"
        "THENCE southerly at right angles to Borden Avenue (Queens Midtown Expressway), 90.00 feet;\n"
        "THENCE westerly along the northerly side of Borden Avenue, 20.72 feet "
        "to the corner, the point or place of BEGINNING."
    ),
}


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


@dataclass
class ParseOutcome:
    """Результат разбора + метаданные для GUI/логов."""

    data: LegalDescription
    source: str
    warnings: list[str] = field(default_factory=list)
    usage: TokenUsage | None = None
    provider: str = ""
    provider_label: str = ""
    model: str = ""
    llm_mode: str = ""


def _flip_segment_direction(seg: Segment) -> Segment:
    ns_flipped: Literal["N", "S"] = "S" if seg.ns == "N" else "N"
    ew_flipped: Literal["E", "W"] = "W" if seg.ew == "E" else "E"
    return Segment(
        ns=ns_flipped,
        degrees=seg.degrees,
        minutes=seg.minutes,
        seconds=seg.seconds,
        ew=ew_flipped,
        distance_ft=seg.distance_ft,
        has_bearing=seg.has_bearing,
        course_note=seg.course_note,
        interior_angle=seg.interior_angle,
    )


def _proper_edge_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    def ccw_turn(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]) -> float:
        return (p2[0] - p1[0]) * (p3[1] - p1[1]) - (p2[1] - p1[1]) * (p3[0] - p1[0])

    d1 = ccw_turn(a1, a2, b1)
    d2 = ccw_turn(a1, a2, b2)
    d3 = ccw_turn(b1, b2, a1)
    d4 = ccw_turn(b1, b2, a2)
    return (d1 * d2 < -1e-6) and (d3 * d4 < -1e-6)


def _trace_closure_and_validity(segments: Sequence[Segment]) -> tuple[float, bool]:
    x, y = 0.0, 0.0
    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    for seg in segments:
        dx, dy = seg.vector
        x += dx
        y += dy
        pts.append((x, y))
    err = math.hypot(x, y)
    verts = pts[:-1]
    n = len(verts)
    area2 = 0.0
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
    if abs(area2) < 2.0:
        return err, False
    for i in range(n):
        a1, a2 = verts[i], verts[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            b1, b2 = verts[j], verts[(j + 1) % n]
            if _proper_edge_intersect(a1, a2, b1, b2):
                return err, False
    return err, True


_LOOKALIKE_DIGIT_PAIRS: set[frozenset[str]] = {
    frozenset(("3", "8")),
    frozenset(("8", "0")),
    frozenset(("1", "7")),
    frozenset(("5", "6")),
    frozenset(("0", "6")),
    frozenset(("0", "9")),
    frozenset(("6", "9")),
    frozenset(("1", "4")),
    frozenset(("2", "7")),
    frozenset(("2", "5")),
    frozenset(("3", "5")),
    frozenset(("5", "9")),
    frozenset(("6", "8")),
    frozenset(("8", "9")),
}


def _single_digit_diff(a_str: str, b_str: str) -> tuple[str, str] | None:
    """Возвращает пару отличающихся цифр (old_d, new_d), если строки отличаются ровно в 1 цифре."""
    if len(a_str) != len(b_str):
        return None
    diffs: list[tuple[str, str]] = []
    for ca, cb in zip(a_str, b_str):
        if ca != cb:
            if not (ca.isdigit() and cb.isdigit()):
                return None
            diffs.append((ca, cb))
            if len(diffs) > 1:
                return None
    return diffs[0] if len(diffs) == 1 else None


def _is_lookalike_digit_pair(da: str, db: str) -> bool:
    return frozenset((da, db)) in _LOOKALIKE_DIGIT_PAIRS


def _mutate_segment_bearing(
    seg: Segment,
    *,
    ns: Literal["N", "S"] | None = None,
    degrees: int | None = None,
    minutes: int | None = None,
    seconds: float | None = None,
    ew: Literal["E", "W"] | None = None,
) -> Segment | None:
    d_val = seg.degrees if degrees is None else degrees
    m_val = seg.minutes if minutes is None else minutes
    s_val = seg.seconds if seconds is None else seconds
    if d_val == 90 and (m_val > 0 or s_val > 1e-6):
        return None
    if not (0 <= d_val <= 90 and 0 <= m_val <= 59 and 0.0 <= s_val < 60.0):
        return None
    return Segment(
        ns=seg.ns if ns is None else ns,
        degrees=d_val,
        minutes=m_val,
        seconds=s_val,
        ew=seg.ew if ew is None else ew,
        distance_ft=seg.distance_ft,
        has_bearing=seg.has_bearing,
        course_note=seg.course_note,
        interior_angle=seg.interior_angle,
    )


def _detect_and_repair_obvious_typos(
    segments: Sequence[Segment],
    *,
    front_segment_index: int | None = None,
    cross_street_side_index: int | None = None,
    from_vision: bool = False,
) -> tuple[list[Segment], dict[int, int] | None, list[str]]:
    """
    Интеллектуальный геодезический поиск очевидных опечаток текста / OCR:
      1) опечатка направления квадранта N↔S или E↔W (или разворот 180° на чертеже);
      2) опечатка в цифрах угла (градусы/минуты/секунды: 3↔8, 8↔0, 1↔7, 5↔6 и др.,
         включая сверку с параллельной/перпендикулярной противоположной стороной);
      3) опечатка похожей цифры (3↔8, 8↔0, 1↔7, 5↔6 и др.) в длине стороны (с защитой
         фасадных сторон вдоль улиц).
    Всегда возвращает подробное русскоязычное пояснение найденной и исправленной опечатки.
    Если опечатка неоднозначна — НЕ меняет данные молча.
    """
    segs = list(segments)
    n = len(segs)
    if not (3 <= n <= 10):
        return segs, None, []

    err0, _ = _trace_closure_and_validity(segs)
    if err0 <= 0.08:
        return segs, None, []

    protected_street = {
        idx for idx in (front_segment_index, cross_street_side_index) if idx is not None
    }

    def _corner_geometry_score(cand_segs: Sequence[Segment]) -> tuple[int, float]:
        """Число прямых углов (90° ± 0.05°) и минимальный sin угла между смежными сторонами."""
        ortho_cnt = 0
        min_sin = 1.0
        cnt = len(cand_segs)
        uvecs: list[tuple[float, float]] = []
        for cs in cand_segs:
            vx, vy = cs.vector
            vlen = math.hypot(vx, vy) or 1.0
            uvecs.append((vx / vlen, vy / vlen))
        for k_i in range(cnt):
            ax, ay = uvecs[k_i]
            bx, by = uvecs[(k_i + 1) % cnt]
            dot_ab = abs(ax * bx + ay * by)
            sin_ab = abs(ax * by - ay * bx)
            if dot_ab <= math.sin(math.radians(0.05)):
                ortho_cnt += 1
            if sin_ab < min_sin:
                min_sin = sin_ab
        return ortho_cnt, round(min_sin, 2)

    # --- 1. Одиночная опечатка квадранта N↔S, E↔W или 180° (N↔S + E↔W) ---
    quad_candidates: list[tuple[int, float, int, float, int, Segment, str]] = []
    for idx, seg in enumerate(segs):
        ns_flip: Literal["N", "S"] = "S" if seg.ns == "N" else "N"
        ew_flip: Literal["E", "W"] = "W" if seg.ew == "E" else "E"
        variants = [
            (_mutate_segment_bearing(seg, ns=ns_flip), f"{seg.ns}→{ns_flip}"),
            (_mutate_segment_bearing(seg, ew=ew_flip), f"{seg.ew}→{ew_flip}"),
            (_mutate_segment_bearing(seg, ns=ns_flip, ew=ew_flip), f"{seg.ns}→{ns_flip}, {seg.ew}→{ew_flip}"),
        ]
        for mutated, kind_tag in variants:
            if mutated is None or mutated.bearing_label == seg.bearing_label:
                continue
            trial = list(segs)
            trial[idx] = mutated
            err, valid = _trace_closure_and_validity(trial)
            if valid and err <= min(0.04, 0.03 * err0):
                ortho_cnt, min_sin = _corner_geometry_score(trial)
                street_pen = 1 if idx in protected_street else 0
                quad_candidates.append(
                    (-ortho_cnt, -min_sin, street_pen, err, idx, mutated, kind_tag)
                )

    if quad_candidates:
        quad_candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        _, _, _, best_err, best_idx, best_seg, kind_tag = quad_candidates[0]
        old_lbl = segs[best_idx].bearing_label
        repaired = list(segs)
        repaired[best_idx] = best_seg
        return (
            repaired,
            None,
            [
                f"Обнаружена и исправлена очевидная опечатка направления в стороне №{best_idx + 1} "
                f"({kind_tag}): «{old_lbl}» → «{best_seg.bearing_label}» "
                f"(невязка {err0:.2f} ft → {best_err:.3f} ft)"
            ],
        )

    # --- 2. Многосегментный разворот 180° / перестановка на чертеже Survey ---
    if n <= 8 and err0 > 0.5:
        best_err = err0
        best_order: tuple[int, ...] | None = None
        best_flips: tuple[bool, ...] | None = None
        orders: list[tuple[int, ...]] = [tuple(range(n))]
        if from_vision and n <= 6:
            orders.extend((0, *perm) for perm in itertools.permutations(range(1, n)))

        for order in orders:
            ordered_base = [segs[i] for i in order]
            for flips in itertools.product((False, True), repeat=n):
                if order == tuple(range(n)) and not any(flips):
                    continue
                candidate = [
                    _flip_segment_direction(s) if fl else s
                    for s, fl in zip(ordered_base, flips)
                ]
                err, valid = _trace_closure_and_validity(candidate)
                if valid and err <= 0.05 and err < best_err:
                    best_err = err
                    best_order = order
                    best_flips = flips
            if best_order is not None and best_err <= 0.01:
                break

        if best_order is not None and best_flips is not None:
            repaired_segs = [
                _flip_segment_direction(segs[idx]) if fl else segs[idx]
                for idx, fl in zip(best_order, best_flips)
            ]
            old_to_new = {old_i: new_i for new_i, old_i in enumerate(best_order)}
            return (
                repaired_segs,
                old_to_new,
                [
                    f"скорректирован порядок/направление обхода сторон по замыканию контура "
                    f"(невязка {err0:.2f} ft → {best_err:.3f} ft)"
                ],
            )

    # --- 3. Опечатка в цифрах угла (градусы / минуты / секунды) одной стороны ---
    dms_candidates: list[tuple[int, int, int, float, int, Segment, str]] = []
    for idx, seg in enumerate(segs):
        if not seg.has_bearing:
            continue
        sec_is_int = abs(seg.seconds - round(seg.seconds)) < 1e-4
        orig_sec_int = int(round(seg.seconds))
        orig_code = f"{seg.degrees:02d}{seg.minutes:02d}{orig_sec_int:02d}"

        seen_dms: set[tuple[int, int, float]] = {(seg.degrees, seg.minutes, round(seg.seconds, 4))}
        trial_dms: list[tuple[int, int, float, bool, str]] = []

        # 3a) Сверка с параллельными и перпендикулярными сторонами участка
        for j, other in enumerate(segs):
            if j == idx or not other.has_bearing:
                continue
            ref_list = [(other.degrees, other.minutes, other.seconds)]
            comp_total_sec = 90.0 * 3600.0 - (
                other.degrees * 3600.0 + other.minutes * 60.0 + other.seconds
            )
            if 0.0 <= comp_total_sec <= 90.0 * 3600.0:
                cd = int(comp_total_sec // 3600)
                rem = comp_total_sec - cd * 3600
                cm = int(rem // 60)
                cs = round(rem - cm * 60, 4)
                if cs >= 60.0:
                    cs -= 60.0
                    cm += 1
                if cm >= 60:
                    cm -= 60
                    cd += 1
                ref_list.append((cd, cm, cs))

            for rd, rm, rs in ref_list:
                key = (rd, rm, round(rs, 4))
                if key in seen_dms:
                    continue
                ref_code = f"{rd:02d}{rm:02d}{int(round(rs)):02d}"
                digit_diffs = [
                    (ca, cb) for ca, cb in zip(orig_code, ref_code) if ca != cb
                ]
                if 1 <= len(digit_diffs) <= 2:
                    seen_dms.add(key)
                    pair_desc = ", ".join(f"{a}→{b}" for a, b in digit_diffs)
                    trial_dms.append((rd, rm, rs, True, pair_desc))

        # 3b) Одиночные замены цифр в градусах, минутах и целых секундах
        deg_str = f"{seg.degrees:02d}"
        for pos in range(2):
            for d_ch in "0123456789":
                if d_ch == deg_str[pos]:
                    continue
                nd = int(deg_str[:pos] + d_ch + deg_str[pos + 1 :])
                if 0 <= nd <= 89:
                    key = (nd, seg.minutes, round(seg.seconds, 4))
                    if key not in seen_dms:
                        seen_dms.add(key)
                        trial_dms.append(
                            (nd, seg.minutes, seg.seconds, False, f"{deg_str[pos]}→{d_ch}")
                        )

        min_str = f"{seg.minutes:02d}"
        for pos in range(2):
            for d_ch in "0123456789":
                if d_ch == min_str[pos]:
                    continue
                nm = int(min_str[:pos] + d_ch + min_str[pos + 1 :])
                if 0 <= nm <= 59:
                    key = (seg.degrees, nm, round(seg.seconds, 4))
                    if key not in seen_dms:
                        seen_dms.add(key)
                        trial_dms.append(
                            (seg.degrees, nm, seg.seconds, False, f"{min_str[pos]}→{d_ch}")
                        )

        if sec_is_int:
            sec_str = f"{orig_sec_int:02d}"
            for pos in range(2):
                for d_ch in "0123456789":
                    if d_ch == sec_str[pos]:
                        continue
                    ns_int = int(sec_str[:pos] + d_ch + sec_str[pos + 1 :])
                    if 0 <= ns_int <= 59:
                        key = (seg.degrees, seg.minutes, float(ns_int))
                        if key not in seen_dms:
                            seen_dms.add(key)
                            trial_dms.append(
                                (
                                    seg.degrees,
                                    seg.minutes,
                                    float(ns_int),
                                    False,
                                    f"{sec_str[pos]}→{d_ch}",
                                )
                            )

        for nd, nm, ns_val, ref_match, pair_desc in trial_dms:
            mutated = _mutate_segment_bearing(seg, degrees=nd, minutes=nm, seconds=ns_val)
            if mutated is None:
                continue
            trial = list(segs)
            trial[idx] = mutated
            err, valid = _trace_closure_and_validity(trial)
            if not valid or err > min(0.03, 0.03 * err0):
                continue
            d_diff = _single_digit_diff(
                f"{seg.degrees:02d}{seg.minutes:02d}{orig_sec_int:02d}",
                f"{nd:02d}{nm:02d}{int(round(ns_val)):02d}",
            )
            is_look = bool(d_diff and _is_lookalike_digit_pair(*d_diff))
            if not (ref_match or is_look):
                continue
            ortho_cnt, _ = _corner_geometry_score(trial)
            street_pen = 1 if idx in protected_street else 0
            dms_candidates.append(
                (
                    -ortho_cnt,
                    0 if ref_match else 1,
                    0 if is_look else 1,
                    street_pen,
                    err,
                    idx,
                    mutated,
                    pair_desc,
                )  # type: ignore[arg-type]
            )

    if dms_candidates:
        dms_candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
        best = dms_candidates[0]
        rivals = [
            c
            for c in dms_candidates[1:]
            if c[5] != best[5]
            and c[:4] == best[:4]
            and abs(c[4] - best[4]) < 0.01
        ]
        if not rivals:
            _, _, _, _, best_err, best_idx, best_seg, pair_desc = best  # type: ignore[misc]
            old_lbl = segs[best_idx].bearing_label
            repaired = list(segs)
            repaired[best_idx] = best_seg
            return (
                repaired,
                None,
                [
                    f"Обнаружена и исправлена опечатка цифр в угле стороны №{best_idx + 1} "
                    f"(цифра {pair_desc}): «{old_lbl}» → «{best_seg.bearing_label}» "
                    f"(невязка {err0:.2f} ft → {best_err:.3f} ft)"
                ],
            )

    # --- 4. Опечатка похожей цифры (3↔8, 8↔0, 1↔7, 5↔6 и др.) в длине стороны ---
    if err0 >= 0.25:
        ex = sum(s.vector[0] for s in segs)
        ey = sum(s.vector[1] for s in segs)
        dist_matches: list[tuple[int, Segment, str, str, tuple[str, str], float]] = []
        for idx, seg in enumerate(segs):
            dx, dy = seg.vector
            length = math.hypot(dx, dy)
            if length < 1e-6:
                continue
            ux, uy = dx / length, dy / length
            perp_err = abs(ex * uy - ey * ux)
            if perp_err > 0.03:
                continue
            target_len = seg.distance_ft - (ex * ux + ey * uy)
            cand_len = round(target_len, 2)
            if cand_len <= 0.5 or abs(cand_len - target_len) > 0.03:
                continue
            s_old = f"{seg.distance_ft:.2f}"
            s_new = f"{cand_len:.2f}"
            diff_pair = _single_digit_diff(s_old, s_new)
            if diff_pair is None or not _is_lookalike_digit_pair(*diff_pair):
                continue
            mutated = seg.model_copy(update={"distance_ft": cand_len})
            trial = list(segs)
            trial[idx] = mutated
            err_after, valid = _trace_closure_and_validity(trial)
            if valid and err_after <= 0.03:
                dist_matches.append((idx, mutated, s_old, s_new, diff_pair, err_after))

        chosen_dist: tuple[int, Segment, str, str, tuple[str, str], float] | None = None
        if len(dist_matches) == 1:
            chosen_dist = dist_matches[0]
        elif len(dist_matches) > 1:
            non_street = [m for m in dist_matches if m[0] not in protected_street]
            if len(non_street) == 1:
                chosen_dist = non_street[0]

        if chosen_dist is not None:
            best_idx, best_seg, s_old, s_new, (d_old, d_new), best_err = chosen_dist
            repaired = list(segs)
            repaired[best_idx] = best_seg
            return (
                repaired,
                None,
                [
                    f"Обнаружена и исправлена очевидная опечатка цифры в длине стороны №{best_idx + 1} "
                    f"(цифра {d_old}→{d_new}): {s_old}' → {s_new}' "
                    f"(невязка {err0:.2f} ft → {best_err:.3f} ft)"
                ],
            )

    return segs, None, []


def _repair_llm_geometry(
    model: LegalDescription,
    *,
    from_vision: bool = False,
    source_text: str = "",
) -> tuple[LegalDescription, list[str]]:
    """
    Упреждающая санитарная проверка и поиск очевидных опечаток ответа LLM / Vision:
      1) не допускает привязки main_street к тыльной/боковой границе PARALLEL WITH / AT RIGHT ANGLES;
      2) не допускает углового cross_street_side_index на рядовом участке (distance_to_corner_ft > 0.5);
      3) ищет очевидные опечатки в буквах N/S/E/W, цифрах углов и похожих цифрах длин
         (3↔8, 8↔0, 1↔7, 5↔6 и др.) либо перестановку/разворот сторон на чертеже Survey,
         всегда формируя подробное пояснение; неоднозначные расхождения не маскирует;
      4) сверяет вектор привязки corner_to_pob_bearing с направлением к POB вдоль main_street.
    """
    notes: list[str] = []
    if model.anomaly_note:
        notes.append(f"[Пояснение LLM] {model.anomaly_note}")
    updates: dict[str, Any] = {}

    f_idx = model.front_segment_index
    if f_idx is not None and 0 <= f_idx < len(model.segments):
        c_note = (model.segments[f_idx].course_note or "").upper()
        if "PARALLEL WITH" in c_note or "AT RIGHT ANGLES" in c_note:
            updates["front_segment_index"] = None
            notes.append(
                "индекс фасада указывал на относительную тыльную/боковую границу — пересчитан по геометрии"
            )

    if (
        model.cross_street_side_index is not None
        and model.distance_to_corner_ft > 0.5
    ):
        updates["cross_street_side_index"] = None

    eff_front = updates.get("front_segment_index", model.front_segment_index)
    eff_cross = updates.get("cross_street_side_index", model.cross_street_side_index)
    repaired_segs, old_to_new, typo_notes = _detect_and_repair_obvious_typos(
        model.segments,
        front_segment_index=eff_front,
        cross_street_side_index=eff_cross,
        from_vision=from_vision,
    )
    if typo_notes:
        updates["segments"] = repaired_segs
        if old_to_new is not None:
            if eff_front is not None and eff_front in old_to_new:
                updates["front_segment_index"] = old_to_new[eff_front]
            if eff_cross is not None and eff_cross in old_to_new:
                updates["cross_street_side_index"] = old_to_new[eff_cross]
        notes.extend(typo_notes)

    if model.distance_to_corner_ft > 1e-6:
        eff_segs = updates.get("segments", model.segments)
        eff_f_idx = updates.get("front_segment_index", model.front_segment_index)
        pob_dir: str | None = None
        explicit_tie: Segment | None = None
        if source_text.strip():
            preamble, lot_body = _split_preamble_and_body(source_text)
            pob_dir, explicit_tie = _extract_pob_tie_hint(
                preamble=preamble,
                lot_body=lot_body,
                distance_to_corner=model.distance_to_corner_ft,
            )
        expected_tie = infer_corner_to_pob_bearing(
            eff_segs,
            distance_to_corner_ft=model.distance_to_corner_ft,
            front_segment_index=eff_f_idx,
            front_side=model.front_side,
            pob_direction=pob_dir,
            explicit_tie_segment=explicit_tie,
        )
        cur_tie = model.corner_to_pob_bearing
        if expected_tie is not None:
            if cur_tie is None or "segments" in updates or "front_segment_index" in updates:
                updates["corner_to_pob_bearing"] = expected_tie
            elif (
                (pob_dir is not None or explicit_tie is not None)
                and _angle_diff_deg(cur_tie.azimuth_deg, expected_tie.azimuth_deg) > 120.0
            ):
                updates["corner_to_pob_bearing"] = expected_tie
                notes.append(
                    "развёрнут румб привязки corner_to_pob_bearing по направлению от угла к POB"
                )

    if updates:
        model = model.model_copy(update=updates)
    return model, notes


ORIENTATION_DETECT_PROMPT = """Examine the attached survey / blueprint image(s), numbered 1..N in the order attached.
Some archival 1980s survey photos or scans may be sideways (90° or 270°) or completely upside-down (180°).
For EACH image index 1..N, determine the CLOCKWISE rotation in degrees (ONLY 0, 90, 180, or 270) that MUST be applied so that the main Title Block, surveyor stamp, street names, and printed notes become upright and readable right-side up (0 = already upright).
Return STRICT JSON ONLY in this exact format:
{"rotations": [{"index": 1, "clockwise_deg": 0, "reason": "short note"}]}"""


def _shrink_data_url_thumbnail(data_url: str, *, max_side_px: int = 768) -> str:
    """Создаёт компактное превью (≤768 px) из data-URL для быстрого автоопределения поворота."""
    if "," not in data_url:
        return data_url
    _, b64 = data_url.split(",", 1)
    try:
        raw = base64.b64decode(b64)
        thumb_bytes, mime, _ = prepare_survey_image_bytes(
            raw,
            rotation_deg=0,
            max_side_px=max_side_px,
            enhance_scan=False,
            output_format="jpeg",
            jpeg_quality=78,
        )
        return f"data:{mime};base64,{base64.b64encode(thumb_bytes).decode('ascii')}"
    except Exception:  # noqa: BLE001
        return data_url


def _rotate_data_url_clockwise(data_url: str, clockwise_deg: int) -> str:
    """Поворачивает уже отрендеренный full-res data-URL на 90°/180°/270° без повторного фильтра."""
    deg = int(clockwise_deg) % 360
    if deg == 0 or "," not in data_url:
        return data_url
    header, b64 = data_url.split(",", 1)
    raw = base64.b64decode(b64)
    fmt: Literal["png", "jpeg"] = "png" if "image/png" in header.lower() else "jpeg"
    rot_bytes, mime, _ = prepare_survey_image_bytes(
        raw,
        rotation_deg=deg,
        max_side_px=4096,
        enhance_scan=False,
        output_format=fmt,
        jpeg_quality=90,
    )
    return f"data:{mime};base64,{base64.b64encode(rot_bytes).decode('ascii')}"


def detect_visual_orientations_llm(
    image_data_urls: Sequence[str],
    cfg: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    key_secret: str = "",
    progress: Callable[[str], None] | None = None,
) -> tuple[list[int], TokenUsage | None]:
    """
    Быстрый предпроход Vision LLM по компактным превью (768 px): определяет для
    каждого фото/страницы угол поворота по часовой стрелке ``0 / 90 / 180 / 270``,
    нужный чтобы поставить перевёрнутый чертёж прямо перед чтением мелких цифр.
    """
    n_imgs = len(image_data_urls)
    if n_imgs == 0:
        return [], None

    api_cfg = cfg.get("api", {})
    target = resolve_api_target(
        cfg,
        provider=provider,
        model=model,
        api_key=api_key,
        key_secret=key_secret,
    )
    client = _make_client(api_cfg, target=target)
    if progress:
        progress(
            f"Автоопределение поворота ({n_imgs} изобр.) через {target['provider_label']} / {target['model']}…"
        )

    thumbs = [_shrink_data_url_thumbnail(u, max_side_px=768) for u in image_data_urls]
    content_blocks: list[dict[str, Any]] = [
        {"type": "text", "text": ORIENTATION_DETECT_PROMPT}
    ]
    for th in thumbs:
        content_blocks.append({"type": "image_url", "image_url": {"url": th}})
    messages = [
        {
            "role": "system",
            "content": "You are a precise document orientation detector for land survey blueprints. Reply with JSON only.",
        },
        {"role": "user", "content": content_blocks},
    ]

    try:
        try:
            completion = _call_with_temperature(
                client.chat.completions.create,
                model=target["model"],
                messages=messages,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        except Exception as inner_exc:  # noqa: BLE001
            if _is_auth_error(inner_exc):
                raise
            completion = _call_with_temperature(
                client.chat.completions.create,
                model=target["model"],
                messages=messages,
                temperature=0.0,
            )
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise LLMParseError(f"ошибка авторизации API при автоповороте: {exc}") from exc
        return [0] * n_imgs, None

    counts = _extract_usage_counts(completion)
    usage = (
        TokenUsage(
            prompt_tokens=counts[0],
            completion_tokens=counts[1],
            total_tokens=counts[2],
            provider=target["provider"],
            provider_label=target["provider_label"],
            model=target["model"],
            available=True,
        )
        if counts is not None
        else TokenUsage(
            provider=target["provider"],
            provider_label=target["provider_label"],
            model=target["model"],
            available=False,
        )
    )

    try:
        raw_content = completion.choices[0].message.content or ""
        parsed = _json_from_text(raw_content)
    except Exception:  # noqa: BLE001
        return [0] * n_imgs, usage

    result_rots = [0] * n_imgs
    items = None
    if isinstance(parsed, dict):
        items = parsed.get("rotations")
        if items is None and "clockwise_deg" in parsed:
            items = [parsed]
    elif isinstance(parsed, list):
        items = parsed

    if isinstance(items, list):
        for pos, entry in enumerate(items):
            if not isinstance(entry, dict):
                continue
            raw_idx = entry.get("index", pos + 1)
            try:
                idx0 = int(raw_idx) - 1
            except (TypeError, ValueError):
                idx0 = pos
            if not (0 <= idx0 < n_imgs):
                idx0 = pos if pos < n_imgs else -1
            if idx0 < 0:
                continue
            try:
                deg = int(round(float(entry.get("clockwise_deg", 0)))) % 360
            except (TypeError, ValueError):
                deg = 0
            # Привязываем к ближайшему кратному 90° (0, 90, 180, 270)
            snapped = min((0, 90, 180, 270), key=lambda c: min((deg - c) % 360, (c - deg) % 360))
            result_rots[idx0] = snapped

    return result_rots, usage


def _prepare_parse_input(
    text: str | None,
    pdf_path: str | Path | None,
    cfg: dict[str, Any],
    *,
    use_llm: bool,
    image_paths: Sequence[str | Path] | None = None,
    pdf_pages: str | Sequence[int] | None = None,
    rotation_deg: int | Sequence[int] = 0,
    enhance_scan: bool | None = None,
    auto_orient: bool = False,
    orient_provider: str | None = None,
    orient_model: str | None = None,
    orient_api_key: str | None = None,
    key_secret: str = "",
    progress: Callable[[str], None] | None = None,
) -> tuple[str, list[str], list[str], TokenUsage | None]:
    """
    Читает/рендерит PDF или пачку фотографий (``.jpg/.png/.tif/.webp/.bmp``) один раз
    и подготавливает ``(combined_text, image_data_urls, warnings, orient_usage)``.
    При многопроходном режиме избавляет от повторного рендера и автоповорота в каждом потоке.
    """
    from pathlib import Path as _Path

    warnings: list[str] = []
    image_data_urls: list[str] = []
    orient_usage: TokenUsage | None = None
    do_enhance = (
        bool(cfg.get("api", {}).get("vision_enhance_scans", True))
        if enhance_scan is None
        else bool(enhance_scan)
    )

    # Удобный ярлык: если в pdf_path передали одиночный .jpg/.png/.tif — перенаправляем в image_paths
    eff_image_paths: list[Path] = [Path(p) for p in (image_paths or [])]
    eff_pdf_path: Path | None = _Path(pdf_path) if pdf_path else None
    if eff_pdf_path is not None and is_image_file(eff_pdf_path):
        eff_image_paths.insert(0, eff_pdf_path)
        eff_pdf_path = None

    if eff_image_paths:
        if not use_llm:
            names = ", ".join(p.name for p in eff_image_paths[:3])
            raise PDFError(
                f"файлы изображений ({names}) не содержат текстового слоя. "
                "Включите «ИИ-разбор (LLM)» и выберите Vision-модель "
                "(например, hydra-gemini-pro, google/gemini-2.5-pro или openai/gpt-4.1)."
            )
        if progress:
            progress(
                f"Подготавливаю фото чертежей ({len(eff_image_paths)} файл., "
                f"{'улучшение контраста 80-х вкл.' if do_enhance else 'без фильтра'})…"
            )
        img_urls, img_notes = render_image_files_base64(
            eff_image_paths,
            rotation_deg=rotation_deg,
            enhance_scan=do_enhance,
            output_format="jpeg",
        )
        image_data_urls.extend(img_urls)
        warnings.extend(img_notes)
        warnings.append(
            f"Загружено фото/сканов чертежа для Vision LLM: {len(img_urls)} шт. "
            f"({', '.join(p.name for p in eff_image_paths[:4])}"
            f"{'…' if len(eff_image_paths) > 4 else ''})"
        )

    pdf_text = ""
    if eff_pdf_path is not None:
        p_name = eff_pdf_path.name
        if progress:
            progress(f"Читаю PDF: {p_name}")
        pdf_info = inspect_pdf(eff_pdf_path)
        # Если явно запрошен диапазон страниц или поворот на фото-PDF — идём через Vision
        explicit_pages = pdf_pages is not None and str(pdf_pages).strip() != ""
        has_rot = (
            (isinstance(rotation_deg, (int, float)) and int(rotation_deg) % 360 != 0)
            or (
                not isinstance(rotation_deg, (int, float))
                and any(int(r) % 360 != 0 for r in rotation_deg)
            )
        )
        if pdf_info["has_text"] and not explicit_pages:
            pdf_text = str(pdf_info["text"])
        if (
            not pdf_info.get("has_legal_text", pdf_info["has_text"])
            or explicit_pages
            or (has_rot and pdf_info.get("has_raster_images"))
        ):
            if use_llm:
                dpi = int(cfg["api"].get("vision_dpi") or 220)
                max_p = int(cfg["api"].get("vision_max_pages") or 8)
                reason = (
                    "нет текстового слоя"
                    if not pdf_info["has_text"]
                    else (
                        f"выбраны страницы ({pdf_pages})"
                        if explicit_pages
                        else "чертёж Survey без текстового обхода румбов"
                    )
                )
                if progress:
                    progress(
                        f"PDF «{p_name}» ({reason}) — рендерю страницы ({dpi} DPI) для Vision LLM…"
                    )
                pdf_urls = render_pdf_pages_base64(
                    eff_pdf_path,
                    dpi=dpi,
                    max_pages=max_p,
                    pages=pdf_pages,
                    rotation_deg=rotation_deg,
                    enhance_scan=do_enhance,
                    output_format="auto",
                )
                image_data_urls.extend(pdf_urls)
                warnings.append(
                    f"PDF «{p_name}» ({reason}) — "
                    f"распознан через Vision LLM ({len(pdf_urls)} стр.)"
                )
            elif not pdf_info["has_text"] and not (text and text.strip()):
                raise PDFError(
                    f"в PDF «{p_name}» нет текстового слоя (векторный чертёж AutoCAD или скан). "
                    "Включите «Использовать LLM» и выберите Vision-модель "
                    "(например, hydra-gemini-pro, nvidia/nemotron-3.5-super-vl:free или openai/gpt-4.1)."
                )

    # Разовый опциональный ИИ-автоповорот (только по явному запросу auto_orient=True;
    # по умолчанию ВЫКЛЮЧЕН из-за дополнительного расхода токенов Vision LLM)
    if use_llm and auto_orient and image_data_urls:
        detected_rots, orient_usage = detect_visual_orientations_llm(
            image_data_urls,
            cfg,
            provider=orient_provider,
            model=orient_model,
            api_key=orient_api_key,
            key_secret=key_secret,
            progress=progress,
        )
        tok_note = (
            f" (доп. вызов LLM: {orient_usage.total_tokens:,} ток.)"
            if (orient_usage is not None and orient_usage.available)
            else " (выполнен доп. вызов LLM)"
        )
        rotated_labels: list[str] = []
        for idx, deg_cw in enumerate(detected_rots):
            if deg_cw in (90, 180, 270):
                image_data_urls[idx] = _rotate_data_url_clockwise(
                    image_data_urls[idx], deg_cw
                )
                rotated_labels.append(f"#{idx + 1} → +{deg_cw}°")
        if rotated_labels:
            warnings.append(
                f"🧭 Автоповорот ИИ (разовый запуск){tok_note} выровнял изображения перед чтением размеров: "
                + ", ".join(rotated_labels)
            )
        else:
            warnings.append(
                f"🧭 Автоповорот ИИ (разовый запуск){tok_note}: все кадры уже в прямой ориентации (0°)"
            )

    combined = "\n\n".join(part for part in (pdf_text, text or "") if part and part.strip())
    combined = _normalize_legal_text(combined).strip()
    if not image_data_urls and len(combined) < 20:
        raise LegalDescriptionError("текст Legal Description пуст или слишком короткий")
    max_chars = int(cfg["api"].get("max_input_chars") or 60000)
    if len(combined) > max_chars:
        warnings.append(
            f"текст длиннее {max_chars} символов — обрезан до {max_chars} для запроса"
        )
        combined = combined[:max_chars]
    return combined, image_data_urls, warnings, orient_usage


def parse_legal_description_full(
    text: str | None = None,
    pdf_path: str | Path | None = None,
    cfg: dict[str, Any] | None = None,
    *,
    image_paths: Sequence[str | Path] | None = None,
    pdf_pages: str | Sequence[int] | None = None,
    rotation_deg: int | Sequence[int] = 0,
    enhance_scan: bool | None = None,
    auto_orient: bool = False,
    use_llm: bool = True,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    key_secret: str = "",
    allow_regex_fallback: bool = False,
    progress: Callable[[str], None] | None = None,
    focus_instruction: str | None = None,
) -> ParseOutcome:
    """
    Полный разбор: PDF / пачка фото чертежей (JPG/PNG/TIFF/WEBP) / текст ->
    LLM (или локальный парсер) -> LegalDescription.
    """
    cfg = copy.deepcopy(cfg) if cfg is not None else load_config()
    combined, image_data_urls, warnings, orient_usage = _prepare_parse_input(
        text,
        pdf_path,
        cfg,
        use_llm=use_llm,
        image_paths=image_paths,
        pdf_pages=pdf_pages,
        rotation_deg=rotation_deg,
        enhance_scan=enhance_scan,
        auto_orient=auto_orient,
        orient_provider=provider,
        orient_model=model,
        orient_api_key=api_key,
        key_secret=key_secret,
        progress=progress,
    )

    llm_error: Exception | None = None
    if use_llm:
        try:
            parsed_model, mode, usage = _extract_with_llm(
                combined,
                cfg,
                api_key=api_key,
                progress=progress,
                provider=provider,
                model=model,
                key_secret=key_secret,
                image_data_urls=image_data_urls or None,
                focus_instruction=focus_instruction,
            )
            if orient_usage is not None and usage is not None:
                usage = TokenUsage(
                    prompt_tokens=orient_usage.prompt_tokens + usage.prompt_tokens,
                    completion_tokens=orient_usage.completion_tokens + usage.completion_tokens,
                    total_tokens=orient_usage.total_tokens + usage.total_tokens,
                    provider=usage.provider,
                    provider_label=usage.provider_label,
                    model=usage.model,
                    available=usage.available or orient_usage.available,
                )
            parsed_model, repair_notes = _repair_llm_geometry(
                parsed_model,
                from_vision=bool(image_data_urls),
                source_text=combined,
            )
            warnings.extend(repair_notes)
            if mode != "structured-outputs":
                warnings.append(f"провайдер не поддержал Structured Outputs, режим: {mode}")
            return ParseOutcome(
                data=parsed_model,
                source="llm-vision" if image_data_urls else "llm",
                warnings=warnings,
                usage=usage,
                provider=usage.provider,
                provider_label=usage.provider_label,
                model=usage.model,
                llm_mode=mode,
            )
        except Exception as exc:  # noqa: BLE001
            llm_error = exc
            if not allow_regex_fallback or image_data_urls:
                raise
            warnings.append(f"LLM недоступна ({exc}); использую локальный парсер")

    if progress:
        progress("Локальный разбор текста (regex)…")
    parsed_model, regex_warnings = parse_with_regex(combined)
    warnings.extend(regex_warnings)

    if llm_error is not None:
        warnings.insert(0, "геометрия получена локальным парсером — проверьте размеры вручную")
    return ParseOutcome(
        data=parsed_model,
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


# ---------------------------------------------------------------------------
# Многопроходный независимый консенсус (2–3 прохода LLM) и разрешение конфликтов
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PassTarget:
    """Конфигурация одного независимого прохода LLM (провайдер + модель)."""

    pass_index: int
    provider: str
    model: str
    api_key: str | None = None


@dataclass(frozen=True)
class DiscrepancyItem:
    """Описание одного конкретного расхождения между ответами проходов LLM."""

    field_id: str
    title: str
    values_by_pass: dict[int, str]

    def summary(self, pass_labels: dict[int, str] | None = None) -> str:
        parts: list[str] = []
        for p_idx in sorted(self.values_by_pass):
            p_name = (
                pass_labels[p_idx]
                if pass_labels and p_idx in pass_labels
                else f"Проход #{p_idx}"
            )
            parts.append(f"{p_name}: «{self.values_by_pass[p_idx]}»")
        return f"{self.title}  →  " + "   vs   ".join(parts)


@dataclass
class ConsensusResult:
    """Результат параллельного запуска 2–3 независимых проходов LLM."""

    agreed: bool
    pass_outcomes: list[ParseOutcome]
    pass_targets: list[PassTarget]
    discrepancies: list[DiscrepancyItem]
    selected_outcome: ParseOutcome
    total_usage: TokenUsage
    round_number: int = 1
    prepared_text: str = ""
    prepared_images: list[str] = field(default_factory=list)
    base_warnings: list[str] = field(default_factory=list)

    def pass_label(self, pass_index: int) -> str:
        """Короткое человекочитаемое имя прохода (1-based)."""
        idx0 = pass_index - 1
        if 0 <= idx0 < len(self.pass_outcomes):
            out = self.pass_outcomes[idx0]
            prov = out.provider_label or out.provider
            mdl = out.model
            badge = " / ".join(p for p in (prov, mdl) if p)
            return f"Проход #{pass_index} ({badge})" if badge else f"Проход #{pass_index}"
        return f"Проход #{pass_index}"

    def pass_labels_map(self) -> dict[int, str]:
        return {i + 1: self.pass_label(i + 1) for i in range(len(self.pass_outcomes))}

    def pass_closure_ft(self, pass_index: int) -> float:
        idx0 = pass_index - 1
        if 0 <= idx0 < len(self.pass_outcomes):
            err, _ = _trace_closure_and_validity(self.pass_outcomes[idx0].data.segments)
            return err
        return 0.0

    def discrepancy_report(self) -> str:
        """Подробная русскоязычная сводка расхождений между проходами."""
        n = len(self.pass_outcomes)
        if self.agreed:
            return f"✓ Консенсус {n}/{n} проходов (раунд #{self.round_number}): ответы полностью совпали."
        labels = self.pass_labels_map()
        lines = [
            f"⚠ Обнаружено расхождение между {n} независимыми проходами LLM "
            f"(раунд #{self.round_number}, спорных пунктов: {len(self.discrepancies)}):"
        ]
        for idx, item in enumerate(self.discrepancies, start=1):
            lines.append(f"  {idx}. {item.summary(labels)}")
        lines.append("")
        lines.append("Невязка контура по каждому проходу:")
        for p_i in range(1, n + 1):
            out = self.pass_outcomes[p_i - 1]
            err = self.pass_closure_ft(p_i)
            lines.append(
                f"  • {labels[p_i]}: сторон {len(out.data.segments)}, "
                f"периметр {out.data.perimeter_ft:.2f} ft, невязка {err:.4f} ft"
            )
        lines.append("")
        lines.append(
            "Взгляните на исходный документ и выберите правую модель, "
            "либо нажмите «Попробуй разобраться ещё раз» для прицельной перепроверки спорных мест."
        )
        return "\n".join(lines)

    def resolve_with_pass(self, pass_index: int, *, reason: str = "") -> ParseOutcome:
        """Формирует итоговый ParseOutcome на базе выбранного человеком прохода."""
        idx0 = pass_index - 1
        if not (0 <= idx0 < len(self.pass_outcomes)):
            raise ValueError(f"некорректный номер прохода: {pass_index}")
        chosen = self.pass_outcomes[idx0]
        label = self.pass_label(pass_index)
        note = (
            reason.strip()
            if reason.strip()
            else f"Конфликт проходов разрешён человеком: выбран {label}"
        )
        merged_warnings = [note, *chosen.warnings]
        return ParseOutcome(
            data=chosen.data,
            source=chosen.source,
            warnings=merged_warnings,
            usage=self.total_usage,
            provider=chosen.provider,
            provider_label=chosen.provider_label,
            model=chosen.model,
            llm_mode=chosen.llm_mode,
        )


_STREET_SUFFIX_CANON: dict[str, str] = {
    "st": "street",
    "str": "street",
    "ave": "avenue",
    "av": "avenue",
    "blvd": "boulevard",
    "rd": "road",
    "dr": "drive",
    "ln": "lane",
    "pl": "place",
    "ct": "court",
    "pkwy": "parkway",
    "expy": "expressway",
    "expwy": "expressway",
    "hwy": "highway",
    "lp": "loop",
}


def _canonical_street_key(name: str | None) -> str:
    """Нормализует имя улицы для сравнения (Aspenwall St. == ASPENWALL STREET)."""
    if not name:
        return ""
    cleaned = re.sub(r"\([^)]*\)", " ", name.lower())
    tokens = re.findall(r"[a-z0-9]+", cleaned)
    norm = [_STREET_SUFFIX_CANON.get(tok, tok) for tok in tokens]
    return " ".join(norm)


def compare_legal_descriptions(
    outcomes: Sequence[ParseOutcome],
) -> tuple[bool, list[DiscrepancyItem]]:
    """
    Геодезическое сравнение 2–3 ответов LLM:
      * отсекает чисто косметическую разницу ('St.' vs 'Street', 'N 0°00' E' vs 'N 0°00' W');
      * фиксирует малейшее расхождение в числе сторон, румбе/угле (до 0.0002° ≈ 0.7"),
        длине стороны (> 0.005 ft), привязке POB (> 0.01 ft) и названиях улиц.
    """
    n = len(outcomes)
    if n <= 1:
        return True, []

    discrepancies: list[DiscrepancyItem] = []
    datas = [o.data for o in outcomes]

    # 1. Названия улиц (main_street / cross_street)
    main_keys = [_canonical_street_key(d.main_street) for d in datas]
    if len(set(main_keys)) > 1:
        discrepancies.append(
            DiscrepancyItem(
                field_id="main_street",
                title="Фасадная улица (main_street)",
                values_by_pass={i + 1: (datas[i].main_street or "—") for i in range(n)},
            )
        )

    cross_keys = [_canonical_street_key(d.cross_street) for d in datas]
    if len(set(cross_keys)) > 1:
        discrepancies.append(
            DiscrepancyItem(
                field_id="cross_street",
                title="Пересекаемая улица (cross_street)",
                values_by_pass={i + 1: (datas[i].cross_street or "—") for i in range(n)},
            )
        )

    # 2. Привязка POB от перекрёстка улиц
    dists_corner = [float(d.distance_to_corner_ft or 0.0) for d in datas]
    if max(dists_corner) - min(dists_corner) > 0.01:
        discrepancies.append(
            DiscrepancyItem(
                field_id="distance_to_corner_ft",
                title="Расстояние от перекрёстка улиц до POB",
                values_by_pass={i + 1: f"{dists_corner[i]:.2f}'" for i in range(n)},
            )
        )
    elif max(dists_corner) > 0.01:
        ties = [d.corner_to_pob_bearing for d in datas]
        present_ties = [t for t in ties if t is not None]
        if len(present_ties) >= 2:
            base_az = present_ties[0].azimuth_deg
            if any(_angle_diff_deg(base_az, t.azimuth_deg) > 1e-3 for t in present_ties[1:]):
                discrepancies.append(
                    DiscrepancyItem(
                        field_id="corner_to_pob_bearing",
                        title="Направление от перекрёстка к POB",
                        values_by_pass={
                            i + 1: (ties[i].bearing_label if ties[i] is not None else "—")
                            for i in range(n)
                        },
                    )
                )

    # 3. Число сторон участка
    seg_counts = [len(d.segments) for d in datas]
    if len(set(seg_counts)) > 1:
        discrepancies.append(
            DiscrepancyItem(
                field_id="segment_count",
                title="Число сторон участка",
                values_by_pass={i + 1: f"{seg_counts[i]} стор." for i in range(n)},
            )
        )
        return False, discrepancies

    # 4. Поэлементное сравнение каждой стороны (направление + длина + внутренний угол)
    seg_len = seg_counts[0]
    for s_idx in range(seg_len):
        segs_i = [d.segments[s_idx] for d in datas]
        base_az = segs_i[0].azimuth_deg
        if any(_angle_diff_deg(base_az, s.azimuth_deg) > 2e-4 for s in segs_i[1:]):
            discrepancies.append(
                DiscrepancyItem(
                    field_id=f"segments[{s_idx}].bearing",
                    title=f"Сторона №{s_idx + 1} — направление/румб",
                    values_by_pass={i + 1: segs_i[i].bearing_label for i in range(n)},
                )
            )

        s_dists = [s.distance_ft for s in segs_i]
        if max(s_dists) - min(s_dists) > 0.005:
            discrepancies.append(
                DiscrepancyItem(
                    field_id=f"segments[{s_idx}].distance_ft",
                    title=f"Сторона №{s_idx + 1} — длина",
                    values_by_pass={i + 1: f"{s_dists[i]:.2f}'" for i in range(n)},
                )
            )

        int_angles = [
            (s.interior_angle or "").strip() for s in segs_i if (s.interior_angle or "").strip()
        ]
        if len(int_angles) == n and len(set(int_angles)) > 1:
            discrepancies.append(
                DiscrepancyItem(
                    field_id=f"segments[{s_idx}].interior_angle",
                    title=f"Сторона №{s_idx + 1} — внутренний угол",
                    values_by_pass={
                        i + 1: (segs_i[i].interior_angle or "—") for i in range(n)
                    },
                )
            )

    return len(discrepancies) == 0, discrepancies


def build_discrepancy_focus_prompt(consensus: ConsensusResult) -> str:
    """
    Строит промпт-инъекцию для повторного арбитражного вызова моделей:
    перечисляет ВСЕ конкретные спорные места и невязки предыдущего раунда и даёт
    жёсткое задание перепроверить первоисточник именно в этих точках.
    """
    next_round = consensus.round_number + 1
    labels = consensus.pass_labels_map()
    bullet_lines: list[str] = []
    for idx, item in enumerate(consensus.discrepancies, start=1):
        bullet_lines.append(f"  {idx}) {item.summary(labels)}")

    closure_lines: list[str] = []
    for p_i in range(1, len(consensus.pass_outcomes) + 1):
        err = consensus.pass_closure_ft(p_i)
        closure_lines.append(f"  - {labels[p_i]}: невязка контура = {err:.4f} ft")

    disputed_block = "\n".join(bullet_lines) if bullet_lines else "  (общая проверка геометрии)"
    closure_block = "\n".join(closure_lines)
    return (
        f"CRITICAL RE-VERIFICATION TASK (ARBITRATION ROUND #{next_round}):\n"
        "Independent readings of this exact document disagreed ONLY on the following specific item(s):\n"
        f"{disputed_block}\n\n"
        "Closure error of each previous reading:\n"
        f"{closure_block}\n\n"
        "INSTRUCTIONS FOR THIS RE-CHECK:\n"
        "1. Focus specifically on the disputed item(s) above in the original source text / survey image "
        "(first verify drawing orientation & North Arrow).\n"
        "2. Re-read every character and digit at the disputed spot with extreme care: verify quadrant "
        "letters (N vs S, E vs W), degree/minute/second digits, fractional inches (1/2\", 1/4\", 3/4\"), "
        "and distance digits (watch for upside-down 6 vs 9 and faded 3 vs 8, 8 vs 0, 1 vs 7, 5 vs 6, 0 vs 6, 0 vs 9).\n"
        "3. Cross-check against opposite parallel sides, perpendicular 90° corners, and total polygon "
        "vector closure (∑ΔX = 0, ∑ΔY = 0).\n"
        "4. Do NOT average values — return the exact reading supported by the document and geometry, "
        "and briefly explain your verification of the disputed point in Russian in `anomaly_note`."
    )


def build_pass_targets(
    cfg: dict[str, Any],
    *,
    primary_provider: str | None = None,
    primary_model: str | None = None,
    primary_api_key: str | None = None,
    passes: int | None = None,
) -> list[PassTarget]:
    """Собирает список из 1, 2 или 3 целей независимых проходов LLM из настроек."""
    api_cfg = cfg.get("api", {})
    cons_cfg = api_cfg.get("consensus") or {}
    count = int(passes if passes is not None else (cons_cfg.get("passes") or 1))
    count = max(1, min(3, count))

    p1_prov = str(primary_provider or api_cfg.get("provider") or "hydra").strip().lower()
    providers_map = api_cfg.get("providers") or {}
    p1_default_model = (
        (providers_map.get(p1_prov) or {}).get("selected_model")
        or api_cfg.get("model")
        or "glm-5.2"
    )
    p1_model = str(primary_model or p1_default_model).strip()
    targets = [
        PassTarget(
            pass_index=1,
            provider=p1_prov,
            model=p1_model,
            api_key=primary_api_key,
        )
    ]
    if count >= 2:
        p2_prov = str(cons_cfg.get("pass2_provider") or p1_prov).strip().lower()
        p2_def = (providers_map.get(p2_prov) or {}).get("selected_model") or p1_model
        p2_model = str(cons_cfg.get("pass2_model") or p2_def).strip()
        p2_key = primary_api_key if p2_prov == p1_prov else None
        targets.append(
            PassTarget(pass_index=2, provider=p2_prov, model=p2_model, api_key=p2_key)
        )
    if count >= 3:
        p3_prov = str(cons_cfg.get("pass3_provider") or p1_prov).strip().lower()
        p3_def = (providers_map.get(p3_prov) or {}).get("selected_model") or p1_model
        p3_model = str(cons_cfg.get("pass3_model") or p3_def).strip()
        p3_key = primary_api_key if p3_prov == p1_prov else None
        targets.append(
            PassTarget(pass_index=3, provider=p3_prov, model=p3_model, api_key=p3_key)
        )
    return targets


def _combine_usages(
    usages: Sequence[TokenUsage | None],
    *,
    label_summary: str = "",
) -> TokenUsage:
    valid = [u for u in usages if u is not None]
    if not valid:
        return TokenUsage(available=False, provider_label=label_summary)
    p_sum = sum(u.prompt_tokens for u in valid)
    c_sum = sum(u.completion_tokens for u in valid)
    t_sum = sum(u.total_tokens for u in valid)
    any_avail = any(u.available for u in valid)
    if len(valid) == 1 and not label_summary:
        return valid[0]
    return TokenUsage(
        prompt_tokens=p_sum,
        completion_tokens=c_sum,
        total_tokens=t_sum,
        provider=valid[0].provider,
        provider_label=label_summary or f"{len(valid)} прох.",
        model="",
        available=any_avail,
    )


def run_multipass_consensus(
    text: str | None = None,
    pdf_path: str | Path | None = None,
    cfg: dict[str, Any] | None = None,
    *,
    image_paths: Sequence[str | Path] | None = None,
    pdf_pages: str | Sequence[int] | None = None,
    rotation_deg: int | Sequence[int] = 0,
    enhance_scan: bool | None = None,
    auto_orient: bool = False,
    targets: Sequence[PassTarget] | None = None,
    passes: int | None = None,
    primary_provider: str | None = None,
    primary_model: str | None = None,
    primary_api_key: str | None = None,
    key_secret: str = "",
    progress: Callable[[str], None] | None = None,
    focus_instruction: str | None = None,
    prior_usage: TokenUsage | None = None,
    round_number: int = 1,
    prepared_input: tuple[str, list[str], list[str]] | None = None,
) -> ConsensusResult:
    """
    Выполняет 2 или 3 независимых параллельных вызова LLM (по одному на каждый PassTarget),
    сравнивает геометрию и метаданные и возвращает ``ConsensusResult``.
    """
    cfg_copy = copy.deepcopy(cfg) if cfg is not None else load_config()
    pass_targets = (
        list(targets)
        if targets is not None
        else build_pass_targets(
            cfg_copy,
            primary_provider=primary_provider,
            primary_model=primary_model,
            primary_api_key=primary_api_key,
            passes=passes,
        )
    )
    if not pass_targets:
        raise LLMParseError("не задано ни одного прохода LLM")

    # Предварительно проверяем наличие ключей для всех выбранных провайдеров,
    # чтобы выдать понятную ошибку с номером прохода до старта рендера и потоков.
    for pt in pass_targets:
        resolved = resolve_api_target(
            cfg_copy,
            provider=pt.provider,
            model=pt.model,
            api_key=pt.api_key,
            key_secret=key_secret,
        )
        if not resolved["api_key"]:
            p_lbl = resolved["provider_label"]
            env_hint = f" (или {resolved['env_key']})" if resolved.get("env_key") else ""
            raise LLMParseError(
                f"Проход #{pt.pass_index} ({p_lbl} / {resolved['model']}): "
                f"не задан API-ключ для провайдера {p_lbl}{env_hint}"
            )

    orient_usage: TokenUsage | None = None
    if prepared_input is not None:
        combined, image_data_urls, base_warnings = (
            prepared_input[0],
            list(prepared_input[1]),
            list(prepared_input[2]),
        )
    else:
        p1 = pass_targets[0]
        combined, image_data_urls, base_warnings, orient_usage = _prepare_parse_input(
            text,
            pdf_path,
            cfg_copy,
            use_llm=True,
            image_paths=image_paths,
            pdf_pages=pdf_pages,
            rotation_deg=rotation_deg,
            enhance_scan=enhance_scan,
            auto_orient=auto_orient,
            orient_provider=p1.provider,
            orient_model=p1.model,
            orient_api_key=p1.api_key,
            key_secret=key_secret,
            progress=progress,
        )

    n_passes = len(pass_targets)
    if progress:
        round_tag = f" [повторный арбитраж, раунд #{round_number}]" if round_number > 1 else ""
        progress(
            f"Параллельный запуск {n_passes} независимых проходов LLM{round_tag}…"
        )

    def _run_single_pass(pt: PassTarget) -> ParseOutcome:
        def _pass_progress(msg: str) -> None:
            if progress:
                progress(f"[Проход #{pt.pass_index}] {msg}")

        parsed_model, mode, usage = _extract_with_llm(
            combined,
            cfg_copy,
            api_key=pt.api_key,
            progress=_pass_progress,
            provider=pt.provider,
            model=pt.model,
            key_secret=key_secret,
            image_data_urls=image_data_urls or None,
            focus_instruction=focus_instruction,
        )
        parsed_model, repair_notes = _repair_llm_geometry(
            parsed_model,
            from_vision=bool(image_data_urls),
            source_text=combined,
        )
        p_warnings = list(base_warnings) + list(repair_notes)
        if mode != "structured-outputs":
            p_warnings.append(
                f"Проход #{pt.pass_index}: провайдер не поддержал Structured Outputs ({mode})"
            )
        return ParseOutcome(
            data=parsed_model,
            source="llm-vision" if image_data_urls else "llm",
            warnings=p_warnings,
            usage=usage,
            provider=usage.provider,
            provider_label=usage.provider_label,
            model=usage.model,
            llm_mode=mode,
        )

    outcomes_by_index: dict[int, ParseOutcome] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_passes) as pool:
        future_map = {
            pool.submit(_run_single_pass, pt): pt for pt in pass_targets
        }
        for fut in concurrent.futures.as_completed(future_map):
            pt = future_map[fut]
            try:
                outcomes_by_index[pt.pass_index] = fut.result()
            except Exception as exc:  # noqa: BLE001
                raise LLMParseError(
                    f"Проход #{pt.pass_index} ({pt.provider} / {pt.model}): {exc}"
                ) from exc

    ordered_outcomes = [outcomes_by_index[pt.pass_index] for pt in pass_targets]
    agreed, discrepancies = compare_legal_descriptions(ordered_outcomes)

    usage_items: list[TokenUsage | None] = [o.usage for o in ordered_outcomes]
    if orient_usage is not None:
        usage_items.insert(0, orient_usage)
    if prior_usage is not None:
        usage_items.insert(0, prior_usage)
    badge_summary = " + ".join(
        f"#{i + 1}:{o.model or o.provider}" for i, o in enumerate(ordered_outcomes)
    )
    if round_number > 1:
        badge_summary += f" (раунд #{round_number})"
    total_usage = _combine_usages(usage_items, label_summary=badge_summary)

    first = ordered_outcomes[0]
    if agreed and n_passes >= 2:
        if round_number > 1:
            consensus_banner = (
                f"✓ Конфликт разрешён на повторной проверке (раунд #{round_number}): "
                f"все {n_passes} прохода сошлись во мнении!"
            )
        else:
            consensus_banner = (
                f"✓ Консенсус {n_passes}/{n_passes} независимых проходов LLM: "
                "ответы полностью совпали"
            )
        # Склеиваем уникальные пояснения со всех проходов без дублей
        merged_warns: list[str] = [consensus_banner]
        for out in ordered_outcomes:
            for w in out.warnings:
                if w not in merged_warns:
                    merged_warns.append(w)
        selected = ParseOutcome(
            data=first.data,
            source=first.source,
            warnings=merged_warns,
            usage=total_usage,
            provider=first.provider,
            provider_label=f"Консенсус {n_passes}/{n_passes}",
            model=", ".join(o.model for o in ordered_outcomes if o.model),
            llm_mode=first.llm_mode,
        )
    else:
        selected = ParseOutcome(
            data=first.data,
            source=first.source,
            warnings=list(first.warnings),
            usage=total_usage,
            provider=first.provider,
            provider_label=first.provider_label,
            model=first.model,
            llm_mode=first.llm_mode,
        )

    return ConsensusResult(
        agreed=agreed,
        pass_outcomes=ordered_outcomes,
        pass_targets=pass_targets,
        discrepancies=discrepancies,
        selected_outcome=selected,
        total_usage=total_usage,
        round_number=round_number,
        prepared_text=combined,
        prepared_images=image_data_urls,
        base_warnings=base_warnings,
    )


def recheck_consensus_discrepancies(
    consensus: ConsensusResult,
    cfg: dict[str, Any] | None = None,
    *,
    key_secret: str = "",
    progress: Callable[[str], None] | None = None,
) -> ConsensusResult:
    """
    Повторный параллельный вызов тех же проходов («Попробуй разобраться ещё раз»)
    с инъекцией в промпт задания сосредоточиться именно на спорных пунктах.
    """
    focus_prompt = build_discrepancy_focus_prompt(consensus)
    return run_multipass_consensus(
        cfg=cfg,
        targets=consensus.pass_targets,
        key_secret=key_secret,
        progress=progress,
        focus_instruction=focus_prompt,
        prior_usage=consensus.total_usage,
        round_number=consensus.round_number + 1,
        prepared_input=(
            consensus.prepared_text,
            consensus.prepared_images,
            consensus.base_warnings,
        ),
    )
