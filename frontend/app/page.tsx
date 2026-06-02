"use client";

import {
  useState,
  useCallback,
  useEffect,
  useRef,
  Suspense,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";

import SearchForm     from "@/components/trials/SearchForm";
import TrialList      from "@/components/trials/TrialList";
import TrialSiteMap   from "@/components/trials/TrialSiteMap";
import PhysicianPanel from "@/components/physicians/PhysicianPanel";

import { useTrials, fetchTrialSites } from "@/hooks/useTrials";
import { usePhysicians }              from "@/hooks/usePhysicians";
import { getConditionSpecialties }    from "@/lib/api";
import { initializeCityStateValidation } from "@/lib/validation";

import type { Trial, TrialSearchFilters, SiteData } from "@/types/trial";
import type { SelectedSite }                         from "@/types/physician";

const SEARCH_H = 64;

function HomeInner() {
  const router       = useRouter();
  const searchParams = useSearchParams();

  const filtersFromUrl: TrialSearchFilters = {
    condition: searchParams.get("condition") ?? "",
    city:      searchParams.get("city")      ?? "",
    state:     searchParams.get("state")     ?? "",
    status:    searchParams.get("status")    ?? "",
    phase:     searchParams.get("phase")     ?? "",
  };
  const hasResults = Boolean(filtersFromUrl.condition.trim());

  const [selectedTrial, setSelectedTrial] = useState<Trial | null>(null);
  const [siteData,      setSiteData]      = useState<SiteData | null>(null);
  const [sitesLoading,  setSitesLoading]  = useState(false);
  const [sitesError,    setSitesError]    = useState<string | null>(null);
  const [selectedSite,  setSelectedSite]  = useState<SelectedSite | null>(null);

  const pinnedTrialSpecialtyRef = useRef<string | undefined>(undefined);
  const pinnedUserSpecialtyRef  = useRef<string | undefined>(undefined);
  const sitesFetchAbortRef = useRef<AbortController | null>(null);

  const {
    physicians:        nearbyPhysicians,
    total:             physicianTotal,
    loading:           physiciansLoading,
    error:             physiciansError,
    searched:          physiciansSearched,
    hasMore:           physiciansHasMore,
    search:            searchPhysicians,
    loadMore:          loadMorePhysicians,
    reset:             resetPhysicians,
    searchSpecialties: physiciansSearchSpecialties,
  } = usePhysicians();

  const prevConditionRef = useRef(filtersFromUrl.condition);

  useEffect(() => {
    if (prevConditionRef.current !== filtersFromUrl.condition) {
      setSelectedTrial(null);
      setSiteData(null);
      setSitesError(null);
      setSelectedSite(null);
      resetPhysicians();
      pinnedTrialSpecialtyRef.current = undefined;
      pinnedUserSpecialtyRef.current  = undefined;
      prevConditionRef.current = filtersFromUrl.condition;
    }
  }, [filtersFromUrl.condition, resetPhysicians]);

  useEffect(() => {
    return () => { sitesFetchAbortRef.current?.abort(); };
  }, []);

  useEffect(() => {
    initializeCityStateValidation();
  }, []);

  const {
    trials,
    loading,
    error,
    totalCount,
    hasMore,
    refetch,
    loadMore,
    correctedQuery,
  } = useTrials(
    hasResults ? filtersFromUrl.condition : null,
    filtersFromUrl.city   || null,
    filtersFromUrl.state  || null,
    filtersFromUrl.status || undefined,
    filtersFromUrl.phase  || undefined,
  );

  const handleSearch = useCallback((nextFilters: TrialSearchFilters) => {
    const params = new URLSearchParams();
    if (nextFilters.condition.trim()) params.set("condition", nextFilters.condition.trim());
    if (nextFilters.city.trim())      params.set("city",      nextFilters.city.trim());
    if (nextFilters.state)            params.set("state",     nextFilters.state);
    if (nextFilters.status)           params.set("status",    nextFilters.status);
    if (nextFilters.phase)            params.set("phase",     nextFilters.phase);
    router.push(`?${params.toString()}`);

    setSelectedTrial(null);
    setSiteData(null);
    setSitesError(null);
    setSelectedSite(null);
    resetPhysicians();
    pinnedTrialSpecialtyRef.current = undefined;
    pinnedUserSpecialtyRef.current  = undefined;
  }, [resetPhysicians, router]);

  const handleSelectTrial = useCallback(async (trial: Trial) => {
    if (selectedTrial?.nctId === trial.nctId) {
      setSelectedTrial(null);
      setSiteData(null);
      setSelectedSite(null);
      resetPhysicians();
      pinnedTrialSpecialtyRef.current = undefined;
      pinnedUserSpecialtyRef.current  = undefined;
      return;
    }

    sitesFetchAbortRef.current?.abort();
    const controller = new AbortController();
    sitesFetchAbortRef.current = controller;

    setSelectedTrial(trial);
    setSiteData(null);
    setSitesError(null);
    setSelectedSite(null);
    resetPhysicians();
    pinnedTrialSpecialtyRef.current = undefined;
    pinnedUserSpecialtyRef.current  = undefined;
    setSitesLoading(true);

    try {
      const data = await fetchTrialSites(trial.nctId, controller.signal);
      if (!controller.signal.aborted) setSiteData(data);
    } catch (err) {
      if (!controller.signal.aborted)
        setSitesError("Could not load site locations. Please try again.");
    } finally {
      if (!controller.signal.aborted) setSitesLoading(false);
    }
  }, [resetPhysicians, selectedTrial]);

  const handleFindPhysicians = useCallback(async (site: SelectedSite) => {
    setSelectedSite(site);
    resetPhysicians();
    pinnedTrialSpecialtyRef.current = undefined;
    pinnedUserSpecialtyRef.current  = undefined;

    const userSearchCondition = filtersFromUrl.condition.trim();
    const trialCondition      = site.condition?.trim() ?? "";

    let trialSpecialty: string | undefined;
    if (trialCondition) {
      try {
        const list = await getConditionSpecialties(trialCondition);
        trialSpecialty = list.length > 0 ? list.join(", ") : trialCondition;
      } catch {
        trialSpecialty = trialCondition;
      }
    }

    let userSpecialty: string | undefined;
    // Phase 1: use AI-corrected query for specialty lookup so
    // "hart attak" → "myocardial infarction" → ["Cardiology", "Interventional Cardiology"]
    // instead of "hart attak" → taxonomy finds nothing → wrong physicians
    const userQueryForSpecialty = correctedQuery ?? userSearchCondition;
    if (userQueryForSpecialty && userQueryForSpecialty.toLowerCase() !== trialCondition.toLowerCase()) {
      try {
        const list = await getConditionSpecialties(userQueryForSpecialty);
        userSpecialty = list.length > 0 ? list.join(", ") : userQueryForSpecialty;
      } catch {
        userSpecialty = userQueryForSpecialty;
      }
    }

    pinnedTrialSpecialtyRef.current = trialSpecialty;
    pinnedUserSpecialtyRef.current  = userSpecialty;

    const initialSpecialty = userSpecialty ?? trialSpecialty;

    searchPhysicians(site, 25, trialSpecialty, userSpecialty, initialSpecialty);
  }, [resetPhysicians, searchPhysicians, filtersFromUrl.condition, correctedQuery]);

  const handlePhysicianSearch = useCallback(
    (radius: number, _specialty: string, panelUserSpecialty: string, _initialSpecialty: string) => {
      if (!selectedSite) return;
      const trialSpecialty   = pinnedTrialSpecialtyRef.current;
      const userBarSpecialty = pinnedUserSpecialtyRef.current;
      const panelInput = panelUserSpecialty.trim() || undefined;
      const initialSpecialty = userBarSpecialty ?? trialSpecialty;
      searchPhysicians(selectedSite, radius, trialSpecialty, panelInput, initialSpecialty);
    },
    [selectedSite, searchPhysicians],
  );

  const handleBackToSites = useCallback(() => {
    setSelectedSite(null);
    resetPhysicians();
  }, [resetPhysicians]);

  const searchFormKey = [
    filtersFromUrl.condition, filtersFromUrl.city, filtersFromUrl.state,
    filtersFromUrl.status, filtersFromUrl.phase,
  ].join("|");

  const kpiData = siteData ? {
    total:      siteData.sites.length,
    recruiting: siteData.sites.filter(s => s.status?.toLowerCase() === "recruiting").length,
    onMap:      siteData.sites.filter(s => s.lat != null && s.lon != null).length,
    countries:  new Set(siteData.sites.map(s => s.country).filter(Boolean)).size || 1,
  } : null;

  return (
    <>
      <style>{`
        .app-shell {
          display: flex; flex-direction: column;
          background: var(--surface);
          font-family: var(--font-sans);
        }

        /* ── Search bar ── */
        .search-bar {
          height: ${SEARCH_H}px; flex-shrink: 0;
          background: #fff;
          border-bottom: 1px solid var(--border);
          padding: 0 20px;
          display: flex; align-items: center;
          box-shadow: var(--shadow-sm);
          z-index: 100;
          position: sticky; top: 0;
        }

        /* ── Hero ── */
        .hero-wrap {
          display: flex; align-items: flex-start; justify-content: center;
          background: linear-gradient(160deg, #eff6ff 0%, #dbeafe 50%, #f0fdf4 100%);
          position: relative;
          min-height: calc(100vh - ${SEARCH_H}px);
        }
        .hero-bg-pattern {
          position: absolute; inset: 0; pointer-events: none;
          background-image: radial-gradient(circle at 1px 1px, rgba(37,99,235,0.07) 1px, transparent 0);
          background-size: 28px 28px;
          opacity: 0.6;
        }
        .hero-content {
          position: relative; z-index: 1;
          width: 100%; max-width: 960px;
          padding: 24px 24px;
          animation: fadeUp 0.5s cubic-bezier(.22,1,.36,1) both;
        }

        /* ── Results: 30/70 split ── */
        .results-layout {
          display: grid;
          grid-template-columns: 30% 70%;
          align-items: start;
          animation: fadeIn 0.25s ease both;
        }
        @media (max-width: 900px) {
          .results-layout { grid-template-columns: 1fr; }
        }

        /* ── Left panel ── */
        .trials-panel {
          border-right: 1px solid var(--border);
          background: #fff;
          display: flex; flex-direction: column; min-width: 0;
          position: sticky; top: ${SEARCH_H}px;
          max-height: calc(100vh - ${SEARCH_H}px);
          overflow-y: auto;
        }

        /* ── Right panel ── */
        .detail-panel {
          display: flex; flex-direction: column;
          background: var(--surface); min-width: 0;
        }

        /* ── Trial detail header ── */
        .trial-detail-header {
          padding: 14px 20px;
          border-bottom: 1px solid var(--border);
          background: #fff; flex-shrink: 0;
        }
        .tdh-badges {
          display: flex; align-items: center; gap: 6px;
          margin-bottom: 7px; flex-wrap: wrap;
        }
        .tdh-nct {
          font-size: 10px; font-weight: 700; color: #2563eb;
          letter-spacing: 1px; text-transform: uppercase;
          font-family: var(--font-mono);
        }
        .tdh-title {
          font-size: 14px; font-weight: 600; color: var(--ink);
          line-height: 1.5; margin-bottom: 5px;
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
        }
        .tdh-sponsor { font-size: 11px; color: var(--muted); }
        .tdh-sponsor strong { color: var(--ink-3); font-weight: 500; }

        /* ── KPI bar ── */
        .kpi-row {
          display: grid; grid-template-columns: repeat(4, 1fr);
          border-bottom: 1px solid var(--border);
          background: #fff; flex-shrink: 0;
        }
        .kpi-cell {
          padding: 12px 8px; text-align: center;
          border-right: 1px solid var(--border);
          transition: background 0.15s;
        }
        .kpi-cell:last-child { border-right: none; }
        .kpi-cell:hover { background: var(--surface); }
        .kpi-num {
          font-size: 22px; font-weight: 700; color: var(--ink);
          line-height: 1; font-family: var(--font-mono);
          letter-spacing: -0.5px;
        }
        .kpi-num.green { color: #16a34a; }
        .kpi-label {
          font-size: 9px; font-weight: 600; letter-spacing: 0.08em;
          text-transform: uppercase; color: var(--muted); margin-top: 4px;
        }

        .detail-content { display: contents; }

        /* ── State boxes ── */
        .state-box {
          display: flex; flex-direction: column; align-items: center;
          justify-content: center; gap: 12px;
          padding: 56px 24px; color: var(--muted);
        }
        .state-msg { font-size: 13px; font-weight: 500; color: var(--muted); }

        /* ── Error box ── */
        .error-box {
          margin: 16px; padding: 14px 16px; border-radius: var(--radius-lg);
          background: var(--coral-50); border: 1px solid #fecaca;
          color: var(--coral-600); font-size: 13px;
          display: flex; flex-direction: column; gap: 10px;
          animation: fadeIn 0.2s ease both;
        }
        .err-label {
          font-size: 10px; font-weight: 700; text-transform: uppercase;
          letter-spacing: 0.8px; opacity: 0.7;
        }

        /* ── No results ── */
        .no-results {
          display: flex; flex-direction: column; align-items: center;
          justify-content: center; gap: 10px;
          padding: 64px 24px; text-align: center; color: var(--muted);
          animation: fadeUp 0.3s ease both;
        }
        .no-results-icon {
          width: 52px; height: 52px; border-radius: 50%;
          background: var(--surface-2);
          display: flex; align-items: center; justify-content: center;
          font-size: 22px;
        }
        .no-results h3 { font-size: 14px; font-weight: 600; color: var(--ink-3); }
        .no-results p  { font-size: 12px; max-width: 220px; line-height: 1.6; }

        /* ── Detail empty ── */
        .detail-empty {
          display: flex; flex-direction: column; align-items: center;
          justify-content: center; gap: 18px;
          height: 100%; padding: 48px; text-align: center; color: var(--muted);
          animation: fadeIn 0.3s ease both;
        }
        .detail-empty-visual {
          width: 80px; height: 80px;
          border-radius: 24px;
          background: linear-gradient(135deg, var(--blue-50), var(--surface-2));
          display: flex; align-items: center; justify-content: center;
          font-size: 36px;
          border: 1px solid var(--border);
        }
        .detail-empty-title {
          font-size: 15px; font-weight: 600; color: var(--ink-2);
        }
        .detail-empty p {
          font-size: 13px; color: var(--muted);
          max-width: 240px; line-height: 1.7;
        }
        .detail-empty-steps {
          display: flex; flex-direction: column; gap: 8px;
          text-align: left; max-width: 260px;
        }
        .detail-step {
          display: flex; align-items: center; gap: 10px;
          font-size: 12px; color: var(--ink-3);
        }
        .detail-step-num {
          width: 22px; height: 22px; border-radius: 50%;
          background: var(--blue-50); color: #1d4ed8;
          font-size: 10px; font-weight: 700;
          display: flex; align-items: center; justify-content: center;
          flex-shrink: 0; border: 1px solid var(--blue-200);
        }
      `}</style>

      <div className="app-shell">

        {/* ── Hero (no search results) ── */}
        {!hasResults && (
          <div className="hero-wrap">
            <div className="hero-bg-pattern" />
            <div className="hero-content">
              <SearchForm
                key="hero"
                onSearch={handleSearch}
                loading={loading}
                compact={false}
                initialValues={filtersFromUrl}
              />
            </div>
          </div>
        )}

        {/* ── Compact search bar ── */}
        {hasResults && (
          <div className="search-bar">
            <SearchForm
              key={searchFormKey}
              onSearch={handleSearch}
              loading={loading}
              compact={true}
              initialValues={filtersFromUrl}
            />
          </div>
        )}

        {/* ── Results ── */}
        {hasResults && (
          <div className="results-layout">

            {/* Left 30%: Trial list */}
            <div className="trials-panel">
              {loading && (
                <div className="state-box">
                  <div className="spinner" />
                  <p className="state-msg">Searching trials…</p>
                </div>
              )}
              {!loading && error && (
                <div className="error-box">
                  <span className="err-label">Error loading trials</span>
                  <p>{error}</p>
                  <button className="btn btn-primary" onClick={refetch}
                    style={{ alignSelf: "flex-start" }}>
                    Try Again
                  </button>
                </div>
              )}
              {!loading && !error && trials.length === 0 && (
                <div className="no-results">
                  <div className="no-results-icon">🔍</div>
                  <h3>No trials found</h3>
                  <p>Try broadening your search or removing filters.</p>
                </div>
              )}
              {!loading && !error && trials.length > 0 && (
                <TrialList
                  trials={trials}
                  totalCount={totalCount}
                  selectedId={selectedTrial?.nctId ?? null}
                  onSelect={handleSelectTrial}
                  hasMore={hasMore}
                  onLoadMore={loadMore}
                  loading={loading}
                />
              )}
            </div>

            {/* Right 70%: Detail / map panel */}
            <div className="detail-panel">

              {!selectedTrial && (
                <div className="detail-empty">
                  <div className="detail-empty-visual">🗺️</div>
                  <div className="detail-empty-title">Select a trial to begin</div>
                  <p>Choose a trial from the list to view its site locations on the map and find nearby physicians.</p>
                  <div className="detail-empty-steps">
                    <div className="detail-step">
                      <span className="detail-step-num">1</span>
                      Click any trial in the list
                    </div>
                    <div className="detail-step">
                      <span className="detail-step-num">2</span>
                      Explore sites on the map
                    </div>
                    <div className="detail-step">
                      <span className="detail-step-num">3</span>
                      Find physicians near sites
                    </div>
                  </div>
                </div>
              )}

              {selectedTrial && (
                <>
                  {/* Trial header */}
                  <div className="trial-detail-header">
                    <div className="tdh-badges">
                      <span className="tdh-nct">{selectedTrial.nctId}</span>

                      {selectedTrial.status && (() => {
                        const s = selectedTrial.status.toLowerCase();
                        let cls = "badge badge-default";
                        if (s === "recruiting")                                      cls = "badge badge-recruiting";
                        else if (s.includes("active"))                               cls = "badge badge-active";
                        else if (s === "completed")                                  cls = "badge badge-completed";
                        else if (s === "terminated")                                 cls = "badge badge-terminated";
                        else if (s.includes("not yet") || s.includes("invitation")) cls = "badge badge-warning";
                        return <span className={cls}>{selectedTrial.status}</span>;
                      })()}

                      {selectedTrial.phases?.map((p) =>
                        p === "N/A" || p === "NA"
                          ? <span key={p} className="badge badge-default">Not applicable</span>
                          : <span key={p} className="badge badge-phase">{p}</span>
                      )}
                    </div>

                    <div className="tdh-title">{selectedTrial.title}</div>

                    {selectedTrial.sponsor && (
                      <div className="tdh-sponsor">
                        Sponsor: <strong>{selectedTrial.sponsor}</strong>
                      </div>
                    )}
                  </div>

                  <div className="detail-content">

                    {sitesLoading && (
                      <div className="state-box">
                        <div className="spinner" />
                        <p className="state-msg">Loading site locations…</p>
                      </div>
                    )}

                    {sitesError && !sitesLoading && (
                      <div className="error-box">
                        <span className="err-label">Error</span>
                        <p>{sitesError}</p>
                      </div>
                    )}

                    {siteData && !sitesLoading && selectedSite && (
                      <PhysicianPanel
                        site={selectedSite}
                        physicians={nearbyPhysicians}
                        total={physicianTotal}
                        loading={physiciansLoading}
                        error={physiciansError}
                        searched={physiciansSearched}
                        hasMore={physiciansHasMore}
                        searchSpecialties={physiciansSearchSpecialties}
                        onSearch={handlePhysicianSearch}
                        onLoadMore={loadMorePhysicians}
                        onBack={handleBackToSites}
                        kpiBar={
                          kpiData && (
                            <div className="kpi-row">
                              <div className="kpi-cell">
                                <div className="kpi-num">{kpiData.total}</div>
                                <div className="kpi-label">Total Sites</div>
                              </div>
                              <div className="kpi-cell">
                                <div className="kpi-num green">{kpiData.recruiting}</div>
                                <div className="kpi-label">Recruiting</div>
                              </div>
                              <div className="kpi-cell">
                                <div className="kpi-num">{kpiData.onMap}</div>
                                <div className="kpi-label">On Map</div>
                              </div>
                              <div className="kpi-cell">
                                <div className="kpi-num">{kpiData.countries}</div>
                                <div className="kpi-label">Countries</div>
                              </div>
                            </div>
                          )
                        }
                      />
                    )}

                    {siteData && !sitesLoading && !selectedSite && (
                      <>
                        {kpiData && (
                          <div className="kpi-row">
                            <div className="kpi-cell">
                              <div className="kpi-num">{kpiData.total}</div>
                              <div className="kpi-label">Total Sites</div>
                            </div>
                            <div className="kpi-cell">
                              <div className="kpi-num green">{kpiData.recruiting}</div>
                              <div className="kpi-label">Recruiting</div>
                            </div>
                            <div className="kpi-cell">
                              <div className="kpi-num">{kpiData.onMap}</div>
                              <div className="kpi-label">On Map</div>
                            </div>
                            <div className="kpi-cell">
                              <div className="kpi-num">{kpiData.countries}</div>
                              <div className="kpi-label">Countries</div>
                            </div>
                          </div>
                        )}
                        <TrialSiteMap
                          sites={siteData.sites}
                          trialTitle={siteData.title}
                          nctId={selectedTrial.nctId}
                          description={selectedTrial.description ?? null}
                          condition={selectedTrial.conditions?.[0] ?? null}
                          inclusionCriteria={selectedTrial.inclusionCriteria}
                          exclusionCriteria={selectedTrial.exclusionCriteria}
                          onFindPhysicians={handleFindPhysicians}
                        />
                      </>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>
        )}

        {/* ── Footer ── */}
        <div style={{
          background: "#fff",
          borderTop: "1px solid var(--border)",
          padding: "20px",
          textAlign: "center",
          fontSize: "12px",
          color: "var(--muted)",
          marginTop: "auto",
        }}>
          <div style={{ marginBottom: "8px" }}>
            <strong>Contact:</strong> leads@aquarient.com
          </div>
          <div>
            Powered by Aquarient Technologies
          </div>
        </div>
      </div>
    </>
  );
}

export default function Home() {
  return (
    <Suspense
      fallback={
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "center",
          height: "100vh", flexDirection: "column", gap: 14,
          fontFamily: "'DM Sans', sans-serif", color: "#8b95a1",
          background: "#f6f7fb",
        }}>
          <div style={{
            width: 26, height: 26, borderRadius: "50%",
            border: "3px solid #e4e8f0", borderTopColor: "#2563eb",
            animation: "spinAnim 0.75s linear infinite",
          }} />
          <span style={{ fontSize: 14, fontWeight: 500 }}>Loading…</span>
          <style>{`@keyframes spinAnim { to { transform: rotate(360deg); } }`}</style>
        </div>
      }
    >
      <HomeInner />
    </Suspense>
  );
}