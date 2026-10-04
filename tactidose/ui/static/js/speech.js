/**
 * Spoken announcements ("Vitamin C dropped from container 1.") in the server's voice:
 * POST /api/agent/speak renders the text with the same chain as the assistant's replies
 * (ElevenLabs when configured, else cached / offline OS voice); the browser's own voice is only
 * the fallback. One announcement at a time: a newer one (or an assistant reply, via
 * cancelPendingSpeech) makes an older one that is still being rendered stay silent, so two
 * voices never cut each other off mid-word.
 */

import { post } from './api.js';

const RENDER_TIMEOUT_MS = 8000;
let seq = 0;

/** Speak `text` with `speaker` (ReplySpeaker) in the server voice; falls back to the browser voice. */
export async function speakNatural(speaker, text) {
  const t = String(text || '').trim();
  if (!t || !speaker) return;
  const mine = ++seq;
  let url = null;
  try {
    const r = await post('/api/agent/speak', { text: t }, { timeoutMs: RENDER_TIMEOUT_MS });
    url = r?.audio_url || null;
  } catch {
    /* no server voice: the browser speaks */
  }
  if (mine !== seq) return;          // something newer is being said
  speaker.speak(t, url);
}

/** Drop any announcement still being rendered (call before speaking something else). */
export function cancelPendingSpeech() {
  seq += 1;
}
