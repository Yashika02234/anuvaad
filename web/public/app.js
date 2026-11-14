/* English → Hindi live interpreter: browser side.
 *
 * Everything heavy runs in the LiveKit agent. This page:
 *  - publishes the mic over WebRTC (echo cancellation on, so Hindi playback isn't re-heard)
 *  - shows live English transcripts + the cleaned sentence the LLM actually translated
 *  - shows streaming Hindi text, plays Hindi audio, and records each Hindi clip so it
 *    stays "ready to play" (per line + whole session download)
 *  - lets the user switch Hands-free / Hold to talk and the tone (RPC)
 */
const LK = window.LivekitClient;
const $ = (s) => document.querySelector(s);

let room = null;
let agentIdentity = null;
let inputMode = "auto";
let rows = []; // {el, segId, state: listening|held|translating|done|dropped, hiSegId, clip, metrics, en, hi}
let clipRow = null; // row whose Hindi is currently being spoken (gets the recorded clip)
let agentStream = null;

// ------------------------------------------------------------------ rows ---
function newRow() {
  $("#empty")?.remove();
  const el = document.createElement("div");
  el.className = "row";
  el.innerHTML = `
    <div class="en"><div class="raw live"></div><div class="clean"></div></div>
    <div class="hi"><div class="text" lang="hi"></div></div>
    <div class="meta"><div class="hi-foot"></div><div class="chips en-chips"></div><div class="chips hi-chips"></div></div>`;
  $("#rows").appendChild(el);
  const row = { el, state: "listening", en: "", hi: "" };
  rows.push(row);
  el.scrollIntoView({ block: "end", behavior: "smooth" });
  return row;
}
const unanswered = () => rows.filter((r) => ["listening", "held"].includes(r.state));
function chip(row, side, text, cls = "") {
  const c = document.createElement("span");
  c.className = `chip ${cls}`;
  c.textContent = text;
  row.el.querySelector(`.${side}-chips`).appendChild(c);
  return c;
}
function clearChips(row, side, cls) {
  row.el.querySelectorAll(`.${side}-chips .chip${cls ? "." + cls : ""}`).forEach((c) => c.remove());
}

// ------------------------------------------------------- transcriptions ---
async function onTranscription(reader, { identity }) {
  const attrs = reader.info.attributes || {};
  const segId = attrs["lk.segment_id"] || reader.info.id;
  const isFinal = attrs["lk.transcription_final"] === "true";
  const isUser = identity === room.localParticipant.identity;

  if (isUser) {
    let row = rows.find((r) => r.segId === segId);
    if (!row) { row = newRow(); row.segId = segId; }
    let text = "";
    for await (const chunk of reader) {
      text += chunk;
      row.el.querySelector(".raw").textContent = text;
    }
    row.en = text;
    if (isFinal) row.el.querySelector(".raw").classList.remove("live");
    return;
  }

  // agent (Hindi) transcript: streams in sync with the audio
  let row = rows.find((r) => r.hiSegId === segId) ||
            rows.find((r) => r.state === "translating" && !r.hiSegId) ||
            [...rows].reverse().find((r) => r.state !== "dropped");
  if (!row) row = newRow();
  row.hiSegId = segId;
  clipRow = row;
  const box = row.el.querySelector(".hi .text");
  let text = "";
  for await (const chunk of reader) {
    text += chunk;
    box.textContent = text;
  }
  row.hi = text;
  row.state = "done";
  if (text.trim()) $("#dl-text").hidden = false; // first translation: offer the transcript
}

