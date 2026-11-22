"""Save the AutoCAD drawing next to the collected PDFs under a fresh name."""
from pathlib import Path
import os
import re
from typing import Any
from uuid import uuid4


class AutoDwgSaveError(RuntimeError):
    """Save did not complete; the source drawing and any partial output remain."""


def save_new_dwg(doc: Any, descriptions_dir: Path, lot: str) -> Path:
    """SaveAs to this directory, choosing a unique filename without overwriting.

    A random 128-bit suffix and an exclusively created reservation coordinate
    parallel application instances. Never reserve over an existing DWG. Partial
    DWGs from failed SaveAs calls are retained so the user can recover them.
    """
    number = str(lot or "").strip()
    number = str(int(number)) if re.fullmatch(r"[0-9]+", number) and int(number) > 0 else "unknown"
    root = Path(descriptions_dir).resolve(strict=True)
    if not root.is_dir():
        raise AutoDwgSaveError("папка описаний недоступна")
    for _ in range(20):
        destination = root / f"lot_{number}_{uuid4().hex}.dwg"
        reservation = root / f".{destination.name}.reserve"
        try:
            fd = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        try:
            if os.path.lexists(destination):
                continue
            try:
                doc.SaveAs(str(destination))
            except Exception as exc:  # noqa: BLE001 - retain partial output on failure
                raise AutoDwgSaveError(f"AutoCAD не подтвердил сохранение; проверьте {destination}: {exc}") from exc
            if not destination.is_file() or destination.stat().st_size == 0:
                raise AutoDwgSaveError(f"AutoCAD не создал непустой DWG; проверьте {destination}")
            return destination
        finally:
            try:
                reservation.unlink(missing_ok=True)
            except OSError:
                # A stale reservation is harmless and must not mask a saved DWG.
                pass
    raise AutoDwgSaveError("не удалось выбрать свободное имя DWG в папке описаний")
