import { useMemo, useState, type CSSProperties } from "react";
import { IcoError, IcoIdle, IcoRetry, IcoRipping, IcoTv, IcoMovie, IcoLibrary } from "./icons";
import { sv, SvPanel } from "./synapse";
import { EncodeClassBadge } from "./EncodeClassBadge";
import { EncodeClassSelect } from "./EncodeClassSelect";
import { formatEta } from "./encodeClasses";
import {
  STATE_FILTERS,
  STATE_LABELS,
  bySeason,
  filterLibrary,
  formatEncodedAt,
  overlayLive,
  replacesExistingEncode,
  savedPercent,
  seasonLabel,
  splitForMiddleTruncation,
  type StateFilter,
} from "./encodeLibrary";
import { formatBytesCompact } from "../../utils/formatting";
import type {
  EncodeClass,
  EncodeLibrary,
  EncodeLibraryEntry,
  EncodeLibraryGroup,
  EncodeLibraryState,
  EncodeProgress,
} from "../../types";

export interface EncodeLibraryViewProps {
  /** null while the first load is in flight (or failed: see `error`). */
  library: EncodeLibrary | null;
  loading?: boolean;
  error?: string | null;
  /** The live `encode_progress` map useJobManagement keeps (no second poller). */
  encodes?: Map<string, EncodeProgress> | EncodeProgress[];
  /** Sources whose re-encode was accepted here and has not shown up live yet. */
  queued?: ReadonlySet<string>;
  /** POST /api/encoding/retry; `codecClass` undefined = Auto (the classifier). */
  onReencode?: (entry: EncodeLibraryEntry, codecClass?: EncodeClass) => void | Promise<void>;
  onRefresh?: () => void;
}

const microLabel: CSSProperties = {
  fontFamily: sv.mono,
  fontSize: 10,
  letterSpacing: "0.16em",
  textTransform: "uppercase",
};

const ellipsis: CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

type PillState = EncodeLibraryState | "queued";

/**
 * Colour per state. `stale` (amber, dashed) and `output_missing` (red, dashed)
 * are the two states where the marker no longer tells the truth, so they get
 * the loudest, most distinct treatment.
 */
const PILL: Record<PillState, { color: string; dashed?: boolean; title: string }> = {
  encoded: { color: sv.green, title: "Encoded; the output recorded in the marker exists" },
  passthrough: {
    color: sv.inkDim,
    title: "Deliberately left as filed (already efficient, or a short extra). Not a failure.",
  },
  stale: {
    color: sv.amber,
    dashed: true,
    title: "The source changed after this encode: the record no longer applies",
  },
  output_missing: {
    color: sv.red,
    dashed: true,
    title: "The recorded output no longer exists (pruned, or already imported by the curator)",
  },
  pending: { color: sv.purple, title: "A codec class is pinned but no encode is recorded yet" },
  encoding: { color: sv.cyan, title: "Encoding now" },
  failed: { color: sv.magenta, title: "The last encode failed" },
  queued: { color: sv.blue, title: "Re-encode requested; waiting for an encode slot" },
};

function StatePill({ state }: { state: PillState }) {
  const p = PILL[state];
  return (
    <span
      data-testid="encode-library-state"
      data-state={state}
      title={p.title}
      style={{
        ...microLabel,
        fontSize: 9,
        fontWeight: 700,
        letterSpacing: "0.12em",
        color: p.color,
        borderWidth: 1,
        borderStyle: p.dashed ? "dashed" : "solid",
        borderColor: `${p.color}${p.dashed ? "cc" : "66"}`,
        background: p.dashed ? `${p.color}14` : "transparent",
        padding: "1px 6px",
        whiteSpace: "nowrap",
      }}
    >
      {p.dashed ? "! " : ""}
      {STATE_LABELS[state]}
    </span>
  );
}

/** A path truncated in the middle; the full path is in `title` and in the DOM (copyable). */
function MiddlePath({ path, testid, style }: { path: string; testid?: string; style?: CSSProperties }) {
  const [head, tail] = splitForMiddleTruncation(path);
  return (
    <span
      data-testid={testid}
      title={path}
      style={{
        display: "flex",
        minWidth: 0,
        fontFamily: sv.mono,
        userSelect: "text",
        cursor: "text",
        ...style,
      }}
    >
      <span style={{ ...ellipsis, flexShrink: 1, minWidth: 0 }}>{head}</span>
      {tail && <span style={{ whiteSpace: "nowrap", flexShrink: 0 }}>{tail}</span>}
    </span>
  );
}

