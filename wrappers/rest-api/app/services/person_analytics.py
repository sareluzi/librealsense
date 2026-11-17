# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Turns on-camera object detection frames into per-person results for clients.

Each OD frame yields one Socket.IO ``person_detections`` event: persons only, after the same
non-maximum suppression the C++ viewer applies, with distance stats and, with tracking on, the
track ID, name and velocity of each person (see person_tracker and person_motion).
"""

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from app.models.person_analytics import PersonAnalyticsConfig
from app.services import person_detector
from app.services.person_com import Intrinsics, person_com
from app.services.person_detector import LatestFrameWorker
from app.services.person_motion import CameraMotion
from app.services.person_tracker import SCORE_HYSTERESIS, IdentityStore, PersonTracker, appearance, iou

logger = logging.getLogger(__name__)

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

    def __init__(self, emit: Callable[[str, Dict[str, Any]], None], identities_file: Optional[Path] = None,
                 detector_model: Optional[Path] = None):
        self._emit = emit
        self._lock = threading.Lock()
        self._configs: Dict[str, PersonAnalyticsConfig] = {}
        self._identities = IdentityStore(identities_file)
        self._trackers: Dict[str, PersonTracker] = {}
        self._camera_motion: Dict[str, CameraMotion] = {}
        self._images: Dict[str, Dict[str, Any]] = {}  # last color size and intrinsics; OD may come without color
        self._last_od_numbers: Dict[str, int] = {}
        self.busy_s = 0.0  # host CPU time spent turning detections into results, for the usage report
        self.detection_busy_s = 0.0  # ... and detecting persons on the host
        self._detector_model = detector_model or Path()
        self._host: Dict[str, Tuple[str, Any, LatestFrameWorker]] = {}  # device -> (host device, detector, worker)

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
        if self._last_od_numbers.get(device_id) == od_frame.get_frame_number() or self.wants_host_frames(device_id):
            return None
        self._last_od_numbers[device_id] = od_frame.get_frame_number()
        return self._publish(device_id, read_persons(od_frame), od_frame.get_frame_number(), od_frame.get_timestamp(),
                             color_frame, {"source": "camera"})

    # ---- Detection on the host (person_detector) instead of the camera ----

    def host_detection_status(self) -> Dict[str, Any]:
        return person_detector.status(self._detector_model)

    def wants_host_frames(self, device_id: str) -> bool:
        return self.get_config(device_id).detection == "host"

    def on_host_frame(self, device_id: str, color_frame: Any, depth_frame: Optional[Any]) -> None:
        """A color frame (and depth aligned to it) for host detection; frames arriving while it is busy are skipped."""
        worker = self._host_worker(device_id)
        if worker:
            worker.submit((color_frame, depth_frame))

    def stop_host(self, device_id: str) -> None:
        with self._lock:
            entry = self._host.pop(device_id, None)
        if entry:
            entry[2].stop()

    def _host_worker(self, device_id: str) -> Optional[LatestFrameWorker]:
        want = self.get_config(device_id).host_device
        with self._lock:
            entry = self._host.get(device_id)
        if entry and entry[0] == want:
            return entry[2]
        self.stop_host(device_id)
        try:
            detector = person_detector.HostDetector(self._detector_model, want)
        except Exception as e:
            # Back to the camera's detection, which the client sees in the config
            logger.error("Host person detection unavailable: %s", e)
            self.set_config(device_id, self.get_config(device_id).model_copy(update={"detection": "camera"}))
            return None
        worker = LatestFrameWorker(lambda item: self._detect_on_host(device_id, detector, *item),
                                   f"host-detection-{device_id}")
        with self._lock:
            self._host[device_id] = (want, detector, worker)
        return worker

    def _detect_on_host(self, device_id: str, detector: Any, color_frame: Any, depth_frame: Optional[Any]) -> None:
        start, wall = time.thread_time(), time.perf_counter()
        vf = color_frame.as_video_frame()
        persons = [{"bbox": b, "score": s, "distance_m": None, "world": None, "com_px": None}
                   for b, s in detector.detect(np.asanyarray(vf.get_data()))]
        depth = _aligned_depth_mm(depth_frame, vf) if depth_frame else None
        if depth is not None:
            # Distance and center of mass as the camera calculates them; only for boxes that can be reported
            cfg = self.get_config(device_id)
            persons = suppress(persons, cfg.nms_score - SCORE_HYSTERESIS, cfg.nms_iou)
            i = vf.get_profile().as_video_stream_profile().get_intrinsics()
            k = Intrinsics(i.fx, i.fy, i.ppx, i.ppy)
            for p in persons:
                p["distance_m"], p["world"], p["com_px"] = person_com(depth, p["bbox"], k)
        self.detection_busy_s += time.thread_time() - start
        self._publish(device_id, persons, color_frame.get_frame_number(), color_frame.get_timestamp(), color_frame,
                      {"source": "host", "host_device": detector.device,
                       "host_detection_ms": round((time.perf_counter() - wall) * 1000, 1)})

    def _publish(self, device_id: str, persons: List[Dict[str, Any]], frame_number: int, timestamp_ms: float,
                 color_frame: Optional[Any], extra: Dict[str, Any]) -> Dict[str, Any]:
        """NMS, tracking and stats over one frame's detections, emitted as a person_detections event."""
        start = time.thread_time()  # CPU time: waiting for the GIL does not count
        cfg = self.get_config(device_id)
        smooth = cfg.tracking and cfg.smoothing
        # With smoothing, the tracker keeps confirmed persons scored a little below the minimum
        persons = suppress(persons, cfg.nms_score - (SCORE_HYSTERESIS if smooth else 0), cfg.nms_iou)
        payload: Dict[str, Any] = {
            "device_id": device_id,
            "od_frame_number": frame_number,
            "od_timestamp_ms": timestamp_ms,
            "server_time": time.time(),
            "color_frame_number": None,
            "image": None,
            "persons": persons,
            "stats": None,
            # True while the camera itself moves (velocities are then relative to it), None without IMU
            "camera_moving": None,
            **extra,
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
            persons = self.tracker(device_id).update(persons, features, positions, timestamp_ms / 1000,
                                                     cfg.nms_score, smooth)
            if not cfg.velocity:
                for p in persons:
                    p["velocity"] = None
        payload["persons"], payload["stats"] = persons, distance_stats(persons)
        self.busy_s += time.thread_time() - start
        self._emit(EVENT_NAME, payload)
        return payload


def _aligned_depth_mm(depth_frame: Any, color: Any) -> Optional[np.ndarray]:
    """Depth in mm when aligned to the color image (alignment gives it the color size and intrinsics), else None."""
    d = depth_frame.as_depth_frame()
    di = d.get_profile().as_video_stream_profile().get_intrinsics()
    ci = color.get_profile().as_video_stream_profile().get_intrinsics()
    if (di.width, di.height, di.fx, di.ppx) != (ci.width, ci.height, ci.fx, ci.ppx):
        return None
    depth = np.asanyarray(d.get_data())
    units = d.get_units()
    return depth if abs(units - 0.001) < 1e-6 else (depth * (units * 1000)).astype(np.uint16)
