// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

// Draws one video frame with the privacy blur and the distance-colored person boxes.

import type { Person } from './api'

export interface Layers {
  blur: boolean
  boxes: boolean
}

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

// The blur is a heavy downscale then a smooth upscale: cheap at any resolution
const BLUR_DOWNSCALE = 16
const small = document.createElement('canvas')

/**
 * Draw `video` onto `ctx` (canvas already sized to the video) with `persons`, whose boxes are in
 * `image`-sized pixels. With blur on, everything outside the boxes is blurred, including the whole
 * frame when there are no (fresh) detections.
 */
export function drawFrame(
  ctx: CanvasRenderingContext2D,
  video: HTMLVideoElement,
  persons: Person[],
  image: { width: number; height: number },
  layers: Layers,
) {
  const { width, height } = ctx.canvas
  const sx = width / image.width
  const sy = height / image.height
  const rects = persons.map((p) => {
    const [x1, y1, x2, y2] = p.bbox
    return [x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy] as const
  })

  if (layers.blur) {
    small.width = Math.max(1, Math.round(width / BLUR_DOWNSCALE))
    small.height = Math.max(1, Math.round(height / BLUR_DOWNSCALE))
    const sctx = small.getContext('2d')!
    sctx.filter = 'blur(1px)'
    sctx.drawImage(video, 0, 0, small.width, small.height)
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(small, 0, 0, width, height)
    // Paste the sharp person regions back, in video pixels
    const vx = video.videoWidth / width
    const vy = video.videoHeight / height
    for (const [x, y, w, h] of rects) ctx.drawImage(video, x * vx, y * vy, w * vx, h * vy, x, y, w, h)
  } else {
    ctx.drawImage(video, 0, 0, width, height)
  }

  if (!layers.boxes) return
  const font = Math.max(12, Math.round(height / 40))
  ctx.font = `600 ${font}px system-ui, sans-serif`
  ctx.lineWidth = Math.max(2, height / 240)
  persons.forEach((p, i) => {
    const [x, y, w, h] = rects[i]
    const color = distanceColor(p.distance_m)
    ctx.strokeStyle = color
    ctx.strokeRect(x, y, w, h)
    const label = `${p.distance_m === null ? '?' : p.distance_m.toFixed(2) + ' m'} · ${p.score}%`
    const tw = ctx.measureText(label).width + 10
    const ty = y >= font + 6 ? y - font - 6 : y // above the box, or inside it at the top edge
    ctx.fillStyle = color
    ctx.fillRect(x, ty, tw, font + 6)
    ctx.fillStyle = '#000'
    ctx.fillText(label, x + 5, ty + font)
  })
}
