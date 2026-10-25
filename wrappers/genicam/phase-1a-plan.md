# GenICam GenTL Producer — Phase 1a Plan

Source proposal: *GenICam support for RealSense cameras* (RealSense software group, October 2026 draft).
Phase 1a = producer prototype for D435/D455 on Windows and Ubuntu, validated in HALCON and Harvesters, ending at Gate A.

## 1. Placement decision

| Option | Pros | Cons |
|---|---|---|
| **A. `wrappers/genicam` in-tree (chosen)** | Same CI and HW-in-loop pytest infra; builds against exact SDK; wrapper precedent (python, csharp, rest-api); core untouched | Release coupled to SDK cadence; code public (Apache 2.0) |
| B. Separate repo (proposal text) | Independent cadence; can stay private | Separate CI/HW runners, SDK version pinning; overhead for a prototype that may be archived at Gate A |
| C. Commercial GenTL toolkit | Faster TL boilerplate | License cost, third-party dependency |
| D. Host-side software GigE Vision bridge | Reaches GEV-on-wire-only tools | Large effort, NIC/driver issues — candidate input for Gate C, not 1a |

Conditions for A:
1. Producer uses **only the public rs2 C++ API** and is a self-contained CMake target, so it can move to its own repo at Gate B without rewrite.
2. Distribution build **links librealsense statically** into the `.cti` and exports only the GenTL C symbols. Windows resolves DLL dependencies of a `.cti` relative to the host app (HALCON/MATLAB), not the `.cti` directory, and a second `realsense2.dll` may already be loaded in-process.

Open items: product/legal OK for public Apache 2.0 producer; EMVA license terms for vendored `GenTL.h` (header states EMVA GenICam license, BSD-style).

## 2. Architecture

```
GenICam consumer (HALCON / Harvesters / ...)
   │  GenTL C API (GCInitLib, TLOpen, IFOpenDevice, DSAnnounceBuffer, EventGetData, ...)
   ▼
realsense2-gentl.cti
   gentl-api.cpp     exported C functions, handle validation, exception -> GC_ERROR, last-error
   node-map.*        GenApi XML generator + virtual register space (single source of truth)
   system/interface  rs2::context, query_devices(), one "RealSense" interface
   device.*          rs2::device, remote-device features, AcquisitionStart/Stop, frame dispatch
   data-stream.*     buffers, input pool / output queue, NEW_BUFFER event
   ▼
librealsense (public rs2 API, unchanged)
```

Key design decisions (made while implementing, revisit in spikes):

- **GenApi XML is generated at runtime** from the same feature table that defines the register map. No hand-maintained XML/register drift; per-device ranges and capabilities come from the live device. Served through the `Local:` port URL.
- **Three data streams** (`Range`, `Infrared`, `Intensity`) — one per SFNC component — in 1a. Multi-part (GenDC) is 1b unless spike S1 shows HALCON needs it earlier.
- **Sensors start on `AcquisitionStart`**, not on `DSStartAcquisition`: depth and IR share the stereo sensor and must open together. A component streams if `ComponentEnable` is set or its data stream is acquiring.
- **`ComponentSelector` selects everything per-component**: `ComponentEnable`, `PixelFormat`, `Width`, `Height`, `ExposureTime`, `ExposureAuto`, `Gain`, `PayloadSize`. Range and Infrared share the stereo sensor, so their exposure/gain values are shared.
- **Width/Height snap to valid profiles**: writing Width picks the closest profile with that width, keeping Height/fps where possible. `AcquisitionFrameRate` is device-wide and applied to every component (nearest supported). Profile features are locked while streaming / `TLParamsLocked=1`.
- **Timestamps** in ns from rs2 global time (shared clock across sensors), `DEVICE_INFO_TIMESTAMP_FREQUENCY = 1e9`. Frame ID = rs2 frame number.
- **Exclusive access**: opening a device already open in this process returns `GC_ERR_RESOURCE_IN_USE`; a sensor busy in another process fails `AcquisitionStart` with a clear message.

### Feature map (remote device)

