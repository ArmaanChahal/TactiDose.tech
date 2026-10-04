/**
 * Care portal controller (care.html) for doctor and family accounts.
 *
 * Left: linked patients (GET /api/care/patients) and "Link a patient" (POST /api/care/links
 * with the patient's ID + link code). Right: the selected patient in ARIA tabs —
 * Overview, Schedule, Containers, Cooldown, Medications, Conversations, History, Reports,
 * Notifications, Device. Only these accounts may change schedules, the cooldown,
 * containers/refills and medications; every change goes through the documented API and
 * the server re-checks the permission.
 */

import { del, get, post } from './api.js';
import { EventStream, RECONNECTED } from './events.js';
import { byId, confirmDialog, debounce, errorText, h, replaceChildren } from './dom.js';
import { icon } from './icons.js';
import { createPrefs } from './prefs.js';
import { requireSession, roleName } from './session.js';
import { initPortal, preparePage, showPageError } from './portal.js';
import { initTabs } from './tabs.js';
import { advanceLocalIso, formatPercent, plural } from './format.js';
import { dropWhen, statusOffset, todayKey } from './status.js';
import { linkBody, linkErrorText } from './links.js';
import { renderNotificationList } from './notifications.js';
import { createReports } from './reports.js';
import { dropStatusInfo } from './words.js';
import { lazyPanel } from './care/panel.js';
import { createOverview } from './care/overview.js';
import { createScheduleTab } from './care/schedule.js';
import { createContainersTab } from './care/containers.js';
import { createSettingsTab } from './care/settings.js';
import { createMedicationsTab } from './care/medications.js';
import { createConversationsTab } from './care/conversations.js';
import { createHistoryTab } from './care/history.js';
import { createDeviceTab } from './care/device.js';

const PATIENT_KEY = 'tactidose.care.patient';

preparePage();

const prefs = createPrefs();
const stream = new EventStream();

const ctx = {
  me: null,
  pid: null,
  patient: null,
  patients: [],
  status: null,
  statusAt: 0,
  offset: null,
  stream,
  notify: () => {},
  getOffset: () => ctx.offset,
  elapsedS: () => (performance.now() - ctx.statusAt) / 1000,
  getNow: () => (ctx.status?.now_local ? advanceLocalIso(ctx.status.now_local, ctx.elapsedS()) : null),
  getToday: () => todayKey(ctx.status),
  medsPromise: null,
  /** Shared, cached GET …/medications for the Schedule, Containers and Medications tabs. */
  medications(force = false) {
    if (force || !ctx.medsPromise) {
      const pid = ctx.pid;
      ctx.medsPromise = get(`/api/patients/${pid}/medications`).catch((err) => {
        ctx.medsPromise = null;
        throw err;
      });
    }
    return ctx.medsPromise;
  },
  invalidateMedications() {
    ctx.medsPromise = null;
  },
  async loadStatus() {
    const pid = ctx.pid;
    const status = await get(`/api/patients/${pid}/status`);
    if (pid === ctx.pid) {
      ctx.status = status;
      ctx.statusAt = performance.now();
      ctx.offset = statusOffset(status);
    }
    return status;
  },
  onChanged() {
    refreshPatientsSoon();
    modules.overview.markStale();
  },
  goTo(tab) {
    tabs?.select(tab);
  },
};

let tabs = null;
let notifications = null;
const modules = {};

// ------------------------------------------------------------------ patients

function relationshipText(p) {
  if (p.relationship === 'doctor') return 'You are their doctor';
  if (p.relationship === 'family') return 'You are family';
  return `Linked as ${roleName(ctx.me?.user?.role)}`;
}

function patientMeta(p) {
  const parts = [`Patient ID ${p.patient_id}`];
  if (p.adherence_7d !== null && p.adherence_7d !== undefined) parts.push(`adherence ${formatPercent(p.adherence_7d)} (7 days)`);
  const d = p.last_drop;
  if (d) {
    const when = dropWhen(d, ctx.getNow(), ctx.getOffset());
    parts.push(d.status === 'DROPPED' ? `last pill ${when}` : `last request ${when}: ${dropStatusInfo(d.status).word.toLowerCase()}`);
  }
  return parts.join(' · ');
}

