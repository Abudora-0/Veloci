"""Domain-agnostic crawl orchestration.

Looks up the right extractor for a listing URL and delegates pagination
and link extraction to it. This module never knows about individual sites.
"""

from __future__ import annotations

from typing import Iterator

from veloci_engine.extractors.registry import find_extractor


class UnsupportedSiteError(Exception):
    def __init__(self, url: str) -> None:
        super().__init__(f"no extractor registered for {url}")
        self.url = url


def crawl_listing(listing_url: str, *, max_items: int | None = None) -> Iterator[str]:
    """Yield video page URLs discovered from a listing/category/tag URL."""
    extractor = find_extractor(listing_url)
    if extractor is None:
        raise UnsupportedSiteError(listing_url)

    yield from extractor.iter_video_urls(listing_url, max_items=max_items)
