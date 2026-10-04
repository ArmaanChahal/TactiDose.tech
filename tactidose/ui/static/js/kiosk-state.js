/**
 * Kiosk view-model: turns GET /api/state + live events into what the kiosk shows.
 * Pure functions (no DOM), unit-tested under Node.
 *
 * Fail-closed presentation: when the server or the device link is unavailable the
 * banner says DEVICE OFFLINE regardless of any cached dose state, and carousel
 * motion always wins over everything else so the user is told to keep hands clear.
 * The screen never authorises anything — it only mirrors the backend.
 */

import { formatClock, relativeDayWord } from './format.js';

/** Intents the kiosk may post (docs/API.md, POST /api/intents). */
export const KIOSK_INTENTS = Object.freeze(['CHECK_DUE', 'DISPENSE', 'CONFIRM_TAKEN', 'REPEAT', 'HELP', 'CANCEL', 'PRIMARY_ACTION']);

const MOTION_STATES = new Set(['HOMING', 'MOVING', 'AT_TARGET']);

export const BANNERS = Object.freeze({
  CONNECTING: { key: 'connecting', word: 'CONNECTING…', detail: '', icon: 'rotate', tone: 'offline' },
  OFFLINE: { key: 'offline', word: 'DEVICE OFFLINE', detail: 'PLEASE ASK FOR ASSISTANCE', icon: 'offline', tone: 'offline' },
  PREPARING: { key: 'preparing', word: 'PREPARING', detail: 'KEEP HANDS CLEAR', icon: 'hand', tone: 'caution' },
  ATTENTION: { key: 'attention', word: 'NEEDS ASSISTANCE', detail: 'PLEASE ASK FOR ASSISTANCE', icon: 'warning', tone: 'alert' },
  DOSE_READY: { key: 'dose-ready', word: 'DOSE READY', detail: "SAY 'TAKEN'", icon: 'check-circle', tone: 'ready' },
  OPEN: { key: 'open', word: 'COMPARTMENT OPEN', detail: "SAY 'CANCEL' TO CLOSE IT", icon: 'open', tone: 'caution' },
  DUE: { key: 'due', word: 'DOSE DUE', detail: "SAY 'DISPENSE'", icon: 'bell', tone: 'due' },
  READY: { key: 'ready', word: 'DEVICE READY', detail: '', icon: 'check-circle', tone: 'ok' },
});

function banner(base, detail) {
  return detail === undefined ? { ...base } : { ...base, detail };
}

function awaitingDose(view) {
  if (view.awaiting) return view.awaiting;
  if (view.phase === 'AWAITING_CONFIRMATION') return view.due?.awaiting_confirmation?.[0] || null;
  return null;
}

function needsReviewBlocked(due) {
  return Array.isArray(due?.blocked) && due.blocked.some((b) => b && b.reason === 'NEEDS_REVIEW');
}

/**
 * view = {loaded, serverOnline, stateError, device: DeviceSnapshot|null, phase,
 *         awaiting: DoseInfo|null, due: DueSummary|null}
 * Returns one of BANNERS (copied, possibly with a more specific detail line).
 */
export function deriveBanner(view) {
  if (!view || !view.loaded) {
    return view && view.serverOnline === false ? banner(BANNERS.OFFLINE, 'RECONNECTING…') : banner(BANNERS.CONNECTING);
  }
  if (view.serverOnline === false) return banner(BANNERS.OFFLINE, 'RECONNECTING…');
  // The server answered but could not report its state: never guess.
  if (view.stateError) return banner(BANNERS.OFFLINE, 'STATUS UNAVAILABLE');
  const device = view.device;
  if (!device || !device.connected) return banner(BANNERS.OFFLINE);
  if (device.responsive === false) return banner(BANNERS.OFFLINE, 'NOT RESPONDING');
  if (view.phase === 'PREPARING' || MOTION_STATES.has(device.state)) return banner(BANNERS.PREPARING);
  if (view.phase === 'ATTENTION' || device.state === 'FAULT') return banner(BANNERS.ATTENTION);
  if (view.phase === 'AWAITING_CONFIRMATION' || awaitingDose(view)) return banner(BANNERS.DOSE_READY);
  if (needsReviewBlocked(view.due)) return banner(BANNERS.ATTENTION);
  if (device.gate === 'OPEN') return banner(BANNERS.OPEN);
  if (view.due?.due?.length) return banner(BANNERS.DUE);
  return banner(BANNERS.READY);
}

