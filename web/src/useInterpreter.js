import { useSyncExternalStore } from "react";
import { Interpreter } from "./lib/interpreter";

// One engine for the whole page (it owns the LiveKit room and audio graph).
export const engine = new Interpreter();
if (typeof window !== "undefined") window.bolo = engine; // handy for debugging / automated checks

/** React view of the engine's state: re-renders whenever the engine emits. */
export function useInterpreter() {
  return useSyncExternalStore(engine.subscribe, engine.getSnapshot);
}
