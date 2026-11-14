import { useEffect, useRef, useState } from "react";

let stopCurrent = null; // only one clip plays at a time

/** "Ready to play" replay button for one recorded Hindi line. */
export default function PlayButton({ url, seconds }) {
  const audio = useRef(null);
  const [on, setOn] = useState(false);

  useEffect(() => {
    const a = new Audio(url);
    a.onended = a.onpause = () => setOn(false);
    audio.current = a;
    return () => a.pause();
  }, [url]);

  const toggle = async () => {
    const a = audio.current;
    if (!a.paused) { a.pause(); return; }
    stopCurrent?.();
    a.currentTime = 0;
    try {
      await a.play();
      setOn(true);
      stopCurrent = () => a.pause();
    } catch (e) {
      console.warn("playback failed", e);
      setOn(false);
    }
  };

  return (
    <button className={on ? "play on" : "play"} title="Replay this Hindi line" onClick={toggle}>
      <span className="ic">{on ? "■" : "▶"}</span>
      {on ? "Stop" : "Play Hindi"} <small>{seconds.toFixed(1)}s</small>
    </button>
  );
}
