/**
 * Caregiver "Medications" tab: confirmed medication records (GET/POST/PATCH/DELETE
 * /api/medications). Adding or editing requires ticking "I confirm this information
 * is correct"; archiving clears the compartment and deactivates schedules.
 */

import { del, get, patch, post } from '../api.js';
import { byId, confirmDialog, debounce, emptyState, errorState, errorText, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { DASH, describeRepeat, formatDateTimeDevice, sourceName, time24To12 } from '../format.js';
import { bindMedicationForm, fillMedicationForm } from '../medform.js';

export function createMedications(ctx) {
  const list = byId('meds-list');
  const form = byId('med-form');
  const card = byId('med-form-card');
  const title = byId('med-form-title');
  const submit = byId('med-submit');
  const cancelEdit = byId('med-cancel-edit');
  const showArchived = byId('meds-show-archived');
  const confirmedBy = byId('med-confirmed-by');
  let editing = null;
  let visible = false;
  let stale = true;
  let requestSeq = 0;

  function prefillName() {
    if (!confirmedBy.value) confirmedBy.value = ctx.caregiverName() || '';
  }

  function endEdit() {
    editing = null;
    title.textContent = 'Add a medication';
    submit.textContent = 'Save medication';
    cancelEdit.hidden = true;
    card.classList.remove('is-editing');
  }

  function startEdit(med) {
    editing = med;
    fillMedicationForm(form, {
      name: med.name,
      strength: med.strength,
      instructions_text: med.instructions_text,
      warnings: med.warnings || [],
      confirmed_by: ctx.caregiverName() || med.confirmed_by || '',
    });
    title.textContent = `Edit ${med.name}`;
    submit.textContent = 'Save changes';
    cancelEdit.hidden = false;
    card.classList.add('is-editing');
    card.scrollIntoView({ block: 'start' });
    form.elements.namedItem('name').focus({ preventScroll: true });
  }

  cancelEdit.addEventListener('click', () => {
    form.reset();
    endEdit();
    prefillName();
  });

  bindMedicationForm(form, {
    errorEl: byId('med-form-error'),
    keepEmpty: () => editing !== null,
    async onSubmit(body) {
      if (editing) {
        const med = await patch(`/api/medications/${editing.medication_id}`, body);
        ctx.notify(`Saved changes to ${med?.name || body.name}.`, 'success');
      } else {
        const med = await post('/api/medications', body);
        ctx.notify(`Added ${med?.name || body.name}. Next: assign it to a compartment and add a schedule.`, 'success');
      }
      form.reset();
      endEdit();
      prefillName();
      ctx.invalidateMedications();
      load();
    },
  });

  showArchived.addEventListener('change', () => load());

  async function load() {
    stale = false;
    const token = ++requestSeq;
    setLoading(list, true);
    try {
      const meds = await get(`/api/medications?include_inactive=${showArchived.checked ? 'true' : 'false'}`);
      if (token !== requestSeq) return;
      render(Array.isArray(meds) ? meds : []);
    } catch (err) {
      if (token !== requestSeq) return;
      replaceChildren(list, errorState(err, load, icon('warning')));
    } finally {
      if (token === requestSeq) setLoading(list, false);
    }
  }

  const loadSoon = debounce(load, 300);

  function scheduleText(med) {
    const active = (med.schedules || []).filter((s) => s.active !== false);
    if (!active.length) return 'No schedule yet';
    return active
      .slice()
      .sort((a, b) => String(a.time_of_day).localeCompare(String(b.time_of_day)))
      .map((s) => `${time24To12(s.time_of_day)} ${describeRepeat(s).toLowerCase()}`)
      .join('; ');
  }

  function render(meds) {
    if (!meds.length) {
      replaceChildren(list, emptyState('No medications yet. Add one below, or scan a label.'));
      return;
    }
    const items = meds.map((med) => {
      const inactive = med.active === false;
      const warnings = Array.isArray(med.warnings) && med.warnings.length
        ? h('ul', {}, med.warnings.map((w) => h('li', {}, w)))
        : DASH;
      const confirmed = [sourceName(med.source)];
      if (med.confirmed_by) confirmed.push(`confirmed by ${med.confirmed_by}`);
      if (med.confirmed_at) confirmed.push(formatDateTimeDevice(med.confirmed_at, ctx.offsetMin));
      return h('li', { class: inactive ? 'item is-inactive' : 'item' },
        h('div', { class: 'item-head' },
          h('h3', { class: 'item-title' }, med.name),
          med.strength ? h('span', { class: 'item-sub' }, med.strength) : null,
          inactive ? h('span', { class: 'badge tone-neutral' }, icon('archive'), 'Archived') : null,
          med.confirmed_by_user ? h('span', { class: 'badge tone-good' }, icon('check'), 'Confirmed') : null),
        h('dl', { class: 'facts' },
          h('dt', {}, 'Compartment'),
          h('dd', {}, med.compartment_number ? `Compartment ${med.compartment_number}` : 'Not assigned'),
          h('dt', {}, 'Schedule'),
          h('dd', {}, scheduleText(med)),
          h('dt', {}, 'Instructions'),
          h('dd', {}, med.instructions_text || DASH),
          h('dt', {}, 'Warnings'),
          h('dd', {}, warnings),
          h('dt', {}, 'Record'),
          h('dd', {}, confirmed.join(' · '))),
        inactive ? null : h('div', { class: 'btn-row' },
          h('button', { type: 'button', class: 'btn btn-small', 'aria-label': `Edit ${med.name}`, on: { click: () => startEdit(med) } }, icon('edit'), 'Edit'),
          h('button', { type: 'button', class: 'btn btn-small', 'aria-label': `Archive ${med.name}`, on: { click: () => archive(med) } }, icon('archive'), 'Archive')));
    });
    replaceChildren(list, h('ul', { class: 'item-list' }, items));
  }

  async function archive(med) {
    const { ok } = await confirmDialog({
      title: `Archive ${med.name}?`,
      message: 'It will no longer be dispensed: its compartment is cleared and its schedules are deactivated. The history is kept.',
      confirmLabel: 'Archive',
      danger: true,
      iconEl: icon('archive'),
    });
    if (!ok) return;
    try {
      await del(`/api/medications/${med.medication_id}`);
      ctx.notify(`${med.name} archived.`, 'success');
      if (editing && editing.medication_id === med.medication_id) {
        form.reset();
        endEdit();
      }
      ctx.invalidateMedications();
    } catch (err) {
      ctx.notify(errorText(err), 'error');
    }
    load();
  }

  prefillName();

  return {
    show() {
      visible = true;
      prefillName();
      if (stale || !list.firstChild) load();
    },
    hide() {
      visible = false;
    },
    markStale() {
      stale = true;
      if (visible) loadSoon();
    },
    /** Used by the label-scan tab when a scan fails: jump to manual entry. */
    openAddForm() {
      form.reset();
      endEdit();
      prefillName();
      card.scrollIntoView({ block: 'start' });
      form.elements.namedItem('name').focus({ preventScroll: true });
    },
  };
}
