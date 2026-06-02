"""
pubmed_service.py — v5
CEO request: Show ALL papers, no time filter.
Uses disease condition as primary filter for accuracy.
"""

import asyncio
import httpx
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

HTTP_TIMEOUT = 15.0
MAX_RESULTS  = 20
MIN_RESULTS  = 5
MAX_RETRIES  = 2
RETRY_DELAY  = 1.5

CURRENT_YEAR = datetime.now().year

SPECIALTY_MESH = {
    "cardiovascular":          "Cardiovascular Diseases",
    "cardiology":              "Cardiology",
    "interventional":          "Cardiology",
    "electrophysiology":       "Cardiac Electrophysiology",
    "cardiac electrophysiology": "Cardiac Electrophysiology",
    "oncology":                "Neoplasms",
    "hematology":              "Hematologic Diseases",
    "neurology":               "Nervous System Diseases",
    "nephrology":              "Kidney Diseases",
    "pulmonary":               "Lung Diseases",
    "gastroenterology":        "Gastrointestinal Diseases",
    "endocrinology":           "Endocrine System Diseases",
    "rheumatology":            "Rheumatic Diseases",
    "dermatology":             "Skin Diseases",
    "psychiatry":              "Mental Disorders",
    "orthopedic":              "Musculoskeletal Diseases",
    "urology":                 "Urologic Diseases",
    "surgery":                 "Surgical Procedures, Operative",
    "internal medicine":       "Internal Medicine",
    "infectious":              "Communicable Diseases",
    "immunology":              "Immune System Diseases",
}

COMMON_LAST_NAMES = {
    "smith", "jones", "johnson", "williams", "brown", "davis",
    "kim", "lee", "chen", "wang", "liu", "zhang",
    "singh", "patel", "kumar", "sharma", "ali", "khan", "ahmed",
    "garcia", "martinez", "rodriguez", "lopez", "gonzalez",
    "taylor", "anderson", "thomas", "jackson", "white",
    "harris", "martin", "thompson", "moore", "robinson",
    "drury",
}


def clean_name(raw_name: str) -> str:
    name = raw_name
    for cred in [
        ", M.D.", ",M.D.", " M.D.", ", MD", ",MD", " MD",
        ", D.O.", ",D.O.", " D.O.", ", DO", ",DO", " DO",
        ", Ph.D.", ", PhD", ", M.D", ",M.D",
        ", MD, MPH", ",MD,MPH", " MD MPH", ", MPH", ",MPH", " MPH",
        ",FACC", ", FACC", ",FSCAI", ", FSCAI", ",FACP", ", FACP",
        ",FAHA", ", FAHA", ",FACS", ", FACS", ",FACOG", ", FACOG",
    ]:
        name = name.replace(cred, "")
    name = name.replace(",", "").strip()
    return " ".join(w.capitalize() for w in name.split())


def _get_mesh_term(specialty: str) -> str:
    if not specialty:
        return ""
    spec_lower = specialty.lower()
    for key, mesh in SPECIALTY_MESH.items():
        if key in spec_lower:
            return mesh
    return ""


def _is_common_name(clean_name_str: str) -> bool:
    parts = clean_name_str.lower().split()
    last  = parts[-1] if parts else ""
    return last in COMMON_LAST_NAMES


