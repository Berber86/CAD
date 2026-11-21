#!/usr/bin/env python3
"""Developer-only page-search runner. No network or key unlock without --live.

The normal drawing workflow does not call this tool. The dedicated switch is
required for live execution unless an explicit isolated test is requested. An
isolated test enables the search in this process only, never in app settings.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import legal_page_search as search  # noqa: E402
import llm_parser as lp  # noqa: E402
from tools import jev_page_probe as common  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--live', action='store_true', help='send page images to RouterAI; requests may be paid')
    parser.add_argument('--isolated-test', action='store_true',
                        help='allow this isolated live test without changing the application switch')
    args = parser.parse_args(argv)
    try:
        cfg = lp.load_config()
    except Exception:
        print('Cannot load configuration; no requests sent.', file=sys.stderr)
        return 2
    if not args.live:
        try:
            import pymupdf
            if args.pdf.suffix.lower() != '.pdf':
                raise ValueError('expected PDF')
            with pymupdf.open(args.pdf) as doc:
                total = len(doc)
                if doc.needs_pass or not total:
                    raise ValueError('unreadable')
            covered = min(total, search.PAGE_CAP)
            groups = search.batch_numbers(covered)
        except Exception:
            print('Cannot open PDF; no requests sent.', file=sys.stderr)
            return 2
        print(json.dumps({'mode': 'dry-run', 'developer_switch_enabled': search.enabled(cfg),
                          'pdf_pages': total, 'covered_pages': covered, 'uncovered_pages': total - covered,
                          'batch_pages': groups, 'initial_requests_without_cache_or_render_errors': len(groups),
                          'max_attempts_per_batch': 6,
                          'message': 'No requests sent. Live mode requires the developer switch and may incur charges.'}, indent=2))
        return 0
    if args.isolated_test:
        cfg = copy.deepcopy(cfg)
        cfg["multiple_description_search"] = {"enabled": True}
    if not search.enabled(cfg):
        print('Developer switch is off. No requests sent.', file=sys.stderr)
        return 2
    try:
        key = common.load_provider_key('routerai')
        result = search.search_pdf(args.pdf, cfg, api_key=key)
    except common.KeyUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except search.SearchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({'mode': 'live', 'pdf_pages': result['pdf_pages'],
                      'covered_pages': result['covered_pages'], 'uncovered_pages': result['uncovered_pages'],
                      'coverage_complete': result['coverage_complete'], 'candidate_pages': result['candidate_pages'],
                      'diagnostics': str(search.diagnostics_path(args.pdf)),
                      'message': 'Search only; no parsing or drawing performed.'}, indent=2))
    return 0 if result['coverage_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
