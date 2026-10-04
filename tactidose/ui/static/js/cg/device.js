/**
 * Caregiver "Device" tab: DeviceSnapshot table (GET /api/hardware + device.state),
 * Home / Stop / Close gate / Reconnect, and the live serial log (device.line,
 * pre-filled from GET /api/log).
 */

import { get, post } from '../api.js';
import { byId, confirmDialog, errorState, errorText, h, replaceChildren } from '../dom.js';
import { icon } from '../icons.js';
import { createLineLog } from '../linelog.js';
import { commandResultBox, snapshotRows } from '../hwview.js';

export function createDevice(ctx) {
  const tableHost = byId('device-table');
  const result = byId('device-result');
  const log = createLineLog(byId('device-log'), { hideHeartbeat: byId('log-hide-hb').checked });
  const pauseBtn = byId('log-pause');
  let snapshot = null;
  let visible = false;
  let stale = true;
  let prefilled = false;

  byId('log-hide-hb').addEventListener('change', (e) => log.setHideHeartbeat(e.target.checked));
  byId('log-clear').addEventListener('click', () => log.clear());
  pauseBtn.addEventListener('click', () => {
    const paused = pauseBtn.getAttribute('aria-pressed') !== 'true';
    pauseBtn.setAttribute('aria-pressed', String(paused));
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
    log.setPaused(paused);
  });

  function renderSnapshot() {
    if (!snapshot) return;
    replaceChildren(tableHost, h('table', { class: 'kv' },
      h('caption', {}, 'Device status'),
      h('tbody', {}, snapshotRows(snapshot).map(([label, value]) => h('tr', {}, h('th', { scope: 'row' }, label), h('td', {}, String(value)))))));
  }

  async function load() {
    stale = false;
    try {
      snapshot = await get('/api/hardware');
      renderSnapshot();
    } catch (err) {
      replaceChildren(tableHost, errorState(err, load, icon('warning')));
    }
    if (!prefilled) {
      prefilled = true;
      try {
        const events = await get('/api/log?limit=100&topics=device.line');
        for (const ev of Array.isArray(events) ? events : []) log.add(ev);
      } catch {
        prefilled = false;
      }
    }
  }

  async function command(title, path, { confirm = null } = {}) {
    if (confirm) {
      const { ok } = await confirmDialog(confirm);
      if (!ok) return;
    }
    replaceChildren(result, h('p', { class: 'muted' }, `${title}…`));
    try {
      const resp = await post(path, {});
      if (resp?.device) {
        snapshot = resp.device;
        renderSnapshot();
      }
      replaceChildren(result, commandResultBox(title, resp));
    } catch (err) {
      replaceChildren(result, errorState(err, null, icon('warning')));
    }
  }

  byId('dev-home').addEventListener('click', () => command('Home', '/api/hardware/home', {
    confirm: {
      title: 'Home the carousel?',
      message: 'The carousel turns back to compartment 1 (the home position). Keep hands clear.',
      confirmLabel: 'Home now',
      iconEl: icon('hand'),
    },
  }));
  byId('dev-close-gate').addEventListener('click', () => command('Close gate', '/api/hardware/close-gate'));
  byId('dev-reconnect').addEventListener('click', () => command('Reconnect', '/api/hardware/reconnect'));
  byId('dev-stop').addEventListener('click', () => command('STOP', '/api/hardware/stop'));

  return {
    show() {
      visible = true;
      if (stale || !snapshot) load();
      else renderSnapshot();
    },
    hide() {
      visible = false;
    },
    markStale() {
      stale = true;
      if (visible) load();
    },
    onDevice(snap) {
      snapshot = snap;
      if (visible) renderSnapshot();
    },
    onLine(envelope) {
      log.add(envelope);
    },
  };
}
