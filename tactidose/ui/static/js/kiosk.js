/**
 * Optional kiosk screen (kiosk.html): a full-screen, voice-first device screen for the
 * signed-in patient. One big status word, the next pill, three huge drop buttons (press
 * twice to drop, so a single accidental touch never drops a pill), a Talk button and
 * large captions. Replies and results are spoken. Same API and rules as the patient
 * portal: POST /api/patients/{pid}/drops and POST /api/agent/chat.
 */

import { LONG_TIMEOUT_MS, get, post } from './api.js';
import { EventStream, RECONNECTED } from './events.js';
import { announce, byId, debounce, errorState, errorText, h, initLiveRegions, replaceChildren } from './dom.js';
import { hydrateIcons, icon } from './icons.js';
import { initThemeCycleButton } from './theme.js';
import { bindConnIndicator } from './conn.js';
import { requireSession, watchSession } from './session.js';
import { containerView, kioskBanner, nextPillText, outcomeView, remainingCooldown } from './status.js';
import { notificationSpeech } from './notifications.js';
import { displayTranscript } from './pcm.js';
import { ReplySpeaker, VoiceInput, voiceInputAvailable } from './voice.js';

const CONFIRM_MS = 6000;

initLiveRegions();
hydrateIcons();
initThemeCycleButton(byId('theme-btn'));
const stream = new EventStream();
bindConnIndicator(byId('conn'), stream, { quietWhenOpen: true });
const speaker = new ReplySpeaker();

const state = {
  pid: null,
  status: null,
  statusAt: 0,
  online: true,
  dropping: false,
  armed: null,
  armedTimer: null,
  conversationId: null,
  chatting: false,
  lastBanner: '',
  spokenDrops: new Set(),
};

function caption(text, { speak = true, assertive = false } = {}) {
  byId('k-caption').textContent = text;
  announce(text, { assertive });
  if (speak && text) speaker.speak(text);
}

// ------------------------------------------------------------------ status

function remaining() {
  return remainingCooldown(state.status, (performance.now() - state.statusAt) / 1000);
}

function renderBanner() {
  const b = kioskBanner({ status: state.status, remainingS: remaining(), dropping: state.dropping, online: state.online });
  const word = byId('k-status-word');
  word.textContent = b.word;
  word.className = `status-word tone-${b.tone}`;
  byId('k-status-detail').textContent = b.detail;
  byId('k-status').dataset.state = b.key;
  if (state.lastBanner === 'wait' && b.key === 'ready') announce('You can drop a pill now.');
  state.lastBanner = b.key;
}

function renderDrops() {
  const box = byId('k-drops');
  const containers = (state.status?.containers || []).map(containerView).filter((v) => v.hasMed).sort((a, b) => a.slot - b.slot);
  if (!containers.length) {
    replaceChildren(box, h('p', { class: 'k-line' }, 'No containers are set up yet.'));
    return;
  }
  const focused = box.contains(document.activeElement) ? document.activeElement.dataset.slot : undefined;
  replaceChildren(box, containers.map((v) => {
    const armed = state.armed === v.slot;
    return h('button', {
      type: 'button',
      class: `k-btn k-btn-drop${armed ? ' is-armed' : ''}${v.canDrop ? '' : ' is-unavailable'}`,
      dataset: { slot: v.slot },
      'aria-disabled': v.canDrop ? null : 'true',
      on: { click: () => pressDrop(v) },
    },
    h('span', { class: 'k-drop-num' }, String(v.number)),
    h('span', { class: 'k-drop-text' },
      h('span', { class: 'k-drop-action' }, armed ? 'Press again to drop' : v.canDrop ? 'Drop pill' : 'Empty'),
      h('span', { class: 'k-drop-med' }, v.medName),
      h('span', { class: 'k-drop-count' }, v.countText)));
  }));
  if (focused !== undefined) box.querySelector(`[data-slot="${focused}"]`)?.focus({ preventScroll: true });
}

function render() {
  renderBanner();
  byId('k-next').textContent = state.status ? `Next pill: ${nextPillText(state.status)}` : '';
  renderDrops();
}

async function loadStatus() {
  if (!state.pid) return;
  try {
    state.status = await get(`/api/patients/${state.pid}/status`);
    state.statusAt = performance.now();
    state.online = true;
  } catch (err) {
    state.online = !(err?.network || err?.timeout);
    if (state.online && !state.status) byId('k-status-detail').textContent = errorText(err);
  }
  render();
}

const loadSoon = debounce(loadStatus, 300);

// ------------------------------------------------------------------ drops (press twice)

function disarm() {
  clearTimeout(state.armedTimer);
  state.armed = null;
  renderDrops();
}

