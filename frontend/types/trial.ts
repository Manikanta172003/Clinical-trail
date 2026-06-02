// types/trial.ts
export interface Trial {
  nctId:             string;
  title:             string;
  status:            string | null;
  description:       string | null;
  conditions:        string[];
  sponsor:           string | null;
  phases:            string[];
  locations:         TrialLocation[];
  inclusionCriteria: string | null;
  exclusionCriteria: string | null;
  pointOfContact:    PointOfContact | null;
}
export interface TrialLocation {
  facility: string | null;
  city:     string | null;
  state:    string | null;
  country:  string | null;
  status:   string | null;
  lat:      number | null;
  lon:      number | null;
}
export interface PointOfContact {
  name:  string | null;
  role:  string | null;
  phone: string | null;
  email: string | null;
}
export interface TrialSearchFilters {
  condition: string;
  city:      string;
  state:     string;
  status:    string;
  phase:     string;
}
export interface TrialFetchParams {
  condition:  string;
  city?:      string | null;
  state?:     string | null;
  status?:    string;
  phase?:     string;
  us_only?:   boolean;
  page_size?: number;
  page?:      number;
}
export interface TrialFetchResponse {
  trials:          Trial[];
  total:           number;
  page:            number;
  page_size:       number;
  // Phase 1 AI Search — corrected query fields
  original_query:  string | null;
  corrected_query: string | null;
  was_corrected:   boolean;
}
export interface SiteData {
  title: string;
  sites: TrialLocation[];
}
export type TrialSite = TrialLocation;
