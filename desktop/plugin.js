/**
 * Computer Use Plus — screen share.
 *
 * While the agent in the conversation you are looking at drives apps, a fanned stack of cards floats at the top
 * right: one per app window it used (newest in front). The front card is a live macOS window capture (MJPEG); the
 * cards behind refresh every few seconds. The Hermes cursor glides to each click, ripples on clicks, draws drags, and
 * a badge shows typing. Click a card behind to bring it forward; click the front card to enlarge it. Stop and
 * Take over / Hand back sit under the stack, with the frame's age or the reason there is no picture.
 *
 * The agent half serves this on 127.0.0.1:47831-47840 (stream.py), one server per Hermes process. This half gets
 * the server's capability token (via `/computer-view token`, or the renderer-only /v1/pair), polls /v1/state, and
 * keeps at most one live stream open, so the browser's per-host connection limit is never exhausted.
 */
import * as sdk from '@hermes/plugin-sdk'
import React from 'react'

const { atom, useValue, host, STATUSBAR_AREAS } = sdk
const h = React.createElement

const PLUGIN_ID = 'computer-use-plus'
const PORTS = Array.from({ length: 10 }, (_, i) => 47831 + i)
const POLL_MS = 700
const RESCAN_MS = 5000
const STILL_MS = 3000 // refresh period of the cards behind the front one
const MAX_CARDS = 4
const POS_KEY = 'computer-use-plus.share.pos'
const VIEWER_ID = `v${Math.random().toString(36).slice(2, 10)}`

// sessions: every session from every process, each tagged with its port. selected: the one for this conversation.
const $state = atom({ sessions: [], ports: [], error: null })
const $hidden = atom(false)
const $takeover = atom(null) // { port, session } while this viewer holds a desktop

let token = null
const base = port => `http://127.0.0.1:${port}`

// ── Capability ──────────────────────────────────────────────────────────────

function findToken(value) {
  if (!value) return null
  if (typeof value === 'string') {
    const m = value.match(/"token"\s*:\s*"([^"]+)"/)
    return m ? m[1] : null
  }
  if (typeof value === 'object') {
    if (typeof value.token === 'string') return value.token
    for (const v of Object.values(value)) {
      const t = findToken(v)
      if (t) return t
    }
  }
  return null
}

async function getToken(ports) {
  try {
    const r = await host.request('command.dispatch', { name: 'computer-view', arg: 'token', session_id: '' })
    const t = findToken(r)
    if (t) return t
  } catch {}
  for (const port of ports) {
    try {
      const r = await fetchJSON(`${base(port)}/v1/pair`, {}, 1500)
      if (r && r.token) return r.token
    } catch {}
  }
  return null
}

