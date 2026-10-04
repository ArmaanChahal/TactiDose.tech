/**
 * Notifications for the signed-in user (ARCHITECTURE §10): a bell with the unread
 * count, a list panel (GET /api/notifications, POST /api/notifications/read), a live
 * toast for every new `notification` SSE event (announced through an aria-live region),
 * an optional desktop Notification when the page is in the background (only after the
 * person allowed it with a click), and an `onLive` hook the patient portal uses to say
 * "pill dropped" out loud.
 *
 * Server text (titles, bodies) is only ever inserted as text.
 */

import { get, post } from './api.js';
import { announce, errorText, h } from './dom.js';
import { icon } from './icons.js';
import { formatWhen } from './format.js';
import { notificationInfo } from './words.js';

const MAX_ITEMS = 100;
const MAX_TOASTS = 3;
const TOAST_MS = 12000;

function stamp(n) {
  const t = Date.parse(String(n?.created_at || '').replace(/(\.\d{3})\d+/, '$1'));
  return Number.isFinite(t) ? t : 0;
}

/** Merge by notification_id (incoming wins), newest first, at most `max`. */
export function mergeNotifications(existing, incoming, max = MAX_ITEMS) {
  const byId = new Map();
  for (const n of [...(existing || []), ...(incoming || [])]) {
    if (!n || n.notification_id === undefined || n.notification_id === null) continue;
    byId.set(n.notification_id, { ...(byId.get(n.notification_id) || {}), ...n });
  }
  return [...byId.values()]
    .sort((a, b) => stamp(b) - stamp(a) || Number(b.notification_id) - Number(a.notification_id))
    .slice(0, max);
}

export function unreadCount(list, filter = null) {
  return (list || []).filter((n) => !n.read_at && (!filter || filter(n))).length;
}

/** One sentence for screen readers / speech: "Pill dropped. Vitamin C dropped from container 1." */
export function notificationSpeech(n) {
  const info = notificationInfo(n?.kind);
  const title = String(n?.title || info.word).trim();
  const body = String(n?.body || '').trim();
  const end = /[.!?]$/.test(title) ? '' : '.';
  return body ? `${title}${end} ${body}` : `${title}${end}`;
}

/** Render `items` into `listEl`. `onRead(ids)` marks some as read. */
export function renderNotificationList(listEl, items, { onRead = null, offsetMin = null, nowLocal = null, patientName = null, empty = 'No notifications yet.' } = {}) {
  if (!items.length) {
    listEl.replaceChildren(h('li', { class: 'state-msg', 'data-state': 'empty' }, empty));
    return;
  }
  listEl.replaceChildren(...items.map((n) => {
    const info = notificationInfo(n.kind);
    const unread = !n.read_at;
    const when = n.created_at ? formatWhen(n.created_at, nowLocal, offsetMin) : '';
    const who = patientName ? patientName(n.patient_id) : null;
    return h('li', { class: `notif-item${unread ? ' is-unread' : ''}` },
      icon(info.icon, { className: `icon tone-${info.tone}` }),
      h('div', { class: 'notif-title' },
        unread ? h('span', { class: 'new-word' }, 'New') : null,
        h('span', {}, n.title || info.word)),
      n.body ? h('div', { class: 'notif-body' }, n.body) : null,
      h('div', { class: 'notif-meta' },
        h('span', {}, [info.word, who, when].filter(Boolean).join(' · ')),
        unread && onRead
          ? h('button', {
            type: 'button',
            class: 'btn btn-small',
            'aria-label': `Mark as read: ${n.title || info.word}`,
            on: { click: () => onRead([n.notification_id]) },
          }, 'Mark as read')
          : null));
  }));
}

function canNotifyDesktop() {
  return typeof Notification === 'function';
}

/** Ask for desktop-notification permission (must run from a click). Resolves the permission. */
export async function requestDesktopPermission() {
  if (!canNotifyDesktop()) return 'unsupported';
  if (Notification.permission === 'granted' || Notification.permission === 'denied') return Notification.permission;
  try {
    return await Notification.requestPermission();
  } catch {
    return 'denied';
  }
}

/**
 * Wire the bell + panel + toasts. Elements: {bell, count, panel, list, markAll, close, toasts}.
 * Options: stream (EventStream), prefs (desktopAlerts), getOffset(), getNow(), onLive(n),
 * patientName(id), onChange(items).
 */
