/**
 * Fetch wrapper for the TactiDose HTTP API v2 (docs/API.md is the contract).
 *
 * - Cookie session: every request is sent with credentials 'same-origin', so the
 *   HttpOnly `td_session` cookie set by POST /api/auth/login travels with it.
 * - JSON request/response bodies. FormData (label photos) and raw bytes (microphone
 *   PCM for /api/agent/transcribe) are sent unchanged.
 * - Errors are thrown as ApiError carrying the HTTP status and the server's `detail`
 *   (FastAPI sends a string, or a list of validation errors for 422).
 * - 401 means "not signed in / session expired": the browser goes to
 *   /login?next=<this page> unless the caller passes {redirectOn401: false}
 *   (the sign-in form itself, session probes).
 *
 * fetch and location can be swapped with configureApi() for unit tests.
 */

export const DEFAULT_TIMEOUT_MS = 30000;
/** Agent replies (Gemini), report generation and label scans can take a while. */
export const LONG_TIMEOUT_MS = 90000;

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

const deps = { fetch: null, location: null };
let redirecting = false;

/** Override dependencies (tests): {fetch, location}. */
export function configureApi(overrides = {}) {
  Object.assign(deps, overrides);
  redirecting = false;
}

function currentLocation() {
  return deps.location || globalThis.location || null;
}

/**
 * A same-origin page path that is safe to return to after signing in, or null.
 * Rejects absolute/protocol-relative URLs (open redirect), API paths and the login page.
 */
export function safeNext(value) {
  if (typeof value !== 'string') return null;
  const v = value.trim();
  if (!v.startsWith('/') || v.startsWith('//') || v.startsWith('/\\')) return null;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\\]/.test(v)) return null;
  if (v === '/api' || v.startsWith('/api/') || v === '/login' || v.startsWith('/login?') || v.startsWith('/login#')) return null;
  return v;
}

export function loginUrl(next = null) {
  const target = safeNext(next);
  return target && target !== '/' ? `/login?next=${encodeURIComponent(target)}` : '/login';
}

/** Leave the page for the sign-in form, remembering where we were. Runs once per page. */
export function redirectToLogin() {
  const loc = currentLocation();
  if (!loc || redirecting) return;
  if (String(loc.pathname || '').startsWith('/login')) return;
  redirecting = true;
  loc.assign(loginUrl(`${loc.pathname || '/'}${loc.search || ''}${loc.hash || ''}`));
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
 * Perform one API request.
 * Options: {body, form, raw, contentType, timeoutMs, signal, redirectOn401}.
 * Resolves the parsed JSON (null for an empty body); rejects with ApiError.
 */
export async function request(method, path, opts = {}) {
  const {
    body,
    form,
    raw,
    contentType = 'application/octet-stream',
    timeoutMs = DEFAULT_TIMEOUT_MS,
    signal = null,
    redirectOn401 = true,
  } = opts;
  const headers = { Accept: 'application/json' };
  let payload;
  if (form !== undefined) {
    payload = form;
  } else if (raw !== undefined) {
    headers['Content-Type'] = contentType;
    payload = raw;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }

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
  if (res.status === 401 && redirectOn401) {
    redirectToLogin();
    throw new ApiError('Your session has ended. Please sign in again.', { status: 401, detail, method, path });
  }
  throw new ApiError(message, { status: res.status, detail, method, path });
}

export const get = (path, opts) => request('GET', path, opts);
export const post = (path, body = {}, opts = {}) => request('POST', path, { ...opts, body });
export const put = (path, body = {}, opts = {}) => request('PUT', path, { ...opts, body });
export const patch = (path, body = {}, opts = {}) => request('PATCH', path, { ...opts, body });
export const del = (path, opts) => request('DELETE', path, opts);
/** Multipart POST (label photos). */
export const upload = (path, formData, opts = {}) => request('POST', path, { timeoutMs: LONG_TIMEOUT_MS, ...opts, form: formData });
/** Raw bytes POST (16 kHz PCM16 audio). */
export const postRaw = (path, bytes, opts = {}) => request('POST', path, { timeoutMs: LONG_TIMEOUT_MS, ...opts, raw: bytes });
