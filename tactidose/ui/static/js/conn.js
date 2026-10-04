/**
 * "Live updates" indicator: shows the EventStream status in words + icon
 * (Live / Reconnecting… / Offline). With `quietWhenOpen` it is hidden while everything
 * works and only appears when live updates are interrupted.
 */

import { icon } from './icons.js';

const LABELS = {
  idle: ['Connecting…', 'rotate', 'connecting'],
  connecting: ['Connecting…', 'rotate', 'connecting'],
  open: ['Connected', 'dot', 'open'],
  reconnecting: ['Reconnecting…', 'warning', 'reconnecting'],
  closed: ['Offline', 'offline', 'closed'],
};

export function bindConnIndicator(el, stream, { quietWhenOpen = false } = {}) {
  if (!el) return;
  stream.onStatus((status) => {
    const [word, iconName, cls] = LABELS[status] || LABELS.connecting;
    el.className = `conn conn-${cls}`;
    el.replaceChildren(icon(iconName), document.createTextNode(` ${word}`));
    el.setAttribute('aria-label', `Connection: ${word}`);
    // A healthy connection is quiet; never show a decorative Live badge.
    // Reconnecting and Offline remain visible and announced.
    el.hidden = status === 'open' || (quietWhenOpen && (status === 'idle' || status === 'connecting'));
  });
}
