/**
 * Guided judge demo (MORNING / NOON / NIGHT, demo mode only), shared by the kiosk and the demo
 * panel. The server runs the flow (tactidose/guided/runner.py); this module only shows it:
 * POST /api/demo/guided/start | answer | stop, live progress from the "demo.guided" event.
 *
 * Speech follows the existing chain: the server's audio_url (ElevenLabs -> cache -> offline voice)
 * or the browser's speechSynthesis, and the text is always shown (captions). The buzzer is a
 * simulated one: a beeping tone from this screen while the server says it is on.
 */

import { get, post } from './api.js';
import { errorText, h, uid } from './dom.js';
import { icon } from './icons.js';

const STEP_WORDS = Object.freeze({
  intro: 'Starting',
  ask_take: 'Asking: take the pill?',
  declined: 'Skipped',
  buzzer: 'Buzzer on, dropping the pill',
  dispensed: 'Drop finished',
  drop_result: 'Drop finished',
  ask_taken: 'Asking: did you take it?',
  taken: 'Noted',
  ask_checkin: 'Asking: how are you feeling?',
  checkin: 'Check-in noted',
  pause: 'Short pause',
  summary: 'Finished',
  emergency: 'Possible emergency: the demo stopped',
  stopped: 'Stopped',
  error: 'Something went wrong',
});

/** Simulated buzzer: a short beep every 600 ms while on (WebAudio; silent where unavailable). */
export class Buzzer {
  constructor() {
    this._ctx = null;
    this._timer = null;
  }

  get on() {
    return this._timer !== null;
  }

  _beep() {
    try {
      this._ctx = this._ctx || new (globalThis.AudioContext || globalThis.webkitAudioContext)();
      const osc = this._ctx.createOscillator();
      const gain = this._ctx.createGain();
      osc.frequency.value = 880;
      gain.gain.value = 0.15;
      osc.connect(gain).connect(this._ctx.destination);
      osc.start();
      osc.stop(this._ctx.currentTime + 0.25);
    } catch {
      /* no audio: the on-screen indicator still shows the buzzer */
    }
  }

  set(on) {
    if (on && this._timer === null) {
      this._beep();
      this._timer = setInterval(() => this._beep(), 600);
    } else if (!on && this._timer !== null) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }
}

/**
 * Mount the guided demo controls in `root`. Options: stream (EventStream), speaker
 * (ReplySpeaker), speak() -> bool (speak prompts here), allowReset (show "fresh demo data"),
 * onCaption(text) (kiosk captions), onAwaiting(kind) (kiosk: start listening), onRunning(bool).
 * Returns {answer(text, mode), stop(), get awaiting, get running}.
 */
