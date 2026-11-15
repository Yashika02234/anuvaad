import { useEffect, useRef } from "react";
import { engine } from "../useInterpreter";
import ModelMenu from "./ModelMenu.jsx";
import ToneMenu from "./ToneMenu.jsx";

const MODES = [
  { id: "auto", label: "Hands-free", title: "Just talk; a short pause ends each sentence. Tap the mic again to end" },
  { id: "ptt", label: "Hold to talk", title: "Hold the mic button (or Space) while you speak" },
];

/** Notice · mic (with halo) · status · controls (talk mode, tone, model). */
export default function MicDock({ orb, connected, inputMode, notice, model, activeModel, tone }) {
  // Hands-free: tap to start, tap again to end. Hold to talk: end from the header.
  const canEnd = connected && inputMode !== "ptt";
  const halo = useRef(null);
  const orbBtn = useRef(null);

  // voice level drives the halo ring directly (no React re-render)
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
      </div>
      <div id="orb-label" className={orb.error ? "orb-label err" : "orb-label"} hidden={orb.state === "idle" && !orb.error}>
        <span className="bars" aria-hidden="true"><i /><i /><i /><i /></span>
        <span id="orb-text">{orb.label}</span>
      </div>
      <div className="dock-controls">
        <div className="seg" role="group" aria-label="How to talk">
          {MODES.map((x) => (
            <button key={x.id} className={inputMode === x.id ? "on" : ""} title={x.title}
                    onClick={() => engine.setInputMode(x.id)}>{x.label}</button>
          ))}
        </div>
        <ToneMenu value={tone} onChange={engine.setTone} className="tone up" />
        <ModelMenu value={model} onChange={engine.setModel} active={activeModel} />
      </div>
    </div>
  );
}
