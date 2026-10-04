/**
 * Scripted checklists for the handoff §25 demo flows. Each step calls the real
 * API and is ticked off (word + icon) or fails with the server's explanation.
 *
 *   A — scheduled dispense: home → dose due now → "What do I take now?" →
 *       "Dispense" → DISPENSED recorded → "Taken" → TAKEN recorded.
 *   B — duplicate prevention: the TAKEN dose is requested again → DUPLICATE,
 *       no motion command on the serial line, duplicate warning spoken.
 *   C — label onboarding via upload (works with the fake extractor): upload a
 *       generated demo label → UNCONFIRMED draft → a person reviews & confirms →
 *       medication saved → the person assigns a compartment.
 *
 * Human steps ("your turn") never auto-confirm: label data is only saved after
 * the operator ticks "I confirm this information is correct".
 */

import { get, post, postText, put, upload } from '../api.js';
import { errorText, h, replaceChildren, uid } from '../dom.js';
import { icon } from '../icons.js';
import { dateKey, formatClock, formatClockDevice } from '../format.js';
import { describeCommand } from '../hwview.js';
import { linesOf } from '../medform.js';

const STATUS = {
  pending: ['pending', 'Not run'],
  running: ['rotate', 'Running…'],
  waiting: ['hand', 'Your turn'],
  done: ['check-circle', 'Done'],
  failed: ['x-circle', 'Failed'],
  skipped: ['slash', 'Skipped'],
};

const MOTION_COMMAND = /^(DISPENSE_SLOT|MOVE_SLOT|OPEN_GATE|HOME)\b/i;
const DUPLICATE_PHRASE = /already been accessed/i;

// ------------------------------------------------------------------ helpers

async function findEvent(shared, eventId) {
  const events = shared.eventDate
    ? await get(`/api/dose-events?date=${encodeURIComponent(shared.eventDate)}`)
    : await get('/api/dose-events');
  const ev = (Array.isArray(events) ? events : []).find((e) => e.event_id === eventId);
  if (!ev) throw new Error(`dose_${eventId} was not found in the dose log.`);
  return ev;
}

async function lastLineSeq() {
  const events = await get('/api/log?limit=200&topics=device.line');
  return (Array.isArray(events) ? events : []).reduce((max, e) => Math.max(max, Number(e.seq) || 0), 0);
}

/** A clear, high-contrast demo label drawn on a canvas (PNG). */
export function makeDemoLabel() {
  const canvas = document.createElement('canvas');
  canvas.width = 1000;
  canvas.height = 640;
  const g = canvas.getContext('2d');
  g.fillStyle = '#ffffff';
  g.fillRect(0, 0, canvas.width, canvas.height);
  g.strokeStyle = '#000000';
  g.lineWidth = 12;
  g.strokeRect(24, 24, canvas.width - 48, canvas.height - 48);
  g.fillStyle = '#000000';
  g.font = 'bold 40px sans-serif';
  g.fillText('DEMO LABEL — NOT A MEDICATION', 70, 110);
  g.font = 'bold 76px sans-serif';
  g.fillText('Vitamin C (demo candy)', 70, 240);
  g.font = '56px sans-serif';
  g.fillText('1 piece', 70, 330);
  g.font = '44px sans-serif';
  g.fillText('Take one piece in the morning.', 70, 440);
  g.fillText('Warning: demo token only.', 70, 520);
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('Could not draw the demo label.'))), 'image/png');
  });
}

function field(labelText, control) {
  return h('div', { class: 'field' }, h('label', { for: control.id }, labelText), control);
}

