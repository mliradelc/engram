import type {
  EncodeLibrary,
  EncodeLibraryEntry,
  EncodeLibraryGroup,
  EncodeLibraryState,
  EncodeProgress,
} from "../../types";

// Plain (non-component) helpers for the encode library screen, kept out of the
// .tsx files so React fast refresh keeps working.

/** The state filter chips, in display order. `all` shows every row. */
export const STATE_FILTERS = [
  { key: "all", label: "All" },
  { key: "encoded", label: "Encoded" },
  { key: "stale", label: "Stale" },
  { key: "output_missing", label: "Missing" },
  { key: "failed", label: "Failed" },
] as const;

export type StateFilter = (typeof STATE_FILTERS)[number]["key"];

/** Human label for a row state. */
export const STATE_LABELS: Record<EncodeLibraryState | "queued", string> = {
  encoded: "Encoded",
  passthrough: "Passthrough",
  stale: "Stale",
  output_missing: "Output missing",
  pending: "Pending",
  encoding: "Encoding",
  failed: "Failed",
  queued: "Queued",
};

/**
 * Fold the live `encode_progress` feed (the map useJobManagement keeps) into
 * the library: a file encoding right now shows `encoding` with the live
 * percent, whatever the (cached, up to 10 s old) scan said. Matched by file
 * name, which is how the hook keys its destination.
 */
export function overlayLive(
  library: EncodeLibrary,
  encodes: Map<string, EncodeProgress> | EncodeProgress[] | undefined,
): EncodeLibrary {
  const rows = encodes ? (Array.isArray(encodes) ? encodes : Array.from(encodes.values())) : [];
  if (rows.length === 0) return library;
  const byFile = new Map(rows.map((r) => [r.file, r]));
  let changed = false;
  const groups = library.groups.map((g) => {
    let groupChanged = false;
    const entries = g.entries.map((e) => {
      const live = byFile.get(e.file);
      if (!live) return e;
      groupChanged = true;
      return {
        ...e,
        state: "encoding" as const,
        codec_class: live.codec_class ?? e.codec_class,
        forced: live.codec_class ? Boolean(live.forced) : e.forced,
        progress: {
          percent: live.percent,
          eta: live.eta,
          speed: live.speed,
          destination_folder: live.destination_folder,
          started_at: live.started_at ?? null,
        },
      };
    });
    if (!groupChanged) return g;
    changed = true;
    return { ...g, entries };
  });
  return changed ? { ...library, groups } : library;
}

function matchesState(entry: EncodeLibraryEntry, filter: StateFilter): boolean {
  return filter === "all" || entry.state === filter;
}

/**
 * Apply the free-text query and the state filter. A group whose title or
 * folder matches the query keeps every entry that passes the state filter;
 * otherwise only entries whose file name or folder match survive. Groups left
 * with no entries are dropped.
 */
export function filterLibrary(
  groups: EncodeLibraryGroup[],
  query: string,
  stateFilter: StateFilter,
): EncodeLibraryGroup[] {
  const q = query.trim().toLowerCase();
  const out: EncodeLibraryGroup[] = [];
  for (const g of groups) {
    const groupHit = !q || g.title.toLowerCase().includes(q) || g.folder.toLowerCase().includes(q);
    const entries = g.entries.filter(
      (e) =>
        matchesState(e, stateFilter) &&
        (groupHit ||
          e.file.toLowerCase().includes(q) ||
          e.folder.toLowerCase().includes(q) ||
          (e.output ?? "").toLowerCase().includes(q)),
    );
    if (entries.length > 0) out.push(entries.length === g.entries.length ? g : { ...g, entries });
  }
  return out;
}

/** Entries of a series group split by season (null season last, as "Other"). */
export function bySeason(entries: EncodeLibraryEntry[]): Array<{ season: number | null; entries: EncodeLibraryEntry[] }> {
  const map = new Map<number | null, EncodeLibraryEntry[]>();
  for (const e of entries) {
    const list = map.get(e.season) ?? [];
    list.push(e);
    map.set(e.season, list);
  }
  return Array.from(map.entries())
    .sort(([a], [b]) => (a ?? Number.MAX_SAFE_INTEGER) - (b ?? Number.MAX_SAFE_INTEGER))
    .map(([season, list]) => ({ season, entries: list }));
}

/** "Season 03" / "Specials" (season 0) / "Other files". */
export function seasonLabel(season: number | null): string {
  if (season === null) return "Other files";
  if (season === 0) return "Specials";
  return `Season ${String(season).padStart(2, "0")}`;
}

/** Percent saved by the encode, or null when either size is unknown. */
export function savedPercent(sourceBytes: number | null, outputBytes: number | null): number | null {
  if (!sourceBytes || outputBytes == null || sourceBytes <= 0) return null;
  return Math.round((1 - outputBytes / sourceBytes) * 100);
}

/**
 * Split a path for middle truncation: the head may ellipsise, the tail (the
 * last `tailChars`, ideally starting at a separator) always shows. Both halves
 * stay in the DOM, so selecting and copying the text yields the full path.
 */
export function splitForMiddleTruncation(path: string, tailChars = 28): [string, string] {
  if (path.length <= tailChars + 8) return [path, ""];
  let cut = path.length - tailChars;
  const sep = path.lastIndexOf("/", cut + 10);
  if (sep > 0 && sep >= cut - 20) cut = sep;
  return [path.slice(0, cut), path.slice(cut)];
}

/** Epoch seconds -> "Nov 3, 2026 14:05". */
export function formatEncodedAt(epochSeconds: number | null): string {
  if (epochSeconds == null || !Number.isFinite(epochSeconds)) return "—";
  const d = new Date(epochSeconds * 1000);
  return `${d.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" })} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

/** The detail of a FastAPI error body (`{"detail": "..."}`), else the raw body. */
export function apiErrorDetail(body: string): string {
  if (!body) return "";
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
    if (parsed.detail !== undefined) return JSON.stringify(parsed.detail);
  } catch {
    // not JSON: fall through to the raw body
  }
  return body;
}

/** True for a row whose re-encode would overwrite a recorded output. */
export function replacesExistingEncode(entry: EncodeLibraryEntry): boolean {
  return entry.output !== null && entry.state !== "passthrough";
}
