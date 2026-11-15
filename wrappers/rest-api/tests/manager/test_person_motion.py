# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Unit tests for person velocity (Kalman filter on 3D position). No device needed."""

import numpy as np
import pytest

from app.services import person_motion as pm
from app.services.person_motion import Motion

FPS = 30


def walk(velocity, seconds=3.0, start=(0.0, 0.0, 3.0), noise=0.05, seed=1):
    """Feed a person walking at constant `velocity` (m/s), with position noise, at 30 fps."""
    rng = np.random.default_rng(seed)
    m = Motion()
    for i in range(int(seconds * FPS)):
        t = i / FPS
        m.update(np.add(start, np.multiply(velocity, t)) + rng.normal(0, noise, 3), 1000 + t)
    return m.velocity()


def test_walking_sideways():
    v = walk((1.0, 0.0, 0.0))
    assert v["speed_mps"] == pytest.approx(1.0, abs=0.1)
    assert v["heading_deg"] == pytest.approx(90, abs=8)
    assert v["moving"]


def test_walking_toward_the_camera():
    v = walk((0.0, 0.0, -1.2), start=(0.0, 0.0, 5.0))
    assert v["speed_mps"] == pytest.approx(1.2, abs=0.1)
    assert abs(v["heading_deg"]) == pytest.approx(180, abs=8)
    assert v["position"][2] == pytest.approx(5.0 - 1.2 * (3 - 1 / FPS), abs=0.1)


def test_standing_still_with_noise_is_not_moving():
    v = walk((0.0, 0.0, 0.0))
    assert not v["moving"] and v["speed_mps"] < pm.STILL_MPS


def test_vertical_motion_is_not_walking():
    assert walk((0.0, 0.5, 0.0), noise=0.0)["speed_mps"] == pytest.approx(0.0, abs=0.01)


def test_reported_only_once_settled_and_restarted_after_a_gap():
    m = Motion()
    for i in range(pm.MIN_UPDATES - 1):
        m.update([0.1 * i, 0, 3], i / FPS)
    assert m.velocity() is None
    m.update([0.1 * pm.MIN_UPDATES, 0, 3], pm.MIN_UPDATES / FPS)
    assert m.velocity() is not None
    m.update([5, 0, 3], pm.MIN_UPDATES / FPS + pm.MAX_GAP_S + 0.5)  # back after being gone
    assert m.velocity() is None and m.updates == 1


def test_a_one_frame_glitch_is_ignored_but_a_lasting_jump_restarts():
    m = Motion()
    for i in range(30):
        m.update([0, 0, 3], i / FPS)
    m.update([0, 0, 6], 30 / FPS)  # background for a frame
    m.update([0, 0, 3], 31 / FPS)
    assert m.velocity()["speed_mps"] < 0.05
    for i in range(32, 32 + pm.MAX_JUMPS):
        m.update([2, 0, 3], i / FPS)  # really somewhere else now
    assert m.updates == 1 and m.velocity() is None
