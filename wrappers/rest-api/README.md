# LibRS Rest API server
## Overview

This Python library provides a convenient wrapper to interact with the [RealSense REST API], a FastAPI-based service that exposes the functionality of the RealSense SDK (librealsense) over a network.

It simplifies remote control and data streaming from RealSense devices by handling communication protocols for:

1.  **RESTful Operations:** For device discovery, configuration, and control.
2.  **WebRTC:** For efficient, browser-compatible live video streaming (RGB, Depth).
3.  **Socket.IO:** For real-time streaming of frame metadata and point cloud data.
    * In order to decode point cloud data in the client, follow those steps:
        * Decode Base64 string to Uint8Array
        * 'bytes' now holds the raw byte data that originated from the NumPy array
        * Interpret bytes as Float32Array.
        IMPORTANT ASSUMPTION: We assume the original NumPy array used float32.
        If it used float64 (Python's default float), use Float64Array here.
        The ArrayBuffer gives access to the raw binary data buffer of the Uint8Array.
        The Float32Array constructor creates a *view* over that buffer, interpreting
        groups of 4 bytes as 32-bit floats without copying data.
        * Now you can use the 'vertices' Float32Array
    * Motion raw data is also propogated using this socket.io channel

## Support Features

*   **Device Management:**
    *   List connected RealSense devices.
    *   Get detailed information about specific devices (name, serial, firmware, etc.).
    *   Perform hardware resets remotely.
*   **Sensor & Option Control:**
    *   List available sensors on a device.
    *   Get detailed sensor information and supported stream profiles.
    *   List and retrieve current values for sensor options (e.g., exposure, gain).
    *   Update writable sensor options.
*   **REST-based Stream Control:**
    *   Start/Stop streams with specific configurations (resolution, format, FPS).
    *   Check the streaming status of a device.
*   **WebRTC Video Streaming:**
    *   Initiate WebRTC sessions with the API to receive live video feeds.
    *   Handles the offer/answer and ICE candidate exchange mechanisms required for WebRTC setup (via REST endpoints).
*   **Socket.IO Data Streaming:**
    *   Establish a Socket.IO connection to the API.
    *   Receive real-time events containing:
        *   Frame metadata (timestamps, frame numbers, etc.).
        *   Point cloud data streams.

## Missing Features
* Not all SDK capabilities are yet supported(e.g. Texture Mapping, Post processing filters,...)

**All endpoints and supported features are documented and available for interactive exploration at the `/docs` endpoint using the built-in OpenAPI (Swagger) UI.**

## Installation

### API Server Setup

Requires Python 3.10+ (tested through 3.14).

1. **Create a virtual environment:**

   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. **Install dependencies:**

   ```bash
   python3 install.py
   ```

   This installs the packages listed in `requirements.txt` and, if `pyrealsense2`
   is not already importable, also pulls it from PyPI. If you already have
   `pyrealsense2` installed (locally built, system package, or any pip install),
   the script leaves it untouched.

3. **Run API server:**

   You can start the server using either method:
   
   ```bash
   python main.py
   ```
   
   Or use the shell script (Linux/macOS):
   
   ```bash
   ./start_server.sh
   ```

   The API server will start on `http://localhost:8000`

4. **Verify the API:**

   Open your browser and navigate to `http://localhost:8000/docs` to explore the API documentation via Swagger UI.

### React Viewer Setup (Optional)

For a modern web-based UI to interact with RealSense devices:

1. **Prerequisites:**
   - Node.js 18+ and npm installed

2. **Navigate to the React viewer directory:**

   ```bash
   cd tools/react-viewer
   ```

3. **Install dependencies:**

   ```bash
   npm install
   ```

4. **Configure environment (if needed):**

   Copy `.env.example` to `.env` and adjust settings if your API server runs on a different port.

## Usage

### Running with React Viewer

1. **Start the REST API server** (in one terminal):

   ```bash
   # From wrappers/rest-api directory
   python main.py
   ```

2. **Start the React viewer** (in another terminal):

   ```bash
   # From wrappers/rest-api/tools/react-viewer directory
   npm run dev
   ```

3. **Open in browser:**

   Navigate to `http://localhost:3000`

For more details about the React viewer features and development, see `tools/react-viewer/README.md`.

### Person Tracker sample

`examples/person-tracker` is a small web app built on this API for cameras with on-camera person
detection (e.g. D585). It shows the persons in color over the camera's aligned depth (D585),
colors each box by distance and shows count / min / max / average distance. The server also tracks
each person, so clicking a box names that person and the name follows them, even after they leave
and come back. Moving persons get a speed and direction arrow, the camera's center-of-mass point of
each person is marked, and a top-down map shows where everyone is. When the camera has an IMU, the
page also streams it and warns while the camera itself moves. The depth background (color instead
of depth when off), a small depth window and a data pipeline card are on by default; a privacy blur
of everything outside the persons is off. Color and depth stream at one selectable resolution
(1280x720 by default). The pipeline card illustrates the layers:
the camera's raw sensors and on-camera perception, host perception (tracking and velocity, planned to
move to the camera), and a placeholder for scene understanding, with frames flowing while streaming.
Each layer has a toggle.

