/**
 * Care portal "Cooldown" tab (doctor/family only): the device's global cooldown
 * (minutes after any drop during which button/assistant drops are refused) and whether
 * scheduled pills drop automatically. GET/PATCH /api/patients/{pid}/settings.
 */

import { get, patch } from '../api.js';
import { byId, errorState, errorText, h, replaceChildren } from '../dom.js';
import { icon } from '../icons.js';
import { formatDuration } from '../format.js';
import { lazyPanel } from './panel.js';

export const MAX_COOLDOWN_MINUTES = 1440;

/** Validate the form. Returns {ok, body} or {ok: false, error}. */
export function settingsBody(cooldownText, autoDrop) {
  const t = String(cooldownText ?? '').trim();
  if (!/^\d{1,4}$/.test(t)) return { ok: false, error: 'Enter the waiting time as whole minutes, for example 60.' };
  const minutes = Number(t);
  if (minutes > MAX_COOLDOWN_MINUTES) return { ok: false, error: 'The waiting time can be at most 1440 minutes (24 hours).' };
  return { ok: true, body: { manual_cooldown_minutes: minutes, auto_drop_enabled: Boolean(autoDrop) } };
}

/** ctx: {pid, notify(message, kind), onChanged()} */
export function createSettingsTab(ctx) {
  const form = byId('settings-form');
  const cooldown = byId('set-cooldown');
  const auto = byId('set-auto');
  const facts = byId('set-facts');
  const errorEl = byId('settings-error');
  const submit = byId('settings-submit');
  let loadedFor = null;

  function showError(message) {
    errorEl.textContent = message || '';
    errorEl.hidden = !message;
  }

  function render(s) {
    cooldown.value = String(s.manual_cooldown_minutes ?? '');
    auto.checked = s.auto_drop_enabled !== false;
    const minutes = Number(s.manual_cooldown_minutes) || 0;
    replaceChildren(facts,
      h('dt', {}, 'Now'), h('dd', {}, minutes ? `Wait ${formatDuration(minutes * 60)} after any drop` : 'No waiting time'),
      h('dt', {}, 'Device'), h('dd', {}, s.device_id || '–'),
      h('dt', {}, 'Containers'), h('dd', {}, String(s.num_slots ?? '–')));
  }

  async function load() {
    if (!ctx.pid) return;
    const pid = ctx.pid;
    showError('');
    try {
      const s = await get(`/api/patients/${pid}/settings`);
      if (pid !== ctx.pid) return;
      loadedFor = pid;
      render(s || {});
    } catch (err) {
      replaceChildren(facts, errorState(err, load, icon('warning')));
    }
  }

  form.addEventListener('input', () => showError(''));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const check = settingsBody(cooldown.value, auto.checked);
    if (!check.ok) {
      showError(check.error);
      cooldown.focus();
      return;
    }
    if (loadedFor !== ctx.pid) {
      showError('The current settings are not loaded yet, so nothing was saved. Use "Try again" above, then save.');
      return;
    }
    submit.disabled = true;
    try {
      const s = await patch(`/api/patients/${ctx.pid}/settings`, check.body);
      render(s || check.body);
      ctx.notify('Saved. The new waiting time applies to the next drop request.', 'success');
      ctx.onChanged();
    } catch (err) {
      showError(errorText(err));
    } finally {
      submit.disabled = false;
    }
  });

  const panel = lazyPanel(load);
  return {
    show: panel.show,
    hide: panel.hide,
    markStale: panel.markStale,
    reset() {
      loadedFor = null;
      form.reset();
      facts.replaceChildren();
      panel.reset();
    },
  };
}