export function createGuidedDemo(root, {
  stream,
  speaker = null,
  speak = () => true,
  allowReset = false,
  onCaption = () => {},
  onAwaiting = () => {},
  onRunning = () => {},
} = {}) {
  const resetId = uid('gd-reset');
  const answerId = uid('gd-answer');
  const startBtn = h('button', { type: 'button', class: 'btn btn-primary' }, icon('play'), 'Run guided demo');
  const stopBtn = h('button', { type: 'button', class: 'btn', disabled: true }, icon('stop'), 'Stop demo');
  const reset = allowReset ? h('input', { type: 'checkbox', id: resetId, checked: true }) : null;
  const status = h('p', { class: 'gd-status', role: 'status' }, 'Not running.');
  const buzzerTag = h('span', { class: 'badge tone-caution gd-buzzer', hidden: true }, icon('bell'), 'Buzzer on');
  const results = h('ul', { class: 'gd-results' });
  const log = h('ol', { class: 'gd-log', role: 'log', 'aria-label': 'Guided demo transcript' });
  const input = h('input', { id: answerId, type: 'text', autocomplete: 'off', maxlength: '500', disabled: true });
  const send = h('button', { type: 'submit', class: 'btn', disabled: true }, icon('send'), 'Answer');
  const form = h('form', { class: 'gd-answer', novalidate: true },
    h('label', { for: answerId }, 'Type an answer (if the microphone does not work)'), input, send);
  root.replaceChildren(
    h('div', { class: 'btn-row' }, startBtn, stopBtn,
      reset ? h('span', { class: 'check-row' }, reset, h('label', { for: resetId }, 'Start from fresh demo data')) : null,
      buzzerTag),
    status, form, results, log);

  const buzzer = new Buzzer();
  let state = { state: 'idle' };
  let lastSaid = '';

  const running = () => ['starting', 'running'].includes(state.state);
  const awaiting = () => (running() ? state.awaiting || null : null);

  function line(who, text) {
    log.append(h('li', { class: `gd-line from-${who}` }, h('strong', {}, who === 'patient' ? 'Patient: ' : 'CareBridge: '), text));
    log.scrollTop = log.scrollHeight;
  }

  function render() {
    const on = running();
    startBtn.disabled = on;
    stopBtn.disabled = !on;
    input.disabled = !awaiting();
    send.disabled = !awaiting();
    const slot = state.slot ? `${state.slot.name[0].toUpperCase()}${state.slot.name.slice(1)} pill` : '';
    const step = STEP_WORDS[state.step] || (state.step ? state.step.replaceAll('_', ' ') : '');
    status.textContent = on
      ? `${slot}${slot && step ? ' — ' : ''}${step}${awaiting() ? ' (waiting for an answer)' : ''}`
      : state.state === 'idle' ? 'Not running.' : `Demo ${state.state}.`;
    buzzerTag.hidden = !state.buzzer;
    results.replaceChildren(...(state.results || []).map((r) => h('li', {}, r.summary)));
    onRunning(on);
  }

  function apply(d) {
    if (!d || typeof d !== 'object') return;
    if (d.run_id !== state.run_id) log.replaceChildren();
    state = d;
    if (d.heard) line('patient', d.heard);
    if (d.say && d.say !== lastSaid) {
      lastSaid = d.say;
      line('assistant', d.say);
      onCaption(d.say);
      if (speak() && speaker) speaker.speak(d.say, d.audio_url || null);
    }
    buzzer.set(Boolean(d.buzzer) && running() && speak());
    render();
    if (d.awaiting && running()) {
      input.value = '';
      onAwaiting(d.awaiting);
    }
  }

  async function answer(text, mode = 'text') {
    const t = String(text || '').trim();
    if (!t || !awaiting()) return false;
    try {
      const r = await post('/api/demo/guided/answer', { text: t, input_mode: mode });
      if (r?.accepted) state = { ...state, awaiting: null };
      render();
      return Boolean(r?.accepted);
    } catch (err) {
      status.textContent = `The answer was not sent: ${errorText(err)}`;
      return false;
    }
  }

  async function stop() {
    buzzer.set(false);
    speaker?.stop();
    try {
      await post('/api/demo/guided/stop', {});
    } catch (err) {
      status.textContent = `Could not stop: ${errorText(err)}`;
    }
  }

  startBtn.addEventListener('click', async () => {
    startBtn.disabled = true;
    log.replaceChildren();
    lastSaid = '';
    try {
      apply(await post('/api/demo/guided/start', { reset: Boolean(reset?.checked) }));
    } catch (err) {
      status.textContent = `The demo did not start: ${errorText(err)}`;
      startBtn.disabled = false;
    }
  });
  stopBtn.addEventListener('click', stop);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    answer(input.value, 'text');
  });

  stream.on('demo.guided', (d, _env, meta) => {
    if (meta?.replayed && !d?.final) return;
    apply(d);
  });

  get('/api/demo/guided').then((d) => {
    if (d && d.state !== 'idle') {
      state = d;
      render();
    }
  }).catch(() => {});

  return {
    answer,
    stop,
    get awaiting() {
      return awaiting();
    },
    get running() {
      return running();
    },
  };
}