def _build_queries(
    clean: str,
    specialty: str,
    disease: str,
) -> list[tuple[str, int]]:
    """
    Build queries from most to least specific.
    NO date filter — CEO requested all papers shown.
    For VERY_COMMON names, TIER 0 uses ultra-specific specialty topics
    that score 75 — bypassing the heavy common-name penalty.
    """
    parts = clean.split()
    if len(parts) < 2:
        return [(f'"{clean}"[Author]', 10)]

    first  = parts[0]
    last   = parts[-1]
    # Extract middle name/initial from NPPES full name
    # "Mustafa Iltimas Ahmed" → middle="Iltimas", middle_initial="I"
    # "Cheng-Han Chen"        → middle=None, use hyphen logic
    # "John Smith"            → middle=None
    middle_parts = parts[1:-1]  # everything between first and last
    middle_name    = middle_parts[0] if middle_parts else ""
    middle_initial = middle_name[0].upper() if middle_name else ""

    mesh  = _get_mesh_term(specialty)
    disease_clean = disease.strip() if disease else ""
    spec_lower = specialty.lower() if specialty else ""

    # Build initials — handle hyphenated names like "Cheng-han" → "CH"
    # Critical: "Chen C" matches ALL Chen with C first name
    # but "Chen CH" matches ONLY Cheng-Han Chen — much more specific
    if "-" in first:
        initials = "".join(p[0].upper() for p in first.split("-"))
    else:
        initials = first[0].upper()

    # Full initials including middle: "Mustafa I" → "MI"
    # This is the most precise PubMed author search possible
    full_initials = initials + middle_initial if middle_initial else initials

    queries = []

    # TIER 0A: Full 3-part name (highest precision seed — "Mustafa Iltimas Ahmed")
    # Only fires when middle name is available from NPPES
    # Returns few results but very high confidence — used as seed alongside broader queries
    if middle_name:
        queries.append((
            f'"{last} {first} {middle_name}"[Author]',
            80
        ))
        if disease_clean:
            queries.append((
                f'"{last} {first} {middle_name}"[Author] AND "{disease_clean}"[Title/Abstract]',
                85
            ))
        # Also add full_initials query (e.g. "Ahmed MI") — catches papers
        # where PubMed indexed middle initial only, not full middle name
        # "Ahmed MI" is far more specific than "Ahmed M"
        queries.append((
            f'"{last} {full_initials}"[Author] AND "{disease_clean}"[Title/Abstract]',
            72
        ) if disease_clean else (
            f'"{last} {full_initials}"[Author]',
            65
        ))

    # TIER 0: Ultra-specific specialty topics (conf_bonus=75)
    # Uses full initials (e.g. "CH" for Cheng-Han) — much more specific
    # When middle initial available, also use full_initials (e.g. "MI" for Mustafa Iltimas)
    specific_topics = _get_specific_topics(spec_lower, disease_clean)
    for topic in specific_topics:
        queries.append((
            f'"{last} {initials}"[Author] AND "{topic}"[Title/Abstract]',
            75
        ))
        # Also search with full initials including middle (e.g. "Ahmed MI" not just "Ahmed M")
        # This catches papers where author signed with middle initial
        if full_initials != initials:
            queries.append((
                f'"{last} {full_initials}"[Author] AND "{topic}"[Title/Abstract]',
                75
            ))

    # TIER 1: Full name + disease condition (most specific)
    if disease_clean:
        queries.append((
            f'"{last} {first}"[Author] AND "{disease_clean}"[Title/Abstract]',
            60
        ))
        queries.append((
            f'"{last} {first}"[Author] AND "{disease_clean}"[MeSH Terms]',
            55
        ))

    # TIER 2: Full name + specialty MeSH
    if mesh:
        queries.append((
            f'"{last} {first}"[Author] AND "{mesh}"[MeSH Terms]',
            50
        ))
        queries.append((
            f'"{last} {first}"[Author] AND "{mesh}"[Title/Abstract]',
            45
        ))

    # TIER 3: Full name only
    queries.append((
        f'"{last} {first}"[Author]',
        35
    ))

    # TIER 4: Full initials (with middle) + disease
    # "Ahmed MI" is far more specific than "Ahmed M"
    if disease_clean:
        queries.append((
            f'"{last} {full_initials}"[Author] AND "{disease_clean}"[Title/Abstract]',
            30
        ))

    if mesh:
        queries.append((
            f'"{last} {full_initials}"[Author] AND "{mesh}"[MeSH Terms]',
            20
        ))

    # TIER 5: Full initials only — last resort
    # Use full_initials (MI) not just initials (M) for better precision
    queries.append((
        f'"{last} {full_initials}"[Author]',
        5
    ))

    return queries


