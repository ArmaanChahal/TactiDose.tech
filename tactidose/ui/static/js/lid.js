/**
 * Open lid / Close lid buttons for the Wi-Fi ESP32 dispenser (POST /api/device/lid), shared by
 * the patient's Home and the care portal's Device tab. Shown only when the device has a lid
 * (GET /api/device -> lid_supported). Dispensing is not here: the Drop pill buttons go through
 * the drop rules (cooldown, double-dose guard) and then to the ESP32's /dispense endpoint.
 */

import { get, post } from './api.js';
import { errorText, h, replaceChildren } from './dom.js';
import { icon } from './icons.js';

const LID_WORDS = Object.freeze({ open: 'Lid is open', closed: 'Lid is closed' });

/** Mount in `root` (hidden until the device reports a lid). Options: notify(message, kind). */
export function createLidControls(root, { notify = () => {} } = {}) {
  const openBtn = h('button', { type: 'button', class: 'btn' }, icon('open'), 'Open lid');
  const closeBtn = h('button', { type: 'button', class: 'btn' }, icon('gate'), 'Close lid');
  const stateLine = h('p', { class: 'status-note', role: 'status' }, '');
  replaceChildren(root, h('div', { class: 'btn-row' }, openBtn, closeBtn), stateLine);
  root.hidden = true;

  function render(device) {
    root.hidden = !device?.lid_supported;
    stateLine.textContent = LID_WORDS[device?.lid] || (device?.connected === false ? 'Dispenser offline' : '');
  }

  async function load() {
    try {
      render(await get('/api/device'));
    } catch {
      root.hidden = true;
    }
  }

  async function setLid(state) {
    openBtn.disabled = closeBtn.disabled = true;
    stateLine.textContent = state === 'open' ? 'Opening the lid…' : 'Closing the lid…';
    try {
      const resp = await post('/api/device/lid', { state });
      render(resp?.device);
      if (resp?.ok) notify(state === 'open' ? 'The lid is open.' : 'The lid is closed.', 'success');
      else notify(`The dispenser did not confirm (${resp?.detail || 'no answer'}).`, 'error');
    } catch (err) {
      notify(`Could not reach the dispenser: ${errorText(err)}`, 'error');
      load();
    } finally {
      openBtn.disabled = closeBtn.disabled = false;
    }
  }

  openBtn.addEventListener('click', () => setLid('open'));
  closeBtn.addEventListener('click', () => setLid('close'));
  load();
  return { load, render };
}
