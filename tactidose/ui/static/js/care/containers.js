/**
 * Care portal "Containers" tab (doctor/family only): per container, assign a medication
 * (PUT /api/patients/{pid}/containers/{slot} {medication_id}), refill
 * (POST …/refill {set} | {add}) and set the capacity / low-stock threshold (PUT).
 */

import { get, post, put } from '../api.js';
import { byId, confirmDialog, emptyState, errorState, errorText, h, replaceChildren, setLoading, uid } from '../dom.js';
import { icon } from '../icons.js';
import { plural } from '../format.js';
import { containerView } from '../status.js';
import { lazyPanel } from './panel.js';

function wholeNumber(text) {
  const t = String(text ?? '').trim();
  return /^\d{1,4}$/.test(t) ? Number(t) : null;
}

/** Refill body: mode 'set' | 'add'. Returns {ok, body} or {ok: false, error}. */
export function refillBody(mode, text, capacity = null, current = 0) {
  const n = wholeNumber(text);
  if (n === null) return { ok: false, error: 'Enter a whole number of pills, for example 20.' };
  if (mode === 'add') {
    if (n < 1) return { ok: false, error: 'Add at least 1 pill.' };
    if (capacity !== null && current + n > capacity) {
      return { ok: false, error: `That is more than fits: the container holds ${capacity} and has ${current}.` };
    }
    return { ok: true, body: { add: n } };
  }
  if (capacity !== null && n > capacity) return { ok: false, error: `The container holds at most ${capacity} pills.` };
  return { ok: true, body: { set: n } };
}

/** Capacity / low-stock body for PUT. */
export function containerSettingsBody(capacityText, thresholdText) {
  const capacity = wholeNumber(capacityText);
  const threshold = wholeNumber(thresholdText);
  if (capacity === null || capacity < 1 || capacity > 500) return { ok: false, error: 'Capacity must be a whole number from 1 to 500.', field: 'capacity' };
  if (threshold === null || threshold > 100) return { ok: false, error: 'The low-stock level must be a whole number from 0 to 100.', field: 'threshold' };
  if (threshold >= capacity) return { ok: false, error: 'The low-stock level must be smaller than the capacity.', field: 'threshold' };
  return { ok: true, body: { capacity, low_stock_threshold: threshold } };
}

