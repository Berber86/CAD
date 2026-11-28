"""Disabled-by-default, developer-only search for every legal-description page.

This module does not invoke the existing parser or drawing pipeline. It sends
whole page images to RouterAI only when explicitly enabled by its caller.
Diagnostics and strictly matched search results are kept separately from the
PDF passport; no images, OCR text, provider bodies or credentials are stored.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
import developer_probability as dp
import file_metadata
import ocr_pages
from multiple_description_cancel import check
from multiple_description_progress import emit, page_list, waiting

PRIMARY = "microsoft/microsoft-decision-1.1"
FALLBACK = "typesafe/jev-1.14"
HYDRA_FALLBACK_MODEL = "gpt-6-luna"
HYDRA_BASE_URL = "https://api.hydraai.ru/v1"
PROVIDER = "routerai"
BASE_URL = "https://routerai.ru/api/v1"
PAGE_CAP = 200
THRESHOLD = 0.30
THRESHOLD_MIN = 0.30
THRESHOLD_MAX = 0.95
THRESHOLD_STEP = 0.05
SCHEMA = "titlesurveyai.legal_page_search.v1"
QUESTION = (
    "Does this page contain actual metes-and-bounds survey geometry or boundary calls of a parcel "
    "(such as BEGINNING/COMMENCING tie distances, THENCE courses, compass bearings, relative "
    "directions like \"parallel with\" or \"at right angles to\", boundary lengths in feet/inches, "
    "or a drawn survey plat with boundary dimensions), including an opening or continuation "
    "fragment? Answer NO if the page only has legal boilerplate, \"being the same premises\" "
    "recitals, covenants/subject-to clauses, address/Block/Lot references, cover sheets, "
    "tax forms, or signatures without boundary measurements."
)
HYDRA_SEARCH_PROMPT = (
    "Inspect the attached whole PDF pages (labeled with exact PDF page numbers). "
    "Identify EVERY page that contains ANY part (opening tie, middle courses, or closing "
    "continuation) of actual metes-and-bounds parcel boundary geometry: BEGINNING/COMMENCING "
    "corner tie distances, THENCE courses, compass bearings/angles, street-relative boundary "
    "calls (PARALLEL WITH / AT RIGHT ANGLES TO), boundary lengths in feet/inches, or a drawn "
    "survey plat with boundary measurements. "
    "Ignore pages that only contain deed cover sheets, recording stamps, Block/Lot/address "
    "references without boundary dimensions, \"being the same premises\" recitals, "
    "\"together with / subject to\" legal clauses, ACRIS/tax forms (RP-5217, TP-584), "
    "notary acknowledgments, or signatures unless boundary measurements appear on that page. "
    "Return STRICT JSON ONLY in this exact schema: {\"pages\": [<integer page numbers>]}."
)
COUNT_INSTRUCTIONS = ("Count distinct metes-and-bounds descriptions of land parcels on the supplied pages. "
                      "One description continued over multiple pages counts once. A title, deed cover, "
                      "map, address or lot reference alone is not a separate description. "
                      "Choose uncertain if you cannot determine the count.")
COUNT_CHOICES = {"zero": "No distinct metes-and-bounds description",
                 "one": "Exactly one distinct metes-and-bounds description",
                 "two": "Exactly two distinct metes-and-bounds descriptions",
                 "three": "Exactly three distinct metes-and-bounds descriptions",
                 "four_plus": "Four or more distinct metes-and-bounds descriptions",
                 "uncertain": "Cannot determine the number of distinct descriptions from these pages"}


class SearchDisabled(RuntimeError):
    """The developer switch is off; no PDF or network access has occurred."""


class SearchError(RuntimeError):
    """Generic, non-sensitive search failure."""


def enabled(config: Mapping[str, Any] | None) -> bool:
    section = config.get("multiple_description_search") if isinstance(config, Mapping) else None
    return isinstance(section, Mapping) and section.get("enabled") is True


def normalize_threshold(value: Any, *, default: float = THRESHOLD) -> float:
    """Clamp and snap a page-selection confidence threshold to 0.30..0.95 (step 0.05)."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    clamped = max(THRESHOLD_MIN, min(THRESHOLD_MAX, numeric))
    steps = round((clamped - THRESHOLD_MIN) / THRESHOLD_STEP)
    return round(THRESHOLD_MIN + steps * THRESHOLD_STEP, 2)


