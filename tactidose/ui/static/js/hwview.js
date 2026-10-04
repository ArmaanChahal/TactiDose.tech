/**
 * Shared hardware views: DeviceSnapshot as words, and the result box for
 * {ok, result: CommandResultView, device} responses (care Device tab, demo console).
 */

import { h } from './dom.js';
import { icon } from './icons.js';
import { DASH, formatSeconds } from './format.js';
import { deviceStateInfo } from './words.js';

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
    ['Mode and port', [snap.mode, snap.port].filter(Boolean).join(' · ') || DASH],
    ['State', `${state.word} (${snap.state || 'UNKNOWN'})`],
    ['Start position found (homed)', yesNo(snap.homed)],
    ['Container at the chute', hasSlot ? `Container ${snap.slot + 1} (slot ${snap.slot})` : 'Unknown or between containers'],
    ['Moving to', hasTarget ? `Container ${snap.target_slot + 1} (slot ${snap.target_slot})` : DASH],
    ['Release gate', { OPEN: 'Open', CLOSED: 'Closed' }[snap.gate] || 'Unknown'],
    ['Ready to drop', yesNo(snap.ready_for_motion)],
    ['Command in progress', snap.in_flight || 'None'],
    ['Last error', snap.last_error || 'None'],
    ['Firmware', snap.fw_version || DASH],
    ['Protocol', snap.proto ? `v${snap.proto}${Number(snap.proto) >= 1.1 ? ' (pill drop supported)' : ''}` : 'v1 (drop is emulated)'],
    ['Drop sensor', yesNo(snap.drop_sensor, 'Present', 'None')],
    ['Containers reported', snap.num_slots_reported ?? DASH],
    ['Last message', snap.last_rx_age_s === null || snap.last_rx_age_s === undefined ? DASH : `${formatSeconds(snap.last_rx_age_s)} ago`],
    ['Restarts seen', snap.resets_seen ?? 0],
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
      lines.push(h('div', { class: 'form-error' }, icon('warning'), 'The release gate may be open — check the device.'));
    }
    if (Array.isArray(r.messages) && r.messages.length) lines.push(h('ol', { class: 'mono' }, r.messages.map((m) => h('li', {}, String(m)))));
  }
  return h('div', { class: `result-box ${ok ? 'is-ok' : 'is-fail'}` },
    h('strong', {}, icon(ok ? 'check-circle' : 'x-circle'), ` ${title}: ${ok ? 'OK' : 'failed'}`),
    lines);
}
