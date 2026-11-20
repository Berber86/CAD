"""System One document comparison for the developer window.

No document, response or API key is persisted. Visual input is experimental:
this uses the TypeSafe asset format, whose Hydra support is not established.
"""
from __future__ import annotations

import base64
import concurrent.futures
import io
import json
import math
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import systemone_diagnostics

MODELS = ("jev-1.14", "solar-decide")
MAX_OPTIONS = 26  # conservative Solar limit; reserve two options for absence/multiple
PAGE_GROUP = MAX_OPTIONS - 2
MAX_QUESTIONS = 32
MAX_IMAGES = 100
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_PAYLOAD_BYTES = 40 * 1024 * 1024
DEFAULT_QUESTIONS = [
    {"type": "noul", "text": text} for text in (
        "Есть ли здесь координаты, румбы или расстояния границ земельного участка?",
        "Does this image contain land parcel coordinates, bearings, or boundary distances?",
        "Содержит ли документ геодезические данные участка: координаты, румбы или расстояния его границ?",
        "Does the document contain land parcel coordinates, bearings, or boundary distances?",
        "Содержит ли документ пространственные геодезические координаты, румбы или расстояния, определяющие положение или границы земельного участка?",
        "Does the document contain geodetic coordinates, bearings, or distances identifying the location or boundaries of the land parcel?",
    )
] + [{"type": "pages", "text": "На какой странице содержатся координаты, румбы или расстояния границ земельного участка?"},
     {"type": "pages", "text": "Which page contains land parcel coordinates, bearings, or boundary distances?"}]


def settings_path() -> Path:
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "TitleSurveyAI" / "developer_probability.json"
    return Path.home() / ".cache" / "titlesurveyai" / "developer_probability.json"


def load_settings(path: Path | None = None) -> dict[str, Any]:
    try:
        value = json.loads((path or settings_path()).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("questions"), list):
            raise ValueError("bad settings")
        questions = [q for q in value["questions"] if isinstance(q, dict) and q.get("type") in ("noul", "score", "pages")]
        return {"mode": value.get("mode") if value.get("mode") in ("ocr", "visual") else "ocr", "questions": questions}
    except (OSError, ValueError, TypeError):
        return {"mode": "ocr", "questions": [dict(q) for q in DEFAULT_QUESTIONS]}


