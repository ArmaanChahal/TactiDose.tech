/**
 * Inline SVG icons (24×24 stroke icons drawn with currentColor; no image files,
 * no icon fonts, works offline). Icons accompany words — they never carry
 * meaning alone — so by default they are hidden from assistive technology.
 */

import { SVG_NS } from './dom.js';

const CIRCLE = 'M12 2.75a9.25 9.25 0 1 0 0 18.5a9.25 9.25 0 1 0 0-18.5z';

/** Each icon is a list of path data strings; entries starting with "fill:" are filled shapes. */
const ICONS = {
  check: ['M5 12.5l4.5 4.5L19 7.5'],
  'check-circle': [CIRCLE, 'M7.5 12.5l3 3 6-6.5'],
  x: ['M6 6l12 12M18 6L6 18'],
  'x-circle': [CIRCLE, 'M8.5 8.5l7 7M15.5 8.5l-7 7'],
  warning: ['M12 3.5L2.5 20.5h19z', 'M12 10v4.5', 'M12 17.6v.1'],
  stop: ['M8 2.75h8l5.25 5.25v8L16 21.25H8L2.75 16V8z', 'M12 7.5v5.5', 'M12 16.4v.1'],
  hand: ['M8 12V6a1.5 1.5 0 0 1 3 0v5M11 11V4.5a1.5 1.5 0 0 1 3 0V11M14 11V6a1.5 1.5 0 0 1 3 0v8c0 4-2.6 7-6.5 7-2.4 0-3.9-1.3-5.2-3.2L3.6 14.9a1.6 1.6 0 0 1 2.6-1.8L8 15V12'],
  clock: [CIRCLE, 'M12 7v5l3.5 2'],
  bell: ['M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z', 'M10 20.5a2 2 0 0 0 4 0'],
  offline: ['M9 3v4M15 3v4', 'M7 7h10v4a5 5 0 0 1-10 0z', 'M12 16v5', 'M3 3l18 18'],
  open: ['M4 11h16v9H4z', 'M4 11l3-6h3', 'M20 11l-3-6h-3', 'M12 14v3'],
  repeat: ['M17 2.5l3 3-3 3', 'M4 11.5v-1a5 5 0 0 1 5-5h11', 'M7 21.5l-3-3 3-3', 'M20 12.5v1a5 5 0 0 1-5 5H4'],
  help: [CIRCLE, 'M9.25 9.25a2.85 2.85 0 1 1 3.9 2.65c-.75.3-1.15.9-1.15 1.7v.4', 'M12 17.2v.1'],
  dispense: ['M12 3v10', 'M7.5 9l4.5 4.5L16.5 9', 'M4 15v4.5h16V15'],
  list: ['M9 3.5h6v3H9z', 'M7 5H5.5v15.5h13V5H17', 'M8.5 11.5h7M8.5 15.5h5'],
  mic: ['M12 3a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z', 'M5.5 11a6.5 6.5 0 0 0 13 0', 'M12 17.5V21'],
  'mic-off': ['M12 3a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z', 'M5.5 11a6.5 6.5 0 0 0 13 0', 'M12 17.5V21', 'M3 3l18 18'],
  sun: ['M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8z', 'M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8'],
  moon: ['M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z'],
  'arrow-right': ['M4 12h15', 'M13 6l6 6-6 6'],
  dot: ['fill:M12 7a5 5 0 1 0 0 10a5 5 0 1 0 0-10z'],
  rotate: ['M20 12a8 8 0 1 1-2.35-5.65', 'M20 4v5h-5'],
  slash: [CIRCLE, 'M5.5 18.5l13-13'],
  camera: ['M4 8h3l2-3h6l2 3h3v11H4z', 'M12 9.5a3.5 3.5 0 1 0 0 7a3.5 3.5 0 1 0 0-7z'],
  upload: ['M12 16V4', 'M7 9l5-5 5 5', 'M4 16v4h16v-4'],
  info: [CIRCLE, 'M12 11v6', 'M12 7.6v.1'],
  home: ['M3.5 11.5L12 4l8.5 7.5', 'M6 10v10h12V10'],
  archive: ['M3.5 4.5h17v4h-17z', 'M5 8.5v11h14v-11', 'M10 12h4'],
  edit: ['M4 20h4L19 9l-4-4L4 16z', 'M13.5 6.5l4 4'],
  calendar: ['M4 6h16v14H4z', 'M4 10h16', 'M8 3.5v4M16 3.5v4'],
  chart: ['M4 4v16h16', 'M8 16v-5M12 16V8M16 16v-3'],
  lock: ['M6 11h12v9H6z', 'M8.5 11V8a3.5 3.5 0 0 1 7 0v3'],
  play: ['M7 4.5v15l12-7.5z'],
  plug: ['M9 3v4M15 3v4', 'M7 7h10v4a5 5 0 0 1-10 0z', 'M12 16v5'],
  terminal: ['M3.5 5h17v14h-17z', 'M7 9l3 3-3 3', 'M12 15h5'],
  gate: ['M4 21V9h16v12', 'M4 9l2-5h12l2 5', 'M9 14h6'],
  pending: [CIRCLE],
  spark: ['M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6'],
};

export const ICON_NAMES = Object.keys(ICONS);

/**
 * Build an icon element. With `label` it becomes role="img" and is announced;
 * without, it is decorative (aria-hidden).
 */
export function icon(name, { label = null, className = 'icon' } = {}) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', className);
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2.4');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('focusable', 'false');
  if (label) {
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', label);
  } else {
    svg.setAttribute('aria-hidden', 'true');
  }
  for (const d of ICONS[name] || ICONS.info) {
    const path = document.createElementNS(SVG_NS, 'path');
    if (d.startsWith('fill:')) {
      path.setAttribute('d', d.slice(5));
      path.setAttribute('fill', 'currentColor');
      path.setAttribute('stroke', 'none');
    } else {
      path.setAttribute('d', d);
    }
    svg.append(path);
  }
  return svg;
}

/** Replace the contents of every `[data-icon]` placeholder in `root` with its icon. */
export function hydrateIcons(root = document) {
  for (const el of root.querySelectorAll('[data-icon]')) {
    el.replaceChildren(icon(el.dataset.icon));
  }
}
