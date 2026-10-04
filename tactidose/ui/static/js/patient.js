/**
 * Patient portal controller (patient.html). The primary user may be blind, have low
 * vision or be older: large text, few big buttons, every result announced in words
 * (aria-live) and optionally spoken.
 *
 * Views (hash routes): Home (next pill, cooldown, containers with "Drop pill",
 * last pill, device), Assistant (voice + text chat), Schedule (read-only), History,
 * Reports and Care team (patient ID + link code to share).
 * Every drop goes to POST /api/patients/{pid}/drops; the server's rules decide and its
 * DropOutcome.message is what the person hears.
 */

import { get, post } from './api.js';
import { EventStream, RECONNECTED } from './events.js';
import { $$, announce, byId, confirmDialog, debounce, errorText, h, replaceChildren } from './dom.js';
import { icon } from './icons.js';
import { createPrefs } from './prefs.js';
import { requireSession } from './session.js';
import { hidePageError, initPortal, preparePage, showPageError } from './portal.js';
import {
  alertsView,
  containerView,
  cooldownView,
  deviceView,
  lastDropText,
  nextPillText,
  outcomeView,
  remainingCooldown,
  statusOffset,
  todayKey,
} from './status.js';
import { advanceLocalIso, spellOut } from './format.js';
import { notificationSpeech } from './notifications.js';
import { createDropHistory } from './history.js';
import { createDispenseButtons } from './dispense.js';
import { createCheckinHistory } from './wellbeing.js';
import { createReports } from './reports.js';
import { ReplySpeaker } from './voice.js';
import { speakNatural } from './speech.js';
import { createAssistant } from './patient/assistant.js';
import { createSchedule } from './patient/schedule.js';

const VIEWS = Object.freeze(['home', 'assistant', 'schedule', 'history', 'reports', 'share']);
const DROP_TIMEOUT_MS = 75000;
const SPOKEN_KINDS = new Set(['PILL_DROPPED', 'DROP_FAILED', 'DROP_UNCERTAIN', 'EMPTY', 'MISSED_DOSE', 'LOW_STOCK']);

preparePage();

const prefs = createPrefs();
const stream = new EventStream();
const speaker = new ReplySpeaker();

const state = {
  me: null,
  pid: null,
  status: null,
  statusAt: 0,
  offset: null,
  view: null,
  dropping: false,
  cooldownWasActive: false,
  lastCooldownText: '',
  /** drop ids whose result was already spoken (so the notification is not spoken twice) */
  spokenDrops: new Set(),
};

let notify = () => {};
let notifications = null;
let assistant = null;
let schedule = null;
let history = null;
let checkins = null;
let reports = null;
let statusSeq = 0;

const getOffset = () => state.offset;
/** The device's "now" (server clock, demo travel included) advanced by the time since the fetch. */
const getNow = () => (state.status?.now_local
  ? advanceLocalIso(state.status.now_local, (performance.now() - state.statusAt) / 1000)
  : null);

// ------------------------------------------------------------------ views

