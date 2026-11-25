"""Experimental per-PDF search + parse coordinator.

All provider calls are explicitly injected; the disabled-by-default runtime
resolves the provider. This module does not run requests on import.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import file_metadata
import legal_page_search
import lot_identity
from multiple_description_cancel import check
from multiple_description_progress import emit, page_list
import multiple_description_cache as cache
from llm_parser import render_pdf_pages_base64
from multiple_description_parse import MAX_IMAGES, Attempt, PageImage, ParseDisabled, ParseResult, parse_pages


class VisionUnavailable(RuntimeError):
    """The selected model cannot accept images and no reserve is configured."""


@dataclass(frozen=True)
class VisionTarget:
    provider: str
    model: str
    image_limit: int


def configured_vision_fallback(config: Mapping[str, Any] | None) -> VisionTarget | None:
    """Read the explicit reserve without accessing credentials or the network."""
    section = config.get('multiple_description_search') if isinstance(config, Mapping) else None
    data = section.get('vision_fallback') if isinstance(section, Mapping) else None
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise ValueError('Invalid vision reserve configuration')
    provider, model, limit = data.get('provider'), data.get('model'), data.get('image_limit')
    if (not isinstance(provider, str) or not provider.strip()
            or not isinstance(model, str) or not model.strip()
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_IMAGES):
        raise ValueError('Invalid vision reserve configuration')
    return VisionTarget(provider.strip(), model.strip(), limit)


def choose_vision_target(selected: VisionTarget, support: str, fallback: VisionTarget | None) -> VisionTarget:
    """Unknown capability is tried; only confirmed absence uses the reserve.

    This is policy only: resolving keys, configuring the Hydra reserve and
    determining trustworthy provider capability remain the caller's work.
    """
    if support not in ("yes", "no", "unknown"):
        raise ValueError('Unknown vision support state')
    if support != 'no':
        return selected
    if fallback is None:
        raise VisionUnavailable('The selected model has no image support and no vision reserve is configured.')
    return fallback


@dataclass(frozen=True)
class PipelineResult:
    search: dict[str, Any]
    parse: ParseResult | None
    status: str
    cached: bool
    original_portal_lot: str | None
    current_portal_lot: str | None
    render_errors: tuple[int, ...]


def candidate_pages_pdf_name(source: Path, pages: Sequence[int]) -> str:
    """Имя копии только релевантных страниц: ``LOT 30 NEW_p2,4.pdf``."""
    return f"{source.stem}_p{','.join(str(int(n)) for n in pages)}.pdf"


def write_candidate_pages_pdf(source: Path, pages: Sequence[int]) -> Path | None:
    """Копия исходника только из релевантных страниц рядом с ним.

    Чтобы пользователю не листать весь документ в поисках описания при
    проверке. Вызывать только с финальным списком кандидатов: страницы
    оцениваются партиями, и вердикт полон лишь после проверки всех партий.
    Любой сбой возвращает None — отсутствие копии не должно губить разбор.
    """
    numbers = sorted({int(n) for n in pages if int(n) > 0})
    if not numbers:
        return None
    try:
        import pymupdf

        source = Path(source)
        target = source.with_name(candidate_pages_pdf_name(source, numbers))
        if target.resolve() == source.resolve():
            return None
        with pymupdf.open(source) as doc:
            if doc.needs_pass or any(not 1 <= n <= len(doc) for n in numbers):
                return None
            with pymupdf.open() as trimmed:
                for n in numbers:
                    trimmed.insert_pdf(doc, from_page=n - 1, to_page=n - 1)
                trimmed.save(target)
        return target
    except Exception:  # noqa: BLE001 - копия для проверки необязательна
        return None


def parse_pdf(
    path: str | Path,
    config: Mapping[str, Any] | None,
    *,
    search_api_key: str,
    provider: str,
    model: str,
    image_limit: int,
    portal_lot: str | None,
    send: Callable[[Sequence[PageImage], Mapping[str, str | None]], str],
    choose: Callable[[Sequence[PageImage], Sequence[Attempt]], int] | None = None,
    search_send: Callable[..., Any] | None = None,
    search_base_url: str = legal_page_search.BASE_URL,
    search_image_side: int = 1568,
    render_dpi: int = 220,
    render_max_side: int = 3200,
    render: Callable[..., list[str]] = render_pdf_pages_base64,
    force_parse: bool = False,
    current: Callable[[], bool] = lambda: True,
    progress: Callable[[str], None] | None = None,
) -> PipelineResult:
    """Search a PDF and parse its candidates using the injected callbacks.

    Returns a diagnostic-only result; the caller must not infer a complete
    document when no images can be rendered, nor use it as a drawing plan.
    """
    if not legal_page_search.enabled(config):
        raise ParseDisabled('Developer multiple-description switch is disabled.')
    check(current)
    if (not isinstance(provider, str) or not provider or not isinstance(model, str) or not model
            or isinstance(image_limit, bool) or not isinstance(image_limit, int) or image_limit < 1):
        raise ValueError('A provider, model and positive image limit are required')
    if (isinstance(render_dpi, bool) or not isinstance(render_dpi, int) or not 96 <= render_dpi <= 350
            or isinstance(render_max_side, bool) or not isinstance(render_max_side, int) or render_max_side < 1200):
        raise ValueError('Invalid rendering settings')
    if portal_lot is not None and not isinstance(portal_lot, str):
        raise ValueError('Invalid portal lot')
    if not isinstance(force_parse, bool):
        raise ValueError('force_parse must be a boolean')
    if not callable(send) or (choose is not None and not callable(choose)):
        raise ValueError('Transport callbacks must be callable')
    path = Path(path)
    search = legal_page_search.search_pdf(path, config, api_key=search_api_key,
                                          base_url=search_base_url, image_side=search_image_side,
                                          send=search_send, current=current, progress=progress)
    check(current)
    candidates = search['candidate_pages']
    emit(progress, f"Страницы для разбора: {page_list(candidates) if candidates else 'нет'}.")
    cache.record_portal_observation(path, search['pdf_sha256'], portal_lot)
    if not candidates:
        status = 'no_candidates' if search['coverage_complete'] else 'search_incomplete'
        return PipelineResult(search, None, status, False, None, portal_lot, ())
    if file_metadata._digest(path) != search['pdf_sha256']:
        raise cache.CacheError('PDF changed after page search; parse not started.')
    # Финальный вердикт о релевантных страницах вынесен (все партии проверены
    # внутри search_pdf): рядом с исходником — копия только этих страниц,
    # чтобы пользователь проверял описание, не листая весь документ.
    extracted = write_candidate_pages_pdf(path, candidates)
    if extracted is not None:
        emit(progress, f"Для проверки: копия только релевантных страниц — {extracted.name}.")
    else:
        emit(progress, "Для проверки: копию релевантных страниц сделать не удалось.")
    pages: list[PageImage] = []
    errors: list[int] = []
    limit = min(MAX_IMAGES, image_limit)
    for n in candidates:
        check(current)
        if len(pages) >= limit:
            break
        emit(progress, f"Разбор: подготовка изображения страницы {n}.")
        try:
            data = render(path, dpi=render_dpi, max_pages=1, max_side_px=render_max_side,
                          pages=[n], rotation_deg=0, enhance_scan=False, output_format='png')
            if len(data) != 1 or not isinstance(data[0], str) or not data[0].startswith('data:image/png;base64,'):
                raise ValueError('invalid page image')
            pages.append(PageImage(n, data[0]))
        except Exception:
            errors.append(n)
    check(current)
    if file_metadata._digest(path) != search['pdf_sha256']:
        raise cache.CacheError('PDF changed while rendering; parse not started.')
    if not pages:
        cache.record_failure(path, search['pdf_sha256'], reason='images_unavailable', pages=candidates)
        return PipelineResult(search, None, 'images_unavailable', False, None, portal_lot, tuple(errors))
    # Search and render coverage are separate from the parser's reported
    # completeness. Missing images must never become a complete document.
    complete = bool(search['coverage_complete']) and not errors
    settings = {'format': 'png', 'dpi': render_dpi, 'max_side_px': render_max_side,
                'rotation_deg': 0, 'enhance_scan': False, 'render_strategy': 'one-page-v1'}
    context = cache.cache_context(pdf_sha256=search['pdf_sha256'], pages=pages,
                                  candidate_pages=candidates, image_settings=settings,
                                  provider=provider, model=model, filename_lot=lot_identity.lot_from_filename(path),
                                  search_complete=complete, image_limit=image_limit)
    saved = None if force_parse else cache.load_result(path, context)
    if saved is not None:
        emit(progress, "Разбор: использован сохранённый результат, новый запрос не отправлялся.")
        result, original_portal_lot = saved
        return PipelineResult(search, result, 'parsed', True, original_portal_lot, portal_lot, tuple(errors))
    result = parse_pages(config, pages, candidate_pages=candidates, image_limit=image_limit,
                         filename_lot=lot_identity.lot_from_filename(path), portal_lot=portal_lot,
                         search_complete=complete, send=send, choose=choose, current_job=current, progress=progress)
    check(current)
    cache.save_result(path, context, result, portal_lot=portal_lot)
    return PipelineResult(search, result, 'parsed' if result.selected_attempt is not None else 'parse_failed',
                          False, portal_lot, portal_lot, tuple(errors))
