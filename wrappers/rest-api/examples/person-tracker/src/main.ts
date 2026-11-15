// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

import './style.css'
import { findCamera, onDetections, openVideo, startStream, stopStream, type Detections } from './api'
import { icon, renderIcons } from './icons'
import { DISTANCE_BANDS, UNKNOWN_COLOR, drawFrame } from './render'

// Same expiry as realsense-viewer: older detections are dropped, so a stalled detector blurs everything
const STALE_MS = 200

renderIcons()

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T
const video = $<HTMLVideoElement>('video')
const canvas = $<HTMLCanvasElement>('canvas')
const ctx = canvas.getContext('2d')!
const startBtn = $<HTMLButtonElement>('start')
const statsEl = $('stats')

type State = 'idle' | 'starting' | 'streaming' | 'error'
function setState(state: State, text: string) {
  $('status').dataset.state = state
  $('status-text').textContent = text
  const streaming = state === 'streaming'
  startBtn.classList.toggle('streaming', streaming)
  startBtn.innerHTML = icon(streaming ? 'stop' : 'play', 22)
  const label = streaming ? 'Stop streaming' : 'Start streaming'
  startBtn.title = label
  startBtn.setAttribute('aria-label', label)
  startBtn.disabled = state === 'starting'
  canvas.hidden = !streaming
  $('placeholder').hidden = streaming
}

function showError(message: string | null) {
  const el = $('error')
  el.hidden = !message
  el.textContent = message ?? ''
}

type Toggle = 'blur' | 'boxes' | 'stats'
const toggles: Record<Toggle, boolean> = { blur: true, boxes: true, stats: true }
try {
  Object.assign(toggles, JSON.parse(localStorage.getItem('person-tracker-toggles') ?? '{}'))
} catch {
  /* storage unavailable: keep defaults */
}

for (const name of Object.keys(toggles) as Toggle[]) {
  const btn = document.querySelector<HTMLButtonElement>(`[data-toggle="${name}"]`)!
  const sync = () => btn.setAttribute('aria-pressed', String(toggles[name]))
  sync()
  btn.onclick = () => {
    toggles[name] = !toggles[name]
    sync()
    statsEl.hidden = !toggles.stats
    try {
      localStorage.setItem('person-tracker-toggles', JSON.stringify(toggles))
    } catch {
      /* ignore */
    }
  }
}
statsEl.hidden = !toggles.stats

const swatch = (c: string, label: string) =>
  `<span><b style="border-color:${c};background:${c}33"></b>${label}</span>`
$('legend').innerHTML =
  DISTANCE_BANDS.map(([, c, label]) => swatch(c, label)).join('') + swatch(UNKNOWN_COLOR, 'Unknown')

let latest: { d: Detections; at: number } | null = null
let session: { deviceId: string; close: () => void } | null = null

const fmt = (m: number | null) => (m === null ? '–' : `${m.toFixed(2)} m`)
const chip = (name: Parameters<typeof icon>[0], label: string, value: string) =>
  `<span class="chip">${icon(name, 16)}<small>${label}</small><strong>${value}</strong></span>`

function showStats(d: Detections | null) {
  const s = d?.stats
  statsEl.innerHTML =
    chip('users', 'Persons', String(s?.count ?? 0)) +
    chip('min', 'Min', fmt(s?.min_m ?? null)) +
    chip('max', 'Max', fmt(s?.max_m ?? null)) +
    chip('avg', 'Avg', fmt(s?.avg_m ?? null))
}

function render() {
  if (!session) return
  if (video.videoWidth) {
    if (canvas.width !== video.videoWidth || canvas.height !== video.videoHeight) {
      canvas.width = video.videoWidth
      canvas.height = video.videoHeight
    }
    const fresh = latest && performance.now() - latest.at < STALE_MS ? latest.d : null
    const image = fresh?.image ?? { width: canvas.width, height: canvas.height }
    drawFrame(ctx, video, fresh?.persons ?? [], image, toggles)
    if (!fresh) showStats(null)
  }
  if ('requestVideoFrameCallback' in video) video.requestVideoFrameCallback(render)
  else requestAnimationFrame(render)
}

async function start() {
  showError(null)
  setState('starting', 'Starting')
  $('device-detail').textContent = 'Looking for a camera with person detection…'
  try {
    const cam = await findCamera()
    const [color] = cam.configs
    $('device-name').textContent = cam.name
    $('device-detail').textContent = `Color ${color.resolution.width}×${color.resolution.height} @ ${color.framerate} fps`
    await startStream(cam.deviceId, cam.configs)
    const unsubscribe = onDetections(cam.deviceId, (d) => {
      latest = { d, at: performance.now() }
      showStats(d)
    })
    let closeVideo: () => void
    try {
      closeVideo = await openVideo(cam.deviceId, video)
    } catch (e) {
      unsubscribe()
      await stopStream(cam.deviceId).catch(() => {})
      throw e
    }
    session = {
      deviceId: cam.deviceId,
      close: () => {
        unsubscribe()
        closeVideo()
      },
    }
    await video.play().catch(() => {})
    setState('streaming', 'Streaming')
    render()
  } catch (e) {
    setState('error', 'Error')
    showError((e as Error).message)
  }
}

async function stop() {
  if (!session) return
  const { deviceId, close } = session
  session = null
  latest = null
  close()
  setState('idle', 'Idle')
  showStats(null)
  await stopStream(deviceId).catch((e) => {
    setState('error', 'Error')
    showError(e.message)
  })
}

startBtn.onclick = () => (session ? stop() : start())
window.addEventListener('pagehide', () => {
  if (session) navigator.sendBeacon(`/api/v1/devices/${session.deviceId}/stream/stop`)
})

setState('idle', 'Idle')
showStats(null)
