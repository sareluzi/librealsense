# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Host person detection: pre/post-processing, the latest-frame worker, and running without ONNX Runtime
or the model. No device needed; the model test runs only where the model file is installed."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.models.person_analytics import PersonAnalyticsConfig
from app.services import person_detector as pd
from app.services.person_analytics import PersonAnalytics

MODEL = Path.home() / ".realsense" / "models" / "damo-epoch233-mean_ap0.668_inp_416_batch_1_bench.onnx"


def heads(cells):
    """Raw model outputs (strides 8/16/32) with a person at each (level, row, col, score, ltrb in bins)."""
    out = []
    for size in (52, 26, 13):
        score = np.zeros((1, 1, size, size), np.float32)
        reg = np.full((1, 44, size, size), -20, np.float32)  # every bin unlikely ...
        reg[0, ::11] = 20  # ... except bin 0: zero distance
        out += [score, reg]
    for level, row, col, s, ltrb in cells:
        score, reg = out[2 * level], out[2 * level + 1]
        score[0, 0, row, col] = s
        for side, b in enumerate(ltrb):
            reg[0, side * 11:(side + 1) * 11, row, col] = -20
            reg[0, side * 11 + b, row, col] = 20
    return out


def test_decode_boxes_and_scores_in_image_pixels():
    # stride 16 cell (row 10, col 13) = center (208, 160) in the 416 input; ltrb 2/3/4/5 bins x16
    found = pd.decode(heads([(1, 10, 13, 0.9, (2, 3, 4, 5))]), 832, 416)
    assert len(found) == 1
    box, score = found[0]
    assert score == 90
    assert box == [(208 - 32) * 2, 160 - 48, (208 + 64) * 2, 160 + 80]  # x scaled 2x back to the image


def test_decode_drops_low_scores_and_overlaps():
    found = pd.decode(heads([(1, 10, 13, 0.9, (3, 3, 3, 3)), (1, 10, 14, 0.6, (3, 3, 3, 3)),  # overlapping
                             (2, 2, 2, 0.5, (1, 1, 1, 1)), (0, 5, 5, 0.01, (2, 2, 2, 2))]), 416, 416)
    assert [s for _, s in found] == [90, 50]


def test_nms_matches_greedy_reference():
    rng = np.random.default_rng(1)
    xy = rng.uniform(0, 300, (60, 2))
    boxes = np.hstack([xy, xy + rng.uniform(20, 120, (60, 2))]).astype(np.float32)
    scores = rng.uniform(0, 1, 60).astype(np.float32)
    keep, done = [], np.zeros(60, bool)
    for i in sorted(range(60), key=lambda i: (-scores[i], i)):
        if done[i]:
            continue
        keep.append(i)
        for j in range(60):
            iw = max(0, min(boxes[i, 2], boxes[j, 2]) - max(boxes[i, 0], boxes[j, 0]))
            ih = max(0, min(boxes[i, 3], boxes[j, 3]) - max(boxes[i, 1], boxes[j, 1]))
            a = lambda k: (boxes[k, 2] - boxes[k, 0]) * (boxes[k, 3] - boxes[k, 1])
            if j != i and iw * ih / (a(i) + a(j) - iw * ih) > 0.65:
                done[j] = True
    assert pd.nms(boxes, scores, 0.65) == keep


def test_preprocess_with_and_without_opencv(monkeypatch):
    rgb = np.random.default_rng(0).integers(0, 256, (720, 1280, 3), dtype=np.uint8)
    monkeypatch.setattr(pd, "cv2", None)
    x = pd.preprocess(rgb)
    assert x.shape == (1, 3, 416, 416) and x.dtype == np.float32 and 0 <= x.min() and x.max() <= 1
    cv2 = pytest.importorskip("cv2")
    monkeypatch.setattr(pd, "cv2", cv2)
    assert np.abs(pd.preprocess(rgb) - x).max() * 255 <= 1.01  # numpy resize within 1 gray level of cv2's


