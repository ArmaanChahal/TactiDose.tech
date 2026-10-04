/**
 * Sign-in page controller (login.html): sign in (POST /api/auth/login), create an account
 * (POST /api/auth/register), then
 *  - patients see their Patient ID and link code to share with their care team;
 *  - doctor/family accounts are guided to "Link a patient" (POST /api/care/links).
 * After signing in, people go back to ?next= when it is a page their role may use,
 * otherwise to their own portal.
 */

import { get, post, safeNext } from './api.js';
import { byId, h, initLiveRegions } from './dom.js';
import { hydrateIcons, icon } from './icons.js';
import { initThemeCycleButton } from './theme.js';
import { destinationFor, fetchSession, isCaregiver, roleName, signOut } from './session.js';
import { linkBody, linkErrorText } from './links.js';
import { spellOut } from './format.js';
import { DEMO_ACCOUNTS, DEMO_PASSWORD, authErrorText, registerBody } from './authforms.js';

initLiveRegions();
hydrateIcons();
initThemeCycleButton(byId('theme-btn'));

const next = safeNext(new URLSearchParams(window.location.search).get('next'));

function showError(id, message) {
  const el = byId(id);
  el.textContent = message || '';
  el.hidden = !message;
}

function setMode(mode, { focus = true } = {}) {
  const signin = mode === 'signin';
  byId('signin-section').hidden = !signin;
  byId('register-section').hidden = signin;
  byId('show-signin').setAttribute('aria-pressed', String(signin));
  byId('show-register').setAttribute('aria-pressed', String(!signin));
  if (focus) byId(signin ? 'signin-title' : 'register-title').focus();
}

function onlyStep(id) {
  for (const section of ['signin-section', 'register-section', 'mode-switch', 'signed-in']) byId(section).hidden = true;
  byId(id).hidden = false;
  byId(id).querySelector('h2')?.focus();
}

function bindShowPassword(buttonId, inputId) {
  const button = byId(buttonId);
  const input = byId(inputId);
  button.addEventListener('click', () => {
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    button.textContent = show ? 'Hide' : 'Show';
    button.setAttribute('aria-pressed', String(show));
    button.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
  });
  button.setAttribute('aria-label', 'Show password');
}

// ------------------------------------------------------------------ sign in

byId('signin-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = byId('signin-email').value.trim();
  const password = byId('signin-password').value;
  if (!email) {
    showError('signin-error', 'Enter your email address.');
    byId('signin-email').focus();
    return;
  }
  if (!password) {
    showError('signin-error', 'Enter your password.');
    byId('signin-password').focus();
    return;
  }
  showError('signin-error', '');
  const submit = byId('signin-submit');
  submit.disabled = true;
  submit.textContent = 'Signing in…';
  try {
    const resp = await post('/api/auth/login', { email, password }, { redirectOn401: false });
    window.location.assign(destinationFor(resp?.user?.role, next));
  } catch (err) {
    showError('signin-error', authErrorText(err, 'signin'));
    byId('signin-password').focus();
    submit.disabled = false;
    submit.textContent = 'Sign in';
  }
});

// ------------------------------------------------------------------ register

function selectedRole() {
  return document.querySelector('input[name="role"]:checked')?.value || 'patient';
}

byId('register-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const check = registerBody({
    name: byId('reg-name').value,
    email: byId('reg-email').value,
    password: byId('reg-password').value,
    role: selectedRole(),
    phone: byId('reg-phone').value,
  });
  if (!check.ok) {
    showError('register-error', check.error);
    byId(check.field)?.focus();
    return;
  }
  showError('register-error', '');
  const submit = byId('register-submit');
  submit.disabled = true;
  submit.textContent = 'Creating your account…';
  try {
    const resp = await post('/api/auth/register', check.body, { redirectOn401: false });
    const role = resp?.user?.role || check.body.role;
    if (role === 'patient' && !resp?.patient?.link_code) {
      // The codes are also part of GET /api/auth/me for patients.
      const me = await fetchSession().catch(() => null);
      if (me?.patient) resp.patient = { ...(resp.patient || {}), ...me.patient };
    }
    if (role === 'patient') showPatientCodes(resp);
    else if (isCaregiver({ role })) onlyStep('link-step');
    else window.location.assign(destinationFor(role, next));
  } catch (err) {
    showError('register-error', authErrorText(err, 'register'));
    submit.disabled = false;
    submit.textContent = 'Create account';
  }
});

