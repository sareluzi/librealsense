// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

// The REST, WebRTC and Socket.IO calls this sample makes against the RealSense REST API.

import { io } from 'socket.io-client'

const API = '/api/v1'

export interface Person {
  bbox: [number, number, number, number] // x1, y1, x2, y2 in color-image pixels
  score: number
  distance_m: number | null
  world: [number, number, number] | null // the camera's 3D center of mass, m
  com_px: [number, number] | null // ... and its pixel in the color image
  track_id: number | null // null until the track is confirmed, or with tracking off
  name: string | null
  velocity: Velocity | null // null until settled, or with tracking or velocity off
  held: boolean // missed by the detector this frame: kept where it was heading (smoothing)
}

export interface Velocity {
  position: [number, number, number] // filtered 3D position, m (x right, y down, z forward)
  vx: number
  vy: number
  vz: number
  speed_mps: number // 3D: |(vx, vy, vz)|
  heading_deg: number // 0 = away from the camera, 90 = to its right, 180 = toward it
  moving: boolean
}

export interface Intrinsics {
  fx: number
  fy: number
  ppx: number
  ppy: number
}

export interface ImageInfo {
  width: number
  height: number
  intrinsics?: Intrinsics
}

export interface Track {
  track_id: number
  name: string | null
  present: boolean
  seconds_since_seen: number
}

export interface AnalyticsConfig {
  nms_score: number
  nms_iou: number
  tracking: boolean
  velocity: boolean
  smoothing: boolean
  detection: 'camera' | 'host'
  host_device: string
}

/** Whether the server can detect persons itself (ONNX Runtime + the camera's model), and on which devices. */
export interface HostDetection {
  available: boolean
  devices: string[]
  reason: string | null
}

export interface Detections {
  device_id: string
  od_frame_number: number
  color_frame_number: number | null
  image: ImageInfo | null
  persons: Person[]
  stats: { count: number; min_m: number | null; max_m: number | null; avg_m: number | null }
  camera_moving: boolean | null // from the IMU; null without one
  source?: 'camera' | 'host' // who detected the persons
  host_device?: string
  host_detection_ms?: number // model + distance + center of mass, per frame
}

interface Profile {
  stream_type: string
  format: string
  width: number
  height: number
  fps: number
}

interface Sensor {
  sensor_id: string
  supported_stream_profiles: Profile[]
}

