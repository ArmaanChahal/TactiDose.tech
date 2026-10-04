/**
 * Scheduled doses (DoseView) as a plain list: time, medication, container and what
 * happened. Used for "Today's schedule" (patient), the care Overview and the care
 * History tab (where SCHEDULED/DUE doses can be skipped).
 */

import { h } from './dom.js';
import { icon } from './icons.js';
import { containerNumberOf, formatClock, formatClockDevice } from './format.js';
import { doseStatusInfo, sourceText } from './words.js';

const SKIPPABLE = new Set(['SCHEDULED', 'DUE', 'HARDWARE_ERROR']);

export function canSkip(dose) {
  return Boolean(dose && SKIPPABLE.has(dose.status));
}

/** Words for one DoseView. */
export function doseSummary(dose, { audience = 'patient', offsetMin = null } = {}) {
  const number = containerNumberOf(dose);
  const status = doseStatusInfo(dose.status, dose.needs_review);
  const time = dose.scheduled_local ? formatClock(dose.scheduled_local) : formatClockDevice(dose.scheduled_at, offsetMin);
  let detail = null;
  if ((dose.status === 'DISPENSED' || dose.status === 'TAKEN') && dose.dispensed_at) {
    const how = dose.dispense_source ? sourceText(dose.dispense_source, audience) : null;
    detail = `Dropped at ${formatClockDevice(dose.dispensed_at, offsetMin)}${how ? ` — ${how.charAt(0).toLowerCase()}${how.slice(1)}` : ''}`;
  } else if (dose.status === 'MISSED') {
    detail = 'The pill did not drop in time.';
  } else if (dose.status === 'HARDWARE_ERROR' && dose.needs_review) {
    detail = 'A caregiver needs to check the device.';
  }
  return {
    time,
    title: dose.medication_name || 'Medication',
    container: number ? `Container ${number}` : 'No container assigned',
    status,
    detail,
  };
}

/** Render `doses` (sorted by time) into the <ul> `listEl`. `onSkip(dose)` adds Skip buttons. */
export function renderDoseList(listEl, doses, { audience = 'patient', offsetMin = null, onSkip = null, empty = 'No pills are scheduled for this day.' } = {}) {
  const items = [...(doses || [])].sort((a, b) => String(a.scheduled_at).localeCompare(String(b.scheduled_at)));
  if (!items.length) {
    listEl.replaceChildren(h('li', { class: 'state-msg', 'data-state': 'empty' }, empty));
    return;
  }
  listEl.replaceChildren(...items.map((dose) => {
    const v = doseSummary(dose, { audience, offsetMin });
    const skip = onSkip && canSkip(dose)
      ? h('button', { type: 'button', class: 'btn btn-small', 'aria-label': `Skip ${v.title} at ${v.time}`, on: { click: () => onSkip(dose) } }, icon('slash'), 'Skip this dose')
      : null;
    return h('li', { class: `item dose-item${v.status.needsReview ? ' needs-attention' : ''}` },
      h('div', { class: 'item-head' },
        h('span', { class: 'dose-time' }, v.time),
        h('p', { class: 'item-title' }, v.title),
        h('span', { class: 'item-sub' }, v.container),
        h('span', { class: `badge tone-${v.status.tone}` }, icon(v.status.icon), v.status.word)),
      v.detail ? h('p', { class: 'item-body' }, v.detail) : null,
      skip ? h('div', { class: 'item-actions' }, skip) : null);
  }));
}
