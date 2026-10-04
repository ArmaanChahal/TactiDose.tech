/**
 * Small "live updates" indicator for the caregiver and demo headers: shows the
 * EventStream status in words + icon (Live / Reconnecting… / Offline).
 */

import { icon } from './icons.js';

const LABELS = {
  idle: ['Connecting…', 'rotate', 'connecting'],
  connecting: ['Connecting…', 'rotate', 'connecting'],
  open: ['Live', 'dot', 'open'],
  reconnecting: ['Reconnecting…', 'warning', 'reconnecting'],
  closed: ['Offline', 'offline', 'closed'],
};

export function bindConnIndicator(el, stream) {
  if (!el) return;
  stream.onStatus((status) => {
    const [word, iconName, cls] = LABELS[status] || LABELS.connecting;
    el.className = `conn conn-${cls}`;
    el.replaceChildren(icon(iconName), document.createTextNode(` ${word}`));
    el.setAttribute('aria-label', `Live updates: ${word}`);
  });
}
