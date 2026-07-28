"""Extractor plugin interface.

Each site plugin implements ``matches`` (does this extractor handle the
given listing URL?) and ``iter_video_urls`` (paginate the listing and yield
individual video page URLs). ``crawler.py`` only talks to this interface;
it never knows about individual sites.
"""

from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable


@runtime_checkable
class Extractor(Protocol):
    """Interface every site plugin must satisfy."""

    #: Domain(s) this extractor is responsible for, e.g. {"rule34video.com"}.
    domains: frozenset[str]

    def matches(self, url: str) -> bool:
        """Return True if this extractor can handle the given listing URL."""
        ...

    def iter_video_urls(self, listing_url: str, *, max_items: int | None = None) -> Iterator[str]:
        """Paginate the listing starting at listing_url, yielding video page URLs.

        Stops early once max_items have been yielded, if given.
        """
        ...


class ExtractorError(Exception):
    """Raised when a listing page can't be parsed by its extractor."""
