/**
 * Kiosk / touchscreen controller (index.html).
 *
 * Mirrors the backend: GET /api/state on load, on reconnect and every 15 s, plus
 * live SSE topics (assistant.state, device.state, assistant.spoken, dose.updated…).
 * Buttons and shortcuts only *request* actions via POST /api/intents — the
 * backend decides whether anything moves.
 *
 * Double-tap protection: while a request is in flight every button except
 * CANCEL is aria-disabled (focus is kept). CANCEL always stays available
 * because it must interrupt a dispense immediately.
 */

import { get, postIntent } from './api.js';
import { EventStream, RECONNECTED } from './events.js';
import { $$, announce, byId, debounce, errorText } from './dom.js';
import { hydrateIcons, icon } from './icons.js';
import { initThemeToggle } from './theme.js';
import {
  KIOSK_INTENTS,
  deriveBanner,
  doseDetailText,
  intentForKey,
  nextEventText,
  suggestedIntent,
  voiceText,
} from './kiosk-state.js';

const POLL_MS = 15000;
const CAPTION_DEDUPE_MS = 4000;
const CANCEL_DEBOUNCE_MS = 500;
const CAPTION_KIND_ICON = { error: 'warning', warning: 'warning', success: 'check-circle', prompt: 'arrow-right' };

const view = {
  loaded: false,
  serverOnline: true,
  stateError: false,
  device: null,
  phase: 'IDLE',
  awaiting: null,
  due: null,
  nowLocal: null,
  voice: null,
  demoMode: false,
};

const ui = {
  banner: byId('status-banner'),
  icon: byId('status-icon'),
  word: byId('status-word'),
  sep: byId('status-sep'),
  detail: byId('status-detail'),
  next: byId('next-event'),
  doseDetail: byId('dose-detail'),
  caption: byId('caption'),
  captionAlert: byId('caption-alert'),
  busy: byId('busy'),
  conn: byId('conn-banner'),
  voice: byId('voice-status'),
  voiceIcon: byId('voice-icon'),
  voiceWord: byId('voice-word'),
  demoChip: byId('demo-chip'),
};

const buttons = $$('[data-intent]');
let inFlight = null;
let lastCancelAt = 0;
let lastCaption = { text: '', at: 0 };
let lastBannerKey = '';

// ------------------------------------------------------------------ rendering

function render() {
  const current = deriveBanner(view);
  const bannerKey = `${current.key}|${current.detail}`;
  if (bannerKey !== lastBannerKey) {
    lastBannerKey = bannerKey;
    ui.banner.dataset.tone = current.tone;
    ui.icon.replaceChildren(icon(current.icon));
    ui.word.textContent = current.word;
    ui.sep.textContent = current.detail ? ' — ' : '';
    ui.detail.textContent = current.detail;
  }

  ui.next.textContent = nextEventText(view);
  const detail = doseDetailText(view);
  ui.doseDetail.textContent = detail;
  ui.doseDetail.hidden = !detail;

  const suggested = suggestedIntent(view, current);
  for (const btn of buttons) {
    const on = btn.dataset.intent === suggested;
    btn.classList.toggle('is-suggested', on);
    const marker = btn.querySelector('.k-next');
    if (marker) marker.hidden = !on;
  }

  const voice = voiceText(view.voice);
  ui.voiceWord.textContent = voice.word;
  ui.voiceIcon.replaceChildren(icon(voice.icon));
  ui.voice.dataset.on = String(voice.on);

  ui.conn.hidden = view.serverOnline !== false;
  ui.demoChip.hidden = !view.demoMode;
}

/**
 * Show what TactiDose said. Errors go to the assertive region, everything else to
 * the polite one; replayed/initial captions update the screen without being announced.
 */
function showCaption(text, kind = 'info', { quiet = false } = {}) {
  if (!text) return;
  const now = Date.now();
  if (text === lastCaption.text && now - lastCaption.at < CAPTION_DEDUPE_MS) return;
  lastCaption = { text, at: now };
  const isError = kind === 'error';
  const target = isError ? ui.captionAlert : ui.caption;
  const other = isError ? ui.caption : ui.captionAlert;
  other.replaceChildren();
  other.hidden = true;
  target.hidden = false;
  target.dataset.kind = kind;
  const politeness = isError ? 'assertive' : 'polite';
  if (quiet) target.setAttribute('aria-live', 'off');
  const iconName = CAPTION_KIND_ICON[kind];
  target.replaceChildren(...(iconName ? [icon(iconName)] : []), document.createTextNode(text));
  if (quiet) setTimeout(() => target.setAttribute('aria-live', politeness), 1200);
}

function setServerOnline(online) {
  if (view.serverOnline === online) return;
  view.serverOnline = online;
  announce(online ? 'Connection restored.' : 'Connection lost. Reconnecting.', { assertive: !online });
  render();
}

