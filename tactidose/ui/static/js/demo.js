/**
 * Demo operator panel controller (demo.html): simulated voice, live transcript,
 * simulator faults/buttons with an animated carousel, raw hardware console,
 * demo clock travel, demo data helpers, scripted handoff §25 flows and a raw
 * state viewer. Every action goes through the documented HTTP API.
 */

import { get, post, postText } from './api.js';
import { EventStream, RECONNECTED, parseTimestamp } from './events.js';
import { $$, byId, confirmDialog, createNotifier, debounce, errorState, errorText, h, prettyJson, replaceChildren } from './dom.js';
import { hydrateIcons, icon } from './icons.js';
import { initThemeToggle } from './theme.js';
import { bindConnIndicator } from './conn.js';
import { CarouselView } from './carousel.js';
import { createLineLog } from './linelog.js';
import { commandResultBox } from './hwview.js';
import {
  addMinutesToLocal,
  clock12,
  dateKey,
  deviceOffsetFrom,
  formatClock,
  formatLongDate,
  formatOffset,
  normalizeTime,
  parseIso,
  time24To12,
} from './format.js';
import { createFlows } from './demo/flows.js';

/** Simulator fault names (docs/API.md, /api/demo/simulator). */
export const FAULTS = Object.freeze([
  ['home_sensor_dead', 'Home sensor dead', 'homing never finds home → HOME_TIMEOUT, FAULT'],
  ['motor_jam', 'Motor jam', 'moves never finish → MOTOR_FAULT'],
  ['unresponsive', 'Unresponsive', 'the device stops answering → timeouts'],
  ['brownout_on_gate', 'Brown-out on gate open', 'the device resets when the gate opens'],
  ['disconnect', 'Disconnect', 'the USB link drops'],
]);

const STATE_SOURCES = Object.freeze([
  ['/api/state', 'Kiosk state — GET /api/state'],
  ['/api/health', 'Health — GET /api/health'],
  ['/api/hardware', 'Device snapshot — GET /api/hardware'],
  ['/api/demo/simulator', 'Simulator — GET /api/demo/simulator'],
  ['/api/demo/clock', 'Demo clock — GET /api/demo/clock'],
]);

const NOTICE_ICONS = { success: 'check-circle', error: 'warning', warning: 'warning', info: 'info' };
const MAX_TRANSCRIPT = 250;

hydrateIcons();
initThemeToggle(byId('theme-toggle'));
const notify = createNotifier(byId('notices'), { iconFor: (kind) => icon(NOTICE_ICONS[kind] || 'info') });
const stream = new EventStream();
bindConnIndicator(byId('conn'), stream);

let offsetMin = null;

// ------------------------------------------------------------------ simulated voice

const voiceText = byId('voice-text');
const voiceReply = byId('voice-reply');
const pendingPhrases = new Set();

function renderReply(reply) {
  voiceReply.dataset.kind = reply?.kind || 'info';
  replaceChildren(voiceReply,
    h('div', { class: 'reply-text' }, reply?.text || '(no reply text)'),
    h('div', { class: 'muted' }, [reply?.intent, reply?.kind, reply?.spoken === false ? 'not spoken' : null].filter(Boolean).join(' · ')),
    h('details', {}, h('summary', {}, 'Outcome JSON'), h('pre', { class: 'json' }, prettyJson(reply?.outcome ?? {}))));
}

async function say(text, button = null) {
  if (pendingPhrases.has(text)) return;
  pendingPhrases.add(text);
  button?.setAttribute('aria-disabled', 'true');
  voiceReply.dataset.kind = 'info';
  replaceChildren(voiceReply, h('div', { class: 'muted' }, `Sending “${text}”…`));
  try {
    renderReply(await postText(text, 'keyboard'));
  } catch (err) {
    voiceReply.dataset.kind = 'error';
    replaceChildren(voiceReply, errorState(err, null, icon('warning')));
  } finally {
    pendingPhrases.delete(text);
    button?.removeAttribute('aria-disabled');
  }
}

byId('voice-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const text = voiceText.value.trim();
  if (!text) {
    voiceText.focus();
    return;
  }
  say(text);
  voiceText.select();
});

for (const btn of $$('[data-phrase]')) {
  btn.addEventListener('click', () => say(btn.dataset.phrase, btn));
}

// ------------------------------------------------------------------ transcript

