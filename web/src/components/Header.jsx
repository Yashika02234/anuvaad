import { engine } from "../useInterpreter";

const WAVE = <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2" /></svg>;
const DOC = <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" /><path d="M14 3v6h6" /><path d="M8 13h8M8 17h5" /></svg>;

/** Brand on the left; once a session has started: Hindi voice on/off and the transcript. */
export default function Header({ connected, inputMode, hasTranscript, voiceOn }) {
  const started = connected || hasTranscript;
  return (
    <header className="top">
      {/* logo: a burgundy Devanagari अ + "nuvaad" (अनुवाद = translation) */}
      <div className="brand" aria-label="Anuvaad"><span className="brand-a" lang="hi" aria-hidden="true">अ</span><span aria-hidden="true">nuvaad</span></div>
      {started && (
        <div className="top-actions">
          <button className="pill" aria-pressed={voiceOn} onClick={() => engine.setVoice(!voiceOn)}
                  title={voiceOn ? "Mute the Hindi voice (keep reading the screen)" : "Turn the Hindi voice back on"}>
            {WAVE}Hindi voice · {voiceOn ? "On" : "Off"}
          </button>
          <button className="pill" disabled={!hasTranscript} onClick={engine.downloadTranscript}
                  title="Download the transcript (English → Hindi)">
            {DOC}Transcript
          </button>
          {connected && inputMode === "ptt" && (
            <button className="pill end" onClick={engine.toggleSession} title="End the session">End session</button>
          )}
        </div>
      )}
    </header>
  );
}
