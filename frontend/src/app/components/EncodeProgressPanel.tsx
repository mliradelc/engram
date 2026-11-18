import { useEffect, useState } from "react";
import { IcoError, IcoIdle, IcoRetry, IcoRipping } from "./icons";
import { sv, SvPanel } from "./synapse";
import { EncodeClassBadge } from "./EncodeClassBadge";
import { EncodeClassSelect } from "./EncodeClassSelect";
import { formatEta, isEncodeClass } from "./encodeClasses";
import type { EncodeClass, EncodeFailure, EncodeOverrideStatus, EncodeProgress } from "../../types";

interface EncodeProgressPanelProps {
  /** Undefined renders the empty state (e.g. a test double of the hook). */
  encodes?: Map<string, EncodeProgress> | EncodeProgress[];
  /** Recently-failed encodes (newest first); each gets a Retry button. */
  failures?: EncodeFailure[];
  /**
   * POSTs /api/encoding/retry with the chosen class; called with the failure
   * alone for Auto. Without it, failed rows show no Retry button or selector.
   */
  onRetry?: (failure: EncodeFailure, codecClass?: EncodeClass) => void | Promise<void>;
  /** POSTs /api/encoding/dismiss. Without it, failed rows show no Dismiss button. */
  onDismiss?: (failure: EncodeFailure) => void | Promise<void>;
  /** GET /api/encoding/override?file= — preselects a row's stored pin. */
  loadOverride?: (source: string) => Promise<EncodeOverrideStatus | null>;
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

/** A filename gets two lines before it ellipsises: one line cut long show names mid-word. */
const twoLineClamp = {
  display: "-webkit-box",
  WebkitLineClamp: 2,
  WebkitBoxOrient: "vertical" as const,
  overflow: "hidden",
  wordBreak: "break-word" as const,
  minWidth: 0,
};

const actionButton = (busy: boolean, color: string) => ({
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  flexShrink: 0,
  background: "transparent",
  border: `1px solid ${sv.lineMid}`,
  color: busy ? sv.inkFaint : color,
  ...microLabel,
  fontWeight: 700,
  padding: "6px 10px",
  cursor: busy ? "wait" : "pointer",
});

interface FailedEncodeRowProps {
  failure: EncodeFailure;
  busy: boolean;
  onRetry?: (failure: EncodeFailure, codecClass?: EncodeClass) => void;
  onDismiss?: (failure: EncodeFailure) => void;
  loadOverride?: (source: string) => Promise<EncodeOverrideStatus | null>;
}

/**
 * One failed encode: what it ran as (badge), the class to retry with
 * (selector, preselected from the file's stored pin), Retry and Dismiss.
 */
function FailedEncodeRow({ failure: f, busy, onRetry, onDismiss, loadOverride }: FailedEncodeRowProps) {
  const [choice, setChoice] = useState<EncodeClass | undefined>(undefined);
  const [touched, setTouched] = useState(false);
  const [status, setStatus] = useState<EncodeOverrideStatus | null>(null);

  useEffect(() => {
    if (!loadOverride) return;
    let live = true;
    void loadOverride(f.source).then((s) => {
      if (live) setStatus(s);
    });
    return () => {
      live = false;
    };
  }, [loadOverride, f.source]);

  const pin = status?.override ?? null;
  const pinned = pin && !pin.stale && isEncodeClass(pin.codec_class) ? pin.codec_class : undefined;
  // The stored pin preselects until the operator picks something themselves.
  const selected = touched ? choice : pinned;
  const classifierOff = status?.classifier_enabled === false;
  const selectTitle = classifierOff
    ? "The codec classifier is off: only Auto (fixed settings) can run"
    : pinned
      ? `Pinned: ${pinned}. Auto clears the pin.`
      : pin?.stale
        ? "The stored pin is stale (the source changed) and is ignored"
        : "Auto lets the classifier decide";

  return (
    <li
      data-testid="encode-failed-row"
      style={{
        display: "flex",
        // Top-aligned so the icon and controls stay level with a two-line name.
        alignItems: "flex-start",
        flexWrap: "wrap",
        gap: 12,
        fontFamily: sv.mono,
        borderLeft: `2px solid ${sv.magenta}99`,
        paddingLeft: 10,
      }}
    >
      <IcoError size={14} color={sv.magenta} style={{ flexShrink: 0, marginTop: 2 }} />
      <div style={{ flex: "1 1 220px", minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 8, minWidth: 0 }}>
          <div
            data-testid="encode-failed-name"
            title={f.source}
            style={{ ...twoLineClamp, fontSize: 13, color: sv.ink }}
          >
            {f.file}
          </div>
          <EncodeClassBadge codecClass={f.codec_class} forced={f.forced} />
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
          {pin?.stale ? " · stale pin ignored" : ""}
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
        {onRetry && (
          <>
            <EncodeClassSelect
              label={`Codec class for ${f.file}`}
              title={selectTitle}
              value={classifierOff ? undefined : selected}
              disabled={busy || classifierOff}
              onChange={(value) => {
                setTouched(true);
                setChoice(value);
              }}
            />
            <button
              type="button"
              data-testid="encode-retry"
              onClick={() => onRetry(f, classifierOff ? undefined : selected)}
              disabled={busy}
              aria-label={`Retry encoding ${f.file}`}
              style={actionButton(busy, sv.cyan)}
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
          </>
        )}
        {onDismiss && (
          <button
            type="button"
            data-testid="encode-dismiss"
            onClick={() => onDismiss(f)}
            disabled={busy}
            aria-label={`Dismiss failed encode of ${f.file}`}
            style={actionButton(busy, sv.inkDim)}
            onMouseEnter={(e) => {
              if (busy) return;
              e.currentTarget.style.color = sv.ink;
              e.currentTarget.style.borderColor = sv.lineHi;
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.color = busy ? sv.inkFaint : sv.inkDim;
              e.currentTarget.style.borderColor = sv.lineMid;
            }}
          >
            Dismiss
          </button>
        )}
      </div>
    </li>
  );
}

/**
 * Post-organize AV1/x265 encodes, as a dashboard card. Sits above the job
 * list and is always rendered, idle included: hiding it when idle would make
 * the feature undiscoverable.
 *
 * Active rows (per file: name, progress bar with percent, ETA, destination
 * folder) are seeded from GET /api/encoding and kept live by `encode_progress`
 * messages (see useJobManagement). A queued file is not shown: the backend
 * lists a file only once it holds an encode slot. Failed rows come from the
 * backend's ring buffer of recent failures and offer Retry (with a codec
 * class: Auto or one of the four forceable ones) and Dismiss. Each row carries
 * the class it ran as and a FORCED chip when an operator chose it.
 */
export function EncodeProgressPanel({
  encodes,
  failures,
  onRetry,
  onDismiss,
  loadOverride,
}: EncodeProgressPanelProps) {
  const source = encodes ?? [];
  const rows = (Array.isArray(source) ? source : Array.from(source.values()))
    .slice()
    .sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0) || a.file.localeCompare(b.file));
  const failed = failures ?? [];
  const [retrying, setRetrying] = useState<ReadonlySet<string>>(() => new Set());

  const active = rows.length > 0;
  const status = active ? `${rows.length} Active` : failed.length > 0 ? `${failed.length} Failed` : "Idle";
  const statusColor = active ? sv.cyan : failed.length > 0 ? sv.magenta : sv.inkFaint;

  const retry = async (failure: EncodeFailure, codecClass?: EncodeClass) => {
    if (!onRetry || retrying.has(failure.source)) return;
    setRetrying((prev) => new Set(prev).add(failure.source));
    try {
      // Auto calls with the failure alone, so the hook omits codec_class.
      await (codecClass === undefined ? onRetry(failure) : onRetry(failure, codecClass));
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
                    <div style={{ display: "flex", alignItems: "flex-start", gap: 8, minWidth: 0 }}>
                      <div
                        data-testid="encode-progress-name"
                        title={row.file}
                        style={{ ...twoLineClamp, color: sv.cyanHi, fontSize: 13, fontWeight: 600 }}
                      >
                        {row.file}
                      </div>
                      <EncodeClassBadge codecClass={row.codec_class} forced={row.forced} />
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
              {failed.map((f) => (
                <FailedEncodeRow
                  key={f.source}
                  failure={f}
                  busy={retrying.has(f.source)}
                  onRetry={onRetry ? (failure, cls) => void retry(failure, cls) : undefined}
                  onDismiss={onDismiss ? (failure) => void onDismiss(failure) : undefined}
                  loadOverride={loadOverride}
                />
              ))}
            </ul>
          </div>
        )}
      </SvPanel>
    </section>
  );
}
