/**
 * Conversation transcript (patient <-> agent) shared by the patient's Assistant view,
 * the kiosk and the care portal's read-only Conversations tab. Tool calls are shown in
 * plain words (with the raw arguments/results for caregivers).
 * Message shape: docs/API.md "Message".
 */

import { h, prettyJson } from './dom.js';
import { icon } from './icons.js';
import { formatWhen } from './format.js';
import { displayTranscript } from './pcm.js';
import { dropStatusInfo } from './words.js';

const TOOL_WORDS = Object.freeze({
  get_patient_status: { patient: 'Checked your pill status', caregiver: 'Checked the patient status' },
  get_recent_drops: { patient: 'Looked at your recent pills', caregiver: 'Looked at recent drops' },
  confirm_pill_taken: { patient: 'Noted that you took your pill', caregiver: 'Marked the latest dose as taken' },
  request_pill: { patient: 'Asked the device for a pill', caregiver: 'Requested a pill drop' },
});

function resultOf(m) {
  const r = m?.tool_result;
  return r && typeof r === 'object' ? r : null;
}

/** Plain-words summary of a role="tool" message: {text, icon, tone}. */
export function toolSummary(m, audience = 'patient') {
  const name = m?.tool_name || 'tool';
  const words = TOOL_WORDS[name];
  const base = words ? words[audience === 'caregiver' ? 'caregiver' : 'patient'] : `Used ${name}`;
  const r = resultOf(m);
  if (name === 'request_pill' && r && r.status) {
    const info = dropStatusInfo(r.status);
    const lead = audience === 'caregiver' ? `Requested a pill: ${info.word.toLowerCase()}` : `Asked for a pill: ${info.word.toLowerCase()}`;
    return { text: r.message ? `${lead}. ${r.message}` : `${lead}.`, icon: info.icon, tone: info.tone };
  }
  if (r && (r.error || r.ok === false)) {
    const why = r.error || r.message || 'it did not work';
    return { text: `${base} — ${typeof why === 'string' ? why : JSON.stringify(why)}`, icon: 'warning', tone: 'caution' };
  }
  return { text: `${base}.`, icon: name === 'request_pill' ? 'pill' : 'list', tone: 'neutral' };
}

/** {who, cls, text, iconName, tone} for one message. */
export function messageView(m, { audience = 'patient' } = {}) {
  if (m.role === 'tool') {
    const t = toolSummary(m, audience);
    return { who: audience === 'caregiver' ? `Tool: ${m.tool_name || 'unknown'}` : 'Action', cls: 'from-tool', text: t.text, iconName: t.icon, tone: t.tone };
  }
  if (m.role === 'user') {
    const spoken = m.input_mode === 'voice';
    return {
      who: audience === 'caregiver' ? `Patient${spoken ? ' (spoken)' : ''}` : `You${spoken ? ' (spoken)' : ''}`,
      cls: 'from-user',
      text: displayTranscript(m.content),
      iconName: spoken ? 'mic' : 'user',
      tone: 'neutral',
    };
  }
  const model = audience === 'caregiver' && m.model ? ` (${m.model})` : '';
  return { who: `Assistant${model}`, cls: 'from-assistant', text: String(m.content || ''), iconName: 'spark', tone: 'neutral' };
}

/** Tool calls the patient does not need to see (status look-ups); caregivers see all. */
const QUIET_TOOLS = new Set(['get_patient_status', 'get_recent_drops']);

export function visibleTo(m, audience = 'patient') {
  return !(audience === 'patient' && m?.role === 'tool' && QUIET_TOOLS.has(m.tool_name));
}

/**
 * Transcript list. `listEl` is an <ol>; the patient view makes it role="log" (live).
 * Options: audience 'patient' | 'caregiver', getOffset(), getNow(), startAtTop (read-only
 * transcripts open at their first message; the live log stays at the newest one).
 */
