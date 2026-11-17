# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

from typing import Optional

from pydantic import BaseModel, Field


class SystemUsageInfo(BaseModel):
    """This server process's share of the host; None where the machine cannot measure it."""
    cpu_percent: Optional[float] = Field(None, description="CPU used by this process, % of the whole machine (all cores)")
    system_cpu_percent: Optional[float] = Field(None, description="CPU used by everything on the machine, %")
    cpu_cores: Optional[int] = Field(None, description="Logical cores of the machine")
    memory_mb: Optional[float] = Field(None, description="Memory used by this process (resident), MB")
    gpu_percent: Optional[float] = Field(None, description="GPU used by this process (busiest engine), %; Windows only")
    npu_percent: Optional[float] = Field(None, description="NPU used by this process, %; Windows only, when the machine has one")
    perception_cpu_percent: Optional[float] = Field(
        None, description="Part of cpu_percent spent on host person processing (NMS, tracking, velocity, smoothing)")
    detection_cpu_percent: Optional[float] = Field(
        None, description="Part of cpu_percent spent detecting persons on the host (model, distance, center of mass)")
    alignment_cpu_percent: Optional[float] = Field(
        None, description="Part of cpu_percent spent aligning depth to color on the host")
