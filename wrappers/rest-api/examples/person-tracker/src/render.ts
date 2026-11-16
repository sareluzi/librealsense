// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

// Draws one video frame with the privacy blur, the distance-colored person boxes and the velocity
// arrows, and the top-down map on its own canvas.

import type { ImageInfo, Intrinsics, Person } from './api'

export interface Layers {
  blur: boolean
  depthBg: boolean
  boxes: boolean
  com: boolean
  velocity: boolean
}

const ARROW_SECONDS = 1 // arrows point to where a person will be in this long
const MAP_RANGE_M = 6

// Upper bound (m) of each distance band and its box color; unknown distance is grey
export const DISTANCE_BANDS: [number, string, string][] = [
  [1, '#ff3b30', '< 1 m'],
  [2, '#ff9500', '1-2 m'],
  [3, '#ffd60a', '2-3 m'],
  [5, '#34c759', '3-5 m'],
  [Infinity, '#0a84ff', '> 5 m'],
]
export const UNKNOWN_COLOR = '#8e8e93'

export const distanceColor = (d: number | null) =>
  d === null ? UNKNOWN_COLOR : DISTANCE_BANDS.find(([max]) => d < max)![1]

// A stable color per identity: by name when named (so it survives restarts), else by track ID
const IDENTITY_COLORS = ['#bf5af2', '#64d2ff', '#ff375f', '#30d158', '#ffd60a', '#ff9f0a', '#5e5ce6', '#ac8e68']
export function identityColor(name: string | null, trackId: number) {
  const key = name ?? String(trackId)
  let h = 0
  for (const c of key) h = (h * 31 + c.charCodeAt(0)) >>> 0
  return IDENTITY_COLORS[h % IDENTITY_COLORS.length]
}

/** Who a person is, for labels: their name, `#id`, or null while unconfirmed or untracked. */
export const who = (p: Person) => (p.track_id === null ? null : (p.name ?? `#${p.track_id}`))

/** The person under canvas pixel (x, y), the smallest box if several overlap. */
export function personAt(persons: Person[], image: ImageInfo, canvas: HTMLCanvasElement,
                         x: number, y: number): Person | null {
  const ix = (x * image.width) / canvas.width
  const iy = (y * image.height) / canvas.height
  const area = (p: Person) => (p.bbox[2] - p.bbox[0]) * (p.bbox[3] - p.bbox[1])
  return persons
    .filter((p) => ix >= p.bbox[0] && ix <= p.bbox[2] && iy >= p.bbox[1] && iy <= p.bbox[3])
    .sort((a, b) => area(a) - area(b))[0] ?? null
}

// The blur is a heavy downscale then a smooth upscale: cheap at any resolution
const BLUR_DOWNSCALE = 16
const small = document.createElement('canvas')

/**
 * Draw `video` onto `ctx` (canvas already sized to the video) with `persons`, whose boxes are in
 * `image`-sized pixels. Outside the boxes the background is the color video, or `depth` (the camera's
 * depth aligned to color) with the depth background on; with blur on it is blurred, including the
 * whole frame when there are no (fresh) detections.
 */
