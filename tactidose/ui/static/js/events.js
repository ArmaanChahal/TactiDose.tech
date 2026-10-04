/**
 * Live updates from GET /api/events (Server-Sent Events, filtered per signed-in user).
 *
 * Each SSE message is `event: <topic>` with `data: {"seq", "topic", "data", "ts"}`.
 * On connect the server replays the last 50 permitted events, so after a reconnect the
 * same events can arrive twice: they are de-duplicated by (seq, ts) — ts makes the key
 * survive a server restart, where seq starts again from 1.
 *
 * Reconnection is managed here (exponential backoff with jitter) instead of relying on
 * the browser's built-in retry, which gives up after a non-200 response (for example a
 * 401 once the session has ended — see session.watchSession()).
 * Handlers receive (data, envelope, meta) where meta.replayed is true for events that
 * were replayed from history right after connecting (so pages can update their view
 * without re-announcing stale messages).
 */

/** Topics of tactidose.core.bus.Topic used by the portals. */
export const PORTAL_TOPICS = Object.freeze([
  'notification',
  'drop.updated',
  'patient.status',
  'agent.message',
  'report.updated',
  'device.state',
  'wellbeing.prompt',
]);

/** Extra topics sent in demo mode to users linked to the device's patient. */
export const DEMO_TOPICS = Object.freeze([
  'device.line',
  'device.event',
  'sim.physical',
  'clock.changed',
  'system.notice',
]);

export const KNOWN_TOPICS = Object.freeze([...PORTAL_TOPICS, ...DEMO_TOPICS]);

/** Pseudo-topic dispatched (with no data) when the stream re-opens after a drop. */
export const RECONNECTED = 'stream.reconnected';

const DEDUPE_LIMIT = 2000;
const REPLAY_WINDOW_MS = 1500;
const REPLAY_MIN_AGE_MS = 2500;

/** Parse an ISO-8601 timestamp that may carry microseconds (Python isoformat). */
export function parseTimestamp(ts) {
  if (!ts || typeof ts !== 'string') return NaN;
  return Date.parse(ts.replace(/(\.\d{3})\d+/, '$1'));
}

export class EventStream {
  /**
   * @param {object} [options]
   * @param {string} [options.url]
   * @param {(url: string) => EventSource} [options.eventSourceFactory] test hook
   * @param {{setTimeout: Function, clearTimeout: Function}} [options.timers] test hook
   * @param {() => number} [options.now] wall-clock ms (test hook)
   * @param {() => number} [options.random] 0..1 for jitter (test hook)
   */
  constructor({
    url = '/api/events',
    eventSourceFactory = null,
    minDelayMs = 1000,
    maxDelayMs = 15000,
    timers = null,
    now = null,
    random = null,
  } = {}) {
    this.url = url;
    this._factory = eventSourceFactory || ((u) => new EventSource(u, { withCredentials: true }));
    this._minDelay = minDelayMs;
    this._maxDelay = maxDelayMs;
    this._timers = timers || { setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: (t) => clearTimeout(t) };
    this._now = now || (() => Date.now());
    this._random = random || Math.random;
    this._handlers = new Map();
    this._statusHandlers = new Set();
    this._topics = new Set(KNOWN_TOPICS);
    this._seen = new Set();
    this._seenOrder = [];
    this._es = null;
    this._timer = null;
    this._attempt = 0;
    this._failures = 0;
    this._openedAt = 0;
    this._everOpened = false;
    this._closed = false;
    this._status = 'idle';
    this._onVisible = null;
  }

  get status() {
    return this._status;
  }

  /** Consecutive connection failures since the last successful open. */
  get failures() {
    return this._failures;
  }

  /** Subscribe to a topic ('*' = every topic). Returns an unsubscribe function. */
  on(topic, handler) {
    if (!this._handlers.has(topic)) this._handlers.set(topic, new Set());
    this._handlers.get(topic).add(handler);
    if (topic !== '*' && topic !== RECONNECTED && !this._topics.has(topic)) {
      this._topics.add(topic);
      if (this._es) this._listen(this._es, topic);
    }
    return () => this._handlers.get(topic)?.delete(handler);
  }

  /** Connection status callback: 'connecting' | 'open' | 'reconnecting' | 'closed'. */
  onStatus(handler) {
    this._statusHandlers.add(handler);
    handler(this._status);
    return () => this._statusHandlers.delete(handler);
  }

