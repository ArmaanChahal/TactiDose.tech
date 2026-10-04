/**
 * Pure wording helpers for the demo panel (unit-tested under Node).
 */

/** Known simulator faults (hardware/simulator.py FAULT_NAMES); others are shown by name. */
export const FAULT_LABELS = Object.freeze({
  home_sensor_dead: ['Home sensor dead', 'homing never finds home: start-position timeout, then a fault'],
  motor_jam: ['Motor jam', 'moves never finish: motor fault'],
  unresponsive: ['Unresponsive', 'the device stops answering: timeouts'],
  brownout_on_gate: ['Restart when the gate opens', 'a brown-out resets the device mid-drop: uncertain drop'],
  disconnect: ['Disconnect', 'the USB link drops'],
});

/** [label, description] for a fault name, including names this page does not know yet. */
export function faultLabel(name) {
  const known = FAULT_LABELS[name];
  if (known) return known;
  const words = String(name).replace(/_/g, ' ');
  return [words.charAt(0).toUpperCase() + words.slice(1), ''];
}

/** One /api/health value in words (the shape of each entry is up to the server). */
export function healthValue(v) {
  if (v === true) return 'on';
  if (v === false) return 'off';
  if (v && typeof v === 'object') {
    if (v.configured === false) return 'not set up';
    return String(v.mode || v.provider || v.engine || v.status || (v.ok === false ? 'problem' : 'ok'));
  }
  return v === null || v === undefined ? 'unknown' : String(v);
}
