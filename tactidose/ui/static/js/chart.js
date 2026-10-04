/**
 * Accessible inline-SVG column chart: share of scheduled doses confirmed taken,
 * per day (GET /api/analytics/summary -> by_day).
 *
 * Data-viz rules applied: one series, so one hue (categorical slot 1, validated
 * against both theme surfaces) and no legend box — the title names it; columns
 * at most 24px wide with a 4px rounded data-end, square on the baseline;
 * hairline solid gridlines; direct labels only on the latest and the lowest day;
 * a per-column tooltip on hover *and* keyboard focus (roving tabindex, arrow
 * keys) that enhances but never gates — every value is also in the data table.
 * Text is always rendered in text tokens, never in the series colour.
 */

import { h, s } from './dom.js';
import { formatLongDate, parseIso, toPercent, weekdayShort } from './format.js';

const HEIGHT = 270;
const MARGIN = { top: 30, right: 12, bottom: 56, left: 52 };
const MAX_BAR = 24;
const TICKS = [0, 25, 50, 75, 100];

const round = (v) => Math.round(v * 10) / 10;
const toInt = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);

/** Normalise by_day rows: {date, scheduled, taken, missed, other, pct (0–100 | null)}. */
export function chartRows(byDay) {
  return (Array.isArray(byDay) ? byDay : []).map((d) => {
    const scheduled = toInt(d.scheduled);
    const taken = toInt(d.taken);
    const missed = toInt(d.missed);
    let pct = toPercent(d.rate);
    if (pct === null && scheduled > 0) pct = (taken / scheduled) * 100;
    if (scheduled === 0) pct = null;
    return { date: String(d.date ?? ''), scheduled, taken, missed, other: Math.max(0, scheduled - taken - missed), pct };
  });
}

/** Selective direct labels: the latest day with data (endpoint) and the lowest day (extreme). */
export function directLabelIndexes(rows) {
  const withData = rows.map((r, i) => ({ r, i })).filter(({ r }) => r.pct !== null);
  if (!withData.length) return new Set();
  let low = withData[0];
  for (const item of withData) if (item.r.pct < low.r.pct) low = item;
  return new Set([withData[withData.length - 1].i, low.i]);
}

/** Show every n-th x label (counted back from the latest day) so labels never collide. */
export function labelEvery(bandPx, minPx = 54) {
  return Math.max(1, Math.ceil(minPx / Math.max(1, bandPx)));
}

/** Column path with rounded top corners (radius ≤ 4) and a square base. */
export function columnPath(x, y, width, height, radius = 4) {
  if (!(height > 0) || !(width > 0)) return '';
  const r = Math.min(radius, width / 2, height);
  const base = round(y + height);
  return `M${round(x)} ${base}V${round(y + r)}A${r} ${r} 0 0 1 ${round(x + r)} ${round(y)}`
    + `H${round(x + width - r)}A${r} ${r} 0 0 1 ${round(x + width)} ${round(y + r)}V${base}Z`;
}

function shortDay(key) {
  const p = parseIso(key);
  return p ? `${weekdayShort(key)} ${p.day}` : key;
}

export function describeRow(r) {
  const day = formatLongDate(r.date);
  if (r.pct === null) return `${day}: no doses scheduled`;
  const missed = r.missed ? `, ${r.missed} missed` : '';
  return `${day}: ${Math.round(r.pct)}% taken, ${r.taken} of ${r.scheduled} doses${missed}`;
}

/**
 * Mount a chart in `host` (position: relative). Returns {update(byDay), destroy()}.
 * `label` is the accessible name of the chart group.
 */
