"""
Страница «Данные» (перед Шагом 0): папки-задачи и очередь автопотока.

Имя папки работы задано жёстко: сначала номер работы (цифры; дальше могут
быть ещё буквы или символы), затем «#», после него — адрес объекта,
например «1234A#417 WEST 33RD STREET». Из имени берутся номер и адрес;
адрес уходит в автопоток Шага 0 (портал сам находит район, block и lot).

Каждая задача работает в подпапке ``data`` своей папки: туда падают PDF
описаний, обрезки, кэши, ``_manifest.json``, готовый DWG и полный лог
сессии задачи (``session_log.txt``) — чтобы было видно, что происходило
и где сломалось.

Модуль — чистая логика имён и путей; сама очередь живёт в ``main.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import log_mode

#: Подпапка задачи, куда складывается вся её работа.
DATA_DIR_NAME = "data"
#: Файл полного лога сессии задачи внутри ``data``.
SESSION_LOG_NAME = "session_log.txt"

_JOB_NAME_RE = re.compile(r"^(\d+)([^#]*)#(.+)$", re.DOTALL)


@dataclass(frozen=True)
class JobFolderInfo:
    """Разобранное имя папки-задачи."""

    path: Path
    #: Номер работы — ведущие цифры имени.
    job_no: str
    #: Остаток между номером и «#» (буквы/символы; может быть пустым).
    suffix: str
    #: Адрес объекта — всё после первого «#».
    address: str

    @property
    def title(self) -> str:
        """«1234A · 417 WEST 33RD STREET» — короткая подпись задачи."""
        return f"{self.job_no}{self.suffix} · {self.address}"


def parse_job_folder_name(name: str) -> tuple[str, str, str] | None:
    """
    «1234A#417 WEST 33RD STREET» -> (номер, суффикс, адрес); иначе None.

    Номер — ведущие цифры; между номером и «#» допустимы буквы/символы;
    адрес — всё после первого «#», непустой.
    """
    match = _JOB_NAME_RE.match(str(name or "").strip())
    if not match:
        return None
    job_no, suffix, address = match.group(1), match.group(2).strip(), match.group(3).strip()
    if not address:
        return None
    return job_no, suffix, address


def describe_job_folder(path: str | Path) -> JobFolderInfo | None:
    """Разобранная папка-задача или None, если имя не по правилу."""
    folder = Path(path)
    parsed = parse_job_folder_name(folder.name)
    if parsed is None:
        return None
    job_no, suffix, address = parsed
    return JobFolderInfo(folder, job_no, suffix, address)


def invalid_reason(name: str) -> str:
    """Почему имя папки не подходит под правило «номер…#адрес»."""
    text = str(name or "").strip()
    if "#" not in text:
        return "нет «#» перед адресом"
    head, _sep, tail = text.partition("#")
    if not tail.strip():
        return "пустой адрес после «#»"
    head = head.strip()
    if not head or not head[0].isdigit():
        return "не начинается с номера работы"
    return "не подходит под «номер…#адрес»"


def scan_catalog(parent: str | Path) -> list[Path]:
    """Непосредственные подпапки каталога, отсортированные по имени."""
    root = Path(parent)
    try:
        children = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return []
    return sorted(children, key=lambda item: item.name.casefold())


def data_dir_for(job_folder: str | Path) -> Path:
    """Подпапка ``data`` папки-задачи — вся работа падает сюда."""
    return Path(job_folder) / DATA_DIR_NAME


def session_log_path(job_folder: str | Path) -> Path:
    """Файл полного лога сессии задачи: ``<папка>/data/session_log.txt``."""
    return data_dir_for(job_folder) / SESSION_LOG_NAME


def session_log_lines(entries: Iterable[Sequence[str]]) -> list[str]:
    """
    Тело ``session_log.txt`` из записей приёмника ``(метка, источник, уровень, текст)``.

    Единственный вариант лога — без режимов. Раньше в файл сваливался весь
    поток: каждая страница OCR, каждый шаг загрузки и каждый повтор повторов
    при повторах запросов — читать такой лог тяжело и человеку, и при разборе
    сбоев. Теперь из информационных записей уходит только механический шум
    (``log_mode.is_log_file_noise``: постраничные счётчики, шаги загрузок,
    технические параметры), повторы подряд схлопываются в одну строку со
    счётчиком, а весь содержательный ход работы — шаги, итоги, предупреждения
    и ошибки любой длины — остаётся как был.
    """
    kept: list[tuple[str, str, str, str]] = []
    for stamp, source, level, text in entries:
        text = str(text)
        if not text.strip():
            continue
        if str(level) == log_mode.LEVEL_INFO and log_mode.is_log_file_noise(text):
            continue
        kept.append((str(stamp), str(source), str(level), text))
    grouped: list[list] = []
    for stamp, source, level, text in kept:
        if grouped and grouped[-1][1:4] == [source, level, text]:
            grouped[-1][4] += 1
        else:
            grouped.append([stamp, source, level, text, 1])
    return [
        f"[{stamp}] [{source}] {text}" + (f" (повтор ×{count})" if count > 1 else "")
        for stamp, source, _level, text, count in grouped
    ]
