# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

"""
Live view of a RealSense camera through the GenTL producer, the way a GenICam application sees it:
depth (colorized, center distance in mm), infrared and color, one window each. Press q or Esc to quit.

    pip install harvesters opencv-python
    python gentl-viewer.py <build>/Release/realsense2-gentl.cti [--serial <sn>]
"""

import argparse
import sys

import cv2
import numpy as np
from harvesters.core import Harvester, ImageAcquirer

MAX_DEPTH_MM = 4000  # end of the depth color scale


def fix_harvesters_multi_stream():
    # Harvesters 1.4 keeps one announced-buffer list for all data streams and revokes it through every stream
    announce = ImageAcquirer._announce_buffers

    def announce_per_stream( self, data_stream=None, _buffer_tokens=None ):
        buffers = announce( self, data_stream=data_stream, _buffer_tokens=_buffer_tokens )
        self.__dict__.setdefault( '_rs_buffers', [] ).append( ( data_stream, buffers ) )
        return buffers

    def release_per_stream( self ):
        for data_stream, buffers in self.__dict__.pop( '_rs_buffers', [] ):
            if data_stream.is_open():
                self._flush_buffers( data_stream )
                for b in buffers:
                    data_stream.revoke_buffer( b )
        self._announced_buffers.clear()
        while not self._queue.empty():
            self._queue.get_nowait()

    ImageAcquirer._announce_buffers = announce_per_stream
    ImageAcquirer._release_buffers = release_per_stream


def to_image( component, depth_scale ):
    fmt = component.data_format
    if fmt == 'Coord3D_C16':
        depth_mm = component.data.reshape( component.height, component.width ) * depth_scale
        image = cv2.applyColorMap( cv2.convertScaleAbs( depth_mm, alpha=255. / MAX_DEPTH_MM ), cv2.COLORMAP_JET )
        image[depth_mm == 0] = 0
        center = depth_mm[component.height // 2, component.width // 2]
        cv2.drawMarker( image, ( component.width // 2, component.height // 2 ), ( 255, 255, 255 ) )
        cv2.putText( image, f'{center:.0f} mm', ( 10, 30 ), cv2.FONT_HERSHEY_SIMPLEX, 1, ( 255, 255, 255 ), 2 )
        return 'Range (depth)', image
    if fmt == 'RGB8':
        rgb = component.data.reshape( component.height, component.width, 3 )
        return 'Intensity (color)', cv2.cvtColor( rgb, cv2.COLOR_RGB2BGR )
    return 'Infrared', component.data.reshape( component.height, component.width ).copy()


def main():
    parser = argparse.ArgumentParser( description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter )
    parser.add_argument( 'cti', help='path to realsense2-gentl.cti' )
    parser.add_argument( '--serial', help='camera serial number (default: first camera)' )
    parser.add_argument( '--frames', type=int, default=0, help='stop after this many frame sets (default: run until q)' )
    args = parser.parse_args()

    fix_harvesters_multi_stream()
    h = Harvester()
    h.add_file( args.cti, check_existence=True, check_validity=True )
    h.update()
    if not h.device_info_list:
        sys.exit( 'no camera found by the producer' )
    for d in h.device_info_list:
        print( f'found {d.model} {d.serial_number}' )

    ia = h.create( { 'serial_number': args.serial } if args.serial else 0 )
    nm = ia.remote_device.node_map
    for component in nm.ComponentSelector.symbolics:
        nm.ComponentSelector.value = component
        print( f'  {component:<10} {nm.PixelFormat.value:<12} {nm.Width.value}x{nm.Height.value}'
               f' @ {nm.AcquisitionFrameRate.value:.0f} fps' )
    depth_scale = nm.Scan3dCoordinateScale.value if 'Range' in nm.ComponentSelector.symbolics else 1.

    ia.start()
    try:
        count = 0
        while not args.frames or count < args.frames:
            buffers = ia.fetch( timeout=3 )
            for b in buffers if isinstance( buffers, list ) else [buffers]:
                title, image = to_image( b.payload.components[0], depth_scale )
                b.queue()
                cv2.imshow( title, image )
            count += 1
            if cv2.waitKey( 1 ) & 0xFF in ( ord( 'q' ), 27 ):
                break
        print( f'{count} frame sets' )
    finally:
        ia.stop()
        ia.destroy()
        h.reset()
        cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
