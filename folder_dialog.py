"""
Нативный диалог выбора нескольких папок (Windows ``IFileOpenDialog``).

Зачем: tkinter ``askdirectory`` умеет только одну папку. В очереди «Данные»
папки работ приходилось добавлять по одной, каждый раз вылезая в проводнике
из предыдущей на уровень вверх (живая просьба пользователя: выбирать сразу
несколько, с Ctrl). Системный ``IFileOpenDialog`` с флагами
``PICKFOLDERS | ALLOWMULTISELECT`` — тот же Проводник, но возвращает все
отмеченные папки.

Реализация — чистый ctypes по COM-таблицам интерфейса: новых зависимостей не
нужно. На не-Windows и при любом сбое COM бросается :class:`FolderDialogError`,
и вызывающий код откатывается на обычный ``askdirectory``.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

#: Флаги IFileOpenDialog (FILEOPENDIALOGOPTIONS).
_FOS_NOCHANGEDIR = 0x00000008
_FOS_PICKFOLDERS = 0x00000020
_FOS_FORCEFILESYSTEM = 0x00000040
_FOS_ALLOWMULTISELECT = 0x00000200
_FOS_PATHMUSTEXIST = 0x00000800

_SIGDN_FILESYSPATH = 0x80058000
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_HRESULT_CANCELLED = 0x800704C7  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
_HRESULT_CHANGED_MODE = 0x80010106  # RPC_E_CHANGED_MODE: COM уже поднят не нами

_CLSID_FILE_OPEN_DIALOG = "{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}"
_IID_FILE_OPEN_DIALOG = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
_IID_SHELL_ITEM = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"

# Индексы методов в vtable (IUnknown: 0 QueryInterface, 1 AddRef, 2 Release).
_IDX_RELEASE = 2
_IDX_SHOW = 3  # IModalWindow
_IDX_SET_OPTIONS = 9  # IFileDialog
_IDX_GET_OPTIONS = 10
_IDX_SET_FOLDER = 12
_IDX_SET_TITLE = 17
_IDX_GET_RESULTS = 27  # IFileOpenDialog
_IDX_ARRAY_GET_COUNT = 7  # IShellItemArray
_IDX_ARRAY_GET_ITEM_AT = 8
_IDX_ITEM_GET_DISPLAY_NAME = 5  # IShellItem


class FolderDialogError(RuntimeError):
    """Нативный диалог недоступен — вызывающий код откатывается на askdirectory."""


def available() -> bool:
    """Нативный диалог папок есть только в Windows."""
    return sys.platform == "win32"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    hr = ctypes.windll.ole32.IIDFromString(text, ctypes.byref(guid))
    if hr != 0:
        raise FolderDialogError(f"IIDFromString({text}) вернул {hr:#010x}")
    return guid


def _method(iface: int, index: int, restype, *argtypes):
    """Функция метода COM-интерфейса по индексу в его vtable."""
    vtable = ctypes.cast(iface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(iface: int) -> None:
    if iface:
        _method(iface, _IDX_RELEASE, ctypes.c_ulong)(iface)


def _code(hr: int) -> int:
    return int(hr) & 0xFFFFFFFF


def ask_folders(
    title: str = "",
    *,
    parent_hwnd: int = 0,
    initial_dir: str | Path | None = None,
) -> list[str]:
    """
    Диалог выбора папок (можно несколько: Ctrl/Shift) → пути; отмена → ``[]``.

    Любой сбой COM — :class:`FolderDialogError`. ``parent_hwnd`` — окно-владелец
    (диалог модален к нему, как остальные диалоги приложения); ``initial_dir`` —
    папка, в которой диалог откроется (обычно родительская для прошлого выбора,
    чтобы не начинать внутри уже добавленной папки).
    """
    if not available():
        raise FolderDialogError("нативный диалог папок есть только в Windows")
    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32

    co_owned = False
    dialog = ctypes.c_void_p()
    results = ctypes.c_void_p()
    initial_item = ctypes.c_void_p()
    try:
        hr = int(ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED))
        if hr == 0:  # S_OK: инициализировали мы — нам и закрывать.
            co_owned = True
        elif _code(hr) not in (1, _HRESULT_CHANGED_MODE):  # S_FALSE — уже было.
            raise FolderDialogError(f"CoInitializeEx вернул {hr:#010x}")

        clsid = _guid(_CLSID_FILE_OPEN_DIALOG)
        iid = _guid(_IID_FILE_OPEN_DIALOG)
        hr = int(ole32.CoCreateInstance(
            ctypes.byref(clsid), None, _CLSCTX_INPROC_SERVER,
            ctypes.byref(iid), ctypes.byref(dialog)))
        if hr != 0 or not dialog:
            raise FolderDialogError(f"CoCreateInstance(FileOpenDialog) вернул {hr:#010x}")
        iface = int(dialog.value or 0)

        options = ctypes.c_uint32()
        hr = int(_method(iface, _IDX_GET_OPTIONS, ctypes.c_long,
                         ctypes.POINTER(ctypes.c_uint32))(iface, ctypes.byref(options)))
        if hr != 0:
            raise FolderDialogError(f"IFileDialog.GetOptions вернул {hr:#010x}")
        flags = options.value | _FOS_PICKFOLDERS | _FOS_ALLOWMULTISELECT \
            | _FOS_FORCEFILESYSTEM | _FOS_PATHMUSTEXIST | _FOS_NOCHANGEDIR
        hr = int(_method(iface, _IDX_SET_OPTIONS, ctypes.c_long,
                         ctypes.c_uint32)(iface, flags))
        if hr != 0:
            raise FolderDialogError(f"IFileDialog.SetOptions вернул {hr:#010x}")

        if title:
            _method(iface, _IDX_SET_TITLE, ctypes.c_long, ctypes.c_wchar_p)(iface, str(title))

        if initial_dir:
            item_iid = _guid(_IID_SHELL_ITEM)
            hr = int(shell32.SHCreateItemFromParsingName(
                str(initial_dir), None, ctypes.byref(item_iid), ctypes.byref(initial_item)))
            if hr == 0 and initial_item:
                # Ошибка начальной папки не страшна: диалог откроется в своей.
                _method(iface, _IDX_SET_FOLDER, ctypes.c_long,
                        ctypes.c_void_p)(iface, initial_item)

        hr = int(_method(iface, _IDX_SHOW, ctypes.c_long,
                         ctypes.c_void_p)(iface, int(parent_hwnd or 0)))
        if _code(hr) == _HRESULT_CANCELLED:
            return []
        if hr != 0:
            raise FolderDialogError(f"IFileDialog.Show вернул {hr:#010x}")

        hr = int(_method(iface, _IDX_GET_RESULTS, ctypes.c_long,
                         ctypes.POINTER(ctypes.c_void_p))(iface, ctypes.byref(results)))
        if hr != 0 or not results:
            raise FolderDialogError(f"IFileOpenDialog.GetResults вернул {hr:#010x}")
        array = int(results.value or 0)

        count = ctypes.c_uint32()
        hr = int(_method(array, _IDX_ARRAY_GET_COUNT, ctypes.c_long,
                         ctypes.POINTER(ctypes.c_uint32))(array, ctypes.byref(count)))
        if hr != 0:
            raise FolderDialogError(f"IShellItemArray.GetCount вернул {hr:#010x}")

        paths: list[str] = []
        for index in range(int(count.value)):
            item = ctypes.c_void_p()
            hr = int(_method(array, _IDX_ARRAY_GET_ITEM_AT, ctypes.c_long,
                             ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))(
                                 array, index, ctypes.byref(item)))
            if hr != 0 or not item:
                continue
            try:
                text_ptr = ctypes.c_void_p()
                hr = int(_method(int(item.value or 0), _IDX_ITEM_GET_DISPLAY_NAME,
                                 ctypes.c_long, ctypes.c_uint32,
                                 ctypes.POINTER(ctypes.c_void_p))(
                                     int(item.value or 0), _SIGDN_FILESYSPATH,
                                     ctypes.byref(text_ptr)))
                if hr == 0 and text_ptr:
                    try:
                        value = ctypes.cast(text_ptr, ctypes.c_wchar_p).value
                    finally:
                        ole32.CoTaskMemFree(text_ptr)
                    if value:
                        paths.append(str(value))
            finally:
                _release(int(item.value or 0))
        return paths
    except FolderDialogError:
        raise
    except Exception as exc:  # noqa: BLE001 - любой сбой ctypes/COM — откат вызывающему
        raise FolderDialogError(f"нативный диалог папок не удался: {exc}") from exc
    finally:
        _release(int(results.value or 0))
        _release(int(initial_item.value or 0))
        _release(int(dialog.value or 0))
        if co_owned:
            try:
                ole32.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