async function fetchJSON(url, opts = {}, timeout = 1500) {
  const ctl = new AbortController()
  const t = setTimeout(() => ctl.abort(), timeout)
  try {
    const r = await fetch(url, { cache: 'no-store', ...opts, signal: ctl.signal })
    if (r.status === 401) {
      const err = new Error('unauthorized')
      err.unauthorized = true
      throw err
    }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`)
    return await r.json()
  } finally {
    clearTimeout(t)
  }
}

const auth = () => (token ? { Authorization: `Bearer ${token}` } : {})

async function post(port, path) {
  return fetchJSON(base(port) + path, { method: 'POST', headers: { ...auth(), 'X-Computer-Use-Plus': '1' } }, 4000)
}

// ── Polling ─────────────────────────────────────────────────────────────────

function readAtom(a) {
  try {
    return a && typeof a.get === 'function' ? a.get() : null
  } catch {
    return null
  }
}

// The chat the user is looking at: the focused tile (the SDK's documented choice for readouts that follow the
// user between tiles), falling back to the primary chat only when nothing is focused.
function activeUiSession() {
  return readAtom(host.state && host.state.focusedSessionId) || readAtom(host.state && host.state.activeSessionId)
}

function hostHasSessionAtom() {
  return Boolean(host.state && (host.state.activeSessionId || host.state.focusedSessionId))
}

function startPolling(ctx) {
  let alive = new Set()
  let lastScan = 0
  let busy = false
  const tick = async () => {
    if (busy) return
    busy = true
    try {
      const now = Date.now()
      const scan = now - lastScan > RESCAN_MS || alive.size === 0
      if (scan) lastScan = now
      const targets = scan ? PORTS : [...alive]
      if (!token) token = await getToken(targets)
      let unauthorized = false
      const results = await Promise.all(
        targets.map(async port => {
          try {
            const body = await fetchJSON(`${base(port)}/v1/state?viewer=${VIEWER_ID}`, { headers: auth() })
            return { port, body }
          } catch (err) {
            if (err.unauthorized) unauthorized = true
            return null
          }
        })
      )
      if (unauthorized) token = null // re-pair on the next tick
      const ok = results.filter(Boolean)
      alive = new Set(ok.map(r => r.port))
      const sessions = []
      for (const { port, body } of ok) {
        const skew = Date.now() / 1000 - (body.now || Date.now() / 1000)
        for (const s of body.sessions || []) sessions.push({ ...s, port, lastMs: (s.last_activity + skew) * 1000 })
      }
      sessions.sort((a, b) => b.lastMs - a.lastMs)
      $state.set({ sessions, ports: [...alive], error: unauthorized && !ok.length ? 'unauthorized' : null })
    } finally {
      busy = false
    }
  }
  tick()
  ctx.setInterval(tick, POLL_MS)
}

// The session the user is looking at: same Desktop session id, still watchable, with at least one window.
function selected() {
  const ui = activeUiSession()
  const { sessions } = $state.get()
  const usable = sessions.filter(s => s.watchable && s.apps && s.apps.length)
  // No host session to match (older Desktop): fall back to the newest; otherwise only this conversation's.
  if (!ui) return hostHasSessionAtom() ? null : usable[0] || null
  return usable.find(s => s.ui_session_id === ui) || null
}

function othersRunning() {
  const ui = activeUiSession()
  return $state.get().sessions.filter(s => s.watchable && s.apps && s.apps.length && s.ui_session_id !== ui).length
}

function paused(s) {
  const t = $takeover.get()
  return Boolean((s && s.human_has_control) || (t && s && t.port === s.port))
}

function statusOf(s) {
  if (paused(s)) return { key: 'paused', text: 'You have control — Hermes is paused' }
  if (s.stopping) return { key: 'stopping', text: 'Stopping…' }
  if (s.ended) {
    const f = s.last_failure
    return f ? { key: 'failed', text: `Ended · last problem: ${f.label}` } : { key: 'ended', text: 'Done' }
  }
  if (s.inflight > 0) return { key: 'running', text: s.label || 'Working…' }
  if (s.ok === false) {
    const why = s.status === 'refused' ? 'Refused' : s.status === 'unknown' ? 'Outcome unknown' : 'Failed'
    return { key: 'failed', text: `${why}: ${s.label || ''}` }
  }
  return { key: 'waiting', text: `Waiting for Hermes · last: ${s.label || '—'}` }
}

function pictureNote(app, s) {
  if (paused(s)) return 'Paused while you have control'
  const c = app.capture || {}
  if (c.frame_age == null && !c.error) return 'Connecting…'
  if (c.error) return `No picture: ${c.error}`
  return null
}

function ageText(app) {
  const c = app.capture || {}
  if (c.frame_age == null) return ''
  const a = Math.round(c.frame_age)
  const src = c.method === 'screen' ? ' · screen crop' : ''
  return (a <= 2 ? 'live' : a < 60 ? `${a}s old` : `${Math.round(a / 60)}m old`) + src
}

// ── Cards (plain DOM: <img> streams must not be re-created by re-renders) ───

const CSS = `
.cup-share{position:fixed;z-index:2147483000;width:560px;pointer-events:none;font:12px/1.35 system-ui,sans-serif;
  transition:opacity .25s ease,transform .25s ease}
.cup-share.cup-off{opacity:0;transform:translateY(-6px)}
.cup-share.cup-off *{pointer-events:none!important}
.cup-deck{position:relative;height:200px;transition:height .3s}
.cup-card{position:absolute;top:0;right:0;width:300px;border-radius:10px;overflow:hidden;pointer-events:auto;
  background:#0d0d0d;border:1px solid rgba(255,255,255,.14);box-shadow:0 10px 30px rgba(0,0,0,.45),0 2px 6px rgba(0,0,0,.3);
  transform-origin:right center;transition:transform .45s cubic-bezier(.2,.8,.2,1),opacity .3s,filter .3s;cursor:pointer}
.cup-card.cup-back{filter:brightness(.7)}
.cup-card.cup-back:hover{filter:brightness(.95)}
.cup-stage{position:relative;height:200px;background:#000}
.cup-stage img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;object-position:center top;display:block}
.cup-share.k-running .cup-card.cup-front{border-color:rgba(99,102,241,.75)}
.cup-tag{position:absolute;left:6px;top:6px;padding:1px 7px;border-radius:999px;background:rgba(99,102,241,.92);color:#fff;
  font-size:10.5px;line-height:16px;font-weight:600;max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;z-index:4}
.cup-card.cup-back .cup-tag{background:rgba(60,60,60,.9)}
.cup-age{position:absolute;right:6px;top:6px;padding:1px 6px;border-radius:999px;background:rgba(0,0,0,.6);color:#ccc;
  font-size:10px;line-height:15px;z-index:4}
.cup-age.cup-old{color:#f59e0b}
.cup-ph{position:absolute;inset:0;display:grid;place-items:center;padding:14px;color:#aaa;text-align:center;line-height:1.4;
  font-size:11.5px;z-index:3;background:rgba(0,0,0,.55)}
.cup-ph[hidden]{display:none}
.cup-cursor{position:absolute;left:0;top:0;width:22px;height:22px;margin:-3px 0 0 -4px;pointer-events:none;z-index:5;
  transition:transform .55s cubic-bezier(.25,.8,.25,1),opacity .3s;filter:drop-shadow(0 1px 2px rgba(0,0,0,.7))}
.cup-cursor svg{width:22px;height:22px}
.cup-name{position:absolute;left:16px;top:16px;padding:1px 6px;border-radius:6px;background:#6366f1;color:#fff;font-size:10px;
  line-height:15px;font-weight:600;white-space:nowrap}
.cup-ripple{position:absolute;width:30px;height:30px;margin:-15px 0 0 -15px;border-radius:50%;pointer-events:none;z-index:4;
  border:2px solid #818cf8;animation:cup-ripple .6s ease-out forwards}
.cup-ripple.r-right{border-color:#f59e0b}
@keyframes cup-ripple{from{transform:scale(.3);opacity:1}to{transform:scale(1.7);opacity:0}}
.cup-key{position:absolute;padding:2px 7px;border-radius:6px;background:rgba(0,0,0,.8);color:#fff;font-size:10.5px;line-height:1.3;
  pointer-events:none;white-space:nowrap;z-index:6;max-width:240px;overflow:hidden;text-overflow:ellipsis}
.cup-key[hidden]{display:none}
.cup-trail{position:absolute;inset:0;pointer-events:none;z-index:4;width:100%;height:100%}
.cup-bar{margin-left:auto;width:300px;display:flex;align-items:center;gap:6px;margin-top:8px;padding:5px 6px 5px 10px;pointer-events:auto;
  border-radius:10px;background:rgba(24,24,27,.92);border:1px solid rgba(255,255,255,.1);color:#ddd;backdrop-filter:blur(8px)}
.cup-dot{width:7px;height:7px;border-radius:50%;flex:none;background:#777}
.k-running .cup-dot{background:#22c55e;animation:cup-pulse 1.1s ease-in-out infinite}
.k-waiting .cup-dot{background:#818cf8;animation:cup-pulse 2s ease-in-out infinite}
.k-failed .cup-dot{background:#ef4444}
.k-paused .cup-dot,.k-stopping .cup-dot{background:#f59e0b}
@keyframes cup-pulse{0%,100%{opacity:1}50%{opacity:.3}}
.cup-label{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cup-btn{flex:none;height:22px;padding:0 7px;border:0;border-radius:6px;background:rgba(255,255,255,.08);color:#ddd;cursor:pointer;font-size:11.5px}
.cup-btn:hover{background:rgba(255,255,255,.16);color:#fff}
.cup-btn.cup-stop:hover{background:#ef4444;color:#fff}
.cup-btn.cup-on{background:#f59e0b;color:#000}
.cup-btn.cup-x{background:transparent;padding:0 4px;color:#999}
.cup-btn[hidden]{display:none}
.cup-big{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.6);display:grid;place-items:center;backdrop-filter:blur(4px)}
.cup-big[hidden]{display:none}
.cup-big .cup-card{position:relative;width:min(1100px,90vw);transform:none!important;cursor:zoom-out}
.cup-big .cup-stage{height:min(82vh,760px)}
`

const CURSOR_SVG =
  '<svg viewBox="0 0 24 24"><path d="M4 2.5l15.5 9-6.8 1.5-3.6 6.5z" fill="#6366f1" stroke="#fff" stroke-width="1.7" stroke-linejoin="round"/></svg>'

function el(tag, cls, text) {
  const n = document.createElement(tag)
  if (cls) n.className = cls
  if (text != null) n.textContent = text
  return n
}

// One card for one window. mode 'live' holds an MJPEG stream; 'still' refreshes a JPEG every STILL_MS; 'off' holds
// nothing. destroy() is final: no retry or refresh survives it.
function makeCard(ctx, onClick) {
  const card = el('div', 'cup-card')
  const stage = el('div', 'cup-stage')
  const img = el('img')
  img.draggable = false
  img.alt = ''
  const ph = el('div', 'cup-ph', 'Connecting…')
  const tag = el('div', 'cup-tag')
  const age = el('div', 'cup-age')
  const trail = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  trail.setAttribute('class', 'cup-trail')
  const cursor = el('div', 'cup-cursor')
  cursor.innerHTML = CURSOR_SVG
  cursor.append(el('div', 'cup-name', 'Hermes'))
  cursor.style.opacity = '0'
  const key = el('div', 'cup-key')
  key.hidden = true
  stage.append(img, ph, tag, age, trail, cursor, key)
  card.append(stage)
  ctx.addEventListener(card, 'click', onClick)

  const st = { mode: 'off', url: '', dead: false, timer: null, retry: 1000, seq: 0, loaded: false }
  const clearTimer = () => {
    if (st.timer) clearTimeout(st.timer)
    st.timer = null
  }

  ctx.addEventListener(img, 'load', () => {
    if (st.dead) return
    st.loaded = true
    st.retry = 1000
  })
  ctx.addEventListener(img, 'error', () => {
    if (st.dead || st.mode === 'off') return
    st.loaded = false
    clearTimer()
    // Back off and re-point the same source; a destroyed or re-moded card cancels this.
    st.timer = setTimeout(() => {
      if (st.dead || st.mode === 'off') return
      img.src = `${st.url}&r=${Date.now()}`
    }, st.retry)
    st.retry = Math.min(st.retry * 2, 10000)
  })

  function setSource(mode, url) {
    if (st.dead) return
    if (mode === st.mode && url === st.url) return
    clearTimer()
    st.mode = mode
    st.url = url
    st.retry = 1000
    if (mode === 'off') {
      img.removeAttribute('src')
      return
    }
    img.src = `${url}&r=${Date.now()}`
    if (mode === 'still') {
      const refresh = () => {
        if (st.dead || st.mode !== 'still') return
        const next = new Image()
        next.onload = () => {
          if (!st.dead && st.mode === 'still') img.src = next.src
        }
        next.src = `${st.url}&r=${Date.now()}`
        st.timer = setTimeout(refresh, STILL_MS)
      }
      st.timer = setTimeout(refresh, STILL_MS)
    }
  }

  // The picture's rectangle inside the stage (object-fit: contain, anchored top-centre).
  function contentRect() {
    const bw = stage.clientWidth || 300
    const bh = stage.clientHeight || 200
    const nw = img.naturalWidth
    const nh = img.naturalHeight
    if (!nw || !nh) return { x: 0, y: 0, w: bw, h: bh }
    const k = Math.min(bw / nw, bh / nh)
    const w = nw * k
    const hh = nh * k
    return { x: (bw - w) / 2, y: 0, w, h: hh }
  }

  function update(app, s) {
    tag.textContent = app.app || app.title || 'App'
    card.title = [app.app, app.title].filter(Boolean).join(' — ')
    const note = pictureNote(app, s)
    ph.hidden = !(note && (!st.loaded || paused(s) || (app.capture && app.capture.error && !app.capture.frame_age)))
    ph.textContent = note || ''
    age.textContent = ageText(app)
    age.classList.toggle('cup-old', Boolean(app.capture && app.capture.frame_age > 4 && s.inflight > 0))
    age.hidden = !age.textContent

    const r = contentRect()
    const p = s.pointer && s.pointer.window_id === app.window_id ? s.pointer : null
    if (p) {
      const x = r.x + p.x * r.w
      const y = r.y + p.y * r.h
      cursor.style.opacity = '1'
      cursor.style.transform = `translate(${x}px, ${y}px)`
      if (p.seq !== st.seq) {
        const first = st.seq === 0
        st.seq = p.seq
        if (!first && Date.now() / 1000 - p.at < 3) animate(p, r, x, y)
      }
    } else {
      cursor.style.opacity = '0'
    }
    // Typing: on the pointer when there is one, else along the bottom of the window.
    const t = s.typing
    const typing = t && t.window_id === app.window_id && Date.now() / 1000 - t.at < 4
    key.hidden = !typing
    if (typing) {
      key.textContent = `⌨ ${t.text}`
      if (p) {
        key.style.left = `${r.x + p.x * r.w + 14}px`
        key.style.top = `${r.y + p.y * r.h + 20}px`
      } else {
        key.style.left = `${r.x + 8}px`
        key.style.top = `${r.y + r.h - 28}px`
      }
    }
  }

  function animate(p, r, x, y) {
    if (/click/.test(p.kind)) {
      setTimeout(() => !st.dead && ripple(x, y, p.kind), 500)
      if (p.kind === 'double_click') setTimeout(() => !st.dead && ripple(x, y, 'click'), 680)
    }
    trail.replaceChildren()
    if (p.kind === 'drag' && p.to_x != null) {
      const x2 = r.x + p.to_x * r.w
      const y2 = r.y + p.to_y * r.h
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line')
      line.setAttribute('x1', String(x))
      line.setAttribute('y1', String(y))
      line.setAttribute('x2', String(x2))
      line.setAttribute('y2', String(y2))
      line.setAttribute('stroke', '#818cf8')
      line.setAttribute('stroke-width', '2')
      line.setAttribute('stroke-dasharray', '4 3')
      trail.append(line)
      setTimeout(() => {
        if (!st.dead) cursor.style.transform = `translate(${x2}px, ${y2}px)`
      }, 550)
      setTimeout(() => !st.dead && trail.replaceChildren(), 1800)
    }
  }

  function ripple(x, y, kind) {
    const r = el('div', 'cup-ripple' + (kind === 'right_click' ? ' r-right' : ''))
    r.style.left = `${x}px`
    r.style.top = `${y}px`
    stage.append(r)
    setTimeout(() => r.remove(), 700)
  }

  function destroy() {
    st.dead = true
    clearTimer()
    img.removeAttribute('src')
    card.remove()
  }

  return { card, setSource, update, destroy, st }
}

function urlFor(s, app, kind, width, fps) {
  const q = `window=${app.window_id}&session=${encodeURIComponent(s.session_id)}&width=${width}&t=${encodeURIComponent(token || '')}`
  return kind === 'live' ? `${base(s.port)}/v1/stream?${q}&fps=${fps}` : `${base(s.port)}/v1/frame?${q}`
}

function createShare(ctx) {
  const style = el('style')
  style.textContent = CSS
  document.head.appendChild(style)

  const root = el('div', 'cup-share cup-off')
  const deck = el('div', 'cup-deck')
  const bar = el('div', 'cup-bar')
  const dot = el('div', 'cup-dot')
  const label = el('div', 'cup-label')
  const btnStop = el('button', 'cup-btn cup-stop', '■ Stop')
  const btnTake = el('button', 'cup-btn', 'Take over')
  const btnX = el('button', 'cup-btn cup-x', '×')
  btnStop.title = 'Stop Hermes now (interrupts this conversation’s turn)'
  btnTake.title = 'Pause Hermes and use the computer yourself until you hand it back'
  btnX.title = 'Hide the screen share (stops capturing)'
  for (const b of [btnStop, btnTake, btnX]) b.type = 'button'
  bar.append(dot, label, btnStop, btnTake, btnX)
  root.append(deck, bar)
  document.body.appendChild(root)

  const big = el('div', 'cup-big')
  big.hidden = true
  // Marked as an overlay surface so Hermes's Esc-to-cancel stands down while it is open: Escape closes the
  // enlarged view only, never the running turn.
  big.setAttribute('role', 'dialog')
  big.setAttribute('aria-label', 'Enlarged screen share')
  big.setAttribute('data-overlay-surface', '')
  document.body.appendChild(big)
  let bigCard = null
  let bigWindow = null

  const cards = new Map() // `${port}:${session}:${window}` -> card
  let pinned = null
  let pinnedFor = null
  let shownSession = null

  function readPos() {
    try {
      return { right: 20, top: 64, ...(JSON.parse(localStorage.getItem(POS_KEY)) || {}) }
    } catch {
      return { right: 20, top: 64 }
    }
  }
  const pos = readPos()
  root.style.right = `${pos.right}px`
  root.style.top = `${pos.top}px`

  function closeBig() {
    if (bigCard) bigCard.destroy()
    bigCard = null
    bigWindow = null
    big.hidden = true
    render()
  }

  function openBig(s, app) {
    if (bigCard) bigCard.destroy()
    bigCard = makeCard(ctx, closeBig)
    bigWindow = app.window_id
    big.replaceChildren(bigCard.card)
    big.hidden = false
    render() // the front card drops its stream while the big view holds one
  }
  ctx.addEventListener(big, 'click', e => {
    if (e.target === big) closeBig()
  })

  function clearCards() {
    for (const c of cards.values()) c.destroy()
    cards.clear()
    if (bigCard) {
      bigCard.destroy()
      bigCard = null
      bigWindow = null
      big.hidden = true
    }
  }

  function render() {
    const s = selected()
    const visible = Boolean(s) && !$hidden.get()
    const mineHeld = $takeover.get()
    // A takeover this viewer holds always keeps a usable Hand back, even when its session ended, expired, is
    // hidden or belongs to another tile: the agent stays paused until the person hands back.
    root.classList.toggle('cup-off', !visible && !mineHeld)
    if (!visible) {
      clearCards() // hidden or nothing to show: no stream, no refresh
      deck.style.height = '0px'
      if (mineHeld) {
        root.className = root.className.replace(/\bk-\w+/g, '').trim() + ' k-paused'
        label.textContent = 'You have control — Hermes is paused'
        btnStop.hidden = true
        btnTake.hidden = false
        btnTake.textContent = 'Hand back'
        btnTake.classList.add('cup-on')
      }
      return
    }
    deck.style.height = ''
    if (shownSession !== `${s.port}:${s.session_id}`) {
      clearCards()
      shownSession = `${s.port}:${s.session_id}`
      pinned = null
    }
    const stt = statusOf(s)
    root.className = root.className.replace(/\bk-\w+/g, '').trim() + ` k-${stt.key}`
    label.textContent = stt.text
    label.title = stt.text
    btnStop.hidden = s.ended
    const mine = $takeover.get()
    btnTake.textContent = mine ? 'Hand back' : 'Take over'
    btnTake.classList.toggle('cup-on', Boolean(mine))
    btnTake.hidden = s.human_has_control && !mine // someone else holds it: not ours to hand back

    let apps = s.apps.slice(0, MAX_CARDS)
    if (pinned != null && apps[0] && apps[0].window_id !== pinnedFor) pinned = null
    if (pinned != null) {
      const i = apps.findIndex(a => a.window_id === pinned)
      if (i > 0) apps = [apps[i], ...apps.slice(0, i), ...apps.slice(i + 1)]
    }
    const keyOf = app => `${s.port}:${s.session_id}:${app.window_id}`
    const keep = new Set(apps.map(keyOf))
    for (const [k, c] of cards) {
      if (!keep.has(k)) {
        c.destroy()
        cards.delete(k)
      }
    }
    const hold = paused(s)
    apps.forEach((app, i) => {
      let c = cards.get(keyOf(app))
      if (!c) {
        c = makeCard(ctx, () => {
          const cur = selected()
          if (!cur || !c.app) return
          if (c.card.classList.contains('cup-front')) openBig(cur, c.app)
          else {
            pinned = c.app.window_id
            pinnedFor = cur.apps[0] && cur.apps[0].window_id
            render()
          }
        })
        cards.set(keyOf(app), c)
        deck.append(c.card)
      }
      c.app = app
      const front = i === 0
      c.card.classList.toggle('cup-front', front)
      c.card.classList.toggle('cup-back', !front)
      c.card.style.transform = `translateX(${-i * 110}px) translateY(${i * 10}px) scale(${Math.pow(0.82, i)})`
      c.card.style.zIndex = String(100 - i)
      let mode = front ? 'live' : 'still'
      if (hold || (front && bigWindow === app.window_id)) mode = 'off'
      c.setSource(mode, mode === 'off' ? '' : urlFor(s, app, mode, front ? 720 : 400, 8))
      c.update(app, s)
    })
    if (bigCard) {
      const app = s.apps.find(a => a.window_id === bigWindow)
      if (!app) closeBig()
      else {
        bigCard.setSource(hold ? 'off' : 'live', hold ? '' : urlFor(s, app, 'live', 1280, 10))
        bigCard.update(app, s)
      }
    }
  }

  // Drag the stack by its status bar.
  let drag = null
  ctx.addEventListener(bar, 'pointerdown', e => {
    if (e.target.closest('.cup-btn')) return
    const r = root.getBoundingClientRect()
    drag = { x: e.clientX, y: e.clientY, right: window.innerWidth - r.right, top: r.top }
    bar.setPointerCapture(e.pointerId)
  })
  ctx.addEventListener(bar, 'pointermove', e => {
    if (!drag) return
    const right = Math.max(0, Math.min(window.innerWidth - 200, drag.right - (e.clientX - drag.x)))
    const top = Math.max(0, Math.min(window.innerHeight - 120, drag.top + (e.clientY - drag.y)))
    root.style.right = `${right}px`
    root.style.top = `${top}px`
    drag.last = { right, top }
  })
  ctx.addEventListener(bar, 'pointerup', () => {
    if (drag?.last) localStorage.setItem(POS_KEY, JSON.stringify(drag.last))
    drag = null
  })

  ctx.addEventListener(btnX, 'click', () => $hidden.set(true))
  ctx.addEventListener(btnStop, 'click', async () => {
    const s = selected()
    if (!s) return
    const jobs = [post(s.port, `/v1/stop?session=${encodeURIComponent(s.session_id)}`)]
    if (s.ui_session_id) jobs.push(host.request('session.interrupt', { session_id: s.ui_session_id }))
    const res = await Promise.allSettled(jobs)
    if (res.every(r => r.status === 'rejected')) host.notifyError?.(new Error('Could not stop Hermes'), 'Computer use')
  })
  ctx.addEventListener(btnTake, 'click', async () => {
    const mine = $takeover.get()
    try {
      if (mine) {
        // Hand back exactly the lease this viewer took, whatever is selected now.
        await post(mine.port, `/v1/handback?viewer=${VIEWER_ID}`)
        $takeover.set(null)
      } else {
        const s = selected()
        if (!s) return
        await post(s.port, `/v1/takeover?viewer=${VIEWER_ID}&session=${encodeURIComponent(s.session_id)}`)
        $takeover.set({ port: s.port, session: s.session_id })
      }
    } catch (err) {
      host.notifyError?.(err, mine ? 'Could not hand back' : 'Could not take over')
    }
  })
  ctx.addEventListener(window, 'keydown', e => {
    if (e.key === 'Escape' && bigCard) {
      e.preventDefault() // the host's Esc-to-cancel ignores handled events
      setTimeout(closeBig, 0) // after every Esc listener has seen the overlay
    }
  })

  const unsubs = [$state.subscribe(render), $hidden.subscribe(render), $takeover.subscribe(render)]
  // A new session in this conversation reopens a stack the user hid for an earlier one.
  let lastSession = null
  unsubs.push(
    $state.subscribe(() => {
      const s = selected()
      const id = s ? `${s.port}:${s.session_id}` : null
      if (id && id !== lastSession && $hidden.get()) $hidden.set(false)
      lastSession = id || lastSession
    })
  )
  for (const k of ['activeSessionId', 'focusedSessionId']) {
    const a = host.state && host.state[k]
    if (a && typeof a.subscribe === 'function') unsubs.push(a.subscribe(() => render()))
  }
  ctx.onDispose(() => {
    unsubs.forEach(u => typeof u === 'function' && u())
    clearCards()
    root.remove()
    big.remove()
    style.remove()
    const mine = $takeover.get()
    if (mine) post(mine.port, `/v1/handback?viewer=${VIEWER_ID}`).catch(() => {})
  })
}

// ── Status-bar chip ─────────────────────────────────────────────────────────

function Chip() {
  useValue($state)
  const hidden = useValue($hidden)
  useValue($takeover)
  const s = selected()
  if (!s) {
    const n = othersRunning()
    return n
      ? h('span', { className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)', title: 'Hermes is using the computer in another conversation' },
          `Computer · ${n} other chat${n === 1 ? '' : 's'}`)
      : null
  }
  const st = statusOf(s)
  const color = { running: '#22c55e', failed: '#ef4444', paused: '#f59e0b', stopping: '#f59e0b', waiting: '#818cf8' }[st.key] || '#777'
  return h(
    'button',
    {
      type: 'button',
      title: hidden ? 'Show the screen share' : 'Hide the screen share',
      className: 'flex items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-primary)',
      onClick: () => $hidden.set(!hidden)
    },
    h('span', { style: { width: 6, height: 6, borderRadius: 3, background: color, display: 'inline-block' } }),
    `Computer · ${s.apps.length} app${s.apps.length === 1 ? '' : 's'}`
  )
}

export default {
  id: PLUGIN_ID,
  name: 'Computer Use Plus',
  register(ctx) {
    createShare(ctx)
    startPolling(ctx)

    ctx.register({
      id: 'chip',
      area: (STATUSBAR_AREAS && STATUSBAR_AREAS.right) || 'statusBar.right',
      order: 120,
      render: () => h(Chip, null)
    })

    ctx.register({
      id: 'toggle',
      area: 'palette',
      data: {
        id: `${PLUGIN_ID}.toggle`,
        label: 'Toggle computer-use screen share',
        keywords: ['computer', 'preview', 'screen', 'share', 'agent', 'live', 'cursor'],
        run: () => $hidden.set(!$hidden.get())
      }
    })
  }
}