async function pressDrop(v) {
  if (state.dropping) {
    caption('A pill is already dropping. Please wait.', { speak: true });
    return;
  }
  if (!v.canDrop) {
    caption(v.blocked, { assertive: true });
    return;
  }
  if (state.armed !== v.slot) {
    clearTimeout(state.armedTimer);
    state.armed = v.slot;
    state.armedTimer = setTimeout(disarm, CONFIRM_MS);
    renderDrops();
    caption(`Press again to drop ${v.medName} from container ${v.number}.`);
    return;
  }
  disarm();
  state.dropping = true;
  speaker.stop();
  renderBanner();
  caption('Dropping a pill. Please wait.', { speak: false });
  try {
    const outcome = await post(`/api/patients/${state.pid}/drops`, { slot: v.slot }, { timeoutMs: 75000 });
    const view = outcomeView(outcome);
    if (outcome?.drop_id) state.spokenDrops.add(outcome.drop_id);
    caption(view.message, { assertive: !view.dropped });
  } catch (err) {
    const uncertain = err?.timeout || err?.network;
    caption(uncertain
      ? 'There was no answer from TactiDose. The pill may or may not have dropped. Ask your caregiver to check before trying again.'
      : `The pill was not dropped: ${errorText(err)}`, { assertive: true });
  } finally {
    // Keep "Dropping" until the fresh status is in, then show the new state directly.
    await loadStatus();
    state.dropping = false;
    render();
  }
}

// ------------------------------------------------------------------ talk

async function chat(text) {
  if (state.chatting) return;
  state.chatting = true;
  byId('k-heard').textContent = `You said: ${displayTranscript(text)}`;
  caption('Thinking…', { speak: false });
  try {
    const body = { text, input_mode: 'voice', speak: true };
    if (state.conversationId) body.conversation_id = state.conversationId;
    const reply = await post('/api/agent/chat', body, { timeoutMs: LONG_TIMEOUT_MS });
    state.conversationId = reply?.conversation_id ?? state.conversationId;
    for (const a of reply?.actions || []) if (a?.drop_id) state.spokenDrops.add(a.drop_id);
    byId('k-caption').textContent = reply?.text || '';
    announce(reply?.text || '');
    if (reply?.text) speaker.speak(reply.text, reply.audio_url || null);
    if ((reply?.actions || []).length) loadSoon();
  } catch (err) {
    caption(`The assistant could not answer: ${errorText(err)}. You can use the drop buttons.`, { assertive: true });
  } finally {
    state.chatting = false;
  }
}

const talkBtn = byId('k-talk');
const voice = new VoiceInput({
  onState: (st, message) => {
    const on = st === 'listening' || st === 'starting';
    talkBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    talkBtn.classList.toggle('is-listening', st === 'listening');
    byId('k-talk-label').textContent = on ? 'Stop and send' : st === 'processing' ? 'Working…' : 'Talk';
    if (message) caption(message, { speak: st === 'error' });
  },
  onInterim: (text) => {
    byId('k-heard').textContent = `Hearing: ${displayTranscript(text)}`;
  },
  onResult: (text) => chat(text),
});

function toggleTalk() {
  if (!voiceInputAvailable()) {
    caption('Voice is not available on this screen. Use the drop buttons.');
    return;
  }
  speaker.stop();
  voice.toggle();
}

talkBtn.addEventListener('click', toggleTalk);
byId('k-stop').addEventListener('click', async () => {
  voice.cancel();
  speaker.stop();
  disarm();
  try {
    await post('/api/device/stop', {});
    caption('Stopped.', { speak: true });
  } catch (err) {
    caption(`Could not stop the device: ${errorText(err)}`, { assertive: true });
  }
});

document.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const key = String(e.key).toLowerCase();
  if (key === 'escape') {
    voice.cancel();
    speaker.stop();
    disarm();
    return;
  }
  if (key === 't') {
    e.preventDefault();
    toggleTalk();
    return;
  }
  if (/^[1-9]$/.test(key)) {
    const v = (state.status?.containers || []).map(containerView).find((c) => c.number === Number(key) && c.hasMed);
    if (v) {
      e.preventDefault();
      pressDrop(v);
    }
  }
});

// ------------------------------------------------------------------ start

async function start() {
  let me;
  try {
    me = await requireSession({ roles: ['patient'] });
  } catch (err) {
    const box = byId('page-error');
    box.hidden = false;
    box.replaceChildren(errorState(err, () => window.location.reload(), icon('warning')));
    return;
  }
  if (!me) return;
  state.pid = me.patient?.patient_id ?? me.user.user_id;
  byId('k-who').textContent = me.user.display_name;
  watchSession(stream);
  stream.onStatus((s) => {
    if (s === 'reconnecting' || s === 'closed') state.online = false;
    else if (s === 'open') state.online = true;
    renderBanner();
  });
  stream.on('patient.status', loadSoon);
  stream.on('drop.updated', loadSoon);
  stream.on('device.state', (d) => {
    if (state.status && d && typeof d === 'object') {
      state.status = { ...state.status, device: d };
      renderBanner();
    }
  });
  stream.on('notification', (n, _env, meta) => {
    if (meta?.replayed) return;
    loadSoon();
    const dropId = n?.data?.drop_id;
    if (dropId !== undefined && state.spokenDrops.has(dropId)) return;
    if (dropId !== undefined) state.spokenDrops.add(dropId);
    if (!voice.active && !state.chatting) caption(notificationSpeech(n));
  });
  stream.on(RECONNECTED, loadStatus);
  stream.start();
  await loadStatus();
  setInterval(renderBanner, 1000);
  setInterval(loadStatus, 60000);
}

start();
