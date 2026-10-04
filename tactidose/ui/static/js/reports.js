/**
 * PDF reports shared by both portals (ARCHITECTURE §8): make a report for the last N
 * days (POST /api/patients/{pid}/reports), list them, view the PDF in the page (with a
 * download link) plus its summary as text (PDFs are hard for screen readers), and
 * send it to the doctor (POST /api/reports/{rid}/send) with the delivery status.
 * Without SMTP the server saves the email as a file: status SAVED.
 */

import { get, post, LONG_TIMEOUT_MS } from './api.js';
import { debounce, emptyState, errorState, errorText, h, replaceChildren, setLoading, uid } from './dom.js';
import { icon } from './icons.js';
import { formatBytes, formatDateTimeDevice, formatPercent, formatWhen, plural } from './format.js';
import { deliveryInfo } from './words.js';

export const REPORT_DAYS = Object.freeze([1, 3, 7, 14, 30, 60, 90]);

/** One sentence per delivery attempt. */
export function deliveryText(d) {
  const info = deliveryInfo(d?.status);
  const to = d?.to_email ? ` to ${d.to_email}` : '';
  if (d?.status === 'SENT') return `Sent${to}.`;
  if (d?.status === 'SAVED') return `Saved as an email file${to}. Email sending is not set up on this server, so the message was stored for sending later.`;
  if (d?.status === 'FAILED') return `Not sent${to}${d.error ? `: ${d.error}` : '.'}`;
  return `${info.word}${to}.`;
}

/** Key numbers from ReportMeta.stats when present (the shape is owned by the reports module). */
export function statsHighlights(stats) {
  if (!stats || typeof stats !== 'object') return [];
  const out = [];
  const num = (k) => (Number.isFinite(Number(stats[k])) ? Number(stats[k]) : null);
  if (stats.adherence_rate !== undefined && stats.adherence_rate !== null) out.push(`Adherence ${formatPercent(stats.adherence_rate)}`);
  const pairs = [
    ['scheduled_doses', 'scheduled dose'], ['scheduled', 'scheduled dose'], ['dropped', 'pill dropped', 'pills dropped'],
    ['missed', 'missed dose'], ['manual_drops', 'drop by button', 'drops by button'], ['agent_drops', 'drop by the assistant', 'drops by the assistant'],
    ['uncertain_drops', 'uncertain drop'],
  ];
  const used = new Set();
  for (const [key, one, many] of pairs) {
    const v = num(key);
    if (v === null || used.has(one)) continue;
    used.add(one);
    out.push(plural(v, one, many));
  }
  return out.slice(0, 6);
}

function narrativeSourceText(source) {
  if (source === 'gemini') return 'Summary written with AI from the conversations (check it against the details).';
  if (source === 'rules') return 'Summary written automatically from the records.';
  return null;
}

/**
 * Mount the reports view in `root`. Options: getPatientId(), audience 'patient' |
 * 'caregiver', notify(message, kind), getOffset(), getNow(), stream (EventStream).
 */
