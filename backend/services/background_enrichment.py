"""
background_enrichment.py
────────────────────────
Fixed version:
  1. Reduced concurrent enrichment from 3 → 2 (Semantic Scholar rate limit)
  2. Added 1 second delay between each physician enrichment
  3. Never skips due to empty disease string
"""

from __future__ import annotations

import asyncio
import logging

from services.physician_insights_service import enrich_physician
from services import ai_cache_service as cache

logger = logging.getLogger(__name__)

# Reduced to 2 — Semantic Scholar free tier allows ~1 req/sec
# With 2 concurrent tasks each making ~3 SS calls, we stay within limits
_MAX_CONCURRENT = 2

# Delay between starting each physician enrichment (seconds)
# Prevents Semantic Scholar 429 bursts
_ENRICH_DELAY = 1.0


async def enrich_one(
    npi:          str,
    name:         str,
    specialty:    str,
    disease:      str,
    groq_api_key: str,
    npi_state:    str = "",
) -> None:
    """Enrich a single physician and store in cache."""
    if not npi:
        return

    if cache.exists(npi, disease):
        logger.debug("Skip (cached) NPI=%s disease=%r", npi, disease)
        return

    try:
        data = await enrich_physician(
            npi          = npi,
            name         = name,
            specialty    = specialty,
            disease      = disease,
            groq_api_key = groq_api_key,
            npi_state    = npi_state,
        )
        cache.set(npi, disease, data)
        logger.info("Enrichment complete NPI=%s name=%r", npi, name)
    except Exception as exc:
        logger.warning("Enrichment failed NPI=%s name=%r: %s", npi, name, exc)


async def enrich_batch(
    physicians:   list[dict],
    disease:      str,
    groq_api_key: str,
) -> None:
    """
    Enrich a batch of physicians with rate-limit-aware staggered execution.
    Adds a delay between each physician to avoid Semantic Scholar 429 bursts.
    """
    if not physicians:
        return

    effective_disease = disease.strip() if disease else "clinical_trial"
    valid = [p for p in physicians if p.get("npi")]

    if not valid:
        return

    logger.info(
        "Background enrichment starting | %d physicians | disease=%r",
        len(valid), effective_disease,
    )

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

    async def _bounded(p: dict, index: int) -> None:
        # Stagger start times — physician 0 starts immediately,
        # physician 1 starts after 1s, physician 2 after 2s, etc.
        # This prevents all physicians from hitting Semantic Scholar at once
        await asyncio.sleep(index * _ENRICH_DELAY)

        async with semaphore:
            await enrich_one(
                npi          = p.get("npi", ""),
                name         = p.get("name", "Unknown Physician"),
                specialty    = p.get("taxonomy_desc", "Physician"),
                disease      = effective_disease,
                groq_api_key = groq_api_key,
                npi_state    = p.get("state", ""),
            )

    tasks = [_bounded(p, i) for i, p in enumerate(valid)]
    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info(
        "Background enrichment done | %d physicians | cache_size=%d",
        len(valid), cache.size(),
    )
