// The mark of the console.
//
// An own drawing instead of a stock gamepad icon: a console over several servers is an overview —
// four tiles, one of them active. The mark says what the tool does.
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} fill="none" aria-hidden="true" focusable="false">
      {/* Housing with a chipped corner — the same language as the identity surfaces. */}
      <path
        d="M3 3h20l6 6v20H3z"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinejoin="round"
      />
      {/* Four tiles, the active one top left. */}
      <rect x="8" y="8" width="7" height="7" fill="currentColor" />
      <rect x="17.6" y="8" width="7" height="7" stroke="currentColor" strokeWidth={1.6} />
      <rect x="8" y="17.6" width="7" height="7" stroke="currentColor" strokeWidth={1.6} />
      <rect x="17.6" y="17.6" width="7" height="7" stroke="currentColor" strokeWidth={1.6} />
    </svg>
  );
}
