"""Проверки run.bat в изолированной папке и без обращения к сети (только Windows)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    wheel = _make_local_wheel(launcher_dir)
    (launcher_dir / "requirements.txt").write_text(f"./{wheel}\n", encoding="utf-8")
    (launcher_dir / "main.py").write_text(
        "import launcher_probe\nassert launcher_probe.VALUE == 'installed'\nprint('LAUNCH_OK')\n",
        encoding="utf-8",
    )
    result = _run_launcher(launcher_dir)
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")
    assert b"LAUNCH_OK" in result.stdout


@requires_windows
@pytest.mark.parametrize(
    ("script", "marker"),
    [
        ("import nonexistent_launcher_probe_module\n", b"ModuleNotFoundError"),
        ("import sys\nprint('NEGATIVE_EXIT')\nsys.exit(-1)\n", b"NEGATIVE_EXIT"),
    ],
    ids=("import-error", "negative-exit-code"),
)
def test_launch_errors_keep_diagnostics_and_return_failure(launcher_dir, script, marker):
    (launcher_dir / "requirements.txt").write_text("", encoding="utf-8")
    (launcher_dir / "main.py").write_text(script, encoding="utf-8")
    result = _run_launcher(launcher_dir)
    assert result.returncode == 1
    assert marker in result.stdout
    assert b"tsai_crash.log" in result.stdout


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
