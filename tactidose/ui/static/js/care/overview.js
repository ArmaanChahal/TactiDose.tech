/**
 * Care portal "Overview" tab: the patient's situation now (device, cooldown, automatic
 * drops, next dose, last drop, adherence), what needs attention (alerts, uncertain
 * drops awaiting review), containers, today's doses, the latest drops and a
 * seven-day table of scheduled doses.
 */

import { get } from '../api.js';
import { byId, emptyState, errorState, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { formatPercent, plural, shiftDateKey, formatDateLabel } from '../format.js';
import { alertsView, containerView, cooldownView, deviceView, dropWhen, nextPillText, remainingCooldown, todayKey } from '../status.js';
import { renderDoseList } from '../doses.js';
import { dropSummary } from '../history.js';
import { dropStatusInfo, sourceText } from '../words.js';
import { lazyPanel } from './panel.js';

/** Count one day's DoseViews: {dropped, missed, skipped, open, problems}. */
export function summarizeDay(doses) {
  const out = { dropped: 0, missed: 0, skipped: 0, open: 0, problems: 0 };
  for (const d of doses || []) {
    if (d.status === 'DISPENSED' || d.status === 'TAKEN') out.dropped += 1;
    else if (d.status === 'MISSED') out.missed += 1;
    else if (d.status === 'CANCELLED') out.skipped += 1;
    else if (d.status === 'HARDWARE_ERROR') out.problems += 1;
    else out.open += 1;
  }
  return out;
}

/** ctx: {pid, patient, loadStatus(), getOffset(), getNow(), goTo(tab)} */
export function createOverview(ctx) {
  const facts = byId('ov-facts');
  const alerts = byId('ov-alerts');
  const containers = byId('ov-containers');
  const today = byId('ov-today');
  const drops = byId('ov-drops');
  const week = byId('ov-week');
  let weekFor = null;

  function fact(label, ...value) {
    return [h('dt', {}, label), h('dd', {}, ...value)];
  }

  function renderFacts(status) {
    const dev = deviceView(status.device);
    const cd = cooldownView(status, remainingCooldown(status, ctx.elapsedS()));
    const cooldownText = cd.active
      ? `Running: the patient can drop again ${cd.at ? `at ${cd.at} ` : ''}(${cd.countdown}).`
      : `Not running. Waiting time after a drop: ${plural(Number(status.cooldown_minutes) || 0, 'minute')}.`;
    const last = status.last_drop;
    const lastText = last
      ? `${last.medication_name || 'Pill'}, ${dropWhen(last, status.now_local, ctx.getOffset())} — ${dropStatusInfo(last.status).word.toLowerCase()} (${sourceText(last.source, 'caregiver').toLowerCase()})`
      : 'No drops yet.';
    const adherence = ctx.patient?.adherence_7d;
    replaceChildren(facts,
      fact('Device', h('span', { class: `badge tone-${dev.tone}` }, icon(dev.icon), dev.word), ' ', dev.text),
      fact('Cooldown', cooldownText),
      fact('Automatic drops', status.auto_drop_enabled === false ? 'Off — nothing drops by itself' : 'On'),
      fact('Next scheduled', nextPillText(status)),
      fact('Last drop', lastText),
      fact('Adherence, 7 days', adherence === null || adherence === undefined ? 'Not enough data yet' : formatPercent(adherence)));
  }

  function renderAlerts(status, uncertain) {
    const items = alertsView(status).map((a) => h('li', { class: `item${a.tone === 'bad' ? ' needs-attention' : ' is-caution'}` },
      h('div', { class: 'item-head' }, icon(a.tone === 'bad' ? 'warning' : 'info', { className: `icon tone-${a.tone}` }), h('p', { class: 'item-title' }, a.text))));
    for (const d of uncertain) {
      const v = dropSummary(d, { audience: 'caregiver', offsetMin: ctx.getOffset() });
      items.push(h('li', { class: 'item needs-attention' },
        h('div', { class: 'item-head' }, icon('help', { className: 'icon tone-bad' }),
          h('p', { class: 'item-title' }, `Not sure a pill dropped: ${v.title}, ${v.time}. Drops are paused until you record what happened.`)),
        h('div', { class: 'item-actions' },
          h('button', { type: 'button', class: 'btn btn-primary', on: { click: () => ctx.goTo('history') } }, 'Review in History'))));
    }
    if (!items.length) {
      replaceChildren(alerts, h('li', { class: 'state-msg', 'data-state': 'empty' }, icon('check-circle', { className: 'icon tone-good' }), ' Nothing needs attention.'));
      return;
    }
    replaceChildren(alerts, items);
  }

  function renderContainers(status) {
    const rows = (status.containers || []).map(containerView).sort((a, b) => a.slot - b.slot);
    if (!rows.length) {
      replaceChildren(containers, emptyState('No containers yet.'));
      return;
    }
    replaceChildren(containers, h('ul', { class: 'item-list' }, rows.map((v) => {
      const stock = v.badge
        ? h('span', { class: `badge tone-${v.badge.tone}` }, icon(v.badge.icon), v.badge.word === 'Low' ? 'Low stock' : 'Empty')
        : (v.hasMed ? h('span', { class: 'badge tone-good' }, icon('check'), 'OK') : h('span', { class: 'badge tone-neutral' }, icon('slash'), 'Not in use'));
      return h('li', { class: `item${v.empty ? ' needs-attention' : v.low ? ' is-caution' : ''}` },
        h('div', { class: 'item-head' },
          h('span', { class: 'item-sub' }, `Container ${v.number ?? '?'}`),
          h('p', { class: 'item-title' }, v.medName),
          stock),
        h('p', { class: 'item-body' }, [v.strength, v.hasMed ? `${v.count}${v.capacity ? ` of ${v.capacity}` : ''} pills left` : null].filter(Boolean).join(' · ')));
    })));
  }

  function renderDrops(list) {
    if (!list.length) {
      replaceChildren(drops, h('li', { class: 'state-msg', 'data-state': 'empty' }, 'No drops in the last 7 days.'));
      return;
    }
    replaceChildren(drops, list.slice(0, 6).map((d) => {
      const v = dropSummary(d, { audience: 'caregiver', offsetMin: ctx.getOffset() });
      return h('li', { class: `item${v.needsReview ? ' needs-attention' : ''}` },
        h('div', { class: 'item-head' },
          h('span', { class: 'item-sub' }, dropWhen(d, ctx.getNow(), ctx.getOffset())),
          h('p', { class: 'item-title' }, v.title),
          h('span', { class: `badge tone-${v.status.tone}` }, icon(v.status.icon), v.status.word)),
        h('p', { class: 'item-body' }, [v.source, v.reason].filter(Boolean).join(' · ')));
    }));
  }

  async function loadWeek(status) {
    const end = todayKey(status);
    if (!end || weekFor === `${ctx.pid}|${end}`) return;
    weekFor = `${ctx.pid}|${end}`;
    const days = Array.from({ length: 7 }, (_, i) => shiftDateKey(end, i - 6));
    setLoading(week, true);
    try {
      const results = await Promise.all(days.map((d) => get(`/api/patients/${ctx.pid}/doses?date=${d}`)));
      const rows = days.map((d, i) => ({ day: d, ...summarizeDay(Array.isArray(results[i]) ? results[i] : []) }));
      const total = rows.reduce((acc, r) => ({ dropped: acc.dropped + r.dropped, missed: acc.missed + r.missed }), { dropped: 0, missed: 0 });
      const rate = total.dropped + total.missed ? total.dropped / (total.dropped + total.missed) : null;
      replaceChildren(week,
        h('p', {}, rate === null
          ? 'No completed scheduled doses in the last 7 days.'
          : `${formatPercent(rate)} of finished scheduled doses dropped: ${plural(total.dropped, 'dropped dose')}, ${plural(total.missed, 'missed dose')}.`),
        h('div', { class: 'table-wrap', tabindex: '0', role: 'region', 'aria-label': 'Scheduled doses per day (scrolls sideways on small screens)' }, h('table', { class: 'week-table' },
          h('caption', { class: 'visually-hidden' }, 'Scheduled doses per day, last 7 days'),
          h('thead', {}, h('tr', {}, ['Day', 'Dropped', 'Missed', 'Skipped', 'Problems', 'To come'].map((t, i) => h('th', { scope: 'col', class: i ? 'num' : null }, t)))),
          h('tbody', {}, rows.map((r) => h('tr', { class: r.missed || r.problems ? 'row-attention' : null },
            h('th', { scope: 'row' }, r.day === end ? `Today, ${formatDateLabel(r.day)}` : formatDateLabel(r.day)),
            h('td', { class: 'num' }, String(r.dropped)),
            h('td', { class: 'num' }, r.missed ? h('strong', {}, String(r.missed)) : '0'),
            h('td', { class: 'num' }, String(r.skipped)),
            h('td', { class: 'num' }, r.problems ? h('strong', {}, String(r.problems)) : '0'),
            h('td', { class: 'num' }, String(r.open))))))));
    } catch (err) {
      weekFor = null;
      replaceChildren(week, errorState(err, () => loadWeek(status), icon('warning')));
    } finally {
      setLoading(week, false);
    }
  }

  async function load() {
    const pid = ctx.pid;
    if (!pid) return;
    setLoading(facts, true);
    try {
      const [status, recent, uncertain] = await Promise.all([
        ctx.loadStatus(),
        get(`/api/patients/${pid}/drops?days=7`).catch(() => []),
        get(`/api/patients/${pid}/drops?days=30&status=UNCERTAIN`).catch(() => []),
      ]);
      if (pid !== ctx.pid || !status) return;
      renderFacts(status);
      renderAlerts(status, (Array.isArray(uncertain) ? uncertain : []).filter((d) => d.needs_review));
      renderContainers(status);
      renderDoseList(today, status.today || [], { audience: 'caregiver', offsetMin: ctx.getOffset(), empty: 'No doses scheduled today.' });
      renderDrops(Array.isArray(recent) ? recent : []);
      loadWeek(status);
    } catch (err) {
      replaceChildren(facts, errorState(err, load, icon('warning')));
    } finally {
      setLoading(facts, false);
    }
  }

  const panel = lazyPanel(load);
  return {
    show: panel.show,
    hide: panel.hide,
    markStale: panel.markStale,
    reset() {
      weekFor = null;
      panel.reset();
    },
    /** Live DeviceSnapshot: update the device line without a refetch. */
    updateDevice(status) {
      if (panel.visible && status) renderFacts(status);
    },
  };
}