// ------------------------------------------------------ pipeline events ---
function onEvent(ev) {
  heldSentence = ev.type === "held"; // any other event ends the "waiting for the rest" state
  renderOrb();
  if (ev.type === "held") {
    const row = unanswered().at(-1);
    if (row) { row.state = "held"; chip(row, "en", "⏳ waiting for the rest of the sentence", "warn"); }
  } else if (ev.type === "dropped") {
    const row = unanswered().at(-1);
    const fillerOnly = ev.reason === "only fillers" && ev.fillers?.length;
    const why = fillerOnly ? `only fillers: ${ev.fillers.join(", ")}` : ev.reason || "noise / no speech";
    if (row) {
      row.state = "dropped"; row.el.classList.add("dropped");
      const raw = row.el.querySelector(".raw");
      raw.classList.remove("live");
      if (ev.raw && !raw.textContent.trim()) raw.textContent = ev.raw;
      chip(row, "en", why, "grey");
    }
  } else if (ev.type === "turn") {
    // merge any held fragments into one row: that's the sentence we translate
    const pending = unanswered();
    const row = pending[0] || newRow();
    pending.slice(1).forEach((r) => { r.el.remove(); rows = rows.filter((x) => x !== r); });
    row.state = "translating";
    row.el.querySelector(".raw").classList.remove("live");
    row.el.querySelector(".raw").textContent = ev.raw;
    row.el.querySelector(".clean").textContent = ev.cleaned;
    row.en = ev.cleaned;
    // show the raw ASR line only when cleaning actually changed something
    const norm = (t) => (t || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
    row.el.classList.toggle("same", norm(ev.raw) === norm(ev.cleaned));
    clearChips(row, "en");
    if (ev.forced) chip(row, "en", "speaker paused mid-sentence: translated anyway", "grey");
    chip(row, "en", ev.tone === "casual" ? "casual · तुम" : "formal · आप");
    ev.asr_fixes?.forEach((f) => chip(row, "en", `heard: ${f}`));
    ev.fillers?.forEach((f) => chip(row, "en", `removed “${f}”`, "grey"));
    ev.entities?.forEach((e) => chip(row, "en", `kept: ${e}`));
    ev.hints?.forEach((h) => chip(row, "en", h));
  } else if (ev.type === "metrics") {
    const row = [...rows].reverse().find((r) => r.hiSegId && !r.metrics) || rows.at(-1);
    if (!row) return;
    row.metrics = ev;
    row.el.querySelector(".hi-foot").appendChild(latencyBar(ev));
    if (ev.interrupted) chip(row, "hi", "✋ interrupted by speaker", "warn");
  }
}

// stacked bar: where the time went (ASR incl. end-of-speech wait → translate → voice)
function latencyBar(ev) {
  const ms = (v) => (v == null ? "–" : `${Math.round(v * 1000)} ms`);
  const parts = [["asr", ev.stt], ["llm", ev.llm_ttft], ["tts", ev.tts_ttfb]];
  const sum = parts.reduce((a, [, v]) => a + (v || 0), 0) || 1;
  const wrap = document.createElement("span");
  wrap.className = "lat";
  wrap.title = `ASR ${ms(ev.stt)} · Translate ${ms(ev.llm_ttft)} · TTS ${ms(ev.tts_ttfb)}`;
  const bar = document.createElement("span");
  bar.className = "bar";
  parts.forEach(([cls, v]) => {
    const i = document.createElement("i");
    i.className = cls;
    i.style.width = `${((v || 0) / sum) * 100}%`;
    bar.appendChild(i);
  });
  wrap.append(bar);
  if (ev.e2e != null) { // no total for e.g. very short replies the SDK doesn't time: show nothing rather than "–"
    const total = document.createElement("b");
    total.textContent = `${ev.e2e.toFixed(2)}s`;
    if (ev.e2e > 2.5) total.className = "slow";
    wrap.append(total, document.createTextNode("latency"));
  }
  return wrap;
}

// "ready to play" button for a recorded Hindi clip
function playButton(url, seconds) {
  const audio = new Audio(url);
  const b = document.createElement("button");
  b.className = "play";
  b.title = "Replay this Hindi line";
  const set = (on) => {
    b.classList.toggle("on", on);
    b.innerHTML = `<span class="ic">${on ? "■" : "▶"}</span>${on ? "Stop" : "Play Hindi"} <small>${seconds.toFixed(1)}s</small>`;
  };
  b._stop = () => { audio.pause(); set(false); };
  set(false);
  b.onclick = async () => {
    if (!audio.paused) return b._stop();
    document.querySelectorAll(".play.on").forEach((x) => x !== b && x._stop?.()); // one clip at a time
    audio.currentTime = 0;
    try { await audio.play(); set(true); } catch (e) { console.warn("playback failed", e); set(false); }
  };
  audio.onended = audio.onpause = () => set(false);
  return b;
}

// ----------------------------------------------------- audio + metering ---
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
// mic level drives the orb (ring size + "Hearing you…")
function micMeter(stream) {
  const src = audioCtx.createMediaStreamSource(stream);
  const an = audioCtx.createAnalyser();
  an.fftSize = 256;
  src.connect(an);
  const buf = new Uint8Array(an.frequencyBinCount);
  (function tick() {
    an.getByteTimeDomainData(buf);
    let peak = 0;
    for (const v of buf) peak = Math.max(peak, Math.abs(v - 128) / 128);
    micLevel = room ? peak : 0;
    renderOrb();
    requestAnimationFrame(tick);
  })();
}

function onAgentAudio(track) {
  const el = track.attach();
  el.autoplay = true;
  document.body.appendChild(el);
  agentStream = new MediaStream([track.mediaStreamTrack]);
  startTap(agentStream);
}

// ---- Hindi audio capture -------------------------------------------------
// MediaRecorder on a remote WebRTC track is unreliable (empty / unplayable webm
// clips), so we tap the raw PCM the page is already playing and write plain WAV.
const CLIP_SR = 24000;
let tapNode = null, recording = false, clipPCM = [], stopTimer = null, preroll = [];
const sessionPCM = [];

function startTap(stream) {
  if (tapNode) return;
  const src = audioCtx.createMediaStreamSource(stream);
  tapNode = audioCtx.createScriptProcessor(4096, 1, 1);
  const mute = audioCtx.createGain();
  mute.gain.value = 0; // the <audio> element already plays it; this path is silent
  src.connect(tapNode); tapNode.connect(mute); mute.connect(audioCtx.destination);
  tapNode.onaudioprocess = (e) => {
    const chunk = resample(e.inputBuffer.getChannelData(0), audioCtx.sampleRate);
    if (recording) { clipPCM.push(chunk); return; }
    // pre-roll: the "speaking" signal arrives a moment after the audio starts, so keep the
    // last ~0.5 s to prepend; otherwise the replay clip loses its first word
    preroll.push(chunk);
    while (preroll.length > 7) preroll.shift(); // each chunk is 4096 samples ≈ 85 ms -> ~0.6 s
  };
}
function resample(input, sr) {
  const r = sr / CLIP_SR, out = new Float32Array(Math.floor(input.length / r));
  for (let i = 0; i < out.length; i++) out[i] = input[Math.floor(i * r)];
  return out;
}
function trimSilence(pcm, thr = 0.01) {
  let a = 0, b = pcm.length;
  while (a < b && Math.abs(pcm[a]) < thr) a++;
  while (b > a && Math.abs(pcm[b - 1]) < thr) b--;
  const pad = CLIP_SR * 0.05; // keep 50 ms around the speech
  return pcm.subarray(Math.max(0, a - pad), Math.min(pcm.length, b + pad));
}
function concat(chunks) {
  const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}
function wavBlob(pcm, sr = CLIP_SR) {
  const buf = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(buf);
  const str = (o, s) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, pcm.length * 2, true);
  pcm.forEach((s, i) => v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 0x7fff, true));
  return new Blob([buf], { type: "audio/wav" });
}

