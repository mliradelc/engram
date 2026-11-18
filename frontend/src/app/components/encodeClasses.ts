import type { EncodeClass } from "../../types";

// Plain (non-component) helpers for the encode panel, kept out of the .tsx
// component files so React fast refresh keeps working.

/**
 * The classes an operator may force, in the order the backend lists them.
 * `hdr` and `interlaced` are deliberately absent: both are properties of the
 * source (the backend refuses them with 422, and deinterlacing is applied from
 * the source whatever class is chosen).
 */
export const ENCODE_CLASSES: readonly EncodeClass[] = [
  "passthrough",
  "animation",
  "live_action_grainy",
  "live_action_clean",
];

/** The `<select>` value standing for Auto (the classifier decides). */
export const AUTO_CLASS = "";

export function isEncodeClass(value: unknown): value is EncodeClass {
  return typeof value === "string" && (ENCODE_CLASSES as readonly string[]).includes(value);
}

/** Seconds -> "1h 05m" / "4m 10s" / "12s"; null -> an em dash (ETA unknown). */
export function formatEta(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = s % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(rest).padStart(2, "0")}s`;
  return `${rest}s`;
}