export function createAdherenceChart(host, { label = 'Adherence by day' } = {}) {
  let rows = [];
  let lastWidth = 0;
  let cols = [];
  const plot = h('div', { class: 'chart-plot' });
  const tip = h('div', { class: 'chart-tip', 'aria-hidden': 'true', hidden: true });
  host.replaceChildren(plot, tip);

  function hideTip() {
    tip.hidden = true;
    for (const c of cols) c.classList.remove('is-active');
  }

  function showTip(index, barTop) {
    const r = rows[index];
    if (!r) return;
    for (const [i, c] of cols.entries()) c.classList.toggle('is-active', i === index);
    tip.replaceChildren(
      h('div', { class: 'tip-value' }, r.pct === null ? 'No doses' : `${Math.round(r.pct)}%`,
        r.pct === null ? null : h('span', { class: 'tip-unit' }, ' taken')),
      h('div', { class: 'tip-date' }, formatLongDate(r.date)),
      r.scheduled ? h('div', { class: 'tip-row' }, `${r.taken} of ${r.scheduled} doses taken`) : null,
      r.missed ? h('div', { class: 'tip-row' }, `${r.missed} missed`) : null,
      r.other ? h('div', { class: 'tip-row' }, `${r.other} other (pending, unconfirmed or skipped)`) : null,
    );
    tip.hidden = false;
    const width = host.clientWidth || lastWidth;
    const band = (lastWidth - MARGIN.left - MARGIN.right) / rows.length;
    const cx = MARGIN.left + band * index + band / 2;
    const left = Math.max(0, Math.min(cx - tip.offsetWidth / 2, width - tip.offsetWidth));
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(Math.max(0, barTop - tip.offsetHeight - 12))}px`;
  }

  function focusCol(index) {
    const target = cols[Math.max(0, Math.min(index, cols.length - 1))];
    if (!target) return;
    for (const c of cols) c.setAttribute('tabindex', c === target ? '0' : '-1');
    target.focus();
  }

  function draw() {
    const width = Math.max(280, Math.floor(host.clientWidth || 640));
    lastWidth = width;
    cols = [];
    hideTip();
    if (!rows.length || rows.every((r) => r.scheduled === 0)) {
      plot.replaceChildren(h('p', { class: 'state-msg', 'data-state': 'empty' }, 'No scheduled doses in this period yet.'));
      return;
    }
    const plotW = width - MARGIN.left - MARGIN.right;
    const plotH = HEIGHT - MARGIN.top - MARGIN.bottom;
    const band = plotW / rows.length;
    const barW = Math.max(4, Math.min(MAX_BAR, Math.floor(band * 0.6)));
    const every = labelEvery(band);
    const labelled = directLabelIndexes(rows);
    const yOf = (pct) => MARGIN.top + plotH - (pct / 100) * plotH;

    const svg = s('svg', {
      class: 'chart',
      width,
      height: HEIGHT,
      viewBox: `0 0 ${width} ${HEIGHT}`,
      role: 'group',
      'aria-label': `${label}. Use the arrow keys to move between days.`,
    });

    for (const t of TICKS) {
      const y = Math.round(yOf(t)) + 0.5;
      if (t > 0) svg.append(s('line', { class: 'chart-grid', x1: MARGIN.left, x2: width - MARGIN.right, y1: y, y2: y }));
      svg.append(s('text', { class: 'chart-tick', x: MARGIN.left - 8, y, text: `${t}%` }));
    }

    rows.forEach((r, i) => {
      const left = MARGIN.left + band * i;
      const cx = left + band / 2;
      const barTop = r.pct === null ? yOf(0) : yOf(r.pct);
      const col = s('g', {
        class: 'col',
        role: 'img',
        tabindex: i === rows.length - 1 ? '0' : '-1',
        'aria-label': describeRow(r),
      });
      col.append(s('rect', { class: 'col-hit', x: round(left), y: MARGIN.top - 6, width: round(band), height: plotH + 6 }));
      if (r.pct !== null && r.pct > 0) {
        col.append(s('path', { class: 'col-bar', d: columnPath(cx - barW / 2, barTop, barW, (r.pct / 100) * plotH) }));
      }
      col.append(s('rect', {
        class: 'col-focus', x: round(left + 2), y: MARGIN.top - 8, width: round(Math.max(4, band - 4)), height: plotH + 10, rx: 6,
      }));
      if (labelled.has(i) && r.pct !== null) {
        col.append(s('text', { class: 'chart-cap', x: round(cx), y: round(barTop - 8), text: `${Math.round(r.pct)}%` }));
      }
      col.addEventListener('pointerenter', () => showTip(i, barTop));
      col.addEventListener('pointermove', () => showTip(i, barTop));
      col.addEventListener('pointerleave', hideTip);
      col.addEventListener('focus', () => showTip(i, barTop));
      col.addEventListener('blur', hideTip);
      cols.push(col);
      svg.append(col);

      if ((rows.length - 1 - i) % every === 0) {
        svg.append(s('text', { class: 'chart-x', x: round(cx), y: MARGIN.top + plotH + 22 },
          s('tspan', { x: round(cx), text: shortDay(r.date) }),
          s('tspan', { class: 'chart-x-sub', x: round(cx), dy: '1.3em', text: r.scheduled ? `${r.taken}/${r.scheduled}` : '–' }),
        ));
      }
    });

    const baseY = Math.round(yOf(0)) + 0.5;
    svg.append(s('line', { class: 'chart-baseline', x1: MARGIN.left, x2: width - MARGIN.right, y1: baseY, y2: baseY }));

    svg.addEventListener('keydown', (e) => {
      const index = cols.indexOf(document.activeElement);
      if (index < 0) return;
      const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: cols.length - 1 };
      if (e.key in moves) {
        e.preventDefault();
        focusCol(moves[e.key]);
      } else if (e.key === 'Escape') {
        hideTip();
      }
    });
    svg.addEventListener('pointerleave', hideTip);
    plot.replaceChildren(svg);
  }

  let observer = null;
  if (typeof ResizeObserver === 'function') {
    observer = new ResizeObserver(() => {
      if (rows.length && Math.abs((host.clientWidth || 0) - lastWidth) > 8) draw();
    });
    observer.observe(host);
  }

  return {
    update(byDay) {
      rows = chartRows(byDay);
      draw();
    },
    destroy() {
      observer?.disconnect();
    },
  };
}

/** Data-table twin of the chart (the WCAG-clean equivalent). */
export function adherenceTable(byDay, { caption = 'Adherence by day' } = {}) {
  const rows = chartRows(byDay);
  const head = h('tr', {},
    h('th', { scope: 'col' }, 'Day'),
    h('th', { scope: 'col', class: 'num' }, 'Scheduled'),
    h('th', { scope: 'col', class: 'num' }, 'Taken'),
    h('th', { scope: 'col', class: 'num' }, 'Missed'),
    h('th', { scope: 'col', class: 'num' }, 'Adherence'),
  );
  const body = rows.map((r) => h('tr', {},
    h('th', { scope: 'row' }, formatLongDate(r.date)),
    h('td', { class: 'num' }, String(r.scheduled)),
    h('td', { class: 'num' }, String(r.taken)),
    h('td', { class: 'num' }, String(r.missed)),
    h('td', { class: 'num' }, r.pct === null ? '–' : `${Math.round(r.pct)}%`),
  ));
  return h('div', { class: 'table-wrap' },
    h('table', {}, h('caption', {}, caption), h('thead', {}, head), h('tbody', {}, body)));
}
