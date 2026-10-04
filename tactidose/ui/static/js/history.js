/**
 * Pill drop history (GET /api/patients/{pid}/drops) shared by both portals: every
 * request with its outcome, including refused ones and their reasons. Caregivers can
 * resolve UNCERTAIN drops ("It dropped" / "It did not drop",
 * POST …/drops/{drop_id}/resolve).
 */

import { get, post } from './api.js';
import { confirmDialog, emptyState, errorState, errorText, h, replaceChildren, setLoading, uid } from './dom.js';
import { icon } from './icons.js';
import { containerNumberOf, dateKey, formatClockDevice, formatLongDate, plural, relativeDayWord } from './format.js';
import { dropStatusInfo, reasonText, sourceText } from './words.js';

export const HISTORY_DAYS = Object.freeze([1, 7, 14, 30]);
export const STATUS_FILTERS = Object.freeze([
  ['', 'All requests'],
  ['DROPPED', 'Dropped'],
  ['DENIED', 'Not dropped'],
  ['FAILED', 'Drop failed'],
  ['UNCERTAIN', 'Not sure if it dropped'],
]);

/** Plain words for one PillDropView. */
export function dropSummary(drop, { audience = 'patient', offsetMin = null } = {}) {
  const number = containerNumberOf(drop);
  const med = drop.medication_name || 'Unknown medication';
  const iso = drop.completed_at || drop.requested_local || drop.requested_at;
  const status = dropStatusInfo(drop.status, drop.needs_review);
  const reason = drop.status === 'DROPPED' ? null : reasonText(drop.reason);
  let pills = null;
  if (drop.status === 'DROPPED' && Number.isFinite(Number(drop.pill_count_after))) {
    pills = `${plural(Number(drop.pill_count_after), 'pill')} left in the container afterwards`;
  }
  return {
    title: number ? `${med} — container ${number}` : med,
    time: formatClockDevice(iso, offsetMin),
    iso,
    status,
    reason,
    source: sourceText(drop.source, audience),
    pills,
    needsReview: drop.status === 'UNCERTAIN' && Boolean(drop.needs_review),
    note: drop.review_note || null,
  };
}

/** Group drops (newest first) by device-local day: [{key, label, items}]. */
export function groupByDay(drops, offsetMin = null, nowLocal = null) {
  const groups = [];
  const byKey = new Map();
  for (const d of drops || []) {
    const iso = d.requested_local || d.requested_at || d.completed_at;
    const key = dateKey(iso, d.requested_local ? null : offsetMin) || 'unknown';
    if (!byKey.has(key)) {
      const word = nowLocal ? relativeDayWord(`${key}T12:00:00`, nowLocal) : null;
      const label = key === 'unknown' ? 'Unknown day' : `${word ? `${word[0].toUpperCase()}${word.slice(1)} — ` : ''}${formatLongDate(key)}`;
      const group = { key, label, items: [] };
      byKey.set(key, group);
      groups.push(group);
    }
    byKey.get(key).items.push(d);
  }
  return groups;
}

/**
 * Mount the history view in `root`. Options: getPatientId(), audience, canResolve,
 * notify(message, kind), getOffset(), getNow(), onChanged().
 */
