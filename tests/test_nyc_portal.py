from __future__ import annotations

from pathlib import Path

import pytest

import nyc_portal


def _write_portal_screen(path):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1000, 800), "red")
    ImageDraw.Draw(image).rectangle((400, 84, 999, 799), fill="blue")
    image.save(path)


def test_parse_neighbour_result_keeps_only_numeric_map_labels():
    assert nyc_portal.parse_neighbour_result({
        "left_neighbor_lot": "68", "right_neighbor_lot": 72,
        "confidence": "high", "notes": "Labels visible.",
    }) == ("68", "72", "high", "Labels visible.")
    with pytest.raises(nyc_portal.PortalLookupError, match="нечисловое"):
        nyc_portal.parse_neighbour_result({"left_neighbor_lot": "6?", "right_neighbor_lot": 72})


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


def test_portal_driver_uses_current_window_and_address_suggestion_keys(tmp_path, monkeypatch):
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
        nyc_portal, "_key", lambda _handle, vk: keys.append(vk),
    )
    monkeypatch.setattr(
        nyc_portal, "_type_into_address", lambda _handle, address, _rows: clicks.append((42, address)),
    )
    ocr_calls = iter([
        # The first entry is consumed by the quick top-left-quarter portal check
        # that now runs before any Select hover/click.
        [nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
        [nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
        # Confirms the dropdown actually opened (its "Address" option row is now
        # visible) before Down/Down/Enter are sent — see the regression test for
        # why: sending them before the menu was open made it look like it opened
        # and instantly did nothing.
        [nyc_portal.ScreenText("Address", (20, 130, 80, 150))],
        [nyc_portal.ScreenText("Address (Enter an Address, Intersection or Common Place Name)",
                               (20, 160, 550, 180))],
        [nyc_portal.ScreenText("Unrelated page text; no address suggestion", (30, 210, 500, 230))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
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
    assert clicks == [(42, "Select"), (42, "87 Stafford Ave")]
    assert waits == [0.2, 0.2, 0.2, 0.2, 3.0, 0.2]
    assert keys == [
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_RETURN,
        nyc_portal.browser_save.VK_TAB,
        nyc_portal.browser_save.VK_DOWN,
        nyc_portal.browser_save.VK_RETURN,
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
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda *_args: None)
    ocr_calls = iter([
        [nyc_portal.ScreenText("Select", (20, 100, 80, 130))],
        # Confirms the dropdown actually opened before Down/Down/Enter are sent.
        [nyc_portal.ScreenText("Address", (20, 130, 80, 150))],
        [nyc_portal.ScreenText("Address (Enter an Address)", (20, 160, 550, 180))],
        [nyc_portal.ScreenText("Unrelated page text; no address suggestion", (30, 210, 500, 230))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
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
    assert waits == [0.6, 0.2, 0.2, 0.2, 0.2, 3.0, 0.2]  # 0.6 — ждём переключения на новую вкладку
    assert key_events[0] == nyc_portal.browser_save.VK_RETURN  # navigate current tab
    assert key_events[-2:] == [nyc_portal.browser_save.VK_DOWN, nyc_portal.browser_save.VK_RETURN]


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
    monkeypatch.setattr(nyc_portal, "_key", lambda _handle, vk: keys.append(vk))
    monkeypatch.setattr(nyc_portal, "_type_into_address", lambda *_args: None)
    screens = iter([
        # The first entry is consumed by the quick top-left-quarter portal check
        # that now runs before any Select hover/click.
        [nyc_portal.ScreenText("Select", (10, 10, 90, 30)),
         nyc_portal.ScreenText("Borough/Block", (10, 40, 200, 60)),
         nyc_portal.ScreenText("Address", (10, 70, 100, 90))],
        [nyc_portal.ScreenText("Select", (10, 10, 90, 30)),
         nyc_portal.ScreenText("Borough/Block", (10, 40, 200, 60)),
         nyc_portal.ScreenText("Address", (10, 70, 100, 90))],
        [nyc_portal.ScreenText("Address (Enter an Address)", (20, 160, 550, 180))],
        [nyc_portal.ScreenText("Unrelated page text; no address suggestion", (30, 210, 500, 230))],
        [nyc_portal.ScreenText("Find Another Property", (10, 90, 180, 115)),
         nyc_portal.ScreenText("Borough Staten Island", (20, 260, 180, 280)),
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
    assert waits == [0.2, 0.2, 0.2, 0.2, 3.0, 0.2]
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
    monkeypatch.setattr(nyc_portal, "_save_window_capture", lambda _h, path: Path(path).write_bytes(b"x"))

    def ocr(_path):
        seen.append(True)
        return screens[min(len(seen), len(screens)) - 1]

    monkeypatch.setattr(nyc_portal, "_screen_ocr", ocr)
    return clock, seen


def test_fast_mode_waits_five_seconds_between_screen_checks(monkeypatch):
    select = [nyc_portal.ScreenText("Select", (20, 100, 80, 130))]
    empty = [nyc_portal.ScreenText("loading", (20, 100, 80, 130))]
    clock, seen = _wait_setup(monkeypatch, [empty, empty, select])

    rows = nyc_portal._wait_screen(
        1, lambda screen: any(r.text == "Select" for r in screen),
        timeout=60, description="страницу", pause=nyc_portal.FAST_SCREEN_CHECK_PAUSE_SEC,
    )
    assert rows == select
    assert len(seen) == 3
    assert clock.sleeps == [5.0, 5.0]


def test_default_wait_keeps_the_fast_short_polling(monkeypatch):
    empty = [nyc_portal.ScreenText("loading", (20, 100, 80, 130))]
    clock, seen = _wait_setup(monkeypatch, [empty])
    with pytest.raises(nyc_portal.PortalLookupError, match="Не дождался"):
        nyc_portal._wait_screen(1, lambda _s: False, timeout=1.5, description="страницу")
    # Проверки при t=0, 0.6, 1.2 и у дедлайна t=1.5: последняя пауза укорочена до остатка.
    assert len(seen) == 4
    assert clock.sleeps == [0.6, 0.6, pytest.approx(0.3)]


def test_wait_screen_makes_a_final_check_exactly_at_the_deadline(monkeypatch):
    empty = [nyc_portal.ScreenText("loading", (20, 100, 80, 130))]
    clock, seen = _wait_setup(monkeypatch, [empty])
    with pytest.raises(nyc_portal.PortalLookupError):
        nyc_portal._wait_screen(1, lambda _s: False, timeout=5, description="окно", pause=5.0)
    assert len(seen) == 2  # при t=0 и при t=5, а не только при t=0
    assert clock.sleeps == [5.0]


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


def test_lookup_parcel_passes_fast_mode_to_the_portal_driver(monkeypatch, tmp_path):
    received = {}

    def drive(address, map_path, progress=None, *, fast_mode=False):
        received["fast_mode"] = fast_mode
        raise nyc_portal.PortalLookupError("стоп после проверки аргумента")

    monkeypatch.setattr(nyc_portal, "_drive_portal", drive)
    with pytest.raises(nyc_portal.PortalLookupError, match="стоп"):
        nyc_portal.lookup_parcel(
            "87 Stafford Ave", tmp_path, cfg={"api": {}}, provider="hydra", model="vision",
            fast_mode=True,
        )
    assert received == {"fast_mode": True}


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