```bash
cd examples/person-tracker && npm install && npm run build
```

The server serves the build at `http://localhost:8000/samples/person-tracker/` (restart it after
the first build). The page starts color + `depth` + `object_detection` (+ `gyro` and `accel` when
present) with `POST /devices/{id}/stream/start`, after setting the depth sensor's `enable_aligned_depth`
option when the camera has it (it can change only while depth is stopped). Depth always streams, so
the depth layers only show or hide it. Color and depth share the selected resolution, so they match
pixel for pixel (at 1280x960, D585 firmware before 7.62 reports persons without a distance). It plays color and
depth over WebRTC and draws the server's `person_detections` Socket.IO events, sent once per
detection frame:

```json
{ "device_id": "...", "od_frame_number": 165, "color_frame_number": 230,
  "image": {"width": 1280, "height": 960},
  "camera_moving": false,
  "persons": [{"bbox": [688, 314, 993, 693], "score": 85, "distance_m": 1.26, "world": [0.55, -0.23, 1.11],
               "com_px": [842.0, 401.3],
               "track_id": 4, "name": "Alice", "held": false,
               "velocity": {"position": [0.55, -0.23, 1.11], "vx": 0.9, "vy": 0.0, "vz": -0.4,
                            "speed_mps": 0.98, "heading_deg": 114.0, "moving": true}}],
  "stats": {"count": 1, "min_m": 1.26, "max_m": 1.26, "avg_m": 1.26} }
```

Boxes are in color-image pixels. `world` is the camera's 3D center of mass of the person in meters
(camera frame) and `com_px` its color-image pixel, both `null` when the camera reports none.
`camera_moving` comes from the camera's gyro and accelerometer: `true` while it turns, shakes or is
carried (person velocities are then relative to the camera), `null` without IMU data. The server keeps persons only and drops overlapping boxes the same way
realsense-viewer does. Tune that with `GET/PUT /devices/{id}/person-analytics/config`.

Tracking (`"tracking": true` in that config, the default) matches each detection to a person by
box overlap, 3D position and clothing color. `track_id` is `null` until a person has been seen for a
few frames. Name a person with `PUT /devices/{id}/person-analytics/tracks/{track_id}` and
`{"name": "Alice"}` (`null` removes it). `GET .../tracks` lists who is here and who was seen recently.
Named persons' clothing colors are saved in `~/.realsense/person-tracker-identities.json`, so they
are recognized again after a restart. List and forget them with `GET .../identities` and
`DELETE .../identities/{name}`. Clothing color only tells apart people who dress differently.

