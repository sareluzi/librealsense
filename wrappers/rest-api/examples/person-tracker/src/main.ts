// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

import './style.css'
import {
  findCamera,
  getConfig,
  onDetections,
  openVideo,
  setAlignedDepth,
  setConfig,
  startStream,
  stopStream,
  losesDistanceWithDepth,
  type Camera,
  type Detections,
  type Resolution,
} from './api'
import { icon, renderIcons } from './icons'
import { People } from './people'
import { Pipeline } from './pipeline'
import { DISTANCE_BANDS, UNKNOWN_COLOR, drawFrame, drawMap, personAt } from './render'

// Same expiry as realsense-viewer: older detections are dropped, so a stalled detector blurs everything
const STALE_MS = 200

renderIcons()

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T
const video = $<HTMLVideoElement>('video')
const depthVideo = $<HTMLVideoElement>('depth-video')
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
type Toggle = 'blur' | 'depthBg' | 'boxes' | 'stats' | 'com' | 'tracking' | 'velocity' | 'map' | 'depth' | 'pipeline'
const toggles: Record<Toggle, boolean> = {
  blur: false,
  depthBg: true,
  boxes: true,
  stats: true,
  com: true,
  tracking: true,
  velocity: true,
  map: true,
  depth: true,
  pipeline: true,
}
// Bumped when the defaults change, so everyone starts from the new ones
const TOGGLES_KEY = 'person-tracker-toggles-v2'
try {
  Object.assign(toggles, JSON.parse(localStorage.getItem(TOGGLES_KEY) ?? '{}'))
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
      localStorage.setItem(TOGGLES_KEY, JSON.stringify(toggles))
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
  $('depth-card').hidden = !toggles.depth
  $('pipeline-card').hidden = !toggles.pipeline
  $('side').hidden = !toggles.map && !toggles.depth && !toggles.pipeline
  renderMap(null)
}
showMap()

const swatch = (c: string, label: string) =>
  `<span><b style="border-color:${c};background:${c}33"></b>${label}</span>`
$('legend').innerHTML =
  DISTANCE_BANDS.map(([, c, label]) => swatch(c, label)).join('') + swatch(UNKNOWN_COLOR, 'Unknown')

let latest: { d: Detections; at: number } | null = null
let session: { deviceId: string; cam: Camera; depthMode: DepthMode; res: string; close: () => void } | null = null

// Color + depth resolution: 1280x720 unless the user picked another
const resolutionEl = $<HTMLSelectElement>('resolution')
const resKey = (r: Resolution) => `${r.width}x${r.height}`
let wantedRes = '1280x720'
try {
  wantedRes = localStorage.getItem('person-tracker-resolution') ?? wantedRes
} catch {
  /* storage unavailable: keep the default */
}
function showResolutions(cam: Camera) {
  resolutionEl.innerHTML = cam.resolutions
    .map((r) => {
      const note = cam.hasDepth && losesDistanceWithDepth(r) ? ' (no person distances)' : ''
      return `<option value="${resKey(r)}">${r.width} × ${r.height}${note}</option>`
    })
    .join('')
  resolutionEl.value = cam.resolutions.some((r) => resKey(r) === wantedRes) ? wantedRes : resKey(cam.resolutions[0])
  resolutionEl.disabled = false
}
const chosenResolution = (cam: Camera) =>
  cam.resolutions.find((r) => resKey(r) === resolutionEl.value) ?? cam.resolutions[0]
resolutionEl.onchange = () => {
  wantedRes = resolutionEl.value
  try {
    localStorage.setItem('person-tracker-resolution', wantedRes)
  } catch {
    /* ignore */
  }
  if (session && session.res !== wantedRes) restart()
}
findCamera().then(showResolutions, () => (resolutionEl.innerHTML = '<option>No camera found</option>'))

// Depth always streams, aligned to color by the camera when it can, so the depth knobs only show or hide
// it: the alignment can change only while depth is stopped.
type DepthMode = 'none' | 'raw' | 'aligned'
const depthMode = (cam: Camera): DepthMode => (!cam.hasDepth ? 'none' : cam.alignSensorId ? 'aligned' : 'raw')