function startClip() {
  if (!tapNode) return;
  if (stopTimer) { clearTimeout(stopTimer); finishClip(); }
  clipPCM = preroll.splice(0); // start with the audio just before "speaking" was signalled
  recording = true;
}
function stopClip() {
  if (!recording || stopTimer) return;
  const row = clipRow;
  // the tap lags playback by one buffer (~85 ms): keep recording a moment longer
  stopTimer = setTimeout(() => finishClip(row), 300);
}
function finishClip(row = clipRow) {
  stopTimer = null;
  recording = false;
  const pcm = trimSilence(concat(clipPCM));
  clipPCM = [];
  if (!row || pcm.length < CLIP_SR * 0.2) return; // nothing audible was spoken
  sessionPCM.push(pcm, new Float32Array(CLIP_SR * 0.4)); // 0.4 s gap between lines
  $("#dl-audio").hidden = false;
  row.el.querySelector(".hi .play")?.remove();
  row.el.querySelector(".hi-foot").prepend(playButton(URL.createObjectURL(wavBlob(pcm)), pcm.length / CLIP_SR));
}

// ----------------------------------------------------------- UI state ---
// a message shown under the orb while idle (e.g. a connection error)
let orbNote = "";
function setNote(t) { orbNote = t; $("#orb").dataset.state = ""; renderOrb(); }

function onAgentState(state) {
  agentState = state;
  if (state === "speaking") startClip(); else stopClip();
  renderOrb();
}

