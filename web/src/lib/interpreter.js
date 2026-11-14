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

export class Interpreter {
  constructor() {
    this.room = null;
    this.agentIdentity = null;
    this.agentState = null;
    this.inputMode = "auto";
    this.tone = "auto";
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
      hasTranscript: this.hasTranscript,
      hasAudio: this.hasAudio,
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
              [...this.rows].reverse().find((r) => r.state !== "dropped");
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
      // merge any held fragments into one row: that's the sentence we translate
      const pending = this.unanswered();
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
      this.chip(row, "en", ev.tone === "casual" ? "casual · तुम" : "formal · आप");
      ev.asr_fixes?.forEach((f) => this.chip(row, "en", `heard: ${f}`));
      ev.fillers?.forEach((f) => this.chip(row, "en", `removed “${f}”`, "grey"));
      ev.entities?.forEach((e) => this.chip(row, "en", `kept: ${e}`));
      ev.hints?.forEach((h) => this.chip(row, "en", h));
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
    document.body.appendChild(el);
    if (!this.recorder) this.recorder = new HindiRecorder(this.ctx, new MediaStream([track.mediaStreamTrack]));
  }

  onAgentState(state) {
    this.agentState = state;
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
    if (this.inputMode !== "auto") await this.rpc("set_input_mode", this.inputMode);
  }

  // ------------------------------------------------------------ controls ---
  setTone = (tone) => { this.tone = tone; this.emit(); this.rpc("set_tone", tone); };

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