const transcript = byId('transcript');

function timeOf(envelope) {
  const ms = parseTimestamp(envelope?.ts);
  const d = Number.isFinite(ms) ? new Date(ms) : new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
}

function addTranscript(kind, who, text, envelope, extraClass = null) {
  const stick = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 48;
  transcript.append(h('li', { class: extraClass },
    h('span', { class: 't' }, timeOf(envelope)),
    h('span', { class: `who who-${kind}` }, who),
    h('span', {}, text)));
  while (transcript.children.length > MAX_TRANSCRIPT) transcript.firstElementChild.remove();
  if (stick) transcript.scrollTop = transcript.scrollHeight;
}

byId('transcript-clear').addEventListener('click', () => transcript.replaceChildren());

stream.on('assistant.spoken', (d, env) => {
  const meta = [d.kind, d.audio].filter(Boolean).join(', ');
  addTranscript('said', 'TactiDose said', `${d.text || ''}${meta ? ` (${meta})` : ''}`, env);
});
stream.on('voice.heard', (d, env) => {
  const confidence = Number.isFinite(Number(d.confidence)) ? ` ${Math.round(Number(d.confidence) * 100)}%` : '';
  const verdict = d.accepted ? 'accepted' : 'ignored';
  addTranscript('heard', 'Heard', `“${d.text || ''}”${confidence} → ${d.intent || 'no intent'} (${verdict})`, env, d.accepted ? null : 'is-ignored');
});
stream.on('assistant.intent', (d, env) => {
  addTranscript('intent', 'Intent', `${d.intent || '?'} from ${d.source || '?'}${d.text ? ` (“${d.text}”)` : ''}`, env);
});

// ------------------------------------------------------------------ simulator

const carousel = new CarouselView(byId('sim-carousel'), { numSlots: 6, label: 'Simulated carousel' });
const simCaption = byId('sim-caption');
const simPhysical = byId('sim-physical');
const simStatus = byId('sim-status');
const simUnavailable = byId('sim-unavailable');
const simButtons = [byId('sim-press-confirm'), byId('sim-press-cancel'), byId('sim-reboot')];
const faultInputs = new Map();
let simAvailable = false;

for (const [name, label, desc] of FAULTS) {
  const id = `fault-${name}`;
  const input = h('input', { type: 'checkbox', id, disabled: true });
  input.addEventListener('change', () => setFault(name, label, input));
  faultInputs.set(name, input);
  byId('sim-faults').append(h('div', { class: 'check-row' }, input,
    h('label', { for: id }, label, h('span', { class: 'fault-desc' }, ` — ${desc}`))));
}

