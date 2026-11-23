"""Секретный тест моделей на построение property line (окно разработчика).

Четыре свободных слота «провайдер + модель + уровень рассуждений» получают
ОДИН и тот же подготовленный вход (страницы PDF или фото — картинками
Vision), запускаются параллельно, каждый результат превращается в DrawPlan
той же геометрией, что и основной Шаг 1. Сбой одного слота не роняет
остальные. Чистая логика вынесена сюда, окно — в model_test_ui.py.
"""
from __future__ import annotations

import concurrent.futures
import copy
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import llm_parser as lp
from acad_builder import AcadBuilder

SLOT_COUNT = 4

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}


@dataclass
class ModelTestSlot:
    provider: str
    model: str
    reasoning: str = ""  # "" — уровень не задан (дефолт провайдера / без параметра)


@dataclass
class SlotResult:
    slot: ModelTestSlot
    outcome: Any = None  # lp.ParseOutcome
    plan: Any = None  # acad_builder.DrawPlan
    error: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome is not None and self.plan is not None


def default_slots(cfg: dict[str, Any]) -> list[ModelTestSlot]:
    """Стартовые слоты: активная модель программы + следующие провайдеры по кругу."""
    entries = []
    providers = ((cfg.get("api") or {}).get("providers") or {})
    for pid in lp.PROVIDER_ORDER:
        if pid in providers:
            entries.append(pid)
    for pid in providers:
        if pid not in entries:
            entries.append(pid)
    if not entries:
        entries = ["hydra"]
    active = str((cfg.get("api") or {}).get("provider") or entries[0]).strip().lower()
    if active not in entries:
        active = entries[0]
    ordered = [active] + [pid for pid in entries if pid != active]

    slots: list[ModelTestSlot] = []
    for index in range(SLOT_COUNT):
        pid = ordered[index % len(ordered)]
        p_cfg = providers.get(pid) or {}
        models = [str(m) for m in (p_cfg.get("models") or []) if str(m).strip()]
        model = str(p_cfg.get("selected_model") or p_cfg.get("default_model") or "")
        if index == 0:
            model = str((cfg.get("api") or {}).get("model") or model)
        if not model:
            model = models[0] if models else "grok-4.7"
        slots.append(ModelTestSlot(provider=pid, model=model))
    return slots


def load_slots(cfg: dict[str, Any]) -> list[ModelTestSlot]:
    """Слоты из конфига (model_test.slots), иначе — стартовые."""
    raw = ((cfg.get("model_test") or {}).get("slots") or [])
    slots: list[ModelTestSlot] = []
    for item in raw:
        if isinstance(item, dict) and str(item.get("provider") or "").strip():
            slots.append(ModelTestSlot(
                provider=str(item["provider"]).strip().lower(),
                model=str(item.get("model") or "").strip(),
                reasoning=str(item.get("reasoning") or "").strip(),
            ))
    if not slots:
        return default_slots(cfg)
    while len(slots) < SLOT_COUNT:
        slots.append(default_slots(cfg)[len(slots)])
    return slots[:SLOT_COUNT]


def slots_as_dicts(slots: list[ModelTestSlot]) -> list[dict[str, str]]:
    return [
        {"provider": s.provider, "model": s.model, "reasoning": s.reasoning}
        for s in slots
    ]


def slot_config(cfg: dict[str, Any], slot: ModelTestSlot) -> dict[str, Any]:
    """Копия конфига под слот: провайдер/модель слота + его уровень рассуждений."""
    out = copy.deepcopy(cfg)
    api = out.setdefault("api", {})
    api["provider"] = slot.provider
    api["model"] = slot.model
    if slot.reasoning:
        providers = api.setdefault("providers", {})
        hydra_cfg = providers.setdefault("hydra", {})
        by_model = hydra_cfg.setdefault("reasoning_by_model", {})
        if isinstance(by_model, dict):
            by_model[slot.model] = slot.reasoning
    return out


