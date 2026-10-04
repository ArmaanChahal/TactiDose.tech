/**
 * Header and shared panels of the patient and care portals: who is signed in, sign
 * out, display settings (colours, text size, desktop alerts), the notification bell,
 * page notices, the live-updates indicator and the session watchdog.
 * Both patient.html and care.html contain the element ids used here.
 */

import { byId, createNotifier, errorState, initLiveRegions } from './dom.js';
import { hydrateIcons, icon } from './icons.js';
import { buildDisplaySettings } from './theme.js';
import { bindConnIndicator } from './conn.js';
import { bindPrefCheckbox } from './prefs.js';
import { createNotificationCenter, requestDesktopPermission } from './notifications.js';
import { roleName, signOut, watchSession } from './session.js';

const NOTICE_ICONS = { success: 'check-circle', error: 'warning', warning: 'warning', info: 'info' };

/** Prepare a page before the session is known (icons, live regions). */
export function preparePage() {
  initLiveRegions();
  hydrateIcons();
}

/** Full-page error with a retry button (e.g. the server cannot be reached). */
export function showPageError(err, retry) {
  const box = byId('page-error');
  if (!box) return;
  box.hidden = false;
  box.replaceChildren(errorState(err, retry, icon('warning')));
}

export function hidePageError() {
  const box = byId('page-error');
  if (box) box.hidden = true;
}

function wireSettings(prefs) {
  const btn = byId('settings-btn');
  const panel = byId('settings-panel');
  const closeBtn = byId('settings-close');
  buildDisplaySettings(byId('display-settings'));
  const open = () => {
    panel.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    panel.querySelector('h2')?.focus();
  };
  const close = () => {
    panel.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    btn.focus();
  };
  btn.addEventListener('click', () => (panel.hidden ? open() : close()));
  closeBtn.addEventListener('click', close);
  panel.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close();
  });

  const desktop = byId('desktop-alerts');
  const desktopStatus = byId('desktop-alerts-status');
  if (typeof Notification !== 'function') {
    desktop.disabled = true;
    desktopStatus.textContent = 'This browser cannot show desktop alerts.';
    return;
  }
  desktop.checked = prefs.get('desktopAlerts') && Notification.permission === 'granted';
  desktop.addEventListener('change', async () => {
    if (!desktop.checked) {
      prefs.set('desktopAlerts', false);
      desktopStatus.textContent = 'Desktop alerts are off.';
      return;
    }
    const permission = await requestDesktopPermission();
    if (permission === 'granted') {
      prefs.set('desktopAlerts', true);
      desktopStatus.textContent = 'Desktop alerts are on. They appear when this page is in the background.';
    } else {
      desktop.checked = false;
      prefs.set('desktopAlerts', false);
      desktopStatus.textContent = 'The browser did not allow alerts. You can allow them in the browser settings for this site.';
    }
  });
}

/**
 * Wire the shared header. Returns {notify, notifications}.
 * Options: me, stream, prefs, getOffset(), getNow(), onLiveNotification(n), patientName(id),
 * prefCheckboxes: {prefKey: elementId}, quietConnection, onNotificationsChange(items).
 */
export function initPortal({
  me,
  stream,
  prefs,
  getOffset = () => null,
  getNow = () => null,
  onLiveNotification = null,
  patientName = null,
  prefCheckboxes = {},
  quietConnection = false,
  onNotificationsChange = null,
}) {
  const who = byId('who');
  who.replaceChildren(icon('user'), document.createTextNode(` ${me.user.display_name} · ${roleName(me.user.role)}`));
  byId('signout').addEventListener('click', () => signOut());
  wireSettings(prefs);
  for (const [key, id] of Object.entries(prefCheckboxes)) bindPrefCheckbox(prefs, key, byId(id));
  bindConnIndicator(byId('conn'), stream, { quietWhenOpen: quietConnection });
  watchSession(stream);
  const notify = createNotifier(byId('page-notices'), { iconFor: (kind) => icon(NOTICE_ICONS[kind] || 'info') });
  const notifications = createNotificationCenter({
    bell: byId('bell'),
    count: byId('bell-count'),
    panel: byId('notif-panel'),
    list: byId('notif-list'),
    markAll: byId('notif-mark-all'),
    close: byId('notif-close'),
    toasts: byId('toasts'),
  }, { stream, prefs, getOffset, getNow, onLive: onLiveNotification, onChange: onNotificationsChange, patientName });
  return { notify, notifications };
}
