"""
semantic_scholar_service.py — v4 (Metrics + Verification Only)
==============================================================
In the new architecture, Semantic Scholar is used for:
  1. Citation metrics (h-index, total citations)
  2. Verifying a PubMed match via author ID lookup
  3. Enriching publications with Semantic Scholar URLs

It is NO LONGER the primary source for publications.
PubMed is now primary. This service supplements PubMed data.

Medical verification using fieldsOfStudy is retained to
prevent false positive matches on common names.
"""

import asyncio
import httpx
import logging
import re

logger = logging.getLogger(__name__)

SS_AUTHOR_SEARCH  = "https://api.semanticscholar.org/graph/v1/author/search"
SS_AUTHOR_DETAILS = "https://api.semanticscholar.org/graph/v1/author"

HTTP_TIMEOUT = 20
MAX_RETRIES  = 2
RETRY_DELAY  = 2.0

# Non-medical field keywords for rejection
NON_MEDICAL_TITLE_KW = [
    "hvac", "truss", "concrete", "caisson", "anisotropic", "shell theory",
    "elastic constants", "sandwich plate", "cylindrical shell", "floating roof",
    "penetration depth", "construction", "steel frame", "moment frame",
    "natural gas", "methane", "emissions", "electricity generation",
    "carbon capture", "geospatial", "solvent extraction",
    "organic droplet", "radioactivity", "recycling initiative",
    "robotics", "algorithm", "software", "compiler", "neural network",
    "machine learning applied to materials",
]

MEDICAL_TITLE_KW = [
    "patient", "clinical", "trial", "cancer", "tumor", "disease",
    "treatment", "therapy", "surgery", "diagnosis", "drug", "hospital",
    "cardiac", "heart", "blood", "immune", "infection", "syndrome",
    "disorder", "medical", "health", "dose", "mortality", "survival",
    "cohort", "randomized", "placebo", "efficacy", "outcome",
    "biomarker", "imaging", "pharmaceutical", "pathology",
]


def clean_name(raw_name: str) -> str:
    name = raw_name
    for cred in [
        ", M.D.", ",M.D.", " M.D.", ", MD", ",MD", " MD",
        ", DO", ",DO", " DO", ", Ph.D.", ", PhD", ", M.D", ",M.D",
        ",FACC", ", FACC", ",FSCAI", ", FSCAI", ",FACP", ", FACP",
        ",FAHA", ", FAHA", ",FACS", ", FACS", ",FACOG", ", FACOG",
    ]:
        name = name.replace(cred, "")
    name = name.replace(",", "").strip()
    return " ".join(w.capitalize() for w in name.split())


# Keep as module-level export for backward compatibility
_clean_physician_name = clean_name


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _author_matches(physician_clean: str, candidate_name: str) -> bool:
    p_parts = physician_clean.lower().split()
    c_parts = candidate_name.lower().split()
    if not p_parts or not c_parts:
        return False

    p_last  = _normalize(p_parts[-1])
    p_first = _normalize(p_parts[0])
    c_norm  = [_normalize(x) for x in c_parts if x]

    if not any(p_last == part for part in c_norm):
        return False

    return any(
        part == p_first or part == p_first[0]
        for part in c_norm
        if part != p_last
    )


