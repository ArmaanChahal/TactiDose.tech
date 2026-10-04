/**
 * Voice for the patient portal and kiosk (ARCHITECTURE §7 "Voice").
 *
 * Input — push to talk: the browser's SpeechRecognition when it exists; otherwise (or
 * when that service is unreachable, e.g. offline) the microphone is captured with
 * getUserMedia + an AudioWorklet, resampled to 16 kHz mono PCM16 and sent to
 * POST /api/agent/transcribe (the offline Vosk recognizer on the server).
 *
 * Output — ReplySpeaker plays AgentReply.audio_url and falls back to speechSynthesis.
 * The microphone and the speaker never run at the same time.
 */

import { postRaw } from './api.js';
import { errorText } from './dom.js';
import { MAX_SECONDS, TARGET_RATE, concatFloat32, createSilenceDetector, floatToPcm16, resample } from './pcm.js';

export const WORKLET_URL = '/static/js/pcm-worklet.js';
/** A browser recognizer that has not started after this long is treated as unavailable. */
export const BROWSER_START_TIMEOUT_MS = 3000;

function recognitionClass() {
  return globalThis.SpeechRecognition || globalThis.webkitSpeechRecognition || null;
}

export function browserRecognitionAvailable() {
  return Boolean(recognitionClass());
}

export function micCaptureAvailable() {
  return Boolean(globalThis.navigator?.mediaDevices?.getUserMedia)
    && Boolean(globalThis.AudioContext || globalThis.webkitAudioContext);
}

export function voiceInputAvailable() {
  return browserRecognitionAvailable() || micCaptureAvailable();
}

/** Plain-language text for getUserMedia / SpeechRecognition failures. */
export function micErrorText(err) {
  const name = typeof err === 'string' ? err : err?.name || err?.error;
  switch (name) {
    case 'NotAllowedError':
    case 'not-allowed':
    case 'SecurityError':
      return 'The microphone is blocked. Allow the microphone for this page in the browser, or type your message.';
    case 'NotFoundError':
    case 'OverconstrainedError':
    case 'audio-capture':
      return 'No microphone was found. Please type your message.';
    case 'NotReadableError':
      return 'The microphone is being used by another program. Close it, or type your message.';
    case 'no-speech':
      return 'I did not hear anything. Press Talk and try again.';
    default:
      return `The microphone could not start (${errorText(err)}). Please type your message.`;
  }
}

/**
 * Push-to-talk controller.
 * Callbacks: onState(state, message) with state 'idle' | 'starting' | 'listening' |
 * 'processing' | 'error'; onInterim(text); onResult(text, {mode, confidence}).
 */
export class VoiceInput {
  constructor({
    onState = () => {},
    onInterim = () => {},
    onResult = () => {},
    preferOffline = () => false,
    lang = 'en-US',
    startTimeoutMs = BROWSER_START_TIMEOUT_MS,
  } = {}) {
    this.startTimeoutMs = startTimeoutMs;
    this.onState = onState;
    this.onInterim = onInterim;
    this.onResult = onResult;
    this.preferOffline = preferOffline;
    this.lang = lang;
    this._state = 'idle';
    this._rec = null;
    this._capture = null;
    this._browserBroken = false;
    this._session = 0;
  }

  get state() {
    return this._state;
  }

  get active() {
    return this._state === 'starting' || this._state === 'listening';
  }

  /** Which engine the next start() will use: 'browser' | 'offline' | null. */
  get engine() {
    if (browserRecognitionAvailable() && !this._browserBroken && !this.preferOffline()) return 'browser';
    return micCaptureAvailable() ? 'offline' : null;
  }

  _set(state, message = '') {
    this._state = state;
    try {
      this.onState(state, message);
    } catch (err) {
      console.error('TactiDose voice: state handler failed', err);
    }
  }

  toggle() {
    if (this.active) this.stop();
    else if (this._state !== 'processing') this.start();
  }

  start() {
    if (this.active || this._state === 'processing') return;
    const engine = this.engine;
    if (engine === 'browser') this._startBrowser();
    else if (engine === 'offline') this._startOffline();
    else this._set('error', 'Voice input is not available in this browser. Please type your message.');
  }

  /** Finish listening and send what was heard. */
  stop() {
    if (this._rec) {
      try {
        this._rec.stop();
      } catch {
        /* already stopped */
      }
    } else if (this._capture) {
      this._finishOffline(true);
    } else if (this.active) {
      // Still waiting for microphone permission: give up quietly.
      this.cancel();
    }
  }

