/**
 * Care portal "Medications" tab (doctor/family only): list, add (POST), edit (PATCH)
 * and archive (DELETE) medications — every save needs the explicit "I confirm this
 * information is correct" tick. Optional extra: read a label photo with Gemini
 * (POST /api/patients/{pid}/scans) -> UNCONFIRMED draft -> a person checks every field
 * -> confirm (POST …/scans/{scan_id}/confirm) or reject (…/reject).
 */

import { del, patch, post, upload } from '../api.js';
import { byId, confirmDialog, emptyState, errorState, errorText, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { formatDateTimeDevice, time24To12 } from '../format.js';
import { bindMedicationForm, fillMedicationForm } from '../medform.js';
import { lazyPanel } from './panel.js';

export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
export const IMAGE_TYPES = Object.freeze(['image/jpeg', 'image/png', 'image/webp']);
const FALLBACK_FAILURE = 'Could not reliably read label. Please enter or verify information manually.';

export function sourceName(source) {
  return { manual: 'Entered by hand', label_scan: 'From a label photo', demo_seed: 'Demo data' }[source] || source || '–';
}

/** Label-scan failure text for HTTP errors (the feature is an optional extra). */
export function scanErrorText(err) {
  if ([404, 501, 503].includes(err?.status)) return 'Reading labels from photos is not set up on this server. Please enter the medication by hand.';
  if (err?.status === 413) return 'The photo is too large. Please choose a smaller one.';
  return `The photo could not be read: ${errorText(err)}`;
}

function canvasToBlob(canvas, type, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('Could not encode the photo.'))), type, quality);
  });
}

/** Accept JPEG/PNG/WebP up to 8 MB; larger photos are scaled down and re-encoded as JPEG. */
async function prepareImage(file) {
  if (!IMAGE_TYPES.includes(file.type)) throw new Error('Please choose a JPEG, PNG or WebP photo.');
  if (file.size <= MAX_IMAGE_BYTES) return file;
  if (typeof createImageBitmap !== 'function') throw new Error('The photo is larger than 8 MB. Please choose a smaller one.');
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 2400 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const blob = await canvasToBlob(canvas, 'image/jpeg', 0.88);
  if (blob.size > MAX_IMAGE_BYTES) throw new Error('The photo is larger than 8 MB even after resizing. Please choose a smaller one.');
  return blob;
}

