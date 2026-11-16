# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Velocity of a tracked person, and whether the camera itself is moving.

Motion: a constant-velocity Kalman filter on a person's 3D position (x, y and z). Positions are in
camera coordinates (meters; x right, y down, z forward) and times come from the camera's frame
timestamps, so network or processing jitter does not show up as speed.

CameraMotion: from the camera's IMU. While the camera moves, person velocities are relative to it.
"""

import math
from typing import Any, Dict, Optional, Sequence

import numpy as np

POSITION_STD_M = 0.08   # measurement noise of the camera's person position
ACCEL_STD = 3.0         # how hard people speed up, turn or stop (m/s^2)
MAX_GAP_S = 1.0         # a longer gap (person was gone) restarts the filter
MIN_UPDATES = 8         # updates before the velocity is reported
STILL_MPS = 0.2         # speed below which a person counts as standing still
JUMP_M = 0.4            # a position farther than this (+ MAX_SPEED_MPS * dt) from the prediction is a glitch
MAX_SPEED_MPS = 3.0
MAX_JUMPS = 3           # this many glitches in a row: the person really is elsewhere, restart


class Motion:
    def __init__(self):
        self._pos: Optional[np.ndarray] = None
        self._vel = np.zeros(3)
        # The same noise applies to every axis and all axes update together, so one 2x2
        # covariance (position, velocity) serves x, y and z
        self._cov = np.eye(2)
        self._t = 0.0
        self.updates = 0
        self._jumps = 0

    def update(self, position: Sequence[float], t: float) -> None:
        """Add a measured position (m) taken at time `t` (s)."""
        z = np.asarray(position, dtype=float)
        dt = t - self._t
        if self._pos is not None and 0 < dt <= MAX_GAP_S:
            # The camera's position sometimes jumps to the background or another person for a frame
            if np.linalg.norm(z - (self._pos + self._vel * dt)) > JUMP_M + MAX_SPEED_MPS * dt:
                self._jumps += 1
                if self._jumps < MAX_JUMPS:
                    return
            else:
                self._jumps = 0
        if self._pos is None or not 0 < dt <= MAX_GAP_S or self._jumps:
            self._pos, self._vel = z, np.zeros(3)
            self._cov = np.diag([POSITION_STD_M ** 2, 1.0])
            self._t, self.updates, self._jumps = t, 1, 0
            return
        f = np.array([[1, dt], [0, 1]])
        q = ACCEL_STD ** 2 * np.array([[dt ** 4 / 4, dt ** 3 / 2], [dt ** 3 / 2, dt ** 2]])
        cov = f @ self._cov @ f.T + q
        gain = cov[:, 0] / (cov[0, 0] + POSITION_STD_M ** 2)
        residual = z - (self._pos + self._vel * dt)
        self._pos = self._pos + self._vel * dt + gain[0] * residual
        self._vel = self._vel + gain[1] * residual
        self._cov = cov - np.outer(gain, cov[0])
        self._t = t
        self.updates += 1

    def velocity(self) -> Optional[Dict[str, Any]]:
        """Filtered position and velocity, or None until the filter has settled."""
        if self.updates < MIN_UPDATES:
            return None
        vx, vy, vz = (float(v) for v in self._vel)
        speed = math.sqrt(vx * vx + vy * vy + vz * vz)
        return {
            "position": [round(float(p), 3) for p in self._pos],
            "vx": round(vx, 3), "vy": round(vy, 3), "vz": round(vz, 3),
            "speed_mps": round(speed, 3),
            # On the floor plane: 0 = straight away from the camera, 90 = to its right, 180 = toward it
            "heading_deg": round(math.degrees(math.atan2(vx, vz)), 1),
            "moving": speed >= STILL_MPS,
        }


GYRO_MOVING_RADPS = 0.06   # turning faster than ~3.4 deg/s
ACCEL_MOVING_MPS2 = 0.3    # acceleration beyond the slowly tracked gravity vector
GRAVITY_TAU_S = 1.0        # how fast the gravity estimate follows a new resting orientation
MOVING_HOLD_S = 1.0        # still reported as moving this long after the last sign of motion
IMU_TIMEOUT_S = 1.0        # no IMU sample for this long: unknown


class CameraMotion:
    """Whether the camera itself turns, shakes or is carried, from its gyro and accelerometer."""

    def __init__(self):
        self._gravity: Optional[np.ndarray] = None
        self._last_accel = 0.0
        self._last_sample = -math.inf
        self._last_motion = -math.inf

    def on_gyro(self, xyz: Sequence[float], t: float) -> None:
        """Angular velocity (rad/s) at time `t` (s)."""
        self._last_sample = t
        if np.linalg.norm(xyz) > GYRO_MOVING_RADPS:
            self._last_motion = t

    def on_accel(self, xyz: Sequence[float], t: float) -> None:
        """Acceleration (m/s^2, gravity included) at time `t` (s)."""
        a = np.asarray(xyz, dtype=float)
        self._last_sample = t
        if self._gravity is None:
            self._gravity, self._last_accel = a, t
            return
        k = min(1.0, max(0.0, t - self._last_accel) / GRAVITY_TAU_S)
        self._last_accel = t
        if np.linalg.norm(a - self._gravity) > ACCEL_MOVING_MPS2:
            self._last_motion = t
        self._gravity = self._gravity + k * (a - self._gravity)

    def moving(self, t: float) -> Optional[bool]:
        """True while the camera moves (and briefly after), None without recent IMU data."""
        if t - self._last_sample > IMU_TIMEOUT_S:
            return None
        return t - self._last_motion <= MOVING_HOLD_S