/** Human review step for flow C: resolves with the reviewed values, rejects on "Reject". */
function reviewScan(stepUi, scan) {
  return new Promise((resolve, reject) => {
    const ex = scan.extracted || {};
    const name = h('input', { id: uid('fc-name'), type: 'text', value: ex.medication_name || '', maxlength: '200' });
    const strength = h('input', { id: uid('fc-strength'), type: 'text', value: ex.strength || '', maxlength: '120' });
    const instructions = h('textarea', { id: uid('fc-instr'), rows: '2' });
    instructions.value = ex.visible_instructions || '';
    const warnings = h('textarea', { id: uid('fc-warn'), rows: '2' });
    warnings.value = (ex.warnings_visible || []).join('\n');
    const boxId = uid('fc-confirm');
    const box = h('input', { type: 'checkbox', id: boxId });
    const error = h('p', { class: 'form-error', role: 'alert', hidden: true });
    const confirmBtn = h('button', { type: 'button', class: 'btn btn-primary' }, 'Confirm and save');
    const rejectBtn = h('button', { type: 'button', class: 'btn btn-danger' }, 'Reject scan');

    confirmBtn.addEventListener('click', () => {
      if (!name.value.trim()) {
        error.textContent = 'The name is required.';
        error.hidden = false;
        name.focus();
        return;
      }
      if (!box.checked) {
        error.textContent = "Check every field, then tick 'I confirm this information is correct'.";
        error.hidden = false;
        box.focus();
        return;
      }
      confirmBtn.disabled = true;
      rejectBtn.disabled = true;
      resolve({
        name: name.value.trim(),
        strength: strength.value.trim(),
        instructions_text: instructions.value.trim(),
        warnings: linesOf(warnings.value),
      });
    });
    rejectBtn.addEventListener('click', async () => {
      confirmBtn.disabled = true;
      rejectBtn.disabled = true;
      try {
        await post(`/api/onboarding/scans/${scan.scan_id}/reject`, {});
        reject(new Error('The operator rejected the scan. Nothing was saved.'));
      } catch (err) {
        reject(err);
      }
    });

    replaceChildren(stepUi,
      h('div', { class: 'unconfirmed-banner', role: 'note' }, icon('warning'), 'UNCONFIRMED — review every field against the label'),
      ex.confidence_notes ? h('p', { class: 'muted' }, `Reader notes: ${ex.confidence_notes}`) : null,
      ex.legible === false ? h('p', { class: 'form-error' }, 'The reader says the label may not be legible.') : null,
      field('Name', name),
      field('Strength', strength),
      field('Instructions', instructions),
      field('Warnings (one per line)', warnings),
      h('div', { class: 'confirm-box' }, box, h('label', { for: boxId }, 'I confirm this information is correct')),
      error,
      h('div', { class: 'btn-row' }, confirmBtn, rejectBtn));
    name.focus();
  });
}

/** Human step for flow C: choose a compartment for the new medication. */
function chooseCompartment(stepUi, compartments, medication) {
  return new Promise((resolve, reject) => {
    const selectId = uid('fc-slot');
    const select = h('select', { id: selectId },
      compartments.map((c) => h('option', { value: String(c.slot) },
        `Compartment ${c.compartment_number || c.slot + 1}${c.medication_name ? ` (now: ${c.medication_name})` : ' (empty)'}`)));
    const firstEmpty = compartments.find((c) => !c.medication_id);
    if (firstEmpty) select.value = String(firstEmpty.slot);
    const assignBtn = h('button', { type: 'button', class: 'btn btn-primary' }, 'Assign');
    const error = h('p', { class: 'form-error', role: 'alert', hidden: true });
    assignBtn.addEventListener('click', async () => {
      assignBtn.disabled = true;
      const slot = Number(select.value);
      try {
        await put(`/api/compartments/${slot}`, { medication_id: medication.medication_id });
        resolve(`${medication.name} assigned to compartment ${slot + 1}. Load the candy with Caregiver → Compartments → Present for loading.`);
      } catch (err) {
        error.textContent = errorText(err);
        error.hidden = false;
        assignBtn.disabled = false;
      }
    });
    replaceChildren(stepUi,
      h('div', { class: 'field' }, h('label', { for: selectId }, `Compartment for ${medication.name}`), select),
      error,
      h('div', { class: 'btn-row' }, assignBtn));
    select.focus();
    if (!compartments.length) reject(new Error('No compartments are configured.'));
  });
}

// ------------------------------------------------------------------ flow definitions