const actionButton = (disabled: boolean, color: string): CSSProperties => ({
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  flexShrink: 0,
  background: "transparent",
  border: `1px solid ${sv.lineMid}`,
  color: disabled ? sv.inkFaint : color,
  ...microLabel,
  fontWeight: 700,
  padding: "6px 10px",
  cursor: disabled ? "not-allowed" : "pointer",
});

interface RowProps {
  entry: EncodeLibraryEntry;
  queued: boolean;
  onReencode?: (entry: EncodeLibraryEntry, codecClass?: EncodeClass) => void | Promise<void>;
}

function EncodeLibraryRow({ entry: e, queued, onReencode }: RowProps) {
  const [choice, setChoice] = useState<EncodeClass | undefined>(undefined);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const state: PillState = queued && e.state !== "encoding" && e.state !== "failed" ? "queued" : e.state;
  const blocked = busy || state === "encoding" || state === "queued";
  const saved = savedPercent(e.source_bytes, e.output_bytes);
  const code =
    e.season !== null && e.episode !== null
      ? `S${String(e.season).padStart(2, "0")}E${String(e.episode).padStart(2, "0")}`
      : null;

  const fire = async () => {
    if (!onReencode) return;
    setConfirming(false);
    setBusy(true);
    try {
      await (choice === undefined ? onReencode(e) : onReencode(e, choice));
    } finally {
      setBusy(false);
    }
  };

  const borderColor =
    state === "stale" ? sv.amber : state === "output_missing" ? sv.red : state === "failed" ? sv.magenta : state === "encoding" ? sv.cyan : sv.line;

  return (
    <li
      data-testid="encode-library-row"
      data-source={e.source}
      style={{
        fontFamily: sv.mono,
        borderLeft: `2px solid ${borderColor}`,
        padding: "8px 0 8px 12px",
        display: "grid",
        gap: 4,
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
        <div style={{ flex: "1 1 280px", minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0, flexWrap: "wrap" }}>
            {code && <span style={{ fontSize: 11, color: sv.cyanDim, fontWeight: 700 }}>{code}</span>}
            <span
              data-testid="encode-library-name"
              title={e.source}
              style={{ ...ellipsis, fontSize: 13, color: sv.ink, minWidth: 0, flex: "0 1 auto" }}
            >
              {e.file}
            </span>
            <EncodeClassBadge codecClass={e.codec_class} forced={e.forced} />
            <StatePill state={state} />
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "2px 14px", marginTop: 4, fontSize: 11, color: sv.inkDim }}>
            <span data-testid="encode-library-sizes" title="Source size → encoded output size">
              {formatBytesCompact(e.source_bytes ?? 0)}
              {e.output_bytes != null && <> → {formatBytesCompact(e.output_bytes)}</>}
              {saved !== null && (
                <span style={{ color: saved >= 0 ? sv.green : sv.amber }}>
                  {" "}
                  ({saved >= 0 ? `−${saved}` : `+${-saved}`}%)
                </span>
              )}
            </span>
            <span
              data-testid="encode-library-encoded-at"
              title="When the encode record (marker) was last written. The marker stores no encode time, so this is the marker file's modification time."
            >
              {e.encoded_at != null ? `marked ${formatEncodedAt(e.encoded_at)}` : "not encoded yet"}
            </span>
          </div>
          {e.output && (
            <div style={{ display: "flex", gap: 6, marginTop: 2, fontSize: 10, color: sv.inkDim, minWidth: 0 }}>
              <span style={{ flexShrink: 0 }}>out →</span>
              <MiddlePath path={e.output} testid="encode-library-output" />
            </div>
          )}
          {state === "encoding" && e.progress && (
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 6 }}>
              <div
                role="progressbar"
                aria-label={`Encoding ${e.file}`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(e.progress.percent ?? 0)}
                style={{ flex: 1, height: 4, background: sv.bg3, overflow: "hidden" }}
              >
                <div style={{ width: `${Math.min(100, Math.max(0, e.progress.percent ?? 0))}%`, height: "100%", background: sv.cyan }} />
              </div>
              <span style={{ fontSize: 11, color: sv.ink }}>{(e.progress.percent ?? 0).toFixed(1)}%</span>
            </div>
          )}
          {(e.note || e.failure?.reason) && (
            <div
              title={e.failure?.reason ?? e.note ?? undefined}
              style={{
                ...ellipsis,
                fontSize: 11,
                marginTop: 2,
                color: state === "failed" ? sv.magentaHi : state === "output_missing" ? sv.red : state === "stale" ? sv.amber : sv.inkDim,
              }}
            >
              {state === "failed" && e.failure?.reason ? e.failure.reason : e.note}
            </div>
          )}
        </div>
        {onReencode && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
            <EncodeClassSelect
              label={`Pipeline for ${e.file}`}
              title="Pipeline for the re-encode. Auto lets the classifier decide (and clears a pin)."
              value={choice}
              disabled={blocked}
              onChange={setChoice}
            />
            <button
              type="button"
              data-testid="encode-library-reencode"
              aria-label={`Re-encode ${e.file}`}
              disabled={blocked}
              onClick={() => (replacesExistingEncode(e) ? setConfirming(true) : void fire())}
              style={actionButton(blocked, sv.cyan)}
            >
              <IcoRetry size={12} />
              Re-encode
            </button>
          </div>
        )}
      </div>
      {confirming && (
        <div
          role="alertdialog"
          aria-label={`Confirm re-encode of ${e.file}`}
          data-testid="encode-library-confirm"
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            gap: 10,
            marginTop: 4,
            padding: "8px 10px",
            border: `1px solid ${sv.amber}66`,
            background: `${sv.amber}10`,
            fontSize: 11,
            color: sv.amber,
          }}
        >
          <span style={{ flex: "1 1 260px" }}>
            This replaces the existing encode{e.output ? ` (${e.output.split("/").pop()})` : ""}. No second
            version is kept. Pipeline: {choice ?? "Auto (classifier)"}.
          </span>
          <button
            type="button"
            data-testid="encode-library-confirm-yes"
            onClick={() => void fire()}
            style={actionButton(false, sv.amber)}
          >
            Replace
          </button>
          <button
            type="button"
            data-testid="encode-library-confirm-no"
            onClick={() => setConfirming(false)}
            style={actionButton(false, sv.inkDim)}
          >
            Cancel
          </button>
        </div>
      )}
    </li>
  );
}

