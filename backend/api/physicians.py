from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional, List

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from core.config import cfg
from core.validation import validate_lat_lng, validate_radius
from core.helpers import sanitise
from services import nppes, zip_database, taxonomy as tax_service
from services import ai_cache_service as cache
from services.physician_insights_service import enrich_physician
from services.background_enrichment import enrich_batch

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Tuning constants ──────────────────────────────────────────────────────────
_CENTROID_BUFFER_MILES  = 10.0
MAX_SUGGESTED           = 5
MAX_CONCURRENT_NPPES    = 12
MAX_DENSE_ZIPS          = 8
_MIN_PHYSICIANS_THRESHOLD = 5

_DENSE_SPECIALTIES = frozenset({
    "Medical Oncology",
    "Hematology & Oncology",
    "Hematology",
    "Radiation Oncology",
    "Internal Medicine",
    "Family Medicine",
    "General Practice",
    "Cardiovascular Disease",
    "Neurology",
    "Psychiatry",
    "Dermatology",
    "Gastroenterology",
    "Pulmonary Disease",
    "Rheumatology",
    "Nephrology",
    "Infectious Disease",
    "Endocrinology, Diabetes & Metabolism",
    "Obstetrics & Gynecology",
    "Orthopaedic Surgery",
    "Urology",
    "Ophthalmology",
    "Otolaryngology",
})

_EXCLUDED_TAXONOMY_KEYWORDS = [
    "nurse", "nursing", "registered nurse", "licensed practical",
    "licensed vocational", "nurse practitioner", "clinical nurse",
    "certified nurse", "pharmacist", "pharmacy", "medical assistant",
    "physician assistant", "technician", "technologist", "therapist",
    "physical therapy", "occupational therapy", "speech", "audiologist",
    "optician", "dietitian", "nutritionist", "social worker", "counselor",
    "case manager", "health educator", "community health", "home health",
    "aide", "assistant", "coordinator", "administrator", "dental",
    "dentist", "orthodontist", "podiatrist", "chiropractor", "acupuncturist",
    "midwife", "doula", "paramedic", "emergency medical", "phlebotomist",
    "radiology technician", "radiology technologist",
]


def _is_excluded_provider(taxonomy_desc: Optional[str]) -> bool:
    if not taxonomy_desc:
        return False
    lower = taxonomy_desc.lower()
    return any(kw in lower for kw in _EXCLUDED_TAXONOMY_KEYWORDS)


def _haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _resolve_specialty_input(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    clean = sanitise(raw, cfg.MAX_DESC_LEN)
    if not clean:
        return []
    return tax_service.resolve_with_broader(clean)


# ── Async NPPES fetch helpers ─────────────────────────────────────────────────

async def _fetch_nppes_async(
    semaphore: asyncio.Semaphore,
    zipcode: str,
    desc: str,
) -> list[dict]:
    async with semaphore:
        loop = asyncio.get_running_loop()
        rows, _ = await loop.run_in_executor(
            None,
            lambda: nppes.fetch_with_retry({
                "postal_code":          zipcode,
                "taxonomy_description": desc,
                "limit":                50,
            }),
        )
        results = []
        for row in rows:
            parsed = nppes.parse_physician(row)
            if parsed and not _is_excluded_provider(parsed.get("taxonomy_desc")):
                parsed["matched_specialty"] = desc
                results.append(parsed)
        return results


async def _run_parallel_nppes(
    zip_batch: list[str],
    query_descriptions: list[str],
    early_stop_threshold: int,
) -> list[dict]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_NPPES)
    seen_npis: set[str] = set()
    raw_physicians: list[dict] = []
    lock = asyncio.Lock()

    dense_zip_counts: dict[str, int] = {d: 0 for d in query_descriptions}

    async def _task(zipcode: str, desc: str) -> None:
        async with lock:
            if len(raw_physicians) >= early_stop_threshold:
                return
            if desc in _DENSE_SPECIALTIES:
                if dense_zip_counts[desc] >= MAX_DENSE_ZIPS:
                    return
                dense_zip_counts[desc] += 1

        physicians = await _fetch_nppes_async(semaphore, zipcode, desc)

        async with lock:
            for p in physicians:
                if p["npi"] not in seen_npis:
                    seen_npis.add(p["npi"])
                    raw_physicians.append(p)

    tasks = []
    for zipcode in zip_batch:
        for desc in query_descriptions:
            tasks.append(_task(zipcode, desc))

    await asyncio.gather(*tasks, return_exceptions=True)

    return raw_physicians