function renderPhysical(p) {
  if (!p) return;
  if (Number(p.num_slots)) carousel.setNumSlots(Number(p.num_slots));
  const gate = p.gate_open === true ? 'OPEN' : p.gate_open === false ? 'CLOSED' : 'UNKNOWN';
  carousel.update({ angleDeg: p.angle_deg, slot: p.slot ?? null, gate, targetSlot: p.target_slot ?? null });
  simCaption.textContent = `${carousel.describe()}${p.state ? ` Firmware state: ${p.state}.` : ''}`;
  const rows = [
    ['Angle', Number.isFinite(Number(p.angle_deg)) ? `${Number(p.angle_deg).toFixed(1)}°` : '–'],
    ['At the gate', p.slot === null || p.slot === undefined ? 'between compartments' : `compartment ${Number(p.slot) + 1} (slot ${p.slot})`],
    ['Gate', gate.toLowerCase()],
    ['State', p.state || '–'],
  ];
  for (const [key, value] of Object.entries(p)) {
    if (['angle_deg', 'slot', 'gate_open', 'state'].includes(key)) continue;
    rows.push([key, typeof value === 'object' ? JSON.stringify(value) : String(value)]);
  }
  replaceChildren(simPhysical, rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
}

function renderSim(sim) {
  simAvailable = Boolean(sim?.available);
  simUnavailable.hidden = simAvailable;
  for (const [name, input] of faultInputs) {
    input.checked = Boolean(sim?.faults?.[name]);
    input.disabled = !simAvailable;
  }
  for (const btn of simButtons) btn.disabled = !simAvailable;
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

byId('sim-press-confirm').addEventListener('click', () => simAction({ press: 'CONFIRM' }, 'CONFIRM button pressed.'));
byId('sim-press-cancel').addEventListener('click', () => simAction({ press: 'CANCEL' }, 'CANCEL button pressed.'));
byId('sim-reboot').addEventListener('click', () => simAction({ reboot: true }, 'Device rebooting…'));

stream.on('sim.physical', (p) => renderPhysical(p));

// ------------------------------------------------------------------ hardware console

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
    replaceChildren(hwResult, commandResultBox(text, await post('/api/hardware/command', { line: text })));
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
    replaceChildren(hwResult, commandResultBox('STOP', await post('/api/hardware/stop', {})));
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

async function prefillLog() {
  try {
    const events = await get('/api/log?limit=100&topics=device.line');
    for (const ev of Array.isArray(events) ? events : []) hwLog.add(ev);
  } catch {
    /* the live stream still fills the log */
  }
}

// ------------------------------------------------------------------ demo clock

const clockTime = byId('clock-time');
const clockMeta = byId('clock-meta');
const clockStatus = byId('clock-status');
let clockState = null;
let clockFetchedAt = 0;

function elapsedMinutes() {
  return (performance.now() - clockFetchedAt) / 60000;
}

function tickClock() {
  if (!clockState) return;
  const p = parseIso(clockState.now_local);
  if (!p) return;
  const now = new Date(p.wallMs + elapsedMinutes() * 60000);
  clockTime.textContent = clock12(now.getUTCHours(), now.getUTCMinutes(), now.getUTCSeconds());
}

function renderClock(clock) {
  if (!clock?.now_local) return;
  clockState = clock;
  clockFetchedAt = performance.now();
  offsetMin = deviceOffsetFrom(clock.now_local);
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
    renderClock(await post('/api/demo/clock', body));
    clockStatus.textContent = doneText;
  } catch (err) {
    clockStatus.textContent = errorText(err);
    notify(errorText(err), 'error');
  }
}

function plusMinutes(minutes) {
  if (!clockState) return;
  const target = addMinutesToLocal(clockState.now_local, minutes + elapsedMinutes());
  travel({ local_datetime: target }, `Moved forward ${minutes} minutes.`);
}

byId('clock-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const t = normalizeTime(byId('clock-set').value);
  if (!t) {
    clockStatus.textContent = 'Enter a time first.';
    return;
  }
  travel({ local_time: t }, `Travelled to ${time24To12(t)}.`);
});
byId('clock-plus15').addEventListener('click', () => plusMinutes(15));
byId('clock-plus60').addEventListener('click', () => plusMinutes(60));
byId('clock-reset').addEventListener('click', () => travel({ reset: true }, 'Back to real time.'));
byId('clock-next').addEventListener('click', async () => {
  clockStatus.textContent = 'Jumping to the next scheduled dose…';
  try {
    const resp = await post('/api/demo/jump-to-next-dose', {});
    renderClock(resp?.clock);
    const due = resp?.due?.due || [];
    if (due.length) {
      const d = due[0];
      clockStatus.textContent = `${due.length} dose${due.length === 1 ? '' : 's'} due now — ${d.medication_name}, ${formatClock(d.scheduled_local)}, compartment ${d.compartment_number ?? '?'}.`;
    } else {
      clockStatus.textContent = resp?.due?.next_upcoming ? 'Jumped, but no dose is dispensable yet.' : 'No upcoming scheduled dose was found.';
    }
  } catch (err) {
    clockStatus.textContent = errorText(err);
    notify(errorText(err), 'error');
  }
});

stream.on('clock.changed', () => loadClockSoon());
setInterval(tickClock, 1000);

// ------------------------------------------------------------------ demo data

const doseMed = byId('dose-med');
const doseResult = byId('dose-now-result');

async function loadMeds() {
  try {
    const meds = await get('/api/medications?include_inactive=false');
    const keep = doseMed.value;
    const options = [h('option', { value: '' }, 'Any medication (first eligible)')];
    for (const m of Array.isArray(meds) ? meds : []) {
      const where = m.compartment_number ? `compartment ${m.compartment_number}` : 'no compartment';
      options.push(h('option', { value: String(m.medication_id) }, `${m.name} · ${where}`));
    }
    doseMed.replaceChildren(...options);
    if (keep && Array.from(doseMed.options).some((o) => o.value === keep)) doseMed.value = keep;
  } catch (err) {
    doseMed.replaceChildren(h('option', { value: '' }, `Could not load medications (${errorText(err)})`));
  }
}

