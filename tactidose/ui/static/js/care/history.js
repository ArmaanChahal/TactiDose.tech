/**
 * Care portal "History" tab: every drop request (shared history view with "It dropped"
 * / "It did not drop" for uncertain drops) and the scheduled doses of a chosen day with
 * "Skip this dose" (POST /api/patients/{pid}/doses/{event_id}/skip), and the patient's saved
 * well-being check-ins (read-only).
 */

import { get, post } from '../api.js';
import { byId, confirmDialog, errorState, errorText, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { createDropHistory } from '../history.js';
import { createCheckinHistory } from '../wellbeing.js';
import { doseSummary, renderDoseList } from '../doses.js';
import { lazyPanel } from './panel.js';

/** ctx: {pid, notify(message, kind), getOffset(), getNow(), getToday(), onChanged()} */
export function createHistoryTab(ctx) {
  const drops = createDropHistory(byId('cg-history-root'), {
    getPatientId: () => ctx.pid,
    audience: 'caregiver',
    canResolve: true,
    notify: ctx.notify,
    getOffset: ctx.getOffset,
    getNow: ctx.getNow,
    onChanged: ctx.onChanged,
  });
  const checkins = createCheckinHistory(byId('cg-wellbeing-root'), {
    getPatientId: () => ctx.pid,
    audience: 'caregiver',
    getOffset: ctx.getOffset,
  });
  const dateInput = byId('doses-date');
  const doseList = byId('doses-list');
  let seq = 0;

  async function loadDoses() {
    if (!ctx.pid) return;
    if (!dateInput.value) dateInput.value = ctx.getToday() || '';
    if (!dateInput.value) return;
    const token = ++seq;
    setLoading(doseList, true);
    try {
      const doses = await get(`/api/patients/${ctx.pid}/doses?date=${dateInput.value}`);
      if (token !== seq) return;
      renderDoseList(doseList, Array.isArray(doses) ? doses : [], { audience: 'caregiver', offsetMin: ctx.getOffset(), onSkip: skip });
    } catch (err) {
      if (token !== seq) return;
      replaceChildren(doseList, h('li', {}, errorState(err, loadDoses, icon('warning'))));
    } finally {
      if (token === seq) setLoading(doseList, false);
    }
  }

  async function skip(dose) {
    const v = doseSummary(dose, { audience: 'caregiver', offsetMin: ctx.getOffset() });
    const { ok, note } = await confirmDialog({
      title: 'Skip this dose?',
      message: `${v.title} at ${v.time}.\nIt will not drop automatically.`,
      confirmLabel: 'Skip this dose',
      noteLabel: 'Reason (optional)',
      iconEl: icon('slash'),
    });
    if (!ok) return;
    try {
      await post(`/api/patients/${ctx.pid}/doses/${dose.event_id}/skip`, note ? { note } : {});
      ctx.notify(`Skipped: ${v.title} at ${v.time}.`, 'success');
      ctx.onChanged();
    } catch (err) {
      ctx.notify(errorText(err), 'error');
    }
    loadDoses();
  }

  dateInput.addEventListener('change', loadDoses);
  byId('doses-today').addEventListener('click', () => {
    dateInput.value = ctx.getToday() || '';
    loadDoses();
  });

  const panel = lazyPanel(() => {
    drops.load();
    checkins.load();
    loadDoses();
  });
  return {
    show: panel.show,
    hide: panel.hide,
    markStale: panel.markStale,
    reset() {
      dateInput.value = '';
      doseList.replaceChildren();
      panel.reset();
    },
  };
}
