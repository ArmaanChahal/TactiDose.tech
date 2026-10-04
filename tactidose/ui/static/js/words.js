/**
 * Plain-language words for every status, reason and source the API sends
 * (db.models enums and hardware result codes). Pure data + lookups; unit-tested,
 * including a check that every enum value in tactidose/db/models.py has an entry.
 *
 * Every state is a word + an icon + a tone: colour is never the only signal.
 * Words are sentence case (no shouting capitals) and short.
 */

/** pill_drops.status (DropStatus). */
export const DROP_STATUS = Object.freeze({
  DROPPED: { word: 'Dropped', icon: 'check-circle', tone: 'good' },
  DENIED: { word: 'Not dropped', icon: 'slash', tone: 'caution' },
  FAILED: { word: 'Drop failed', icon: 'x-circle', tone: 'bad' },
  UNCERTAIN: { word: 'Not sure if it dropped', icon: 'help', tone: 'bad' },
});

/** DropOutcome.reason for DENIED (DenyReason). */
export const DENY_REASON = Object.freeze({
  COOLDOWN: 'Too soon after the last pill',
  EMPTY: 'The container is empty',
  NO_MEDICATION: 'No medication in that container',
  UNKNOWN_MEDICATION: 'That medication was not found',
  ALREADY_SATISFIED: 'This dose was already dropped',
  IN_PROGRESS: 'Another pill was dropping',
  DEVICE_UNAVAILABLE: 'The device was not ready',
  NEEDS_REVIEW: 'A caregiver must check an earlier drop first',
  NOT_ALLOWED: 'Not allowed for this account',
  DB_ERROR: 'The records could not be checked',
});

/** DropOutcome.reason for FAILED / UNCERTAIN (hardware and host codes). */
export const HARDWARE_REASON = Object.freeze({
  NO_PILL: 'No pill came out',
  NO_BUZZER: 'No buzzer is fitted',
  INVALID_SLOT: 'The device does not have that container',
  NOT_HOMED: 'The device needed to reset its position',
  BUSY: 'The device was busy',
  HOME_TIMEOUT: 'The device could not find its start position',
  MOTOR_FAULT: 'The motor had a problem',
  INVALID_STATE: 'The device was not ready',
  UNKNOWN_COMMAND: 'The device did not understand the request',
  STOPPED: 'The drop was stopped',
  TIMEOUT: 'The device did not answer in time',
  DISCONNECTED: 'The device disconnected',
  NOT_CONNECTED: 'The device is not connected',
  DEVICE_RESET: 'The device restarted',
  BUSY_LOCAL: 'Another command was running',
  INVALID_ARGUMENT: 'The request was not valid',
});

/** pill_drops.source (DropSource), worded for the patient and for caregivers. */
export const DROP_SOURCE = Object.freeze({
  schedule: { patient: 'Dropped automatically at its time', caregiver: 'Scheduled auto-drop' },
  manual: { patient: 'You pressed Drop pill', caregiver: 'Patient pressed Drop pill' },
  agent: { patient: 'You asked the assistant', caregiver: 'Requested through the assistant' },
  button: { patient: 'You pressed the device button', caregiver: 'Device button' },
  demo: { patient: 'Demo panel', caregiver: 'Demo panel' },
});

/** Notification.kind (NotificationKind). */
export const NOTIFICATION_KIND = Object.freeze({
  PILL_DROPPED: { word: 'Pill dropped', icon: 'check-circle', tone: 'good' },
  DROP_DENIED: { word: 'Pill not dropped', icon: 'slash', tone: 'caution' },
  DROP_FAILED: { word: 'Drop failed', icon: 'x-circle', tone: 'bad', urgent: true },
  DROP_UNCERTAIN: { word: 'Check the device', icon: 'help', tone: 'bad', urgent: true },
  LOW_STOCK: { word: 'Running low', icon: 'warning', tone: 'caution' },
  EMPTY: { word: 'Container empty', icon: 'warning', tone: 'bad', urgent: true },
  MISSED_DOSE: { word: 'Missed pill', icon: 'x-circle', tone: 'bad', urgent: true },
  REPORT_READY: { word: 'Report ready', icon: 'file', tone: 'info' },
  REPORT_SENT: { word: 'Report sent', icon: 'mail', tone: 'info' },
  DEVICE_ALERT: { word: 'Device problem', icon: 'warning', tone: 'bad', urgent: true },
  HEALTH_CONCERN: { word: 'Needs attention', icon: 'warning', tone: 'bad', urgent: true },
});

