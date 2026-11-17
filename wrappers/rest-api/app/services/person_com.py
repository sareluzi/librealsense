# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""A person's center of mass and distance from aligned depth, on the host.

A numpy port of common/utilities/com/center-of-mass.cpp (the viewer's "Application" distance, which
mirrors the camera's own calculation), so host detections get the same COM and distance as the
camera's. Arithmetic is float32 like the C++, and tests compare it with results of the C++ code;
keep the two in step when either changes.
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

F = np.float32

SUBTRACT_FROM_DEPTH = 400
SCALE_DEPTH = F(30.0)
MIN_DEPTH = 400
MAX_DEPTH = 8000
NO_DEPTH = 10000
GOOD_DEPTH_RATIO = F(0.3)
MIN_CLUSTER_FRACT = F(0.15)
NEARBY_REJECT_RATIO = F(3.0)
NEARBY_REJECT_RANGE_D8U = 35
NEARBY_REJECT_DEPTH_RATIO = F(2.2)
COM_UPPER_FRACTION = F(0.65)
COM_CENTROID_FRACTION = F(0.40)
NUM_DEPTH_SAMPLES = 5
MIN_CENTER_FILL_RATE = F(0.25)
CENTER_REGION_DIV = 6
MAX_PLAUSIBLE_WIDTH_MM = F(2000.0)
MAX_DEPTH_8U = math.ceil((MAX_DEPTH - SUBTRACT_FROM_DEPTH) / float(SCALE_DEPTH))


@dataclass
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float


@dataclass
class CenterOfMass:
    mean_body_depth: float = 0.0           # Euclidean distance to the person, mm; 0 = unreliable
    world_pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # camera coordinates, mm
    image_pos: Tuple[float, float] = (0.0, 0.0)             # COM pixel


def depth_8u(depth_mm: np.ndarray) -> np.ndarray:
    """create_depth_8u: 16-bit depth (mm) to the scaled 8-bit form calculate() uses."""
    t = np.maximum(depth_mm.astype(np.int32) - SUBTRACT_FROM_DEPTH, 0).astype(F)
    v = (t * F(1.0 / SCALE_DEPTH) + F(0.5)).astype(np.int32)
    return np.minimum(v, 255).astype(np.uint8)


def _pixel_to_camera(px: int, py: int, depth: F, k: Intrinsics) -> Tuple[F, F, F]:
    return ((F(px) - F(k.cx)) * depth / F(k.fx), (F(py) - F(k.cy)) * depth / F(k.fy), depth)


def _mean_surrounding_depth(depth: np.ndarray, x: int, y: int, interval: int, min_range: int, max_range: int,
                            fraction_non_zero: F = F(0.1), max_range_surrounding: int = 500) -> int:
    h, w = depth.shape
    interval = min(interval, 5)
    x0, x1 = max(x - interval, 2), min(x + interval, w - 2)
    y0, y1 = max(y - interval, 2), min(y + interval, h - 2)
    if x1 <= x0 or y1 <= y0:
        return 0
    vals = depth[y0:y1 + 1, x0:x1 + 1].astype(np.int64).ravel()
    vals = np.where((vals > min_range) & (vals < max_range), vals, 0)
    window = (2 * interval + 1) ** 2
    if np.count_nonzero(vals) < int(fraction_non_zero * F(window)):
        return 0
    nz = vals[vals > 0]
    if not nz.size:
        return 0
    mean = int(nz.sum() // nz.size)
    lo, hi = max(mean - max_range_surrounding, min_range + 1), min(mean + max_range_surrounding, max_range - 1)
    sel = vals[(vals >= lo) & (vals <= hi)]
    return int(sel.sum() // sel.size) if sel.size else 0


def _depth_at(depth: np.ndarray, px: F, py: F) -> int:
    h, w = depth.shape
    x = min(max(int(px + F(0.5)), 0), w - 1)
    y = min(max(int(py + F(0.5)), 0), h - 1)
    return _mean_surrounding_depth(depth, x, y, 5, 0, NO_DEPTH)


def _clamp_rect(x: int, y: int, w: int, h: int, img_w: int, img_h: int) -> Tuple[int, int, int, int]:
    ox, oy = max(0, x), max(0, y)
    x2, y2 = min(x + w, img_w) - 1, min(y + h, img_h) - 1
    return ox, oy, max(0, x2 - ox + 1), max(0, y2 - oy + 1)


def _hist_range_mean(hist: np.ndarray, start: int, end: int) -> F:
    s = n = F(0)
    for i in range(start, end + 1):  # sequential float32 sums, as in the C++
        s = F(s + hist[i] * F(i))
        n = F(n + hist[i])
    return F(s / n) if n > 0 else F(0)


def _center_of_mask(mask: np.ndarray, max_y: int) -> Optional[Tuple[int, int]]:
    h = mask.shape[0]
    if max_y <= 0 or max_y > h:
        max_y = h
    m = mask[:max_y]
    total = int(m.sum())
    if total == 0:
        return None
    cols = m.sum(axis=0)
    median_x = int(np.argmax(np.cumsum(cols) * 2 >= total))
    sum_y = int((m.sum(axis=1) * np.arange(max_y)).sum())
    return median_x, int(F(sum_y) / F(total))


def _com_with_depth_range(depth_mm: np.ndarray, roi: Tuple[int, int, int, int]) -> Optional[Tuple[F, int, int]]:
    rx, ry, rw, rh = roi
    if rw <= 0 or rh <= 0:
        return None
    data = depth_8u(depth_mm[ry:ry + rh, rx:rx + rw])  # per pixel, so only the ROI is converted
    hist_rows = max(1, int(F(rh) * COM_UPPER_FRACTION))
    size = MAX_DEPTH_8U + 1
    hist = np.bincount(data[:hist_rows].ravel(), minlength=256)[:size].astype(F)
    hist[0] = 0
    sum_el = int(hist.sum())
    if sum_el < int(GOOD_DEPTH_RATIO * F(rw) * F(hist_rows)):
        return None
    fract = (hist / F(sum_el)).astype(F)

    ranges: List[Tuple[int, int, F]] = []
    while True:
        loc = int(np.argmax(fract[1:])) + 1
        if float(fract[loc]) < 0.001:
            break
        end = loc + 1
        while end < size and fract[end] >= F(0.01):
            end += 1
        end = min(end, size - 1)
        start = loc - 1
        while start >= 1 and fract[start] >= F(0.01):
            start -= 1
        start = max(start, 1)
        f = F(0)
        for j in range(start, end + 1):
            f = F(f + fract[j])
            fract[j] = 0
        ranges.append((start, end, f))
    if not ranges:
        return None

    # The nearest cluster, unless it is small and a much larger one sits close behind it (clutter)
    best_start, best_end = ranges[0][0], ranges[0][1]
    best_mid = math.inf
    for i, (s, e, f) in enumerate(ranges):
        mid = (s + e) // 2
        if mid >= best_mid:
            continue
        if f < MIN_CLUSTER_FRACT:
            depth_i = F(mid) * SCALE_DEPTH + F(SUBTRACT_FROM_DEPTH)
            dominated = False
            for j, (sj, ej, fj) in enumerate(ranges):
                if j == i:
                    continue
                mid_j = (sj + ej) // 2
                depth_j = F(mid_j) * SCALE_DEPTH + F(SUBTRACT_FROM_DEPTH)
                nearby = abs(mid_j - mid) <= NEARBY_REJECT_RANGE_D8U or depth_j > NEARBY_REJECT_DEPTH_RATIO * depth_i
                if nearby and fj >= NEARBY_REJECT_RATIO * f:
                    dominated = True
                    break
            if dominated:
                continue
        best_mid, best_start, best_end = mid, s, e

    if best_end - best_start >= MAX_DEPTH_8U - 1:
        return None
    mean8 = _hist_range_mean(hist, best_start, best_end)
    for s, e, _ in ranges:  # extend to a slightly nearer head cluster
        if e < best_start:
            if mean8 - _hist_range_mean(hist, s, e) <= 5:
                best_start = s
                break
    mean8 = _hist_range_mean(hist, best_start, best_end)
    depth_mean = F(math.floor(F(mean8 * SCALE_DEPTH + F(SUBTRACT_FROM_DEPTH))))

    mask = ((data >= best_start) & (data <= best_end)).astype(np.int64)
    com = _center_of_mask(mask, max(1, int(F(rh) * COM_CENTROID_FRACTION)))
    if com is None:
        com = _center_of_mask(mask, max(1, int(F(rh) * COM_UPPER_FRACTION)))
        if com is None:
            return None
    return depth_mean, com[0] + rx, com[1] + ry


def _column_fallback(rect: Tuple[int, int, int, int], depth: np.ndarray, center: Tuple[F, F],
                     k: Optional[Intrinsics]) -> CenterOfMass:
    """run_non_range_com_calculation_flow: nearest valid depth along the box's center column."""
    x, y, w, h = rect
    cx, cy = center
    sy = F(y + 10) if y > cy else cy
    step = F(h) / F(NUM_DEPTH_SAMPLES + 3)
    chosen = F(0)

    def consider(d: int):
        nonlocal chosen
        if MIN_DEPTH < d <= MAX_DEPTH and (chosen == 0 or d < chosen):
            chosen = F(d)

    consider(_depth_at(depth, cx, sy))
    for i in range(NUM_DEPTH_SAMPLES + 3):
        consider(_depth_at(depth, cx, min(F(y) + F(i + 1) * step, F(y + h - 1))))
    out = CenterOfMass(mean_body_depth=float(chosen))
    if k and chosen > MIN_DEPTH:
        pt = _pixel_to_camera(int(cx + F(0.5)), int(cy + F(0.5)), chosen, k)
        out.world_pos = tuple(float(v) for v in pt)
        out.image_pos = (float(cx), float(cy))
        out.mean_body_depth = float(np.sqrt(F(pt[0] * pt[0] + pt[1] * pt[1] + pt[2] * pt[2])))
    return out


def calculate(depth_mm: np.ndarray, rect: Tuple[int, int, int, int], center: Tuple[float, float],
              k: Optional[Intrinsics]) -> CenterOfMass:
    """center_of_mass_calculator::calculate for aligned depth (no color-to-depth shift).

    rect is (x, y, width, height) and center the person's center, both in depth (= color) pixels.
    """
    roi = _clamp_rect(*rect, depth_mm.shape[1], depth_mm.shape[0])
    found = _com_with_depth_range(depth_mm, roi)
    center = (F(center[0]), F(center[1]))
    if found is None:
        return _column_fallback(rect, depth_mm, center, k)
    depth_mean, px, py = found
    out = CenterOfMass(mean_body_depth=0.0 if depth_mean <= MIN_DEPTH else float(depth_mean))
    if k and depth_mean > MIN_DEPTH:
        pt = _pixel_to_camera(px, py, depth_mean, k)
        out.world_pos = tuple(float(v) for v in pt)
        out.image_pos = (float(px), float(py))
        out.mean_body_depth = float(np.sqrt(F(pt[0] * pt[0] + pt[1] * pt[1] + pt[2] * pt[2])))
    return out


def is_minz_violation(depth_mm: np.ndarray, rect: Tuple[int, int, int, int], k: Intrinsics,
                      estimated_mm: float) -> bool:
    """Too close to measure: almost no depth at the box center, or a background-sized width."""
    if estimated_mm <= 0 or k.fx <= 0:
        return False
    x, y, w, h = rect
    hw, hh = max(1, w // CENTER_REGION_DIV), max(1, h // CENTER_REGION_DIV)
    cx, cy = x + w // 2, y + h // 2
    img_h, img_w = depth_mm.shape
    xs0, xs1 = max(cx - hw, 0), min(cx + hw, img_w)
    ys0, ys1 = max(cy - hh, 0), min(cy + hh, img_h)
    region = depth_mm[ys0:ys1, xs0:xs1]
    if region.size and F(np.count_nonzero(region)) / F(region.size) < MIN_CENTER_FILL_RATE:
        return True
    return F(estimated_mm) * F(w) / F(k.fx) > MAX_PLAUSIBLE_WIDTH_MM


def viewer_rect(bbox: Sequence[int], img_w: int, img_h: int):
    """The viewer's COM rect (x, y, w, h) and center for a box (x1, y1, x2, y2), or None when off the frame."""
    x1, y1, x2, y2 = (float(v) for v in bbox)
    bx, by = max(x1, 0.0), max(y1, 0.0)
    bw, bh = min(x2, img_w) - bx, min(y2, img_h) - by
    if bw <= 0 or bh <= 0:
        return None
    rx, ry = int(bx), int(by)
    rect = (rx, ry, int(F(bx + bw) + F(0.5)) - rx, int(F(by + bh) + F(0.5)) - ry)
    return rect, (F(bx) + F(bw) * F(0.5), F(by) + F(bh) * F(0.5))


def person_com(depth_mm: np.ndarray, bbox: Sequence[int], k: Intrinsics):
    """The viewer's flow for one detection box (x1, y1, x2, y2) over aligned depth (mm).

    Returns (distance m or None, world [x, y, z] m or None, COM pixel [x, y] or None).
    """
    r = viewer_rect(bbox, depth_mm.shape[1], depth_mm.shape[0])
    if r is None:
        return None, None, None
    com = calculate(depth_mm, r[0], r[1], k)
    if com.mean_body_depth <= 0 or is_minz_violation(depth_mm, r[0], k, com.mean_body_depth):
        return None, None, None
    return com.mean_body_depth / 1000, [v / 1000 for v in com.world_pos], list(com.image_pos)
