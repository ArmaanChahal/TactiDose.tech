/**
 * Caregiver "Compartments" tab: carousel diagram (slot at the gate from
 * device.state), one medication per compartment (PUT /api/compartments/{slot}),
 * and loading mode: "Present for loading" turns the compartment to the gate and
 * opens it; "Done loading" closes the gate and records the time.
 */

import { get, post, put } from '../api.js';
import { byId, confirmDialog, debounce, errorState, errorText, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { formatDateTimeDevice } from '../format.js';
import { CarouselView } from '../carousel.js';
import { describeCommand } from '../hwview.js';

export function createCompartments(ctx) {
  const list = byId('comp-list');
  const caption = byId('comp-caption');
  const result = byId('comp-result');
  const carousel = new CarouselView(byId('comp-carousel'), { numSlots: 6, label: 'Carousel diagram' });
  let comps = [];
  let meds = [];
  let device = null;
  let presented = null;
  let rows = new Map();
  let visible = false;
  let stale = true;
  let requestSeq = 0;

  byId('comp-stop').addEventListener('click', async () => {
    try {
      const resp = await post('/api/hardware/stop', {});
      if (resp?.device) device = resp.device;
      presented = null;
      result.textContent = `STOP sent. ${describeCommand(resp)}`;
      renderDevice();
    } catch (err) {
      result.textContent = '';
      ctx.notify(errorText(err), 'error');
    }
  });

  async function load() {
    stale = false;
    const token = ++requestSeq;
    setLoading(list, true);
    try {
      const [compartments, medications, snapshot] = await Promise.all([
        get('/api/compartments'),
        ctx.loadMedications({ force: true }),
        get('/api/hardware').catch(() => null),
      ]);
      if (token !== requestSeq) return;
      comps = Array.isArray(compartments) ? compartments : [];
      meds = Array.isArray(medications) ? medications : [];
      if (snapshot) device = snapshot;
      render();
    } catch (err) {
      if (token !== requestSeq) return;
      replaceChildren(list, errorState(err, load, icon('warning')));
    } finally {
      if (token === requestSeq) setLoading(list, false);
    }
  }

  const loadSoon = debounce(load, 300);

  function renderDevice() {
    carousel.setNumSlots(comps.length || device?.num_slots_reported || 6);
    carousel.update({
      slot: device?.slot ?? null,
      gate: device?.gate || 'UNKNOWN',
      targetSlot: device?.target_slot ?? null,
      moving: ['MOVING', 'HOMING', 'AT_TARGET'].includes(device?.state),
      assignedSlots: comps.filter((c) => c.medication_id).map((c) => c.slot),
    });
    const prefix = !device ? 'Device status unknown. ' : device.connected ? '' : 'Device not connected. ';
    caption.textContent = `${prefix}${carousel.describe()}`;
    for (const [slot, row] of rows) {
      const atGate = device?.slot === slot;
      row.el.classList.toggle('is-at-gate', atGate);
      row.gateBadge.hidden = !atGate;
      row.done.classList.toggle('btn-primary', presented === slot);
    }
  }

  function medOptions(comp) {
    const options = [h('option', { value: '' }, '— Empty —')];
    const known = new Set();
    for (const med of meds) {
      known.add(med.medication_id);
      options.push(h('option', { value: String(med.medication_id) }, med.strength ? `${med.name} · ${med.strength}` : med.name));
    }
    if (comp.medication_id && !known.has(comp.medication_id)) {
      options.push(h('option', { value: String(comp.medication_id) }, comp.medication_name || `Medication ${comp.medication_id}`));
    }
    return options;
  }

  function render() {
    rows = new Map();
    if (!comps.length) {
      replaceChildren(list, h('p', { class: 'state-msg' }, 'No compartments are configured for this device yet.'));
      renderDevice();
      return;
    }
    const body = comps.map((comp) => {
      const number = comp.compartment_number || comp.slot + 1;
      const selectId = `comp-med-${comp.slot}`;
      const select = h('select', { id: selectId }, medOptions(comp));
      select.value = comp.medication_id ? String(comp.medication_id) : '';
      const save = h('button', {
        type: 'button', class: 'btn btn-small', disabled: true, 'aria-label': `Save compartment ${number}`,
        on: { click: () => assign(comp, select) },
      }, 'Save');
      select.addEventListener('change', () => {
        save.disabled = select.value === (comp.medication_id ? String(comp.medication_id) : '');
      });
      const present = h('button', {
        type: 'button', class: 'btn btn-small', 'aria-label': `Present compartment ${number} for loading`,
        on: { click: () => presentForLoading(comp) },
      }, 'Present for loading');
      const done = h('button', {
        type: 'button', class: 'btn btn-small', 'aria-label': `Done loading compartment ${number}`,
        on: { click: () => doneLoading(comp) },
      }, 'Done loading');
      const gateBadge = h('span', { class: 'badge tone-caution', hidden: true }, icon('gate'), 'At the gate');
      const tr = h('tr', { class: 'comp-row' },
        h('th', { scope: 'row' }, h('div', {}, `Compartment ${number}`), gateBadge),
        h('td', {}, h('label', { for: selectId, class: 'visually-hidden' }, `Medication in compartment ${number}`),
          h('div', { class: 'btn-row' }, select, save)),
        h('td', {}, comp.loaded_at ? formatDateTimeDevice(comp.loaded_at, ctx.offsetMin) : 'Not recorded'),
        h('td', {}, h('div', { class: 'btn-row' }, present, done)));
      rows.set(comp.slot, { el: tr, gateBadge, done });
      return tr;
    });
    replaceChildren(list, h('div', { class: 'table-wrap' },
      h('table', { class: 'comp-table' },
        h('caption', { class: 'visually-hidden' }, 'Compartments and their medications'),
        h('thead', {}, h('tr', {}, ['Compartment', 'Medication', 'Last loaded', 'Loading'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, body))));
    renderDevice();
  }

  async function assign(comp, select) {
    const medicationId = select.value ? Number(select.value) : null;
    try {
      const updated = await put(`/api/compartments/${comp.slot}`, { medication_id: medicationId });
      if (Array.isArray(updated)) comps = updated;
      ctx.invalidateMedications();
      const number = comp.compartment_number || comp.slot + 1;
      ctx.notify(medicationId ? `Compartment ${number} updated.` : `Compartment ${number} is now empty.`, 'success');
      render();
    } catch (err) {
      ctx.notify(errorText(err), 'error');
    }
  }

  async function presentForLoading(comp) {
    const number = comp.compartment_number || comp.slot + 1;
    const { ok } = await confirmDialog({
      title: `Present compartment ${number} for loading?`,
      message: 'The carousel will turn this compartment to the gate and open it. Keep your hands clear until it stops.',
      confirmLabel: 'Turn and open',
      iconEl: icon('hand'),
    });
    if (!ok) return;
    result.textContent = `Moving compartment ${number} to the gate…`;
    try {
      const resp = await post(`/api/compartments/${comp.slot}/present`, {});
      if (resp?.device) device = resp.device;
      if (resp?.ok) {
        presented = comp.slot;
        result.textContent = `Compartment ${number} is open for loading. Add the demo items, then press Done loading.`;
      } else {
        result.textContent = `Could not present compartment ${number}: ${describeCommand(resp)}`;
        ctx.notify(`Could not present compartment ${number}.`, 'error');
      }
      if (resp?.result?.gate_may_be_open) ctx.notify('The gate may be open — check the device before touching it.', 'warning');
      renderDevice();
    } catch (err) {
      result.textContent = '';
      ctx.notify(errorText(err), 'error');
    }
  }

  async function doneLoading(comp) {
    const number = comp.compartment_number || comp.slot + 1;
    try {
      const resp = await post(`/api/compartments/${comp.slot}/loaded`, {});
      if (resp?.device) device = resp.device;
      presented = null;
      result.textContent = resp?.ok === false
        ? `Loading recorded, but closing the gate reported a problem: ${describeCommand(resp)}`
        : `Compartment ${number} marked as loaded. Gate closed.`;
      ctx.notify(`Compartment ${number} marked as loaded.`, resp?.ok === false ? 'warning' : 'success');
      load();
    } catch (err) {
      ctx.notify(errorText(err), 'error');
    }
  }

  return {
    show() {
      visible = true;
      if (stale || !list.firstChild) load();
      else renderDevice();
    },
    hide() {
      visible = false;
    },
    markStale() {
      stale = true;
      if (visible) loadSoon();
    },
    onDevice(snapshot) {
      device = snapshot;
      if (visible && comps.length) renderDevice();
    },
  };
}
