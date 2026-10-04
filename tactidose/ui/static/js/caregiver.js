/**
 * Caregiver setup page controller (caregiver.html).
 *
 * Owns the shared context (event stream, notifications, device clock, medication
 * cache) and routes live events to the tab modules in ./cg/. Each module loads its
 * data lazily when its tab is first shown and refreshes when it is marked stale.
 */

import { clearPin, get, hasPin } from './api.js';
import { EventStream, RECONNECTED } from './events.js';
import { byId, createNotifier, initLiveRegions } from './dom.js';
import { hydrateIcons, icon } from './icons.js';
import { initThemeToggle } from './theme.js';
import { initTabs } from './tabs.js';
import { bindConnIndicator } from './conn.js';
import { dateKey, deviceOffsetFrom } from './format.js';
import { createToday } from './cg/today.js';
import { createMedications } from './cg/medications.js';
import { createScan } from './cg/scan.js';
import { createCompartments } from './cg/compartments.js';
import { createSchedules } from './cg/schedules.js';
import { createDevice } from './cg/device.js';
import { createAnalytics } from './cg/analytics.js';

const NAME_KEY = 'tactidose.caregiverName';
const NOTICE_ICONS = { success: 'check-circle', error: 'warning', warning: 'warning', info: 'info' };

initLiveRegions();
hydrateIcons();
initThemeToggle(byId('theme-toggle'));

const nameInput = byId('caregiver-name');
try {
  nameInput.value = localStorage.getItem(NAME_KEY) || '';
} catch {
  /* storage unavailable */
}
nameInput.addEventListener('change', () => {
  try {
    localStorage.setItem(NAME_KEY, nameInput.value.trim());
  } catch {
    /* ignore */
  }
});

const notify = createNotifier(byId('notices'), { iconFor: (kind) => icon(NOTICE_ICONS[kind] || 'info') });
const stream = new EventStream();
bindConnIndicator(byId('conn'), stream);

let medsCache = null;
let medsRequest = null;

const ctx = {
  stream,
  notify,
  nowLocal: null,
  offsetMin: null,
  modules: {},
  /** Device-local "today" as YYYY-MM-DD (null until the clock is known). */
  get todayKey() {
    return this.nowLocal ? dateKey(this.nowLocal) : null;
  },
  caregiverName() {
    return nameInput.value.trim() || null;
  },
  setNow(nowLocal) {
    if (!nowLocal) return;
    this.nowLocal = nowLocal;
    this.offsetMin = deviceOffsetFrom(nowLocal);
  },
  async refreshClock() {
    try {
      const state = await get('/api/state', { timeoutMs: 5000 });
      this.setNow(state?.now_local || state?.due?.now_local);
    } catch {
      /* clock stays unknown: times fall back to their own offsets */
    }
  },
  /** Active, confirmed medications (cached; force=true refetches). */
  async loadMedications({ force = false } = {}) {
    if (medsCache && !force) return medsCache;
    if (!medsRequest) {
      medsRequest = get('/api/medications?include_inactive=false')
        .then((list) => {
          medsCache = Array.isArray(list) ? list : [];
          return medsCache;
        })
        .finally(() => {
          medsRequest = null;
        });
    }
    return medsRequest;
  },
  invalidateMedications() {
    medsCache = null;
  },
  goTo(tab) {
    tabs.select(tab);
  },
};

// ------------------------------------------------------------------ PIN control

const pinButton = byId('pin-forget');
const updatePinButton = () => {
  pinButton.hidden = !hasPin();
};
pinButton.addEventListener('click', () => {
  clearPin();
  notify('The caregiver PIN was forgotten on this browser.', 'info');
});
window.addEventListener('tactidose:pin', updatePinButton);
updatePinButton();

// ------------------------------------------------------------------ modules + tabs

const modules = {
  today: createToday(ctx),
  medications: createMedications(ctx),
  scan: createScan(ctx),
  compartments: createCompartments(ctx),
  schedules: createSchedules(ctx),
  device: createDevice(ctx),
  analytics: createAnalytics(ctx),
};
ctx.modules = modules;

const staleAll = () => Object.values(modules).forEach((m) => m.markStale());

// The device clock is needed for "today" and local times; load it before the first tab.
await ctx.refreshClock();

const tabs = initTabs(byId('tabs'), {
  onSelect(name, previous) {
    if (previous) modules[previous]?.hide();
    modules[name]?.show();
  },
});

// ------------------------------------------------------------------ live events

stream.on('dose.updated', (_d, _env, meta) => {
  if (meta.replayed) return;
  modules.today.markStale();
  modules.analytics.markStale();
});

stream.on('data.changed', (data, _env, meta) => {
  if (meta.replayed) return;
  switch (data?.entity) {
    case 'medication':
      ctx.invalidateMedications();
      ['medications', 'schedules', 'compartments', 'today'].forEach((m) => modules[m].markStale());
      break;
    case 'schedule':
      ['schedules', 'medications', 'today'].forEach((m) => modules[m].markStale());
      break;
    case 'compartment':
      ctx.invalidateMedications();
      ['compartments', 'medications'].forEach((m) => modules[m].markStale());
      break;
    case 'scan':
      modules.scan.markStale();
      break;
    default:
      ctx.invalidateMedications();
      staleAll();
  }
});

stream.on('device.state', (snapshot) => {
  modules.device.onDevice(snapshot);
  modules.compartments.onDevice(snapshot);
});

stream.on('device.line', (_data, envelope) => modules.device.onLine(envelope));

stream.on('clock.changed', (data, _env, meta) => {
  if (data?.now_local) ctx.setNow(data.now_local);
  if (meta.replayed) return;
  modules.today.onClock();
  modules.analytics.markStale();
});

stream.on('system.notice', (data, _env, meta) => {
  if (meta.replayed || !data?.message) return;
  const kind = data.level === 'error' ? 'error' : data.level === 'warning' ? 'warning' : 'info';
  notify(data.message, kind);
});

stream.on(RECONNECTED, () => {
  ctx.invalidateMedications();
  ctx.refreshClock();
  staleAll();
});

stream.start();
