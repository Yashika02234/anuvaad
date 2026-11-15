import Header from "./components/Header.jsx";
import EmptyState from "./components/EmptyState.jsx";
import Conversation from "./components/Conversation.jsx";
import MicDock from "./components/MicDock.jsx";
import InfoNote from "./components/InfoNote.jsx";
import { useInterpreter } from "./useInterpreter";

export default function App() {
  const s = useInterpreter();
  return (
    <>
      <Header connected={s.connected} inputMode={s.inputMode} hasTranscript={s.hasTranscript} voiceOn={s.voiceOn} />
      <div className="layout">
        <main>
          <div className="rows">
            {s.rows.length ? (
              <>
                <Conversation rows={s.rows} />
                <InfoNote>
                  Seeing some latency? We switch models when a rate limit is reached.
                  Please be patient, or try switching models.
                </InfoNote>
              </>
            ) : <EmptyState />}
          </div>
        </main>
      </div>
      <MicDock orb={s.orb} connected={s.connected} inputMode={s.inputMode} notice={s.notice}
               model={s.model} activeModel={s.activeModel} tone={s.tone} />
    </>
  );
}
