from __future__ import annotations

from pathlib import Path

import pytest

import nyc_portal


_real_start_ocr_warmup = nyc_portal._start_ocr_warmup


@pytest.fixture(autouse=True)
def _stub_ocr_warmup(monkeypatch, tmp_path):
    # Эти тесты подменяют OCR и не загружают настоящую необязательную модель.
    from concurrent.futures import Future
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    ready = Future()
    ready.set_result(object())
    monkeypatch.setattr(nyc_portal, "_start_ocr_warmup", lambda: ready)


def _write_portal_screen(path):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1000, 800), "red")
    ImageDraw.Draw(image).rectangle((400, 84, 999, 799), fill="blue")
    image.save(path)


def test_parse_neighbour_result_keeps_only_numeric_map_labels():
    assert nyc_portal.parse_neighbour_result({
        "left_neighbor_lot": "68", "right_neighbor_lot": 72,
        "confidence": "high", "notes": "Labels visible.",
    }) == ("68", "72", "high", "Labels visible.", None)
    with pytest.raises(nyc_portal.PortalLookupError, match="нечисловое"):
        nyc_portal.parse_neighbour_result({"left_neighbor_lot": "6?", "right_neighbor_lot": 72})


@pytest.mark.parametrize("raw, expected", [
    (35, 35.0), ("35", 35.0), ("35°", 35.0), ("35 deg", 35.0), (190, 10.0),
    (-10, 170.0), (None, None), ("unclear", None), (True, None),
])
def test_parse_neighbour_result_reads_front_street_angle(raw, expected):
    *_rest, angle = nyc_portal.parse_neighbour_result({
        "left_neighbor_lot": "68", "right_neighbor_lot": "72",
        "front_street_angle_deg": raw,
    })
    assert angle == expected


def _stub_typed_visible(monkeypatch, visible_rows):
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _h, p: p)
    monkeypatch.setattr(nyc_portal, "_screen_ocr_region",
                        lambda _p, _r: visible_rows)
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)


@pytest.mark.parametrize("rows", [
    # Подпись целиком одной строкой (как на живом портале).
    [nyc_portal.ScreenText("Address (Enter an Address, Intersection or Common Place Name)",
                           (20, 100, 420, 120))],
    # Только плейсхолдер внутри поля — клик в само поле.
    [nyc_portal.ScreenText("Enter an address", (25, 130, 300, 155))],
    # OCR склеил/разделил слова иначе — «enter an address» дословно нет.
    [nyc_portal.ScreenText("Address (Enter an Address, Intersection or Common Place Name)",
                           (20, 100, 420, 120)),
     nyc_portal.ScreenText("Search", (430, 128, 480, 156))],
])
def test_type_into_address_finds_field_by_label_or_placeholder(rows, monkeypatch):
    clicks = []
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _h: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_focus", lambda _h: None)
    monkeypatch.setattr(nyc_portal.browser_save, "click_browser_window",
                        lambda _h, x, y: clicks.append((x, y)) or True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text", lambda _t: True)
    _stub_typed_visible(monkeypatch, [
        nyc_portal.ScreenText("1769 Stillwell Avenue, Brooklyn, NY", (25, 130, 300, 155))])
    nyc_portal._type_into_address(1, "1769 Stillwell Avenue, Brooklyn", rows)
    assert len(clicks) == 1


def test_type_into_address_retries_second_anchor_when_click_missed_field(monkeypatch):
    """Живой сбой: клик мимо поля — Ctrl+A выделил страницу, адрес ушёл в никуда."""
    rows = [
        nyc_portal.ScreenText("Address (Enter an Address, Intersection or Common Place Name)",
                              (20, 100, 420, 120)),
        nyc_portal.ScreenText("Enter an address", (25, 130, 300, 155)),
    ]
    clicks = []
    typed = []
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _h: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_focus", lambda _h: None)
    monkeypatch.setattr(nyc_portal.browser_save, "click_browser_window",
                        lambda _h, x, y: clicks.append((x, y)) or True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text",
                        lambda t: typed.append(t) or True)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _h, p: p)
    screens = iter([
        [nyc_portal.ScreenText("Terms of Use", (20, 300, 300, 320))],  # мимо поля
        [nyc_portal.ScreenText("1769 Stillwell Avenue, Brooklyn, NY", (25, 130, 300, 155))],
    ])
    monkeypatch.setattr(nyc_portal, "_screen_ocr_region", lambda _p, _r: next(screens))
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    nyc_portal._type_into_address(1, "1769 Stillwell Avenue, Brooklyn", rows)
    assert len(clicks) == 2 and len(typed) == 2


def test_type_into_address_rejects_house_number_seen_only_on_map(monkeypatch):
    """Живой сбой 3870: число дома светится на карте, ввод мимо поля засчитался."""
    rows = [
        nyc_portal.ScreenText("Address (Enter an Address, Intersection or Common Place Name)",
                              (20, 100, 420, 120)),
        nyc_portal.ScreenText("Enter an address", (25, 130, 300, 155)),
    ]
    clicks = []
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _h: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_focus", lambda _h: None)
    monkeypatch.setattr(nyc_portal.browser_save, "click_browser_window",
                        lambda _h, x, y: clicks.append((x, y)) or True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text", lambda _t: True)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _h, p: p)
    screens = iter([
        # Только номер дома где-то на карте — слова улицы в поле нет.
        [nyc_portal.ScreenText("52", (500, 300, 540, 330))],
        [nyc_portal.ScreenText("52 East 81st Street", (25, 130, 300, 155))],
    ])
    monkeypatch.setattr(nyc_portal, "_screen_ocr_region", lambda _p, _r: next(screens))
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    strategy = nyc_portal._type_into_address(1, "52 East 81st Street, New York", rows)
    assert strategy == "placeholder" and len(clicks) == 2


def test_type_into_address_falls_back_to_focused_typing(monkeypatch):
    """Все клики мимо: последняя стратегия — печать в уже стоящий фокус (Tab)."""
    rows = [
        nyc_portal.ScreenText("Address (Enter an Address, Intersection or Common Place Name)",
                              (20, 100, 420, 120)),
        nyc_portal.ScreenText("Enter an address", (25, 130, 300, 155)),
    ]
    clicks = []
    typed = []
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _h: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_focus", lambda _h: None)
    monkeypatch.setattr(nyc_portal.browser_save, "click_browser_window",
                        lambda _h, x, y: clicks.append((x, y)) or True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text",
                        lambda t: typed.append(t) or True)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _h, p: p)
    empty = [nyc_portal.ScreenText("Terms of Use", (20, 300, 300, 320))]
    screens = iter([empty, empty, empty, empty,
                    [nyc_portal.ScreenText("59-11 Fresh Pond Road", (25, 130, 300, 155))]])
    monkeypatch.setattr(nyc_portal, "_screen_ocr_region", lambda _p, _r: next(screens))
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    strategy = nyc_portal._type_into_address(1, "59-11 Fresh Pond Road, Queens", rows)
    assert strategy == "focus" and len(clicks) == 4 and len(typed) == 5


def test_drive_portal_failure_leaves_black_box_snapshot(tmp_path, monkeypatch):
    """Любой сбой портала: снимок экрана в папке задания + человеческий текст."""
    window = nyc_portal.browser_save.BrowserWindow(
        handle=42, title="Some other page - Chrome",
        executable="chrome.exe", image_path=r"C:\Chrome\chrome.exe",
    )
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)

    def capture(_handle, destination):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return path

    monkeypatch.setattr(nyc_portal, "_save_window_capture", capture)

    def rest(*_args, **_kwargs):
        raise nyc_portal._ScreenNotFound("Не дождался страницу после двух проверок.")

    monkeypatch.setattr(nyc_portal, "_drive_portal_rest", rest)
    with pytest.raises(nyc_portal.PortalLookupError) as caught:
        nyc_portal._drive_portal("59-11 Fresh Pond Road, Queens", tmp_path / "TAX_MAP_x.jpg")
    shots = list(tmp_path.glob("PORTAL_FAIL_*.png"))
    assert len(shots) == 1 and str(shots[0]) in str(caught.value)


def test_type_into_address_still_errors_without_any_address_row(monkeypatch):
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _h: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_focus", lambda _h: None)
    with pytest.raises(nyc_portal.PortalLookupError, match="не найдено поле Address"):
        nyc_portal._type_into_address(
            1, "X", [nyc_portal.ScreenText("Find a Property", (0, 0, 100, 20))])


def test_vision_prompt_asks_for_front_street_angle():
    assert "front_street_angle_deg" in nyc_portal.VISION_SYSTEM_PROMPT
    assert "clockwise from geographic north" in nyc_portal.VISION_SYSTEM_PROMPT


def test_vision_prompt_returns_both_neighbours_when_sides_uncertain():
    # Случай 3868: карта повёрнута, оба соседа видны, стороны — нет.
    assert "still return both numbers" in nyc_portal.VISION_SYSTEM_PROMPT
    assert "Return null only for a parcel whose label or adjacency itself is unclear" \
        in nyc_portal.VISION_SYSTEM_PROMPT


def test_local_portal_ocr_extracts_borough_block_and_lot():
    rows = [
        nyc_portal.ScreenText("Borough: Staten Island", (10, 300, 190, 325)),
        nyc_portal.ScreenText("Block: 5,712", (200, 300, 310, 325)),
        nyc_portal.ScreenText("Lot: 70", (320, 300, 400, 325)),
    ]
    assert nyc_portal._extract_portal_details(rows) == ("5712", "70", "Staten Island")


def test_local_portal_ocr_requires_block_and_lot_but_borough_may_be_blank():
    with pytest.raises(nyc_portal.PortalLookupError, match="не смог прочитать Block и Lot"):
        nyc_portal._extract_portal_details([nyc_portal.ScreenText("Lot: 70", (1, 1, 20, 20))])


def test_lookup_uses_local_bbl_and_sends_only_map_to_one_vision_request(tmp_path, monkeypatch):
    from PIL import Image

    map_image = tmp_path / "NYC_Portal_map.png"
    Image.new("RGB", (600, 400), "blue").save(map_image)
    calls = {}
    usage = nyc_portal.TokenUsage(
        prompt_tokens=30, completion_tokens=10, total_tokens=40,
        provider="hydra", provider_label="Hydra AI", model="vision", available=True,
    )

    def drive(address, path, progress=None, **_kwargs):
        calls["address"] = address
        calls["map_path"] = path
        return nyc_portal.PortalCapture("5712", "70", "Staten Island", map_image)

    def encode(paths, **kwargs):
        calls["image_paths"] = list(paths)
        calls["encode_options"] = kwargs
        calls["image_size"] = Image.open(paths[0]).size
        calls["image_pixel"] = Image.open(paths[0]).getpixel((0, 0))
        return ["data:image/jpeg;base64,AA=="], []

    def ask(system_prompt, user_text, cfg, **kwargs):
        calls.update({"system_prompt": system_prompt, "user_text": user_text, "cfg": cfg})
        calls["ask_options"] = kwargs
        return ({
            "left_neighbor_lot": "68", "right_neighbor_lot": "72",
            "confidence": "high", "notes": "Labels visible.",
            "front_street_angle_deg": 35,
        }, usage)

    monkeypatch.setattr(nyc_portal, "_drive_portal", drive)
    monkeypatch.setattr(nyc_portal, "render_image_files_base64", encode)
    monkeypatch.setattr(nyc_portal, "ask_llm_json", ask)
    result = nyc_portal.lookup_parcel(
        "87 Stafford Ave", tmp_path, cfg={"api": {}}, provider="hydra", model="vision",
        api_key="secret", key_secret="pin",
    )
    assert (result.borough, result.block, result.lot) == ("Staten Island", "5712", "70")
    assert result.neighbours == ["68", "72"]
    # Угол фронтальной улицы: Vision + две программные оценки + JSON-кэш.
    assert result.front_street_angle_deg == 35.0
    assert result.front_street_angles["llm"] == 35.0
    assert set(result.front_street_angles) == {
        "llm", "python_hough_lines", "python_structure_tensor"}
    cache = tmp_path / "NYC_Portal_map_angles.json"
    assert cache.is_file()
    import json as _json
    cached = _json.loads(cache.read_text(encoding="utf-8"))
    assert cached["front_street_angle_deg"]["llm"] == 35.0
    assert cached["block"] == "5712" and cached["lot"] == "70"
    assert result.map_image == map_image and result.map_image.is_file()
    assert result.usage is usage
    assert calls["ask_options"]["attempts"] == 1
    assert calls["ask_options"]["single_request"] is True
    assert calls["cfg"]["api"]["max_retries"] == 0
    assert calls["ask_options"]["image_data_urls"] == ["data:image/jpeg;base64,AA=="]
    assert calls["ask_options"]["api_key"] == "secret"
    assert calls["encode_options"]["enhance_scan"] is False
    assert calls["image_pixel"] == (0, 0, 255)  # blue test image: only the map is attached
    assert "only a cropped nyc tax-map" in calls["system_prompt"].lower()
    assert "block_number" not in calls["system_prompt"]


