import InfoNote from "./InfoNote.jsx";

const TRIES = [
  ["Numbers & dates", "Can we move the meeting to 5 PM tomorrow?"],
  ["Names", "Please join the Google Meet, the Asterisk server is down."],
  ["Fillers", "Uh, so, you know, I'll send the report by 5:30."],
  ["Casual + abbreviation", "Hey dude, what's your ETA?"],
  ["Half sentence", <>Can we move the meeting to… <i>(pause)</i> Friday?</>],
  ["Interrupt", "Start talking while the Hindi is still playing."],
];

/** Start screen: the promise on the left, things to try on the right. */
export default function EmptyState() {
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
        <div className="tries">
          {TRIES.map(([label, text]) => (
            <div className="try" key={label}><span>{label}</span>{text}</div>
          ))}
        </div>
      </div>
    </section>
  );
}
