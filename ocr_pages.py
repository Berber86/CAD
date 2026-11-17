"""
Локальное распознавание сканов (бесплатный OCR) для Шага 1.

Зачем: Шаг 1 умеет разбирать только текст, а у скана (например, документа из
ACRIS) текстового слоя нет вовсе — ``PyMuPDF`` отдаёт по такой странице пустую
строку. Раньше выход был один: отправлять файл в ИИ. Здесь бесплатный путь:
страница рендерится в картинку и распознаётся локальным движком RapidOCR
(ONNX, процессор, без интернета и без внешних программ).

Модуль **необязательный**: если пакет не установлен, ``available()`` возвращает
``False``, и программа работает как раньше. Никаких сетевых запросов здесь нет.

Точность: для наших задач достаточно, чтобы распознались румбы и длины
(``N 22°26'12" E, 100.05 feet``) — фразы и регистр не важны, парсер их всё равно
нормализует. Ошибку в цифре ловит замыкание: невязка в футах вместо сотых —
повод переключить файл на ИИ (это делает окно набора, см. ``main.py``).
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import socket
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ocr_numbers import number_key

#: Разрешение по умолчанию. На Шаге 0 пользователь может выбрать 50–1200 dpi;
#: OCR cache key включает DPI, поэтому экспериментальные запуски не перепутаются.
DEFAULT_DPI = 400

#: Скан распознаётся одним проходом на максимуме, который даёт сам документ: рендер не выше
#: собственного разрешения встроенной картинки (page_render_dpi), но и не выше этого потолка.
SCAN_MAX_DPI = 600

#: На Шаге 0 доступны низкие значения для проверки скорости/читаемости.
MIN_DPI = 50
MAX_DPI = 1200
#: Для обычных/высоких запросов сохраняем прежний OCR floor: очень низкое native DPI
#: скана можно слегка увеличить до 200 для лучшей детекции. Явный запрос <200 не повышаем.
UPSAMPLE_FLOOR_DPI = 200

#: Пояснение для окна и отчёта: чем распознаём.
ENGINE_LABEL = "RapidOCR (локально, бесплатно)"
#: Версия разбора текста. Меняется, когда меняется то, что получается из движка
#: (например, восстановление пробелов): старые записи кеша тогда не используются.
TEXT_VERSION = "3-streetjoin"

Progress = Callable[[str], None]


class OcrError(RuntimeError):
    """Распознавание не удалось: страница не читается или движок упал."""


class OcrUnavailable(OcrError):
    """Локальный OCR не установлен — нужен ИИ или установка пакета."""


_engine: Any = None
_engine_error = ""
_available: bool | None = None


def available() -> bool:
    """Установлен ли локальный OCR (проверка один раз за запуск)."""
    global _available
    if _available is None:
        try:  # noqa: SIM105 - проверяем именно импортом, без загрузки модели
            import rapidocr_onnxruntime  # noqa: F401, PLC0415
        except Exception as exc:  # noqa: BLE001 - нет пакета или нет системных библиотек
            _available = False
            globals()["_engine_error"] = str(exc) or type(exc).__name__
        else:
            _available = True
    return bool(_available)


def unavailable_reason() -> str:
    """Почему OCR недоступен — для подсказки в окне."""
    if available():
        return ""
    return _engine_error or "пакет rapidocr-onnxruntime не установлен"


def engine_label() -> str:
    return ENGINE_LABEL if available() else ""


def cache_dir() -> Path:
    """Где лежат распознанные тексты (вне проекта, чтобы не мусорить в папке работ)."""
    override = os.environ.get("TITLESURVEY_OCR_CACHE", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:
        return Path(base) / "TitleSurveyAI" / "ocr_cache"
    return Path.home() / ".cache" / "titlesurveyai" / "ocr_cache"


def _engine_or_raise() -> Any:
    """Движок создаётся один раз: загрузка моделей занимает секунды."""
    global _engine
    if _engine is not None:
        return _engine
    if not available():
        raise OcrUnavailable(
            "локальный OCR не установлен: " + unavailable_reason()
        )
    try:  # noqa: PLC0415 - тяжёлый импорт только по требованию
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:  # noqa: BLE001
        raise OcrUnavailable(f"локальный OCR не запускается: {exc}") from exc
    try:
        _engine = RapidOCR()
    except Exception as exc:  # noqa: BLE001 - нет модели, не хватило памяти и т. п.
        raise OcrUnavailable(f"локальный OCR не запускается: {exc}") from exc
    return _engine


def _cache_key(path: Path, first: int, last: int, dpi: int, tile: int = 1, exact: bool = False) -> str:
    """Ключ кэша: файл, его время и размер, страницы, разрешение и нарезка."""
    try:
        stat = path.stat()
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        stamp = "?"
    mode = "exact" if exact else "capped"
    raw = f"{path.resolve()}|{stamp}|{first}-{last}|{dpi}|{mode}|{tile}|{ENGINE_LABEL}|{TEXT_VERSION}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _read_cache(key: str) -> str | None:
    file = cache_dir() / f"{key}.txt"
    try:
        return file.read_text(encoding="utf-8") if file.is_file() else None
    except OSError:
        return None


def _provenance_file(key: str) -> Path:
    return cache_dir() / f"{key}.json"


def cache_provenance(key: str) -> dict[str, Any] | None:
    """Кто и когда записал запись кэша (None — запись старая, учёта не было)."""
    try:
        data = json.loads(_provenance_file(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def describe_provenance(info: dict[str, Any] | None) -> str:
    """Строка для журнала: «записал user на PC, 17.11.2026 10:04, версия 3.77»."""
    if not info:
        return "записано до ведения учёта: кто и когда — неизвестно"
    parts = [
        f"записал {info.get('записал', '?')} на {info.get('компьютер', '?')}",
        str(info.get("записано", "?")),
        f"программа {info.get('программа', '?')}",
    ]
    return ", ".join(parts)


def _lock_readonly(file: Path) -> None:
    try:
        os.chmod(file, stat.S_IREAD)
    except OSError:
        pass


def _unlock(file: Path) -> None:
    try:
        os.chmod(file, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass


def _write_cache(key: str, text: str, info: dict[str, Any] | None = None) -> None:
    """
    Записывает распознанный текст один раз: существующую запись не перезаписывает.
    Рядом кладётся сведение о записи (кто, с какого компьютера, когда, какой версией).
    Файлы доступны только для чтения, поэтому случайно затереть их нельзя. Это не защита
    от намеренной подмены: по сути это журнал записи.
    """
    try:
        from version import APP_VERSION  # noqa: PLC0415 - версия программы

        folder = cache_dir()
        folder.mkdir(parents=True, exist_ok=True)
        text_file = folder / f"{key}.txt"
        if text_file.exists():
            return
        with open(text_file, "x", encoding="utf-8") as handle:
            handle.write(text)
        _lock_readonly(text_file)
        meta = {
            "записал": _safe_user(),
            "компьютер": _safe_host(),
            "записано": datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S %z"),
            "программа": f"v{APP_VERSION}",
            "распознавание": ENGINE_LABEL,
            "версия текста": TEXT_VERSION,
            "файл": str((info or {}).get("файл", "")),
            "страницы": str((info or {}).get("страницы", "")),
            "dpi": (info or {}).get("dpi"),
        }
        meta_file = _provenance_file(key)
        with open(meta_file, "x", encoding="utf-8") as handle:
            handle.write(json.dumps(meta, ensure_ascii=False, indent=2))
        _lock_readonly(meta_file)
    except FileExistsError:
        pass  # запись уже есть — остаётся первая
    except OSError:
        pass  # кэш — удобство, а не условие работы


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - в некоторых средах пользователя не определить
        return os.environ.get("USERNAME") or os.environ.get("USER") or "?"


def _safe_host() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "?"


def clear_cache() -> int:
    """Удаляет распознанные тексты вместе со сведениями о записи; возвращает число текстов."""
    folder = cache_dir()
    count = 0
    if folder.is_dir():
        for file in folder.glob("*.txt"):
            try:
                _unlock(file)
                file.unlink()
                count += 1
            except OSError:
                pass
        for file in folder.glob("*.json"):
            try:
                _unlock(file)
                file.unlink()
            except OSError:
                pass
    return count


def native_dpi(page: Any) -> float:
    """
    Какое разрешение реально лежит в странице (dpi встроенной картинки).

    Скан ACRIS — это картинка внутри PDF: рендер выше её собственного разрешения
    ничего не добавляет, только растягивает пиксели. Для векторной страницы
    картинок нет — вернём 0 (рендерить можно с любым dpi).
    """
    try:
        images = page.get_images(full=True)
        width_in = float(page.rect.width) / 72.0
        height_in = float(page.rect.height) / 72.0
    except Exception:  # noqa: BLE001 - страница без картинок нам не мешает
        return 0.0
    best = 0.0
    for image in images or []:
        try:
            width_px, height_px = int(image[2]), int(image[3])
        except (IndexError, TypeError, ValueError):
            continue
        if width_in <= 0 or height_in <= 0:
            continue
        best = max(best, width_px / width_in, height_px / height_in)
    return best


def page_render_dpi(page: Any, requested: int = DEFAULT_DPI) -> int:
    """Разрешает малый DPI, но не уменьшает традиционный рендер по умолчанию ниже 200."""
    wanted = max(MIN_DPI, min(MAX_DPI, int(requested)))
    native = native_dpi(page)
    if native <= 0:
        return wanted
    native = max(MIN_DPI, round(native))
    if wanted < UPSAMPLE_FLOOR_DPI:
        # Пользовательский low-DPI эксперимент соблюдается; при этом не растягиваем
        # embedded scan выше его native resolution без причины.
        return min(wanted, native)
    # For normal/high settings, preserve the former 200-DPI OCR floor. Mild upsampling
    # from e.g. 150 to 200 improves small text detection; larger scans stay native-capped.
    return min(wanted, max(UPSAMPLE_FLOOR_DPI, native))


#: Слова, из которых склеенные OCR-строки собираются обратно. Только то, что
#: встречается в описаниях границ и подписях (плюс общеупотребительные связки).
_VOCAB = {
    "all", "that", "certain", "plot", "piece", "or", "parcel", "of", "land", "with", "buildings",
    "and", "improvements", "thereon", "erected", "situate", "situated", "lying", "being", "in",
    "the", "borough", "county", "city", "state", "new", "york", "richmond", "staten", "island",
    "bounded", "described", "as", "follows", "beginning", "at", "a", "point", "on", "place",
    "northwesterly", "northeasterly", "southwesterly", "southeasterly", "northerly", "southerly",
    "easterly", "westerly", "north", "south", "east", "west", "northwest", "northeast",
    "southwest", "southeast", "side", "sides", "street", "avenue", "boulevard", "road", "laid",
    "out", "to", "width", "feet", "foot", "ft", "distant", "from", "corner", "formed", "by",
    "intersection", "thence", "running", "runs", "continuing", "commencing", "degrees", "minutes",
    "seconds", "along", "containing", "said", "premises", "more", "particularly", "commonly",
    "known", "thereof", "contains", "square", "line", "lines", "right", "way", "chains", "tie",
    "is", "are", "be", "by", "it", "for", "an", "as", "this", "then", "distance", "inches",
    "carlton", "stafford", "alvine", "lot", "block", "section", "each", "other", "side", "out",
    "westerly", "northward", "southward", "eastward", "westward", "will", "upon", "which",
    "where", "between", "thereto", "subject", "easement", "deed", "map", "survey", "bearing",
    "beginning", "closing", "and", "not", "no", "so", "there", "here", "it", "its", "his",
}
_VOCAB_SORTED = sorted(_VOCAB, key=len, reverse=True)
_SHORT_OK = {"a", "i", "o", "to", "of", "in", "on", "at", "by", "or", "is", "be", "an", "as", "it", "no", "so", "to", "up"}


def _segment(token: str) -> list[str] | None:
    """Разбивает слитную строку букв на слова словаря; None, если целиком не выходит."""
    lower = token.lower()
    n = len(lower)
    # best[i] = (score, pieces) для префикса длиной i; score — сумма квадратов длин слов.
    best: list[tuple[float, list[str]] | None] = [None] * (n + 1)
    best[0] = (0.0, [])
    for i in range(n):
        if best[i] is None:
            continue
        score_i, pieces_i = best[i]
        for word in _VOCAB_SORTED:
            j = i + len(word)
            if j > n or lower[i:j] != word:
                continue
            if len(word) < 3 and word not in _SHORT_OK:
                continue
            cand = (score_i + len(word) ** 2, pieces_i + [token[i:j]])
            if best[j] is None or cand[0] > best[j][0]:
                best[j] = cand
    result = best[n]
    return result[1] if result else None


#: «Carlton. Boulevard»: имя с заглавной буквы, точка или дефис, суффикс улицы.
_STREET_JOIN_RE = re.compile(
    r"\b([A-Z][a-z]{2,})[.\-]\s*(?=(?:Boulevard|Avenue|Street|Road|Drive|Place|Lane|Court|Parkway|"
    r"Blvd|Ave|St|Rd|Dr|Pl|Ln|Ct|Pkwy)\b)"
)


def restore_word_breaks(text: str) -> str:
    """
    Возвращает пробелы, которые OCR съел между словами и числами.

    Меняются только слитные последовательности, которые целиком раскладываются на
    слова из словаря; числа и незнакомые слова не трогаются. Цифры «40.O0» и
    одиночные буквы в числах сохраняются.
    """
    if not text:
        return text
    # Число и слово слиплись: «161.00feet», «38seconds», «100.00feet».
    out = re.sub(r"(?<=\d)(?=[A-Za-z]{2,})", " ", text)
    # Слово и число слиплись: «north56», но не «O0» внутри числа.
    out = re.sub(r"(?<=[A-Za-z]{3})(?=\d)", " ", out)
    # Дробь с пробелом: «161. 00» -> «161.00».
    out = re.sub(r"(\d)\.\s+(\d{2}\b)", r"\1.\2", out)
    # Точка без пробела между словами: «Carlton.Boulevard».
    out = re.sub(r"(?<=[A-Za-z]{3})\.(?=[A-Za-z]{3})", ". ", out)
    # Название улицы с точкой или дефисом вместо пробела: «Carlton. Boulevard», «Carlton-Boulevard».
    out = _STREET_JOIN_RE.sub(r"\1 ", out)

    def fix(match: re.Match[str]) -> str:
        token = match.group(0)
        if len(token) < 6 or token.lower() in _VOCAB:
            return token
        pieces = _segment(token)
        return " ".join(pieces) if pieces and len(pieces) > 1 else token

    return re.sub(r"[A-Za-z]{6,}", fix, out)


def _lines_to_text(result: Any) -> str:
    """
    Строки OCR в текст сверху вниз.

    RapidOCR возвращает список ``[рамка, текст, уверенность]``; рамка — четыре
    точки. Сортируем по верхней границе рамки, а внутри строки — по левой, с
    округлением по вертикали: иначе слова одной строки перемешиваются.
    """
    rows: list[tuple[float, float, str]] = []
    for item in result or []:
        try:
            box, text = item[0], str(item[1])
        except (TypeError, IndexError):
            continue
        if not text.strip():
            continue
        try:
            ys = [float(point[1]) for point in box]
            xs = [float(point[0]) for point in box]
        except (TypeError, IndexError, ValueError):
            ys, xs = [0.0], [0.0]
        rows.append((min(ys), min(xs), text.strip()))
    rows.sort(key=lambda row: (round(row[0] / 12.0), row[1]))
    return "\n".join(text for _y, _x, text in rows)


def _line_key(line: str) -> str:
    return re.sub(r"[^a-z0-9]", "", line.lower())


# Похожие по написанию символы, которыми OCR путает цифры (в числовом контексте).
# Слова описания участка: вариант слова из этого списка выигрывает у «испорченного» варианта.
_DESCRIPTION_WORDS = frozenset(
    """avenue street boulevard road place thence beginning running feet degrees minutes seconds
    north south east west northerly southerly easterly westerly northeasterly northwesterly
    southeasterly southwesterly side corner formerly known distant point intersection of the along
    land now or city york said and to from with line continuation thereof party first second part
    coordinates which""".split()
)


def _is_numeric_token(token: str) -> bool:
    return number_key(token) is not None


def vote_tokens(first: str, second: str) -> str:
    """Выбор между двумя чтениями одного слова (разные рендеры одной страницы)."""
    if first == second:
        return first
    if _is_numeric_token(first) and _is_numeric_token(second):
        key_first, key_second = number_key(first), number_key(second)
        if key_first == key_second:
            return key_first  # «llo.68» и «110.68» — одно число
        return first if sum(c.isdigit() for c in first) >= sum(c.isdigit() for c in second) else second
    word_first = re.sub(r"[^a-z]", "", first.lower())
    word_second = re.sub(r"[^a-z]", "", second.lower())
    if word_first not in _DESCRIPTION_WORDS and word_second in _DESCRIPTION_WORDS:
        return second
    return first


def _split_words(line: str) -> list[str]:
    return re.findall(r"[^\s,;]+|[,;]", line)


def vote_line(first: str, second: str) -> str:
    """Слияние двух чтений одной строки по словам: там, где слово расходится с соседним один на один,
    выбирается правдоподобный вариант (`vote_tokens`). Лишние слова не добавляются — это делает
    выравнивание строк в `merge_line_texts`."""
    import difflib  # noqa: PLC0415

    # Запятая и «;» отделяются от слов: «Avenue,North» в одном рендере и «Avenue, North» в другом
    # иначе дают разное число слов и выравнивание не работает.
    a_tokens, b_tokens = _split_words(first), _split_words(second)
    matcher = difflib.SequenceMatcher(None, a_tokens, b_tokens, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace" and (i2 - i1) == (j2 - j1):
            out.extend(vote_tokens(x, y) for x, y in zip(a_tokens[i1:i2], b_tokens[j1:j2]))
        else:
            out.extend(a_tokens[i1:i2])
    return " ".join(out)


def _vote_pair(x: str, y: str) -> str:
    """Пара одной строки: основа — более длинная (обрыв короче), слова — голосование."""
    base, other = (x, y) if len(x) >= len(y) else (y, x)
    return vote_line(base, other)


def merge_line_texts(primary: str, secondary: str, *, similar: float = 0.6) -> str:
    """
    Сливает два распознавания одной страницы (разные разрешения) строка за строкой.

    Один рендер иногда теряет целую строку, а другой — расстояние до угла. Строки выравниваются
    по порядку. Для похожих строк берётся более длинная: обрыв из-за куска или ошибки чтения
    делает строку короче. Строки второго текста, которых нет в первом, вставляются на место
    перед ближайшей совпавшей строкой.
    """
    import difflib  # noqa: PLC0415

    a = [line for line in primary.splitlines() if line.strip()]
    b = [line for line in secondary.splitlines() if line.strip()]
    ka = [_line_key(line) for line in a]
    kb = [_line_key(line) for line in b]
    out: list[str] = []

    def merge_block(ia1: int, ia2: int, jb1: int, jb2: int) -> None:
        # Сопоставляем жадно: для каждой строки второго текста — лучшая непарная строка первого.
        pairs: dict[int, int] = {}
        taken: set[int] = set()
        for j in range(jb1, jb2):
            best_i, best_r = -1, 0.0
            for i in range(ia1, ia2):
                if i in taken or not kb[j] or not ka[i]:
                    continue
                ratio = difflib.SequenceMatcher(None, ka[i], kb[j]).ratio()
                if ratio > best_r:
                    best_i, best_r = i, ratio
            if best_i >= 0 and best_r >= similar:
                pairs[best_i] = j
                taken.add(best_i)
        emitted_b = jb1
        for i in range(ia1, ia2):
            if i in pairs:
                j = pairs[i]
                while emitted_b < j:  # несопоставленные строки второго текста перед парой
                    if emitted_b not in pairs.values() and kb[emitted_b]:
                        out.append(b[emitted_b])
                    emitted_b += 1
                out.append(_vote_pair(a[i], b[j]))
                emitted_b = j + 1
            else:
                out.append(a[i])
        while emitted_b < jb2:
            if emitted_b not in pairs.values() and kb[emitted_b]:
                out.append(b[emitted_b])
            emitted_b += 1

    matcher = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                out.append(_vote_pair(a[i], b[j]))
        else:
            merge_block(i1, i2, j1, j2)
    return "\n".join(out)


_DESCRIPTION_START_RE = re.compile(r"\ball\s+that\b", re.IGNORECASE)
_DESCRIPTION_END_RE = re.compile(r"point\s+or\s+place\s+of\s+beginn\w*", re.IGNORECASE)
_BEGINNING_RE = re.compile(r"\bbeginn\w*\b", re.IGNORECASE)
_DESCRIPTION_KEY_RE = re.compile(
    r"\b(all\s+that|beginn\w*|thence|running|feet|foot|degree\w*|minute\w*|second\w*|distant|"
    r"formerly|known\s+as|corner|intersection|point\s+or\s+place|north\w*|south\w*|east\w*|west\w*|"
    r"continuation|land\s+now|coordinates|along|thereof|premises)\b|°",
    re.IGNORECASE,
)


def description_key_lines(text: str) -> str:
    """Строки описания участка для кеша: блок от «ALL that certain» до «point or place of BEGINNING»,
    без шапки документа и номеров страниц. Если блок не найден, возвращается пустая строка —
    тогда кеш сохраняет полный текст, а не теряет данные."""
    lines = [line for line in text.splitlines() if line.strip()]
    start = next((i for i, line in enumerate(lines) if _DESCRIPTION_START_RE.search(line)), None)
    if start is None:
        start = next((i for i, line in enumerate(lines) if _BEGINNING_RE.search(line)), None)
    if start is None:
        return ""
    end = next(
        (j for j in range(start, len(lines)) if _DESCRIPTION_END_RE.search(lines[j])), len(lines) - 1,
    )
    kept = []
    for line in lines[start:end + 1]:
        alnum = len(re.sub(r"\W", "", line))
        if _DESCRIPTION_KEY_RE.search(line) or (alnum >= 4 and re.search(r"\d", line)):
            kept.append(line)
    return "\n".join(kept)


def _page_tiles(page: Any, dpi: int, tile: int, folder: Path) -> list[Path]:
    """Куски страницы в порядке чтения (слева направо, сверху вниз)."""
    import pymupdf  # noqa: PLC0415 - уже зависимость программы

    grid = max(1, int(tile))
    rect = page.rect
    paths: list[Path] = []
    for row in range(grid):
        for col in range(grid):
            clip = pymupdf.Rect(
                rect.x0 + rect.width * col / grid,
                rect.y0 + rect.height * row / grid,
                rect.x0 + rect.width * (col + 1) / grid,
                rect.y0 + rect.height * (row + 1) / grid,
            )
            pixmap = page.get_pixmap(dpi=int(dpi), clip=clip)
            out = folder / f"p{page.number + 1}_{row}{col}.png"
            pixmap.save(str(out))
            paths.append(out)
    return paths


def ocr_pdf_pages(
    path: str | Path,
    first: int,
    last: int,
    *,
    dpi: int = DEFAULT_DPI,
    progress: Progress | None = None,
    use_cache: bool = True,
    tile: int = 1,
    exact_dpi: bool = False,
) -> str:
    """
    Распознаёт страницы ``first``–``last`` PDF и возвращает их текст.

    Кэш по файлу и страницам: повторный запуск (правка подписей, второй проход)
    не тратит ни секунды. Ошибки движка и рендера — ``OcrError``/``OcrUnavailable``.

    ``tile`` — на сколько кусков резать страницу перед распознаванием. Аналог
    «600 dpi», но честный: мелкий шрифт становится крупнее для узнавателя, а
    время растёт пропорционально числу кусков. Обычно 1; второй проход с 2×2
    включается, только если страница не читается целиком.
    """
    pdf = Path(path)
    first, last = int(first), int(last)
    if first < 1 or last < first:
        raise OcrError(f"неверные страницы для распознавания: {first}–{last}")
    key = _cache_key(pdf, first, last, int(dpi), int(tile), bool(exact_dpi))
    if use_cache:
        cached = _read_cache(key)
        if cached is not None:
            if progress:
                progress(
                    "страницы уже распознаны раньше — беру из кэша ("
                    + describe_provenance(cache_provenance(key)) + ")"
                )
            return cached
    engine = _engine_or_raise()
    try:  # noqa: PLC0415 - PyMuPDF уже зависимость программы
        import pymupdf
    except ImportError as exc:  # pragma: no cover - PyMuPDF обязателен
        raise OcrError(f"нет PyMuPDF для рендера страниц: {exc}") from exc
    try:
        doc = pymupdf.open(str(pdf))
    except Exception as exc:  # noqa: BLE001
        raise OcrError(f"не удалось открыть «{pdf.name}»: {exc}") from exc
    parts: list[str] = []
    try:
        total = len(doc)
        if last > total:
            last = total
        for page_no in range(first, last + 1):
            page = doc.load_page(page_no - 1)
            # Выше собственного разрешения картинки рендерить незачем: только пиксели растягиваем.
            # exact_dpi: эскалация просит именно dpi. Потолок по нативному разрешению
            # скана режет рендер (OUR 70: 219 вместо 400) — и OCR теряет строки.
            page_dpi = int(dpi) if exact_dpi else page_render_dpi(page, dpi)
            if progress:
                where = "целиком" if int(tile) <= 1 else f"кусками {int(tile)}×{int(tile)}"
                progress(
                    f"распознаю страницу {page_no} из {last} (OCR, локально, {page_dpi} dpi, {where})…"
                )
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    images = _page_tiles(page, page_dpi, tile, Path(tmp))
                except Exception as exc:  # noqa: BLE001
                    raise OcrError(f"страница {page_no} не рендерится: {exc}") from exc
                page_parts: list[str] = []
                for image in images:
                    try:
                        result, _elapsed = engine(str(image))
                    except Exception as exc:  # noqa: BLE001
                        raise OcrError(f"страница {page_no} не распознана: {exc}") from exc
                    text = _lines_to_text(result)
                    if text.strip():
                        page_parts.append(text)
            if page_parts:
                parts.append("\n".join(page_parts))
    finally:
        doc.close()
    if not parts:
        raise OcrError("на этих страницах ничего не распознано")
    text = restore_word_breaks("\n".join(parts))
    if use_cache:
        _write_cache(
            key, text,
            info={"файл": pdf.name, "страницы": f"{first}–{last}", "dpi": int(dpi)},
        )
    return text
