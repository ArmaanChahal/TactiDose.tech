/**
 * Caregiver "Scan label" tab (handoff §4.3 / §18): camera capture (rear camera
 * preferred) or file upload -> POST /api/onboarding/scan -> an UNCONFIRMED draft
 * shown in an editable form. Only a person ticking "I confirm this information is
 * correct" turns it into a medication (POST …/confirm); Reject discards it.
 * A FAILED scan shows the server's user_message and offers manual entry.
 */

import { get, post, upload } from '../api.js';
import { byId, confirmDialog, debounce, emptyState, errorState, errorText, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { formatDateTimeDevice } from '../format.js';
import { bindMedicationForm, fillMedicationForm } from '../medform.js';

export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
export const IMAGE_TYPES = Object.freeze(['image/jpeg', 'image/png', 'image/webp']);
const FALLBACK_FAILURE = 'Could not reliably read label. Please enter or verify information manually.';

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

function cameraErrorText(err) {
  switch (err && err.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'Camera permission was denied. Allow camera access in the browser, or upload a photo instead.';
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No camera was found. Upload a photo instead.';
    case 'NotReadableError':
      return 'The camera is being used by another application. Close it, or upload a photo instead.';
    default:
      return `Could not start the camera (${errorText(err)}). Upload a photo instead.`;
  }
}

export function createScan(ctx) {
  const video = byId('scan-video');
  const camStatus = byId('camera-status');
  const startBtn = byId('camera-start');
  const captureBtn = byId('camera-capture');
  const stopBtn = byId('camera-stop');
  const fileInput = byId('scan-file');
  const preview = byId('scan-preview');
  const status = byId('scan-status');
  const review = byId('scan-review');
  const reviewTitle = byId('scan-review-title');
  const failed = byId('scan-failed');
  const failedMsg = byId('scan-failed-message');
  const notes = byId('scan-notes');
  const form = byId('scan-form');
  const pending = byId('scan-pending');
  let media = null;
  let current = null;
  let previewUrl = null;
  let busy = false;
  let visible = false;
  let stale = true;

  reviewTitle.setAttribute('tabindex', '-1');

  // ---------------------------------------------------------------- camera

  async function startCamera() {
    if (!navigator.mediaDevices?.getUserMedia) {
      camStatus.textContent = 'The camera is not available in this browser (it needs localhost or https). Upload a photo instead.';
      return;
    }
    camStatus.textContent = 'Starting the camera…';
    try {
      media = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: false,
      });
      video.srcObject = media;
      video.hidden = false;
      await video.play();
      camStatus.textContent = 'Camera on. Hold the label flat and fill the frame, then press Capture photo.';
      startBtn.disabled = true;
      captureBtn.disabled = false;
      stopBtn.disabled = false;
    } catch (err) {
      stopCamera();
      camStatus.textContent = cameraErrorText(err);
    }
  }

  function stopCamera() {
    if (media) media.getTracks().forEach((track) => track.stop());
    media = null;
    video.srcObject = null;
    video.hidden = true;
    startBtn.disabled = false;
    captureBtn.disabled = true;
    stopBtn.disabled = true;
    camStatus.textContent = 'Camera is off.';
  }

  async function capture() {
    if (!media || !video.videoWidth) {
      camStatus.textContent = 'The camera is not ready yet.';
      return;
    }
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    try {
      const blob = await canvasToBlob(canvas, 'image/jpeg', 0.92);
      await submit(blob, 'label-photo.jpg');
    } catch (err) {
      status.textContent = errorText(err);
    }
  }

  startBtn.addEventListener('click', startCamera);
  stopBtn.addEventListener('click', stopCamera);
  captureBtn.addEventListener('click', capture);

  fileInput.addEventListener('change', async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    try {
      const image = await prepareImage(file);
      await submit(image, file.name || 'label.jpg');
    } catch (err) {
      status.textContent = errorText(err);
      ctx.notify(errorText(err), 'error');
    } finally {
      fileInput.value = '';
    }
  });

  // ---------------------------------------------------------------- scanning

  function showPreview(blob) {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    preview.src = previewUrl;
    preview.hidden = false;
  }

  async function submit(blob, filename) {
    if (busy) return;
    busy = true;
    showPreview(blob);
    review.hidden = true;
    failed.hidden = true;
    status.textContent = 'Reading the label… this can take a few seconds.';
    const data = new FormData();
    data.append('image', blob, filename);
    try {
      const scan = await upload('/api/onboarding/scan', data);
      handleScan(scan, { withPhoto: true });
      loadPending();
    } catch (err) {
      status.textContent = '';
      showFailed(errorText(err));
    } finally {
      busy = false;
    }
  }

  function handleScan(scan, { withPhoto }) {
    if (scan && scan.status === 'PENDING_REVIEW' && scan.extracted) {
      openReview(scan, { withPhoto });
    } else if (scan && scan.status === 'FAILED') {
      showFailed(scan.user_message || FALLBACK_FAILURE);
    } else {
      status.textContent = `This scan is ${String(scan?.status || 'unknown').toLowerCase().replace('_', ' ')}.`;
    }
  }

  function showFailed(message) {
    current = null;
    review.hidden = true;
    failedMsg.textContent = message || FALLBACK_FAILURE;
    failed.hidden = false;
    status.textContent = 'The label could not be read. Nothing was saved.';
  }

  function noteItems(scan) {
    const ex = scan.extracted || {};
    const items = [];
    if (ex.legible === false) items.push(h('li', {}, h('strong', {}, 'The label may not be readable. '), 'Check every field carefully or enter it manually.'));
    if (ex.confidence_notes) items.push(h('li', {}, h('strong', {}, 'Reader notes: '), ex.confidence_notes));
    if (Array.isArray(ex.warnings_visible) && ex.warnings_visible.length) {
      items.push(h('li', {}, h('strong', {}, 'Warnings seen on the label: '), ex.warnings_visible.join('; ')));
    }
    const meta = [`Scan #${scan.scan_id}`];
    if (scan.model) meta.push(`read by ${scan.model}`);
    if (scan.created_at) meta.push(formatDateTimeDevice(scan.created_at, ctx.offsetMin));
    items.push(h('li', { class: 'muted' }, meta.join(' · ')));
    return items;
  }

  function openReview(scan, { withPhoto = false } = {}) {
    current = scan;
    const ex = scan.extracted || {};
    fillMedicationForm(form, {
      name: ex.medication_name,
      strength: ex.strength,
      instructions_text: ex.visible_instructions,
      warnings: ex.warnings_visible || [],
      confirmed_by: ctx.caregiverName() || '',
    });
    replaceChildren(notes, h('ul', { class: 'notes-list' }, noteItems(scan)));
    if (!withPhoto) {
      preview.hidden = true;
      notes.append(h('p', { class: 'muted' }, 'The photo of this earlier scan is not shown here: compare the fields with the physical label.'));
    }
    failed.hidden = true;
    review.hidden = false;
    status.textContent = 'Draft ready. It is UNCONFIRMED until you check every field and confirm.';
    reviewTitle.focus();
  }

  function closeReview() {
    current = null;
    review.hidden = true;
    form.reset();
  }

  bindMedicationForm(form, {
    errorEl: byId('scan-form-error'),
    async onSubmit(body) {
      if (!current) throw new Error('There is no scan to confirm. Scan a label first.');
      const scan = current;
      const med = await post(`/api/onboarding/scans/${scan.scan_id}/confirm`, body);
      closeReview();
      ctx.invalidateMedications();
      const name = med?.name || body.name;
      ctx.notify(`Saved ${name}. Next: assign it to a compartment.`, 'success');
      replaceChildren(status,
        h('span', {}, `${name} was saved as a confirmed medication. `),
        h('button', { type: 'button', class: 'btn btn-small btn-primary', on: { click: () => ctx.goTo('compartments') } }, 'Assign a compartment'));
      loadPending();
    },
  });

  byId('scan-reject').addEventListener('click', async () => {
    if (!current) return;
    const scan = current;
    const { ok } = await confirmDialog({
      title: 'Reject this scan?',
      message: 'The draft is discarded and nothing is saved.',
      confirmLabel: 'Reject scan',
      danger: true,
    });
    if (!ok) return;
    try {
      await post(`/api/onboarding/scans/${scan.scan_id}/reject`, {});
      ctx.notify('Scan rejected. Nothing was saved.', 'info');
      closeReview();
      status.textContent = 'Scan rejected.';
    } catch (err) {
      ctx.notify(errorText(err), 'error');
    }
    loadPending();
  });

  byId('scan-manual').addEventListener('click', () => {
    ctx.goTo('medications');
    ctx.modules.medications?.openAddForm();
  });

  // ---------------------------------------------------------------- pending list

  async function loadPending() {
    stale = false;
    setLoading(pending, true);
    try {
      const scans = await get('/api/onboarding/scans?status=PENDING_REVIEW');
      renderPending(Array.isArray(scans) ? scans : []);
    } catch (err) {
      replaceChildren(pending, errorState(err, loadPending, icon('warning')));
    } finally {
      setLoading(pending, false);
    }
  }

  const loadSoon = debounce(loadPending, 300);

  function renderPending(scans) {
    if (!scans.length) {
      replaceChildren(pending, emptyState('No scans are waiting for review.'));
      return;
    }
    replaceChildren(pending, h('ul', { class: 'item-list' }, scans.map((scan) => {
      const name = scan.extracted?.medication_name || '(no name read)';
      return h('li', { class: 'item' },
        h('div', { class: 'item-head' },
          h('h4', { class: 'item-title' }, name),
          h('span', { class: 'badge tone-due' }, icon('warning'), 'Unconfirmed'),
          h('span', { class: 'item-sub' }, `Scan #${scan.scan_id}${scan.created_at ? ` · ${formatDateTimeDevice(scan.created_at, ctx.offsetMin)}` : ''}`)),
        h('button', {
          type: 'button',
          class: 'btn btn-small',
          'aria-label': `Review scan ${scan.scan_id}: ${name}`,
          on: { click: () => openReview(scan, { withPhoto: false }) },
        }, 'Review'));
    })));
  }

  return {
    show() {
      visible = true;
      if (stale || !pending.firstChild) loadPending();
      const name = byId('scan-confirmed-by');
      if (name && !name.value) name.value = ctx.caregiverName() || '';
    },
    hide() {
      visible = false;
      stopCamera();
    },
    markStale() {
      stale = true;
      if (visible) loadSoon();
    },
  };
}
