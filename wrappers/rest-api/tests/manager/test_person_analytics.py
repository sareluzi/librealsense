# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Unit tests for person analytics: OD frame -> person_detections payload. No device needed."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import pyrealsense2 as rs
from fastapi.testclient import TestClient

from app.services.person_analytics import PersonAnalytics, distance_stats, read_persons, suppress
from app.services.rs_manager import RealSenseManager


def _det(box, score=90, depth=2.0, class_id=0):
    return SimpleNamespace(class_id=class_id, score=score, depth=depth, top_left_x=box[0], top_left_y=box[1],
                           bottom_right_x=box[2], bottom_right_y=box[3])


class _OdFrame:
    def __init__(self, dets, frame_number=7, timestamp=1000.0):
        self.dets, self._n, self._ts = dets, frame_number, timestamp

    def get_detection_count(self):
        return len(self.dets)

    def get_detection(self, i):
        return self.dets[i]

    def get_frame_number(self):
        return self._n

    def get_timestamp(self):
        return self._ts

    def get_profile(self):
        return SimpleNamespace(stream_type=lambda: rs.stream.object_detection)

    def as_object_detection_frame(self):
        return self


class _ColorFrame:
    def __init__(self, w=1280, h=720, frame_number=55):
        self.w, self.h, self.n = w, h, frame_number

    def as_video_frame(self):
        return self

    def get_width(self):
        return self.w

    def get_height(self):
        return self.h

    def get_frame_number(self):
        return self.n

    def get_data(self):
        return np.zeros((self.h, self.w, 3), np.uint8)


def test_read_persons_keeps_class_0_and_maps_invalid_depth_to_none():
    persons = read_persons(_OdFrame([_det([0, 0, 10, 10]), _det([5, 5, 9, 9], class_id=1),
                                     _det([20, 20, 30, 30], depth=0.0)]))
    assert [p["bbox"] for p in persons] == [[0, 0, 10, 10], [20, 20, 30, 30]]
    assert persons[0]["distance_m"] == 2.0 and persons[1]["distance_m"] is None
    assert persons[0]["world"] is None


def test_read_persons_world_position_when_com_valid():
    d = _det([0, 0, 10, 10])
    d.center_of_mass_valid = 1
    d.world_position = SimpleNamespace(x=0.1, y=-0.2, z=2.5)
    assert read_persons(_OdFrame([d]))[0]["world"] == pytest.approx([0.1, -0.2, 2.5])


def test_suppress_drops_low_scores_and_overlaps_keeping_highest():
    persons = [
        {"bbox": [0, 0, 100, 100], "score": 60},
        {"bbox": [5, 5, 100, 100], "score": 90},     # overlaps the first, higher score -> kept
        {"bbox": [200, 0, 300, 100], "score": 50},   # separate -> kept
        {"bbox": [400, 0, 500, 100], "score": 30},   # below threshold
    ]
    kept = suppress(persons, min_score=45, max_iou=0.55)
    assert [p["score"] for p in kept] == [90, 50]


def test_distance_stats_ignores_unknown_distances():
    stats = distance_stats([{"distance_m": 1.0}, {"distance_m": 3.0}, {"distance_m": None}])
    assert stats == {"count": 3, "min_m": 1.0, "max_m": 3.0, "avg_m": 2.0}
    assert distance_stats([]) == {"count": 0, "min_m": None, "max_m": None, "avg_m": None}


def test_on_od_frame_emits_payload_and_remembers_image_size():
    emit = MagicMock()
    pa = PersonAnalytics(emit)
    payload = pa.on_od_frame("dev", _OdFrame([_det([0, 0, 10, 10])]), _ColorFrame())
    emit.assert_called_once_with("person_detections", payload)
    assert payload["image"] == {"width": 1280, "height": 720}
    assert payload["color_frame_number"] == 55 and payload["od_frame_number"] == 7
    assert payload["stats"]["count"] == 1
    assert payload["persons"][0]["track_id"] is None  # tracked, not confirmed yet
    # An OD frame without a matching color frame keeps the last known size for box scaling
    assert pa.on_od_frame("dev", _OdFrame([], frame_number=8), None)["image"] == {"width": 1280, "height": 720}


def test_on_od_frame_skips_repeated_frame_number():
    emit = MagicMock()
    pa = PersonAnalytics(emit)
    assert pa.on_od_frame("dev", _OdFrame([], frame_number=3), None) is not None
    assert pa.on_od_frame("dev", _OdFrame([], frame_number=3), _ColorFrame()) is None
    assert emit.call_count == 1


def test_sensor_mode_routes_od_frame_to_analytics_and_queues_nothing():
    mgr = RealSenseManager(MagicMock())
    mgr.person_analytics = MagicMock()
    od = _OdFrame([_det([0, 0, 10, 10])])
    mgr._get_frame_metadata = MagicMock(return_value={})
    frame, _ = mgr._process_sensor_frame(od, "dev", colorizer=None)
    assert frame is None
    mgr.person_analytics.on_od_frame.assert_called_once_with("dev", od, None)


def test_config_endpoint_round_trip(monkeypatch):
    from main import app
    import app.api.dependencies as dependencies
    mgr = RealSenseManager(MagicMock())
    mgr.get_device = MagicMock()
    monkeypatch.setattr(dependencies, "_realsense_manager", mgr)
    client = TestClient(app)
    url = "/api/v1/devices/dev/person-analytics/config"
    assert client.get(url).json() == {"nms_score": 45, "nms_iou": 0.55, "tracking": True}
    new = {"nms_score": 60, "nms_iou": 0.4, "tracking": False}
    assert client.put(url, json=new).json() == new
    assert client.get(url).json() == new
    assert client.put(url, json={"nms_score": 101, "nms_iou": 0.4}).status_code == 422


def test_tracking_endpoints(monkeypatch, tmp_path):
    from main import app
    import app.api.dependencies as dependencies
    from app.services.person_analytics import PersonAnalytics
    mgr = RealSenseManager(MagicMock())
    mgr.get_device = MagicMock()
    mgr.person_analytics = PersonAnalytics(MagicMock(), tmp_path / "ids.json")
    monkeypatch.setattr(dependencies, "_realsense_manager", mgr)
    client = TestClient(app)
    for n in range(5):
        mgr.person_analytics.on_od_frame("dev", _OdFrame([_det([10, 10, 100, 300])], frame_number=n), _ColorFrame())
    url = "/api/v1/devices/dev/person-analytics"
    tracks = client.get(f"{url}/tracks").json()
    assert [t["present"] for t in tracks] == [True]
    tid = tracks[0]["track_id"]
    assert client.put(f"{url}/tracks/{tid}", json={"name": " Alice "}).json()[0]["name"] == "Alice"
    assert client.get(f"{url}/identities").json() == ["Alice"]
    assert client.put(f"{url}/tracks/999", json={"name": "X"}).status_code == 404
    assert client.delete(f"{url}/identities/Alice").status_code == 200
    assert client.get(f"{url}/identities").json() == []
    assert client.delete(f"{url}/identities/Alice").status_code == 404
