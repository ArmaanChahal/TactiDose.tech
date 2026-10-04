/**
 * Patient portal "Assistant" view: chat transcript, text box, push-to-talk (browser
 * speech recognition or the offline recognizer) and spoken replies.
 * POST /api/agent/chat decides nothing about pills itself: the server's deterministic
 * rules have the final say; the UI only shows and speaks what came back.
 */

import { LONG_TIMEOUT_MS, get, post } from '../api.js';
import { byId, debounce, errorText } from '../dom.js';
import { createChatLog } from '../chat.js';
import { displayTranscript } from '../pcm.js';
import { VoiceInput, voiceInputAvailable } from '../voice.js';
import { cancelPendingSpeech } from '../speech.js';
import { speakAfter } from '../wellbeing.js';

const TALK_LABELS = {
  idle: 'Talk',
  error: 'Talk',
  starting: 'Starting…',
  listening: 'Stop and send',
  processing: 'Working…',
};

/**
 * ctx: {pid, prefs, speaker, stream, getOffset, getNow, onActions(actions), isVisible(), show(), notify()}
 *
 * After a pill drops the server offers a well-being check-in (SSE "wellbeing.prompt", or
 * appended to the reply when the assistant dropped it): it is shown here, spoken after the
 * "pill dropped" speech, and the patient answers yes or no like any other message.
 */