`velocity` (`"velocity": true` in the config, the default; needs tracking) comes from a Kalman
filter on each tracked person's 3D position, timed by the camera's frame timestamps. It is `null`
for the first few frames of a track. The filter runs on the full 3D position (the center of mass
above, or the box center at the camera's distance when there is none): `position` is the filtered
position, `vx`/`vy`/`vz` the 3D velocity and `speed_mps` its magnitude. `heading_deg` is on the floor
plane (0° = away from the camera, 90° = to its right), and `moving` is false below 0.2 m/s. One-frame position glitches from the camera are ignored.
`smoothing` (`"smoothing": true` in the config, the default; needs tracking) steadies each tracked
person's box and distance with a One Euro filter: strong smoothing while a person stands still, almost
none while they move, so boxes stop jittering without lagging behind movement. A confirmed person also
stays while scored up to 10 below `nms_score`, and through up to 3 OD frames the camera misses (`held`:
true, the box moved on at its last speed), so boxes do not flicker. The page's "Stable boxes" switch
turns it off to compare.
`image.intrinsics` gives the color camera's focal lengths and principal point for drawing the
vectors on the image.

#### On the camera or on the host

The page's Compute switches run depth alignment and person detection either on the camera (the
default) or on this host, to show what the camera saves it. Switching restarts the stream.

* **Depth alignment on the host**: the camera's `enable_aligned_depth` is turned off and the stream is
  started with `"align_to": "color"`, so the server aligns depth with the SDK.
* **Person detection on the host**: `PUT .../person-analytics/config` with `"detection": "host"` (and
  `"host_device"`: `cpu`, or `gpu` with `onnxruntime-directml`). The camera's `object_detection`
  stream is not started; the server runs the camera's own model (DAMO-YOLO,
  `damo-epoch233-mean_ap0.668_inp_416_batch_1_bench.onnx`) on each color frame it has time for, and
  finds each person's distance and center of mass in the aligned depth with a port of
  `common/utilities/com/center-of-mass.cpp`, the code the viewer uses for the camera's calculation.
  Events then carry `"source": "host"` and `host_detection_ms`.
  It needs ONNX Runtime (`pip install onnxruntime`) and the model file at
  `~/.realsense/models/` (`PERSON_DETECTOR_MODEL` in `config.py`); the model is internal and is not
  part of this repository. `GET .../person-analytics/host-detection` says whether it can run and
  why not; without it, only the camera's detection is offered.

Measured on an 8-core Lunar Lake laptop (other apps running) at 1280x720, one person, as % of the
whole machine:

| Alignment / detection | Server process CPU | Host detection | Host alignment | Detections/s |
|---|---|---|---|---|
| camera / camera | 18.6% | - | - | 27.6 |
| host / camera | 19.9% | - | 1.2% | 27.8 |
| camera / host (CPU) | 28.6% | 10.4% | - | 14.6 |
| host / host (CPU) | 31.0% | 10.0% | 1.5% | 13.6 |

Host detection runs ONNX Runtime on one thread (about 35 ms per frame here): its default thread pool
was only 1.5x faster for 4x the CPU.

`GET /system/usage` reports this server process's share of the host, sampled once a second while
asked: CPU (% of the whole machine, and the parts spent on host person processing, detection and
alignment), memory, and on
Windows its GPU and NPU use from the per-process "GPU Engine" counters. Anything the machine cannot
measure (no `psutil`, no GPU or NPU, another OS) is `null`. The Person Tracker shows it under the
video: detection, center of mass and distance run on the camera, so the host GPU and NPU stay idle and
host perception takes a fraction of a percent of the CPU; most of the server's CPU goes to encoding the
page's video.

### Running Tests

The `run_tests.py` helper at the rest-api root runs both the backend pytest suite and the React viewer Vitest suite:

```bash
# From wrappers/rest-api:
python run_tests.py             # run both backend + frontend
python run_tests.py --backend   # backend pytest only
python run_tests.py --frontend  # react-viewer Vitest only
```

You can also run each suite directly:

```bash
# Backend (FastAPI / pytest)
pytest tests/

# Frontend (React viewer / Vitest)
cd tools/react-viewer && npm test
```

For React viewer end-to-end Playwright tests and full setup details, see
[`tools/react-viewer/tests/INSTALLATION.md`](tools/react-viewer/tests/INSTALLATION.md).

## Advanced Testing

**For comprehensive testing with test extras (from the repository root):**

```bash
pip3 install -r wrappers/rest-api/requirements.txt -r unit-tests/wrappers/rest-api/requirements.txt
```

## Missing features that may be added in the future

1. Post Processing Filters (Advanced)
2. Record and Playback
3. Agent / Ask for assistant
4. Advanced texture mapping
5. Metadata

**Note:** Basic 3D point cloud visualization and IMU data viewing are available in the React viewer.