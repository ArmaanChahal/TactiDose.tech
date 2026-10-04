/**
 * Linking a doctor/family account to a patient (POST /api/care/links) — the form logic
 * shared by the sign-in page (after a caregiver registers) and the care portal.
 * Pure validation is unit-tested under Node.
 */

/** "abcd 2345" / "ABCD-2345" -> "ABCD2345". */
export function normalizeLinkCode(code) {
  return String(code || '').replace(/[\s-]+/g, '').toUpperCase();
}

/** Validate the two fields. Returns {ok, body: {patient_id, link_code}} or {ok: false, error, field}. */
export function linkBody(patientIdText, codeText) {
  const raw = String(patientIdText ?? '').trim().replace(/^#/, '');
  if (!raw) return { ok: false, error: 'Enter the Patient ID (a number).', field: 'patient_id' };
  if (!/^\d{1,9}$/.test(raw) || Number(raw) <= 0) {
    return { ok: false, error: 'The Patient ID is a number, for example 12.', field: 'patient_id' };
  }
  const code = normalizeLinkCode(codeText);
  if (!code) return { ok: false, error: 'Enter the link code.', field: 'link_code' };
  if (!/^[A-Z0-9]{4,16}$/.test(code)) {
    return { ok: false, error: 'The link code has only letters and numbers, for example ABCD2345.', field: 'link_code' };
  }
  return { ok: true, body: { patient_id: Number(raw), link_code: code } };
}

/** Server error -> plain sentence for the link form. */
export function linkErrorText(err) {
  if (err?.status === 404 || err?.status === 403) {
    return 'That Patient ID and link code do not match. Check both with the patient and try again.';
  }
  if (err?.status === 409) return 'You are already linked to this patient.';
  return err?.message || 'The patient could not be linked.';
}
