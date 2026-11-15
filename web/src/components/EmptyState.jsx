import { useRef, useState } from "react";
import InfoNote from "./InfoNote.jsx";

const TRIES = [
  ["Numbers & dates", "Can we move the meeting to 5 PM tomorrow?"],
  ["Names", "Please join the Google Meet, the Asterisk server is down."],
  ["Fillers", "Uh, so, you know, I'll send the report by 5:30."],
  ["Casual + abbreviation", "Hey dude, what's your ETA?"],
  ["Half sentence", <>Can we move the meeting to… <i>(pause)</i> Friday?</>],
  ["Interrupt", "Start talking while the Hindi is still playing."],
];

/** Start screen: the promise on the left, things to try on the right.
 *  On phones the examples become swipeable cards with dots (see styles.css). */
export default function EmptyState() {
  const track = useRef(null);
  const [active, setActive] = useState(0);
  const cards = () => [...(track.current?.children || [])];
  const onScroll = () => {
    const el = track.current;
    if (!el) return;
    const x = el.scrollLeft;
    const i = cards().reduce((best, c, k) => (Math.abs(c.offsetLeft - el.offsetLeft - x) <
      Math.abs(cards()[best].offsetLeft - el.offsetLeft - x) ? k : best), 0);
    setActive(i);
  };
  const goTo = (i) => {
    const el = track.current;
    const c = cards()[i];
    if (el && c) el.scrollTo({ left: c.offsetLeft - el.offsetLeft, behavior: "smooth" });
  };
  return (
    <section className="empty" id="empty">
      <div className="empty-main">
        <h2 className="hero">Speak English.<br /><span>Be heard in Hindi.</span></h2>
        <InfoNote>
          We quietly switch between models when a rate limit is reached. If you notice some
          latency, please be patient, or try switching models yourself.
        </InfoNote>
      </div>
      <div className="empty-tries">
        <div className="tries-title">Try saying</div>
        <div className="tries" ref={track} onScroll={onScroll}>
          {TRIES.map(([label, text]) => (
            <div className="try" key={label}><span>{label}</span>{text}</div>
          ))}
        </div>
        <div className="tries-dots" role="tablist" aria-label="Examples">
          {TRIES.map(([label], i) => (
            <button key={label} role="tab" aria-selected={i === active} aria-label={label}
                    className={i === active ? "on" : ""} onClick={() => goTo(i)} />
          ))}
        </div>
      </div>
    </section>
  );
}