def _get_specific_topics(spec_lower: str, disease: str) -> list[str]:
    """
    Return ultra-specific topic terms for a given specialty.
    These are niche enough that false matches are extremely rare.
    """
    topics = []
    disease_lower = disease.lower()

    if "interventional" in spec_lower or "cardiology" in spec_lower or "cardiovascular" in spec_lower:
        topics += ["myocardial bridging", "TAVR", "transcatheter aortic valve",
                   "drug-eluting stent", "structural heart"]
    if "electrophysiology" in spec_lower:
        topics += ["ventricular tachycardia ablation", "atrial fibrillation ablation",
                   "cardiac electrophysiology"]
    if "oncology" in spec_lower:
        topics += ["sentinel lymph node", "MammoSite", "brachytherapy"]
    if "neurology" in spec_lower:
        topics += ["deep brain stimulation", "thrombectomy stroke"]
    if "endocrinology" in spec_lower or "diabetes" in disease_lower:
        topics += ["HbA1c", "insulin resistance", "diabetes mellitus type 2"]
    if "nephrology" in spec_lower:
        topics += ["glomerulonephritis", "renal biopsy", "hemodialysis access"]
    if "pulmonary" in spec_lower:
        topics += ["bronchoscopy", "pulmonary fibrosis", "COPD exacerbation"]

    return topics[:3]  # Max 3 TIER 0 queries to avoid rate limits


