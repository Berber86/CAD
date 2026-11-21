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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import file_metadata  # noqa: E402
import legal_page_search as search  # noqa: E402
import llm_parser as lp  # noqa: E402
import multiple_description_runtime as runtime  # noqa: E402
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
                            'item_count': len(a.items), 'issue': a.issue} for a in parsed.attempts] if parsed else [],
        'parse_error': parsed.error if parsed else None,
        'selection': parsed.selection if parsed else None,
        'comparison_status': parsed.comparison.status if parsed and parsed.comparison else None,
        'descriptions': descriptions,
        'message': 'No AutoCAD drawing was made. A valid response does not prove the geometry correct.',
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--live', action='store_true', help='send PDF page images to RouterAI and Hydra; may be paid')
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
    except Exception:
        print('Cannot open PDF, or its page count is outside 1–8. No requests sent.', file=sys.stderr)
        return 2
    if not args.live:
        print(json.dumps({'mode': 'dry-run', 'pdf_pages': count, 'pdf_sha256': digest,
                          'initial_search_requests': 1, 'maximum_search_requests': 6,
                          'maximum_parse_requests': 4, 'maximum_comparison_requests': 1,
                          'message': 'No requests sent. --live is optional and may incur charges.'}, indent=2))
        return 0
    try:
        # Unlock only for an explicit live run; secrets remain in memory.
        router_key = common.load_provider_key('routerai')
        hydra_key = common.load_provider_key('hydra')
        cfg = copy.deepcopy(lp.load_config())
        cfg.setdefault('multiple_description_search', {})['enabled'] = True
        cfg.setdefault('api', {}).setdefault('providers', {}).setdefault('routerai', {})['api_key'] = router_key
        target, search_key = runtime.prepare_runtime(cfg, selected_provider='hydra',
                                                      selected_model='grok-4.7', selected_api_key=hydra_key)
        router = runtime.provider_target(cfg, 'routerai', search.PRIMARY, explicit_key=router_key)
        with tempfile.TemporaryDirectory(prefix='cad-stage3-') as folder:
            isolated = Path(folder) / args.pdf.name
            shutil.copyfile(args.pdf, isolated)
            if file_metadata._digest(isolated) != digest:
                raise ValueError('source changed during copy')
            with runtime.open_runtime_callbacks(target, cfg) as (send, choose):
                batch = runtime.run_batch([runtime.FileJob(isolated, True)], cfg, target=target,
                                          search_api_key=search_key, search_base_url=router.base_url,
                                          send=send, choose=choose)
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
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'parsed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
