/**
 * AudioWorklet processor for the offline speech path: copies the microphone input
 * (mixed down to mono, at the AudioContext rate) to the main thread in ~2048-sample
 * blocks. Resampling to 16 kHz PCM16 happens in pcm.js. Loaded with
 * audioWorklet.addModule('/static/js/pcm-worklet.js'); it outputs silence.
 */

const BLOCK = 2048;

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(BLOCK);
    this.filled = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input.length && input[0].length) {
      const frames = input[0].length;
      for (let i = 0; i < frames; i += 1) {
        let sum = 0;
        for (let c = 0; c < input.length; c += 1) sum += input[c][i];
        this.buffer[this.filled] = sum / input.length;
        this.filled += 1;
        if (this.filled === BLOCK) {
          const block = this.buffer;
          this.port.postMessage(block, [block.buffer]);
          this.buffer = new Float32Array(BLOCK);
          this.filled = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('tactidose-pcm-capture', PcmCaptureProcessor);
