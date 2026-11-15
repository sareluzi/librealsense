# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Turns on-camera object detection frames into per-person results for clients.

Each OD frame yields one Socket.IO ``person_detections`` event: persons only, after the same
non-maximum suppression the C++ viewer applies, with distance stats.
"""

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from app.models.person_analytics import PersonAnalyticsConfig

PERSON_CLASS_ID = 0
EVENT_NAME = "person_detections"


def _iou(a: List[int], b: List[int]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def read_persons(od_frame: Any) -> List[Dict[str, Any]]:
    """Person detections of an rs.object_detection_frame, as plain dicts."""
    persons = []
    for i in range(od_frame.get_detection_count()):
        d = od_frame.get_detection(i)
        if d.class_id != PERSON_CLASS_ID:
            continue
        # world_position/center_of_mass_* exist only in newer pyrealsense2 builds
        world = None
        if getattr(d, "center_of_mass_valid", 0):
            w = d.world_position
            world = [float(w.x), float(w.y), float(w.z)]
        persons.append({
            "bbox": [d.top_left_x, d.top_left_y, d.bottom_right_x, d.bottom_right_y],
            "score": d.score,
            "distance_m": float(d.depth) if d.depth > 0 else None,
            "world": world,
        })
    return persons


def suppress(persons: List[Dict[str, Any]], min_score: int, max_iou: float) -> List[Dict[str, Any]]:
    """Drop low scores and boxes overlapping a higher-scored one (viewer NMS)."""
    kept: List[Dict[str, Any]] = []
    for p in sorted(persons, key=lambda p: p["score"], reverse=True):
        if p["score"] >= min_score and all(_iou(p["bbox"], k["bbox"]) <= max_iou for k in kept):
            kept.append(p)
    return kept


def distance_stats(persons: List[Dict[str, Any]]) -> Dict[str, Any]:
    dists = [p["distance_m"] for p in persons if p["distance_m"] is not None]
    return {
        "count": len(persons),
        "min_m": min(dists) if dists else None,
        "max_m": max(dists) if dists else None,
        "avg_m": sum(dists) / len(dists) if dists else None,
    }


class PersonAnalytics:
    """Per-device person results, emitted through ``emit(event, payload)``."""

    def __init__(self, emit: Callable[[str, Dict[str, Any]], None]):
        self._emit = emit
        self._lock = threading.Lock()
        self._configs: Dict[str, PersonAnalyticsConfig] = {}
        self._image_sizes: Dict[str, Dict[str, int]] = {}  # last color size; OD may come without color
        self._last_od_numbers: Dict[str, int] = {}

    def get_config(self, device_id: str) -> PersonAnalyticsConfig:
        with self._lock:
            return self._configs.setdefault(device_id, PersonAnalyticsConfig())

    def set_config(self, device_id: str, config: PersonAnalyticsConfig) -> PersonAnalyticsConfig:
        with self._lock:
            self._configs[device_id] = config
        return config

    def on_od_frame(self, device_id: str, od_frame: Any, color_frame: Optional[Any]) -> Optional[Dict[str, Any]]:
        """Process one OD frame (with its matching color frame, if any) and emit the result."""
        # The pipeline syncer hands the same OD frame out again with the next color frame
        if self._last_od_numbers.get(device_id) == od_frame.get_frame_number():
            return None
        self._last_od_numbers[device_id] = od_frame.get_frame_number()
        cfg = self.get_config(device_id)
        persons = suppress(read_persons(od_frame), cfg.nms_score, cfg.nms_iou)
        payload: Dict[str, Any] = {
            "device_id": device_id,
            "od_frame_number": od_frame.get_frame_number(),
            "od_timestamp_ms": od_frame.get_timestamp(),
            "server_time": time.time(),
            "color_frame_number": None,
            "image": None,
            "persons": persons,
            "stats": distance_stats(persons),
        }
        if color_frame:
            vf = color_frame.as_video_frame()
            self._image_sizes[device_id] = {"width": vf.get_width(), "height": vf.get_height()}
            payload["color_frame_number"] = color_frame.get_frame_number()
        payload["image"] = self._image_sizes.get(device_id)
        self._emit(EVENT_NAME, payload)
        return payload
