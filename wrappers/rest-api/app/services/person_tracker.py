# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Host-side person tracking: keeps an ID, and an optional name, on each person across frames.

Detections are matched to tracks by box overlap, 3D position and clothing color, so IDs survive
crossing people, short occlusions and detector box swaps. A person who leaves and comes back is
re-attached to their old track by clothing color alone. Named persons are saved, so they are
recognized again after a server restart.
"""

import json
import logging
import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from app.services.person_motion import BoxSmoother, Motion

# Clothing color feature, per body part (torso, legs): a hue x saturation histogram of colored
# pixels plus a brightness histogram of grey ones, so black/white/grey clothes count too
H_BINS, S_BINS, V_BINS = 16, 3, 4
GREY_SAT = 0.2      # below this saturation (or GREY_VAL brightness) a pixel counts as grey
GREY_VAL = 0.15
SAMPLES = 24        # each part is sampled on a SAMPLES x SAMPLES grid
PARTS = ((0.15, 0.5), (0.5, 0.9))  # torso and legs, as fractions of the box height

MAX_MISSES = 15     # OD frames (~0.5 s) a track may go unseen and still be matched by position
CONFIRM_HITS = 3    # matches before a new track gets reported
LIST_HITS = 30      # matches (~1 s) before an unnamed track stays listed once gone
LOST_TTL_S = 60.0   # an unnamed track unseen this long is forgotten
GALLERY = 30        # clothing features kept per track
MAX_COST = 0.75     # worst match cost still accepted
MIN_SIM = 0.6       # appearance needed to match a track whose box does not overlap
REID_SIM = 0.8      # appearance needed to re-attach a lost track or saved identity
MAX_STEP_M = 1.0    # 3D distance beyond which a detection is not the same person
SCORE_HYSTERESIS = 10  # with smoothing, a confirmed person stays while scored this far below the minimum
HOLD_FRAMES = 3     # with smoothing, a confirmed person's box stays this many missed OD frames (~0.1 s)
HOLD_MAX_IOU = 0.1  # ... unless the held box would overlap a reported one
FRAME_S = 1 / 30    # frame time assumed when no timestamps are given


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _rgb_to_hsv(rgb: np.ndarray):
    """(N, 3) floats in [0, 1] -> hue, saturation, value arrays in [0, 1]."""
    mx, mn = rgb.max(axis=1), rgb.min(axis=1)
    delta = mx - mn
    safe = np.where(delta > 0, delta, 1)
    r, g, b = rgb.T
    hue = np.select([mx == r, mx == g], [(g - b) / safe % 6, (b - r) / safe + 2], (r - g) / safe + 4) / 6
    hue = np.where(delta > 0, hue, 0)
    sat = np.where(mx > 0, delta / np.where(mx > 0, mx, 1), 0)
    return hue, sat, mx


def appearance(image: np.ndarray, bbox: Sequence[int]) -> Optional[np.ndarray]:
    """Clothing color feature of the person in `bbox` of an RGB image, or None if too small."""
    if image.ndim != 3 or image.shape[2] != 3:
        return None
    x1, y1 = max(0, bbox[0]), max(0, bbox[1])
    x2, y2 = min(image.shape[1], bbox[2]), min(image.shape[0], bbox[3])
    w, h = x2 - x1, y2 - y1
    if w < 8 or h < 16:
        return None
    # The middle 60% of the width: less background next to the body
    xs = np.linspace(x1 + 0.2 * w, x2 - 1 - 0.2 * w, SAMPLES).astype(int)
    parts = []
    for top, bottom in PARTS:
        ys = np.linspace(y1 + top * h, y1 + bottom * h - 1, SAMPLES).astype(int)
        hue, sat, val = _rgb_to_hsv(image[np.ix_(ys, xs)].reshape(-1, 3) / 255.0)
        colored = (sat >= GREY_SAT) & (val >= GREY_VAL)
        hist = np.concatenate([
            np.histogram2d(hue[colored], sat[colored], bins=[H_BINS, S_BINS], range=[[0, 1], [GREY_SAT, 1]])[0].ravel(),
            np.histogram(val[~colored], bins=V_BINS, range=(0, 1))[0],
        ])
        parts.append(hist / hist.sum())
    return np.concatenate(parts).astype(np.float32)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Bhattacharyya coefficient averaged over body parts: 1 = same colors, 0 = nothing in common."""
    return float(np.sqrt(a * b).sum()) / len(PARTS)