| GenICam feature | Source |
|---|---|
| `DeviceVendorName`, `DeviceModelName`, `DeviceSerialNumber`, `DeviceFirmwareVersion` | `rs2_camera_info` |
| `DeviceScanType` | `Areascan3D` |
| `ComponentSelector` | `Range` (depth Z16), `Infrared` (IR1 Y8), `Intensity` (color RGB8) |
| `ComponentEnable` | producer state |
| `PixelFormat` | `Coord3D_C16` / `Mono8` / `RGB8` (read-only) |
| `Width`, `Height`, `PayloadSize` | selected stream profile |
| `AcquisitionMode`, `AcquisitionStart`, `AcquisitionStop`, `AcquisitionFrameRate` | sensor open/start/stop, profile fps |
| `TLParamsLocked` | producer state |
| `ExposureTime` (µs) | `RS2_OPTION_EXPOSURE` (color option is in 100 µs units, scaled) |
| `ExposureAuto` | `RS2_OPTION_ENABLE_AUTO_EXPOSURE` |
| `Gain` | `RS2_OPTION_GAIN` |
| `Scan3dDistanceUnit`, `Scan3dOutputMode` | `Millimeter`, `ProjectedC` |
| `Scan3dCoordinateSelector`, `Scan3dCoordinateScale`, `Scan3dCoordinateOffset` | `CoordinateC`, `RS2_OPTION_DEPTH_UNITS` × 1000, 0 |
| `Scan3dInvalidDataFlag`, `Scan3dInvalidDataValue` | true, 0 |
| `Scan3dFocalLength`, `Scan3dPrincipalPointU/V` | depth profile intrinsics (fx, ppx, ppy) |
| `Scan3dBaseline` | `RS2_OPTION_STEREO_BASELINE` |
| `RsLaserPower`, `RsEmitterEnabled` (vendor) | `RS2_OPTION_LASER_POWER`, `RS2_OPTION_EMITTER_ENABLED` |

## 3. Work breakdown (2 engineers, ~7 weeks)

E1 = GenTL core and buffers. E2 = XML, feature mapper, validation.

### Week 0–1 — spikes and skeleton (both)
- **S1 multi-stream**: how HALCON GenICamTL and Harvesters handle a device with 3 data streams (one `open_framegrabber` per stream? stream selector?). If poor, pull GenTL multi-part buffers (`BUFFER_PART_INFO`, not full GenDC) into 1a (+~1 week).
- **S2 selectors**: confirm `ComponentSelector` for exposure/gain vs `SourceSelector`; compare with HALCON 3D examples and other 3D producers.
- **S3 profiles**: validate the Width/Height snapping model with HALCON parameter dialogs; fallback is a vendor `RsStreamProfile` enumeration.
- Skeleton `.cti` loads in Harvesters (`GCInitLib`, `TLOpen`, `GCGetInfo`, interface enumeration).
- License check of vendored `GenTL.h`.

### Weeks 1–3 — GenTL core (E1)
- System / Interface / Device / DataStream / Buffer modules; all module handles usable as ports with minimal TL XML.
- `DSAnnounceBuffer`, `DSAllocAndAnnounceBuffer`, `DSQueueBuffer`, `DSRevokeBuffer`, `DSFlushQueue`, `DSStart/StopAcquisition` (incl. `iNumToAcquire`).
- `GCRegisterEvent(EVENT_NEW_BUFFER)`, `EventGetData` with timeout, `EventKill` (→ `GC_ERR_ABORT`), `EventFlush`.
- Threading: every GenTL call may come from any thread; per-module locks; never stop an rs2 sensor from its own callback; empty input pool → drop + `STREAM_INFO_NUM_UNDERRUN`.

### Weeks 1–4 — XML and feature mapper (E2)
- Runtime XML generator with GenApi schema 1.1 element ordering; selector-indexed registers (`pIndex`).
- Remote device feature table (section 2); live `pMin`/`pMax` from `get_option_range` and profile lists.
- `NoCache` for values that change under auto-exposure.
- CI check: every generated XML loads in the official GenApi (`genicam` pip package) and all features read without error.

### Weeks 3–5 — buffer adapter and Scan3d (E1)
- Copy rs2 frame into announced buffer; fill `BUFFER_INFO_*` (timestamp ns, frame id, width, height, PFNC pixel format, size filled, incomplete / data-larger-than-buffer).
- Scan3d features follow the active depth profile.

### Weeks 4–6 — validation (E2, E1 from week 5)
- Harvesters pytest suite under `unit-tests/genicam/` (device-marked D435/D455, skipped without Harvesters): enumeration, node round-trips, every profile, start/stop cycles, 10-minute soak, fps.
- HALCON on Windows 11 and Ubuntu 22.04/24.04: HDevelop script for enumerate, configure, depth + color at 30 fps, `xyz_to_object_model_3d`.
- Point-cloud checks (see section 5).

### Weeks 6–7 — evaluation kit and Gate A data
- Windows zip + script setting `GENICAM_GENTL64_PATH`; Linux tarball + env script (installer / deb are 1b).
- Quickstart (HALCON, Harvesters), known-limitations list, feedback form.
- Gate A report: fps per OS, point-cloud results, consumer matrix, open bugs.
- PM: integrator candidate list by week 4.