function showDepthNote(cam: Camera | null, mode: DepthMode) {
  $('depth-empty').hidden = mode !== 'none' && !!cam
  $('depth-note').textContent = !cam
    ? 'Near is blue, far is red.'
    : !cam.hasDepth
      ? 'This camera has no depth stream.'
      : mode !== 'none' && losesDistanceWithDepth(chosenResolution(cam))
        ? 'At this resolution the camera reports persons without a distance: pick 1280 × 720 or lower.'
        : mode === 'aligned'
          ? 'Aligned to the color image by the camera. Near is blue, far is red.'
          : 'This camera cannot align depth to color, so there is no depth background. Near is blue, far is red.'
}
showDepthNote(null, 'none')
const people = new People(showError)
const pipeline = new Pipeline($('pipeline'))

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
    const depth = session.depthMode === 'aligned' ? depthVideo : null
    drawFrame(ctx, video, fresh?.persons ?? [], image, toggles, cameraMoving === true, depth)
    if (toggles.map) renderMap(fresh)
    if (!fresh) showStats(null)
  }
  if ('requestVideoFrameCallback' in video) video.requestVideoFrameCallback(render)
  else requestAnimationFrame(render)
}

async function start(known?: Camera) {
  showError(null)
  setState('starting', 'Starting')
  $('device-detail').textContent = 'Looking for a camera with person detection…'
  try {
    const cam = known ?? (await findCamera())
    const mode = depthMode(cam)
    // With depth, color switches to the depth resolution
    showResolutions(cam)
    const res = chosenResolution(cam)
    const configs = cam.streams(res, mode !== 'none')
    const { width, height } = res
    $('device-name').textContent = cam.name
    $('device-image').hidden = !/d585/i.test(cam.name) // product picture, when we have one
    $('device-detail').textContent = `Color ${width}×${height}${mode === 'none' ? '' : ' + depth'} @ ${configs[0].framerate} fps`
    if (cam.alignSensorId && mode !== 'none') await setAlignedDepth(cam.deviceId, cam.alignSensorId, mode === 'aligned')
    await startStream(cam.deviceId, configs)
    const unsubscribe = onDetections(cam.deviceId, (d) => {
      latest = { d, at: performance.now() }
      showStats(d)
      showCameraMotion(d.camera_moving)
      pipeline.update(d, toggles)
    })
    const closers: (() => void)[] = []
    try {
      closers.push(await openVideo(cam.deviceId, video))
      if (mode !== 'none') closers.push(await openVideo(cam.deviceId, depthVideo, 'depth'))
    } catch (e) {
      closers.forEach((c) => c())
      unsubscribe()
      await stopStream(cam.deviceId).catch(() => {})
      throw e
    }
    session = {
      deviceId: cam.deviceId,
      cam,
      depthMode: mode,
      res: resKey(res),
      close: () => {
        unsubscribe()
        closers.forEach((c) => c())
      },
    }
    showDepthNote(cam, mode)
    await applyServerConfig()
    people.follow(cam.deviceId)
    await video.play().catch(() => {})
    if (mode !== 'none') await depthVideo.play().catch(() => {})
    setState('streaming', 'Streaming')
    pipeline.start(cam.name, {
      color: `${width}×${height} @ ${configs[0].framerate} fps`,
      depth: mode,
      imu: configs.some((c) => c.stream_type === 'gyro'),
    })
    render()
  } catch (e) {
    setState('error', 'Error')
    showError((e as Error).message)
  }
}

async function stop() {
  if (!session) return
  const { deviceId, cam, depthMode: mode, close } = session
  session = null
  latest = null
  close()
  people.follow(null)
  showCameraMotion(null)
  depthVideo.srcObject = null
  showDepthNote(null, 'none')
  pipeline.idle()
  setState('idle', 'Idle')
  renderMap(null)
  showStats(null)
  await stopStream(deviceId).catch((e) => {
    setState('error', 'Error')
    showError(e.message)
  })
  // Leave the camera as we found it: other apps expect unaligned depth
  if (mode === 'aligned') await setAlignedDepth(deviceId, cam.alignSensorId!, false).catch(() => {})
}

startBtn.onclick = () => (session ? stop() : start())

async function restart() {
  const cam = session?.cam
  await stop()
  await start(cam)
}
window.addEventListener('pagehide', () => {
  if (session) navigator.sendBeacon(`/api/v1/devices/${session.deviceId}/stream/stop`)
})

setState('idle', 'Idle')
showStats(null)