# ── Core search logic ─────────────────────────────────────────────────────────

async def _run_physician_search(
    lat: float,
    lng: float,
    radius: float,
    query_descriptions: list[str],
    max_display: int,
) -> dict:
    if not query_descriptions:
        return {
            "physicians":         [],
            "total":              0,
            "radius_miles":       radius,
            "zips_searched":      0,
            "search_specialties": [],
        }

    if not zip_database.is_ready():
        zip_database.wait_for_ready(cfg.ZIP_DB_WAIT)

    nearby_zips = zip_database.find_zips_in_radius(lat, lng, radius)
    if not nearby_zips:
        return {
            "physicians":         [],
            "total":              0,
            "radius_miles":       radius,
            "zips_searched":      0,
            "search_specialties": query_descriptions,
        }

    zip_batch = nearby_zips[: cfg.MAX_ZIP_QUERIES]
    early_stop_threshold = max_display * 5

    raw_physicians = await _run_parallel_nppes(zip_batch, query_descriptions, early_stop_threshold)

    if not raw_physicians:
        return {
            "physicians":         [],
            "total":              0,
            "radius_miles":       radius,
            "zips_searched":      len(zip_batch),
            "search_specialties": query_descriptions,
        }

    for p in raw_physicians:
        if p.get("zip"):
            z_lat, z_lng = zip_database.get_zip_coords(p["zip"])
            if z_lat is not None:
                p["_zip_lat"] = z_lat
                p["_zip_lng"] = z_lng
                if p["lat"] is None:
                    p["lat"] = z_lat
                    p["lng"] = z_lng

    centroid_threshold = radius + _CENTROID_BUFFER_MILES
    pre_filtered = [
        p for p in raw_physicians
        if p.get("_zip_lat") is not None
        and _haversine_miles(lat, lng, p["_zip_lat"], p["_zip_lng"]) <= centroid_threshold
    ]

    if not pre_filtered:
        return {
            "physicians":         [],
            "total":              0,
            "radius_miles":       radius,
            "zips_searched":      len(zip_batch),
            "search_specialties": query_descriptions,
        }

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: nppes.batch_geocode_for_display(pre_filtered))

    for p in pre_filtered:
        if p.get("lat") is not None and p.get("lng") is not None:
            p["distance_miles"] = round(_haversine_miles(lat, lng, p["lat"], p["lng"]), 1)
        elif p.get("_zip_lat") is not None:
            p["distance_miles"] = round(_haversine_miles(lat, lng, p["_zip_lat"], p["_zip_lng"]), 1)

    precise = [
        p for p in pre_filtered
        if p.get("distance_miles") is not None and p["distance_miles"] <= radius
    ]
    precise.sort(key=lambda p: p["distance_miles"])

    nppes.apply_coord_jitter(precise)
    top = precise[:max_display]

    for p in top:
        p.pop("_geocoded", None)
        p.pop("_zip_lat", None)
        p.pop("_zip_lng", None)

    return {
        "physicians":         top,
        "total":              len(precise),
        "radius_miles":       radius,
        "zips_searched":      len(zip_batch),
        "search_specialties": query_descriptions,
    }


# ── Auto-relax helper ─────────────────────────────────────────────────────────

async def _search_with_auto_relax(
    lat: float,
    lng: float,
    radius: float,
    query_descriptions: list[str],
    max_display: int,
) -> dict:
    search_result = await _run_physician_search(lat, lng, radius, query_descriptions, max_display)

    if search_result["total"] >= _MIN_PHYSICIANS_THRESHOLD:
        return search_result

    # ── Level 1: broaden each specialty to its parent ────────────────────────
    broader_l1: list[str] = []
    seen_l1: set[str] = set(query_descriptions)
    for desc in query_descriptions:
        for broader in tax_service.SPECIALTY_HIERARCHY.get(desc, []):
            if broader not in seen_l1:
                seen_l1.add(broader)
                broader_l1.append(broader)

    if broader_l1:
        combined_l1 = query_descriptions + broader_l1
        result_l1 = await _run_physician_search(lat, lng, radius, combined_l1, max_display)
        if result_l1["total"] >= _MIN_PHYSICIANS_THRESHOLD:
            result_l1["filter_relaxed"] = "broad_specialty"
            return result_l1

    # ── Level 2: use reverse hierarchy (siblings/children) ──────────────────
    broader_l2: list[str] = []
    seen_l2: set[str] = seen_l1.copy()
    for desc in query_descriptions:
        for sibling in tax_service._BROADER_TO_SPECIFIC.get(desc, []):
            if sibling not in seen_l2:
                seen_l2.add(sibling)
                broader_l2.append(sibling)

    if broader_l2:
        combined_l2 = list(seen_l1) + broader_l2
        result_l2 = await _run_physician_search(lat, lng, radius, combined_l2, max_display)
        if result_l2["total"] >= _MIN_PHYSICIANS_THRESHOLD:
            result_l2["filter_relaxed"] = "broad_specialty"
            return result_l2

    # ── Level 3: generic domain fallback ─────────────────────────────────────
    fallback_condition = " ".join(query_descriptions)
    fallback_specialties = tax_service.get_fallback_specialties(fallback_condition)

    if fallback_specialties:
        combined_l3 = list(seen_l2) + [s for s in fallback_specialties if s not in seen_l2]
        result_l3 = await _run_physician_search(lat, lng, radius, combined_l3, max_display)
        if result_l3["total"] > 0:
            result_l3["filter_relaxed"] = "general_specialty"
            return result_l3

    # ── Level 4: absolute last resort — Internal Medicine ───────────────────
    result_l4 = await _run_physician_search(
        lat, lng, radius, ["Internal Medicine"], max_display
    )
    result_l4["filter_relaxed"] = "internal_medicine_fallback"
    return result_l4


