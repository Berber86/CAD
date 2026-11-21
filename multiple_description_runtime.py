"""Runtime bridge for the disabled-by-default multi-description PDF workflow.

No network access or key lookup occurs on import. Capability lookup is public,
provider-specific, and failure means unknown, never automatic fallback. Paid
requests occur only through the injected/configured transports after an enabled
workflow has been explicitly started by the application.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
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
from acad_builder import AcadBuilder, compute_pob_point
from multiple_description_parse import MAX_IMAGES, DescriptionItem


class RuntimeUnavailable(RuntimeError):
    """Safe operational failure, never include provider bodies or credentials."""


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


def fetch_hydra_catalog() -> Any:
    """Public metadata only; no key or document is sent."""
    response = httpx.get('https://api.hydraai.ru/v1/models', timeout=10.0, follow_redirects=False)
    response.raise_for_status()
    return response.json()


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


def run_batch(jobs: Sequence[FileJob], cfg: Mapping[str, Any], *, target: ResolvedTarget,
              search_api_key: str, send: Callable[..., Any], choose: Callable[..., Any] | None,
              search_send: Callable[..., Any] | None = None,
              search_base_url: str = legal_page_search.BASE_URL, scale: float = 16.0,
              force_parse: bool = False,
              current: Callable[[], bool] = lambda: True,
              progress: Callable[[int, int], None] | None = None) -> BatchResult:
    """Process each file independently and plan all usable results, without UI."""
    if not legal_page_search.enabled(cfg):
        raise pipeline.ParseDisabled('Developer multiple-description switch is disabled.')
    files: list[FileResult] = []
    sources: list[drawing.DescriptionSource] = []
    for index, job in enumerate(jobs, 1):
        if not current():
            return BatchResult(tuple(files), drawing.SceneResult((), ()), target.provider, target.model, True)
        if progress:
            progress(index, len(jobs))
        try:
            portal_lot = job.portal_lot if job.portal_lot is not None else portal_lot_from_passport(job.path)
            result = pipeline.parse_pdf(job.path, cfg, search_api_key=search_api_key,
                                        provider=target.provider, model=target.model,
                                        image_limit=target.image_limit, portal_lot=portal_lot,
                                        send=send, choose=choose, search_send=search_send,
                                        search_base_url=search_base_url,
                                        force_parse=force_parse, current=current)
        except Cancelled:
            return BatchResult(tuple(files), drawing.SceneResult((), ()), target.provider, target.model, True)
        except Exception:
            # Never persist raw provider errors or sensitive text in diagnostics.
            try:
                cache.record_failure(job.path, file_metadata._digest(job.path), reason='processing_failed', pages=[])
            except (OSError, ValueError, cache.CacheError):
                pass
            files.append(FileResult(job, None, 'processing_failed'))
            continue
        if not current():
            return BatchResult(tuple(files), drawing.SceneResult((), ()), target.provider, target.model, True)
        files.append(FileResult(job, result, result.status))
        if result.parse is None:
            continue
        for item in result.parse.items:
            if not current():
                return BatchResult(tuple(files), drawing.SceneResult((), ()), target.provider, target.model, True)
            try:
                outcome = lp.ParseOutcome(data=item.data, source='llm:multi', provider=target.provider,
                                          model=target.model)
                item.data._source_identity = file_metadata.record_outcome(job.path, outcome)
            except (OSError, ValueError):
                try:
                    cache.record_failure(job.path, file_metadata._digest(job.path), reason='metadata_unavailable', pages=[])
                except (OSError, ValueError, cache.CacheError):
                    pass
            sources.append(drawing.DescriptionSource(job.path, job.is_main, item, anchor_for(job.path, item)))
    if not current():
        return BatchResult(tuple(files), drawing.SceneResult((), ()), target.provider, target.model, True)
    scene = drawing.plan_scene(AcadBuilder(dict(cfg)), sources, scale=scale)
    by_index = {entry.description_index: entry for entry in scene.plans}
    failures = {entry.description_index: entry.reason for entry in scene.failures}
    offset = 0
    for file in files:
        if not current():
            return BatchResult(tuple(files), drawing.SceneResult((), ()), target.provider, target.model, True)
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
        return BatchResult(tuple(files), drawing.SceneResult((), ()), target.provider, target.model, True)
    return BatchResult(tuple(files), scene, target.provider, target.model)


def make_runtime_callbacks(target: ResolvedTarget, cfg: Mapping[str, Any]):
    api = cfg.get('api') if isinstance(cfg.get('api'), Mapping) else {}
    timeout = float(api.get('timeout_sec') or 180)
    client = transport.create_client({'api_key': target.api_key, 'base_url': target.base_url}, timeout=timeout)
    return transport.make_sender(client, target.model), transport.make_selector(client, target.model)


@contextmanager
def open_runtime_callbacks(target: ResolvedTarget, cfg: Mapping[str, Any]):
    """Own the transport client for one job and close it even on cancellation."""
    api = cfg.get('api') if isinstance(cfg.get('api'), Mapping) else {}
    timeout = float(api.get('timeout_sec') or 180)
    client = transport.create_client({'api_key': target.api_key, 'base_url': target.base_url}, timeout=timeout)
    try:
        yield transport.make_sender(client, target.model), transport.make_selector(client, target.model)
    finally:
        client.close()
