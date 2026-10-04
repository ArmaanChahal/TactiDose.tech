/**
 * Demo operator panel controller (demo.html, demo mode only). Requires a signed-in user
 * (demo endpoints refuse anonymous calls); quick buttons switch between the seeded demo
 * accounts. Panels: scripted checklists A–D, the simulated device (containers with pill
 * counts, faults, buttons, restart, physical pills), the demo clock, a raw protocol
 * console with the serial log, a live event feed and the demo-data reset.
 */

import { ApiError, get, post } from './api.js';
import { EventStream, RECONNECTED, parseTimestamp } from './events.js';
import { $$, byId, confirmDialog, createNotifier, debounce, errorState, errorText, h, initLiveRegions, replaceChildren } from './dom.js';
import { hydrateIcons, icon } from './icons.js';
import { initThemeCycleButton } from './theme.js';
import { bindConnIndicator } from './conn.js';
import { CarouselView, pillsFromPhysical } from './carousel.js';
import { createLineLog } from './linelog.js';
import { commandResultBox } from './hwview.js';
import { requireSession, roleName, watchSession } from './session.js';
import { DEMO_ACCOUNTS } from './authforms.js';
import { createGuidedDemo } from './guided.js';
import { ReplySpeaker } from './voice.js';
import {
  addMinutesToLocal,
  advanceLocalIso,
  clock12,
  dateKey,
  deviceOffsetFrom,
  deviceWall,
  formatLongDate,
  formatOffset,
  normalizeTime,
  parseIso,
  time24To12,
} from './format.js';
import { createFlows } from './demo/flows.js';
import { faultLabel, healthValue } from './demo/labels.js';

const MAX_EVENTS = 80;
const FEED_TOPICS = Object.freeze(['notification', 'drop.updated', 'patient.status', 'agent.message', 'report.updated', 'device.event', 'system.notice', 'clock.changed']);

initLiveRegions();
hydrateIcons();
initThemeCycleButton(byId('theme-btn'));
const notify = createNotifier(byId('notices'), { iconFor: (kind) => icon({ success: 'check-circle', error: 'warning', warning: 'warning' }[kind] || 'info') });
const stream = new EventStream();
bindConnIndicator(byId('conn'), stream);

let me = null;
const recentEvents = [];

// ------------------------------------------------------------------ session & account switch

function renderSession() {
  const who = me?.user ? `${me.user.display_name} (${roleName(me.user.role)})` : 'Nobody';
  byId('session-who').textContent = `Signed in as ${who}.`;
  replaceChildren(byId('switch-buttons'), DEMO_ACCOUNTS.map((a) => h('button', {
    type: 'button',
    class: `btn btn-small${me?.user?.email === a.email ? ' is-current' : ''}`,
    'aria-pressed': me?.user?.email === a.email ? 'true' : 'false',
    on: { click: () => switchTo(a) },
  }, icon('user'), `Use ${a.label}`)));
}

async function switchTo(account) {
  const password = byId('switch-password').value;
  byId('switch-status').textContent = `Signing in as ${account.label}…`;
  try {
    await post('/api/auth/logout', {}, { redirectOn401: false }).catch(() => null);
    await post('/api/auth/login', { email: account.email, password }, { redirectOn401: false });
    window.location.reload();
  } catch (err) {
    byId('switch-status').textContent = err instanceof ApiError && err.status === 401
      ? 'That password was not accepted for the demo account. Check the demo password, or sign in on the sign-in page.'
      : errorText(err);
  }
}

async function loadHealth() {
  try {
    const health = await get('/api/health', { redirectOn401: false });
    const parts = [];
    for (const [key, label] of [['hardware', 'Hardware'], ['agent', 'Agent'], ['tts', 'Voice'], ['smtp', 'Email']]) {
      if (health && health[key] !== undefined) parts.push(`${label}: ${healthValue(health[key])}`);
    }
    byId('session-mode').textContent = parts.join(' · ');
  } catch {
    byId('session-mode').textContent = 'Server status unavailable.';
  }
}

