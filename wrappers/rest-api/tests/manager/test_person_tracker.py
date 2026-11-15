# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Unit tests for host-side person tracking, on synthetic frames. No device needed."""

import json

import numpy as np

from app.services import person_tracker as pt
from app.services.person_tracker import IdentityStore, PersonTracker, appearance, similarity

W, H = 640, 480
BOX_W, BOX_H = 80, 200

# Torso and legs colors (RGB)
RED_BLUE = ((220, 30, 30), (30, 40, 200))
GREEN_BLACK = ((30, 200, 60), (15, 15, 15))
WHITE_GREY = ((240, 240, 240), (120, 120, 120))


def person(x, clothes, y=150, depth=2.0):
    """A person dict at left edge x, with its clothes for drawing."""
    return {"bbox": [x, y, x + BOX_W, y + BOX_H], "score": 90, "distance_m": depth,
            "world": [(x + BOX_W / 2 - W / 2) / 300 * depth, 0.0, depth], "_clothes": clothes}


def frame(persons):
    """Image with each person drawn as torso + legs over a mid-grey background, back to front."""
    img = np.full((H, W, 3), 90, np.uint8)
    for p in sorted(persons, key=lambda p: -p["world"][2]):
        x1, y1, x2, y2 = p["bbox"]
        mid = y1 + BOX_H // 2
        img[y1:mid, x1:x2] = p["_clothes"][0]
        img[mid:y2, x1:x2] = p["_clothes"][1]
    return img


def step(tracker, persons):
    img = frame(persons)
    dets = [{k: v for k, v in p.items() if k != "_clothes"} for p in persons]
    tracker.update(dets, [appearance(img, d["bbox"]) for d in dets])
    return dets


def ids(dets):
    return [d["track_id"] for d in dets]


def test_appearance_separates_clothes_including_greys():
    a = appearance(frame([person(100, RED_BLUE)]), [100, 150, 180, 350])
    a2 = appearance(frame([person(300, RED_BLUE)]), [300, 150, 380, 350])
    b = appearance(frame([person(100, GREEN_BLACK)]), [100, 150, 180, 350])
    c = appearance(frame([person(100, WHITE_GREY)]), [100, 150, 180, 350])
    assert similarity(a, a2) > 0.95
    assert similarity(a, b) < 0.3 and similarity(b, c) < 0.3
    assert appearance(frame([]), [0, 0, 4, 4]) is None  # too small


def test_new_track_is_reported_after_confirmation_and_keeps_its_id():
    t = PersonTracker(IdentityStore(None))
    seen = [ids(step(t, [person(100 + 5 * i, RED_BLUE)]))[0] for i in range(10)]
    assert seen[:pt.CONFIRM_HITS - 1] == [None] * (pt.CONFIRM_HITS - 1)
    assert len(set(seen[pt.CONFIRM_HITS - 1:])) == 1 and seen[-1] is not None


def test_ids_survive_two_people_crossing():
    t = PersonTracker(IdentityStore(None))
    first = None
    for i in range(30):
        a = person(100 + 15 * i, RED_BLUE, depth=2.0)
        b = person(500 - 15 * i, GREEN_BLACK, depth=3.0)
        dets = step(t, [a, b])
        if i == 5:
            first = ids(dets)
    assert ids(dets) == first  # same list order: a then b


def test_person_who_leaves_and_returns_gets_the_same_id():
    t = PersonTracker(IdentityStore(None))
    for i in range(10):
        dets = step(t, [person(100, RED_BLUE), person(400, GREEN_BLACK)])
    a_id, b_id = ids(dets)
    for _ in range(pt.MAX_MISSES + 20):  # a is gone long enough to count as lost
        step(t, [person(400, GREEN_BLACK)])
    dets = step(t, [person(250, RED_BLUE), person(400, GREEN_BLACK)])  # back, somewhere else
    assert ids(dets) == [a_id, b_id]


def test_new_person_gets_a_new_id():
    t = PersonTracker(IdentityStore(None))
    for _ in range(5):
        dets = step(t, [person(100, RED_BLUE)])
    for _ in range(5):
        dets = step(t, [person(100, RED_BLUE), person(400, WHITE_GREY)])
    assert dets[1]["track_id"] not in (None, dets[0]["track_id"])


def test_name_is_saved_and_recognized_after_restart(tmp_path):
    path = tmp_path / "ids.json"
    t = PersonTracker(IdentityStore(path))
    for i in range(10):
        dets = step(t, [person(100 + 3 * i, RED_BLUE)])
    assert t.set_name(dets[0]["track_id"], "Alice")
    assert dets[0]["name"] is None  # names show from the next frame
    assert step(t, [person(130, RED_BLUE)])[0]["name"] == "Alice"
    assert list(json.loads(path.read_text())["identities"]) == ["Alice"]

    restarted = PersonTracker(IdentityStore(path))
    assert [x["name"] for x in restarted.tracks()] == ["Alice"]
    assert step(restarted, [person(300, RED_BLUE)])[0]["name"] == "Alice"
    assert step(restarted, [person(500, GREEN_BLACK)])[0]["name"] is None


def test_naming_a_new_track_takes_over_a_lost_track_with_that_name():
    t = PersonTracker(IdentityStore(None))
    for _ in range(5):
        dets = step(t, [person(100, RED_BLUE)])
    t.set_name(dets[0]["track_id"], "Bob")
    for _ in range(5):
        dets = step(t, [person(400, GREEN_BLACK)])
    t.set_name(dets[0]["track_id"], "Bob")
    assert [x["name"] for x in t.tracks()] == ["Bob"]


def test_rename_untag_and_forget(tmp_path):
    store = IdentityStore(tmp_path / "ids.json")
    t = PersonTracker(store)
    for _ in range(5):
        dets = step(t, [person(100, RED_BLUE)])
    tid = dets[0]["track_id"]
    t.set_name(tid, "Carol")
    t.set_name(tid, "Caroline")
    assert store.names() == ["Caroline"]
    t.set_name(tid, None)
    assert store.names() == [] and t.tracks()[0]["name"] is None
    t.set_name(tid, "Dan")
    assert t.forget("Dan") and store.names() == []
    assert t.tracks()[0]["track_id"] == tid  # still tracked, just unnamed
    assert not t.set_name(9999, "Nobody")


def test_unreadable_identities_file_is_ignored(tmp_path):
    path = tmp_path / "ids.json"
    path.write_text("not json")
    assert IdentityStore(path).names() == []


def test_brief_unnamed_tracks_drop_off_the_list_once_gone():
    t = PersonTracker(IdentityStore(None))
    for _ in range(pt.CONFIRM_HITS):
        step(t, [person(100, RED_BLUE), person(400, GREEN_BLACK)])
    for _ in range(pt.LIST_HITS):
        step(t, [person(400, GREEN_BLACK)])
    for _ in range(pt.MAX_MISSES + 1):
        step(t, [])
    listed = t.tracks()  # only the one tracked for long enough
    assert len(listed) == 1 and not listed[0]["present"]
