#!/usr/bin/env python3
"""Isolated stage-3 parse probe. Dry-run is free; --live may incur charges.

The PDF is copied to a temporary directory and no AutoCAD drawing is made. The
application's developer switch and original PDF/passport/sidecars are untouched.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import file_metadata  # noqa: E402
import legal_page_search as search  # noqa: E402
import llm_parser as lp  # noqa: E402
import multiple_description_runtime as runtime  # noqa: E402
from multiple_description_errors import classify_exception, describe_failure  # noqa: E402
from multiple_description_parse import PageImage, read_response  # noqa: E402
from multiple_description_progress import waiting  # noqa: E402
from tools import jev_page_probe as common  # noqa: E402

MAX_PAGES = 8  # One search batch; deliberately limit the live test budget.


def describe_result(batch: runtime.BatchResult) -> dict:
    """Display enough to compare sides, without keys, images or raw replies."""
    file = batch.files[0]
    result = file.result
    parsed = result.parse if result else None
    plans = {entry.description_index: entry for entry in batch.scene.plans}
    descriptions = []
    for index, item in enumerate(parsed.items if parsed else ()):
        plan = plans.get(index)
        descriptions.append({
            'ordinal': index + 1, 'complete': item.complete,
            'lot_from_description': item.data.lot_number,
            'distance_to_corner_ft': item.data.distance_to_corner_ft,
            'corner_to_pob_bearing': (item.data.corner_to_pob_bearing.bearing_label
                                      if item.data.corner_to_pob_bearing else None),
            'placed_in_shared_group': bool(plan and plan.placement_known),
            'pob_in_scene': list(plan.plan.pob) if plan else None,
            'planning_diagnostic': plan.diagnostic if plan else 'planning_failed',
            'sides': [{'bearing': seg.bearing_label, 'bearing_explicit': seg.has_bearing,
                       'bearing_solved': seg.bearing_solved, 'distance_ft': seg.distance_ft}
                      for seg in item.data.segments],
        })
    return {
        'mode': 'live', 'provider': batch.provider, 'model': batch.model,
        'status': file.status,
        'coverage_complete': result.search.get('coverage_complete') if result else False,
        'candidate_pages': result.search.get('candidate_pages') if result else [],
        'parsed_pages': list(parsed.pages) if parsed else [],
        'unparsed_pages': list(parsed.unparsed_pages) if parsed else [],
        'parse_complete': parsed.complete if parsed else False,
        'search_attempts': [{'model': a['model'], 'pages': a['pages'], 'error_kind': a['error_kind'],
                             'source': a['source']} for a in result.search.get('attempts', [])] if result else [],
        'parse_attempts': [{'pages': list(a.pages), 'valid_response': a.valid_response,
                            'item_count': len(a.items), 'issue': a.issue, 'failure_kind': a.failure_kind,
                            'elapsed_seconds': a.elapsed_seconds} for a in parsed.attempts] if parsed else [],
        'parse_error': parsed.error if parsed else None,
        'selection': parsed.selection if parsed else None,
        'comparison_status': parsed.comparison.status if parsed and parsed.comparison else None,
        'descriptions': descriptions,
        'message': 'No AutoCAD drawing was made. A valid response does not prove the geometry correct.',
    }


def _diagnose_pages(raw: str, total: int) -> list[int]:
    """Validate explicit page numbers without accepting duplicates or ranges."""
    parts = raw.split(',')
    if not parts or any(not p.strip().isascii() or not p.strip().isdigit() for p in parts):
        raise ValueError('invalid pages')
    pages = [int(p.strip()) for p in parts]
    if not pages or pages != sorted(set(pages)) or any(n < 1 or n > total for n in pages):
        raise ValueError('invalid pages')
    return pages


def diagnose_once(pdf: Path, digest: str, numbers: list[int], progress) -> tuple[dict, int]:
    """One explicit Grok call, without page search, sidecars, or retries."""
    progress('Диагностика: проверка сохранённого ключа Hydra.')
    hydra_key = common.load_provider_key('hydra')
    cfg = lp.load_config()
    target = runtime.provider_target(cfg, 'hydra', 'grok-4.7', explicit_key=hydra_key, image_limit=10)
    progress('Диагностика: подготовка выбранных страниц.')
    images = []
    for n in numbers:
        data = lp.render_pdf_pages_base64(pdf, dpi=220, max_pages=1, max_side_px=3200,
                                          pages=[n], rotation_deg=0, enhance_scan=False, output_format='png')
        if len(data) != 1 or not data[0].startswith('data:image/png;base64,'):
            raise ValueError('unavailable page image')
        images.append(PageImage(n, data[0]))
    if file_metadata._digest(pdf) != digest:
        raise ValueError('source changed')
    import lot_identity
    context = {'filename_lot': lot_identity.lot_from_filename(pdf), 'portal_lot': None}
    report = {'mode': 'single-request-diagnostic', 'provider': 'hydra', 'model': 'grok-4.7',
              'pages': numbers, 'search_requests': 0, 'max_parse_requests': 1}
    with runtime.open_runtime_callbacks(target, cfg) as (send, _):
        progress('Диагностика: отправляем один запрос разбора. Ожидаем ответ.')
        start = time.monotonic()
        try:
            with waiting(progress):
                response = send(tuple(images), context)
        except Exception as exc:
            kind = classify_exception(exc)
            report.update(status='request_failed', failure_kind=kind,
                          elapsed_seconds=round(time.monotonic() - start, 2))
            progress(f'Диагностика: запрос не выполнен ({describe_failure(kind)}).')
            return report, 1
        report['elapsed_seconds'] = round(time.monotonic() - start, 2)
    attempt = read_response(response, numbers)
    report.update(status='response_received' if attempt.valid_response else 'invalid_response',
                  failure_kind=None, response_issue=attempt.issue, complete=attempt.complete,
                  description_count=len(attempt.items),
                  descriptions=[{'ordinal': i + 1, 'complete': item.complete,
                                 'lot_from_description': item.data.lot_number,
                                 'sides': [{'bearing': seg.bearing_label, 'distance_ft': seg.distance_ft,
                                            'bearing_explicit': seg.has_bearing, 'bearing_solved': seg.bearing_solved}
                                           for seg in item.data.segments]}
                                for i, item in enumerate(attempt.items)])
    progress('Диагностика: ответ получен. Проверьте результат по документу.')
    return report, 0 if attempt.valid_response else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--live', action='store_true', help='send PDF page images to providers; may be paid')
    parser.add_argument('--diagnose-pages', metavar='2,3', help='one Hydra parse request for explicit pages; no search')
    args = parser.parse_args(argv)
    try:
        import pymupdf
        if args.pdf.suffix.lower() != '.pdf':
            raise ValueError('expected PDF')
        with pymupdf.open(args.pdf) as doc:
            count = len(doc)
            if doc.needs_pass or not 1 <= count <= MAX_PAGES:
                raise ValueError('unsupported PDF')
        digest = file_metadata._digest(args.pdf)
        chosen_pages = _diagnose_pages(args.diagnose_pages, count) if args.diagnose_pages is not None else None
    except Exception:
        print('Cannot open PDF, page count is outside 1–8, or selected pages are invalid. No requests sent.', file=sys.stderr)
        return 2
    if not args.live and chosen_pages is not None:
        print(json.dumps({'mode': 'dry-run-single-request-diagnostic', 'pdf_pages': count, 'pdf_sha256': digest,
                          'pages': chosen_pages, 'search_requests': 0, 'maximum_parse_requests': 1,
                          'message': 'No requests sent. --live is optional and may incur charges.'}, indent=2))
        return 0
    if not args.live:
        print(json.dumps({'mode': 'dry-run', 'pdf_pages': count, 'pdf_sha256': digest,
                          'initial_search_requests': 1, 'maximum_search_requests': 6,
                          'maximum_parse_requests': 4, 'maximum_comparison_requests': 1,
                          'message': 'No requests sent. --live is optional and may incur charges.'}, indent=2))
        return 0
    def progress(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    if chosen_pages is not None:
        try:
            report, result_code = diagnose_once(args.pdf, digest, chosen_pages, progress)
        except common.KeyUnavailable:
            print('Cannot access a saved Hydra key. No requests sent.', file=sys.stderr)
            return 2
        except Exception:
            print('Diagnostic could not complete. A request may already have been charged.', file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return result_code

    try:
        # Unlock only for an explicit live run; secrets remain in memory.
        progress('Подготовка: проверка сохранённых ключей. При необходимости введите PIN локально.')
        router_key = common.load_provider_key('routerai')
        hydra_key = common.load_provider_key('hydra')
        cfg = copy.deepcopy(lp.load_config())
        cfg.setdefault('multiple_description_search', {})['enabled'] = True
        cfg.setdefault('api', {}).setdefault('providers', {}).setdefault('routerai', {})['api_key'] = router_key
        progress('Подготовка: проверка модели и настроек. Документ пока не отправлен.')
        target, search_key = runtime.prepare_runtime(cfg, selected_provider='hydra',
                                                      selected_model='grok-4.7', selected_api_key=hydra_key)
        router = runtime.provider_target(cfg, 'routerai', search.PRIMARY, explicit_key=router_key)
        progress(f'Разбирающая модель: {target.provider} / {target.model}; лимит изображений: {target.image_limit}.')
        with tempfile.TemporaryDirectory(prefix='cad-stage3-') as folder:
            isolated = Path(folder) / args.pdf.name
            shutil.copyfile(args.pdf, isolated)
            if file_metadata._digest(isolated) != digest:
                raise ValueError('source changed during copy')
            with runtime.open_runtime_callbacks(target, cfg) as (send, choose):
                batch = runtime.run_batch([runtime.FileJob(isolated, True)], cfg, target=target,
                                          search_api_key=search_key, search_base_url=router.base_url,
                                          send=send, choose=choose, detail=progress)
            report = describe_result(batch)
            report['image_limit'] = target.image_limit
            report['vision_capability'] = target.capability
    except common.KeyUnavailable:
        print('Cannot access a saved provider key. No requests sent.', file=sys.stderr)
        return 2
    except Exception:
        # Do not echo exceptions: provider messages may include secrets or content.
        print('Probe could not complete. Check local settings; a request may already have been charged.',
              file=sys.stderr)
        return 2
    progress('Проверка завершена. Итог ниже; AutoCAD не запускался.')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'parsed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
