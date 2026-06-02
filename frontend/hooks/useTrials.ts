// hooks/useTrials.ts
"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { fetchTrials, fetchTrialSites }             from "@/lib/api";
import type { Trial }                               from "@/types/trial";

const STATUS_ORDER: Record<string, number> = {
  "recruiting":              0,
  "active, not recruiting":  1,
  "not yet recruiting":      2,
  "enrolling by invitation": 3,
  "completed":               4,
  "terminated":              5,
  "suspended":               6,
  "withdrawn":               7,
  "unknown status":          8,
};

function sortTrialsByStatus(trials: Trial[]): Trial[] {
  return [...trials].sort((a, b) => {
    const aRank = STATUS_ORDER[a.status?.toLowerCase() ?? ""] ?? 99;
    const bRank = STATUS_ORDER[b.status?.toLowerCase() ?? ""] ?? 99;
    return aRank - bRank;
  });
}

export function useTrials(
  condition: string | null,
  city?:     string | null,
  state?:    string | null,
  status?:   string,
  phase?:    string,
) {
  const [trials,          setTrials]          = useState<Trial[]>([]);
  const [loading,         setLoading]         = useState(false);
  const [error,           setError]           = useState<string | null>(null);
  const [totalCount,      setTotalCount]      = useState(0);
  const [hasMore,         setHasMore]         = useState(false);

  // Phase 1 AI Search — store corrected query so page.tsx can use it
  // for specialty lookup instead of the raw misspelled user query
  const [correctedQuery,  setCorrectedQuery]  = useState<string | null>(null);
  const [wasCorreected,   setWasCorrected]    = useState(false);

  const LIMIT = 10;
  const offsetRef     = useRef(0);
  const abortRef      = useRef<AbortController | null>(null);

  const requestKey    = `${condition ?? ""}|${city ?? ""}|${state ?? ""}|${status ?? ""}|${phase ?? ""}`;
  const requestKeyRef = useRef(requestKey);

  const load = useCallback(
    async (reset: boolean, key: string) => {
      if (!condition) return;

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      const currentOffset = reset ? 0 : offsetRef.current;

      setLoading(true);
      setError(null);

      try {
        const data = await fetchTrials(
          {
            condition,
            city,
            state,
            status,
            phase,
            us_only:   true,
            page_size: LIMIT,
            page:      Math.floor(currentOffset / LIMIT) + 1,
          },
          controller.signal,
        );

        if (controller.signal.aborted || requestKeyRef.current !== key) return;

        const loadedCount = currentOffset + data.trials.length;
        offsetRef.current = loadedCount;

        setTrials((prev) =>
          reset
            ? sortTrialsByStatus(data.trials)
            : sortTrialsByStatus([...prev, ...data.trials])
        );
        setTotalCount(data.total);
        setHasMore(loadedCount < data.total);

        // Phase 1 — store corrected query from backend response
        // On first load (reset=true), capture whatever the backend corrected to.
        // On loadMore (reset=false), keep the existing corrected query.
        if (reset) {
          setCorrectedQuery(data.corrected_query ?? null);
          setWasCorrected(data.was_corrected ?? false);
        }

      } catch (err: unknown) {
        if ((err as Error).name === "AbortError") return;
        if (requestKeyRef.current !== key) return;
        setError((err as Error).message || "Failed to fetch trials");
      } finally {
        if (!controller.signal.aborted && requestKeyRef.current === key) {
          setLoading(false);
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [condition, city, state, status, phase],
  );

  useEffect(() => {
    requestKeyRef.current = requestKey;
    offsetRef.current     = 0;

    setTrials([]);
    setError(null);
    setTotalCount(0);
    setHasMore(false);
    setCorrectedQuery(null);
    setWasCorrected(false);

    if (!condition) {
      abortRef.current?.abort();
      setLoading(false);
      return;
    }

    void load(true, requestKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey]);

  useEffect(() => () => { abortRef.current?.abort(); }, []);

  const refetch = useCallback(() => {
    offsetRef.current = 0;
    void load(true, requestKeyRef.current);
  }, [load]);

  const loadMore = useCallback(() => {
    if (loading || !hasMore || !condition) return;
    void load(false, requestKeyRef.current);
  }, [condition, hasMore, load, loading]);

  return {
    trials,
    loading,
    error,
    totalCount,
    hasMore,
    refetch,
    loadMore,
    // Phase 1 additions
    correctedQuery,
    wasCorreected,
  };
}

export { fetchTrialSites };