# ── /search ───────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_physicians(
    request:           Request,
    background_tasks:  BackgroundTasks,
    lat:               float               = Query(...,  description="Latitude of trial site"),
    lng:               float               = Query(...,  description="Longitude of trial site"),
    radius:            float               = Query(25.0, description="Search radius in miles (1–100)"),
    specialty:         Optional[List[str]] = Query(None, description="Resolved from trial condition — multiple allowed"),
    initial_specialty: Optional[List[str]] = Query(None, description="Specialty from user's first search — multiple allowed"),
    user_specialty:    Optional[List[str]] = Query(None, description="Extra specialty entered by user — multiple allowed"),
    trial_status:      Optional[str]       = Query(None, description="Trial enrollment status e.g. Recruiting, Completed"),
    condition:         Optional[str]       = Query(None, description="Trial condition — passed for background AI enrichment"),
    response:          Response            = None,
):
    if response:
        response.headers["Cache-Control"] = "private, max-age=600"

    try:
        lat, lng = validate_lat_lng(lat, lng)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        radius = validate_radius(radius)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    resolved_groups: list[list[str]] = []

    def _collect(raw: Optional[str]) -> None:
        resolved = _resolve_specialty_input(raw)
        if resolved:
            resolved_groups.append(resolved)

    for s in (initial_specialty or []):
        _collect(s)
    for s in (user_specialty or []):
        _collect(s)
    if not resolved_groups:
        for s in (specialty or []):
            _collect(s)

    descriptions: list[str] = []
    seen_descs:   set[str]  = set()

    def _add(desc: str) -> None:
        if not desc or desc in seen_descs or len(descriptions) >= cfg.MAX_DESC_COUNT:
            return
        seen_descs.add(desc)
        descriptions.append(desc)

    for group in resolved_groups:
        if group:
            _add(group[0])
    for group in resolved_groups:
        for desc in group[1:]:
            _add(desc)

    query_descriptions = descriptions[: cfg.MAX_TAX_QUERIES]

    if not query_descriptions:
        return {
            "physicians":         [],
            "total":              0,
            "radius_miles":       radius,
            "zips_searched":      0,
            "search_specialties": [],
            "trial_status":       trial_status or "unknown",
            "filter_relaxed":     None,
        }

    logger.info(
        "Physician /search | lat=%.4f lng=%.4f radius=%.1fmi "
        "initial=%r user=%r specialty=%r → descriptions=%s trial_status=%r",
        lat, lng, radius,
        initial_specialty, user_specialty, specialty,
        query_descriptions, trial_status,
    )

    # ── Run search ────────────────────────────────────────────────────────────
    search_result = await _search_with_auto_relax(lat, lng, radius, query_descriptions, cfg.MAX_DISPLAY)

    search_result["trial_status"]   = trial_status or "unknown"
    search_result["filter_relaxed"] = search_result.get("filter_relaxed")

    # ── Trigger background AI enrichment for all visible physicians ───────────
    # Build disease context — never empty so enrichment never skips
    disease_ctx = (
        (condition or "").strip()
        or (specialty[0] if specialty else "")
        or (initial_specialty[0] if initial_specialty else "")
        or "clinical_trial"
    )
    if search_result.get("physicians"):
        background_tasks.add_task(
            enrich_batch,
            search_result["physicians"],
            disease_ctx,
            getattr(cfg, "GROQ_API_KEY", ""),
        )

    return search_result


