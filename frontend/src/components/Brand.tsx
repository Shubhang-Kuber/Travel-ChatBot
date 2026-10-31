import { PlaneTakeoff } from 'lucide-react';

export function Logo() {
  return (
    <span className="logo">
      <span className="logo-mark" aria-hidden="true">
        <PlaneTakeoff size={20} strokeWidth={2.2} />
      </span>
      <span className="logo-name">
        Travel<b>Bot</b>
      </span>
    </span>
  );
}

/** A plane drawn pointing right, so it can follow a path (CSS offset-rotate) or fly along a track. */
export function PlaneEast({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <path d="M23 12 3 3l5 9-5 9z" fill="currentColor" />
    </svg>
  );
}