export function createNotificationCenter(els, {
  stream,
  prefs = null,
  getOffset = () => null,
  getNow = () => null,
  onLive = null,
  onChange = null,
  patientName = null,
} = {}) {
  let items = [];
  const known = new Set();
  let loaded = false;

  function setCount() {
    const n = unreadCount(items);
    if (els.count) {
      els.count.textContent = String(n);
      els.count.dataset.count = String(n);
    }
    if (els.bell) {
      els.bell.setAttribute('aria-label', n ? `Notifications, ${n} new` : 'Notifications, none new');
    }
  }

  function render() {
    setCount();
    if (els.list && !els.panel?.hidden) {
      renderNotificationList(els.list, items, { onRead: markRead, offsetMin: getOffset(), nowLocal: getNow(), patientName });
    }
    if (els.markAll) els.markAll.disabled = unreadCount(items) === 0;
    if (onChange) onChange(items);
  }

  async function load() {
    try {
      const list = await get('/api/notifications?limit=50');
      items = mergeNotifications([], Array.isArray(list) ? list : []);
      for (const n of items) known.add(n.notification_id);
      loaded = true;
      render();
    } catch (err) {
      if (els.list && !els.panel?.hidden) {
        els.list.replaceChildren(h('li', { class: 'state-msg state-error', role: 'alert' }, `Could not load notifications: ${errorText(err)}`));
      }
    }
  }

  async function markRead(ids = null) {
    try {
      await post('/api/notifications/read', ids ? { ids } : {});
      const now = new Date().toISOString();
      items = items.map((n) => (!ids || ids.includes(n.notification_id) ? { ...n, read_at: n.read_at || now } : n));
      render();
      announce(ids ? 'Marked as read.' : 'All notifications marked as read.');
    } catch (err) {
      announce(`Could not mark as read: ${errorText(err)}`, { assertive: true });
    }
  }

  /**
   * Toasts float over the page: reserve their height at the bottom of the page and in
   * focus scrolling, so they never hide content or the focused control for long.
   */
  function updateInset() {
    if (!els.toasts || typeof document === 'undefined') return;
    const height = els.toasts.children.length ? Math.ceil(els.toasts.getBoundingClientRect().height) + 16 : 0;
    document.body.style.paddingBottom = height ? `${height}px` : '';
    document.documentElement.style.scrollPaddingBottom = height ? `${height}px` : '';
  }

  function dismissToast(item) {
    item.remove();
    updateInset();
  }

  function toast(n) {
    if (!els.toasts) return;
    const info = notificationInfo(n.kind);
    const item = h('div', { class: `toast tone-${info.tone}` },
      icon(info.icon, { className: `icon tone-${info.tone}` }),
      h('div', { class: 'toast-title' }, n.title || info.word),
      n.body ? h('div', { class: 'toast-body' }, n.body) : null);
    item.append(h('button', {
      type: 'button',
      class: 'btn btn-small',
      'aria-label': `Dismiss: ${n.title || info.word}`,
      on: { click: () => dismissToast(item) },
    }, 'Dismiss'));
    els.toasts.append(item);
    while (els.toasts.children.length > MAX_TOASTS) els.toasts.firstElementChild.remove();
    updateInset();
    // Keep the toast while it has focus or the pointer is over it.
    setTimeout(function hide() {
      if (!item.isConnected) return;
      if (item.contains(document.activeElement) || item.matches(':hover')) {
        setTimeout(hide, 3000);
        return;
      }
      dismissToast(item);
    }, info.urgent ? TOAST_MS * 2 : TOAST_MS);
  }

  function desktop(n) {
    if (!prefs?.get('desktopAlerts') || !canNotifyDesktop() || Notification.permission !== 'granted') return;
    if (typeof document !== 'undefined' && document.visibilityState === 'visible') return;
    try {
      const note = new Notification(n.title || notificationInfo(n.kind).word, {
        body: n.body || '',
        tag: `tactidose-${n.notification_id}`,
        icon: '/static/img/favicon.svg',
      });
      note.addEventListener('click', () => {
        window.focus();
        note.close();
      });
    } catch {
      /* e.g. Android needs a service worker: the in-page toast is enough */
    }
  }

  stream?.on('notification', (n, _env, meta) => {
    if (!n || n.notification_id === undefined) return;
    const isNew = !known.has(n.notification_id);
    known.add(n.notification_id);
    items = mergeNotifications(items, [n]);
    render();
    if (!isNew || meta?.replayed || !loaded) return;
    toast(n);
    announce(notificationSpeech(n), { assertive: Boolean(notificationInfo(n.kind).urgent) });
    desktop(n);
    if (onLive) {
      try {
        onLive(n);
      } catch (err) {
        console.error('CareBridge notifications: onLive failed', err);
      }
    }
  });

  function open() {
    if (!els.panel) return;
    els.panel.hidden = false;
    els.bell?.setAttribute('aria-expanded', 'true');
    render();
    load();
    els.panel.querySelector('h2')?.focus();
  }

  function close({ focusBell = true } = {}) {
    if (!els.panel) return;
    els.panel.hidden = true;
    els.bell?.setAttribute('aria-expanded', 'false');
    if (focusBell) els.bell?.focus();
  }

  els.bell?.addEventListener('click', () => (els.panel?.hidden ? open() : close()));
  els.close?.addEventListener('click', () => close());
  els.markAll?.addEventListener('click', () => markRead(null));
  els.panel?.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close();
  });
  // Escape (outside dialogs) dismisses the toasts.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !els.toasts?.children.length || document.querySelector('dialog[open]')) return;
    els.toasts.replaceChildren();
    updateInset();
  });

  return {
    load,
    open,
    close,
    markRead,
    get items() {
      return items;
    },
  };
}