// ---- mic status orb ------------------------------------------------------
let agentState = null, micLevel = 0, lastVoiceAt = 0, heldSentence = false, connecting = false;
const ORB = {
  idle:       ["Tap to start", "Start session"],
  connecting: ["Connecting…", "Connecting"],
  waiting:    ["Waiting for the translator…", "Waiting for the translator agent"],
  listening:  ["Listening…", "Listening, speak in English"],
  hearing:    ["Hearing you…", "Hearing you"],
  held:       ["Waiting for the rest of the sentence…", "Waiting for the rest of the sentence"],
  thinking:   ["Translating…", "Translating"],
  speaking:   ["Speaking Hindi…", "Speaking Hindi"],
  ptt:        ["Hold to talk (or Space)", "Hold to talk"],
};
function orbState() {
  if (!room) return connecting ? "connecting" : "idle";
  if (!agentIdentity) return connecting ? "connecting" : "waiting";
  if (agentState === "speaking") return "speaking";
  if (agentState === "thinking") return "thinking";
  // hold "hearing" ~0.5 s after the voice drops, so it doesn't flicker between words
  if (micLevel > 0.06) lastVoiceAt = performance.now();
  const talking = inputMode === "ptt" ? pttDown : performance.now() - lastVoiceAt < 500;
  if (talking) return "hearing";
  if (inputMode === "ptt") return "ptt";
  return heldSentence ? "held" : "listening";
}
function renderOrb() {
  const orb = $("#orb");
  orb.parentElement.style.setProperty("--lvl", Math.min(1, micLevel * 2.5).toFixed(2)); // the halo ring
  const s = orbState();
  if (orb.dataset.state === s) return;
  // the hint above the example sentences follows the session state
  const lead = $("#empty-lead");
  if (lead) lead.innerHTML =
    !room ? "Tap the <b>mic</b> (or <b>Start session</b>), allow the microphone, and try one of these:"
    : inputMode === "ptt" ? "<b>Hold the mic</b> (or Space) and say one of these in English:"
    : "<b>Just speak</b> — say one of these in English, then pause:";
  orb.dataset.state = s;
  $(".orb-dock").dataset.state = s; // the label + halo are styled from the dock
  $("#orb-text").textContent = s === "idle" && orbNote ? orbNote : ORB[s][0];
  $("#orb-label").classList.toggle("err", s === "idle" && !!orbNote);
  orb.setAttribute("aria-label", ORB[s][1]);
}

async function rpc(method, payload = "") {
  if (!room || !agentIdentity) return;
  try {
    return await room.localParticipant.performRpc({ destinationIdentity: agentIdentity, method, payload });
  } catch (e) { console.warn("RPC failed", method, e); }
}

// ------------------------------------------------------------ connect ---
async function connect() {
  const btn = $("#connect");
  if (room) { await room.disconnect(); return; }
  btn.disabled = true;
  connecting = true; setNote("");
  try {
    await audioCtx.resume();
    const code = new URLSearchParams(location.search).get("code") || "";
    const res = await fetch(`/api/token?code=${encodeURIComponent(code)}`);
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const { url, token } = await res.json();

    room = new LK.Room({ adaptiveStream: true, dynacast: true });
    room.registerTextStreamHandler("lk.transcription", onTranscription);
    room
      .on(LK.RoomEvent.DataReceived, (payload, _p, _k, topic) => {
        if (topic === "translator.events") onEvent(JSON.parse(new TextDecoder().decode(payload)));
      })
      .on(LK.RoomEvent.TrackSubscribed, (track, _pub, p) => {
        if (track.kind === "audio" && p.isAgent) onAgentAudio(track);
      })
      .on(LK.RoomEvent.ParticipantConnected, (p) => { if (p.isAgent) agentReady(p); })
      .on(LK.RoomEvent.ParticipantAttributesChanged, (attrs, p) => {
        if (p.isAgent && attrs["lk.agent.state"]) onAgentState(attrs["lk.agent.state"]);
      })
      .on(LK.RoomEvent.Disconnected, () => {
        room = null; agentIdentity = null; agentState = null; heldSentence = false; micLevel = 0;        renderOrb();
        if (recording) finishClip();
        btn.textContent = "Start session"; btn.classList.add("primary"); btn.classList.remove("end");
      });

    await room.connect(url, token);
    await room.localParticipant.setMicrophoneEnabled(true, {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    });
    await room.startAudio();
    const mic = room.localParticipant.getTrackPublication(LK.Track.Source.Microphone)?.track;
    if (mic) micMeter(new MediaStream([mic.mediaStreamTrack]));
    room.remoteParticipants.forEach((p) => p.isAgent && agentReady(p));

    btn.textContent = "End session"; btn.classList.remove("primary"); btn.classList.add("end");  } catch (e) {
    console.error(e);
    setNote(`Could not connect: ${e.message}`);
    room = null;
  } finally {
    btn.disabled = false;
    connecting = false; renderOrb();
  }
}