// ------------------------------------------------------------------ state

function applyState(state) {
  const firstLoad = !view.loaded;
  view.loaded = true;
  view.stateError = false;
  view.device = state.device || null;
  view.phase = state.assistant?.phase || 'IDLE';
  view.awaiting = state.assistant?.awaiting || null;
  view.due = state.due || null;
  view.nowLocal = state.now_local || state.due?.now_local || null;
  view.voice = state.voice || null;
  view.demoMode = Boolean(state.demo_mode);
  const last = state.assistant?.last_reply;
  if (firstLoad && last?.text) showCaption(last.text, last.kind, { quiet: true });
  render();
}

async function refreshState() {
  try {
    const state = await get('/api/state');
    setServerOnline(true);
    applyState(state);
  } catch (err) {
    if (err.network || err.timeout) {
      setServerOnline(false);
    } else {
      // Server answered but cannot report state: fail closed (shown as offline / unavailable).
      view.loaded = true;
      view.stateError = true;
      render();
      console.error('TactiDose kiosk: /api/state failed', err);
    }
  }
}

const refreshSoon = debounce(refreshState, 300);

// ------------------------------------------------------------------ intents

function setBusy(intent) {
  inFlight = intent;
  for (const btn of buttons) {
    if (btn.dataset.intent === 'CANCEL') continue;
    if (intent) btn.setAttribute('aria-disabled', 'true');
    else btn.removeAttribute('aria-disabled');
  }
  ui.busy.textContent = intent ? 'PLEASE WAIT…' : '';
}

async function sendIntent(intent) {
  if (!KIOSK_INTENTS.includes(intent)) return;
  const isCancel = intent === 'CANCEL';
  if (isCancel) {
    const now = Date.now();
    if (now - lastCancelAt < CANCEL_DEBOUNCE_MS) return;
    lastCancelAt = now;
  } else if (inFlight) {
    announce('Please wait.');
    return;
  } else {
    setBusy(intent);
  }
  try {
    const reply = await postIntent(intent, 'ui');
    setServerOnline(true);
    if (reply && reply.text) showCaption(reply.text, reply.kind || 'info');
  } catch (err) {
    if (err.network) setServerOnline(false);
    showCaption(errorText(err), 'error');
  } finally {
    if (!isCancel) setBusy(null);
    refreshSoon();
  }
}

for (const btn of buttons) {
  btn.addEventListener('click', () => sendIntent(btn.dataset.intent));
}

// Remember how each control got focus: a button that merely kept focus after a
// tap/click must not swallow Space/Enter (they stay the main button); only a
// control reached with the keyboard keeps its native activation.
let lastModality = 'keyboard';
const keyboardFocus = new WeakSet();
document.addEventListener('pointerdown', () => {
  lastModality = 'pointer';
}, true);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Tab') lastModality = 'keyboard';
}, true);
document.addEventListener('focusin', (e) => {
  if (lastModality === 'keyboard') keyboardFocus.add(e.target);
  else keyboardFocus.delete(e.target);
}, true);

function keyboardFocused(el) {
  return keyboardFocus.has(el);
}

document.addEventListener('keydown', (e) => {
  if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.repeat) return;
  if (document.querySelector('dialog[open]')) return;
  const target = e.target instanceof Element ? e.target : null;
  const typing = Boolean(target && target.closest('input, textarea, select, [contenteditable="true"]'));
  const control = target ? target.closest('button, a[href], [role="button"], summary') : null;
  const onControl = Boolean(control && keyboardFocused(control));
  const intent = intentForKey(e.key, { onControl, typing });
  if (!intent) return;
  e.preventDefault();
  sendIntent(intent);
});

// ------------------------------------------------------------------ live events

const stream = new EventStream();

stream.onStatus((status) => {
  if (status === 'open') setServerOnline(true);
  if (status === 'reconnecting') refreshSoon();
});

stream.on(RECONNECTED, () => refreshState());

stream.on('device.state', (snapshot) => {
  view.device = snapshot;
  render();
});

stream.on('assistant.state', (data) => {
  view.phase = data.phase || 'IDLE';
  view.awaiting = data.phase === 'AWAITING_CONFIRMATION' ? data.dose || view.awaiting : null;
  render();
  refreshSoon();
});

stream.on('assistant.spoken', (data, _env, meta) => {
  showCaption(data.text, data.kind || 'info', { quiet: meta.replayed });
});

stream.on('voice.status', (data) => {
  view.voice = data;
  render();
});

for (const topic of ['dose.updated', 'clock.changed', 'data.changed']) {
  stream.on(topic, () => refreshSoon());
}

// ------------------------------------------------------------------ start

hydrateIcons();
initThemeToggle(byId('theme-toggle'), { upper: true });
render();
stream.start();
refreshState();
setInterval(refreshState, POLL_MS);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refreshState();
});
