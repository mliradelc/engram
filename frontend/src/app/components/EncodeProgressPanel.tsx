import { useState } from "react";
import { IcoError, IcoIdle, IcoRetry, IcoRipping } from "./icons";
import { sv, SvPanel } from "./synapse";
import type { EncodeFailure, EncodeProgress } from "../../types";

interface EncodeProgressPanelProps {
  /** Undefined renders the empty state (e.g. a test double of the hook). */
  encodes?: Map<string, EncodeProgress> | EncodeProgress[];
  /** Recently-failed encodes (newest first); each gets a Retry button. */
  failures?: EncodeFailure[];
  /** POSTs /api/encoding/retry. Without it, failed rows show no Retry button. */
  onRetry?: (failure: EncodeFailure) => void | Promise<void>;
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

/** Epoch seconds -> "14:05" today, else "Nov 3 14:05". */
function formatFailedAt(epochSeconds: number): string {
  if (!Number.isFinite(epochSeconds)) return "";
  const d = new Date(epochSeconds * 1000);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return time;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

const microLabel = {
  fontFamily: sv.mono,
  fontSize: 10,
  letterSpacing: "0.16em",
  textTransform: "uppercase" as const,
};

const ellipsis = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap" as const,
};

/**
 * Post-organize AV1/x265 encodes, as a dashboard card. Sits above the job
 * list and is always rendered, idle included: hiding it when idle would make
 * the feature undiscoverable.
 *
 * Active rows (per file: name, progress bar with percent, ETA, destination
 * folder) are seeded from GET /api/encoding and kept live by `encode_progress`
 * messages (see useJobManagement). A queued file is not shown: the backend
 * lists a file only once it holds an encode slot. Failed rows come from the
 * backend's ring buffer of recent failures and offer Retry.
 */
export function EncodeProgressPanel({ encodes, failures, onRetry }: EncodeProgressPanelProps) {
  const source = encodes ?? [];
  const rows = (Array.isArray(source) ? source : Array.from(source.values()))
    .slice()
    .sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0) || a.file.localeCompare(b.file));
  const failed = failures ?? [];
  const [retrying, setRetrying] = useState<ReadonlySet<string>>(() => new Set());

  const active = rows.length > 0;
  const status = active ? `${rows.length} Active` : failed.length > 0 ? `${failed.length} Failed` : "Idle";
  const statusColor = active ? sv.cyan : failed.length > 0 ? sv.magenta : sv.inkFaint;

  const retry = async (failure: EncodeFailure) => {
    if (!onRetry || retrying.has(failure.source)) return;
    setRetrying((prev) => new Set(prev).add(failure.source));
    try {
      await onRetry(failure);
    } finally {
      setRetrying((prev) => {
        const next = new Set(prev);
        next.delete(failure.source);
        return next;
      });
    }
  };

  return (
    <section data-testid="encode-progress-panel" aria-label="Encoding">
      <SvPanel
        glow={active}
        pad={20}
        accent={active ? `${sv.cyan}55` : sv.lineMid}
        testid="encode-progress-card"
        style={{ background: `linear-gradient(180deg, ${sv.bg2}cc, ${sv.bg1}ee)`, color: sv.ink }}
      >
        {/* Header row: icon + title + subtitle, status chip on the right. */}
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
            {active ? (
              <IcoRipping size={18} color={sv.cyan} />
            ) : (
              <IcoIdle size={18} color={sv.inkDim} />
            )}
            <div style={{ minWidth: 0 }}>
              <h2
                style={{
                  fontFamily: sv.display,
                  fontSize: 15,
                  fontWeight: 700,
                  letterSpacing: "0.04em",
                  color: sv.ink,
                  margin: 0,
                }}
              >
                Encoding
              </h2>
              <div style={{ ...microLabel, color: sv.inkFaint, marginTop: 2 }}>
                Post-organize AV1 / x265 re-encode
              </div>
            </div>
          </div>
          <span
            data-testid="encode-progress-status"
            style={{
              ...microLabel,
              fontWeight: 700,
              color: statusColor,
              border: `1px solid ${statusColor}4d`,
              padding: "3px 8px",
              whiteSpace: "nowrap",
            }}
          >
            {status}
          </span>
        </div>

        <div style={{ marginTop: 16 }}>
          {active ? (
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 14 }}>
              {rows.map((row) => {
                const pct = Math.min(100, Math.max(0, row.percent ?? 0));
                return (
                  <li key={row.file} data-testid="encode-progress-row" style={{ fontFamily: sv.mono }}>
                    <div
                      title={row.file}
                      style={{ ...ellipsis, color: sv.cyanHi, fontSize: 13, fontWeight: 600 }}
                    >
                      {row.file}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 6 }}>
                      <div
                        role="progressbar"
                        aria-label={`Encoding ${row.file}`}
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={Math.round(pct)}
                        style={{ flex: 1, height: 6, background: sv.bg3, overflow: "hidden" }}
                      >
                        <div
                          style={{
                            width: `${pct}%`,
                            height: "100%",
                            background: `linear-gradient(90deg, ${sv.cyanDim}, ${sv.cyan})`,
                            boxShadow: `0 0 8px ${sv.cyan}66`,
                          }}
                        />
                      </div>
                      <span
                        data-testid="encode-progress-percent"
                        style={{ minWidth: 52, textAlign: "right", fontSize: 13, color: sv.ink }}
                      >
                        {pct.toFixed(1)}%
                      </span>
                    </div>
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        gap: 12,
                        marginTop: 4,
                        color: sv.inkDim,
                        fontSize: 11,
                      }}
                    >
                      <span
                        data-testid="encode-progress-destination"
                        title={row.destination_folder}
                        style={ellipsis}
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
          ) : (
            <div
              data-testid="encode-progress-empty"
              style={{
                border: `1px dashed ${sv.lineMid}`,
                padding: "14px 16px",
                fontFamily: sv.mono,
              }}
            >
              <div style={{ ...microLabel, fontSize: 11, fontWeight: 700, color: sv.inkDim }}>
                Nothing encoding
              </div>
              <p style={{ margin: "6px 0 0", fontSize: 11, lineHeight: 1.5, color: sv.inkFaint }}>
                Filed titles are re-encoded here after organizing. Progress and ETA show up as
                soon as an encode starts.
              </p>
            </div>
          )}
        </div>

        {failed.length > 0 && (
          <div style={{ marginTop: 18 }}>
            <div
              style={{
                ...microLabel,
                color: sv.magentaDim,
                borderTop: `1px solid ${sv.line}`,
                paddingTop: 12,
                marginBottom: 10,
              }}
            >
              Failed recently
            </div>
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 10 }}>
              {failed.map((f) => {
                const busy = retrying.has(f.source);
                return (
                  <li
                    key={f.source}
                    data-testid="encode-failed-row"
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 12,
                      fontFamily: sv.mono,
                      borderLeft: `2px solid ${sv.magenta}99`,
                      paddingLeft: 10,
                    }}
                  >
                    <IcoError size={14} color={sv.magenta} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div title={f.source} style={{ ...ellipsis, fontSize: 13, color: sv.ink }}>
                        {f.file}
                      </div>
                      <div
                        data-testid="encode-failed-reason"
                        title={f.reason}
                        style={{ ...ellipsis, fontSize: 11, color: sv.magentaHi, marginTop: 2 }}
                      >
                        {f.reason}
                      </div>
                      <div style={{ ...ellipsis, fontSize: 10, color: sv.inkFaint, marginTop: 2 }}>
                        {formatFailedAt(f.failed_at)} · → {f.destination_folder}
                      </div>
                    </div>
                    {onRetry && (
                      <button
                        type="button"
                        onClick={() => void retry(f)}
                        disabled={busy}
                        aria-label={`Retry encoding ${f.file}`}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 6,
                          flexShrink: 0,
                          background: "transparent",
                          border: `1px solid ${sv.lineMid}`,
                          color: busy ? sv.inkFaint : sv.cyan,
                          ...microLabel,
                          fontWeight: 700,
                          padding: "6px 10px",
                          cursor: busy ? "wait" : "pointer",
                        }}
                        onMouseEnter={(e) => {
                          if (busy) return;
                          e.currentTarget.style.color = sv.cyanHi;
                          e.currentTarget.style.borderColor = sv.lineHi;
                        }}
                        onMouseLeave={(e) => {
                          e.currentTarget.style.color = busy ? sv.inkFaint : sv.cyan;
                          e.currentTarget.style.borderColor = sv.lineMid;
                        }}
                      >
                        <IcoRetry size={12} />
                        Retry
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </SvPanel>
    </section>
  );
}