export function createChatLog(listEl, { audience = 'patient', getOffset = () => null, getNow = () => null, startAtTop = false } = {}) {
  const seen = new Set();
  let lastConversation = null;

  function row(m) {
    const v = messageView(m, { audience });
    const when = m.created_at ? formatWhen(m.created_at, getNow(), getOffset()) : '';
    const item = h('li', { class: `chat-msg ${v.cls}`, dataset: { messageId: m.message_id ?? '' } },
      h('div', { class: 'chat-who' }, icon(v.iconName, { className: `icon tone-${v.tone}` }), h('span', {}, v.who),
        when ? h('span', { class: 'chat-time' }, when) : null),
      h('p', { class: 'chat-text' }, v.text));
    if (audience === 'caregiver' && m.role === 'tool' && (m.tool_args || m.tool_result)) {
      item.append(h('details', { class: 'chat-tool-details' },
        h('summary', {}, 'Tool details'),
        h('pre', {}, `Arguments: ${prettyJson(m.tool_args ?? {})}\nResult: ${prettyJson(m.tool_result ?? {})}`)));
    }
    return item;
  }

  function scrollToEnd() {
    listEl.scrollTop = listEl.scrollHeight;
  }

  return {
    /** Replace the transcript. */
    set(messages, { emptyText = 'No messages yet.' } = {}) {
      seen.clear();
      lastConversation = null;
      const items = (messages || []).filter((m) => m && m.message_id !== undefined && visibleTo(m, audience));
      for (const m of items) seen.add(m.message_id);
      listEl.replaceChildren(...(items.length ? items.map(row) : [h('li', { class: 'chat-divider', 'data-state': 'empty' }, emptyText)]));
      if (items.length) lastConversation = items[items.length - 1].conversation_id ?? null;
      if (startAtTop) listEl.scrollTop = 0;
      else scrollToEnd();
    },
    /** Append messages not shown yet (dedupe by message_id). Returns how many were added. */
    add(messages) {
      let added = 0;
      for (const m of messages || []) {
        if (!m || m.message_id === undefined || seen.has(m.message_id)) continue;
        if (!visibleTo(m, audience)) {
          seen.add(m.message_id);
          continue;
        }
        listEl.querySelector('[data-state="empty"]')?.remove();
        if (lastConversation !== null && m.conversation_id !== undefined && m.conversation_id !== lastConversation) {
          listEl.append(h('li', { class: 'chat-divider' }, 'New conversation'));
        }
        seen.add(m.message_id);
        listEl.append(row(m));
        lastConversation = m.conversation_id ?? lastConversation;
        added += 1;
      }
      if (added) scrollToEnd();
      return added;
    },
    /** Placeholder for the person's message while the reply is on its way. */
    addPending(text, { spoken = false } = {}) {
      listEl.querySelector('[data-state="empty"]')?.remove();
      const item = h('li', { class: 'chat-msg from-user is-pending' },
        h('div', { class: 'chat-who' }, icon(spoken ? 'mic' : 'user'), h('span', {}, spoken ? 'You (spoken)' : 'You')),
        h('p', { class: 'chat-text' }, displayTranscript(text)));
      const thinking = h('li', { class: 'chat-msg from-assistant is-pending' },
        h('div', { class: 'chat-who' }, icon('spark'), h('span', {}, 'Assistant')),
        h('p', { class: 'chat-text' }, 'Thinking…'));
      listEl.append(item, thinking);
      scrollToEnd();
      return {
        /** The reply arrived: keep the person's message in place (no second announcement). */
        confirm(messages) {
          thinking.remove();
          const mine = (messages || []).find((m) => m && m.role === 'user' && m.message_id !== undefined);
          if (mine) {
            seen.add(mine.message_id);
            item.classList.remove('is-pending');
            item.dataset.messageId = String(mine.message_id);
            if (lastConversation !== null && mine.conversation_id !== undefined && mine.conversation_id !== lastConversation) {
              item.before(h('li', { class: 'chat-divider' }, 'New conversation'));
            }
            lastConversation = mine.conversation_id ?? lastConversation;
          } else {
            item.remove();
          }
        },
        done() {
          item.remove();
          thinking.remove();
        },
        fail(message) {
          item.classList.remove('is-pending');
          thinking.classList.remove('is-pending');
          thinking.querySelector('.chat-text').textContent = message;
        },
      };
    },
    get conversationId() {
      return lastConversation;
    },
    has(messageId) {
      return seen.has(messageId);
    },
  };
}