export function drawFrame(
  ctx: CanvasRenderingContext2D,
  video: HTMLVideoElement,
  persons: Person[],
  image: ImageInfo,
  layers: Layers,
  cameraMoving = false,
  depth: HTMLVideoElement | null = null,
) {
  const { width, height } = ctx.canvas
  const sx = width / image.width
  const sy = height / image.height
  const rects = persons.map((p) => {
    const [x1, y1, x2, y2] = p.bbox
    return [x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy] as const
  })

  const background = layers.depthBg && depth?.videoWidth ? depth : video
  if (layers.blur) {
    small.width = Math.max(1, Math.round(width / BLUR_DOWNSCALE))
    small.height = Math.max(1, Math.round(height / BLUR_DOWNSCALE))
    const sctx = small.getContext('2d')!
    sctx.filter = 'blur(1px)'
    sctx.drawImage(background, 0, 0, small.width, small.height)
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(small, 0, 0, width, height)
  } else {
    ctx.drawImage(background, 0, 0, width, height)
  }
  if (layers.blur || background !== video) {
    // Paste the sharp color person regions back, in video pixels
    const vx = video.videoWidth / width
    const vy = video.videoHeight / height
    for (const [x, y, w, h] of rects) ctx.drawImage(video, x * vx, y * vy, w * vx, h * vy, x, y, w, h)
  }

  const font = Math.max(12, Math.round(height / 40))
  ctx.font = `600 ${font}px system-ui, sans-serif`
  ctx.lineWidth = Math.max(2, height / 240)
  if (layers.boxes) persons.forEach((p, i) => {
    const [x, y, w, h] = rects[i]
    const color = distanceColor(p.distance_m)
    ctx.strokeStyle = color
    ctx.strokeRect(x, y, w, h)
    const name = who(p)
    const label = [name, p.distance_m === null ? '?' : p.distance_m.toFixed(2) + ' m', `${p.score}%`]
      .filter(Boolean)
      .join(' · ')
    const dot = name ? font * 0.8 : 0 // identity color dot before the text
    const tw = ctx.measureText(label).width + 10 + (dot && dot + 6)
    const ty = y >= font + 6 ? y - font - 6 : y // above the box, or inside it at the top edge
    const lx = Math.max(0, Math.min(x, width - tw)) // kept inside the frame
    ctx.fillStyle = color
    ctx.fillRect(lx, ty, tw, font + 6)
    if (dot) {
      ctx.beginPath()
      ctx.arc(lx + 5 + dot / 2, ty + (font + 6) / 2, dot / 2, 0, 2 * Math.PI)
      ctx.fillStyle = identityColor(p.name, p.track_id!)
      ctx.fill()
      ctx.strokeStyle = '#000'
      ctx.lineWidth = 1.5
      ctx.stroke()
      ctx.lineWidth = Math.max(2, height / 240)
    }
    ctx.fillStyle = '#000'
    ctx.fillText(label, lx + 5 + (dot && dot + 6), ty + font)
  })
  if (layers.com) drawCentersOfMass(ctx, persons, image)
  if (layers.velocity) drawVelocities(ctx, persons, image, font, cameraMoving)
}

const add = (a: number[], b: number[], k: number) => a.map((v, i) => v + b[i] * k)

/** Image pixel of a 3D camera point, or null behind (or too close to) the camera. */
function project(intr: Intrinsics, [x, y, z]: number[]): [number, number] | null {
  return z > 0.2 ? [(intr.fx * x) / z + intr.ppx, (intr.fy * y) / z + intr.ppy] : null
}

function arrow(ctx: CanvasRenderingContext2D, [x1, y1]: number[], [x2, y2]: number[], width: number) {
  const angle = Math.atan2(y2 - y1, x2 - x1)
  const head = width * 4
  ctx.beginPath()
  ctx.moveTo(x1, y1)
  ctx.lineTo(x2, y2)
  ctx.moveTo(x2 - head * Math.cos(angle - 0.45), y2 - head * Math.sin(angle - 0.45))
  ctx.lineTo(x2, y2)
  ctx.lineTo(x2 - head * Math.cos(angle + 0.45), y2 - head * Math.sin(angle + 0.45))
  // Dark outline under a white line: visible on any background
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  ctx.strokeStyle = 'rgba(0,0,0,0.75)'
  ctx.lineWidth = width + 3
  ctx.stroke()
  ctx.strokeStyle = '#fff'
  ctx.lineWidth = width
  ctx.stroke()
}

/** "1.4 m/s", with "toward" / "away" when most of the motion is along the camera axis, "relative" while the camera moves. */
export function speedLabel(v: NonNullable<Person['velocity']>, relative = false) {
  const along = Math.abs(v.vz) > 0.7 * v.speed_mps ? (v.vz < 0 ? ' · toward' : ' · away') : ''
  return `${v.speed_mps.toFixed(1)} m/s${along}${relative ? ' · relative' : ''}`
}

/** The camera's center-of-mass point of each person: a glowing dot ringed in the box color. */
function drawCentersOfMass(ctx: CanvasRenderingContext2D, persons: Person[], image: ImageInfo) {
  const sx = ctx.canvas.width / image.width
  const sy = ctx.canvas.height / image.height
  const r = Math.max(5, ctx.canvas.height / 110)
  ctx.save()
  for (const p of persons) {
    if (!p.com_px) continue
    const [x, y] = [p.com_px[0] * sx, p.com_px[1] * sy]
    ctx.shadowColor = '#fff'
    ctx.shadowBlur = r * 2.5
    ctx.beginPath()
    ctx.arc(x, y, r, 0, 2 * Math.PI)
    ctx.fillStyle = '#fff'
    ctx.fill()
    ctx.shadowBlur = 0
    ctx.lineWidth = Math.max(2, r / 2.5)
    ctx.strokeStyle = distanceColor(p.distance_m)
    ctx.stroke()
  }
  ctx.restore()
}