// ------------------------------------------------------------------ live event feed

function summarize(topic, d) {
  switch (topic) {
    case 'notification': return `${d.kind || ''}: ${d.title || ''}${d.body ? ` — ${d.body}` : ''}`;
    case 'drop.updated': return `drop ${d.drop_id ?? '?'} ${d.status || ''}${d.reason ? ` (${d.reason})` : ''} · ${d.source || ''} · container ${d.container_number ?? '?'}`;
    case 'patient.status': return `patient ${d.patient_id ?? '?'} changed${d.reason ? `: ${d.reason}` : ''}`;
    case 'agent.message': return `conversation ${d.conversation_id ?? '?'} · ${d.role || ''} message ${d.message_id ?? ''}`;
    case 'report.updated': return `report ${d.report_id ?? '?'} ${d.status || ''}`;
    case 'device.event': return `${d.code || ''} ${d.line ? `(${d.line})` : ''}`;
    case 'system.notice': return `${d.level || 'info'}: ${d.message || ''}`;
    case 'clock.changed': return `clock ${d.now_local || ''}`;
    default: return JSON.stringify(d);
  }
}

function addEvent(topic, data, env) {
  recentEvents.push({ topic, data, at: Date.now() });
  if (recentEvents.length > 200) recentEvents.shift();
  const list = byId('events');
  const ms = parseTimestamp(env?.ts);
  const d = Number.isFinite(ms) ? new Date(ms) : new Date();
  const time = [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
  list.append(h('li', {}, h('span', { class: 't' }, time), h('span', { class: 'topic' }, topic), h('span', {}, summarize(topic, data || {}))));
  while (list.children.length > MAX_EVENTS) list.firstElementChild.remove();
  list.scrollTop = list.scrollHeight;
}

for (const topic of FEED_TOPICS) stream.on(topic, (d, env) => addEvent(topic, d, env));
byId('events-clear').addEventListener('click', () => byId('events').replaceChildren());

// ------------------------------------------------------------------ simulator

const carousel = new CarouselView(byId('sim-carousel'), { numSlots: 3, label: 'Simulated pill device' });
const simCaption = byId('sim-caption');
const simStatus = byId('sim-status');
const simButtons = [byId('sim-press-confirm'), byId('sim-press-cancel'), byId('sim-reboot')];
const faultInputs = new Map();
let simAvailable = false;
let numSlots = 3;

function renderFaults(faults) {
  const box = byId('sim-faults');
  for (const name of Object.keys(faults || {})) {
    if (faultInputs.has(name)) continue;
    const id = `fault-${name}`;
    const [label, desc] = faultLabel(name);
    const input = h('input', { type: 'checkbox', id });
    input.addEventListener('change', () => setFault(name, label, input));
    faultInputs.set(name, input);
    box.append(h('div', { class: 'check-row' }, input,
      h('label', { for: id }, label, desc ? h('span', { class: 'fault-desc' }, ` — ${desc}`) : null)));
  }
  for (const [name, input] of faultInputs) {
    input.checked = Boolean(faults?.[name]);
    input.disabled = !simAvailable;
  }
}

function renderPillSlots(n) {
  const select = byId('pills-slot');
  if (select.options.length === n) return;
  const keep = select.value;
  select.replaceChildren(...Array.from({ length: n }, (_, i) => h('option', { value: String(i) }, `Container ${i + 1}`)));
  if (keep && Number(keep) < n) select.value = keep;
}

function renderPhysical(p) {
  if (!p) return;
  if (Number(p.num_slots)) {
    numSlots = Number(p.num_slots);
    carousel.setNumSlots(numSlots);
  }
  renderPillSlots(numSlots);
  const gate = p.gate_open === true ? 'OPEN' : p.gate_open === false ? 'CLOSED' : 'UNKNOWN';
  carousel.update({
    angleDeg: p.angle_deg,
    slot: p.slot ?? null,
    gate,
    targetSlot: p.target_slot ?? null,
    moving: Boolean(p.moving) || ['MOVING', 'HOMING', 'AT_TARGET'].includes(p.state),
    pills: pillsFromPhysical(p, numSlots),
  });
  simCaption.textContent = `${carousel.describe()}${p.state ? ` Firmware state: ${p.state}.` : ''}`;
  if (p.faults) renderFaults(p.faults);
  const rows = Object.entries(p).filter(([k]) => k !== 'faults')
    .map(([k, v]) => [k, typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)]);
  replaceChildren(byId('sim-physical'), rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
}

function renderSim(sim) {
  simAvailable = Boolean(sim?.available);
  byId('sim-unavailable').hidden = simAvailable;
  for (const btn of simButtons) btn.disabled = !simAvailable;
  renderFaults(sim?.faults || {});
  if (sim?.physical) renderPhysical(sim.physical);
}

async function loadSim() {
  try {
    renderSim(await get('/api/demo/simulator'));
  } catch (err) {
    simStatus.textContent = errorText(err);
    renderSim({ available: false });
  }
}

async function simAction(body, doneText) {
  try {
    renderSim(await post('/api/demo/simulator', body));
    simStatus.textContent = doneText;
  } catch (err) {
    simStatus.textContent = errorText(err);
    notify(errorText(err), 'error');
  }
}

async function setFault(name, label, input) {
  const enabled = input.checked;
  input.disabled = true;
  try {
    renderSim(await post('/api/demo/simulator', { fault: name, enabled }));
    simStatus.textContent = `${label}: ${enabled ? 'on' : 'off'}.`;
  } catch (err) {
    input.checked = !enabled;
    notify(errorText(err), 'error');
  } finally {
    input.disabled = !simAvailable;
  }
}

byId('sim-press-confirm').addEventListener('click', () => simAction({ press: 'CONFIRM' }, 'The Confirm button was pressed.'));
byId('sim-press-cancel').addEventListener('click', () => simAction({ press: 'CANCEL' }, 'The Cancel button was pressed.'));
byId('sim-reboot').addEventListener('click', () => simAction({ reboot: true }, 'The device is restarting…'));
byId('pills-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const slot = Number(byId('pills-slot').value);
  const count = Number(byId('pills-count').value);
  if (!Number.isInteger(count) || count < 0) {
    simStatus.textContent = 'Enter a whole number of pills.';
    return;
  }
  simAction({ pills: { slot, count } }, `Container ${slot + 1} now physically holds ${count} pills.`);
});

