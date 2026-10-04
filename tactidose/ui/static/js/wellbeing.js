/**
 * Well-being check-ins (GET /api/patients/{pid}/wellbeing) shared by both portals: the
 * "Well-being check-ins" section next to the pill history. Each check-in shows its ratings,
 * the patient's own notes and the pill it followed. Informal, non-clinical answers: shown as
 * the patient gave them, never scored. Only the patient can delete one
 * (DELETE /api/wellbeing/v1/me/history/{record_id}).
 *
 * Also `speakAfter`: say the after-drop check-in offer once the "pill dropped" speech ends.
 */

import { del, get } from './api.js';
import { confirmDialog, emptyState, errorState, errorText, h, replaceChildren, setLoading, uid } from './dom.js';
import { icon } from './icons.js';
import { formatDateTimeDevice } from './format.js';
import { sourceText } from './words.js';

export const CHECKIN_DAYS = Object.freeze([7, 30, 90]);

const STATUS_WORDS = Object.freeze({ skipped: 'skipped', not_reached: 'not answered' });

/** Plain words for one check-in view: {title, when, after, lines: [{label, value, note}], support}. */
export function checkinSummary(c, { audience = 'patient', offsetMin = null } = {}) {
  const d = c.after_drop;
  let after = audience === 'patient' ? 'Started by you' : 'Started by the patient';
  if (d) {
    const med = d.medication_name || 'a pill';
    const number = d.container_number ? ` (container ${d.container_number})` : '';
    const at = formatDateTimeDevice(d.dropped_local || d.dropped_at, d.dropped_local ? null : offsetMin);
    after = `After ${med}${number} dropped at ${at} · ${sourceText(d.source, audience)}`;
  }
  return {
    title: 'Well-being check-in',
    when: formatDateTimeDevice(c.completed_local || c.completed_at, c.completed_local ? null : offsetMin),
    after,
    lines: (c.answers || []).map((a) => ({
      label: a.label || a.question_id,
      value: a.status === 'answered' ? String(a.answer_value ?? '') : STATUS_WORDS[a.status] || a.status,
      note: a.note_text || null,
    })),
    support: Boolean(c.support_requested),
  };
}

/**
 * Mount the check-in list in `root`. Options: getPatientId(), audience ('patient' |
 * 'caregiver'), canDelete, notify(message, kind), getOffset().
 */
export function createCheckinHistory(root, {
  getPatientId,
  audience = 'patient',
  canDelete = false,
  notify = () => {},
  getOffset = () => null,
} = {}) {
  const daysId = uid('wb-days');
  const days = h('select', { id: daysId }, CHECKIN_DAYS.map((d) => h('option', { value: String(d), selected: d === 30 }, `Last ${d} days`)));
  const refresh = h('button', { type: 'button', class: 'btn' }, icon('rotate'), 'Refresh');
  const list = h('ul', { class: 'item-list' });
  root.replaceChildren(
    h('div', { class: 'filter-row' }, h('div', { class: 'field' }, h('label', { for: daysId }, 'Show'), days), refresh),
    list);

  let seq = 0;

  async function load() {
    const pid = getPatientId();
    if (!pid) return;
    const token = ++seq;
    setLoading(list, true);
    try {
      const items = await get(`/api/patients/${pid}/wellbeing?days=${Number(days.value) || 30}`);
      if (token !== seq) return;
      render(Array.isArray(items) ? items : []);
    } catch (err) {
      if (token !== seq) return;
      replaceChildren(list, h('li', {}, errorState(err, load, icon('warning'))));
    } finally {
      if (token === seq) setLoading(list, false);
    }
  }

  function render(items) {
    if (!items.length) {
      const hint = audience === 'patient'
        ? 'No saved check-ins in this period. After a pill drops you will be asked if you want one.'
        : 'No saved check-ins in this period.';
      replaceChildren(list, h('li', {}, emptyState(hint)));
      return;
    }
    const offset = getOffset();
    replaceChildren(list, items.map((c) => item(c, offset)));
  }

  function item(c, offset) {
    const v = checkinSummary(c, { audience, offsetMin: offset });
    const rows = h('dl', { class: 'checkin-answers' }, v.lines.map((l) => h('div', { class: 'checkin-answer' },
      h('dt', {}, l.label),
      h('dd', {}, l.value, l.note ? h('span', { class: 'checkin-note' }, ` — “${l.note}”`) : null))));
    const actions = canDelete
      ? h('div', { class: 'item-actions' }, h('button', {
        type: 'button', class: 'btn', 'aria-label': `Delete the check-in from ${v.when}`, on: { click: () => remove(c, v) },
      }, icon('trash'), 'Delete'))
      : null;
    return h('li', { class: 'item' },
      h('div', { class: 'item-head' },
        h('span', { class: 'item-sub' }, v.when),
        h('p', { class: 'item-title' }, v.title),
        v.support ? h('span', { class: 'badge tone-caution' }, icon('users'), 'Asked for support') : null),
      h('p', { class: 'item-body' }, v.after),
      rows,
      actions);
  }

  async function remove(c, v) {
    const { ok } = await confirmDialog({
      title: 'Delete this check-in?',
      message: `The check-in from ${v.when}, with its notes.\nYour care team will no longer see it.`,
      confirmLabel: 'Delete',
      iconEl: icon('trash'),
    });
    if (!ok) return;
    try {
      await del(`/api/wellbeing/v1/me/history/${encodeURIComponent(c.record_id)}`);
      notify('The check-in was deleted.', 'success');
    } catch (err) {
      notify(errorText(err), 'error');
    }
    load();
  }

  days.addEventListener('change', load);
  refresh.addEventListener('click', load);
  return { load };
}

/** Speak `text` once `speaker` has finished what it is saying (at most `maxWaitMs`). */
export function speakAfter(speaker, text, { maxWaitMs = 15000 } = {}) {
  const started = Date.now();
  const tick = () => {
    if (speaker.speaking && Date.now() - started < maxWaitMs) {
      setTimeout(tick, 300);
      return;
    }
    speaker.speak(text);
  };
  setTimeout(tick, 600);
}
