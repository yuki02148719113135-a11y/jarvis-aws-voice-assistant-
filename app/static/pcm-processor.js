// マイクから来た float32 を int16 の PCM に変換してメインスレッドへ渡す。
// AudioContext 側で 16kHz を指定しているので、ここではリサンプルしない。

const FRAMES_PER_MESSAGE = 1024; // 16kHz で約 64ms

class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Int16Array(FRAMES_PER_MESSAGE);
    this.offset = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      // -1.0〜1.0 を -32768〜32767 に変換する
      const clamped = Math.max(-1, Math.min(1, channel[i]));
      this.buffer[this.offset++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;

      if (this.offset === FRAMES_PER_MESSAGE) {
        // コピーを渡す。転送後に使い回すため
        this.port.postMessage(this.buffer.slice());
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-processor", PCMProcessor);
