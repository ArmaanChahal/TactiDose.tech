/**
 * Session helpers shared by every page: who is signed in (GET /api/auth/me), page
 * guards that send people to the right portal, sign-out, and a watchdog that notices
 * when the session ended while the live stream was reconnecting.
 */

import { ApiError, get, post, redirectToLogin } from './api.js';

export const CAREGIVER_ROLES = Object.freeze(['doctor', 'family']);

export const ROLE_NAMES = Object.freeze({
  patient: 'Patient',
  doctor: 'Doctor',
  family: 'Family member',
});

export function isCaregiver(user) {
  return Boolean(user && CAREGIVER_ROLES.includes(user.role));
}

export function roleName(role) {
  return ROLE_NAMES[role] || (role ? String(role) : 'User');
}

/** The portal a role belongs to. */
export function homeFor(role) {
  if (role === 'patient') return '/patient';
  if (CAREGIVER_ROLES.includes(role)) return '/care';
  return '/login';
}

/**
 * Where to go after signing in: `next` when it is a page this role may use,
 * otherwise the role's own portal. `next` must already be a safe same-origin path.
 */
export function destinationFor(role, next = null) {
  const home = homeFor(role);
  if (!next) return home;
  const path = String(next).split(/[?#]/)[0];
  const patientPages = ['/patient', '/kiosk', '/demo'];
  const carePages = ['/care', '/demo'];
  const allowed = role === 'patient' ? patientPages : CAREGIVER_ROLES.includes(role) ? carePages : [];
  return allowed.includes(path) ? next : home;
}

/** GET /api/auth/me without redirecting; resolves null when nobody is signed in. */
export async function fetchSession() {
  try {
    return await get('/api/auth/me', { redirectOn401: false });
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return null;
    throw err;
  }
}

/**
 * Page guard. Resolves the session, or navigates away and resolves null:
 * to /login when signed out, or to the right portal when the role does not fit.
 * Network errors are thrown so the page can show "Cannot reach the server".
 */
export async function requireSession({ roles = null } = {}) {
  const me = await fetchSession();
  if (!me || !me.user) {
    redirectToLogin();
    return null;
  }
  if (roles && !roles.includes(me.user.role)) {
    globalThis.location?.replace(homeFor(me.user.role));
    return null;
  }
  return me;
}

export async function signOut() {
  try {
    await post('/api/auth/logout', {}, { redirectOn401: false });
  } catch {
    /* already signed out or server unreachable: leave anyway */
  }
  globalThis.location?.assign('/login');
}

/**
 * When the live stream drops, ask the server whether the session is still valid; if it
 * is not (EventSource cannot see the 401 itself), go to the sign-in page instead of
 * retrying forever. Runs once per outage (the status only changes once per outage).
 */
export function watchSession(stream) {
  let checking = false;
  stream.onStatus(async (status) => {
    if (status !== 'reconnecting' || checking) return;
    checking = true;
    try {
      const me = await fetchSession();
      if (!me) redirectToLogin();
    } catch {
      /* server unreachable: keep reconnecting */
    } finally {
      checking = false;
    }
  });
}
