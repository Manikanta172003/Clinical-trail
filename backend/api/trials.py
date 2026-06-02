"""
api/trials.py
REST endpoints for clinical-trial search and detail.

Routes (mounted at /api/trials in main.py):
  GET  /                                    — paginated, filtered trial search
  GET  /condition/{condition}/specialties   — map condition → NUCC specialties
  GET  /cities-by-state                     — ZIP-derived city lists for validation
  GET  /validate-city-state                 — server-side city/state validation
  GET  /{nct_id}/sites                      — site locations for a single trial
  GET  /{nct_id}                            — single trial detail (must come AFTER /sites)

Changes:
  - Change #1: city and state query params are validated before the search
    is executed; invalid values return HTTP 422 with a clear message.
  - Change #9: The endpoint now returns the full matched dataset in `trials`
    and sets `total` to the real count. The frontend's useTrials hook handles
    client-side pagination (PAGE_SIZE = 10). The `page` / `page_size` params
    are kept for backwards compatibility but the response always includes all
    matched trials so the frontend can paginate without re-fetching.
  - v3: /condition/{condition}/specialties now delegates to the taxonomy
    service's full 4-pass _condition_map_lookup() instead of a single
    dict-key lookup, so multi-word or mixed-case conditions from
    ClinicalTrials.gov are resolved correctly.
  - Phase 1 AI Search: condition query is corrected by ai_search_service
    before reaching ClinicalTrials.gov. Corrected query + original query
    returned in response so frontend can show "Showing results for X" banner.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from core.config import cfg
from services.clinicaltrials_api import (
    fetch_trials_with_filters,
    fetch_study_detail,
    validate_city,
    validate_state,
    STATE_ABBREV_TO_FULL,
)
from services import taxonomy as tax_service

# ── Phase 1: AI Search Intelligence ──────────────────────────────────────────
from services.ai_search_service import correct_query
# ─────────────────────────────────────────────────────────────────────────────


logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request / response models ─────────────────────────────────────────────────

class TrialSearchResponse(BaseModel):
    trials:           list[dict[str, Any]]
    total:            int
    page:             int
    page_size:        int
    # Phase 1 additions — frontend uses these for "Showing results for X" banner
    original_query:   str | None = None
    corrected_query:  str | None = None
    was_corrected:    bool       = False


class SiteLocation(BaseModel):
    facility: str | None
    city:     str | None
    state:    str | None
    country:  str | None
    status:   str | None
    lat:      float | None
    lon:      float | None


class TrialSitesResponse(BaseModel):
    title: str
    sites: list[SiteLocation]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=TrialSearchResponse)
async def search_trials(
    condition: str = Query(...,  description="Disease / condition to search for"),
    status:    str | None = Query(None, description="e.g. RECRUITING"),
    phase:     str | None = Query(None, description="e.g. PHASE2"),
    city:      str | None = Query(None, description="Filter by city"),
    state:     str | None = Query(None, description="2-letter state abbreviation or full name, e.g. CT or Connecticut"),
    us_only:   bool       = Query(False, description="Restrict to US locations"),
    page:      int        = Query(1,  ge=1,  description="1-based page number"),
    page_size: int        = Query(10, ge=1, le=500, description="Results per page — set high to retrieve full dataset"),
    response:  Response   = None,
) -> TrialSearchResponse:
    # ── HTTP Caching: Cache search results for 5 minutes ──────────────────────
    if response:
        response.headers["Cache-Control"] = "private, max-age=300"

    # ── Validate city and state before touching the service ───────────────────
    city_ok,  city_reason  = validate_city(city)
    state_ok, state_reason = validate_state(state)

    if not city_ok:
        raise HTTPException(status_code=422, detail=f"Invalid city filter — {city_reason}")
    if not state_ok:
        raise HTTPException(status_code=422, detail=f"Invalid state filter — {state_reason}")

    # ── Phase 1: AI Query Correction ──────────────────────────────────────────
    # Correct spelling mistakes, layman terms, abbreviations BEFORE search.
    # "hart attak" → "heart attack"
    # "sugar disease" → "diabetes mellitus"
    # "HFpEF" → "heart failure with preserved ejection fraction"
    query_result      = await correct_query(condition)
    search_condition  = query_result.corrected_query
    # ─────────────────────────────────────────────────────────────────────────

    logger.info(
        "Trial search | original=%r → search_condition=%r | corrected=%s layer=%s",
        condition, search_condition,
        query_result.was_corrected, query_result.correction_layer,
    )

    filters: dict[str, Any] = {
        "condition": search_condition,   # ← corrected query used here
        "status":    status,
        "phase":     phase,
        "city":      city,
        "state":     state,
        "us_only":   us_only,
    }

    offset = (page - 1) * page_size

    try:
        trials, total = fetch_trials_with_filters(
            filters=filters,
            limit=page_size,
            offset=offset,
        )
    except Exception as exc:
        logger.exception("Error fetching trials: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Upstream ClinicalTrials.gov request failed",
        ) from exc

    return TrialSearchResponse(
        trials=trials,
        total=total,
        page=page,
        page_size=page_size,
        # Phase 1 — pass correction info to frontend
        original_query=query_result.original_query,
        corrected_query=query_result.corrected_query,
        was_corrected=query_result.was_corrected,
    )


@router.get("/condition/{condition}/specialties")
async def get_condition_specialties(
    condition: str,
    response:  Response = None,
) -> dict[str, Any]:
    """
    Map a medical condition string to relevant NUCC physician specialties.

    Used by the frontend's getConditionSpecialties() call immediately before
    launching a physician search from a trial site, so that clicking
    "Find physicians near this site" on a trial with condition
    "Metastatic High Grade Sarcoma" still finds Medical Oncologists and
    Orthopaedic Surgeons rather than returning zero results.

    Resolution uses the taxonomy service's full 4-pass _condition_map_lookup():
      1. Exact key match
      2. Prefix match
      3. Substring key match  (new — handles multi-word trial conditions)
      4. Token overlap        (new — handles token-level partial matches)

    This replaces the previous single dict-key lookup which failed whenever
    ClinicalTrials.gov returned a condition string that didn't exactly match
    a key in CONDITION_MAP (e.g. mixed case, extra words, phase suffix).

    HTTP Caching: 24 hours — condition→specialty mappings are static.

    Example:
      GET /api/trials/condition/Metastatic%20High%20Grade%20Sarcoma/specialties
      → {
          "condition": "Metastatic High Grade Sarcoma",
          "specialties": ["Medical Oncology", "General Surgery"],
          "count": 2
        }

    v4 changes:
      - Now handles multiple conditions separated by commas or "and" (e.g.
        "Neurology, Interventional Cardiology, Cardiovascular Disease").
        Each condition is resolved independently and results are merged.
    """
    if response:
        response.headers["Cache-Control"] = "public, max-age=86400"

    if not condition or not condition.strip():
        raise HTTPException(status_code=422, detail="Condition cannot be empty")

    clean = condition.strip()

    # Split on common delimiters: commas, " and ", " & "
    # This handles cases like "Neurology, Interventional Cardiology, Cardiovascular Disease"
    parts = re.split(r',\s*|\s+and\s+|\s+&\s+', clean, flags=re.IGNORECASE)

    all_specialties: list[str] = []
    seen: set[str] = set()

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Try original case first, then lowercase as fallback
        hits = tax_service._condition_map_lookup(part)
        if not hits:
            hits = tax_service._condition_map_lookup(part.lower())

        if hits:
            for specialty in hits:
                if specialty not in seen:
                    seen.add(specialty)
                    all_specialties.append(specialty)

    specialties = all_specialties


    logger.info(
        "Condition specialties | condition=%r → specialties=%s",
        clean, specialties or "none",
    )

    return {
        "condition":  clean,
        "specialties": specialties,
        "count":      len(specialties),
    }


@router.get("/cities-by-state", response_model=dict[str, list[str]])
async def get_cities_by_state(response: Response = None) -> dict[str, list[str]]:
    """
    Return a mapping of US states → valid cities for frontend validation.

    Frontend uses this to prevent searches with a city/state mismatch
    (e.g. "Boston" + "California").

    HTTP Caching: 30 days — city lists change rarely.
    """
    if response:
        response.headers["Cache-Control"] = "public, max-age=3600"

    try:
        from services import zip_database
        if not zip_database.is_ready():
            zip_database.wait_for_ready(cfg.ZIP_DB_WAIT)

        return zip_database.get_cities_by_state()
    except Exception as exc:
        logger.exception("Error building cities-by-state: %s", exc)
        return {}


@router.get("/validate-city-state")
async def validate_city_state_endpoint(
    city:  str | None = Query(None, description="City name to validate"),
    state: str | None = Query(None, description="2-letter state abbreviation"),
) -> dict[str, Any]:
    """
    Validate that a city belongs to the selected state.
    Returns { isValid: bool, error?: string }.
    """
    if not city and not state:
        return {"isValid": True}
    if city and not state:
        return {"isValid": True}
    if not city and state:
        return {"isValid": True}

    if city and state:
        try:
            from services import zip_database
            if not zip_database.is_ready():
                zip_database.wait_for_ready(cfg.ZIP_DB_WAIT)

            city_normalized  = city.strip().lower()
            state_normalized = state.upper()

            cities_by_state = zip_database.get_cities_by_state()
            state_cities = [c.lower() for c in cities_by_state.get(state_normalized, [])]

            if city_normalized in state_cities:
                return {"isValid": True}

            state_full = STATE_ABBREV_TO_FULL.get(state_normalized.lower(), state)
            return {
                "isValid": False,
                "error": f'Invalid city/state combination: "{city}" is not a city in {state_full}',
            }
        except Exception as exc:
            logger.warning("Error validating city/state: %s", exc)
            return {"isValid": True}

    return {"isValid": True}


# ── Generic trial detail endpoints (must come AFTER all specific routes) ──────

@router.get("/{nct_id}/sites", response_model=TrialSitesResponse)
async def get_trial_sites(nct_id: str, response: Response = None) -> TrialSitesResponse:
    """
    Return the title and all site locations for a single trial.
    HTTP Caching: 24 hours.
    """
    if response:
        response.headers["Cache-Control"] = "public, max-age=86400"

    try:
        data = fetch_study_detail(nct_id)
    except Exception as exc:
        logger.exception("Error fetching sites for %s: %s", nct_id, exc)
        raise HTTPException(status_code=502, detail=f"Could not fetch trial {nct_id}") from exc

    if not data:
        raise HTTPException(status_code=404, detail=f"Trial {nct_id} not found")

    protocol       = data.get("protocolSection", {})
    id_module      = protocol.get("identificationModule", {})
    contacts       = protocol.get("contactsLocationsModule", {})
    status_mod     = protocol.get("statusModule", {})
    overall_status = status_mod.get("overallStatus")

    title = id_module.get("briefTitle") or nct_id

    raw_locations = contacts.get("locations") or []
    sites: list[SiteLocation] = []
    for loc in raw_locations:
        geo      = loc.get("geoPoint") or {}
        s_status = loc.get("recruitmentStatus") or overall_status
        sites.append(SiteLocation(
            facility = loc.get("facility"),
            city     = loc.get("city"),
            state    = loc.get("state"),
            country  = loc.get("country"),
            status   = s_status,
            lat      = geo.get("lat"),
            lon      = geo.get("lon"),
        ))

    return TrialSitesResponse(title=title, sites=sites)


@router.get("/{nct_id}")
async def get_trial(nct_id: str, response: Response = None) -> dict[str, Any]:
    """
    Return full trial details (all protocol sections).
    HTTP Caching: 24 hours.
    """
    if response:
        response.headers["Cache-Control"] = "public, max-age=86400"

    try:
        data = fetch_study_detail(nct_id)
    except Exception as exc:
        logger.exception("Error fetching trial %s: %s", nct_id, exc)
        raise HTTPException(status_code=502, detail=f"Could not fetch trial {nct_id}") from exc

    if not data:
        raise HTTPException(status_code=404, detail=f"Trial {nct_id} not found")

    return data