function groupKey(g: EncodeLibraryGroup): string {
  return `${g.kind}:${g.title}`;
}

const KIND_ICON = {
  series: IcoTv,
  film: IcoMovie,
  other: IcoLibrary,
} as const;

interface GroupProps {
  group: EncodeLibraryGroup;
  total: number;
  expanded: boolean;
  onToggle: () => void;
  queued: ReadonlySet<string>;
  onReencode?: RowProps["onReencode"];
}

function EncodeLibraryGroupSection({ group: g, total, expanded, onToggle, queued, onReencode }: GroupProps) {
  const Icon = KIND_ICON[g.kind];
  const flagged = g.entries.filter((e) => e.state === "stale" || e.state === "output_missing" || e.state === "failed").length;
  const listStyle: CSSProperties = { listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 4 };
  return (
    <section data-testid="encode-library-group" data-kind={g.kind} aria-label={g.title}>
      <SvPanel pad={0} accent={expanded ? `${sv.cyan}44` : sv.line} hideCorners={!expanded}>
        <button
          type="button"
          data-testid="encode-library-group-toggle"
          aria-expanded={expanded}
          onClick={onToggle}
          style={{
            width: "100%",
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "12px 16px",
            background: "transparent",
            border: "none",
            color: sv.ink,
            textAlign: "left",
            cursor: "pointer",
          }}
        >
          <span style={{ fontFamily: sv.mono, color: sv.cyan, width: 12, flexShrink: 0 }}>{expanded ? "▾" : "▸"}</span>
          <Icon size={16} color={g.kind === "other" ? sv.inkDim : sv.cyan} style={{ flexShrink: 0 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              data-testid="encode-library-group-title"
              style={{ ...ellipsis, fontFamily: sv.display, fontSize: 15, fontWeight: 700, letterSpacing: "0.03em" }}
            >
              {g.title}
            </div>
            {g.folder ? (
              <MiddlePath
                path={g.folder}
                testid="encode-library-group-folder"
                style={{ fontSize: 11, color: sv.inkDim, marginTop: 2 }}
              />
            ) : (
              <div style={{ fontFamily: sv.mono, fontSize: 11, color: sv.inkDim, marginTop: 2 }}>
                Not under the TV or Movies root
              </div>
            )}
          </div>
          {flagged > 0 && (
            <span style={{ ...microLabel, color: sv.amber, flexShrink: 0 }} title="Stale, missing-output or failed rows">
              {flagged} need attention
            </span>
          )}
          <span
            data-testid="encode-library-group-count"
            style={{ ...microLabel, fontWeight: 700, color: sv.inkDim, border: `1px solid ${sv.lineMid}`, padding: "3px 8px", flexShrink: 0 }}
          >
            {g.entries.length === total ? total : `${g.entries.length}/${total}`} {g.kind === "series" ? "eps" : "files"}
          </span>
        </button>
        {expanded && (
          <div style={{ padding: "0 16px 14px 40px" }}>
            {g.kind === "series" ? (
              bySeason(g.entries).map(({ season, entries }) => (
                <div key={String(season)} data-testid="encode-library-season" style={{ marginTop: 10 }}>
                  <div style={{ ...microLabel, color: sv.cyanDim, borderBottom: `1px solid ${sv.line}`, paddingBottom: 4, marginBottom: 4, display: "flex", gap: 10, minWidth: 0 }}>
                    <span style={{ flexShrink: 0 }}>{seasonLabel(season)}</span>
                    <span style={{ color: sv.inkDim, flexShrink: 0 }}>· {entries.length}</span>
                    <MiddlePath path={entries[0].folder} style={{ textTransform: "none", letterSpacing: 0, color: sv.inkDim }} />
                  </div>
                  <ul style={listStyle}>
                    {entries.map((e) => (
                      <EncodeLibraryRow key={e.source} entry={e} queued={queued.has(e.source)} onReencode={onReencode} />
                    ))}
                  </ul>
                </div>
              ))
            ) : (
              <ul style={{ ...listStyle, marginTop: 6 }}>
                {g.entries.map((e) => (
                  <EncodeLibraryRow key={e.source} entry={e} queued={queued.has(e.source)} onReencode={onReencode} />
                ))}
              </ul>
            )}
          </div>
        )}
      </SvPanel>
    </section>
  );
}

function LiveSection({ encodes }: { encodes: EncodeProgress[] }) {
  const active = encodes.length > 0;
  return (
    <SvPanel
      glow={active}
      pad={18}
      accent={active ? `${sv.cyan}55` : sv.lineMid}
      testid="encode-library-live"
      style={{ background: `linear-gradient(180deg, ${sv.bg2}cc, ${sv.bg1}ee)`, color: sv.ink }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        {active ? <IcoRipping size={16} color={sv.cyan} /> : <IcoIdle size={16} color={sv.inkDim} />}
        <h2 style={{ fontFamily: sv.display, fontSize: 14, fontWeight: 700, letterSpacing: "0.04em", margin: 0 }}>Encoding now</h2>
        <span style={{ ...microLabel, marginLeft: "auto", color: active ? sv.cyan : sv.inkFaint }}>
          {active ? `${encodes.length} active` : "Idle"}
        </span>
      </div>
      {active ? (
        <ul style={{ listStyle: "none", margin: "12px 0 0", padding: 0, display: "grid", gap: 12 }}>
          {encodes.map((row) => {
            const pct = Math.min(100, Math.max(0, row.percent ?? 0));
            return (
              <li key={row.file} data-testid="encode-library-live-row" style={{ fontFamily: sv.mono }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                  <span title={row.file} style={{ ...ellipsis, color: sv.cyanHi, fontSize: 13, fontWeight: 600 }}>
                    {row.file}
                  </span>
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
                  <span style={{ minWidth: 52, textAlign: "right", fontSize: 13 }}>{pct.toFixed(1)}%</span>
                </div>
                <div style={{ display: "flex", gap: 16, marginTop: 4, fontSize: 11, color: sv.inkDim, minWidth: 0 }}>
                  <span style={{ flexShrink: 0 }}>ETA {formatEta(row.eta)}</span>
                  <span style={{ flexShrink: 0 }}>{row.speed ?? "—"}</span>
                  <span style={{ flexShrink: 0 }}>{row.codec ?? ""}</span>
                  <MiddlePath path={`→ ${row.destination_folder}`} />
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p style={{ margin: "8px 0 0", fontFamily: sv.mono, fontSize: 11, color: sv.inkDim }}>
          Nothing is encoding. Progress shows here the moment an encode holds a slot.
        </p>
      )}
    </SvPanel>
  );
}

/**
 * The encode library: a live section pinned on top (fed by the existing
 * `encode_progress` WebSocket state), a filter bar, and every recorded encode
 * grouped by series / film folder, each row with a pipeline selector and
 * Re-encode. Presentational: data and the retry call come in as props.
 */
export function EncodeLibraryView({
  library,
  loading = false,
  error = null,
  encodes,
  queued = new Set<string>(),
  onReencode,
  onRefresh,
}: EncodeLibraryViewProps) {
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState<StateFilter>("all");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const liveRows = useMemo(() => {
    const rows = encodes ? (Array.isArray(encodes) ? encodes : Array.from(encodes.values())) : [];
    return rows.slice().sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0) || a.file.localeCompare(b.file));
  }, [encodes]);

  const merged = useMemo(() => (library ? overlayLive(library, encodes) : null), [library, encodes]);
  const totalsByGroup = useMemo(() => {
    const m = new Map<string, number>();
    for (const g of merged?.groups ?? []) m.set(groupKey(g), g.entries.length);
    return m;
  }, [merged]);
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: 0 };
    for (const g of merged?.groups ?? []) {
      for (const e of g.entries) {
        c.all += 1;
        c[e.state] = (c[e.state] ?? 0) + 1;
      }
    }
    return c;
  }, [merged]);
  const visible = useMemo(
    () => (merged ? filterLibrary(merged.groups, query, stateFilter) : []),
    [merged, query, stateFilter],
  );
  const filtering = query.trim() !== "" || stateFilter !== "all";

  return (
    <div data-testid="encode-library" style={{ display: "grid", gap: 18 }}>
      <LiveSection encodes={liveRows} />

      {/* Filter bar */}
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
        <input
          type="search"
          data-testid="encode-library-search"
          aria-label="Search encodes"
          placeholder="Search title, file or folder…"
          value={query}
          onChange={(ev) => setQuery(ev.target.value)}
          style={{
            flex: "1 1 260px",
            minWidth: 200,
            fontFamily: sv.mono,
            fontSize: 12,
            color: sv.ink,
            background: sv.bg1,
            border: `1px solid ${sv.lineMid}`,
            borderRadius: 0,
            padding: "8px 10px",
            outline: "none",
          }}
        />
        <div role="group" aria-label="Filter by state" style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {STATE_FILTERS.map((f) => {
            const on = stateFilter === f.key;
            return (
              <button
                key={f.key}
                type="button"
                data-testid={`encode-library-filter-${f.key}`}
                aria-pressed={on}
                onClick={() => setStateFilter(f.key)}
                style={{
                  ...microLabel,
                  fontWeight: 700,
                  padding: "6px 10px",
                  background: on ? `${sv.cyan}1f` : "transparent",
                  color: on ? sv.cyan : sv.inkDim,
                  border: `1px solid ${on ? sv.lineHi : sv.lineMid}`,
                  cursor: "pointer",
                }}
              >
                {f.label} {counts[f.key] ?? 0}
              </button>
            );
          })}
        </div>
        {onRefresh && (
          <button type="button" data-testid="encode-library-refresh" onClick={onRefresh} disabled={loading} style={actionButton(loading, sv.inkDim)}>
            {loading ? "Scanning…" : "Rescan"}
          </button>
        )}
      </div>

      {error && (
        <div
          data-testid="encode-library-error"
          style={{ fontFamily: sv.mono, fontSize: 12, color: sv.magentaHi, border: `1px solid ${sv.magenta}55`, padding: "10px 12px", display: "flex", gap: 8, alignItems: "center" }}
        >
          <IcoError size={14} color={sv.magenta} />
          {error}
        </div>
      )}

      {merged === null ? (
        !error && (
          <div data-testid="encode-library-loading" style={{ ...microLabel, color: sv.inkDim, padding: 20 }}>
            Scanning the library for encode records…
          </div>
        )
      ) : merged.groups.length === 0 ? (
        <div
          data-testid="encode-library-empty"
          style={{ border: `1px dashed ${sv.lineMid}`, padding: "18px 20px", fontFamily: sv.mono }}
        >
          <div style={{ ...microLabel, fontSize: 11, fontWeight: 700, color: sv.inkDim }}>No encodes recorded yet</div>
          <p style={{ margin: "6px 0 0", fontSize: 11, lineHeight: 1.6, color: sv.inkDim }}>
            Every title the encode hook finishes (or deliberately leaves as filed) is listed here, grouped by series or
            film. Nothing under {merged.root_tv ?? "the TV root"} or {merged.root_movies ?? "the Movies root"} carries an
            encode record yet.
          </p>
        </div>
      ) : visible.length === 0 ? (
        <div data-testid="encode-library-no-match" style={{ ...microLabel, color: sv.inkDim, padding: 16, border: `1px dashed ${sv.lineMid}` }}>
          No encodes match this filter
        </div>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {visible.map((g) => {
            const key = groupKey(g);
            const isOpen = expanded[key] ?? filtering;
            return (
              <EncodeLibraryGroupSection
                key={key}
                group={g}
                total={totalsByGroup.get(key) ?? g.entries.length}
                expanded={isOpen}
                onToggle={() => setExpanded((prev) => ({ ...prev, [key]: !isOpen }))}
                queued={queued}
                onReencode={onReencode}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