/** ctx: {pid, medications(force), invalidateMedications(), notify(message, kind), getOffset(), onChanged(), goTo(tab)} */
export function createMedicationsTab(ctx) {
  const list = byId('med-list');
  const showArchived = byId('med-show-archived');
  const form = byId('med-form');
  const formTitle = byId('med-form-title');
  const submit = byId('med-submit');
  const cancelEdit = byId('med-cancel-edit');
  let meds = [];
  let editing = null;
  let seq = 0;

  // ---------------------------------------------------------------- list

  async function load(force = false) {
    if (!ctx.pid) return;
    const token = ++seq;
    setLoading(list, true);
    try {
      const items = await ctx.medications(force);
      if (token !== seq) return;
      meds = Array.isArray(items) ? items : [];
      render();
    } catch (err) {
      if (token !== seq) return;
      replaceChildren(list, errorState(err, () => load(true), icon('warning')));
    } finally {
      if (token === seq) setLoading(list, false);
    }
  }

  function render() {
    const shown = meds.filter((m) => showArchived.checked || m.active !== false);
    if (!shown.length) {
      replaceChildren(list, emptyState('No medications yet. Add one with the form.'));
      return;
    }
    replaceChildren(list, h('ul', { class: 'item-list' }, shown.map(item)));
  }

  function item(m) {
    const archived = m.active === false;
    const times = (m.schedules || []).map((s) => time24To12(s.time_of_day)).join(', ');
    const facts = [
      m.compartment_number ? `Container ${m.compartment_number}` : 'No container',
      times ? `Drops at ${times}` : 'No times scheduled',
      sourceName(m.source),
      m.confirmed_by ? `Confirmed by ${m.confirmed_by}${m.confirmed_at ? `, ${formatDateTimeDevice(m.confirmed_at, ctx.getOffset())}` : ''}` : null,
    ].filter(Boolean);
    return h('li', { class: `item${archived ? ' is-off' : ''}` },
      h('div', { class: 'item-head' },
        h('p', { class: 'item-title' }, m.name, m.strength ? h('span', { class: 'muted' }, ` · ${m.strength}`) : null),
        archived ? h('span', { class: 'badge tone-neutral' }, icon('archive'), 'Archived') : null),
      h('p', { class: 'item-body' }, facts.join(' · ')),
      m.instructions_text ? h('p', { class: 'item-body' }, `Instructions: ${m.instructions_text}`) : null,
      (m.warnings || []).length ? h('p', { class: 'item-body' }, `Warnings: ${m.warnings.join('; ')}`) : null,
      archived ? null : h('div', { class: 'item-actions' },
        h('button', { type: 'button', class: 'btn btn-small', 'aria-label': `Change ${m.name}`, on: { click: () => startEdit(m) } }, icon('edit'), 'Change'),
        h('button', { type: 'button', class: 'btn btn-small', 'aria-label': `Archive ${m.name}`, on: { click: () => archive(m) } }, icon('archive'), 'Archive')));
  }

  showArchived.addEventListener('change', render);

  // ---------------------------------------------------------------- add / edit

  function endEdit() {
    editing = null;
    form.reset();
    formTitle.textContent = 'Add a medication';
    submit.textContent = 'Save medication';
    cancelEdit.hidden = true;
    byId('med-form-card').classList.remove('is-editing');
  }

  function startEdit(m) {
    editing = m;
    fillMedicationForm(form, m);
    formTitle.textContent = `Change ${m.name}`;
    submit.textContent = 'Save changes';
    cancelEdit.hidden = false;
    const card = byId('med-form-card');
    card.classList.add('is-editing');
    card.scrollIntoView({ block: 'start' });
    formTitle.focus({ preventScroll: true });
  }

  cancelEdit.addEventListener('click', endEdit);

  bindMedicationForm(form, {
    errorEl: byId('med-form-error'),
    keepEmpty: () => Boolean(editing),
    async onSubmit(body) {
      if (editing) {
        await patch(`/api/patients/${ctx.pid}/medications/${editing.medication_id}`, body);
        ctx.notify(`${body.name} was updated.`, 'success');
      } else {
        await post(`/api/patients/${ctx.pid}/medications`, body);
        ctx.notify(`${body.name} was saved. Next: put it in a container and add its times.`, 'success');
      }
      endEdit();
      ctx.invalidateMedications();
      ctx.onChanged();
      load(true);
    },
  });

  async function archive(m) {
    const { ok } = await confirmDialog({
      title: `Archive ${m.name}?`,
      message: 'It will no longer drop. Its schedules stop and its container is freed. Past history is kept.',
      confirmLabel: 'Archive',
      danger: true,
      iconEl: icon('archive'),
    });
    if (!ok) return;
    try {
      await del(`/api/patients/${ctx.pid}/medications/${m.medication_id}`);
      ctx.notify(`${m.name} was archived.`, 'success');
      if (editing && editing.medication_id === m.medication_id) endEdit();
      ctx.invalidateMedications();
      ctx.onChanged();
    } catch (err) {
      ctx.notify(errorText(err), 'error');
    }
    load(true);
  }

  // ---------------------------------------------------------------- label scan (optional extra)

  const fileInput = byId('scan-file');
  const preview = byId('scan-preview');
  const scanStatus = byId('scan-status');
  const review = byId('scan-review');
  const notes = byId('scan-notes');
  const scanForm = byId('scan-form');
  let scan = null;
  let previewUrl = null;
  let scanning = false;

  function showPreview(blob) {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    preview.src = previewUrl;
    preview.hidden = false;
  }

  function closeReview() {
    scan = null;
    review.hidden = true;
    scanForm.reset();
  }

  function openReview(s) {
    scan = s;
    const ex = s.extracted || {};
    fillMedicationForm(scanForm, {
      name: ex.medication_name,
      strength: ex.strength,
      instructions_text: ex.visible_instructions,
      warnings: ex.warnings_visible || [],
    });
    const items = [];
    if (ex.legible === false) items.push(h('li', {}, h('strong', {}, 'The label may not be readable. '), 'Check every field carefully or type it by hand.'));
    if (ex.confidence_notes) items.push(h('li', {}, h('strong', {}, 'Reader notes: '), ex.confidence_notes));
    items.push(h('li', { class: 'muted' }, `Scan ${s.scan_id}${s.model ? `, read by ${s.model}` : ''}`));
    replaceChildren(notes, h('ul', { class: 'notes-list' }, items));
    review.hidden = false;
    scanStatus.textContent = 'Draft ready. It is not confirmed until you check every field and tick the box.';
    byId('scan-name').focus();
  }

  fileInput.addEventListener('change', async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file || scanning) return;
    scanning = true;
    closeReview();
    try {
      const image = await prepareImage(file);
      showPreview(image);
      scanStatus.textContent = 'Reading the label… this can take a few seconds.';
      const data = new FormData();
      data.append('image', image, file.name || 'label.jpg');
      const result = await upload(`/api/patients/${ctx.pid}/scans`, data);
      if (result?.status === 'PENDING_REVIEW' && result.extracted) openReview(result);
      else scanStatus.textContent = result?.user_message || FALLBACK_FAILURE;
    } catch (err) {
      scanStatus.textContent = scanErrorText(err);
    } finally {
      scanning = false;
      fileInput.value = '';
    }
  });

  bindMedicationForm(scanForm, {
    errorEl: byId('scan-form-error'),
    async onSubmit(body) {
      if (!scan) throw new Error('There is no scan to confirm. Choose a photo first.');
      const med = await post(`/api/patients/${ctx.pid}/scans/${scan.scan_id}/confirm`, body);
      closeReview();
      preview.hidden = true;
      ctx.invalidateMedications();
      ctx.onChanged();
      ctx.notify(`${med?.name || body.name} was saved. Next: put it in a container.`, 'success');
      scanStatus.textContent = `${med?.name || body.name} was saved as a confirmed medication.`;
      load(true);
    },
  });

  byId('scan-reject').addEventListener('click', async () => {
    if (!scan) return;
    const current = scan;
    const { ok } = await confirmDialog({
      title: 'Reject this scan?',
      message: 'The draft is thrown away and nothing is saved.',
      confirmLabel: 'Reject',
      danger: true,
    });
    if (!ok) return;
    try {
      await post(`/api/patients/${ctx.pid}/scans/${current.scan_id}/reject`, {});
      closeReview();
      preview.hidden = true;
      scanStatus.textContent = 'Scan rejected. Nothing was saved.';
    } catch (err) {
      scanStatus.textContent = errorText(err);
    }
  });

  const panel = lazyPanel(() => load(false));
  return {
    show: panel.show,
    hide: panel.hide,
    markStale: panel.markStale,
    reset() {
      endEdit();
      closeReview();
      preview.hidden = true;
      scanStatus.textContent = '';
      meds = [];
      list.replaceChildren();
      panel.reset();
    },
  };
}
