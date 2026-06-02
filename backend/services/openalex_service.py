"""
openalex_service.py — Fixed v2
Changes:
  1. Added comprehensive medical topic keywords for better area extraction
  2. Publication-derived areas are now more specific and clinical
"""

import httpx
import logging

logger = logging.getLogger(__name__)

OPENALEX_URL = "https://api.openalex.org/authors"
HTTP_TIMEOUT = 12.0

# Comprehensive medical topic keywords for deriving research areas from paper titles
MEDICAL_TOPIC_KEYWORDS = {
    # Cardiology
    "heart failure":      "Heart Failure",
    "cardiac":            "Cardiology",
    "cardiovascular":     "Cardiovascular Medicine",
    "coronary":           "Coronary Artery Disease",
    "myocardial":         "Myocardial Disease",
    "arrhythmia":         "Cardiac Arrhythmia",
    "atrial fibrillation":"Atrial Fibrillation",
    "hypertension":       "Hypertension",
    "atherosclerosis":    "Atherosclerosis",
    "interventional":     "Interventional Cardiology",
    "catheter":           "Interventional Cardiology",
    "stent":              "Interventional Cardiology",
    "impella":            "Mechanical Circulatory Support",

    # Oncology
    "cancer":             "Oncology",
    "tumor":              "Oncology",
    "oncology":           "Oncology",
    "chemotherapy":       "Medical Oncology",
    "immunotherapy":      "Immunotherapy",
    "checkpoint":         "Immunotherapy & Checkpoint Inhibitors",
    "trastuzumab":        "HER2-Targeted Therapy",
    "tyrosine kinase":    "Targeted Cancer Therapy",
    "leukemia":           "Hematologic Oncology",
    "lymphoma":           "Hematologic Oncology",
    "myeloma":            "Hematologic Oncology",
    "metastatic":         "Metastatic Cancer",
    "breast cancer":      "Breast Oncology",
    "lung cancer":        "Thoracic Oncology",
    "colorectal":         "Gastrointestinal Oncology",
    "everolimus":         "mTOR Inhibitor Therapy",
    "pertuzumab":         "HER2-Targeted Therapy",
    "ceramide":           "Cancer Biology",

    # Neurology
    "stroke":             "Stroke & Cerebrovascular Disease",
    "neurology":          "Neurology",
    "alzheimer":          "Neurodegenerative Disease",
    "parkinson":          "Movement Disorders",
    "epilepsy":           "Epilepsy",
    "multiple sclerosis": "Multiple Sclerosis",

    # Other specialties
    "diabetes":           "Endocrinology & Diabetes",
    "kidney":             "Nephrology",
    "renal":              "Nephrology",
    "liver":              "Hepatology",
    "hepatic":            "Hepatology",
    "lung":               "Pulmonology",
    "pulmonary":          "Pulmonology",
    "respiratory":        "Pulmonology",
    "gastro":             "Gastroenterology",
    "colon":              "Gastroenterology",
    "rheumatology":       "Rheumatology",
    "arthritis":          "Rheumatology",
    "immune":             "Immunology",
    "infection":          "Infectious Disease",
    "sepsis":             "Critical Care Medicine",
    "icu":                "Critical Care Medicine",
    "surgery":            "Surgery",
    "surgical":           "Surgery",
    "pediatric":          "Pediatrics",
    "mental":             "Psychiatry",
    "depression":         "Psychiatry",
    "thyroid":            "Endocrinology",
    "dermatology":        "Dermatology",
    "skin":               "Dermatology",
    "spine":              "Orthopedics",
    "bone":               "Orthopedics",
    "urology":            "Urology",
    "gynecology":         "Gynecology",
    "obstetrics":         "Obstetrics",
    "hematology":         "Hematology",
    "blood":              "Hematology",
    "anemia":             "Hematology",
    "vaccination":        "Preventive Medicine",
    "vaccine":            "Preventive Medicine",
}


def clean_name(raw_name: str) -> str:
    name = raw_name
    for cred in [
        ", M.D.", ",M.D.", " M.D.", ", MD", ",MD", " MD",
        ", DO", ",DO", " DO", ", Ph.D.", ", PhD", ", M.D", ",M.D",
        ",FACC", ", FACC", ",FSCAI", ", FSCAI", ",FACP", ", FACP",
    ]:
        name = name.replace(cred, "")
    return " ".join(w.capitalize() for w in name.replace(",", "").strip().split())


async def openalex_lookup(name: str, client: httpx.AsyncClient) -> dict:
    """Get citation metrics and research areas from OpenAlex."""
    clean = clean_name(name)
    logger.info("OpenAlex lookup | clean_name=%r", clean)

    try:
        resp = await client.get(
            OPENALEX_URL,
            params={
                "search":   clean,
                "per-page": 1,
                "select":   "id,display_name,cited_by_count,summary_stats,x_concepts,topics",
                "mailto":   "contact@aquarient.com",
            },
            timeout=HTTP_TIMEOUT,
        )

        if resp.status_code != 200:
            logger.warning("OpenAlex returned %d for %r", resp.status_code, clean)
            return _empty()

        items = resp.json().get("results", [])
        if not items:
            logger.info("OpenAlex: no results for %r", clean)
            return _empty()

        author = items[0]
        stats  = author.get("summary_stats", {})

        logger.info(
            "OpenAlex: found %r | citations=%d | h_index=%d",
            author.get("display_name"),
            author.get("cited_by_count", 0),
            stats.get("h_index", 0),
        )

        # Get research areas — no level filter
        concepts = sorted(
            author.get("x_concepts", []),
            key=lambda c: c.get("score", 0),
            reverse=True,
        )
        research_areas = [
            c["display_name"] for c in concepts[:5]
            if c.get("display_name")
        ]

        # Fallback to topics
        if not research_areas:
            topics = author.get("topics", [])
            research_areas = [
                t["display_name"] for t in topics[:5]
                if t.get("display_name")
            ]

        logger.info("OpenAlex: research_areas=%s", research_areas)

        return {
            "research_areas":         research_areas,
            "total_citations":        author.get("cited_by_count", 0),
            "h_index":                stats.get("h_index", 0),
            "i10_index":              stats.get("i10_index", 0),
            "citations_last_5_years": stats.get("2yr_mean_citedness", 0),
        }

    except Exception as exc:
        logger.warning("OpenAlex failed for %r: %s", name, exc)
        return _empty()


def derive_areas_from_publications(publications: list[dict]) -> list[str]:
    """
    Extract clinical research areas from publication titles.
    More specific than OpenAlex concepts for medical specialties.
    """
    found = {}  # area → count (to rank by frequency)
    for pub in publications[:15]:
        title = (pub.get("title") or "").lower()
        for keyword, area in MEDICAL_TOPIC_KEYWORDS.items():
            if keyword in title:
                found[area] = found.get(area, 0) + 1

    # Sort by frequency, return top 5
    sorted_areas = sorted(found.items(), key=lambda x: x[1], reverse=True)
    return [area for area, _ in sorted_areas[:5]]


def _empty() -> dict:
    return {
        "research_areas":         [],
        "total_citations":        0,
        "h_index":                0,
        "i10_index":              0,
        "citations_last_5_years": 0,
    }
