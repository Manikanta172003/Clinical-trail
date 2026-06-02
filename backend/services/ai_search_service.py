"""
ai_search_service.py
=====================
Phase 1 — AI Search Intelligence.

Intercepts every user search query BEFORE it reaches ClinicalTrials.gov.
Corrects spelling mistakes, resolves layman terms, expands abbreviations.

Pipeline (3 layers, each is a fallback for the previous):
  Layer 1 — Text Cleaning   (always runs, instant, no API)
  Layer 2 — Groq LLM        (AI understands meaning — "sugar disease" → "diabetes mellitus")
  Layer 3 — RapidFuzz       (last resort spelling fix — "lukemia" → "leukemia")

Returns:
  QueryResult(
      original_query   = "hart attak",
      corrected_query  = "heart attack",
      was_corrected    = True,
      correction_layer = "groq",
  )
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from rapidfuzz import fuzz, process

from services.query_cache import get_cached_query, set_cached_query

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GROQ_API_KEY = os.getenv("GROQ_API_KEY") or "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"
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = 8.0

# Hard wall — if AI correction takes longer than this, skip it and
# search with the original query immediately. User never waits more
# than this for results regardless of Groq cold start or network lag.
MAX_CORRECTION_SECONDS = 5.0

# RapidFuzz score threshold — raised to 88 to prevent false matches
# on multi-word terms (e.g. "Glycogen Storage Disease" matched COPD at 85)
FUZZY_SCORE_THRESHOLD = 88

# Only run RapidFuzz on short queries (1-2 words max)
# Multi-word queries like "Glycogen Storage Disease" are almost certainly
# already valid medical terms — fuzzy matching causes false positives
FUZZY_MAX_WORDS = 2


# ── Known correct medical terms for RapidFuzz (Layer 3 safety net) ───────────
# Not a synonym dictionary — just correct spellings for fuzzy matching.
# Meaning/synonym resolution is handled by Groq (Layer 2).
MEDICAL_TERMS = [
    "heart attack", "heart failure", "myocardial infarction",
    "atrial fibrillation", "coronary artery disease", "hypertension",
    "hypotension", "stroke", "diabetes mellitus", "type 2 diabetes",
    "type 1 diabetes", "leukemia", "lymphoma", "multiple myeloma",
    "lung cancer", "breast cancer", "prostate cancer", "colorectal cancer",
    "melanoma", "glioblastoma", "pancreatic cancer", "ovarian cancer",
    "kidney cancer", "bladder cancer", "thyroid cancer", "liver cancer",
    "Parkinson's disease", "Alzheimer's disease", "multiple sclerosis",
    "amyotrophic lateral sclerosis", "epilepsy", "migraine",
    "chronic obstructive pulmonary disease", "asthma", "pulmonary fibrosis",
    "pneumonia", "tuberculosis", "chronic kidney disease",
    "inflammatory bowel disease", "crohn's disease", "ulcerative colitis",
    "rheumatoid arthritis", "systemic lupus erythematosus", "psoriasis",
    "anemia", "deep vein thrombosis", "pulmonary embolism",
    "non-alcoholic fatty liver disease", "cirrhosis", "hepatitis",
    "HIV", "sepsis", "obesity", "osteoporosis", "scoliosis",
    "macular degeneration", "glaucoma", "cataracts",
    "anxiety disorder", "depression", "schizophrenia", "bipolar disorder",
    "post-traumatic stress disorder",
    "attention deficit hyperactivity disorder",
    "autism spectrum disorder", "eating disorder",
    "heart failure with preserved ejection fraction",
    "heart failure with reduced ejection fraction",
    "transcatheter aortic valve replacement",
    "percutaneous coronary intervention",
    "coronary artery bypass grafting",
    "transient ischemic attack",
    "venous thromboembolism",
    "non-small cell lung cancer",
    "small cell lung cancer",
    "diffuse large b-cell lymphoma",
    "chronic lymphocytic leukemia",
    "acute myeloid leukemia",
    "myelodysplastic syndrome",
    "obstructive sleep apnea",
    "gastroesophageal reflux disease",
    "benign prostatic hyperplasia",
    "nonalcoholic steatohepatitis",
    "immune thrombocytopenia",
]


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class QueryResult:
    original_query:   str
    corrected_query:  str
    was_corrected:    bool
    correction_layer: str  # "cache" | "groq" | "fuzzy" | "none"


# ── Layer 1: Text Cleaning ────────────────────────────────────────────────────

def _clean_text(query: str) -> str:
    """
    Strip extra whitespace and special characters.
    Preserve hyphens (non-small-cell) and apostrophes (Parkinson's).
    """
    cleaned = query.strip()
    cleaned = re.sub(r"[^\w\s\-\']", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ── Layer 2: Groq LLM Correction ─────────────────────────────────────────────

async def _groq_correct(query: str, client: httpx.AsyncClient) -> Optional[str]:
    """
    Ask Groq to normalize the query to the standard medical term.
    Handles spelling mistakes, layman terms, and abbreviations.

    Examples:
      "hart attak"   → "heart attack"
      "sugar disease" → "diabetes mellitus"
      "HFpEF"        → "heart failure with preserved ejection fraction"
      "weak heart"   → "heart failure"
      "lukemia"      → "leukemia"
      "als"          → "amyotrophic lateral sclerosis"
    """
    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY not set — skipping Groq correction layer")
        return None

    prompt = (
        "You are a spelling corrector for a clinical trial search engine.\n\n"
        "Your ONLY job is to fix spelling mistakes and expand abbreviations.\n"
        "NEVER translate common English terms to medical jargon.\n\n"
        "Rules:\n"
        "- Fix spelling ONLY: 'hart attak' -> 'heart attack'\n"
        "- Expand abbreviations: 'HFpEF' -> 'heart failure with preserved ejection fraction'\n"
        "- If already correct English, return EXACTLY as-is\n"
        "- 'heart attack' stays 'heart attack' — do NOT change to myocardial infarction\n"
        "- 'stroke' stays 'stroke' — do NOT change to cerebrovascular accident\n"
        "- 'diabetes' stays 'diabetes' — do NOT change to diabetes mellitus\n"
        "- 'cancer' stays 'cancer'\n"
        "- Return ONLY the result. No explanation. No extra words. Max 10 words.\n\n"
        f"Input: {query}\n"
        "Output:"
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
                "max_tokens":  25,
                "temperature": 0.0,
                "messages":    [{"role": "user", "content": prompt}],
            },
            timeout=GROQ_TIMEOUT,
        )

        if resp.status_code == 429:
            logger.warning("Groq rate limit hit during query correction")
            return None
        if resp.status_code != 200:
            logger.warning("Groq correction returned %d for %r", resp.status_code, query)
            return None

        corrected = resp.json()["choices"][0]["message"]["content"].strip()

        # Sanity checks
        if not corrected:
            return None
        if len(corrected) > 120:
            logger.warning("Groq returned too-long correction for %r — ignoring", query)
            return None
        if corrected.lower().strip() == query.lower().strip():
            # No change — return None so we don't cache a no-op
            return None

        logger.info(
            "Groq correction | original=%r → corrected=%r",
            query, corrected,
        )
        return corrected

    except Exception as exc:
        logger.warning("Groq correction failed for %r: %s", query, exc)
        return None


# ── Layer 3: RapidFuzz Safety Net ────────────────────────────────────────────

def _fuzzy_correct(query: str) -> Optional[str]:
    """
    Last resort fuzzy string matching against known medical terms.
    Catches pure spelling mistakes that Groq might miss or rate-limit.

    "lukemia"   → "leukemia"      (score 93)
    "astma"     → "asthma"        (score 91)

    GUARD: Only runs on 1-2 word queries.
    Multi-word terms like "Glycogen Storage Disease" skip this layer
    entirely — they are almost certainly already valid medical terms
    and fuzzy matching produces dangerous false positives.
    """
    word_count = len(query.strip().split())
    if word_count > FUZZY_MAX_WORDS:
        logger.debug(
            "RapidFuzz skipped — %d words (max %d): %r",
            word_count, FUZZY_MAX_WORDS, query,
        )
        return None

    result = process.extractOne(
        query.lower(),
        [t.lower() for t in MEDICAL_TERMS],
        scorer=fuzz.WRatio,
        score_cutoff=FUZZY_SCORE_THRESHOLD,
    )

    if result is None:
        return None

    matched_lower = result[0]
    score         = result[1]

    # Restore original casing from MEDICAL_TERMS list
    corrected = next(
        (t for t in MEDICAL_TERMS if t.lower() == matched_lower),
        matched_lower,
    )

    if corrected.lower().strip() == query.lower().strip():
        return None

    logger.info(
        "RapidFuzz correction | original=%r → corrected=%r score=%d",
        query, corrected, score,
    )
    return corrected


# ── Main Entry Point ──────────────────────────────────────────────────────────

async def correct_query(raw_query: str) -> QueryResult:
    """
    Main entry point — call this from trials.py before hitting ClinicalTrials.gov.

    Usage in trials.py:
        from services.ai_search_service import correct_query
        result = await correct_query(condition)
        search_condition = result.corrected_query

    Returns QueryResult with:
        original_query   — what user typed
        corrected_query  — what we send to ClinicalTrials.gov
        was_corrected    — True if any correction happened
        correction_layer — "cache" | "groq" | "fuzzy" | "none"
    """
    if not raw_query or not raw_query.strip():
        return QueryResult(
            original_query=raw_query,
            corrected_query=raw_query,
            was_corrected=False,
            correction_layer="none",
        )

    cleaned = _clean_text(raw_query)

    # ── Check cache first — skip all API calls if already corrected ───────────
    cached = get_cached_query(cleaned)
    if cached:
        return QueryResult(
            original_query=raw_query,
            corrected_query=cached,
            was_corrected=cached.lower() != cleaned.lower(),
            correction_layer="cache",
        )

    corrected: Optional[str] = None
    layer_used = "none"

    async def _run_ai_layers() -> tuple[Optional[str], str]:
        """Run Groq + RapidFuzz layers. Wrapped in timeout by caller."""
        async with httpx.AsyncClient(
            headers={"User-Agent": "ClinTrialNavigator/1.0"},
            follow_redirects=True,
        ) as client:
            # Layer 2: Groq
            result = await _groq_correct(cleaned, client)
            if result:
                return result, "groq"

        # Layer 3: RapidFuzz (no network — always fast)
        result = _fuzzy_correct(cleaned)
        if result:
            return result, "fuzzy"

        return None, "none"

    try:
        corrected, layer_used = await asyncio.wait_for(
            _run_ai_layers(),
            timeout=MAX_CORRECTION_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "AI correction timed out after %.1fs for %r — using original query",
            MAX_CORRECTION_SECONDS, raw_query,
        )
        corrected = None
        layer_used = "timeout"

    # ── Final result ──────────────────────────────────────────────────────────
    final_query   = corrected if corrected else cleaned
    was_corrected = bool(corrected) and (final_query.lower() != cleaned.lower())

    # Cache successful corrections for next time
    if was_corrected:
        set_cached_query(cleaned, final_query)

    logger.info(
        "AI Search | original=%r → final=%r | layer=%s | corrected=%s",
        raw_query, final_query, layer_used, was_corrected,
    )

    return QueryResult(
        original_query=raw_query,
        corrected_query=final_query,
        was_corrected=was_corrected,
        correction_layer=layer_used,
    )
