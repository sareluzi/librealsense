# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_realsense_manager
from app.models.person_analytics import HostDetectionStatus, PersonAnalyticsConfig, TrackInfo, TrackName
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
    if config.detection == "host":
        status = rs_manager.person_analytics.host_detection_status()
        if not status["available"] or config.host_device not in status["devices"]:
            raise HTTPException(status_code=400, detail=status["reason"] or f"No {config.host_device} for host detection")
    return rs_manager.person_analytics.set_config(device_id, config)


@router.get("/host-detection", response_model=HostDetectionStatus)
async def get_host_detection(device_id: str, rs_manager: RealSenseManager = Depends(get_realsense_manager)):
    """Whether persons can be detected on this host instead of the camera (ONNX Runtime + the model), and on what."""
    rs_manager.get_device(device_id)
    return rs_manager.person_analytics.host_detection_status()


@router.get("/tracks", response_model=List[TrackInfo])
async def get_tracks(device_id: str, rs_manager: RealSenseManager = Depends(get_realsense_manager)):
    """Tracked persons: present now, recently seen, and named persons not seen yet."""
    rs_manager.get_device(device_id)
    return rs_manager.person_analytics.tracker(device_id).tracks()


@router.put("/tracks/{track_id}", response_model=List[TrackInfo])
async def name_track(
    device_id: str,
    track_id: int,
    body: TrackName,
    rs_manager: RealSenseManager = Depends(get_realsense_manager),
):
    """Name (tag) a tracked person, or remove the name with null. Returns the updated tracks."""
    rs_manager.get_device(device_id)
    tracker = rs_manager.person_analytics.tracker(device_id)
    if not tracker.set_name(track_id, (body.name or "").strip() or None):
        raise HTTPException(status_code=404, detail=f"Track {track_id} not found")
    return tracker.tracks()


@router.get("/identities", response_model=List[str])
async def get_identities(device_id: str, rs_manager: RealSenseManager = Depends(get_realsense_manager)):
    """Names of saved persons, recognized again after a server restart."""
    rs_manager.get_device(device_id)
    return rs_manager.person_analytics.identities()


@router.delete("/identities/{name}")
async def forget_identity(device_id: str, name: str, rs_manager: RealSenseManager = Depends(get_realsense_manager)):
    rs_manager.get_device(device_id)
    if not rs_manager.person_analytics.forget(name):
        raise HTTPException(status_code=404, detail=f"No saved person named '{name}'")
    return {"forgotten": name}