def test_portal_driver_uses_current_window_and_clicks_address_suggestion(tmp_path, monkeypatch):
    window = nyc_portal.browser_save.BrowserWindow(
        handle=42, title="Property Information Portal - Chrome",
        executable="chrome.exe", image_path=r"C:\Chrome\chrome.exe",
    )
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _handle: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _handle: True)
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _handle: (0, 0, 1000, 800))
    clicks = []
    keys = []
    waits = []
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda seconds: waits.append(seconds))
    monkeypatch.setattr(
        nyc_portal, "_click_text",
        lambda handle, rows, text: clicks.append((handle, text)) or rows[0],
    )
    monkeypatch.setattr(
        nyc_portal, "_click_row",
        lambda handle, row: clicks.append((handle, row.text)) or row,
    )
    monkeypatch.setattr(
        nyc_portal, "_key", lambda _handle, vk: keys.append(vk),
    )
    monkeypatch.setattr(
        nyc_portal, "_type_into_address", lambda _handle, address, _rows, *_rest: clicks.append((42, address)),
    )
    ocr_calls = iter([
        # The first entry is consumed by the quick top-left-quarter portal check
        # that now runs before any Select hover/click.
        [nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
        # Confirms the dropdown actually opened (its "Address" option row is now
        # visible) before Down/Down/Enter are sent — see the regression test for
        # why: sending them before the menu was open made it look like it opened
        # and instantly did nothing.
        [nyc_portal.ScreenText("Address", (20, 130, 80, 150))],
        [nyc_portal.ScreenText("Address (Enter an Address, Intersection or Common Place Name)",
                               (20, 160, 550, 180))],
        [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (30, 255, 500, 275))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))],
        [nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))],
    ])
    monkeypatch.setattr(nyc_portal, "_screen_ocr", lambda _path: next(ocr_calls))

    def capture(_handle, destination):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_portal_screen(path)
        return path

    monkeypatch.setattr(nyc_portal, "_save_window_capture", capture)
    destination = tmp_path / "parcel.png"
    capture_result = nyc_portal._drive_portal("87 Stafford Ave", destination)
    assert capture_result.block == "5712" and capture_result.lot == "70"
    assert capture_result.borough == "Staten Island"
    assert capture_result.map_image.is_file()
    assert clicks == [(42, "Select"), (42, "87 Stafford Ave"),
                      (42, "87 Stafford Avenue, Staten Island, NY, USA")]
    assert waits == [1.0, 1.0, 0.2, 0.2, 0.2, 1.5, 0.2, 5.0, 6.0]
    assert keys == [
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_RETURN,
        nyc_portal.browser_save.VK_TAB,
    ]


def test_portal_driver_stops_on_challenge_without_keypress(tmp_path, monkeypatch):
    window = nyc_portal.browser_save.BrowserWindow(
        handle=42, title="Property Information Portal - Chrome",
        executable="chrome.exe", image_path=r"C:\Chrome\chrome.exe",
    )
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _handle: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _handle: True)

    def save_screen(_handle, path):
        _write_portal_screen(Path(path))
        return Path(path)

    monkeypatch.setattr(nyc_portal, "_save_window_capture", save_screen)
    monkeypatch.setattr(
        nyc_portal, "_screen_ocr",
        lambda _path: [nyc_portal.ScreenText("Verify you are human", (1, 1, 100, 20))],
    )
    key_calls = []
    monkeypatch.setattr(nyc_portal, "_key", lambda *args: key_calls.append(args))
    with pytest.raises(nyc_portal.PortalLookupError, match="CAPTCHA"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "parcel.png")
    assert key_calls == []


def test_non_portal_active_browser_opens_portal_in_a_new_tab(tmp_path, monkeypatch):
    window = nyc_portal.browser_save.BrowserWindow(
        handle=77, title="New Tab - Google Chrome",
        executable="chrome.exe", image_path=r"C:\Chrome\chrome.exe",
    )
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _handle: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _handle: True)
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _handle: (0, 0, 1000, 800))
    typed = []
    key_events = []
    waits = []
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda seconds: waits.append(seconds))
    new_tabs = []
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_t", lambda: new_tabs.append(1) or True)
    monkeypatch.setattr(nyc_portal.browser_save, "active_window_title", lambda: "New Tab - Google Chrome")
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_l", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text", lambda text: typed.append(text) or True)
    monkeypatch.setattr(nyc_portal, "_key", lambda _handle, vk: key_events.append(vk))
    monkeypatch.setattr(
        nyc_portal, "_click_text",
        lambda handle, rows, text: rows[0],
    )
    monkeypatch.setattr(nyc_portal, "_click_row", lambda _handle, row: row)
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda *_args: None)
    ocr_calls = iter([
        [nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
        # Confirms the dropdown actually opened before Down/Down/Enter are sent.
        [nyc_portal.ScreenText("Address", (20, 130, 80, 150))],
        [nyc_portal.ScreenText("Address (Enter an Address)", (20, 160, 550, 180))],
        [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (30, 255, 500, 275))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))],
        [nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))],
    ])
    monkeypatch.setattr(nyc_portal, "_screen_ocr", lambda _path: next(ocr_calls))

    def capture(_handle, destination):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_portal_screen(path)
        return path

    monkeypatch.setattr(nyc_portal, "_save_window_capture", capture)
    destination = tmp_path / "parcel.png"
    capture_result = nyc_portal._drive_portal("87 Stafford Ave", destination)
    assert capture_result.block == "5712" and capture_result.lot == "70"
    assert capture_result.borough == "Staten Island"
    assert capture_result.map_image.is_file()
    assert typed == [nyc_portal.PORTAL_URL]
    assert new_tabs == [1]  # портал открыт в новой вкладке
    assert waits == [0.6, 5.0, 1.0, 0.2, 0.2, 0.2, 1.5, 0.2, 5.0, 6.0]  # 0.6 — ждём переключения на новую вкладку
    # Перед Enter — Delete: сносит автодополнение адреса из истории браузера,
    # иначе Enter уходит на последнюю страницу портала, а не на напечатанный
    # адрес (живой случай Николая с просмотром улиц Cyclomedia).
    assert key_events[0] == nyc_portal.browser_save.VK_DELETE
    assert key_events[1] == nyc_portal.browser_save.VK_RETURN
    # Подсказка выбирается кликом мыши, а не Down+Enter: клавиши заканчиваются на Tab.
    assert key_events[-1] == nyc_portal.browser_save.VK_TAB


def test_single_request_json_mode_never_falls_back_to_second_paid_call(monkeypatch):
    import llm_parser

    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            message = type("Message", (), {"content": '{"ok": true}'})
            choice = type("Choice", (), {"message": message, "finish_reason": "stop"})
            usage = type("Usage", (), {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
            return type("Completion", (), {"choices": [choice], "usage": usage})

    client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    monkeypatch.setattr(
        llm_parser, "resolve_api_target",
        lambda *args, **kwargs: {
            "model": "vision", "provider": "hydra", "provider_label": "Hydra",
            "api_key": "key", "base_url": "https://example.invalid", "env_key": "",
        },
    )
    monkeypatch.setattr(llm_parser, "_make_client", lambda *args, **kwargs: client)
    data, _usage = llm_parser.ask_llm_json(
        "system", "return JSON", {"api": {}}, single_request=True, attempts=3,
    )
    assert data == {"ok": True}
    assert len(calls) == 1
    assert "response_format" not in calls[0]


def test_portal_driver_does_not_close_a_select_menu_left_open(tmp_path, monkeypatch):
    window = nyc_portal.browser_save.BrowserWindow(
        handle=42, title="Property Information Portal - Chrome",
        executable="chrome.exe", image_path=r"C:\Chrome\chrome.exe",
    )
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _handle: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _handle: True)
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _handle: (0, 0, 1000, 800))
    clicks, keys = [], []
    waits = []
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda seconds: waits.append(seconds))
    monkeypatch.setattr(nyc_portal, "_click_text", lambda *_args: clicks.append(True))
    row_clicks = []
    monkeypatch.setattr(nyc_portal, "_click_row", lambda _handle, row: row_clicks.append(row.text) or row)
    monkeypatch.setattr(nyc_portal, "_key", lambda _handle, vk: keys.append(vk))
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda *_args: None)
    screens = iter([
        # The first entry is consumed by the quick top-left-quarter portal check
        # that now runs before any Select hover/click.
        [nyc_portal.ScreenText("Select", (10, 10, 90, 30)),
         nyc_portal.ScreenText("Borough/Block", (10, 40, 200, 60)),
         nyc_portal.ScreenText("Address", (10, 70, 100, 90))],
        [nyc_portal.ScreenText("Address (Enter an Address)", (20, 160, 550, 180))],
        [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (30, 255, 500, 275))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))],
        [nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))],
    ])
    monkeypatch.setattr(nyc_portal, "_screen_ocr", lambda _path: next(screens))

    def capture(_handle, destination):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_portal_screen(path)
        return path

    monkeypatch.setattr(nyc_portal, "_save_window_capture", capture)
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "parcel.png")
    assert result.block == "5712" and result.lot == "70"
    assert result.map_image.is_file()
    assert clicks == []
    assert row_clicks == ["87 Stafford Avenue, Staten Island, NY, USA"]
    assert waits == [1.0, 0.2, 0.2, 0.2, 1.5, 0.2, 5.0, 6.0]
    assert keys[:5] == [
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_RETURN,
        nyc_portal.browser_save.VK_TAB,
    ]


def test_tax_map_crop_excludes_all_left_property_details_and_header(tmp_path):
    from PIL import Image, ImageDraw

    source = tmp_path / "full.png"
    image = Image.new("RGB", (1000, 500), "red")
    draw = ImageDraw.Draw(image)
    draw.rectangle((400, 0, 999, 51), fill="green")  # site header above the map
    draw.rectangle((400, 52, 999, 499), fill="blue")  # map pane
    image.save(source)
    rows = [
        nyc_portal.ScreenText("Borough: Staten Island", (10, 300, 160, 320)),
        nyc_portal.ScreenText("Block: 5712", (180, 300, 270, 320)),
        nyc_portal.ScreenText("Lot: 70", (300, 300, 390, 320)),
    ]
    output = nyc_portal._crop_tax_map(source, rows, tmp_path / "map.png")
    crop = Image.open(output)
    assert crop.size == (598, 448)
    assert crop.getpixel((0, 0)) == (0, 0, 255)
    assert crop.getpixel((crop.width - 1, crop.height - 1)) == (0, 0, 255)


def test_initial_portal_check_ocr_uses_only_top_left_quarter(tmp_path, monkeypatch):
    from PIL import Image

    source = tmp_path / "browser.png"
    Image.new("RGB", (1000, 800), "white").save(source)
    seen = {}

    def ocr(path):
        seen["size"] = Image.open(path).size
        return [nyc_portal.ScreenText("Select", (10, 10, 50, 30))]

    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    rows = nyc_portal._screen_ocr_top_left_quarter(source)
    assert seen["size"] == (500, 400)
    assert nyc_portal._contains(rows, "Select")


def test_focus_does_not_fight_an_open_select_dropdown_popup(monkeypatch):
    """Keeping keyboard input flowing to a just-opened <select> dropdown matters:
    treating its own popup HWND as "not focused" and forcing the parent tab back
    to the foreground would close the dropdown within a blink of opening it."""
    window = nyc_portal.browser_save.BrowserWindow(
        handle=42, title="Property Information Portal - Chrome",
        executable="chrome.exe", image_path=r"C:\Chrome\chrome.exe",
    )
    monkeypatch.setattr(nyc_portal.browser_save, "list_windows", lambda: [window])
    monkeypatch.setattr(nyc_portal.browser_save, "foreground", lambda _handle: True)
    # The dropdown popup (a different HWND, same browser process) is foreground.
    monkeypatch.setattr(nyc_portal.browser_save, "foreground_is_acceptable", lambda _handle: True)
    nyc_portal._focus(42)  # must not raise


def test_focus_raises_when_a_different_application_is_actually_focused(monkeypatch):
    window = nyc_portal.browser_save.BrowserWindow(
        handle=42, title="Property Information Portal - Chrome",
        executable="chrome.exe", image_path=r"C:\Chrome\chrome.exe",
    )
    monkeypatch.setattr(nyc_portal.browser_save, "list_windows", lambda: [window])
    monkeypatch.setattr(nyc_portal.browser_save, "foreground", lambda _handle: True)
    monkeypatch.setattr(nyc_portal.browser_save, "foreground_is_acceptable", lambda _handle: False)
    with pytest.raises(nyc_portal.PortalLookupError, match="активировать"):
        nyc_portal._focus(42)