def configured_threshold(config: Mapping[str, Any] | None) -> float:
    """Return validated page-selection confidence threshold from config."""
    section = config.get("multiple_description_search") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        return THRESHOLD
    return normalize_threshold(section.get("threshold"), default=THRESHOLD)


def batch_numbers(count: int) -> list[list[int]]:
    if isinstance(count, bool) or not 1 <= count <= PAGE_CAP:
        raise ValueError("invalid page count")
    result = []
    first = 1
    while True:
        last = min(first + dp.ROUTERAI_IMAGE_LIMIT - 1, count)
        result.append(list(range(first, last + 1)))
        if last == count:
            return result
        first = last


def questions_for(numbers: list[int], *, count: bool) -> dict[str, dict[str, Any]]:
    if not numbers or len(numbers) > dp.ROUTERAI_IMAGE_LIMIT or len(set(numbers)) != len(numbers):
        raise ValueError("expected one to eight distinct pages")
    questions = {f"page_{n}": {"type": "noul", "instructions": f"For PDF page {n}: {QUESTION}"}
                 for n in numbers}
    if count:
        questions["count"] = {"type": "choice", "instructions": COUNT_INSTRUCTIONS, "criteria": COUNT_CHOICES}
    return questions


def _fingerprint(digest: str, pages: list[dict[str, Any]], questions: dict[str, Any],
                 model: str, image_side: int, base_url: str) -> str:
    # The exact image bytes and ordered questions/context are part of validity.
    payload = {"pdf_sha256": digest, "pages": [p["page"] for p in pages],
               "image_sha256": [hashlib.sha256(p["content"]).hexdigest() for p in pages],
               "image_side": image_side, "image_format": "png", "provider": PROVIDER,
               "base_url": base_url, "model": model, "questions": questions}
    return hashlib.sha256(json.dumps(payload, sort_keys=False, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def diagnostics_path(pdf: Path) -> Path:
    return pdf.with_name(pdf.name + ".page-search.json")


def _load_history(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"schema": SCHEMA, "runs": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SearchError("Cannot read existing search diagnostics; file was not overwritten.") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("runs"), list):
        raise SearchError("Existing search diagnostics have an incompatible format; file was not overwritten.")
    return data


def _cached(history: dict[str, Any], fingerprint: str, questions: dict[str, Any], model: str) -> dict[str, Any]:
    """Reuse only individually valid answers attached to the exact request fingerprint."""
    found: dict[str, Any] = {}
    for run in reversed(history["runs"]):
        if not isinstance(run, dict):
            continue
        stored_attempts = run.get("attempts", [])
        if not isinstance(stored_attempts, list):
            continue
        for attempt in reversed(stored_attempts):
            if not isinstance(attempt, dict) or attempt.get("fingerprint") != fingerprint or attempt.get("model") != model:
                continue
            answers = attempt.get("answers")
            if not isinstance(answers, dict):
                continue
            for key, q in questions.items():
                entry = answers.get(key)
                if key in found or not isinstance(entry, dict) or entry.get("status") != "ok":
                    continue
                value = entry.get("value")
                if q["type"] == "noul":
                    valid = isinstance(value, (float, int)) and not isinstance(value, bool) and 0 <= value <= 1 and math.isfinite(value)
                else:
                    valid = isinstance(value, str) and value in q["criteria"]
                if valid:
                    found[key] = value
    return found


def _request(pages: list[dict[str, Any]], questions: dict[str, Any], model: str,
             key: str, base_url: str, send: Callable[..., Any]) -> tuple[dict[str, Any], str | None, float, dict[str, float | int] | None]:
    start = time.monotonic()
    answers: dict[str, Any] = {}
    error = None
    usage = None
    try:
        state, assets = dp.make_state(pages, "visual")
        data = send(base_url, key, model, state, assets, questions)
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            usage = {}
            for field in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens", "cost", "total_cost"):
                value = data["usage"].get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1e18 and math.isfinite(value):
                    usage[field] = value
            usage = usage or None
        raw = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            raise ValueError("invalid response")
        for name, question in questions.items():
            try:
                result = dp.normalize_answer(raw.get(name), question)
                answers[name] = {"status": "ok", "value": result["value"] if question["type"] == "noul" else result["choice"]}
            except (ValueError, TypeError, KeyError):
                answers[name] = {"status": "error", "value": None}
    except httpx.HTTPStatusError as exc:
        error = f"http_{exc.response.status_code}"
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.HTTPError:
        error = "network_error"
    except dp.InvalidSystemOneResponse:
        error = "invalid_response"
    except Exception:
        error = "processing_error"
    if error:
        answers = {name: {"status": "error", "value": None} for name in questions}
    return answers, error, round(time.monotonic() - start, 3), usage


_OCR_OPENER_RE = re.compile(
    r"\b(?:all\s+that\s+(?:certa[i1l]n\s+)?(?:plot|p[i1l]ece|parcel|tract|lot)"
    r"|beg[i1l][nm]{2}[i1l]ng\b|commenc[i1l]ng\b|metes\s+and\s+bounds"
    r"|schedule\s+a\b|bou[nm]ded\s+and\s+descr[i1l]bed"
    r"|s[i1l]tuate[,\s]+ly[i1l]ng\s+and\s+be[i1l]ng"
    r"|po[i1l]nt\s+(?:or\s+place\s+)?of\s+beg[i1l][nm]{2}[i1l]ng)\b",
    re.IGNORECASE,
)
_OCR_CLOSING_POB_RE = re.compile(
    r"\bto\s+(?:the\s+)?(?:true\s+)?(?:po[i1l]nt(?:\s+or\s+place)?\s+of\s+|place\s+of\s+)?beg[i1l][nm]{2}[i1l]ng\b",
    re.IGNORECASE,
)
_OCR_THENCE_RE = re.compile(r"\b(?:runn[i1l]ng\s+)?t[hb]e[nmh]ce\b", re.IGNORECASE)
_OCR_RELATIVE_RE = re.compile(
    r"\b(?:parallel\s+w[i1l]th|at\s+r[i1l]ght\s+angles\s+to|inter[i1l]or\s+angle)\b",
    re.IGNORECASE,
)
_OCR_BEARING_RE = re.compile(
    r"\b[NS5]\s*\d{1,2}\s*(?:°|deg\w*|d|o|0|\s)\s*\d{1,2}"
    r"(?:\s*(?:'|′|min\w*|\s)\s*\d{1,2}(?:\.\d+)?)?\s*(?:\"|″|sec\w*)?\s*[EW]\b",
    re.IGNORECASE,
)
_OCR_DIST_RE = re.compile(
    r"\b\d{1,4}(?:[.,]\d{1,4})?\s*(?:f[eoc]{2}t|f[s]?et|ft\.?|cha[i1l]ns?|l[i1l]nks?|varas?|'|′)",
    re.IGNORECASE,
)
_OCR_DIR_CONTEXT_RE = re.compile(
    r"\b(?:north\w*|south\w*|east\w*|west\w*|along|corner|d[i1l]stant|s[i1l]de\s+of|party\s+wall|avenue|street|boulevard|road|lane|drive)\b",
    re.IGNORECASE,
)


def _normalize_ocr_noise(text: str) -> str:
    """Repair common RapidOCR character substitutions inside numeric/angle tokens."""
    if not text:
        return ""
    cleaned = text.replace("°", "°").replace("′", "'").replace("″", '"')
    # Fix O/o -> 0 and I/l -> 1 when adjacent to digits, decimals, or angle/foot punctuation.
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = re.sub(r"(?<=\d)[Oo](?=[0-9°'\".,\s])", "0", cleaned)
        cleaned = re.sub(r"(?<=[NSns\s])[Oo](?=\d)", "0", cleaned)
        cleaned = re.sub(r"(?<=\d)[Il|](?=[0-9°'\".,])", "1", cleaned)
        cleaned = re.sub(r"(?<=[0-9.])([Il|])(?=\d)", "1", cleaned)
    return cleaned


def _ocr_page_has_legal_description(text: str) -> tuple[bool, bool]:
    """Return (has_ld_cues, needs_next_page_continuation) resilient to OCR typos."""
    if not text or not text.strip():
        return False, False
    norm = _normalize_ocr_noise(text)
    opener = bool(_OCR_OPENER_RE.search(norm))
    bearings = len(_OCR_BEARING_RE.findall(norm))
    thence = len(_OCR_THENCE_RE.findall(norm))
    relative = bool(_OCR_RELATIVE_RE.search(norm))
    has_dist = bool(_OCR_DIST_RE.search(norm))
    has_dir = bool(_OCR_DIR_CONTEXT_RE.search(norm))
    dist_and_dir = has_dist and (has_dir or thence > 0)

    is_ld = (
        bearings >= 2
        or (bearings >= 1 and (thence >= 1 or dist_and_dir or opener))
        or (opener and (thence >= 1 or dist_and_dir or relative))
        or (thence >= 2 and dist_and_dir)
        or (relative and dist_and_dir)
    )
    if not is_ld:
        return False, False
    # Check whether closure back to POB already appears after any opening word.
    closing = bool(_OCR_CLOSING_POB_RE.search(norm))
    return True, not closing


def _chunk_numbers(numbers: list[int], size: int = dp.ROUTERAI_IMAGE_LIMIT) -> list[list[int]]:
    return [numbers[i:i + size] for i in range(0, len(numbers), size)]


def _hydra_questions(numbers: list[int]) -> dict[str, dict[str, Any]]:
    return {f"page_{n}": {"type": "noul", "instructions": f"hydra_ld_chunk:{n}"} for n in numbers}


def _hydra_fingerprint(digest: str, pages: list[dict[str, Any]], model: str,
                       image_side: int, base_url: str) -> str:
    payload = {"pdf_sha256": digest, "pages": [p["page"] for p in pages],
               "image_sha256": [hashlib.sha256(p["content"]).hexdigest() for p in pages],
               "image_side": image_side, "image_format": "png", "provider": "hydra",
               "base_url": base_url, "model": model, "prompt": HYDRA_SEARCH_PROMPT}
    return hashlib.sha256(json.dumps(payload, sort_keys=False, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _default_hydra_search_send(base_url: str, api_key: str, model: str,
                               pages: list[dict[str, Any]]) -> dict[str, Any]:
    import base64
    from multiple_description_parse import decode_json_object
    from multiple_description_transport import _call, create_client

    content: list[dict[str, Any]] = [
        {"type": "text", "text": "PDF page numbers attached: " + ", ".join(str(p["page"]) for p in pages)}
    ]
    for page in pages:
        b64 = base64.b64encode(page["content"]).decode("ascii")
        content.append({"type": "text", "text": f"PDF page {page['page']}"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    client = create_client({"api_key": api_key, "base_url": base_url}, timeout=90.0)
    try:
        raw = _call(
            client,
            model,
            [
                {"role": "system", "content": HYDRA_SEARCH_PROMPT},
                {"role": "user", "content": content},
            ],
            reasoning_effort="none",
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    parsed, _ = decode_json_object(raw, expected_keys=frozenset({"pages"}))
    return parsed


def _parse_hydra_pages_payload(payload: Any, allowed: set[int]) -> list[int]:
    if isinstance(payload, str):
        from multiple_description_parse import decode_json_object
        payload, _ = decode_json_object(payload, expected_keys=frozenset({"pages"}))
    if not isinstance(payload, dict) or set(payload) != {"pages"}:
        raise ValueError("invalid hydra search response")
    raw_pages = payload.get("pages")
    if not isinstance(raw_pages, list):
        raise ValueError("invalid hydra pages list")
    selected: list[int] = []
    for val in raw_pages:
        if isinstance(val, bool):
            raise ValueError("invalid hydra page entry")
        if isinstance(val, int):
            num = val
        elif isinstance(val, str) and val.strip().isdigit():
            num = int(val.strip())
        else:
            raise ValueError("invalid hydra page entry")
        if num in allowed and num not in selected:
            selected.append(num)
    return sorted(selected)


def _screen_pages_with_ocr(
    path: Path,
    numbers: list[int],
    *,
    ocr_dpi: int,
    ocr_fn: Callable[[Path, int], str] | None,
    current: Callable[[], bool],
    progress: Callable[[str], None] | None,
) -> dict[int, dict[str, Any]]:
    """Third-line defense: embedded text + local RapidOCR scan for unresolved pages."""
    results: dict[int, dict[str, Any]] = {}
    if not numbers:
        return results
    try:
        import pymupdf
    except Exception:  # noqa: BLE001
        return {n: {"inspected": False, "matched": True, "needs_next": False} for n in numbers}

    engine_ready = ocr_fn is not None or ocr_pages.available()
    try:
        doc = pymupdf.open(str(path))
    except Exception:  # noqa: BLE001
        return {n: {"inspected": False, "matched": True, "needs_next": False} for n in numbers}
    try:
        for n in numbers:
            check(current)
            if not 1 <= n <= len(doc):
                results[n] = {"inspected": False, "matched": True, "needs_next": False}
                continue
            page = doc.load_page(n - 1)
            try:
                embedded = page.get_text("text") or ""
            except Exception:  # noqa: BLE001
                embedded = ""
            matched, needs_next = _ocr_page_has_legal_description(embedded)
            inspected = bool(embedded.strip())
            try:
                has_images = bool(page.get_images(full=False))
            except Exception:  # noqa: BLE001
                has_images = False
            if not matched and (len(embedded.strip()) < 60 or has_images) and engine_ready:
                try:
                    if ocr_fn is not None:
                        ocr_text = ocr_fn(path, n)
                    else:
                        ocr_text = ocr_pages.ocr_pdf_pages(
                            path, n, n, dpi=ocr_dpi, progress=progress, use_cache=True,
                        )
                    ocr_matched, ocr_next = _ocr_page_has_legal_description(ocr_text or "")
                    matched = matched or ocr_matched
                    needs_next = needs_next or ocr_next
                    inspected = True
                except ocr_pages.OcrUnavailable:
                    pass
                except ocr_pages.OcrError:
                    # OcrError (e.g. blank image page with no recognized text) still inspected the page.
                    inspected = True
                except Exception:  # noqa: BLE001
                    pass
            results[n] = {
                "inspected": inspected,
                "matched": matched if inspected else True,
                "needs_next": needs_next if (inspected and matched) else False,
            }
    finally:
        doc.close()
    return results


def search_pdf(path: str | Path, config: Mapping[str, Any] | None, *, api_key: str,
               base_url: str = BASE_URL, image_side: int = dp.PDF_IMAGE_MAX_SIDE,
               threshold: float | None = None,
               send: Callable[..., Any] | None = None,
               hydra_api_key: str | None = None,
               hydra_base_url: str = HYDRA_BASE_URL,
               hydra_model: str = HYDRA_FALLBACK_MODEL,
               hydra_send: Callable[..., Any] | None = None,
               ocr_fallback: bool = False,
               ocr_dpi: int = 200,
               ocr_fn: Callable[[Path, int], str] | None = None,
               current: Callable[[], bool] = lambda: True,
               progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Evaluate up to 200 pages and save a diagnostic sidecar; never build geometry.

    The GUI calls this only after the user explicitly selects the experimental
    session mode. An unknown page with an available image is included in
    candidate_pages, not classified as empty.
    """
    if not enabled(config):
        raise SearchDisabled("Developer page search is disabled.")
    check(current)
    active_threshold = (
        configured_threshold(config)
        if threshold is None
        else normalize_threshold(threshold)
    )
    if not api_key:
        raise SearchError("RouterAI key is unavailable; no requests sent.")
    dp.valid_pdf_image_side(image_side)
    if not base_url.startswith("https://"):
        raise SearchError("An HTTPS provider endpoint is required.")
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        raise SearchError("Expected a PDF.")
    target = diagnostics_path(path)
    # Read and validate the sidecar before any paid request.
    with file_metadata._LOCK:
        history = _load_history(target)
    try:
        import pymupdf
        digest = file_metadata._digest(path)
        with pymupdf.open(path) as doc:
            total = len(doc)
            if doc.needs_pass or total < 1:
                raise ValueError("unreadable PDF")
    except Exception as exc:
        raise SearchError("Cannot open the PDF; no requests sent.") from exc
    covered = min(total, PAGE_CAP)
    groups = batch_numbers(covered)
    emit(progress, f"Поиск: подготовка {covered} из {total} страниц PDF.")
    rendered: dict[int, dict[str, Any]] = {}
    render_errors = []
    for n in range(1, covered + 1):
        check(current)
        if n == 1 or n == covered or n % 10 == 0:
            emit(progress, f"Поиск: подготовка страницы {n} из {covered}.")
        try:
            page = dp.prepare_documents([(str(path), str(n))], "visual", provider=PROVIDER,
                                        pdf_image_side=image_side)
            if len(page) != 1 or page[0]["page"] != n or not isinstance(page[0]["content"], bytes):
                raise ValueError("invalid render")
            rendered[n] = {**page[0], "file": "document.pdf"}
        except Exception:
            render_errors.append(n)
    if file_metadata._digest(path) != digest:
        raise SearchError("PDF changed while preparing pages; no requests sent.")
    send = send or dp.post_routerai
    attempts: list[dict[str, Any]] = []
    results = []
    auth_rejected = threading.Event()
    auth_lock = threading.Lock()
    auth_notified = False

    def process_group(group: list[int]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Один батч страниц: кэш, основная модель, ограниченные повторы, запасная.

        Батчи независимы и выполняются параллельно (см. ниже); общие данные
        здесь только читаются, а попытки копятся в локальный список.
        Недооценённая страница не теряется: она попадает в кандидаты как
        неполная, поэтому гнаться за оценкой шестью запросами подряд не нужно.
        """
        check(current)
        local_attempts: list[dict[str, Any]] = []
        available = [n for n in group if n in rendered]
        pending = list(available)
        observed: dict[int, dict[str, Any]] = {}
        count_result: dict[str, Any] = {"status": "unknown", "value": None}

        def attempt(numbers: list[int], model: str, include_count: bool) -> bool:
            nonlocal auth_notified, count_result
            check(current)
            pages = [rendered[n] for n in numbers]
            questions = questions_for(numbers, count=include_count)
            fp = _fingerprint(digest, pages, questions, model, image_side, base_url)
            cached = _cached(history, fp, questions, model)
            if cached:
                emit(progress, f"Поиск: кэш {model}, страницы {page_list(numbers)}.")
                answers = {name: ({"status": "ok", "value": cached[name]} if name in cached else
                                  {"status": "error", "value": None}) for name in questions}
                error, seconds, source, usage = None, 0.0, "cache", None
            elif auth_rejected.is_set():
                return False
            else:
                emit(progress, f"Поиск: запрос {model}, страницы {page_list(numbers)}; ожидаем ответ.")
                check(current)
                with waiting(progress):
                    answers, error, seconds, usage = _request(pages, questions, model, api_key, base_url, send)
                check(current)
                source = "network"
            valid_pages = sum(answers[f"page_{n}"]["status"] == "ok" for n in numbers)
            error_text = {"timeout": "тайм-аут", "network_error": "ошибка сети",
                          "invalid_response": "некорректный ответ", "processing_error": "ошибка обработки"}
            problem = error_text.get(error, error if error and error.startswith('http_') else "ошибка запроса") if error else ""
            emit(progress, f"Поиск: ответ {model}, страниц с оценкой {valid_pages}/{len(numbers)}, "
                           f"{seconds:.1f} с" + (f", {problem}" if problem else "") + ".")
            local_attempts.append({"batch_pages": group, "pages": numbers, "model": model,
                                   "fingerprint": fp, "source": source, "seconds": seconds, "usage": usage,
                                   "error_kind": error, "answers": answers})
            for n in numbers:
                answer = answers[f"page_{n}"]
                if answer["status"] == "ok" and n not in observed:
                    observed[n] = {"value": answer["value"], "model": model, "source": source}
            if include_count and count_result["status"] != "ok" and answers["count"]["status"] == "ok":
                count_result = {"status": "ok", "value": answers["count"]["value"],
                                "model": model, "source": source}
            if error in ("http_401", "http_403"):
                auth_rejected.set()
                notify = False
                with auth_lock:
                    if not auth_notified:
                        auth_notified = True
                        notify = True
                if notify:
                    emit(progress, "Поиск: доступ к RouterAI отклонён (ошибка авторизации) — "
                                   "дальнейшие запросы поиска остановлены.")
                return False
            return True

        if pending:
            # A complete initial batch may use Jev cache when its precise request
            # context matches, even though Microsoft is the primary model.
            initial_qs = questions_for(pending, count=len(available) == len(group))
            initial_pages = [rendered[n] for n in pending]
            used_cache = False
            for model in (PRIMARY, FALLBACK):
                fp = _fingerprint(digest, initial_pages, initial_qs, model, image_side, base_url)
                if _cached(history, fp, initial_qs, model):
                    # Keep original context for every cache lookup; do not replace
                    # a previously accepted answer with a later cached answer.
                    attempt(available, model, len(available) == len(group))
                    used_cache = True
            pending = [n for n in available if n not in observed]
            if not used_cache and pending and not auth_rejected.is_set():
                attempt(pending, PRIMARY, len(available) == len(group))
                pending = [n for n in pending if n not in observed]
            # A cache read costs no live attempt. When some cached answers are
            # missing, ask only those pages, without retrying count by itself.
            # Повторов немного: недооценённая страница всё равно попадёт в
            # кандидаты как неполная, а живой прогон 24.11.2026 показал, как
            # один батч гоняли пять раз по ~150 с тайм-аута подряд.
            microsoft_attempts = 0 if used_cache else 1
            while pending and microsoft_attempts < 2 and not auth_rejected.is_set():
                if not attempt(pending, PRIMARY, False):
                    break
                microsoft_attempts += 1
                pending = [n for n in pending if n not in observed]
            for _ in range(2):
                if not pending or auth_rejected.is_set():
                    break
                if not attempt(pending, FALLBACK, False):
                    break
                pending = [n for n in pending if n not in observed]
        return {"pages": group, "count": count_result,
                "observations": {str(n): observed.get(n) for n in group}}, local_attempts

    # Батчи независимы: страницы уже отрисованы, запросы друг друга не ждут.
    # Порядок результатов сохраняем по исходному списку батчей.
    if len(groups) <= 1:
        batch_outputs = [process_group(groups[0])] if groups else []
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(groups)),
                                thread_name_prefix="page-search") as pool:
            batch_outputs = list(pool.map(process_group, groups))
    for batch_result, batch_attempts in batch_outputs:
        results.append(batch_result)
        attempts.extend(batch_attempts)
    check(current)

    # Identify pages that Tier 1 (RouterAI Microsoft / Jev) did not answer.
    unresolved = [
        n for n in range(1, covered + 1)
        if n in rendered and all(b["observations"][str(n)] is None for b in results if n in b["pages"])
    ]
    hydra_enabled = bool((hydra_api_key or hydra_send is not None) and hydra_base_url.startswith("https://"))
    hydra_observed: dict[int, dict[str, Any]] = {}
    ocr_observed: dict[int, dict[str, Any]] = {}
    if unresolved and (hydra_enabled or ocr_fallback):
        check(current)
        if hydra_enabled and ocr_fallback:
            emit(progress, f"Поиск: RouterAI не ответил по {len(unresolved)} стр. — "
                           f"параллельно отправляю чанки по 8 стр. в Hydra ({hydra_model}) и запускаю локальный OCR.")
        elif hydra_enabled:
            emit(progress, f"Поиск: RouterAI не ответил по {len(unresolved)} стр. — "
                           f"отправляю чанки по 8 стр. в Hydra ({hydra_model}).")
        else:
            emit(progress, f"Поиск: RouterAI не ответил по {len(unresolved)} стр. — "
                           "проверяю страницы локальным OCR.")

        hydra_auth_failed = threading.Event()
        hydra_chunks = _chunk_numbers(unresolved, dp.ROUTERAI_IMAGE_LIMIT) if hydra_enabled else []
        hydra_caller = hydra_send or _default_hydra_search_send

        def run_hydra_chunk(chunk: list[int]) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
            check(current)
            chunk_pages = [rendered[n] for n in chunk]
            questions = _hydra_questions(chunk)
            fp = _hydra_fingerprint(digest, chunk_pages, hydra_model, image_side, hydra_base_url)
            cached = _cached(history, fp, questions, hydra_model)
            answers: dict[str, Any] = {}
            error_kind: str | None = None
            source = "hydra_network"
            started = time.monotonic()
            chunk_obs: dict[int, dict[str, Any]] = {}
            if cached and len(cached) == len(chunk):
                source = "hydra_cache"
                for n in chunk:
                    val = cached[f"page_{n}"]
                    answers[f"page_{n}"] = {"status": "ok", "value": val}
                    chunk_obs[n] = {"value": val, "model": hydra_model, "source": source}
                emit(progress, f"Поиск: кэш Hydra {hydra_model}, страницы {page_list(chunk)}.")
            elif hydra_auth_failed.is_set():
                error_kind = "http_401"
                answers = {name: {"status": "error", "value": None} for name in questions}
            else:
                emit(progress, f"Поиск: запрос Hydra {hydra_model}, страницы {page_list(chunk)}.")
                check(current)
                try:
                    with waiting(progress):
                        raw_resp = hydra_caller(hydra_base_url, hydra_api_key or "", hydra_model, chunk_pages)
                    check(current)
                    matched = set(_parse_hydra_pages_payload(raw_resp, set(chunk)))
                    for n in chunk:
                        val = 1.0 if n in matched else 0.0
                        answers[f"page_{n}"] = {"status": "ok", "value": val}
                        chunk_obs[n] = {"value": val, "model": hydra_model, "source": source}
                except Exception as exc:  # noqa: BLE001
                    status = getattr(exc, "status_code", None)
                    if status is None and isinstance(exc, httpx.HTTPStatusError):
                        status = exc.response.status_code
                    if status in (401, 403):
                        error_kind = f"http_{status}"
                        hydra_auth_failed.set()
                    elif isinstance(exc, httpx.TimeoutException) or "timeout" in str(exc).lower():
                        error_kind = "timeout"
                    else:
                        error_kind = f"http_{status}" if status else "processing_error"
                    answers = {name: {"status": "error", "value": None} for name in questions}
            seconds = round(time.monotonic() - started, 3)
            emit(progress, f"Поиск: ответ Hydra {hydra_model}, страниц {len(chunk_obs)}/{len(chunk)}, "
                           f"{seconds:.1f} с" + (f", {error_kind}" if error_kind else "") + ".")
            attempt_rec = {
                "batch_pages": chunk, "pages": chunk, "model": hydra_model,
                "fingerprint": fp, "source": source, "seconds": seconds,
                "usage": None, "error_kind": error_kind, "answers": answers,
            }
            return chunk_obs, attempt_rec

        worker_count = max(1, (min(4, len(hydra_chunks)) if hydra_chunks else 0) + (1 if ocr_fallback else 0))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="page-search-fallback") as pool:
            ocr_future = (
                pool.submit(
                    _screen_pages_with_ocr,
                    path, unresolved, ocr_dpi=ocr_dpi, ocr_fn=ocr_fn,
                    current=current, progress=progress,
                )
                if ocr_fallback else None
            )
            hydra_futures = [pool.submit(run_hydra_chunk, chunk) for chunk in hydra_chunks]
            for fut in hydra_futures:
                chunk_obs, attempt_rec = fut.result()
                hydra_observed.update(chunk_obs)
                attempts.append(attempt_rec)
            if ocr_future is not None:
                ocr_observed = ocr_future.result()
        check(current)

    # Compute OCR continuation flags (if page n started an LD without closing POB, include n + 1).
    ocr_continuation: set[int] = set()
    for n in unresolved:
        info = ocr_observed.get(n)
        if info and info.get("inspected") and info.get("matched") and info.get("needs_next"):
            if (n + 1) in rendered and (n + 1) in unresolved:
                ocr_continuation.add(n + 1)

    page_results = []
    for n in range(1, covered + 1):
        obs = [b["observations"][str(n)] for b in results if n in b["pages"]]
        valid = [x["value"] for x in obs if x is not None]
        if valid:
            incomplete = any(x is None for x in obs)
            maximum = max(valid)
            candidate = n in rendered and (incomplete or maximum >= active_threshold)
        elif n in hydra_observed:
            h_obs = hydra_observed[n]
            obs = [h_obs]
            maximum = h_obs["value"]
            incomplete = False
            candidate = n in rendered and maximum >= active_threshold
        elif n in ocr_observed:
            o_info = ocr_observed[n]
            inspected = bool(o_info.get("inspected"))
            matched = bool(o_info.get("matched")) or (n in ocr_continuation)
            obs = [{"value": None, "model": "ocr", "source": "ocr_fallback",
                    "inspected": inspected, "matched": matched}]
            maximum = None
            incomplete = True
            candidate = n in rendered and (matched if inspected else True)
        else:
            incomplete = any(x is None for x in obs)
            maximum = None
            candidate = n in rendered and incomplete
        page_results.append({"page": n, "observations": obs, "max_probability": maximum,
                             "incomplete": incomplete, "image_available": n in rendered,
                             "candidate": candidate})
    run = {"created_at": datetime.now(timezone.utc).isoformat(), "pdf_sha256": digest,
           "source_name": path.name, "pdf_pages": total, "covered_pages": covered,
           "uncovered_pages": total - covered, "render_errors": render_errors,
           "coverage_complete": total == covered and not any(p["incomplete"] for p in page_results),
           "threshold": active_threshold, "batches": results, "pages": page_results,
           "candidate_pages": [p["page"] for p in page_results if p["candidate"]],
           "count_scope": "document" if len(groups) == 1 and total == covered else "batch_only",
           "attempts": attempts}
    try:
        if file_metadata._digest(path) != digest:
            raise SearchError("PDF changed during search; results were not saved.")
        with file_metadata._LOCK:
            saved_history = _load_history(target)
            saved_history["runs"].append(run)
            file_metadata._write_json(target, saved_history)
    except SearchError:
        raise
    except OSError as exc:
        raise SearchError("Cannot save search diagnostics.") from exc
    emit(progress, f"Поиск завершён: отобрано страниц {len(run['candidate_pages'])}, "
                   f"полный просмотр: {'да' if run['coverage_complete'] else 'нет'}.")
    return copy.deepcopy(run)
