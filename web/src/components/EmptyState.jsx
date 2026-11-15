const TRIES = [
  ["Numbers & dates", "Can we move the meeting to 5 PM tomorrow?"],
  ["Names", "Please join the Google Meet, the Asterisk server is down."],
  ["Fillers", "Uh, so, you know, I'll send the report by 5:30."],
  ["Casual + abbreviation", "Hey dude, what's your ETA?"],
  ["Half sentence", <>Can we move the meeting to… <i>(pause)</i> Friday?</>],
  ["Interrupt", "Start talking while the Hindi is still playing."],
];

export default function EmptyState({ connected, inputMode }) {
  // the hint follows the session state
  const lead = !connected
    ? <>Tap the <b>mic</b> (or <b>Start session</b>), allow the microphone, and try one of these:</>
    : inputMode === "ptt"
      ? <><b>Hold the mic</b> (or Space) and say one of these in English:</>
      : <><b>Just speak</b>, say one of these in English, then pause. Tap the <b>mic</b> again to end:</>;

  return (
    <section className="empty" id="empty">
      <div className="badge"><i />Real-time voice · English → <span lang="hi">हिंदी</span></div>
      <h2 className="hero">Speak English.<br /><span>Be heard in Hindi.</span></h2>
      <p className="empty-lead">{lead}</p>
      <div className="tries">
        {TRIES.map(([label, text]) => (
          <div className="try" key={label}><span>{label}</span>{text}</div>
        ))}
      </div>
    </section>
  );
}