stream.on('sim.physical', (p) => renderPhysical(p));

// ------------------------------------------------------------------ device console

const hwResult = byId('hw-result');
const hwLine = byId('hw-line');
const hwLog = createLineLog(byId('hw-log'), { hideHeartbeat: byId('hw-hide-hb').checked });
const hwPause = byId('hw-pause');

async function sendLine(line) {
  const text = String(line || '').trim();
  if (!text) {
    hwLine.focus();
    return;
  }
  replaceChildren(hwResult, h('p', { class: 'muted' }, `Sending ${text}…`));
  try {
    replaceChildren(hwResult, commandResultBox(text, await post('/api/demo/command', { line: text }, { timeoutMs: 60000 })));
  } catch (err) {
    replaceChildren(hwResult, errorState(err, null, icon('warning')));
  }
}

byId('hw-form').addEventListener('submit', (e) => {
  e.preventDefault();
  sendLine(hwLine.value);
});
for (const btn of $$('[data-line]')) btn.addEventListener('click', () => sendLine(btn.dataset.line));
byId('hw-stop').addEventListener('click', async () => {
  try {
    replaceChildren(hwResult, commandResultBox('Stop', await post('/api/device/stop', {})));
  } catch (err) {
    replaceChildren(hwResult, errorState(err, null, icon('warning')));
  }
});
byId('hw-reconnect').addEventListener('click', async () => {
  try {
    const resp = await post('/api/device/reconnect', {});
    replaceChildren(hwResult, h('div', { class: `result-box ${resp?.ok ? 'is-ok' : 'is-fail'}` }, resp?.ok ? 'Reconnecting to the device.' : 'Reconnect was refused (a command is running, or there is no device).'));
  } catch (err) {
    replaceChildren(hwResult, errorState(err, null, icon('warning')));
  }
});
byId('hw-hide-hb').addEventListener('change', (e) => hwLog.setHideHeartbeat(e.target.checked));
byId('hw-clear').addEventListener('click', () => hwLog.clear());
hwPause.addEventListener('click', () => {
  const paused = hwPause.getAttribute('aria-pressed') !== 'true';
  hwPause.setAttribute('aria-pressed', String(paused));
  hwPause.textContent = paused ? 'Resume' : 'Pause';
  hwLog.setPaused(paused);
});

