# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""The host COM/distance port against the C++ it mirrors (common/utilities/com/center-of-mass.cpp).

data/person_com_golden.json holds the C++ results for scenes() (the viewer's flow per box: calculate()
then is_minz_violation()). Regenerate it with the C++ when either side changes: build a small program
that runs that flow on each scene and prints the fields below.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from app.services import person_com as pc

DATA = Path(__file__).parent / "data"
K = (600.0, 600.0, 320.0, 240.0)


def _scene(name, depth, boxes, k=K):
    return {"name": name, "depth": depth.astype(np.uint16), "boxes": np.array(boxes, np.int32),
            "intrinsics": np.array(k, np.float32)}


def scenes():
    """Synthetic edge cases for each branch of the C++, plus a real D585 frame (cropped around the person)."""
    rng = np.random.default_rng(7)
    W, H = 640, 480
    noise = lambda shape, sd: rng.normal(0, sd, shape)
    wall = lambda mm: np.full((H, W), mm, float) + noise((H, W), 15)
    out = []

    d = wall(4000)
    d[100:420, 250:390] = 1500 + noise((320, 140), 10)  # a person in front of a wall
    out.append(_scene("person_on_wall", d, [[230, 90, 410, 430]]))

    d = wall(3000)
    d[200:330, 300:340] = 2000 + noise((130, 40), 10)  # a small person in a wide box
    out.append(_scene("small_person_wide_box", d, [[150, 180, 500, 350]]))

    d = wall(5000)
    d[80:440, 240:400] = 2000 + noise((360, 160), 10)
    d[150:180, 300:320] = 800  # clutter in front of the body
    out.append(_scene("clutter_in_front", d, [[230, 70, 410, 450]]))

    d = wall(3500)
    d[150:400, 260:380] = 1800 + noise((250, 120), 8)
    d[100:150, 290:350] = 1720  # head slightly nearer than the body
    out.append(_scene("head_cluster", d, [[250, 95, 390, 405]]))

    d = np.zeros((H, W))
    d[::2, ::3] = 2200  # too sparse for the histogram: the column fallback
    out.append(_scene("sparse_fallback", d, [[200, 100, 360, 420]]))

    d = wall(2500)
    d[200:280, 280:360] = 0  # a hole at the box center: too close to measure
    out.append(_scene("minz_center_hole", d, [[220, 120, 420, 360]]))

    d = wall(6000)  # nothing but far background in a huge box: implausible width
    out.append(_scene("minz_wide_background", d, [[10, 10, 630, 470]]))

    d = wall(4000)
    d[50:480, 0:120] = 1300 + noise((430, 120), 10)  # a person cut by the image edge
    out.append(_scene("clipped_at_edge", d, [[-30, 40, 130, 500]]))

    d = np.clip(wall(9000), 0, 65535)  # beyond the max range
    out.append(_scene("out_of_range", d, [[200, 100, 400, 400]]))

    z = np.load(DATA / "person_com_real.npz")
    out.append(_scene("d585_aligned", z["depth"], z["boxes"], tuple(z["intrinsics"])))
    return out


def run_port(scene):
    """The port's flow per box, as the C++ golden program prints it."""
    depth = scene["depth"]
    k = pc.Intrinsics(*(float(v) for v in scene["intrinsics"]))
    rows = []
    for box in scene["boxes"]:
        rect, center = pc.viewer_rect(box, depth.shape[1], depth.shape[0])
        com = pc.calculate(depth, rect, center, k)
        rows.append([com.mean_body_depth, *com.world_pos, *com.image_pos,
                     int(pc.is_minz_violation(depth, rect, k, com.mean_body_depth))])
    return rows


@pytest.mark.parametrize("scene", scenes(), ids=lambda s: s["name"])
def test_port_matches_cpp(scene):
    golden = json.loads((DATA / "person_com_golden.json").read_text())[scene["name"]]
    for got, want in zip(run_port(scene), golden):
        assert got[6] == want[6]  # minZ flag
        assert got[:6] == pytest.approx(want[:6], rel=1e-5, abs=1e-3)


def test_depth_8u_matches_cpp_rounding():
    d = np.array([[0, 399, 400, 414, 415, 1000, 8000, 8050, 65535]], np.uint16)
    assert pc.depth_8u(d).tolist() == [[0, 0, 0, 0, 1, 20, 253, 255, 255]]


def test_person_com_reports_meters_or_none():
    s = scenes()[0]
    k = pc.Intrinsics(*K)
    dist, world, px = pc.person_com(s["depth"], s["boxes"][0], k)
    assert dist == pytest.approx(1.5, abs=0.05) and world[2] == pytest.approx(1.5, abs=0.05)
    assert 250 <= px[0] < 390 and 100 <= px[1] < 420
    hole = scenes()[5]
    assert pc.person_com(hole["depth"], hole["boxes"][0], k) == (None, None, None)
