/**
 * Live serial log (device.line events: {"dir": "rx"|"tx", "line"}), shared by the
 * caregiver Device tab and the demo hardware console. De-duplicates by event
 * (seq, ts), keeps the newest `max` lines, can pause, and can hide the periodic
 * PING/STATUS heartbeat so real traffic stays readable.
 */

import { h } from './dom.js';
import { parseTimestamp } from './events.js';

const HEARTBEAT = [/^PING$/i, /^STATUS$/i, /^OK PONG$/i, /^OK STATUS\b/i];

export function isHeartbeat(line) {
  const text = String(line || '').trim();
  return HEARTBEAT.some((re) => re.test(text));
}

function timeOf(ts) {
  const ms = parseTimestamp(ts);
  const d = Number.isFinite(ms) ? new Date(ms) : new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
}

export function createLineLog(listEl, { max = 400, hideHeartbeat = true } = {}) {
  const entries = [];
  const seen = new Set();
  let paused = false;
  let hideHb = hideHeartbeat;

  const visible = (e) => !(hideHb && isHeartbeat(e.line));

  function row(e) {
    const tx = e.dir === 'tx';
    const isErr = !tx && /^ERR\b/i.test(e.line);
    return h('li', { class: isErr ? 'is-err' : null },
      h('span', { class: 't' }, e.time),
      h('span', { class: `dir ${tx ? 'dir-tx' : 'dir-rx'}` }, tx ? 'TX →' : 'RX ←'),
      h('span', { class: 'line' }, e.line),
    );
  }

  function nearBottom() {
    return listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < 48;
  }

  function trimDom() {
    while (listEl.children.length > max) listEl.firstElementChild.remove();
  }

  function rerender() {
    listEl.replaceChildren(...entries.filter(visible).slice(-max).map(row));
    listEl.scrollTop = listEl.scrollHeight;
  }

  return {
    /** Add one bus envelope ({seq, ts, data: {dir, line}}). */
    add(envelope) {
      const data = envelope?.data || {};
      if (!data.line) return;
      const key = `${envelope.seq}|${envelope.ts}`;
      if (envelope.seq !== undefined) {
        if (seen.has(key)) return;
        seen.add(key);
      }
      const entry = { key, dir: data.dir === 'tx' ? 'tx' : 'rx', line: String(data.line), time: timeOf(envelope.ts) };
      entries.push(entry);
      if (entries.length > max * 2) {
        entries.splice(0, entries.length - max);
        seen.clear();
        for (const e of entries) seen.add(e.key);
      }
      if (paused || !visible(entry)) return;
      const stick = nearBottom();
      listEl.append(row(entry));
      trimDom();
      if (stick) listEl.scrollTop = listEl.scrollHeight;
    },
    clear() {
      entries.length = 0;
      listEl.replaceChildren();
    },
    setPaused(on) {
      paused = Boolean(on);
      if (!paused) rerender();
    },
    setHideHeartbeat(on) {
      hideHb = Boolean(on);
      rerender();
    },
    get size() {
      return entries.length;
    },
  };
}