  /** Stop listening without sending anything. */
  cancel() {
    this._session += 1;
    if (this._rec) {
      const rec = this._rec;
      this._rec = null;
      try {
        rec.abort();
      } catch {
        /* ignore */
      }
      this._set('idle', 'Stopped listening.');
    } else if (this._capture) {
      this._finishOffline(false);
    } else if (this.active) {
      this._set('idle', 'Stopped listening.');
    }
  }

  // ---------------------------------------------------------------- browser recognition

  /**
   * Make sure the microphone permission is answered before the recognizer starts, so a slow
   * "Allow" click is not mistaken for a broken speech service by the start watchdog.
   */
  async _ensureMicPermission() {
    try {
      const status = await navigator.permissions?.query({ name: 'microphone' });
      if (status?.state === 'granted') return;
    } catch {
      /* this browser cannot query the microphone permission: ask below */
    }
    if (!navigator.mediaDevices?.getUserMedia) return;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
  }

  async _startBrowser() {
    const session = ++this._session;
    this._set('starting', 'Starting the microphone…');
    try {
      await this._ensureMicPermission();
    } catch (err) {
      if (session === this._session) this._set('error', micErrorText(err));
      return;
    }
    if (session !== this._session) return;
    const SR = recognitionClass();
    let rec;
    try {
      rec = new SR();
    } catch {
      this._browserBroken = true;
      this._startOffline();
      return;
    }
    this._rec = rec;
    rec.lang = this.lang;
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;
    let finalText = '';
    let confidence = null;
    let failed = false;
    let started = false;
    // Some browsers have the API but no speech service: it never starts and never fails.
    const watchdog = setTimeout(() => {
      if (session !== this._session || started) return;
      this._browserBroken = true;
      this._rec = null;
      this._session += 1; // the aborted recognizer's own "end" must not report "did not hear"
      try {
        rec.abort();
      } catch {
        /* ignore */
      }
      if (micCaptureAvailable()) this._startOffline();
      else this._set('error', 'Speech recognition is not available. Please type your message.');
    }, this.startTimeoutMs);
    const markStarted = () => {
      started = true;
      clearTimeout(watchdog);
    };
    rec.addEventListener('audiostart', markStarted);
    rec.addEventListener('start', () => {
      markStarted();
      if (session === this._session) this._set('listening', 'Listening. Speak now.');
    });
    rec.addEventListener('result', (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i += 1) {
        const r = e.results[i];
        if (r.isFinal) {
          finalText += r[0].transcript;
          confidence = r[0].confidence;
        } else {
          interim += r[0].transcript;
        }
      }
      if (session === this._session) this.onInterim(`${finalText} ${interim}`.trim());
    });
    rec.addEventListener('error', (e) => {
      clearTimeout(watchdog);
      if (session !== this._session) return;
      failed = true;
      if (['network', 'service-not-allowed', 'language-not-supported', 'bad-grammar'].includes(e.error)) {
        // The browser's speech service is unreachable: use the offline recognizer instead.
        this._browserBroken = true;
        this._rec = null;
        if (micCaptureAvailable()) {
          this._startOffline();
          return;
        }
        this._set('error', 'Speech recognition is not available. Please type your message.');
      } else if (e.error === 'aborted') {
        this._set('idle', 'Stopped listening.');
      } else {
        this._set('error', micErrorText(e.error));
      }
    });
    rec.addEventListener('end', () => {
      clearTimeout(watchdog);
      if (session !== this._session) return;
      this._rec = null;
      const text = finalText.trim();
      if (text) {
        this._set('idle', '');
        this.onResult(text, { mode: 'browser', confidence });
      } else if (!failed) {
        this._set('idle', 'I did not hear anything. Press Talk and try again.');
      }
    });
    try {
      rec.start();
    } catch {
      clearTimeout(watchdog);
      this._rec = null;
      this._browserBroken = true;
      this._startOffline();
    }
  }

  // ---------------------------------------------------------------- offline capture

  async _startOffline() {
    if (!micCaptureAvailable()) {
      this._set('error', 'Voice input is not available in this browser. Please type your message.');
      return;
    }
    const session = ++this._session;
    this._set('starting', 'Starting the microphone…');
    let stream = null;
    let ctx = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
      ctx = new Ctx();
      if (ctx.state === 'suspended') await ctx.resume();
      const source = ctx.createMediaStreamSource(stream);
      const sink = ctx.createGain();
      sink.gain.value = 0;
      sink.connect(ctx.destination);
      const capture = { stream, ctx, source, sink, node: null, chunks: [], frames: 0, rate: ctx.sampleRate };
      const detector = createSilenceDetector();
      const onChunk = (chunk) => {
        if (this._capture !== capture) return;
        capture.chunks.push(chunk);
        capture.frames += chunk.length;
        const verdict = detector.update(chunk, capture.rate);
        if (capture.frames / capture.rate >= MAX_SECONDS || verdict === 'done') this._finishOffline(true);
        else if (verdict === 'nothing') this._finishOffline(false, 'I did not hear anything. Press Talk and try again.');
      };
      if (ctx.audioWorklet && typeof AudioWorkletNode === 'function') {
        try {
          await ctx.audioWorklet.addModule(WORKLET_URL);
          capture.node = new AudioWorkletNode(ctx, 'tactidose-pcm-capture');
          capture.node.port.addEventListener('message', (e) => onChunk(e.data));
          capture.node.port.start();
        } catch (err) {
          console.warn('TactiDose voice: AudioWorklet unavailable, using ScriptProcessor', err);
          capture.node = null;
        }
      }
      if (!capture.node) {
        capture.node = ctx.createScriptProcessor(4096, 1, 1);
        capture.node.addEventListener('audioprocess', (e) => onChunk(new Float32Array(e.inputBuffer.getChannelData(0))));
      }
      if (session !== this._session) throw new DOMException('cancelled', 'AbortError');
      source.connect(capture.node);
      capture.node.connect(sink);
      this._capture = capture;
      this._set('listening', 'Listening. Speak now, then press Talk again to send.');
    } catch (err) {
      stream?.getTracks().forEach((t) => t.stop());
      ctx?.close().catch(() => {});
      if (err?.name === 'AbortError') return;
      this._set('error', micErrorText(err));
    }
  }

  async _finishOffline(send, message = null) {
    const capture = this._capture;
    this._capture = null;
    if (!capture) return;
    try {
      capture.source.disconnect();
      capture.node?.disconnect();
    } catch {
      /* already disconnected */
    }
    capture.stream.getTracks().forEach((t) => t.stop());
    capture.ctx.close().catch(() => {});
    if (!send) {
      this._set('idle', message || 'Stopped listening.');
      return;
    }
    const samples = concatFloat32(capture.chunks);
    if (samples.length < capture.rate * 0.3) {
      this._set('idle', 'That was too short. Press Talk and speak.');
      return;
    }
    const pcm = floatToPcm16(resample(samples, capture.rate, TARGET_RATE));
    this._set('processing', 'Working out what you said…');
    try {
      const res = await postRaw('/api/agent/transcribe', pcm, { contentType: 'application/octet-stream' });
      const text = String(res?.text || '').trim();
      if (!text) {
        this._set('idle', 'I did not catch that. Please try again.');
        return;
      }
      this._set('idle', '');
      this.onResult(text, { mode: 'offline', confidence: res?.confidence ?? null });
    } catch (err) {
      this._set('error', err?.status === 503
        ? 'Speech recognition is not available on this server right now. Please type your message.'
        : `I could not understand the recording: ${errorText(err)}`);
    }
  }
}

