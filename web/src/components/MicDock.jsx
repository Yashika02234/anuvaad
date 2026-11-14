import { useEffect, useRef } from "react";
import { engine } from "../useInterpreter";

/** Transcript download · mic (with halo) · Hindi audio download, and the status below. */
export default function MicDock({ orb, inputMode, hasTranscript, hasAudio }) {
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
  const onClick = () => { if (!engine.room && !engine.connecting) engine.connect(); };

  return (
    <div className="orb-dock" data-state={orb.state}>
      <div className="dock-row">
        <button id="dl-text" className="icon" hidden={!hasTranscript} title="Download transcript (English → Hindi)"
                aria-label="Download transcript" onClick={engine.downloadTranscript}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" /><path d="M14 3v6h6" /><path d="M8 13h8M8 17h5" /></svg>
        </button>
        <div className="halo" ref={halo}>
          <button id="orb" ref={orbBtn} className="orb" data-state={orb.state} aria-live="polite" aria-label={orb.aria}
                  onClick={onClick} onPointerDown={onPointerDown} onPointerUp={engine.pttEnd} onPointerCancel={engine.pttEnd}>
            <svg className="ic-mic" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>
            <svg className="ic-spk" viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5z" /><path d="M15.5 8.5a5 5 0 0 1 0 7" /><path d="M18.5 5.5a9 9 0 0 1 0 13" /></svg>
          </button>
        </div>
        <button id="dl-audio" className="icon" hidden={!hasAudio} title="Download Hindi audio (WAV)"
                aria-label="Download Hindi audio" onClick={engine.downloadAudio}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5z" /><path d="M15.5 8.5a5 5 0 0 1 0 7" /><path d="M18.5 5.5a9 9 0 0 1 0 13" /></svg>
        </button>
      </div>
      <div id="orb-label" className={orb.error ? "orb-label err" : "orb-label"}>
        <span className="bars" aria-hidden="true"><i /><i /><i /><i /></span>
        <span id="orb-text">{orb.label}</span>
      </div>
    </div>
  );
}
