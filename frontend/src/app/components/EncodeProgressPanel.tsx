import { sv } from "./synapse";
import type { EncodeProgress } from "../../types";

interface EncodeProgressPanelProps {
  /** Undefined renders the empty state (e.g. a test double of the hook). */
  encodes?: Map<string, EncodeProgress> | EncodeProgress[];
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

/**
 * Files the post-organize AV1/x265 hook is encoding right now. Per file,
 * exactly four things: name, progress bar with percent, ETA, destination
 * folder. Seeded from GET /api/encoding and kept live by `encode_progress`
 * messages (see useJobManagement). A queued file is not shown: the backend
 * lists a file only once it holds an encode slot.
 */
export function EncodeProgressPanel({ encodes }: EncodeProgressPanelProps) {
  const source = encodes ?? [];
  const rows = (Array.isArray(source) ? source : Array.from(source.values()))
    .slice()
    .sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0) || a.file.localeCompare(b.file));

  return (
    <section
      data-testid="encode-progress-panel"
      aria-label="Encoding"
      style={{
        fontFamily: sv.mono,
        fontSize: 12,
        color: sv.ink,
        background: sv.bg1,
        border: `1px solid ${sv.inkGhost}`,
        padding: "10px 12px",
      }}
    >
      <div
        style={{
          fontSize: 10,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
          color: sv.inkDim,
          marginBottom: 8,
        }}
      >
        Encoding
      </div>
      {rows.length === 0 ? (
        <div data-testid="encode-progress-empty" style={{ color: sv.inkFaint }}>
          Nothing encoding
        </div>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
          {rows.map((row) => {
            const pct = Math.min(100, Math.max(0, row.percent ?? 0));
            return (
              <li key={row.file} data-testid="encode-progress-row">
                <div
                  title={row.file}
                  style={{
                    color: sv.cyanHi,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {row.file}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
                  <div
                    role="progressbar"
                    aria-label={`Encoding ${row.file}`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(pct)}
                    style={{ flex: 1, height: 4, background: sv.bg3, overflow: "hidden" }}
                  >
                    <div style={{ width: `${pct}%`, height: "100%", background: sv.cyan }} />
                  </div>
                  <span data-testid="encode-progress-percent" style={{ minWidth: 48, textAlign: "right" }}>
                    {pct.toFixed(1)}%
                  </span>
                </div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 12,
                    marginTop: 2,
                    color: sv.inkDim,
                    fontSize: 11,
                  }}
                >
                  <span
                    data-testid="encode-progress-destination"
                    title={row.destination_folder}
                    style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                  >
                    → {row.destination_folder}
                  </span>
                  <span data-testid="encode-progress-eta" style={{ flexShrink: 0 }}>
                    ETA {formatEta(row.eta)}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
