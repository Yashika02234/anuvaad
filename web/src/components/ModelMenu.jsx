import ToneMenu from "./ToneMenu.jsx";

// Must match the names in translator/config.py ("auto" = the agent's own fallback order).
const SHORT = { "gpt-oss-120b": "gpt-oss 120B", qwen: "Qwen", "gpt-oss-20b": "gpt-oss 20B", gemini: "Gemini" };
const MODELS = [
  { v: "auto", label: "Auto", hint: "Switches to whichever model is free right now" },
  { v: "gpt-oss-120b", label: "gpt-oss 120B", button: "gpt-oss 120B", hint: "Recommended · Groq, free rate limit" },
  { v: "qwen", label: "Qwen 3.8 · fastest", button: "Qwen · fastest", hint: "Groq, free rate limit" },
  { v: "gpt-oss-20b", label: "gpt-oss 20B", button: "gpt-oss 20B", hint: "Lighter · Groq, free rate limit" },
  { v: "gemini", label: "Gemini Flash", button: "Gemini Flash", hint: "Google, lower free limits" },
];

/** Translation model. Auto shows the model answering right now; a pinned model is tried
 *  first and the others take over when it's busy. */
export default function ModelMenu({ value, onChange, active }) {
  const options = MODELS.map((m) => (m.v === "auto"
    ? { ...m, button: active ? `Auto · ${SHORT[active] || active}` : "Model · Auto" }
    : m));
  return (
    <ToneMenu value={value} onChange={onChange} options={options} id="model" label="Translation model"
              className="tone up" title="Translation model. If it's busy, the others take over in order" />
  );
}