/** Velocity arrows of moving persons, from their (filtered) position to where they will be. */
function drawVelocities(ctx: CanvasRenderingContext2D, persons: Person[], image: ImageInfo, font: number, relative: boolean) {
  const intr = image.intrinsics
  if (!intr) return
  const sx = ctx.canvas.width / image.width
  const sy = ctx.canvas.height / image.height
  const lw = Math.max(2, ctx.canvas.height / 200)
  ctx.font = `600 ${font}px system-ui, sans-serif`
  for (const p of persons) {
    const v = p.velocity
    if (!v?.moving) continue
    const a = project(intr, v.position)
    const b = project(intr, add(v.position, [v.vx, v.vy, v.vz], ARROW_SECONDS))
    if (!a || !b) continue
    const from = [a[0] * sx, a[1] * sy]
    const to = [b[0] * sx, b[1] * sy]
    arrow(ctx, from, to, lw)
    const label = speedLabel(v, relative)
    const tw = ctx.measureText(label).width + 10
    const lx = Math.max(0, Math.min(to[0] + 6, ctx.canvas.width - tw))
    const ly = Math.max(0, to[1] - font - 4)
    ctx.fillStyle = 'rgba(0,0,0,0.7)'
    ctx.fillRect(lx, ly, tw, font + 6)
    ctx.fillStyle = '#fff'
    ctx.fillText(label, lx + 5, ly + font)
  }
}

/** Top view on its own canvas: the camera at the bottom center, each person as a dot. `image` gives the field of view. */
export function drawMap(ctx: CanvasRenderingContext2D, persons: Person[], image: ImageInfo | null) {
  const { width, height } = ctx.canvas
  const size = Math.min(width, height)
  const pad = Math.round(size * 0.05)
  const cx = width / 2
  const cy = height - pad
  const scale = Math.min(cy - pad, width / 2 - pad) / MAP_RANGE_M
  const font = Math.max(11, Math.round(size / 26))
  const small = Math.max(10, Math.round(font * 0.85))

  ctx.save()
  ctx.clearRect(0, 0, width, height)

  // Range rings every meter, labelled every 2
  ctx.font = `500 ${small}px system-ui, sans-serif`
  ctx.lineWidth = 1
  for (let r = 1; r <= MAP_RANGE_M; r++) {
    ctx.strokeStyle = r % 2 ? 'rgba(255,255,255,0.08)' : 'rgba(255,255,255,0.18)'
    ctx.beginPath()
    ctx.arc(cx, cy, r * scale, Math.PI, 2 * Math.PI)
    ctx.stroke()
    if (r % 2 === 0) {
      ctx.fillStyle = 'rgba(255,255,255,0.45)'
      ctx.fillText(`${r} m`, cx + 4, cy - r * scale + small + 2)
    }
  }
  // Field of view
  if (image?.intrinsics) {
    const half = Math.atan(image.width / 2 / image.intrinsics.fx)
    const reach = MAP_RANGE_M * scale * 1.2
    ctx.strokeStyle = 'rgba(47,151,227,0.5)'
    ctx.beginPath()
    ctx.moveTo(cx - reach * Math.sin(half), cy - reach * Math.cos(half))
    ctx.lineTo(cx, cy)
    ctx.lineTo(cx + reach * Math.sin(half), cy - reach * Math.cos(half))
    ctx.stroke()
  }
  ctx.fillStyle = '#2f97e3'
  ctx.fillRect(cx - 6, cy - 3, 12, 6) // the camera

  const toMap = (x: number, z: number) => [cx + x * scale, cy - z * scale]
  for (const p of persons) {
    const v = p.velocity
    const pos = v?.position ?? p.world
    if (!pos) continue
    const [px, py] = toMap(pos[0], pos[2])
    if (v?.moving) arrow(ctx, [px, py], toMap(pos[0] + v.vx * ARROW_SECONDS, pos[2] + v.vz * ARROW_SECONDS), 2)
    ctx.font = `600 ${font}px system-ui, sans-serif`
    ctx.beginPath()
    ctx.arc(px, py, Math.max(5, size / 45), 0, 2 * Math.PI)
    ctx.fillStyle = p.track_id === null ? distanceColor(p.distance_m) : identityColor(p.name, p.track_id)
    ctx.fill()
    ctx.strokeStyle = '#000'
    ctx.lineWidth = 1.5
    ctx.stroke()
    const name = who(p)
    if (name) {
      ctx.fillStyle = '#fff'
      ctx.fillText(name, px + 8, py - 6)
    }
  }
  ctx.restore()
}
