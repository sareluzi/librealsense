// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

// The host usage card: how little of the host the server process takes, since detection, center of
// mass and distance run on the camera. Rows the machine cannot measure are left out.

import { getUsage, type Usage } from './api'
import { icon, type IconName } from './icons'

const ROWS: { key: keyof Usage; icon: IconName; label: string }[] = [
  { key: 'cpu_percent', icon: 'cpu', label: 'CPU' },
  { key: 'perception_cpu_percent', icon: 'tracking', label: 'Perception' },
  { key: 'detection_cpu_percent', icon: 'boxes', label: 'Detection' },
  { key: 'alignment_cpu_percent', icon: 'depth', label: 'Alignment' },
  { key: 'gpu_percent', icon: 'monitor', label: 'GPU' },
  { key: 'npu_percent', icon: 'sparkles', label: 'NPU' },
  { key: 'memory_mb', icon: 'layers', label: 'Memory' },
]

const pct = (v: number) => (v < 1 && v > 0 ? v.toFixed(2) : v.toFixed(1)) + '%'

export class UsageCard {
  private timer = 0

  constructor(private el: HTMLElement) {
    el.innerHTML =
      ROWS.map(
        (r) =>
          `<div class="usage-row" data-key="${r.key}" hidden>${icon(r.icon, 15)}<span>${r.label}</span>` +
          `<b></b><small></small><i class="usage-bar"><i></i></i></div>`,
      ).join('') + '<p class="muted usage-note"></p>'
  }

  /** Poll while shown; the server samples once a second. */
  show(on: boolean) {
    clearInterval(this.timer)
    if (!on) return
    const poll = () => getUsage().then((u) => this.render(u), () => this.render(null))
    poll()
    this.timer = window.setInterval(poll, 1000)
  }

  private render(u: Usage | null) {
    let any = false
    for (const r of ROWS) {
      const row = this.el.querySelector<HTMLElement>(`[data-key="${r.key}"]`)!
      const v = u?.[r.key] ?? null
      row.hidden = v === null
      if (v === null) continue
      any = true
      const memory = r.key === 'memory_mb'
      row.querySelector('b')!.textContent = memory ? `${v} MB` : pct(v)
      row.querySelector('small')!.textContent =
        r.key === 'cpu_percent' && u!.cpu_cores
          ? `of ${u!.cpu_cores} cores` + (u!.system_cpu_percent === null ? '' : ` · machine ${pct(u!.system_cpu_percent)}`)
          : r.key === 'perception_cpu_percent'
            ? 'tracking, velocity, smoothing'
            : r.key === 'detection_cpu_percent'
              ? 'on host: model, distance, COM'
              : r.key === 'alignment_cpu_percent'
                ? 'on host: depth to color'
                : r.key === 'gpu_percent' || r.key === 'npu_percent'
              ? 'used by this process'
              : ''
      row.querySelector<HTMLElement>('.usage-bar')!.hidden = memory
      row.querySelector<HTMLElement>('.usage-bar > i')!.style.width = `${Math.min(100, v)}%`
    }
    this.el.querySelector('.usage-note')!.textContent = !u
      ? 'Usage is not available from this server.'
      : u.cpu_cores === null
        ? 'Measuring…'
        : !any
          ? 'This machine cannot measure usage.'
          : 'Detection and alignment rows show the work done on the host instead of the camera (Compute, left). Most of the rest encodes this page\'s video.'
  }
}
