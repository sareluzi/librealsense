# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Live: a camera with on-camera object detection (e.g. D585) yields person_detections events."""

import time

import pytest

import main  # noqa: F401  (before pyrealsense2 loads: prefers the local SDK build, as the server does)
from app.models.stream import Resolution, StreamConfig

SECONDS = 4


@pytest.fixture
def od_camera():
    """(manager, device_id, color sensor+profile, OD sensor+profile, captured events), or skip."""
    from app.services.socketio import sio
    from app.services.rs_manager import RealSenseManager

    manager = RealSenseManager(sio)
    for dev in manager.get_devices():
        sensors = manager.get_sensors(dev.device_id)
        find = lambda t: next(((s, p) for s in sensors for p in s.supported_stream_profiles
                               if p.stream_type.lower() == t and (t != "color" or p.format.lower() == "rgb8")), None)
        od, color = find("object_detection"), find("color")
        if od and color:
            # Pair OD with a color profile at its rate
            color = next(((color[0], p) for p in color[0].supported_stream_profiles
                          if p.stream_type.lower() == "color" and p.format.lower() == "rgb8" and p.fps == od[1].fps), color)
            events = []
            manager.person_analytics._emit = lambda name, payload: events.append(payload)
            return manager, dev.device_id, color, od, events
    pytest.skip("No connected camera has an object detection stream")


def _check(events):
    assert len(events) >= 5 * SECONDS, f"only {len(events)} person_detections events in {SECONDS}s"
    numbers = [e["od_frame_number"] for e in events]
    assert len(numbers) == len(set(numbers)), "an OD frame was reported twice"
    for e in events:
        assert e["stats"]["count"] == len(e["persons"])
        w, h = e["image"]["width"], e["image"]["height"]
        for p in e["persons"]:
            x1, y1, x2, y2 = p["bbox"]
            assert 0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h, p
    # Tracking is on by default: anyone in view for a while gets a track ID
    if sum(1 for e in events if e["persons"]) > 10:
        assert any(p["track_id"] is not None for e in events for p in e["persons"]), "no person was tracked"


def test_person_detections_pipeline_mode(od_camera):
    manager, device_id, (cs, cp), (os_, op), events = od_camera
    cfg = lambda s, p: StreamConfig(sensor_id=s.sensor_id, stream_type=p.stream_type.lower(), format=p.format.lower(),
                                    resolution=Resolution(width=p.width, height=p.height), framerate=p.fps)
    manager.start_stream(device_id, [cfg(cs, cp), cfg(os_, op)])
    try:
        time.sleep(SECONDS)
    finally:
        manager.stop_stream(device_id)
    _check(events)


def test_person_detections_sensor_mode(od_camera):
    manager, device_id, (cs, cp), (os_, op), events = od_camera
    manager.start_sensor(device_id, cs.sensor_id, [cp])
    try:
        manager.start_sensor(device_id, os_.sensor_id, [op])
        time.sleep(SECONDS)
    finally:
        for s in (os_, cs):
            try:
                manager.stop_sensor(device_id, s.sensor_id)
            except Exception:
                pass
    _check(events)
    assert all(e["image"] for e in events), "OD frames were not paired with a color frame"
