/**
 * Per-browser preferences of the person using this screen (localStorage), e.g.
 * "speak replies out loud". Pure apart from the storage it is given; unit-tested.
 */

export const PREFS_KEY = 'tactidose.prefs';

export const DEFAULT_PREFS = Object.freeze({
  /** Play the assistant's replies out loud (server audio, else the browser voice). */
  speakReplies: true,
  /** Say "pill dropped" (and other drop results) out loud. */
  speakDrops: true,
  /** Ask "Drop a pill from container 1?" before dropping. */
  confirmDrops: true,
  /** Show desktop notifications when the page is in the background (needs permission). */
  desktopAlerts: false,
  /** Always use the offline recognizer on the server instead of the browser's speech service. */
  offlineSpeech: false,
});

function defaultStorage() {
  try {
    return globalThis.localStorage || null;
  } catch {
    return null;
  }
}

export function createPrefs(storage = defaultStorage()) {
  let values = { ...DEFAULT_PREFS };
  try {
    const saved = JSON.parse(storage?.getItem(PREFS_KEY) || '{}');
    if (saved && typeof saved === 'object') {
      for (const key of Object.keys(DEFAULT_PREFS)) {
        if (typeof saved[key] === typeof DEFAULT_PREFS[key]) values[key] = saved[key];
      }
    }
  } catch {
    values = { ...DEFAULT_PREFS };
  }
  const listeners = new Set();
  return {
    get(key) {
      return values[key];
    },
    set(key, value) {
      if (!(key in DEFAULT_PREFS)) throw new Error(`unknown preference ${key}`);
      values = { ...values, [key]: Boolean(value) };
      try {
        storage?.setItem(PREFS_KEY, JSON.stringify(values));
      } catch {
        /* storage unavailable */
      }
      for (const fn of listeners) fn(key, values[key]);
    },
    all() {
      return { ...values };
    },
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

/** Bind a checkbox to a boolean preference. */
export function bindPrefCheckbox(prefs, key, checkbox) {
  if (!checkbox) return;
  checkbox.checked = Boolean(prefs.get(key));
  checkbox.addEventListener('change', () => prefs.set(key, checkbox.checked));
  prefs.onChange((k, v) => {
    if (k === key) checkbox.checked = Boolean(v);
  });
}
