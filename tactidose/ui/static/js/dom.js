/**
 * Small DOM helpers shared by every CareBridge page.
 *
 * Safety rule: text that comes from the server (medication names, label-scan
 * output, error details) is only ever inserted as text nodes / textContent,
 * never as HTML, so it can never inject markup or script.
 *
 * This module touches `document` only inside functions, so pure modules that
 * import it can still be loaded (and unit-tested) outside a browser.
 */

export const SVG_NS = 'http://www.w3.org/2000/svg';

const BOOLEAN_PROPS = new Set(['hidden', 'disabled', 'checked', 'required', 'readOnly', 'multiple', 'selected', 'open', 'autofocus']);

let uidCounter = 0;

/** Unique id for ARIA wiring (labels, descriptions). */
export function uid(prefix = 'id') {
  uidCounter += 1;
  return `${prefix}-${uidCounter}`;
}

function appendChildren(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function applyProps(el, props) {
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null) continue;
    if (key === 'on') {
      for (const [type, handler] of Object.entries(value)) el.addEventListener(type, handler);
    } else if (key === 'class' || key === 'className') {
      const cls = Array.isArray(value) ? value.filter(Boolean).join(' ') : String(value);
      if (cls) el.setAttribute('class', cls);
    } else if (key === 'text') {
      el.textContent = String(value);
    } else if (key === 'dataset') {
      for (const [k, v] of Object.entries(value)) {
        if (v !== undefined && v !== null) el.dataset[k] = String(v);
      }
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(el.style, value);
    } else if (key === 'value') {
      el.value = value;
    } else if (BOOLEAN_PROPS.has(key)) {
      el[key] = Boolean(value);
    } else if (/^on[a-z]/i.test(key)) {
      throw new Error(`Inline handler "${key}" is not allowed; use the "on" map`);
    } else if (key.startsWith('aria-') || key === 'role') {
      el.setAttribute(key, String(value));
    } else if (value === false) {
      continue;
    } else {
      el.setAttribute(key === 'htmlFor' ? 'for' : key, value === true ? '' : String(value));
    }
  }
}

/**
 * Create an HTML element: h('button', {class: 'btn', on: {click}}, 'Label').
 * Children may be strings, numbers, nodes, arrays or null/false (skipped).
 */
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  applyProps(el, props);
  appendChildren(el, children);
  return el;
}

/** Create an SVG element (same props contract as h()). */
export function s(tag, props = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'on') {
      for (const [type, handler] of Object.entries(value)) el.addEventListener(type, handler);
    } else if (key === 'text') {
      el.textContent = String(value);
    } else if (/^on[a-z]/i.test(key)) {
      throw new Error(`Inline handler "${key}" is not allowed; use the "on" map`);
    } else {
      el.setAttribute(key === 'className' ? 'class' : key, value === true ? '' : String(value));
    }
  }
  appendChildren(el, children);
  return el;
}

/** getElementById that reports (instead of silently returning null) a missing element. */
export function byId(id) {
  const el = document.getElementById(id);
  if (!el) console.error(`CareBridge UI: element #${id} not found`);
  return el;
}

export function $(selector, root = document) {
  return root.querySelector(selector);
}

export function $$(selector, root = document) {
  return Array.from(root.querySelectorAll(selector));
}

/** Replace all children of `el` with `children` (strings become text nodes). */
export function replaceChildren(el, ...children) {
  el.replaceChildren();
  appendChildren(el, children);
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

export function debounce(fn, ms) {
  let timer = null;
  const wrapped = (...args) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, ms);
  };
  wrapped.cancel = () => {
    if (timer) clearTimeout(timer);
    timer = null;
  };
  return wrapped;
}

/** Human-readable message for any thrown value (ApiError carries the server's detail). */
export function errorText(err) {
  if (!err) return 'Unknown error';
  if (typeof err === 'string') return err;
  return err.message || String(err);
}

// ------------------------------------------------------------------ loading / empty / error states

/**
 * Mark a container as loading. On first load (no children) a "Loading…" message is
 * shown; on refresh the previous content stays visible at reduced opacity.
 */
export function setLoading(el, loading, label = 'Loading…') {
  el.setAttribute('aria-busy', loading ? 'true' : 'false');
  if (loading && !el.firstChild) {
    el.append(h('p', { class: 'state-msg', 'data-state': 'loading' }, label));
  }
  if (!loading) {
    for (const placeholder of el.querySelectorAll('[data-state="loading"]')) placeholder.remove();
  }
  el.classList.toggle('is-refreshing', loading && !el.querySelector('[data-state="loading"]'));
}

export function emptyState(message, extra = null) {
  return h('div', { class: 'state-msg', 'data-state': 'empty' }, h('span', {}, message), extra);
}

/** Error box with the server detail and an optional "Try again" button. */
export function errorState(err, retry = null, iconEl = null) {
  const body = h('div', { class: 'state-error-body' },
    h('strong', {}, 'Something went wrong'),
    h('span', {}, errorText(err)),
    retry ? h('button', { type: 'button', class: 'btn btn-small', on: { click: retry } }, 'Try again') : null,
  );
  return h('div', { class: 'state-msg state-error', role: 'alert', 'data-state': 'error' }, iconEl, body);
}

// ------------------------------------------------------------------ live announcements & notices

function liveRegion(id, politeness) {
  let el = document.getElementById(id);
  if (!el) {
    el = h('div', { id, class: 'visually-hidden', 'aria-live': politeness, 'aria-atomic': 'true' });
    if (politeness === 'assertive') el.setAttribute('role', 'alert');
    document.body.append(el);
  }
  return el;
}

