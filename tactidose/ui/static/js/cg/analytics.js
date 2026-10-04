/**
 * Caregiver "Analytics" tab: local adherence summary (GET /api/analytics/summary)
 * as stat tiles + an accessible bar chart with a data-table twin, plus the
 * Snowflake sync status, manual sync and report tables.
 */

import { get, post } from '../api.js';
import { byId, debounce, emptyState, errorState, errorText, h, replaceChildren } from '../dom.js';
import { icon } from '../icons.js';
import { DASH, formatCount, formatDateTimeDevice, formatMinutes, formatPercent, toPercent } from '../format.js';
import { adherenceTable, createAdherenceChart } from '../chart.js';

const WINDOW_NAMES = { morning: 'Morning (5–12)', afternoon: 'Afternoon (12–17)', evening: 'Evening (17–22)', night: 'Night (22–5)' };

/** Stat tiles: [{label, value, note, icon, hero}] from a summary. */
export function summaryTiles(summary, days) {
  const t = summary?.totals || {};
  const windowDays = summary?.window_days || days;
  return [
    {
      hero: true,
      label: `Adherence, last ${windowDays} days`,
      value: formatPercent(summary?.adherence_rate),
      note: `${formatCount(t.taken ?? 0)} of ${formatCount(t.scheduled ?? 0)} scheduled doses confirmed taken`,
    },
    { label: 'Taken', value: formatCount(t.taken), icon: 'check-circle' },
    { label: 'Missed', value: formatCount(t.missed), icon: t.missed ? 'x-circle' : null, note: t.missed ? 'window closed without access' : null },
    { label: 'Accessed, not confirmed', value: formatCount(t.accessed_unconfirmed), icon: t.accessed_unconfirmed ? 'open' : null },
    { label: 'Hardware errors', value: formatCount(t.hardware_errors), icon: t.hardware_errors ? 'warning' : null },
    { label: 'Average time to "taken"', value: formatMinutes(summary?.avg_confirm_delay_minutes), note: 'after the scheduled time' },
    { label: 'Pending', value: formatCount(t.pending) },
    { label: 'Skipped / cancelled', value: formatCount(t.cancelled) },
  ];
}

function tile({ label, value, note, icon: iconName, hero }) {
  return h('div', { class: hero ? 'tile tile-hero' : 'tile' },
    h('span', { class: 'tile-label' }, label),
    h('span', { class: 'tile-value' }, value),
    note || iconName ? h('span', { class: 'tile-note' }, iconName ? icon(iconName) : null, note || '') : null);
}

function simpleTable(caption, head, rows) {
  return h('div', { class: 'table-wrap' }, h('table', {},
    h('caption', {}, caption),
    h('thead', {}, h('tr', {}, head.map(([text, cls]) => h('th', { scope: 'col', class: cls || null }, text)))),
    h('tbody', {}, rows)));
}