def test_status_without_onnx_runtime_or_model(monkeypatch, tmp_path):
    monkeypatch.setattr(pd, "ort", None)
    s = pd.status(MODEL)
    assert not s["available"] and "ONNX Runtime" in s["reason"] and s["devices"] == []
    monkeypatch.setattr(pd, "ort", SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"]))
    s = pd.status(tmp_path / "missing.onnx")
    assert not s["available"] and "missing.onnx" in s["reason"]
    (tmp_path / "m.onnx").write_bytes(b"x")
    assert pd.status(tmp_path / "m.onnx") == {"available": True, "devices": ["cpu"], "reason": None}


def test_worker_processes_the_newest_item_only():
    seen, gate = [], threading.Event()
    w = pd.LatestFrameWorker(lambda i: (gate.wait(1), seen.append(i)), "test")
    for i in range(5):
        w.submit(i)  # the first may already be running; the rest replace each other
        time.sleep(0.01)
    gate.set()
    time.sleep(0.2)
    w.stop()
    assert seen[-1] == 4 and len(seen) <= 2


@pytest.mark.skipif(pd.ort is None or not MODEL.is_file(), reason="needs ONNX Runtime and the model")
def test_model_runs_on_an_image():
    found = pd.HostDetector(MODEL).detect(np.zeros((720, 1280, 3), np.uint8))
    assert all(0 <= s <= 100 for _, s in found)


# ---- The host path through PersonAnalytics ----

class _Intr(SimpleNamespace):
    pass


def _video(data, fx=600.0, frame_number=5, units=0.001):
    h, w = data.shape[:2]
    intr = _Intr(width=w, height=h, fx=fx, fy=fx, ppx=w / 2, ppy=h / 2)
    profile = SimpleNamespace(as_video_stream_profile=lambda: SimpleNamespace(get_intrinsics=lambda: intr))
    f = SimpleNamespace(get_data=lambda: data, get_width=lambda: w, get_height=lambda: h,
                        get_profile=lambda: profile, get_frame_number=lambda: frame_number,
                        get_timestamp=lambda: 1000.0, get_units=lambda: units)
    f.as_video_frame = f.as_depth_frame = lambda: f
    return f


def _host_analytics(found):
    emit = MagicMock()
    pa = PersonAnalytics(emit)
    pa.set_config("dev", PersonAnalyticsConfig(detection="host"))
    detector = SimpleNamespace(device="cpu", detect=lambda rgb: found)
    return pa, emit, detector


def test_host_detection_publishes_with_the_cameras_distance_and_com():
    depth = np.full((480, 640), 4000, np.uint16)
    depth[100:420, 250:390] = 1500  # a person in front of a wall
    pa, emit, det = _host_analytics([([230, 90, 410, 430], 88)])
    pa._detect_on_host("dev", det, _video(np.zeros((480, 640, 3), np.uint8)), _video(depth))
    payload = emit.call_args[0][1]
    assert payload["source"] == "host" and payload["host_device"] == "cpu" and payload["host_detection_ms"] >= 0
    p = payload["persons"][0]
    assert p["distance_m"] == pytest.approx(1.5, abs=0.05) and p["com_px"] and p["world"][2] == pytest.approx(1.5, abs=0.02)  # 30 mm depth bins


def test_host_detection_without_aligned_depth_has_no_distance():
    pa, emit, det = _host_analytics([([230, 90, 410, 430], 88)])
    other_size = _video(np.full((240, 320), 1500, np.uint16))
    pa._detect_on_host("dev", det, _video(np.zeros((480, 640, 3), np.uint8)), other_size)
    assert emit.call_args[0][1]["persons"][0]["distance_m"] is None


def test_camera_od_frames_are_ignored_while_detecting_on_the_host():
    pa, emit, _ = _host_analytics([])
    assert pa.on_od_frame("dev", SimpleNamespace(get_frame_number=lambda: 1), None) is None
    emit.assert_not_called()


def test_host_detection_falls_back_to_the_camera_when_it_cannot_load(monkeypatch):
    pa, _, _ = _host_analytics([])
    monkeypatch.setattr(pd, "HostDetector", MagicMock(side_effect=RuntimeError("no runtime")))
    pa.on_host_frame("dev", object(), None)
    assert pa.get_config("dev").detection == "camera" and not pa.wants_host_frames("dev")


def test_config_rejects_host_detection_when_unavailable(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    import app.api.dependencies as dependencies
    from app.services.rs_manager import RealSenseManager
    mgr = RealSenseManager(MagicMock())
    mgr.get_device = MagicMock()
    monkeypatch.setattr(dependencies, "_realsense_manager", mgr)
    client = TestClient(app)
    base = "/api/v1/devices/dev/person-analytics"
    monkeypatch.setattr(pd, "status", lambda model: {"available": False, "devices": [], "reason": "no runtime"})
    assert client.get(f"{base}/host-detection").json() == {"available": False, "devices": [], "reason": "no runtime"}
    r = client.put(f"{base}/config", json={"detection": "host"})
    assert r.status_code == 400 and "no runtime" in r.text
    monkeypatch.setattr(pd, "status", lambda model: {"available": True, "devices": ["cpu"], "reason": None})
    assert client.put(f"{base}/config", json={"detection": "host", "host_device": "gpu"}).status_code == 400
    assert client.put(f"{base}/config", json={"detection": "host"}).json()["detection"] == "host"