def prepare_input(path: str | Path, cfg: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """Один подготовленный вход для всех слотов: страницы/фото картинками.

    Текст не подмешивается: каждая модель читает одни и те же изображения,
    локальный regex-разбор в тесте не участвует.
    """
    source = Path(path)
    if not source.exists():
        raise lp.PDFError(f"файл не найден: {source}")
    api = cfg.get("api") or {}
    max_pages = int(api.get("vision_max_pages") or 8)
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        urls = lp.render_pdf_pages_base64(source, max_pages=max_pages)
        return "", urls, [f"PDF: страниц в тесте — {len(urls)} из файла {source.name}"]
    if suffix in IMAGE_SUFFIXES:
        urls, notes = lp.render_image_files_base64([source])
        return "", urls, notes
    raise lp.PDFError(f"для теста нужен PDF или фото чертежа, получено: {source.name}")


def _run_one(
    prepared: tuple[str, list[str], list[str]],
    slot: ModelTestSlot,
    cfg: dict[str, Any],
    key_secret: str,
    scale: float,
) -> SlotResult:
    started = time.monotonic()
    try:
        outcome = lp.parse_legal_description_full(
            cfg=slot_config(cfg, slot),
            provider=slot.provider,
            model=slot.model,
            api_key=None,
            key_secret=key_secret,
            use_llm=True,
            ask_on_doubt=True,
            allow_regex_fallback=False,
            prepared_input=prepared,
        )
        plan = AcadBuilder(cfg).plan(outcome.data, scale=scale)
        return SlotResult(slot=slot, outcome=outcome, plan=plan,
                          seconds=time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001 — сбой слота не роняет остальные
        return SlotResult(slot=slot, error=str(exc),
                          seconds=time.monotonic() - started)


def run_all_slots(
    prepared: tuple[str, list[str], list[str]],
    slots: list[ModelTestSlot],
    cfg: dict[str, Any],
    *,
    key_secret: str = "",
    scale: float = 16.0,
    progress: Callable[[int, SlotResult], None] | None = None,
) -> list[SlotResult]:
    """Параллельный запуск всех слотов; результаты — в порядке слотов."""
    results: dict[int, SlotResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(slots)) as pool:
        future_map = {
            pool.submit(_run_one, prepared, slot, cfg, key_secret, scale): index
            for index, slot in enumerate(slots)
        }
        for future in concurrent.futures.as_completed(future_map):
            index = future_map[future]
            result = future.result()
            results[index] = result
            if progress is not None:
                progress(index, result)
    return [results[i] for i in range(len(slots))]


def plan_metrics(plan: Any) -> dict[str, float]:
    """Краткие цифры результата для таблицы сравнения."""
    segments = list(getattr(plan.description, "segments", []) or [])
    return {
        "closure_ft": float(plan.closure.distance),
        "area_sqft": float(plan.area_sqft),
        "perimeter_ft": float(plan.closure.perimeter),
        "sides": float(len(segments)),
    }


def metrics_text(result: SlotResult) -> str:
    """Однострочная сводка слота для окна."""
    head = f"{result.slot.provider} / {result.slot.model}"
    if result.slot.reasoning:
        head += f" · {result.slot.reasoning}"
    if result.error:
        return f"{head} — ошибка за {result.seconds:.0f} с: {result.error}"
    if result.outcome is None or result.plan is None:
        return f"{head} — нет результата"
    m = plan_metrics(result.plan)
    usage = getattr(result.outcome, "usage", None)
    tokens = ""
    if usage is not None and getattr(usage, "available", False):
        tokens = f" · токенов {usage.total_tokens}"
    return (
        f"{head} — замыкание {m['closure_ft']:.3f} ft · площадь {m['area_sqft']:.0f} sqft · "
        f"сторон {int(m['sides'])} · {result.seconds:.0f} с{tokens}"
    )
