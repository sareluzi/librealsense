# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

from pydantic import BaseModel, Field


class PersonAnalyticsConfig(BaseModel):
    # Defaults match the C++ viewer's object detection NMS
    nms_score: int = Field(45, ge=0, le=100, description="Minimum detection score (0-100)")
    nms_iou: float = Field(0.55, ge=0.0, le=1.0, description="Overlap above which the lower-scored box is dropped")