function showView(name, { focus = true } = {}) {
  if (!VIEWS.includes(name)) return;
  state.view = name;
  for (const v of VIEWS) byId(`view-${v}`).hidden = v !== name;
  for (const a of $$('.views-nav a')) {
    if (a.dataset.view === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  if (focus) byId(`${name}-title`).focus();
  if (name === 'assistant') assistant?.show();
  if (name === 'schedule') schedule?.load();
  if (name === 'history') {
    history?.load();
    checkins?.load();
  }
  if (name === 'reports') reports?.load();
}

window.addEventListener('hashchange', () => {
  const name = window.location.hash.slice(1);
  if (VIEWS.includes(name)) showView(name);
});

// ------------------------------------------------------------------ home: status

async function loadStatus() {
  if (!state.pid) return;
  const token = ++statusSeq;
  try {
    const status = await get(`/api/patients/${state.pid}/status`);
    if (token !== statusSeq) return;
    state.status = status;
    state.statusAt = performance.now();
    state.offset = statusOffset(status);
    hidePageError();
    renderHome();
  } catch (err) {
    if (token !== statusSeq) return;
    if (!state.status) {
      byId('next-pill').textContent = `Your pill status could not be loaded: ${errorText(err)}`;
      byId('cooldown-text').textContent = 'Not known right now.';
      byId('last-drop').textContent = 'Not known right now.';
      byId('device-line').textContent = 'Not known right now.';
    } else {
      notify(`Could not refresh your pill status: ${errorText(err)}`, 'warning');
    }
  }
}

const loadStatusSoon = debounce(loadStatus, 300);

function cooldownLeft() {
  return remainingCooldown(state.status, (performance.now() - state.statusAt) / 1000);
}

function renderCooldown(force = false) {
  if (!state.status) return;
  const v = cooldownView(state.status, cooldownLeft());
  if (force || v.text !== state.lastCooldownText) {
    state.lastCooldownText = v.text;
    replaceChildren(byId('cooldown-text'), icon(v.active ? 'clock' : 'check-circle', { className: `icon tone-${v.active ? 'caution' : 'good'}` }), ` ${v.text}`);
    byId('cooldown-rule').textContent = v.rule;
  }
  byId('cooldown-card').classList.toggle('is-waiting', v.active);
  for (const btn of $$('.btn-drop')) btn.classList.toggle('is-waiting', v.active);
  if (state.cooldownWasActive && !v.active) {
    announce('You can drop a pill now.');
    loadStatusSoon();
  }
  state.cooldownWasActive = v.active;
}

function renderContainers() {
  const list = byId('containers');
  // Re-rendering replaces the buttons: keep keyboard focus on the same container.
  const focusedSlot = list.contains(document.activeElement) ? document.activeElement.dataset?.slot : undefined;
  paintContainers(list);
  if (focusedSlot !== undefined) list.querySelector(`.btn-drop[data-slot="${focusedSlot}"]`)?.focus({ preventScroll: true });
}

function paintContainers(list) {
  const containers = (state.status?.containers || []).map(containerView).sort((a, b) => a.slot - b.slot);
  if (!containers.length) {
    replaceChildren(list, h('li', { class: 'state-msg' }, 'No containers are set up yet. Ask your doctor or family to set them up.'));
    return;
  }
  replaceChildren(list, containers.map((v) => {
    const titleId = `container-${v.slot}-title`;
    const countId = `container-${v.slot}-count`;
    const busy = state.dropping;
    let button = null;
    if (v.hasMed) {
      button = h('button', {
        type: 'button',
        class: `btn btn-drop${v.canDrop ? '' : ' is-unavailable'}`,
        'aria-label': v.canDrop ? v.dropLabel : `Empty: ${v.medName}, container ${v.number}`,
        'aria-describedby': `${countId} cooldown-text`,
        'aria-disabled': v.canDrop && !busy ? null : 'true',
        dataset: { slot: v.slot },
        on: { click: () => requestDrop(v) },
      }, icon('pill'), v.canDrop ? 'Drop pill' : 'Empty');
    }
    return h('li', { class: `container-card${v.empty ? ' is-empty' : ''}${v.low ? ' is-low' : ''}${v.hasMed ? '' : ' is-unassigned'}`, 'aria-labelledby': titleId },
      h('h3', { id: titleId, class: 'container-title' }, v.title),
      h('p', { class: 'container-med' }, v.medName),
      v.strength ? h('p', { class: 'container-strength' }, v.strength) : null,
      h('p', { id: countId, class: 'container-count' }, v.countText,
        v.badge ? h('span', { class: `badge badge-solid-${v.badge.solid}` }, icon(v.badge.icon), v.badge.word === 'Low' ? 'Low stock' : 'Empty') : null),
      button);
  }));
  renderCooldown(true);
}

function renderDevice(device) {
  const v = deviceView(device);
  replaceChildren(byId('device-line'),
    h('span', { class: `badge tone-${v.tone}` }, icon(v.icon), v.word), ' ', v.text);
}

function renderAlerts() {
  const alerts = alertsView(state.status);
  byId('alerts-box').hidden = !alerts.length;
  replaceChildren(byId('alerts-list'), alerts.map((a) => h('li', { class: `alert-item tone-${a.tone}` },
    icon(a.tone === 'bad' ? 'warning' : 'info'), h('span', { class: 'alert-text' }, a.text))));
}

function renderHome() {
  const s = state.status;
  byId('next-pill').textContent = nextPillText(s);
  byId('auto-drop-note').textContent = s.auto_drop_enabled === false
    ? 'Automatic drops are turned off. Use the Drop pill buttons when it is time.'
    : 'Scheduled pills drop by themselves at their time.';
  renderContainers();
  byId('last-drop').textContent = lastDropText(s);
  renderDevice(s.device);
  renderAlerts();
}

// ------------------------------------------------------------------ home: drops

function showResult(view) {
  const box = byId('drop-result');
  box.hidden = false;
  box.className = `drop-result tone-${view.tone || 'neutral'}${view.pending ? ' is-pending' : ''}`;
  replaceChildren(box,
    icon(view.icon || 'info', { className: 'icon drop-result-icon' }),
    h('div', {},
      h('p', { class: 'drop-result-word' }, view.word || ''),
      h('p', { class: 'drop-result-message' }, view.message)));
}

function speakIfWanted(text) {
  if (prefs.get('speakDrops') && text) speakNatural(speaker, text);   // server voice (ElevenLabs if set)
}

function setDropping(on) {
  state.dropping = on;
  for (const btn of $$('.btn-drop')) {
    if (on) btn.setAttribute('aria-disabled', 'true');
    else if (!btn.classList.contains('is-unavailable')) btn.removeAttribute('aria-disabled');
    btn.setAttribute('aria-busy', on ? 'true' : 'false');
  }
}

async function requestDrop(v) {
  if (state.dropping) {
    announce('A pill is already dropping. Please wait.');
    return;
  }
  if (!v.canDrop) {
    showResult({ word: 'Not dropped', icon: 'warning', tone: 'caution', message: v.blocked });
    announce(v.blocked, { assertive: true });
    speakIfWanted(v.blocked);
    return;
  }
  if (prefs.get('confirmDrops')) {
    const { ok } = await confirmDialog({
      title: `Drop a pill from container ${v.number}?`,
      message: `${v.medName}${v.strength ? `, ${v.strength}` : ''}.`,
      confirmLabel: 'Yes, drop it',
      cancelLabel: 'No',
      iconEl: icon('pill'),
    });
    if (!ok) return;
  }
  setDropping(true);
  speaker.stop();
  showResult({ word: 'Dropping…', icon: 'rotate', tone: 'neutral', pending: true, message: `Dropping a pill from container ${v.number}. Please wait.` });
  announce('Dropping a pill. Please wait.');
  try {
    const outcome = await post(`/api/patients/${state.pid}/drops`, { slot: v.slot }, { timeoutMs: DROP_TIMEOUT_MS });
    const view = outcomeView(outcome);
    showResult(view);
    announce(view.message, { assertive: !view.dropped });
    if (outcome?.drop_id) state.spokenDrops.add(outcome.drop_id);
    speakIfWanted(view.message);
  } catch (err) {
    const uncertain = err?.timeout || err?.network;
    const message = uncertain
      ? `There was no answer from CareBridge (${errorText(err)}). The pill may or may not have dropped. Check History before you try again.`
      : `The pill was not dropped: ${errorText(err)}`;
    showResult({ word: uncertain ? 'Not sure if it dropped' : 'Not dropped', icon: 'warning', tone: 'bad', message });
    announce(message, { assertive: true });
    speakIfWanted(message);
  } finally {
    setDropping(false);
    loadStatus();
    if (state.view === 'history') history?.load();
  }
}

async function stopDevice() {
  try {
    const resp = await post('/api/device/stop', {});
    const message = resp?.ok === false ? 'The stop command did not get through.' : 'Stop sent to the pill device.';
    notify(message, resp?.ok === false ? 'error' : 'success');
  } catch (err) {
    notify(`Could not stop the device: ${errorText(err)}`, 'error');
  }
}

/** Drops made by the assistant: the reply already says what happened. */
function onAssistantActions(actions) {
  if (!actions.length) return;
  for (const a of actions) if (a?.drop_id) state.spokenDrops.add(a.drop_id);
  loadStatusSoon();
}

// ------------------------------------------------------------------ live notifications

function onLiveNotification(n) {
  loadStatusSoon();
  if (!SPOKEN_KINDS.has(n.kind) || !prefs.get('speakDrops')) return;
  const dropId = n.data?.drop_id;
  if (dropId !== undefined && state.spokenDrops.has(dropId)) return;
  // A drop or assistant turn of this page is still waiting for its answer: that answer is spoken
  // (speaking this too would cut one voice off with the other).
  if (state.dropping || assistant?.busy) return;
  if (dropId !== undefined) state.spokenDrops.add(dropId);
  if (!assistant?.listening) speakNatural(speaker, notificationSpeech(n));
}

// ------------------------------------------------------------------ care team

function renderShare() {
  const p = state.me.patient || {};
  const pid = p.patient_id ?? state.pid;
  const code = p.link_code || '';
  byId('share-pid').textContent = pid ? String(pid) : '–';
  byId('share-code').textContent = code || 'Not available';
  byId('share-pid-spelled').textContent = pid ? `Spelled out: ${spellOut(String(pid))}` : '';
  byId('share-code-spelled').textContent = code ? `Spelled out: ${spellOut(code)}` : '';
  byId('share-copy').addEventListener('click', async () => {
    const text = `CareBridge — Patient ID: ${pid}, link code: ${code}`;
    try {
      await navigator.clipboard.writeText(text);
      byId('share-status').textContent = 'Copied. You can paste it into a message.';
    } catch {
      byId('share-status').textContent = `Copying is not possible here. The codes are: Patient ID ${pid}, link code ${code}.`;
    }
  });
}

// ------------------------------------------------------------------ start

async function start() {
  let me;
  try {
    me = await requireSession({ roles: ['patient'] });
  } catch (err) {
    showPageError(err, () => window.location.reload());
    return;
  }
  if (!me) return;
  state.me = me;
  state.pid = me.patient?.patient_id ?? me.user.user_id;

  ({ notify, notifications } = initPortal({
    me,
    stream,
    prefs,
    getOffset,
    getNow,
    onLiveNotification,
    quietConnection: true,
    prefCheckboxes: {
      speakReplies: 'pref-speak-replies',
      speakDrops: 'pref-speak-drops',
      confirmDrops: 'pref-confirm-drops',
      offlineSpeech: 'pref-offline-speech',
    },
  }));
  // The same preference also has a checkbox right under the chat.
  const speakBox = byId('speak-replies');
  speakBox.checked = Boolean(prefs.get('speakReplies'));
  speakBox.addEventListener('change', () => prefs.set('speakReplies', speakBox.checked));
  prefs.onChange((key, value) => {
    if (key === 'speakReplies') speakBox.checked = Boolean(value);
  });

  byId('device-stop').addEventListener('click', stopDevice);
  const dispense = createDispenseButtons(byId('dispense-controls'), {
    // Wi-Fi dispenser: "Dispense pill N" = the Drop pill button of container N (same rules, same feedback).
    onDispense: (number) => {
      const v = (state.status?.containers || []).map(containerView).find((c) => c.number === number);
      if (v) requestDrop(v);
      else notify(`Container ${number} is not set up.`, 'error');
    },
  });
  renderShare();

  assistant = createAssistant({
    pid: state.pid,
    prefs,
    speaker,
    stream,
    getOffset,
    getNow,
    onActions: onAssistantActions,
    isVisible: () => state.view === 'assistant',
    notify: (message, kind) => notify(message, kind),
    show: () => {
      window.location.hash = '#assistant';
      showView('assistant', { focus: false });
    },
  });
  schedule = createSchedule({
    pid: state.pid,
    getOffset,
    getToday: () => todayKey(state.status),
    containerFor: (mid) => (state.status?.containers || []).find((c) => c.medication_id === mid)?.container_number ?? null,
  });
  history = createDropHistory(byId('history-root'), {
    getPatientId: () => state.pid,
    audience: 'patient',
    canResolve: false,
    notify,
    getOffset,
    getNow,
  });
  checkins = createCheckinHistory(byId('wellbeing-root'), {
    getPatientId: () => state.pid,
    audience: 'patient',
    canDelete: true,
    notify,
    getOffset,
  });
  reports = createReports(byId('reports-root'), {
    getPatientId: () => state.pid,
    audience: 'patient',
    notify,
    getOffset,
    getNow,
    stream,
  });

  stream.on('patient.status', (d) => {
    if (d?.patient_id !== undefined && Number(d.patient_id) !== Number(state.pid)) return;
    loadStatusSoon();
    if (state.view === 'schedule') schedule.load();
    if (d?.reason === 'wellbeing' && state.view === 'history') checkins.load();
  });
  stream.on('drop.updated', (d, _env, meta) => {
    loadStatusSoon();
    if (!meta?.replayed && state.view === 'history') history.load();
  });
  stream.on('device.state', (d) => {
    if (!state.status || !d || typeof d !== 'object') return;
    state.status = { ...state.status, device: d };
    renderDevice(d);
    dispense.render(d);   // online / offline changed
  });
  stream.on(RECONNECTED, () => {
    loadStatus();
    notifications.load();
    if (state.view === 'schedule') schedule.load();
    if (state.view === 'history') history.load();
  });

  const initial = window.location.hash.slice(1);
  showView(VIEWS.includes(initial) ? initial : 'home', { focus: false });
  stream.start();
  notifications.load();
  await loadStatus();
  setInterval(() => renderCooldown(), 1000);
  setInterval(() => {
    if (document.visibilityState === 'visible') loadStatus();
  }, 60000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') loadStatusSoon();
  });
}

start();