stream.on('device.line', (_d, env) => hwLog.add(env));

// ------------------------------------------------------------------ demo clock

const clockTime = byId('clock-time');
const clockMeta = byId('clock-meta');
const clockStatus = byId('clock-status');
let clockState = null;
let clockFetchedAt = 0;

function elapsedS() {
  return (performance.now() - clockFetchedAt) / 1000;
}

function nowLocal() {
  return clockState?.now_local ? advanceLocalIso(clockState.now_local, elapsedS()) : null;
}

function tickClock() {
  const p = parseIso(nowLocal());
  if (p) clockTime.textContent = clock12(p.hour, p.minute, p.second);
}

function renderClock(clock) {
  if (!clock?.now_local) return;
  clockState = clock;
  clockFetchedAt = performance.now();
  const travel = clock.travelling ? `time travel ${formatOffset(clock.offset_s)}` : 'real time';
  clockMeta.textContent = `${formatLongDate(dateKey(clock.now_local))} · ${clock.tz || 'device time zone'} · ${travel}`;
  tickClock();
}

async function loadClock() {
  try {
    renderClock(await get('/api/demo/clock'));
  } catch (err) {
    clockMeta.textContent = errorText(err);
  }
}

const loadClockSoon = debounce(loadClock, 250);

async function travel(body, doneText) {
  clockStatus.textContent = 'Changing the demo clock…';
  try {
    const clock = await post('/api/demo/clock', body);
    renderClock(clock);
    clockStatus.textContent = doneText;
    return clock;
  } catch (err) {
    clockStatus.textContent = errorText(err);
    notify(errorText(err), 'error');
    throw err;
  }
}

/**
 * Move the clock to a device-local wall time "YYYY-MM-DDTHH:MM" (`{local_datetime}`),
 * falling back to `{offset_minutes}` if the server does not take local_datetime.
 */
async function travelTo(localMinute, minutesAhead, doneText) {
  const wanted = parseIso(localMinute);
  try {
    const clock = await travel({ local_datetime: localMinute }, doneText);
    const got = parseIso(clock?.now_local);
    if (got && wanted && Math.abs(got.wallMs - wanted.wallMs) < 3 * 60000) return clock;
  } catch (err) {
    if (!(err instanceof ApiError) || ![400, 422].includes(err.status)) throw err;
  }
  const base = Math.round((Number(clockState?.offset_s) || 0) / 60);
  return travel({ offset_minutes: base + Math.ceil(minutesAhead) }, doneText);
}

function plusMinutes(minutes) {
  if (!clockState) return;
  const target = addMinutesToLocal(clockState.now_local, minutes + elapsedS() / 60);
  travelTo(target, minutes, `Moved forward ${minutes} minutes.`).catch(() => {});
}

