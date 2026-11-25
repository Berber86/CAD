"""Нативный диалог выбора папок: вне Windows честно недоступен (откат — askdirectory)."""
from __future__ import annotations

import sys

import pytest

import folder_dialog


@pytest.mark.skipif(sys.platform == "win32", reason="проверяется не-Windows ветка")
def test_native_folder_dialog_unavailable_off_windows():
    assert folder_dialog.available() is False
    with pytest.raises(folder_dialog.FolderDialogError):
        folder_dialog.ask_folders("Папки работ")