class IdentityStore:
    """Clothing galleries of named persons, in a JSON file (memory only when `path` is None)."""

    def __init__(self, path: Optional[Path]):
        self._path = path
        self._lock = threading.Lock()
        self._identities: Dict[str, List[List[float]]] = {}
        if path and path.exists():
            try:
                self._identities = json.loads(path.read_text())["identities"]
            except (ValueError, KeyError, OSError) as e:
                logging.warning("Ignoring unreadable person identities file %s: %s", path, e)

    def load(self) -> Dict[str, List[np.ndarray]]:
        with self._lock:
            return {n: [np.array(f, dtype=np.float32) for f in g] for n, g in self._identities.items()}

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._identities)

    def put(self, name: str, gallery: Sequence[np.ndarray]) -> None:
        with self._lock:
            self._identities[name] = [[round(float(x), 5) for x in f] for f in gallery]
            self._write()

    def delete(self, name: str) -> bool:
        with self._lock:
            found = self._identities.pop(name, None) is not None
            if found:
                self._write()
            return found

    def _write(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"version": 1, "identities": self._identities}))
        except OSError as e:
            logging.warning("Could not save person identities to %s: %s", self._path, e)


class Track:
    def __init__(self, track_id: int, name: Optional[str] = None, gallery: Sequence[np.ndarray] = ()):
        self.id = track_id
        self.name = name
        self.gallery: deque = deque(gallery, maxlen=GALLERY)
        self.bbox: Optional[List[int]] = None
        self.world: Optional[List[float]] = None
        self.motion = Motion()
        self.smoother = BoxSmoother()
        self.shown: Optional[Dict[str, Any]] = None  # last reported person, for holding it through a miss
        self.hits = 0
        self.misses = MAX_MISSES + 1 if gallery else 0  # saved identities start out lost
        self.last_seen = time.monotonic()

    @property
    def present(self) -> bool:
        return self.misses <= MAX_MISSES

    @property
    def confirmed(self) -> bool:
        return self.hits >= CONFIRM_HITS or self.name is not None

    def appearance_sim(self, feature: Optional[np.ndarray]) -> Optional[float]:
        """Mean of the 3 best matches in the gallery: robust to a few bad samples."""
        if feature is None or not self.gallery:
            return None
        best = sorted((similarity(feature, g) for g in self.gallery), reverse=True)[:3]
        return sum(best) / len(best)