export function createAssistant(ctx) {
  const log = byId('chat-log');
  const form = byId('chat-form');
  const input = byId('chat-text');
  const sendBtn = byId('chat-send');
  const talkBtn = byId('talk-btn');
  const talkLabel = byId('talk-label');
  const voiceStatus = byId('voice-status');
  const interim = byId('voice-interim');
  const chat = createChatLog(log, { audience: 'patient', getOffset: ctx.getOffset, getNow: ctx.getNow });
  let conversationId = null;
  let busy = false;
  let loaded = false;
  /** after-drop check-in offers already shown (the SSE event and a chat reply can both carry one) */
  const offered = new Set();

  async function loadConversation() {
    loaded = true;
    try {
      const conversations = await get(`/api/patients/${ctx.pid}/conversations?limit=1`);
      const latest = Array.isArray(conversations) ? conversations[0] : null;
      if (!latest) {
        chat.set([], { emptyText: 'No messages yet. Try: "Can I have my pill?" or "When is my next pill?"' });
        return;
      }
      conversationId = latest.conversation_id;
      const messages = await get(`/api/patients/${ctx.pid}/conversations/${latest.conversation_id}/messages`);
      chat.set(Array.isArray(messages) ? messages : []);
    } catch (err) {
      chat.set([], { emptyText: `Earlier messages could not be loaded (${errorText(err)}). You can still talk to the assistant.` });
    }
  }

  async function refreshConversation(cid) {
    try {
      const messages = await get(`/api/patients/${ctx.pid}/conversations/${cid}/messages`);
      if (chat.add(Array.isArray(messages) ? messages : [])) conversationId = cid;
    } catch {
      /* the next turn shows everything anyway */
    }
  }

  const refreshSoon = debounce((cid) => refreshConversation(cid), 500);

  function setBusy(on) {
    busy = on;
    sendBtn.disabled = on;
    sendBtn.setAttribute('aria-busy', on ? 'true' : 'false');
  }

  async function send(rawText, mode = 'text') {
    const text = String(rawText || '').trim();
    if (!text) {
      input.focus();
      return;
    }
    if (busy) {
      voiceStatus.textContent = 'Please wait for the answer to your last message.';
      return;
    }
    setBusy(true);
    ctx.speaker.stop();
    const pending = chat.addPending(text, { spoken: mode === 'voice' });
    try {
      const body = { text, input_mode: mode, speak: Boolean(ctx.prefs.get('speakReplies')) };
      if (conversationId) body.conversation_id = conversationId;
      const reply = await post('/api/agent/chat', body, { timeoutMs: LONG_TIMEOUT_MS });
      if (reply?.wellbeing?.offer_id) offered.add(reply.wellbeing.offer_id);
      conversationId = reply?.conversation_id ?? conversationId;
      const messages = Array.isArray(reply?.messages) ? reply.messages : [];
      pending.confirm(messages);
      chat.add(messages.filter((m) => m.role !== 'user'));
      if (!messages.some((m) => m.role === 'assistant') && reply?.text) {
        chat.add([{ message_id: `reply-${Date.now()}`, role: 'assistant', content: reply.text, conversation_id: conversationId }]);
      }
      const actions = Array.isArray(reply?.actions) ? reply.actions : [];
      ctx.onActions(actions);
      if (ctx.prefs.get('speakReplies') && reply?.text) {
        cancelPendingSpeech();
        ctx.speaker.speak(reply.text, reply.audio_url || null);
      }
    } catch (err) {
      const message = err?.timeout || err?.network
        ? `There was no answer (${errorText(err)}). If you asked for a pill, check Home or History before asking again.`
        : `The assistant could not answer: ${errorText(err)}. You can use the Drop pill buttons on Home.`;
      pending.fail(message);
      if (ctx.prefs.get('speakReplies')) ctx.speaker.speak(message);
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value;
    input.value = '';
    send(text, 'text');
  });

  // ---------------------------------------------------------------- voice

  function renderVoice(state, message) {
    const listening = state === 'listening' || state === 'starting';
    talkBtn.setAttribute('aria-pressed', listening ? 'true' : 'false');
    talkBtn.classList.toggle('is-listening', state === 'listening');
    talkBtn.setAttribute('aria-busy', state === 'processing' ? 'true' : 'false');
    talkLabel.textContent = TALK_LABELS[state] || 'Talk';
    if (!listening) interim.hidden = true;
    voiceStatus.textContent = message || '';
  }

  const voice = new VoiceInput({
    onState: renderVoice,
    onInterim: (text) => {
      interim.hidden = false;
      interim.textContent = `Hearing: ${displayTranscript(text)}`;
    },
    onResult: (text) => {
      interim.hidden = true;
      send(text, 'voice');
    },
    preferOffline: () => Boolean(ctx.prefs.get('offlineSpeech')),
  });

  if (!voiceInputAvailable()) {
    talkBtn.setAttribute('aria-disabled', 'true');
    voiceStatus.textContent = 'Voice input is not available in this browser. Please type your message.';
  }

  function toggleTalk() {
    if (!voiceInputAvailable()) {
      voiceStatus.textContent = 'Voice input is not available in this browser. Please type your message.';
      return;
    }
    ctx.speaker.stop();
    voice.toggle();
  }

  talkBtn.addEventListener('click', toggleTalk);

  document.addEventListener('keydown', (e) => {
    if (e.altKey && !e.ctrlKey && !e.metaKey && (e.code === 'KeyM' || String(e.key).toLowerCase() === 'm')) {
      e.preventDefault();
      if (!ctx.isVisible()) ctx.show();
      toggleTalk();
    } else if (e.key === 'Escape' && (voice.active || ctx.speaker.speaking)) {
      voice.cancel();
      ctx.speaker.stop();
    }
  });

  byId('stop-speaking').addEventListener('click', () => ctx.speaker.stop());
  // Optional well-being check-in (tactidose/wellbeing.py): the server answers these turns
  // itself and never stores them in the conversation. It does not affect pills.
  byId('start-checkin')?.addEventListener('click', () => send('Start a well-being check-in', 'text'));

  byId('new-chat').addEventListener('click', () => {
    conversationId = null;
    chat.set([], { emptyText: 'New conversation. Ask about your pills, or ask for a pill.' });
    input.focus();
  });

  ctx.stream.on('wellbeing.prompt', (d, _env, meta) => {
    if (meta?.replayed || !d?.offer_id || !d.text || offered.has(d.offer_id)) return;
    offered.add(d.offer_id);
    if (busy) return; // the reply of the message in flight carries the offer
    chat.add([{ message_id: `wb-${d.offer_id}`, role: 'assistant', content: d.text }]);
    if (ctx.prefs.get('speakReplies') || ctx.prefs.get('speakDrops')) speakAfter(ctx.speaker, d.text);
    if (!ctx.isVisible()) ctx.notify?.('Would you like a quick well-being check-in? Open Assistant and say yes or no.', 'info');
  });

  ctx.stream.on('agent.message', (d, _env, meta) => {
    if (meta?.replayed || busy || !loaded || !d?.conversation_id) return;
    if (d.message_id !== undefined && chat.has(d.message_id)) return;
    refreshSoon(d.conversation_id);
  });

  return {
    /** A message is waiting for the assistant's reply (its outcome will be spoken). */
    get busy() {
      return busy;
    },
    show() {
      if (!loaded) loadConversation();
    },
    reload: loadConversation,
    send,
    get listening() {
      return voice.active;
    },
  };
}
