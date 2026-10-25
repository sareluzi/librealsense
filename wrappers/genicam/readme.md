# RealSense GenTL Producer (prototype)

A GenICam GenTL producer (`realsense2-gentl.cti`) that makes RealSense depth cameras visible to machine-vision software such as HALCON, MATLAB, MIL, VisionPro and Harvesters. It's built on the public librealsense API, so any camera librealsense can open shows up as a GenICam device.

Status: Phase 1a prototype. See [phase-1a-plan.md](phase-1a-plan.md).

## Build

```
cmake .. -DBUILD_GENTL_PRODUCER=ON -DBUILD_SHARED_LIBS=OFF
cmake --build . --config Release --target realsense2-gentl
```

`BUILD_SHARED_LIBS=OFF` links librealsense into the `.cti`, which is the form to hand to other people. Consumers load the `.cti` into their own process. On Windows, a shared `realsense2.dll` would be looked up next to the consumer's executable, not next to the `.cti`.

## Use

Add the directory that contains `realsense2-gentl.cti` to `GENICAM_GENTL64_PATH`, or load the file directly:

```python
from harvesters.core import Harvester
h = Harvester()
h.add_file('realsense2-gentl.cti')
h.update()
ia = h.create()
nm = ia.remote_device.node_map
nm.ComponentSelector.value = 'Range'
nm.Width.value = 640          # snaps Height to a supported profile (640x480)
ia.start()
```

Live view of depth, infrared and color:

```
pip install harvesters opencv-python
python wrappers/genicam/gentl-viewer.py build/Release/realsense2-gentl.cti
```

## Model

| GenTL | RealSense |
|---|---|
| System / Interface | one `rs2::context`, one `RealSense` interface |
| Device (ID = serial number) | `rs2::device` |
| Data streams `Range`, `Infrared`, `Intensity` | depth Z16 → `Coord3D_C16`, IR1 Y8 → `Mono8`, color RGB8 → `RGB8` |

- Sensors start on `AcquisitionStart`. A component streams if its `ComponentEnable` is set or its data stream is acquiring. Range and Infrared share the stereo sensor, so Infrared follows the Range resolution and frame rate.
- `ComponentSelector` selects `ComponentEnable`, `PixelFormat`, `Width`, `Height`, `PayloadSize`, `ExposureTime`, `ExposureAuto` and `Gain`. Range and Infrared share exposure and gain.
- `Width` and `Height` snap to the closest supported profile. `AcquisitionFrameRate` applies to every component. These features are locked while streaming or while `TLParamsLocked` is set.
- To build a point cloud from a Range buffer, use `X = (u - Scan3dPrincipalPointU) * Z / Scan3dFocalLength`, the same for Y with V, and `Z = value * Scan3dCoordinateScale` in mm. A value of 0 means invalid.
- Buffer timestamps are in ns, taken from librealsense global time, which is one clock shared by all sensors.
- Vendor features: `RsLaserPower`, `RsEmitterEnabled`.

## Limitations

- Only one process can stream from a camera at a time, the same as two librealsense applications. If another process is streaming, `AcquisitionStart` fails with `GC_ERR_RESOURCE_IN_USE`.
- No multi-part (GenDC) buffers yet: each component has its own data stream.
- Harvesters 1.4 releases buffers wrongly for devices with several data streams. See the fixture in `unit-tests/wrappers/genicam/pytest-gentl-producer.py`.
- Tools that only speak GigE Vision on the wire won't see the camera.

## Tests

```
pip install -r unit-tests/wrappers/genicam/requirements.txt
cd unit-tests
python -m pytest -v wrappers/genicam
```
