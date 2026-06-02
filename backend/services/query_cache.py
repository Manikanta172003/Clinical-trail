"""
query_cache.py
==============
In-memory cache for AI-corrected search queries.

Why this exists:
  Every user search hits Bytez + optionally Groq to correct the query.
  If two users search "hart attak" in the same session, we should not
  call Bytez twice for the same input. Cache the correction for 24 hours.

Storage: In-memory dict (works locally + on Render).
TTL: 24 hours per entry.
Key: lowercased + stripped original query string.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 24 * 60 * 60   # 24 hours

# Structure: { "hart attak": { "corrected": "heart attack", "ts": float } }
_CACHE: dict[str, dict] = {}


def get_cached_query(raw_query: str) -> Optional[str]:
    """
    Return cached corrected query string, or None if not cached / expired.
    """
    key = raw_query.lower().strip()
    entry = _CACHE.get(key)

    if entry is None:
        return None

    age = time.time() - entry.get("ts", 0)
    if age > CACHE_TTL_SECONDS:
        del _CACHE[key]
        logger.debug("Query cache expired for %r", key)
        return None

    logger.info(
        "Query cache HIT | original=%r → corrected=%r | age=%.0fh",
        key, entry["corrected"], age / 3600,
    )
    return entry["corrected"]


def set_cached_query(raw_query: str, corrected_query: str) -> None:
    """
    Store a corrected query in cache.
    Only stores if correction is different from original.
    """
    key = raw_query.lower().strip()

    if key == corrected_query.lower().strip():
        # No correction happened — no need to cache
        return

    _CACHE[key] = {
        "corrected": corrected_query,
        "ts": time.time(),
    }
    logger.info(
        "Query cache SET | original=%r → corrected=%r",
        key, corrected_query,
    )


def cache_stats() -> dict:
    """Debug helper — returns current cache size."""
    return {
        "total_entries": len(_CACHE),
        "ttl_hours": CACHE_TTL_SECONDS // 3600,
    }
