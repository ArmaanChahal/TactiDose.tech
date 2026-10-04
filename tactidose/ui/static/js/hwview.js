/**
 * Shared hardware views: DeviceSnapshot as words, and the result box for
 * {ok, result: CommandResultView, device} responses (caregiver Device tab,
 * compartments loading mode, demo hardware console).
 */

import { h } from './dom.js';
import { icon } from './icons.js';
import { DASH, deviceStateInfo, formatSeconds } from './format.js';

const yesNo = (v, yes = 'Yes', no = 'No') => (v === true ? yes : v === false ? no : 'Unknown');

/** Rows of [label, value] describing a DeviceSnapshot in words. */
export function snapshotRows(snap) {
  if (!snap) return [];
  const state = deviceStateInfo(snap.state);
  const hasSlot = snap.slot !== null && snap.slot !== undefined;
  const hasTarget = snap.target_slot !== null && snap.target_slot !== undefined;
  return [
    ['Connection', snap.connected ? 'Connected' : 'Not connected'],
    ['Responding', yesNo(snap.responsive, 'Yes', 'No — not answering')],
    ['Mode / port', [snap.mode, snap.port].filter(Boolean).join(' · ') || DASH],
    ['State', `${state.word} (${snap.state || 'UNKNOWN'})`],
    ['Homed', yesNo(snap.homed)],
    ['At the gate', hasSlot ? `Compartment ${snap.slot + 1} (slot ${snap.slot})` : 'Unknown / between compartments'],
    ['Moving to', hasTarget ? `Compartment ${snap.target_slot + 1} (slot ${snap.target_slot})` : DASH],
    ['Gate', { OPEN: 'Open', CLOSED: 'Closed' }[snap.gate] || 'Unknown'],
    ['Ready for motion', yesNo(snap.ready_for_motion)],
    ['Command in flight', snap.in_flight || 'None'],
    ['Last error', snap.last_error || 'None'],
    ['Firmware', snap.fw_version || DASH],
    ['Slots reported', snap.num_slots_reported ?? DASH],
    ['Last message', snap.last_rx_age_s === null || snap.last_rx_age_s === undefined ? DASH : `${formatSeconds(snap.last_rx_age_s)} ago`],
    ['Resets seen', snap.resets_seen ?? 0],
  ];
}

/** One-line summary of a command response. */
export function describeCommand(resp) {
  const r = resp?.result;
  if (!r) return resp?.ok ? 'Done.' : 'The command failed.';
  const certainty = r.definitive === false ? 'uncertain' : 'definitive';
  return `${r.command || 'Command'} → ${r.ok ? 'OK' : 'failed'} (${r.code || 'no code'}, ${certainty})`;
}

/** Result box for {ok, result: CommandResultView, device}. */
export function commandResultBox(title, resp) {
  const r = resp?.result;
  const ok = r ? Boolean(r.ok) : Boolean(resp?.ok);
  const lines = [];
  if (r) {
    const elapsed = r.elapsed_s !== undefined && r.elapsed_s !== null ? ` in ${formatSeconds(r.elapsed_s)}` : '';
    lines.push(h('div', {}, `${r.command || title} → ${r.code || DASH}${r.definitive === false ? ' (uncertain outcome)' : ''}${elapsed}`));
    if (r.gate_may_be_open) {
      lines.push(h('div', { class: 'form-error' }, icon('warning'), 'The gate may be open — check the device before touching it.'));
    }
    if (Array.isArray(r.messages) && r.messages.length) lines.push(h('ol', {}, r.messages.map((m) => h('li', {}, String(m)))));
  }
  return h('div', { class: `result-box ${ok ? 'is-ok' : 'is-fail'}` },
    h('strong', {}, icon(ok ? 'check-circle' : 'x-circle'), ` ${title}: ${ok ? 'OK' : 'failed'}`),
    lines);
}
