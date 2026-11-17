# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

from typing import Optional

from pydantic import BaseModel, Field


class PersonAnalyticsConfig(BaseModel):
    # Defaults match the C++ viewer's object detection NMS
    nms_score: int = Field(45, ge=0, le=100, description="Minimum detection score (0-100)")
    nms_iou: float = Field(0.55, ge=0.0, le=1.0, description="Overlap above which the lower-scored box is dropped")
    tracking: bool = Field(True, description="Keep an ID and name on each person across frames")
    velocity: bool = Field(True, description="Report each tracked person's velocity (needs tracking)")
    smoothing: bool = Field(True, description="Steady boxes and distances, keep a tracked person through a slightly "
                            "lower score or a few missed frames (needs tracking)")


class TrackInfo(BaseModel):
    track_id: int
    name: Optional[str] = None
    present: bool
    seconds_since_seen: float


class TrackName(BaseModel):
    name: Optional[str] = Field(None, max_length=64, description="Name for the person; null or empty removes it")