/** The kiosk button that matches the current step (highlighted; still just a request). */
export function suggestedIntent(view, current = deriveBanner(view)) {
  switch (current.key) {
    case 'dose-ready':
      return 'CONFIRM_TAKEN';
    case 'due':
      return 'DISPENSE';
    case 'open':
      return 'CANCEL';
    case 'attention':
      return 'HELP';
    default:
      return null;
  }
}

function compartmentWords(dose) {
  return dose && dose.compartment_number ? `COMPARTMENT ${dose.compartment_number}` : 'NO COMPARTMENT ASSIGNED';
}

/** The dose the "next event" line talks about, with its kind. */
export function focusDose(view) {
  const open = awaitingDose(view);
  if (open) return { kind: 'open', dose: open };
  const due = view?.due?.due?.[0];
  if (due) return { kind: 'due', dose: due };
  const next = view?.due?.next_upcoming;
  if (next) return { kind: 'next', dose: next };
  return { kind: 'none', dose: null };
}

/** Dose information is only shown while it is known to be current. */
function stateTrusted(view) {
  return Boolean(view && view.loaded && !view.stateError && view.serverOnline !== false);
}

/** "NEXT EVENT: 2:00 PM · COMPARTMENT 3" (or DUE NOW / OPEN NOW / NO UPCOMING DOSES). */
export function nextEventText(view) {
  if (!stateTrusted(view)) return '';
  const { kind, dose } = focusDose(view);
  if (kind === 'open') return `OPEN NOW: ${compartmentWords(dose)}`;
  if (kind === 'due') return `DUE NOW: ${formatClock(dose.scheduled_local)} · ${compartmentWords(dose)}`;
  if (kind === 'next') {
    const day = relativeDayWord(dose.scheduled_local, view.nowLocal || view.due?.now_local);
    const prefix = day && day !== 'TODAY' ? `${day} ` : '';
    return `NEXT EVENT: ${prefix}${formatClock(dose.scheduled_local)} · ${compartmentWords(dose)}`;
  }
  return 'NO UPCOMING DOSES';
}

/** Medication name + strength of the focused dose, upper-cased for the kiosk (or ''). */
export function doseDetailText(view) {
  if (!stateTrusted(view)) return '';
  const { dose } = focusDose(view);
  if (!dose || !dose.medication_name) return '';
  return [dose.medication_name, dose.strength].filter(Boolean).join(' · ').toUpperCase();
}

/** Voice status line from {enabled, listening, muted, error}. */
export function voiceText(voice) {
  if (!voice) return { word: 'VOICE: UNKNOWN', icon: 'mic-off', on: false };
  if (voice.enabled && voice.listening) return { word: 'VOICE ON', icon: 'mic', on: true };
  return { word: 'VOICE OFF', icon: 'mic-off', on: false };
}

/**
 * Map a key press to an intent, or null. Space/Enter only act as the main button
 * when focus is not on another control (so Tab + Enter still activates that button).
 */
export function intentForKey(key, { onControl = false, typing = false } = {}) {
  if (key === 'Escape' || key === 'Esc') return 'CANCEL';
  if (typing) return null;
  if ((key === ' ' || key === 'Spacebar' || key === 'Enter') && !onControl) return 'PRIMARY_ACTION';
  if (key === 'r' || key === 'R') return 'REPEAT';
  if (key === 'h' || key === 'H') return 'HELP';
  return null;
}