  start() {
    this._closed = false;
    if (this._es || this._timer) return this;
    this._setStatus('connecting');
    this._connect();
    if (typeof document !== 'undefined' && !this._onVisible) {
      this._onVisible = () => {
        if (document.visibilityState === 'visible' && this._status !== 'open' && !this._closed) this.reconnectNow();
      };
      document.addEventListener('visibilitychange', this._onVisible);
    }
    return this;
  }

  /** Drop any pending backoff and connect immediately. */
  reconnectNow() {
    if (this._closed) return;
    if (this._timer) {
      this._timers.clearTimeout(this._timer);
      this._timer = null;
    }
    this._dropSource();
    this._connect();
  }

  close() {
    this._closed = true;
    if (this._timer) this._timers.clearTimeout(this._timer);
    this._timer = null;
    this._dropSource();
    if (this._onVisible && typeof document !== 'undefined') document.removeEventListener('visibilitychange', this._onVisible);
    this._onVisible = null;
    this._setStatus('closed');
  }

  // ---------------------------------------------------------------- internals

  _setStatus(status) {
    if (this._status === status) return;
    this._status = status;
    for (const handler of this._statusHandlers) {
      try {
        handler(status);
      } catch (err) {
        console.error('CareBridge events: status handler failed', err);
      }
    }
  }

  _listen(es, topic) {
    es.addEventListener(topic, (ev) => this._handleRaw(topic, ev.data));
  }

  _connect() {
    let es;
    try {
      es = this._factory(this.url);
    } catch (err) {
      console.error('CareBridge events: could not open stream', err);
      this._failures += 1;
      this._scheduleReconnect();
      return;
    }
    this._es = es;
    es.addEventListener('open', () => {
      if (this._es !== es) return;
      this._attempt = 0;
      this._failures = 0;
      this._openedAt = this._now();
      this._setStatus('open');
      if (this._everOpened) this._dispatch(RECONNECTED, null, null, { replayed: false });
      this._everOpened = true;
    });
    es.addEventListener('error', () => {
      if (this._es !== es || this._closed) return;
      this._failures += 1;
      this._dropSource();
      this._setStatus('reconnecting');
      this._scheduleReconnect();
    });
    es.addEventListener('message', (ev) => this._handleRaw(null, ev.data));
    for (const topic of this._topics) this._listen(es, topic);
  }

  _dropSource() {
    if (this._es) {
      try {
        this._es.close();
      } catch {
        /* ignore */
      }
    }
    this._es = null;
  }

  _scheduleReconnect() {
    if (this._closed || this._timer) return;
    const base = Math.min(this._maxDelay, this._minDelay * 2 ** this._attempt);
    const delay = Math.round(base + this._random() * 250);
    this._attempt += 1;
    this._timer = this._timers.setTimeout(() => {
      this._timer = null;
      if (!this._closed) this._connect();
    }, delay);
    this.lastDelayMs = delay;
  }

  _handleRaw(eventType, raw) {
    let envelope;
    try {
      envelope = JSON.parse(raw);
    } catch {
      console.warn('CareBridge events: ignoring malformed message', raw);
      return;
    }
    if (!envelope || typeof envelope !== 'object') return;
    const topic = envelope.topic || eventType;
    if (!topic) return;
    const key = `${envelope.seq}|${envelope.ts}`;
    if (envelope.seq !== undefined) {
      if (this._seen.has(key)) return;
      this._seen.add(key);
      this._seenOrder.push(key);
      if (this._seenOrder.length > DEDUPE_LIMIT) this._seen.delete(this._seenOrder.shift());
    }
    const now = this._now();
    const age = now - parseTimestamp(envelope.ts);
    const replayed = now - this._openedAt < REPLAY_WINDOW_MS && Number.isFinite(age) && age > REPLAY_MIN_AGE_MS;
    this._dispatch(topic, envelope.data ?? {}, envelope, { replayed });
  }

  _dispatch(topic, data, envelope, meta) {
    const targets = [...(this._handlers.get(topic) || []), ...(topic === RECONNECTED ? [] : this._handlers.get('*') || [])];
    for (const handler of targets) {
      try {
        handler(data, envelope, meta);
      } catch (err) {
        console.error(`CareBridge events: handler for ${topic} failed`, err);
      }
    }
  }
}
