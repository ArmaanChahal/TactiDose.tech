/**
 * Pure helpers of the sign-in page (validation, error wording, demo accounts);
 * unit-tested under Node.
 */

import { ApiError } from './api.js';

/** Demo accounts seeded in demo mode (ARCHITECTURE §9). Shown only when the server says demo mode is on. */
export const DEMO_ACCOUNTS = Object.freeze([
  { email: 'alex@demo.tactidose', label: 'Alex (patient)' },
  { email: 'sam@demo.tactidose', label: 'Sam (family)' },
  { email: 'dr.lee@demo.tactidose', label: 'Dr. Lee (doctor)' },
]);
export const DEMO_PASSWORD = 'demo1234';

/** Plain-language message for sign-in / registration failures. */
export function authErrorText(err, action = 'signin') {
  if (!(err instanceof ApiError)) return err?.message || 'Something went wrong. Please try again.';
  if (err.network || err.timeout) return err.message;
  if (action === 'signin' && err.status === 401) return 'The email or password is not right. Please try again.';
  if (action === 'register' && err.status === 409) return 'An account with this email already exists. Sign in instead.';
  if (action === 'register' && err.status === 403) return 'New accounts cannot be created on this server.';
  if (err.status === 422) return `Please check the form: ${err.message}`;
  return err.message;
}

/** Client-side checks before POST /api/auth/register. Returns {ok, body} or {ok: false, error, field}. */
export function registerBody({ name, email, password, role, phone }) {
  const displayName = String(name || '').trim();
  const mail = String(email || '').trim();
  if (!displayName) return { ok: false, error: 'Enter your name.', field: 'reg-name' };
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(mail)) return { ok: false, error: 'Enter your email address, for example name@example.com.', field: 'reg-email' };
  if (String(password || '').length < 8) return { ok: false, error: 'The password needs at least 8 characters.', field: 'reg-password' };
  if (!['patient', 'doctor', 'family'].includes(role)) return { ok: false, error: 'Choose who you are.', field: 'role-patient' };
  const body = { email: mail, password: String(password), display_name: displayName, role };
  const tel = String(phone || '').trim();
  if (tel) body.phone = tel;
  return { ok: true, body };
}