function flowDefinitions(env) {
  const A = {
    id: 'A',
    title: 'Flow A — Scheduled dispense',
    intro: 'Homes the device, creates a dose due now, then runs the voice dialogue end to end.',
    steps: [
      {
        label: 'System homes',
        async run() {
          const hw = await get('/api/hardware');
          if (!hw?.connected) throw new Error('The device is not connected. Check the hardware mode / USB cable.');
          if (hw.gate === 'OPEN' || hw.state === 'GATE_OPEN') {
            throw new Error('The gate is open. Finish the current dose ("Taken") or press Cancel first.');
          }
          if (hw.homed && hw.state === 'READY') return `Already homed (compartment ${hw.slot + 1} at the gate).`;
          const resp = await post('/api/hardware/home', {});
          if (!resp?.ok) throw new Error(`Homing failed: ${describeCommand(resp)}`);
          return 'Homed: compartment 1 is at the gate.';
        },
      },
      {
        label: 'Create a dose that is due now',
        async run(shared) {
          const medicationId = env.selectedMedication();
          const resp = await post('/api/demo/dose-now', medicationId ? { medication_id: medicationId } : {});
          const ev = resp?.event;
          if (!ev) throw new Error('No dose event was created.');
          shared.eventId = ev.event_id;
          shared.eventDate = dateKey(ev.scheduled_local);
          if (!ev.compartment_number) {
            throw new Error(`${ev.medication_name} has no compartment. Assign one in Caregiver → Compartments, then run again.`);
          }
          return `dose_${ev.event_id}: ${ev.medication_name}, compartment ${ev.compartment_number}, due ${formatClock(ev.scheduled_local)}.`;
        },
      },
      {
        label: 'User command is recognized: "What do I take now?"',
        async run(shared) {
          const reply = await postText('What do I take now?', 'keyboard');
          // With check_due_auto_dispense the reply already reports the dispense.
          shared.autoDispensed = reply?.outcome?.status === 'DISPENSED';
          if (reply?.intent !== 'CHECK_DUE' && !shared.autoDispensed) {
            throw new Error(`Understood as ${reply?.intent || 'nothing'}, expected CHECK_DUE.`);
          }
          return `TactiDose said: “${reply.text}”`;
        },
      },
      {
        label: 'Backend selects the slot; the carousel turns and the gate opens: "Dispense"',
        async run(shared) {
          if (shared.autoDispensed) return 'Already dispensed by "What do I take now?" (auto-dispense is on).';
          const reply = await postText('Dispense', 'keyboard');
          const status = reply?.outcome?.status;
          if (status !== 'DISPENSED') throw new Error(`Dispense returned ${status || reply?.kind || 'no outcome'}: “${reply?.text || ''}”`);
          const dose = reply.outcome.dose;
          const other = dose && shared.eventId && dose.event_id !== shared.eventId ? ` (note: dispensed dose_${dose.event_id}, an earlier due dose)` : '';
          if (dose?.event_id) shared.eventId = dose.event_id;
          return `Gate open at compartment ${dose?.compartment_number ?? '?'}${other}. TactiDose said: “${reply.text}”`;
        },
      },
      {
        label: 'Backend records DISPENSED',
        async run(shared) {
          const ev = await findEvent(shared, shared.eventId);
          if (!['DISPENSED', 'TAKEN'].includes(ev.status)) throw new Error(`dose_${ev.event_id} is ${ev.status}, expected DISPENSED.`);
          const at = ev.dispensed_at ? ` at ${formatClockDevice(ev.dispensed_at, env.offsetMin())}` : '';
          return `dose_${ev.event_id} is ${ev.status}${at}.`;
        },
      },
      {
        label: 'User confirms: "Taken"',
        async run() {
          const reply = await postText('Taken', 'keyboard');
          const status = reply?.outcome?.status;
          if (status !== 'CONFIRMED' && status !== 'ALREADY_CONFIRMED') {
            throw new Error(`Taken returned ${status || reply?.kind || 'no outcome'}: “${reply?.text || ''}”`);
          }
          return `TactiDose said: “${reply.text}”`;
        },
      },
      {
        label: 'Backend records TAKEN',
        async run(shared) {
          const ev = await findEvent(shared, shared.eventId);
          if (ev.status !== 'TAKEN') throw new Error(`dose_${ev.event_id} is ${ev.status}, expected TAKEN.`);
          shared.takenEventId = ev.event_id;
          return `dose_${ev.event_id} is TAKEN. Flow A complete.`;
        },
      },
    ],
  };

  const B = {
    id: 'B',
    title: 'Flow B — Duplicate prevention',
    intro: 'Requests the already-taken dose again and checks that nothing moves. Run Flow A first.',
    steps: [
      {
        label: 'Find the dose that was already taken',
        async run(shared) {
          const events = shared.eventDate
            ? await get(`/api/dose-events?date=${encodeURIComponent(shared.eventDate)}`)
            : await get('/api/dose-events');
          const list = Array.isArray(events) ? events : [];
          const ev = list.find((e) => e.event_id === shared.takenEventId && e.status === 'TAKEN')
            || list.filter((e) => e.status === 'TAKEN').pop();
          if (!ev) throw new Error('No dose is TAKEN today. Run Flow A first.');
          shared.duplicateEventId = ev.event_id;
          return `dose_${ev.event_id} (${ev.medication_name}) is TAKEN.`;
        },
      },
      {
        label: 'Check that no other dose is due (so nothing can be dispensed by mistake)',
        async run() {
          const state = await get('/api/state');
          const due = state?.due?.due || [];
          if (due.length) {
            throw new Error(`dose_${due[0].event_id} (${due[0].medication_name}) is due now. Skip it in Caregiver → Today or reset the demo data first.`);
          }
          if (state?.due?.awaiting_confirmation?.length) throw new Error('A dose is waiting for "Taken". Confirm or cancel it first.');
          return 'No other dose is dispensable now.';
        },
      },
      {
        label: 'The same scheduled event is requested again: "Dispense"',
        async run(shared) {
          shared.lineSeq = await lastLineSeq();
          const reply = await postText('Dispense', 'keyboard');
          shared.duplicateReply = reply;
          const status = reply?.outcome?.status;
          if (status !== 'DUPLICATE') throw new Error(`Expected DUPLICATE, got ${status || reply?.kind || 'no outcome'}: “${reply?.text || ''}”`);
          return 'The backend refused: DUPLICATE.';
        },
      },
      {
        label: 'No hardware movement occurs',
        async run(shared) {
          const events = await get('/api/log?limit=200&topics=device.line');
          const motion = (Array.isArray(events) ? events : []).filter((e) => Number(e.seq) > shared.lineSeq
            && e.data?.dir === 'tx' && MOTION_COMMAND.test(String(e.data?.line || '')));
          if (motion.length) throw new Error(`A motion command was sent: ${motion[0].data.line}`);
          return 'No DISPENSE_SLOT / MOVE_SLOT / OPEN_GATE / HOME was sent to the device.';
        },
      },
      {
        label: 'System speaks the duplicate warning',
        async run(shared) {
          const text = shared.duplicateReply?.text || '';
          if (!DUPLICATE_PHRASE.test(text)) throw new Error(`Expected “That scheduled dose has already been accessed.”, heard “${text}”.`);
          return `TactiDose said: “${text}” Flow B complete.`;
        },
      },
    ],
  };

  const C = {
    id: 'C',
    title: 'Flow C — Label onboarding (upload)',
    intro: 'Uploads a generated demo label (or the photo you choose below). Works offline with TACTIDOSE_LABEL_EXTRACTOR=fake.',
    upload: true,
    steps: [
      {
        label: 'Camera scans the demo label',
        async run(shared, _ui, flow) {
          const chosen = flow.fileInput?.files?.[0];
          const image = chosen || await makeDemoLabel();
          const data = new FormData();
          data.append('image', image, chosen?.name || 'demo-label.png');
          const scan = await upload('/api/onboarding/scan', data);
          shared.scan = scan;
          if (scan?.status === 'FAILED') {
            throw new Error(`${scan.user_message || 'The scan failed.'} (Tip: set TACTIDOSE_LABEL_EXTRACTOR=fake for the offline demo.)`);
          }
          if (scan?.status !== 'PENDING_REVIEW') throw new Error(`Unexpected scan status ${scan?.status}.`);
          return `Scan #${scan.scan_id} is ${scan.status} (UNCONFIRMED).`;
        },
      },
      {
        label: 'Gemini extracts the visible medication data',
        async run(shared) {
          const ex = shared.scan?.extracted;
          if (!ex || !ex.medication_name) throw new Error('No medication name was extracted.');
          return `${ex.medication_name}${ex.strength ? ` · ${ex.strength}` : ''} (read by ${shared.scan.model || 'unknown model'}).`;
        },
      },
      {
        label: 'UI shows the UNCONFIRMED information — your turn: check and confirm',
        human: true,
        async run(shared, ui) {
          shared.reviewed = await reviewScan(ui, shared.scan);
          return 'Reviewed and confirmed by the operator.';
        },
      },
      {
        label: 'Human confirms → the medication is saved',
        async run(shared) {
          const med = await post(`/api/onboarding/scans/${shared.scan.scan_id}/confirm`, {
            ...shared.reviewed,
            confirmed: true,
            confirmed_by: 'demo operator',
          });
          shared.medication = med;
          return `Saved medication #${med?.medication_id}: ${med?.name} (${med?.source || 'label_scan'}).`;
        },
      },
      {
        label: 'User assigns a compartment — your turn',
        human: true,
        async run(shared, ui) {
          const compartments = await get('/api/compartments');
          return chooseCompartment(ui, Array.isArray(compartments) ? compartments : [], shared.medication);
        },
      },
    ],
  };

  return [A, B, C];
}

