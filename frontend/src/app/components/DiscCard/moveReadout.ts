import { formatEtaPrecise, formatGigabytes } from "../../../utils/formatting";
import type { MoveProgress } from "../../../types";

/**
 * `14.7 GB / 39.5 GB · 42.1 MB/s · 15m 12s left`. Speed and ETA are left off
 * while unknown (the first sample of a title carries neither).
 */
export function moveReadout(move: MoveProgress): string {
  const parts = [`${formatGigabytes(move.current_bytes)} / ${formatGigabytes(move.total_bytes)}`];
  if (move.speed) parts.push(move.speed);
  if (move.eta !== null && move.eta !== undefined) parts.push(`${formatEtaPrecise(move.eta)} left`);
  return parts.join(" · ");
}

/** `✓ VERIFIED — 39.5 GB transferred`. */
export function verifiedReadout(move: MoveProgress): string {
  return `✓ VERIFIED — ${formatGigabytes(move.total_bytes)} transferred`;
}
