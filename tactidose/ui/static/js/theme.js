/**
 * Display settings shared by all pages: colour theme (white on black — the default —,
 * black on white, yellow on black) and text size, persisted in localStorage.
 * theme-init.js applies them before first paint; this module changes them.
 */

import { h, uid } from './dom.js';
import { icon } from './icons.js';

export const THEME_KEY = 'tactidose.theme';
export const TEXT_SIZE_KEY = 'tactidose.textSize';

export const THEMES = Object.freeze([
  { id: 'dark', label: 'White on black' },
  { id: 'light', label: 'Black on white' },
  { id: 'yellow', label: 'Yellow on black' },
]);

export const TEXT_SIZES = Object.freeze([
  { id: 'normal', label: 'Standard' },
  { id: 'large', label: 'Large' },
  { id: 'xlarge', label: 'Extra large' },
]);

function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: the choice lasts until the page is closed */
  }
}

export function currentTheme() {
  const t = document.documentElement.getAttribute('data-theme');
  return THEMES.some((x) => x.id === t) ? t : 'light';
}

export function applyTheme(theme) {
  const id = THEMES.some((x) => x.id === theme) ? theme : 'light';
  document.documentElement.setAttribute('data-theme', id);
  store(THEME_KEY, id);
  document.dispatchEvent(new CustomEvent('tactidose:theme', { detail: { theme: id } }));
}

export function currentTextSize() {
  const s = document.documentElement.getAttribute('data-text-size');
  return TEXT_SIZES.some((x) => x.id === s) ? s : 'normal';
}

export function applyTextSize(size) {
  const id = TEXT_SIZES.some((x) => x.id === size) ? size : 'normal';
  if (id === 'normal') document.documentElement.removeAttribute('data-text-size');
  else document.documentElement.setAttribute('data-text-size', id);
  store(TEXT_SIZE_KEY, id);
}

function radioGroup(legend, name, options, current, onChange) {
  const inputs = options.map((opt) => {
    const id = uid(`${name}-${opt.id}`);
    const input = h('input', { type: 'radio', id, name, value: opt.id, checked: opt.id === current });
    input.addEventListener('change', () => {
      if (input.checked) onChange(opt.id);
    });
    return h('div', { class: 'radio-row' }, input, h('label', { for: id }, opt.label));
  });
  return h('fieldset', { class: 'choice-fieldset' }, h('legend', {}, legend), h('div', { class: 'choice-list' }, inputs));
}

/** Append "Colours" and "Text size" radio groups to `container`. */
export function buildDisplaySettings(container) {
  if (!container) return;
  container.append(
    radioGroup('Colours', uid('theme'), THEMES, currentTheme(), applyTheme),
    radioGroup('Text size', uid('textsize'), TEXT_SIZES, currentTextSize(), applyTextSize),
  );
}

/** A single button that cycles through the colour themes (demo panel, kiosk). */
export function initThemeCycleButton(button) {
  if (!button) return;
  const render = () => {
    const theme = THEMES.find((t) => t.id === currentTheme()) || THEMES[0];
    const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    button.replaceChildren(icon(theme.id === 'light' ? 'sun' : 'moon'), document.createTextNode(` Colours: ${theme.label}`));
    button.setAttribute('aria-label', `Colours: ${theme.label}. Switch to ${next.label}`);
  };
  button.addEventListener('click', () => {
    const index = THEMES.findIndex((t) => t.id === currentTheme());
    applyTheme(THEMES[(index + 1) % THEMES.length].id);
    render();
  });
  render();
}
