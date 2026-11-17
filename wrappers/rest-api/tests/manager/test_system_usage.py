# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Unit tests for the host usage sampler. No device needed."""

import time
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.services import system_usage as su
from app.services.rs_manager import RealSenseManager

KEYS = {"cpu_percent", "system_cpu_percent", "cpu_cores", "memory_mb", "gpu_percent", "npu_percent",
        "perception_cpu_percent", "detection_cpu_percent", "alignment_cpu_percent"}


def _sample(monkeypatch, busy=None):
    monkeypatch.setattr(su, "INTERVAL_S", 0.05)
    usage = su.SystemUsage(busy)
    assert usage.get() == {}  # the first call starts the sampler
    for _ in range(100):
        time.sleep(0.02)
        if u := usage.get():
            return u
    raise AssertionError("no sample")


def test_reports_cpu_memory_and_each_part_of_the_work(monkeypatch):
    busy = [0.0]

    def spend():
        busy[0] += 0.01  # 10 ms of host processing per 50 ms sample
        return {"perception": busy[0], "detection": 2 * busy[0]}

    u = _sample(monkeypatch, spend)
    assert set(u) == KEYS
    assert u["cpu_cores"] >= 1 and u["memory_mb"] > 0 and u["cpu_percent"] >= 0
    assert 0 < u["perception_cpu_percent"] < u["detection_cpu_percent"] <= 100
    assert u["alignment_cpu_percent"] is None  # not reported by this caller


def test_unmeasurable_values_are_none(monkeypatch):
    # No psutil, not Windows (so no per-process GPU/NPU counters), no perception callback
    monkeypatch.setattr(su, "psutil", None)
    monkeypatch.setattr(su.platform, "system", lambda: "Linux")
    u = _sample(monkeypatch)
    assert u["cpu_percent"] is None and u["memory_mb"] is None and u["system_cpu_percent"] is None
    assert u["gpu_percent"] is None and u["npu_percent"] is None and u["perception_cpu_percent"] is None
    assert u["cpu_cores"] >= 1


def test_failing_gpu_counters_are_none(monkeypatch):
    def broken():
        raise OSError("no counters")
    monkeypatch.setattr(su, "_GpuEngines", broken)
    monkeypatch.setattr(su.platform, "system", lambda: "Windows")
    u = _sample(monkeypatch)
    assert u["gpu_percent"] is None and u["npu_percent"] is None


def test_sampler_stops_when_nobody_asks(monkeypatch):
    monkeypatch.setattr(su, "IDLE_STOP_S", 0.1)
    usage = su.SystemUsage()
    monkeypatch.setattr(su, "INTERVAL_S", 0.05)
    usage.get()
    time.sleep(0.5)
    assert usage._thread is None


def test_usage_endpoint(monkeypatch):
    from main import app
    import app.api.dependencies as dependencies
    monkeypatch.setattr(dependencies, "_realsense_manager", RealSenseManager(MagicMock()))
    body = TestClient(app).get("/api/v1/system/usage").json()
    assert set(body) == KEYS  # all null until the first sample
