import Header from "./components/Header.jsx";
import EmptyState from "./components/EmptyState.jsx";
import Conversation from "./components/Conversation.jsx";
import MicDock from "./components/MicDock.jsx";
import { useInterpreter } from "./useInterpreter";

export default function App() {
  const s = useInterpreter();
  return (
    <>
      <Header connected={s.connected} connecting={s.connecting} inputMode={s.inputMode} tone={s.tone} />
      <div className="layout">
        <main>
          <div className="rows">
            {s.rows.length ? <Conversation rows={s.rows} /> : <EmptyState connected={s.connected} inputMode={s.inputMode} />}
          </div>
        </main>
      </div>
      <MicDock orb={s.orb} connected={s.connected} inputMode={s.inputMode} hasTranscript={s.hasTranscript} hasAudio={s.hasAudio}
               notice={s.notice} />
    </>
  );
}
