// lib/api.ts
//
// v9 changes:
//  - Added fetchSuggestedPhysicians() — calls GET /api/physicians/suggested
//    with the trial condition and a list of NPIs to exclude (already shown
//    in the main list). Returns up to 5 supporting/related specialists.
//  - All other helpers unchanged from v8.

import type { TrialFetchParams, TrialFetchResponse, SiteData, Trial } from "@/types/trial";
import type { AIInsightsData } from "@/types/physician";
import type {
  PhysicianSearchParams,
  SuggestedPhysicianParams,
  PhysicianFetchResponse,
  SuggestedPhysicianFetchResponse,
  LeadPayload,
  Physician,
  SelectedSite,
} from "@/types/physician";

const BASE_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").trim().replace(/\/+$/, "");

async function apiFetch<T>(
  path:     string,
  options?: RequestInit,
  signal?:  AbortSignal,
): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
    ...(signal ? { signal } : {}),
  });

  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`API ${res.status}: ${detail}`);
  }

  return res.json() as Promise<T>;
}

// ── Trials ────────────────────────────────────────────────────────────────────

export async function fetchTrials(
  params: TrialFetchParams,
  signal?: AbortSignal,
): Promise<TrialFetchResponse> {
  const qs = new URLSearchParams(
    Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => [k, String(v)]),
  ).toString();

  return apiFetch<TrialFetchResponse>(`/api/trials/?${qs}`, undefined, signal);
}

export async function fetchTrialSites(nctId: string, signal?: AbortSignal): Promise<SiteData> {
  return apiFetch<SiteData>(`/api/trials/${nctId}/sites`, undefined, signal);
}

// ── Physicians ────────────────────────────────────────────────────────────────

/**
 * Build a URLSearchParams string from PhysicianSearchParams.
 * Specialty fields are split on commas and appended as repeated params.
 */
function buildPhysicianParams(params: PhysicianSearchParams): string {
  const qs = new URLSearchParams();

  qs.append("lat",    String(params.lat));
  qs.append("lng",    String(params.lng));
  qs.append("radius", String(params.radius));

  const specialtyFields = [
    ["specialty",         params.specialty],
    ["initial_specialty", params.initial_specialty],
    ["user_specialty",    params.user_specialty],
  ] as const;

  for (const [key, value] of specialtyFields) {
    if (!value?.trim()) continue;
    value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
      .forEach((s) => qs.append(key, s));
  }

  return qs.toString();
}

export async function fetchPhysicians(
  params:  PhysicianSearchParams,
  signal?: AbortSignal,
): Promise<PhysicianFetchResponse> {
  return apiFetch<PhysicianFetchResponse>(
    `/api/physicians/search?${buildPhysicianParams(params)}`,
    undefined,
    signal,
  );
}

/**
 * Fetch suggested physicians related to the trial condition.
 * These are supporting/broader specialists (e.g. pediatricians for childhood
 * cancer) — NOT the same as the main search list.
 *
 * Pass exclude_npis (NPIs already in the main list) so the backend
 * de-duplicates automatically.
 */
export async function fetchSuggestedPhysicians(
  params:  SuggestedPhysicianParams,
  signal?: AbortSignal,
): Promise<SuggestedPhysicianFetchResponse> {
  const qs = new URLSearchParams();
  qs.append("lat",    String(params.lat));
  qs.append("lng",    String(params.lng));
  qs.append("radius", String(params.radius));

  if (params.condition?.trim()) {
    qs.append("condition", params.condition.trim());
  }

  // Send each excluded NPI as a repeated param
  (params.exclude_npis ?? []).forEach((npi) => qs.append("exclude_npis", npi));

  return apiFetch<SuggestedPhysicianFetchResponse>(
    `/api/physicians/suggested?${qs.toString()}`,
    undefined,
    signal,
  );
}

/**
 * Fetch the NUCC specialty list for a medical condition string.
 */
export async function getConditionSpecialties(
  condition: string,
  signal?: AbortSignal,
): Promise<string[]> {
  if (!condition?.trim()) return [];

  try {
    const encoded = encodeURIComponent(condition.trim());
    const response = await apiFetch<{ specialties: string[] }>(
      `/api/trials/condition/${encoded}/specialties`,
      undefined,
      signal,
    );
    return response.specialties ?? [];
  } catch (err) {
    console.warn(`Could not fetch specialties for condition "${condition}":`, err);
    return [];
  }
}