/** ctx: {pid, medications(), invalidateMedications(), notify(message, kind), onChanged()} */
export function createContainersTab(ctx) {
  const root = byId('containers-list');
  let containers = [];
  let meds = [];
  let seq = 0;

  async function load() {
    if (!ctx.pid) return;
    const token = ++seq;
    setLoading(root, true);
    try {
      const [items, medications] = await Promise.all([get(`/api/patients/${ctx.pid}/containers`), ctx.medications()]);
      if (token !== seq) return;
      containers = (Array.isArray(items) ? items : []).slice().sort((a, b) => a.slot - b.slot);
      meds = (Array.isArray(medications) ? medications : []).filter((m) => m.active !== false);
      render();
    } catch (err) {
      if (token !== seq) return;
      replaceChildren(root, errorState(err, load, icon('warning')));
    } finally {
      if (token === seq) setLoading(root, false);
    }
  }

  function render() {
    if (!containers.length) {
      replaceChildren(root, emptyState('This patient has no device containers yet.'));
      return;
    }
    replaceChildren(root, containers.map(card));
  }

  function card(c) {
    const v = containerView(c);
    const titleId = uid('cont-title');
    const medId = uid('cont-med');
    const countId = uid('cont-count');
    const capId = uid('cont-cap');
    const lowId = uid('cont-low');
    const status = h('p', { class: 'field-hint', role: 'status' });

    const medSelect = h('select', { id: medId },
      h('option', { value: '' }, 'No medication (container not used)'),
      meds.map((m) => h('option', { value: String(m.medication_id), selected: m.medication_id === c.medication_id },
        `${m.name}${m.strength ? ` · ${m.strength}` : ''}`)));
    const assignForm = h('form', { class: 'mini-form', novalidate: true },
      h('div', { class: 'field' }, h('label', { for: medId }, 'Medication in this container'), medSelect),
      h('button', { type: 'submit', class: 'btn btn-primary btn-small' }, 'Save medication'));
    assignForm.addEventListener('submit', (e) => {
      e.preventDefault();
      assign(c, medSelect.value ? Number(medSelect.value) : null, status);
    });

    const countInput = h('input', { id: countId, type: 'number', min: '0', max: String(c.capacity || 500), step: '1', inputmode: 'numeric', value: String(c.pill_count ?? 0) });
    const refillForm = h('form', { class: 'mini-form', novalidate: true },
      h('div', { class: 'field' }, h('label', { for: countId }, 'Pills'), countInput,
        h('p', { class: 'field-hint' }, 'Set the exact count after counting, or add the pills you put in.')),
      h('div', { class: 'btn-row' },
        h('button', { type: 'submit', class: 'btn btn-small', value: 'set' }, 'Set the count to this'),
        h('button', { type: 'button', class: 'btn btn-small', on: { click: () => refill(c, 'add', countInput, status) } }, icon('plus'), 'Add this many')));
    refillForm.addEventListener('submit', (e) => {
      e.preventDefault();
      refill(c, 'set', countInput, status);
    });

    const capInput = h('input', { id: capId, type: 'number', min: '1', max: '500', step: '1', inputmode: 'numeric', value: String(c.capacity ?? '') });
    const lowInput = h('input', { id: lowId, type: 'number', min: '0', max: '100', step: '1', inputmode: 'numeric', value: String(c.low_stock_threshold ?? '') });
    const settingsForm = h('form', { class: 'mini-form', novalidate: true },
      h('div', { class: 'field-pair' },
        h('div', { class: 'field' }, h('label', { for: capId }, 'Capacity (pills)'), capInput),
        h('div', { class: 'field' }, h('label', { for: lowId }, 'Low-stock alert at'), lowInput)),
      h('button', { type: 'submit', class: 'btn btn-small' }, 'Save limits'));
    settingsForm.addEventListener('submit', (e) => {
      e.preventDefault();
      saveLimits(c, capInput.value, lowInput.value, status);
    });

    const facts = h('dl', { class: 'facts' },
      h('dt', {}, 'Medication'), h('dd', {}, v.medName, v.strength ? ` · ${v.strength}` : ''),
      h('dt', {}, 'Pills'), h('dd', {}, v.hasMed ? `${v.count} of ${c.capacity}` : `${v.count}`,
        v.badge ? h('span', { class: `badge tone-${v.badge.tone}` }, icon(v.badge.icon), v.badge.word === 'Low' ? 'Low stock' : 'Empty') : null),
      h('dt', {}, 'Low-stock alert'), h('dd', {}, `at ${plural(c.low_stock_threshold ?? 0, 'pill')} or fewer`));

    return h('section', { class: `card container-admin${v.empty ? ' is-empty' : ''}${v.low ? ' is-low' : ''}`, 'aria-labelledby': titleId },
      h('h3', { id: titleId }, `Container ${v.number}`),
      facts, assignForm, refillForm, settingsForm, status);
  }

  async function assign(c, medicationId, status) {
    if ((c.medication_id ?? null) === medicationId) {
      status.textContent = 'Nothing changed.';
      return;
    }
    const med = meds.find((m) => m.medication_id === medicationId);
    const { ok } = await confirmDialog({
      title: medicationId ? `Put ${med?.name || 'this medication'} in container ${c.slot + 1}?` : `Stop using container ${c.slot + 1}?`,
      message: medicationId
        ? 'Make sure the container really holds this medication. Check the pill count afterwards.'
        : 'Nothing will drop from this container until a medication is assigned again.',
      confirmLabel: medicationId ? 'Yes, assign it' : 'Yes, stop using it',
      iconEl: icon('pill'),
    });
    if (!ok) return;
    try {
      await put(`/api/patients/${ctx.pid}/containers/${c.slot}`, { medication_id: medicationId });
      ctx.notify(`Container ${c.slot + 1} updated.`, 'success');
      ctx.invalidateMedications();
      ctx.onChanged();
      load();
    } catch (err) {
      status.textContent = errorText(err);
    }
  }

  async function refill(c, mode, input, status) {
    const check = refillBody(mode, input.value, c.capacity ?? null, Number(c.pill_count) || 0);
    if (!check.ok) {
      status.textContent = check.error;
      input.focus();
      return;
    }
    try {
      const updated = await post(`/api/patients/${ctx.pid}/containers/${c.slot}/refill`, check.body);
      const count = updated?.pill_count ?? (mode === 'set' ? check.body.set : (Number(c.pill_count) || 0) + check.body.add);
      ctx.notify(`Container ${c.slot + 1} now has ${plural(count, 'pill')}.`, 'success');
      ctx.onChanged();
      load();
    } catch (err) {
      status.textContent = errorText(err);
    }
  }

  async function saveLimits(c, capText, lowText, status) {
    const check = containerSettingsBody(capText, lowText);
    if (!check.ok) {
      status.textContent = check.error;
      return;
    }
    try {
      await put(`/api/patients/${ctx.pid}/containers/${c.slot}`, check.body);
      ctx.notify(`Container ${c.slot + 1} limits saved.`, 'success');
      ctx.onChanged();
      load();
    } catch (err) {
      status.textContent = errorText(err);
    }
  }

  const panel = lazyPanel(load);
  return {
    show: panel.show,
    hide: panel.hide,
    markStale: panel.markStale,
    reset() {
      containers = [];
      root.replaceChildren();
      panel.reset();
    },
  };
}
