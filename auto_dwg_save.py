"""Save the AutoCAD drawing next to the collected PDFs under a fresh name."""
from pathlib import Path
import os
import re
import time
from typing import Any, Callable
from uuid import uuid4


class AutoDwgSaveError(RuntimeError):
    """Save did not complete; the source drawing and any partial output remain."""


#: COM-коды «AutoCAD занят, повтори позже»: вызов отклонён получателем,
#: отмена вызова и «приложение занято» фильтра сообщений. Ночной прогон
#: 24–25.11.2026: SaveAs дважды за ночь умер на -2147418111 ровно один раз —
#: без повтора задание теряло полностью нарисованный чертёж.
_BUSY_HRESULTS = frozenset({-2147418111, -2147418110, -2147417846})
_BUSY_MARKS = ("-2147418111", "-2147417846", "отклон", "rejected", "занят", "busy")


def _is_busy_error(exc: BaseException) -> bool:
    """True, если ошибка похожа на временную занятость AutoCAD, а не на отказ."""
    if getattr(exc, "hresult", None) in _BUSY_HRESULTS:
        return True
    text = str(exc).lower()
    return any(mark in text for mark in _BUSY_MARKS)


def save_new_dwg(
    doc: Any,
    descriptions_dir: Path,
    lot: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    save_attempts: int = 5,
    save_delay: float = 2.0,
) -> Path:
    """SaveAs to this directory, choosing a unique filename without overwriting.

    A random 128-bit suffix and an exclusively created reservation coordinate
    parallel application instances. Never reserve over an existing DWG. Partial
    DWGs from failed SaveAs calls are retained so the user can recover them.

    A busy AutoCAD (call rejected, -2147418111) is retried for a few seconds:
    the drawing is already fully built at this point, and one rejected SaveAs
    must not cost the whole job. Non-busy errors fail immediately, as before.
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
            save_error: Exception | None = None
            for attempt in range(max(1, int(save_attempts))):
                try:
                    doc.SaveAs(str(destination))
                    save_error = None
                    break
                except Exception as exc:  # noqa: BLE001 - retain partial output on failure
                    save_error = exc
                    if not _is_busy_error(exc) or attempt == max(1, int(save_attempts)) - 1:
                        break
                    sleep(max(0.0, float(save_delay)))
            if save_error is not None:
                raise AutoDwgSaveError(f"AutoCAD не подтвердил сохранение; проверьте {destination}: {save_error}") from save_error
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