async function agentReady(p) {
  agentIdentity = p.identity;
  if (p.attributes?.["lk.agent.state"]) onAgentState(p.attributes["lk.agent.state"]);
  renderOrb();
  // push current UI settings to the agent
  await rpc("set_tone", $("#tone").value);
  if (inputMode !== "auto") await rpc("set_input_mode", inputMode);
}

// ----------------------------------------------------------- controls ---
$("#connect").onclick = connect;
$("#tone").onchange = (e) => rpc("set_tone", e.target.value);

// custom tone menu: drives the hidden <select id="tone"> so the RPC wiring stays the same
(() => {
  const btn = $("#tone-btn"), list = $("#tone-list"), items = [...list.querySelectorAll("li")];
  const LABEL = { auto: "Tone · Auto", formal: "Formal · आप", casual: "Casual · तुम" };
  const open = (on) => {
    list.hidden = !on;
    btn.setAttribute("aria-expanded", String(on));
    if (on) (items.find((li) => li.getAttribute("aria-selected") === "true") || items[0]).focus();
  };
  const choose = (li) => {
    items.forEach((x) => x.setAttribute("aria-selected", String(x === li)));
    $("#tone-label").textContent = LABEL[li.dataset.v];
    const sel = $("#tone");
    if (sel.value !== li.dataset.v) { sel.value = li.dataset.v; sel.dispatchEvent(new Event("change")); }
    open(false);
    btn.focus();
  };
  btn.onclick = () => open(list.hidden);
  items.forEach((li) => { li.onclick = () => choose(li); });
  list.addEventListener("keydown", (e) => {
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (i >= 0) choose(items[i]); }
    else if (e.key === "Escape") { open(false); btn.focus(); }
  });
  document.addEventListener("click", (e) => { if (!$("#tone-menu").contains(e.target)) open(false); });
})();

document.querySelectorAll(".seg button").forEach((b) => {
  b.onclick = async () => {
    inputMode = b.dataset.mode;
    document.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b));
    renderOrb();
    await rpc("set_input_mode", inputMode);
  };
});

// the orb: tap to start; in push-to-talk mode it is the talk button
let pttDown = false;
async function pttStart() { if (pttDown || inputMode !== "ptt" || !agentIdentity) return; pttDown = true; renderOrb(); await rpc("ptt_start"); }
async function pttEnd() { if (!pttDown) return; pttDown = false; renderOrb(); await rpc("ptt_end"); }
const orb = $("#orb");
orb.addEventListener("click", () => { if (!room && !connecting) connect(); });
orb.addEventListener("pointerdown", (e) => { if (room && inputMode === "ptt") { orb.setPointerCapture(e.pointerId); pttStart(); } });
orb.addEventListener("pointerup", pttEnd);
orb.addEventListener("pointercancel", pttEnd);
const spaceTarget = (e) => e.target === document.body || e.target === orb;
document.addEventListener("keydown", (e) => { if (e.code === "Space" && spaceTarget(e)) { e.preventDefault(); if (!e.repeat) pttStart(); } });
document.addEventListener("keyup", (e) => { if (e.code === "Space" && spaceTarget(e)) { e.preventDefault(); pttEnd(); } });


function download(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
}
$("#dl-audio").onclick = () => download(wavBlob(concat(sessionPCM)), "hindi-session.wav");
$("#dl-text").onclick = () => {
  const lines = rows.filter((r) => r.state !== "dropped" && r.hi)
    .map((r, i) => `${i + 1}. EN: ${r.en}\n   HI: ${r.hi}${r.metrics?.e2e ? `\n   latency: ${r.metrics.e2e.toFixed(2)}s` : ""}`);
  download(new Blob([lines.join("\n\n")], { type: "text/plain;charset=utf-8" }), "transcript.txt");
};
