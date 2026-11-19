import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { Layers } from "lucide-react";
import { ApiError, apiFetch, apiFetchVoid } from "../api/client";
import { SvAtmosphere, SvPageHeader, sv } from "../app/components/synapse";
import { EncodeLibraryView } from "../app/components/EncodeLibraryView";
import { apiErrorDetail } from "../app/components/encodeLibrary";
import { useJobManagement } from "../app/hooks/useJobManagement";
import { ROUTES } from "../config/routes";
import type { EncodeClass, EncodeLibrary, EncodeLibraryEntry, EncodeRetryRequest, EncodeRetryResponse } from "../types";

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return apiErrorDetail(error.body) || error.message;
  return error instanceof Error ? error.message : String(error);
}

/**
 * `/encodes`: every recorded encode (GET /api/encoding/library), grouped by
 * series / film, with a live section on top.
 *
 * Live progress comes from useJobManagement, the same hook (and the same single
 * WebSocket) the dashboard uses; the dashboard is unmounted on this route, so
 * there is still exactly one socket and no extra poller. The library itself is
 * fetched on mount, when an encode finishes (a file leaves the live map), and
 * on Rescan; the backend caches the disk scan for 10 s.
 */
export default function EncodeLibraryPage() {
  const navigate = useNavigate();
  const devMode = window.location.search.includes("mock=true");
  const { encodes } = useJobManagement(devMode);
  const [library, setLibrary] = useState<EncodeLibrary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [queued, setQueued] = useState<ReadonlySet<string>>(() => new Set());
  const live = useRef(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiFetch<EncodeLibrary>("/api/encoding/library");
      if (!live.current) return;
      setLibrary(data);
      setError(null);
      // A queued source the server now reports as encoding/failed no longer needs the local flag.
      setQueued((prev) => {
        if (prev.size === 0) return prev;
        const next = new Set(prev);
        for (const g of data.groups) {
          for (const e of g.entries) {
            if (e.state === "encoding" || e.state === "failed") next.delete(e.source);
          }
        }
        return next.size === prev.size ? prev : next;
      });
    } catch (err) {
      if (!live.current) return;
      setError(`Could not load the encode library. ${errorMessage(err)}`);
      // Idle-safe: never leave the screen on "loading" forever.
      setLibrary((prev) => prev ?? { root_tv: null, root_movies: null, groups: [], totals: {} });
    } finally {
      if (live.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    live.current = true;
    void load();
    return () => {
      live.current = false;
    };
  }, [load]);

  // Refetch when a file leaves the live map (its encode finished or failed),
  // and drop the queued flag of a file that has started encoding.
  const liveFiles = encodes ? Array.from(encodes.keys()).sort().join("\n") : "";
  const prevLive = useRef(liveFiles);
  useEffect(() => {
    const before = prevLive.current ? prevLive.current.split("\n") : [];
    const now = new Set(liveFiles ? liveFiles.split("\n") : []);
    prevLive.current = liveFiles;
    if (now.size > 0 && library) {
      setQueued((prev) => {
        if (prev.size === 0) return prev;
        const next = new Set(prev);
        for (const g of library.groups) for (const e of g.entries) if (now.has(e.file)) next.delete(e.source);
        return next.size === prev.size ? prev : next;
      });
    }
    if (before.some((f) => !now.has(f))) void load();
  }, [liveFiles, library, load]);

  const reencode = useCallback(
    async (entry: EncodeLibraryEntry, codecClass?: EncodeClass) => {
      try {
        if (codecClass === undefined) {
          // Auto must mean the classifier: a retry without a class still honours
          // a stored pin, so clear it first (404 is left for the retry to report).
          try {
            await apiFetchVoid(`/api/encoding/override?file=${encodeURIComponent(entry.source)}`, { method: "DELETE" });
          } catch (err) {
            if (!(err instanceof ApiError && err.status === 404)) throw err;
          }
        }
        const body: EncodeRetryRequest =
          codecClass === undefined ? { file: entry.source } : { file: entry.source, codec_class: codecClass };
        await apiFetch<EncodeRetryResponse>("/api/encoding/retry", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        setQueued((prev) => new Set(prev).add(entry.source));
        toast.success(`Re-encode queued: ${entry.file} (${codecClass ?? "Auto"})`);
      } catch (err) {
        // 409/422 bodies explain why (already encoding, HDR, staging): show them as-is.
        const detail = errorMessage(err);
        if (err instanceof ApiError && err.status === 409) toast.warning(detail);
        else toast.error(`Could not re-encode ${entry.file}: ${detail}`);
      }
    },
    [],
  );

  return (
    <SvAtmosphere>
      <SvPageHeader
        title="Encode Library"
        subtitle="› Actual and past encodes, by series and film"
        icon={<Layers size={20} />}
        onBack={() => navigate(ROUTES.HOME)}
      />
      <div style={{ maxWidth: sv.layoutMaxWidth, margin: "0 auto", padding: `24px ${sv.layoutPadX}px 48px` }}>
        <EncodeLibraryView
          library={library}
          loading={loading}
          error={error}
          encodes={encodes}
          queued={queued}
          onReencode={reencode}
          onRefresh={() => void load()}
        />
      </div>
    </SvAtmosphere>
  );
}
