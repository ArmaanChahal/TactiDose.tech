/**
 * Caregiver "Today" tab: the dose log for one day (GET /api/dose-events) with the
 * caregiver review actions. Doses with an uncertain hardware outcome are locked
 * (needs_review) until a person records whether the compartment was accessed —
 * the UI never guesses and never retries them automatically.
 */

import { get, post } from '../api.js';
import { byId, confirmDialog, debounce, emptyState, errorState, errorText, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { DASH, doseStatusInfo, formatClock, formatClockDevice, formatLongDate, shiftDateKey } from '../format.js';

const SKIPPABLE = new Set(['SCHEDULED', 'DUE', 'HARDWARE_ERROR']);

export function statusBadge(status, needsReview) {
  const info = doseStatusInfo(status, needsReview);
  const badges = [h('span', { class: `badge tone-${info.tone}` }, icon(info.icon), info.word)];
  if (info.needsReview) badges.push(h('span', { class: 'badge tone-bad' }, icon('stop'), 'Needs review'));
  return badges;
}

export function createToday(ctx) {
  const list = byId('today-list');
  const summary = byId('today-summary');
  const heading = byId('today-heading');
  const dateInput = byId('today-date');
  let selected = null; // YYYY-MM-DD, or null = the device's today
  let visible = false;
  let stale = true;
  let requestSeq = 0;

  const currentKey = () => selected || ctx.todayKey;

  function goTo(key) {
    selected = key && key !== ctx.todayKey ? key : null;
    load();
  }

  byId('today-prev').addEventListener('click', () => {
    const base = currentKey();
    if (base) goTo(shiftDateKey(base, -1));
  });
  byId('today-next').addEventListener('click', () => {
    const base = currentKey();
    if (base) goTo(shiftDateKey(base, 1));
  });
  byId('today-today').addEventListener('click', () => goTo(null));
  byId('today-refresh').addEventListener('click', () => load());
  dateInput.addEventListener('change', () => {
    if (dateInput.value) goTo(dateInput.value);
  });

  async function load() {
    stale = false;
    const token = ++requestSeq;
    const key = currentKey();
    setLoading(list, true);
    try {
      const events = key
        ? await get(`/api/dose-events?date=${encodeURIComponent(key)}`)
        : await get('/api/dose-events');
      if (token !== requestSeq) return;
      render(Array.isArray(events) ? events : [], key);
    } catch (err) {
      if (token !== requestSeq) return;
      summary.textContent = '';
      replaceChildren(list, errorState(err, load, icon('warning')));
    } finally {
      if (token === requestSeq) setLoading(list, false);
    }
  }

  const loadSoon = debounce(load, 300);

  function renderHeading(key) {
    const isToday = key && key === ctx.todayKey;
    heading.textContent = key ? `Doses for ${formatLongDate(key)}${isToday ? ' (today)' : ''}` : 'Doses today';
    if (key) dateInput.value = key;
  }

  function summaryText(events) {
    const counts = new Map();
    let review = 0;
    for (const ev of events) {
      const word = doseStatusInfo(ev.status).word.toLowerCase();
      counts.set(word, (counts.get(word) || 0) + 1);
      if (ev.needs_review) review += 1;
    }
    const parts = Array.from(counts, ([word, n]) => `${n} ${word}`);
    if (review) parts.push(`${review} need${review === 1 ? 's' : ''} review`);
    return `${events.length} dose${events.length === 1 ? '' : 's'}: ${parts.join(', ')}.`;
  }

  function details(ev) {
    const lines = [];
    if (ev.dispensed_at) lines.push(`Opened ${formatClockDevice(ev.dispensed_at, ctx.offsetMin)}${ev.dispense_source ? ` (${ev.dispense_source})` : ''}`);
    if (ev.confirmed_taken_at) lines.push(`Taken ${formatClockDevice(ev.confirmed_taken_at, ctx.offsetMin)}${ev.confirm_source ? ` (${ev.confirm_source})` : ''}`);
    if (ev.missed_at) lines.push(`Missed at ${formatClockDevice(ev.missed_at, ctx.offsetMin)}`);
    if (ev.cancelled_at) lines.push(`Skipped at ${formatClockDevice(ev.cancelled_at, ctx.offsetMin)}`);
    if (ev.attempts) lines.push(`Attempts: ${ev.attempts}`);
    if (ev.hardware_result) lines.push(`Hardware: ${ev.hardware_result}`);
    if (ev.review_note) lines.push(`Note: ${ev.review_note}`);
    return lines.length ? lines.map((line) => h('div', {}, line)) : DASH;
  }

  function actionButton(label, context, handler, cls = 'btn btn-small') {
    return h('button', { type: 'button', class: cls, 'aria-label': `${label} — ${context}`, on: { click: handler } }, label);
  }

  function actions(ev) {
    const context = `${formatClock(ev.scheduled_local)} ${ev.medication_name}`;
    const out = [];
    if (ev.needs_review || ev.status === 'HARDWARE_ERROR') {
      out.push(actionButton('It was accessed', context, () => resolve(ev, true), 'btn btn-small btn-primary'));
      out.push(actionButton('It was NOT accessed', context, () => resolve(ev, false)));
    }
    if (ev.status === 'DISPENSED') out.push(actionButton('Mark taken', context, () => markTaken(ev)));
    if (SKIPPABLE.has(ev.status)) out.push(actionButton('Skip', context, () => skip(ev)));
    return out.length ? h('div', { class: 'dose-actions' }, out) : DASH;
  }

  function render(events, key) {
    renderHeading(key);
    if (!events.length) {
      summary.textContent = '';
      replaceChildren(list, emptyState(
        key && key !== ctx.todayKey ? 'No doses were scheduled for this day.' : 'No doses scheduled for today.',
        h('a', { href: '#schedules' }, ' Add a schedule'),
      ));
      return;
    }
    summary.textContent = summaryText(events);
    const rows = events.map((ev) => h('tr', { class: ev.needs_review ? 'dose-row needs-review' : 'dose-row' },
      h('td', { class: 'dose-time' }, formatClock(ev.scheduled_local)),
      h('td', {},
        h('div', { class: 'dose-med' }, ev.medication_name || DASH),
        ev.strength ? h('div', { class: 'muted' }, ev.strength) : null),
      h('td', {}, ev.compartment_number ? `Compartment ${ev.compartment_number}` : 'Not assigned'),
      h('td', {}, h('div', { class: 'status-cell' }, statusBadge(ev.status, ev.needs_review))),
      h('td', { class: 'dose-details' }, details(ev)),
      h('td', {}, actions(ev)),
    ));
    replaceChildren(list, h('div', { class: 'table-wrap' },
      h('table', { class: 'dose-table' },
        h('caption', { class: 'visually-hidden' }, heading.textContent),
        h('thead', {}, h('tr', {},
          ['Time', 'Medication', 'Compartment', 'Status', 'Details', 'Actions'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, rows))));
  }

  async function run(action, success) {
    try {
      await action();
      ctx.notify(success, 'success');
    } catch (err) {
      ctx.notify(errorText(err), 'error');
    }
    load();
  }

  async function resolve(ev, accessed) {
    const what = `${formatClock(ev.scheduled_local)} · ${ev.medication_name}`;
    const { ok, note } = await confirmDialog({
      title: accessed ? 'Record: the dose WAS accessed?' : 'Record: the dose was NOT accessed?',
      message: accessed
        ? `${what}\n\nChoose this if the compartment opened or the dose may have been taken. It is recorded as accessed and will not be dispensed again.`
        : `${what}\n\nOnly choose this after checking the device: the dose is still in its compartment and the gate is closed. The dose becomes due again and can be dispensed.`,
      confirmLabel: accessed ? 'It was accessed' : 'It was NOT accessed',
      noteLabel: 'Note for the log (optional)',
      danger: !accessed,
      iconEl: icon('warning'),
    });
    if (!ok) return;
    await run(
      () => post(`/api/dose-events/${ev.event_id}/resolve`, { accessed, note: note || undefined, by: ctx.caregiverName() || undefined }),
      accessed ? 'Recorded as accessed.' : 'Recorded as not accessed — the dose is due again.',
    );
  }

  async function skip(ev) {
    const { ok, note } = await confirmDialog({
      title: 'Skip this dose?',
      message: `${formatClock(ev.scheduled_local)} · ${ev.medication_name}\n\nIt is recorded as skipped and will not be dispensed.`,
      confirmLabel: 'Skip dose',
      noteLabel: 'Reason (optional)',
      danger: true,
    });
    if (!ok) return;
    await run(
      () => post(`/api/dose-events/${ev.event_id}/skip`, { note: note || undefined, by: ctx.caregiverName() || undefined }),
      'Dose skipped.',
    );
  }

  async function markTaken(ev) {
    const { ok } = await confirmDialog({
      title: 'Mark this dose as taken?',
      message: `${formatClock(ev.scheduled_local)} · ${ev.medication_name}\n\nOnly do this if you know the dose was taken.`,
      confirmLabel: 'Mark taken',
    });
    if (!ok) return;
    await run(() => post(`/api/dose-events/${ev.event_id}/mark-taken`, { by: ctx.caregiverName() || undefined }), 'Marked as taken.');
  }

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
    /** The demo clock moved: "today" may now be another day. */
    onClock() {
      stale = true;
      if (visible) loadSoon();
    },
  };
}