const loadMedsSoon = debounce(loadMeds, 300);

byId('dose-now').addEventListener('click', async () => {
  doseResult.textContent = 'Creating a dose that is due now…';
  try {
    const body = doseMed.value ? { medication_id: Number(doseMed.value) } : {};
    const resp = await post('/api/demo/dose-now', body);
    const ev = resp?.event;
    if (!ev) {
      doseResult.textContent = 'The server did not return a dose.';
      return;
    }
    const where = ev.compartment_number ? `compartment ${ev.compartment_number}` : 'NO compartment assigned — assign one before dispensing';
    doseResult.textContent = `Created dose_${ev.event_id}: ${ev.medication_name}, due ${formatClock(ev.scheduled_local)}, ${where}.`;
  } catch (err) {
    doseResult.textContent = errorText(err);
    notify(errorText(err), 'error');
  }
});

byId('demo-seed').addEventListener('click', async () => {
  try {
    const resp = await post('/api/demo/seed', {});
    doseResult.textContent = resp?.created ? 'Demo data created.' : 'Demo data was already present.';
    loadMeds();
  } catch (err) {
    notify(errorText(err), 'error');
  }
});

byId('demo-reset').addEventListener('click', async () => {
  const reseed = byId('reset-reseed').checked;
  const { ok } = await confirmDialog({
    title: 'Reset the demo data?',
    message: `All dose events are cleared${reseed ? ' and the demo medications are seeded again' : ''}. The demo clock returns to real time. This cannot be undone.`,
    confirmLabel: 'Reset demo data',
    danger: true,
    iconEl: icon('warning'),
  });
  if (!ok) return;
  try {
    await post('/api/demo/reset', { reseed });
    notify('Demo data reset.', 'success');
    flows.resetAll();
    loadMeds();
    loadClock();
    loadSim();
    loadRaw();
  } catch (err) {
    notify(errorText(err), 'error');
  }
});

stream.on('data.changed', (d, _env, meta) => {
  if (meta.replayed) return;
  if (!d?.entity || ['medication', 'compartment'].includes(d.entity)) loadMedsSoon();
});

// ------------------------------------------------------------------ raw state viewer

const stateSource = byId('state-source');
const stateJson = byId('state-json');
const stateAuto = byId('state-auto');
stateSource.replaceChildren(...STATE_SOURCES.map(([path, label]) => h('option', { value: path }, label)));

async function loadRaw() {
  const path = stateSource.value;
  try {
    const data = await get(path);
    if (path === stateSource.value) stateJson.textContent = prettyJson(data);
  } catch (err) {
    stateJson.textContent = `Error: ${errorText(err)}`;
  }
}

const loadRawSoon = debounce(loadRaw, 600);
stateSource.addEventListener('change', loadRaw);
byId('state-refresh').addEventListener('click', loadRaw);
stream.on('*', (_d, env, meta) => {
  if (!stateAuto.checked || meta.replayed) return;
  if (env?.topic === 'device.line' || env?.topic === 'sim.physical') return;
  loadRawSoon();
});

// ------------------------------------------------------------------ scripted flows

const flows = createFlows(byId('flows'), {
  notify,
  selectedMedication: () => (doseMed.value ? Number(doseMed.value) : null),
  offsetMin: () => offsetMin,
});

// ------------------------------------------------------------------ start

async function loadHealth() {
  try {
    const health = await get('/api/health');
    const cfg = health?.config || {};
    if (Number(cfg.num_slots)) carousel.setNumSlots(Number(cfg.num_slots));
    const badge = byId('mode-badge');
    badge.textContent = `Hardware: ${cfg.hardware_mode || health?.hardware?.mode || '?'} · demo mode ${cfg.demo_mode === false ? 'off' : 'on'}`;
    if (cfg.demo_mode === false) notify('Demo mode is off on the server: demo controls will be refused.', 'warning');
  } catch (err) {
    byId('mode-badge').textContent = 'Server unreachable';
  }
}

stream.on(RECONNECTED, () => {
  loadHealth();
  loadSim();
  loadClock();
  loadMeds();
  loadRaw();
});

stream.start();
loadHealth();
loadSim();
loadClock();
loadMeds();
loadRaw();
prefillLog();
