import { useEffect, useRef } from "react";
import { engine } from "../useInterpreter";

/** Transcript download · mic (with halo) · Hindi audio download, and the status below. */
export default function MicDock({ orb, connected, inputMode, hasTranscript, hasAudio, notice }) {
  // Hands-free: the mic toggles the session (tap to start, tap again to end).
  // Hold to talk: pressing the mic means "talk", so that session is ended from the header.
  const canEnd = connected && inputMode !== "ptt";
  const halo = useRef(null);
  const orbBtn = useRef(null);

  // the voice level drives the halo ring directly (60 fps, so no React re-render)
  useEffect(() => engine.onLevel((lvl) => {
    halo.current?.style.setProperty("--lvl", Math.min(1, lvl * 2.5).toFixed(2));
  }), []);

  // Hold to talk with the Space bar (when nothing else has focus)
  useEffect(() => {
    const isTarget = (e) => e.target === document.body || e.target === orbBtn.current;
    const down = (e) => { if (e.code === "Space" && isTarget(e)) { e.preventDefault(); if (!e.repeat) engine.pttStart(); } };
    const up = (e) => { if (e.code === "Space" && isTarget(e)) { e.preventDefault(); engine.pttEnd(); } };
    document.addEventListener("keydown", down);
    document.addEventListener("keyup", up);
    return () => { document.removeEventListener("keydown", down); document.removeEventListener("keyup", up); };
  }, []);

  const onPointerDown = (e) => {
    if (engine.room && inputMode === "ptt") { e.currentTarget.setPointerCapture(e.pointerId); engine.pttStart(); }
  };
  const onClick = () => {
    if (engine.connecting) return;
    if (!engine.room) engine.connect();
    else if (canEnd) engine.toggleSession(); // second tap ends the session
  };

  return (
    <div className="orb-dock" data-state={orb.state}>
      {notice && (
        <div className={`notice ${notice.kind}`} role="status" aria-live="polite">
          {notice.kind === "warn" ? "⏳ " : ""}{notice.text}
        </div>
      )}
      <div className="dock-row">
        <button id="dl-text" className="icon" hidden={!hasTranscript} title="Download transcript (English → Hindi)"
                aria-label="Download transcript" onClick={engine.downloadTranscript}>
          {/* document with a download arrow */}
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" /><path d="M14 3v6h6" /><path d="M12 11v6M9.5 14.5 12 17l2.5-2.5" /></svg>
        </button>
        <div className="halo" ref={halo}>
          <button id="orb" ref={orbBtn} className={canEnd ? "orb can-end" : "orb"} data-state={orb.state}
                  aria-live="polite" aria-label={canEnd ? `${orb.aria}. Tap to end the session` : orb.aria}
                  title={canEnd ? "Tap to end the session" : undefined}
                  onClick={onClick} onPointerDown={onPointerDown} onPointerUp={engine.pttEnd} onPointerCancel={engine.pttEnd}>
            <svg className="ic-mic" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>
            <svg className="ic-spk" viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5z" /><path d="M15.5 8.5a5 5 0 0 1 0 7" /><path d="M18.5 5.5a9 9 0 0 1 0 13" /></svg>
            <svg className="ic-stop" viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" /></svg>
          </button>
        </div>
        <button id="dl-audio" className="icon" hidden={!hasAudio} title="Download Hindi audio (WAV)"
                aria-label="Download Hindi audio" onClick={engine.downloadAudio}>
          {/* sound waveform with a download arrow (not a speaker, which reads as "volume") */}
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 10v4M6 7v10M9 4v16M12 8v8" /><path d="M18 4v11M15 12l3 3 3-3M15 20h6" /></svg>
        </button>
      </div>
      <div id="orb-label" className={orb.error ? "orb-label err" : "orb-label"}>
        <span className="bars" aria-hidden="true"><i /><i /><i /><i /></span>
        <span id="orb-text">{orb.label}</span>
      </div>
      {connected && (
        <div className="orb-hint">
          {canEnd ? "Tap the mic to end the session" : "Use End session (top right) to finish"}
        </div>
      )}
    </div>
  );
}
