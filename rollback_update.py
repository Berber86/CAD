"""
Откат программы на резервную копию прежней версии (вызывается из run.bat).

Запускается в папке программы: ``.venv\\Scripts\\python.exe rollback_update.py``.
Использует только app_updater, без GUI: программа может не стартовать, а откат
всё равно должен работать. Код возврата 0 — откат выполнен, 1 — нет.
"""

from __future__ import annotations

import sys
from pathlib import Path

import app_updater


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    try:
        result = app_updater.rollback_latest(app_dir)
    except app_updater.UpdateError as exc:
        print(f"Откат не выполнен: {exc}")
        return 1
    except OSError as exc:
        print(f"Откат не выполнен из-за файловой ошибки: {exc}")
        return 1
    version = result.version or "неизвестна"
    print(
        f"Откат выполнен: возвращена версия {version}, "
        f"восстановлено файлов — {result.files_restored}, удалено новых — {result.files_removed}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
