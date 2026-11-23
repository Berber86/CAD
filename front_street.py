"""
Шаг 0: угол фронтальной улицы нашего лота к северу — оценкой по снимку карты.

Север на снимке tax map смотрит строго вверх. Угол — ориентация линии
улицы **по часовой стрелке от севера**, в градусах ``[0, 180)``: 0 — улица
идёт север–юг, 90 — запад–восток (у линии нет направления, поэтому 180).

Тот же угол в том же запросе называет и Vision-модель (соседи по карте);
здесь — две независимые программные оценки, чтобы было что сравнивать:

1. ``estimate_hough_lines`` — отрезки прямых (Canny + Hough, OpenCV, если
   установлен): длина отрезков — вес в круговом среднем удвоенных углов.
2. ``estimate_structure_tensor`` — поле градиентов яркости (только numpy):
   структурный тензор даёт главную ось линий без поиска самих отрезков.

Клиент сам сравнивает значения (JSON-кэш — ``map_angle_cache.py``) и
применяет самое точное; программа выбор за него не делает. Любая оценка
может честно вернуть ``None`` — это лучше выдуманного числа.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

#: Минимальная «согласованность» направлений, ниже которой оценке не верим.
_MIN_AGREEMENT = 0.30
_MIN_COHERENCE = 0.15


def normalize_line_angle(deg: float) -> float:
    """Ориентация линии — в ``[0, 180)``: у линии нет направления."""
    return float(deg) % 180.0


def angle_difference(a: float, b: float) -> float:
    """Расстояние между ориентациями линий, градусы ``[0, 90]``."""
    diff = abs(normalize_line_angle(a) - normalize_line_angle(b)) % 180.0
    return min(diff, 180.0 - diff)


def _bearing_from_image_dir(dx: float, dy: float) -> float:
    """
    Направление (dx, dy) в координатах картинки (y вниз) -> градусы по
    часовой от севера (север — вверх, (0, -1)).
    """
    return normalize_line_angle(math.degrees(math.atan2(dx, -dy)))


def _double_angle_mean(angles_deg, weights) -> tuple[float, float]:
    """Круговое среднее ориентаций линий (углы удваиваются: период 180)."""
    sx = sy = total = 0.0
    for angle, weight in zip(angles_deg, weights):
        doubled = math.radians(2.0 * float(angle))
        sx += math.cos(doubled) * float(weight)
        sy += math.sin(doubled) * float(weight)
        total += float(weight)
    if total <= 0:
        return 0.0, 0.0
    agreement = math.hypot(sx, sy) / total
    mean = 0.5 * math.degrees(math.atan2(sy, sx))
    return normalize_line_angle(mean), agreement


def _image_angle_from_axis_deg(axis_deg_from_x: float) -> float:
    """Угол оси от оси X картинки (y вниз) -> ориентация к северу."""
    rad = math.radians(axis_deg_from_x)
    return _bearing_from_image_dir(math.cos(rad), math.sin(rad))


def estimate_structure_tensor(image) -> Optional[float]:
    """
    Главная ориентация линий по полю градиентов (numpy, без OpenCV).

    Градиент поперёк линии, поэтому ось линий — главная ось тензора минус
    90°. Когерентность тензора — мера того, что у картинки вообще есть одно
    главное направление; ниже порога честно возвращаем ``None``.
    """
    try:
        import numpy as np
        from PIL import ImageFilter

        # Сглаживание обязательно: без него дискретные градиенты диагональных
        # линий дают систематический сдвиг оси (на синтетике — до 8°).
        gray = np.asarray(image.convert("L").filter(ImageFilter.GaussianBlur(2)), dtype=np.float64)
        if gray.size == 0 or float(gray.max()) == float(gray.min()):
            return None
        gy, gx = np.gradient(gray)
        mag = np.hypot(gx, gy)
        if float(mag.max()) <= 0:
            return None
        # Только заметные границы: слабые градиенты — шум заливок и подписей.
        threshold = float(np.percentile(mag, 90))
        mask = mag >= max(threshold, 1e-9)
        if int(mask.sum()) < 50:
            return None
        w = mag[mask]
        sxx = float(np.sum(w * gx[mask] ** 2))
        syy = float(np.sum(w * gy[mask] ** 2))
        sxy = float(np.sum(w * gx[mask] * gy[mask]))
        total = sxx + syy
        if total <= 0:
            return None
        coherence = math.hypot(sxx - syy, 2.0 * sxy) / total
        if coherence < _MIN_COHERENCE:
            return None
        grad_axis = 0.5 * math.degrees(math.atan2(2.0 * sxy, sxx - syy))
        line_axis = grad_axis + 90.0
        return round(_image_angle_from_axis_deg(line_axis), 1)
    except Exception:  # noqa: BLE001 - оценка необязательна, падение = None
        return None


def estimate_hough_lines(image) -> Optional[float]:
    """
    Ориентация по отрезкам прямых (OpenCV Hough). Нет OpenCV — ``None``.

    Длинные отрезки весят больше: границы участков и линий улиц длиннее
    подписей. Согласованность направлений ниже порога — ``None``.
    """
    try:
        import cv2
        import numpy as np

        gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        height, width = gray.shape[:2]
        min_dim = max(1, min(height, width))
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        edges = cv2.Canny(blurred, 50, 150)
        segments = cv2.HoughLinesP(
            edges, 1.0, math.pi / 180.0,
            threshold=max(50, min_dim // 10),
            minLineLength=max(40, min_dim // 7),
            maxLineGap=12,
        )
        if segments is None or len(segments) == 0:
            return None
        angles: list[float] = []
        weights: list[float] = []
        # OpenCV 4: (N, 1, 4); OpenCV 5: (N, 4) — приводим к списку четвёрок.
        for x1, y1, x2, y2 in segments.reshape(-1, 4):
            dx, dy = float(x2 - x1), float(y2 - y1)
            length = math.hypot(dx, dy)
            if length <= 0:
                continue
            axis = math.degrees(math.atan2(dy, dx))
            angles.append(_image_angle_from_axis_deg(axis))
            weights.append(length)
        if not angles:
            return None
        mean, agreement = _double_angle_mean(angles, weights)
        if agreement < _MIN_AGREEMENT:
            return None
        return round(mean, 1)
    except Exception:  # noqa: BLE001 - нет OpenCV или отрезков: честный None
        return None


def estimate_all(image_path: str | Path) -> dict[str, Optional[float]]:
    """
    Все программные оценки угла по снимку карты.

    Ключи стабильны — по ним пишется JSON-кэш (``map_angle_cache.py``):
    ``python_hough_lines`` и ``python_structure_tensor``.
    """
    result: dict[str, Optional[float]] = {
        "python_hough_lines": None,
        "python_structure_tensor": None,
    }
    try:
        from PIL import Image

        with Image.open(image_path) as source:
            image = source.convert("RGB")
    except Exception:  # noqa: BLE001 - снимка нет: оценки нет, а не авария
        return result
    result["python_hough_lines"] = estimate_hough_lines(image)
    result["python_structure_tensor"] = estimate_structure_tensor(image)
    return result
