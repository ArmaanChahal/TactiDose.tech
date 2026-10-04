/**
 * Scripted demo checklists for the operator panel. Each checklist calls the real API in
 * order and ticks its steps off; nothing here bypasses the drop rules.
 *   A  a scheduled pill drops by itself (jump to the next dose -> auto-drop -> notification)
 *   B  "Drop pill", then a second drop refused by the global cooldown
 *   C  the assistant drops a pill when asked, then refuses another one
 *   D  make a report and send it to the doctor (SAVED as an .eml without SMTP)
 */

import { LONG_TIMEOUT_MS, get, post } from '../api.js';
import { errorText, h, uid } from '../dom.js';
import { icon } from '../icons.js';
import { containerNumberOf, formatBytes, formatClock } from '../format.js';
import { deliveryText } from '../reports.js';
import { dropStatusInfo, reasonText } from '../words.js';

export class FlowError extends Error {}

const STEP_STATES = Object.freeze({
  pending: { word: 'Not started', icon: 'pending' },
  running: { word: 'Running…', icon: 'rotate' },
  done: { word: 'Done', icon: 'check-circle' },
  failed: { word: 'Failed', icon: 'x-circle' },
  skipped: { word: 'Skipped', icon: 'slash' },
});

const DROP_WAIT_MS = 60000;
const NOTE_WAIT_MS = 20000;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Poll `check()` until it returns something truthy or the time runs out (null). */
export async function waitFor(check, { timeoutMs = 30000, intervalMs = 1500, now = () => Date.now() } = {}) {
  const deadline = now() + timeoutMs;
  for (;;) {
    const result = await check();
    if (result) return result;
    if (now() >= deadline) return null;
    await sleep(intervalMs);
  }
}

export function maxId(list, key) {
  return (Array.isArray(list) ? list : []).reduce((m, x) => Math.max(m, Number(x?.[key]) || 0), 0);
}

/** A scheduled drop newer than `mark` whose outcome is known (in-flight rows have no completed_at). */
export function findScheduledDrop(drops, mark) {
  return (Array.isArray(drops) ? drops : [])
    .filter((d) => d.source === 'schedule' && Number(d.drop_id) > mark && (d.completed_at || d.status === 'DENIED'))
    .sort((a, b) => a.drop_id - b.drop_id)[0] || null;
}

function patientId(me) {
  return me?.patient?.patient_id ?? (me?.user?.role === 'patient' ? me.user.user_id : me?.patients?.[0]?.patient_id) ?? null;
}

function requirePatient(ctx) {
  const me = ctx.session();
  if (me?.user?.role !== 'patient') {
    throw new FlowError('Sign in as the patient (Alex) in the "Signed in" card: only the patient can drop pills or talk to the assistant.');
  }
  return patientId(me);
}

function usableContainer(status, avoidSlot = null) {
  const list = (status?.containers || []).filter((c) => c.medication_id !== null && c.medication_id !== undefined && Number(c.pill_count) > 0);
  return list.find((c) => c.slot !== avoidSlot) || list[0] || null;
}

/** Shared first step of B and C: patient session, no cooldown running, a container with pills. */
async function prepareManualDrop(ctx, s) {
  s.pid = requirePatient(ctx);
  let status = await get(`/api/patients/${s.pid}/status`);
  let note = 'No cooldown is running.';
  if (Number(status.cooldown_remaining_s) > 0) {
    await ctx.skipCooldown(status);
    status = await get(`/api/patients/${s.pid}/status`);
    if (Number(status.cooldown_remaining_s) > 0) {
      throw new FlowError('A cooldown is still running after moving the clock. Reset the demo data, or set the cooldown to a shorter time in the care portal.');
    }
    note = 'A cooldown from an earlier drop was running, so the demo clock moved past it.';
  }
  const c = usableContainer(status);
  if (!c) throw new FlowError('No container has a medication with pills. Refill one in the care portal (Containers tab).');
  s.container = c;
  s.status = status;
  return `${note} Using container ${containerNumberOf(c)}: ${c.medication_name}, ${c.pill_count} pills.`;
}