/** Speaks replies: server audio (AgentReply.audio_url) first, then the browser's voice. */
export class ReplySpeaker {
  constructor({ onState = () => {} } = {}) {
    this.onState = onState;
    this._audio = null;
    this._speaking = false;
  }

  get speaking() {
    return this._speaking;
  }

  _set(speaking) {
    this._speaking = speaking;
    try {
      this.onState(speaking);
    } catch {
      /* ignore */
    }
  }

  stop() {
    if (this._audio) {
      this._audio.pause();
      this._audio = null;
    }
    try {
      globalThis.speechSynthesis?.cancel();
    } catch {
      /* ignore */
    }
    this._set(false);
  }

  /** Browser speech synthesis. Returns false when unavailable. */
  synth(text) {
    const synth = globalThis.speechSynthesis;
    if (!synth || typeof SpeechSynthesisUtterance !== 'function' || !text) return false;
    synth.cancel();
    const u = new SpeechSynthesisUtterance(String(text));
    u.lang = 'en-US';
    u.rate = 0.95;
    u.addEventListener('end', () => this._set(false));
    u.addEventListener('error', () => this._set(false));
    this._set(true);
    synth.speak(u);
    return true;
  }

  speak(text, audioUrl = null) {
    this.stop();
    if (!audioUrl) return this.synth(text);
    const audio = new Audio(audioUrl);
    this._audio = audio;
    let fellBack = false;
    const fallback = () => {
      if (fellBack || this._audio !== audio) return;
      fellBack = true;
      this._audio = null;
      this.synth(text);
    };
    audio.addEventListener('ended', () => {
      if (this._audio === audio) this._audio = null;
      this._set(false);
    });
    audio.addEventListener('error', fallback);
    this._set(true);
    audio.play().catch(fallback);
    return true;
  }
}