/**
 * Create the hidden live regions up front: screen readers often miss the first
 * change in a region that was only just inserted. Call once when a page starts.
 */
export function initLiveRegions() {
  liveRegion('td-live-polite', 'polite');
  liveRegion('td-live-assertive', 'assertive');
}

/** Announce a short message to screen readers without showing it. */
export function announce(message, { assertive = false } = {}) {
  const el = liveRegion(assertive ? 'td-live-assertive' : 'td-live-polite', assertive ? 'assertive' : 'polite');
  el.textContent = '';
  // A new text node on the next frame makes repeated identical messages announce again.
  setTimeout(() => {
    el.textContent = message;
  }, 50);
}

/**
 * Visible notification list (success/info auto-dismiss; errors stay until closed).
 * `container` must NOT itself be a live region: each message is announced once,
 * politely, or assertively for errors.
 */
export function createNotifier(container, { iconFor = null } = {}) {
  return function notify(message, kind = 'info') {
    const item = h('div', { class: `notice notice-${kind}` },
      iconFor ? iconFor(kind) : null,
      h('span', { class: 'notice-text' }, message),
    );
    const close = h('button', {
      type: 'button',
      class: 'btn btn-small notice-close',
      'aria-label': 'Dismiss message',
      on: { click: () => item.remove() },
    }, 'Dismiss');
    item.append(close);
    container.append(item);
    while (container.children.length > 4) container.firstElementChild.remove();
    announce(message, { assertive: kind === 'error' });
    if (kind !== 'error') setTimeout(() => item.remove(), 8000);
    return item;
  };
}

// ------------------------------------------------------------------ dialogs

function supportsDialog() {
  return typeof HTMLDialogElement === 'function' && 'showModal' in HTMLDialogElement.prototype;
}

function buildDialog({ title, message, iconEl, fields, actions }) {
  const titleId = uid('dlg-title');
  const descId = uid('dlg-desc');
  const form = h('form', { method: 'dialog', class: 'dialog-form' },
    h('h2', { id: titleId, class: 'dialog-title' }, iconEl, title),
    message ? h('p', { id: descId, class: 'dialog-message' }, message) : null,
    fields,
    h('div', { class: 'dialog-actions' }, actions),
  );
  const dlg = h('dialog', { class: 'dialog', 'aria-labelledby': titleId }, form);
  if (message) dlg.setAttribute('aria-describedby', descId);
  return dlg;
}

function runDialog(dlg, initialFocus, collect) {
  return new Promise((resolve) => {
    const previous = document.activeElement;
    dlg.addEventListener('close', () => {
      const result = collect(dlg.returnValue);
      dlg.remove();
      if (previous && typeof previous.focus === 'function' && document.contains(previous)) previous.focus();
      resolve(result);
    });
    document.body.append(dlg);
    dlg.showModal();
    if (initialFocus) initialFocus.focus();
  });
}

/**
 * Modal confirmation. Resolves {ok, note}. `noteLabel` adds an optional free-text note field.
 * Falls back to window.confirm when <dialog> is unsupported.
 */
export function confirmDialog({
  title,
  message = '',
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  danger = false,
  noteLabel = null,
  iconEl = null,
} = {}) {
  if (!supportsDialog()) {
    const ok = window.confirm(`${title}\n\n${message}`);
    return Promise.resolve({ ok, note: '' });
  }
  let note = null;
  const fields = [];
  if (noteLabel) {
    const noteId = uid('dlg-note');
    note = h('textarea', { id: noteId, rows: '2', maxlength: '255' });
    fields.push(h('div', { class: 'field' }, h('label', { for: noteId }, noteLabel), note));
  }
  const cancel = h('button', { type: 'submit', value: 'cancel', formnovalidate: true, class: 'btn' }, cancelLabel);
  const confirm = h('button', { type: 'submit', value: 'confirm', class: danger ? 'btn btn-danger' : 'btn btn-primary' }, confirmLabel);
  const dlg = buildDialog({ title, message, iconEl, fields, actions: [cancel, confirm] });
  return runDialog(dlg, cancel, (value) => ({ ok: value === 'confirm', note: note ? note.value.trim() : '' }));
}

/** Modal text prompt. Resolves the entered string, or null when cancelled. */
export function promptDialog({
  title,
  message = '',
  label = 'Value',
  type = 'text',
  inputmode = null,
  autocomplete = 'off',
  confirmLabel = 'OK',
  iconEl = null,
} = {}) {
  if (!supportsDialog()) {
    const value = window.prompt(`${title}\n${message}`);
    return Promise.resolve(value === null ? null : value.trim());
  }
  const inputId = uid('dlg-input');
  const input = h('input', { id: inputId, type, required: true, autocomplete, inputmode });
  const field = h('div', { class: 'field' }, h('label', { for: inputId }, label), input);
  const cancel = h('button', { type: 'submit', value: 'cancel', formnovalidate: true, class: 'btn' }, 'Cancel');
  const ok = h('button', { type: 'submit', value: 'ok', class: 'btn btn-primary' }, confirmLabel);
  // OK comes first in the DOM so pressing Enter in the field (implicit submission) means OK.
  const dlg = buildDialog({ title, message, iconEl, fields: [field], actions: [ok, cancel] });
  return runDialog(dlg, input, (value) => (value === 'ok' && input.value.trim() ? input.value.trim() : null));
}

/** Pretty JSON for viewers (always inserted with textContent). */
export function prettyJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
