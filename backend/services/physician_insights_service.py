"""
physician_insights_service.py — v3
===================================
Enhanced pipeline with Europe PMC + Publication Verification:

  Physician Name
        ↓
  Clean + Normalize
        ↓
  PARALLEL: PubMed + Europe PMC + Semantic Scholar + OpenAlex
        ↓
  PMID Cross-matching (shared PMIDs get +20 confidence bonus)
        ↓
  Publication Verification (affiliation state check + Groq semantic title check)
        ↓
  Confidence Scoring
        ↓
  Groq AI Summary
        ↓
  Final Response
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

from services.pubmed_service           import pubmed_lookup, clean_name as pubmed_clean
from services.semantic_scholar_service import semantic_scholar_metrics
from services.openalex_service         import openalex_lookup, derive_areas_from_publications
from services.europepmc_service        import europepmc_lookup
from services.publication_verifier     import verify_publications
from services.citations_cache          import (
    get_cached_citations,
    set_cached_citations,
    refresh_citations_background,
)

logger = logging.getLogger(__name__)

GROQ_API_KEY   = os.getenv("GROQ_API_KEY") or "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"
GROQ_URL       = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL     = "llama-3.3-70b-versatile"
HTTP_TIMEOUT   = 12.0
MIN_CONFIDENCE = 35

NPI_STATE_FROM_ADDRESS = {}


def _extract_npi_state(specialty: str) -> str:
    return ""


def _cross_match_pmids(
    pubmed_pubs: list[dict],
    epmc_pubs:   list[dict],
) -> tuple[list[dict], int]:
    pubmed_pmids = {p.get("pmid") for p in pubmed_pubs if p.get("pmid")}
    epmc_pmids   = {p.get("pmid") for p in epmc_pubs   if p.get("pmid")}

    shared_pmids = pubmed_pmids & epmc_pmids
    cross_match_count = len(shared_pmids)

    if shared_pmids:
        logger.info("PMID cross-match: %d shared PMIDs → +20 confidence bonus", cross_match_count)

    for pub in pubmed_pubs:
        if pub.get("pmid") in shared_pmids:
            pub["cross_matched"] = True

    pubmed_pmid_set = {p.get("pmid") for p in pubmed_pubs}
    epmc_exclusive  = [p for p in epmc_pubs if p.get("pmid") not in pubmed_pmid_set]

    merged = pubmed_pubs + epmc_exclusive
    merged.sort(key=lambda p: p.get("year") or 0, reverse=True)

    return merged, cross_match_count


if GROQ_API_KEY:
    logger.info("Groq API key loaded (%d chars)", len(GROQ_API_KEY))
else:
    logger.warning("GROQ_API_KEY not set — template summaries will be used")


async def _groq_summary(
    name:              str,
    specialty:         str,
    disease:           str,
    publication_count: int,
    total_citations:   int,
    h_index:           int,
    research_areas:    list[str],
    client:            httpx.AsyncClient,
) -> str:
    if not GROQ_API_KEY:
        return _template_summary(name, specialty, disease, publication_count, research_areas)

    # FIX: Use physician's OWN specialty as context for AI summary.
    # This prevents e.g. an Endocrinologist being described as
    # "relevant to Alzheimer's trials" just because the user searched Alzheimer's.
    # Use specialty if it's meaningful, otherwise fall back to disease.
    specialty_context = specialty if specialty and specialty not in ("Physician", "clinical trial") \
                        else disease

    if publication_count > 0:
        prompt = (
            f"Write a 3-sentence professional physician profile summary.\n\n"
            f"Physician: {name}\n"
            f"PRIMARY Specialty: {specialty_context}\n"
            f"Publications: {publication_count}\n"
            f"Total Citations: {total_citations}\n\n"
            f"Instructions:\n"
            f"- Start with Dr. [LastName] is...\n"
            f"- Base the ENTIRE summary exclusively on the PRIMARY Specialty above\n"
            f"- Strict Alignment: Never mention any medical specialty, subspecialty, or organ system outside of {specialty_context}\n"
            f"- Context Boundary: Ignore paper titles that overlap with other branches of medicine; focus exclusively on {specialty_context}\n"
            f"- Mention their research impact in {specialty_context}\n"
            f"- Mention relevance to {specialty_context} clinical trials\n"
            f"- Professional healthcare tone, under 80 words\n"
            f"- Never invent awards or achievements"
        )
    else:
        prompt = (
            f"Write a 3-sentence professional physician profile for a clinical trial platform.\n\n"
            f"Physician: {name}\n"
            f"PRIMARY Specialty: {specialty_context}\n\n"
            f"Instructions:\n"
            f"- Start with Dr. [LastName] is...\n"
            f"- Base the ENTIRE summary exclusively on the PRIMARY Specialty above\n"
            f"- Strict Alignment: Never mention any medical specialty, subspecialty, or organ system outside of {specialty_context}\n"
            f"- Describe why a {specialty_context} specialist is valuable for clinical trials\n"
            f"- Professional tone, under 70 words\n"
            f"- Do NOT mention publication or citation numbers"
        )

    try:
        resp = await client.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       GROQ_MODEL,
                "max_tokens":  150,
                "temperature": 0.3,
                "messages":    [{"role": "user", "content": prompt}],
            },
            timeout=HTTP_TIMEOUT,
        )

        if resp.status_code == 429:
            logger.warning("Groq rate limit for %r", name)
            return _template_summary(name, specialty, disease, publication_count, research_areas)
        if resp.status_code == 401:
            logger.error("Groq 401 for %r — invalid key", name)
            return _template_summary(name, specialty, disease, publication_count, research_areas)
        if resp.status_code != 200:
            logger.warning("Groq %d for %r", resp.status_code, name)
            return _template_summary(name, specialty, disease, publication_count, research_areas)

        text = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info("Groq summary generated for %r (%d chars)", name, len(text))
        return text

    except Exception as exc:
        logger.warning("Groq failed for %r: %s", name, exc)
        return _template_summary(name, specialty, disease, publication_count, research_areas)


def _groq_awards(
    name:              str,
    specialty:         str,
    h_index:           int,
    total_citations:   int,
    publication_count: int,
) -> list[str]:
    awards = []

    if total_citations >= 5000:
        awards.append(f"Highly cited researcher with over {total_citations:,} citations in medical literature")
    elif total_citations >= 1000:
        awards.append(f"Research cited over {total_citations:,} times in peer-reviewed literature")
    elif total_citations >= 100:
        awards.append(f"Active researcher with {total_citations} citations in medical literature")

    if h_index >= 30:
        awards.append(f"H-index of {h_index} — indicating sustained high-impact research output")
    elif h_index >= 15:
        awards.append(f"H-index of {h_index} — demonstrating consistent research contributions in {specialty}")

    if publication_count >= 50:
        awards.append(f"Prolific academic author with {publication_count}+ indexed publications")
    elif publication_count >= 20:
        awards.append(f"{publication_count}+ indexed publications in medical literature")

    return awards[:3]


def _template_summary(
    name:              str,
    specialty:         str,
    disease:           str,
    publication_count: int,
    research_areas:    list[str],
) -> str:
    clean = re.sub(r",?\s*(M\.?D\.?|D\.?O\.?|Ph\.?D\.?)$", "", name, flags=re.IGNORECASE).strip()
    last  = clean.split()[-1] if clean else "this physician"

    specialty_context = specialty if specialty and specialty not in ("Physician", "clinical trial") \
                        else disease

    if publication_count > 0:
        areas = f"with focus on {', '.join(research_areas[:2])}" if research_areas else ""
        return (
            f"Dr. {last} is a {specialty} specialist with {publication_count} indexed publications {areas}. "
            f"Their clinical expertise makes them a relevant contact for "
            f"{specialty_context} clinical trial recruitment and participation."
        )
    return (
        f"Dr. {last} is a {specialty} specialist with clinical expertise relevant to "
        f"{specialty_context} management and treatment. "
        f"As a {specialty} practitioner, they may be a strong candidate for "
        f"clinical trial referrals and patient recruitment in this disease area."
    )


async def enrich_physician(
    npi:          str,
    name:         str,
    specialty:    str,
    disease:      str,
    groq_api_key: str,
    npi_state:    str = "",
) -> dict:
    logger.info(
        "═══ Enriching NPI=%s name=%r specialty=%r disease=%r ═══",
        npi, name, specialty, disease,
    )

    async with httpx.AsyncClient(
        headers={"User-Agent": "ClinTrialNavigator/1.0 (contact@aquarient.com)"},
        follow_redirects=True,
    ) as client:

        logger.info("Starting parallel enrichment for %r", name)

        pubmed_task   = asyncio.create_task(pubmed_lookup(name, specialty, client, disease))
        epmc_task     = asyncio.create_task(europepmc_lookup(name, specialty, client, disease))
        ss_task       = asyncio.create_task(semantic_scholar_metrics(name, specialty, client))
        openalex_task = asyncio.create_task(openalex_lookup(name, client))

        pubmed_data, epmc_data, ss_data, openalex_data = await asyncio.gather(
            pubmed_task, epmc_task, ss_task, openalex_task,
            return_exceptions=False,
        )

        logger.info(
            "PubMed: publications=%d confidence=%d",
            len(pubmed_data.get("publications", [])),
            pubmed_data.get("confidence", 0),
        )
        logger.info(
            "EuropePMC: publications=%d confidence=%d",
            len(epmc_data.get("publications", [])),
            epmc_data.get("confidence", 0),
        )
        logger.info(
            "SS metrics: citations=%d h_index=%d",
            ss_data.get("total_citations", 0),
            ss_data.get("h_index", 0),
        )
        logger.info(
            "OpenAlex: research_areas=%s citations=%d",
            openalex_data.get("research_areas", []),
            openalex_data.get("total_citations", 0),
        )

        # --- PMID Cross-matching ---
        pubmed_pubs = pubmed_data.get("publications", [])
        epmc_pubs   = epmc_data.get("publications", [])

        pubmed_conf = pubmed_data.get("confidence", 0)
        epmc_conf   = epmc_data.get("confidence", 0)

        # Check if physician has a very common surname
        # Only these names need TIER 0 protection — they have thousands of
        # researchers worldwide making EuropePMC results unreliable
        # Strip credentials FIRST, then extract last name
        _name_clean = name
        for _cred in [
            ", M.D., Ph.D.", ", M.D.,Ph.D.", ", M.D.", ",M.D.", " M.D.",
            ", MD", ",MD", " MD", ", DO", ",DO", " DO",
            ", Ph.D.", ", PhD", ",Ph.D.", ",PhD",
            ",FACC", ", FACC", ",FSCAI", ", FSCAI",
            ",FACP", ", FACP", ",FAHA", ", FAHA",
        ]:
            _name_clean = _name_clean.replace(_cred, "")
        _name_clean = _name_clean.replace(",", "").strip()
        _name_parts = _name_clean.lower().split()
        _last = _name_parts[-1] if _name_parts else ""

        VERY_COMMON_SURNAMES = {
            "chen", "wang", "liu", "zhang", "li", "kim", "lee",
            "nguyen", "garcia", "rodriguez", "martinez",
        }
        _is_very_common = _last in VERY_COMMON_SURNAMES

        # Smart merge strategy:
        #
        # TIER 0 + VERY COMMON NAME (pubmed_conf >= 75 AND common surname):
        #   PubMed used ultra-specific topic queries (TAVR, myocardial bridging etc.)
        #   AND the physician has a very common surname (chen, wang, kim etc.)
        #   EuropePMC "Chen C" / "Kim J" queries pull thousands of wrong researchers.
        #   → Keep PubMed papers only + cross-matched EuropePMC papers
        #   → Reject all EuropePMC-exclusive papers
        #
        # ALL OTHER PHYSICIANS (uncommon name OR normal pubmed queries):
        #   Standard merge — EuropePMC exclusive papers included
        #   This catches older papers, European journal papers missed by PubMed

        if pubmed_conf >= 75 and _is_very_common:
            # TIER 0 protection for very common surnames only
            pubmed_pmid_set = {p.get("pmid") for p in pubmed_pubs if p.get("pmid")}
            epmc_cross_matched = [
                p for p in epmc_pubs
                if p.get("pmid") and p.get("pmid") in pubmed_pmid_set
            ]
            cross_match_count = len(epmc_cross_matched)
            merged_pubs = pubmed_pubs
            confidence  = pubmed_conf

            logger.info(
                "TIER 0 + common surname (%r): PubMed=%d (conf=%d) | "
                "EuropePMC cross-matched=%d | EuropePMC-exclusive rejected=%d",
                _last, len(pubmed_pubs), pubmed_conf,
                cross_match_count,
                len(epmc_pubs) - cross_match_count,
            )

            if cross_match_count > 0:
                confidence = min(100, confidence + 20)
                logger.info("Cross-match bonus: confidence → %d", confidence)

        else:
            # Normal merge — all physicians with uncommon surnames
            # or TIER 0 physicians with uncommon surnames (e.g. Hollowed, Walters)
            merged_pubs, cross_match_count = _cross_match_pmids(pubmed_pubs, epmc_pubs)

            if epmc_conf > pubmed_conf and len(epmc_pubs) > 0:
                confidence = epmc_conf
                logger.info(
                    "Using EuropePMC confidence (%d) over PubMed (%d)",
                    epmc_conf, pubmed_conf,
                )
            else:
                confidence = pubmed_conf

            if cross_match_count > 0:
                confidence = min(100, confidence + 20)
                logger.info("Cross-match bonus: confidence → %d", confidence)

        # --- Confidence check ---
        if confidence < MIN_CONFIDENCE:
            logger.info(
                "Low confidence (%d) for %r — hiding publications",
                confidence, name,
            )
            raw_publications = []
        else:
            raw_publications = merged_pubs

        # --- Publication Verification ---
        if raw_publications:
            verified_publications = await verify_publications(
                publications    = raw_publications,
                specialty       = specialty,
                npi_state       = npi_state,
                client          = client,
                physician_name  = pubmed_clean(name),
            )
            logger.info(
                "Verification: %d → %d publications kept",
                len(raw_publications), len(verified_publications),
            )
        else:
            verified_publications = []

        # --- FIX #5: Sort publications by year descending (newest first) ---
        # Applies to ALL merge paths — TIER 0 (common surnames) had no sort before.
        # _cross_match_pmids() sorts the normal path but TIER 0 used pubmed_pubs raw.
        # This single sort after verification guarantees consistent ordering
        # regardless of which merge path was taken.
        publications = sorted(
            verified_publications,
            key=lambda p: p.get("year") or 0,
            reverse=True,
        )

        pub_count = len(publications)

        # --- Citation metrics (cache-aware) ---
        # Problem: Semantic Scholar re-matches different authors each search
        # causing citations to fluctuate (e.g. 365 → 448 → 312).
        # Solution: Cache per NPI with 7-day TTL.
        #   HIT  → return cached value instantly, skip SS/OA metrics
        #   STALE → return stale value instantly + refresh in background
        #   MISS → fetch fresh from SS/OA, store in cache

        ss_citations = ss_data.get("total_citations", 0)
        ss_h_index   = ss_data.get("h_index", 0)
        oa_citations = openalex_data.get("total_citations", 0)
        oa_h_index   = openalex_data.get("h_index", 0)

        cached = get_cached_citations(npi)

        if cached is not None:
            # Cache HIT or STALE — use cached value for stable display
            total_citations = cached["citations"]
            h_index         = cached["h_index"]
            logger.info(
                "Citations from cache | NPI=%s | citations=%d | h_index=%d | stale=%s",
                npi, total_citations, h_index, cached["is_stale"],
            )

            if cached["is_stale"]:
                # Return stale data now, refresh silently in background
                async def _fetch_citations(name: str, specialty: str):
                    """Re-fetch from SS/OA for background refresh."""
                    import httpx as _httpx
                    async with _httpx.AsyncClient(
                        headers={"User-Agent": "ClinTrialNavigator/1.0"},
                        follow_redirects=True,
                    ) as _client:
                        _ss  = await semantic_scholar_metrics(name, specialty, _client)
                        _oa  = await openalex_lookup(name, _client)
                        _sc  = _ss.get("total_citations", 0)
                        _shi = _ss.get("h_index", 0)
                        _oc  = _oa.get("total_citations", 0)
                        _ohi = _oa.get("h_index", 0)
                        if _sc > 0:
                            return _sc, _shi
                        elif _oc > 0 and _oc < 5000:
                            return _oc, _ohi
                        return 0, 0

                refresh_citations_background(npi, name, specialty, _fetch_citations)

        else:
            # Cache MISS — compute from fresh SS/OA data and store
            if confidence >= MIN_CONFIDENCE and ss_citations > 0:
                total_citations = ss_citations
                h_index         = ss_h_index
            elif confidence >= MIN_CONFIDENCE and ss_citations == 0:
                total_citations = oa_citations if oa_citations < 5000 else 0
                h_index         = oa_h_index   if oa_citations < 5000 else 0
            else:
                total_citations = ss_citations
                h_index         = ss_h_index

            # Store in cache for next search
            if total_citations > 0 or h_index > 0:
                set_cached_citations(npi, total_citations, h_index)
                logger.info(
                    "Citations cached (fresh) | NPI=%s | citations=%d | h_index=%d",
                    npi, total_citations, h_index,
                )

        # --- Research areas ---
        # Always start with NPI specialty as base
        specialty_base = [s.strip() for s in specialty.split(",") if s.strip()]

        # Block 1: Non-human/basic science fields
        NON_MEDICAL = {
            "chemistry", "physics", "biology", "microbiology",
            "giardia", "neuroscience", "astronomy", "engineering",
            "mathematics", "materials science", "agronomy",
            "environmental science", "operating system",
            "particle physics", "nuclear physics",
        }

        # Block 2: Cross-medical contamination filter
        ALL_MEDICAL_FIELDS = {
            "psychiatry", "oncology", "pulmonology", "critical care",
            "dermatology", "pediatrics", "gastroenterology",
            "ophthalmology", "urology", "orthopedics", "hematology",
            "rheumatology", "endocrinology", "immunology", "pathology",
        }
        spec_lower = specialty.lower()
        safe_fields = {f for f in ALL_MEDICAL_FIELDS if f in spec_lower}
        fields_to_block = ALL_MEDICAL_FIELDS - safe_fields

        def _filter_areas(areas):
            return [
                a for a in areas
                if not any(nm in a.lower() for nm in NON_MEDICAL)
                and not any(bf in a.lower() for bf in fields_to_block)
            ]

        if publications:
            pub_derived = derive_areas_from_publications(publications)
            if pub_derived:
                filtered = _filter_areas(pub_derived)
                combined = specialty_base + [a for a in filtered if a not in specialty_base]
                research_areas = combined[:5]
                logger.info("Research areas (specialty + filtered pub): %s", research_areas)
            else:
                research_areas = specialty_base
                logger.info("Research areas from specialty (pub keywords empty): %s", research_areas)
        else:
            if confidence >= MIN_CONFIDENCE:
                oa_areas = openalex_data.get("research_areas", [])
                filtered_oa = _filter_areas(oa_areas)
                combined = specialty_base + [a for a in filtered_oa if a not in specialty_base]
                research_areas = combined[:5]
                logger.info("Research areas from specialty+OpenAlex: %s", research_areas)
            else:
                research_areas = specialty_base
                logger.info("Research areas from specialty (low confidence): %s", research_areas)

        # --- Groq AI Summary ---
        ai_summary = await _groq_summary(
            name              = name,
            specialty         = specialty,
            disease           = disease,
            publication_count = pub_count,
            total_citations   = total_citations,
            h_index           = h_index,
            research_areas    = research_areas,
            client            = client,
        )

        awards = _groq_awards(
            name              = name,
            specialty         = specialty,
            h_index           = h_index,
            total_citations   = total_citations,
            publication_count = pub_count,
        )

        logger.info(
            "═══ Enrichment complete NPI=%s | pubs=%d | citations=%d | "
            "h_index=%d | areas=%d | awards=%d | confidence=%d ═══",
            npi, pub_count, total_citations,
            h_index, len(research_areas), len(awards), confidence,
        )

    return {
        "npi":      npi,
        "name":     name,
        "specialty": specialty,
        "disease":  disease,
        "status":   "ready",

        "publication_count": pub_count,
        "publications":      publications,
        "top_topics":        [],

        "total_citations":        total_citations,
        "h_index":                h_index,
        "i10_index":              openalex_data.get("i10_index", 0),
        "citations_last_5_years": openalex_data.get("citations_last_5_years", 0),

        "research_areas":   research_areas,
        "awards":           awards,
        "confidence_score": confidence,
        "ai_summary":       ai_summary,
    }
