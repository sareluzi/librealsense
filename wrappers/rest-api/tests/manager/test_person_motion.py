# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Unit tests for person velocity (Kalman filter on 3D position). No device needed."""

import numpy as np
import pytest

from app.services import person_motion as pm
from app.services.person_motion import BoxSmoother, CameraMotion, Motion

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


def test_speed_is_3d():
    v = walk((0.6, 0.0, 0.8), noise=0.0)
    assert v["speed_mps"] == pytest.approx(1.0, abs=0.02)
    v = walk((0.0, 0.5, 0.0), noise=0.0)  # straight up or down counts too
    assert v["speed_mps"] == pytest.approx(0.5, abs=0.02) and v["vy"] == pytest.approx(0.5, abs=0.02)


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



def imu(camera, gyro=(0.004, -0.002, 0.001), accel=(1.1, -9.65, -1.6), seconds=2.0, start=0.0):
    """Feed the IMU at 200 Hz from `start`; returns the end time."""
    t = start
    for i in range(int(seconds * 200)):
        t = start + i / 200
        camera.on_gyro(gyro, t)
        camera.on_accel(accel, t)
    return t


def test_camera_at_rest_is_not_moving():
    c = CameraMotion()
    assert c.moving(0.0) is None  # no IMU yet
    t = imu(c)
    assert c.moving(t) is False


def test_camera_turning_or_carried_is_moving_then_settles():
    c = CameraMotion()
    t = imu(c)
    t = imu(c, gyro=(0.0, 0.3, 0.0), seconds=0.5, start=t)  # panning at ~17 deg/s
    assert c.moving(t) is True
    t = imu(c, seconds=pm.MOVING_HOLD_S + 0.5, start=t)
    assert c.moving(t) is False
    t = imu(c, accel=(1.1, -9.65 + 1.0, -1.6), seconds=0.2, start=t)  # lifted
    assert c.moving(t) is True


def test_camera_tilted_to_a_new_rest_settles():
    c = CameraMotion()
    t = imu(c)
    t = imu(c, accel=(3.3, -9.2, -1.6), seconds=pm.GRAVITY_TAU_S * 6, start=t)  # new orientation, at rest
    assert c.moving(t) is False


def test_imu_silence_is_unknown():
    c = CameraMotion()
    t = imu(c)
    assert c.moving(t + pm.IMU_TIMEOUT_S + 0.1) is None


def smooth_track(x_of_t, seconds=4.0, noise=3.0, seed=1):
    """Center x of a 100 px box at x_of_t(t), with edge noise (px), through a BoxSmoother at 30 fps."""
    rng = np.random.default_rng(seed)
    s = BoxSmoother()
    out = []
    for i in range(int(seconds * FPS)):
        t = i / FPS
        x = x_of_t(t)
        box, _ = s.update([x + rng.normal(0, noise), 100, x + 100 + rng.normal(0, noise), 400], None, 1000 + t)
        out.append((box[0] + box[2]) / 2 - (x + 50))  # error vs the true center
    return np.array(out)


def test_box_smoother_steadies_a_still_box():
    # Raw, the center moves ~2.4 px a frame with this noise
    assert np.mean(np.abs(np.diff(smooth_track(lambda t: 300)[30:]))) < 0.8


def test_box_smoother_follows_walking_and_jumps_quickly():
    assert abs(np.mean(smooth_track(lambda t: 100 + 400 * t)[30:])) < 10  # 400 px/s: under ~25 ms behind
    err = smooth_track(lambda t: 100 if t < 2 else 250, noise=0)
    jump = 2 * FPS
    assert abs(err[jump + 2]) < 10 and abs(err[jump + 4]) < 2


def test_box_smoother_predicts_a_moving_box():
    s = BoxSmoother()
    for i in range(30):
        s.update([100 + 10 * i, 0, 200 + 10 * i, 100], 2.0, i / FPS)  # 300 px/s
    assert s.predict(30 / FPS)[0] == pytest.approx(400, abs=5)
