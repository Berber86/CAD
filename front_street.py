"""
Шаг 0: угол фронтальной улицы нашего лота к северу — оценкой по снимку карты.

Север на снимке tax map смотрит строго вверх. Угол — ориентация линии
улицы **по часовой стрелке от севера**, в градусах ``[0, 180)``: 0 — улица
идёт север–юг, 90 — запад–восток (у линии нет направления, поэтому 180).

Тот же угол в том же запросе называет и Vision-модель (соседи по карте);
здесь — две независимые программные оценки, чтобы было что сравнивать:

1. ``estimate_hough_lines`` — отрезки прямых (Canny + Hough, OpenCV, если
   установлен): доминирующее семейство направлений, взвешенное по длине.
2. ``estimate_structure_tensor`` — поле градиентов яркости (только numpy):
   структурный тензор даёт главную ось линий без поиска самих отрезков.

На реальной карте всегда есть ДВА перпендикулярных семейства линий
(улицы и авеню, фасады и боковые межи), поэтому глобальное среднее по всем
направлениям бессмысленно — оценки ищут семейство, а не среднее. Какое из
двух семейств — фронтальное, подсказывает сам снимок: за фасадной стороной
выделенного голубым участка лежит улица — одна огромная связная область
без меж через весь блок, а за боковыми и задней — соседние участки,
каждый областью размером с наш (``_parcel_anchor``). Яркость заливки не
подсказывает: на живых картах участки такие же серые, как улица. Без
голубой подсветки оценка отрезков честно берёт доминирующее семейство
сетки, а тензорная может отказаться (``None``) — это лучше выдуманного
числа.

Клиент сам сравнивает значения (JSON-кэш — ``map_angle_cache.py``) и
применяет самое точное; программа выбор за него не делает.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

#: Минимальная согласованность направлений внутри семейства, ниже — не верим.
_MIN_AGREEMENT = 0.5
#: Доля общей длины отрезков, которую должно держать доминирующее семейство.
_MIN_CLUSTER_SHARE = 0.35
#: Когерентность тензора без привязки к участку (смешанные семейства гасят её).
_MIN_COHERENCE = 0.15
#: Когерентность тензора по градиентам одного семейства.
_MIN_COHERENCE_FAMILY = 0.30
#: Улица за фасадной стороной — связная область без меж площадью заметно
#: больше самого участка (полоса через весь блок); соседний участок даёт
#: область примерно его размера. Порог — между этими масштабами.
_STREET_REGION_MIN_RATIO = 1.5


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


def _axis_deg_from_bearing(bearing_deg: float) -> float:
    """Обратное преобразование: азимут -> угол оси от оси X картинки (y вниз)."""
    rad = math.radians(float(bearing_deg))
    return math.degrees(math.atan2(-math.cos(rad), math.sin(rad)))


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


def _dominant_cluster(angles, weights, half_width: float = 8.0):
    """
    Самое тяжёлое семейство направлений: окно ±``half_width`` градусов с
    наибольшей суммарной длиной. Возвращает (среднее, согласованность, доля).
    """
    total = float(sum(weights))
    if total <= 0:
        return 0.0, 0.0, 0.0
    best: tuple[float, float] | None = None  # (вес окна, центр)
    for center in angles:
        acc = 0.0
        for angle, weight in zip(angles, weights):
            if angle_difference(angle, center) <= half_width:
                acc += float(weight)
        if best is None or acc > best[0]:
            best = (acc, float(center))
    assert best is not None
    chosen = [(a, w) for a, w in zip(angles, weights)
              if angle_difference(a, best[1]) <= half_width]
    mean, agreement = _double_angle_mean([a for a, _ in chosen], [w for _, w in chosen])
    return mean, agreement, float(sum(w for _, w in chosen)) / total


def _parcel_anchor(image) -> tuple[tuple[int, int, int, int] | None, float | None]:
    """
    Голубой участок на снимке: (рамка, азимут фронтального семейства | None).

    Кандидаты в семейства — главная ось контура участка и ось поперёк неё.
    Фронтальное — то, за крайней стороной которого лежит не соседний
    участок (связная область без меж размером с наш), а улица: одна
    огромная область через весь блок. Проба яркостью здесь не работает:
    на живых tax map заливка участков такая же серая, как улица, и проба
    честно называла «улицей» боковые стороны (живой случай 25.11.2026,
    Reno Ave: обе оценки ушли на перпендикулярное семейство — 34° вместо
    125°). Угловой участок смотрит на улицу двумя семействами — берём то,
    у которого длиннее фасад. Не вышло — None: оценки тогда обойдутся без
    привязки к фасаду.
    """
    try:
        import numpy as np

        arr = np.asarray(image.convert("RGB")).astype(np.int32)
        if arr.ndim != 3 or arr.shape[2] < 3:
            return None, None
        red, green, blue = arr[..., 0], arr[..., 1], arr[..., 2]
        cyan = (green - red > 25) & (blue - red > 25) & (green > 120)
        if int(cyan.sum()) < 300:
            return None, None
        mask = cyan
        try:
            import cv2

            count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
                cyan.astype(np.uint8), 8)
            if count > 1:
                biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                mask = labels == biggest
                if int(mask.sum()) < 300:
                    return None, None
        except Exception:  # noqa: BLE001 - без OpenCV берём всю подсветку как есть
            pass
        ys, xs = np.nonzero(mask)
        bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))

        # Семейства-кандидаты: стороны описанного прямоугольника участка.
        # Тензор по маске здесь ненадёжен: подписи внутри участка и
        # скруглённые углы делают его почти изотропным.
        family_a: float | None = None
        try:
            import cv2

            contours, _hierarchy = cv2.findContours(
                mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                contour = max(contours, key=cv2.contourArea)
                points = np.asarray(
                    cv2.boxPoints(cv2.minAreaRect(contour)), dtype=np.float64,
                ).reshape(4, 2)
                for i in range(4):
                    x1, y1 = points[i]
                    x2, y2 = points[(i + 1) % 4]
                    if math.hypot(float(x2 - x1), float(y2 - y1)) >= 20.0:
                        family_a = _bearing_from_image_dir(
                            float(x2 - x1), float(y2 - y1))
                        break
        except Exception:  # noqa: BLE001 - нет OpenCV: семейства не знаем
            family_a = None
        if family_a is None:
            return bbox, None
        candidates = (family_a, normalize_line_angle(family_a + 90.0))

        luminance = arr.mean(axis=2)
        height, width = luminance.shape
        xf = xs.astype(np.float64)
        yf = ys.astype(np.float64)

        # Области без меж: тёмные линии (и подписи) — перегородки, чуть
        # расширенные, чтобы цифры размеров на линиях не пробивали дыр.
        try:
            import cv2

            sealed = cv2.dilate(
                (luminance < 120).astype(np.uint8), np.ones((5, 5), np.uint8))
            _count, regions, region_stats, _centroids = (
                cv2.connectedComponentsWithStats((1 - sealed).astype(np.uint8), 8))
        except Exception:  # noqa: BLE001 - нет OpenCV: фронта не знаем
            return bbox, None
        parcel_area = float(mask.sum())

        def _outside_region(family: float, proj_l, proj_n, lx, ly, nx, ny,
                            side: float) -> tuple[float, float]:
            """(площадь области за стороной, её доля в пробе); (0, 0) — нет данных."""
            extreme = float(proj_n.max() if side > 0 else proj_n.min())
            span = float(proj_l.max() - proj_l.min())
            labels: list[int] = []
            for offset in (6.0, 10.0, 14.0, 18.0, 22.0):
                for fraction in np.linspace(0.06, 0.94, 30):
                    along = float(proj_l.min()) + float(fraction) * span
                    normal_at = extreme + side * offset
                    px = int(round(lx * along + nx * normal_at))
                    py = int(round(ly * along + ny * normal_at))
                    if 0 <= px < width and 0 <= py < height:
                        label = int(regions[py, px])
                        if label > 0:
                            labels.append(label)
            if not labels:
                return 0.0, 0.0
            values, counts = np.unique(labels, return_counts=True)
            top = int(np.argmax(counts))
            area = float(region_stats[int(values[top]), cv2.CC_STAT_AREA])
            return area, float(counts[top]) / len(labels)

        best: tuple[float, float] | None = None  # (длина фасада, семейство)
        for family in candidates:
            rad = math.radians(family)
            lx, ly = math.sin(rad), -math.cos(rad)   # вдоль улицы
            nx, ny = math.cos(rad), math.sin(rad)    # поперёк улицы
            proj_l = xf * lx + yf * ly
            proj_n = xf * nx + yf * ny
            for side in (-1.0, 1.0):
                area, cover = _outside_region(family, proj_l, proj_n, lx, ly, nx, ny, side)
                if area >= _STREET_REGION_MIN_RATIO * parcel_area and cover >= 0.5:
                    extent = float(proj_l.max() - proj_l.min())
                    if best is None or extent > best[0]:
                        best = (extent, family)
                    break
        return bbox, (best[1] if best else None)
    except Exception:  # noqa: BLE001 - якорь необязателен, падение = без якоря
        return None, None


def _image_angle_from_axis_deg(axis_deg_from_x: float) -> float:
    """Угол оси от оси X картинки (y вниз) -> ориентация к северу."""
    rad = math.radians(axis_deg_from_x)
    return _bearing_from_image_dir(math.cos(rad), math.sin(rad))


def estimate_structure_tensor(image) -> Optional[float]:
    """
    Главная ориентация линий по полю градиентов (numpy, без OpenCV).

    Градиент поперёк линии, поэтому ось линий — главная ось тензора минус
    90°. Считаем по окрестности голубого участка и, если фронтальное
    семейство известно, только по градиентам поперёк него: смешанные
    семейства на реальной карте иначе дают ось «между» ними. Когерентность
    ниже порога — честный ``None``.
    """
    try:
        import numpy as np
        from PIL import ImageFilter

        bbox, front = _parcel_anchor(image)
        view = image
        if bbox is not None:
            x0, y0, x1, y1 = bbox
            pad_x = int(0.6 * max(1, x1 - x0))
            pad_y = int(0.6 * max(1, y1 - y0))
            view = image.crop((
                max(0, x0 - pad_x), max(0, y0 - pad_y),
                min(image.width, x1 + pad_x), min(image.height, y1 + pad_y),
            ))
        # Сглаживание обязательно: без него дискретные градиенты диагональных
        # линий дают систематический сдвиг оси (на синтетике — до 8°).
        gray = np.asarray(view.convert("L").filter(ImageFilter.GaussianBlur(2)), dtype=np.float64)
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
        gx_s, gy_s = gx[mask], gy[mask]
        weight = mag[mask] ** 2

        if front is not None:
            # Оставляем градиенты поперёк фронтального семейства (±20°).
            target_axis = (_axis_deg_from_bearing(front) + 90.0) % 180.0
            axes = np.degrees(np.arctan2(gy_s, gx_s)) % 180.0
            diff = np.abs(axes - target_axis) % 180.0
            diff = np.minimum(diff, 180.0 - diff)
            chosen = diff <= 20.0
            if float(weight[chosen].sum()) < 0.08 * float(weight.sum()):
                return None
            gx_s, gy_s, weight = gx_s[chosen], gy_s[chosen], weight[chosen]
            min_coherence = _MIN_COHERENCE_FAMILY
        else:
            min_coherence = _MIN_COHERENCE

        sxx = float(np.sum(weight * gx_s ** 2))
        syy = float(np.sum(weight * gy_s ** 2))
        sxy = float(np.sum(weight * gx_s * gy_s))
        total = sxx + syy
        if total <= 0:
            return None
        coherence = math.hypot(sxx - syy, 2.0 * sxy) / total
        if coherence < min_coherence:
            return None
        grad_axis = 0.5 * math.degrees(math.atan2(2.0 * sxy, sxx - syy))
        line_axis = grad_axis + 90.0
        return round(_image_angle_from_axis_deg(line_axis), 1)
    except Exception:  # noqa: BLE001 - оценка необязательна, падение = None
        return None


def estimate_hough_lines(image) -> Optional[float]:
    """
    Ориентация по отрезкам прямых (OpenCV Hough). Нет OpenCV — ``None``.

    Длинные отрезки весят больше: границы участков и бордюры улиц длиннее
    подписей. Берём не среднее по всем отрезкам (два перпендикулярных
    семейства его уводят), а отрезки фронтального семейства — если голубой
    участок нашёлся; иначе доминирующее семейство сетки. Слабая
    согласованность внутри семейства — ``None``.
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
        total_length = float(sum(weights))
        if not angles or total_length < 100.0:
            return None

        _bbox, front = _parcel_anchor(image)
        if front is not None:
            chosen = [(a, w) for a, w in zip(angles, weights)
                      if angle_difference(a, front) <= 12.0]
            chosen_length = float(sum(w for _, w in chosen))
            if chosen_length >= max(120.0, 0.10 * total_length):
                mean, agreement = _double_angle_mean(
                    [a for a, _ in chosen], [w for _, w in chosen])
                if agreement >= _MIN_AGREEMENT:
                    return round(mean, 1)
            # Якорь есть, а его семейство отрезками не подтверждается —
            # доминирующий кластер мог бы увести на поперечную улицу.
            return None

        mean, agreement, share = _dominant_cluster(angles, weights)
        if share < _MIN_CLUSTER_SHARE or agreement < _MIN_AGREEMENT:
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
