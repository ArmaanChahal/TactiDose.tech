/**
 * "Dispense pill 1 / 2 / 3" buttons for the Wi-Fi ESP32 dispenser - patient Home only. A button
 * hands its container number to the page, which asks for the pill exactly like the Drop pill
 * buttons (POST /api/patients/{pid}/drops): the drop rules decide, then the server calls the
 * ESP32's /dispense?pill=N. Dispensing never touches the lid (restocking: js/lid.js).
 * Shown only on the Wi-Fi dispenser (GET /api/device -> mode "wifi").
 */

import { get } from './api.js';
import { h, replaceChildren } from './dom.js';
import { icon } from './icons.js';

/** Mount in `root` (hidden unless the device is the Wi-Fi ESP32). onDispense(containerNumber). */
export function createDispenseButtons(root, { onDispense }) {
  let count = 0;
  root.hidden = true;

  function render(device) {
    const wifi = device?.mode === 'wifi';
    root.hidden = !wifi;
    const n = wifi ? Number(device.num_slots_reported) || 3 : 0;
    if (n === count) return;
    count = n;
    replaceChildren(root, h('div', { class: 'btn-row' }, Array.from({ length: n }, (_, i) => h('button', {
      type: 'button',
      class: 'btn btn-primary',
      'aria-label': `Dispense pill ${i + 1}: drop a pill from container ${i + 1}`,
      on: { click: () => onDispense(i + 1) },
    }, icon('pill'), `Dispense pill ${i + 1}`))));
  }

  async function load() {
    try {
      render(await get('/api/device'));
    } catch {
      root.hidden = true;
    }
  }

  load();
  return { load, render };
}
