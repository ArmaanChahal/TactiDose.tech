/**
 * Care portal "Schedule" tab (doctor/family only): list times per medication, add
 * (POST /api/patients/{pid}/schedules), edit (PATCH …/schedules/{sid}), turn a time
 * off or on (PATCH {active}) and delete (DELETE). Times are device-local "HH:MM".
 */

import { del, get, patch, post } from '../api.js';
import { byId, confirmDialog, emptyState, errorState, errorText, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { WEEKDAYS, describeRepeat, normalizeTime, time24To12 } from '../format.js';
import { lazyPanel } from './panel.js';

/** Validate the schedule form values. Returns {ok, body} or {ok: false, error, field}. */
export function scheduleBody({ medicationId, time, frequency, days, active, editing }) {
  const timeOfDay = normalizeTime(time);
  if (!editing && !medicationId) return { ok: false, error: 'Choose a medication.', field: 'medication_id' };
  if (!timeOfDay) return { ok: false, error: 'Enter a time, for example 08:00.', field: 'time_of_day' };
  const weekly = frequency === 'WEEKLY';
  const chosen = WEEKDAYS.filter((d) => (days || []).includes(d));
  if (weekly && !chosen.length) return { ok: false, error: 'Choose at least one day, or pick "Every day".', field: 'days' };
  const body = { time_of_day: timeOfDay, frequency: weekly ? 'WEEKLY' : 'DAILY' };
  if (weekly) body.days_of_week = chosen;
  else if (editing) body.days_of_week = [...WEEKDAYS];
  if (editing) body.active = Boolean(active);
  else body.medication_id = Number(medicationId);
  return { ok: true, body };
}

/** Group schedules by medication (active first, then by time). */
export function groupSchedules(schedules, { showInactive = false } = {}) {
  const groups = new Map();
  for (const s of schedules || []) {
    if (!showInactive && s.active === false) continue;
    const key = s.medication_id;
    if (!groups.has(key)) groups.set(key, { medicationId: key, name: s.medication_name || `Medication ${key}`, items: [] });
    groups.get(key).items.push(s);
  }
  const out = [...groups.values()];
  for (const g of out) g.items.sort((a, b) => String(a.time_of_day).localeCompare(String(b.time_of_day)));
  out.sort((a, b) => a.name.localeCompare(b.name));
  return out;
}

/** ctx: {pid, medications(), notify(message, kind), onChanged()} */
export function createScheduleTab(ctx) {
  const list = byId('sched-list');
  const form = byId('sched-form');
  const title = byId('sched-form-title');
  const medSelect = byId('sched-med');
  const timeInput = byId('sched-time');
  const daysBox = byId('sched-days');
  const activeRow = byId('sched-active-row');
  const activeBox = byId('sched-active');
  const errorEl = byId('sched-form-error');
  const submit = byId('sched-submit');
  const cancelEdit = byId('sched-cancel-edit');
  const showInactive = byId('sched-show-inactive');
  let editing = null;
  let schedules = [];
  let meds = [];
  let seq = 0;

  const frequency = () => (form.elements.namedItem('frequency').value === 'WEEKLY' ? 'WEEKLY' : 'DAILY');
  const chosenDays = () => Array.from(form.querySelectorAll('input[name="days"]:checked'), (el) => el.value);

  function syncDays() {
    daysBox.disabled = frequency() !== 'WEEKLY';
  }

  function showError(message) {
    errorEl.textContent = message || '';
    errorEl.hidden = !message;
  }

  for (const radio of form.querySelectorAll('input[name="frequency"]')) radio.addEventListener('change', syncDays);
  form.addEventListener('input', () => showError(''));
  showInactive.addEventListener('change', render);

  function fillMedSelect() {
    const keep = medSelect.value;
    const active = meds.filter((m) => m.active !== false);
    const options = [h('option', { value: '' }, active.length ? 'Choose a medication…' : 'No medications yet — add one first')];
    for (const med of active) {
      const where = med.compartment_number ? ` (container ${med.compartment_number})` : ' (no container)';
      options.push(h('option', { value: String(med.medication_id) }, `${med.name}${med.strength ? ` · ${med.strength}` : ''}${where}`));
    }
    medSelect.replaceChildren(...options);
    if (keep && active.some((m) => String(m.medication_id) === keep)) medSelect.value = keep;
  }

  function endEdit() {
    editing = null;
    form.reset();
    syncDays();
    title.textContent = 'Add a time';
    submit.textContent = 'Save time';
    cancelEdit.hidden = true;
    activeRow.hidden = true;
    medSelect.disabled = false;
    byId('sched-form-card').classList.remove('is-editing');
  }

  function startEdit(schedule) {
    editing = schedule;
    showError('');
    title.textContent = `Change ${time24To12(schedule.time_of_day)} · ${schedule.medication_name || 'medication'}`;
    submit.textContent = 'Save changes';
    cancelEdit.hidden = false;
    activeRow.hidden = false;
    activeBox.checked = schedule.active !== false;
    if (!Array.from(medSelect.options).some((o) => o.value === String(schedule.medication_id))) {
      medSelect.append(h('option', { value: String(schedule.medication_id) }, schedule.medication_name || `Medication ${schedule.medication_id}`));
    }
    medSelect.value = String(schedule.medication_id);
    medSelect.disabled = true;
    timeInput.value = normalizeTime(schedule.time_of_day) || '';
    const weekly = schedule.frequency === 'WEEKLY';
    form.elements.namedItem('frequency').value = weekly ? 'WEEKLY' : 'DAILY';
    for (const box of form.querySelectorAll('input[name="days"]')) {
      box.checked = weekly && (schedule.days_of_week || []).includes(box.value);
    }
    syncDays();
    const card = byId('sched-form-card');
    card.classList.add('is-editing');
    card.scrollIntoView({ block: 'start' });
    title.focus({ preventScroll: true });
  }

  cancelEdit.addEventListener('click', endEdit);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const check = scheduleBody({
      medicationId: medSelect.value,
      time: timeInput.value,
      frequency: frequency(),
      days: chosenDays(),
      active: activeBox.checked,
      editing: Boolean(editing),
    });
    if (!check.ok) {
      showError(check.error);
      const target = check.field === 'days' ? form.querySelector('input[name="days"]') : check.field === 'medication_id' ? medSelect : timeInput;
      target?.focus();
      return;
    }
    submit.disabled = true;
    try {
      if (editing) {
        await patch(`/api/patients/${ctx.pid}/schedules/${editing.schedule_id}`, check.body);
        ctx.notify('The time was changed.', 'success');
      } else {
        await post(`/api/patients/${ctx.pid}/schedules`, check.body);
        ctx.notify(`Added a time: ${time24To12(check.body.time_of_day)}.`, 'success');
      }
      endEdit();
      ctx.onChanged();
      load();
    } catch (err) {
      showError(errorText(err));
    } finally {
      submit.disabled = false;
    }
  });

  async function load() {
    if (!ctx.pid) return;
    const token = ++seq;
    setLoading(list, true);
    try {
      const [items, medications] = await Promise.all([get(`/api/patients/${ctx.pid}/schedules`), ctx.medications()]);
      if (token !== seq) return;
      schedules = Array.isArray(items) ? items : [];
      meds = Array.isArray(medications) ? medications : [];
      if (!editing) fillMedSelect();
      render();
    } catch (err) {
      if (token !== seq) return;
      replaceChildren(list, errorState(err, load, icon('warning')));
    } finally {
      if (token === seq) setLoading(list, false);
    }
  }

  function render() {
    const groups = groupSchedules(schedules, { showInactive: showInactive.checked });
    if (!groups.length) {
      const hidden = schedules.filter((s) => s.active === false).length;
      replaceChildren(list, emptyState(hidden ? `No times are on (${hidden} turned off — tick "Show turned-off times").` : 'No times yet. Add one with the form.'));
      return;
    }
    replaceChildren(list, groups.map((g) => h('section', { class: 'sched-group' },
      h('h4', {}, icon('pill'), ` ${g.name}`),
      h('ul', { class: 'item-list' }, g.items.map((s) => {
        const on = s.active !== false;
        const label = `${time24To12(s.time_of_day)} ${g.name}`;
        return h('li', { class: `item${on ? '' : ' is-off'}` },
          h('div', { class: 'item-head' },
            h('span', { class: 'dose-time' }, time24To12(s.time_of_day)),
            h('span', { class: 'item-sub' }, describeRepeat(s)),
            on ? h('span', { class: 'badge tone-good' }, icon('check'), 'On') : h('span', { class: 'badge tone-neutral' }, icon('slash'), 'Off')),
          h('div', { class: 'item-actions' },
            h('button', { type: 'button', class: 'btn btn-small', 'aria-label': `Change ${label}`, on: { click: () => startEdit(s) } }, icon('edit'), 'Change'),
            h('button', { type: 'button', class: 'btn btn-small', 'aria-label': `${on ? 'Turn off' : 'Turn on'} ${label}`, on: { click: () => setActive(s, !on) } }, on ? 'Turn off' : 'Turn on'),
            h('button', { type: 'button', class: 'btn btn-small', 'aria-label': `Delete ${label}`, on: { click: () => remove(s) } }, icon('trash'), 'Delete')));
      })))));
  }

  async function setActive(s, active) {
    try {
      await patch(`/api/patients/${ctx.pid}/schedules/${s.schedule_id}`, { active });
      ctx.notify(active ? 'The time is on again.' : 'The time is turned off. Its future doses will not drop.', 'success');
      ctx.onChanged();
    } catch (err) {
      ctx.notify(errorText(err), 'error');
    }
    load();
  }

  async function remove(s) {
    const { ok } = await confirmDialog({
      title: 'Delete this time?',
      message: `${time24To12(s.time_of_day)} · ${s.medication_name || ''}\nFuture doses from it are cancelled. Past history is kept.`,
      confirmLabel: 'Delete',
      danger: true,
      iconEl: icon('trash'),
    });
    if (!ok) return;
    try {
      await del(`/api/patients/${ctx.pid}/schedules/${s.schedule_id}`);
      ctx.notify('The time was deleted.', 'success');
      if (editing && editing.schedule_id === s.schedule_id) endEdit();
      ctx.onChanged();
    } catch (err) {
      ctx.notify(errorText(err), 'error');
    }
    load();
  }

  syncDays();
  const panel = lazyPanel(load);
  return {
    show: panel.show,
    hide: panel.hide,
    markStale: panel.markStale,
    reset() {
      endEdit();
      schedules = [];
      list.replaceChildren();
      panel.reset();
    },
  };
}
