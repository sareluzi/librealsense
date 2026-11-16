// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

// The data pipeline card: the layers of data from the camera's raw sensors, through on-camera and host
// perception, to scene understanding, with frames flowing up between them while streaming.

import type { Detections } from './api'
import { icon, type IconName } from './icons'

export interface PipelineSetup {
  color: string // "1280×720 @ 30 fps"
  depth: 'none' | 'raw' | 'aligned'
  imu: boolean
}

const item = (id: string, name: IconName, label: string) =>
  `<div class="pl-item" id="pl-${id}">${icon(name, 15)}<span>${label}</span><b></b></div>`

// Frames flowing up: one lane per stream color
const flow = (lanes: string[]) =>
  `<div class="pl-flow">${lanes.map((c, i) => `<span style="--c:${c};--d:${i * 0.23}s"></span>`).join('')}</div>`

const MARKUP = `
  <div class="pl-layer pl-planned">
    <h3>${icon('sparkles', 15)}Scene understanding<span class="pl-badge">Planned</span></h3>
    <p class="muted">Empty for now: rooms, objects and activities around the people.</p>
  </div>
  ${flow(['var(--pl-host)'])}
  <div class="pl-zone">
    <div class="pl-zone-title">${icon('cpu', 14)}Host · REST server</div>
    <div class="pl-layer pl-host">
      <h3>Host perception<span class="pl-badge pl-soon" title="These run on the host today and will move to on-camera compute in later versions">${icon('upload', 12)}To camera later</span></h3>
      ${item('tracking', 'tracking', 'Tracking &amp; re-ID')}
      ${item('velocity', 'velocity', '3D velocity')}
    </div>
  </div>
  <div class="pl-link">${flow(['var(--pl-cam)', 'var(--pl-cam)'])}<span class="pl-link-label">USB · detections + frames</span></div>
  <div class="pl-zone pl-camera">
    <div class="pl-zone-title">${icon('video', 14)}<span id="pl-camera-name">Camera</span> · on-camera compute</div>
    <div class="pl-layer pl-cam">
      <h3>On-camera perception</h3>
      ${item('detect', 'boxes', 'Person detection')}
      ${item('com', 'com', 'Center of mass')}
      ${item('distance', 'ruler', 'Distance')}
    </div>
    ${flow(['var(--pl-rgb)', 'var(--pl-depth)', 'var(--pl-imu)'])}
    <div class="pl-layer pl-raw">
      <h3>Raw sensors</h3>
      ${item('rgb', 'video', 'RGB')}
      ${item('depth', 'depth', 'Depth')}
      ${item('imu', 'move', 'IMU')}
    </div>
  </div>`

export class Pipeline {
  private setup: PipelineSetup | null = null
  private times: number[] = [] // recent detection event times, for the rate
  private shown = 0

  constructor(private root: HTMLElement) {
    root.innerHTML = MARKUP
    this.idle()
  }

  private set(id: string, value: string, on = true) {
    const el = this.root.querySelector<HTMLElement>(`#pl-${id}`)!
    el.classList.toggle('off', !on)
    el.querySelector('b')!.textContent = value
  }

  idle() {
    this.setup = null
    this.times = []
    this.root.dataset.live = 'false'
    for (const id of ['rgb', 'depth', 'imu', 'detect', 'com', 'distance', 'tracking', 'velocity']) this.set(id, 'Idle', false)
  }

  start(cameraName: string, setup: PipelineSetup) {
    this.setup = setup
    this.root.dataset.live = 'true'
    this.root.querySelector('#pl-camera-name')!.textContent = cameraName.replace(/^RealSense /, '')
    this.set('rgb', setup.color)
    this.set('depth', setup.depth === 'none' ? 'Off' : setup.depth === 'aligned' ? 'Aligned to RGB' : 'Raw', setup.depth !== 'none')
    this.set('imu', setup.imu ? 'Gyro + accel' : 'None', setup.imu)
  }

  /** Refresh the live values from a detection event, at most 4 times a second. */
  update(d: Detections, host: { tracking: boolean; velocity: boolean }) {
    if (!this.setup) return
    const now = performance.now()
    this.times.push(now)
    while (this.times[0] < now - 2000) this.times.shift()
    if (now - this.shown < 250) return
    this.shown = now
    const rate = this.times.length > 1 ? ((this.times.length - 1) * 1000) / (now - this.times[0]) : 0
    const persons = d.persons
    const n = (f: (p: Detections['persons'][0]) => unknown) => persons.filter(f).length

    if (this.setup.imu) this.set('imu', d.camera_moving ? 'Camera moving' : 'Steady')
    this.set('detect', `${persons.length} · ${rate.toFixed(0)} fps`)
    this.set('com', persons.length ? `${n((p) => p.com_px)} of ${persons.length}` : '–', persons.length > 0)
    this.set('distance', d.stats.min_m === null ? '–' : `${d.stats.min_m.toFixed(2)}–${d.stats.max_m!.toFixed(2)} m`, d.stats.min_m !== null)
    const tracked = n((p) => p.track_id !== null)
    this.set('tracking', host.tracking ? `${tracked} tracked · ${n((p) => p.name)} named` : 'Off', host.tracking)
    const velocity = host.tracking && host.velocity
    this.set('velocity', velocity ? `${n((p) => p.velocity?.moving)} moving` : 'Off', velocity)
  }
}
