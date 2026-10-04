/**
 * PatientStatus (GET /api/patients/{pid}/status) -> short, plain sentences for the
 * portals and the kiosk. Pure functions, no DOM; unit-tested under Node.
 *
 * Deterministic rules live on the server: these helpers only describe what the server
 * said (cooldown remaining, next scheduled dose, container inventory). A "Drop pill"
 * press is always sent to the server, which has the final word.
 */

import {
  DASH,
  containerNumberOf,
  deviceOffsetFrom,
  deviceWall,
  formatClock,
  formatClockDevice,
  formatCountdown,
  formatWhen,
  plural,
  relativeDayWord,
} from './format.js';
import { deviceStateInfo, dropStatusInfo, reasonText, sourceText } from './words.js';

/** Device offset in minutes east of UTC, from PatientStatus.now_local. */
export function statusOffset(status) {
  return deviceOffsetFrom(status?.now_local);
}

export function containerNumber(c) {
  return containerNumberOf(c);
}

/** One container card: words for the medication, the pills left and the stock badge. */
export function containerView(c) {
  const number = containerNumber(c);
  const count = Number.isFinite(Number(c?.pill_count)) ? Math.max(0, Number(c.pill_count)) : 0;
  const hasMed = Boolean(c && c.medication_id !== null && c.medication_id !== undefined && c.medication_name);
  const empty = Boolean(c?.empty) || count <= 0;
  const low = !empty && Boolean(c?.low_stock);
  let badge = null;
  if (hasMed && empty) badge = { word: 'Empty', icon: 'warning', tone: 'bad', solid: 'danger' };
  else if (hasMed && low) badge = { word: 'Low', icon: 'warning', tone: 'caution', solid: 'caution' };
  const medName = hasMed ? c.medication_name : 'No medication';
  let countText;
  if (!hasMed) countText = 'Nothing assigned to this container';
  else if (empty) countText = 'No pills left';
  else countText = `${plural(count, 'pill')} left`;
  let blocked = null;
  if (!hasMed) blocked = 'There is no medication in this container.';
  else if (empty) blocked = 'This container is empty. Ask your caregiver to refill it.';
  return {
    slot: Number(c?.slot),
    number,
    title: `Container ${number ?? '?'}`,
    hasMed,
    medName,
    strength: hasMed && c.strength ? c.strength : null,
    count,
    capacity: Number.isFinite(Number(c?.capacity)) ? Number(c.capacity) : null,
    empty: hasMed && empty,
    low,
    badge,
    countText,
    badgeText: badge ? (badge.word === 'Low' ? `Low: ${plural(count, 'pill')} left` : 'Empty') : null,
    canDrop: hasMed && !empty,
    blocked,
    dropLabel: hasMed ? `Drop pill: ${c.medication_name}, container ${number}` : `Container ${number}: no medication`,
  };
}

/** Seconds of cooldown left `elapsedS` seconds after the status was fetched. */
export function remainingCooldown(status, elapsedS = 0) {
  const r = Number(status?.cooldown_remaining_s);
  if (!Number.isFinite(r) || r <= 0) return 0;
  return Math.max(0, r - Math.max(0, Number(elapsedS) || 0));
}

/**
 * Global cooldown banner. `remainingS` is the live remaining time; the "at" time comes
 * from next_manual_allowed_at in device time.
 */
export function cooldownView(status, remainingS = remainingCooldown(status)) {
  const offset = statusOffset(status);
  const minutes = Number(status?.cooldown_minutes) || 0;
  if (!(remainingS > 0)) {
    return {
      active: false,
      text: 'You can drop a pill now.',
      short: 'You can drop a pill now',
      rule: minutes > 0
        ? `After any pill drops, you wait ${plural(minutes, 'minute')} before you can drop another.`
        : 'There is no waiting time between pills.',
    };
  }
  const atIso = status?.next_manual_allowed_at;
  const at = atIso ? formatClockDevice(atIso, offset) : null;
  const countdown = formatCountdown(remainingS);
  const when = at && at !== DASH ? `at ${at} — ${countdown}` : countdown;
  return {
    active: true,
    text: `You can drop another pill ${when}.`,
    short: `Wait ${countdown.replace(/^in /, '')}`,
    at,
    countdown,
    rule: `After any pill drops, you wait ${plural(minutes, 'minute')} before you can drop another.`,
  };
}

function containerPhrase(number) {
  return number ? `container ${number}` : 'no container assigned';
}

/** "Vitamin C at 1:00 PM — container 2" (+ "tomorrow" when it is not today). */
export function nextPillText(status) {
  const next = status?.next_scheduled;
  if (!next) return 'No pills are scheduled.';
  const name = next.medication_name || 'Your pill';
  const iso = next.scheduled_local || next.scheduled_at;
  const offset = statusOffset(status);
  const day = relativeDayWord(iso, status?.now_local, offset);
  const time = next.scheduled_local ? formatClock(next.scheduled_local) : formatClockDevice(next.scheduled_at, offset);
  const number = containerNumberOf(next);
  let when;
  if (day === 'today') when = `at ${time}`;
  else if (day) when = `${day} at ${time}`;
  else when = formatWhen(iso, status?.now_local, offset);
  return `${name} ${when} — ${containerPhrase(number)}`;
}

/** When a PillDropView happened, in device time ("today at 8:00 AM"). */
export function dropWhen(drop, nowLocal = null, offsetMin = null) {
  if (!drop) return DASH;
  const offset = offsetMin ?? deviceOffsetFrom(drop.requested_local) ?? deviceOffsetFrom(nowLocal);
  const iso = drop.completed_at || drop.requested_local || drop.requested_at;
  return formatWhen(iso, nowLocal, offset);
}

