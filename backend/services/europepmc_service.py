"""
europepmc_service.py
Europe PMC — 4th publication source.
Catches papers missed by PubMed (older papers, different name formats).
No API key required. Covers all US journals indexed in PubMed/PMC + more.
"""

import asyncio
import httpx
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

EUROPEPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
HTTP_TIMEOUT  = 15.0
MAX_RESULTS   = 20
MAX_RETRIES   = 2
RETRY_DELAY   = 1.5


def _clean_name(raw_name: str) -> str:
    name = raw_name
    for cred in [
        ", M.D.", ",M.D.", " M.D.", ", MD", ",MD", " MD",
        ", DO", ",DO", " DO", ", Ph.D.", ", PhD",
        ",FACC", ", FACC", ",FSCAI", ", FSCAI",
    ]:
        name = name.replace(cred, "")
    name = name.replace(",", "").strip()
    return " ".join(w.capitalize() for w in name.split())


def _build_queries(
    clean: str,
    specialty: str,
    disease: str,
) -> list[tuple[str, int]]:
    """
    Europe PMC uses AUTH:"Last FC" format.
    Build queries from most to least specific.
    No date filter — show all papers.
    """
    parts = clean.split()
    if len(parts) < 2:
        return [(f'AUTH:"{clean}"', 10)]

    first = parts[0]
    last  = parts[-1]
    initial = first[0].upper()
    disease_clean = disease.strip() if disease else ""
    spec_lower = specialty.lower() if specialty else ""

    # Extract specialty keyword for title search
    spec_keyword = ""
    for kw in ["cardiology", "cardiac", "cardiovascular", "heart",
               "oncology", "neurology", "nephrology", "pulmonary",
               "electrophysiology", "interventional"]:
        if kw in spec_lower:
            spec_keyword = kw
            break

    queries = []

    # TIER 1: Full name + disease (most specific)
    if disease_clean:
        queries.append((
            f'AUTH:"{last} {first}" AND TITLE:"{disease_clean}"',
            60
        ))
        queries.append((
            f'AUTH:"{last} {initial}" AND TITLE:"{disease_clean}"',
            50
        ))

    # TIER 2: Full name + specialty keyword
    if spec_keyword:
        queries.append((
            f'AUTH:"{last} {first}" AND TITLE:"{spec_keyword}"',
            50
        ))
        queries.append((
            f'AUTH:"{last} {initial}" AND TITLE:"{spec_keyword}"',
            45
        ))

    # TIER 3: Full name only
    queries.append((
        f'AUTH:"{last} {first}"',
        35
    ))

    # TIER 4: Initial only + disease
    if disease_clean:
        queries.append((
            f'AUTH:"{last} {initial}" AND (TITLE:"{disease_clean}" OR KW:"{disease_clean}")',
            30
        ))

    # TIER 5: Initial only — last resort
    queries.append((
        f'AUTH:"{last} {initial}"',
        5
    ))

    return queries


async def _fetch_with_retry(
    client: httpx.AsyncClient,
    params: dict,
) -> Optional[dict]:
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.get(
                EUROPEPMC_URL,
                params=params,
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code == 429:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                return None
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as exc:
            logger.warning("EuropePMC error attempt %d: %s", attempt + 1, exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
    return None


async def europepmc_lookup(
    name: str,
    specialty: str,
    client: httpx.AsyncClient,
    disease: str = "",
) -> dict:
    """
    Europe PMC publication lookup.
    Returns publications + confidence score.
    """
    clean = _clean_name(name)
    logger.info(
        "EuropePMC lookup | clean_name=%r | specialty=%r | disease=%r",
        clean, specialty, disease,
    )

    queries = _build_queries(clean, specialty, disease)

    best_papers     = []
    best_query      = ""
    best_conf_bonus = 0

    for query, conf_bonus in queries:
        data = await _fetch_with_retry(client, {
            "query":      query,
            "format":     "json",
            "pageSize":   MAX_RESULTS,
            "resultType": "core",
        })

        if not data:
            continue

        total  = data.get("hitCount", 0)
        papers = data.get("resultList", {}).get("result", [])

        logger.debug("EuropePMC query=%r → hits=%d", query, total)

        if total > 0 and papers and conf_bonus > best_conf_bonus:
            best_papers     = papers
            best_query      = query
            best_conf_bonus = conf_bonus

            # Stop at first high-confidence result
            if conf_bonus >= 50 and total >= 1:
                logger.info(
                    "EuropePMC: high-confidence match | query=%r hits=%d",
                    query, total,
                )
                break

    if not best_papers:
        logger.info("EuropePMC: no results for %r", clean)
        return _empty_result()

    publications = []
    for paper in best_papers[:MAX_RESULTS]:
        title = paper.get("title", "").rstrip(".")
        if not title:
            continue

        pub_year = None
        year_str = paper.get("pubYear", "")
        if year_str and year_str.isdigit():
            pub_year = int(year_str)

        pmid    = paper.get("pmid", "")
        doi     = paper.get("doi", "")
        epmc_id = paper.get("id", "")

        pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}" if pmid else ""
        doi_url    = f"https://doi.org/{doi}" if doi else ""
        epmc_url   = f"https://europepmc.org/article/MED/{pmid}" if pmid else \
                     f"https://europepmc.org/article/{epmc_id}" if epmc_id else ""

        best_url = doi_url or pubmed_url or epmc_url

        publications.append({
            "title":                title,
            "year":                 pub_year,
            "pubmed_url":           pubmed_url,
            "doi_url":              doi_url,
            "semantic_scholar_url": "",
            "best_url":             best_url,
            "source":               "Europe PMC",
            "pmid":                 pmid,
        })

    # Sort by year descending
    publications.sort(key=lambda p: p.get("year") or 0, reverse=True)

    confidence = _score_confidence(best_conf_bonus, len(publications), clean)

    logger.info(
        "EuropePMC result | name=%r | publications=%d | confidence=%d",
        clean, len(publications), confidence,
    )

    return {
        "publications":      publications,
        "publication_count": len(publications),
        "query_used":        best_query,
        "confidence":        confidence,
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


# Names so common globally that even full name + disease filter is unreliable
VERY_COMMON_LAST_NAMES = {
    "chen", "wang", "liu", "zhang", "li", "kim", "lee",
    "nguyen", "garcia", "rodriguez", "martinez",
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
        # Always heavy penalty — thousands of researchers worldwide
        # share these surnames, even with distinctive first names
        score -= 50
    elif last in COMMON_LAST_NAMES:
        if conf_bonus >= 45:
            score -= 10
        else:
            score -= 40

    return max(0, min(100, score))


def _empty_result() -> dict:
    return {
        "publications":      [],
        "publication_count": 0,
        "query_used":        "",
        "confidence":        0,
    }
