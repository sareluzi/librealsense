# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

# Drives the GenTL producer (realsense2-gentl.cti) through Harvesters, the reference Python GenTL consumer

import importlib.util
import logging
import os
import time

import numpy as np
import pyrealsense2 as rs
import pytest
from pytest_check import check
from rspy import file, repo

log = logging.getLogger(__name__)

pytestmark = [pytest.mark.device_each("D435*", "D455")]


def _find_cti():
    if repo.build:
        for cti in file.find( repo.build, r'(^|/)realsense2-gentl\.cti$' ):
            return os.path.join( repo.build, cti )


CTI = _find_cti()
if not CTI:
    pytest.skip( "realsense2-gentl.cti not built (BUILD_GENTL_PRODUCER=OFF)", allow_module_level=True )
if importlib.util.find_spec( "harvesters" ) is None:
    pytest.fail( "missing harvesters; pip install -r unit-tests/wrappers/genicam/requirements.txt",
                 pytrace=False )

from harvesters.core import Harvester, ImageAcquirer  # noqa: E402


@pytest.fixture
def harvesters_multi_stream_fix( monkeypatch ):
    # Harvesters 1.4 keeps one announced-buffer list for all data streams and revokes it through every stream
    original = ImageAcquirer._announce_buffers

    def announce( self, data_stream=None, _buffer_tokens=None ):
        buffers = original( self, data_stream=data_stream, _buffer_tokens=_buffer_tokens )
        self.__dict__.setdefault( '_rs_buffers', [] ).append( ( data_stream, buffers ) )
        return buffers

    def release( self ):
        for data_stream, buffers in self.__dict__.pop( '_rs_buffers', [] ):
            if data_stream.is_open():
                self._flush_buffers( data_stream )
                for b in buffers:
                    data_stream.revoke_buffer( b )
        self._announced_buffers.clear()
        while not self._queue.empty():
            self._queue.get_nowait()

    monkeypatch.setattr( ImageAcquirer, '_announce_buffers', announce )
    monkeypatch.setattr( ImageAcquirer, '_release_buffers', release )


@pytest.fixture
def acquirer( test_device, harvesters_multi_stream_fix ):
    dev, ctx = test_device
    serial = dev.get_info( rs.camera_info.serial_number )
    h = Harvester()
    h.add_file( CTI, check_existence=True, check_validity=True )
    h.update()
    ia = h.create( { 'serial_number': serial } )
    yield dev, ia
    ia.destroy()
    h.reset()


def _fetch_all( ia, timeout=3 ):
    buffers = ia.fetch( timeout=timeout )
    return buffers if isinstance( buffers, list ) else [buffers]


def _depth_profile( dev, width, height, fps ):
    for p in dev.first_depth_sensor().get_stream_profiles():
        v = p.as_video_stream_profile()
        if p.stream_type() == rs.stream.depth and p.format() == rs.format.z16 \
                and ( v.width(), v.height(), v.fps() ) == ( width, height, fps ):
            return v
    pytest.fail( f"no depth profile {width}x{height}@{fps}" )


def test_enumeration( test_device ):
    dev, ctx = test_device
    h = Harvester()
    h.add_file( CTI, check_existence=True, check_validity=True )
    h.update()
    try:
        serials = [d.serial_number for d in h.device_info_list]
        assert dev.get_info( rs.camera_info.serial_number ) in serials
    finally:
        h.reset()


def test_scan3d_features_match_intrinsics( acquirer ):
    dev, ia = acquirer
    nm = ia.remote_device.node_map
    nm.ComponentSelector.value = 'Range'
    width, height, fps = nm.Width.value, nm.Height.value, int( nm.AcquisitionFrameRate.value )
    intr = _depth_profile( dev, width, height, fps ).get_intrinsics()
    check.equal( nm.PixelFormat.value, 'Coord3D_C16' )
    check.equal( nm.Scan3dOutputMode.value, 'ProjectedC' )
    check.equal( nm.Scan3dDistanceUnit.value, 'Millimeter' )
    check.almost_equal( nm.Scan3dFocalLength.value, intr.fx, abs=1e-3 )
    check.almost_equal( nm.Scan3dFocalLength.value, intr.fy, abs=1e-3 )  # SFNC has a single focal length
    check.almost_equal( nm.Scan3dPrincipalPointU.value, intr.ppx, abs=1e-3 )
    check.almost_equal( nm.Scan3dPrincipalPointV.value, intr.ppy, abs=1e-3 )
    depth_units = dev.first_depth_sensor().get_option( rs.option.depth_units )
    check.almost_equal( nm.Scan3dCoordinateScale.value, depth_units * 1000, rel=1e-6 )


