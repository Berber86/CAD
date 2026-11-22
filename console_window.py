"""
Консольное окно запуска (run.bat): спрятать, когда окно программы уже показано.

Зачем (4.41): программа запускается из `run.bat`, а cmd ждёт завершения
`python.exe`. После обновления старое окно программы закрывается, а окно батника
оставалось висеть в панели задач — за несколько обновлений их накапливалось
несколько штук. Теперь run.bat поднимает GUI отдельным процессом без консоли
(`pythonw.exe`) и сразу завершается, поэтому окна cmd не остаётся вовсе.

Этот модуль — страховка для запасного пути: если `pythonw.exe` в окружении нет,
run.bat запускает обычный `python.exe` в своей консоли и просит спрятать её,
как только окно программы появится (переменная окружения `TSAI_HIDE_CONSOLE`).
Спрятанное окно не имеет кнопки в панели задач, но остаётся живым: консольный
вывод и код возврата сохраняются, а run.bat по-прежнему может показать ошибку
и предложить откат. При сбое запуска консоль возвращается на экран
(`show_console`), чтобы текст ошибки и вопрос об откате были видны.

Вне Windows и при запуске без консоли (например, `pythonw.exe`) все функции —
безопасные заглушки: `GetConsoleWindow()` возвращает 0, и ничего не происходит.
"""

from __future__ import annotations

import os
import sys
from typing import Any

#: Переменная окружения, которой run.bat просит спрятать консоль после показа окна.
HIDE_CONSOLE_ENV = "TSAI_HIDE_CONSOLE"
#: Значения, которые считаются согласием (остальное — «не прятать»).
_TRUE_VALUES = frozenset({"1", "yes", "true", "on", "да"})

#: Константы WinAPI ShowWindow.
SW_HIDE = 0
SW_SHOW = 5

__all__ = [
    "HIDE_CONSOLE_ENV",
    "SW_HIDE",
    "SW_SHOW",
    "console_hwnd",
    "hide_console",
    "hide_requested",
    "show_console",
]


def hide_requested() -> bool:
    """Просил ли run.bat спрятать консоль (переменная ``TSAI_HIDE_CONSOLE``)."""
    raw = os.environ.get(HIDE_CONSOLE_ENV)
    return str(raw or "").strip().lower() in _TRUE_VALUES


def console_hwnd() -> int:
    """
    Дескриптор окна консоли текущего процесса; ``0`` — консоли нет или это не Windows.

    Отдельной функцией, чтобы тесты могли подменить платформу и WinAPI.
    """
    if not sys.platform.startswith("win"):
        return 0
    try:
        import ctypes  # noqa: PLC0415 - WinAPI нужен только в Windows

        kernel32 = getattr(ctypes, "windll", None)
        if kernel32 is None:  # pragma: no cover - защита от необычной сборки ctypes
            return 0
        return int(kernel32.kernel32.GetConsoleWindow() or 0)
    except Exception:  # noqa: BLE001 - без консоли работаем как обычно
        return 0


def _show_window(hwnd: int, mode: int) -> bool:
    """Вызывает ``ShowWindow`` для окна консоли; ``False``, если окна нет или WinAPI недоступен."""
    if not hwnd:
        return False
    try:
        import ctypes  # noqa: PLC0415

        user32 = getattr(ctypes, "windll", None)
        if user32 is None:  # pragma: no cover
            return False
        user32.user32.ShowWindow(hwnd, mode)
        return True
    except Exception:  # noqa: BLE001 - прятать консоль не обязательно
        return False


def hide_console() -> bool:
    """
    Прячет окно консоли (кнопка в панели задач пропадает вместе с ним).

    Возвращает ``True``, если окно было спрятано. Процесс консоли при этом жив:
    он завершится сам, когда программа выйдет и run.bat дойдёт до конца.
    """
    return _show_window(console_hwnd(), SW_HIDE)


def show_console() -> bool:
    """
    Возвращает окно консоли на экран и отдаёт ему фокус.

    Нужно при сбое запуска: run.bat печатает ошибку и предлагает откатиться на
    резервную копию — этот текст должен быть виден.
    """
    hwnd = console_hwnd()
    if not _show_window(hwnd, SW_SHOW):
        return False
    try:
        import ctypes  # noqa: PLC0415

        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 - фокус не критичен
        pass
    return True


def as_dict() -> dict[str, Any]:
    """Состояние консоли одной строкой — для журнала и тестов."""
    return {
        "platform": sys.platform,
        "requested": hide_requested(),
        "hwnd": console_hwnd(),
    }