export function createAnalytics(ctx) {
  const daysSelect = byId('an-days');
  const body = byId('an-body');
  const tiles = byId('an-tiles');
  const chartTable = byId('an-chart-table');
  const windows = byId('an-windows');
  const errors = byId('an-errors');
  const source = byId('an-source');
  const sfStatus = byId('sf-status');
  const sfSync = byId('sf-sync');
  const sfReportBtn = byId('sf-report-btn');
  const sfReport = byId('sf-report');
  const chart = createAdherenceChart(byId('an-chart'), { label: 'Doses taken, by day' });
  let visible = false;
  let stale = true;
  let requestSeq = 0;
  let hasData = false;

  daysSelect.addEventListener('change', () => load());
  byId('an-refresh').addEventListener('click', () => load());

  async function load() {
    stale = false;
    const token = ++requestSeq;
    const days = Number(daysSelect.value) || 7;
    body.classList.add('is-refreshing');
    body.setAttribute('aria-busy', 'true');
    try {
      const summary = await get(`/api/analytics/summary?days=${days}`);
      if (token !== requestSeq) return;
      render(summary || {}, days);
    } catch (err) {
      if (token !== requestSeq) return;
      if (!hasData) replaceChildren(tiles, errorState(err, load, icon('warning')));
      else ctx.notify(`Could not refresh analytics: ${errorText(err)}`, 'error');
    } finally {
      if (token === requestSeq) {
        body.classList.remove('is-refreshing');
        body.setAttribute('aria-busy', 'false');
      }
    }
    loadSnowflake();
  }

  const loadSoon = debounce(load, 800);

  function render(summary, days) {
    hasData = true;
    replaceChildren(tiles, summaryTiles(summary, days).map(tile));
    source.textContent = summary.source ? `Source: ${summary.source === 'local' ? 'this device (local database)' : summary.source}` : '';
    chart.update(summary.by_day || []);
    replaceChildren(chartTable, adherenceTable(summary.by_day || [], { caption: 'Doses taken, by day' }));
    renderWindows(summary.by_time_window || []);
    renderErrors(summary.device_errors || []);
  }

  function renderWindows(items) {
    if (!items.length) {
      replaceChildren(windows, h('h4', {}, 'Missed doses by time of day'), emptyState('No data yet.'));
      return;
    }
    let worst = null;
    for (const w of items) {
      const p = toPercent(w.miss_rate);
      if (p !== null && p > 0 && (!worst || p > toPercent(worst.miss_rate))) worst = w;
    }
    replaceChildren(windows, simpleTable('Missed doses by time of day',
      [['Time of day'], ['Scheduled', 'num'], ['Missed', 'num'], ['Miss rate', 'num']],
      items.map((w) => h('tr', {},
        h('th', { scope: 'row' }, WINDOW_NAMES[w.time_window] || w.time_window, w === worst ? h('span', { class: 'muted' }, ' — most missed') : null),
        h('td', { class: 'num' }, formatCount(w.scheduled)),
        h('td', { class: 'num' }, formatCount(w.missed)),
        h('td', { class: 'num' }, formatPercent(w.miss_rate))))));
  }

  function renderErrors(items) {
    if (!items.length) {
      replaceChildren(errors, h('h4', {}, 'Device errors'), emptyState('No device errors recorded in this period.'));
      return;
    }
    replaceChildren(errors, simpleTable('Device errors',
      [['Code'], ['Count', 'num']],
      items.map((e) => h('tr', {}, h('th', { scope: 'row', class: 'mono' }, e.code), h('td', { class: 'num' }, formatCount(e.count))))));
  }

  // ---------------------------------------------------------------- Snowflake

  function renderSnowflake(sf) {
    const configured = Boolean(sf?.configured);
    sfSync.disabled = !configured;
    sfReportBtn.disabled = !configured;
    const rows = [
      ['Status', configured ? 'Configured' : 'Not configured — adherence events wait in the local outbox'],
      ['Last sync', sf?.last_sync ? formatDateTimeDevice(sf.last_sync, ctx.offsetMin) : 'Never'],
      ['Waiting to send', formatCount(sf?.pending ?? 0)],
      ['Sent', formatCount(sf?.sent ?? 0)],
      ['Last error', sf?.last_error || 'None'],
    ];
    replaceChildren(sfStatus, h('table', { class: 'kv' },
      h('caption', {}, 'Snowflake sync'),
      h('tbody', {}, rows.map(([k, v]) => h('tr', {}, h('th', { scope: 'row' }, k), h('td', {}, String(v)))))));
  }

  async function loadSnowflake() {
    try {
      renderSnowflake(await get('/api/analytics/snowflake'));
    } catch (err) {
      replaceChildren(sfStatus, errorState(err, loadSnowflake, icon('warning')));
    }
  }

  sfSync.addEventListener('click', async () => {
    sfSync.disabled = true;
    try {
      const sf = await post('/api/analytics/snowflake/sync', {});
      renderSnowflake(sf);
      ctx.notify(sf?.last_error ? `Sync finished with an error: ${sf.last_error}` : 'Snowflake sync finished.', sf?.last_error ? 'warning' : 'success');
    } catch (err) {
      ctx.notify(errorText(err), 'error');
      sfSync.disabled = false;
    }
  });

  sfReportBtn.addEventListener('click', async () => {
    replaceChildren(sfReport, h('p', { class: 'muted' }, 'Running report queries…'));
    try {
      const report = await get('/api/analytics/snowflake/report');
      if (!report?.configured) {
        replaceChildren(sfReport, emptyState('Snowflake is not configured, so there is no cloud report.'));
        return;
      }
      const parts = [];
      if (report.error) parts.push(errorState(report.error));
      for (const q of report.queries || []) {
        const columns = Array.isArray(q.columns) ? q.columns : [];
        const rows = Array.isArray(q.rows) ? q.rows : [];
        parts.push(h('section', { class: 'sf-query' },
          h('h4', {}, q.name || 'Query'),
          q.description ? h('p', { class: 'muted' }, q.description) : null,
          rows.length
            ? simpleTable(q.name || 'Query result', columns.map((c) => [c]),
              rows.map((row) => h('tr', {}, (Array.isArray(row) ? row : [row]).map((cell) => h('td', {}, cell === null || cell === undefined ? DASH : String(cell))))))
            : emptyState('No rows.')));
      }
      replaceChildren(sfReport, parts.length ? parts : emptyState('The report returned no queries.'));
    } catch (err) {
      replaceChildren(sfReport, errorState(err, null, icon('warning')));
    }
  });

  return {
    show() {
      visible = true;
      if (stale || !hasData) load();
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
