/**
 * Restocking lid of the Wi-Fi ESP32 dispenser (POST /api/device/lid {state: open|close}) - care
 * portal, Containers tab, doctor/family only. Open the lid, put pills in, record the new counts
 * with Refill, close the lid. The lid has nothing to do with dispensing (js/dispense.js).
 * Shown only when the device has a lid (GET /api/device -> lid_supported).
 */

import { get, post } from './api.js';
import { errorText, h, replaceChildren } from './dom.js';
import { icon } from './icons.js';

const LID_WORDS = Object.freeze({ open: 'The lid is open: refill the containers, then close it.', closed: 'The lid is closed.' });

/** Mount in `root` (hidden until the device reports a lid). Options: notify(message, kind). */
export function createLidControls(root, { notify = () => {} } = {}) {
  const openBtn = h('button', { type: 'button', class: 'btn' }, icon('open'), 'Open lid to restock');
  const closeBtn = h('button', { type: 'button', class: 'btn' }, icon('gate'), 'Close lid');
  const stateLine = h('p', { class: 'status-note', role: 'status' }, '');
  replaceChildren(root,
    h('h3', {}, icon('archive'), ' Restock the dispenser'),
    h('p', { class: 'field-hint' }, 'Open the lid, put the pills in, use Refill on each container below to record the new count, then close the lid.'),
    h('div', { class: 'btn-row' }, openBtn, closeBtn),
    stateLine);
  root.hidden = true;

  function render(device) {
    root.hidden = !device?.lid_supported;
    stateLine.textContent = LID_WORDS[device?.lid] || (device?.connected === false ? 'The dispenser is offline.' : '');
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
      if (resp?.ok) notify(state === 'open' ? 'The lid is open for restocking.' : 'The lid is closed.', 'success');
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
