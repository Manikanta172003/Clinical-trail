// types/physician.ts

export interface Physician {
  npi:                string;
  name:               string;
  taxonomy_desc?:     string | null;
  address?:           string | null;
  phone?:             string | null;
  lat?:               number | null;
  lng?:               number | null;
  distance_miles?:    number | null;
  matched_specialty?: string | null;
}

export interface SelectedSite {
  lat:       number;
  lng:       number;
  facility:  string | null;
  city:      string | null;
  state:     string | null;
  nct_id:    string;
  condition: string | null;
}

export interface PhysicianSearchParams {
  lat:                number;
  lng:                number;
  radius:             number;
  specialty?:         string;
  initial_specialty?: string;
  user_specialty?:    string;
}

export interface SuggestedPhysicianParams {
  lat:          number;
  lng:          number;
  radius:       number;
  condition?:   string;
  exclude_npis?: string[];
}

export interface PhysicianFetchResponse {
  physicians:         Physician[];
  total:              number;
  radius_miles:       number;
  zips_searched:      number;
  search_specialties: string[];
}

export type SuggestedPhysicianFetchResponse = PhysicianFetchResponse;

export interface LeadPayload {
  name:            string;
  email:           string;
  phone?:          string;
  npi?:            string;
  nct_id?:         string;
  site?:           string;
  message?:        string;
  company?:        string;
  lead_source?:    string;
  title?:          string;
  physician_name?: string;
  auto?:           boolean;
}

export type LeadModalTrigger = "card" | "load_more";

export type TrialStatus =
  | "Recruiting"
  | "Active, not recruiting"
  | "Not yet recruiting"
  | "Enrolling by invitation"
  | "Completed"
  | "Terminated"
  | "Suspended"
  | "Withdrawn"
  | "Unknown status";

export const STATUS_SORT_ORDER: Record<string, number> = {
  "Recruiting":              0,
  "Active, not recruiting":  1,
  "Enrolling by invitation": 2,
  "Not yet recruiting":      3,
  "Completed":               4,
  "Terminated":              5,
  "Suspended":               6,
  "Withdrawn":               7,
  "Unknown status":          8,
};

export function getStatusSortOrder(status: string): number {
  return STATUS_SORT_ORDER[status] ?? 8;
}

export const STATUS_COLORS: Record<string, { bg: string; fg: string; border: string }> = {
  "Recruiting":              { bg: "#dcfce7", fg: "#15803d", border: "#bbf7d0" },
  "Enrolling by invitation": { bg: "#d1fae5", fg: "#065f46", border: "#a7f3d0" },
  "Active, not recruiting":  { bg: "#dbeafe", fg: "#1d4ed8", border: "#bfdbfe" },
  "Not yet recruiting":      { bg: "#fef9c3", fg: "#a16207", border: "#fde68a" },
  "Completed":               { bg: "#f1f5f9", fg: "#475569", border: "#e2e8f0" },
  "Terminated":              { bg: "#fee2e2", fg: "#dc2626", border: "#fecaca" },
  "Suspended":               { bg: "#fef3c7", fg: "#b45309", border: "#fde68a" },
  "Withdrawn":               { bg: "#f3f4f6", fg: "#6b7280", border: "#e5e7eb" },
  "Unknown status":          { bg: "#f3f4f6", fg: "#9ca3af", border: "#e5e7eb" },
};

export function getStatusColors(status: string): { bg: string; fg: string; border: string } {
  return STATUS_COLORS[status] ?? { bg: "#f3f4f6", fg: "#6b7280", border: "#e5e7eb" };
}

// ── Publication type ──────────────────────────────────────────────────────────

export interface PublicationItem {
  title:                 string;
  year?:                 number | null;
  source:                string;
  // URL fields — best_url is always the right one to use for Open Publication
  best_url?:             string;
  semantic_scholar_url?: string;
  doi_url?:              string;
  pubmed_url?:           string;
  url?:                  string;  // legacy fallback
  verified_author_match?: boolean;
}

// ── AI Insights type ──────────────────────────────────────────────────────────

export interface AIInsightsData {
  npi:       string;
  name:      string;
  specialty: string;
  disease:   string;
  status:    "ready" | "loading" | "error";
  error?:    string;

  // Publications
  publication_count: number;
  publications:      PublicationItem[];
  top_topics:        string[];

  // Citations
  total_citations:        number;
  h_index:                number;
  i10_index:              number;
  citations_last_5_years: number;

  // Research areas
  research_areas: string[];

  // Awards (Phase 4 — empty array for now)
  awards: string[];

  // AI-generated summary
  ai_summary: string;
}