def save_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    target = path or settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Whitelist only question and settings fields; never write document paths/results/keys.
    questions = [{k: q[k] for k in ("type", "text", "low", "high", "levels", "active") if k in q}
                 for q in settings.get("questions", []) if isinstance(q, dict)]
    payload = {"mode": settings.get("mode", "ocr"), "questions": questions}
    fd, tmp = tempfile.mkstemp(prefix=".developer-probability-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def parse_pages(spec: str, count: int) -> list[int]:
    if not spec.strip():
        return list(range(1, count + 1))
    selected: set[int] = set()
    for item in spec.split(","):
        item = item.strip()
        if not item:
            raise ValueError("Пустой элемент в списке страниц")
        pieces = item.split("-")
        if len(pieces) not in (1, 2) or not all(p.strip().isdigit() for p in pieces):
            raise ValueError("Страницы задаются, например, 1-3,5")
        a = int(pieces[0]); b = int(pieces[-1])
        if a < 1 or b < a or b > count:
            raise ValueError(f"Страницы должны быть от 1 до {count}")
        selected.update(range(a, b + 1))
    return sorted(selected)


def build_questions(questions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not questions or len(questions) > MAX_QUESTIONS:
        raise ValueError(f"Нужно от 1 до {MAX_QUESTIONS} вопросов")
    result: dict[str, dict[str, Any]] = {}
    for idx, q in enumerate(questions):
        text = str(q.get("text", "")).strip()
        kind = q.get("type")
        if not text or kind not in ("noul", "score", "pages"):
            raise ValueError(f"Заполните вопрос {idx + 1}")
        if kind == "pages":
            continue
        item: dict[str, Any] = {"type": kind, "instructions": text}
        if kind == "score":
            levels = q.get("levels", [])
            if isinstance(levels, str):
                levels = [v.strip() for v in levels.split("|") if v.strip()]
            if not levels:
                low, high = str(q.get("low", "")).strip(), str(q.get("high", "")).strip()
                if not low or not high:
                    raise ValueError(f"Для шкалы вопроса {idx + 1} укажите значения 0 и 1")
                levels = [low, high]
            if not 2 <= len(levels) <= 10 or any(not str(v).strip() for v in levels):
                raise ValueError("Шкала должна содержать от 2 до 10 описанных уровней")
            item["criteria"] = list(levels)
        result[f"q{idx + 1}"] = item
    return result


def _to_png(image: Any) -> bytes:
    from PIL import ImageOps
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((1568, 1568))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data = buf.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение слишком велико для экспериментального API")
    return data


def _ocr(image: Any) -> str:
    # No persistent temporary files or OCR cache.
    import numpy as np
    from PIL import ImageOps
    import ocr_pages
    image = ImageOps.exif_transpose(image).convert("RGB")
    array = np.asarray(image)[:, :, ::-1].copy()  # RapidOCR expects BGR
    result, _ = ocr_pages.run_engine(ocr_pages._engine_or_raise(), array)
    text = ocr_pages._lines_to_text(result).strip()
    if not text:
        raise ValueError("Локальный OCR не обнаружил текста на странице")
    return ocr_pages.restore_word_breaks(text)


def prepare_documents(files: list[tuple[str, str]], mode: str) -> list[dict[str, Any]]:
    """Page records with only in-memory text or PNG bytes, never both."""
    if not files or mode not in ("ocr", "visual"):
        raise ValueError("Выберите файл и режим обработки")
    from PIL import Image
    import pymupdf
    pages: list[dict[str, Any]] = []
    for filename, spec in files:
        path = Path(filename)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            with pymupdf.open(str(path)) as pdf:
                if pdf.needs_pass:
                    raise ValueError(f"PDF защищён паролем: {path.name}")
                selected = parse_pages(spec, len(pdf))
                if len(pages) + len(selected) > MAX_IMAGES:
                    raise ValueError(f"За один запуск допускается не более {MAX_IMAGES} страниц и изображений")
                for page_no in selected:
                    page = pdf[page_no - 1]
                    native = page.get_text("text").strip() if mode == "ocr" else ""
                    # A page with a small text layer may still contain a scan. OCR it
                    # instead of assuming that the text layer represents the whole page.
                    if native and not page.get_images(full=True):
                        value: str | bytes = native
                    else:
                        scale = min(2.0, 2400 / max(page.rect.width, page.rect.height))
                        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                        image = Image.open(io.BytesIO(pix.tobytes("png")))
                        if mode == "ocr":
                            scanned = _ocr(image)
                            value = (native + "\n[OCR]\n" + scanned) if native else scanned
                        else:
                            value = _to_png(image)
                    pages.append({"file": path.name, "page": page_no, "content": value})
        elif suffix in (".jpg", ".jpeg", ".png"):
            if spec.strip():
                raise ValueError("Номера страниц можно задавать только для PDF")
            with Image.open(path) as image:
                value = _ocr(image) if mode == "ocr" else _to_png(image)
            pages.append({"file": path.name, "page": None, "content": value})
        else:
            raise ValueError("Поддерживаются PDF, JPG и PNG")
        if len(pages) > MAX_IMAGES:
            raise ValueError(f"За один запуск допускается не более {MAX_IMAGES} страниц и изображений")
    if not pages:
        raise ValueError("Не выбрано ни одной страницы")
    return pages


def make_state(pages: list[dict[str, Any]], mode: str) -> tuple[dict[str, Any], dict[str, Any]]:
    state: dict[str, Any] = {}
    assets: dict[str, Any] = {}
    for i, page in enumerate(pages, 1):
        label = f"page_{i}"
        description = f"{page['file']} — page {page['page']}" if page['page'] else page['file']
        state[label + "_description"] = description
        if mode == "visual":
            ref = str(uuid.uuid4())
            state[label] = ref
            assets[ref] = {"type": "image", "format": "png", "data_base64": base64.b64encode(page["content"]).decode("ascii")}
        else:
            state[label] = page["content"]
    return state, assets


class SystemOneResponse(dict):
    """Parsed successful response with the original HTTP response attached."""

    def __init__(self, data: dict[str, Any], response: httpx.Response) -> None:
        super().__init__(data)
        self.http_response = response


class InvalidSystemOneResponse(ValueError):
    """A successful HTTP status with an invalid System One response body."""

    def __init__(self, message: str, response: httpx.Response) -> None:
        super().__init__(message)
        self.http_response = response


def _capture_provider_response(response: httpx.Response) -> dict[str, Any]:
    """Capture the complete HTTP status, headers and body for the user's window.

    This intentionally does not filter sensitive content: the user explicitly
    requested complete provider responses. It must never write to disk or logs.
    Binary/non-decodable bodies are represented losslessly as hex.
    """
    try:
        body = response.content.decode(response.encoding or "utf-8", errors="strict")
        encoding = "текст"
    except (UnicodeError, LookupError):
        body = response.content.hex()
        encoding = "hex (байты ответа)"
    headers = [(name.decode("latin-1"), value.decode("latin-1"))
               for name, value in response.headers.raw]
    return {"status": response.status_code, "http_version": response.http_version,
            "reason": response.reason_phrase, "headers": headers, "body": body, "encoding": encoding}


def post_systemone(base_url: str, api_key: str, model: str, state: dict[str, Any], assets: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
    if not api_key:
        raise ValueError("Не настроен ключ Hydra")
    if not base_url.startswith("https://"):
        raise ValueError("Для Hydra требуется HTTPS-адрес API")
    payload: dict[str, Any] = {"model": model, "state": state, "questions": questions}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if assets:
        payload["assets"] = assets
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValueError("Запрос слишком велик; выберите меньше страниц")
    response = httpx.post(base_url.rstrip("/") + "/systemone", content=body, headers=headers, timeout=120.0, follow_redirects=False)
    response.raise_for_status()
    try:
        data = response.json()
    except (ValueError, RecursionError) as exc:
        raise InvalidSystemOneResponse("API вернул некорректный JSON", response) from exc
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise InvalidSystemOneResponse("API не вернул ответы System One", response)
    return SystemOneResponse(data, response)


def normalize_answer(answer: Any, question: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("type") != question["type"]:
        raise ValueError("API вернул ответ неправильного типа")
    result: dict[str, Any] = {"type": answer["type"]}
    if question["type"] == "noul":
        value = answer.get("noul")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("API вернул неверную вероятность")
        result["value"] = float(value)
    elif question["type"] == "score":
        probs = answer.get("probabilities")
        count = len(question["criteria"])
        if not isinstance(probs, dict) or set(probs) != {str(i) for i in range(count)}:
            raise ValueError("API не вернул распределение шкалы")
        values = list(probs.values())
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in values) or not math.isclose(sum(values), 1, abs_tol=0.02):
            raise ValueError("API вернул неверное распределение шкалы")
        result["value"] = sum(i * probs[str(i)] for i in range(count)) / ((count - 1) * sum(values))
        result["probabilities"] = probs
        result["levels"] = question["criteria"]
    elif question["type"] == "choice":
        choice = answer.get("choice")
        if choice not in question["criteria"]:
            raise ValueError("API вернул неизвестный вариант")
        result["choice"] = choice
        result["probabilities"] = answer.get("probabilities")
    if isinstance(answer.get("confidence"), (int, float)) and not isinstance(answer.get("confidence"), bool):
        result["confidence"] = answer["confidence"]
    return result


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        message = f"Hydra вернула HTTP {status}."
        if status == 422:
            message += " " + systemone_diagnostics.summarize_validation_error(exc.response)
        return message + " Если выбран графический режим, он может не поддерживаться; OCR не подставлен."
    if isinstance(exc, httpx.TimeoutException):
        return "Превышено время ожидания Hydra."
    if isinstance(exc, httpx.HTTPError):
        return "Ошибка соединения с Hydra."
    if isinstance(exc, ValueError):
        return str(exc)
    try:
        import ocr_pages
        if isinstance(exc, ocr_pages.OcrError):
            return "Локальный OCR недоступен или не смог распознать документ. Проверьте установку requirements-ocr.txt."
    except ImportError:
        pass
    return "Не удалось обработать запрос. Проверьте файл, OCR и настройки Hydra."


def _usage(data: dict[str, Any]) -> dict[str, Any]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {k: v for k, v in usage.items() if isinstance(k, str) and not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)}


def run_model(model: str, base_url: str, api_key: str, pages: list[dict[str, Any]], mode: str, questions: list[dict[str, Any]], send=post_systemone) -> dict[str, Any]:
    """One model, independently; errors never replace the other model's output."""
    start = time.monotonic()
    entries: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []
    timings: list[float] = []
    provider_responses: list[dict[str, Any]] = []
    request_context: tuple[list[dict[str, Any]], int] | None = None
    try:
        normal = build_questions(questions)
        page_questions = [(i, q) for i, q in enumerate(questions) if q["type"] == "pages"]
        if page_questions and (len({p['file'] for p in pages}) != 1 or any(p['page'] is None for p in pages)):
            raise ValueError("Поиск страницы доступен только для одного PDF")
        def request(selected: list[dict[str, Any]], qs: dict[str, Any]) -> dict[str, Any]:
            nonlocal request_context
            request_context = (selected, len(qs))
            state, assets = make_state(selected, mode)
            started = time.monotonic()
            try:
                response = send(base_url, api_key, model, state, assets, qs)
            except httpx.HTTPStatusError as exc:
                provider_responses.append(_capture_provider_response(exc.response))
                raise
            except InvalidSystemOneResponse as exc:
                provider_responses.append(_capture_provider_response(exc.http_response))
                raise
            finally:
                timings.append(time.monotonic() - started)
            if isinstance(response, SystemOneResponse):
                provider_responses.append(_capture_provider_response(response.http_response))
            usages.append(_usage(response))
            return response["answers"]
        if normal:
            raw = request(pages, normal)
            for key, q in normal.items():
                try:
                    entries.append({"question": questions[int(key[1:]) - 1]["text"], "answer": normalize_answer(raw.get(key), q)})
                except ValueError as exc:
                    entries.append({"question": questions[int(key[1:]) - 1]["text"], "error": str(exc)})
        for _, q in page_questions:
            found: list[int] = []
            details: list[dict[str, Any]] = []
            groups_with_data = 0
            for offset in range(0, len(pages), PAGE_GROUP):
                group = pages[offset:offset + PAGE_GROUP]
                criteria = {f"p{p['page']}": f"Page {p['page']} contains the relevant data and no other page in this group does" for p in group}
                criteria.update({"none": "No page in this group contains the relevant data", "multiple": "More than one page in this group contains the relevant data"})
                choice_q = {"type": "choice", "instructions": q["text"], "criteria": criteria}
                choice = normalize_answer(request(group, {"page": choice_q}).get("page"), choice_q)
                details.append({"group": [p["page"] for p in group], "answer": choice})
                selected = choice["choice"]
                if selected != "none":
                    groups_with_data += 1
                if selected == "multiple":
                    boundary_criteria = {f"p{p['page']}": f"Page {p['page']}" for p in group}
                    # Include uncertainty instead of forcing a fabricated page.
                    boundary_criteria["none"] = "No relevant data or cannot identify a page"
                    first = {"type": "choice", "instructions": q["text"] + " Identify the FIRST page in this group containing the requested data.", "criteria": boundary_criteria}
                    last = {"type": "choice", "instructions": q["text"] + " Identify the LAST page in this group containing the requested data.", "criteria": boundary_criteria}
                    result = request(group, {"first": first, "last": last})
                    a = normalize_answer(result.get("first"), first); b = normalize_answer(result.get("last"), last)
                    details.append({"first": a, "last": b})
                    if a["choice"] == "none" or b["choice"] == "none":
                        raise ValueError("Не удалось определить первую и последнюю страницы")
                    aa, bb = int(a["choice"][1:]), int(b["choice"][1:])
                    if aa >= bb:
                        raise ValueError("Модель не смогла определить разные первую и последнюю страницы")
                    found.extend((aa, bb))
                elif selected != "none":
                    found.append(int(selected[1:]))
            if groups_with_data > 1:
                # Resolve global first/last among the candidates from each group;
                # send only candidate pages, not an oversized full document again.
                candidates = sorted(set(found))
                candidate_pages = [p for p in pages if p["page"] in candidates]
                criteria = {f"p{number}": f"Page {number}" for number in candidates}
                criteria["none"] = "No relevant data or cannot identify a page"
                first = {"type": "choice", "instructions": q["text"] + " Identify the FIRST page among the candidate pages containing the requested data.", "criteria": criteria}
                last = {"type": "choice", "instructions": q["text"] + " Identify the LAST page among the candidate pages containing the requested data.", "criteria": criteria}
                raw = request(candidate_pages, {"first": first, "last": last})
                a = normalize_answer(raw.get("first"), first); b = normalize_answer(raw.get("last"), last)
                details.append({"candidate_pages": candidates, "first": a, "last": b})
                if a["choice"] == "none" or b["choice"] == "none":
                    raise ValueError("Не удалось определить первую и последнюю страницы среди групп")
                aa, bb = int(a["choice"][1:]), int(b["choice"][1:])
                if aa >= bb:
                    raise ValueError("Модель не смогла определить разные первую и последнюю страницы среди групп")
                found = [aa, bb]
            entries.append({"question": q["text"], "answer": {"type": "pages", "first": min(found) if found else None, "last": max(found) if found else None, "details": details}})
        return {"model": model, "seconds": time.monotonic() - start, "entries": entries, "usage": usages, "request_seconds": timings, "provider_responses": provider_responses}
    except Exception as exc:  # preserve independent results and redact network details
        message = _safe_error(exc)
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 422 and mode == "visual" and request_context:
            selected, count = request_context
            message += " " + systemone_diagnostics.describe_visual_inputs(
                [page["content"] for page in selected], count)
        return {"model": model, "seconds": time.monotonic() - start, "entries": entries, "usage": usages, "request_seconds": timings, "error": message, "provider_responses": provider_responses}


def run_both(base_url: str, api_key: str, pages: list[dict[str, Any]], mode: str, questions: list[dict[str, Any]], send=post_systemone) -> list[dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_model, model, base_url, api_key, pages, mode, questions, send) for model in MODELS]
        return [f.result() for f in futures]
