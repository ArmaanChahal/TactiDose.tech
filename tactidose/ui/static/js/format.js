/**
 * Pure formatting helpers (no DOM access; unit-tested under Node).
 *
 * Times are always shown in the *device* timezone, not the browser's: `*_local`
 * fields carry the device offset ("2026-10-04T08:00:00-07:00") so their wall-clock
 * digits are used directly; UTC fields are shifted by a device offset taken from
 * a `*_local` value (see deviceOffsetFrom()).
 */

const ISO_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$/i;

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const MONTHS_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const DAYS_LONG = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

export const DASH = '–';

/** Weekday codes used by schedules (Schedule.days_of_week). */
export const WEEKDAYS = Object.freeze(['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']);
export const WEEKDAY_NAMES = Object.freeze({
  MON: 'Monday', TUE: 'Tuesday', WED: 'Wednesday', THU: 'Thursday', FRI: 'Friday', SAT: 'Saturday', SUN: 'Sunday',
});

const pad2 = (n) => String(n).padStart(2, '0');

/**
 * Parse an ISO-8601 date/datetime. Returns wall-clock parts as written, the offset in
 * minutes (null when absent) and epochMs (null when the offset is unknown).
 */
export function parseIso(value) {
  if (typeof value !== 'string') return null;
  const m = ISO_RE.exec(value.trim());
  if (!m) return null;
  const [, y, mo, d, hh = '0', mi = '0', ss = '0', frac = '', tz] = m;
  let offsetMin = null;
  if (tz) {
    if (tz.toUpperCase() === 'Z') {
      offsetMin = 0;
    } else {
      const sign = tz[0] === '-' ? -1 : 1;
      const digits = tz.slice(1).replace(':', '');
      offsetMin = sign * (Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2, 4) || 0));
    }
  }
  const ms = Number(`${frac}000`.slice(0, 3));
  const wallMs = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(hh), Number(mi), Number(ss), ms);
  return {
    year: Number(y),
    month: Number(mo),
    day: Number(d),
    hour: Number(hh),
    minute: Number(mi),
    second: Number(ss),
    offsetMin,
    wallMs,
    epochMs: offsetMin === null ? null : wallMs - offsetMin * 60000,
  };
}

/** Offset (minutes east of UTC) of a `*_local` ISO string, or null. */
export function deviceOffsetFrom(localIso) {
  const p = parseIso(localIso);
  return p ? p.offsetMin : null;
}

function wallParts(wallMs) {
  const d = new Date(wallMs);
  return {
    year: d.getUTCFullYear(),
    month: d.getUTCMonth() + 1,
    day: d.getUTCDate(),
    hour: d.getUTCHours(),
    minute: d.getUTCMinutes(),
    second: d.getUTCSeconds(),
    weekday: d.getUTCDay(),
    wallMs,
  };
}

/** Wall-clock parts of `iso` in the device offset (falls back to the string's own wall clock). */
export function deviceWall(iso, offsetMin = null) {
  const p = parseIso(iso);
  if (!p) return null;
  if (offsetMin === null || offsetMin === undefined || p.epochMs === null) return wallParts(p.wallMs);
  return wallParts(p.epochMs + offsetMin * 60000);
}

export function clock12(hour, minute, second = null) {
  const suffix = hour < 12 ? 'AM' : 'PM';
  const h12 = hour % 12 === 0 ? 12 : hour % 12;
  const sec = second === null ? '' : `:${pad2(second)}`;
  return `${h12}:${pad2(minute)}${sec} ${suffix}`;
}

/** "8:00 AM" — wall clock of the ISO string itself (use for `*_local` fields). */
export function formatClock(iso, { seconds = false } = {}) {
  const p = parseIso(iso);
  if (!p) return DASH;
  return clock12(p.hour, p.minute, seconds ? p.second : null);
}

/** "8:03 AM" — any ISO timestamp shown in the device offset. */
export function formatClockDevice(iso, offsetMin, { seconds = false } = {}) {
  const w = deviceWall(iso, offsetMin);
  if (!w) return DASH;
  return clock12(w.hour, w.minute, seconds ? w.second : null);
}