export function createReports(root, {
  getPatientId,
  audience = 'patient',
  notify = () => {},
  getOffset = () => null,
  getNow = () => null,
  stream = null,
} = {}) {
  const daysId = uid('rep-days');
  const emailId = uid('rep-email');
  const daysSelect = h('select', { id: daysId }, REPORT_DAYS.map((d) => h('option', { value: String(d), selected: d === 7 }, d === 1 ? 'The last day' : `The last ${d} days`)));
  const makeBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, icon('file'), 'Make a report');
  const status = h('p', { class: 'report-status', role: 'status' });
  const form = h('form', { class: 'report-form', novalidate: true },
    h('div', { class: 'field' }, h('label', { for: daysId }, 'Period'), daysSelect),
    h('div', { class: 'form-actions' }, makeBtn));
  const emailInput = audience === 'caregiver'
    ? h('input', { id: emailId, type: 'email', autocomplete: 'email', placeholder: 'Leave empty for the linked doctors' })
    : null;
  const emailField = emailInput
    ? h('div', { class: 'field' }, h('label', { for: emailId }, 'Send reports to (optional)'), emailInput,
      h('p', { class: 'field-hint' }, 'Empty: every linked doctor gets it.'))
    : null;
  const listTitleId = uid('rep-list');
  const list = h('div', { class: 'report-list' });
  const viewerTitle = h('h3', { tabindex: '-1' }, 'Report');
  const viewerSummary = h('div', { class: 'report-summary' });
  const viewerFrameSlot = h('div', {});
  const viewerLinks = h('p', { class: 'btn-row' });
  const viewer = h('section', { class: 'report-viewer card', hidden: true },
    h('div', { class: 'panel-head' }, viewerTitle,
      h('button', { type: 'button', class: 'btn btn-small', on: { click: () => closeViewer() } }, 'Close report')),
    viewerSummary, viewerLinks, viewerFrameSlot);
  replaceChildren(root, form, emailField, status,
    h('h3', { id: listTitleId }, audience === 'caregiver' ? 'Reports' : 'Your reports'), list, viewer);

  let reports = [];
  let openId = null;
  let seq = 0;

  async function load() {
    const pid = getPatientId();
    if (!pid) return;
    const token = ++seq;
    setLoading(list, true);
    try {
      const items = await get(`/api/patients/${pid}/reports`);
      if (token !== seq) return;
      reports = Array.isArray(items) ? items : [];
      render();
    } catch (err) {
      if (token !== seq) return;
      replaceChildren(list, errorState(err, load, icon('warning')));
    } finally {
      if (token === seq) setLoading(list, false);
    }
  }

  const loadSoon = debounce(load, 400);

  function render() {
    if (!reports.length) {
      replaceChildren(list, emptyState('No reports yet. Choose a period and press "Make a report".'));
      return;
    }
    replaceChildren(list, h('ul', { class: 'item-list', 'aria-labelledby': listTitleId }, reports.map(item)));
  }

  function item(r) {
    const offset = getOffset();
    const deliveries = Array.isArray(r.deliveries) ? r.deliveries : [];
    const failed = r.status && r.status !== 'READY';
    const facts = [
      r.period_start && r.period_end ? `Covers ${formatDateTimeDevice(r.period_start, offset)} to ${formatDateTimeDevice(r.period_end, offset)}` : null,
      r.created_at ? `made ${formatWhen(r.created_at, getNow(), offset)}` : null,
      r.pdf_size ? formatBytes(r.pdf_size) : null,
    ].filter(Boolean).join(', ');
    const highlights = statsHighlights(r.stats);
    const deliveryList = deliveries.length
      ? h('ul', { class: 'delivery-list' }, deliveries.map((d) => {
        const info = deliveryInfo(d.status);
        return h('li', {}, h('span', { class: `badge tone-${info.tone}` }, icon(info.icon), info.word), ' ', deliveryText(d));
      }))
      : null;
    const sendBtn = h('button', { type: 'button', class: 'btn', disabled: failed, on: { click: () => send(r, sendBtn) } },
      icon('mail'), audience === 'caregiver' ? 'Send to the doctor' : 'Send to my doctor');
    return h('li', { class: 'item' },
      h('div', { class: 'item-head' },
        h('p', { class: 'item-title' }, r.title || `Report ${r.report_id}`),
        failed ? h('span', { class: 'badge tone-bad' }, icon('x-circle'), 'Could not be made') : null),
      facts ? h('p', { class: 'item-body' }, `${facts}.`) : null,
      highlights.length ? h('p', { class: 'item-body' }, highlights.join(' · ')) : null,
      failed ? null : h('div', { class: 'item-actions' },
        h('button', { type: 'button', class: 'btn btn-primary', 'aria-label': `View ${r.title || 'report'}`, on: { click: () => openViewer(r) } }, icon('eye'), 'View'),
        h('a', { class: 'btn', href: `/api/reports/${r.report_id}/pdf?download=1`, download: `tactidose-report-${r.report_id}.pdf` }, icon('download'), 'Download PDF'),
        sendBtn),
      deliveryList);
  }

  async function generate(e) {
    e.preventDefault();
    const pid = getPatientId();
    const days = Number(daysSelect.value) || 7;
    makeBtn.disabled = true;
    status.textContent = `Making a report for ${days === 1 ? 'the last day' : `the last ${days} days`}… this can take a few seconds.`;
    try {
      const report = await post(`/api/patients/${pid}/reports`, { days }, { timeoutMs: LONG_TIMEOUT_MS });
      status.textContent = 'The report is ready.';
      notify('Your report is ready.', 'success');
      reports = [report, ...reports.filter((r) => r.report_id !== report.report_id)];
      render();
      openViewer(report);
    } catch (err) {
      status.textContent = `The report could not be made: ${errorText(err)}`;
    } finally {
      makeBtn.disabled = false;
    }
  }

  async function send(r, button) {
    const to = emailInput?.value.trim() || '';
    if (emailInput && to && !emailInput.checkValidity()) {
      status.textContent = 'That email address does not look right.';
      emailInput.focus();
      return;
    }
    button.disabled = true;
    status.textContent = 'Sending the report…';
    try {
      const resp = await post(`/api/reports/${r.report_id}/send`, to ? { to_email: to } : {}, { timeoutMs: LONG_TIMEOUT_MS });
      const deliveries = Array.isArray(resp?.deliveries) ? resp.deliveries : [];
      if (!deliveries.length) {
        status.textContent = 'There is no doctor linked yet, so the report was not sent to anyone.';
      } else {
        status.textContent = deliveries.map(deliveryText).join(' ');
        const anyFailed = deliveries.some((d) => d.status === 'FAILED');
        notify(anyFailed ? 'The report could not be sent to everyone.' : 'The report was sent.', anyFailed ? 'error' : 'success');
      }
      reports = reports.map((x) => (x.report_id === r.report_id
        ? { ...x, deliveries: [...deliveries, ...(Array.isArray(x.deliveries) ? x.deliveries : [])] }
        : x));
      render();
      refreshOne(r.report_id);
    } catch (err) {
      status.textContent = `The report was not sent: ${errorText(err)}`;
    } finally {
      button.disabled = false;
    }
  }

  async function refreshOne(rid) {
    try {
      const fresh = await get(`/api/reports/${rid}`);
      if (!fresh) return;
      reports = reports.map((x) => (x.report_id === rid ? fresh : x));
      render();
    } catch {
      /* the list keeps the delivery we just added */
    }
  }

  function openViewer(r) {
    openId = r.report_id;
    const src = `/api/reports/${r.report_id}/pdf`;
    viewerTitle.textContent = r.title || `Report ${r.report_id}`;
    const source = narrativeSourceText(r.narrative_source);
    replaceChildren(viewerSummary,
      r.narrative ? h('div', {}, h('h4', {}, 'Summary'), h('p', { class: 'report-narrative' }, r.narrative), source ? h('p', { class: 'muted' }, source) : null) : null,
      statsHighlights(r.stats).length ? h('p', {}, statsHighlights(r.stats).join(' · ')) : null);
    replaceChildren(viewerLinks,
      h('a', { class: 'btn', href: `/api/reports/${r.report_id}/pdf?download=1`, download: `tactidose-report-${r.report_id}.pdf` }, icon('download'), 'Download PDF'),
      h('a', { class: 'btn', href: src, target: '_blank', rel: 'noopener' }, icon('file'), 'Open the PDF in a new tab'));
    replaceChildren(viewerFrameSlot, h('iframe', { class: 'report-frame', src, title: `PDF: ${r.title || 'report'}` }));
    viewer.hidden = false;
    viewerTitle.focus();
  }

  function closeViewer() {
    openId = null;
    viewer.hidden = true;
    viewerFrameSlot.replaceChildren();
  }

  form.addEventListener('submit', generate);
  stream?.on('report.updated', (d, _env, meta) => {
    if (meta?.replayed) return;
    if (d?.patient_id !== undefined && Number(d.patient_id) !== Number(getPatientId())) return;
    loadSoon();
  });

  return {
    load,
    reset() {
      reports = [];
      closeViewer();
      list.replaceChildren();
      status.textContent = '';
    },
    get openReportId() {
      return openId;
    },
  };
}
