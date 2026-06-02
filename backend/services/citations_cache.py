"""
citations_cache.py
==================
In-memory citations cache with 7-day TTL + background refresh.

Why this exists:
  Semantic Scholar returns different author matches on every search,
  causing total_citations to fluctuate (e.g. 365 → 448 → 312).
  Caching per NPI locks in a stable value for 7 days.

Storage: In-memory dict (works locally + on Render).
  Future upgrade path: swap _CACHE dict for Redis calls —
  only _get() and _set() need to change, nothing else.

Usage in physician_insights_service.py:
  from services.citations_cache import get_cached_citations, set_cached_citations, refresh_citations_background

TTL: 7 days
Stale strategy: return stale immediately + trigger background refetch
  → User always gets instant response
  → Fresh data ready for next search
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache TTL
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60   # 7 days

# ---------------------------------------------------------------------------
# Internal storage
# Replace this dict with Redis calls in future for Render/production.
# Structure: { npi: { "citations": int, "h_index": int, "ts": float } }
# ---------------------------------------------------------------------------
_CACHE: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Internal helpers — only these two functions touch _CACHE directly.
# Swap these for Redis to upgrade storage without touching anything else.
# ---------------------------------------------------------------------------

def _get(npi: str) -> Optional[dict]:
    """Return raw cache entry or None."""
    return _CACHE.get(npi)


def _set(npi: str, citations: int, h_index: int) -> None:
    """Write entry to cache with current timestamp."""
    _CACHE[npi] = {
        "citations": citations,
        "h_index":   h_index,
        "ts":        time.time(),
    }
    logger.info(
        "Citations cache SET | NPI=%s | citations=%d | h_index=%d",
        npi, citations, h_index,
    )


def _is_fresh(entry: dict) -> bool:
    """True if entry is within TTL window."""
    age = time.time() - entry.get("ts", 0)
    return age < CACHE_TTL_SECONDS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_cached_citations(npi: str) -> Optional[dict]:
    """
    Return cached citations for this NPI, or None if not cached at all.

    If the entry exists but is stale (>7 days), returns the stale data
    anyway so the caller can respond immediately.
    The caller should then trigger refresh_citations_background().

    Returns dict with keys: citations, h_index, is_stale (bool)
    Returns None if NPI has never been cached.
    """
    entry = _get(npi)
    if entry is None:
        logger.debug("Citations cache MISS | NPI=%s", npi)
        return None

    is_stale = not _is_fresh(entry)
    age_hours = (time.time() - entry["ts"]) / 3600

    if is_stale:
        logger.info(
            "Citations cache STALE | NPI=%s | age=%.1fh | "
            "returning stale data, background refresh will run",
            npi, age_hours,
        )
    else:
        logger.info(
            "Citations cache HIT | NPI=%s | citations=%d | h_index=%d | age=%.1fh",
            npi, entry["citations"], entry["h_index"], age_hours,
        )

    return {
        "citations": entry["citations"],
        "h_index":   entry["h_index"],
        "is_stale":  is_stale,
    }


def set_cached_citations(npi: str, citations: int, h_index: int) -> None:
    """
    Store fresh citation data for this NPI.
    Call this after a successful Semantic Scholar / OpenAlex fetch.
    """
    _set(npi, citations, h_index)


def refresh_citations_background(
    npi:      str,
    name:     str,
    specialty: str,
    fetch_fn,           # async callable: fetch_fn(name, specialty) → (citations, h_index)
) -> None:
    """
    Fire-and-forget background task to refresh stale cache entry.
    Wraps the async fetch in a task so the caller is never blocked.

    Usage:
        if cached and cached["is_stale"]:
            refresh_citations_background(npi, name, specialty, _fetch_citations)
    """
    async def _run():
        try:
            logger.info(
                "Citations background refresh START | NPI=%s name=%r",
                npi, name,
            )
            citations, h_index = await fetch_fn(name, specialty)
            if citations > 0 or h_index > 0:
                _set(npi, citations, h_index)
                logger.info(
                    "Citations background refresh DONE | NPI=%s | "
                    "citations=%d | h_index=%d",
                    npi, citations, h_index,
                )
            else:
                logger.info(
                    "Citations background refresh returned zeros for NPI=%s — "
                    "keeping stale data",
                    npi,
                )
        except Exception as exc:
            logger.warning(
                "Citations background refresh FAILED | NPI=%s | error=%s",
                npi, exc,
            )

    # Schedule as a background task — never blocks the caller
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_run())
        else:
            logger.warning(
                "No running event loop for background refresh NPI=%s", npi
            )
    except Exception as exc:
        logger.warning("Could not schedule background refresh: %s", exc)


def cache_stats() -> dict:
    """
    Debug helper — returns current cache stats.
    Call via: GET /api/debug/citations-cache (optional endpoint)
    """
    total   = len(_CACHE)
    fresh   = sum(1 for e in _CACHE.values() if _is_fresh(e))
    stale   = total - fresh

    return {
        "total_entries": total,
        "fresh":         fresh,
        "stale":         stale,
        "ttl_days":      CACHE_TTL_SECONDS // 86400,
    }