/** "Mon 5 Oct, 8:03 AM" in the device offset. */
export function formatDateTimeDevice(iso, offsetMin) {
  const w = deviceWall(iso, offsetMin);
  if (!w) return DASH;
  return `${DAYS[w.weekday]} ${w.day} ${MONTHS[w.month - 1]}, ${clock12(w.hour, w.minute)}`;
}

/** "YYYY-MM-DD" wall date of an ISO string (in its own offset). */
export function dateKey(iso) {
  const p = parseIso(iso);
  if (!p) return null;
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)}`;
}

function keyToWallMs(key) {
  const p = parseIso(key);
  return p ? p.wallMs : NaN;
}

/** "Mon 5 Oct" for "2026-10-05". */
export function formatDateLabel(key) {
  const ms = keyToWallMs(key);
  if (Number.isNaN(ms)) return DASH;
  const w = wallParts(ms);
  return `${DAYS[w.weekday]} ${w.day} ${MONTHS[w.month - 1]}`;
}

/** "Monday 5 October 2026" for "2026-10-05". */
export function formatLongDate(key) {
  const ms = keyToWallMs(key);
  if (Number.isNaN(ms)) return DASH;
  const w = wallParts(ms);
  return `${DAYS_LONG[w.weekday]} ${w.day} ${MONTHS_LONG[w.month - 1]} ${w.year}`;
}

/** Short weekday ("Mon") for "2026-10-05". */
export function weekdayShort(key) {
  const ms = keyToWallMs(key);
  return Number.isNaN(ms) ? '' : DAYS[wallParts(ms).weekday];
}

/** Whole days from `fromKey` to `toKey` (both "YYYY-MM-DD"). */
export function dayDiff(fromKey, toKey) {
  const a = keyToWallMs(fromKey);
  const b = keyToWallMs(toKey);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.round((b - a) / 86400000);
}

/** "2026-10-05" shifted by `days`. */
export function shiftDateKey(key, days) {
  const ms = keyToWallMs(key);
  if (Number.isNaN(ms)) return key;
  const w = wallParts(ms + days * 86400000);
  return `${w.year}-${pad2(w.month)}-${pad2(w.day)}`;
}

/** "TODAY" / "TOMORROW" / "YESTERDAY" for a dose time relative to the device's "now", else null. */
export function relativeDayWord(iso, nowLocalIso) {
  const diff = dayDiff(dateKey(nowLocalIso), dateKey(iso));
  if (diff === 0) return 'TODAY';
  if (diff === 1) return 'TOMORROW';
  if (diff === -1) return 'YESTERDAY';
  return null;
}

/** Wall-clock "YYYY-MM-DDTHH:MM" `minutes` after the device-local `nowLocalIso` (seconds dropped). */
export function addMinutesToLocal(nowLocalIso, minutes) {
  const p = parseIso(nowLocalIso);
  if (!p) return null;
  const w = wallParts(p.wallMs + minutes * 60000);
  return `${w.year}-${pad2(w.month)}-${pad2(w.day)}T${pad2(w.hour)}:${pad2(w.minute)}`;
}

/** Normalise "8:5", "08:05:00" … to "08:05"; returns null when not a time. */
export function normalizeTime(value) {
  const m = /^\s*(\d{1,2}):(\d{1,2})(?::\d{1,2}(?:\.\d+)?)?\s*$/.exec(String(value ?? ''));
  if (!m) return null;
  const hh = Number(m[1]);
  const mm = Number(m[2]);
  if (hh > 23 || mm > 59) return null;
  return `${pad2(hh)}:${pad2(mm)}`;
}

/** "08:00" -> "8:00 AM". */
export function time24To12(hhmm) {
  const t = normalizeTime(hhmm);
  if (!t) return hhmm ? String(hhmm) : DASH;
  const [hh, mm] = t.split(':').map(Number);
  return clock12(hh, mm);
}

/** Rates may arrive as a fraction (0–1) or a percentage (0–100); returns 0–100 or null. */
export function toPercent(rate) {
  if (rate === null || rate === undefined || rate === '') return null;
  const n = Number(rate);
  if (!Number.isFinite(n)) return null;
  const pct = n >= 0 && n <= 1 ? n * 100 : n;
  return Math.max(0, Math.min(100, pct));
}

export function formatPercent(rate) {
  const p = toPercent(rate);
  return p === null ? DASH : `${Math.round(p)}%`;
}

export function formatMinutes(minutes) {
  if (minutes === null || minutes === undefined || !Number.isFinite(Number(minutes))) return DASH;
  const m = Number(minutes);
  if (m < 1) return 'under 1 min';
  if (m < 60) return `${Math.round(m)} min`;
  const h = Math.floor(m / 60);
  const rest = Math.round(m - h * 60);
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

export function formatSeconds(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return DASH;
  const s = Number(seconds);
  return s < 10 ? `${s.toFixed(2)} s` : `${Math.round(s)} s`;
}

/** "+2 h 05 min" / "-30 min" for a demo clock offset in seconds. */
export function formatOffset(seconds) {
  const s = Number(seconds) || 0;
  const sign = s < 0 ? '−' : '+';
  const abs = Math.round(Math.abs(s) / 60);
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  if (!h) return `${sign}${m} min`;
  return `${sign}${h} h ${pad2(m)} min`;
}

export function formatCount(n) {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return DASH;
  return Number(n).toLocaleString('en-US');
}

export function slotToCompartment(slot) {
  return slot === null || slot === undefined ? null : Number(slot) + 1;
}

export function compartmentName(number) {
  return number ? `Compartment ${number}` : 'No compartment';
}

/** Repeat rule of a Schedule in words. */
export function describeRepeat(schedule) {
  if (!schedule) return DASH;
  const days = Array.isArray(schedule.days_of_week) ? schedule.days_of_week : [];
  if (schedule.frequency !== 'WEEKLY' || days.length === 0 || days.length === 7) return 'Every day';
  const order = WEEKDAYS.filter((d) => days.includes(d));
  return order.map((d) => WEEKDAY_NAMES[d].slice(0, 3)).join(', ');
}

/** Dose status -> word + icon + tone (never colour alone). */
export const DOSE_STATUS = Object.freeze({
  SCHEDULED: { word: 'Scheduled', icon: 'clock', tone: 'neutral' },
  DUE: { word: 'Due now', icon: 'bell', tone: 'due' },
  DISPENSING: { word: 'Preparing', icon: 'rotate', tone: 'caution' },
  DISPENSED: { word: 'Accessed, not confirmed', icon: 'open', tone: 'info' },
  TAKEN: { word: 'Taken', icon: 'check-circle', tone: 'good' },
  MISSED: { word: 'Missed', icon: 'x-circle', tone: 'bad' },
  CANCELLED: { word: 'Skipped', icon: 'slash', tone: 'neutral' },
  HARDWARE_ERROR: { word: 'Hardware error', icon: 'warning', tone: 'bad' },
});

export function doseStatusInfo(status, needsReview = false) {
  const base = DOSE_STATUS[status] || { word: status ? String(status) : 'Unknown', icon: 'help', tone: 'neutral' };
  return { ...base, needsReview: Boolean(needsReview) };
}

/** Device state -> word + icon + tone for caregiver/demo views. */
export const DEVICE_STATE = Object.freeze({
  BOOT: { word: 'Starting up', icon: 'rotate', tone: 'caution' },
  HOMING: { word: 'Homing', icon: 'rotate', tone: 'caution' },
  READY: { word: 'Ready', icon: 'check-circle', tone: 'good' },
  MOVING: { word: 'Moving', icon: 'rotate', tone: 'caution' },
  AT_TARGET: { word: 'Settling at compartment', icon: 'rotate', tone: 'caution' },
  GATE_OPEN: { word: 'Gate open', icon: 'open', tone: 'caution' },
  SAFE_STOP: { word: 'Stopped (will re-home)', icon: 'stop', tone: 'caution' },
  FAULT: { word: 'Fault (needs homing)', icon: 'warning', tone: 'bad' },
  UNKNOWN: { word: 'Unknown', icon: 'help', tone: 'neutral' },
});

export function deviceStateInfo(state) {
  return DEVICE_STATE[state] || { word: state ? String(state) : 'Unknown', icon: 'help', tone: 'neutral' };
}

/** Medication.source in words. */
export function sourceName(source) {
  return { manual: 'Entered manually', label_scan: 'From a label scan', demo_seed: 'Demo data' }[source] || source || DASH;
}
