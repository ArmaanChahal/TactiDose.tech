/**
 * Open lid / Close lid buttons for the Wi-Fi ESP32 dispenser (POST /api/device/lid), shared by
 * the patient's Home and the care portal's Device tab. Shown only when the device has a lid
 * (GET /api/device -> lid_supported).
 *
 * Patient only (``onDispense`` given): "Dispense pill 1 / 2 / 3" buttons. They hand the container
 * number to the page, which asks for it like the Drop pill buttons (POST /api/patients/{pid}/drops):
 * the drop rules decide, then the server opens the lid, dispenses and closes the lid 5 s later.
 */

import { get, post } from './api.js';
import { errorText, h, replaceChildren } from './dom.js';
import { icon } from './icons.js';

const LID_WORDS = Object.freeze({ open: 'Lid is open', closed: 'Lid is closed' });

/**
 * Mount in `root` (hidden until the device reports a lid). Options: notify(message, kind),
 * onDispense(containerNumber) (patient only: adds the Dispense pill buttons).
 */
export function createLidControls(root, { notify = () => {}, onDispense = null } = {}) {
  const openBtn = h('button', { type: 'button', class: 'btn' }, icon('open'), 'Open lid');
  const closeBtn = h('button', { type: 'button', class: 'btn' }, icon('gate'), 'Close lid');
  const stateLine = h('p', { class: 'status-note', role: 'status' }, '');
  const dispenseRow = h('div', { class: 'btn-row lid-dispense', hidden: true });
  replaceChildren(root, h('div', { class: 'btn-row' }, openBtn, closeBtn), dispenseRow, stateLine);
  root.hidden = true;
  let dispenseCount = 0;

  function renderDispense(count) {
    if (!onDispense || count === dispenseCount) return;
    dispenseCount = count;
    dispenseRow.hidden = count < 1;
    replaceChildren(dispenseRow, Array.from({ length: count }, (_, i) => h('button', {
      type: 'button',
      class: 'btn btn-primary',
      'aria-label': `Dispense pill ${i + 1}: opens the lid, drops a pill from container ${i + 1}, closes the lid after 5 seconds`,
      on: { click: () => onDispense(i + 1) },
    }, icon('pill'), `Dispense pill ${i + 1}`)));
  }

  function render(device) {
    root.hidden = !device?.lid_supported;
    if (device?.lid_supported) renderDispense(Number(device.num_slots_reported) || 3);
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
