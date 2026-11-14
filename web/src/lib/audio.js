// Audio helpers: mic level meter + recording the Hindi the page plays as WAV clips.
//
// MediaRecorder on a remote WebRTC track is unreliable (empty / unplayable webm
// clips), so we tap the raw PCM the page is already playing and write plain WAV.

export const CLIP_SR = 24000;

export function resample(input, sr) {
  const r = sr / CLIP_SR;
  const out = new Float32Array(Math.floor(input.length / r));
  for (let i = 0; i < out.length; i++) out[i] = input[Math.floor(i * r)];
  return out;
}

export function trimSilence(pcm, thr = 0.01) {
  let a = 0, b = pcm.length;
  while (a < b && Math.abs(pcm[a]) < thr) a++;
  while (b > a && Math.abs(pcm[b - 1]) < thr) b--;
  const pad = CLIP_SR * 0.05; // keep 50 ms around the speech
  return pcm.subarray(Math.max(0, a - pad), Math.min(pcm.length, b + pad));
}

export function concat(chunks) {
  const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}

export function wavBlob(pcm, sr = CLIP_SR) {
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(buf);
  const str = (o, s) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, pcm.length * 2, true);
  pcm.forEach((s, i) => v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 0x7fff, true));
  return new Blob([buf], { type: "audio/wav" });
}

/** Calls onPeak(0..1) every animation frame with the stream's level. Returns a stop function. */
export function micMeter(ctx, stream, onPeak) {
  const src = ctx.createMediaStreamSource(stream);
  const an = ctx.createAnalyser();
  an.fftSize = 256;
  src.connect(an);
  const buf = new Uint8Array(an.frequencyBinCount);
  let raf = 0;
  const tick = () => {
    an.getByteTimeDomainData(buf);
    let peak = 0;
    for (const v of buf) peak = Math.max(peak, Math.abs(v - 128) / 128);
    onPeak(peak);
    raf = requestAnimationFrame(tick);
  };
  tick();
  return () => { cancelAnimationFrame(raf); src.disconnect(); };
}

/** Records each spoken Hindi line from the agent's audio stream as a WAV clip. */
export class HindiRecorder {
  constructor(ctx, stream) {
    this.recording = false;
    this.clip = [];
    this.preroll = [];
    this.session = [];
    this.stopTimer = null;
    this.pendingDone = null;

    const src = ctx.createMediaStreamSource(stream);
    const tap = ctx.createScriptProcessor(4096, 1, 1);
    const mute = ctx.createGain();
    mute.gain.value = 0; // the <audio> element already plays it; this path is silent
    src.connect(tap); tap.connect(mute); mute.connect(ctx.destination);
    tap.onaudioprocess = (e) => {
      const chunk = resample(e.inputBuffer.getChannelData(0), ctx.sampleRate);
      if (this.recording) { this.clip.push(chunk); return; }
      // pre-roll: the "speaking" signal arrives a moment after the audio starts, so keep
      // the last ~0.6 s to prepend; otherwise the replay clip loses its first word
      this.preroll.push(chunk);
      while (this.preroll.length > 7) this.preroll.shift(); // 4096 samples ≈ 85 ms each
    };
  }

  start() {
    if (this.stopTimer) { clearTimeout(this.stopTimer); this.finish(this.pendingDone); }
    this.clip = this.preroll.splice(0);
    this.recording = true;
  }

  /** onDone({url, seconds}) gets the finished clip (if anything audible was spoken). */
  stop(onDone) {
    if (!this.recording || this.stopTimer) return;
    this.pendingDone = onDone;
    // the tap lags playback by one buffer (~85 ms): keep recording a moment longer
    this.stopTimer = setTimeout(() => this.finish(onDone), 300);
  }

  finish(onDone) {
    this.stopTimer = null;
    this.pendingDone = null;
    this.recording = false;
    const pcm = trimSilence(concat(this.clip));
    this.clip = [];
    if (pcm.length < CLIP_SR * 0.2) return; // nothing audible was spoken
    this.session.push(pcm, new Float32Array(CLIP_SR * 0.4)); // 0.4 s gap between lines
    onDone?.({ url: URL.createObjectURL(wavBlob(pcm)), seconds: pcm.length / CLIP_SR });
  }

  sessionWav() {
    return wavBlob(concat(this.session));
  }
}

export function download(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
}
