/**
 * Pure formatting helpers (no DOM access; unit-tested under Node).
 *
 * Times are always shown in the *device* timezone, not the browser's: `*_local`
 * fields carry the device offset ("2026-10-04T08:00:00-07:00") so their wall-clock
 * digits are used directly; UTC fields are shifted by a device offset taken from a
 * `*_local` value (see deviceOffsetFrom()). The server clock may be "time travelling"
 * in demo mode, so "now" always comes from the server (PatientStatus.now_local) plus
 * the time elapsed since it was fetched — never from the browser's Date.
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
 * User-facing container number of a ContainerInfo / DoseView / PillDropView: its
 * `container_number`, else `slot + 1`, else null (never "container 1" for an unknown slot).
 */
export function containerNumberOf(obj) {
  if (!obj) return null;
  const n = obj.container_number;
  if (n !== null && n !== undefined && n !== '' && Number.isFinite(Number(n))) return Number(n);
  const s = obj.slot;
  if (s === null || s === undefined || s === '' || !Number.isFinite(Number(s))) return null;
  return Number(s) + 1;
}

/** "1 pill" / "2 pills". */
export function plural(n, one, many = `${one}s`) {
  const v = Number(n);
  return `${Number.isFinite(v) ? v : 0} ${v === 1 ? one : many}`;
}

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

/** Epoch milliseconds of an ISO string with an offset, else NaN. */
export function epochOf(iso) {
  const p = parseIso(iso);
  return p && p.epochMs !== null ? p.epochMs : NaN;
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

/** "YYYY-MM-DD" wall date of an ISO string in the device offset (or its own offset). */
export function dateKey(iso, offsetMin = null) {
  const w = deviceWall(iso, offsetMin);
  if (!w) return null;
  return `${w.year}-${pad2(w.month)}-${pad2(w.day)}`;
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

/** "today" / "tomorrow" / "yesterday" for `iso` relative to the device's now, else null. */
export function relativeDayWord(iso, nowLocalIso, offsetMin = null) {
  const offset = offsetMin ?? deviceOffsetFrom(nowLocalIso);
  const diff = dayDiff(dateKey(nowLocalIso), dateKey(iso, offset));
  if (diff === 0) return 'today';
  if (diff === 1) return 'tomorrow';
  if (diff === -1) return 'yesterday';
  return null;
}

/**
 * "today at 8:00 AM" / "tomorrow at 8:00 AM" / "on Mon 5 Oct at 8:00 AM" in device time.
 * Without `nowLocalIso` it falls back to "Mon 5 Oct at 8:00 AM".
 */
export function formatWhen(iso, nowLocalIso = null, offsetMin = null) {
  const offset = offsetMin ?? deviceOffsetFrom(nowLocalIso);
  const w = deviceWall(iso, offset);
  if (!w) return DASH;
  const time = clock12(w.hour, w.minute);
  const word = nowLocalIso ? relativeDayWord(iso, nowLocalIso, offset) : null;
  if (word) return `${word} at ${time}`;
  return `${nowLocalIso ? 'on ' : ''}${DAYS[w.weekday]} ${w.day} ${MONTHS[w.month - 1]} at ${time}`;
}

/** Wall-clock "YYYY-MM-DDTHH:MM" `minutes` after the device-local `nowLocalIso` (seconds dropped). */
export function addMinutesToLocal(nowLocalIso, minutes) {
  const p = parseIso(nowLocalIso);
  if (!p) return null;
  const w = wallParts(p.wallMs + minutes * 60000);
  return `${w.year}-${pad2(w.month)}-${pad2(w.day)}T${pad2(w.hour)}:${pad2(w.minute)}`;
}

/** Device-local ISO string `seconds` after `nowLocalIso`, keeping its offset (for live clocks). */
export function advanceLocalIso(nowLocalIso, seconds) {
  const p = parseIso(nowLocalIso);
  if (!p) return null;
  const w = wallParts(p.wallMs + Math.round(seconds * 1000));
  const tz = p.offsetMin === null ? '' : `${p.offsetMin < 0 ? '-' : '+'}${pad2(Math.floor(Math.abs(p.offsetMin) / 60))}:${pad2(Math.abs(p.offsetMin) % 60)}`;
  return `${w.year}-${pad2(w.month)}-${pad2(w.day)}T${pad2(w.hour)}:${pad2(w.minute)}:${pad2(w.second)}${tz}`;
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

export function formatSeconds(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return DASH;
  const s = Number(seconds);
  return s < 10 ? `${s.toFixed(2)} s` : `${Math.round(s)} s`;
}

/**
 * Words for a remaining duration, rounded up to the minute so the countdown never
 * says "0 minutes" while a cooldown is still running:
 * "less than a minute", "23 minutes", "1 hour", "1 hour 5 minutes".
 */
export function formatDuration(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return 'no time';
  if (s < 60) return 'less than a minute';
  const total = Math.ceil(s / 60);
  const h = Math.floor(total / 60);
  const m = total % 60;
  if (!h) return plural(m, 'minute');
  return m ? `${plural(h, 'hour')} ${plural(m, 'minute')}` : plural(h, 'hour');
}

/** "in 23 minutes" / "in less than a minute" / "now". */
export function formatCountdown(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return 'now';
  return `in ${formatDuration(s)}`;
}

/** "+2 h 05 min" / "−30 min" for a demo clock offset in seconds. */
export function formatOffset(seconds) {
  const s = Number(seconds) || 0;
  const sign = s < 0 ? '−' : '+';
  const abs = Math.round(Math.abs(s) / 60);
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  if (!h) return `${sign}${m} min`;
  return `${sign}${h} h ${pad2(m)} min`;
}

export function formatBytes(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v < 0) return DASH;
  if (v < 1024) return `${v} bytes`;
  if (v < 1024 * 1024) return `${Math.round(v / 1024)} KB`;
  return `${(v / (1024 * 1024)).toFixed(1)} MB`;
}

/** Repeat rule of a Schedule in words. */
export function describeRepeat(schedule) {
  if (!schedule) return DASH;
  const days = Array.isArray(schedule.days_of_week) ? schedule.days_of_week : [];
  if (schedule.frequency !== 'WEEKLY' || days.length === 0 || days.length === 7) return 'Every day';
  const order = WEEKDAYS.filter((d) => days.includes(d));
  return order.map((d) => WEEKDAY_NAMES[d].slice(0, 3)).join(', ');
}

/** "ALEX2026" -> "A, L, E, X, 2, 0, 2, 6" so screen readers spell a code out. */
export function spellOut(code) {
  return Array.from(String(code || '').replace(/\s+/g, '')).join(', ');
}
