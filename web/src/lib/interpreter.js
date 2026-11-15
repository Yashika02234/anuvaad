// The realtime engine behind the UI. Everything heavy runs in the LiveKit agent; this:
//  - publishes the mic over WebRTC (echo cancellation on, so Hindi playback isn't re-heard)
//  - collects live English transcripts + the cleaned sentence the LLM actually translated
//  - collects streaming Hindi text, plays Hindi audio, and records each Hindi clip so it
//    stays "ready to play" (per line + whole-session download)
//  - sends the UI settings (Hands-free / Hold to talk, tone) to the agent over RPC
//
// It is plain JavaScript with a tiny subscribe/getSnapshot store, so React reads it with
// useSyncExternalStore and the realtime code stays independent of rendering.

import { Room, RoomEvent, Track } from "livekit-client";
import { HindiRecorder, download, micMeter } from "./audio";

export const ORB = {
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

const norm = (t) => (t || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();

// Free-tier pacing (frontend estimate only). The translator's LLM runs on a free plan that
// fits about 2 sentences a minute; every turn the agent reports (translated or skipped)
// is one LLM call. The page can't see the real rate-limit error, so it infers a failed
// call: the agent went thinking -> listening without speaking and didn't report a skip.
const PACE = {
  windowMs: 60_000,
  // with automatic model fallback there is roughly 3x the room of one free-plan model
  hintAt: 6, // calls in the last minute: gentle tip
  warnAt: 9, // calls in the last minute: "slow down"
  failGraceMs: 1200, // wait this long for a late "dropped" event before calling it a failure
};
const MODEL_NAMES = { qwen: "Qwen", "gpt-oss-20b": "gpt-oss 20B", "gpt-oss-120b": "gpt-oss 120B", gemini: "Gemini" };
const modelName = (m) => MODEL_NAMES[m] || m;
const NOTICE = {
  hint: ["info", "Tip: pause a little between sentences. The translators run on free plans with per-minute limits.", 6000],
  warn: (n) => ["warn", `Slow down a little: ${n} sentences in the last minute. The free plans may start refusing.`, 8000],
  failed: ["warn", "The translator didn't respond. All models may be busy (free-plan limits). Pause a few seconds, then try again.", 10000],
  busy: (m) => ["info", `${modelName(m)} is busy (free-plan limit). The next model takes over.`, 6000],
  back: (m) => ["info", `${modelName(m)} is available again.`, 4000],
};

export class Interpreter {
  constructor() {
    this.room = null;
    this.agentIdentity = null;
    this.agentState = null;
    this.inputMode = "auto";
    this.tone = "auto";
    this.voiceOn = true; // Hindi playback (off = read the screen only)
    this.audioEls = [];
    this.model = "auto"; // auto = the agent's fallback order, or a model to try first
    this.activeModel = null; // the model that translated the latest line
    this.rows = []; // {id, segId, hiSegId, state, raw, rawLive, clean, en, hi, same, enChips, hiChips, metrics, clip}
    this.nextId = 1;
    this.clipRowId = null; // row whose Hindi is currently being spoken (gets the recorded clip)
    this.connecting = false;
    this.note = ""; // shown under the mic while idle (e.g. a connection error)
    this.held = false;
    this.pttDown = false;
    this.micLevel = 0;
    this.lastVoiceAt = 0;
    this.orbState = "idle";
    this.hasTranscript = false;
    this.hasAudio = false;
    this.ctx = null;
    this.recorder = null;
    this.stopMeter = null;
    this.llmCalls = []; // timestamps of turns the agent reported (each one = one LLM call)
    this.thinkingRowIds = []; // rows waiting for a translation when the agent started thinking
    this.notice = null; // {kind: "info" | "warn", text} shown above the mic
    this.noticeTimer = null;

    this.listeners = new Set();
    this.levelListeners = new Set();
    this.snap = null;
    this.emit();
  }

  // ---------------------------------------------------------------- store ---
  subscribe = (fn) => { this.listeners.add(fn); return () => this.listeners.delete(fn); };
  getSnapshot = () => this.snap;
  onLevel = (fn) => { this.levelListeners.add(fn); return () => this.levelListeners.delete(fn); };

  emit() {
    const s = this.orbState;
    this.snap = {
      rows: this.rows.map((r) => ({ ...r })),
      orb: {
        state: s,
        label: s === "idle" && this.note ? this.note : ORB[s][0],
        aria: ORB[s][1],
        error: s === "idle" && !!this.note,
      },
      connected: !!this.room,
      connecting: this.connecting,
      inputMode: this.inputMode,
      tone: this.tone,
      model: this.model,
      activeModel: this.activeModel,
      voiceOn: this.voiceOn,
      hasTranscript: this.hasTranscript,
      hasAudio: this.hasAudio,
      notice: this.notice,
    };
    this.listeners.forEach((fn) => fn());
  }

  // ----------------------------------------------------------------- rows ---
  newRow() {
    const row = { id: this.nextId++, state: "listening", raw: "", rawLive: true, clean: "", en: "", hi: "",
                  same: false, enChips: [], hiChips: [], metrics: null, clip: null };
    this.rows.push(row);
    return row;
  }
  unanswered() { return this.rows.filter((r) => r.state === "listening" || r.state === "held"); }
  chip(row, side, text, cls = "") { row[side === "en" ? "enChips" : "hiChips"].push({ text, cls }); }

  // -------------------------------------------------- free-tier pacing ---
  /** Show a notice above the mic for a while. A "warn" is never replaced by an "info". */
  showNotice([kind, text, ms]) {
    if (this.notice?.kind === "warn" && kind === "info") return;
    clearTimeout(this.noticeTimer);
    this.notice = { kind, text };
    this.noticeTimer = setTimeout(() => { this.notice = null; this.emit(); }, ms);
    this.emit();
  }

  /** One LLM call happened (a turn was translated or skipped): check the pace. */
  noteCall() {
    const now = Date.now();
    this.llmCalls = this.llmCalls.filter((t) => now - t < PACE.windowMs);
    this.llmCalls.push(now);
    const n = this.llmCalls.length;
    if (n >= PACE.warnAt) this.showNotice(NOTICE.warn(n));
    else if (n === PACE.hintAt) this.showNotice(NOTICE.hint);
  }

  /** Rows the agent thought about but never answered (no Hindi, no skip): mark them failed. */
  checkUnanswered(ids) {
    let failed = 0;
    for (const row of this.rows) {
      if (!ids.includes(row.id) || row.hi.trim() || ["done", "dropped", "failed"].includes(row.state)) continue;
      row.state = "failed";
      row.rawLive = false;
      this.chip(row, "en", "⚠ not translated: translator busy?", "warn");
      failed++;
    }
    if (failed) this.showNotice(NOTICE.failed);
  }

  // ------------------------------------------------------- transcriptions ---
  onTranscription = async (reader, { identity }) => {
    const attrs = reader.info.attributes || {};
    const segId = attrs["lk.segment_id"] || reader.info.id;
    const isFinal = attrs["lk.transcription_final"] === "true";

    if (identity === this.room?.localParticipant.identity) {
      let row = this.rows.find((r) => r.segId === segId);
      if (!row) { row = this.newRow(); row.segId = segId; }
      let text = "";
      for await (const chunk of reader) {
        text += chunk;
        row.raw = text;
        this.emit();
      }
      row.en = text;
      if (isFinal) row.rawLive = false;
      this.emit();
      return;
    }

    // agent (Hindi) transcript: streams in sync with the audio
    let row = this.rows.find((r) => r.hiSegId === segId) ||
              this.rows.find((r) => r.state === "translating" && !r.hiSegId) ||
              [...this.rows].reverse().find((r) => r.state !== "dropped" && r.state !== "failed");
    if (!row) row = this.newRow();
    row.hiSegId = segId;
    this.clipRowId = row.id;
    let text = "";
    for await (const chunk of reader) {
      text += chunk;
      row.hi = text;
      this.emit();
    }
    row.state = "done";
    if (text.trim()) this.hasTranscript = true; // first translation: offer the transcript
    this.emit();
  };

  // ------------------------------------------------------ pipeline events ---
  onEvent = (ev) => {
    this.held = ev.type === "held"; // any other event ends the "waiting for the rest" state
    if (ev.type === "held") {
      const row = this.unanswered().at(-1);
      if (row) { row.state = "held"; this.chip(row, "en", "⏳ waiting for the rest of the sentence", "warn"); }
    } else if (ev.type === "dropped") {
      this.noteCall();
      const row = this.unanswered().at(-1);
      const fillerOnly = ev.reason === "only fillers" && ev.fillers?.length;
      const why = fillerOnly ? `only fillers: ${ev.fillers.join(", ")}` : ev.reason || "noise / no speech";
      if (row) {
        row.state = "dropped";
        row.rawLive = false;
        if (ev.raw && !row.raw.trim()) row.raw = ev.raw;
        this.chip(row, "en", why, "grey");
      }
    } else if (ev.type === "turn") {
      this.noteCall();
      // merge any held fragments into one row: that's the sentence we translate
      // (a row still "translating" with no Hindi yet was announced by a preemptive run
      //  that got replaced: reuse it instead of leaving a stuck row)
      const pending = [...this.unanswered(), ...this.rows.filter((r) => r.state === "translating" && !r.hi)];
      const row = pending[0] || this.newRow();
      const merged = new Set(pending.slice(1));
      this.rows = this.rows.filter((r) => !merged.has(r));
      row.state = "translating";
      row.rawLive = false;
      row.raw = ev.raw;
      row.clean = ev.cleaned;
      row.en = ev.cleaned;
      row.same = norm(ev.raw) === norm(ev.cleaned); // show the raw line only if cleaning changed it
      row.enChips = [];
      if (ev.forced) this.chip(row, "en", "speaker paused mid-sentence: translated anyway", "grey");
      if (ev.tone) this.chip(row, "en", ev.tone === "casual" ? "casual · तुम" : "formal · आप"); // only when forced
      if (ev.model) this.activeModel = ev.model;
      if (ev.fallback && ev.model && this.model !== "auto") this.chip(row, "en", `via ${modelName(ev.model)} (first choice busy)`, "grey");
      ev.asr_fixes?.forEach((f) => this.chip(row, "en", `heard: ${f}`));
      ev.fillers?.forEach((f) => this.chip(row, "en", `removed “${f}”`, "grey"));
      ev.entities?.forEach((e) => this.chip(row, "en", `kept: ${e}`));
      ev.hints?.forEach((h) => this.chip(row, "en", h));
    } else if (ev.type === "model_status") {
      this.showNotice(ev.available ? NOTICE.back(ev.model) : NOTICE.busy(ev.model));
    } else if (ev.type === "metrics") {
      const row = [...this.rows].reverse().find((r) => r.hiSegId && !r.metrics) || this.rows.at(-1);
      if (!row) return;
      row.metrics = ev;
      if (ev.interrupted) this.chip(row, "hi", "✋ interrupted by speaker", "warn");
    }
    this.refreshOrb(true);
  };

  // ------------------------------------------------------------ audio ---
  onAgentAudio(track) {
    const el = track.attach();
    el.autoplay = true;
    el.muted = !this.voiceOn;
    this.audioEls.push(el);
    document.body.appendChild(el);
    if (!this.recorder) this.recorder = new HindiRecorder(this.ctx, new MediaStream([track.mediaStreamTrack]));
  }

  onAgentState(state) {
    const prev = this.agentState;
    this.agentState = state;
    if (state === "thinking") {
      // the sentence(s) the agent is now translating
      this.thinkingRowIds = this.rows
        .filter((r) => ["listening", "held", "translating"].includes(r.state) && (r.raw || r.en).trim() && !r.hi)
        .map((r) => r.id);
    } else if (prev === "thinking" && state === "listening") {
      // thought, but never spoke: either a skip (a "dropped" event is on its way) or a failed
      // LLM call, e.g. the free plan's rate limit. Give the skip event a moment to arrive.
      const ids = this.thinkingRowIds;
      this.thinkingRowIds = [];
      setTimeout(() => { this.checkUnanswered(ids); this.emit(); }, PACE.failGraceMs);
    }
    if (this.recorder) {
      if (state === "speaking") this.recorder.start();
      else {
        const rowId = this.clipRowId;
        this.recorder.stop((clip) => {
          const row = this.rows.find((r) => r.id === rowId);
          if (!row) return;
          row.clip = clip;
          this.hasAudio = true;
          this.emit();
        });
      }
    }
    this.refreshOrb(true);
  }

  // ------------------------------------------------------------ mic state ---
  computeOrb() {
    if (!this.room) return this.connecting ? "connecting" : "idle";
    if (!this.agentIdentity) return this.connecting ? "connecting" : "waiting";
    if (this.agentState === "speaking") return "speaking";
    if (this.agentState === "thinking") return "thinking";
    // hold "hearing" ~0.5 s after the voice drops, so it doesn't flicker between words
    if (this.micLevel > 0.06) this.lastVoiceAt = performance.now();
    const talking = this.inputMode === "ptt" ? this.pttDown : performance.now() - this.lastVoiceAt < 500;
    if (talking) return "hearing";
    if (this.inputMode === "ptt") return "ptt";
    return this.held ? "held" : "listening";
  }

  /** Re-derive the mic state; emit only when it changed (or when forced). */
  refreshOrb(force = false) {
    const s = this.computeOrb();
    if (force || s !== this.orbState) { this.orbState = s; this.emit(); }
  }

  // ------------------------------------------------------------- session ---
  async rpc(method, payload = "") {
    if (!this.room || !this.agentIdentity) return undefined;
    try {
      return await this.room.localParticipant.performRpc({ destinationIdentity: this.agentIdentity, method, payload });
    } catch (e) {
      console.warn("RPC failed", method, e);
      return undefined;
    }
  }

  toggleSession = () => (this.room ? this.room.disconnect() : this.connect());

  connect = async () => {
    if (this.room || this.connecting) return;
    this.connecting = true;
    this.note = "";
    this.refreshOrb(true);
    try {
      this.ctx ??= new (window.AudioContext || window.webkitAudioContext)();
      await this.ctx.resume();
      const code = new URLSearchParams(location.search).get("code") || "";
      const res = await fetch(`/api/token?code=${encodeURIComponent(code)}`);
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const { url, token } = await res.json();

      const room = new Room({ adaptiveStream: true, dynacast: true });
      this.room = room;
      room.registerTextStreamHandler("lk.transcription", this.onTranscription);
      room
        .on(RoomEvent.DataReceived, (payload, _p, _k, topic) => {
          if (topic === "translator.events") this.onEvent(JSON.parse(new TextDecoder().decode(payload)));
        })
        .on(RoomEvent.TrackSubscribed, (track, _pub, p) => {
          if (track.kind === "audio" && p.isAgent) this.onAgentAudio(track);
        })
        .on(RoomEvent.ParticipantConnected, (p) => { if (p.isAgent) this.agentReady(p); })
        .on(RoomEvent.ParticipantAttributesChanged, (attrs, p) => {
          if (p.isAgent && attrs["lk.agent.state"]) this.onAgentState(attrs["lk.agent.state"]);
        })
        .on(RoomEvent.Disconnected, () => {
          this.room = null; this.agentIdentity = null; this.agentState = null;
          this.held = false; this.micLevel = 0; this.pttDown = false;
          this.stopMeter?.(); this.stopMeter = null;
          if (this.recorder?.recording) this.recorder.finish();
          this.refreshOrb(true);
        });

      await room.connect(url, token);
      await room.localParticipant.setMicrophoneEnabled(true, {
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      });
      await room.startAudio();
      const mic = room.localParticipant.getTrackPublication(Track.Source.Microphone)?.track;
      if (mic) {
        this.stopMeter = micMeter(this.ctx, new MediaStream([mic.mediaStreamTrack]), (peak) => {
          this.micLevel = this.room ? peak : 0;
          this.levelListeners.forEach((fn) => fn(this.micLevel));
          this.refreshOrb();
        });
      }
      room.remoteParticipants.forEach((p) => p.isAgent && this.agentReady(p));
    } catch (e) {
      console.error(e);
      this.note = `Could not connect: ${e.message}`;
      this.room = null;
    } finally {
      this.connecting = false;
      this.refreshOrb(true);
    }
  };

  async agentReady(p) {
    this.agentIdentity = p.identity;
    if (p.attributes?.["lk.agent.state"]) this.onAgentState(p.attributes["lk.agent.state"]);
    this.refreshOrb(true);
    // push the current UI settings to the agent
    await this.rpc("set_tone", this.tone);
    await this.rpc("set_model", this.model);
    if (this.inputMode !== "auto") await this.rpc("set_input_mode", this.inputMode);
  }

  // ------------------------------------------------------------ controls ---
  setVoice = (on) => {
    this.voiceOn = on;
    this.audioEls.forEach((el) => { el.muted = !on; });
    this.emit();
  };

  setTone = (tone) => { this.tone = tone; this.emit(); this.rpc("set_tone", tone); };

  setModel = async (model) => {
    this.model = model;
    this.emit();
    const applied = await this.rpc("set_model", model);
    // the agent answers with the model it uses (e.g. "auto" if this one isn't configured)
    if (applied && applied !== this.model) { this.model = applied; this.emit(); }
  };

  setInputMode = (mode) => { this.inputMode = mode; this.refreshOrb(true); this.rpc("set_input_mode", mode); };

  pttStart = async () => {
    if (this.pttDown || this.inputMode !== "ptt" || !this.agentIdentity) return;
    this.pttDown = true;
    this.refreshOrb(true);
    await this.rpc("ptt_start");
  };

  pttEnd = async () => {
    if (!this.pttDown) return;
    this.pttDown = false;
    this.refreshOrb(true);
    await this.rpc("ptt_end");
  };

  downloadAudio = () => { if (this.recorder) download(this.recorder.sessionWav(), "hindi-session.wav"); };

  downloadTranscript = () => {
    const lines = this.rows.filter((r) => r.state !== "dropped" && r.hi).map((r, i) =>
      `${i + 1}. EN: ${r.en}\n   HI: ${r.hi}${r.metrics?.e2e ? `\n   latency: ${r.metrics.e2e.toFixed(2)}s` : ""}`);
    download(new Blob([lines.join("\n\n")], { type: "text/plain;charset=utf-8" }), "transcript.txt");
  };
}
