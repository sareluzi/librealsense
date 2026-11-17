# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""Person detection on the host, with the model the D585 runs on-camera (DAMO-YOLO, ONNX).

Optional: needs ONNX Runtime and the model file (settings PERSON_DETECTOR_MODEL). Without either,
status() says why and only the camera's detection is available. Pre- and post-processing follow the
model's evaluation code (person-benchmark: damo_person_bench spec and damo_bench decoder): RGB
stretched to 416x416 and scaled to [0, 1]; per output scale, DFL box distances from cell centers;
score threshold 0.03, then NMS at IoU 0.65.
"""

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

try:
    import onnxruntime as ort
except ImportError:  # optional: no host detection
    ort = None
try:
    import cv2  # optional: the model's own resize, and much faster than the numpy one below
except ImportError:
    cv2 = None

logger = logging.getLogger(__name__)

INPUT_SIZE = 416
SCORE_THRESH = 0.03
NMS_IOU = 0.65
# ONNX Runtime providers this sample offers, as host devices
PROVIDERS = {"cpu": "CPUExecutionProvider", "gpu": "DmlExecutionProvider"}


def status(model_path: Path) -> Dict[str, Any]:
    """Whether host detection can run here, on which devices, and if not, why."""
    if ort is None:
        return {"available": False, "devices": [], "reason": "ONNX Runtime is not installed (pip install onnxruntime)"}
    if not Path(model_path).is_file():
        return {"available": False, "devices": [], "reason": f"No person detection model at {model_path}"}
    have = set(ort.get_available_providers())
    return {"available": True, "devices": [d for d, p in PROVIDERS.items() if p in have], "reason": None}


_resize_maps: Dict[Tuple[int, int], Tuple[np.ndarray, ...]] = {}


def _axis(src: int, dst: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    # Bilinear with pixel-center alignment, as cv2.INTER_LINEAR
    x = np.maximum((np.arange(dst, dtype=np.float32) + 0.5) * (src / dst) - 0.5, 0)
    i0 = np.minimum(x.astype(np.int32), src - 1)
    return i0, np.minimum(i0 + 1, src - 1), (x - i0).astype(np.float32)


def preprocess(rgb: np.ndarray) -> np.ndarray:
    """HxWx3 uint8 RGB -> 1x3x416x416 float32 in [0, 1], stretched (no letterbox)."""
    if cv2 is not None:
        out = cv2.resize(rgb, (INPUT_SIZE, INPUT_SIZE), interpolation=cv2.INTER_LINEAR)
        return np.ascontiguousarray(out.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
    h, w = rgb.shape[:2]
    if (w, h) not in _resize_maps:
        _resize_maps[(w, h)] = (*_axis(h, INPUT_SIZE), *_axis(w, INPUT_SIZE))
    y0, y1, wy, x0, x1, wx = _resize_maps[(w, h)]
    # Gather the 416x416 neighbors in uint8 first; only they become float
    r0, r1 = rgb[y0], rgb[y1]
    px = lambda rows, cols: rows[:, cols].astype(np.float32)
    wx3 = wx[None, :, None]
    top = px(r0, x0) + (px(r0, x1) - px(r0, x0)) * wx3
    bottom = px(r1, x0) + (px(r1, x1) - px(r1, x0)) * wx3
    out = top + (bottom - top) * wy[:, None, None]
    return np.ascontiguousarray(np.rint(out).transpose(2, 0, 1)[None] / 255.0, dtype=np.float32)


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> List[int]:
    """Greedy NMS, highest score first (ties by index); drops boxes overlapping a kept one by more than iou."""
    order = np.lexsort((np.arange(len(scores)), -scores))
    area = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])
    keep: List[int] = []
    while order.size:
        i, rest = order[0], order[1:]
        keep.append(int(i))
        iw = np.maximum(0, np.minimum(boxes[i, 2], boxes[rest, 2]) - np.maximum(boxes[i, 0], boxes[rest, 0]))
        ih = np.maximum(0, np.minimum(boxes[i, 3], boxes[rest, 3]) - np.maximum(boxes[i, 1], boxes[rest, 1]))
        inter = iw * ih
        union = area[i] + area[rest] - inter
        order = rest[~(np.where(union > 0, inter / np.where(union > 0, union, 1), 0) > iou)]
    return keep


def decode(outputs: List[np.ndarray], width: int, height: int) -> List[Tuple[List[int], int]]:
    """Raw heads -> [(box [x1, y1, x2, y2] in image pixels, score 0-100)]."""
    levels: Dict[Tuple[int, int], List[np.ndarray]] = {}
    for o in outputs:
        if o.ndim == 4:
            levels.setdefault(o.shape[2:], []).append(o)
    all_boxes, all_scores = [], []
    for (h, w), pair in levels.items():
        score, reg = sorted(pair, key=lambda a: a.shape[1])  # the score head has fewer channels
        stride = INPUT_SIZE // h
        bins = reg.shape[1] // 4
        r = reg[0].reshape(4, bins, h, w)
        r = np.exp(r - r.max(axis=1, keepdims=True))
        dist = (r / r.sum(axis=1, keepdims=True) * np.arange(bins, dtype=np.float32)[None, :, None, None]).sum(axis=1)
        dist = dist.reshape(4, -1).T * stride
        cy, cx = np.mgrid[0:h, 0:w].astype(np.float32) * stride
        cx, cy = cx.ravel(), cy.ravel()
        all_boxes.append(np.stack([cx - dist[:, 0], cy - dist[:, 1], cx + dist[:, 2], cy + dist[:, 3]], axis=1))
        all_scores.append(score[0, 0].ravel())
    boxes, scores = np.concatenate(all_boxes), np.concatenate(all_scores)
    sel = scores >= SCORE_THRESH
    boxes, scores = boxes[sel], scores[sel]
    scale = np.array([width / INPUT_SIZE, height / INPUT_SIZE] * 2, np.float32)
    return [([int(round(float(v))) for v in boxes[i] * scale], int(round(float(scores[i]) * 100)))
            for i in nms(boxes, scores, NMS_IOU)]


class HostDetector:
    def __init__(self, model_path: Path, device: str = "cpu"):
        if ort is None:
            raise RuntimeError("ONNX Runtime is not installed")
        self.device = device
        # One thread: inference then runs on the caller's thread, so its CPU time is measurable, and ORT's
        # default pool spins idle cores (on an 8-core laptop ~4x the CPU for a 1.5x faster frame)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(model_path), opts, providers=[PROVIDERS[device]])
        self._input = self._session.get_inputs()[0].name

    def detect(self, rgb: np.ndarray) -> List[Tuple[List[int], int]]:
        """Persons in an RGB image: [(box [x1, y1, x2, y2], score 0-100)]."""
        return decode(self._session.run(None, {self._input: preprocess(rgb)}), rgb.shape[1], rgb.shape[0])


class LatestFrameWorker:
    """Runs `process(item)` on a thread, always on the newest item; older ones still waiting are dropped."""

    def __init__(self, process: Callable[[Any], None], name: str):
        self._process = process
        self._cond = threading.Condition()
        self._item: Any = None
        self._stop = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, item: Any) -> None:
        with self._cond:
            self._item = item
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._item is None and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                item, self._item = self._item, None
            try:
                self._process(item)
            except Exception:
                logger.exception("Host person detection failed")
