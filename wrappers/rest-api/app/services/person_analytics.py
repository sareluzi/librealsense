# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Turns on-camera object detection frames into per-person results for clients.

Each OD frame yields one Socket.IO ``person_detections`` event: persons only, after the same
non-maximum suppression the C++ viewer applies, with distance stats and, with tracking on, the
track ID, name and velocity of each person (see person_tracker and person_motion).
"""

import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from app.models.person_analytics import PersonAnalyticsConfig
from app.services.person_motion import CameraMotion
from app.services.person_tracker import SCORE_HYSTERESIS, IdentityStore, PersonTracker, appearance, iou

PERSON_CLASS_ID = 0
EVENT_NAME = "person_detections"


def read_persons(od_frame: Any) -> List[Dict[str, Any]]:
    """Person detections of an rs.object_detection_frame, as plain dicts."""
    persons = []
    for i in range(od_frame.get_detection_count()):
        d = od_frame.get_detection(i)
        if d.class_id != PERSON_CLASS_ID:
            continue
        # world_position/center_of_mass_* exist only in newer pyrealsense2 builds
        world = com = None
        if getattr(d, "center_of_mass_valid", 0):
            w = d.world_position
            world = [float(w.x), float(w.y), float(w.z)]
            com = [float(d.center_of_mass_x), float(d.center_of_mass_y)]
        persons.append({
            "bbox": [d.top_left_x, d.top_left_y, d.bottom_right_x, d.bottom_right_y],
            "score": d.score,
            "distance_m": float(d.depth) if d.depth > 0 else None,
            "world": world,  # the camera's 3D center of mass, m
            "com_px": com,   # ... and its pixel in the color image
        })
    return persons


def suppress(persons: List[Dict[str, Any]], min_score: int, max_iou: float) -> List[Dict[str, Any]]:
    """Drop low scores and boxes overlapping a higher-scored one (viewer NMS)."""
    kept: List[Dict[str, Any]] = []
    for p in sorted(persons, key=lambda p: p["score"], reverse=True):
        if p["score"] >= min_score and all(iou(p["bbox"], k["bbox"]) <= max_iou for k in kept):
            kept.append(p)
    return kept


def position(p: Dict[str, Any], intrinsics: Optional[Dict[str, float]]) -> Optional[List[float]]:
    """3D position (m) of a person: the camera's center of mass, else the box center at the camera's distance."""
    if p["world"]:
        return p["world"]
    if not intrinsics or p["distance_m"] is None:
        return None
    x1, y1, x2, y2 = p["bbox"]
    d = p["distance_m"]
    return [((x1 + x2) / 2 - intrinsics["ppx"]) / intrinsics["fx"] * d,
            ((y1 + y2) / 2 - intrinsics["ppy"]) / intrinsics["fy"] * d, d]


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

    def __init__(self, emit: Callable[[str, Dict[str, Any]], None], identities_file: Optional[Path] = None):
        self._emit = emit
        self._lock = threading.Lock()
        self._configs: Dict[str, PersonAnalyticsConfig] = {}
        self._identities = IdentityStore(identities_file)
        self._trackers: Dict[str, PersonTracker] = {}
        self._camera_motion: Dict[str, CameraMotion] = {}
        self._images: Dict[str, Dict[str, Any]] = {}  # last color size and intrinsics; OD may come without color
        self._last_od_numbers: Dict[str, int] = {}

    def get_config(self, device_id: str) -> PersonAnalyticsConfig:
        with self._lock:
            return self._configs.setdefault(device_id, PersonAnalyticsConfig())

    def set_config(self, device_id: str, config: PersonAnalyticsConfig) -> PersonAnalyticsConfig:
        with self._lock:
            self._configs[device_id] = config
        return config

    def tracker(self, device_id: str) -> PersonTracker:
        with self._lock:
            if device_id not in self._trackers:
                self._trackers[device_id] = PersonTracker(self._identities)
            return self._trackers[device_id]

    def identities(self) -> List[str]:
        return self._identities.names()

    def forget(self, name: str) -> bool:
        with self._lock:
            trackers = list(self._trackers.values())
        found = [t.forget(name) for t in trackers]  # each drops the name from its tracks
        return self._identities.delete(name) or any(found)

    def on_motion(self, device_id: str, stream: str, xyz: List[float]) -> None:
        """One IMU sample ("gyro" or "accel"); feeds the camera-movement detector."""
        motion = self._camera_motion.setdefault(device_id, CameraMotion())
        (motion.on_gyro if stream == "gyro" else motion.on_accel)(xyz, time.monotonic())

    def on_od_frame(self, device_id: str, od_frame: Any, color_frame: Optional[Any]) -> Optional[Dict[str, Any]]:
        """Process one OD frame (with its matching color frame, if any) and emit the result."""
        # The pipeline syncer hands the same OD frame out again with the next color frame
        if self._last_od_numbers.get(device_id) == od_frame.get_frame_number():
            return None
        self._last_od_numbers[device_id] = od_frame.get_frame_number()
        cfg = self.get_config(device_id)
        smooth = cfg.tracking and cfg.smoothing
        # With smoothing, the tracker keeps confirmed persons scored a little below the minimum
        persons = suppress(read_persons(od_frame), cfg.nms_score - (SCORE_HYSTERESIS if smooth else 0), cfg.nms_iou)
        payload: Dict[str, Any] = {
            "device_id": device_id,
            "od_frame_number": od_frame.get_frame_number(),
            "od_timestamp_ms": od_frame.get_timestamp(),
            "server_time": time.time(),
            "color_frame_number": None,
            "image": None,
            "persons": persons,
            "stats": None,
            # True while the camera itself moves (velocities are then relative to it), None without IMU
            "camera_moving": None,
        }
        if device_id in self._camera_motion:
            payload["camera_moving"] = self._camera_motion[device_id].moving(time.monotonic())
        image = None
        if color_frame:
            vf = color_frame.as_video_frame()
            i = vf.get_profile().as_video_stream_profile().get_intrinsics()
            self._images[device_id] = {"width": vf.get_width(), "height": vf.get_height(),
                                       "intrinsics": {"fx": i.fx, "fy": i.fy, "ppx": i.ppx, "ppy": i.ppy}}
            payload["color_frame_number"] = color_frame.get_frame_number()
            image = np.asanyarray(vf.get_data())
        payload["image"] = self._images.get(device_id)
        for p in persons:
            p["track_id"] = p["name"] = p["velocity"] = None
            p["held"] = False
        if cfg.tracking:
            intrinsics = payload["image"]["intrinsics"] if payload["image"] else None
            features = [appearance(image, p["bbox"]) if image is not None else None for p in persons]
            positions = [position(p, intrinsics) for p in persons]
            persons = self.tracker(device_id).update(persons, features, positions, od_frame.get_timestamp() / 1000,
                                                     cfg.nms_score, smooth)
            if not cfg.velocity:
                for p in persons:
                    p["velocity"] = None
        payload["persons"], payload["stats"] = persons, distance_stats(persons)
        self._emit(EVENT_NAME, payload)
        return payload