export function createDropHistory(root, {
  getPatientId,
  audience = 'patient',
  canResolve = false,
  notify = () => {},
  getOffset = () => null,
  getNow = () => null,
  onChanged = () => {},
} = {}) {
  const daysId = uid('hist-days');
  const statusId = uid('hist-status');
  const days = h('select', { id: daysId }, HISTORY_DAYS.map((d) => h('option', { value: String(d), selected: d === 7 }, d === 1 ? 'Today and yesterday' : `Last ${d} days`)));
  const status = h('select', { id: statusId }, STATUS_FILTERS.map(([v, label]) => h('option', { value: v }, label)));
  const refresh = h('button', { type: 'button', class: 'btn' }, icon('rotate'), 'Refresh');
  const list = h('div', { class: 'history-list' });
  root.replaceChildren(
    h('div', { class: 'filter-row' },
      h('div', { class: 'field' }, h('label', { for: daysId }, 'Show'), days),
      h('div', { class: 'field' }, h('label', { for: statusId }, 'Which'), status),
      refresh),
    list);

  let seq = 0;
  let loadedOnce = false;

  async function load() {
    const pid = getPatientId();
    if (!pid) return;
    const token = ++seq;
    setLoading(list, true);
    const d = Number(days.value) || 7;
    const s = status.value;
    try {
      const path = s
        ? `/api/patients/${pid}/drops?days=${d}&status=${s}`
        : `/api/patients/${pid}/drops?days=${d}`;
      const drops = await get(path);
      if (token !== seq) return;
      loadedOnce = true;
      render(Array.isArray(drops) ? drops : []);
    } catch (err) {
      if (token !== seq) return;
      replaceChildren(list, errorState(err, load, icon('warning')));
    } finally {
      if (token === seq) setLoading(list, false);
    }
  }

  function render(drops) {
    if (!drops.length) {
      replaceChildren(list, emptyState(status.value ? 'Nothing matches this filter.' : 'No pill requests in this period.'));
      return;
    }
    const offset = getOffset();
    const groups = groupByDay(drops, offset, getNow());
    replaceChildren(list, groups.map((g) => {
      const headingId = uid('hist-day');
      return h('section', { class: 'history-day', 'aria-labelledby': headingId },
        h('h3', { id: headingId, class: 'day-heading' }, g.label),
        h('ul', { class: 'item-list' }, g.items.map((d) => item(d, offset))));
    }));
  }

  function item(drop, offset) {
    const v = dropSummary(drop, { audience, offsetMin: offset });
    const lines = [v.source, v.reason, v.pills, v.note ? `Note: ${v.note}` : null].filter(Boolean);
    const actions = [];
    if (canResolve && v.needsReview) {
      actions.push(
        h('button', { type: 'button', class: 'btn btn-primary', 'aria-label': `It dropped: ${v.title} at ${v.time}`, on: { click: () => resolve(drop, true) } }, icon('check'), 'It dropped'),
        h('button', { type: 'button', class: 'btn', 'aria-label': `It did not drop: ${v.title} at ${v.time}`, on: { click: () => resolve(drop, false) } }, icon('x'), 'It did not drop'),
      );
    }
    return h('li', { class: `item${v.needsReview ? ' needs-attention' : ''}` },
      h('div', { class: 'item-head' },
        h('span', { class: 'item-sub' }, v.time),
        h('p', { class: 'item-title' }, v.title),
        h('span', { class: `badge tone-${v.status.tone}` }, icon(v.status.icon), v.status.word)),
      lines.length ? h('p', { class: 'item-body' }, lines.join(' · ')) : null,
      actions.length ? h('div', { class: 'item-actions' }, actions) : null);
  }

  async function resolve(drop, dropped) {
    const pid = getPatientId();
    const v = dropSummary(drop, { audience, offsetMin: getOffset() });
    const { ok, note } = await confirmDialog({
      title: dropped ? 'Record that the pill dropped?' : 'Record that no pill dropped?',
      message: dropped
        ? `${v.title} at ${v.time}.\nThe pill count goes down by one and the device can drop pills again.`
        : `${v.title} at ${v.time}.\nThe pill count stays the same and the device can drop pills again.`,
      confirmLabel: dropped ? 'Yes, it dropped' : 'No pill dropped',
      noteLabel: 'Note (optional)',
      iconEl: icon('help'),
    });
    if (!ok) return;
    try {
      const body = note ? { dropped, note } : { dropped };
      await post(`/api/patients/${pid}/drops/${drop.drop_id}/resolve`, body);
      notify(dropped ? 'Recorded: the pill dropped.' : 'Recorded: no pill dropped.', 'success');
      onChanged();
    } catch (err) {
      notify(errorText(err), 'error');
    }
    load();
  }

  days.addEventListener('change', load);
  status.addEventListener('change', load);
  refresh.addEventListener('click', load);

  return {
    load,
    get loaded() {
      return loadedOnce;
    },
  };
}
