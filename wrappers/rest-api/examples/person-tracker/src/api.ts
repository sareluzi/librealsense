// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

// The REST, WebRTC and Socket.IO calls this sample makes against the RealSense REST API.

import { io } from 'socket.io-client'

const API = '/api/v1'

export interface Person {
  bbox: [number, number, number, number] // x1, y1, x2, y2 in color-image pixels
  score: number
  distance_m: number | null
  world: [number, number, number] | null
}

export interface Detections {
  device_id: string
  od_frame_number: number
  color_frame_number: number | null
  image: { width: number; height: number } | null
  persons: Person[]
  stats: { count: number; min_m: number | null; max_m: number | null; avg_m: number | null }
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

/** First camera with an object detection stream, and the color + OD configs to start on it. */
export async function findCamera(): Promise<{ deviceId: string; name: string; configs: StreamConfig[] }> {
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
    // RGB8 at the OD rate, the largest one no wider than 1280 so the blur stays cheap
    const rgb = color.profiles.filter((p) => p.format.toLowerCase() === 'rgb8' && p.fps === odProfile.fps)
    const colorProfile = rgb.filter((p) => p.width <= 1280).sort((a, b) => b.width - a.width)[0] ?? rgb[0]
    if (!colorProfile) continue
    return {
      deviceId: dev.device_id,
      name: dev.name,
      configs: [toConfig(color.sensor.sensor_id, colorProfile), toConfig(od.sensor.sensor_id, odProfile)],
    }
  }
  throw new Error('No connected camera has an object detection stream')
}

export const startStream = (deviceId: string, configs: StreamConfig[]) =>
  request('POST', `/devices/${deviceId}/stream/start`, { configs })

export const stopStream = (deviceId: string) => request('POST', `/devices/${deviceId}/stream/stop`)

/** Subscribe to the server's per-OD-frame person results. Returns the unsubscribe function. */
export function onDetections(deviceId: string, callback: (d: Detections) => void): () => void {
  const socket = io({ path: '/socket', transports: ['websocket', 'polling'], tryAllTransports: true })
  socket.on('person_detections', (d: Detections) => d.device_id === deviceId && callback(d))
  return () => socket.disconnect()
}

/** Play the device's color stream into `video` over WebRTC. Returns the close function. */
export async function openVideo(deviceId: string, video: HTMLVideoElement): Promise<() => void> {
  const pc = new RTCPeerConnection()
  pc.ontrack = (e) => (video.srcObject = e.streams[0] ?? new MediaStream([e.track]))

  // The server makes the offer; we answer and exchange ICE candidates over REST
  const offer = await request<{ session_id: string; sdp: string; type: RTCSdpType }>('POST', '/webrtc/offer', {
    device_id: deviceId,
    stream_types: ['color'],
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
