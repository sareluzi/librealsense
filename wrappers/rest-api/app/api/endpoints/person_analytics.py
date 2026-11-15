# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

from fastapi import APIRouter, Depends

from app.api.dependencies import get_realsense_manager
from app.models.person_analytics import PersonAnalyticsConfig
from app.services.rs_manager import RealSenseManager

router = APIRouter()


@router.get("/config", response_model=PersonAnalyticsConfig)
async def get_config(device_id: str, rs_manager: RealSenseManager = Depends(get_realsense_manager)):
    rs_manager.get_device(device_id)
    return rs_manager.person_analytics.get_config(device_id)


@router.put("/config", response_model=PersonAnalyticsConfig)
async def set_config(
    device_id: str,
    config: PersonAnalyticsConfig,
    rs_manager: RealSenseManager = Depends(get_realsense_manager),
):
    rs_manager.get_device(device_id)
    return rs_manager.person_analytics.set_config(device_id, config)
