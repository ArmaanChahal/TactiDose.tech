/**
 * Pure audio helpers for the offline speech path (POST /api/agent/transcribe takes raw
 * 16-bit little-endian mono PCM at 16 kHz, at most 30 s). No DOM; unit-tested under Node.
 */

export const TARGET_RATE = 16000;
/** The server accepts at most 30 s; stop a little earlier to stay under the limit. */
export const MAX_SECONDS = 29.5;

export function concatFloat32(chunks) {
  let length = 0;
  for (const c of chunks) length += c.length;
  const out = new Float32Array(length);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

/**
 * Convert mono float samples from `inRate` to `outRate`. Downsampling averages every
 * input sample that falls into an output sample's window (a box low-pass filter, good
 * enough for speech recognition); upsampling interpolates linearly.
 */
export function resample(input, inRate, outRate = TARGET_RATE) {
  if (!(inRate > 0) || !(outRate > 0)) throw new RangeError('sample rates must be positive');
  if (inRate === outRate) return Float32Array.from(input);
  const ratio = inRate / outRate;
  const outLength = Math.floor(input.length / ratio);
  const out = new Float32Array(outLength);
  if (ratio > 1) {
    for (let i = 0; i < outLength; i += 1) {
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.max(start + 1, Math.floor((i + 1) * ratio)));
      let sum = 0;
      for (let j = start; j < end; j += 1) sum += input[j];
      out[i] = sum / (end - start);
    }
  } else {
    for (let i = 0; i < outLength; i += 1) {
      const pos = i * ratio;
      const j = Math.floor(pos);
      const a = input[j] ?? 0;
      const b = j + 1 < input.length ? input[j + 1] : a;
      out[i] = a + (b - a) * (pos - j);
    }
  }
  return out;
}

/** Float samples in [-1, 1] -> ArrayBuffer of signed 16-bit little-endian PCM. */
export function floatToPcm16(samples) {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < samples.length; i += 1) {
    const v = Number.isFinite(samples[i]) ? Math.max(-1, Math.min(1, samples[i])) : 0;
    view.setInt16(i * 2, v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7fff), true);
  }
  return buffer;
}

export function rms(samples) {
  if (!samples || !samples.length) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

/**
 * End-of-speech detector for push-to-talk: after speech was heard, `silenceMs` of quiet
 * means the person finished. `update(chunk, sampleRate)` returns 'waiting' (no speech
 * yet), 'speaking', 'done' (speech then silence) or 'nothing' (no speech for maxWaitMs).
 */
export function createSilenceDetector({ threshold = 0.015, silenceMs = 1600, maxWaitMs = 8000 } = {}) {
  let heard = false;
  let quietMs = 0;
  let waitedMs = 0;
  return {
    update(chunk, sampleRate) {
      const ms = (chunk.length / sampleRate) * 1000;
      if (rms(chunk) >= threshold) {
        heard = true;
        quietMs = 0;
        return 'speaking';
      }
      if (!heard) {
        waitedMs += ms;
        return waitedMs >= maxWaitMs ? 'nothing' : 'waiting';
      }
      quietMs += ms;
      return quietMs >= silenceMs ? 'done' : 'speaking';
    },
    get heard() {
      return heard;
    },
  };
}

/** Text from the recognizer as shown on screen: Vosk's "[unk]" (unknown word) becomes "…". */
export function displayTranscript(text) {
  return String(text || '').replace(/\[unk\]/gi, '…').replace(/\s+/g, ' ').trim();
}