def _is_medical_author(papers: list[dict]) -> bool:
    """Verify author is medical using paper titles and fieldsOfStudy."""
    if not papers:
        return True

    medical_count     = 0
    non_medical_count = 0

    for paper in papers[:12]:
        title  = (paper.get("title") or "").lower()
        fields = [f.lower() for f in (paper.get("fieldsOfStudy") or [])]

        has_medical_field = any(
            f in {"medicine", "biology", "health sciences", "pharmacology"}
            for f in fields
        )
        has_non_medical_field = any(
            f in {"engineering", "materials science", "computer science",
                  "mathematics", "physics", "geology"}
            for f in fields
        )

        has_medical_title     = any(kw in title for kw in MEDICAL_TITLE_KW)
        has_non_medical_title = any(kw in title for kw in NON_MEDICAL_TITLE_KW)

        if has_medical_field or has_medical_title:
            medical_count += 1
        if has_non_medical_field or has_non_medical_title:
            non_medical_count += 1

    # Reject if clearly non-medical
    if non_medical_count >= 3 and medical_count == 0:
        logger.info("SS: rejected non-medical author (non_medical=%d medical=%d)",
                    non_medical_count, medical_count)
        return False

    if non_medical_count > medical_count * 2 and non_medical_count >= 3:
        logger.info("SS: rejected predominantly non-medical (non_medical=%d medical=%d)",
                    non_medical_count, medical_count)
        return False

    return True


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
) -> httpx.Response | None:
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                return None
            return resp
        except Exception as exc:
            logger.warning("SS request error attempt %d: %s", attempt + 1, exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
    return None


async def semantic_scholar_metrics(
    name: str,
    specialty: str,
    client: httpx.AsyncClient,
) -> dict:
    """
    Get citation metrics from Semantic Scholar.
    Returns h-index, total citations.
    Does NOT return publications (PubMed is primary for that).
    """
    clean = clean_name(name)
    logger.info("SS metrics lookup | clean_name=%r", clean)

    # Search for author
    resp = await _get_with_retry(client, SS_AUTHOR_SEARCH, {
        "query":  clean,
        "limit":  5,
        "fields": "authorId,name,paperCount,citationCount,hIndex",
    })

    if resp is None or resp.status_code != 200:
        logger.info("SS: no response for %r", clean)
        return _empty_ss()

    candidates = resp.json().get("data", [])
    best = None

    # Find best matching author
    p_norm = _normalize(clean)
    for c in candidates:
        if _normalize(c.get("name", "")) == p_norm:
            best = c
            break
    if not best:
        for c in candidates:
            if _author_matches(clean, c.get("name", "")):
                best = c
                break

    if not best:
        logger.info("SS: no author match for %r", clean)
        return _empty_ss()

    author_id = best.get("authorId")
    if not author_id:
        return _empty_ss()

    logger.info("SS: matched %r → %r (id=%s)", clean, best.get("name"), author_id)

    # Fetch full details for medical verification
    details_resp = await _get_with_retry(
        client,
        f"{SS_AUTHOR_DETAILS}/{author_id}",
        {"fields": "name,paperCount,citationCount,hIndex,papers.title,papers.fieldsOfStudy"},
    )

    if details_resp is None or details_resp.status_code != 200:
        # Return basic metrics from search result
        return {
            "total_citations": best.get("citationCount", 0),
            "h_index":         best.get("hIndex", 0),
            "paper_count":     best.get("paperCount", 0),
            "author_id":       author_id,
        }

    details = details_resp.json()
    papers  = details.get("papers", [])

    # Medical verification
    if not _is_medical_author(papers):
        logger.info("SS: rejected non-medical author %r for %r",
                    details.get("name"), clean)
        return _empty_ss()

    logger.info(
        "SS metrics | author=%r | citations=%d | h_index=%d",
        details.get("name"),
        details.get("citationCount", 0),
        details.get("hIndex", 0),
    )

    return {
        "total_citations": details.get("citationCount", 0),
        "h_index":         details.get("hIndex", 0),
        "paper_count":     details.get("paperCount", 0),
        "author_id":       author_id,
    }


# Legacy function kept for backward compatibility
async def semantic_scholar_lookup(
    name: str,
    specialty: str,
    disease: str,
    client: httpx.AsyncClient,
) -> dict:
    """Legacy wrapper — now returns metrics only, no publications."""
    metrics = await semantic_scholar_metrics(name, specialty, client)
    return {
        "publication_count": metrics.get("paper_count", 0),
        "total_citations":   metrics.get("total_citations", 0),
        "h_index":           metrics.get("h_index", 0),
        "publications":      [],  # PubMed is now primary for publications
    }


def _empty_ss() -> dict:
    return {
        "total_citations": 0,
        "h_index":         0,
        "paper_count":     0,
        "author_id":       None,
    }
