/**
 * Fetch wrapper for the TactiDose HTTP API (docs/API.md is the contract).
 *
 * - JSON request/response bodies; FormData is sent as multipart unchanged.
 * - Errors are thrown as ApiError carrying the HTTP status and the server's
 *   `detail` (FastAPI returns a string, or a list of validation errors for 422).
 * - Caregiver PIN: the `X-Caregiver-Pin` header is added from localStorage when
 *   a PIN is stored. On 401 the user is asked for the PIN once and the request
 *   is retried once; a second 401 forgets the stored PIN.
 *
 * Dependencies (fetch, storage, PIN prompt) can be swapped with configureApi()
 * so the logic is unit-testable outside a browser.
 */

import { promptDialog } from './dom.js';
import { icon } from './icons.js';

export const PIN_STORAGE_KEY = 'tactidose.caregiverPin';
export const DEFAULT_TIMEOUT_MS = 30000;
/** POST /api/intents waits until the assistant handled the intent (≤ 60 s server-side). */
export const INTENT_TIMEOUT_MS = 75000;

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null, method = 'GET', path = '', network = false, timeout = false } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.method = method;
    this.path = path;
    this.network = network;
    this.timeout = timeout;
  }
}

const deps = {
  fetch: null,
  storage: null,
  promptPin: null,
};

/** Override dependencies (tests): {fetch, storage, promptPin}. */
export function configureApi(overrides = {}) {
  Object.assign(deps, overrides);
}

function storage() {
  if (deps.storage) return deps.storage;
  try {
    return globalThis.localStorage || null;
  } catch {
    return null;
  }
}

export function getPin() {
  try {
    return storage()?.getItem(PIN_STORAGE_KEY) || null;
  } catch {
    return null;
  }
}

/** Lets pages show/hide a "Forget PIN" control (no-op outside a browser). */
function pinChanged() {
  if (typeof globalThis.dispatchEvent === 'function' && typeof Event === 'function') {
    globalThis.dispatchEvent(new Event('tactidose:pin'));
  }
}

export function setPin(pin) {
  try {
    storage()?.setItem(PIN_STORAGE_KEY, pin);
  } catch {
    /* storage unavailable: the PIN is used for this request only */
  }
  pinChanged();
}

export function clearPin() {
  try {
    storage()?.removeItem(PIN_STORAGE_KEY);
  } catch {
    /* ignore */
  }
  pinChanged();
}

export function hasPin() {
  return Boolean(getPin());
}

function locText(loc) {
  if (!Array.isArray(loc)) return '';
  const parts = loc.filter((p, i) => !(i === 0 && ['body', 'query', 'path', 'header'].includes(p)));
  return parts.length ? `${parts.join('.')}: ` : '';
}

/** Turn a FastAPI `detail` (string | validation-error list | object) into one readable sentence. */
export function detailToMessage(detail, fallback = 'Request failed') {
  if (detail === null || detail === undefined || detail === '') return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (typeof item === 'string') return item;
      if (item && typeof item === 'object') return `${locText(item.loc)}${item.msg || item.message || JSON.stringify(item)}`;
      return String(item);
    });
    return parts.filter(Boolean).join('; ') || fallback;
  }
  if (typeof detail === 'object') {
    return detail.message || detail.msg || detail.detail || JSON.stringify(detail);
  }
  return String(detail);
}

let pinRequest = null;

function askForPin(reason) {
  if (deps.promptPin) return Promise.resolve(deps.promptPin(reason));
  // Concurrent 401s share one prompt.
  if (!pinRequest) {
    const generic = !reason || /^caregiver pin required\.?$/i.test(String(reason).trim());
    pinRequest = promptDialog({
      title: 'Caregiver PIN required',
      message: generic ? 'Enter the caregiver PIN to make changes.' : reason,
      label: 'Caregiver PIN',
      type: 'password',
      inputmode: 'numeric',
      confirmLabel: 'Unlock',
      iconEl: icon('lock'),
    }).finally(() => {
      pinRequest = null;
    });
  }
  return pinRequest;
}

async function parseBody(res) {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * Perform one API request. Options: {body, form, timeoutMs, signal, auth}.
 * Resolves the parsed JSON (or null for an empty body); rejects with ApiError.
 */
export async function request(method, path, opts = {}) {
  const { body, form, timeoutMs = DEFAULT_TIMEOUT_MS, signal = null, auth = true, retried = false } = opts;
  const headers = { Accept: 'application/json' };
  let payload;
  if (form !== undefined) {
    payload = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const pin = auth ? getPin() : null;
  if (pin) headers['X-Caregiver-Pin'] = pin;

  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const onAbort = () => controller.abort();
  if (signal) signal.addEventListener('abort', onAbort, { once: true });

  const doFetch = deps.fetch || ((...args) => globalThis.fetch(...args));
  let res;
  try {
    res = await doFetch(path, { method, headers, body: payload, signal: controller.signal, credentials: 'same-origin' });
  } catch (err) {
    if (timedOut) {
      throw new ApiError('The TactiDose server did not answer in time. Please try again.', { method, path, timeout: true });
    }
    if (signal && signal.aborted) throw err;
    throw new ApiError('Cannot reach the TactiDose server. Check that it is running.', { method, path, network: true });
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onAbort);
  }

  const data = await parseBody(res).catch(() => null);
  if (res.ok) return data;

  const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : data;
  const message = detailToMessage(detail, `${res.status} ${res.statusText || 'error'}`.trim());

  if (res.status === 401 && auth) {
    if (!retried) {
      const entered = await askForPin(message);
      if (entered) {
        setPin(entered);
        return request(method, path, { ...opts, retried: true });
      }
      throw new ApiError(`Caregiver PIN required: ${message}`, { status: 401, detail, method, path });
    }
    clearPin();
    throw new ApiError('The caregiver PIN was not accepted. Please try again.', { status: 401, detail, method, path });
  }
  throw new ApiError(message, { status: res.status, detail, method, path });
}

export const get = (path, opts) => request('GET', path, opts);
export const post = (path, body = {}, opts = {}) => request('POST', path, { ...opts, body });
export const put = (path, body = {}, opts = {}) => request('PUT', path, { ...opts, body });
export const patch = (path, body = {}, opts = {}) => request('PATCH', path, { ...opts, body });
export const del = (path, opts) => request('DELETE', path, opts);
/** Multipart POST (label scans). */
export const upload = (path, formData, opts = {}) => request('POST', path, { timeoutMs: 90000, ...opts, form: formData });

/** Kiosk buttons / shortcuts: POST /api/intents {intent, source}. */
export function postIntent(intent, source = 'ui') {
  return post('/api/intents', { intent, source }, { timeoutMs: INTENT_TIMEOUT_MS });
}

/** Demo panel simulated voice: POST /api/intents {text, source}. */
export function postText(text, source = 'keyboard') {
  return post('/api/intents', { text, source }, { timeoutMs: INTENT_TIMEOUT_MS });
}
