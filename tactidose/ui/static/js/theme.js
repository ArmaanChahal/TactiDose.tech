/**
 * Dark / light high-contrast theme toggle, persisted in localStorage and shared
 * by all pages (theme-init.js applies it before first paint).
 */

import { icon } from './icons.js';

export const THEME_KEY = 'tactidose.theme';

export function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}

export function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* ignore: theme just will not persist */
  }
  document.dispatchEvent(new CustomEvent('tactidose:theme', { detail: { theme } }));
}

/**
 * Wire a button that shows the current theme in words ("Theme: dark") and
 * switches on click. `upper` renders the label in capitals (kiosk).
 */
export function initThemeToggle(button, { upper = false } = {}) {
  if (!button) return;
  const render = () => {
    const theme = currentTheme();
    const label = `Theme: ${theme === 'light' ? 'light' : 'dark'}`;
    button.replaceChildren(icon(theme === 'light' ? 'sun' : 'moon'), document.createTextNode(` ${upper ? label.toUpperCase() : label}`));
    button.setAttribute('aria-label', `${label}. Switch to the ${theme === 'light' ? 'dark' : 'light'} theme`);
  };
  button.addEventListener('click', () => {
    applyTheme(currentTheme() === 'light' ? 'dark' : 'light');
    render();
  });
  render();
}