function showPatientCodes(resp) {
  const pid = resp?.patient?.patient_id ?? resp?.user?.user_id;
  const code = resp?.patient?.link_code || '';
  byId('codes-pid').textContent = pid ? String(pid) : '–';
  byId('codes-code').textContent = code || 'See "Care team" on your page';
  byId('codes-pid-spelled').textContent = pid ? `Spelled out: ${spellOut(String(pid))}` : '';
  byId('codes-code-spelled').textContent = code ? `Spelled out: ${spellOut(code)}` : '';
  byId('codes-continue').setAttribute('href', destinationFor('patient', next));
  byId('codes-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(`CareBridge — Patient ID: ${pid}, link code: ${code}`);
      byId('codes-status').textContent = 'Copied. You can paste it into a message.';
    } catch {
      byId('codes-status').textContent = `Copying is not possible here. The codes are: Patient ID ${pid}, link code ${code}.`;
    }
  });
  onlyStep('patient-codes');
}

// ------------------------------------------------------------------ caregiver: link a patient

byId('link-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const check = linkBody(byId('link-pid').value, byId('link-code').value);
  if (!check.ok) {
    showError('link-error', check.error);
    byId(check.field === 'patient_id' ? 'link-pid' : 'link-code').focus();
    return;
  }
  showError('link-error', '');
  const submit = byId('link-submit');
  submit.disabled = true;
  try {
    await post('/api/care/links', check.body);
    window.location.assign(`/care#patient-${check.body.patient_id}`);
  } catch (err) {
    showError('link-error', linkErrorText(err));
    submit.disabled = false;
  }
});

// ------------------------------------------------------------------ start

byId('show-signin').addEventListener('click', () => setMode('signin'));
byId('show-register').addEventListener('click', () => setMode('register'));
bindShowPassword('signin-show', 'signin-password');
bindShowPassword('reg-show', 'reg-password');
byId('signed-in-signout').addEventListener('click', () => signOut());

function showDemoAccounts() {
  const box = byId('demo-account-buttons');
  box.replaceChildren(...DEMO_ACCOUNTS.map((a) => h('button', {
    type: 'button',
    class: 'btn',
    on: {
      click: () => {
        byId('signin-email').value = a.email;
        byId('signin-password').value = DEMO_PASSWORD;
        byId('signin-submit').focus();
      },
    },
  }, icon('user'), a.label)));
  byId('demo-accounts').hidden = false;
}

async function start() {
  if (window.location.hash === '#register') setMode('register', { focus: false });
  try {
    const health = await get('/api/health', { redirectOn401: false });
    const cfg = health && typeof health === 'object' ? { ...(health.config || {}), ...health } : {};
    if (cfg.demo_mode === true) showDemoAccounts();
    if (cfg.allow_registration === false) {
      byId('register-disabled').hidden = false;
      byId('register-form').hidden = true;
    }
  } catch {
    /* health is optional for this page */
  }
  try {
    const me = await fetchSession();
    if (me?.user) {
      const dest = destinationFor(me.user.role, next);
      byId('signed-in-text').textContent = `You are signed in as ${me.user.display_name} (${roleName(me.user.role)}).`;
      byId('signed-in-continue').setAttribute('href', dest);
      byId('signed-in-continue').textContent = me.user.role === 'patient' ? 'Continue to my pills' : 'Continue to the care portal';
      byId('signed-in').hidden = false;
    }
  } catch (err) {
    const box = byId('page-error');
    box.hidden = false;
    box.replaceChildren(h('p', { class: 'state-msg state-error', role: 'alert' }, icon('warning'), ` ${err.message}`));
  }
}

start();
