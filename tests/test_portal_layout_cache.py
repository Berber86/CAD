from __future__ import annotations

import json

import portal_layout_cache as cache


def test_cache_round_trip_stores_only_layout_and_matches_browser_and_size(tmp_path):
    path = tmp_path / "portal_layout.json"
    box = (20, 160, 250, 180)
    assert cache.save("Chrome.exe", (1000, 800), box, path)
    assert json.loads(path.read_text()) == {
        "version": 1, "browser": "chrome.exe", "size": [1000, 800], "address_label": list(box),
    }
    assert cache.load("chrome.exe", (1000, 800), path) == box
    assert cache.load("msedge.exe", (1000, 800), path) is None
    assert cache.load("Chrome.exe", (1001, 800), path) is None


def test_bad_or_unusable_cache_is_ignored_and_cannot_overwrite_good_file(tmp_path):
    path = tmp_path / "layout.json"
    assert cache.save("chrome.exe", (1000, 800), (20, 160, 250, 180), path)
    initial = path.read_text()
    for box in [(20, 20, 900, 80), (20, 10, 19, 20), (-1, 0, 20, 30), (True, 1, 10, 20)]:
        assert not cache.save("chrome.exe", (1000, 800), box, path)
        assert path.read_text() == initial
    for content in ['{broken', '[]', json.dumps({"version": 2}), 'x' * 17000]:
        path.write_text(content)
        assert cache.load("chrome.exe", (1000, 800), path) is None


def test_cache_unwritable_is_nonfatal(tmp_path):
    parent = tmp_path / "not-a-dir"
    parent.write_text("x")
    assert not cache.save("chrome.exe", (1000, 800), (20, 160, 250, 180), parent / "layout.json")
    assert cache.load("chrome.exe", (1000, 800), parent / "layout.json") is None