# ── /suggested ────────────────────────────────────────────────────────────────

@router.get("/suggested")
async def suggested_physicians(
    request:      Request,
    lat:          float               = Query(...,  description="Latitude of trial site"),
    lng:          float               = Query(...,  description="Longitude of trial site"),
    radius:       float               = Query(25.0, description="Search radius in miles (1–100)"),
    condition:    Optional[str]       = Query(None, description="Trial condition — drives specialty resolution"),
    exclude_npis: Optional[List[str]] = Query(None, description="NPIs already shown in main list"),
    response:     Response            = None,
):
    if response:
        response.headers["Cache-Control"] = "private, max-age=600"

    try:
        lat, lng = validate_lat_lng(lat, lng)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        radius = validate_radius(radius)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if not condition or not condition.strip():
        return {
            "physicians":         [],
            "total":              0,
            "radius_miles":       radius,
            "zips_searched":      0,
            "search_specialties": [],
            "filter_relaxed":     None,
        }

    clean_condition = sanitise(condition.strip(), cfg.MAX_DESC_LEN)
    all_resolved    = tax_service.resolve_with_broader(clean_condition)

    if not all_resolved:
        all_resolved = tax_service.get_fallback_specialties(clean_condition)
        if all_resolved:
            logger.info(
                "Suggested /suggested | condition=%r resolved via fallback → %s",
                condition, all_resolved,
            )

    if not all_resolved:
        all_resolved = ["Internal Medicine"]
        logger.info(
            "Suggested /suggested | condition=%r fell back to Internal Medicine",
            condition,
        )

    query_descriptions = all_resolved[: cfg.MAX_TAX_QUERIES + 2]
    exclude_set        = set(exclude_npis or [])

    logger.info(
        "Physician /suggested | lat=%.4f lng=%.4f radius=%.1fmi "
        "condition=%r → descriptions=%s (excluding %d npis)",
        lat, lng, radius, condition, query_descriptions, len(exclude_set),
    )

    suggested_result = await _run_physician_search(lat, lng, radius, query_descriptions, MAX_SUGGESTED * 4)

    filtered     = [p for p in suggested_result["physicians"] if p["npi"] not in exclude_set]
    filtered     = filtered[:MAX_SUGGESTED]
    used_fallback = (
        all_resolved != tax_service.resolve_with_broader(clean_condition)
        if all_resolved else False
    )

    return {
        "physicians":         filtered,
        "total":              max(0, suggested_result["total"] - len(exclude_set)),
        "radius_miles":       suggested_result["radius_miles"],
        "zips_searched":      suggested_result["zips_searched"],
        "search_specialties": suggested_result["search_specialties"],
        "filter_relaxed":     "condition_fallback" if used_fallback else None,
    }


# ── /insights/{npi} ───────────────────────────────────────────────────────────

@router.get("/insights/{npi}")
async def get_physician_insights(
    npi:       str,
    disease:   str = Query("",  description="Trial condition for context"),
    name:      str = Query("",  description="Physician name for API lookups"),
    specialty: str = Query("",  description="Physician specialty for search"),
):
    """
    Return AI-enriched insights for a physician.

    Flow:
      1. Check cache (key = npi + disease)  →  return instantly if hit
      2. Cache miss  →  run enrichment now  →  return result
      3. Error       →  return { status: "error" }
    """
    if not npi:
        raise HTTPException(status_code=422, detail="npi is required")

    # 1. Cache hit
    cached = cache.get(npi, disease)
    if cached:
        logger.debug("Insights cache HIT  NPI=%s disease=%r", npi, disease)
        return cached

    # 2. Cache miss — enrich now
    logger.info("Insights cache MISS NPI=%s disease=%r — enriching now", npi, disease)

    if not name:
        raise HTTPException(
            status_code=422,
            detail="name is required when insights are not yet cached",
        )

    try:
        data = await enrich_physician(
            npi          = npi,
            name         = name,
            specialty    = specialty or "Physician",
            disease      = disease   or "clinical trial",
            groq_api_key = getattr(cfg, "GROQ_API_KEY", ""),
        )
        cache.set(npi, disease, data)
        return data

    except Exception as exc:
        logger.error("Insights enrichment failed NPI=%s: %s", npi, exc)
        return {
            "npi":    npi,
            "status": "error",
            "error":  "Unable to load AI insights at this time. Please try again.",
        }