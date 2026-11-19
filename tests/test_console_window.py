"""
Проверки console_window: окно run.bat прячется после показа программы и возвращается при сбое.

Зачем (4.41): после обновления старое окно cmd оставалось висеть в панели задач —
за несколько обновлений их накапливалось несколько. Теперь run.bat поднимает GUI
отдельным процессом без консоли и закрывается сам, а этот модуль — страховка для
запасного пути (pythonw.exe не нашёлся): консоль прячется, когда окно уже показано,
и возвращается на экран, если запуск не удался (там текст ошибки и вопрос об откате).

WinAPI подменяется: тесты работают без Windows и без настоящей консоли.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

import console_window as cw


class _FakeUser32:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def ShowWindow(self, hwnd, mode):  # noqa: N802 - имя из WinAPI
        self.calls.append(("ShowWindow", (hwnd, mode)))
        return 1

    def SetForegroundWindow(self, hwnd):  # noqa: N802 - имя из WinAPI
        self.calls.append(("SetForegroundWindow", (hwnd,)))
        return 1


class _FakeKernel32:
    def __init__(self, hwnd: int = 4242) -> None:
        self._hwnd = hwnd
        self.calls = 0

    def GetConsoleWindow(self):  # noqa: N802 - имя из WinAPI
        self.calls += 1
        return self._hwnd


class _FakeWindll:
    def __init__(self, hwnd: int = 4242) -> None:
        self.kernel32 = _FakeKernel32(hwnd)
        self.user32 = _FakeUser32()


@pytest.fixture()
def windows(monkeypatch):
    """Windows с подменённым WinAPI: консольное окно существует (hwnd=4242)."""
    monkeypatch.setattr(cw.sys, "platform", "win32")
    fake = _FakeWindll()
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    return fake


@pytest.fixture()
def no_console(monkeypatch):
    """Windows без консоли (программа запущена через pythonw.exe)."""
    monkeypatch.setattr(cw.sys, "platform", "win32")
    fake = _FakeWindll(hwnd=0)
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    return fake


# --------------------------------------------------------------------------- #
# Просьба run.bat спрятать консоль
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["1", "yes", "TRUE", "On", "да", " 1 "])
def test_hide_requested_accepts_true_values(monkeypatch, value):
    monkeypatch.setenv(cw.HIDE_CONSOLE_ENV, value)
    assert cw.hide_requested() is True


@pytest.mark.parametrize("value", ["", "0", "no", "off", "нет", "maybe"])
def test_hide_requested_rejects_other_values(monkeypatch, value):
    monkeypatch.setenv(cw.HIDE_CONSOLE_ENV, value)
    assert cw.hide_requested() is False


def test_hide_requested_is_false_without_env(monkeypatch):
    monkeypatch.delenv(cw.HIDE_CONSOLE_ENV, raising=False)
    assert cw.hide_requested() is False


def test_hide_requested_env_name_matches_run_bat():
    """run.bat ставит именно эту переменную — иначе консоль не спрятать."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "run.bat").read_text(
        encoding="utf-8", errors="replace"
    )
    assert f'set "{cw.HIDE_CONSOLE_ENV}=1"' in text


# --------------------------------------------------------------------------- #
# Показ/скрытие окна консоли
# --------------------------------------------------------------------------- #

def test_hide_console_calls_show_window_hide(windows):
    assert cw.hide_console() is True
    assert windows.user32.calls == [("ShowWindow", (4242, cw.SW_HIDE))]


def test_show_console_returns_window_and_focus(windows):
    assert cw.show_console() is True
    assert windows.user32.calls == [
        ("ShowWindow", (4242, cw.SW_SHOW)),
        ("SetForegroundWindow", (4242,)),
    ]


def test_console_hwnd_is_zero_without_console(no_console):
    assert cw.console_hwnd() == 0
    assert no_console.kernel32.calls == 1


def test_hide_and_show_are_noops_without_console(no_console):
    """pythonw.exe: консоли нет — прятать нечего, и это не ошибка."""
    assert cw.hide_console() is False
    assert cw.show_console() is False
    assert no_console.user32.calls == []


def test_console_helpers_are_noops_off_windows(monkeypatch):
    monkeypatch.setattr(cw.sys, "platform", "linux")
    assert cw.console_hwnd() == 0
    assert cw.hide_console() is False
    assert cw.show_console() is False


def test_console_helpers_survive_winapi_failure(monkeypatch):
    """Сбой WinAPI не должен ронять запуск программы."""
    monkeypatch.setattr(cw.sys, "platform", "win32")

    class _Broken:
        @property
        def kernel32(self):
            raise OSError("нет WinAPI")

        @property
        def user32(self):
            raise OSError("нет WinAPI")

    monkeypatch.setattr(ctypes, "windll", _Broken(), raising=False)
    assert cw.console_hwnd() == 0
    assert cw.hide_console() is False
    assert cw.show_console() is False


def test_as_dict_reports_state(monkeypatch, windows):
    monkeypatch.setenv(cw.HIDE_CONSOLE_ENV, "1")
    state = cw.as_dict()
    assert state == {"platform": "win32", "requested": True, "hwnd": 4242}


# --------------------------------------------------------------------------- #
# run.bat: окно cmd закрывается сразу после запуска программы
# --------------------------------------------------------------------------- #

def _run_bat_text() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "run.bat").read_text(
        encoding="utf-8", errors="replace"
    )


def test_run_bat_launches_gui_without_console_and_exits():
    """
    Главное в 4.41: cmd НЕ ждёт программу.

    GUI поднимается через pythonw.exe отдельным процессом (`start`), после чего
    run.bat сразу заканчивается — окно cmd закрывается и не остаётся в панели задач.
    Раньше cmd ждал python.exe, а обновление заменяло run.bat во время его выполнения,
    из-за чего старое окно зависало.
    """
    text = _run_bat_text()
    assert 'if exist ".venv\\Scripts\\pythonw.exe"' in text
    assert 'start "" ".venv\\Scripts\\pythonw.exe" main.py' in text
    # Запасной путь без pythonw — обычный python.exe и просьба спрятать консоль.
    assert 'set "TSAI_HIDE_CONSOLE=1"' in text
    assert '".venv\\Scripts\\python.exe" main.py' in text


def test_run_bat_checks_files_before_launch():
    """Побитые обновлением файлы видны в консоли до запуска, вместе с предложением откатиться."""
    text = _run_bat_text()
    assert "compileall.compile_dir" in text
    assert ":launch_error" in text
    # Откат на резервную копию остаётся доступным.
    assert "rollback_update.py" in text


def test_run_bat_keeps_crlf_and_no_bom():
    from pathlib import Path

    data = (Path(__file__).resolve().parents[1] / "run.bat").read_bytes()
    assert data.startswith(b"@echo off\r\n")
    assert data.count(b"\n") == data.count(b"\r\n")


def test_pythonw_launch_has_no_console_streams_guard():
    """
    В pythonw.exe `sys.stdout`/`sys.stderr` равны None, и print() кончается
    RuntimeError («lost sys.stdout») — диагностика сбоя ударила бы по себе же.
    Поэтому main.py заменяет их заглушкой на входе.
    """
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "if sys.stdout is None:" in text and "if sys.stderr is None:" in text
    assert "io.StringIO()" in text


def test_module_platform_is_reported_for_diagnostics():
    assert cw.as_dict()["platform"] == sys.platform
