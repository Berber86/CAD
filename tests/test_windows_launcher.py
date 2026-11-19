"""Проверки run.bat в изолированной папке и без обращения к сети (только Windows)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from zipfile import ZipFile

import pytest


requires_windows = pytest.mark.skipif(sys.platform != "win32", reason="run.bat требует Windows cmd.exe")
ROOT = Path(__file__).resolve().parents[1]


def test_run_bat_has_windows_line_endings():
    data = (ROOT / "run.bat").read_bytes()
    assert data.startswith(b"@echo off\r\n")  # без UTF-8 BOM
    assert data.count(b"\n") == data.count(b"\r\n") == data.count(b"\r")


@pytest.fixture
def launcher_dir(tmp_path):
    # Проверяем пробелы, Unicode и специальные для cmd символы в пути.
    work = tmp_path / "CAD & test (папка!)"
    work.mkdir()
    shutil.copy2(ROOT / "run.bat", work / "run.bat")
    subprocess.run(
        [sys.executable, "-m", "venv", str(work / ".venv")],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return work


def _run_launcher(work: Path) -> subprocess.CompletedProcess:
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("PIP_") and key.upper() not in {"PYTHONPATH", "PYTHONHOME"}
    }
    env.update(
        PIP_NO_INDEX="1",
        PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PYTHONIOENCODING="utf-8",
        USERPROFILE=str(work),
    )
    return subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "run.bat"],
        cwd=work,
        env=env,
        input=b"\r\n",  # для pause при ошибке
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )


def _wait_for(path: Path, timeout: float = 60.0) -> bool:
    """
    Ждём файл-маркер от программы.

    С 4.41 run.bat поднимает GUI отдельным процессом (pythonw.exe) и закрывается сам,
    поэтому к моменту его возврата программа может ещё запускаться.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.2)
    return path.exists()


def _make_local_wheel(work: Path) -> str:
    """Минимальный wheel, чтобы проверить установку в старую .venv без интернета."""
    filename = "launcher_probe-1.0-py3-none-any.whl"
    dist = "launcher_probe-1.0.dist-info"
    files = {
        "launcher_probe.py": "VALUE = 'installed'\n",
        f"{dist}/METADATA": "Metadata-Version: 2.1\nName: launcher-probe\nVersion: 1.0\n\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
    }
    records = "".join(f"{name},,\n" for name in files) + f"{dist}/RECORD,,\n"
    with ZipFile(work / filename, "w") as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)
        wheel.writestr(f"{dist}/RECORD", records)
    return filename


@requires_windows
def test_existing_venv_gets_missing_dependency_before_launch(launcher_dir):
    """Зависимости ставятся до запуска, и программа действительно поднимается."""
    wheel = _make_local_wheel(launcher_dir)
    (launcher_dir / "requirements.txt").write_text(f"./{wheel}\n", encoding="utf-8")
    marker = launcher_dir / "started.txt"
    (launcher_dir / "main.py").write_text(
        "import launcher_probe\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(launcher_probe.VALUE, encoding='utf-8')\n",
        encoding="utf-8",
    )
    result = _run_launcher(launcher_dir)
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")
    assert _wait_for(marker), "программа не запущена"
    assert marker.read_text(encoding="utf-8") == "installed"


@requires_windows
def test_launcher_does_not_wait_for_the_program(launcher_dir):
    """
    Окно cmd закрывается сразу после запуска программы (4.41).

    Раньше run.bat ждал завершения python.exe, а обновление заменяло run.bat прямо во
    время его выполнения — старое окно cmd зависало и оставалось в панели задач (за
    несколько обновлений их накапливалось несколько). Теперь GUI поднимается отдельным
    процессом без консоли: вывод программы в окно run.bat не попадает, а сам run.bat
    завершается, не дожидаясь её.
    """
    (launcher_dir / "requirements.txt").write_text("", encoding="utf-8")
    marker = launcher_dir / "started.txt"
    (launcher_dir / "main.py").write_text(
        "from pathlib import Path\n"
        "print('CONSOLE_OUTPUT_OF_THE_PROGRAM')\n"
        f"Path({str(marker)!r}).write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )
    result = _run_launcher(launcher_dir)
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")
    assert _wait_for(marker), "программа не запущена"
    assert b"CONSOLE_OUTPUT_OF_THE_PROGRAM" not in result.stdout


@requires_windows
def test_broken_files_are_reported_before_launch(launcher_dir):
    """Побитые обновлением файлы видны в консоли: запуск не начинается, предлагается откат."""
    (launcher_dir / "requirements.txt").write_text("", encoding="utf-8")
    (launcher_dir / "main.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    result = _run_launcher(launcher_dir)
    assert result.returncode == 1
    assert b"Error compiling" in result.stdout
    assert b"tsai_crash.log" in result.stdout


@requires_windows
def test_program_failure_is_reported_by_the_program(launcher_dir):
    """
    Сбой уже запущенной программы — на её стороне, не в run.bat.

    Программа работает без консоли, поэтому свои ошибки она показывает сама: пишет
    ``tsai_crash.log`` и открывает диалог (``main._show_startup_failure``). run.bat к
    этому моменту закрыт, и его код возврата остаётся нулевым.
    """
    (launcher_dir / "requirements.txt").write_text("", encoding="utf-8")
    marker = launcher_dir / "started.txt"
    (launcher_dir / "main.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n"
        "raise SystemExit(-1)\n",
        encoding="utf-8",
    )
    result = _run_launcher(launcher_dir)
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")
    assert _wait_for(marker), "программа не запускалась"


@requires_windows
def test_install_error_does_not_start_application(launcher_dir):
    (launcher_dir / "requirements.txt").write_text("--invalid-test-option\n", encoding="utf-8")
    (launcher_dir / "main.py").write_text(
        "from pathlib import Path\nPath('started.txt').touch()\n", encoding="utf-8"
    )
    result = _run_launcher(launcher_dir)
    assert result.returncode == 1
    assert not (launcher_dir / "started.txt").exists()
    assert b"tsai_crash.log" not in result.stdout
