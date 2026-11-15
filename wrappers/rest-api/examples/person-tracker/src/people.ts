// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

// The People list (tracked persons, present or not) and the popover that names a person.

import { forgetIdentity, getIdentities, getTracks, nameTrack, type Track } from './api'
import { icon } from './icons'
import { identityColor } from './render'

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T
const escape = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`)

function lastSeen(t: Track) {
  if (t.present) return 'here'
  const s = t.seconds_since_seen
  return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : 'not seen yet'
}

export class People {
  private deviceId: string | null = null
  private timer = 0
  private editing: number | null = null
  private onError: (message: string) => void

  constructor(onError: (message: string) => void) {
    this.onError = onError
    const form = $<HTMLFormElement>('tagger')
    form.onsubmit = (e) => {
      e.preventDefault()
      this.save($<HTMLInputElement>('tag-name').value.trim() || null)
    }
    $('tag-remove').onclick = () => this.save(null)
    $('tag-cancel').onclick = () => this.closeTagger()
    form.onkeydown = (e) => {
      if (e.key === 'Escape') this.closeTagger() // no `&&` here: a handler returning false cancels the key
    }
    $('people').onclick = (e) => {
      const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-action]')
      if (!btn) return
      const li = btn.closest('li')!
      if (btn.dataset.action === 'rename') this.openTagger(Number(li.dataset.track), li.dataset.name || null)
      else this.forget(li.dataset.name!)
    }
  }

  /** Start (deviceId) or stop (null) following a camera's tracks. */
  follow(deviceId: string | null) {
    this.deviceId = deviceId
    clearInterval(this.timer)
    this.closeTagger()
    if (deviceId) this.timer = window.setInterval(() => this.refresh(), 1000)
    this.refresh()
  }

  async refresh(tracks?: Track[]) {
    const id = this.deviceId
    if (!id) return this.show([])
    try {
      this.show(tracks ?? (await getTracks(id)))
    } catch {
      /* the next poll retries */
    }
  }

  private show(tracks: Track[]) {
    const here = tracks.filter((t) => t.present).length
    $('people-count').textContent = tracks.length ? `${here} here` : ''
    $('people-hint').hidden = !this.deviceId || tracks.some((t) => t.name)
    $('people').innerHTML = tracks
      .sort((a, b) => Number(b.present) - Number(a.present) || a.track_id - b.track_id)
      .map((t) => {
        const name = t.name ? escape(t.name) : `Person #${t.track_id}`
        return `<li data-track="${t.track_id}" data-name="${t.name ? escape(t.name) : ''}" class="${t.present ? 'here' : ''}">
          <span class="dot" style="background:${identityColor(t.name, t.track_id)}"></span>
          <span class="who${t.name ? '' : ' unnamed'}">${name}</span>
          <span class="when">${lastSeen(t)}</span>
          <button data-action="rename" title="${t.name ? 'Rename' : 'Name'}" aria-label="Name ${name}">${icon('pencil', 15)}</button>
          ${t.name ? `<button data-action="forget" title="Forget" aria-label="Forget ${name}">${icon('trash', 15)}</button>` : ''}
        </li>`
      })
      .join('')
  }

  /** Show the naming popover for a track, near (x, y) in viewport CSS pixels (top center if omitted). */
  async openTagger(trackId: number, name: string | null, at?: { x: number; y: number }) {
    if (!this.deviceId) return
    this.editing = trackId
    const form = $<HTMLFormElement>('tagger')
    const input = $<HTMLInputElement>('tag-name')
    $('tag-title').textContent = name ? `Rename ${name}` : `Name person #${trackId}`
    $('tag-remove').hidden = !name
    input.value = name ?? ''
    form.hidden = false
    const box = form.parentElement!.getBoundingClientRect()
    const x = at ? at.x : box.width / 2
    form.style.left = `${Math.min(Math.max(8, x - form.offsetWidth / 2), box.width - form.offsetWidth - 8)}px`
    form.style.top = `${Math.min(at ? at.y + 12 : 16, box.height - form.offsetHeight - 8)}px`
    input.focus()
    input.select()
    // Saved names, so a returning person can be given their old name back
    try {
      $('known-names').innerHTML = (await getIdentities(this.deviceId)).map((n) => `<option value="${escape(n)}">`).join('')
    } catch {
      /* suggestions are optional */
    }
  }

  closeTagger() {
    this.editing = null
    $('tagger').hidden = true
  }

  private async save(name: string | null) {
    const id = this.deviceId
    const track = this.editing
    if (!id || track === null) return
    this.closeTagger()
    try {
      this.refresh(await nameTrack(id, track, name))
    } catch (e) {
      this.onError((e as Error).message)
    }
  }

  private async forget(name: string) {
    if (!this.deviceId || !confirm(`Forget ${name}? Their saved look is deleted.`)) return
    try {
      await forgetIdentity(this.deviceId, name)
      this.refresh()
    } catch (e) {
      this.onError((e as Error).message)
    }
  }
}
