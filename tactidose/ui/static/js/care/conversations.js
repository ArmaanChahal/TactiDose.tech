/**
 * Care portal "Conversations" tab: the patient's conversations with the assistant
 * (GET /api/patients/{pid}/conversations) and a read-only transcript of one
 * (GET …/conversations/{cid}/messages) that shows every tool action and its result.
 */

import { get } from '../api.js';
import { byId, emptyState, errorState, h, replaceChildren, setLoading } from '../dom.js';
import { icon } from '../icons.js';
import { formatWhen, plural } from '../format.js';
import { createChatLog } from '../chat.js';
import { lazyPanel } from './panel.js';

/** "Today at 9:02 AM · 6 messages · spoken" for a conversation summary. */
export function conversationLine(c, nowLocal = null, offsetMin = null) {
  const when = formatWhen(c.last_message_at || c.started_at, nowLocal, offsetMin);
  const channel = { voice: 'spoken', text: 'typed', mixed: 'spoken and typed' }[c.channel] || c.channel || '';
  return [when, plural(Number(c.message_count) || 0, 'message'), channel].filter(Boolean).join(' · ');
}

/** ctx: {pid, getOffset(), getNow()} */
export function createConversationsTab(ctx) {
  const list = byId('conv-list');
  const title = byId('conv-title');
  const transcript = createChatLog(byId('conv-messages'), { audience: 'caregiver', getOffset: ctx.getOffset, getNow: ctx.getNow, startAtTop: true });
  let conversations = [];
  let openId = null;
  let seq = 0;

  async function load() {
    if (!ctx.pid) return;
    const token = ++seq;
    setLoading(list, true);
    try {
      const items = await get(`/api/patients/${ctx.pid}/conversations?limit=50`);
      if (token !== seq) return;
      conversations = Array.isArray(items) ? items : [];
      render();
      if (openId && conversations.some((c) => c.conversation_id === openId)) open(openId, { focus: false });
    } catch (err) {
      if (token !== seq) return;
      replaceChildren(list, h('li', {}, errorState(err, load, icon('warning'))));
    } finally {
      if (token === seq) setLoading(list, false);
    }
  }

  function render() {
    if (!conversations.length) {
      replaceChildren(list, h('li', {}, emptyState('The patient has not talked to the assistant yet.')));
      return;
    }
    replaceChildren(list, conversations.map((c) => {
      const label = c.title || 'Conversation';
      return h('li', { class: `item${c.conversation_id === openId ? ' is-selected' : ''}` },
        h('button', {
          type: 'button',
          class: 'btn conv-open',
          'aria-current': c.conversation_id === openId ? 'true' : null,
          on: { click: () => open(c.conversation_id) },
        }, h('span', { class: 'conv-title' }, label), h('span', { class: 'conv-meta' }, conversationLine(c, ctx.getNow(), ctx.getOffset()))));
    }));
  }

  async function open(cid, { focus = true } = {}) {
    openId = cid;
    render();
    const c = conversations.find((x) => x.conversation_id === cid);
    title.textContent = c?.title || 'Conversation';
    try {
      const messages = await get(`/api/patients/${ctx.pid}/conversations/${cid}/messages`);
      if (openId !== cid) return;
      transcript.set(Array.isArray(messages) ? messages : [], { emptyText: 'This conversation has no messages.' });
      if (focus) title.focus();
    } catch (err) {
      transcript.set([], { emptyText: `The messages could not be loaded: ${err.message}` });
    }
  }

  const panel = lazyPanel(load);
  return {
    show: panel.show,
    hide: panel.hide,
    markStale: panel.markStale,
    reset() {
      conversations = [];
      openId = null;
      list.replaceChildren();
      title.textContent = 'Choose a conversation';
      transcript.set([], { emptyText: 'Choose a conversation to read it.' });
      panel.reset();
    },
  };
}