def test_select_dropdown_popup_stealing_foreground_does_not_abort_the_down_arrow_keys(monkeypatch):
    """Regression test for the Select menu opening and instantly closing: once the
    click opens the dropdown, Windows reports its own popup HWND as foreground, and
    _key() (via _focus) must keep sending Down/Down/Enter instead of forcing the
    parent tab back and killing the popup."""
    window = nyc_portal.browser_save.BrowserWindow(
        handle=42, title="Property Information Portal - Chrome",
        executable="chrome.exe", image_path=r"C:\Chrome\chrome.exe",
    )
    monkeypatch.setattr(nyc_portal.browser_save, "list_windows", lambda: [window])
    monkeypatch.setattr(nyc_portal.browser_save, "foreground", lambda _handle: True)
    # Simulate: foreground is the dropdown's own popup window once it is open.
    monkeypatch.setattr(nyc_portal.browser_save, "foreground_is_acceptable", lambda _handle: True)
    sent = []
    monkeypatch.setattr(nyc_portal.browser_save, "press_key", lambda vk: sent.append(vk) or True)
    nyc_portal._key(42, nyc_portal.browser_save.VK_DOWN)
    nyc_portal._key(42, nyc_portal.browser_save.VK_DOWN)
    nyc_portal._key(42, nyc_portal.browser_save.VK_RETURN)
    assert sent == [
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_RETURN,
    ]