export interface StreamConfig {
  sensor_id: string
  stream_type: string
  format: string
  resolution: { width: number; height: number }
  framerate: number
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(API + path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await res.json().catch(() => null)
  if (!res.ok) throw new Error(data?.detail ?? `${method} ${path}: ${res.status}`)
  return data as T
}

const toConfig = (sensor_id: string, p: Profile): StreamConfig => ({
  sensor_id,
  stream_type: p.stream_type.toLowerCase(),
  format: p.format.toLowerCase(),
  resolution: { width: p.width, height: p.height },
  framerate: p.fps,
})

export interface Resolution {
  width: number
  height: number
}

export interface Camera {
  deviceId: string
  name: string
  resolutions: Resolution[] // color sizes at the OD rate (that depth also has, when there is depth), largest first
  hasDepth: boolean
  alignSensorId: string | null // the sensor with on-camera depth-to-color alignment, if any
  // color, the IMU when present, and depth / the camera's object detection if asked
  streams: (res: Resolution, depth: boolean, od: boolean) => StreamConfig[]
}

/** First camera with an object detection stream, and the streams to start on it. */
export async function findCamera(): Promise<Camera> {
  const devices = await request<{ device_id: string; name: string }[]>('GET', '/devices/')
  for (const dev of devices) {
    const sensors = await request<Sensor[]>('GET', `/devices/${dev.device_id}/sensors/`)
    const pick = (type: string) => {
      for (const s of sensors) {
        const ps = s.supported_stream_profiles.filter((p) => p.stream_type.toLowerCase() === type)
        if (ps.length) return { sensor: s, profiles: ps }
      }
      return null
    }
    const od = pick('object_detection')
    const color = pick('color')
    if (!od || !color) continue
    const odProfile = od.profiles.reduce((a, b) => (b.fps > a.fps ? b : a))
    const atOdRate = (ps: Profile[], format: string) =>
      ps.filter((p) => p.format.toLowerCase() === format && p.fps === odProfile.fps)
    const rgb = atOdRate(color.profiles, 'rgb8')
    // Color and depth always run at one resolution, so aligned depth matches color pixel for pixel
    const depth = pick('depth')
    const z16 = depth ? atOdRate(depth.profiles, 'z16') : []
    const same = (a: Resolution) => (b: Resolution) => a.width === b.width && a.height === b.height
    const resolutions = rgb
      .filter((c) => !z16.length || z16.some(same(c)))
      .map(({ width, height }) => ({ width, height }))
      .sort((a, b) => b.width * b.height - a.width * a.height)
    if (!resolutions.length) continue
    // The IMU, at its lowest rates: enough to tell that the camera moves
    const imu = ['gyro', 'accel'].map((t) => {
      const m = pick(t)
      return m && toConfig(m.sensor.sensor_id, m.profiles.reduce((a, b) => (b.fps < a.fps ? b : a)))
    })
    let alignSensorId: string | null = null
    if (depth && z16.length) {
      const options = await request<{ option_id: string }[]>('GET', `/devices/${dev.device_id}/sensors/${depth.sensor.sensor_id}/options/`)
      if (options.some((o) => o.option_id === 'enable_aligned_depth')) alignSensorId = depth.sensor.sensor_id
    }
    return {
      deviceId: dev.device_id,
      name: dev.name,
      resolutions,
      hasDepth: z16.length > 0,
      alignSensorId,
      streams: (res, withDepth, withOd) => [
        toConfig(color.sensor.sensor_id, rgb.find(same(res))!),
        ...(withOd ? [toConfig(od.sensor.sensor_id, odProfile)] : []),
        ...(imu.every(Boolean) ? (imu as StreamConfig[]) : []),
        ...(withDepth && depth ? [toConfig(depth.sensor.sensor_id, z16.find(same(res))!)] : []),
      ],
    }
  }
  throw new Error('No connected camera has an object detection stream')
}

/** The server process's share of the host; null where the machine cannot measure it. */
export interface Usage {
  cpu_percent: number | null // % of the whole machine
  system_cpu_percent: number | null
  cpu_cores: number | null
  memory_mb: number | null
  gpu_percent: number | null
  npu_percent: number | null
  perception_cpu_percent: number | null // the part of cpu_percent spent on host person processing
  detection_cpu_percent: number | null // ... detecting persons on the host
  alignment_cpu_percent: number | null // ... aligning depth to color on the host
}

export const getUsage = () => request<Usage>('GET', '/system/usage')

/** Start the streams; alignTo has the server align the other streams to that one (e.g. depth to 'color'). */
export const startStream = (deviceId: string, configs: StreamConfig[], alignTo?: string) =>
  request('POST', `/devices/${deviceId}/stream/start`, { configs, align_to: alignTo ?? null })

/** Turn the camera's own depth-to-color alignment on or off; only possible while depth is stopped. */
export const setAlignedDepth = (deviceId: string, sensorId: string, on: boolean) =>
  request('PUT', `/devices/${deviceId}/sensors/${sensorId}/options/enable_aligned_depth/`, { value: on ? 1 : 0 })

export const stopStream = (deviceId: string) => request('POST', `/devices/${deviceId}/stream/stop`)

const analytics = (deviceId: string) => `/devices/${deviceId}/person-analytics`

export const getConfig = (deviceId: string) => request<AnalyticsConfig>('GET', `${analytics(deviceId)}/config`)

export const setConfig = (deviceId: string, config: AnalyticsConfig) =>
  request<AnalyticsConfig>('PUT', `${analytics(deviceId)}/config`, config)

export const getHostDetection = (deviceId: string) =>
  request<HostDetection>('GET', `${analytics(deviceId)}/host-detection`)

export const getTracks = (deviceId: string) => request<Track[]>('GET', `${analytics(deviceId)}/tracks`)

/** Name a tracked person; null removes the name. Returns the updated tracks. */
export const nameTrack = (deviceId: string, trackId: number, name: string | null) =>
  request<Track[]>('PUT', `${analytics(deviceId)}/tracks/${trackId}`, { name })

export const getIdentities = (deviceId: string) => request<string[]>('GET', `${analytics(deviceId)}/identities`)

export const forgetIdentity = (deviceId: string, name: string) =>
  request('DELETE', `${analytics(deviceId)}/identities/${encodeURIComponent(name)}`)

/** Subscribe to the server's per-OD-frame person results. Returns the unsubscribe function. */
export function onDetections(deviceId: string, callback: (d: Detections) => void): () => void {
  const socket = io({ path: '/socket', transports: ['websocket', 'polling'], tryAllTransports: true })
  socket.on('person_detections', (d: Detections) => d.device_id === deviceId && callback(d))
  return () => socket.disconnect()
}

/** Play one of the device's streams (color, or depth colorized by the server) into `video` over WebRTC. Returns the close function. */
export async function openVideo(deviceId: string, video: HTMLVideoElement, stream = 'color'): Promise<() => void> {
  const pc = new RTCPeerConnection()
  pc.ontrack = (e) => (video.srcObject = e.streams[0] ?? new MediaStream([e.track]))

  // The server makes the offer; we answer and exchange ICE candidates over REST
  const offer = await request<{ session_id: string; sdp: string; type: RTCSdpType }>('POST', '/webrtc/offer', {
    device_id: deviceId,
    stream_types: [stream],
  })
  const session = offer.session_id
  const close = () => {
    pc.close()
    request('DELETE', `/webrtc/sessions/${session}`).catch(() => {})
  }
  pc.onicecandidate = (e) => {
    if (e.candidate)
      request('POST', '/webrtc/ice-candidates', {
        session_id: session,
        candidate: e.candidate.candidate,
        sdpMid: e.candidate.sdpMid ?? '',
        sdpMLineIndex: e.candidate.sdpMLineIndex ?? 0,
      }).catch(() => {})
  }
  try {
    await pc.setRemoteDescription(offer)
    const answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    await request('POST', '/webrtc/answer', { session_id: session, sdp: answer.sdp, type: answer.type })
    while (pc.iceConnectionState === 'new' || pc.iceConnectionState === 'checking') {
      const candidates = await request<RTCIceCandidateInit[]>('GET', `/webrtc/sessions/${session}/ice-candidates`)
      for (const c of candidates) await pc.addIceCandidate(c)
      await new Promise((r) => setTimeout(r, 500))
    }
  } catch (e) {
    close()
    throw e
  }
  return close
}