def test_profile_snapping( acquirer ):
    dev, ia = acquirer
    nm = ia.remote_device.node_map
    nm.ComponentSelector.value = 'Range'
    nm.Width.value = 640
    assert ( nm.Width.value, nm.Height.value ) == ( 640, 480 )
    nm.Width.value = 1280
    assert ( nm.Width.value, nm.Height.value ) == ( 1280, 720 )
    with pytest.raises( Exception ):
        nm.Width.value = 641
    assert nm.PayloadSize.value == 1280 * 720 * 2


def test_streaming( acquirer ):
    dev, ia = acquirer
    nm = ia.remote_device.node_map
    expected = {}
    for component in nm.ComponentSelector.symbolics:
        nm.ComponentSelector.value = component
        expected[nm.PixelFormat.value] = ( nm.Width.value, nm.Height.value )
    ia.start()
    for _ in range( 10 ):  # let auto-exposure and the first frames settle
        for b in _fetch_all( ia ):
            b.queue()

    seconds, sets = 5.0, 0
    last_ts = {}
    t0 = time.time()
    while time.time() - t0 < seconds:
        for b in _fetch_all( ia ):
            c = b.payload.components[0]
            check.equal( ( c.width, c.height ), expected[c.data_format] )
            check.greater( b.timestamp_ns, last_ts.get( c.data_format, 0 ) )
            last_ts[c.data_format] = b.timestamp_ns
            if c.data_format == 'Coord3D_C16' and sets == 0:
                check.greater( np.count_nonzero( c.data ), c.data.size // 10, "depth is mostly empty" )
            b.queue()
        sets += 1
    fps = sets / seconds
    log.info( f"frame sets per second: {fps:.1f}" )
    assert set( last_ts ) == set( expected )
    assert fps > 27


def test_params_locked_while_streaming( acquirer ):
    dev, ia = acquirer
    nm = ia.remote_device.node_map
    nm.ComponentSelector.value = 'Range'
    ia.start()
    for b in _fetch_all( ia ):
        b.queue()
    with pytest.raises( Exception ):
        nm.Width.value = 640
    ia.stop()
    nm.TLParamsLocked.value = 0
    nm.Width.value = 640
    assert nm.Width.value == 640


def test_scan3d_point_cloud_matches_rs2( acquirer ):
    # Same depth frame, two projections: Scan3d features vs rs2 intrinsics (Gate A criterion a)
    dev, ia = acquirer
    nm = ia.remote_device.node_map
    nm.ComponentSelector.value = 'Range'
    width, height, fps = nm.Width.value, nm.Height.value, int( nm.AcquisitionFrameRate.value )
    f, u0, v0 = nm.Scan3dFocalLength.value, nm.Scan3dPrincipalPointU.value, nm.Scan3dPrincipalPointV.value
    scale, offset = nm.Scan3dCoordinateScale.value, nm.Scan3dCoordinateOffset.value
    ia.start()
    for _ in range( 10 ):
        for b in _fetch_all( ia ):
            b.queue()
    depth = None
    for b in _fetch_all( ia ):
        c = b.payload.components[0]
        if c.data_format == 'Coord3D_C16':
            depth = c.data.reshape( c.height, c.width ).astype( np.float64 )
        b.queue()
    assert depth is not None
    # rs2_deproject_pixel_to_point is per-pixel; a spread sample of valid pixels is enough
    sample = np.argwhere( depth != nm.Scan3dInvalidDataValue.value )[::97]
    assert len( sample ) > 100
    y, x = sample[:, 0], sample[:, 1]
    z = depth[y, x] * scale + offset
    genicam = np.stack( [( x - u0 ) * z / f, ( y - v0 ) * z / f, z], axis=-1 )

    intr = _depth_profile( dev, width, height, fps ).get_intrinsics()
    depth_units = dev.first_depth_sensor().get_option( rs.option.depth_units )
    native = np.array( [rs.rs2_deproject_pixel_to_point( intr, [float( px ), float( py )], depth[py, px] * depth_units )
                        for py, px in sample] ) * 1000
    rms = np.sqrt( np.mean( np.sum( ( genicam - native ) ** 2, axis=1 ) ) )
    log.info( f"RMS vs rs2 over {len( sample )} points: {rms:.6f} mm" )
    assert rms < 0.1