export function buildFlows(ctx) {
  return [
    {
      id: 'A',
      title: 'A. A scheduled pill drops by itself',
      intro: 'Jumps the demo clock to the next scheduled dose. The scheduler drops it automatically and a "pill dropped" notification arrives — the patient did not have to remember.',
      steps: [
        {
          label: 'Jump the demo clock to the next scheduled dose',
          async run(s) {
            const me = ctx.session();
            s.pid = patientId(me);
            if (!s.pid) throw new FlowError('Sign in first (any demo account).');
            const [drops, notes] = await Promise.all([get(`/api/patients/${s.pid}/drops?days=1`), get('/api/notifications?limit=50')]);
            s.dropMark = maxId(drops, 'drop_id');
            s.noteMark = maxId(notes, 'notification_id');
            const resp = await post('/api/demo/jump-to-next-dose', {});
            ctx.renderClock(resp?.clock);
            if (!resp?.next) throw new FlowError('There is no upcoming scheduled dose. Add a time in the care portal (Schedule tab) first.');
            s.next = resp.next;
            const n = containerNumberOf(resp.next) ?? '?';
            return `Now ${formatClock(resp.clock?.now_local)}. Due: ${resp.next.medication_name} at ${formatClock(resp.next.scheduled_local)}, container ${n}.`;
          },
        },
        {
          label: 'The pill drops automatically',
          async run(s) {
            const drop = await waitFor(async () => findScheduledDrop(await get(`/api/patients/${s.pid}/drops?days=1`), s.dropMark), { timeoutMs: DROP_WAIT_MS });
            if (!drop) throw new FlowError('No automatic drop within a minute. Check that automatic drops are on (care portal, Cooldown tab) and the device is connected.');
            s.drop = drop;
            if (drop.status === 'DROPPED') {
              return `Dropped automatically: ${drop.medication_name}, container ${drop.container_number}. ${drop.pill_count_after ?? '?'} pills left.`;
            }
            if (drop.status === 'DENIED' && drop.reason === 'ALREADY_SATISFIED') {
              return 'Not dropped twice: this dose was already satisfied by an earlier drop of the same medication (the double-dose guard).';
            }
            throw new FlowError(`${dropStatusInfo(drop.status).word}: ${reasonText(drop.reason) || drop.hardware_result || 'no reason given'}.`);
          },
        },
        {
          label: 'A "pill dropped" notification arrives',
          async run(s) {
            if (s.drop?.status !== 'DROPPED') return { skip: 'No pill dropped this time, so there is no "pill dropped" notification.' };
            const isIt = (n) => n && n.kind === 'PILL_DROPPED' && Number(n.notification_id) > s.noteMark;
            const note = await waitFor(async () => ctx.recent('notification').find(isIt)
              || (await get('/api/notifications?limit=50')).find(isIt), { timeoutMs: NOTE_WAIT_MS });
            if (!note) throw new FlowError('No "pill dropped" notification reached this account.');
            return `"${note.title}": ${note.body}`;
          },
        },
      ],
    },
    {
      id: 'B',
      title: 'B. Drop pill, then the cooldown refuses a second one',
      intro: 'Presses "Drop pill" for the patient, then tries again right away: the global cooldown refuses the second pill and says when the next one is allowed.',
      steps: [
        { label: 'Check the patient account and that no cooldown is running', run: (s) => prepareManualDrop(ctx, s) },
        {
          label: 'Press "Drop pill"',
          async run(s) {
            const outcome = await post(`/api/patients/${s.pid}/drops`, { slot: s.container.slot }, { timeoutMs: 75000 });
            if (outcome?.status !== 'DROPPED') throw new FlowError(`${dropStatusInfo(outcome?.status).word}: ${outcome?.message || 'no message'}`);
            return `Dropped: "${outcome.message}"`;
          },
        },
        {
          label: 'Press "Drop pill" again right away',
          async run(s) {
            const status = await get(`/api/patients/${s.pid}/status`);
            const c = usableContainer(status, s.container.slot) || s.container;
            const outcome = await post(`/api/patients/${s.pid}/drops`, { slot: c.slot }, { timeoutMs: 75000 });
            if (outcome?.status === 'DENIED' && outcome.reason === 'COOLDOWN') return `Refused, as it should be: "${outcome.message}"`;
            if (outcome?.status === 'DROPPED') throw new FlowError('A second pill dropped: the cooldown is 0 minutes. Set a waiting time in the care portal (Cooldown tab).');
            throw new FlowError(`${dropStatusInfo(outcome?.status).word} (${outcome?.reason || 'no reason'}): ${outcome?.message || ''}`);
          },
        },
      ],
    },
    {
      id: 'C',
      title: 'C. The assistant drops a pill, then refuses another',
      intro: 'Asks the assistant for a pill: it checks the status and requests one. Asking again is refused by the same cooldown rule. The conversation and its tool calls are stored.',
      steps: [
        { label: 'Check the patient account and that no cooldown is running', run: (s) => prepareManualDrop(ctx, s) },
        {
          label: 'Ask: "Can I have my pill from container …?"',
          async run(s) {
            const n = containerNumberOf(s.container);
            const reply = await post('/api/agent/chat', { text: `Can I have my pill from container ${n} now, please?`, input_mode: 'text' }, { timeoutMs: LONG_TIMEOUT_MS });
            s.conversationId = reply?.conversation_id ?? null;
            const dropped = (reply?.actions || []).find((a) => a.status === 'DROPPED');
            if (!dropped) throw new FlowError(`The assistant did not drop a pill. It said: "${reply?.text || ''}"`);
            return `Assistant (${reply.model || 'agent'}): "${reply.text}"`;
          },
        },
        {
          label: 'Ask for another pill',
          async run(s) {
            const body = { text: 'Can I have another pill now?', input_mode: 'text' };
            if (s.conversationId) body.conversation_id = s.conversationId;
            const reply = await post('/api/agent/chat', body, { timeoutMs: LONG_TIMEOUT_MS });
            const actions = reply?.actions || [];
            if (actions.some((a) => a.status === 'DROPPED')) throw new FlowError('A second pill dropped: the cooldown is 0 minutes.');
            const denied = actions.find((a) => a.status === 'DENIED');
            return `Refused, as it should be: "${reply?.text || ''}"${denied ? ` (rule: ${denied.reason})` : ''}`;
          },
        },
      ],
    },
    {
      id: 'D',
      title: 'D. Make a report and send it to the doctor',
      intro: 'Makes a 7-day PDF report and sends it to the linked doctor. Without an email server it is saved as an email file (status "Saved").',
      steps: [
        {
          label: 'Make a 7-day report',
          async run(s) {
            s.pid = patientId(ctx.session());
            if (!s.pid) throw new FlowError('Sign in first (any demo account).');
            const report = await post(`/api/patients/${s.pid}/reports`, { days: 7 }, { timeoutMs: LONG_TIMEOUT_MS });
            if (report?.status && report.status !== 'READY') throw new FlowError(`The report could not be made (${report.status}).`);
            s.report = report;
            return {
              text: `"${report.title}" is ready${report.pdf_size ? ` (${formatBytes(report.pdf_size)})` : ''}.`,
              link: { href: `/api/reports/${report.report_id}/pdf`, label: 'Open the PDF' },
            };
          },
        },
        {
          label: 'Send it to the doctor',
          async run(s) {
            const resp = await post(`/api/reports/${s.report.report_id}/send`, {}, { timeoutMs: LONG_TIMEOUT_MS });
            const deliveries = Array.isArray(resp?.deliveries) ? resp.deliveries : [];
            if (!deliveries.length) throw new FlowError('No doctor is linked to this patient, so there was nobody to send it to.');
            if (deliveries.every((d) => d.status === 'FAILED')) throw new FlowError(deliveryText(deliveries[0]));
            return deliveries.map(deliveryText).join(' ');
          },
        },
      ],
    },
  ];
}

