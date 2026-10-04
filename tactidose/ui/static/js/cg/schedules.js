/**
 * Caregiver "Schedules" tab: list, create (POST /api/schedules), edit (PATCH) and
 * deactivate (DELETE, which cancels future doses). Times are device-local "HH:MM".
 */

import { del, get, patch, post } from '../api.js';
import { byId, confirmDialog, debounce, emptyState, errorState, errorText, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { WEEKDAYS, describeRepeat, normalizeTime, time24To12 } from '../format.js';

/** Validate the schedule form values. Returns {ok, body, error, field}. */
export function scheduleBody({ medicationId, time, frequency, days, active, editing }) {
  const timeOfDay = normalizeTime(time);
  if (!editing && !medicationId) return { ok: false, error: 'Choose a medication.', field: 'medication_id' };
  if (!timeOfDay) return { ok: false, error: 'Enter a time, for example 08:00.', field: 'time_of_day' };
  const weekly = frequency === 'WEEKLY';
  const chosen = WEEKDAYS.filter((d) => days.includes(d));
  if (weekly && !chosen.length) return { ok: false, error: 'Choose at least one day, or pick "Every day".', field: 'days' };
  const body = { time_of_day: timeOfDay, frequency: weekly ? 'WEEKLY' : 'DAILY' };
  if (weekly) body.days_of_week = chosen;
  else if (editing) body.days_of_week = [...WEEKDAYS];
  if (editing) body.active = Boolean(active);
  else body.medication_id = Number(medicationId);
  return { ok: true, body };
}

export function createSchedules(ctx) {
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
  let visible = false;
  let stale = true;
  let requestSeq = 0;

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
  showInactive.addEventListener('change', () => render());

  function fillMedSelect() {
    const keep = medSelect.value;
    const options = [h('option', { value: '' }, meds.length ? 'Choose a medication…' : 'No confirmed medications yet')];
    for (const med of meds) {
      options.push(h('option', { value: String(med.medication_id) }, med.strength ? `${med.name} · ${med.strength}` : med.name));
    }
    medSelect.replaceChildren(...options);
    if (keep && meds.some((m) => String(m.medication_id) === keep)) medSelect.value = keep;
  }

  function endEdit() {
    editing = null;
    form.reset();
    syncDays();
    title.textContent = 'Add a schedule';
    submit.textContent = 'Save schedule';
    cancelEdit.hidden = true;
    activeRow.hidden = true;
    medSelect.disabled = false;
    form.closest('.form-card')?.classList.remove('is-editing');
  }

  function startEdit(schedule) {
    editing = schedule;
    showError('');
    title.textContent = `Edit ${time24To12(schedule.time_of_day)} · ${schedule.medication_name || 'schedule'}`;
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
    const card = form.closest('.form-card');
    card?.classList.add('is-editing');
    card?.scrollIntoView({ block: 'start' });
    timeInput.focus({ preventScroll: true });
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
      const focusTarget = check.field === 'days' ? form.querySelector('input[name="days"]') : form.elements.namedItem(check.field);
      focusTarget?.focus();
      return;
    }
    submit.disabled = true;
    try {
      if (editing) {
        await patch(`/api/schedules/${editing.schedule_id}`, check.body);
        ctx.notify('Schedule updated.', 'success');
      } else {
        await post('/api/schedules', check.body);
        ctx.notify(`Schedule added for ${time24To12(check.body.time_of_day)}.`, 'success');
      }
      endEdit();
      load();
    } catch (err) {
      showError(errorText(err));
    } finally {
      submit.disabled = false;
    }
  });

  async function load() {
    stale = false;
    const token = ++requestSeq;
    setLoading(list, true);
    try {
      const [items, medications] = await Promise.all([get('/api/schedules'), ctx.loadMedications()]);
      if (token !== requestSeq) return;
      schedules = Array.isArray(items) ? items : [];
      meds = Array.isArray(medications) ? medications : [];
      if (!editing) fillMedSelect();
      render();
    } catch (err) {
      if (token !== requestSeq) return;
      replaceChildren(list, errorState(err, load, icon('warning')));
    } finally {
      if (token === requestSeq) setLoading(list, false);
    }
  }

  const loadSoon = debounce(load, 300);

  function render() {
    const shown = schedules
      .filter((s) => showInactive.checked || s.active !== false)
      .slice()
      .sort((a, b) => String(a.time_of_day).localeCompare(String(b.time_of_day)));
    if (!shown.length) {
      const hiddenCount = schedules.length - shown.length;
      replaceChildren(list, emptyState(hiddenCount
        ? `No active schedules (${hiddenCount} inactive hidden).`
        : 'No schedules yet. Add one below.'));
      return;
    }
    const rows = shown.map((s) => {
      const active = s.active !== false;
      const context = `${time24To12(s.time_of_day)} ${s.medication_name || ''}`.trim();
      return h('tr', {},
        h('td', { class: 'dose-time' }, time24To12(s.time_of_day)),
        h('td', { class: 'dose-med' }, s.medication_name || `Medication ${s.medication_id}`),
        h('td', {}, describeRepeat(s)),
        h('td', {}, active
          ? h('span', { class: 'badge tone-good' }, icon('check'), 'Active')
          : h('span', { class: 'badge tone-neutral' }, icon('slash'), 'Inactive')),
        h('td', {}, h('div', { class: 'dose-actions' },
          h('button', { type: 'button', class: 'btn btn-small', 'aria-label': `Edit — ${context}`, on: { click: () => startEdit(s) } }, icon('edit'), 'Edit'),
          active ? h('button', { type: 'button', class: 'btn btn-small', 'aria-label': `Deactivate — ${context}`, on: { click: () => deactivate(s) } }, 'Deactivate') : null)));
    });
    replaceChildren(list, h('div', { class: 'table-wrap' },
      h('table', {},
        h('caption', { class: 'visually-hidden' }, 'Schedules'),
        h('thead', {}, h('tr', {}, ['Time', 'Medication', 'Repeats', 'Status', 'Actions'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, rows))));
  }

  async function deactivate(schedule) {
    const { ok } = await confirmDialog({
      title: 'Deactivate this schedule?',
      message: `${time24To12(schedule.time_of_day)} · ${schedule.medication_name || ''}\n\nFuture doses from this schedule are cancelled. Past history is kept.`,
      confirmLabel: 'Deactivate',
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`/api/schedules/${schedule.schedule_id}`);
      ctx.notify('Schedule deactivated.', 'success');
      if (editing && editing.schedule_id === schedule.schedule_id) endEdit();
    } catch (err) {
      ctx.notify(errorText(err), 'error');
    }
    load();
  }

  syncDays();

  return {
    show() {
      visible = true;
      if (stale || !list.firstChild) load();
    },
    hide() {
      visible = false;
    },
    markStale() {
      stale = true;
      if (visible) loadSoon();
    },
  };
}
