"""Runtime bridge for the disabled-by-default multi-description PDF workflow.

No network access or key lookup occurs on import. Capability lookup is public,
provider-specific, and failure means unknown, never automatic fallback. Paid
requests occur only through the injected/configured transports after an enabled
workflow has been explicitly started by the application.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
import llm_parser as lp
import legal_page_search
import multiple_description_pipeline as pipeline
import multiple_description_transport as transport
import multiple_description_drawing as drawing
import multiple_description_cache as cache
import file_metadata
from multiple_description_cancel import Cancelled, check
from multiple_description_progress import emit
from acad_builder import AcadBuilder, compute_pob_point
from multiple_description_parse import MAX_IMAGES, DescriptionItem


class RuntimeUnavailable(RuntimeError):
    """Safe operational failure, never include provider bodies or credentials."""


#: Отложенная страховка для медленной Hydra: та же grok-4.7 у RouterAI.
#: Hydra при этом не отменяется — используется первый успешный ответ.
HYDRA_GROK_MODEL = "grok-4.7"
ROUTERAI_GROK_MODEL = "x-ai/grok-4.7"
HEDGE_DELAY_SEC = 300.0
#: ВРЕМЕННО ВЫКЛЮЧЕНО указанием пользователя (5.33): параллельный
#: страховочный вызов RouterAI Grok после HEDGE_DELAY_SEC ожидания
#: основной модели не делается. Механизм, вызывающие места и тесты
#: сохранены — чтобы вернуть страховку, поставить True.
HEDGE_ENABLED = False


@dataclass(frozen=True)
class ResolvedTarget:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    image_limit: int = MAX_IMAGES
    capability: str = 'unknown'


@dataclass(frozen=True)
class FileJob:
    path: Path
    is_main: bool
    portal_lot: str | None = None


@dataclass(frozen=True)
class FileResult:
    job: FileJob
    result: pipeline.PipelineResult | None
    status: str


@dataclass(frozen=True)
class BatchResult:
    files: tuple[FileResult, ...]
    scene: drawing.SceneResult
    provider: str
    model: str
    cancelled: bool = False


def provider_target(cfg: Mapping[str, Any], provider: str, model: str, *,
                    key_secret: str = '', explicit_key: str | None = None,
                    image_limit: int = MAX_IMAGES, capability: str = 'unknown') -> ResolvedTarget:
    """Resolve only this provider's key; never use another provider's generic key."""
    if provider not in lp.PROVIDERS_CATALOG or not isinstance(model, str) or not model.strip():
        raise RuntimeUnavailable('provider_or_model_unavailable')
    api = cfg.get('api') if isinstance(cfg, Mapping) else None
    api = api if isinstance(api, Mapping) else {}
    providers = api.get('providers') if isinstance(api.get('providers'), Mapping) else {}
    p = providers.get(provider) if isinstance(providers.get(provider), Mapping) else {}
    catalog = lp.PROVIDERS_CATALOG[provider]
    base_url = str(p.get('base_url') or catalog.get('base_url') or '').strip()
    parsed = urlsplit(base_url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeUnavailable('invalid_provider_endpoint')
    env_name = str(p.get('env_key') or catalog.get('env_key') or '').strip()
    env = os.environ.get(env_name, '') if env_name else ''
    dotenv = lp.load_dotenv_dict()
    dotenv_key = dotenv.get(env_name, '') if env_name else ''
    # Compatibility with the old single-provider field is safe only for the
    # provider that field belongs to; never send a Hydra key to RouterAI.
    legacy = api.get('api_key') if api.get('provider') == provider else ''
    raw_key = explicit_key or p.get('api_key') or env or dotenv_key or legacy or ''
    try:
        raw_key = lp.clean_provider_key(provider, raw_key)
        key = lp.unprotect_api_key(provider, raw_key, secret=key_secret)
    except Exception as exc:
        raise RuntimeUnavailable('provider_key_unavailable') from exc
    if not key:
        raise RuntimeUnavailable('provider_key_unavailable')
    if isinstance(image_limit, bool) or not isinstance(image_limit, int) or not 1 <= image_limit <= MAX_IMAGES:
        raise RuntimeUnavailable('invalid_image_limit')
    return ResolvedTarget(provider, model.strip(), base_url, key, image_limit, capability)


_HYDRA_CATALOG_LOCK = threading.Lock()
_HYDRA_CATALOG_CACHE: tuple[float, Any] | None = None
_HYDRA_CATALOG_TTL_SEC = 7 * 24 * 3600.0


def _hydra_catalog_cache_path() -> Path:
    base = os.environ.get('LOCALAPPDATA', '').strip()
    root = Path(base) / 'TitleSurveyAI' if base else Path.home() / '.cache' / 'titlesurveyai'
    return root / 'hydra_models_cache.json'


def _compact_catalog(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping) or not isinstance(payload.get('data'), list):
        return None
    entries = []
    for entry in payload['data']:
        if not isinstance(entry, Mapping) or not isinstance(entry.get('id'), str):
            continue
        item: dict[str, Any] = {'id': entry['id']}
        if isinstance(entry.get('input_modalities'), list):
            item['input_modalities'] = list(entry['input_modalities'])
        if 'max_file_count' in entry:
            item['max_file_count'] = entry['max_file_count']
        entries.append(item)
    return {'data': entries}


def _read_hydra_catalog_fallback(cache_path: Path | None = None) -> Any | None:
    now = time.time()
    with _HYDRA_CATALOG_LOCK:
        if _HYDRA_CATALOG_CACHE is not None and 0 <= now - _HYDRA_CATALOG_CACHE[0] <= _HYDRA_CATALOG_TTL_SEC:
            return _HYDRA_CATALOG_CACHE[1]
    path = cache_path or _hydra_catalog_cache_path()
    try:
        stored = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(stored, Mapping):
        return None
    cached_at = stored.get('cached_at')
    catalog = stored.get('catalog')
    if (isinstance(cached_at, (int, float)) and not isinstance(cached_at, bool)
            and 0 <= now - float(cached_at) <= _HYDRA_CATALOG_TTL_SEC
            and isinstance(catalog, Mapping) and isinstance(catalog.get('data'), list)):
        return catalog
    return None


def fetch_hydra_catalog(*, attempts: int = 3, timeout: float = 15.0,
                        cache_path: Path | None = None,
                        sleep: Callable[[float], None] = time.sleep) -> Any:
    """Public metadata only; no key or document is sent."""
    global _HYDRA_CATALOG_CACHE
    last_exc: Exception | None = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            response = httpx.get('https://api.hydraai.ru/v1/models', timeout=timeout, follow_redirects=False)
            response.raise_for_status()
            data = response.json()
            compact = _compact_catalog(data)
            if compact is not None:
                now = time.time()
                with _HYDRA_CATALOG_LOCK:
                    _HYDRA_CATALOG_CACHE = (now, data)
                target = cache_path or _hydra_catalog_cache_path()
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(
                        json.dumps({'cached_at': now, 'catalog': compact}, ensure_ascii=False),
                        encoding='utf-8',
                    )
                except OSError:
                    pass
            return data
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            if attempt < attempts:
                sleep(0.5 * attempt)
    cached = _read_hydra_catalog_fallback(cache_path)
    if cached is not None:
        return cached
    assert last_exc is not None
    raise last_exc


def model_capability(catalog: Any, model: str) -> tuple[str, int | None]:
    """Interpret explicit modalities only; transport failures are unknown."""
    if not isinstance(catalog, Mapping) or not isinstance(catalog.get('data'), list):
        return 'unknown', None
    matches = [entry for entry in catalog['data'] if isinstance(entry, Mapping) and entry.get('id') == model]
    if len(matches) != 1:
        return 'unknown', None
    entry = matches[0]
    modalities = entry.get('input_modalities')
    if not isinstance(modalities, list) or not modalities or any(not isinstance(x, str) for x in modalities):
        capability = 'unknown'
    elif 'image' in modalities:
        capability = 'yes'
    elif 'text' in modalities:
        capability = 'no'
    else:
        capability = 'unknown'
    raw = entry.get('max_file_count')
    limit = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else None
    return capability, limit


def prepare_runtime(cfg: Mapping[str, Any], *, selected_provider: str, selected_model: str,
                    selected_api_key: str | None = None, key_secret: str = '',
                    catalog_fetch: Callable[[], Any] = fetch_hydra_catalog,
                    current: Callable[[], bool] = lambda: True) -> tuple[ResolvedTarget, str]:
    """Resolve selected/vision-reserve target and the separate RouterAI key.

    A Hydra catalog failure or missing modalities means unknown and causes the
    selected model to be tried. Ordinary model errors never switch providers.
    """
    if not legal_page_search.enabled(cfg):
        raise pipeline.ParseDisabled('Developer multiple-description switch is disabled.')
    check(current)
    router = provider_target(cfg, 'routerai', legal_page_search.PRIMARY, key_secret=key_secret,
                             explicit_key=selected_api_key if selected_provider == 'routerai' else None)
    try:
        fallback = pipeline.configured_vision_fallback(cfg)
    except ValueError as exc:
        raise RuntimeUnavailable('invalid_vision_reserve') from exc
    metadata: Any = None
    # The metadata endpoint is public. Failure is deliberately not treated as
    # evidence that the selected model lacks vision.
    if selected_provider == 'hydra':
        try:
            metadata = catalog_fetch()
        except Exception:
            metadata = None
    check(current)
    capability, limit = model_capability(metadata, selected_model) if selected_provider == 'hydra' else ('unknown', None)
    selected = pipeline.VisionTarget(selected_provider, selected_model, min(MAX_IMAGES, limit or MAX_IMAGES))
    try:
        choice = pipeline.choose_vision_target(selected, capability, fallback)
    except pipeline.VisionUnavailable as exc:
        raise RuntimeUnavailable('vision_reserve_unavailable') from exc
    if choice != selected:
        fallback_capability, fallback_limit = model_capability(metadata, choice.model) if choice.provider == 'hydra' else ('unknown', None)
        if fallback_capability == 'no':
            raise RuntimeUnavailable('vision_reserve_unavailable')
        limit = min(MAX_IMAGES, choice.image_limit, fallback_limit or MAX_IMAGES)
        provider, model = choice.provider, choice.model
        actual_capability = fallback_capability
    else:
        limit, provider, model, actual_capability = selected.image_limit, selected.provider, selected.model, capability
    target = provider_target(cfg, provider, model, key_secret=key_secret,
                             explicit_key=selected_api_key if provider == selected_provider else None,
                             image_limit=limit, capability=actual_capability)
    return target, router.api_key


def portal_lot_from_passport(path: Path) -> str | None:
    """Use an unambiguous portal observation only, never a filename as portal."""
    try:
        _, passport = file_metadata._passport(path)
        identity = passport.get('identity') if isinstance(passport, dict) else None
        observations = identity.get('lot') if isinstance(identity, dict) else None
        if not isinstance(observations, list):
            return None
        values = {str(x.get('value')).strip() for x in observations
                  if isinstance(x, dict) and x.get('source') == 'portal' and x.get('value')}
        return next(iter(values)) if len(values) == 1 else None
    except (OSError, ValueError):
        return None


def anchor_for(path: Path, item: DescriptionItem) -> drawing.Anchor | None:
    """Use an explicit shared intersection/tie; never infer it from (0, 0)."""
    desc = item.data
    main = ' '.join(str(desc.main_street or '').upper().split())
    cross = ' '.join(str(desc.cross_street or '').upper().split())
    if not main or not cross:
        return None
    tie = desc.corner_to_pob_bearing
    if (desc.distance_to_corner_ft <= 1e-6 or tie is None or not tie.has_bearing
            or tie.bearing_solved):
        # A zero may also mean an absent tie, and a cross-street boundary does
        # not prove that POB itself is at the intersection. Nominal or solved
        # directions likewise cannot establish a shared coordinate frame.
        return None
    position = compute_pob_point(desc)
    # Same-named streets, even in the same borough, do not establish that
    # separate PDFs refer to one physical intersection. Keep different source
    # documents apart until a stronger cross-document tie is available.
    scope = f'file:{str(path.resolve())}'
    return drawing.Anchor(f'{scope}|{main}|{cross}', position)


def _plan_with_auto_rows(builder: AcadBuilder, sources: Sequence[drawing.DescriptionSource], *,
                         eligible: Sequence[bool] | None = None, scale: float = 16.0
                         ) -> tuple[drawing.SceneResult, int]:
    """Only publish auto groups when their plans preserve a usable real tie."""
    import math
    from multiple_description_candidate import auto_place_rows
    original = tuple(sources)
    proposed, _ = auto_place_rows(original, eligible=eligible)
    scene = drawing.plan_scene(builder, proposed, scale=scale)
    groups: dict[str, list[int]] = {}
    for i, source in enumerate(proposed):
        if source.anchor and source.anchor.group.startswith('auto-row:'):
            groups.setdefault(source.anchor.group, []).append(i)
    by_index = {entry.description_index: entry for entry in scene.plans}
    failed: set[str] = set()
    for group, indices in groups.items():
        if len(indices) != 2 or any(i not in by_index for i in indices):
            failed.add(group)
            continue
        for i in indices:
            if not proposed[i].is_main:
                continue
            plan = by_index[i].plan
            ties = [line for line in plan.lines if line.kind == 'main-street-row-tie']
            distance = plan.description.distance_to_corner_ft
            bearing = plan.description.corner_to_pob_bearing
            if bearing is None:
                failed.add(group)
                break
            vx, vy = bearing.vector
            expected_corner = (plan.pob[0] - vx, plan.pob[1] - vy)
            if (len(ties) != 1 or math.dist(ties[0].end, plan.pob) > drawing.ROUNDING_GAP_LIMIT_FT
                    or math.dist(ties[0].start, expected_corner) > drawing.ROUNDING_GAP_LIMIT_FT
                    or abs(math.dist(ties[0].start, ties[0].end) - distance) > drawing.ROUNDING_GAP_LIMIT_FT):
                failed.add(group)
                break
    if failed:
        safe = tuple(original[i] if source.anchor and source.anchor.group in failed else source
                     for i, source in enumerate(proposed))
        scene = drawing.plan_scene(builder, safe, scale=scale)
    return scene, len(groups) - len(failed)


class _BatchCancelled(Exception):
    """Internal: stop streaming/batch without publishing a partial scene."""


def _process_batch_file(job: FileJob, index: int, total: int | None, cfg: Mapping[str, Any], *,
                        target: ResolvedTarget, search_api_key: str,
                        send: Callable[..., Any], choose: Callable[..., Any] | None,
                        search_send: Callable[..., Any] | None,
                        search_base_url: str, force_parse: bool,
                        current: Callable[[], bool],
                        detail: Callable[[str], None] | None) -> tuple[FileResult, list[drawing.DescriptionSource], list[bool]]:
    if not current():
        raise _BatchCancelled
    emit(detail, f"Файл {index}" + (f" из {total}" if total else "") + f": {job.path.name}.")
    api_cfg = cfg.get('api') if isinstance(cfg.get('api'), Mapping) else {}
    output_format = str(api_cfg.get('vision_output_format') or 'auto').strip().lower()
    if output_format == 'jpg':
        output_format = 'jpeg'
    if output_format not in ('auto', 'png', 'jpeg'):
        output_format = 'auto'
    enhance_scan = bool(api_cfg.get('vision_enhance_scans', True))
    try:
        render_dpi = max(96, min(350, int(api_cfg.get('vision_dpi') or 220)))
    except (TypeError, ValueError):
        render_dpi = 220
    hydra_key: str | None = None
    hydra_url = legal_page_search.HYDRA_BASE_URL
    if target.provider == 'hydra' and target.api_key:
        hydra_key = target.api_key
        hydra_url = target.base_url or hydra_url
    else:
        try:
            h_target = provider_target(cfg, 'hydra', legal_page_search.HYDRA_FALLBACK_MODEL)
            hydra_key = h_target.api_key
            hydra_url = h_target.base_url or hydra_url
        except RuntimeUnavailable:
            hydra_key = None
    try:
        portal_lot = job.portal_lot if job.portal_lot is not None else portal_lot_from_passport(job.path)
        result = pipeline.parse_pdf(job.path, cfg, search_api_key=search_api_key,
                                    provider=target.provider, model=target.model,
                                    image_limit=target.image_limit, portal_lot=portal_lot,
                                    send=send, choose=choose, search_send=search_send,
                                    search_base_url=search_base_url,
                                    hydra_api_key=hydra_key,
                                    hydra_base_url=hydra_url,
                                    ocr_fallback=True,
                                    render_dpi=render_dpi,
                                    output_format=output_format,
                                    enhance_scan=enhance_scan,
                                    window_excess=True,
                                    force_parse=force_parse, current=current, progress=detail)
    except Cancelled as exc:
        raise _BatchCancelled from exc
    except Exception:
        emit(detail, "Файл: обработка не завершена; подробности ответа не записываются.")
        # Never persist raw provider errors or sensitive text in diagnostics.
        try:
            cache.record_failure(job.path, file_metadata._digest(job.path), reason='processing_failed', pages=[])
        except (OSError, ValueError, cache.CacheError):
            pass
        return FileResult(job, None, 'processing_failed'), [], []
    if not current():
        raise _BatchCancelled
    status_text = {'parsed': 'разбор выполнен', 'no_candidates': 'описания поиском не найдены',
                   'search_incomplete': 'поиск неполный', 'images_unavailable': 'страницы недоступны',
                   'parse_failed': 'разбор не выполнен'}
    emit(detail, f"Файл: {status_text.get(result.status, 'неизвестное состояние')}, "
                 f"описаний {len(result.parse.items) if result.parse else 0}.")
    file_result = FileResult(job, result, result.status)
    if result.parse is None:
        return file_result, [], []
    sources: list[drawing.DescriptionSource] = []
    complete: list[bool] = []
    for item in result.parse.items:
        if not current():
            raise _BatchCancelled
        try:
            outcome = lp.ParseOutcome(data=item.data, source='llm:multi', provider=target.provider,
                                      model=target.model)
            item.data._source_identity = file_metadata.record_outcome(
                job.path, outcome, expected_digest=result.search['pdf_sha256'])
        except (OSError, ValueError):
            try:
                cache.record_failure(job.path, result.search['pdf_sha256'], reason='metadata_unavailable', pages=[])
            except (OSError, ValueError, cache.CacheError):
                pass
        sources.append(drawing.DescriptionSource(job.path, job.is_main, item, anchor_for(job.path, item)))
        complete.append(result.parse.complete)
    return file_result, sources, complete


def _finish_batch(files: list[FileResult], sources: list[drawing.DescriptionSource],
                  source_complete: list[bool], cfg: Mapping[str, Any], *, target: ResolvedTarget,
                  scale: float, current: Callable[[], bool],
                  detail: Callable[[str], None] | None) -> BatchResult:
    if not current():
        raise _BatchCancelled
    emit(detail, f"Подготовка схемы: описаний {len(sources)}.")
    scene, paired = _plan_with_auto_rows(AcadBuilder(dict(cfg)), sources, eligible=source_complete, scale=scale)
    emit(detail, f"Схема подготовлена: контуров {len(scene.plans)}, ошибок построения {len(scene.failures)}.")
    if paired:
        emit(detail, f"Геометрически сопоставлено пар участков: {paired}.")
    unlocated = sum(not entry.placement_known for entry in scene.plans)
    if unlocated:
        emit(detail, f"{unlocated} контуров без явной привязки к перекрёстку в описании; "
                     "построены как в обычном режиме — проверьте их положение на схеме.")
    partial = sum(entry.diagnostic == 'partial_description' for entry in scene.plans)
    if partial:
        emit(detail, f"{partial} описаний неполные: построены по известным сторонам, "
                     "как в обычном режиме.")
    by_index = {entry.description_index: entry for entry in scene.plans}
    failures = {entry.description_index: entry.reason for entry in scene.failures}
    offset = 0
    for file in files:
        if not current():
            raise _BatchCancelled
        result = file.result
        parsed = result.parse.items if result and result.parse else ()
        rows = []
        for ordinal, _item in enumerate(parsed, 1):
            index = offset + ordinal - 1
            entry = by_index.get(index)
            rows.append({'ordinal': ordinal, 'placement_known': entry.placement_known if entry else None,
                         'reason': entry.diagnostic if entry else failures.get(index, 'planning_failed')})
        offset += len(parsed)
        if result and rows:
            try:
                cache.record_scene_diagnostics(file.job.path, result.search['pdf_sha256'],
                                               is_main=file.job.is_main, descriptions=rows)
            except (OSError, ValueError, cache.CacheError):
                pass
    if not current():
        raise _BatchCancelled
    return BatchResult(tuple(files), scene, target.provider, target.model)


def _run_batch_jobs(jobs: Iterable[FileJob], total: int | None, cfg: Mapping[str, Any], *, target: ResolvedTarget,
                    search_api_key: str, send: Callable[..., Any], choose: Callable[..., Any] | None,
                    search_send: Callable[..., Any] | None = None,
                    search_base_url: str = legal_page_search.BASE_URL, scale: float = 16.0,
                    force_parse: bool = False,
                    current: Callable[[], bool] = lambda: True,
                    progress: Callable[[int, int], None] | None = None,
                    detail: Callable[[str], None] | None = None) -> BatchResult:
    if not legal_page_search.enabled(cfg):
        raise pipeline.ParseDisabled('Developer multiple-description switch is disabled.')
    files: list[FileResult] = []
    sources: list[drawing.DescriptionSource] = []
    source_complete: list[bool] = []
    try:
        for index, job in enumerate(jobs, 1):
            if progress and total:
                progress(index, total)
            file_result, file_sources, file_complete = _process_batch_file(
                job, index, total, cfg, target=target, search_api_key=search_api_key,
                send=send, choose=choose, search_send=search_send,
                search_base_url=search_base_url, force_parse=force_parse,
                current=current, detail=detail)
            files.append(file_result)
            sources.extend(file_sources)
            source_complete.extend(file_complete)
        return _finish_batch(files, sources, source_complete, cfg, target=target,
                             scale=scale, current=current, detail=detail)
    except _BatchCancelled:
        return BatchResult(tuple(files), drawing.SceneResult((), ()), target.provider, target.model, True)


def run_batch(jobs: Sequence[FileJob], cfg: Mapping[str, Any], *, target: ResolvedTarget,
              search_api_key: str, send: Callable[..., Any], choose: Callable[..., Any] | None,
              search_send: Callable[..., Any] | None = None,
              search_base_url: str = legal_page_search.BASE_URL, scale: float = 16.0,
              force_parse: bool = False,
              current: Callable[[], bool] = lambda: True,
              progress: Callable[[int, int], None] | None = None,
              detail: Callable[[str], None] | None = None) -> BatchResult:
    """Process each file independently and plan all usable results, without UI."""
    return _run_batch_jobs(jobs, len(jobs), cfg, target=target, search_api_key=search_api_key,
                           send=send, choose=choose, search_send=search_send,
                           search_base_url=search_base_url, scale=scale,
                           force_parse=force_parse, current=current,
                           progress=progress, detail=detail)


def run_batch_streaming(jobs: Iterable[FileJob], cfg: Mapping[str, Any], *, target: ResolvedTarget,
                        search_api_key: str, send: Callable[..., Any], choose: Callable[..., Any] | None,
                        search_send: Callable[..., Any] | None = None,
                        search_base_url: str = legal_page_search.BASE_URL, scale: float = 16.0,
                        force_parse: bool = False,
                        current: Callable[[], bool] = lambda: True,
                        detail: Callable[[str], None] | None = None) -> BatchResult:
    """Process jobs as an iterator yields them; plan the scene after it ends.

    Автопоток кормит сюда PDF по мере скачивания: поиск страниц и разбор
    первого файла идут, пока сбор качает следующий. Сцена, как и раньше,
    строится один раз по всем готовым описаниям.
    """
    return _run_batch_jobs(jobs, None, cfg, target=target, search_api_key=search_api_key,
                           send=send, choose=choose, search_send=search_send,
                           search_base_url=search_base_url, scale=scale,
                           force_parse=force_parse, current=current, detail=detail)


def _make_callbacks(target: ResolvedTarget, cfg: Mapping[str, Any], client: Any,
                      hedge_target: ResolvedTarget | None, hedge_client: Any,
                      progress: Callable[[str], None] | None,
                      hedge_delay_sec: float):
    import inspect
    api = cfg.get('api') if isinstance(cfg.get('api'), Mapping) else {}
    raw_temp = api.get('temperature', 0.0)
    temperature = float(raw_temp) if raw_temp is not None else 0.0
    effort = lp.resolve_reasoning_effort(cfg, provider=target.provider, model=target.model)
    sender_kw = {'temperature': temperature} if 'temperature' in inspect.signature(transport.make_sender).parameters else {}
    selector_kw = {'temperature': temperature} if 'temperature' in inspect.signature(transport.make_selector).parameters else {}
    send = transport.make_sender(client, target.model, effort, **sender_kw)
    choose = transport.make_selector(client, target.model, effort, **selector_kw)
    if (HEDGE_ENABLED and hedge_target is not None and hedge_client is not None
            and target.provider == 'hydra' and target.model == HYDRA_GROK_MODEL
            and hedge_target.provider == 'routerai' and hedge_target.model == ROUTERAI_GROK_MODEL):
        hedge_effort = lp.resolve_reasoning_effort(
            cfg, provider=hedge_target.provider, model=hedge_target.model)
        send = transport.make_hedged_sender(
            send, transport.make_sender(hedge_client, hedge_target.model, hedge_effort, **sender_kw),
            delay=hedge_delay_sec, notice=progress)
        choose = transport.make_hedged_selector(
            choose, transport.make_selector(hedge_client, hedge_target.model, hedge_effort, **selector_kw),
            delay=hedge_delay_sec, notice=progress)
    return send, choose


def make_runtime_callbacks(target: ResolvedTarget, cfg: Mapping[str, Any], *,
                           hedge_target: ResolvedTarget | None = None,
                           progress: Callable[[str], None] | None = None,
                           hedge_delay_sec: float = HEDGE_DELAY_SEC):
    api = cfg.get('api') if isinstance(cfg.get('api'), Mapping) else {}
    timeout = float(api.get('timeout_sec') or 500)
    client = transport.create_client({'api_key': target.api_key, 'base_url': target.base_url}, timeout=timeout)
    hedge_client = None
    if HEDGE_ENABLED and hedge_target is not None:
        try:
            hedge_client = transport.create_client(
                {'api_key': hedge_target.api_key, 'base_url': hedge_target.base_url}, timeout=timeout)
        except Exception:  # noqa: BLE001 - страховка необязательна
            hedge_client = None
    return _make_callbacks(target, cfg, client, hedge_target, hedge_client, progress, hedge_delay_sec)


@contextmanager
def open_runtime_callbacks(target: ResolvedTarget, cfg: Mapping[str, Any], *,
                           hedge_target: ResolvedTarget | None = None,
                           progress: Callable[[str], None] | None = None,
                           hedge_delay_sec: float = HEDGE_DELAY_SEC):
    """Own the transport client for one job and close it even on cancellation."""
    api = cfg.get('api') if isinstance(cfg.get('api'), Mapping) else {}
    timeout = float(api.get('timeout_sec') or 500)
    clients = [transport.create_client({'api_key': target.api_key, 'base_url': target.base_url}, timeout=timeout)]
    hedge_client = None
    if HEDGE_ENABLED and hedge_target is not None:
        try:
            hedge_client = transport.create_client(
                {'api_key': hedge_target.api_key, 'base_url': hedge_target.base_url}, timeout=timeout)
            clients.append(hedge_client)
        except Exception:  # noqa: BLE001 - страховка необязательна
            hedge_client = None
    try:
        yield _make_callbacks(target, cfg, clients[0], hedge_target, hedge_client,
                              progress, hedge_delay_sec)
    finally:
        for client in reversed(clients):
            try:
                client.close()
            except Exception:  # noqa: BLE001 - закрытие не должно маскировать результат
                pass


def restore_cached_batch(job: FileJob, cfg: Mapping[str, Any], *, scale: float = 16.0) -> BatchResult:
    """Explicitly restore one complete historic parse; never call a provider."""
    loaded = cache.load_latest_offline(job.path)
    if loaded is None:
        raise RuntimeUnavailable('offline_cache_unavailable')
    parsed, context = loaded
    sources = [drawing.DescriptionSource(job.path, job.is_main, item, anchor_for(job.path, item))
               for item in parsed.items]
    scene, _ = _plan_with_auto_rows(AcadBuilder(dict(cfg)), sources, scale=scale)
    if file_metadata._digest(job.path) != context['pdf_sha256']:
        raise RuntimeUnavailable('offline_cache_unavailable')
    result = pipeline.PipelineResult(search={'pdf_sha256': context['pdf_sha256']}, parse=parsed,
                                     status='parsed', cached=True, original_portal_lot=None,
                                     current_portal_lot=None, render_errors=())
    return BatchResult((FileResult(job, result, 'parsed'),), scene, context['provider'], context['model'])