/** Render the checklists into `container`. ctx: {session(), recent(topic), renderClock(clock), skipCooldown(status), notify()} */
export function createFlows(container, ctx) {
  const flows = buildFlows(ctx);
  const views = flows.map((flow) => {
    const titleId = uid(`flow-${flow.id}`);
    const status = h('p', { class: 'flow-status', role: 'status' });
    const steps = flow.steps.map((step) => {
      const stateEl = h('span', { class: 'step-state' });
      const iconSlot = h('span', { class: 'step-icon' });
      const detail = h('p', { class: 'step-detail' });
      const li = h('li', { class: 'flow-step' }, iconSlot, h('span', { class: 'step-label' }, step.label), stateEl, detail);
      return { step, li, stateEl, iconSlot, detail };
    });
    const runBtn = h('button', { type: 'button', class: 'btn btn-primary' }, icon('play'), `Run checklist ${flow.id}`);
    const resetBtn = h('button', { type: 'button', class: 'btn' }, 'Reset');
    const article = h('article', { class: 'flow', 'aria-labelledby': titleId },
      h('h3', { id: titleId }, flow.title),
      h('p', { class: 'muted' }, flow.intro),
      h('ol', { class: 'flow-steps' }, steps.map((x) => x.li)),
      h('div', { class: 'btn-row' }, runBtn, resetBtn),
      status);
    const view = { flow, steps, runBtn, resetBtn, status, running: false };
    runBtn.addEventListener('click', () => run(view));
    resetBtn.addEventListener('click', () => reset(view));
    container.append(article);
    reset(view);
    return view;
  });

  function setStep(x, state, text = '', link = null) {
    const info = STEP_STATES[state];
    x.li.dataset.state = state;
    x.stateEl.textContent = info.word;
    x.iconSlot.replaceChildren(icon(info.icon));
    x.detail.replaceChildren(text ? document.createTextNode(text) : '');
    if (link) x.detail.append(' ', h('a', { href: link.href, target: '_blank', rel: 'noopener' }, link.label));
  }

  function reset(view) {
    if (view.running) return;
    for (const x of view.steps) setStep(x, 'pending');
    view.status.textContent = '';
  }

  async function run(view) {
    if (view.running) return;
    reset(view);
    view.running = true;
    view.runBtn.disabled = true;
    view.resetBtn.disabled = true;
    view.status.textContent = `Running checklist ${view.flow.id}…`;
    const state = {};
    let failed = false;
    for (const x of view.steps) {
      if (failed) {
        setStep(x, 'skipped', 'Skipped because an earlier step failed.');
        continue;
      }
      setStep(x, 'running');
      try {
        const result = await x.step.run(state);
        if (result && typeof result === 'object' && result.skip) setStep(x, 'skipped', result.skip);
        else if (result && typeof result === 'object') setStep(x, 'done', result.text, result.link);
        else setStep(x, 'done', String(result || ''));
      } catch (err) {
        failed = true;
        setStep(x, 'failed', err instanceof FlowError ? err.message : errorText(err));
      }
    }
    view.status.textContent = failed ? `Checklist ${view.flow.id} stopped: a step failed.` : `Checklist ${view.flow.id} finished.`;
    ctx.notify(view.status.textContent, failed ? 'error' : 'success');
    view.running = false;
    view.runBtn.disabled = false;
    view.resetBtn.disabled = false;
  }

  return {
    resetAll() {
      for (const v of views) reset(v);
    },
  };
}
