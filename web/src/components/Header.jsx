import ToneMenu from "./ToneMenu.jsx";
import { engine } from "../useInterpreter";

const MODES = [
  { id: "auto", label: "Hands-free", title: "After you start a session: just talk, a short pause ends each sentence" },
  { id: "ptt", label: "Hold to talk", title: "After you start a session: hold the mic button (or Space) while you speak" },
];

export default function Header({ connected, connecting, inputMode, tone }) {
  return (
    <header className="top">
      <div className="brand">Bolo</div>
      <div className="controls">
        <div className="seg" role="group" aria-label="How to talk once the session starts">
          {MODES.map((m) => (
            <button key={m.id} className={inputMode === m.id ? "on" : ""} title={m.title}
                    onClick={() => engine.setInputMode(m.id)}>
              {m.label}
            </button>
          ))}
        </div>
        <ToneMenu value={tone} onChange={engine.setTone} />
        <button id="connect" className={connected ? "end" : "primary"} disabled={connecting} onClick={engine.toggleSession}>
          {connected ? "End session" : "Start session"}
        </button>
      </div>
    </header>
  );
}
