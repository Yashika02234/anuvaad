/** Small, quiet ⓘ note (model switching / latency). */
export default function InfoNote({ children }) {
  return (
    <p className="info-note">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></svg>
      <span>{children}</span>
    </p>
  );
}
