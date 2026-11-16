// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

import './style.css'
import { findCamera, getConfig, onDetections, openVideo, setConfig, startStream, stopStream, type Detections } from './api'
import { icon, renderIcons } from './icons'
import { People } from './people'
import { DISTANCE_BANDS, UNKNOWN_COLOR, drawFrame, drawMap, personAt } from './render'

// Same expiry as realsense-viewer: older detections are dropped, so a stalled detector blurs everything
const STALE_MS = 200

renderIcons()

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T
const video = $<HTMLVideoElement>('video')
const canvas = $<HTMLCanvasElement>('canvas')
const ctx = canvas.getContext('2d')!
const mapCanvas = $<HTMLCanvasElement>('map')
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

// Tracking and velocity run on the server; the others only change what this page draws
type Toggle = 'blur' | 'boxes' | 'stats' | 'com' | 'tracking' | 'velocity' | 'map'
const toggles: Record<Toggle, boolean> = {
  blur: true,
  boxes: true,
  stats: true,
  com: true,
  tracking: true,
  velocity: true,
  map: true,
}
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
    showMap()
    if (name === 'tracking' || name === 'velocity') applyServerConfig()
    syncVelocityToggle()
    try {
      localStorage.setItem('person-tracker-toggles', JSON.stringify(toggles))
    } catch {
      /* ignore */
    }
  }
}
statsEl.hidden = !toggles.stats

// Minimum detection confidence: the server's threshold, so boxes, stats and tracking agree
const minScore = $<HTMLInputElement>('min-score')
try {
  minScore.value = localStorage.getItem('person-tracker-min-score') ?? minScore.value
} catch {
  /* storage unavailable: keep the default */
}
const showMinScore = () => ($('min-score-value').textContent = `${minScore.value}%`)
showMinScore()
let minScoreTimer = 0
minScore.oninput = () => {
  showMinScore()
  clearTimeout(minScoreTimer)
  minScoreTimer = window.setTimeout(() => {
    try {
      localStorage.setItem('person-tracker-min-score', minScore.value)
    } catch {
      /* ignore */
    }
    applyServerConfig()
  }, 150)
}

/** Draw the top-down map card, its canvas kept at the card's on-screen size. */
function renderMap(d: Detections | null) {
  const w = Math.round(mapCanvas.clientWidth * devicePixelRatio)
  const h = Math.round(mapCanvas.clientHeight * devicePixelRatio)
  if (!w || !h) return
  if (mapCanvas.width !== w || mapCanvas.height !== h) {
    mapCanvas.width = w
    mapCanvas.height = h
  }
  drawMap(mapCanvas.getContext('2d')!, d?.persons ?? [], d?.image ?? null)
}

function showMap() {
  $('map-card').hidden = !toggles.map
  renderMap(null)
}
showMap()

const swatch = (c: string, label: string) =>
  `<span><b style="border-color:${c};background:${c}33"></b>${label}</span>`
$('legend').innerHTML =
  DISTANCE_BANDS.map(([, c, label]) => swatch(c, label)).join('') + swatch(UNKNOWN_COLOR, 'Unknown')

let latest: { d: Detections; at: number } | null = null
let session: { deviceId: string; close: () => void } | null = null
const people = new People(showError)

// Velocity needs tracking: its toggle is greyed out while tracking is off
function syncVelocityToggle() {
  document.querySelector('[data-toggle="velocity"]')!.classList.toggle('needs', !toggles.tracking)
}
syncVelocityToggle()

async function applyServerConfig() {
  if (!session) return
  const id = session.deviceId
  try {
    await setConfig(id, {
      ...(await getConfig(id)),
      nms_score: Number(minScore.value),
      tracking: toggles.tracking,
      velocity: toggles.velocity,
    })
  } catch (e) {
    showError((e as Error).message)
  }
}

const freshDetections = () => (latest && performance.now() - latest.at < STALE_MS ? latest.d : null)

/** The tracked person under a mouse event on the canvas, if any. */
function trackedPersonAt(e: MouseEvent) {
  const d = freshDetections()
  if (!d?.image) return null
  const r = canvas.getBoundingClientRect()
  const p = personAt(d.persons, d.image, canvas, ((e.clientX - r.left) * canvas.width) / r.width,
    ((e.clientY - r.top) * canvas.height) / r.height)
  return p?.track_id != null ? p : null
}

canvas.onmousemove = (e) => canvas.classList.toggle('taggable', !!trackedPersonAt(e))
canvas.onclick = (e) => {
  const p = trackedPersonAt(e)
  if (!p) return people.closeTagger()
  const v = canvas.parentElement!.getBoundingClientRect()
  people.openTagger(p.track_id!, p.name, { x: e.clientX - v.left, y: e.clientY - v.top })
}

// Camera movement, from the IMU: a toast while it moves, and a status line under the camera
let cameraMoving: boolean | null = null
let toastTimer = 0
function showCameraMotion(moving: boolean | null) {
  if (moving === cameraMoving) return
  cameraMoving = moving
  const imu = $('imu')
  imu.hidden = moving === null
  imu.dataset.state = moving ? 'moving' : 'steady'
  $('imu-text').textContent = moving ? 'Camera moving: speeds are relative' : 'Camera steady (IMU)'
  clearTimeout(toastTimer)
  if (moving) $('toast').hidden = false
  else toastTimer = window.setTimeout(() => ($('toast').hidden = true), 1500)
}

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
    const fresh = freshDetections()
    const image = fresh?.image ?? { width: canvas.width, height: canvas.height }
    drawFrame(ctx, video, fresh?.persons ?? [], image, toggles, cameraMoving === true)
    if (toggles.map) renderMap(fresh)
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
    $('device-image').hidden = !/d585/i.test(cam.name) // product picture, when we have one
    $('device-detail').textContent = `Color ${color.resolution.width}×${color.resolution.height} @ ${color.framerate} fps`
    await startStream(cam.deviceId, cam.configs)
    const unsubscribe = onDetections(cam.deviceId, (d) => {
      latest = { d, at: performance.now() }
      showStats(d)
      showCameraMotion(d.camera_moving)
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
    await applyServerConfig()
    people.follow(cam.deviceId)
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
  people.follow(null)
  showCameraMotion(null)
  setState('idle', 'Idle')
  renderMap(null)
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