// ------------------------------------------------------------------ runner

export function createFlows(container, env) {
  const shared = {};
  const flows = flowDefinitions(env);

  function setStep(flow, index, status, detail = null) {
    const step = flow.items[index];
    const [iconName, word] = STATUS[status];
    step.li.dataset.status = status;
    step.icon.replaceChildren(icon(iconName));
    step.state.textContent = word;
    if (detail !== null) step.detail.textContent = detail;
    if (status !== 'waiting') step.ui.replaceChildren();
  }

  function reset(flow) {
    flow.items.forEach((_, i) => setStep(flow, i, 'pending', ''));
    flow.summary.textContent = '';
  }

  async function run(flow) {
    if (flow.running) return;
    flow.running = true;
    flow.runBtn.disabled = true;
    flow.resetBtn.disabled = true;
    reset(flow);
    let failedAt = -1;
    for (const [i, step] of flow.steps.entries()) {
      setStep(flow, i, step.human ? 'waiting' : 'running');
      try {
        const detail = await step.run(shared, flow.items[i].ui, flow);
        setStep(flow, i, 'done', detail || '');
      } catch (err) {
        setStep(flow, i, 'failed', errorText(err));
        failedAt = i;
        break;
      }
    }
    if (failedAt >= 0) {
      for (let j = failedAt + 1; j < flow.steps.length; j += 1) setStep(flow, j, 'skipped', '');
      flow.summary.textContent = `${flow.title}: stopped at step ${failedAt + 1}.`;
      env.notify(`${flow.title} stopped at step ${failedAt + 1}: ${flow.items[failedAt].detail.textContent}`, 'error');
    } else {
      flow.summary.textContent = `${flow.title}: all ${flow.steps.length} steps passed.`;
      env.notify(`${flow.title} passed.`, 'success');
    }
    flow.running = false;
    flow.runBtn.disabled = false;
    flow.resetBtn.disabled = false;
  }

  for (const flow of flows) {
    const titleId = uid(`flow-${flow.id}`);
    flow.items = flow.steps.map((step, i) => {
      const iconEl = h('span', { class: 'step-icon' });
      const state = h('span', { class: 'step-state' });
      const detail = h('div', { class: 'step-detail' });
      const ui = h('div', { class: 'step-ui' });
      const li = h('li', { class: 'step', 'data-status': 'pending' }, iconEl,
        h('div', { class: 'step-main' },
          h('span', { class: 'step-label' }, `${i + 1}. ${step.label}`), ' ', state, detail, ui));
      return { li, icon: iconEl, state, detail, ui };
    });
    flow.runBtn = h('button', { type: 'button', class: 'btn btn-primary', on: { click: () => run(flow) } }, icon('play'), `Run flow ${flow.id}`);
    flow.resetBtn = h('button', { type: 'button', class: 'btn', on: { click: () => reset(flow) } }, 'Reset checklist');
    flow.summary = h('p', { class: 'muted', role: 'status' });
    let uploadField = null;
    if (flow.upload) {
      const fileId = uid('flow-file');
      flow.fileInput = h('input', { type: 'file', id: fileId, accept: 'image/jpeg,image/png,image/webp' });
      uploadField = h('div', { class: 'field' },
        h('label', { for: fileId }, 'Label photo (optional — a demo label is generated if empty)'), flow.fileInput);
    }
    container.append(h('article', { class: 'flow', 'aria-labelledby': titleId },
      h('h3', { id: titleId }, flow.title),
      h('p', { class: 'muted' }, flow.intro),
      uploadField,
      h('ol', { class: 'steps' }, flow.items.map((item) => item.li)),
      h('div', { class: 'btn-row' }, flow.runBtn, flow.resetBtn),
      flow.summary));
    reset(flow);
  }

  return {
    resetAll() {
      for (const key of Object.keys(shared)) delete shared[key];
      for (const flow of flows) if (!flow.running) reset(flow);
    },
  };
}