/** dose_events.status (DoseStatus). */
export const DOSE_STATUS = Object.freeze({
  SCHEDULED: { word: 'Coming up', icon: 'clock', tone: 'neutral' },
  DUE: { word: 'Due now', icon: 'bell', tone: 'due' },
  DISPENSING: { word: 'Dropping now', icon: 'rotate', tone: 'caution' },
  DISPENSED: { word: 'Dropped', icon: 'check-circle', tone: 'good' },
  TAKEN: { word: 'Taken', icon: 'check-circle', tone: 'good' },
  MISSED: { word: 'Missed', icon: 'x-circle', tone: 'bad' },
  CANCELLED: { word: 'Skipped', icon: 'slash', tone: 'neutral' },
  HARDWARE_ERROR: { word: 'Drop problem, trying again', icon: 'warning', tone: 'bad' },
});

/** Firmware states (DeviceSnapshot.state). */
export const DEVICE_STATE = Object.freeze({
  BOOT: { word: 'Starting up', icon: 'rotate', tone: 'caution' },
  HOMING: { word: 'Finding its start position', icon: 'rotate', tone: 'caution' },
  READY: { word: 'Ready', icon: 'check-circle', tone: 'good' },
  MOVING: { word: 'Moving', icon: 'rotate', tone: 'caution' },
  AT_TARGET: { word: 'Lining up a container', icon: 'rotate', tone: 'caution' },
  GATE_OPEN: { word: 'Releasing a pill', icon: 'open', tone: 'caution' },
  SAFE_STOP: { word: 'Stopped (will reset)', icon: 'stop', tone: 'caution' },
  FAULT: { word: 'Needs attention', icon: 'warning', tone: 'bad' },
  UNKNOWN: { word: 'Unknown', icon: 'help', tone: 'neutral' },
});

/** report_deliveries.status. */
export const DELIVERY_STATUS = Object.freeze({
  SENT: { word: 'Sent', icon: 'check-circle', tone: 'good' },
  SAVED: { word: 'Saved as an email file', icon: 'file', tone: 'info' },
  FAILED: { word: 'Not sent', icon: 'x-circle', tone: 'bad' },
});

const UNKNOWN_INFO = { word: 'Unknown', icon: 'help', tone: 'neutral' };

export function dropStatusInfo(status, needsReview = false) {
  const base = DROP_STATUS[status] || { ...UNKNOWN_INFO, word: status ? String(status) : 'Unknown' };
  if (status === 'UNCERTAIN' && needsReview) return { ...base, word: 'Not sure if it dropped — needs checking', needsReview: true };
  return { ...base, needsReview: Boolean(needsReview) };
}

/** Sentence for a DENIED reason or a hardware code; null when there is none. */
export function reasonText(reason) {
  if (!reason) return null;
  const code = String(reason).trim().split(/\s+/)[0].toUpperCase();
  return DENY_REASON[code] || HARDWARE_REASON[code] || `Reason: ${reason}`;
}

export function sourceText(source, audience = 'patient') {
  const entry = DROP_SOURCE[source];
  if (!entry) return source ? String(source) : '';
  return audience === 'caregiver' ? entry.caregiver : entry.patient;
}

export function notificationInfo(kind) {
  return NOTIFICATION_KIND[kind] || { word: 'Notice', icon: 'info', tone: 'info' };
}

export function doseStatusInfo(status, needsReview = false) {
  const base = DOSE_STATUS[status] || { ...UNKNOWN_INFO, word: status ? String(status) : 'Unknown' };
  if (needsReview) return { ...base, word: `${base.word} — needs checking`, tone: 'bad', needsReview: true };
  return { ...base, needsReview: false };
}

export function deviceStateInfo(state) {
  return DEVICE_STATE[state] || { ...UNKNOWN_INFO, word: state ? String(state) : 'Unknown' };
}

export function deliveryInfo(status) {
  return DELIVERY_STATUS[status] || { ...UNKNOWN_INFO, word: status ? String(status) : 'Unknown' };
}
