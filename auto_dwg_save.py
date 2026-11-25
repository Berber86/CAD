"""Save the AutoCAD drawing next to the collected PDFs under a fresh name."""
from pathlib import Path
import os
import re
import time
from typing import Any, Callable, Iterable, Sequence
from uuid import uuid4


class AutoDwgSaveError(RuntimeError):
    """Save did not complete; the source drawing and any partial output remain."""


def save_new_dwg(
    doc: Any,
    descriptions_dir: Path,
    lot: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    save_attempts: int = 5,
    save_delay: float = 1.0,
) -> Path:
    """SaveAs to this directory, choosing a unique filename without overwriting.

    A random 128-bit suffix and an exclusively created reservation coordinate
    parallel application instances. Never reserve over an existing DWG. Partial
    DWGs from failed SaveAs calls are retained so the user can recover them.

    Any failed SaveAs is retried for a few seconds: the drawing is already
    fully built at this point, and one rejected call must not cost the whole
    job. Busy AutoCAD (call rejected, -2147418111) is the known case, but a
    just-created document also rejects calls with texts no classifier knows
    (live case 25.11.2026: ``<unknown>.SaveAs`` a second after the draw).
    A genuinely permanent error still surfaces honestly after the attempts.
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
                    if attempt == max(1, int(save_attempts)) - 1:
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


def warnings_path_for(dwg_path: Path) -> Path:
    """Файл предупреждений рядом с DWG: то же имя, суффикс ``.warnings.txt``."""
    dwg = Path(dwg_path)
    return dwg.with_name(f"{dwg.stem}.warnings.txt")


def write_dwg_warnings(
    dwg_path: Path,
    sections: Iterable[tuple[str, Sequence[str]]],
    *,
    error: str = "",
    stamp: str | None = None,
) -> Path | None:
    """
    Записывает предупреждения построения property line в файл по имени DWG.

    ``sections`` — (подпись плана, его предупреждения) по каждому построенному
    плану: расчётные предупреждения плана и предупреждения отрисовки, в порядке
    выдачи. Файл создаётся при каждом сохранении DWG — и когда предупреждений
    не было (это тоже видно сразу, без раскопок журнала сессии). Ошибка записи
    не должна губить уже сохранённый DWG: при сбое возвращается None.
    """
    dwg = Path(dwg_path)
    blocks: list[str] = []
    total = 0
    for title, warnings in sections:
        items = [str(warning) for warning in warnings if str(warning).strip()]
        if not items:
            continue
        total += len(items)
        blocks.append(f"== {title} ==")
        blocks.extend(f"- {item}" for item in items)
        blocks.append("")
    lines = [
        "Предупреждения при построении property line",
        f"DWG: {dwg.name}",
        f"Записано: {stamp or time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    if str(error or "").strip():
        lines.append(f"Ошибка отрисовки: {str(error).strip()}")
        lines.append("")
    lines += blocks
    if total == 0:
        lines.append("Предупреждений не было.")
    target = warnings_path_for(dwg)
    try:
        target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except OSError:
        return None
    return target