class _FakeClock:
    """Подмена time: monotonic() идёт вперёд только от sleep()."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _wait_setup(monkeypatch, screens):
    clock = _FakeClock()
    seen = []
    monkeypatch.setattr(nyc_portal, "time", clock)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _handle: None)
    captures = []
    def capture(_h, path):
        captures.append(True)
        Path(path).write_bytes(str(len(captures)).encode())
    monkeypatch.setattr(nyc_portal, "_save_window_capture", capture)

    def ocr(_path):
        seen.append(True)
        return screens[min(len(seen), len(screens)) - 1]

    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    return clock, seen


def test_reliable_wait_takes_one_snapshot_after_initial_delay(monkeypatch):
    select = [nyc_portal.ScreenText("Select", (20, 100, 80, 130))]
    clock, seen = _wait_setup(monkeypatch, [select])
    rows = nyc_portal._wait_screen(1, lambda screen: bool(screen), description="страницу", first_delay=1.5)
    assert rows == select
    assert len(seen) == 1 and clock.sleeps == [1.5]


def test_reliable_wait_retries_only_once_after_two_seconds(monkeypatch):
    empty = [nyc_portal.ScreenText("loading", (20, 100, 80, 130))]
    select = [nyc_portal.ScreenText("Select", (20, 100, 80, 130))]
    clock, seen = _wait_setup(monkeypatch, [empty, select])
    rows = nyc_portal._wait_screen(1, lambda screen: screen[0].text == "Select", description="форму", first_delay=1)
    assert rows == select
    assert len(seen) == 2 and clock.sleeps == [1, 2.0]


def test_reliable_wait_stops_after_two_failed_checks(monkeypatch):
    clock, seen = _wait_setup(monkeypatch, [[]])
    with pytest.raises(nyc_portal.PortalLookupError, match="двух проверок"):
        nyc_portal._wait_screen(1, lambda _s: False, description="окно", first_delay=3)
    assert len(seen) == 2 and clock.sleeps == [3, 2.0]


def test_map_is_captured_only_after_the_redraw_wait_following_the_wheel(monkeypatch):
    import ctypes

    clock = _FakeClock()
    monkeypatch.setattr(nyc_portal, "time", clock)
    monkeypatch.setattr(nyc_portal, "sys", type("S", (), {"platform": "win32"}))
    wheel = []

    class _User32:
        def GetWindowRect(self, _hwnd, rect_ref):
            rect = rect_ref._obj
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1000, 800
            return 1

        def SetCursorPos(self, x, y):
            wheel.append(("cursor", x, y))

        def mouse_event(self, flags, dx, dy, data, extra):
            wheel.append(("wheel", data))

    fake_windll = type("W", (), {"user32": _User32()})()
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    nyc_portal._zoom_out_map_pane(1)
    assert wheel == [("cursor", 750, 400), ("wheel", -120)]
    assert clock.sleeps == [0.1, nyc_portal.MAP_REDRAW_WAIT_SEC]
    assert nyc_portal.MAP_REDRAW_WAIT_SEC == pytest.approx(3.8)
    clock.sleeps.clear()
    nyc_portal._zoom_out_map_pane(1, 1.5)
    assert clock.sleeps == [0.1, 1.5]  # пользовательская пауза быстрого режима


def test_lookup_parcel_passes_fast_mode_to_the_portal_driver(monkeypatch, tmp_path):
    received = {}

    def drive(address, map_path, progress=None, *, fast_mode=False, fast_pause_sec=None):
        received["fast_mode"] = fast_mode
        received["fast_pause_sec"] = fast_pause_sec
        raise nyc_portal.PortalLookupError("стоп после проверки аргумента")

    monkeypatch.setattr(nyc_portal, "_drive_portal", drive)
    with pytest.raises(nyc_portal.PortalLookupError, match="стоп"):
        nyc_portal.lookup_parcel(
            "87 Stafford Ave", tmp_path, cfg={"api": {}}, provider="hydra", model="vision",
            fast_mode=True, fast_pause_sec=2.5,
        )
    assert received == {"fast_mode": True, "fast_pause_sec": 2.5}


@pytest.mark.parametrize(
    ("text", "expected"),
    [("5", 5.0), ("1", 1.0), ("3,5", 3.5), (" 2.25 ", 2.25), ("4.9", 4.9)],
)
def test_fast_pause_accepts_any_number_from_one_to_five(text, expected):
    assert nyc_portal.parse_fast_pause(text) == expected


@pytest.mark.parametrize("text", ["", "0.9", "5.1", "6", "-1", "abc", "nan", "inf", None])
def test_fast_pause_rejects_numbers_outside_the_range_and_garbage(text):
    assert nyc_portal.parse_fast_pause(text) is None


def test_address_is_not_typed_when_ctrl_t_did_not_lead_to_a_new_tab(tmp_path, monkeypatch):
    """Если после Ctrl+T активна не новая вкладка (например, переписка), текст не печатается."""
    window = nyc_portal.browser_save.BrowserWindow(
        handle=77, title="Arena - Chrome", executable="chrome.exe", image_path=r"C:\Chrome\chrome.exe",
    )
    typed = []
    omnibox = []
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _handle: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _handle: True)
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_t", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "active_window_title", lambda: "Arena - Chrome")
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_l", lambda: omnibox.append("ctrl_l") or True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text", lambda text: typed.append(text) or True)
    with pytest.raises(nyc_portal.PortalLookupError, match="не новая вкладка"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "parcel.png")
    assert typed == [] and omnibox == []


def test_same_unchanged_screenshot_does_not_repeat_ocr(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(nyc_portal, "time", clock)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    captures, scans = [], []
    def capture(_, path):
        captures.append(True)
        Path(path).write_bytes(b"same pixels")
    monkeypatch.setattr(nyc_portal, "_save_window_capture", capture)
    monkeypatch.setattr(nyc_portal, "_screen_ocr", lambda _: scans.append(True) or [])
    with pytest.raises(nyc_portal.PortalLookupError):
        nyc_portal._wait_screen(1, lambda r: False, first_delay=1.5, description="экран")
    assert len(captures) == 2 and len(scans) == 1


def test_region_ocr_offsets_boxes_to_window_coordinates(tmp_path, monkeypatch):
    from PIL import Image
    path = tmp_path / "image.png"
    Image.new("RGB", (1000, 800), "white").save(path)
    sizes = []
    def ocr(path):
        sizes.append(Image.open(path).size)
        return [nyc_portal.ScreenText("x", (5, 10, 15, 20), 0.9)]
    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    rows = nyc_portal._screen_ocr_region(path, (.2, .25, .8, .75))
    assert sizes == [(600, 400)]
    assert rows[0].box == (205, 210, 215, 220)


def test_initial_screen_reuses_quarter_and_only_falls_back_if_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _, path: _write_portal_screen(Path(path)))
    seen = []
    def ocr(path):
        from PIL import Image
        size = Image.open(path).size
        seen.append(size)
        return [nyc_portal.ScreenText("Select", (10, 10, 80, 30))]
    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    assert nyc_portal._contains(nyc_portal._initial_screen(42), "Select")
    assert seen == [(500, 400)]
    seen.clear()
    def fallback(path):
        from PIL import Image
        size = Image.open(path).size
        seen.append(size)
        return [] if size == (500, 400) else [nyc_portal.ScreenText("Select", (700, 200, 760, 230))]
    monkeypatch.setattr(nyc_portal, "_screen_ocr", fallback)
    assert nyc_portal._contains(nyc_portal._initial_screen(42), "Select")
    assert seen == [(500, 400), (1000, 800)]


def test_normal_suggestion_takes_first_row_below_field_not_the_input(monkeypatch):
    """Текст подсказки не сверяется (решение пользователя): первая строка списка."""
    label = [nyc_portal.ScreenText("Enter an Address", (10, 160, 300, 180))]
    row = lambda text, y: nyc_portal.ScreenText(text, (20, y, 300, y + 15))
    input_value = row("87 Stafford Avenue", 210)
    correct = row("87 Stafford Avenue, Staten Island, NY, USA", 250)
    incorrect = row("88 Stafford Avenue, Staten Island", 250)
    later_match = row("87 Stafford Avenue", 280)
    assert nyc_portal._suggestion_row([input_value]) is None
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _: None)
    correct_ny = row("87 Stafford Avenue, Staten Island, NY, USA", 250)
    incorrect_ny = row("88 Stafford Avenue, Staten Island, NY, USA", 250)
    monkeypatch.setattr(nyc_portal, "_wait_screen", lambda *args, **kw: [input_value, correct_ny])
    assert nyc_portal._wait_after_address_input(42, "87 Stafford Ave", label) == correct_ny
    # Даже «чужая» первая строка берётся как есть: проверка текста убрана.
    monkeypatch.setattr(nyc_portal, "_wait_screen", lambda *args, **kw: [incorrect_ny, later_match])
    assert nyc_portal._wait_after_address_input(42, "87 Stafford Ave", label) == incorrect_ny


@pytest.mark.parametrize("start", ["select", "address", "previous"])
def test_fast_driver_skips_intermediate_ocr_and_uses_pauses(start, tmp_path, monkeypatch):
    from PIL import Image
    window = nyc_portal.browser_save.BrowserWindow(handle=42, title="Property Information Portal - Chrome", executable="chrome.exe")
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _: True)
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_zoom_out_map_pane", lambda *_: None)
    sleeps, clicks, keys, typed, sizes = [], [], [], [], []
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(nyc_portal, "_click_text", lambda _, rows, text: clicks.append(text) or rows[0])
    monkeypatch.setattr(nyc_portal, "_key", lambda _, key: keys.append(key))
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda _, address, rows, *_rest: typed.append(("click", address)))
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text", lambda address: typed.append(("focused", address)) or True)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _, path: _write_portal_screen(Path(path)))
    initial = {
        "select": [nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
        "address": [nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
        "previous": [nyc_portal.ScreenText("Find Another Property", (20, 100, 200, 130))],
    }[start]
    card = [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
            nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
            nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
            nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))]
    screens = iter([initial] + ([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))]] if start == "previous" else []) + [card])
    def ocr(path):
        sizes.append(Image.open(path).size)
        return next(screens)
    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", fast_mode=True, fast_pause_sec=2.5)
    assert (result.block, result.lot) == ("5712", "70")
    assert len(sizes) == (3 if start == "previous" else 2)
    assert sizes[0] == (500, 400) and sizes[-1] == (1000, 800)
    assert typed == [("click" if start == "address" else "focused", "87 Stafford Ave")]
    assert sleeps.count(2.5) == (2 if start == "address" else 4 if start == "select" else 5)
    assert ("Select" in clicks) == (start != "address")
    assert ("Find Another Property" in clicks) == (start == "previous")


def test_fast_driver_stops_before_intermediate_action_when_title_indicates_challenge(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(nyc_portal, "time", clock)
    window = nyc_portal.browser_save.BrowserWindow(handle=42, title="Verify you are human - Chrome", executable="chrome.exe")
    monkeypatch.setattr(nyc_portal.browser_save, "list_windows", lambda: [window])
    monkeypatch.setattr(nyc_portal.browser_save, "foreground", lambda _: pytest.fail("challenge must stop"))
    with pytest.raises(nyc_portal.PortalLookupError, match="CAPTCHA"):
        nyc_portal._fast_wait(42, 2.5)
    assert clock.sleeps == [2.5]


def test_fast_new_tab_waits_then_uses_only_two_required_ocr_calls(tmp_path, monkeypatch):
    from PIL import Image
    window = nyc_portal.browser_save.BrowserWindow(handle=42, title="New Tab - Chrome", executable="chrome.exe")
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _: True)
    monkeypatch.setattr(nyc_portal.browser_save, "active_window_title", lambda: "New Tab - Chrome")
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_t", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_l", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text", lambda _: True)
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_zoom_out_map_pane", lambda *_: None)
    monkeypatch.setattr(nyc_portal, "_click_text", lambda _, rows, text: rows[0])
    monkeypatch.setattr(nyc_portal, "_key", lambda *args: None)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _, path: _write_portal_screen(Path(path)))
    sleeps, sizes = [], []
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda value: sleeps.append(value))
    screens = iter([
        [nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))],
    ])
    def ocr(path):
        sizes.append(Image.open(path).size)
        return next(screens)
    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", fast_mode=True, fast_pause_sec=2)
    assert (result.block, result.lot) == ("5712", "70")
    assert sizes == [(650, 640), (1000, 800)]
    assert sleeps.count(2) == 5  # portal, select, address field, suggestions, card


def test_region_wait_uses_full_fallback_once_at_timeout(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(nyc_portal, "time", clock)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _, path: _write_portal_screen(Path(path)))
    regions, full = [], []
    monkeypatch.setattr(nyc_portal, "_screen_ocr_region", lambda *a: regions.append(True) or [])
    monkeypatch.setattr(nyc_portal, "_screen_ocr", lambda _: full.append(True) or [nyc_portal.ScreenText("Select", (750, 100, 800, 130))])
    result = nyc_portal._wait_screen(42, lambda rows: nyc_portal._contains(rows, "Select"), first_delay=1.2,
                                     description="форму", region=nyc_portal._FORM_REGION)
    assert nyc_portal._contains(result, "Select")
    assert len(regions) == 1 and len(full) == 1


def test_fast_card_missing_data_stops_without_guess_or_vision(tmp_path, monkeypatch):
    window = nyc_portal.browser_save.BrowserWindow(handle=42, title="Property Information Portal - Chrome", executable="chrome.exe")
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _: True)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _, path: _write_portal_screen(Path(path)))
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _: None)
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda *_: None)
    monkeypatch.setattr(nyc_portal, "_key", lambda *_: None)
    screens = iter([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
                    [nyc_portal.ScreenText("loading", (20, 200, 120, 220))],
                    [nyc_portal.ScreenText("loading", (20, 200, 120, 220))]])
    monkeypatch.setattr(nyc_portal, "_screen_ocr", lambda _: next(screens))
    monkeypatch.setattr(nyc_portal, "ask_llm_json", lambda *a, **kw: pytest.fail("не отправлять карту при отсутствии Block/Lot"))
    with pytest.raises(nyc_portal.PortalLookupError, match="Увеличьте паузу"):
        nyc_portal.lookup_parcel("87 Stafford Ave", tmp_path, cfg={"api": {}}, provider="hydra", model="test", fast_mode=True)


def test_suggestion_row_ignores_map_numbers_and_typed_text():
    number_on_map = nyc_portal.ScreenText("70", (420, 250, 445, 275))
    typed = nyc_portal.ScreenText("417 WEST 33RD STREET", (43, 249, 203, 264))
    suggestion = nyc_portal.ScreenText("417 W 33 ST, MANHATTAN, NY, 10001, USA", (16, 281, 298, 295))
    assert nyc_portal._suggestion_row([number_on_map, typed, suggestion]) == suggestion
    assert nyc_portal._suggestion_row([number_on_map, typed]) is None


def test_warmup_initializes_in_background_and_shares_result(monkeypatch):
    import threading
    started, release = threading.Event(), threading.Event()
    engine = object()
    def load():
        started.set()
        assert release.wait(2)
        return engine
    monkeypatch.setattr(nyc_portal.ocr_pages, "_engine_or_raise", load)
    future = _real_start_ocr_warmup()
    try:
        assert started.wait(1)
        assert not future.done()
    finally:
        release.set()
    assert future.result(timeout=2) is engine


def test_ocr_warmup_failure_reported_before_first_screenshot(tmp_path, monkeypatch):
    from concurrent.futures import Future
    future = Future()
    future.set_exception(nyc_portal.ocr_pages.OcrUnavailable("ошибка модели"))
    monkeypatch.setattr(nyc_portal, "_start_ocr_warmup", lambda: future)
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: nyc_portal.browser_save.BrowserWindow(
        42, "Property Information Portal - Chrome", "chrome.exe"))
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _: True)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda *_: pytest.fail("не снимать до готовности OCR"))
    with pytest.raises(nyc_portal.PortalLookupError, match="подготовить локальный OCR"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png")


def test_ready_callback_waits_only_after_first_browser_pause(monkeypatch):
    clock, seen = _wait_setup(monkeypatch, [[nyc_portal.ScreenText("Select", (0, 0, 10, 10))]])
    ready_at = []
    nyc_portal._wait_screen(42, lambda rows: bool(rows), description="форму", first_delay=3,
                            ready=lambda: ready_at.append(clock.now))
    assert ready_at == [3] and len(seen) == 1


def test_reliable_reads_card_left_from_final_capture_and_keeps_map_crop_anchors(tmp_path, monkeypatch):
    from PIL import Image
    window = nyc_portal.browser_save.BrowserWindow(handle=42, title="Property Information Portal - Chrome", executable="chrome.exe")
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal, "_click_row", lambda _h, row: row)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _: True)
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda *_: None)
    monkeypatch.setattr(nyc_portal, "_key", lambda *_: None)
    monkeypatch.setattr(nyc_portal, "_zoom_out_map_pane", lambda *_: None)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _, path: _write_portal_screen(Path(path)))
    sleeps, sizes = [], []
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda n: sleeps.append(n))
    screens = iter([
        [nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
        [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block", (20, 300, 70, 320)),
         nyc_portal.ScreenText("Lot", (300, 300, 390, 320))],
        [nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 320)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 320))],
    ])
    def ocr(path):
        sizes.append(Image.open(path).size)
        return next(screens)
    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png")
    assert (result.block, result.lot) == ("5712", "70")
    assert sizes == [(500, 400), (650, 640), (1000, 800), (500, 800)]
    assert sleeps == [1.0, 5.0, 6.0]
    assert Image.open(result.map_image).getpixel((0, 0)) == (0, 0, 255)


def test_reliable_unreadable_bbl_asks_vision_once_and_stops_if_unusable(tmp_path, monkeypatch):
    window = nyc_portal.browser_save.BrowserWindow(handle=42, title="Property Information Portal - Chrome", executable="chrome.exe")
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal, "_click_row", lambda _h, row: row)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _: True)
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda *_: None)
    monkeypatch.setattr(nyc_portal, "_key", lambda *_: None)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _, path: _write_portal_screen(Path(path)))
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _: None)
    screens = iter([
        [nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
        [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot", (300, 300, 400, 330))],
        [nyc_portal.ScreenText("Block", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot", (300, 300, 400, 330))],
        [nyc_portal.ScreenText("Block", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot", (300, 300, 400, 330))],
    ])
    monkeypatch.setattr(nyc_portal, "_screen_ocr", lambda _: next(screens))
    calls = []
    def ask(*args, **kw):
        calls.append((args, kw))
        return {"block": None, "lot": None, "borough": None}, nyc_portal.TokenUsage()
    monkeypatch.setattr(nyc_portal, "ask_llm_json", ask)
    with pytest.raises(nyc_portal.PortalLookupError, match="не смог уверенно прочитать"):
        nyc_portal.lookup_parcel("87 Stafford Ave", tmp_path, cfg={"api": {}}, provider="hydra", model="test")

    assert len(calls) == 1 and calls[0][1]["single_request"] is True


def test_driver_prewarms_ocr_in_parallel_with_browser_preparation(tmp_path, monkeypatch):
    import threading
    started, release, browser_seen, reached_capture = (threading.Event() for _ in range(4))
    def load():
        started.set()
        if not release.wait(2):
            raise RuntimeError("OCR warmup was never released")
        return object()
    monkeypatch.setattr(nyc_portal.ocr_pages, "_engine_or_raise", load)
    monkeypatch.setattr(nyc_portal, "_start_ocr_warmup", _real_start_ocr_warmup)
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    def browser():
        browser_seen.set()
        return nyc_portal.browser_save.BrowserWindow(42, "Property Information Portal - Chrome", "chrome.exe")
    monkeypatch.setattr(nyc_portal, "_portal_window", browser)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _: True)
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _: None)
    def capture(_):
        reached_capture.set()
        raise nyc_portal.PortalLookupError("тестовая остановка")
    monkeypatch.setattr(nyc_portal, "_initial_screen", capture)
    errors = []
    def run():
        try:
            nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png")
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert started.wait(1) and browser_seen.wait(1)
        assert not reached_capture.is_set()  # настройки браузера шли параллельно загрузке
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive() and reached_capture.is_set()
    assert len(errors) == 1 and "тестовая остановка" in str(errors[0])


def test_reliable_missing_borough_uses_vision_on_same_left_half(tmp_path, monkeypatch):
    from PIL import Image
    window = nyc_portal.browser_save.BrowserWindow(handle=42, title="Property Information Portal - Chrome", executable="chrome.exe")
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal, "_click_row", lambda _h, row: row)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _: True)
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda *_: None)
    monkeypatch.setattr(nyc_portal, "_key", lambda *_: None)
    monkeypatch.setattr(nyc_portal, "_zoom_out_map_pane", lambda *_: None)
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _: None)
    captures = []
    def capture(_, path):
        captures.append(True)
        _write_portal_screen(Path(path))
    monkeypatch.setattr(nyc_portal, "_save_window_capture", capture)
    sizes = []
    screens = iter([
        [nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
        [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 320)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 320))],
        [nyc_portal.ScreenText("Block 5712", (20, 300, 150, 320)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 320))],
        [nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 320)),
         nyc_portal.ScreenText("Lot 70", (300, 300, 400, 320))],
    ])
    def ocr(path):
        sizes.append(Image.open(path).size)
        return next(screens)
    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    seen = []
    def fallback(path):
        seen.append((Image.open(path).size, len(captures)))
        return "5712", "70", "Staten Island"
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", details_vision=fallback)
    assert (result.block, result.lot, result.borough) == ("5712", "70", "Staten Island")
    assert sizes[-1] == (500, 800)
    assert seen == [((500, 800), 4)]
    assert result.details_source == "vision"
    assert len(captures) == 4  # форма, подсказка, карточка, финальная карта/карточка


def test_reliable_suggestion_takes_first_screen_with_a_list(monkeypatch):
    """Сверки текста нет: первый же снимок с выпавшим списком — и выбираем."""
    label = [nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))]
    first = nyc_portal.ScreenText("88 Stafford Avenue, Staten Island, NY, USA", (20, 250, 260, 270))
    clock = _FakeClock()
    monkeypatch.setattr(nyc_portal, "time", clock)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    captures = []
    def capture(_, path):
        captures.append(True)
        Path(path).write_bytes(str(len(captures)).encode())
    monkeypatch.setattr(nyc_portal, "_save_window_capture", capture)
    monkeypatch.setattr(nyc_portal, "_screen_ocr_region", lambda *_: [first])
    picked = nyc_portal._wait_after_address_input(42, "87 Stafford Ave", label)
    assert picked == first
    assert len(captures) == 1 and clock.sleeps == [5.0]


def test_reliable_initial_screen_failure_retries_once_but_challenge_does_not(tmp_path, monkeypatch):
    window = nyc_portal.browser_save.BrowserWindow(handle=42, title="Property Information Portal - Chrome", executable="chrome.exe")
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _: True)
    waits = []
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda n: waits.append(n))
    calls = []
    def missing(_):
        calls.append(True)
        raise nyc_portal._InitialScreenNotFound("нет формы")
    monkeypatch.setattr(nyc_portal, "_initial_screen", missing)
    with pytest.raises(nyc_portal.PortalLookupError, match="нет формы"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png")
    assert len(calls) == 2 and waits == [1.0, 2.0]
    calls.clear()
    waits.clear()
    def challenge(_):
        calls.append(True)
        raise nyc_portal.PortalLookupError("CAPTCHA")
    monkeypatch.setattr(nyc_portal, "_initial_screen", challenge)
    with pytest.raises(nyc_portal.PortalLookupError, match="CAPTCHA"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png")
    assert len(calls) == 1 and waits == [1.0]


def _optimal_setup(monkeypatch, tmp_path):
    from PIL import Image
    window = nyc_portal.browser_save.BrowserWindow(42, "Property Information Portal - Chrome", "chrome.exe")
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _: True)
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_zoom_out_map_pane", lambda *_: None)
    clicks, keys, waits, captures, scans = [], [], [], [], []
    monkeypatch.setattr(nyc_portal, "_click_text", lambda _, rows, text: clicks.append(text) or rows[0])
    monkeypatch.setattr(nyc_portal, "_click_row", lambda _, row: clicks.append(row.text) or row)
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda _, address, rows, *_rest: clicks.append((address, rows[0].box)))
    monkeypatch.setattr(nyc_portal, "_key", lambda _, vk: keys.append(vk))
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda n: waits.append(n))
    def capture(_, path):
        captures.append(True)
        _write_portal_screen(Path(path))
    monkeypatch.setattr(nyc_portal, "_save_window_capture", capture)
    queue = []
    def ocr(path):
        scans.append(Image.open(path).size)
        return queue.pop(0)
    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    return queue, clicks, keys, waits, captures, scans


def _optimal_card():
    return [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
            nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
            nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
            nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))]


def test_reliable_teaches_cache_then_optimal_uses_smaller_ocr_and_verifies_suggestion(tmp_path, monkeypatch):
    q, clicks, keys, waits, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    label = nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))
    suggestion = nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))
    q.extend([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
              [nyc_portal.ScreenText("Address", (20, 130, 80, 150))], [label], [suggestion], _optimal_card(), _optimal_card()])
    reliable = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "one.png")
    assert reliable.block == "5712"
    assert nyc_portal.portal_layout_cache.load("chrome.exe", (1000, 800)) == label.box
    assert len(captures) == 6  # five checks and final map
    clicks.clear(); keys.clear(); waits.clear(); captures.clear(); scans.clear()
    # The address label is offset into the cached crop whose origin is (0, 80).
    q.extend([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
              [nyc_portal.ScreenText("Enter an Address", (20, 80, 250, 100))],
              [suggestion], _optimal_card()])
    logs = []
    optimal = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "two.png", logs.append, optimal_mode=True)
    assert (optimal.block, optimal.lot, optimal.borough) == ("5712", "70", "Staten Island")
    assert clicks == ["Select", ("87 Stafford Ave", label.box),
                      "87 Stafford Avenue, Staten Island, NY, USA"]
    assert scans == [(500, 400), (350, 200), (650, 640), (1000, 800)]
    assert len(captures) == 5 and waits == [1., .7, .2, .2, .2, 1., .2, 1.2, 2.]
    assert any("использую сохранённую" in msg for msg in logs)
    assert not q


def test_optimised_stale_layout_falls_back_to_full_same_snapshot(tmp_path, monkeypatch):
    q, clicks, keys, waits, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    assert nyc_portal.portal_layout_cache.save("chrome.exe", (1000, 800), (20, 160, 250, 180))
    q.extend([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))], [],
              [nyc_portal.ScreenText("Enter an Address", (100, 400, 400, 420))],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (100, 490, 300, 510))], _optimal_card(), _optimal_card()])
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", optimal_mode=True)
    assert result.block == "5712" and ("87 Stafford Ave", (100, 400, 400, 420)) in clicks
    assert scans[:3] == [(500, 400), (350, 200), (1000, 800)]
    assert len(captures) == 6  # one retry, full OCR reuses it
    assert not q


def test_optimal_missing_or_other_browser_cache_runs_reliable_and_learns(tmp_path, monkeypatch):
    q, clicks, keys, waits, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    assert nyc_portal.portal_layout_cache.save("msedge.exe", (1000, 800), (20, 160, 250, 180))
    q.extend([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
              [nyc_portal.ScreenText("Address", (20, 130, 80, 150))],
              [nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))], _optimal_card(), _optimal_card()])
    logs = []
    nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", logs.append, optimal_mode=True)
    assert len(captures) == 6 and scans[1] == (650, 640)
    assert nyc_portal.portal_layout_cache.load("chrome.exe", (1000, 800)) == (20, 160, 250, 180)
    assert any("надёжный поиск" in line for line in logs)


def test_optimised_stops_only_when_no_suggestion_list_at_all(tmp_path, monkeypatch):
    """Проверки текста нет; стоп — только если список подсказок так и не выпал
    даже после повторного ввода адреса (восстановление, а не пассивное ожидание)."""
    q, clicks, keys, waits, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    assert nyc_portal.portal_layout_cache.save("chrome.exe", (1000, 800), (20, 160, 250, 180))
    empty = [nyc_portal.ScreenText("No suggestions", (20, 250, 200, 270))]
    q.extend([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
              [nyc_portal.ScreenText("Enter an Address", (20, 80, 250, 100))],
              # Первое ожидание, повторный ввод, второе ожидание — списка
              # нет ни разу (экранов с запасом: часть проверок ест полнокадровый
              # повторный OCR того же снимка).
              empty, empty, empty, empty, empty, empty,
              empty, empty, empty, empty, empty, empty])
    with pytest.raises(nyc_portal.PortalLookupError, match="список подсказок"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", optimal_mode=True)
    assert keys.count(nyc_portal.browser_save.VK_RETURN) == 1  # only menu, never select address
    assert sum(1 for click in clicks if isinstance(click, tuple)) == 2  # адрес вводился дважды
    assert not (tmp_path / "map.png").exists()


def test_optimal_stops_if_window_size_changes_before_address_typing(tmp_path, monkeypatch):
    q, clicks, keys, waits, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    assert nyc_portal.portal_layout_cache.save("chrome.exe", (1000, 800), (20, 160, 250, 180))
    bounds = iter([(0, 0, 1000, 800), (0, 0, 1000, 800), (0, 0, 1000, 800), (0, 0, 1100, 800)])
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _: next(bounds))
    q.extend([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
              [nyc_portal.ScreenText("Enter an Address", (20, 80, 250, 100))]])
    with pytest.raises(nyc_portal.PortalLookupError, match="Размер окна изменился"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", optimal_mode=True)
    assert all(not isinstance(c, tuple) for c in clicks)


def test_modes_cannot_both_be_enabled_before_touching_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: pytest.fail("browser touched"))
    with pytest.raises(nyc_portal.PortalLookupError, match="только один режим"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", optimal_mode=True, fast_mode=True)


def test_no_coordinate_learning_after_failed_suggestion(tmp_path, monkeypatch):
    q, _, _, _, _, _ = _optimal_setup(monkeypatch, tmp_path)
    wrong = [nyc_portal.ScreenText("No suggestions", (20, 250, 200, 270))]
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              # Ожидание подсказок, повторный ввод и второе ожидание —
              # списка как не было, так и нет (экранов с запасом).
              wrong, wrong, wrong, wrong, wrong, wrong,
              wrong, wrong, wrong, wrong, wrong, wrong])
    with pytest.raises(nyc_portal.PortalLookupError):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png")
    assert not nyc_portal.portal_layout_cache.cache_path().exists()


def test_fast_mode_never_teaches_cache(tmp_path, monkeypatch):
    q, _, _, _, _, _ = _optimal_setup(monkeypatch, tmp_path)
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))], _optimal_card()])
    nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", fast_mode=True)
    assert not nyc_portal.portal_layout_cache.cache_path().exists()


def test_lookup_passes_optimised_flag_to_driver(tmp_path, monkeypatch):
    received = {}
    def drive(address, map_path, progress=None, **options):
        received.update(options)
        raise nyc_portal.PortalLookupError("стоп")
    monkeypatch.setattr(nyc_portal, "_drive_portal", drive)
    with pytest.raises(nyc_portal.PortalLookupError, match="стоп"):
        nyc_portal.lookup_parcel("87 Stafford Ave", tmp_path, cfg={}, provider="x", model="y", optimal_mode=True)
    assert received["optimal_mode"] is True and received["fast_mode"] is False


def test_fast_uses_configured_pause_for_map_redraw_and_short_key_delays(tmp_path, monkeypatch):
    q, _, keys, waits, captures, _ = _optimal_setup(monkeypatch, tmp_path)
    zooms = []
    monkeypatch.setattr(nyc_portal, "_zoom_out_map_pane", lambda *args: zooms.append(args))
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text", lambda _: True)
    q.extend([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))], _optimal_card()])
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", fast_mode=True, fast_pause_sec=1.5)
    assert (result.block, result.lot) == ("5712", "70")
    assert zooms == [(42, 1.5)]
    assert len(captures) == 3  # начальная форма, карточка, итоговая карта
    assert waits == [1.5, .05, .05, .05, 1.5, .05, 1.5, .05, 1.5]
    assert len(keys) == 7


@pytest.mark.parametrize("pause", [0, 0.5, 5.1, -1, float("nan"), "bad"])
def test_fast_rejects_invalid_pause_before_touching_browser(tmp_path, monkeypatch, pause):
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: pytest.fail("не трогать браузер"))
    with pytest.raises(nyc_portal.PortalLookupError, match="от 1 до 5"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", fast_mode=True, fast_pause_sec=pause)


def test_fast_card_missing_borough_stops_before_map_or_vision(tmp_path, monkeypatch):
    q, _, _, _, captures, _ = _optimal_setup(monkeypatch, tmp_path)
    card = _optimal_card()
    card[1] = nyc_portal.ScreenText("Borough", (20, 260, 180, 280))
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))], card, card])
    monkeypatch.setattr(nyc_portal, "ask_llm_json", lambda *a, **kw: pytest.fail("Vision до чтения Borough недопустим"))
    with pytest.raises(nyc_portal.PortalLookupError, match="Borough"):
        nyc_portal.lookup_parcel("87 Stafford Ave", tmp_path, cfg={}, provider="x", model="y", fast_mode=True)
    # +1 снимок — «чёрный ящик» сбоя (PORTAL_FAIL_*.png), карты/Vision нет.
    assert len(captures) == 4 and not list(tmp_path.glob("TAX_MAP_*"))


def test_optimised_updates_verified_cache_then_fast_uses_it_without_teaching(tmp_path, monkeypatch):
    q, clicks, _, _, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    old_box = (20, 160, 250, 180)
    new_box = (40, 165, 260, 185)
    assert nyc_portal.portal_layout_cache.save("chrome.exe", (1000, 800), old_box)
    q.extend([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
              [nyc_portal.ScreenText("Enter an Address", (40, 85, 260, 105))],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (40, 260, 200, 280))], _optimal_card()])
    nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "optimal.png", optimal_mode=True)
    assert nyc_portal.portal_layout_cache.load("chrome.exe", (1000, 800)) == new_box
    initial_data = nyc_portal.portal_layout_cache.cache_path().read_bytes()
    scans.clear(); captures.clear(); clicks.clear()
    # OCR current text in the cache-derived crop (y0 = 85), never a blind cached click.
    q.extend([[], [nyc_portal.ScreenText("Enter an Address", (42, 82, 262, 102))], _optimal_card()])
    logs = []
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "fast.png", logs.append, fast_mode=True)
    assert result.block == "5712" and scans[:2] == [(500, 400), (360, 200)]
    assert clicks == [("87 Stafford Ave", (42, 167, 262, 187))]
    assert len(captures) == 3  # начальная форма, финальная карточка, карта
    assert any("общего кэша" in line for line in logs)
    assert nyc_portal.portal_layout_cache.cache_path().read_bytes() == initial_data


def test_fast_cache_miss_rechecks_full_initial_form_on_same_screenshot(tmp_path, monkeypatch):
    q, clicks, _, _, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    assert nyc_portal.portal_layout_cache.save("chrome.exe", (1000, 800), (20, 160, 250, 180))
    monkeypatch.setattr(nyc_portal, "_type_focused_address", lambda _, a: clicks.append(("typed", a)))
    # Select from the cached *partial* region must not be trusted: look at the usual quarter.
    q.extend([[], [nyc_portal.ScreenText("Select", (20, 20, 80, 40))],
              [nyc_portal.ScreenText("Select", (20, 100, 80, 130))], _optimal_card()])
    nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", fast_mode=True)
    assert scans == [(500, 400), (350, 200), (1000, 800), (1000, 800)]
    assert len(captures) == 3 and clicks == ["Select", ("typed", "87 Stafford Ave")]


def test_stale_optimal_region_switches_remaining_actions_to_reliable_and_refreshes_cache(tmp_path, monkeypatch):
    q, _, _, waits, captures, _ = _optimal_setup(monkeypatch, tmp_path)
    old = (20, 160, 250, 180)
    new = (100, 400, 400, 420)
    assert nyc_portal.portal_layout_cache.save("chrome.exe", (1000, 800), old)
    q.extend([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))], [],
              [nyc_portal.ScreenText("Enter an Address", new)],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (100, 490, 300, 510))], _optimal_card(), _optimal_card()])
    logs = []
    nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", logs.append, optimal_mode=True)
    assert any("оставшиеся действия" in line and "надёжному" in line for line in logs)
    assert waits[-3:] == [0.2, 5.0, 6.0]  # longer reliable waits for suggestion and card
    assert len(captures) == 6
    assert nyc_portal.portal_layout_cache.load("chrome.exe", (1000, 800)) == new


@pytest.mark.parametrize("first", ["not_loaded", "unreadable"])
def test_fast_card_failure_uses_one_reliable_retry_without_retyping(tmp_path, monkeypatch, first):
    q, clicks, keys, waits, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    initial = [nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))]
    bad_card = ([nyc_portal.ScreenText("loading", (10, 100, 100, 120))] if first == "not_loaded"
                else [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
                      nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
                      nyc_portal.ScreenText("Block", (20, 300, 150, 330)),
                      nyc_portal.ScreenText("Lot", (300, 300, 400, 330))])
    q.extend([initial, bad_card, _optimal_card()])
    logs = []
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", logs.append, fast_mode=True, fast_pause_sec=1.5)
    assert (result.block, result.lot) == ("5712", "70")
    assert len(captures) == 4 and scans == [(500, 400), (1000, 800), (1000, 800)]
    assert clicks == [("87 Stafford Ave", initial[0].box)]
    assert keys == [nyc_portal.browser_save.VK_DOWN, nyc_portal.browser_save.VK_RETURN]
    assert waits == [1.5, .05, 1.5, 2.0]
    assert any("остаётся непроверенной" in line for line in logs)
    assert not nyc_portal.portal_layout_cache.cache_path().exists()


def test_fast_card_detected_challenge_never_retries_or_sends_vision(tmp_path, monkeypatch):
    q, _, _, _, captures, _ = _optimal_setup(monkeypatch, tmp_path)
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("verify you are human", (20, 160, 250, 180))]])
    monkeypatch.setattr(nyc_portal, "ask_llm_json", lambda *a, **kw: pytest.fail("CAPTCHA"))
    with pytest.raises(nyc_portal.PortalLookupError, match="CAPTCHA"):
        nyc_portal.lookup_parcel("87 Stafford Ave", tmp_path, cfg={}, provider="x", model="y", fast_mode=True)
    assert len(captures) == 3  # +1 — снимок «чёрного ящика»; Vision не вызывался


def test_fast_retry_challenge_stops_immediately_without_more_captures(tmp_path, monkeypatch):
    q, _, _, _, captures, _ = _optimal_setup(monkeypatch, tmp_path)
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("loading", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("captcha", (20, 160, 250, 180))]])
    with pytest.raises(nyc_portal.PortalLookupError, match="CAPTCHA"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", fast_mode=True)
    # +1 снимок — пассивный «чёрный ящик» после стопа; проверок/OCR больше нет.
    assert len(captures) == 4 and not (tmp_path / "map.png").exists()


def test_stale_optimal_cache_does_not_cause_blind_click_if_no_field(tmp_path, monkeypatch):
    q, clicks, keys, _, captures, _ = _optimal_setup(monkeypatch, tmp_path)
    old = (20, 160, 250, 180)
    assert nyc_portal.portal_layout_cache.save("chrome.exe", (1000, 800), old)
    q.extend([[nyc_portal.ScreenText("Select", (20, 100, 80, 130))], [], []])
    with pytest.raises(nyc_portal.PortalLookupError, match="поля Address"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", optimal_mode=True)
    assert clicks == ["Select"]  # no click in an unverified address field
    assert len(captures) == 4 and keys.count(nyc_portal.browser_save.VK_RETURN) == 1
    assert nyc_portal.portal_layout_cache.load("chrome.exe", (1000, 800)) == old


def test_reliable_final_snapshot_saves_right_first_then_reads_left_locally(tmp_path, monkeypatch):
    from PIL import Image
    q, _, _, waits, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    map_path = tmp_path / "map.png"
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))],
              [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115))],
              [nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
               nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
               nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))]])
    original = nyc_portal._screen_ocr
    def scan(path):
        if Path(path).name == "card_left.png":
            assert map_path.exists()  # карта уже выделена из того же кадра
            assert Image.open(path).size == (500, 800)
            assert Image.open(path).getpixel((10, 100)) == (255, 0, 0)
            assert Image.open(map_path).getpixel((0, 0)) == (0, 0, 255)
        return original(path)
    monkeypatch.setattr(nyc_portal, "_screen_ocr", scan)
    result = nyc_portal._drive_portal("87 Stafford Ave", map_path,
                                      details_vision=lambda _: pytest.fail("OCR справился"))
    assert result.details_source == "ocr" and (result.block, result.lot) == ("5712", "70")
    assert len(captures) == 4 and scans == [(500, 400), (650, 640), (1000, 800), (500, 800)]
    assert waits == [1., 5., 6.]


def test_reliable_vision_fallback_sends_only_left_once_and_separately_sends_map(tmp_path, monkeypatch):
    from PIL import Image
    q, _, _, _, captures, _ = _optimal_setup(monkeypatch, tmp_path)
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))],
              [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115))],
              [nyc_portal.ScreenText("Block", (20, 300, 150, 330))]])
    images, calls = [], []
    def encode(paths, **kwargs):
        path = Path(paths[0])
        with Image.open(path) as image:
            images.append((path.name, image.size, image.getpixel((0, min(100, image.height - 1)))))
        if path.name == "card_left.png":
            assert list(tmp_path.glob("TAX_MAP_*")), "сначала сохраняется карта"
        return ["data:image/jpeg;base64,AA=="], []
    def ask(system, user_text, cfg, **kw):
        calls.append((system, user_text, cfg, kw))
        if len(calls) == 1:
            return {"borough": "Staten Island", "block": "5,712", "lot": "70"}, nyc_portal.TokenUsage(prompt_tokens=10, total_tokens=10)
        return {"left_neighbor_lot": "68", "right_neighbor_lot": "72"}, nyc_portal.TokenUsage(prompt_tokens=20, total_tokens=20)
    monkeypatch.setattr(nyc_portal, "render_image_files_base64", encode)
    monkeypatch.setattr(nyc_portal, "ask_llm_json", ask)
    logs = []
    result = nyc_portal.lookup_parcel("87 Stafford Ave", tmp_path, cfg={"api": {"max_retries": 8}},
                                       provider="hydra", model="vision", progress=logs.append)
    assert (result.borough, result.block, result.lot, result.details_source) == ("Staten Island", "5712", "70", "vision")
    assert result.details_usage.prompt_tokens == 10 and result.usage.prompt_tokens == 20
    assert len(calls) == 2 and len(captures) == 4
    assert all(call[2]["api"]["max_retries"] == 0 and call[3]["attempts"] == 1 and call[3]["single_request"] for call in calls)
    assert images[0] == ("card_left.png", (500, 800), (255, 0, 0))
    assert images[1][0].startswith("TAX_MAP_") and images[1][2] == (0, 0, 255)
    assert "87 Stafford" not in calls[0][1] and "87 Stafford" in calls[1][1]
    assert any("персональные данные" in line for line in logs)
    assert not list(tmp_path.rglob("card_left.png"))


@pytest.mark.parametrize("data", [
    {"borough": "Staten Island", "block": None, "lot": "70"},
    {"borough": "Queens", "block": "5712", "lot": "7?"},
    {"borough": "unknown", "block": "5712", "lot": "70"},
    {"borough": "Staten Island", "block": "57x12", "lot": "70"},
])
def test_vision_details_rejects_unclear_or_non_numeric_values(data):
    with pytest.raises(nyc_portal.PortalLookupError):
        nyc_portal._parse_vision_details(data)


def test_reliable_card_ocr_exception_uses_vision_once(tmp_path, monkeypatch):
    q, _, _, _, captures, _ = _optimal_setup(monkeypatch, tmp_path)
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))],
              [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115))]])
    original = nyc_portal._screen_ocr
    def ocr(path):
        if Path(path).name == "card_left.png":
            raise RuntimeError("OCR failure")
        return original(path)
    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    calls = []
    def fallback(path):
        calls.append(path)
        return "5712", "70", "Staten Island"
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", details_vision=fallback)
    assert result.details_source == "vision" and len(calls) == 1 and len(captures) == 4


def test_reliable_detected_challenge_on_left_never_calls_vision(tmp_path, monkeypatch):
    q, _, _, _, captures, _ = _optimal_setup(monkeypatch, tmp_path)
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))],
              [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115))],
              [nyc_portal.ScreenText("captcha", (20, 200, 150, 220))]])
    with pytest.raises(nyc_portal.PortalLookupError, match="CAPTCHA"):
        nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png",
                                 details_vision=lambda _: pytest.fail("CAPTCHA не отправлять"))
    assert len(captures) == 5  # +1 — снимок «чёрного ящика»; Vision не вызывался


def test_unrecognized_local_borough_requests_vision(tmp_path, monkeypatch):
    q, _, _, _, _, _ = _optimal_setup(monkeypatch, tmp_path)
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))],
              [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115))],
              [nyc_portal.ScreenText("Borough Sttaten lsIand", (20, 260, 180, 280)),
               nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
               nyc_portal.ScreenText("Lot 70", (300, 300, 400, 330))]])
    calls = []
    def read(path):
        calls.append(path)
        return "5712", "70", "Staten Island"
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", details_vision=read)
    assert result.details_source == "vision" and len(calls) == 1


def test_reliable_failed_vision_request_not_retried_or_followed_by_map_request(tmp_path, monkeypatch):
    q, _, _, _, _, _ = _optimal_setup(monkeypatch, tmp_path)
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))],
              [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115))], []])
    calls = []
    monkeypatch.setattr(nyc_portal, "render_image_files_base64", lambda *a, **kw: (["image"], []))
    def fail(*args, **kw):
        calls.append(kw)
        raise RuntimeError("провайдер не ответил")
    monkeypatch.setattr(nyc_portal, "ask_llm_json", fail)
    with pytest.raises(RuntimeError, match="не ответил"):
        nyc_portal.lookup_parcel("87 Stafford Ave", tmp_path, cfg={}, provider="x", model="y")
    assert len(calls) == 1 and calls[0]["single_request"] and calls[0]["attempts"] == 1


def test_reliable_without_confirmed_card_does_not_send_screenshot(tmp_path, monkeypatch):
    q, _, _, _, captures, _ = _optimal_setup(monkeypatch, tmp_path)
    # Карточки нет ни после клика по подсказке, ни после запасных Down+Enter:
    # экраны — форма, подсказка, 2 проверки карточки, снимок формы (нет ни
    # Search, ни отказа), ещё 2 проверки карточки.
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))],
              [], [], [], [], []])
    monkeypatch.setattr(nyc_portal, "ask_llm_json", lambda *a, **kw: pytest.fail("страница не подтверждена"))
    with pytest.raises(nyc_portal.PortalLookupError, match="карточку объекта"):
        nyc_portal.lookup_parcel("87 Stafford Ave", tmp_path, cfg={}, provider="x", model="y")
    # +1 снимок — «чёрный ящик» сбоя; снимка карты (TAX_MAP_*) при этом нет.
    assert len(captures) == 8 and not list(tmp_path.glob("TAX_MAP_*"))
    assert list(tmp_path.glob("PORTAL_FAIL_*.png"))


def test_suggestion_row_on_live_screenshot_rows():
    """Живые строки OCR со скриншота 2026-11-23: подсказка узнаётся по тексту."""
    input_row = nyc_portal.ScreenText("417 WEST 33RD STREET", (43, 249, 203, 264))
    suggestion = nyc_portal.ScreenText("417 W 33 ST, MANHATTAN, NY, 10001, USA", (16, 281, 298, 295))
    brooklyn = nyc_portal.ScreenText("W 33 ST, BROOKLYN, NY, 10112, USA", (16, 311, 258, 326))
    map_3d = nyc_portal.ScreenText("3D", (635, 127, 658, 144))
    assert nyc_portal._suggestion_row([input_row, suggestion, brooklyn, map_3d]) == suggestion
    assert nyc_portal._suggestion_row([input_row, map_3d]) is None


def test_wait_takes_whatever_first_suggestion_shows(monkeypatch):
    label = [nyc_portal.ScreenText("Enter an Address", (10, 160, 300, 180))]
    suggestion = nyc_portal.ScreenText("417 W 33 ST, MANHATTAN, NY, 10001, USA", (20, 250, 300, 265))
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _: None)
    monkeypatch.setattr(nyc_portal, "_wait_screen", lambda *args, **kw: [suggestion])
    picked = nyc_portal._wait_after_address_input(42, "417 WEST 33RD STREET", label)
    assert picked == suggestion


def test_suggestion_wait_reports_what_ocr_saw(monkeypatch):
    label = [nyc_portal.ScreenText("Enter an Address", (10, 160, 300, 180))]
    seen = nyc_portal.ScreenText("417 W 33 St, MANHATTAN, NY, 10001, USA", (20, 250, 300, 265))
    clock_calls = {"n": 0}

    def fake_wait(*args, **kwargs):
        clock_calls["n"] += 1
        raise nyc_portal._ScreenNotFound("нет")

    monkeypatch.setattr(nyc_portal, "_wait_screen", fake_wait)
    monkeypatch.setattr(nyc_portal, "_suggestion_screen_hint",
                        lambda *a: " На снимке первая подсказка: «417 W 33 St, MANHATTAN, NY, 10001, USA»; жду «417 WEST 33RD STREET».")
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _: None)
    with pytest.raises(nyc_portal.PortalLookupError) as caught:
        nyc_portal._wait_after_address_input(42, "417 WEST 33RD STREET", label)
    assert clock_calls["n"] == 2  # два цикла ожидания, потом честная остановка
    assert "417 W 33 St" in str(caught.value)
    assert seen is not None
    assert seen is not None


def test_rejected_text_search_recovers_by_retyping_and_clicking(tmp_path, monkeypatch):
    """Живой случай 3868 (24.11.2026): портал отклонил поиск по тексту подписи
    подсказки («Address not found…», «No results») — ввод адреса заново и
    выбор подсказки кликом доводят до карточки."""
    q, clicks, keys, waits, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    suggestion = nyc_portal.ScreenText("59-11 FRESH POND RD, QUEENS, NY, 11378, USA", (20, 250, 420, 270))
    rejected = [nyc_portal.ScreenText("Address not found within the NYC limits", (20, 200, 420, 220)),
                nyc_portal.ScreenText("No results", (20, 250, 200, 270))]
    card = [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
            nyc_portal.ScreenText("Borough Queens", (20, 260, 180, 280)),
            nyc_portal.ScreenText("Block 2754", (20, 300, 150, 330)),
            nyc_portal.ScreenText("Lot 21", (300, 300, 400, 330))]
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [suggestion], rejected, rejected, [suggestion], card, card])
    logs = []
    result = nyc_portal._drive_portal("59-11 Fresh Pond Road, Queens", tmp_path / "map.png", logs.append)
    assert (result.borough, result.block, result.lot) == ("Queens", "2754", "21")
    typed = [c for c in clicks if isinstance(c, tuple)]
    assert len(typed) == 2  # адрес вводился дважды: до и после отказа портала
    row_clicks = [c for c in clicks if isinstance(c, str)]
    assert row_clicks == [suggestion.text, suggestion.text]
    assert any("отклонил поиск по тексту" in line for line in logs)
    assert not q


def test_search_button_is_clicked_when_card_does_not_follow_suggestion_click(tmp_path, monkeypatch):
    """Выбор подсказки кликом заполнил поле, но поиск сам не стартовал:
    нажимаем Search и только потом ждём карточку."""
    q, clicks, keys, waits, captures, scans = _optimal_setup(monkeypatch, tmp_path)
    suggestion = nyc_portal.ScreenText("87 Stafford Avenue, Staten Island, NY, USA", (20, 250, 200, 270))
    form = [nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180)),
            nyc_portal.ScreenText("Search", (560, 160, 610, 180))]
    q.extend([[nyc_portal.ScreenText("Enter an Address", (20, 160, 250, 180))],
              [suggestion], form, form, _optimal_card(), _optimal_card()])
    logs = []
    result = nyc_portal._drive_portal("87 Stafford Ave", tmp_path / "map.png", logs.append)
    assert (result.block, result.lot) == ("5712", "70")
    row_clicks = [c for c in clicks if isinstance(c, str)]
    assert row_clicks == [suggestion.text, "Search"]
    assert any("нажимаю Search" in line for line in logs)
    assert not q


def test_type_into_address_clicks_field_content_in_dirty_reused_tab(monkeypatch):
    """Живой сбой 3870 (24.11.2026): вкладка осталась после упавшего задания —
    плашка «Address not found», в поле старый адрес. Смещения от подписи
    уводят клик мимо поля; клик по самому содержимому поля попадает."""
    rows = [
        nyc_portal.ScreenText("Address (Enter an Address, Intersection or Common Place Name)",
                              (20, 100, 420, 120)),
        nyc_portal.ScreenText("Address not found within the NYC limits", (20, 130, 420, 150)),
        nyc_portal.ScreenText("59-11 FRESH POND RD, QUEENS, NY, 11378, USA", (25, 160, 400, 180)),
        nyc_portal.ScreenText("Terms of Use", (20, 300, 300, 320)),
    ]
    clicks = []
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _h: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_focus", lambda _h: None)
    monkeypatch.setattr(nyc_portal.browser_save, "click_browser_window",
                        lambda _h, x, y: clicks.append((x, y)) or True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text", lambda _t: True)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _h, p: p)
    screens = iter([
        [nyc_portal.ScreenText("59-11 FRESH POND RD, QUEENS, NY, 11378, USA", (25, 160, 400, 180))],
        [nyc_portal.ScreenText("52 East 81st Street, New York", (25, 160, 400, 180))],
    ])
    monkeypatch.setattr(nyc_portal, "_screen_ocr_region", lambda _p, _r: next(screens))
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    strategy = nyc_portal._type_into_address(1, "52 East 81st Street, New York", rows)
    assert strategy == "field_text"
    # Первый клик — под подпись (плашка/старый текст ещё на месте), второй —
    # точно в центр содержимого поля.
    assert clicks[-1] == ((25 + 400) // 2, (160 + 180) // 2)


def test_type_into_address_ignores_terms_disclaimer_for_field_text(monkeypatch):
    """Пустое поле перед Terms of Use: field_text не должен кликать в юридический текст под Terms of Use."""
    rows = [
        nyc_portal.ScreenText("Address (Enter an Address, Intersection or Common Place Name)",
                              (40, 250, 400, 266)),
        nyc_portal.ScreenText("Terms of Use:", (40, 333, 120, 349)),
        nyc_portal.ScreenText("The City of New York makes no warranties...", (40, 352, 580, 378)),
    ]
    clicks = []
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _h: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_focus", lambda _h: None)
    monkeypatch.setattr(nyc_portal.browser_save, "click_browser_window",
                        lambda _h, x, y: clicks.append((x, y)) or True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text", lambda _t: True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_key", lambda _k: True)
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _h, p: p)
    screens = iter([
        [nyc_portal.ScreenText("Terms of Use:", (40, 333, 120, 349))],  # label missed in mock
        [nyc_portal.ScreenText("18 Princewood Avenue, Staten Island", (45, 298, 350, 315))],
    ])
    monkeypatch.setattr(nyc_portal, "_screen_ocr_region", lambda _p, _r: next(screens))
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    strategy = nyc_portal._type_into_address(1, "18 Princewood Avenue, Staten Island", rows)
    assert strategy == "label_low"
    assert all(y < 333 for _, y in clicks)  # never clicked The City of New York disclaimer!


# ------------------------------------------- гонка моделей про соседей и ранний BBL


def _race_setup(monkeypatch, tmp_path, ask):
    from PIL import Image

    map_image = tmp_path / "NYC_Portal_map.png"
    Image.new("RGB", (600, 400), "blue").save(map_image)
    monkeypatch.setattr(
        nyc_portal, "_drive_portal",
        lambda *a, **k: nyc_portal.PortalCapture("5712", "70", "Staten Island", map_image))
    monkeypatch.setattr(
        nyc_portal, "render_image_files_base64",
        lambda paths, **kw: (["data:image/jpeg;base64,AA=="], []))
    monkeypatch.setattr(nyc_portal, "ask_llm_json", ask)
    return map_image


_RACE_CFG = {"api": {"providers": {"hydra": {"api_key": "hydra-key"}}}}


def test_neighbour_race_accepts_the_first_valid_answer(tmp_path, monkeypatch):
    import threading

    import llm_parser

    usage = nyc_portal.TokenUsage(prompt_tokens=10, total_tokens=10)
    release_main = threading.Event()
    main_started = threading.Event()
    seen_cfg = {}

    def ask(system, user_text, cfg, **kwargs):
        seen_cfg[kwargs["model"]] = cfg
        if kwargs["model"] == nyc_portal.NEIGHBOUR_RACE_MODEL:
            return {"left_neighbor_lot": "68", "right_neighbor_lot": "72",
                    "confidence": "high"}, usage
        main_started.set()
        release_main.wait(10)
        return {"left_neighbor_lot": "60", "right_neighbor_lot": "61",
                "confidence": "low"}, usage

    _race_setup(monkeypatch, tmp_path, ask)
    try:
        result = nyc_portal.lookup_parcel(
            "87 Stafford Ave", tmp_path, cfg=dict(_RACE_CFG),
            provider="hydra", model="main-model", api_key="hydra-key")
    finally:
        release_main.set()
    assert result.neighbours == ["68", "72"]
    assert result.neighbour_model.endswith(nyc_portal.NEIGHBOUR_RACE_MODEL)
    assert main_started.is_set()  # основная модель спрошена параллельно, а не после
    assert llm_parser.resolve_reasoning_effort(
        seen_cfg[nyc_portal.NEIGHBOUR_RACE_MODEL], provider="hydra",
        model=nyc_portal.NEIGHBOUR_RACE_MODEL) == "medium"
    # Тайм-аут портальных запросов — 180 с у обоих участников гонки,
    # а не общий api.timeout_sec основного разбора.
    assert seen_cfg["main-model"]["api"]["timeout_sec"] == 180
    assert seen_cfg[nyc_portal.NEIGHBOUR_RACE_MODEL]["api"]["timeout_sec"] == 180


def test_neighbour_race_falls_back_when_the_fast_model_fails(tmp_path, monkeypatch):
    usage = nyc_portal.TokenUsage(prompt_tokens=10, total_tokens=10)

    def ask(system, user_text, cfg, **kwargs):
        if kwargs["model"] == nyc_portal.NEIGHBOUR_RACE_MODEL:
            raise RuntimeError("hydra unavailable")
        return {"left_neighbor_lot": "68", "right_neighbor_lot": "72",
                "confidence": "high"}, usage

    _race_setup(monkeypatch, tmp_path, ask)
    result = nyc_portal.lookup_parcel(
        "87 Stafford Ave", tmp_path, cfg=dict(_RACE_CFG),
        provider="hydra", model="main-model", api_key="hydra-key")
    assert result.neighbours == ["68", "72"]
    assert result.neighbour_model.endswith("main-model")


def test_neighbour_race_reports_when_both_models_fail(tmp_path, monkeypatch):
    def ask(system, user_text, cfg, **kwargs):
        raise RuntimeError("down")

    _race_setup(monkeypatch, tmp_path, ask)
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    with pytest.raises(nyc_portal.PortalLookupError, match="Ни одна"):
        nyc_portal.lookup_parcel(
            "87 Stafford Ave", tmp_path, cfg=dict(_RACE_CFG),
            provider="hydra", model="main-model", api_key="hydra-key")


def test_neighbour_request_stays_single_without_a_hydra_key(tmp_path, monkeypatch):
    usage = nyc_portal.TokenUsage(prompt_tokens=10, total_tokens=10)
    calls = []

    def ask(system, user_text, cfg, **kwargs):
        calls.append(kwargs["model"])
        return {"left_neighbor_lot": "68", "right_neighbor_lot": "72",
                "confidence": "high"}, usage

    _race_setup(monkeypatch, tmp_path, ask)
    monkeypatch.delenv("HYDRA_API_KEY", raising=False)
    monkeypatch.setattr(nyc_portal, "load_dotenv_dict", lambda: {})
    result = nyc_portal.lookup_parcel(
        "87 Stafford Ave", tmp_path, cfg={"api": {}},
        provider="openrouter", model="some-model", api_key="other-key")
    assert calls == ["some-model"]
    assert result.neighbour_model == "openrouter: some-model"


def test_no_duplicate_request_when_selected_model_is_the_race_model(tmp_path, monkeypatch):
    usage = nyc_portal.TokenUsage(prompt_tokens=10, total_tokens=10)
    calls = []

    def ask(system, user_text, cfg, **kwargs):
        calls.append(kwargs["model"])
        return {"left_neighbor_lot": "68", "right_neighbor_lot": "72",
                "confidence": "high"}, usage

    _race_setup(monkeypatch, tmp_path, ask)
    result = nyc_portal.lookup_parcel(
        "87 Stafford Ave", tmp_path, cfg=dict(_RACE_CFG), provider="hydra",
        model=nyc_portal.NEIGHBOUR_RACE_MODEL, api_key="hydra-key")
    assert calls == [nyc_portal.NEIGHBOUR_RACE_MODEL]
    assert result.neighbours == ["68", "72"]


def test_on_identified_fires_before_the_neighbour_request(tmp_path, monkeypatch):
    usage = nyc_portal.TokenUsage(prompt_tokens=10, total_tokens=10)
    events = []

    def ask(system, user_text, cfg, **kwargs):
        events.append("ask")
        return {"left_neighbor_lot": "68", "right_neighbor_lot": "72",
                "confidence": "high"}, usage

    _race_setup(monkeypatch, tmp_path, ask)
    monkeypatch.delenv("HYDRA_API_KEY", raising=False)
    monkeypatch.setattr(nyc_portal, "load_dotenv_dict", lambda: {})
    result = nyc_portal.lookup_parcel(
        "87 Stafford Ave", tmp_path, cfg={"api": {}}, provider="openrouter",
        model="some-model", api_key="other-key",
        on_identified=lambda capture: events.append(
            ("identified", capture.block, capture.lot)))
    assert result.neighbours == ["68", "72"]
    assert events[0] == ("identified", "5712", "70")
    assert events[1] == "ask"


def test_vision_prompt_takes_left_right_relative_to_the_front_street():
    # Указание пользователя: левый/правый сосед — относительно фронтальной
    # улицы нашего участка, а не краёв картинки.
    prompt = nyc_portal.VISION_SYSTEM_PROMPT
    assert "RELATIVE TO THE FRONT STREET" in prompt
    assert "standing on that front street" in prompt
    assert "not to the edges of the image" in prompt
    assert "visual left as left_neighbor_lot" not in prompt


def test_neighbour_user_text_defines_sides_by_the_front_street(tmp_path, monkeypatch):
    usage = nyc_portal.TokenUsage(prompt_tokens=10, total_tokens=10)
    texts = []

    def ask(system, user_text, cfg, **kwargs):
        texts.append(user_text)
        return {"left_neighbor_lot": "68", "right_neighbor_lot": "72",
                "confidence": "high"}, usage

    _race_setup(monkeypatch, tmp_path, ask)
    monkeypatch.delenv("HYDRA_API_KEY", raising=False)
    monkeypatch.setattr(nyc_portal, "load_dotenv_dict", lambda: {})
    nyc_portal.lookup_parcel(
        "87 Stafford Ave", tmp_path, cfg={"api": {}}, provider="openrouter",
        model="some-model", api_key="other-key")
    assert "front street" in texts[0] and "left hand" in texts[0]


def test_neighbour_request_retries_after_a_timeout(tmp_path, monkeypatch):
    # Живой случай из очереди: «Request timed out» с первой попытки хоронил
    # всё задание. Теперь запрос повторяется и поиск живёт.
    usage = nyc_portal.TokenUsage(prompt_tokens=10, total_tokens=10)
    calls = []

    def ask(system, user_text, cfg, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise RuntimeError("Request timed out.")
        return {"left_neighbor_lot": "68", "right_neighbor_lot": "72",
                "confidence": "high"}, usage

    _race_setup(monkeypatch, tmp_path, ask)
    monkeypatch.delenv("HYDRA_API_KEY", raising=False)
    monkeypatch.setattr(nyc_portal, "load_dotenv_dict", lambda: {})
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    result = nyc_portal.lookup_parcel(
        "87 Stafford Ave", tmp_path, cfg={"api": {}}, provider="openrouter",
        model="some-model", api_key="other-key")
    assert result.neighbours == ["68", "72"]
    assert calls == ["some-model", "some-model"]


def test_neighbour_race_leg_retries_before_losing(tmp_path, monkeypatch):
    import threading

    usage = nyc_portal.TokenUsage(prompt_tokens=10, total_tokens=10)
    release_main = threading.Event()
    terra_calls = []

    def ask(system, user_text, cfg, **kwargs):
        if kwargs["model"] == nyc_portal.NEIGHBOUR_RACE_MODEL:
            terra_calls.append(1)
            if len(terra_calls) == 1:
                raise RuntimeError("Request timed out.")
            return {"left_neighbor_lot": "68", "right_neighbor_lot": "72",
                    "confidence": "high"}, usage
        release_main.wait(10)
        return {"left_neighbor_lot": "60", "right_neighbor_lot": "61",
                "confidence": "low"}, usage

    _race_setup(monkeypatch, tmp_path, ask)
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    try:
        result = nyc_portal.lookup_parcel(
            "87 Stafford Ave", tmp_path, cfg=dict(_RACE_CFG),
            provider="hydra", model="main-model", api_key="hydra-key")
    finally:
        release_main.set()
    assert result.neighbours == ["68", "72"]
    assert result.neighbour_model.endswith(nyc_portal.NEIGHBOUR_RACE_MODEL)
    assert len(terra_calls) == 2  # первая попытка упала, вторая выиграла гонку


def test_neighbour_request_gives_up_only_after_all_attempts(tmp_path, monkeypatch):
    calls = []

    def ask(system, user_text, cfg, **kwargs):
        calls.append(1)
        raise RuntimeError("Request timed out.")

    _race_setup(monkeypatch, tmp_path, ask)
    monkeypatch.delenv("HYDRA_API_KEY", raising=False)
    monkeypatch.setattr(nyc_portal, "load_dotenv_dict", lambda: {})
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    with pytest.raises(RuntimeError, match="timed out"):
        nyc_portal.lookup_parcel(
            "87 Stafford Ave", tmp_path, cfg={"api": {}}, provider="openrouter",
            model="some-model", api_key="other-key")
    assert len(calls) == nyc_portal.NEIGHBOUR_ATTEMPTS


def test_type_into_address_survives_failed_click_without_ctrl_a(monkeypatch):
    """Клик не состоялся (курсор не встал): Ctrl+A не жмём, пробуем следующий якорь."""
    rows = [
        nyc_portal.ScreenText("Address (Enter an Address, Intersection or Common Place Name)",
                              (20, 100, 420, 120)),
        nyc_portal.ScreenText("Enter an address", (25, 130, 300, 155)),
    ]
    clicks = []
    typed = []
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _h: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal, "_focus", lambda _h: None)
    monkeypatch.setattr(nyc_portal.browser_save, "click_browser_window",
                        lambda _h, x, y: clicks.append((x, y)) or len(clicks) > 1)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text",
                        lambda t: typed.append(t) or True)
    _stub_typed_visible(monkeypatch, [
        nyc_portal.ScreenText("1769 Stillwell Avenue, Brooklyn, NY", (25, 130, 300, 155))])
    strategy = nyc_portal._type_into_address(1, "1769 Stillwell Avenue, Brooklyn", rows)
    assert strategy == "placeholder" and len(clicks) == 2 and len(typed) == 1


def test_losing_neighbour_race_thread_stops_retrying_and_logging_after_winner(tmp_path, monkeypatch):
    import threading

    winner_done = threading.Event()
    loser_exited = threading.Event()
    calls = []
    messages = []

    def ask(system, user_text, cfg, **kwargs):
        model = kwargs["model"]
        calls.append(model)
        if model == nyc_portal.NEIGHBOUR_RACE_MODEL:
            return {"left_neighbor_lot": "51", "right_neighbor_lot": "53", "confidence": "high", "notes": ""}, None
        assert winner_done.wait(5)
        try:
            raise RuntimeError("Request timed out.")
        finally:
            loser_exited.set()

    _race_setup(monkeypatch, tmp_path, ask)
    monkeypatch.setenv("HYDRA_API_KEY", "hydra-secret")
    result = nyc_portal.lookup_parcel(
        "11 Mosel Loop", tmp_path, cfg={"api": {}}, provider="hydra",
        model="claude-haiku-5.5", api_key="hydra-secret", progress=messages.append)
    winner_done.set()
    assert loser_exited.wait(5)
    # Give the losing thread a moment to unwind its exception handler.
    for t in threading.enumerate():
        if t.name == "portal-neighbour-race":
            t.join(timeout=2)
    assert result.left_neighbour == "51" and result.right_neighbour == "53"
    assert calls.count("claude-haiku-5.5") == 1  # no retries 2 or 3 after terra won
    assert not any("повторяю — попытка" in line for line in messages)


def test_portal_window_respects_preferred_browser_and_errors_cleanly_if_absent(monkeypatch):
    chrome = nyc_portal.browser_save.BrowserWindow(
        10, "Property Information Portal - Chrome", "chrome.exe", r"C:\Chrome\chrome.exe",
    )
    edge = nyc_portal.browser_save.BrowserWindow(
        20, "New Tab - Microsoft Edge", "msedge.exe", r"C:\Edge\msedge.exe",
    )
    monkeypatch.setattr(nyc_portal.browser_save, "list_windows", lambda: [chrome, edge])
    monkeypatch.setattr(nyc_portal.browser_save, "active_window_handle", lambda: 10)
    prev = nyc_portal.browser_save.get_preferred_browser()
    try:
        nyc_portal.browser_save.set_preferred_browser("edge")
        assert nyc_portal._portal_window() == edge

        nyc_portal.browser_save.set_preferred_browser("firefox")
        with pytest.raises(nyc_portal.PortalLookupError, match="Mozilla Firefox"):
            nyc_portal._portal_window()
    finally:
        nyc_portal.browser_save.set_preferred_browser(prev)
        nyc_portal.browser_save.reset_auto_browser_pin()


def test_new_tab_polls_when_chrome_takes_longer_than_single_delay(tmp_path, monkeypatch):
    window = nyc_portal.browser_save.BrowserWindow(
        77, "Previous Page - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe",
    )
    titles = iter(("Previous Page - Google Chrome", "Previous Page - Google Chrome", "Новая вкладка - Google Chrome"))
    typed: list[str] = []
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _handle: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _handle: True)
    monkeypatch.setattr(nyc_portal.browser_save, "window_bounds", lambda _handle: (0, 0, 1000, 800))
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_t", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "active_window_title", lambda: next(titles, "Новая вкладка - Google Chrome"))
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_l", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_a", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "type_text", lambda text: typed.append(text) or True)
    monkeypatch.setattr(nyc_portal, "_key", lambda *_a: None)
    monkeypatch.setattr(nyc_portal, "_click_text", lambda _h, rows, _t: rows[0])
    monkeypatch.setattr(nyc_portal, "_click_row", lambda _h, row: row)
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda *_args: None)
    ocr_calls = iter([
        [nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
        [nyc_portal.ScreenText("Address", (20, 130, 80, 150))],
        [nyc_portal.ScreenText("Address (Enter an Address)", (20, 160, 550, 180))],
        [nyc_portal.ScreenText("859 Father Capodanno Boulevard, Staten Island, NY", (30, 255, 500, 275))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot 10", (300, 300, 400, 330))],
        [nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
         nyc_portal.ScreenText("Block 5712", (20, 300, 150, 330)),
         nyc_portal.ScreenText("Lot 10", (300, 300, 400, 330))],
    ])
    monkeypatch.setattr(nyc_portal, "_screen_ocr", lambda _path: next(ocr_calls))

    def capture(_handle, destination):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_portal_screen(path)
        return path

    monkeypatch.setattr(nyc_portal, "_save_window_capture", capture)

    capture_result = nyc_portal._drive_portal("859 Father Capodanno Boulevard", tmp_path / "map.png")
    assert capture_result.block == "5712" and capture_result.lot == "10"
    assert nyc_portal.PORTAL_URL in typed


def test_ctrl_t_failure_includes_actual_active_title(tmp_path, monkeypatch):
    window = nyc_portal.browser_save.BrowserWindow(
        77, "Some Tab - Google Chrome", "chrome.exe", r"C:\Chrome\chrome.exe",
    )
    monkeypatch.setattr(nyc_portal.browser_save, "available", lambda: True)
    monkeypatch.setattr(nyc_portal, "_portal_window", lambda: window)
    monkeypatch.setattr(nyc_portal, "_focus", lambda _handle: None)
    monkeypatch.setattr(nyc_portal.ocr_pages, "available", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "maximize_browser_window", lambda _handle: True)
    monkeypatch.setattr(nyc_portal.time, "sleep", lambda _s: None)
    monkeypatch.setattr(nyc_portal.browser_save, "press_ctrl_t", lambda: True)
    monkeypatch.setattr(nyc_portal.browser_save, "active_window_title", lambda: "Some Tab - Google Chrome")
    with pytest.raises(nyc_portal.PortalLookupError, match="some tab - google chrome"):
        nyc_portal._drive_portal("859 Father Capodanno", tmp_path / "parcel.png")