/** "Last pill: Vitamin C, today at 8:00 AM. Dropped automatically at its time." */
export function lastDropText(status) {
  const drop = status?.last_drop;
  if (!drop) return 'No pills have dropped yet.';
  const offset = statusOffset(status);
  const name = drop.medication_name || `container ${drop.container_number ?? '?'}`;
  const when = dropWhen(drop, status?.now_local, offset);
  const how = sourceText(drop.source, 'patient');
  if (drop.status === 'DROPPED') return `Last pill: ${name}, ${when}. ${how}.`;
  if (drop.status === 'UNCERTAIN') return `Last drop: ${name}, ${when}. It is not certain the pill dropped; a caregiver will check.`;
  const info = dropStatusInfo(drop.status);
  const reason = reasonText(drop.reason);
  return `Last request: ${name}, ${when}. ${info.word}${reason ? `: ${reason.toLowerCase()}` : ''}.`;
}

/** The device line for the patient: ok + a short sentence. */
export function deviceView(device) {
  if (!device || typeof device !== 'object' || !Object.keys(device).length) {
    return { ok: false, tone: 'neutral', icon: 'help', word: 'Unknown', text: 'Device status is not known yet.' };
  }
  if (device.mode === 'none') {
    return { ok: false, tone: 'bad', icon: 'offline', word: 'Not set up', text: 'No pill device is set up. Pills cannot drop.' };
  }
  if (!device.connected) {
    return { ok: false, tone: 'bad', icon: 'offline', word: 'Offline', text: 'The pill device is offline. Pills cannot drop until it reconnects.' };
  }
  if (device.responsive === false) {
    return { ok: false, tone: 'bad', icon: 'warning', word: 'Not responding', text: 'The pill device is not responding.' };
  }
  const info = deviceStateInfo(device.state);
  if (device.state === 'FAULT') {
    return { ok: false, tone: 'bad', icon: 'warning', word: info.word, text: 'The pill device needs attention. Your caregiver can reset it.' };
  }
  if (device.state === 'READY') {
    return { ok: true, tone: 'good', icon: 'check-circle', word: 'Ready', text: 'The pill device is ready.' };
  }
  return { ok: true, tone: 'caution', icon: info.icon, word: info.word, text: `The pill device is busy: ${info.word.toLowerCase()}.` };
}

/** PatientStatus.alerts -> [{kind, text, tone}] (unknown shapes are tolerated). */
export function alertsView(status) {
  const alerts = Array.isArray(status?.alerts) ? status.alerts : [];
  return alerts
    .map((a) => (typeof a === 'string' ? { kind: 'INFO', message: a } : a))
    .filter((a) => a && (a.message || a.text))
    .map((a) => ({
      kind: a.kind || 'INFO',
      text: String(a.message || a.text),
      tone: ['EMPTY', 'DROP_FAILED', 'DROP_UNCERTAIN', 'DEVICE_ALERT', 'MISSED_DOSE', 'NEEDS_REVIEW'].includes(a.kind) ? 'bad' : 'caution',
    }));
}

/** Short phrase for a DoseView time ("8:00 AM"). */
export function doseTime(dose, offsetMin = null) {
  if (!dose) return DASH;
  if (dose.scheduled_local) return formatClock(dose.scheduled_local);
  return formatClockDevice(dose.scheduled_at, offsetMin);
}

/** The device-local date ("YYYY-MM-DD") of PatientStatus.now_local. */
export function todayKey(status) {
  const w = deviceWall(status?.now_local);
  if (!w) return null;
  return `${w.year}-${String(w.month).padStart(2, '0')}-${String(w.day).padStart(2, '0')}`;
}

/** Spoken/visible sentence for a DropOutcome; the server's message is authoritative. */
export function outcomeView(outcome) {
  const status = outcome?.status || 'UNKNOWN';
  const info = dropStatusInfo(status);
  const message = outcome?.message || (status === 'DROPPED' ? 'Your pill dropped.' : 'The pill did not drop.');
  return {
    status,
    dropped: status === 'DROPPED',
    word: info.word,
    icon: info.icon,
    tone: info.tone,
    message,
    urgent: status === 'FAILED' || status === 'UNCERTAIN',
  };
}

/**
 * Kiosk banner: one big word + one line, from the status, the live cooldown, whether a
 * drop is in progress and whether the server can be reached.
 * Returns {key, word, detail, tone, icon}.
 */
export function kioskBanner({ status = null, remainingS = 0, dropping = false, online = true } = {}) {
  if (!online) return { key: 'offline', word: 'Offline', detail: 'Cannot reach TactiDose. Trying again…', tone: 'bad', icon: 'offline' };
  if (!status) return { key: 'loading', word: 'Starting', detail: 'Getting your pill status…', tone: 'neutral', icon: 'rotate' };
  if (dropping) return { key: 'dropping', word: 'Dropping', detail: 'A pill is dropping. Please wait.', tone: 'caution', icon: 'rotate' };
  const dev = deviceView(status.device);
  if (!dev.ok) return { key: 'device', word: dev.word, detail: dev.text, tone: 'bad', icon: dev.icon };
  const cd = cooldownView(status, remainingS);
  if (cd.active) return { key: 'wait', word: 'Please wait', detail: cd.text, tone: 'caution', icon: 'clock' };
  const next = status.next_scheduled;
  if (next && next.status === 'DUE') return { key: 'due', word: 'Pill due', detail: `${nextPillText(status)}. It drops by itself.`, tone: 'due', icon: 'bell' };
  return { key: 'ready', word: 'Ready', detail: 'You can drop a pill now.', tone: 'good', icon: 'check-circle' };
}
