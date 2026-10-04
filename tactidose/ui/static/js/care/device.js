/**
 * Care portal "Device" tab: the device snapshot in words (GET /api/device, live
 * `device.state` events) and Home / Stop / Reconnect (doctor/family; Stop is always
 * allowed). The Wi-Fi ESP32's restocking lid is on the Containers tab.
 */

import { get, post } from '../api.js';
import { byId, errorState, errorText, h, replaceChildren } from '../dom.js';
import { icon } from '../icons.js';
import { commandResultBox, snapshotRows } from '../hwview.js';
import { lazyPanel } from './panel.js';

/** ctx: {notify(message, kind)} */
export function createDeviceTab(ctx) {
  const facts = byId('dev-facts');
  const result = byId('dev-result');
  const buttons = [byId('dev-home'), byId('dev-stop'), byId('dev-reconnect'), byId('dev-refresh')];

  function render(snap) {
    replaceChildren(facts, snapshotRows(snap).flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, String(v))]));
  }

  async function load() {
    try {
      render(await get('/api/device'));
    } catch (err) {
      replaceChildren(facts, errorState(err, load, icon('warning')));
    }
  }

  async function command(path, title) {
    for (const b of buttons) b.disabled = true;
    replaceChildren(result, h('p', { class: 'muted' }, `${title}…`));
    try {
      const resp = await post(path, {});
      if (resp?.result) replaceChildren(result, commandResultBox(title, resp));
      else replaceChildren(result, h('div', { class: `result-box ${resp?.ok ? 'is-ok' : 'is-fail'}` }, `${title}: ${resp?.ok ? 'done' : 'refused (a command is running, or there is no device)'}`));
      if (resp?.device) render(resp.device);
    } catch (err) {
      replaceChildren(result, errorState(err, null, icon('warning')));
      ctx.notify(errorText(err), 'error');
    } finally {
      for (const b of buttons) b.disabled = false;
    }
  }

  byId('dev-home').addEventListener('click', () => command('/api/device/home', 'Find start position'));
  byId('dev-stop').addEventListener('click', () => command('/api/device/stop', 'Stop'));
  byId('dev-reconnect').addEventListener('click', () => command('/api/device/reconnect', 'Reconnect'));
  byId('dev-refresh').addEventListener('click', load);

  const panel = lazyPanel(load);
  return {
    show: panel.show,
    hide: panel.hide,
    markStale: panel.markStale,
    reset: panel.reset,
    /** Live DeviceSnapshot from `device.state`. */
    update(snap) {
      if (snap && typeof snap === 'object') render(snap);
    },
  };
}