/** Used by checklists B and C: move the clock just past a running cooldown. */
async function skipCooldown(status) {
  if (!clockState) await loadClock();
  const offset = deviceOffsetFrom(status.now_local);
  const minutes = Math.ceil((Number(status.cooldown_remaining_s) || 0) / 60) + 1;
  let target = null;
  if (status.next_manual_allowed_at) {
    const w = deviceWall(status.next_manual_allowed_at, offset);
    if (w) {
      const iso = `${w.year}-${String(w.month).padStart(2, '0')}-${String(w.day).padStart(2, '0')}T${String(w.hour).padStart(2, '0')}:${String(w.minute).padStart(2, '0')}:00`;
      target = addMinutesToLocal(iso, 1);
    }
  }
  if (!target) target = addMinutesToLocal(nowLocal() || status.now_local, minutes);
  return travelTo(target, minutes, 'Moved the clock past the cooldown.');
}

byId('clock-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const t = normalizeTime(byId('clock-set').value);
  if (!t) {
    clockStatus.textContent = 'Enter a time first.';
    return;
  }
  travel({ local_time: t }, `Travelled to ${time24To12(t)}.`).catch(() => {});
});
byId('clock-plus15').addEventListener('click', () => plusMinutes(15));
byId('clock-plus60').addEventListener('click', () => plusMinutes(60));
byId('clock-reset').addEventListener('click', () => travel({ reset: true }, 'Back to real time.').catch(() => {}));
byId('clock-next').addEventListener('click', async () => {
  clockStatus.textContent = 'Jumping to the next scheduled dose…';
  try {
    const resp = await post('/api/demo/jump-to-next-dose', {});
    renderClock(resp?.clock);
    const next = resp?.next;
    clockStatus.textContent = next
      ? `Now at the next dose: ${next.medication_name}, container ${next.container_number ?? '?'}. It drops automatically in a moment.`
      : 'No upcoming scheduled dose was found.';
  } catch (err) {
    clockStatus.textContent = errorText(err);
    notify(errorText(err), 'error');
  }
});

stream.on('clock.changed', () => loadClockSoon());
setInterval(tickClock, 1000);

// ------------------------------------------------------------------ demo data

byId('demo-reset').addEventListener('click', async () => {
  const reseed = byId('reset-reseed').checked;
  const { ok } = await confirmDialog({
    title: 'Reset the demo data?',
    message: `Drops, doses, notifications, conversations and reports are cleared${reseed ? ', and the demo accounts and medications are seeded again' : ''}. The clock returns to real time. This cannot be undone.`,
    confirmLabel: 'Reset demo data',
    danger: true,
    iconEl: icon('warning'),
  });
  if (!ok) return;
  try {
    await post('/api/demo/reset', { reseed });
    byId('reset-status').textContent = 'Demo data reset.';
    notify('Demo data reset.', 'success');
    flows.resetAll();
    loadClock();
    loadSim();
  } catch (err) {
    byId('reset-status').textContent = errorText(err);
    notify(errorText(err), 'error');
  }
});

// ------------------------------------------------------------------ checklists

const flows = createFlows(byId('flows'), {
  session: () => me,
  recent: (topic) => recentEvents.filter((e) => e.topic === topic).map((e) => e.data),
  renderClock,
  skipCooldown,
  notify,
});

// ------------------------------------------------------------------ start

async function start() {
  try {
    me = await requireSession();
  } catch (err) {
    const box = byId('page-error');
    box.hidden = false;
    box.replaceChildren(errorState(err, () => window.location.reload(), icon('warning')));
    return;
  }
  if (!me) return;
  renderSession();
  watchSession(stream);
  createGuidedDemo(byId('guided-root'), {
    stream,
    speaker: new ReplySpeaker(),
    speak: () => byId('guided-speak').checked,
    allowReset: true,
  });
  stream.on(RECONNECTED, () => {
    loadSim();
    loadClock();
  });
  stream.start();
  loadHealth();
  loadSim();
  loadClock();
}

start();