async def _fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
) -> Optional[dict]:
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                return None
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as exc:
            logger.warning("PubMed request error attempt %d: %s", attempt + 1, exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
    return None


async def pubmed_lookup(
    name: str,
    specialty: str,
    client: httpx.AsyncClient,
    disease: str = "",
) -> dict:
    """
    PubMed lookup — ALL papers, no time filter.
    Uses name + disease + specialty for accurate matching.
    """
    clean = clean_name(name)
    is_common = _is_common_name(clean)

    logger.info(
        "PubMed lookup | clean_name=%r | specialty=%r | disease=%r | common_name=%s",
        clean, specialty, disease, is_common,
    )

    queries = _build_queries(clean, specialty, disease)

    best_pmids      = []
    best_query      = ""
    best_count      = 0
    best_conf_bonus = 0
    all_tier0_pmids = []   # Collect ALL TIER 0 results before stopping
    found_tier0     = False

    for query, conf_bonus in queries:
        data = await _fetch_with_retry(client, PUBMED_SEARCH_URL, {
            "db":      "pubmed",
            "term":    query,
            "retmax":  MAX_RESULTS,
            "retmode": "json",
            "sort":    "relevance",
        })
        if not data:
            continue

        esearch = data.get("esearchresult", {})
        pmids   = esearch.get("idlist", [])
        count   = int(esearch.get("count", 0))

        logger.debug("PubMed query=%r → count=%d", query, count)

        if count > 0:
            if conf_bonus >= 65:
                # TIER 0: collect ALL results — don't stop yet
                # Threshold 65 includes full-name (80), full-name+disease (85),
                # and full_initials (72, 65) queries for better coverage
                all_tier0_pmids.extend(pmids)
                if not best_query:
                    best_query      = query
                    best_conf_bonus = conf_bonus
                found_tier0 = True
                logger.info(
                    "PubMed TIER 0: query=%r count=%d → collecting",
                    query, count,
                )
            elif not found_tier0 and (not best_pmids or conf_bonus > best_conf_bonus):
                # Lower tiers — only use if no TIER 0 results
                best_count      = count
                best_pmids      = pmids
                best_query      = query
                best_conf_bonus = conf_bonus

                if conf_bonus >= 50 and count >= 1:
                    logger.info(
                        "PubMed: high-confidence match | query=%r count=%d conf_bonus=%d",
                        query, count, conf_bonus,
                    )
                    break

    # If TIER 0 found results, use merged deduplicated PMIDs
    if all_tier0_pmids:
        seen = set()
        deduped = []
        for pmid in all_tier0_pmids:
            if pmid not in seen:
                seen.add(pmid)
                deduped.append(pmid)
        best_pmids      = deduped[:MAX_RESULTS]
        best_count      = len(best_pmids)
        best_conf_bonus = 70
        logger.info(
            "PubMed TIER 0 merged: %d unique PMIDs from all specific-topic queries",
            len(best_pmids),
        )

    if not best_pmids:
        logger.info("PubMed: no results for %r", clean)
        return _empty_pubmed()

    logger.info(
        "PubMed: best query=%r | count=%d | conf_bonus=%d",
        best_query, best_count, best_conf_bonus,
    )

    # Fetch paper details
    summary_data = await _fetch_with_retry(client, PUBMED_FETCH_URL, {
        "db":      "pubmed",
        "id":      ",".join(best_pmids[:MAX_RESULTS]),
        "retmode": "json",
    })

    if not summary_data:
        return _empty_pubmed()

    result_map   = summary_data.get("result", {})
    publications = []

    for pmid in best_pmids[:MAX_RESULTS]:
        paper = result_map.get(pmid, {})
        title = paper.get("title", "").rstrip(".")
        if not title:
            continue

        pub_year = None
        for date_field in ["pubdate", "epubdate", "sortpubdate"]:
            m = re.search(r"\d{4}", paper.get(date_field, ""))
            if m:
                pub_year = int(m.group())
                break

        pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"
        doi_url = ""
        for aid in paper.get("articleids", []):
            if aid.get("idtype") == "doi":
                doi_val = aid.get("value", "")
                if doi_val:
                    doi_url = f"https://doi.org/{doi_val}"
                    break

        # Extract author list from PubMed metadata
        # Used by verifier to confirm physician is actual author
        # e.g. "Burns D" could be Daniel Burns or Durand Burns
        raw_authors = paper.get("authors", [])
        author_names = [
            a.get("name", "") for a in raw_authors
            if a.get("authtype", "") == "Author"
        ]

        publications.append({
            "title":                title,
            "year":                 pub_year,
            "pubmed_url":           pubmed_url,
            "doi_url":              doi_url,
            "semantic_scholar_url": "",
            "best_url":             doi_url or pubmed_url,
            "source":               "PubMed",
            "pmid":                 pmid,
            "authors":              author_names,
        })

    publications.sort(key=lambda p: p.get("year") or 0, reverse=True)

    confidence = _score_confidence(best_conf_bonus, len(publications), clean)

    logger.info(
        "PubMed result | name=%r | publications=%d | confidence=%d",
        clean, len(publications), confidence,
    )

    return {
        "pmids":               [p["pmid"] for p in publications],
        "publications":        publications,
        "publication_count":   len(publications),
        "search_variant_used": best_query,
        "time_window":         "all-time",
        "confidence":          confidence,
    }


# Surnames so globally common that even full name + disease filter
# cannot reliably identify the correct physician — always penalise heavily
# Surnames so globally common that even full name + disease filter
# cannot reliably identify the correct physician — always penalise heavily
# UNLESS the query used a very specific medical topic (conf_bonus >= 70)
VERY_COMMON_LAST_NAMES = {
    "chen", "wang", "liu", "zhang", "li", "kim", "lee",
    "nguyen", "garcia", "rodriguez", "martinez",
}

# Very specific medical topics — if a paper matches these for a common name,
# it's almost certainly the right physician (not a false match)
SPECIFIC_TOPIC_TERMS = {
    # Interventional cardiology
    "tavr", "transcatheter aortic valve", "myocardial bridging",
    "drug-eluting stent", "plga nanoparticles cardiomyocytes",
    "mammosite", "brachytherapy breast",
    # Electrophysiology
    "ablation atrial fibrillation", "ventricular tachycardia ablation",
    # Oncology
    "sentinel lymph node biopsy", "mammosite",
    # Neurology
    "deep brain stimulation",
}


def _score_confidence(conf_bonus: int, pub_count: int, clean_name_str: str) -> int:
    score = conf_bonus

    if pub_count >= 10:
        score += 20
    elif pub_count >= 5:
        score += 15
    elif pub_count >= 2:
        score += 10
    elif pub_count >= 1:
        score += 5

    parts = clean_name_str.lower().split()
    last  = parts[-1] if parts else ""

    if last in VERY_COMMON_LAST_NAMES:
        if conf_bonus >= 70:
            # Very specific topic query — reliable even for common names
            score -= 10
        else:
            # Generic disease/specialty query — too many false matches
            score -= 50
    elif last in COMMON_LAST_NAMES:
        if conf_bonus >= 45:
            score -= 10  # Light penalty — full name + filter, fairly reliable
        else:
            score -= 40  # Heavy penalty — initial only, very unreliable

    return max(0, min(100, score))


def _empty_pubmed() -> dict:
    return {
        "pmids":               [],
        "publications":        [],
        "publication_count":   0,
        "search_variant_used": "",
        "time_window":         "all-time",
        "confidence":          0,
    }
