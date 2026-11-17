# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""How much of the host this server process uses: CPU, memory, GPU and NPU.

Sampled once a second in the background while someone asks. Every value is optional: what the
machine cannot measure (no psutil, no GPU or NPU, no per-process GPU counters outside Windows) is None.
"""

import ctypes
import logging
import os
import platform
import re
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

try:
    import psutil
except ImportError:  # optional: CPU and memory are then unknown
    psutil = None

INTERVAL_S = 1.0
IDLE_STOP_S = 10.0  # the sampler stops this long after the last request
PARTS = ("perception", "detection", "alignment")  # host work reported separately


class _GpuEngines:
    """This process's GPU and NPU utilization from Windows' per-process "GPU Engine" counters (PDH).

    NPUs show there as adapters whose engines are all "neural"; any other adapter counts as a GPU.
    Like Task Manager, an adapter's use is its busiest engine's.
    """

    _INSTANCE = re.compile(r"pid_(\d+)_luid_(\w+?)_phys_\d+_eng_(\d+)_engtype_(.*)", re.IGNORECASE)

    def __init__(self):
        pdh = ctypes.WinDLL("pdh")  # raises off Windows
        self._pdh = pdh
        self._query, self._counter = ctypes.c_void_p(), ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query)) or pdh.PdhAddEnglishCounterW(
                self._query, r"\GPU Engine(*)\Utilization Percentage", None, ctypes.byref(self._counter)):
            raise OSError("no GPU Engine counters")
        pdh.PdhCollectQueryData(self._query)  # rates need a first sample

    def sample(self) -> Dict[str, Optional[float]]:
        if self._pdh.PdhCollectQueryData(self._query):
            raise OSError("PdhCollectQueryData failed")

        class Item(ctypes.Structure):
            _fields_ = [("name", ctypes.c_wchar_p), ("status", ctypes.c_uint32), ("value", ctypes.c_double)]

        size, count = ctypes.c_uint32(0), ctypes.c_uint32(0)
        PDH_FMT_DOUBLE, PDH_MORE_DATA = 0x200, 0x800007D2
        rc = self._pdh.PdhGetFormattedCounterArrayW(self._counter, PDH_FMT_DOUBLE, ctypes.byref(size),
                                                    ctypes.byref(count), None) & 0xFFFFFFFF
        if rc != PDH_MORE_DATA:
            return {"gpu": None, "npu": None}
        buf = ctypes.create_string_buffer(size.value)
        if self._pdh.PdhGetFormattedCounterArrayW(self._counter, PDH_FMT_DOUBLE, ctypes.byref(size),
                                                  ctypes.byref(count), buf):
            return {"gpu": None, "npu": None}
        items = ctypes.cast(buf, ctypes.POINTER(Item))
        types: Dict[str, set] = {}        # adapter -> engine types (any process)
        mine: Dict[tuple, float] = {}     # (adapter, engine) -> this process's use
        pid = str(os.getpid())
        for i in range(count.value):
            m = self._INSTANCE.match(items[i].name or "")
            if not m:
                continue
            proc, luid, eng, kind = m.groups()
            types.setdefault(luid, set()).add(kind.lower())
            if proc == pid and items[i].status == 0:
                mine[(luid, eng)] = mine.get((luid, eng), 0.0) + items[i].value
        npus = {a for a, k in types.items() if k == {"neural"}}
        gpus = set(types) - npus
        busiest = lambda adapters: max([v for (a, _), v in mine.items() if a in adapters], default=0.0)
        return {"gpu": round(busiest(gpus), 1) if gpus else None, "npu": round(busiest(npus), 1) if npus else None}


class SystemUsage:
    """Latest usage of this process.

    `busy_s` returns the CPU seconds spent so far per part of the work ("perception", "detection",
    "alignment"); each is reported as `<part>_cpu_percent`.
    """

    def __init__(self, busy_s: Optional[Callable[[], Dict[str, float]]] = None):
        self._busy_s = busy_s
        self._lock = threading.Lock()
        self._latest: Dict[str, Any] = {}
        self._asked = 0.0
        self._thread: Optional[threading.Thread] = None

    def get(self) -> Dict[str, Any]:
        with self._lock:
            self._asked = time.monotonic()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="system-usage", daemon=True)
                self._thread.start()
            return dict(self._latest)

    def _run(self) -> None:
        proc = psutil.Process() if psutil else None
        cores = (psutil.cpu_count() if psutil else None) or os.cpu_count()
        engines = None
        if platform.system() == "Windows":
            try:
                engines = _GpuEngines()
            except Exception as e:
                logger.info("Per-process GPU/NPU usage unavailable: %s", e)
        if proc:
            proc.cpu_percent()
            psutil.cpu_percent()
        busy, t = self._read_busy(), time.monotonic()
        while True:
            time.sleep(INTERVAL_S)
            usage: Dict[str, Any] = {"cpu_percent": None, "system_cpu_percent": None, "cpu_cores": cores,
                                     "memory_mb": None, "gpu_percent": None, "npu_percent": None,
                                     **{f"{k}_cpu_percent": None for k in PARTS}}
            try:
                if proc:
                    # psutil gives % of one core; report % of the whole machine like Task Manager
                    usage["cpu_percent"] = round(proc.cpu_percent() / (cores or 1), 1)
                    usage["system_cpu_percent"] = round(psutil.cpu_percent(), 1)
                    usage["memory_mb"] = round(proc.memory_info().rss / 2 ** 20)
            except Exception as e:
                logger.debug("CPU usage: %s", e)
            if engines:
                try:
                    g = engines.sample()
                    usage["gpu_percent"], usage["npu_percent"] = g["gpu"], g["npu"]
                except Exception as e:
                    logger.info("Per-process GPU/NPU usage stopped: %s", e)
                    engines = None
            now, b = time.monotonic(), self._read_busy()
            if busy is not None and b is not None and cores:
                for k in PARTS:
                    if k in b and k in busy:
                        usage[f"{k}_cpu_percent"] = round((b[k] - busy[k]) / (now - t) * 100 / cores, 2)
            busy, t = b, now
            with self._lock:
                self._latest = usage
                if now - self._asked > IDLE_STOP_S:
                    self._thread = None
                    return

    def _read_busy(self) -> Optional[Dict[str, float]]:
        try:
            return self._busy_s() if self._busy_s else None
        except Exception:
            return None