| Workstream | Owner | Weeks |
|---|---|---|
| Spikes + skeleton | E1+E2 | 1 |
| GenTL core | E1 | 2.5 |
| XML + feature mapper | E2 | 3 |
| Buffer adapter + Scan3d | E1 | 1.5 |
| Validation | E2 → E1 | 2 |
| Eval kit, docs, Gate A report | both | 1 |

## 4. Build

```
cmake .. -DBUILD_GENTL_PRODUCER=ON -DBUILD_SHARED_LIBS=OFF   # distribution: librealsense linked into the .cti
cmake --build . --config Release --target realsense2-gentl
```

With `BUILD_SHARED_LIBS=ON` (development) the `.cti` depends on `realsense2.dll`/`.so`, which is copied next to it on Windows; that works for Harvesters/Python but not reliably for HALCON.

## 5. Proposal corrections to raise before approval

1. **Gate A point-cloud criterion is ill-posed.** D4xx depth noise at 1 m is several mm, so "1 mm RMS" between two captures can't pass, and the two paths can't stream simultaneously (exclusive access). Replace with:
   - (a) **same depth frame** projected via Scan3d features vs `rs2::pointcloud` with rs2 intrinsics: RMS < 0.1 mm (validates the mapping);
   - (b) plane fit on a flat target at 1 m, GenTL vs native path: mean distance within 1 mm (validates scale/units).
2. **Multi-stream risk sits in 1a**, not 1b — spike S1 in week 1 decides.
3. **Repository/licensing**: in-tree = public Apache 2.0; update the proposal's "own repository" line.
4. "librealsense untouched" still holds: only a CMake option and `add_subdirectory` outside the wrapper.

## 6. Status

- [x] Plan
- [x] Vendored `GenTL.h` (v1.5, EMVA) under `third-party/gentl/`
- [x] CMake option `BUILD_GENTL_PRODUCER`, target `realsense2-gentl` (`.cti`)
- [x] GenTL modules, ports, runtime-generated XML (official GenApi parses it), feature mapper, buffers, events
- [x] Harvesters pytest suite `unit-tests/wrappers/genicam/` — 6/6 pass on D455 / Windows 11 (shared build)
- [x] Point-cloud check (a): same frame, Scan3d vs `rs2_deproject_pixel_to_point` — RMS 0.00005 mm
- [x] Static-link build on Windows: self-contained 7.7 MB `.cti` (system DLLs only), streams at 30 fps from an isolated folder
- [ ] Linux build + `--exclude-libs` symbol check; trim Windows exports (see findings)
- [ ] Concurrency hardening: atomic check-and-open for `IFOpenDevice`/`TLOpenInterface`/`GCRegisterEvent`; `DSStartAcquisition` on a new component while the device is already streaming (restart sensors or reject)
- [ ] Spikes S1–S3 with HALCON
- [ ] D435 run, Ubuntu 22.04/24.04 run, 10-minute soak, plane-fit check (b)
- [ ] Verify color `ExposureTime` scaling (assumed UVC 100 µs units)
- [ ] Eval kit + Gate A report

## 7. Findings so far

- **Throughput**: Harvesters receives depth + IR + color framesets at 30.0 fps (848x480 / 848x480 / 1280x720) on D455.
- **Scan3d**: D4xx depth is rectified with fx == fy and zero distortion, so the single SFNC `Scan3dFocalLength` is exact; projection matches rs2 to numerical precision.
- **S1 input — Harvesters multi-stream is broken**: Harvesters 1.4 opens every data stream and returns one buffer per stream from `fetch()`, but keeps a single announced-buffer list and revokes it through every stream on `stop()`. A spec-compliant producer must reject the repeated revoke, so Harvesters raises on teardown (the producer routes buffers to their owning stream and rejects the repeat; the test suite patches Harvesters). This is evidence that a **single data stream with multi-part buffers** may be the more compatible design; weigh it with the HALCON result in S1.
- **Windows exports**: the static `.cti` exports 862 symbols. 57 are GenTL; the rest are librealsense and rosbag2/rcutils C++ symbols carrying their own `dllexport` macros. Exports are per-DLL on Windows, so nothing clashes, but they should be trimmed before distribution. A `.def` file won't help, because `__declspec(dllexport)` in linked objects still exports; those libraries need their export macros empty in static builds.
- Harvesters warns that GenTL 1.6 functions (`DSAnnounceCompositeBuffer`, flows, segments) are missing — harmless for 1.5 consumers.