function renderPatients() {
  const list = byId('patient-list');
  if (!ctx.patients.length) {
    replaceChildren(list, h('li', { class: 'state-msg', 'data-state': 'empty' }, 'No linked patients yet.'));
    return;
  }
  replaceChildren(list, ctx.patients.map((p) => {
    const selected = p.patient_id === ctx.pid;
    const unread = Number(p.unread_alerts) || 0;
    return h('li', {},
      h('button', {
        type: 'button',
        class: `patient-btn${selected ? ' is-selected' : ''}`,
        'aria-current': selected ? 'true' : null,
        on: { click: () => select(p.patient_id, { focus: true }) },
      },
      h('span', { class: 'patient-btn-name' }, p.display_name),
      h('span', { class: 'patient-btn-meta' }, patientMeta(p)),
      unread ? h('span', { class: 'badge tone-caution' }, icon('bell'), plural(unread, 'unread alert')) : null));
  }));
}

async function loadPatients() {
  try {
    const list = await get('/api/care/patients');
    ctx.patients = Array.isArray(list) ? list : [];
  } catch (err) {
    ctx.notify(`Could not load your patients: ${errorText(err)}`, 'error');
    return;
  }
  if (!ctx.patients.length) {
    ctx.pid = null;
    ctx.patient = null;
    renderPatients();
    byId('patient-area').hidden = true;
    byId('no-patient').hidden = false;
    byId('link-box').open = true;
    return;
  }
  byId('no-patient').hidden = true;
  const current = ctx.patients.find((p) => p.patient_id === ctx.pid);
  if (current) {
    ctx.patient = current;
    renderPatients();
    return;
  }
  let remembered = null;
  try {
    remembered = Number(localStorage.getItem(PATIENT_KEY));
  } catch {
    remembered = null;
  }
  const pick = ctx.patients.find((p) => p.patient_id === remembered) || ctx.patients[0];
  select(pick.patient_id);
}

const refreshPatientsSoon = debounce(loadPatients, 1000);

function select(pid, { focus = false } = {}) {
  const p = ctx.patients.find((x) => x.patient_id === pid);
  if (!p) return;
  const changed = ctx.pid !== pid;
  ctx.pid = pid;
  ctx.patient = p;
  try {
    localStorage.setItem(PATIENT_KEY, String(pid));
  } catch {
    /* remembered for this visit only */
  }
  byId('patient-area').hidden = false;
  byId('no-patient').hidden = true;
  byId('patient-name').textContent = p.display_name;
  byId('patient-meta').textContent = `Patient ID ${pid} · ${relationshipText(p)}`;
  renderPatients();
  if (changed) {
    ctx.status = null;
    ctx.offset = null;
    ctx.invalidateMedications();
    for (const m of Object.values(modules)) m.reset();
  }
  modules[tabs.current]?.show();
  if (focus) byId('patient-name').focus();
}

// ------------------------------------------------------------------ link / unlink

function showLinkError(message) {
  const el = byId('link-error');
  el.textContent = message || '';
  el.hidden = !message;
}

byId('link-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const check = linkBody(byId('link-pid').value, byId('link-code').value);
  if (!check.ok) {
    showLinkError(check.error);
    byId(check.field === 'patient_id' ? 'link-pid' : 'link-code').focus();
    return;
  }
  showLinkError('');
  const submit = byId('link-submit');
  submit.disabled = true;
  try {
    const linked = await post('/api/care/links', check.body);
    byId('link-form').reset();
    byId('link-box').open = false;
    ctx.notify(`Linked to ${linked?.display_name || `patient ${check.body.patient_id}`}.`, 'success');
    await loadPatients();
    select(check.body.patient_id, { focus: true });
  } catch (err) {
    showLinkError(linkErrorText(err));
  } finally {
    submit.disabled = false;
  }
});

byId('no-patient-link').addEventListener('click', () => {
  byId('link-box').open = true;
  byId('link-pid').focus();
});

byId('unlink-btn').addEventListener('click', async () => {
  const p = ctx.patient;
  if (!p) return;
  const { ok } = await confirmDialog({
    title: `Stop following ${p.display_name}?`,
    message: 'You will no longer see their pills or get their notifications. The patient can share their link code again later.',
    confirmLabel: 'Stop following',
    danger: true,
  });
  if (!ok) return;
  try {
    await del(`/api/care/links/${p.patient_id}`);
    ctx.notify(`You no longer follow ${p.display_name}.`, 'success');
    ctx.pid = null;
    for (const m of Object.values(modules)) m.reset();
    await loadPatients();
  } catch (err) {
    ctx.notify(errorText(err), 'error');
  }
});

