"""The stage-3 probe never contacts providers without explicit live mode."""
import json

from tools import multiple_description_stage3 as tool
from test_legal_page_search import make_pdf


def test_dry_run_reads_only_and_reports_bounded_request_budget(tmp_path, monkeypatch, capsys):
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 5)
    monkeypatch.setattr(tool.common, 'load_provider_key', lambda *a: (_ for _ in ()).throw(AssertionError('key')))
    monkeypatch.setattr(tool.runtime, 'prepare_runtime', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))
    assert tool.main([str(pdf)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['pdf_pages'] == 5 and result['mode'] == 'dry-run'
    assert result['maximum_search_requests'] == 6
    assert result['maximum_parse_requests'] == 4 and result['maximum_comparison_requests'] == 1
    assert list(tmp_path.iterdir()) == [pdf]


def test_large_or_non_pdf_rejected_before_key_lookup(tmp_path, monkeypatch, capsys):
    pdf = tmp_path / 'large.pdf'
    make_pdf(pdf, 9)
    monkeypatch.setattr(tool.common, 'load_provider_key', lambda *a: (_ for _ in ()).throw(AssertionError('key')))
    assert tool.main([str(pdf), '--live']) == 2
    assert 'No requests sent' in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [pdf]


def test_mock_live_uses_copy_and_cleans_all_diagnostics(tmp_path, monkeypatch, capsys):
    import contextlib
    from test_multiple_description_runtime import config, described, searcher
    from test_multiple_description_parse import answer
    source = tmp_path / 'OUR 6.pdf'
    make_pdf(source, 2)
    cfg = config()
    cfg['multiple_description_search']['enabled'] = False
    monkeypatch.setattr(tool.lp, 'load_config', lambda: cfg)
    monkeypatch.setattr(tool.common, 'load_provider_key', lambda p: {'hydra': 'h-secret', 'routerai': 'r-secret'}[p])
    target = tool.runtime.ResolvedTarget('hydra', 'grok-4.7', 'https://example.test', 'h-secret', 10)
    router = tool.runtime.ResolvedTarget('routerai', 'search', 'https://example.test', 'r-secret', 10)
    def prepare(loaded, **kwargs):
        assert loaded is not cfg and loaded['multiple_description_search']['enabled'] is True
        assert loaded['api']['providers']['routerai']['api_key'] == 'r-secret'
        return target, 'r-secret'
    monkeypatch.setattr(tool.runtime, 'prepare_runtime', prepare)
    monkeypatch.setattr(tool.runtime, 'provider_target', lambda *a, **k: router)
    @contextlib.contextmanager
    def callbacks(*args):
        yield lambda *a: answer([described('5', 55), described('6', 100)]), None
    monkeypatch.setattr(tool.runtime, 'open_runtime_callbacks', callbacks)
    run_batch = tool.runtime.run_batch
    used_paths = []
    def fake_run(jobs, loaded, **kwargs):
        used_paths.append(jobs[0].path)
        assert jobs[0].path.name == source.name and jobs[0].path != source
        return run_batch(jobs, loaded, search_send=searcher, **kwargs)
    monkeypatch.setattr(tool.runtime, 'run_batch', fake_run)
    assert tool.main([str(source), '--live']) == 0
    captured = capsys.readouterr()
    output = captured.out
    report = json.loads(output)
    assert 'Поиск: запрос' in captured.err and 'Разбор: запрос' in captured.err
    assert 'Схема подготовлена' in captured.err and 'secret' not in captured.err
    assert report['status'] == 'parsed' and len(report['descriptions']) == 2
    assert report['candidate_pages'] == [1, 2]
    assert 'secret' not in output
    assert used_paths and not used_paths[0].exists()
    assert list(tmp_path.iterdir()) == [source]
    assert cfg['multiple_description_search']['enabled'] is False


def test_diagnostic_dry_run_validates_page_list_without_network(tmp_path, monkeypatch, capsys):
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 5)
    monkeypatch.setattr(tool.common, 'load_provider_key', lambda *a: (_ for _ in ()).throw(AssertionError('key')))
    assert tool.main([str(pdf), '--diagnose-pages', '2,3']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['pages'] == [2, 3] and report['search_requests'] == 0
    assert report['maximum_parse_requests'] == 1
    for bad in ('0,2', '2,2', '3,2', '1,6', '2-3', ''):
        assert tool.main([str(pdf), '--diagnose-pages', bad, '--live']) == 2
        assert 'No requests sent' in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [pdf]


def test_mock_single_diagnostic_sends_once_without_search_and_masks_errors(tmp_path, monkeypatch, capsys):
    import contextlib
    from test_multiple_description_runtime import config
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 5)
    cfg = config()
    monkeypatch.setattr(tool.lp, 'load_config', lambda: cfg)
    keys = []
    monkeypatch.setattr(tool.common, 'load_provider_key', lambda provider: keys.append(provider) or 'secret')
    target = tool.runtime.ResolvedTarget('hydra', 'grok-4.7', 'https://example.test', 'secret', 10)
    monkeypatch.setattr(tool.runtime, 'provider_target', lambda *a, **k: target)
    calls = []
    class ApiError(Exception):
        status_code = 429
    @contextlib.contextmanager
    def callbacks(*args):
        def send(images, context):
            calls.append(([p.number for p in images], context))
            raise ApiError('SECRET body')
        yield send, lambda *a: (_ for _ in ()).throw(AssertionError('comparison'))
    monkeypatch.setattr(tool.runtime, 'open_runtime_callbacks', callbacks)
    monkeypatch.setattr(tool.runtime, 'run_batch', lambda *a, **k: (_ for _ in ()).throw(AssertionError('search')))
    assert tool.main([str(pdf), '--diagnose-pages', '2,3', '--live']) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result['failure_kind'] == 'http_429' and result['search_requests'] == 0
    assert result['max_parse_requests'] == 1 and result['elapsed_seconds'] >= 0
    assert keys == ['hydra'] and len(calls) == 1 and calls[0][0] == [2, 3]
    assert 'SECRET' not in captured.out + captured.err and 'secret' not in captured.out + captured.err
    assert list(tmp_path.iterdir()) == [pdf]


def test_mock_single_diagnostic_success_uses_existing_parser_only_once(tmp_path, monkeypatch, capsys):
    import contextlib
    from test_multiple_description_runtime import config, described
    from test_multiple_description_parse import answer
    pdf = tmp_path / 'OUR 6.pdf'
    make_pdf(pdf, 5)
    cfg = config()
    monkeypatch.setattr(tool.lp, 'load_config', lambda: cfg)
    monkeypatch.setattr(tool.common, 'load_provider_key', lambda provider: 'test-key')
    target = tool.runtime.ResolvedTarget('hydra', 'grok-4.7', 'https://example.test', 'test-key', 10)
    monkeypatch.setattr(tool.runtime, 'provider_target', lambda *a, **k: target)
    calls = []
    @contextlib.contextmanager
    def callbacks(*args):
        def send(images, context):
            calls.append([p.number for p in images])
            return answer([described('5', 55), described('6', 100)])
        yield send, None
    monkeypatch.setattr(tool.runtime, 'open_runtime_callbacks', callbacks)
    assert tool.main([str(pdf), '--diagnose-pages', '2,3', '--live']) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report['status'] == 'response_received' and report['description_count'] == 2
    assert [d['lot_from_description'] for d in report['descriptions']] == ['5', '6']
    assert calls == [[2, 3]] and report['search_requests'] == 0
    assert list(tmp_path.iterdir()) == [pdf]