class PersonTracker:
    """Tracks the persons of one camera. Thread safe: frames and REST calls come from different threads."""

    def __init__(self, identities: IdentityStore):
        self._identities = identities
        self._lock = threading.Lock()
        self._tracks: Dict[int, Track] = {}
        self._next_id = 1
        self._frames = 0
        for name, gallery in identities.load().items():
            self._add(Track(0, name, gallery))

    def _add(self, track: Track) -> Track:
        track.id = self._next_id
        self._next_id += 1
        self._tracks[track.id] = track
        return track

    @staticmethod
    def _cost(t: Track, p: Dict[str, Any], feature: Optional[np.ndarray]) -> Optional[float]:
        overlap = iou(t.bbox, p["bbox"]) if t.bbox else 0.0
        sim = t.appearance_sim(feature)
        if overlap == 0 and (sim is None or sim < MIN_SIM):
            return None
        step = math.dist(t.world, p["world"]) if t.world and p["world"] else None
        if step is not None and step > MAX_STEP_M:
            return None
        return (0.45 * (1 - overlap)
                + 0.2 * (step / MAX_STEP_M if step is not None else 0.5)
                + 0.35 * (1 - sim if sim is not None else 0.5))

    def update(self, persons: List[Dict[str, Any]], features: List[Optional[np.ndarray]],
               positions: Optional[List[Optional[List[float]]]] = None, when: Optional[float] = None,
               min_score: int = 0, smooth: bool = False) -> List[Dict[str, Any]]:
        """Match one OD frame's persons to tracks; sets each person's `track_id`, `name` and `velocity`.

        `positions` (3D, m) and `when` (camera time, s) of the persons feed each track's velocity.
        Persons scored below `min_score` only continue a confirmed track. With `smooth`, boxes and
        distances are smoothed and a confirmed person missed for a few frames is held (`held`: True).
        Returns the persons to report.
        """
        now = time.monotonic()
        self._frames += 1
        when = self._frames * FRAME_S if when is None else when
        with self._lock:
            assigned: Dict[int, Track] = {}  # person index -> track

            # 1. Present tracks, cheapest pairs first
            present = [t for t in self._tracks.values() if t.present]
            pairs = sorted((c, ti, di) for ti, t in enumerate(present) for di, p in enumerate(persons)
                           if (p["score"] >= min_score or t.confirmed)
                           and (c := self._cost(t, p, features[di])) is not None and c <= MAX_COST)
            used = set()
            for _, ti, di in pairs:
                if ti not in used and di not in assigned:
                    used.add(ti)
                    assigned[di] = present[ti]

            # 2. Persons left over: someone who left (or a saved identity) coming back
            for di in range(len(persons)):
                if di in assigned or features[di] is None or persons[di]["score"] < min_score:
                    continue
                lost = [(t.appearance_sim(features[di]) or 0, t) for t in self._tracks.values()
                        if not t.present and t not in assigned.values()]
                sim, best = max(lost, key=lambda x: x[0], default=(0, None))
                if best is not None and sim >= REID_SIM:
                    assigned[di] = best

            # 3. Everyone else is new
            for di in range(len(persons)):
                if di not in assigned and persons[di]["score"] >= min_score:
                    assigned[di] = self._add(Track(0))

            for di, t in assigned.items():
                p, f = persons[di], features[di]
                t.bbox, t.world = p["bbox"], p["world"]
                t.hits += 1
                t.misses = 0
                t.last_seen = now
                # Only clean samples: a box overlapping another person mixes their colors
                clean = all(iou(p["bbox"], q["bbox"]) < 0.1 for q in persons if q is not p)
                if f is not None and clean and (len(t.gallery) < 5 or t.hits % 3 == 0):
                    t.gallery.append(f)
                if positions and positions[di]:
                    t.motion.update(positions[di], when)
                if smooth:
                    p["bbox"], p["distance_m"] = t.smoother.update(p["bbox"], p["distance_m"], when)
                p["track_id"] = t.id if t.confirmed else None
                p["name"] = t.name
                p["velocity"] = t.motion.velocity() if t.confirmed else None
                t.shown = p
            reported = [p for di, p in enumerate(persons) if di in assigned]

            for t in list(self._tracks.values()):
                if t in assigned.values():
                    continue
                t.misses += 1
                box = t.smoother.predict(when) if smooth and t.confirmed and t.shown and t.misses <= HOLD_FRAMES else None
                # Not when the person already shows under another box (e.g. picked up by a new track)
                if box and all(iou(box, r["bbox"]) <= HOLD_MAX_IOU for r in reported):
                    reported.append(dict(t.shown, bbox=box, held=True, velocity=t.motion.velocity(), name=t.name))
                if t.misses == MAX_MISSES + 1 and t.name:
                    self._identities.put(t.name, t.gallery)  # keep what we learned about them
                if not t.present and t.name is None and (not t.confirmed or now - t.last_seen > LOST_TTL_S):
                    del self._tracks[t.id]
            return reported

    def tracks(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return [{"track_id": t.id, "name": t.name, "present": t.present,
                     "seconds_since_seen": round(now - t.last_seen, 1)}
                    for t in sorted(self._tracks.values(), key=lambda t: t.id)
                    if t.name or (t.confirmed and (t.present or t.hits >= LIST_HITS))]

    def set_name(self, track_id: int, name: Optional[str]) -> bool:
        """Name a track (None removes the name). A name already on another track moves here, with its gallery."""
        with self._lock:
            t = self._tracks.get(track_id)
            if t is None:
                return False
            if t.name and t.name != name:
                self._identities.delete(t.name)
            if name:
                other = next((o for o in self._tracks.values() if o.name == name and o is not t), None)
                if other:
                    # Older samples first, so the newest (this track's) are the ones kept
                    t.gallery = deque([*other.gallery, *t.gallery], maxlen=GALLERY)
                    del self._tracks[other.id]
                self._identities.put(name, t.gallery)
            t.name = name or None
            return True

    def forget(self, name: str) -> bool:
        """Drop a saved identity; a track carrying the name keeps its ID but loses the name."""
        with self._lock:
            for t in list(self._tracks.values()):
                if t.name == name:
                    t.name = None
                    if not t.present:
                        del self._tracks[t.id]
            return self._identities.delete(name)
