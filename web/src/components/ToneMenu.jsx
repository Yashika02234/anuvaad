import { useEffect, useRef, useState } from "react";

const TONE_OPTIONS = [
  { v: "auto", label: "Auto", button: "Tone · Auto", hint: "Picks आप or तुम for you" },
  { v: "formal", label: "Formal · आप", button: "Formal · आप", hint: "Polite: work, clients, elders" },
  { v: "casual", label: "Casual · तुम", button: "Casual · तुम", hint: "Friendly: friends and peers" },
];

/** Styled replacement for a native <select> (whose popup can't be styled).
 *  Used for the tone and the model menus (same look, different options). */
export default function ToneMenu({ value, onChange, options = TONE_OPTIONS, id = "tone", title = "Hindi politeness level", label = "Tone", className = "tone" }) {
  const OPTIONS = options;
  const [open, setOpen] = useState(false);
  const wrap = useRef(null);
  const btn = useRef(null);
  const items = useRef([]);
  const current = OPTIONS.find((o) => o.v === value) || OPTIONS[0];

  // focus the selected option on open; close on any outside click
  useEffect(() => {
    if (!open) return undefined;
    items.current[OPTIONS.indexOf(current)]?.focus();
    const close = (e) => { if (!wrap.current?.contains(e.target)) setOpen(false); };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [open, current]);

  const choose = (o) => {
    if (o.v !== value) onChange(o.v);
    setOpen(false);
    btn.current?.focus();
  };

  const onKeyDown = (e) => {
    const i = items.current.indexOf(document.activeElement);
    const n = OPTIONS.length;
    if (e.key === "ArrowDown") { e.preventDefault(); items.current[(i + 1) % n]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items.current[(i - 1 + n) % n]?.focus(); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (i >= 0) choose(OPTIONS[i]); }
    else if (e.key === "Escape") { setOpen(false); btn.current?.focus(); }
  };

  return (
    <div className={className} ref={wrap}>
      <button ref={btn} type="button" id={`${id}-btn`} className="tone-btn" aria-haspopup="listbox"
              aria-expanded={open} title={title} onClick={() => setOpen((x) => !x)}>
        <span id={`${id}-label`}>{current.button}</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
      </button>
      <ul className="tone-list" id={`${id}-list`} role="listbox" aria-label={label} hidden={!open} onKeyDown={onKeyDown}>
        {OPTIONS.map((o, i) => (
          <li key={o.v} ref={(el) => { items.current[i] = el; }} role="option" tabIndex={-1}
              aria-selected={o.v === value} onClick={() => choose(o)}>
            <b>{o.label}</b>
            <small>{o.hint}</small>
          </li>
        ))}
      </ul>
    </div>
  );
}
