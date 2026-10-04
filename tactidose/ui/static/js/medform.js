/**
 * Medication form binding (care portal): manual entry, editing, and reviewing an
 * UNCONFIRMED label scan. The form contains fields named name, strength,
 * instructions_text, warnings (one per line) and the checkbox `confirmed`
 * ("I confirm this information is correct").
 *
 * Nothing is submitted unless a person ticked the confirmation box: the body carries
 * `confirmed: true` only because a human confirmed it (docs/API.md: 422 otherwise).
 */

import { errorText } from './dom.js';

/** Split a textarea into trimmed, non-empty lines. */
export function linesOf(text) {
  return String(text || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

/**
 * Validate the current values. Returns {ok, body, error, field}. `body` matches
 * POST/PATCH /api/patients/{pid}/medications and POST …/scans/{scan_id}/confirm.
 */
export function readMedicationForm(form, { keepEmpty = false } = {}) {
  const value = (name) => (form.elements.namedItem(name)?.value ?? '').trim();
  const name = value('name');
  if (!name) return { ok: false, error: 'Enter the medication name exactly as printed on the label.', field: 'name' };
  if (name.length > 200) return { ok: false, error: 'The name is too long (200 characters at most).', field: 'name' };
  const confirmBox = form.elements.namedItem('confirmed');
  if (!confirmBox || !confirmBox.checked) {
    return { ok: false, error: "Check every field, then tick 'I confirm this information is correct'.", field: 'confirmed' };
  }
  const body = { name };
  for (const key of ['strength', 'instructions_text']) {
    const v = value(key);
    if (v || keepEmpty) body[key] = v;
  }
  body.warnings = linesOf(form.elements.namedItem('warnings')?.value);
  body.confirmed = true;
  return { ok: true, body };
}

/** Put values into the form (Medication / scan extraction -> fields). Unticks the confirm box. */
export function fillMedicationForm(form, values = {}) {
  const set = (name, v) => {
    const el = form.elements.namedItem(name);
    if (el) el.value = v ?? '';
  };
  set('name', values.name);
  set('strength', values.strength);
  set('instructions_text', values.instructions_text);
  set('warnings', Array.isArray(values.warnings) ? values.warnings.join('\n') : values.warnings || '');
  const box = form.elements.namedItem('confirmed');
  if (box) box.checked = false;
}

/**
 * Wire submit handling: validation messages in `errorEl` (role=alert); the submit
 * button is disabled while `onSubmit(body)` runs; errors from the server are shown.
 */
export function bindMedicationForm(form, { errorEl, onSubmit, keepEmpty = () => false }) {
  const submit = form.querySelector('[type="submit"]');
  const showError = (message) => {
    if (!errorEl) return;
    errorEl.textContent = message || '';
    errorEl.hidden = !message;
  };
  form.addEventListener('input', () => showError(''));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const result = readMedicationForm(form, { keepEmpty: keepEmpty() });
    if (!result.ok) {
      showError(result.error);
      form.elements.namedItem(result.field)?.focus();
      return;
    }
    showError('');
    if (submit) submit.disabled = true;
    try {
      await onSubmit(result.body);
    } catch (err) {
      showError(errorText(err));
    } finally {
      if (submit) submit.disabled = false;
    }
  });
  return { showError };
}
