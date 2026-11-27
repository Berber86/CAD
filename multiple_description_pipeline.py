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


def _candidate_windows(candidates: Sequence[int], limit: int, *, max_windows: int = 3) -> list[list[int]]:
    """Split >limit candidate pages into overlapping windows (stride limit-1, max 3 windows)."""
    items = list(candidates)
    if len(items) <= limit:
        return [items]
    stride = max(1, limit - 1)
    windows: list[list[int]] = []
    start = 0
    while start < len(items) and len(windows) < max_windows:
        win = items[start:start + limit]
        windows.append(win)
        if start + limit >= len(items):
            break
        start += stride
    return windows


def _item_dedup_key(item: DescriptionItem) -> tuple[Any, ...]:
    desc = item.data
    lot = lot_identity.canonical(getattr(desc, 'lot_number', '') or '')
    segs = tuple(
        (round(float(s.vector[0]), 2), round(float(s.vector[1]), 2), round(float(s.distance_ft), 2))
        for s in (getattr(desc, 'segments', None) or ())
    )
    dist = round(float(getattr(desc, 'distance_to_corner_ft', 0.0) or 0.0), 2)
    street = ' '.join(str(getattr(desc, 'main_street', '') or '').upper().split())
    return (lot, street, dist, segs)


def _merge_window_results(
    candidates: Sequence[int],
    window_results: Sequence[ParseResult],
    coverage_complete: bool,
    render_errors: Sequence[int],
) -> ParseResult:
    deduped: list[DescriptionItem] = []
    seen_pos: dict[tuple[Any, ...], int] = {}
    for w_res in window_results:
        for item in w_res.items:
            key = _item_dedup_key(item)
            if key in seen_pos:
                idx = seen_pos[key]
                if item.complete and not deduped[idx].complete:
                    deduped[idx] = item
            else:
                seen_pos[key] = len(deduped)
                deduped.append(item)
    parsed_pages = tuple(sorted({p for r in window_results for p in r.pages}))
    unparsed_pages = tuple(n for n in candidates if n not in parsed_pages)
    attempts = tuple(a for r in window_results for a in r.attempts)
    any_selected = any(r.selected_attempt is not None for r in window_results)
    selected_attempt = 0 if any_selected else None
    all_complete = (
        coverage_complete
        and not render_errors
        and not unparsed_pages
        and bool(window_results)
        and all(r.complete for r in window_results)
    )
    errors = [r.error for r in window_results if r.error]
    return ParseResult(
        items=tuple(deduped),
        pages=parsed_pages,
        unparsed_pages=unparsed_pages,
        complete=all_complete,
        attempts=attempts,
        selected_attempt=selected_attempt,
        selection='windowed_merge' if any_selected else None,
        error=None if any_selected else (errors[0] if errors else 'parse_failed'),
        comparison=None,
    )


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
    hydra_api_key: str | None = None,
    hydra_base_url: str = legal_page_search.HYDRA_BASE_URL,
    hydra_model: str = legal_page_search.HYDRA_FALLBACK_MODEL,
    hydra_send: Callable[..., Any] | None = None,
    ocr_fallback: bool = False,
    ocr_dpi: int = 200,
    render_dpi: int = 220,
    render_max_side: int = 3200,
    output_format: str = 'png',
    enhance_scan: bool = False,
    window_excess: bool = False,
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
    fmt = str(output_format or 'png').strip().lower()
    if fmt == 'jpg':
        fmt = 'jpeg'
    if fmt not in ('png', 'auto', 'jpeg'):
        raise ValueError('Invalid output_format')
    if portal_lot is not None and not isinstance(portal_lot, str):
        raise ValueError('Invalid portal lot')
    if not isinstance(force_parse, bool):
        raise ValueError('force_parse must be a boolean')
    if not callable(send) or (choose is not None and not callable(choose)):
        raise ValueError('Transport callbacks must be callable')
    path = Path(path)
    search_kwargs: dict[str, Any] = dict(
        api_key=search_api_key,
        base_url=search_base_url,
        image_side=search_image_side,
        send=search_send,
        current=current,
        progress=progress,
    )
    if hydra_api_key or hydra_send is not None:
        search_kwargs.update(
            hydra_api_key=hydra_api_key,
            hydra_base_url=hydra_base_url,
            hydra_model=hydra_model,
            hydra_send=hydra_send,
        )
    if ocr_fallback:
        search_kwargs.update(ocr_fallback=True, ocr_dpi=ocr_dpi)
    search = legal_page_search.search_pdf(path, config, **search_kwargs)
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

    limit = min(MAX_IMAGES, image_limit)
    use_windows = bool(window_excess and limit >= 4 and len(candidates) > limit)
    windows = _candidate_windows(candidates, limit) if use_windows else [list(candidates[:limit])]
    target_numbers = sorted({n for w in windows for n in w})
    if use_windows:
        emit(progress, f"Кандидатов больше {limit} ({len(candidates)} стр.) — "
                       f"обрабатываю в {len(windows)} перекрывающихся окнах.")

    rendered_by_num: dict[int, PageImage] = {}
    errors: list[int] = []
    for n in target_numbers:
        check(current)
        emit(progress, f"Разбор: подготовка изображения страницы {n} ({fmt.upper()}).")
        try:
            data = render(path, dpi=render_dpi, max_pages=1, max_side_px=render_max_side,
                          pages=[n], rotation_deg=0, enhance_scan=bool(enhance_scan), output_format=fmt)
            if (len(data) != 1 or not isinstance(data[0], str)
                    or not (data[0].startswith('data:image/png;base64,')
                            or data[0].startswith('data:image/jpeg;base64,'))):
                raise ValueError('invalid page image')
            rendered_by_num[n] = PageImage(n, data[0])
        except Exception:
            errors.append(n)
    check(current)
    if file_metadata._digest(path) != search['pdf_sha256']:
        raise cache.CacheError('PDF changed while rendering; parse not started.')
    if not rendered_by_num:
        cache.record_failure(path, search['pdf_sha256'], reason='images_unavailable', pages=candidates)
        return PipelineResult(search, None, 'images_unavailable', False, None, portal_lot, tuple(errors))

    settings = {'format': fmt, 'dpi': render_dpi, 'max_side_px': render_max_side,
                'rotation_deg': 0, 'enhance_scan': bool(enhance_scan), 'render_strategy': 'one-page-v1'}

    if len(windows) == 1:
        pages = [rendered_by_num[n] for n in windows[0] if n in rendered_by_num]
        # Search and render coverage are separate from the parser's reported
        # completeness. Missing images must never become a complete document.
        complete = bool(search['coverage_complete']) and not errors
        context = cache.cache_context(pdf_sha256=search['pdf_sha256'], pages=pages,
                                      candidate_pages=candidates, image_settings=settings,
                                      provider=provider, model=model,
                                      filename_lot=lot_identity.lot_from_filename(path),
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
        if not result.items and not force_parse:
            fallback = cache.load_latest_complete(path)
            if fallback is not None:
                restored, original_portal_lot = fallback
                emit(progress, "Разбор: свежий запрос не удался — использовано ранее сохранённое "
                               "полное описание этого PDF; сверьте его перед использованием.")
                return PipelineResult(search, restored, 'parsed', True, original_portal_lot,
                                      portal_lot, tuple(errors))
        return PipelineResult(search, result, 'parsed' if result.selected_attempt is not None else 'parse_failed',
                              False, portal_lot, portal_lot, tuple(errors))

    # Windowed multi-pass for >limit candidate pages (all windows stay independently cacheable).
    window_results: list[ParseResult] = []
    all_cached = True
    first_original_portal: str | None = None
    for idx, win in enumerate(windows, 1):
        check(current)
        win_pages = [rendered_by_num[n] for n in win if n in rendered_by_num]
        if not win_pages:
            continue
        win_errors = [n for n in win if n not in rendered_by_num]
        win_complete = bool(search['coverage_complete']) and not win_errors and len(win) == len(candidates)
        win_context = cache.cache_context(pdf_sha256=search['pdf_sha256'], pages=win_pages,
                                          candidate_pages=win, image_settings=settings,
                                          provider=provider, model=model,
                                          filename_lot=lot_identity.lot_from_filename(path),
                                          search_complete=win_complete, image_limit=image_limit)
        saved_win = None if force_parse else cache.load_result(path, win_context)
        if saved_win is not None:
            w_res, w_orig = saved_win
            if first_original_portal is None:
                first_original_portal = w_orig
            emit(progress, f"Разбор (окно {idx}/{len(windows)}): использован кэш.")
        else:
            all_cached = False
            emit(progress, f"Разбор (окно {idx}/{len(windows)}): страницы {page_list([p.number for p in win_pages])}.")
            w_res = parse_pages(config, win_pages, candidate_pages=win, image_limit=image_limit,
                                filename_lot=lot_identity.lot_from_filename(path), portal_lot=portal_lot,
                                search_complete=win_complete, send=send, choose=choose,
                                current_job=current, progress=progress)
            check(current)
            cache.save_result(path, win_context, w_res, portal_lot=portal_lot)
        window_results.append(w_res)
    merged = _merge_window_results(candidates, window_results, bool(search['coverage_complete']), errors)
    if not merged.items and not force_parse:
        fallback = cache.load_latest_complete(path)
        if fallback is not None:
            restored, original_portal_lot = fallback
            emit(progress, "Разбор: свежий запрос не удался — использовано ранее сохранённое "
                           "полное описание этого PDF; сверьте его перед использованием.")
            return PipelineResult(search, restored, 'parsed', True, original_portal_lot,
                                  portal_lot, tuple(errors))
    orig_portal = first_original_portal if (all_cached and window_results) else portal_lot
    return PipelineResult(search, merged, 'parsed' if merged.selected_attempt is not None else 'parse_failed',
                          all_cached and bool(window_results), orig_portal, portal_lot, tuple(errors))