/**
 * Fire a physician search directly from a Trial object.
 */
export async function getPhysiciansForTrial(
  trial:            Trial,
  userSpecialty:    string | null = null,
  initialSpecialty: string | null = null,
  radius:           number        = 25,
  signal?:          AbortSignal,
): Promise<PhysicianFetchResponse | null> {
  const site =
    trial.locations?.find((s) => s.lat != null && s.lon != null) ??
    trial.locations?.[0];

  if (!site?.lat || !site?.lon) {
    console.warn(
      `[getPhysiciansForTrial] Trial ${trial.nctId} has no coordinates — skipping physician search.`,
    );
    return null;
  }

  const trialCondition = trial.conditions?.[0] ?? "";

  const params: PhysicianSearchParams = {
    lat:    site.lat,
    lng:    site.lon,
    radius,
    ...(trialCondition.trim()    ? { specialty:         trialCondition.trim()   } : {}),
    ...(initialSpecialty?.trim() ? { initial_specialty: initialSpecialty.trim() } : {}),
    ...(userSpecialty?.trim()    ? { user_specialty:    userSpecialty.trim()    } : {}),
  };

  return apiFetch<PhysicianFetchResponse>(
    `/api/physicians/search?${buildPhysicianParams(params)}`,
    undefined,
    signal,
  );
}

// ── Leads ─────────────────────────────────────────────────────────────────────

function buildLeadPayload(raw: LeadPayload): Record<string, unknown> {
  const name  = raw.name?.trim()  ?? "";
  const email = raw.email?.trim() ?? "";

  if (!name)  throw new Error("Lead submission requires a name.");
  if (!email) throw new Error("Lead submission requires an email address.");

  const payload: Record<string, unknown> = {
    name,
    email,
    lead_source: raw.lead_source ?? "Clinical Trial",
    company:     raw.company     ?? "Individual Physicians",
    auto:        raw.auto        ?? false,
  };

  if (raw.phone?.trim())          payload.phone           = raw.phone.trim();
  if (raw.npi?.trim())            payload.npi             = raw.npi.trim();
  if (raw.nct_id?.trim())         payload.nct_id          = raw.nct_id.trim();
  if (raw.site?.trim())           payload.site            = raw.site.trim();
  if (raw.message?.trim())        payload.message         = raw.message.trim();
  if (raw.title?.trim())          payload.title           = raw.title.trim();
  if (raw.physician_name?.trim()) payload.physician_name  = raw.physician_name.trim();

  return payload;
}

export async function submitLead(
  payload: LeadPayload,
): Promise<{ success: boolean; id?: string }> {
  return apiFetch("/api/leads", {
    method: "POST",
    body:   JSON.stringify(buildLeadPayload(payload)),
  });
}

export async function submitAutoLead(
  physician: Physician,
  site:      SelectedSite,
): Promise<{ success: boolean; id?: string }> {
  const payload: Record<string, unknown> = {
    name:           physician.name,
    company:        "Individual Physicians",
    lead_source:    "Clinical Trial",
    physician_name: physician.name,
    npi:            physician.npi,
    nct_id:         site.nct_id,
    auto:           true,
  };

  if (physician.phone?.trim())         payload.phone = physician.phone.trim();
  if (physician.taxonomy_desc?.trim()) payload.title = physician.taxonomy_desc.trim();
  if (site.facility?.trim())           payload.site  = site.facility.trim();

  return apiFetch("/api/leads", {
    method: "POST",
    body:   JSON.stringify(payload),
  });
}

interface FetchAIInsightsParams {
  npi:      string;
  name:     string;
  specialty: string;
  disease:  string;
}

export async function fetchAIInsights({
  npi,
  name,
  specialty,
  disease,
}: FetchAIInsightsParams): Promise<AIInsightsData> {
  const params = new URLSearchParams({
    name,
    specialty,
    disease,
  });

  const res = await fetch(
  `${BASE_URL}/api/physicians/insights/${encodeURIComponent(npi)}?${params.toString()}`
);

  if (!res.ok) {
    throw new Error(`AI Insights request failed: ${res.status}`);
  }

  return res.json();
}