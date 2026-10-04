/**
 * Patient portal "Schedule" view (read-only): the doses of one day
 * (GET /api/patients/{pid}/doses?date=) and the regular times (GET …/schedules).
 * Only doctor/family accounts can change the schedule (care portal).
 */

import { get } from '../api.js';
import { byId, emptyState, errorState, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { dayDiff, describeRepeat, formatLongDate, shiftDateKey, time24To12 } from '../format.js';
import { renderDoseList } from '../doses.js';

export function dayTitle(key, todayKey) {
  const diff = todayKey ? dayDiff(todayKey, key) : null;
  const word = { 0: 'Today', 1: 'Tomorrow', [-1]: 'Yesterday' }[diff];
  return word ? `${word} — ${formatLongDate(key)}` : formatLongDate(key);
}

/** ctx: {pid, getOffset(), getToday(), containerFor(medicationId)} */
export function createSchedule(ctx) {
  const doseList = byId('sched-doses');
  const schedList = byId('sched-list');
  const title = byId('sched-day-title');
  let day = null;
  let seq = 0;

  async function load() {
    const today = ctx.getToday();
    if (!day) day = today;
    if (!day) return;
    const token = ++seq;
    title.textContent = dayTitle(day, today);
    setLoading(doseList, true);
    try {
      const [doses, schedules] = await Promise.all([
        get(`/api/patients/${ctx.pid}/doses?date=${day}`),
        get(`/api/patients/${ctx.pid}/schedules`),
      ]);
      if (token !== seq) return;
      renderDoseList(doseList, Array.isArray(doses) ? doses : [], { audience: 'patient', offsetMin: ctx.getOffset() });
      renderSchedules(Array.isArray(schedules) ? schedules : []);
    } catch (err) {
      if (token !== seq) return;
      replaceChildren(doseList, h('li', {}, errorState(err, load, icon('warning'))));
    } finally {
      if (token === seq) setLoading(doseList, false);
    }
  }

  function renderSchedules(schedules) {
    const active = schedules.filter((s) => s.active !== false)
      .sort((a, b) => String(a.time_of_day).localeCompare(String(b.time_of_day)));
    if (!active.length) {
      replaceChildren(schedList, h('li', {}, emptyState('No regular times are set. Your doctor or family can add them.')));
      return;
    }
    replaceChildren(schedList, active.map((s) => {
      const number = ctx.containerFor(s.medication_id);
      return h('li', { class: 'item' },
        h('div', { class: 'item-head' },
          h('span', { class: 'dose-time' }, time24To12(s.time_of_day)),
          h('p', { class: 'item-title' }, s.medication_name || 'Medication'),
          h('span', { class: 'item-sub' }, number ? `Container ${number}` : 'No container assigned')),
        h('p', { class: 'item-body' }, describeRepeat(s)));
    }));
  }

  function move(days) {
    day = shiftDateKey(day || ctx.getToday(), days);
    load();
  }

  byId('sched-prev').addEventListener('click', () => move(-1));
  byId('sched-next').addEventListener('click', () => move(1));
  byId('sched-today').addEventListener('click', () => {
    day = ctx.getToday();
    load();
  });

  return { load };
}