// ------------------------------------------------------------------ per-patient notifications tab

function createPatientNotificationsTab() {
  const list = byId('pn-list');
  let visible = false;
  const mine = () => (notifications?.items || []).filter((n) => Number(n.patient_id) === Number(ctx.pid));
  function render() {
    if (!visible) return;
    renderNotificationList(list, mine(), {
      onRead: (ids) => notifications.markRead(ids),
      offsetMin: ctx.getOffset(),
      nowLocal: ctx.getNow(),
      empty: 'No notifications about this patient.',
    });
  }
  byId('pn-mark-all').addEventListener('click', () => {
    const ids = mine().filter((n) => !n.read_at).map((n) => n.notification_id);
    if (ids.length) notifications.markRead(ids);
  });
  return {
    show() {
      visible = true;
      render();
    },
    hide() {
      visible = false;
    },
    markStale: render,
    reset() {
      list.replaceChildren();
    },
    render,
  };
}

// ------------------------------------------------------------------ start

async function start() {
  let me;
  try {
    me = await requireSession({ roles: ['doctor', 'family'] });
  } catch (err) {
    showPageError(err, () => window.location.reload());
    return;
  }
  if (!me) return;
  ctx.me = me;

  const portal = initPortal({
    me,
    stream,
    prefs,
    getOffset: ctx.getOffset,
    getNow: ctx.getNow,
    patientName: (id) => ctx.patients.find((p) => p.patient_id === Number(id))?.display_name || null,
    onNotificationsChange: () => modules.notifications?.render(),
  });
  ctx.notify = portal.notify;
  notifications = portal.notifications;

  const reports = createReports(byId('cg-reports-root'), {
    getPatientId: () => ctx.pid,
    audience: 'caregiver',
    notify: ctx.notify,
    getOffset: ctx.getOffset,
    getNow: ctx.getNow,
    stream,
  });
  const reportsPanel = lazyPanel(() => reports.load());

  Object.assign(modules, {
    overview: createOverview(ctx),
    schedule: createScheduleTab(ctx),
    containers: createContainersTab(ctx),
    settings: createSettingsTab(ctx),
    medications: createMedicationsTab(ctx),
    conversations: createConversationsTab(ctx),
    history: createHistoryTab(ctx),
    reports: {
      show: reportsPanel.show,
      hide: reportsPanel.hide,
      markStale: reportsPanel.markStale,
      reset() {
        reports.reset();
        reportsPanel.reset();
      },
    },
    notifications: createPatientNotificationsTab(),
    device: createDeviceTab(ctx),
  });

  // A link from the sign-in page ("#patient-12") picks that patient first.
  const linked = /^#patient-(\d+)$/.exec(window.location.hash);
  if (linked) {
    try {
      localStorage.setItem(PATIENT_KEY, linked[1]);
    } catch {
      /* ignore */
    }
    window.history.replaceState(null, '', '#overview');
  }

  tabs = initTabs(document.querySelector('[role="tablist"]'), {
    onSelect: (current, previous) => {
      if (previous) modules[previous]?.hide();
      if (ctx.pid) modules[current]?.show();
    },
  });

  const isMine = (d) => d && d.patient_id !== undefined && Number(d.patient_id) === Number(ctx.pid);
  stream.on('patient.status', (d) => {
    refreshPatientsSoon();
    if (!isMine(d)) return;
    for (const name of ['overview', 'schedule', 'containers', 'settings', 'medications']) modules[name].markStale();
  });
  stream.on('drop.updated', (d) => {
    refreshPatientsSoon();
    if (!isMine(d)) return;
    modules.overview.markStale();
    modules.history.markStale();
    modules.containers.markStale();
  });
  stream.on('agent.message', (d) => {
    if (isMine(d)) modules.conversations.markStale();
  });
  stream.on('device.state', (d) => {
    modules.device.update(d);
    if (ctx.status && d && typeof d === 'object') {
      ctx.status = { ...ctx.status, device: d };
      modules.overview.updateDevice(ctx.status);
    }
  });
  stream.on('notification', () => refreshPatientsSoon());
  stream.on(RECONNECTED, () => {
    loadPatients();
    notifications.load();
    for (const m of Object.values(modules)) m.markStale();
  });

  stream.start();
  await loadPatients();
  notifications.load();
}

start();
