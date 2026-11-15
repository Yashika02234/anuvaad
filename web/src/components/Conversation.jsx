import { useEffect } from "react";
import PlayButton from "./PlayButton.jsx";

const cx = (...c) => c.filter(Boolean).join(" ");
const ms = (v) => (v == null ? "–" : `${Math.round(v * 1000)} ms`);

function Latency({ m }) {
  if (m.e2e == null) return null; // very short replies aren't timed: show nothing rather than "–"
  return (
    <span className="lat" title={`ASR ${ms(m.stt)} · Translate ${ms(m.llm_ttft)} · TTS ${ms(m.tts_ttfb)}`}>
      <b className={m.e2e > 2.5 ? "slow" : ""}>{m.e2e.toFixed(2)}s</b> latency
    </span>
  );
}

function Chips({ list, side }) {
  return (
    <div className={`chips ${side}-chips`}>
      {list.map((c, i) => <span key={i} className={cx("chip", c.cls)}>{c.text}</span>)}
    </div>
  );
}

function Row({ row }) {
  return (
    <div className={cx("row", row.state === "dropped" && "dropped", row.state === "failed" && "failed", row.same && "same")}>
      <div className="en">
        <div className={cx("raw", row.rawLive && "live")}>{row.raw}</div>
        <div className="clean">{row.clean}</div>
      </div>
      <div className="hi"><div className="text" lang="hi">{row.hi}</div></div>
      <div className="meta">
        <div className="hi-foot">
          {row.clip && <PlayButton url={row.clip.url} seconds={row.clip.seconds} />}
          {row.metrics && <Latency m={row.metrics} />}
        </div>
        <Chips list={row.enChips} side="en" />
        <Chips list={row.hiChips} side="hi" />
      </div>
    </div>
  );
}

export default function Conversation({ rows }) {
  // keep the newest line in view: scroll to the page bottom, whose padding keeps it above the
  // mic (scrolling the row itself to the screen edge would put it behind the mic)
  useEffect(() => {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
  }, [rows.length]);
  return rows.map((r) => <Row key={r.id} row={r} />);
}
